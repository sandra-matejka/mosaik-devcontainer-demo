"""
Base controller simulator for PV in mosaik.

This simulator computes active/reactive setpoints for a generator-like device.
It is designed to be country-agnostic; country-specific defaults are injected
via `CountryControllerSim` (YAML -> defaults).

Main I/O (per entity):
  Inputs (optional):
    - vm:            voltage magnitude at PCC (pu)
    - p_meas:         measured active power (MW) (fallback)
    - p_available:    available PV power (MW) from a profile simulator
    - curtailment_cmd:0..1 factor from a policy simulator (contract/market/tariff/rule-based)
    - f_hz:           grid frequency (Hz) (currently stored only; not used in power flow)
  Outputs:
    - p_set:          active power setpoint (MW)
    - q_set:          reactive power setpoint (MVAr)
    - frequency_hz:   last received frequency (Hz)

Conventions:
  - `curtailment_cmd` is interpreted as a fraction of `p_available`:
      p_req = curtailment_cmd * p_available
  - If `p_available` is not provided, the controller falls back to `p_meas`
    (or `p_set_const` if no measurement exists).
"""

import math
import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "GenCtrl": {
            "public": True,
            "params": [
                # --- core control ---
                "reactive_control_mode",

                # --- fixed modes ---
                "cosphi_fix",
                "q_fix_var",

                # --- limits / ratings ---
                "pn_mw",
                "sn_mva",
                "p_max_mw",
                "q_min",
                "q_max",

                # --- sign convention ---
                "cosphi_sign",

                # --- optional fallback ---
                "p_set_const",
            ],
            "attrs": [
                # Inputs
                "vm",
                "p_meas",
                "p_available",
                "curtailment_cmd",
                "f_hz",
                # Outputs
                "p_set",
                "q_set",
                "frequency_hz",
            ],
        },
    },
}


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp x into [lo, hi]."""
    return max(lo, min(hi, x))


def _q_from_cosphi(p_mw: float, cosphi: float, sign: float = 1.0) -> float:
    """
    Compute reactive power from active power and power factor.

    Q = P * tan(phi), phi = arccos(cosphi)
    If P is in MW, Q will be in MVAr (same base scaling).
    """
    cosphi = max(1e-6, min(1.0, float(cosphi)))
    phi = math.acos(cosphi)
    return sign * float(p_mw) * math.tan(phi)


def _interp_piecewise(x: float, xp: list[float], yp: list[float]) -> float:
    """
    Linear interpolation with saturation at the endpoints.

    Args:
      x:  query value
      xp: x breakpoints (monotonically increasing)
      yp: y values at breakpoints

    Returns:
      Interpolated y value.
    """
    if x <= xp[0]:
        return yp[0]
    if x >= xp[-1]:
        return yp[-1]

    for i in range(len(xp) - 1):
        if xp[i] <= x <= xp[i + 1]:
            x0, x1 = xp[i], xp[i + 1]
            y0, y1 = yp[i], yp[i + 1]
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return yp[-1]


def _q_circle_limit_mvar(p_mw: float, sn_mva: float) -> float:
    """
    Apparent power circle limit:
      Q_max = sqrt(S^2 - P^2)
    """
    s = float(sn_mva)
    p = float(p_mw)
    if s <= 0:
        return float("inf")
    v = s * s - p * p
    return math.sqrt(v) if v > 0 else 0.0


def _q_capability_limits_mvar(vm_pu: float, sn_mva: float, cap_cfg: dict) -> tuple[float, float]:
    """
    Convert voltage-dependent capability curves into absolute Q limits (MVAr).

    The YAML is expected to contain:
      q_capability:
        overexcited:   u_points, q_points   (Q positive)
        underexcited:  u_points, q_points   (Q negative)
    Values are per-unit on S_n (sn_mva).
    """
    if sn_mva <= 0 or cap_cfg is None:
        return (-float("inf"), float("inf"))

    over = cap_cfg.get("overexcited", {})
    under = cap_cfg.get("underexcited", {})

    uo = over.get("u_points", [])
    qo = over.get("q_points", [])
    uu = under.get("u_points", [])
    qu = under.get("q_points", [])

    qmax_rel = _interp_piecewise(vm_pu, uo, qo) if uo and qo else float("inf")
    qmin_rel = _interp_piecewise(vm_pu, uu, qu) if uu and qu else -float("inf")

    q_max = float(qmax_rel) * float(sn_mva)
    q_min = float(qmin_rel) * float(sn_mva)
    return (q_min, q_max)


def _p_u_factor(vm_pu: float, p_u_cfg: dict) -> float:
    """
    Volt-Watt limiter factor f(U) in [0,1].

    Expected config shape:
      p_u:
        enabled: true
        u_points: [...]
        p_points: [...]
    """
    if not p_u_cfg or not p_u_cfg.get("enabled", False):
        return 1.0
    u_pts = p_u_cfg.get("u_points", [1.10, 1.12])
    p_pts = p_u_cfg.get("p_points", [1.0, 0.0])
    fac = float(_interp_piecewise(vm_pu, u_pts, p_pts))
    return _clamp(fac, 0.0, 1.0)


def to_float(x, default: float = 0.0) -> float:
    """
    Robust conversion to float for mosaik inputs.

    Mosaik inputs can be:
      - scalar: 0.99
      - list/tuple: [0.99]
      - dict: {src_eid: [0.99]}  (depending on how you parse upstream)

    This helper extracts the "latest" value where possible.
    """
    if x is None:
        return default

    if isinstance(x, (list, tuple)):
        return float(x[-1]) if x else default

    if isinstance(x, dict):
        return float(list(x.values())[-1]) if x else default

    try:
        return float(x)
    except Exception:
        return default


# -----------------------------------------------------------------------------
# Simulator implementation
# -----------------------------------------------------------------------------
class BaseCtrlSim(mosaik_api.Simulator):
    """
    Generic PV controller simulator.

    A country-specific subclass can override `self.defaults` in init().
    """
    DEFAULTS = {
        "q_min": -0.3,
        "q_max": +0.3,
        "p_set_const": 0.0,
        # Optional fallback if no policy input is connected
        "curtailment_factor": 1.0,
    }

    def __init__(self, defaults=None):
        super().__init__(META)
        self.defaults = dict(self.DEFAULTS)
        if defaults:
            self.defaults.update(defaults)
        self._state: dict[str, dict] = {}
        self._step_size: int = 1

    def init(self, sid, time_resolution=1.0, **sim_params):
        """
        Initialize simulator.

        Mosaik may pass `step_size` here; we store it so `step()` advances
        consistently with the scenario step size.
        """
        self._step_size = int(sim_params.get("step_size", 1))
        # Allow overriding defaults via init() parameters if needed.
        self.defaults.update(sim_params)
        return self.meta

    def create(self, num, model, **params):
        created = []
        for _ in range(num):
            eid = f"{model}_{len(self._state)}"

            # Merge defaults with per-entity params.
            p = dict(self.defaults)
            p.update(params)

            self._state[eid] = {
                "p": p,
                "p_set": float(p.get("p_set_const", 0.0)),
                "q_set": 0.0,
                "frequency_hz": 50.0,
            }
            created.append({"eid": eid, "type": model})
        return created

    def step(self, time, inputs, max_advance=None):
        """
        Compute p_set and q_set for each entity.
        """
        for eid, st in self._state.items():
            params = st["p"]

            # ------------------------------------------------------
            # Input extraction (robust against mosaik input formats)
            # ------------------------------------------------------
            vm = None
            p_meas = None
            f_hz = 50.0
            p_available = None
            curtailment_cmd = None

            if eid in inputs:
                if "vm" in inputs[eid]:
                    for _src, series in inputs[eid]["vm"].items():
                        vm = to_float(series)

                if "p_meas" in inputs[eid]:
                    for _src, series in inputs[eid]["p_meas"].items():
                        p_meas = to_float(series)

                if "f_hz" in inputs[eid]:
                    for _src, series in inputs[eid]["f_hz"].items():
                        f_hz = to_float(series)

                if "p_available" in inputs[eid]:
                    for _src, series in inputs[eid]["p_available"].items():
                        p_available = to_float(series)

                if "curtailment_cmd" in inputs[eid]:
                    for _src, series in inputs[eid]["curtailment_cmd"].items():
                        curtailment_cmd = to_float(series)

            # Fallbacks if inputs are missing
            if p_meas is None:
                p_meas = float(params.get("p_set_const", 0.0))

            if curtailment_cmd is None:
                # If no policy is connected, use a constant factor from params.
                curtailment_cmd = float(params.get("curtailment_factor", 1.0))

            curtailment_cmd = _clamp(float(curtailment_cmd), 0.0, 1.0)
            st["frequency_hz"] = f_hz

            # ------------------------------------------------------
            # Active power control
            # ------------------------------------------------------
            if p_available is not None:
                # Policy scales the available PV power (0..1).
                p_req = curtailment_cmd * float(p_available)
            else:
                # Fallback: treat measured/constant power as "requested".
                p_req = float(p_meas)

            if vm is None:
                p_set = p_req
            else:
                # Optional Volt-Watt limiter
                pu_cfg = params.get("p_u", {})
                fac = _p_u_factor(float(vm), pu_cfg)

                pn_mw = float(params.get("pn_mw", params.get("p_max_mw", abs(p_req))))
                p_max_allowed = fac * pn_mw

                p_set = min(p_req, p_max_allowed) if p_req >= 0 else p_req

            st["p_set"] = float(p_set)
            p_mw = float(p_set)

            # ------------------------------------------------------
            # Reactive power control
            # ------------------------------------------------------
            mode = str(params.get("reactive_control_mode", "cosphi_fixed"))

            if mode == "q_fixed":
                q = float(params.get("q_fix_var", 0.0))

            elif mode == "cosphi_fixed":
                cosphi = float(params.get("cosphi_fix", 1.0))
                sign = float(params.get("cosphi_sign", 1.0))
                q = _q_from_cosphi(p_mw, cosphi, sign)

            elif mode == "cosphi_p":
                cfg = params.get("cosphi_p", {})
                p_points = cfg.get("p_points", [0.0, 0.5, 1.0])
                cosphi_points = cfg.get("cosphi_points", [1.0, 1.0, 0.9])

                p_max = float(params.get("p_max_mw", max(abs(p_mw), 1e-6)))
                p_rel = abs(p_mw) / p_max

                cosphi = _interp_piecewise(p_rel, p_points, cosphi_points)
                direction = cfg.get("direction", "underexcited")
                sign = 1.0 if direction == "underexcited" else -1.0

                q = _q_from_cosphi(p_mw, cosphi, sign)

            elif mode == "q_u":
                if vm is None:
                    q = 0.0
                else:
                    cfg = params.get("q_u", {})
                    u_points = cfg.get("u_points", [0.92, 0.96, 1.05, 1.08])
                    q_points = cfg.get("q_points", [1.0, 0.0, 0.0, -1.0])
                    q_max = float(cfg.get("q_max_mvar", 0.25))

                    q_rel = _interp_piecewise(float(vm), u_points, q_points)
                    q = q_rel * q_max

            else:
                # Safe fallback
                q = float(params.get("q_fix_var", 0.0))

            # ------------------------------------------------------
            # Apply limits (capability curve, apparent power circle, hard bounds)
            # ------------------------------------------------------
            sn_mva = float(params.get("sn_mva", 0.0))

            cap_cfg = params.get("q_capability")
            if vm is not None and cap_cfg and sn_mva > 0:
                q_min_cap, q_max_cap = _q_capability_limits_mvar(float(vm), sn_mva, cap_cfg)
                q = _clamp(float(q), q_min_cap, q_max_cap)

            if sn_mva > 0:
                q_circle = _q_circle_limit_mvar(p_mw, sn_mva)
                q = _clamp(float(q), -q_circle, q_circle)

            q_min = float(params.get("q_min", -999))
            q_max = float(params.get("q_max", 999))
            st["q_set"] = _clamp(float(q), q_min, q_max)

        # Advance by scenario step size if available.
        return time + self._step_size

    def get_data(self, outputs):
        """Return requested output attributes for mosaik."""
        data = {}
        for eid, attrs in outputs.items():
            st = self._state[eid]
            data[eid] = {}
            for a in attrs:
                if a == "p_set":
                    data[eid][a] = st["p_set"]
                elif a == "q_set":
                    data[eid][a] = st["q_set"]
                elif a == "frequency_hz":
                    data[eid][a] = st.get("frequency_hz", 50.0)
        return data