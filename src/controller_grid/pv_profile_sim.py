"""
PV availability profile simulator.

This simulator produces a simple "bell-shaped" daily PV availability curve
for each PV entity:

  p_available (MW)

Model assumptions:
- Sunrise at 06:00, sunset at 18:00 (fixed).
- PV availability follows a sine curve between sunrise and sunset.
- Peak availability is `p_peak_mw`.

Typical use:
  PVProfile.p_available -> PV controller input p_available
"""

from __future__ import annotations

import math
import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "PVProfile": {
            "public": True,
            "params": ["p_peak_mw"],
            "attrs": ["p_available"],
        },
    },
}


class PVProfileSim(mosaik_api.Simulator):
    """Time-based simulator producing PV available power per day."""

    def __init__(self):
        super().__init__(META)
        self.entities: dict[str, dict] = {}
        self.step_size: int = 1

    def init(self, sid, time_resolution=1.0, **sim_params):
        """
        Store scenario step size.

        Mosaik passes `step_size` at world.start(...). We advance by that step.
        """
        self.step_size = int(sim_params.get("step_size", 1))
        return self.meta

    def create(self, num, model, **params):
        """Create `num` PVProfile entities."""
        res = []
        for _ in range(num):
            eid = f"PVProfile_{len(self.entities)}"
            p_peak = float(params.get("p_peak_mw", 0.005))  # default 5 kW = 0.005 MW

            self.entities[eid] = {
                "p_peak": p_peak,
                "p_available": 0.0,
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        """
        Compute PV availability from time-of-day.

        Day model:
          - 06:00–18:00: sine bell curve
          - otherwise: 0
        """
        hour = (time / 3600.0) % 24.0

        for eid, st in self.entities.items():
            if 6.0 <= hour <= 18.0:
                # Map 6..18h to 0..pi for a smooth bell shape
                angle = math.pi * (hour - 6.0) / 12.0
                p = st["p_peak"] * math.sin(angle)
            else:
                p = 0.0

            st["p_available"] = max(0.0, float(p))

        return time + self.step_size

    def get_data(self, outputs):
        """Return requested outputs for mosaik/hdf5 logging."""
        return {
            eid: {a: self.entities[eid][a] for a in attrs}
            for eid, attrs in outputs.items()
        }