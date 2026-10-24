"""Netlist model shared by the synthesis and place-and-route engines.

Everything is stored at single-bit granularity (``dout[3]``), which keeps
slicing, concatenation and port connections exact: they are wire aliases rather
than logic. Word-level operators survive as *macro cells* (``ADD``, ``LT``,
``MUX2`` ...) with a declared width, so the timing engine can price them the way
a real DesignWare-style component would be priced.

Two representations exist:

``WordDesign``
    Output of elaboration: macro cells, aliases, signal table, inferred
    clocks and asynchronous resets.

``GateNetlist``
    Output of technology mapping: every cell is a library cell with an area,
    leakage power and Liberty timing arcs. This is what place-and-route
    consumes and what DEF/GDSII are written from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

CONST0 = "1'b0"
CONST1 = "1'b1"


class UnionFind:
    """Union-find over bit names; resolves wire aliases after elaboration."""

    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def add(self, name: str) -> None:
        if name not in self.parent:
            self.parent[name] = name

    def find(self, name: str) -> str:
        self.add(name)
        root = name
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[name] != root:          # path compression
            self.parent[name], name = root, self.parent[name]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Constants never get merged away from each other; prefer the
        # alphabetically smaller root purely for deterministic output.
        if ra < rb:
            self.parent[rb] = ra
        else:
            self.parent[ra] = rb

    def resolve(self, name: str) -> str:
        root = self.find(name)
        return root


# ---------------------------------------------------------------------------
# Elaboration output
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    name: str            # flat name, e.g. "u_alu.result"
    width: int
    direction: str = "internal"     # input | output | internal
    bits: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.bits:
            if self.width == 1:
                self.bits = [self.name]
            else:
                self.bits = [f"{self.name}[{i}]" for i in range(self.width)]


@dataclass
class MacroCell:
    name: str
    type: str                         # ADD SUB MUL AND OR XOR XNOR NOT SHL SHR
                                      # EQ NE LT LE GT GE MUX2 REDOR DFF DFFR
                                      # DFFE RADDR BUF
    width: int
    inputs: Dict[str, List[str]]      # port name -> bit list (LSB first)
    outputs: Dict[str, List[str]]


@dataclass
class WordDesign:
    top: str
    signals: Dict[str, Signal] = field(default_factory=dict)
    cells: List[MacroCell] = field(default_factory=list)
    aliases: List[Tuple[str, str]] = field(default_factory=list)
    clocks: Dict[str, float] = field(default_factory=dict)     # clock port -> period
    async_resets: Set[str] = field(default_factory=set)
    params: Dict[str, int] = field(default_factory=dict)
    ports: List[str] = field(default_factory=list)
    port_dirs: Dict[str, str] = field(default_factory=dict)

    # -- queries ---------------------------------------------------------
    def bit_width(self, sig: str) -> int:
        return self.signals[sig].width

    def cell_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for c in self.cells:
            counts[c.type] = counts.get(c.type, 0) + 1
        return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Gate-level netlist (post technology mapping)
# ---------------------------------------------------------------------------


@dataclass
class Pin:
    """One physical pin of a placed cell instance."""

    name: str            # library pin name
    direction: str       # input | output
    net: str             # resolved net name


@dataclass
class GateCell:
    instance: str                       # unique instance name
    cell: str                           # library cell name, e.g. "NAND2X1"
    inputs: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, str] = field(default_factory=dict)
    # physical state, filled in by the place-and-route engine
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    fixed: bool = False
    orientation: str = "N"
    # provenance: which macro cell and bit slice this came from
    origin: str = ""
    bit_span: Tuple[int, int] = (0, 0)

    @property
    def is_sequential(self) -> bool:
        return self.cell.startswith("DFF")


@dataclass
class GateNetlist:
    top: str
    cells: List[GateCell] = field(default_factory=list)
    nets: Dict[str, Set[str]] = field(default_factory=dict)      # net -> pin keys
    ports: Dict[str, str] = field(default_factory=dict)          # port name -> net
    port_dirs: Dict[str, str] = field(default_factory=dict)
    constants: Dict[str, int] = field(default_factory=lambda: {CONST0: 0, CONST1: 1})

    def add_cell(self, cell: GateCell) -> GateCell:
        self.cells.append(cell)
        for pin, net in cell.inputs.items():
            self.nets.setdefault(net, set()).add(f"{cell.instance}/{pin}")
        for pin, net in cell.outputs.items():
            self.nets.setdefault(net, set()).add(f"{cell.instance}/{pin}")
        return cell

    def by_instance(self, name: str) -> GateCell:
        for c in self.cells:
            if c.instance == name:
                return c
        raise KeyError(name)

    def fanin(self, net: str) -> List[GateCell]:
        owners = []
        for key in sorted(self.nets.get(net, ())):  # "inst/pin"
            inst = key.split("/", 1)[0]
            cell = self.by_instance(inst)
            if net in cell.outputs.values():
                owners.append(cell)
        return owners

    def fanout(self, net: str) -> List[Tuple[GateCell, str]]:
        sinks = []
        for key in sorted(self.nets.get(net, ())):
            inst, pin = key.split("/", 1)
            cell = self.by_instance(inst)
            if pin in cell.inputs:
                sinks.append((cell, pin))
        return sinks

    # -- statistics ------------------------------------------------------
    def sequential_cells(self) -> List[GateCell]:
        return [c for c in self.cells if c.is_sequential]

    def driver_map(self) -> Dict[str, GateCell]:
        drivers: Dict[str, GateCell] = {}
        for c in self.cells:
            for net in c.outputs.values():
                drivers[net] = c
        return drivers

    def bit_nets(self) -> Dict[str, List[str]]:
        """Group nets back into buses using the ``name[i]`` convention."""
        buses: Dict[str, List[str]] = {}
        for net in self.nets:
            if net.endswith("]") and "[" in net:
                base, idx = net.rsplit("[", 1)
                try:
                    buses.setdefault(base, []).append(net)
                except KeyError:                 # pragma: no cover - defensive
                    buses[base] = [net]
        return buses
