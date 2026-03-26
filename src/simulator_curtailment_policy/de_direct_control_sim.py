"""
Germany §14a / SteuVE direct control policy (simplified).

This is a *policy* simulator. It does not directly set grid power.
Instead it outputs:
  - p_limit_mw (MW): absolute import power limit to be enforced by the actuator
  - trip_cmd (always 0): included for unified wiring across countries
  - tripped (always 0): included for unified logging across countries

Inputs:
  - control_state (0..3): discrete DSO control state (optional)
  - p_limit_kw: explicit limit in kW (optional; takes precedence)
  - p_request_mw: optional request signal (ignored for control, can be logged)

Policy principle:
- Use absolute kW limits (not % curtailment).
- Enforce a minimum guaranteed power level (default 4.2 kW).
"""

from __future__ import annotations

import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "DEDirectControlPolicy": {
            "public": True,
            "params": [
                "min_power_kw",        # default 4.2
                "default_limit_kw",    # used if no signal is received
                "state_limits_kw",     # dict: state -> limit_kw
            ],
            "attrs": [
                # Inputs
                "control_state",
                "p_limit_kw",
                "p_request_mw",  # optional; used only for logging/debug
                # Outputs
                "p_limit_mw",
                "trip_cmd",
                "tripped",
            ],
        }
    },
}


def _to_float(v, default: float = 0.0) -> float:
    """Convert mosaik input values (scalar or list) to float."""
    if v is None:
        return default
    if isinstance(v, list):
        return float(v[-1]) if v else default
    return float(v)


def _to_int(v, default: int = 0) -> int:
    """Convert mosaik input values (scalar or list) to int."""
    if v is None:
        return default
    if isinstance(v, list):
        return int(v[-1]) if v else default
    return int(v)


class DEDirectControlPolicySim(mosaik_api.Simulator):
    """Policy that outputs a device-specific import power limit in MW."""

    def __init__(self):
        super().__init__(META)
        self._ents: dict[str, dict] = {}
        self.step_size: int = 1

    def init(self, sid, time_resolution=1.0, **sim_params):
        self.step_size = int(sim_params.get("step_size", 1))
        return self.meta

    def create(self, num, model, **params):
        res = []
        for _ in range(num):
            eid = f"{model}_{len(self._ents)}"
            default_limit_kw = float(params.get("default_limit_kw", 11.0))
            self._ents[eid] = {
                "min_power_kw": float(params.get("min_power_kw", 4.2)),
                "default_limit_kw": default_limit_kw,
                "state_limits_kw": params.get("state_limits_kw", {0: None, 1: 8.0, 2: 6.0, 3: 4.2}),
                # outputs
                "p_limit_mw": default_limit_kw / 1000.0,
                "trip_cmd": 0,
                "tripped": 0,
                # optional logging
                "p_request_mw": 0.0,
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        for eid, st in self._ents.items():
            limit_kw = None

            if eid in inputs and "p_request_mw" in inputs[eid]:
                for _src, v in inputs[eid]["p_request_mw"].items():
                    st["p_request_mw"] = _to_float(v, 0.0)

            if eid in inputs:
                # Explicit kW limit has priority
                if "p_limit_kw" in inputs[eid]:
                    for _src, v in inputs[eid]["p_limit_kw"].items():
                        limit_kw = _to_float(v)

                # Otherwise derive from the control state
                if limit_kw is None and "control_state" in inputs[eid]:
                    for _src, v in inputs[eid]["control_state"].items():
                        state = _to_int(v, 0)
                        limit_kw = st["state_limits_kw"].get(state, None)

            # If nothing was provided, use the default (no curtailment)
            if limit_kw is None:
                limit_kw = st["default_limit_kw"]

            # Minimum guaranteed power level
            limit_kw = max(float(limit_kw), st["min_power_kw"])

            st["p_limit_mw"] = float(limit_kw) / 1000.0
            st["trip_cmd"] = 0
            st["tripped"] = 0

        return time + self.step_size

    def get_data(self, outputs):
        return {eid: {a: self._ents[eid][a] for a in attrs} for eid, attrs in outputs.items()}