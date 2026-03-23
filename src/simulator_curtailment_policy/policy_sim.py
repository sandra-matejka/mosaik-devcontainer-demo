"""
Simple policy simulator (placeholder).

This module provides a minimal policy model that outputs a constant
`curtailment_cmd` (0..1). It is useful for:
  - smoke tests (end-to-end wiring)
  - baseline scenarios (constant curtailment factor)

If you need dynamic policies (tariff-/market-/voltage-driven), implement them
as dedicated PolicySims that output:
  - curtailment_cmd (0..1) for PV-style scaling, or
  - p_limit_mw / trip_cmd for controllable loads.
"""

from __future__ import annotations

import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "ContractPolicy": {
            "public": True,
            "params": ["curtailment_cmd"],
            "attrs": ["curtailment_cmd"],
        },
    },
}


class PolicySim(mosaik_api.Simulator):
    """Outputs a constant curtailment command (0..1)."""

    def __init__(self):
        super().__init__(META)
        self._ents: dict[str, dict] = {}
        self.step_size: int = 900  # default; overridden by mosaik via init()

    def init(self, sid, time_resolution=1.0, **sim_params):
        """
        Store scenario step size so this simulator advances in lock-step
        with the scenario (e.g., 900s for 15-minute steps).
        """
        self.step_size = int(sim_params.get("step_size", self.step_size))
        return self.meta

    def create(self, num, model, **params):
        """Create `num` constant policy entities."""
        res = []
        for _ in range(num):
            eid = f"{model}-{len(self._ents)}"
            self._ents[eid] = {
                # 1.0 means no curtailment, 0.0 means full curtailment
                "curtailment_cmd": float(params.get("curtailment_cmd", 1.0)),
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        # Policy is constant; no state updates required.
        return time + self.step_size

    def get_data(self, outputs):
        """Return requested attributes (primarily used by the HDF5 logger)."""
        return {eid: {a: self._ents[eid][a] for a in attrs} for eid, attrs in outputs.items()}