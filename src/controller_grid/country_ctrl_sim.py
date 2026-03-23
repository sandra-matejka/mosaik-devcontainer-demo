"""
Country-specific controller wrapper.

This module loads country parameter files from:
  controller/params/<COUNTRY>.yaml

The YAML is mapped into the flat parameter structure expected by BaseCtrlSim
(e.g. q_u curves, capability curves, voltage/frequency limits).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .base_ctrl import BaseCtrlSim


def _load_country_yaml(country: str) -> dict[str, Any]:
    """
    Load the YAML configuration for a given country code.

    Args:
        country: Country code, e.g. "AT", "DE", "IT" (case-insensitive).

    Returns:
        Parsed YAML as a Python dict.

    Raises:
        FileNotFoundError: If the config file is missing.
    """
    here = Path(__file__).resolve().parent
    cfg_path = here / "params" / f"{country.upper()}.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"No country config found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _flatten_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Map the structured YAML into the flat defaults dict used by BaseCtrlSim.

    The YAML structure is expected to contain:
      - voltage: min_pu, max_pu, time_profile
      - frequency: min_hz, max_hz, time_profile
      - control_defaults: controller parameters (e.g., q_u, cosphi_p, q_capability)

    Returns:
        A flat dict that can be merged into BaseCtrlSim.defaults.
    """
    v = cfg.get("voltage", {})
    f = cfg.get("frequency", {})
    cd = cfg.get("control_defaults", {})

    def prof_to_seconds(profile_items):
        """
        Convert time profile entries into (low, high, max_seconds) tuples.

        We store max duration in seconds so downstream logic can compare
        against simulation time steps.
        """
        out = []
        for item in profile_items:
            max_m = item.get("max_minutes", None)
            out.append(
                (
                    float(item["low"]),
                    float(item["high"]),
                    None if max_m is None else int(max_m * 60),
                )
            )
        return out

    return {
        # Voltage limits
        "Voltage_min_pu": float(v["min_pu"]),
        "Voltage_max_pu": float(v["max_pu"]),
        "Voltage_time_profile": prof_to_seconds(v["time_profile"]),
        # Frequency limits (only relevant if you model frequency explicitly)
        "Frequency_min_Hz": float(f["min_hz"]),
        "Frequency_max_Hz": float(f["max_hz"]),
        "Frequency_time_profile": prof_to_seconds(f["time_profile"]),
        # Controller defaults (e.g. Q(U), cosphi(P), capability curves)
        **cd,
    }


class CountryControllerSim(BaseCtrlSim):
    """
    Controller simulator with country-specific defaults loaded from YAML.

    Usage:
        ctrl = world.start("Ctrl", step_size=900, country="AT")
        c = ctrl.GenCtrl(...)
    """

    def init(self, sid, time_resolution=1.0, **sim_params):
        # Country selection
        country = sim_params.get("country", "AT")

        # Load and flatten YAML config
        cfg = _load_country_yaml(country)
        defaults = _flatten_cfg(cfg)

        # Update BaseCtrlSim defaults with YAML parameters
        self.defaults.update(defaults)

        # Allow overriding YAML defaults via `world.start(..., key=value)`
        # (excluding the country code itself).
        self.defaults.update({k: v for k, v in sim_params.items() if k != "country"})

        # Let BaseCtrlSim store step_size etc.
        return super().init(sid, time_resolution=time_resolution, **sim_params)