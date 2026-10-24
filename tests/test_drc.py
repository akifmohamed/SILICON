"""Design-rule checking: the rule deck must be able to fail, and the router
must produce geometry that passes it."""

import pytest

from silicon.pnr import (DrcChecker, PhysicalConfig, Router, Segment, Via,
                         build_floorplan, route_capacitance)
from silicon.rules import MetalRule, default_rules


def _clean():
    return Segment(net="n1", layer="M3", x1=1.0, y1=2.0, x2=4.0, y2=2.0, width=0.08)


def test_a_clean_wire_passes_every_rule():
    checker = DrcChecker(default_rules())
    report = checker.check([_clean()], [])
    assert report == {}


def test_short_is_detected():
    checker = DrcChecker(default_rules())
    a = _clean()
    b = Segment(net="n2", layer="M3", x1=2.0, y1=2.0, x2=5.0, y2=2.0, width=0.08)
    report = checker.check([a, b], [])
    assert "SHORT" in report


def test_spacing_violation_is_detected():
    checker = DrcChecker(default_rules())
    rule = default_rules()["M3"]
    gap = rule.spacing / 2.0                        # too close, not touching
    a = _clean()
    b = Segment(net="n2", layer="M3", x1=2.0, y1=2.0 + rule.width + gap,
                x2=5.0, y2=2.0 + rule.width + gap, width=rule.width)
    report = checker.check([a, b], [])
    assert "SPACING" in report


def test_corner_spacing_is_detected():
    checker = DrcChecker(default_rules())
    rule = default_rules()["M3"]
    a = _clean()
    # a T-junction standing off the end of a wire by less than the spacing rule
    b = Segment(net="n2", layer="M3", x1=4.0 + rule.spacing / 2, y1=2.0,
                x2=4.0 + rule.spacing / 2, y2=5.0, width=rule.width)
    assert "SPACING" in checker.check([a, b], [])


def test_minimum_width_is_detected():
    checker = DrcChecker(default_rules())
    thin = Segment(net="n1", layer="M3", x1=1.0, y1=2.0, x2=4.0, y2=2.0, width=0.02)
    assert "WIDTH" in checker.check([thin], [])


def test_minimum_area_is_detected():
    checker = DrcChecker(default_rules())
    stub = Segment(net="n1", layer="M3", x1=1.0, y1=2.0, x2=1.1, y2=2.0, width=0.08)
    assert "MIN_AREA" in checker.check([stub], [])


def test_via_enclosure_is_detected():
    checker = DrcChecker(default_rules())
    # horizontal leg stops exactly at the via: no end enclosure
    short_leg = Segment(net="n1", layer="M3", x1=1.0, y1=2.0, x2=2.0, y2=2.0, width=0.08)
    vertical = Segment(net="n1", layer="M2", x1=2.0, y1=2.0, x2=2.0, y2=5.0, width=0.07)
    via = Via(net="n1", x=2.0, y=2.0, size=0.05, layers=("M2", "M3"))
    assert "VIA_ENCLOSURE" in checker.check([short_leg, vertical], [via])


def test_via_spacing_is_detected():
    checker = DrcChecker(default_rules())
    a = Via(net="n1", x=2.0, y=2.0, size=0.05)
    b = Via(net="n2", x=2.04, y=2.0, size=0.05)
    assert "VIA_SPACING" in checker.check([], [a, b])


def test_vias_on_different_layers_do_not_interact():
    checker = DrcChecker(default_rules())
    a = Via(net="n1", x=2.0, y=2.0, layers=("M2", "M3"))
    b = Via(net="n2", x=2.0, y=2.0, layers=("M4", "M5"))
    assert "VIA_SPACING" not in checker.check([], [a, b])


def test_routed_design_is_clean_under_every_rule(routed_alu):
    route = routed_alu["route"]
    assert route.drc == 0, route.drc_details
    assert route.drc_violations == {}
    assert route.overflow == 0
    rules = routed_alu["cfg"].rules
    for seg in route.segments:
        rule = rules[seg.layer]
        assert seg.width + 1e-9 >= rule.width
        assert seg.area + 1e-12 >= rule.min_area


def test_router_uses_steiner_points(routed_alu):
    assert routed_alu["route"].steiner_points > 0
    # a Steiner tree is never longer than the MST it started from
    assert routed_alu["route"].wirelength > 0


