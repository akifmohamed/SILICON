import pytest

from silicon.pnr import PhysicalConfig, route_capacitance


def test_floorplan_geometry(routed_alu, library):
    plan = routed_alu["plan"]
    assert plan.core.width > 0 and plan.core.height > 0
    assert plan.die.width > plan.core.width
    assert plan.row_count > 0
    assert plan.rows[0].sites >= 1
    for row in plan.rows:
        assert row.sites * library.site_width <= plan.core.width + 1e-6
        assert abs(row.x2 - row.x1 - plan.core.width) < 1e-6


def test_placement_is_legal(routed_alu):
    placement = routed_alu["placement"]
    assert placement.overlaps == 0
    assert placement.legalized
    assert placement.hpwl > 0
    assert 0 < placement.utilisation < 1


def test_cells_stay_inside_the_core(routed_alu):
    plan, netlist = routed_alu["plan"], routed_alu["netlist"]
    for cell in netlist.cells:
        assert cell.x >= plan.core.x1 - 1e-6
        assert cell.x + cell.width <= plan.core.x2 + 1e-6
        assert cell.y >= plan.core.y1 - 1e-6


def test_cts_covers_every_flop(routed_alu):
    cts, netlist = routed_alu["cts"], routed_alu["netlist"]
    flops = netlist.sequential_cells()
    assert cts.sinks == len(flops)
    assert cts.levels >= 1
    assert cts.skew >= 0
    # every flop is a sink and now hangs off a clock-tree net
    assert set(cts.sink_delays) == {c.instance for c in flops}
    assert all(c.inputs["CK"] != "clk" for c in flops)


def test_cts_buffers_are_library_cells(routed_alu, library):
    for buffer in routed_alu["cts"].buffers:
        assert library.has(buffer.cell)
        assert buffer.cell.startswith("BUF")


def test_router_is_clean(routed_alu):
    route = routed_alu["route"]
    assert route.drc == 0
    assert not route.unrouted_nets
    assert route.wirelength > 0
    assert route.vias_count > 0
    assert set(route.layer_usage) <= {"M1", "M2", "M3", "M4", "M5"}


def test_route_capacitance_is_physical(routed_alu):
    caps = routed_alu["caps"]
    assert caps
    assert all(value >= 0 for value in caps.values())
    assert sum(caps.values()) > 0


def test_fillers_fill_the_rows(routed_alu):
    assert routed_alu["fillers"]
    assert all(f.cell == "FILLX1" for f in routed_alu["fillers"])


def test_utilisation_target_is_respected(library, alu_netlist):
    import copy
    from silicon.pnr import Placer, build_floorplan
    netlist = copy.deepcopy(alu_netlist)
    cfg = PhysicalConfig(core_utilization=0.5)
    plan = build_floorplan(netlist, library, cfg)
    result = Placer(netlist, library, plan, cfg).place()
    assert result.utilisation <= 0.75


def test_routing_is_deterministic(routed_alu, library):
    import copy
    from silicon.pnr import Router
    netlist = copy.deepcopy(routed_alu["netlist"])
    again = Router(netlist, library, routed_alu["plan"], routed_alu["cfg"]).route()
    assert again.wirelength == pytest.approx(routed_alu["route"].wirelength)
    assert again.drc == routed_alu["route"].drc


def test_routing_is_deterministic_across_processes(alu_netlist):
    """Different hash seeds must not change placement or routing."""
    import json
    import os
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import json,sys;"
        "from silicon.pnr import *;"
        "from silicon.library import default_library;"
        "from silicon.synth import synthesize;"
        "from silicon.verilog import parse_modules;"
        "from silicon.elaborate import elaborate;"
        f"src=open({os.path.join(root, 'examples/rtl/alu.v')!r}).read();"
        "d=elaborate(parse_modules(src,'alu.v'),'alu');"
        "lib=default_library();"
        "nl,_=synthesize(d,lib);"
        "cfg=PhysicalConfig();"
        "plan=build_floorplan(nl,lib,cfg);"
        "pl=Placer(nl,lib,plan,cfg).place();"
        "cts=ClockTree(nl,lib,cfg,counter_start=len(nl.cells),plan=plan).build('clk');"
        "[nl.add_cell(b) for b in cts.buffers];"
        "r=Router(nl,lib,plan,cfg).route();"
        "print(json.dumps([round(pl.hpwl,6),round(r.wirelength,6),r.vias_count,r.drc]))"
    )
    results = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=root)
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, env=env, check=True, cwd=root)
        results.append(json.loads(out.stdout.strip().splitlines()[-1]))
    assert results[0] == results[1], results
