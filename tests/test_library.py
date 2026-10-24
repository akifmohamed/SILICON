import pytest

from silicon.library import LibertyError, default_library, read_library


def test_builtin_library(library):
    assert library.name == "stdcells_32nm"
    assert len(library.cells) >= 15
    assert library.has("NAND2X1") and library.has("DFFRX1")
    assert library.row_height > 0 and library.site_width > 0
    assert library.total_area() > 0


def test_cell_attributes(library):
    cell = library.cell("NAND2X1")
    assert cell.area > 0
    assert cell.width > 0
    assert {"A", "B", "Y"} <= set(cell.pins)
    assert cell.pins["Y"].direction == "output"
    assert cell.pins["A"].capacitance > 0


def test_arc_lookup_is_monotonic_in_load(library):
    arc = library.cell("NAND2X1").arc("A", "Y")
    assert arc is not None
    slow = arc.rise.lookup(0.10, 0.020)
    fast = arc.rise.lookup(0.010, 0.001)
    assert slow > fast


def test_unknown_cell_and_arc(library):
    with pytest.raises(LibertyError):
        library.cell("NOSUCHX9")
    assert library.cell("NAND2X1").arc("A", "NOPE") is None


def test_parse_errors_are_reported(tmp_path):
    bad = tmp_path / "bad.lib"
    bad.write_text("library (x) { cell (A) { area : } }\n")
    with pytest.raises(LibertyError):
        read_library(str(bad))


def test_reload_from_disk(tmp_path, library):
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "silicon" / "lib" / "stdcells_32nm.lib"
    reloaded = read_library(str(src))
    assert set(reloaded.cells) == set(library.cells)
