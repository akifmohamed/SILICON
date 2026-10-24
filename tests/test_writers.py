"""Tests for the output writers: DEF, GDSII, LEF and the SVG/text views."""

import struct

import pytest

from silicon.lef import LefMacro, read_lef, write_block_lef, write_cell_lef, write_tech_lef
from silicon.writers import (ascii_layout, read_gds_summary, write_def, write_gds,
                             write_svg)


def _write_all(tmp_path, routed_alu):
    netlist, library = routed_alu["netlist"], routed_alu["library"]
    plan, route = routed_alu["plan"], routed_alu["route"]
    fillers = routed_alu["fillers"]
    paths = {
        "def": tmp_path / "alu.def",
        "gds": tmp_path / "alu.gds",
        "lef": tmp_path / "alu.lef",
        "cells": tmp_path / "cells.lef",
        "tech": tmp_path / "tech.lef",
        "svg": tmp_path / "alu.svg",
    }
    write_def(str(paths["def"]), netlist, library, plan, route, "alu",
              fillers=fillers)
    write_gds(str(paths["gds"]), netlist, library, plan, route, "alu",
              fillers=fillers)
    write_svg(str(paths["svg"]), netlist, library, plan, route, "alu",
              title="alu", fillers=fillers)
    write_block_lef(str(paths["lef"]), netlist, library, plan, "alu")
    write_cell_lef(str(paths["cells"]), library)
    write_tech_lef(str(paths["tech"]), library)
    return paths


def test_def_has_components_rows_and_nets(tmp_path, routed_alu):
    paths = _write_all(tmp_path, routed_alu)
    text = paths["def"].read_text()
    netlist = routed_alu["netlist"]
    assert text.startswith("VERSION 5.8 ;")
    assert "END DESIGN" in text
    assert f"COMPONENTS {len(netlist.cells) + len(routed_alu['fillers'])} ;" in text
    assert text.count("\nROW ") == routed_alu["plan"].row_count
    assert "DIEAREA" in text
    # every placed instance is present
    for cell in netlist.cells[:20]:
        assert f"- {cell.instance} {cell.cell} + PLACED" in text
    # routed nets carry segments
    assert "+ ROUTED M3" in text or "+ ROUTED M2" in text


def test_gds_is_parseable_and_complete(tmp_path, routed_alu):
    paths = _write_all(tmp_path, routed_alu)
    raw = paths["gds"].read_bytes()
    # header record: length 6, type 0x00, datatype 2, version 600
    length, rec, dtype = struct.unpack(">HBB", raw[:4])
    assert (length, rec, dtype) == (6, 0x00, 2)
    assert struct.unpack(">h", raw[4:6])[0] == 600
    summary = read_gds_summary(str(paths["gds"]))
    netlist = routed_alu["netlist"]
    expected_placements = len(netlist.cells) + len(routed_alu["fillers"])
    assert summary["srefs"] == expected_placements
    assert summary["paths"] == len(routed_alu["route"].segments)
    # one label per routed net plus the block label
    assert summary["texts"] == len(routed_alu["route"].net_wirelength) + 1
    assert summary["boundaries"] > 0
    # record lengths must tile the file exactly
    pos, total = 0, len(raw)
    while pos < total:
        size = struct.unpack(">H", raw[pos:pos + 2])[0]
        assert 4 <= size <= total - pos
        pos += size
    assert pos == total


def test_lef_round_trip(tmp_path, routed_alu):
    paths = _write_all(tmp_path, routed_alu)
    library = routed_alu["library"]
    macros = read_lef(str(paths["cells"]))
    assert set(macros) == set(library.cells)
    inverter = macros["INVX1"]
    assert isinstance(inverter, LefMacro)
    assert inverter.width == pytest.approx(library.cell("INVX1").width, abs=1e-4)
    assert inverter.height == pytest.approx(library.row_height, abs=1e-4)
    assert set(inverter.pins) == {"A", "Y"}
    assert inverter.pins["Y"] == "OUTPUT"
    flop = macros["DFFRX1"]
    assert {"D", "CK", "RN", "Q"} <= set(flop.pins)

    block = read_lef(str(paths["lef"]))["alu"]
    assert block.width == pytest.approx(routed_alu["plan"].die.width, abs=1e-3)
    assert set(block.pins) == set(routed_alu["netlist"].port_dirs)

    tech = paths["tech"].read_text()
    assert "SITE CoreSite" in tech
    for layer in routed_alu["route"].layer_usage:
        assert f"LAYER {layer}" in tech


def test_svg_geometry_matches_layout(tmp_path, routed_alu):
    paths = _write_all(tmp_path, routed_alu)
    svg = paths["svg"].read_text()
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    netlist = routed_alu["netlist"]
    # one rect per drawn cell (logic + fillers)
    assert svg.count('class="c"') == len(netlist.cells) + len(routed_alu["fillers"])
    assert svg.count("<line") == len(routed_alu["route"].segments) + \
        routed_alu["plan"].row_count
    for port in routed_alu["plan"].io_pins:
        assert port in routed_alu["netlist"].port_dirs


def test_ascii_layout_marks_cells(routed_alu):
    text = ascii_layout(routed_alu["netlist"], routed_alu["plan"], width=60, height=14)
    lines = text.splitlines()
    assert lines[0].startswith("+") and lines[-1].startswith("core")
    body = "\n".join(lines[1:-1])
    assert "#" in body
    assert "F" in body          # flops
    assert "C" in body          # clock buffers
    assert body.count("\n") + 1 == len(lines) - 2
