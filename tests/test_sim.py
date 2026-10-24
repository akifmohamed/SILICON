"""The correctness gate for ``dc``: mapped netlist vs elaborated RTL.

``equivalence_check`` drives the *gate* netlist and the *word-level* elaborated
design with the same random vectors and compares the outputs cycle by cycle.
Anything wrong in the technology mapper, the optimiser, the fanout buffer pass or
the serialiser shows up here.
"""

import pytest

from silicon.elaborate import elaborate
from silicon.sim import equivalence_check
from silicon.synth import synthesize
from silicon.verilog import parse_modules

SOURCES = {
    "alu": ("alu_source", "rst_n"),
    "counter": ("counter_source", "rst_n"),
    "mac": ("mac_source", "rst_n"),
}


@pytest.mark.parametrize("top", sorted(SOURCES))
def test_mapped_netlist_matches_rtl(top, request):
    fixture, reset = SOURCES[top]
    source = request.getfixturevalue(fixture)
    library = request.getfixturevalue("library")
    design = elaborate(parse_modules(source, f"{top}.v"), top)
    netlist, _stats = synthesize(design, library)
    report = equivalence_check(netlist, design, clock="clk", reset=reset,
                               vectors=96, seed=7)
    assert report.mismatches == 0, report.summary()
    assert report.x_free, report.summary()
    assert report.ok


def test_a_corrupted_netlist_fails_the_check(alu_source, library):
    """The checker must be able to fail: break a flop and expect mismatches."""
    design = elaborate(parse_modules(alu_source, "alu.v"), "alu")
    netlist, _stats = synthesize(design, library)
    flop = netlist.sequential_cells()[0]
    original = flop.inputs["D"]
    flop.inputs["D"] = flop.outputs["Q"]        # hold instead of load
    try:
        report = equivalence_check(netlist, design, clock="clk", reset="rst_n",
                                   vectors=32, seed=3)
    finally:
        flop.inputs["D"] = original
    assert report.mismatches > 0


def test_check_is_deterministic(alu_source, library):
    design = elaborate(parse_modules(alu_source, "alu.v"), "alu")
    netlist, _stats = synthesize(design, library)
    first = equivalence_check(netlist, design, clock="clk", reset="rst_n",
                              vectors=24, seed=11)
    second = equivalence_check(netlist, design, clock="clk", reset="rst_n",
                               vectors=24, seed=11)
    assert (first.mismatches, second.mismatches) == (0, 0)
    assert first.output_bits == second.output_bits
