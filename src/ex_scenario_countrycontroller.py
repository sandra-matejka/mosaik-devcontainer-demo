"""
Country controller scenario (AT/DE/IT) – unified architecture.

This scenario demonstrates a unified modelling pattern across countries:
  1) Grid (pandapower via mosaik-components)
  2) Controllable loads:
       Load profile (current)  -> Policy (country-specific) -> LoadAct (common actuator) -> ControlledGen (negative P)
  3) PV penetration:
       PVProfile -> PV Controller -> ControlledGen (positive P)

Notes:
- We currently use the Grid-Load entities as profile sources (P[MW]).
  If we disable those loads in the grid, their P output becomes 0 in this simulator version.
  For a production setup we recommend using external CSV profiles and then disabling the grid loads.
- ControlledGen is used as a generic "injection" element. Negative P represents consumption.
"""

import random
import warnings
from typing import cast

import mosaik
import mosaik.util
from mosaik.async_scenario import SimConfig

warnings.simplefilter(action="ignore", category=FutureWarning)
warnings.simplefilter(action="ignore", category=UserWarning)

# -----------------------------------------------------------------------------
# Simulation configuration
# -----------------------------------------------------------------------------
SIMULATION_DURATION = 24 * 3600  # seconds (1 day)
STEP = 900  # seconds (15 minutes)

# PV penetration parameters (applied to the set of controllable loads)
LOADS_PER_PV = 3            # how many controllable loads share one PV plant (aggregation)
PV_PENETRATION = 0.50       # 0..1 fraction of PV groups to instantiate
PV_PEAK_PER_LOAD_KW = 5.0   # PV peak per load (kW)
RANDOM_SEED = 42            # reproducible grouping/selection


