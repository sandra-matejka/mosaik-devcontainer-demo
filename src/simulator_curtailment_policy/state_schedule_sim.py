"""
Control state schedule simulator.

This simulator outputs a discrete `control_state` based on the time of day.
It is typically used for Germany (§14a) to emulate DSO control windows.

Default example:
  - 17:15–20:15  -> state_event (e.g., 3)
  - otherwise    -> state_normal (e.g., 0)

The output can be connected to a DE policy simulator as:
  StateSchedule.control_state -> DEDirectControlPolicy.control_state
"""

from __future__ import annotations

import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "StateSchedule": {
            "public": True,
            "params": [
                "state_normal",   # e.g. 0
                "state_event",    # e.g. 3
                "event_start_h",  # e.g. 17.25  (17:15)
                "event_end_h",    # e.g. 20.25  (20:15)
            ],
            "attrs": ["control_state"],
        },
    },
}


class StateScheduleSim(mosaik_api.Simulator):
    """Outputs a discrete control_state according to a daily time window."""

    def __init__(self):
        super().__init__(META)
        self.step_size: int = 900
        self.entities: dict[str, dict] = {}

    def init(self, sid, time_resolution=1.0, **sim_params):
        self.step_size = int(sim_params.get("step_size", self.step_size))
        return self.meta

    def create(self, num, model, **params):
        res = []
        for _ in range(num):
            eid = f"{model}_{len(self.entities)}"
            self.entities[eid] = {
                "state_normal": int(params.get("state_normal", 0)),
                "state_event": int(params.get("state_event", 3)),
                "event_start_h": float(params.get("event_start_h", 17.25)),
                "event_end_h": float(params.get("event_end_h", 20.25)),
                "control_state": int(params.get("state_normal", 0)),
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        """
        Update the control_state based on time-of-day.

        Note: This supports event windows crossing midnight (start > end).
        """
        hour = (time / 3600.0) % 24.0

        for eid, st in self.entities.items():
            start = st["event_start_h"] % 24.0
            end = st["event_end_h"] % 24.0

            # Window does not cross midnight
            if start <= end:
                in_event = start <= hour < end
            # Window crosses midnight (e.g., 22:15–06:30)
            else:
                in_event = (hour >= start) or (hour < end)

            st["control_state"] = st["state_event"] if in_event else st["state_normal"]

        return time + self.step_size

    def get_data(self, outputs):
        return {eid: {"control_state": self.entities[eid]["control_state"]} for eid in outputs}