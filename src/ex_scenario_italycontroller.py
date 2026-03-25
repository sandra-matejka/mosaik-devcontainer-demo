import warnings
import mosaik
import mosaik.util
import random

warnings.simplefilter(action="ignore", category=FutureWarning)
warnings.simplefilter(action="ignore", category=UserWarning)

SIM_CONFIG: mosaik.SimConfig = {
    "hdf5": {"python": "mosaik_hdf5:MosaikHdf5"},
    "Grid": {"python": "mosaik_components.pandapower:Simulator"},
    "LoadAct": {"python": "controller_grid.controllable_load_actuator:ControllableLoadActuatorSim"},
    "PVProfile": {"python": "controller_grid.pv_profile_sim:PVProfileSim"},
    "Policy": {"python": "simulator_curtailment_policy.policy_sim:PolicySim"},
    "ITPolicy": {"python": "simulator_curtailment_policy.it_contract_limit_sim:ITPolicyLimitSim"},
    "Ctrl": {"python": "controller_grid.country_ctrl_sim:CountryControllerSim"},
}

SIMULATION_DURATION = 24 * 3600  # 1 day
STEP = 900  # 15 min

# ---- PV penetration parameters ----
LOADS_PER_PV = 3          # <--- wie viele Loads teilen sich eine PV
PV_PENETRATION = 0.50     # <--- 0..1 Anteil der PV-Gruppen
PV_PEAK_PER_LOAD_KW = 5.0  # <--- PV-peak pro Load (kW)
RANDOM_SEED = 42          # <--- für reproduzierbare Auswahl


def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


