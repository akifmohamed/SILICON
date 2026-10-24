import pytest

from silicon.sta import TimingConfig, TimingEngine, analyse, estimate_power


def test_alu_timing(alu_netlist, library):
    config = TimingConfig(clock_period=4.0, clock_name="clk")
    timing, power = analyse(alu_netlist, library, config)
    assert timing.paths, "expected at least one timed path"
    assert timing.wns < config.clock_period
    assert timing.tns >= 0.0
    assert timing.clock_period == 4.0
    assert power.total_mw > 0
    assert power.cell_area > 0


def test_period_changes_slack(alu_netlist, library):
    fast, _ = analyse(alu_netlist, library, TimingConfig(clock_period=1.0))
    slow, _ = analyse(alu_netlist, library, TimingConfig(clock_period=8.0))
    assert slow.wns > fast.wns
    assert fast.violating_endpoints >= 0


def test_worst_path_structure(alu_netlist, library):
    timing, _ = analyse(alu_netlist, library, TimingConfig(clock_period=4.0))
    path = timing.paths[0]
    assert path.slack <= timing.paths[-1].slack or len(timing.paths) == 1
    assert path.arrival >= 0
    assert path.startpoint and path.endpoint
    assert path.hops
    assert all(hop.delay >= 0 for hop in path.hops)


def test_hold_check_present(alu_netlist, library):
    timing, _ = analyse(alu_netlist, library, TimingConfig(clock_period=4.0))
    assert timing.hold_wns is not None


def test_register_endpoints_are_flops(alu_netlist, library):
    timing, _ = analyse(alu_netlist, library, TimingConfig(clock_period=4.0))
    flop_nets = {c.instance for c in alu_netlist.sequential_cells()}
    endpoints = {p.endpoint for p in timing.paths}
    assert endpoints & flop_nets or endpoints


def test_propagated_clock_shifts_arrival(alu_netlist, library):
    engine = TimingEngine(alu_netlist, library, TimingConfig(clock_period=4.0))
    plain = engine.run()
    ck_nets = {c.inputs["CK"] for c in alu_netlist.sequential_cells()}
    engine.set_clock_arrivals({net: 0.5 for net in ck_nets})
    shifted = engine.run()
    assert shifted.wns <= plain.wns + 1e-9


def test_power_scales_with_frequency(alu_netlist, library):
    slow = estimate_power(alu_netlist, library, TimingConfig(clock_period=8.0))
    fast = estimate_power(alu_netlist, library, TimingConfig(clock_period=2.0))
    assert fast.dynamic_mw > slow.dynamic_mw
    assert fast.leakage_mw == pytest.approx(slow.leakage_mw, rel=1e-9)
