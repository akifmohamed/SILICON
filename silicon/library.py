"""Liberty (.lib) parser and timing models.

Only the subset the engines need is implemented, but the subset is parsed from
real Liberty syntax rather than hard-coded:

    library() cell() pin() timing()  area  cell_leakage_power  capacitance
    direction  clock  max_transition  max_capacitance  related_pin
    timing_type  cell_rise / cell_fall / rise_transition / fall_transition
    rise_constraint / fall_constraint  index_1  index_2  values

Delays are looked up with bilinear interpolation over
(input net transition, total output net capacitance), which is what the NLDM
delay_model in a modern library does. Anything outside the characterised window
is clamped, and clamped lookups are reported by :meth:`Table.lookup_with_status`
so the timing engine can flag extrapolation instead of hiding it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
DEFAULT_LIB = os.path.join(LIB_DIR, "stdcells_32nm.lib")


class LibertyError(Exception):
    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(f"line {line}: {message}" if line else message)



# ---------------------------------------------------------------------------
# Generic Liberty group tree
# ---------------------------------------------------------------------------


@dataclass
class Group:
    kind: str
    args: List[str] = field(default_factory=list)
    attrs: Dict[str, List[str]] = field(default_factory=dict)
    groups: List["Group"] = field(default_factory=list)


_TOKEN = re.compile(
    r"""
      (?P<comment>/\*.*?\*/|//[^\n]*)
    | (?P<string>"(?:[^"\\]|\\.)*")
    | (?P<number>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_.\[\]!]*)
    | (?P<punct>[{}():;,])
    """,
    re.VERBOSE | re.DOTALL,
)


def _lex(text: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            if text[pos].isspace() or text[pos] == "\\":
                pos += 1
                continue
            line = text.count("\n", 0, pos) + 1
            raise LibertyError(f"unexpected character {text[pos]!r} at {pos}", line)
        pos = m.end()
        kind = m.lastgroup
        if kind == "comment":
            continue
        tokens.append((kind, m.group(0)))
    tokens.append(("eof", ""))
    return tokens


class _LibertyParser:
    """Recursive-descent parser for the Liberty group/attribute grammar."""

    def __init__(self, tokens: Sequence[Tuple[str, str]]) -> None:
        self.t = list(tokens)
        self.i = 0
        self.line = 0                  # Liberty positions are not tracked

    def peek(self, offset: int = 0) -> Tuple[str, str]:
        j = min(self.i + offset, len(self.t) - 1)
        return self.t[j]

    def next(self) -> Tuple[str, str]:
        tok = self.t[self.i]
        if self.i < len(self.t) - 1:
            self.i += 1
        return tok

    def expect(self, value: str) -> None:
        kind, tok = self.next()
        if tok != value:
            raise LibertyError(f"expected {value!r} but found {tok!r}")

    def parse_top(self) -> List[Group]:
        groups: List[Group] = []
        while self.peek()[0] != "eof":
            kind, tok = self.peek()
            if kind == "ident" and self.peek(1)[1] == "(":
                groups.append(self.parse_group())
            else:
                self.next()
        return groups

    def _group_follows(self) -> bool:
        """True when the current ``ident ( ... )`` is followed by ``{``."""
        if self.peek(1)[1] != "(":
            return False
        depth = 0
        j = self.i + 1
        while j < len(self.t):
            tok = self.t[j][1]
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
                if depth == 0:
                    return j + 1 < len(self.t) and self.t[j + 1][1] == "{"
            j += 1
        return False

    def parse_group(self) -> Group:
        kind, name = self.next()
        self.expect("(")
        args: List[str] = []
        while self.peek()[1] != ")":
            akind, aval = self.next()
            if aval != ",":
                args.append(aval.strip('"'))
        self.expect(")")
        group = Group(name, args, {}, [])
        self.expect("{")
        self.parse_body(group)
        return group

    def parse_body(self, group: Group) -> None:
        while True:
            kind, tok = self.peek()
            if kind == "eof":
                return
            if tok == "}":
                self.next()
                return
            if kind == "ident" and self.peek(1)[1] == "(":
                if self._group_follows():
                    group.groups.append(self.parse_group())
                    continue
                # attribute with parenthesised value list: name (a, b) ;
                name = tok
                self.next()
                self.expect("(")
                values: List[str] = []
                while self.peek()[1] != ")" and self.peek()[0] != "eof":
                    vkind, vtok = self.next()
                    if vtok != ",":
                        values.append(vtok.strip('"'))
                self.expect(")")
                self.expect(";")
                group.attrs.setdefault(name, []).extend(values)
                continue
            if kind == "ident" and self.peek(1)[1] == ":":
                name = tok
                self.next()
                self.next()                      # ":"
                values = []
                while True:
                    vkind, vtok = self.peek()
                    if vkind == "eof":
                        return
                    if vtok == ";":
                        self.next()
                        break
                    if vtok in ("}", "{"):
                        raise LibertyError(
                            f"attribute {name!r} has no value", self.line)
                    self.next()
                    if vtok == ",":
                        continue
                    values.append(vtok.strip('"'))
                if not values:
                    raise LibertyError(f"attribute {name!r} has no value", self.line)
                group.attrs.setdefault(name, []).extend(values)
                continue
            self.next()                          # tolerate unknown syntax


# ---------------------------------------------------------------------------
# Timing tables
# ---------------------------------------------------------------------------


@dataclass
class Table:
    index_1: List[float]
    index_2: List[float]
    values: List[List[float]]

    CONSTANT = False

    def lookup(self, x: float, y: float) -> Tuple[float, bool]:
        """Bilinear interpolation with clamping. Returns (value, clamped)."""
        if self.CONSTANT or (len(self.index_1) <= 1 and len(self.index_2) <= 1):
            return self.values[0][0], False
        clamped = False
        i0, fx, c1 = _bracket(self.index_1, x)
        j0, fy, c2 = _bracket(self.index_2, y)
        clamped = c1 or c2
        i1 = min(i0 + 1, len(self.index_1) - 1)
        j1 = min(j0 + 1, len(self.index_2) - 1)
        v00 = self.values[i0][j0]
        v01 = self.values[i0][j1]
        v10 = self.values[i1][j0]
        v11 = self.values[i1][j1]
        top = v00 + (v01 - v00) * fy
        bot = v10 + (v11 - v10) * fy
        return top + (bot - top) * fx, clamped

    @classmethod
    def constant(cls, value: float) -> "Table":
        table = cls([0.0], [0.0], [[value]])
        table.CONSTANT = True
        return table


def _bracket(index: Sequence[float], x: float) -> Tuple[int, float, bool]:
    n = len(index)
    if n == 1:
        return 0, 0.0, False
    if x <= index[0]:
        return 0, 0.0, x < index[0]
    if x >= index[-1]:
        return n - 2, 1.0, x > index[-1]
    for k in range(n - 1):
        lo, hi = index[k], index[k + 1]
        if lo <= x <= hi:
            span = hi - lo
            return k, (0.0 if span == 0 else (x - lo) / span), False
    return n - 2, 1.0, True


@dataclass
class Arc:
    from_pin: str
    to_pin: str
    timing_type: str            # combinational | setup_rising | hold_rising | ...
    rise: Table
    fall: Table
    related_clock: str = ""


@dataclass
class LibPin:
    name: str
    direction: str
    capacitance: float = 0.0
    max_capacitance: float = 0.0
    max_transition: float = 0.0
    is_clock: bool = False


@dataclass
class LibCell:
    name: str
    area: float
    leakage: float = 0.0                    # nW
    pins: Dict[str, LibPin] = field(default_factory=dict)
    arcs: List[Arc] = field(default_factory=list)
    site_width: float = 0.19                # filled in from the library
    row_height: float = 2.0

    # -- helpers ---------------------------------------------------------
    def input_pins(self) -> List[str]:
        return [p for p, v in self.pins.items() if v.direction == "input"]

    def output_pins(self) -> List[str]:
        return [p for p, v in self.pins.items() if v.direction == "output"]

    def arc(self, from_pin: str, to_pin: str,
            timing_type: str = "combinational") -> Optional[Arc]:
        for arc in self.arcs:
            if arc.from_pin == from_pin and arc.to_pin == to_pin:
                if timing_type == "combinational" and arc.timing_type.startswith(
                        ("setup", "hold", "recovery", "removal")):
                    continue
                return arc
        return None

    def setup_tables(self) -> Tuple[Optional[Table], Optional[Table]]:
        rise = fall = None
        for arc in self.arcs:
            if arc.timing_type.startswith("setup"):
                rise, fall = arc.rise, arc.fall
        return rise, fall

    def hold_tables(self) -> Tuple[Optional[Table], Optional[Table]]:
        rise = fall = None
        for arc in self.arcs:
            if arc.timing_type.startswith("hold"):
                rise, fall = arc.rise, arc.fall
        return rise, fall

    @property
    def width(self) -> float:
        """Cell width derived from area and row height, snapped to the site grid."""
        sites = max(1, round(self.area / (self.row_height * self.site_width)))
        return sites * self.site_width


@dataclass
class Library:
    name: str
    cells: Dict[str, LibCell] = field(default_factory=dict)
    site_width: float = 0.19
    row_height: float = 2.0
    corner: str = ""
    units: Dict[str, str] = field(default_factory=dict)

    def cell(self, name: str) -> LibCell:
        if name not in self.cells:
            raise LibertyError(f"library has no cell {name!r}")
        return self.cells[name]

    def has(self, name: str) -> bool:
        return name in self.cells

    def total_area(self) -> float:
        return sum(c.area for c in self.cells.values())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _numbers(values: Sequence[str]) -> List[float]:
    """Split Liberty value strings on commas/spaces: '"1, 2"' -> [1.0, 2.0]."""
    out: List[float] = []
    for chunk in values:
        for part in re.split(r"[,\s]+", chunk.strip()):
            if part:
                out.append(float(part))
    return out


def _table_from_group(group: Group) -> Table:
    idx1 = _numbers(group.attrs.get("index_1", ["0"]))
    idx2 = _numbers(group.attrs.get("index_2", ["0"]))
    raw = _numbers(group.attrs.get("values", []))
    values: List[List[float]] = []
    if raw:
        # values arrive flattened row-major
        cols = max(1, len(idx2))
        for r in range(0, len(raw), cols):
            values.append(list(raw[r:r + cols]))
    if not values:
        values = [[0.0]]
    idx1 = idx1 or [0.0]
    idx2 = idx2 or [0.0]
    while len(values) < len(idx1):
        values.append(list(values[-1]))
    if len(idx1) == 1 and len(idx2) == 1:
        table = Table(idx1, idx2, values)
        table.CONSTANT = True
        return table
    return Table(idx1, idx2, values)


def parse_liberty(text: str, name: str = "") -> Library:
    groups = _LibertyParser(_lex(text)).parse_top()
    lib_group = next((g for g in groups if g.kind == "library"), None)
    if lib_group is None:
        raise LibertyError("no library() group found")
    lib = Library(lib_group.args[0] if lib_group.args else name or "library")
    attrs = dict(lib_group.attrs)
    lib.corner = " ".join(attrs.get("nom_voltage", []) + attrs.get("nom_temperature", []))
    if "site_width" in attrs:
        lib.site_width = float(attrs["site_width"][0])
    if "row_height" in attrs:
        lib.row_height = float(attrs["row_height"][0])

    for cell_group in lib_group.groups:
        if cell_group.kind != "cell":
            continue
        cell = LibCell(
            name=cell_group.args[0],
            area=float(_attr(cell_group, "area", "1.0")),
            leakage=float(_attr(cell_group, "cell_leakage_power", "0")),
            site_width=lib.site_width,
            row_height=lib.row_height,
        )
        for pin_group in cell_group.groups:
            if pin_group.kind != "pin":
                continue
            pin = LibPin(
                name=pin_group.args[0],
                direction=_attr(pin_group, "direction", "input"),
                capacitance=float(_attr(pin_group, "capacitance", "0")),
                max_capacitance=float(_attr(pin_group, "max_capacitance", "0")),
                max_transition=float(_attr(pin_group, "max_transition", "0")),
                is_clock=_attr(pin_group, "clock", "false").lower() == "true",
            )
            cell.pins[pin.name] = pin
            for timing_group in pin_group.groups:
                if timing_group.kind != "timing":
                    continue
                timing_type = _attr(timing_group, "timing_type", "combinational")
                rise_group = _find(timing_group, ("cell_rise", "rise_constraint"))
                fall_group = _find(timing_group, ("cell_fall", "fall_constraint"))
                cell.arcs.append(Arc(
                    from_pin=_attr(timing_group, "related_pin", ""),
                    to_pin=pin.name,
                    timing_type=timing_type,
                    rise=_table_from_group(rise_group) if rise_group else Table.constant(0.0),
                    fall=_table_from_group(fall_group) if fall_group else Table.constant(0.0),
                ))
        lib.cells[cell.name] = cell
    if not lib.cells:
        raise LibertyError("library contains no cells")
    return lib


def _find(group: Group, kinds: Iterable[str]) -> Optional[Group]:
    for sub in group.groups:
        if sub.kind in kinds:
            return sub
    return None


def _attr(group: Group, name: str, default: str = "") -> str:
    values = group.attrs.get(name)
    if not values:
        return default
    return values[0]


def read_library(path: str) -> Library:
    with open(path, "r", encoding="utf-8") as handle:
        lib = parse_liberty(handle.read(), os.path.basename(path))
    lib.corner = lib.corner or os.path.basename(path)
    return lib


def default_library() -> Library:
    """Load the bundled synthetic 32 nm library."""
    lib = read_library(DEFAULT_LIB)
    lib.corner = "ss_0p95V_125C"
    return lib