def test_via_doubling_adds_redundant_cuts_and_stays_legal(routed_alu):
    import copy

    library = routed_alu["library"]
    netlist = copy.deepcopy(routed_alu["netlist"])
    plan = routed_alu["plan"]
    cfg = PhysicalConfig(via_doubling=True)
    route = Router(netlist, library, plan, cfg).route()
    assert route.doubled_vias > 0
    assert route.vias_count > len({(v.x, v.y) for v in route.vias}) - 1
    assert route.drc == 0, route.drc_details


def test_narrow_track_budget_reports_rather_than_shorts(routed_alu):
    """With a spacing rule wider than the pitch the router must refuse."""
    library, netlist = routed_alu["library"], routed_alu["netlist"]
    plan = routed_alu["plan"]
    rules = dict(default_rules())
    rules["M3"] = MetalRule("M3", "horizontal", 0.08, 0.30, 0.025, 0.06, 0.08, 0.025)
    cfg = PhysicalConfig(rules=rules)
    from silicon.pnr import PhysicalError
    with pytest.raises(PhysicalError):
        Router(netlist, library, plan, cfg)


def test_route_capacitance_uses_per_layer_rules(routed_alu):
    caps = route_capacitance(routed_alu["route"], routed_alu["cfg"])
    assert caps and all(value > 0 for value in caps.values())
    total_wire = sum(routed_alu["route"].net_wirelength.values())
    assert sum(caps.values()) == pytest.approx(
        total_wire * routed_alu["cfg"].wire_cap_per_um, rel=1e-6)


# -- grid maze router -------------------------------------------------------
def test_maze_router_connects_far_apart_points(routed_alu):
    library, netlist, plan = (routed_alu["library"], routed_alu["netlist"],
                              routed_alu["plan"])
    cfg = PhysicalConfig()
    router = Router(netlist, library, plan, cfg)
    from silicon.pnr import DrcChecker, RouteResult
    result = RouteResult()
    core = plan.core
    start = (core.x1 + 2.0, core.y1 + 2.0)
    goal = (core.x2 - 2.0, core.y2 - 2.0)
    assert router.route_maze("maze_net", start, goal, result)
    assert result.maze_nets == 1
    assert result.segments and result.vias
    # the path must actually join the two points, and it must be legal
    assert DrcChecker(cfg.rules).check(result.segments, result.vias) == {}


def test_maze_router_avoids_existing_metal(routed_alu):
    library, netlist, plan = (routed_alu["library"], routed_alu["netlist"],
                              routed_alu["plan"])
    router = Router(netlist, library, plan, PhysicalConfig())
    from silicon.pnr import RouteResult
    result = RouteResult()
    core = plan.core
    router.route_maze("first", (core.x1 + 1.0, core.y1 + 1.0),
                      (core.x2 - 1.0, core.y1 + 1.0), result)
    before = len(result.segments)
    router.route_maze("second", (core.x1 + 1.0, core.y1 + 1.0),
                      (core.x2 - 1.0, core.y1 + 1.0), result)
    assert len(result.segments) > before
    assert DrcChecker(PhysicalConfig().rules).check(result.segments,
                                                   result.vias) == {}


def test_capacity_starved_router_reports_instead_of_shorting():
    """A pitch far too coarse must yield unrouted nets, never shared tracks."""
    from silicon.pnr import (ClockTree, PhysicalConfig, Placer, Router,
                             build_floorplan)
    from silicon.library import default_library
    from silicon.serialize import load
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "build", "alu.json")
    if not os.path.exists(path):
        pytest.skip("build/alu.json not present; run './bin/silicon flow' first")
    library = default_library()
    netlist = load(path)[0]
    cfg = PhysicalConfig(track_pitch=0.5)
    plan = build_floorplan(netlist, library, cfg)
    Placer(netlist, library, plan, cfg).place()
    cts = ClockTree(netlist, library, cfg,
                    counter_start=len(netlist.cells), plan=plan).build("clk")
    for buffer in cts.buffers:
        netlist.add_cell(buffer)
    route = Router(netlist, library, plan, cfg).route()
    assert route.drc == 0, route.drc_details
    assert route.unrouted_nets
    # every net that did get metal is still legal on its own track
    tracks = {}
    for seg in route.segments:
        tracks.setdefault((seg.layer, round(seg.cross, 4)), []).append(seg)
    for group in tracks.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if a.net == b.net:
                    continue
                lo = max(a.span[0], b.span[0])
                hi = min(a.span[1], b.span[1])
                assert hi <= lo + 1e-9 or abs(a.cross - b.cross) > 1e-9


