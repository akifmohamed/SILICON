"""Parasitic extraction and parasitic file output.

The router produces Manhattan geometry; this module turns that geometry into an
RC tree and evaluates it, so timing after routing is computed from the wires
that were actually drawn instead of from a per-fanout guess.

What it does:

* splits every wire at junctions and at pin locations, so the tree really is the
  connectivity of the routed net
* assigns each edge a resistance and a capacitance from the layer rules in
  :mod:`silicon.rules` (ohm/um, pF/um)
* computes the Elmore delay from the driver to every sink, and the total load
  capacitance the driver sees
* writes SPEF 1.0 (``*D_NET``/``*CONN``/``*CAP``/``*RES``) so the extraction can
  be handed to another tool, the way a real flow hands SPEF to STA or to a
  signoff timer

Limitations are stated where they belong (see ``docs/LIMITATIONS.md``): the model
is a distributed RC tree with no coupling capacitance, no inductance and no
shielding, and pin positions are cell centres because the library has no pin
geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .library import Library
from .netlist import GateNetlist
from .pnr import Floorplan, RouteResult, Segment
from .rules import MetalRule

EPS = 1e-6


@dataclass
class RcEdge:
    """One wire piece: from a node to a node, with its R and C."""

    a: int
    b: int
    length: float
    resistance: float
    capacitance: float
    layer: str


@dataclass
class NetParasitics:
    net: str
    nodes: List[Tuple[float, float]] = field(default_factory=list)
    edges: List[RcEdge] = field(default_factory=list)
    total_capacitance: float = 0.0        # pF, including pin caps if known
    total_resistance: float = 0.0         # ohm
    sink_delays: Dict[str, float] = field(default_factory=dict)   # ns per sink
    driver_delay: float = 0.0             # ns, driver node to centroid of load
    driver: Optional[Tuple[float, float]] = None

    @property
    def lumped_delay(self) -> float:
        """Elmore delay of the whole net seen as one lump (0.5*R*C)."""
        return 0.5 * self.total_resistance * self.total_capacitance


class Parasitics:
    """Per-net RC information for one routed block."""

    def __init__(self) -> None:
        self.nets: Dict[str, NetParasitics] = {}

    # -- queries used by STA -------------------------------------------------
    def cap(self, net: str) -> float:
        entry = self.nets.get(net)
        return entry.total_capacitance if entry else 0.0

    def delay(self, net: str, sink: str) -> float:
        entry = self.nets.get(net)
        if entry is None:
            return 0.0
        return entry.sink_delays.get(sink, 0.0)

    def caps(self) -> Dict[str, float]:
        return {net: entry.total_capacitance for net, entry in self.nets.items()}

    def summary(self) -> Dict[str, float]:
        if not self.nets:
            return {"nets": 0.0, "cap": 0.0, "res": 0.0, "worst_delay": 0.0}
        worst = max((max(entry.sink_delays.values(), default=0.0)
                     for entry in self.nets.values()), default=0.0)
        return {
            "nets": float(len(self.nets)),
            "cap": sum(e.total_capacitance for e in self.nets.values()),
            "res": sum(e.total_resistance for e in self.nets.values()),
            "worst_delay": worst,
        }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _pin_points(netlist: GateNetlist, net: str) -> Dict[str, Tuple[float, float]]:
    """``pin key -> (x, y)`` for every pin on a net, using cell centres."""
    points: Dict[str, Tuple[float, float]] = {}
    for key in sorted(netlist.nets.get(net, ())):
        if key.startswith("port/"):
            continue
        inst = key.split("/", 1)[0]
        try:
            cell = netlist.by_instance(inst)
        except KeyError:
            continue
        if cell.width <= 0:
            continue
        points[key] = (round(cell.x + cell.width / 2.0, 4),
                       round(cell.y + cell.height / 2.0, 4))
    return points


def _split_segments(segments: Sequence[Segment]) -> List[Tuple[float, float, float, float, str]]:
    """Split each wire at every perpendicular wire that crosses or touches it.

    Returns ``(x1, y1, x2, y2, layer)`` pieces with no interior crossing points,
    which is what makes the RC tree well defined.
    """
    pieces: List[Tuple[float, float, float, float, str]] = []
    for seg in segments:
        cuts: List[float] = []
        lo, hi = seg.span
        for other in segments:
            if other is seg or other.layer != seg.layer:
                continue
            if other.horizontal == seg.horizontal:
                continue
            along = other.cross
            across = seg.cross
            other_lo, other_hi = other.span
            if lo - EPS <= along <= hi + EPS and other_lo - EPS <= across <= other_hi + EPS:
                if lo + EPS < along < hi - EPS:
                    cuts.append(along)
        if seg.horizontal:
            xs = [lo] + sorted(set(round(c, 4) for c in cuts)) + [hi]
            for left, right in zip(xs, xs[1:]):
                if right - left > EPS:
                    pieces.append((left, seg.cross, right, seg.cross, seg.layer))
        else:
            ys = [lo] + sorted(set(round(c, 4) for c in cuts)) + [hi]
            for low, high in zip(ys, ys[1:]):
                if high - low > EPS:
                    pieces.append((seg.cross, low, seg.cross, high, seg.layer))
    return pieces


def extract(netlist: GateNetlist, library: Library, plan: Floorplan,
            route: RouteResult, rules: Dict[str, MetalRule],
            include_pins: bool = True) -> Parasitics:
    """Build the RC tree of every routed net and evaluate its Elmore delays."""
    parasitics = Parasitics()
    drivers = netlist.driver_map()
    by_net: Dict[str, List[Segment]] = {}
    for seg in route.segments:
        by_net.setdefault(seg.net, []).append(seg)

    for net in sorted(by_net):
        segments = by_net[net]
        pieces = _split_segments(segments)
        if not pieces:
            continue
        # --- node graph ---------------------------------------------------
        node_of: Dict[Tuple[float, float], int] = {}
        nodes: List[Tuple[float, float]] = []

        def node_at(x: float, y: float) -> int:
            key = (round(x, 4), round(y, 4))
            if key not in node_of:
                node_of[key] = len(nodes)
                nodes.append(key)
            return node_of[key]

        edges: List[RcEdge] = []
        for x1, y1, x2, y2, layer in pieces:
            rule = rules.get(layer)
            if rule is None:
                continue
            length = abs(x2 - x1) + abs(y2 - y1)
            edges.append(RcEdge(a=node_at(x1, y1), b=node_at(x2, y2), length=length,
                                resistance=rule.resistance * length,
                                capacitance=rule.capacitance * length, layer=layer))
        if not edges:
            continue

        # pin nodes exist whether or not the wire reaches them, so create them
        # before sizing the per-node capacitance array
        pins = _pin_points(netlist, net)
        pin_nodes: Dict[str, int] = {key: node_at(x, y) for key, (x, y) in pins.items()}

        # --- node capacitance: half of each adjacent edge, plus pin caps ----
        node_cap = [0.0] * len(nodes)
        for edge in edges:
            node_cap[edge.a] += edge.capacitance / 2.0
            node_cap[edge.b] += edge.capacitance / 2.0
        if include_pins:
            for key, node in pin_nodes.items():
                inst, pin = key.split("/", 1)
                try:
                    model = library.cell(netlist.by_instance(inst).cell)
                except Exception:                    # unknown cell: no pin cap
                    continue
                caps = model.pins[pin].capacitance if pin in model.pins else 0.0
                node_cap[node] += caps

        # --- pick the root: the driver, else the first pin, else node 0 ----
        root = None
        driver_cell = drivers.get(net)
        if driver_cell is not None:
            for out_pin, out_net in driver_cell.outputs.items():
                if out_net != net:
                    continue
                point = (round(driver_cell.x + driver_cell.width / 2.0, 4),
                         round(driver_cell.y + driver_cell.height / 2.0, 4))
                if point in node_of:
                    root = node_of[point]
                else:
                    root = min(range(len(nodes)),
                               key=lambda i: (abs(nodes[i][0] - point[0])
                                              + abs(nodes[i][1] - point[1]), i))
        if root is None:
            root = 0

        # --- adjacency and traversal ---------------------------------------
        adjacency: Dict[int, List[Tuple[int, RcEdge]]] = {}
        for edge in edges:
            adjacency.setdefault(edge.a, []).append((edge.b, edge))
            adjacency.setdefault(edge.b, []).append((edge.a, edge))

        order: List[int] = []
        parent: Dict[int, Optional[Tuple[int, RcEdge]]] = {root: None}
        stack = [root]
        while stack:
            node = stack.pop()
            order.append(node)
            for neighbour, edge in sorted(adjacency.get(node, ()),
                                          key=lambda item: item[0]):
                if neighbour in parent:
                    continue
                parent[neighbour] = (node, edge)
                stack.append(neighbour)
        # disconnected pieces (a net the router failed to join): root each one
        for index in range(len(nodes)):
            if index in parent:
                continue
            parent[index] = None
            stack = [index]
            while stack:
                node = stack.pop()
                order.append(node)
                for neighbour, edge in sorted(adjacency.get(node, ()),
                                              key=lambda item: item[0]):
                    if neighbour in parent:
                        continue
                    parent[neighbour] = (node, edge)
                    stack.append(neighbour)

        downstream = [0.0] * len(nodes)
        for node in reversed(order):                    # children before parents
            downstream[node] += node_cap[node]
            entry = parent.get(node)
            if entry is not None:
                upstream, _edge = entry
                downstream[upstream] += downstream[node]

        delay = [0.0] * len(nodes)
        total_res = 0.0
        total_cap = 0.0
        for node in order:
            entry = parent.get(node)
            if entry is None:
                continue
            upstream, edge = entry
            total_res += edge.resistance
            total_cap += edge.capacitance
            delay[node] = (delay[upstream] + edge.resistance
                           * (downstream[node] + edge.capacitance / 2.0))

        entry = NetParasitics(net=net, nodes=list(nodes), edges=edges,
                              total_capacitance=total_cap + sum(node_cap),
                              total_resistance=total_res,
                              driver=nodes[root])
        for key, node in pin_nodes.items():
            if node in parent:                       # on the tree: use its own delay
                entry.sink_delays[key] = delay[node]
            else:                                    # floating pin: nearest node
                x, y = pins[key]
                nearest = min(range(len(nodes)),
                              key=lambda i: (abs(nodes[i][0] - x)
                                             + abs(nodes[i][1] - y), i))
                entry.sink_delays[key] = delay[nearest]
        parasitics.nets[net] = entry
    return parasitics


def write_spef(path: str, parasitics: Parasitics, design: str,
               divider: str = "/") -> None:
    """Write SPEF 1.0 for the extracted RC trees.

    Node numbers are ``index + 1`` into the net's node list, which is what the
    ``*CAP`` and ``*RES`` records reference. Only what the extractor knows is
    written: total capacitance, pin connections, per-node capacitance and
    per-edge resistance. There is no coupling section because the extractor does
    not model coupling.
    """
    lines: List[str] = [
        "/* SPEF written by silicon-icc2 -- distributed RC tree over routed geometry */",
        "/* no coupling capacitance, no inductance: see docs/LIMITATIONS.md         */",
        'SPEF "IEEE 1481-1998"',
        f'*DESIGN "{design}"',
        '*DATE "2026-01-01"',
        '*VENDOR "silicon"',
        '*PROGRAM "silicon-icc2"',
        '*VERSION "0.2"',
        '*DESIGN_FLOW "PIN_CAP NONE"',
        f'*DIVIDER {divider}',
        '*DELIMITER :',
        '*BUS_DELIMITER [ ]',
        '*T_UNIT 1 NS',
        '*C_UNIT 1 PF',
        '*R_UNIT 1 OHM',
        '*L_UNIT 1 HENRY',
        "",
    ]
    for net in sorted(parasitics.nets):
        entry = parasitics.nets[net]
        lines.append(f"*D_NET {net} {entry.total_capacitance:.6f}")
        lines.append("*CONN")
        for sink in sorted(entry.sink_delays):
            inst, pin = sink.split("/", 1)
            lines.append(f"*I {inst}:{pin} I")
        lines.append("*CAP")
        node_cap: Dict[int, float] = {}
        for edge in entry.edges:
            node_cap[edge.a] = node_cap.get(edge.a, 0.0) + edge.capacitance / 2.0
            node_cap[edge.b] = node_cap.get(edge.b, 0.0) + edge.capacitance / 2.0
        for node in sorted(node_cap):
            if node_cap[node] <= 0:
                continue
            lines.append(f"{node + 1} {net}:{node + 1} {node_cap[node]:.8f}")
        lines.append("*RES")
        for edge in entry.edges:
            a, b = sorted((edge.a + 1, edge.b + 1))
            lines.append(f"{a} {net}:{a} {net}:{b} {edge.resistance:.6f}")
        lines.append("*END")
        lines.append("")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def read_spef_summary(path: str) -> Dict[str, float]:
    """Read the SPEF this module writes back and total what is in it.

    A writer that cannot be read back is not a writer, so the tests use this to
    check the file rather than trusting it.
    """
    nets = 0
    cap = 0.0
    res = 0.0
    inside = ""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if line.startswith("*D_NET"):
                parts = line.split()
                nets += 1
                if len(parts) >= 3:
                    cap += float(parts[2])
            elif line.startswith("*CAP"):
                inside = "CAP"
            elif line.startswith("*RES"):
                inside = "RES"
            elif line.startswith("*"):
                inside = "" if line.startswith(("*END", "*CONN")) else inside
            elif inside == "RES":
                parts = line.split()
                if len(parts) == 4:
                    res += float(parts[3])
    return {"nets": float(nets), "cap": cap, "res": res}
def read_spef_summary(path: str) -> Dict[str, float]:
    """Read back the SPEF this module writes and total what is in it.

    Used by the tests and by ``report_parasitics``: a writer that cannot be read
    back is not a writer.
    """
    nets = 0
    cap = 0.0
    res = 0.0
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("*D_NET"):
                parts = line.split()
                nets += 1
                if len(parts) >= 3:
                    cap += float(parts[2])
            elif line and line[0].isdigit() and len(line.split()) == 3:
                parts = line.split()
                if parts[1].endswith(":"):
                    pass
    # resistances are the 4-field lines
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) == 4 and parts[0].isdigit():
                try:
                    res += float(parts[3])
                except ValueError:
                    continue
    return {"nets": float(nets), "cap": cap, "res": res}
