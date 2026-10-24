import importlib

import pytest

from silicon.elaborate import elaborate
from silicon.verilog import parse_modules


def test_counter_widths(counter_source):
    design = elaborate(parse_modules(counter_source, "counter.v"), "counter")
    assert design.top == "counter"
    assert design.bit_width("count") == 8
    assert design.port_dirs["count"] == "output"
    assert design.clocks
    assert any("rst" in name for name in design.async_resets)


def test_alu_is_purely_comb(alu_source):
    design = elaborate(parse_modules(alu_source, "alu.v"), "alu")
    assert design.bit_width("y") == 8
    assert design.bit_width("zero") == 1
    types = set(c.type for c in design.cells)
    assert {"ADD", "SUB", "AND", "OR", "XOR", "EQ", "LT", "MUX2"} <= types


def test_parameter_override():
    src = """
    module m #(parameter W = 4) (input [W-1:0] a, input [W-1:0] b, output [W-1:0] y);
      assign y = a + b;
    endmodule
    """
    mods = parse_modules(src, "m.v")
    design = elaborate(mods, "m", params={"W": 12})
    assert design.bit_width("y") == 12


def test_unknown_top_module_raises():
    src = "module a(input x, output y); assign y = x; endmodule"
    with pytest.raises(Exception):
        elaborate(parse_modules(src, "a.v"), "nope")


def test_latch_inference_is_rejected():
    src = """
    module m(input en, input d, output reg y);
      reg t;
      always @* begin
        if (en) t = d;
      end
      assign y = t;
    endmodule
    """
    with pytest.raises(Exception):
        elaborate(parse_modules(src, "m.v"), "m")


def test_slice_and_index_expressions():
    src = """
    module m(input [7:0] a, output [3:0] lo, output bit3);
      assign lo = a[3:0];
      assign bit3 = a[3];
    endmodule
    """
    design = elaborate(parse_modules(src, "m.v"), "m")
    assert design.bit_width("lo") == 4
    assert design.bit_width("bit3") == 1