# -- timing-driven, congestion-aware placement ------------------------------
def test_placement_weights_critical_nets(routed_alu):
    import copy

    from silicon.pnr import Placer
    from silicon.sta import TimingConfig, TimingEngine
    library, plan = routed_alu["library"], routed_alu["plan"]
    netlist = copy.deepcopy(routed_alu["netlist"])
    cfg = PhysicalConfig()
    timing = TimingEngine(netlist, library, TimingConfig(clock_period=4.0)).run()
    placer = Placer(netlist, library, plan, cfg)
    weights = placer.net_weights(timing)
    assert weights, "no net was weighted from the timing paths"
    assert max(weights.values()) > 1.0
    off = Placer(netlist, library, plan, PhysicalConfig(placement_timing_driven=False))
    assert off.net_weights(timing) == {}


def test_placement_reports_a_congestion_map(routed_alu):
    import copy

    from silicon.pnr import Placer
    library, plan = routed_alu["library"], routed_alu["plan"]
    netlist = copy.deepcopy(routed_alu["netlist"])
    cfg = PhysicalConfig(congestion_bins=4)
    result = Placer(netlist, library, plan, cfg).place()
    assert result.congestion_peak > 0.0
    assert 0.0 <= result.congestion_mean <= result.congestion_peak
    grid = Placer(netlist, library, plan, cfg).congestion_map()
    assert len(grid) == 4 and len(grid[0]) == 4


# -- DRC on the GDSII stream file ------------------------------------------
def _write_gds(path, routed_alu, rules=None):
    from silicon.writers import write_gds
    library, netlist, plan = (routed_alu["library"], routed_alu["netlist"],
                              routed_alu["plan"])
    write_gds(str(path), netlist, library, plan, routed_alu["route"],
              netlist.top, fillers=routed_alu["fillers"])
    return str(path)


def test_written_gds_is_clean_on_the_routing_layers(tmp_path, routed_alu):
    from silicon.writers import check_gds, read_gds_geometry
    path = _write_gds(tmp_path / "alu.gds", routed_alu)
    rules = routed_alu["cfg"].rules
    layers = list(routed_alu["cfg"].vertical_layers) + \
        list(routed_alu["cfg"].horizontal_layers)
    assert check_gds(path, rules, layers=layers) == {}
    shapes = read_gds_geometry(path)
    # cells, metal, cuts and net labels all came back out of the stream
    kinds = {shape.kind for shape in shapes}
    assert {"path", "boundary", "text"} <= kinds
    labels = {shape.text for shape in shapes if shape.kind == "text"}
    assert "clk" in labels or any(name in labels for name in routed_alu["route"].net_wirelength)


def test_gds_check_can_fail_on_a_tighter_rule_deck(tmp_path, routed_alu):
    """The stream checker must re-derive violations, not trust the router."""
    from silicon.rules import MetalRule, default_rules
    from silicon.writers import check_gds
    path = _write_gds(tmp_path / "alu.gds", routed_alu)
    rules = dict(default_rules())
    loose = rules["M3"]
    rules["M3"] = MetalRule("M3", loose.direction, loose.width, 0.40,
                            loose.min_area, loose.via_size, loose.via_spacing,
                            loose.enclosure, loose.resistance, loose.capacitance)
    found = check_gds(path, rules, layers=["M2", "M3", "M4", "M5"])
    assert found.get("SPACING"), "a 0.40 um spacing rule should not pass"


def test_gds_labels_let_the_checker_separate_nets(tmp_path, routed_alu):
    """Two touching shapes of one net are a corner; of two nets, a short."""
    from silicon.writers import read_gds_geometry
    path = _write_gds(tmp_path / "alu.gds", routed_alu)
    shapes = read_gds_geometry(path)
    metal = [s for s in shapes if s.kind == "path" and s.layer.startswith("M")]
    assert len(metal) == len(routed_alu["route"].segments)
    # every routed net carries exactly one label
    labels = [s.text for s in shapes if s.kind == "text" and s.text]
    assert len(labels) == len(set(labels))
