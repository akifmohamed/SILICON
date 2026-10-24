"""LEF 5.8 writer and a small reader, for the cell library and the block.

LEF (Library Exchange Format) is the format the place-and-route tool really
consumes: a technology file describing layers, vias and routing rules, and a
cell file describing every macro (size, pins, pin geometry, obstructions).

The geometry here is simplified on purpose -- one routing rectangle per pin,
plus a keep-out rectangle -- because the cells are synthetic. What is *not*
simplified is the structure: a reader that only understands the subset written
here can parse the output back, which is what :func:`read_lef` is for and what
``tests/test_lef.py`` checks.

Also emitted is the block-level LEF (a ``MACRO`` for the implemented block with
its pins on the boundary), which is what a higher level of hierarchy would need.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .library import Library, LibCell
from .netlist import GateNetlist
from .pnr import Floorplan

# routing layer stack, coarse to fine
_TECH_LAYERS: Sequence[Tuple[str, str, float, float, float]] = (
    # name, direction, pitch, width, spacing
    ("M1", "HORIZONTAL", 0.20, 0.07, 0.07),
    ("M2", "VERTICAL", 0.20, 0.07, 0.07),
    ("M3", "HORIZONTAL", 0.20, 0.09, 0.09),
    ("M4", "VERTICAL", 0.20, 0.09, 0.09),
    ("M5", "HORIZONTAL", 0.20, 0.12, 0.12),
    ("M6", "VERTICAL", 0.30, 0.12, 0.12),
)


def _fmt(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def write_tech_lef(path: str, library: Library, design_name: str = "silicon") -> None:
    """Write the technology LEF: layers, via rules, site and units."""
    lines: List[str] = []
    add = lines.append
    add(f"VERSION 5.8 ;")
    add("BUSBITCHARS \"[]\" ;")
    add("DIVIDERCHAR \"/\" ;")
    add(f"UNITS")
    add(f"  DATABASE MICRONS 1000 ;")
    add(f"END UNITS")
    add("")
    add("MANUFACTURINGGRID 0.0005 ;")
    add("")
    add("SITE CoreSite")
    add(f"  CLASS CORE ;")
    add(f"  SYMMETRY Y ;")
    add(f"  SIZE {_fmt(library.site_width)} BY {_fmt(library.row_height)} ;")
    add("END CoreSite")
    add("")
    for name, direction, pitch, width, spacing in _TECH_LAYERS:
        add(f"LAYER {name}")
        add("  TYPE ROUTING ;")
        add(f"  DIRECTION {direction} ;")
        add(f"  PITCH {_fmt(pitch)} ;")
        add(f"  WIDTH {_fmt(width)} ;")
        add(f"  SPACING {_fmt(spacing)} ;")
        add(f"  AREA 0.02 ;")
        add("END " + name)
        add("")
    add("VIA VIA12 DEFAULT")
    add("  LAYER M1 ; RECT -0.05 -0.05 0.05 0.05 ;")
    add("  LAYER M2 ; RECT -0.05 -0.05 0.05 0.05 ;")
    add("END VIA12")
    add("")
    add("VIA VIA23 DEFAULT")
    add("  LAYER M2 ; RECT -0.05 -0.05 0.05 0.05 ;")
    add("  LAYER M3 ; RECT -0.05 -0.05 0.05 0.05 ;")
    add("END VIA23")
    add("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _pin_rects(cell: LibCell) -> Dict[str, Tuple[float, float, float, float]]:
    """A deterministic pin placement: inputs on the left, outputs on the right."""
    inputs = sorted(p.name for p in cell.pins.values() if p.direction == "input")
    outputs = sorted(p.name for p in cell.pins.values() if p.direction == "output")
    rects: Dict[str, Tuple[float, float, float, float]] = {}
    width, height = cell.width, cell.row_height
    pitch = 0.07

    def stack(names: Sequence[str], x0: float) -> None:
        if not names:
            return
        step = (height - 0.2) / max(1, len(names))
        for index, name in enumerate(names):
            y0 = 0.1 + index * step
            rects[name] = (x0, y0, min(x0 + 0.03, width - 0.01), min(y0 + pitch, height - 0.01))

    stack(inputs, 0.01)
    stack(outputs, max(width - 0.04, 0.01))
    return rects


def _write_macro(add, cell: LibCell, library: Library) -> None:
    add(f"MACRO {cell.name}")
    add("  CLASS CORE ;")
    add("  ORIGIN 0 0 ;")
    add(f"  FOREIGN {cell.name} 0 0 ;")
    add(f"  SIZE {_fmt(cell.width)} BY {_fmt(cell.row_height)} ;")
    add("  SYMMETRY X Y ;")
    add(f"  SITE CoreSite ;")
    rects = _pin_rects(cell)
    for pin in sorted(cell.pins.values(), key=lambda p: p.name):
        direction = "OUTPUT" if pin.direction == "output" else "INPUT"
        add(f"  PIN {pin.name}")
        add(f"    DIRECTION {direction} ;")
        add("    USE SIGNAL ;")
        add(f"    CAPACITANCE {_fmt(max(pin.capacitance, 0.0005))} ;")
        rect = rects.get(pin.name)
        if rect is not None:
            add("    PORT")
            add("      LAYER M1 ;")
            add(f"      RECT {_fmt(rect[0])} {_fmt(rect[1])} {_fmt(rect[2])} {_fmt(rect[3])} ;")
            add("    END")
        add(f"  END {pin.name}")
    if cell.area > 0:
        add(f"  OBS")
        add("    LAYER M1 ;")
        add(f"    RECT 0.02 0.02 {_fmt(cell.width - 0.02)} {_fmt(cell.row_height - 0.02)} ;")
        add("  END")
    add(f"END {cell.name}")
    add("")


def write_cell_lef(path: str, library: Library) -> None:
    """Write a LEF macro for every cell in the library."""
    lines: List[str] = []
    add = lines.append
    add("VERSION 5.8 ;")
    add("BUSBITCHARS \"[]\" ;")
    add("DIVIDERCHAR \"/\" ;")
    add(f"UNITS")
    add("  DATABASE MICRONS 1000 ;")
    add("END UNITS")
    add("")
    add(f"# {library.name}: {len(library.cells)} cells, synthetic 32 nm library")
    add(f"# total cell area {library.total_area():.2f} um^2")
    add("")
    for name in sorted(library.cells):
        _write_macro(add, library.cells[name], library)
    add("END LIBRARY")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def write_block_lef(path: str, netlist: GateNetlist, library: Library,
                    plan: Floorplan, block_name: str = "") -> None:
    """Write a LEF macro for the implemented block (pins on the die boundary)."""
    name = block_name or netlist.top
    rows = plan.row_count
    lines: List[str] = []
    add = lines.append
    add("VERSION 5.8 ;")
    add("BUSBITCHARS \"[]\" ;")
    add("DIVIDERCHAR \"/\" ;")
    add("UNITS")
    add("  DATABASE MICRONS 1000 ;")
    add("END UNITS")
    add("")
    add(f"MACRO {name}")
    add("  CLASS BLOCK ;")
    add("  ORIGIN 0 0 ;")
    add(f"  SIZE {_fmt(plan.die.width)} BY {_fmt(plan.die.height)} ;")
    add(f"  SITE CoreSite ;")
    for port in sorted(netlist.port_dirs):
        direction = "OUTPUT" if netlist.port_dirs[port].lower() == "output" else "INPUT"
        add(f"  PIN {port}")
        add(f"    DIRECTION {direction} ;")
        add("    USE SIGNAL ;")
        if port in plan.io_pins:
            x, y, side = plan.io_pins[port]
            add("    PORT")
            add("      LAYER M2 ;")
            add(f"      RECT {_fmt(x - 0.05)} {_fmt(y - 0.05)} {_fmt(x + 0.05)} {_fmt(y + 0.05)} ;")
            add(f"      PLACED {side} ;")
            add("    END")
        add(f"  END {port}")
    add(f"  OBS")
    add("    LAYER M1 ;")
    add(f"    RECT {_fmt(plan.core.x1)} {_fmt(plan.core.y1)} "
        f"{_fmt(plan.core.x2)} {_fmt(plan.core.y2)} ;")
    add("  END")
    add(f"END {name}")
    add("")
    add(f"# {rows} rows, core {plan.core.width:.2f} x {plan.core.height:.2f} um")
    add("END LIBRARY")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@dataclass
class LefMacro:
    name: str
    width: float = 0.0
    height: float = 0.0
    cls: str = "CORE"
    pins: Dict[str, str] = field(default_factory=dict)      # pin -> direction


def read_lef(path: str) -> Dict[str, LefMacro]:
    """Parse the ``MACRO`` blocks of a LEF file (the subset this module writes)."""
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    macros: Dict[str, LefMacro] = {}
    current: Optional[LefMacro] = None
    current_pin: Optional[str] = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().rstrip(";").strip()
        if not line:
            continue
        parts = line.split()
        head = parts[0].upper()
        if head == "MACRO" and len(parts) > 1:
            current = LefMacro(name=parts[1])
        elif head == "END" and len(parts) > 1:
            if current is not None and parts[1] == current.name:
                macros[current.name] = current
                current = None
            elif current_pin is not None and parts[1] == current_pin:
                current_pin = None
        elif current is None:
            continue
        elif head == "SIZE" and len(parts) >= 4:
            current.width = float(parts[1])
            current.height = float(parts[3])
        elif head == "CLASS" and len(parts) > 1:
            current.cls = parts[1].upper()
        elif head == "PIN" and len(parts) > 1:
            current_pin = parts[1]
            current.pins.setdefault(current_pin, "INPUT")
        elif head == "DIRECTION" and current_pin and len(parts) > 1:
            current.pins[current_pin] = parts[1].upper()
    return macros