with mosaik.World(SIM_CONFIG) as world:
    # Start simulators
    db = world.start("hdf5", step_size=STEP, duration=SIMULATION_DURATION)
    pp = world.start("Grid", step_size=STEP)
    loadact = world.start("LoadAct", step_size=STEP)
    pvsim = world.start("PVProfile", step_size=STEP)
    polsim = world.start("Policy", step_size=STEP)
    itpol = world.start("ITPolicy", step_size=STEP)
    ctrl = world.start("Ctrl", step_size=STEP, country="IT")

    grid = pp.Grid(simbench="1-LV-rural2--0-sw")
    hdf5 = db.Database(filename="simresults_ex_scenario_italycontroller.hdf5")

    # Policy (global/shared)
    policy = polsim.ContractPolicy(curtailment_cmd=1)

    # Collect entities
    bus_entities = []
    load_entities = []
    staticgen_entities = []
    line_entities = []
    externalgrid_entities = []
    policy_entities = []
    dev_entities = []
    loadgen_entities = []   # ControlledGen, die den Verbrauch ins Grid bringt

    for entity in grid.children:
        match entity.type:
            case "Bus":
                bus_entities.append(entity)
            case "Load":
                load_entities.append(entity)
            case "StaticGen":
                staticgen_entities.append(entity)
            case "Line":
                line_entities.append(entity)
            case "ExternalGrid":
                externalgrid_entities.append(entity)
            case _:
                print(f"Unknown entity type: {entity.type}")

    # --- Mapping from pandapower net ---
    net = pp.get_net()

    # index -> entity
    bus_by_idx = {int(bus.eid.split("-")[1]): bus for bus in bus_entities}
    load_by_idx = {int(load.eid.split("-")[1]): load for load in load_entities}

    # alle Loads werden Controllable Loads
    controllable_load_idxs = [int(i) for i in net.load.index if int(i) in load_by_idx] # type: ignore
    print("Controllable loads:", len(controllable_load_idxs))

    for li in controllable_load_idxs:
        bus_idx = int(net.load.at[li, "bus"])  # type: ignore
        rated_mw = float(net.load.at[li, "p_mw"])  # type: ignore # als "requested" baseline

        # 1) IT Curtailment
        pol = itpol.ITPolicyLimit(
            p_contract_kw=3.0,
            headroom=0.10,
            trip_delay_s=180,
            reconnect_delay_s=60,
            mode="hard_trip",  # "hard_trip" oder "limit"
        )

        # 2) Actuator (setzt tatsächliche Leistung)
        dev = loadact.ControllableLoad()

        # 3) “Last” als ControlledGen (negatives P)
        load_gen = pp.ControlledGen(bus=bus_idx)

        # 4) Sollwert kommt aus ursprünglichem Load-Profil (P[MW])
        src_load = load_by_idx[li]
        world.connect(src_load, dev, ("P[MW]", "p_request_mw"))
        world.connect(src_load, pol,  ("P[MW]", "p_request_mw"))   # <-- Policy braucht es für hard_trip timing


        # 5) Limit kommt von der Policy
        # Policy -> Actuator
        world.connect(pol, dev, ("p_limit_mw", "p_limit_mw"))
        world.connect(pol, dev, ("trip_cmd", "trip_cmd"))

        # 6) Actuator -> Grid (Verbrauch als negatives P)
        world.connect(
            dev, load_gen,
            ("p_grid_mw", "P[MW]"),
            time_shifted=True,
            initial_data={"p_grid_mw": 0.0},
        )

        policy_entities.append(pol)
        dev_entities.append(dev)
        loadgen_entities.append(load_gen)

    # Ursprüngliche Loads im Grid deaktivieren, damit sie nicht doppelt zählen
    # --> dann funktioniert das logging nicht mehr, in Zukunft wäre es sowieso sinnvoll stattdessen Lastprofile einzuspielen
    # existing_load_eids = {l.eid for l in load_entities}
    # disable_eids = [f"Load-{i}" for i in controllable_load_idxs if f"Load-{i}" in existing_load_eids]
    # pp.disable_elements(disable_eids)

    # 1) controllable loads shufflen und gruppieren
    lidxs = controllable_load_idxs[:]
    random.seed(RANDOM_SEED)
    random.shuffle(lidxs)
    groups = list(chunk(lidxs, LOADS_PER_PV))

    # # letzte unvollständige Gruppe behalten,
    # # damit es nie 0 wird.
    # # drop: groups = [g for g in groups if len(g) == LOADS_PER_PV]

    # 2) penetration
    random.shuffle(groups)
    groups = groups[: int(round(PV_PENETRATION * len(groups)))]

    print(f"Loads total: {len(load_entities)}")
    print(
        f"PV groups (LOADS_PER_PV={LOADS_PER_PV}): {len(groups)} selected (penetration={PV_PENETRATION})")

    # # --- Build PV+Controller+ControlledGen per group ---
    pvs = []
    gen_ctrls = []

    pv_peak_per_load_mw = PV_PEAK_PER_LOAD_KW / 1000.0

    # 3) pro Gruppe PV am Bus des ersten Loads
    for g in groups:
        rep_load = g[0]
        bus_idx = int(net.load.at[rep_load, "bus"])  # type: ignore

        # PV profile for this group
        pv_peak_mw = pv_peak_per_load_mw * len(g)
        pv = pvsim.PVProfile(p_peak_mw=pv_peak_mw)
        pvs.append(pv)

        # Controlled generator at same bus (injects into grid)
        ctrl_gen = pp.ControlledGen(bus=bus_idx)

        # Controller for this PV
        c = ctrl.GenCtrl(
            # Defaults / ratings
            # p_set_const=0.003,
            sn_mva=max(0.001, pv_peak_mw),  # grober Startwert
            pn_mw=max(0.001, pv_peak_mw),
        )
        gen_ctrls.append(c)

        # Grid -> Controller (voltage)
        world.connect(bus_by_idx[bus_idx], c, ("Vm[pu]", "vm"))

        # PV profile -> Controller
        world.connect(pv, c, ("p_available", "p_available"))

        # Policy -> Controller (curtailment_cmd 0..1)
        world.connect(policy, c, ("curtailment_cmd", "curtailment_cmd"))

        # Controller -> Grid (active power setpoint)
        world.connect(
            c, ctrl_gen,
            ("p_set", "P[MW]"),
            time_shifted=True,
            initial_data={"p_set": 0.0},
        )

    # --- Logging (grid + loads + etc.) ---
    mosaik.util.connect_many_to_one(
        world, bus_entities, hdf5, "P[MW]", "Q[MVar]", "Vm[pu]", "Va[deg]")
    # mosaik.util.connect_many_to_one(world, load_entities, hdf5, "P[MW]", "Q[MVar]")
    mosaik.util.connect_many_to_one(
        world, staticgen_entities, hdf5, "P[MW]", "Q[MVar]")
    mosaik.util.connect_many_to_one(
        world, line_entities, hdf5, "I[kA]", "loading[%]")
    mosaik.util.connect_many_to_one(
        world, externalgrid_entities, hdf5, "P[MW]", "Q[MVar]")

    # PV + Controller outputs
    if pvs:
        mosaik.util.connect_many_to_one(world, pvs, hdf5, "p_available")
    if gen_ctrls:
        mosaik.util.connect_many_to_one(
            world, gen_ctrls, hdf5, "p_set", "q_set")
    
    # DSO-Limit (MW)
    mosaik.util.connect_many_to_one(world, policy_entities, hdf5, "p_limit_mw","trip_cmd","tripped")

    # Actuator: Wunsch, Limit, tatsächlich gezogen + Grid-Wert
    mosaik.util.connect_many_to_one(world, dev_entities, hdf5,
                                "p_request_mw", "p_limit_mw", "trip_cmd",
                                "p_set_mw", "p_grid_mw", "reduction_mw", "reduction_pct")

    world.run(until=SIMULATION_DURATION)
