"""Shell-level tests: the two tools really are independent and scriptable."""

import io
import os

import pytest

from silicon.icc2_shell import Icc2Shell, PnrSession
from silicon.dc_shell import DcShell, DcSession

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "rtl")


def _run(shell, script: str) -> str:
    buffer = io.StringIO()
    shell.out = buffer
    shell.run_script(script)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def dc_output(tmp_path_factory):
    """Synthesise the ALU through dc_shell and keep the JSON handoff file."""
    out = tmp_path_factory.mktemp("dc")
    json_path = out / "alu.json"
    buffer = io.StringIO()
    shell = DcShell(DcSession(), out=buffer)
    ok = shell.run_script(f"""
        read_verilog {EXAMPLES}/alu.v
        elaborate alu
        create_clock -period 4.0 -name clk
        compile
        report_qor
        write -format json -output {json_path}
        write -format verilog -output {out}/alu.v
    """)
    return {"shell": shell, "json": str(json_path), "verilog": str(out / "alu.v"),
            "text": buffer.getvalue(), "code": ok}


def test_dc_shell_script_produces_a_netlist_database(dc_output):
    assert os.path.exists(dc_output["json"])
    assert os.path.exists(dc_output["verilog"])
    text = dc_output["text"]
    assert "Report : qor" in text
    assert "Timing Path Group" in text
    assert "alu" in text


def test_icc2_consumes_the_json_database_alone(dc_output, tmp_path):
    """icc2 must not need dc: everything it reads is the JSON file."""
    def_path = tmp_path / "alu.def"
    gds_path = tmp_path / "alu.gds"
    lef_path = tmp_path / "alu.lef"
    buffer = io.StringIO()
    shell = Icc2Shell(PnrSession(), out=buffer)
    shell.run_script(f"""
        read_netlist {dc_output['json']}
        initialize_floorplan -core_utilization 0.62 -aspect_ratio 1.2 -core_offset {{3 3}}
        place_opt
        clock_opt
        route_auto
        report_drc
        write_def {def_path}
        write_gds {gds_path}
        write_lef {lef_path}
    """)
    text = buffer.getvalue()
    assert "Loaded block 'alu'" in text
    assert "Total DRC violations" in text
    assert "0\n" in text
    for path in (def_path, gds_path, lef_path):
        assert path.exists() and path.stat().st_size > 0


def test_icc2_rejects_rtl(alu_source, tmp_path):
    rtl = tmp_path / "alu.v"
    rtl.write_text(alu_source)
    buffer = io.StringIO()
    shell = Icc2Shell(PnrSession(), out=buffer)
    shell.run_script(f"read_netlist {rtl}")
    assert "not RTL" in buffer.getvalue()


def test_stage_ordering_is_enforced(dc_output):
    buffer = io.StringIO()
    shell = Icc2Shell(PnrSession(), out=buffer)
    shell.run_script(f"""
        read_netlist {dc_output['json']}
        clock_opt
    """)
    assert "has not run yet" in buffer.getvalue()


def test_brace_arguments_survive_tokenisation(dc_output):
    buffer = io.StringIO()
    shell = Icc2Shell(PnrSession(), out=buffer)
    shell.run_script(f"""
        read_netlist {dc_output['json']}
        initialize_floorplan -core_offset {{5 6}}
    """)
    session = shell.s
    assert session.plan.core.x1 == pytest.approx(5.0)
    assert session.plan.core.y1 == pytest.approx(6.0)


def test_help_lists_both_tool_vocabularies(dc_output):
    dc_help = _run(DcShell(DcSession()), "help")
    assert "compile" in dc_help and "read_verilog" in dc_help
    icc2_help = _run(Icc2Shell(PnrSession()), "help")
    assert "route_auto" in icc2_help and "initialize_floorplan" in icc2_help
    assert "compile" not in icc2_help
