"""
Italy contracted power policy (potenza impegnata + tolerance + optional meter trip).

This is a *policy* simulator that outputs:
  - p_limit_mw: contracted import limit (MW) including headroom (tolerance)
  - trip_cmd:   0/1 hard disconnect command (only in "hard_trip" mode)
  - tripped:    0/1 internal status for logging

Inputs:
  - p_request_mw: requested import power (MW); used for trip timing in hard_trip mode

Modes:
  - "limit":     Only provides p_limit_mw; never trips.
  - "hard_trip": Trips (trip_cmd=1) if p_request_mw stays above the limit for
                 trip_delay_s seconds; reconnects after reconnect_delay_s below limit.

Important:
- The actual limiting/tripping is applied by the common actuator (LoadAct).
  The policy only outputs constraints and status.
"""

from __future__ import annotations

import mosaik_api_v3 as mosaik_api

META = {
    "type": "time-based",
    "models": {
        "ITPolicyLimit": {
            "public": True,
            "params": [
                "p_contract_kw",
                "headroom",
                "trip_delay_s",
                "reconnect_delay_s",
                "mode",  # "hard_trip" or "limit"
            ],
            "attrs": [
                # Input
                "p_request_mw",
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


class ITPolicyLimitSim(mosaik_api.Simulator):
    """Contracted-power policy with optional meter-like trip behavior."""

    def __init__(self):
        super().__init__(META)
        self.step_size = 900
        self.ents: dict[str, dict] = {}

    def init(self, sid, time_resolution=1.0, **sim_params):
        self.step_size = int(sim_params.get("step_size", 900))
        return self.meta

    def create(self, num, model, **params):
        res = []
        for _ in range(num):
            eid = f"{model}_{len(self.ents)}"
            p_contract_kw = float(params.get("p_contract_kw", 3.0))
            headroom = float(params.get("headroom", 0.10))
            trip_delay_s = int(params.get("trip_delay_s", 180))
            reconnect_delay_s = int(params.get("reconnect_delay_s", 60))
            mode = str(params.get("mode", "hard_trip"))

            p_limit_mw = (p_contract_kw * (1.0 + headroom)) / 1000.0

            self.ents[eid] = {
                "p_contract_kw": p_contract_kw,
                "headroom": headroom,
                "trip_delay_s": trip_delay_s,
                "reconnect_delay_s": reconnect_delay_s,
                "mode": mode,
                # internal timers
                "over_s": 0,
                "under_s": 0,
                # status
                "tripped": 0,
                # I/O for logging
                "p_request_mw": 0.0,
                "p_limit_mw": p_limit_mw,
                "trip_cmd": 0,
            }
            res.append({"eid": eid, "type": model})
        return res

    def step(self, time, inputs, max_advance=None):
        dt = self.step_size

        for eid, st in self.ents.items():
            # Read request (used for trip logic)
            p_req = 0.0
            if eid in inputs and "p_request_mw" in inputs[eid]:
                for _src, v in inputs[eid]["p_request_mw"].items():
                    p_req = _to_float(v, 0.0)
            st["p_request_mw"] = max(0.0, p_req)

            # Update limit (constant per entity, but recompute to reflect config changes)
            p_lim = (st["p_contract_kw"] * (1.0 + st["headroom"])) / 1000.0
            st["p_limit_mw"] = p_lim

            if st["mode"] == "limit":
                # No trip in this mode; the actuator will apply the cap.
                st["tripped"] = 0
                st["trip_cmd"] = 0
                st["over_s"] = 0
                st["under_s"] = 0
            else:
                # Meter-like trip with delays
                if st["tripped"] == 0:
                    if p_req > p_lim:
                        st["over_s"] += dt
                        if st["over_s"] >= st["trip_delay_s"]:
                            st["tripped"] = 1
                            st["under_s"] = 0
                    else:
                        st["over_s"] = 0
                else:
                    if p_req <= p_lim:
                        st["under_s"] += dt
                        if st["under_s"] >= st["reconnect_delay_s"]:
                            st["tripped"] = 0
                            st["over_s"] = 0
                    else:
                        st["under_s"] = 0

                st["trip_cmd"] = 1 if st["tripped"] else 0

        return time + self.step_size

    def get_data(self, outputs):
        return {eid: {a: self.ents[eid][a] for a in attrs} for eid, attrs in outputs.items()}