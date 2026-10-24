import pytest

from silicon.verilog import VerilogError, parse_modules


def test_parse_simple_module(alu_source):
    modules = parse_modules(alu_source, "alu.v")
    assert len(modules) == 1
    mod = modules[0]
    assert mod.name == "alu"
    assert mod.params and mod.params[0].name == "WIDTH"
    names = [name for decl in mod.ports for name in decl.names]
    assert names == ["clk", "rst_n", "a", "b", "op", "y", "zero"]


def test_parse_counter(counter_source):
    mod = parse_modules(counter_source, "counter.v")[0]
    assert mod.name == "counter"
    assert len(mod.items) > 3


def test_parse_operators_and_concat():
    src = """
    module m(input [3:0] a, input [3:0] b, output [7:0] y);
      assign y = {a, b} + 8'd3;
    endmodule
    """
    mod = parse_modules(src, "m.v")[0]
    assert mod.name == "m"


def test_parse_case_and_multiple_modules():
    src = """
    module a(input x, output y); assign y = x; endmodule
    module b(input x, output y); assign y = ~x; endmodule
    """
    mods = parse_modules(src, "two.v")
    assert [m.name for m in mods] == ["a", "b"]


def test_parse_rejects_garbage():
    with pytest.raises(VerilogError):
        parse_modules("module m(input a; endmodule", "bad.v")


def test_comments_are_ignored():
    src = """
    // leading comment
    module m(input a, output y); /* inline */ assign y = a; endmodule
    """
    mod = parse_modules(src, "c.v")[0]
    assert mod.name == "m"
