"""Parasitic extraction, OCV, library limits and min/max corner analysis."""

import os

import pytest

from silicon.library import (bundled_corners, default_library, fast_library,
                             read_library)
from silicon.parasitics import extract, read_spef_summary, write_spef
from silicon.sta import TimingConfig, TimingEngine, analyse_corners

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# -- libraries and corners --------------------------------------------------
def test_fast_corner_is_faster_and_leakier():
    slow = default_library()
    fast = fast_library()
    assert set(slow.cells) == set(fast.cells)
    for cell in ("INVX1", "NAND2X1", "DFFX1"):
        model_slow, model_fast = slow.cell(cell), fast.cell(cell)
        slow_arc = model_slow.arcs[0]
        fast_arc = model_fast.arcs[0]
        assert fast_arc.rise.lookup(0.05, 0.005)[0] < slow_arc.rise.lookup(0.05, 0.005)[0]
        assert model_slow.area == model_fast.area        # same cells, same area
        assert model_fast.leakage > model_slow.leakage


def test_corner_tags_are_derived_from_operating_conditions():
    assert default_library().corner == "ss_0p95V_125C"
    assert fast_library().corner == "ff_1p10V_m40C"
    bundled = bundled_corners()
    assert set(bundled) == {"max", "min"}


def test_corner_generator_reproduces_the_checked_in_library(tmp_path):
    """tools/make_corner.py is the source of truth for the fast corner."""
    import importlib.util

    path = os.path.join(ROOT, "tools", "make_corner.py")
    spec = importlib.util.spec_from_file_location("make_corner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    generated = module.build(os.path.join(ROOT, "silicon", "lib", "stdcells_32nm.lib"))
    target = tmp_path / "ff.lib"
    target.write_text(generated)
    checked_in = open(os.path.join(ROOT, "silicon", "lib", "stdcells_32nm_ff.lib"),
                      encoding="utf-8").read()
    assert target.read_text() == checked_in


# -- extraction -------------------------------------------------------------
def test_extraction_builds_rc_trees(routed_alu):
    parasitics = routed_alu["parasitics"]
    assert parasitics.nets
    for net, entry in parasitics.nets.items():
        assert entry.edges, net
        assert entry.total_capacitance > 0
        assert entry.total_resistance > 0
        assert entry.sink_delays
        assert all(value >= 0 for value in entry.sink_delays.values())
    stats = parasitics.summary()
    assert stats["nets"] == len(parasitics.nets)
    assert stats["cap"] > 0 and stats["res"] > 0


def test_extraction_splits_wires_at_junctions(routed_alu):
    """A net with several segments must come out as more than one RC edge."""
    parasitics = routed_alu["parasitics"]
    multi = [entry for entry in parasitics.nets.values() if len(entry.edges) >= 3]
    assert multi, "no multi-edge nets were extracted"
    for entry in multi[:5]:
        assert len(entry.nodes) >= len(entry.edges)


def test_more_wire_means_more_delay_and_capacitance():
    """Extraction must be monotone in geometry, not a constant."""
    from silicon.library import default_library as lib
    from silicon.netlist import GateCell, GateNetlist
    from silicon.pnr import Floorplan, Rectangle, RouteResult, Segment
    from silicon.rules import default_rules

    library = lib()
    netlist = GateNetlist(top="t")
    netlist.add_cell(GateCell(instance="u1", cell="BUFX1", x=0.0, y=0.0,
                              width=1.0, height=2.0,
                              inputs={"A": "a"}, outputs={"Y": "n"}))
    netlist.add_cell(GateCell(instance="u2", cell="BUFX1", x=20.0, y=0.0,
                              width=1.0, height=2.0,
                              inputs={"A": "n"}, outputs={"Y": "y"}))
    netlist.nets["n"] = {"u1/Y", "u2/A"}
    plan = Floorplan(die=Rectangle(0, 0, 30, 10), core=Rectangle(1, 1, 29, 9))
    short = RouteResult()
    short.segments.append(Segment(net="n", layer="M3", x1=0.5, y1=1.0, x2=20.5, y2=1.0))
    long = RouteResult()
    long.segments.append(Segment(net="n", layer="M3", x1=0.5, y1=1.0, x2=20.5, y2=1.0))
    long.segments.append(Segment(net="n", layer="M3", x1=0.5, y1=1.0, x2=0.5, y2=8.0))
    rules = default_rules()
    a = extract(netlist, library, plan, short, rules).nets["n"]
    b = extract(netlist, library, plan, long, rules).nets["n"]
    assert b.total_capacitance > a.total_capacitance
    assert b.total_resistance > a.total_resistance


def test_spef_round_trip(tmp_path, routed_alu):
    path = tmp_path / "alu.spef"
    write_spef(str(path), routed_alu["parasitics"], "alu")
    text = path.read_text()
    assert text.startswith("/* SPEF written by silicon-icc2")
    assert '*T_UNIT 1 NS' in text and '*C_UNIT 1 PF' in text
    assert "*D_NET" in text and "*RES" in text
    summary = read_spef_summary(str(path))
    expected = routed_alu["parasitics"].summary()
    assert summary["nets"] == expected["nets"]
    # SPEF stores six decimals, so compare at that precision
    assert summary["cap"] == pytest.approx(expected["cap"], abs=1e-4)
    assert summary["res"] == pytest.approx(expected["res"], abs=1e-2)


# -- timing with parasitics, OCV and corners --------------------------------
def test_parasitics_load_the_critical_path(routed_alu):
    """Extracted RC must make timing worse than no wire model at all."""
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    config = TimingConfig(clock_period=4.0)
    engine = TimingEngine(netlist, library, config)
    engine.set_clock_arrivals(routed_alu["cts"].sink_delays)
    without = engine.run()

    engine = TimingEngine(netlist, library, config)
    engine.set_clock_arrivals(routed_alu["cts"].sink_delays)
    with_rc = engine.run(parasitics=routed_alu["parasitics"])
    assert with_rc.wns < without.wns
    assert with_rc.wire_delay > 0


def test_ocv_derates_stretch_the_setup_path(routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    base = TimingConfig(clock_period=4.0, derate_cell_late=1.0, derate_cell_early=1.0,
                        derate_net_late=1.0, derate_net_early=1.0)
    engine = TimingEngine(netlist, library, base)
    engine.set_clock_arrivals(routed_alu["cts"].sink_delays)
    clean = engine.run(parasitics=routed_alu["parasitics"])
    assert clean.derated is False

    tight = TimingConfig(clock_period=4.0, derate_cell_late=1.20, derate_cell_early=0.80,
                         derate_net_late=1.20, derate_net_early=0.80)
    engine = TimingEngine(netlist, library, tight)
    engine.set_clock_arrivals(routed_alu["cts"].sink_delays)
    derated = engine.run(parasitics=routed_alu["parasitics"])
    assert derated.derated is True
    assert derated.wns < clean.wns


def test_library_transition_limit_is_used(routed_alu):
    """A design-level max_transition is honoured when nothing tighter exists."""
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    loose = TimingConfig(clock_period=4.0, max_transition=10.0)
    engine = TimingEngine(netlist, library, loose)
    assert engine.run(parasitics=routed_alu["parasitics"]).max_slew_violations == 0

    library_missing_limits = library
    tight = TimingConfig(clock_period=4.0, max_transition=1e-4)
    for cell in library_missing_limits.cells.values():
        for pin in cell.pins.values():
            pin.max_transition = 0.0        # force the design limit to be used
    try:
        engine = TimingEngine(netlist, library_missing_limits, tight)
        assert engine.run(parasitics=routed_alu["parasitics"]).max_slew_violations > 0
    finally:
        for cell in library_missing_limits.cells.values():
            for pin in cell.pins.values():
                pin.max_transition = 0.4


def test_min_max_analysis_takes_hold_from_the_fast_corner(routed_alu):
    netlist = routed_alu["netlist"]
    corners = bundled_corners()
    config = TimingConfig(clock_period=4.0)
    merged, power, per_corner = analyse_corners(netlist, corners, config,
                                                parasitics=routed_alu["parasitics"])
    assert set(per_corner) == {"max", "min"}
    slow, fast = per_corner["max"], per_corner["min"]
    # setup is worse at the slow corner, so that is what the merged result reports
    assert merged.wns == slow.wns
    assert merged.hold_wns == fast.hold_wns
    assert fast.wns > slow.wns                 # faster cells give more setup slack
    assert merged.corners == {"max": slow.corner, "min": fast.corner}
    assert power.total_mw > 0


def test_single_corner_analysis_still_works(routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    merged, power, per_corner = analyse_corners(netlist, {"max": library},
                                                TimingConfig(clock_period=4.0))
    assert set(per_corner) == {"max"}
    assert merged.hold_wns == per_corner["max"].hold_wns
    assert merged.corners == {"max": library.corner}


# -- OCV styles -------------------------------------------------------------
def test_aocv_factors_shrink_pessimism_with_depth(routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    config = TimingConfig(clock_period=4.0, ocv_mode="aocv", aocv_slope=0.06)
    engine = TimingEngine(netlist, library, config)

    # depth 1 is exactly the flat derate: no averaging has happened yet
    assert engine.derate("cell_late", 1) == pytest.approx(config.derate_cell_late)
    assert engine.derate("cell_early", 1) == pytest.approx(config.derate_cell_early)
    # late derates fall towards 1.0 with depth, early derates rise towards it
    late = [engine.derate("cell_late", depth) for depth in range(1, 25)]
    early = [engine.derate("net_early", depth) for depth in range(1, 25)]
    assert late == sorted(late, reverse=True) and late[-1] >= 1.0
    assert early == sorted(early) and early[-1] <= 1.0
    # deep paths are treated as ideal for the random component, not better than it
    assert engine.derate("cell_late", 1000) == pytest.approx(1.0)
    assert engine.derate("cell_early", 1000) == pytest.approx(1.0)


def test_aocv_recovers_slack_but_keeps_the_flop_derate(routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    parasitics = routed_alu["parasitics"]

    def run(mode):
        engine = TimingEngine(netlist, library,
                              TimingConfig(clock_period=4.0, ocv_mode=mode),
                              parasitics=parasitics)
        engine.set_clock_arrivals(routed_alu["cts"].sink_delays)
        return engine.run(parasitics=parasitics)

    flat, aocv = run("flat"), run("aocv")
    assert aocv.ocv_mode == "aocv" and aocv.derated
    assert aocv.max_depth > 3
    assert aocv.wns > flat.wns                  # less pessimism on a deep path
    assert aocv.hold_wns != flat.hold_wns
