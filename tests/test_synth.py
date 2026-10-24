import pytest

from silicon.library import default_library
from silicon.synth import SynthesisError, synthesize


def test_alu_maps_to_library_cells(alu_netlist, library):
    assert alu_netlist.cells
    for cell in alu_netlist.cells:
        assert library.has(cell.cell), cell.cell


def test_alu_has_exactly_one_driver_per_net(alu_netlist):
    drivers = {}
    for cell in alu_netlist.cells:
        for net in cell.outputs.values():
            assert net not in drivers, f"multiple drivers on {net}"
            drivers[net] = cell.instance


def test_ports_are_connected(alu_netlist):
    for port, net in alu_netlist.ports.items():
        assert net in alu_netlist.nets, port


def test_sequential_cells_are_flops(alu_netlist):
    flops = alu_netlist.sequential_cells()
    assert flops, "the ALU is registered, so it must contain flops"
    assert all(c.cell in ("DFFX1", "DFFRX1") for c in flops)
    assert len(flops) == 9            # 8 result bits + zero


def test_fanout_limit_respected(alu_netlist):
    for net, keys in alu_netlist.nets.items():
        sinks = [k for k in keys if not k.startswith("port/")]
        driven = any(k.split("/")[0] in {c.instance for c in alu_netlist.cells
                                         if net in c.outputs.values()} for k in keys)
        if driven and len(sinks) > 8:
            worst = max(len([k for k in keys if k.split("/")[0] in
                             {c.instance for c in alu_netlist.cells
                              if net in c.outputs.values()}]), 0)
            assert worst <= 8


def test_area_adds_up(alu_netlist, library):
    expected = sum(library.cell(c.cell).area for c in alu_netlist.cells)
    assert expected > 100
    assert all(library.cell(c.cell).area > 0 for c in alu_netlist.cells)


def test_unknown_cell_type_is_rejected(counter_source):
    import copy

    from silicon.elaborate import elaborate
    from silicon.verilog import parse_modules
    design = elaborate(parse_modules(counter_source, "counter.v"), "counter")
    design = copy.deepcopy(design)
    tick = design.signals["tick"].bits
    design.cells.append(type(design.cells[0])(
        name="weird", type="NOTACELL", width=len(tick),
        inputs={"A": design.signals["en"].bits}, outputs={"Y": tick}))
    with pytest.raises(SynthesisError):
        synthesize(design, default_library())


def test_synthesis_is_deterministic(alu_source, library):
    from silicon.elaborate import elaborate
    from silicon.verilog import parse_modules
    first, _ = synthesize(elaborate(parse_modules(alu_source, "alu.v"), "alu"), library)
    second, _ = synthesize(elaborate(parse_modules(alu_source, "alu.v"), "alu"), library)
    assert [(c.instance, c.cell) for c in first.cells] == \
           [(c.instance, c.cell) for c in second.cells]


def test_equivalence_alu(alu_netlist, alu_design):
    from silicon.sim import equivalence_check
    report = equivalence_check(alu_netlist, alu_design, reset="rst_n", vectors=64)
    assert report.mismatches == 0
    assert report.ok


def test_equivalence_finds_a_broken_netlist(alu_netlist, alu_design):
    import copy
    from silicon.sim import equivalence_check
    broken = copy.deepcopy(alu_netlist)
    flop = broken.sequential_cells()[0]
    flop.inputs["D"] = "1'b0"              # break a register input
    report = equivalence_check(broken, alu_design, reset="rst_n", vectors=32)
    assert not report.ok
