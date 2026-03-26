"""
Controllable load actuator (country-agnostic).

This simulator is the shared "actuator" layer for all countries. It translates:
  - requested consumption (p_request_mw)
  - policy constraints (p_limit_mw, trip_cmd)

into:
  - actual consumption (p_set_mw)
  - grid injection for pandapower ControlledGen (p_grid_mw = -p_set_mw)
  - reduction metrics (reduction_mw, reduction_pct)

Key semantics:
- p_request_mw: requested import power (MW), >= 0
- p_limit_mw: maximum allowed import power (MW); use +inf for "no limit"
- trip_cmd: hard disconnect command (0/1). If 1, p_set_mw is forced to 0.
- p_grid_mw: negative injection representing consumption (MW)

This keeps scenario wiring identical across AT/DE/IT:
  Request source -> Policy -> Actuator -> Grid element
"""

from __future__ import annotations

import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "ControllableLoad": {
            "public": True,
            "params": [],
            "attrs": [
                # Inputs
                "p_request_mw",
                "p_limit_mw",
                "trip_cmd",
                # Outputs
                "p_set_mw",
                "p_grid_mw",
                "reduction_mw",
                "reduction_pct",
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


class ControllableLoadActuatorSim(mosaik_api.Simulator):
    """Shared actuator that applies limit/trip constraints to a load request."""

    def __init__(self):
        super().__init__(META)
        self._ents: dict[str, dict] = {}
        self.step_size: int = 1

    def init(self, sid, time_resolution=1.0, **sim_params):
        """Store scenario step size so this simulator advances consistently."""
        self.step_size = int(sim_params.get("step_size", 1))
        return self.meta

    def create(self, num, model, **params):
        """Create `num` ControllableLoad entities."""
        res = []
        for _ in range(num):
            eid = f"{model}_{len(self._ents)}"
            self._ents[eid] = {
                # store last inputs for logging/debugging
                "p_request_mw": 0.0,
                "p_limit_mw": float("inf"),
                "trip_cmd": 0,
                # outputs
                "p_set_mw": 0.0,
                "p_grid_mw": 0.0,
                "reduction_mw": 0.0,
                "reduction_pct": 0.0,
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        """
        Apply policy constraints.

        Rule order:
          1) trip_cmd (hard disconnect) overrides everything -> p_set_mw = 0
          2) otherwise apply limit: p_set_mw = min(p_request_mw, p_limit_mw)
        """
        for eid, st in self._ents.items():
            p_req = 0.0
            p_lim = float("inf")
            trip_cmd = 0

            if eid in inputs:
                if "p_request_mw" in inputs[eid]:
                    for _src, v in inputs[eid]["p_request_mw"].items():
                        p_req = _to_float(v, 0.0)

                if "p_limit_mw" in inputs[eid]:
                    for _src, v in inputs[eid]["p_limit_mw"].items():
                        p_lim = _to_float(v, float("inf"))

                if "trip_cmd" in inputs[eid]:
                    for _src, v in inputs[eid]["trip_cmd"].items():
                        trip_cmd = _to_int(v, 0)

            # Persist last inputs for logging
            st["p_request_mw"] = max(0.0, p_req)
            st["p_limit_mw"] = max(0.0, p_lim) if p_lim != float("inf") else float("inf")
            st["trip_cmd"] = 1 if trip_cmd else 0

            # Apply policy
            if st["trip_cmd"] == 1:
                p_set = 0.0
            else:
                if st["p_limit_mw"] != float("inf"):
                    p_set = min(st["p_request_mw"], st["p_limit_mw"])
                else:
                    p_set = st["p_request_mw"]

            st["p_set_mw"] = p_set
            st["p_grid_mw"] = -p_set  # consumption as negative injection

            # Reduction relative to the request
            st["reduction_mw"] = max(0.0, st["p_request_mw"] - p_set)
            if st["p_request_mw"] > 1e-12:
                st["reduction_pct"] = max(0.0, min(1.0, 1.0 - (p_set / st["p_request_mw"])))
            else:
                st["reduction_pct"] = 0.0

        return time + self.step_size

    def get_data(self, outputs):
        """Return requested attributes for mosaik/hdf5 logging."""
        return {eid: {a: self._ents[eid][a] for a in attrs} for eid, attrs in outputs.items()}