def chunk(lst, n):
    """Yield successive chunks of size n from a list."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def create_load_policy(world, country, sims, rated_mw, state=None):
    """
    Create a country-specific *policy entity* for controllable loads.

    The unified interface expected by the actuator (LoadAct) is:
      - outputs: p_limit_mw, trip_cmd, tripped
      - optional input: p_request_mw (ignored by some policies)

    Args:
        world: mosaik world instance.
        country: "DE" | "IT" | "AT".
        sims: dict of started simulators.
        rated_mw: nominal/baseline load power in MW (used for defaults).
        state: optional DE state schedule entity.

    Returns:
        Policy entity instance.
    """
    if country == "DE":
        pol = sims["depol"].DEDirectControlPolicy(
            min_power_kw=4.2,
            default_limit_kw=rated_mw * 1000.0,
            state_limits_kw={0: None, 1: 8.0, 2: 6.0, 3: 4.2},
        )
        # DE uses time-windowed state schedule (e.g., 17:15–20:15 -> state 3).
        if state is not None:
            world.connect(state, pol, ("control_state", "control_state"))
        return pol

    if country == "IT":
        # Italian contracted-power logic (contract + headroom + optional trip behaviour)
        return sims["itpol"].ITPolicyLimit(
            p_contract_kw=3.0,
            headroom=0.10,
            trip_delay_s=180,
            reconnect_delay_s=60,
            mode="hard_trip",
        )

    if country == "AT":
        # Austria: no curtailment for controllable loads (policy is "no limit").
        return sims["atpol"].ATNoLimitPolicy()

    raise ValueError(f"Unsupported country: {country}")


def build_sim_config(country: str) -> SimConfig:
    """
    Build mosaik SIM_CONFIG for a given country.

    This function keeps the scenario code identical across countries; only the
    country-specific policy simulator(s) are toggled here.
    """
    sim_config = {
        "hdf5": {"python": "mosaik_hdf5:MosaikHdf5"},
        "Grid": {"python": "mosaik_components.pandapower:Simulator"},
        "LoadAct": {"python": "controller_grid.controllable_load_actuator:ControllableLoadActuatorSim"},
        "PVProfile": {"python": "controller_grid.pv_profile_sim:PVProfileSim"},
        "Policy": {"python": "simulator_curtailment_policy.policy_sim:PolicySim"},
        "Ctrl": {"python": "controller_grid.country_ctrl_sim:CountryControllerSim"},
    }

    if country == "DE":
        sim_config["State"] = {"python": "simulator_curtailment_policy.state_schedule_sim:StateScheduleSim"}
        sim_config["DEPolicy"] = {"python": "simulator_curtailment_policy.de_direct_control_sim:DEDirectControlPolicySim"}
    elif country == "IT":
        sim_config["ITPolicy"] = {"python": "simulator_curtailment_policy.it_contract_limit_sim:ITPolicyLimitSim"}
    elif country == "AT":
        sim_config["ATPolicy"] = {"python": "simulator_curtailment_policy.at_nolimit_policy_sim:ATNoLimitPolicySim"}

    return cast(SimConfig, sim_config)


# -----------------------------------------------------------------------------
# Scenario entry point
# -----------------------------------------------------------------------------
country = "AT"
SIM_CONFIG = build_sim_config(country)

with mosaik.World(SIM_CONFIG) as world:
    # -------------------------------------------------------------------------
    # Start simulators
    # -------------------------------------------------------------------------
    sims = {}
    sims["db"] = world.start("hdf5", step_size=STEP, duration=SIMULATION_DURATION)
    sims["pp"] = world.start("Grid", step_size=STEP)
    sims["loadact"] = world.start("LoadAct", step_size=STEP)
    sims["pvsim"] = world.start("PVProfile", step_size=STEP)
    sims["polsim"] = world.start("Policy", step_size=STEP)
    sims["ctrl"] = world.start("Ctrl", step_size=STEP, country=country)

    if country == "DE":
        sims["statesim"] = world.start("State", step_size=STEP)
        sims["depol"] = world.start("DEPolicy", step_size=STEP)
    if country == "AT":
        sims["atpol"] = world.start("ATPolicy", step_size=STEP)
    if country == "IT":
        sims["itpol"] = world.start("ITPolicy", step_size=STEP)

    # -------------------------------------------------------------------------
    # Instantiate grid + result database
    # -------------------------------------------------------------------------
    grid = sims["pp"].Grid(simbench="1-LV-rural2--0-sw")
    hdf5 = sims["db"].Database(filename=f"simresults_ex_scenario_{country.lower()}_controller.hdf5")

    # PV curtailment policy placeholder (constant scaling 0..1)
    pv_policy = sims["polsim"].ContractPolicy(curtailment_cmd=1)

    # DE-only: time-windowed control state (used by DE load policy)
    state = None
    if country == "DE":
        state = sims["statesim"].StateSchedule(
            state_normal=0,
            state_event=3,
            event_start_h=17.25,  # 17:15
            event_end_h=20.25,    # 20:15
        )

    # -------------------------------------------------------------------------
    # Collect grid entities (mosaik entity tree)
    # -------------------------------------------------------------------------
    bus_entities = []
    load_entities = []
    staticgen_entities = []
    line_entities = []
    externalgrid_entities = []

    for ent in grid.children:
        match ent.type:
            case "Bus":
                bus_entities.append(ent)
            case "Load":
                load_entities.append(ent)
            case "StaticGen":
                staticgen_entities.append(ent)
            case "Line":
                line_entities.append(ent)
            case "ExternalGrid":
                externalgrid_entities.append(ent)
            case _:
                print(f"Unknown entity type: {ent.type}")

    # -------------------------------------------------------------------------
    # Get pandapower net snapshot for bus/load index mapping
    # (Pylance may not know the exact type here; at runtime this is a pandapower net.)
    # -------------------------------------------------------------------------
    net = sims["pp"].get_net()  # type: ignore[attr-defined]

    bus_by_idx = {int(bus.eid.split("-")[1]): bus for bus in bus_entities}
    load_by_idx = {int(ld.eid.split("-")[1]): ld for ld in load_entities}

    # Here we currently treat *all* grid loads as controllable devices.
    # If you want §14a realism, filter by device rating instead of all loads.
    controllable_load_idxs = [int(i) for i in net.load.index if int(i) in load_by_idx]  # type: ignore[attr-defined]
    print("Controllable loads:", len(controllable_load_idxs))

    # Containers for logging and later processing
    policy_entities = []
    dev_entities = []
    loadgen_entities = []

    # -------------------------------------------------------------------------
    # Build controllable loads (unified: Request -> Policy -> Actuator -> Grid)
    # -------------------------------------------------------------------------
    for load_idx in controllable_load_idxs:
        bus_idx = int(net.load.at[load_idx, "bus"])  # type: ignore[attr-defined]
        rated_mw = float(net.load.at[load_idx, "p_mw"])  # type: ignore[attr-defined]

        # Country policy (provides p_limit_mw, trip_cmd, tripped)
        pol = create_load_policy(world, country, sims, rated_mw=rated_mw, state=state)

        # Common actuator (computes p_set and exports p_grid_mw = -p_set_mw)
        dev = sims["loadact"].ControllableLoad()

        # Grid representation of controllable consumption (negative injection)
        load_gen = sims["pp"].ControlledGen(bus=bus_idx)

        # Requested power currently comes from the original grid-load profile output.
        # (If those loads are disabled, this becomes 0; use CSV profiles in production.)
        src_load = load_by_idx[load_idx]
        world.connect(src_load, dev, ("P[MW]", "p_request_mw"))

        # Optional: provide request to the policy (some policies ignore it; Italy may use it for trip logic)
        world.connect(src_load, pol, ("P[MW]", "p_request_mw"))

        # Policy outputs -> actuator inputs
        world.connect(pol, dev, ("p_limit_mw", "p_limit_mw"))
        world.connect(pol, dev, ("trip_cmd", "trip_cmd"))

        # Actuator -> grid (break feedback cycles with time_shifted)
        world.connect(
            dev,
            load_gen,
            ("p_grid_mw", "P[MW]"),
            time_shifted=True,
            initial_data={"p_grid_mw": 0.0},
        )

        policy_entities.append(pol)
        dev_entities.append(dev)
        loadgen_entities.append(load_gen)

    # NOTE: Disabling the original grid loads avoids double counting, but also sets Load.P to 0 in this simulator.
    # For CSV-based request profiles, you can safely enable the disable_elements(...) block again.

    # -------------------------------------------------------------------------
    # PV penetration based on controllable loads (not on all grid loads)
    # -------------------------------------------------------------------------
    load_idxs_for_pv = controllable_load_idxs[:]
    random.seed(RANDOM_SEED)
    random.shuffle(load_idxs_for_pv)

    groups = list(chunk(load_idxs_for_pv, LOADS_PER_PV))
    random.shuffle(groups)
    groups = groups[: int(round(PV_PENETRATION * len(groups)))]

    print(f"Loads total: {len(load_entities)}")
    print(f"PV groups (LOADS_PER_PV={LOADS_PER_PV}): {len(groups)} selected (penetration={PV_PENETRATION})")

    pvs = []
    gen_ctrls = []
    pv_peak_per_load_mw = PV_PEAK_PER_LOAD_KW / 1000.0

    for group in groups:
        rep_load = group[0]
        bus_idx = int(net.load.at[rep_load, "bus"])  # type: ignore[attr-defined]

        pv_peak_mw = pv_peak_per_load_mw * len(group)
        pv = sims["pvsim"].PVProfile(p_peak_mw=pv_peak_mw)
        pvs.append(pv)

        pv_gen = sims["pp"].ControlledGen(bus=bus_idx)

        pv_ctrl = sims["ctrl"].GenCtrl(
            sn_mva=max(0.001, pv_peak_mw),
            pn_mw=max(0.001, pv_peak_mw),
        )
        gen_ctrls.append(pv_ctrl)

        world.connect(bus_by_idx[bus_idx], pv_ctrl, ("Vm[pu]", "vm"))
        world.connect(pv, pv_ctrl, ("p_available", "p_available"))
        world.connect(pv_policy, pv_ctrl, ("curtailment_cmd", "curtailment_cmd"))

        world.connect(
            pv_ctrl,
            pv_gen,
            ("p_set", "P[MW]"),
            time_shifted=True,
            initial_data={"p_set": 0.0},
        )

    # -------------------------------------------------------------------------
    # Logging (HDF5)
    # -------------------------------------------------------------------------
    mosaik.util.connect_many_to_one(world, bus_entities, hdf5, "P[MW]", "Q[MVar]", "Vm[pu]", "Va[deg]")
    mosaik.util.connect_many_to_one(world, staticgen_entities, hdf5, "P[MW]", "Q[MVar]")
    mosaik.util.connect_many_to_one(world, line_entities, hdf5, "I[kA]", "loading[%]")
    mosaik.util.connect_many_to_one(world, externalgrid_entities, hdf5, "P[MW]", "Q[MVar]")

    if pvs:
        mosaik.util.connect_many_to_one(world, pvs, hdf5, "p_available")
    if gen_ctrls:
        mosaik.util.connect_many_to_one(world, gen_ctrls, hdf5, "p_set", "q_set")

    mosaik.util.connect_many_to_one(world, policy_entities, hdf5, "p_limit_mw", "trip_cmd", "tripped")
    mosaik.util.connect_many_to_one(
        world,
        dev_entities,
        hdf5,
        "p_request_mw",
        "p_limit_mw",
        "trip_cmd",
        "p_set_mw",
        "p_grid_mw",
        "reduction_mw",
        "reduction_pct",
    )

    world.run(until=SIMULATION_DURATION)