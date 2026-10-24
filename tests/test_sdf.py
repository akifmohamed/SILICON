"""SDF 3.0 output: structure, content and the icc2 command."""

import io
import os
import re

import pytest

from silicon.icc2_shell import Icc2Shell, PnrSession
from silicon.sdf import build_sdf, sdf_counts, write_sdf
from silicon.sta import TimingConfig, TimingEngine

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples", "rtl")


def _triplets(text: str):
    return [float(v) for v in re.findall(r"\((\d+\.\d+)::(\d+\.\d+)\)", text)
            for v in v]


def test_sdf_is_balanced_and_keeps_the_declared_counts(routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    engine = TimingEngine(netlist, library, TimingConfig(clock_period=4.0),
                          parasitics=routed_alu["parasitics"])
    engine.set_clock_arrivals(routed_alu["cts"].sink_delays)
    engine.run(parasitics=routed_alu["parasitics"])
    text = build_sdf(netlist, library, engine)
    assert text.count("(") == text.count(")")
    assert text.startswith("(DELAYFILE")
    assert text.rstrip().endswith(")")
    counts = sdf_counts(text)
    assert counts["arcs"] > 0
    assert counts["interconnects"] > 0
    assert counts["checks"] == 2 * sum(1 for c in netlist.cells if c.is_sequential)
    # operating conditions come from the library, not from constants
    assert f"(VOLTAGE {library.nom_voltage:.3f}::" in text
    assert f"(TEMPERATURE {library.nom_temperature:.1f}::" in text
    assert text.count('(CELL (CELLTYPE "alu") (INSTANCE)') == 1


def test_interconnect_delays_come_from_the_extracted_rc(routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    config = TimingConfig(clock_period=4.0)
    with_rc = TimingEngine(netlist, library, config,
                           parasitics=routed_alu["parasitics"])
    with_rc.run(parasitics=routed_alu["parasitics"])
    text = build_sdf(netlist, library, with_rc)
    delays = [float(m) for m in re.findall(r"\(INTERCONNECT \S+ \S+ \((\d+\.\d+)::", text)]
    assert delays and max(delays) > 0.0

    pre_route = TimingEngine(netlist, library, config)      # no parasitics
    pre_route.run()
    flat = build_sdf(netlist, library, pre_route)
    flat_delays = [float(m) for m in
                   re.findall(r"\(INTERCONNECT \S+ \S+ \((\d+\.\d+)::", flat)]
    assert flat_delays and max(flat_delays) == 0.0


def test_icc2_write_sdf(tmp_path, routed_alu):
    out = tmp_path / "alu.sdf"
    shell = Icc2Shell(PnrSession(), out=io.StringIO())
    session = shell.s
    session.netlist = routed_alu["netlist"]
    session.library = routed_alu["library"]
    session.parasitics = routed_alu["parasitics"]
    session.cts = routed_alu["cts"]
    buffer = io.StringIO()
    shell.out = buffer
    shell._fatal = False
    shell.run_script(f"write_sdf {out}")
    assert "SDF written" in buffer.getvalue(), buffer.getvalue()
    text = out.read_text()
    assert "(SDFVERSION \"3.0\")" in text
    assert os.path.getsize(out) > 1000
