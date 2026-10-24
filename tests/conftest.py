import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EXAMPLES = os.path.join(ROOT, "examples", "rtl")


def read_example(name: str) -> str:
    with open(os.path.join(EXAMPLES, name), "r", encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def alu_source():
    return read_example("alu.v")


@pytest.fixture(scope="session")
def counter_source():
    return read_example("counter.v")


@pytest.fixture(scope="session")
def mac_source():
    return read_example("mac.v")


@pytest.fixture(scope="session")
def library():
    from silicon.library import default_library
    return default_library()


@pytest.fixture(scope="session")
def alu_netlist(library, alu_source):
    from silicon.elaborate import elaborate
    from silicon.synth import synthesize
    from silicon.verilog import parse_modules
    design = elaborate(parse_modules(alu_source, "alu.v"), "alu")
    netlist, _stats = synthesize(design, library)
    return netlist


@pytest.fixture(scope="session")
def alu_design(alu_source):
    from silicon.elaborate import elaborate
    from silicon.verilog import parse_modules
    return elaborate(parse_modules(alu_source, "alu.v"), "alu")


@pytest.fixture(scope="session")
def counter_design(counter_source):
    from silicon.elaborate import elaborate
    from silicon.verilog import parse_modules
    return elaborate(parse_modules(counter_source, "counter.v"), "counter")


@pytest.fixture(scope="session")
def routed_alu(alu_netlist, library):
    """Floorplan -> place -> CTS -> route on a copy of the ALU netlist."""
    import copy

    from silicon.pnr import (ClockTree, PhysicalConfig, Placer, Router,
                             build_floorplan, insert_fillers, route_capacitance)
    netlist = copy.deepcopy(alu_netlist)
    cfg = PhysicalConfig()
    plan = build_floorplan(netlist, library, cfg)
    placement = Placer(netlist, library, plan, cfg).place()
    cts = ClockTree(netlist, library, cfg).build("clk")
    for buffer_cell in cts.buffers:
        netlist.add_cell(buffer_cell)
    route = Router(netlist, library, plan, cfg).route()
    fillers = insert_fillers(netlist, library, plan)
    return {"netlist": netlist, "library": library, "plan": plan,
            "placement": placement, "cts": cts, "route": route,
            "fillers": fillers, "cfg": cfg,
            "caps": route_capacitance(route, cfg)}
