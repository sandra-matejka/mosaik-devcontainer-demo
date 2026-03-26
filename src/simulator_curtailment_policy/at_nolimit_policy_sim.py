"""
Austria no-limit load policy (compatibility policy).

This policy exists to keep a *unified country architecture* across AT/DE/IT:
  Request source -> Country Policy -> Shared Load Actuator -> Grid element

Austria currently does not apply a load curtailment mechanism in this project.
Therefore this simulator outputs:
  - p_limit_mw: very large number (effectively unlimited)
  - trip_cmd: 0
  - tripped: 0

It also exposes an optional p_request_mw input solely for interface compatibility
(it is ignored).
"""

from __future__ import annotations

import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "ATNoLimitPolicy": {
            "public": True,
            "params": ["p_limit_mw"],
            "attrs": [
                "p_request_mw",  # input (ignored)
                "p_limit_mw",    # output
                "trip_cmd",      # output
                "tripped",       # output
            ],
        }
    },
}


class ATNoLimitPolicySim(mosaik_api.Simulator):
    """Policy that never constrains load power (p_limit is effectively infinite)."""

    def __init__(self):
        super().__init__(META)
        self.step_size: int = 900
        self.ents: dict[str, dict] = {}

    def init(self, sid, time_resolution=1.0, **sim_params):
        self.step_size = int(sim_params.get("step_size", self.step_size))
        return self.meta

    def create(self, num, model, **params):
        res = []
        for _ in range(num):
            eid = f"{model}_{len(self.ents)}"
            self.ents[eid] = {
                # Use a large number rather than infinity to avoid downstream serialization issues.
                "p_limit_mw": float(params.get("p_limit_mw", 1e9)),
                "trip_cmd": 0,
                "tripped": 0,
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        # Constant outputs; no state updates needed.
        return time + self.step_size

    def get_data(self, outputs):
        return {eid: {a: self.ents[eid][a] for a in attrs} for eid, attrs in outputs.items()}