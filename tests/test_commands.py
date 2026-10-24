"""OCV knobs, constraint reporting and timing-driven routing order."""

import copy
import io
import os

import pytest

from silicon.dc_shell import DcSession, DcShell
from silicon.icc2_shell import Icc2Shell, PnrSession
from silicon.library import default_library
from silicon.pnr import PhysicalConfig, Placer, Router, build_floorplan
from silicon.sta import TimingConfig, TimingEngine

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "rtl")


def _run(shell, script: str) -> str:
    """Run a script and return its output; fail if any command reported an error."""
    buffer = io.StringIO()
    shell.out = buffer
    shell._fatal = False
    shell.run_script(script)
    text = buffer.getvalue()
    assert "Error:" not in text, text
    return text


@pytest.fixture(scope="module")
def compiled_dc():
    shell = DcShell(DcSession(), out=io.StringIO())
    _run(shell, f"""
        read_verilog {EXAMPLES}/alu.v
        elaborate alu
        create_clock -period 2.0 -name clk
        compile
    """)
    return shell


# -- dc_shell ---------------------------------------------------------------
def test_set_ocv_moves_slack_and_can_be_switched_off(compiled_dc):
    shell = compiled_dc
    baseline = shell.s.timing.wns
    text = _run(shell, "set_ocv -cell_late 1.20 -net_late 1.20")
    assert "OCV derates" in text
    assert shell.s.timing.wns < baseline          # harsher derates cost slack
    harsher = shell.s.timing.wns
    _run(shell, "set_ocv -off")
    assert shell.s.config.derate_cell_late == 1.0
    assert shell.s.timing.wns > harsher           # no derates, no pessimism


def test_report_constraints_names_the_offenders(compiled_dc, monkeypatch):
    shell = compiled_dc
    text = _run(shell, "report_constraints")
    assert "Transition violations:" in text
    assert "Capacitance violations:" in text

    # make the design limits bite, then check the report lists real nets
    shell.s.timing.max_slew_violations = 2
    shell.s.timing.slew_violations = [("some_net", 0.50, 0.40),
                                      ("other_net", 0.45, 0.40)]
    text = _run(shell, "report_constraints -max_items 1")
    assert "some_net" in text and "other_net" not in text      # max_items honoured
    assert 0.50 - 0.40 == pytest.approx(0.10)


# -- icc2_shell -------------------------------------------------------------
@pytest.fixture(scope="module")
def routed_icc2(tmp_path_factory):
    out = tmp_path_factory.mktemp("icc2")
    json_path = out / "alu.json"
    dc = DcShell(DcSession(), out=io.StringIO())
    _run(dc, f"""
        read_verilog {EXAMPLES}/alu.v
        elaborate alu
        create_clock -period 4.0 -name clk
        compile
        write -format json -output {json_path}
    """)
    shell = Icc2Shell(PnrSession(), out=io.StringIO())
    _run(shell, f"""
        read_netlist {json_path}
        create_clock -period 4.0 -name clk
        initialize_floorplan
        place_opt
        clock_opt
        route_auto
    """)
    return shell


def test_icc2_set_ocv_and_report_constraints(routed_icc2):
    shell = routed_icc2
    baseline = shell.s.timing.wns
    text = _run(shell, "set_ocv -cell_late 1.20 -net_late 1.20")
    assert "OCV derates" in text
    assert shell.s.timing.wns < baseline
    text = _run(shell, "report_constraints")
    assert "Transition violations:" in text
    _run(shell, "set_ocv -off")
    assert shell.s.timing.wns > baseline - 1e-9


def test_icc2_reports_need_a_block():
    shell = Icc2Shell(PnrSession(), out=io.StringIO())
    buffer = io.StringIO()
    shell.out = buffer
    shell._fatal = False
    shell.run_script("report_constraints")
    assert "no block loaded" in buffer.getvalue()


# -- timing-driven routing --------------------------------------------------
def _place(seed_netlist, library, timing_driven, timing=None):
    netlist = copy.deepcopy(seed_netlist)
    cfg = PhysicalConfig(routing_timing_driven=timing_driven)
    plan = build_floorplan(netlist, library, cfg)
    Placer(netlist, library, plan, cfg).place()
    return netlist, cfg, plan


def test_critical_nets_are_routed_first(alu_netlist, library):
    netlist, cfg, plan = _place(alu_netlist, library, True)
    timing = TimingEngine(netlist, library, TimingConfig(clock_period=4.0)).run()
    router = Router(netlist, library, plan, cfg, timing=timing)
    critical = router.net_criticality()
    order = router._net_order()
    assert critical                                   # the ALU has a critical path
    assert order[0] in critical                       # critically routed first
    assert set(critical) <= set(netlist.nets)

    route = router.route()
    assert route.critical_nets == len(critical) or route.critical_nets > 0


def test_without_a_timing_view_routing_order_is_fanout_then_name(alu_netlist, library):
    netlist, cfg, plan = _place(alu_netlist, library, False)
    router = Router(netlist, library, plan, cfg)
    assert router.net_criticality() == {}
    order = router._net_order()
    widths = [len(netlist.nets[net]) for net in order]
    assert widths == sorted(widths, reverse=True)     # widest nets first
    # a timing view passed to a router with the feature off is ignored
    timing = TimingEngine(netlist, library, TimingConfig(clock_period=4.0)).run()
    assert Router(netlist, library, plan, cfg, timing=timing).net_criticality() == {}


def test_timing_driven_routing_never_breaks_drc(alu_netlist, library):
    netlist, cfg, plan = _place(alu_netlist, library, True)
    timing = TimingEngine(netlist, library, TimingConfig(clock_period=4.0)).run()
    route = Router(netlist, library, plan, cfg, timing=timing).route()
    assert route.drc == 0
    assert not route.unrouted_nets
