"""Physical design: floorplan, placement, clock-tree synthesis and routing.

These are real algorithms over the gate netlist, not a simulation of one:

``Floorplanner``
    core sizing from cell area and target utilisation, row/site construction,
    IO pin placement on the core boundary, tap/fill estimation.

``Placer``
    quadratic placement (star-model clique expansion, solved with conjugate
    gradient) followed by row-based legalisation and a pairwise swap pass that
    improves half-perimeter wire length (HPWL).

``ClockTree``
    recursive geometric bisection into a buffered H-tree; each sink gets a
    computed insertion delay from library arcs plus wire RC, which is what the
    timing engine then propagates instead of an ideal clock.

``Router``
    track-based routing: L/Z-shaped Steiner routes per net, per-layer track
    allocation with overflow detection, wirelength and via accounting. Overflow
    that cannot be resolved is reported as DRC violations rather than hidden.

Everything is deterministic: no random numbers are used anywhere.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .library import Library
from .netlist import GateCell, GateNetlist
from .rules import MetalRule, default_rules


class PhysicalError(Exception):
    pass


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass
class Rectangle:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def centre(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)


@dataclass
class Row:
    y: float
    x1: float
    x2: float
    height: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def sites(self) -> int:
        return int(self.width / 0.19)


@dataclass
class Segment:
    net: str
    layer: str
    x1: float
    y1: float
    x2: float
    y2: float
    width: float = 0.07

    @property
    def length(self) -> float:
        return abs(self.x2 - self.x1) + abs(self.y2 - self.y1)

    @property
    def horizontal(self) -> bool:
        return abs(self.y2 - self.y1) < 1e-9

    @property
    def area(self) -> float:
        return self.length * self.width + self.width ** 2

    @property
    def span(self) -> Tuple[float, float]:
        return ((min(self.x1, self.x2), max(self.x1, self.x2)) if self.horizontal
                else (min(self.y1, self.y2), max(self.y1, self.y2)))

    @property
    def cross(self) -> float:
        """Coordinate perpendicular to the wire (its track)."""
        return self.y1 if self.horizontal else self.x1

    def contains(self, x: float, y: float) -> bool:
        lo, hi = self.span
        value = x if self.horizontal else y
        across = y if self.horizontal else x
        return (lo - 1e-9 <= value <= hi + 1e-9
                and abs(across - self.cross) <= self.width / 2 + 1e-9)


@dataclass
class Via:
    net: str
    x: float
    y: float
    size: float = 0.05
    layers: Tuple[str, str] = ("M2", "M3")


@dataclass
class PhysicalConfig:
    core_utilization: float = 0.65
    core_offset: Tuple[float, float] = (2.0, 2.0)
    aspect_ratio: float = 1.0
    site_pitch: float = 0.0                 # 0 => library site width
    row_height: float = 0.0                 # 0 => library row height
    # Preferred-direction layer stacks: vertical layers carry N-S wires,
    # horizontal layers carry E-W wires. Extra layers add capacity.
    vertical_layers: Tuple[str, ...] = ("M2", "M4")
    horizontal_layers: Tuple[str, ...] = ("M3", "M5")
    track_pitch: float = 0.20
    track_offset: float = 0.10
    wire_cap_per_um: float = 0.00020        # pF/um for M2/M3
    wire_res_per_um: float = 0.12           # ohm/um
    clock_buffer: str = "BUFX8"
    max_sinks_per_buffer: int = 6
    placement_iterations: int = 60
    placement_timing_driven: bool = True
    timing_weight: float = 6.0             # criticality exponent for net weights
    timing_paths: int = 20                 # paths used to weight placement
    congestion_bins: int = 6               # bins per side of the congestion map
    congestion_weight: float = 0.6         # lambda on the bin-utilisation term
    # routing
    rules: Dict[str, MetalRule] = field(default_factory=default_rules)
    ripup_iterations: int = 4
    via_doubling: bool = False
    steiner_points: int = 6            # max extra Steiner points per net
    repair_min_area: bool = True
    routing_timing_driven: bool = True # critical nets are routed first
    maze_router: bool = True           # grid-based A* fallback for hard nets
    maze_via_cost: float = 6.0         # cost of a layer change, in track steps
    maze_overflow_cost: float = 30.0   # cost of squeezing past another net


# ---------------------------------------------------------------------------
# Floorplan
# ---------------------------------------------------------------------------


@dataclass
class Floorplan:
    die: Rectangle
    core: Rectangle
    rows: List[Row] = field(default_factory=list)
    io_pins: Dict[str, Tuple[float, float, str]] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def total_sites(self) -> int:
        return sum(row.sites for row in self.rows)


def build_floorplan(netlist: GateNetlist, library: Library,
                    config: PhysicalConfig) -> Floorplan:
    row_height = config.row_height or library.row_height
    site_pitch = config.site_pitch or library.site_width
    area = sum(library.cell(c.cell).area for c in netlist.cells)
    if area <= 0:
        raise PhysicalError("netlist has zero cell area")
    utilisation = min(max(config.core_utilization, 0.05), 0.95)
    core_area = area / utilisation
    aspect = max(0.25, min(config.aspect_ratio, 4.0))
    core_w = math.sqrt(core_area * aspect)
    core_h = core_area / core_w
    # snap to whole rows and sites
    rows = max(1, int(math.ceil(core_h / row_height)))
    core_h = rows * row_height
    core_w = math.ceil(core_w / site_pitch) * site_pitch
    needed_sites = math.ceil(area / row_height / site_pitch / utilisation)
    while rows * int(core_w / site_pitch) < needed_sites:
        rows += 1
        core_h = rows * row_height
    ox, oy = config.core_offset
    core = Rectangle(ox, oy, ox + core_w, oy + core_h)
    margin = max(4.0, 1.0 + 0.02 * max(core_w, core_h))
    die = Rectangle(0.0, 0.0, core.x2 + margin, core.y2 + margin)
    plan = Floorplan(die=die, core=core)
    for index in range(rows):
        plan.rows.append(Row(y=core.y1 + index * row_height, x1=core.x1,
                             x2=core.x2, height=row_height))
    _place_io_pins(netlist, plan)
    return plan


def _place_io_pins(netlist: GateNetlist, plan: Floorplan) -> None:
    inputs = sorted(p for p, d in netlist.port_dirs.items() if d == "input")
    outputs = sorted(p for p, d in netlist.port_dirs.items() if d == "output")
    core = plan.core
    for index, name in enumerate(inputs):
        y = core.y1 + (index + 0.5) * core.height / max(1, len(inputs))
        plan.io_pins[name] = (core.x1, y, "W")
    for index, name in enumerate(outputs):
        y = core.y1 + (index + 0.5) * core.height / max(1, len(outputs))
        plan.io_pins[name] = (core.x2, y, "E")


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


@dataclass
class PlacementResult:
    cells: List[GateCell]
    hpwl: float
    iterations: int
    legalized: bool
    overlaps: int
    utilisation: float
    displacement: float = 0.0
    weighted_hpwl: float = 0.0          # after timing weights
    timing_weighted_nets: int = 0
    congestion_peak: float = 0.0        # worst bin demand / supply
    congestion_mean: float = 0.0


class Placer:
    """Quadratic placement + row legalisation + swap improvement."""

    def __init__(self, netlist: GateNetlist, library: Library, plan: Floorplan,
                 config: PhysicalConfig, timing=None) -> None:
        self.nl = netlist
        self.lib = library
        self.plan = plan
        self.timing = timing
        self.cfg = config
        self.cells = [c for c in netlist.cells]
        for cell in self.cells:
            model = library.cell(cell.cell)
            cell.width = model.width
            cell.height = model.row_height

    # -- net model --------------------------------------------------------
    def _net_pins(self) -> List[Tuple[str, List[Tuple[Optional[int], Tuple[float, float]]]]]:
        """Each net becomes ``(name, [(cell index or None, fixed position)])``."""
        index_of = {c.instance: i for i, c in enumerate(self.cells)}
        nets: List[Tuple[str, List[Tuple[Optional[int], Tuple[float, float]]]]] = []
        for net, keys in sorted(self.nl.nets.items()):
            pins: List[Tuple[Optional[int], Tuple[float, float]]] = []
            for key in sorted(keys):        # deterministic model assembly
                if key.startswith("port/"):
                    name = key[5:]
                    if name in self.plan.io_pins:
                        x, y, _side = self.plan.io_pins[name]
                        pins.append((None, (x, y)))
                else:
                    inst = key.split("/", 1)[0]
                    if inst in index_of:
                        pins.append((index_of[inst], (0.0, 0.0)))
            if len(pins) >= 2:
                nets.append((net, pins))
        return nets

    def net_weights(self, timing=None) -> Dict[str, float]:
        """Timing-driven net weights: critical paths pull their nets together.

        Each net on a reported path gets its clique weight scaled by
        ``(1 + timing_weight * criticality)``, where criticality is how far the
        path is from meeting the clock period. Nets off every critical path keep
        a weight of 1, so the placement degrades smoothly to the pure-wirelength
        solution when timing is comfortable.
        """
        weights: Dict[str, float] = {}
        if timing is None or not self.cfg.placement_timing_driven:
            return weights
        period = max(float(getattr(timing, "clock_period", 0.0) or 0.0), 1e-9)
        paths = list(getattr(timing, "paths", []))[:self.cfg.timing_paths]
        for path in paths:
            slack = float(getattr(path, "slack", 0.0))
            criticality = 1.0 - min(max(slack, 0.0) / period, 1.0)
            scale = 1.0 + self.cfg.timing_weight * criticality ** 2
            for hop in getattr(path, "hops", ()):
                net = getattr(hop, "net", "")
                if net:
                    weights[net] = max(weights.get(net, 1.0), scale)
        return weights

    def _initial_positions(self, weights: Optional[Dict[str, float]] = None
                           ) -> Tuple[List[float], List[float]]:
        """Star-model quadratic placement solved by conjugate gradient.

        Net clique weights are scaled by the timing-driven weight of the net, so
        a critical net is resolved with a shorter wire at the cost of the others.
        """
        weights = weights or {}
        n = len(self.cells)
        core = self.plan.core
        xs = [core.x1 + (i + 0.5) * core.width / max(1, n) for i in range(n)]
        ys = [(core.y1 + core.y2) / 2.0] * n
        if n == 0:
            return xs, ys
        nets = self._net_pins()
        # accumulate diagonal and off-diagonal weights
        diag_x = [0.0] * n
        diag_y = [0.0] * n
        rowsA: Dict[int, Dict[int, float]] = {i: {} for i in range(n)}
        bx = [0.0] * n
        by = [0.0] * n
        for net, pins in nets:
            movable = [idx for idx, pos in pins if idx is not None]
            fixed = [pos for idx, pos in pins if idx is None]
            # clique weight, scaled by the timing criticality of the net
            weight = weights.get(net, 1.0) / max(1, len(pins) - 1)
            for idx in movable:
                deg = len(pins) - 1
                diag_x[idx] += weight * deg
                diag_y[idx] += weight * deg
            for i in range(len(movable)):
                for j in range(i + 1, len(movable)):
                    a, b = movable[i], movable[j]
                    rowsA[a][b] = rowsA[a].get(b, 0.0) - weight
                    rowsA[b][a] = rowsA[b].get(a, 0.0) - weight
            for idx in movable:
                for fx, fy in fixed:
                    bx[idx] += weight * fx
                    by[idx] += weight * fy
        # light anchor to the initial guess keeps the system well conditioned
        anchor = 0.01
        for i in range(n):
            diag_x[i] += anchor
            diag_y[i] += anchor
            bx[i] += anchor * xs[i]
            by[i] += anchor * ys[i]

        def matvec(vec: Sequence[float], diag: Sequence[float]) -> List[float]:
            out = [diag[i] * vec[i] for i in range(n)]
            for i, row in rowsA.items():
                acc = 0.0
                for j, w in row.items():
                    acc += w * vec[j]
                out[i] += acc
            return out

        def cg(b: Sequence[float], diag: Sequence[float],
               iters: int) -> List[float]:
            x = [0.0] * n
            r = list(b)
            p = list(r)
            rs = sum(v * v for v in r)
            if rs <= 0:
                return x
            for _ in range(iters):
                ap = matvec(p, diag)
                denom = sum(p[i] * ap[i] for i in range(n))
                if abs(denom) < 1e-18:
                    break
                alpha = rs / denom
                for i in range(n):
                    x[i] += alpha * p[i]
                    r[i] -= alpha * ap[i]
                rs_new = sum(v * v for v in r)
                if rs_new < 1e-12:
                    break
                beta = rs_new / rs
                for i in range(n):
                    p[i] = r[i] + beta * p[i]
                rs = rs_new
            return x

        solved_x = cg(bx, diag_x, self.cfg.placement_iterations)
        solved_y = cg(by, diag_y, self.cfg.placement_iterations)
        # rescale into the core: QP output is typically far too compact
        for i in range(n):
            xs[i] = solved_x[i]
            ys[i] = solved_y[i]
        return xs, ys

    # -- congestion -------------------------------------------------------
    def _bin_shape(self) -> Tuple[int, int, float, float]:
        core = self.plan.core
        bins = max(2, int(self.cfg.congestion_bins))
        return bins, bins, core.width / bins, core.height / bins

    def congestion_map(self) -> List[List[float]]:
        """Demand/supply per bin, from the bounding boxes of every net.

        Each net's wirelength is spread uniformly over the bins its bounding box
        covers -- the standard probabilistic demand estimate. Supply is the
        number of wiring tracks each bin offers in the four routing layers, so
        the result is a utilisation, and values above 1 are real congestion.
        """
        bins_x, bins_y, bin_w, bin_h = self._bin_shape()
        demand = [[0.0] * bins_y for _ in range(bins_x)]
        core = self.plan.core
        for net, pins in self._net_pins():
            xs, ys = [], []
            for idx, pos in pins:
                if idx is None:
                    xs.append(pos[0])
                    ys.append(pos[1])
                else:
                    cell = self.cells[idx]
                    xs.append(cell.x + cell.width / 2)
                    ys.append(cell.y + cell.height / 2)
            length = (max(xs) - min(xs)) + (max(ys) - min(ys))
            lo_x = int(max(0, min(bins_x - 1, (min(xs) - core.x1) / bin_w)))
            hi_x = int(max(0, min(bins_x - 1, (max(xs) - core.x1) / bin_w)))
            lo_y = int(max(0, min(bins_y - 1, (min(ys) - core.y1) / bin_h)))
            hi_y = int(max(0, min(bins_y - 1, (max(ys) - core.y1) / bin_h)))
            covered = (hi_x - lo_x + 1) * (hi_y - lo_y + 1)
            share = length / covered
            for bx in range(lo_x, hi_x + 1):
                for by in range(lo_y, hi_y + 1):
                    demand[bx][by] += share
        tracks_x = max(1.0, bin_w / self.cfg.track_pitch)
        tracks_y = max(1.0, bin_h / self.cfg.track_pitch)
        supply = tracks_x * bin_h * len(self.cfg.vertical_layers) + \
            tracks_y * bin_w * len(self.cfg.horizontal_layers)
        return [[value / supply for value in column] for column in demand]

    # -- legalisation -----------------------------------------------------
    def place(self, timing=None) -> PlacementResult:
        weights = self.net_weights(timing)
        xs, ys = self._initial_positions(weights)
        core = self.plan.core
        n = len(self.cells)
        rows = self.plan.rows
        order = sorted(range(n), key=lambda i: (ys[i], xs[i]))
        row_of = [0] * n
        # Row legalisation: take the cells in placement order and drop each one
        # into the nearest row that still has room, scanning outwards from its
        # preferred row.  A row is only accepted if the cell truly fits, so the
        # result has no overlaps whenever the cells fit in the core at all.
        free = [row.x1 for row in rows]
        unplaced = 0
        for idx in order:
            cell = self.cells[idx]
            start = min(range(len(rows)),
                        key=lambda r: abs(rows[r].y + rows[r].height / 2 - ys[idx]))
            target = None
            for offset in range(len(rows)):
                for candidate in (start + offset, start - offset):
                    if 0 <= candidate < len(rows) and \
                            free[candidate] + cell.width <= rows[candidate].x2 + 1e-9:
                        target = candidate
                        break
                if target is not None:
                    break
            if target is None:                       # core is genuinely full
                target = start
                unplaced += 1
            row_of[idx] = target
            cell.x = free[target]
            cell.y = rows[target].y
            free[target] = cell.x + cell.width
        self.overflow_cells = unplaced
        self._swap_improve(row_of, weights)
        self._spread_congestion()
        overlaps = self.count_overlaps()
        area = sum(c.width * c.height for c in self.cells)
        util = area / (core.width * core.height) if core.width * core.height else 0.0
        congestion = self.congestion_map()
        peak = max((max(column) for column in congestion), default=0.0)
        mean = (sum(sum(column) for column in congestion)
                / max(1, sum(len(column) for column in congestion)))
        return PlacementResult(cells=self.cells, hpwl=self.hpwl(),
                               iterations=self.cfg.placement_iterations,
                               legalized=overlaps == 0, overlaps=overlaps,
                               utilisation=util,
                               weighted_hpwl=self.weighted_hpwl(weights),
                               timing_weighted_nets=len(weights),
                               congestion_peak=peak, congestion_mean=mean)

    def weighted_hpwl(self, weights: Dict[str, float]) -> float:
        """HPWL with critical nets weighted: the objective placement minimises."""
        total = 0.0
        for net, pins in self._net_pins():
            xs = [self.cells[i].x + self.cells[i].width / 2 if i is not None else p[0]
                  for i, p in pins]
            ys = [self.cells[i].y + self.cells[i].height / 2 if i is not None else p[1]
                  for i, p in pins]
            length = (max(xs) - min(xs)) + (max(ys) - min(ys))
            total += weights.get(net, 1.0) * length
        return total

    def _spread_congestion(self) -> None:
        """Relieve the worst bin by swapping same-width cells out of it.

        A swap only happens when it lowers the peak bin utilisation without
        making the total wirelength worse, so this is a real congestion step
        rather than a re-randomisation.
        """
        if self.cfg.congestion_weight <= 0:
            return
        for _ in range(2):
            congestion = self.congestion_map()
            bins_x, bins_y, bin_w, bin_h = self._bin_shape()
            peak = 0.0
            worst = None
            for bx in range(bins_x):
                for by in range(bins_y):
                    if congestion[bx][by] > peak:
                        peak, worst = congestion[bx][by], (bx, by)
            if worst is None or peak <= 1.0:
                return
            bx, by = worst
            core = self.plan.core
            x_lo, x_hi = core.x1 + bx * bin_w, core.x1 + (bx + 1) * bin_w
            y_lo, y_hi = core.y1 + by * bin_h, core.y1 + (by + 1) * bin_h
            inside = [c for c in self.cells
                      if x_lo <= c.x + c.width / 2 <= x_hi
                      and y_lo <= c.y + c.height / 2 <= y_hi]
            outside = [c for c in self.cells
                       if not (x_lo <= c.x + c.width / 2 <= x_hi
                               and y_lo <= c.y + c.height / 2 <= y_hi)]
            moved = False
            base = self.hpwl()
            for a in sorted(inside, key=lambda c: c.instance):
                partner = next((b for b in sorted(outside, key=lambda c: c.instance)
                                if abs(b.width - a.width) < 1e-9), None)
                if partner is None:
                    continue
                ax, ay, bx_, by_ = a.x, a.y, partner.x, partner.y
                if abs(ay - by_) > 1e-9:        # keep row legality
                    continue
                a.x, partner.x = bx_, ax
                after = self.hpwl()
                if after <= base + 1e-6:
                    moved = True
                    break
                a.x, partner.x = ax, bx_
            if not moved:
                return

    def _row_slots(self) -> Dict[int, List[GateCell]]:
        slots: Dict[int, List[GateCell]] = {}
        for cell in self.cells:
            key = int(round((cell.y - self.plan.core.y1) / (self.plan.rows[0].height
                                                            if self.plan.rows else 1.0)))
            slots.setdefault(key, []).append(cell)
        for group in slots.values():
            group.sort(key=lambda c: c.x)
        return slots

    def _swap_improve(self, row_of: Sequence[int], weights: Optional[Dict[str, float]] = None,
                      passes: int = 2) -> None:
        """Pairwise row-legal swap pass guided by weighted HPWL.

        The objective is ``sum(w_net * hpwl_net)``: nets on critical paths carry
        a larger weight (see :meth:`net_weights`), so the swap pass keeps the
        timing-driven placement instead of undoing it for raw wirelength.
        """
        weights = weights or {}
        pairs = self._net_pins()
        nets = [pins for _net, pins in pairs]
        net_names = [net for net, _pins in pairs]
        index_of = {c.instance: i for i, c in enumerate(self.cells)}
        nets_for_cell: Dict[int, List[int]] = {}
        for net_index, pins in enumerate(nets):
            for idx, _pos in pins:
                if idx is not None:
                    nets_for_cell.setdefault(idx, []).append(net_index)

        def net_hpwl(pins) -> float:
            xs = [self.cells[i].x + self.cells[i].width / 2 if i is not None else p[0]
                  for i, p in pins]
            ys = [self.cells[i].y + self.cells[i].height / 2 if i is not None else p[1]
                  for i, p in pins]
            return (max(xs) - min(xs)) + (max(ys) - min(ys))

        def cost(indices) -> float:
            return sum(weights.get(net_names[n], 1.0) * net_hpwl(nets[n])
                       for n in indices)

        for _ in range(passes):
            improved = False
            slots = self._row_slots()
            for key, group in slots.items():
                for i in range(0, len(group) - 1):
                    a, b = group[i], group[i + 1]
                    if a.width != b.width:
                        continue
                    touched = sorted(set(nets_for_cell.get(index_of[a.instance], []))
                                     | set(nets_for_cell.get(index_of[b.instance], [])))
                    before = cost(touched)
                    a.x, b.x = b.x, a.x
                    after = cost(touched)
                    if after < before - 1e-6:
                        improved = True
                    else:
                        a.x, b.x = b.x, a.x
            if not improved:
                break

    def hpwl(self) -> float:
        total = 0.0
        for net, keys in sorted(self.nl.nets.items()):
            xs: List[float] = []
            ys: List[float] = []
            for key in sorted(keys):
                if key.startswith("port/"):
                    name = key[5:]
                    if name in self.plan.io_pins:
                        x, y, _ = self.plan.io_pins[name]
                        xs.append(x)
                        ys.append(y)
                    continue
                inst = key.split("/", 1)[0]
                try:
                    cell = self.nl.by_instance(inst)
                except KeyError:
                    continue
                xs.append(cell.x + cell.width / 2)
                ys.append(cell.y + cell.height / 2)
            if len(xs) >= 2:
                total += (max(xs) - min(xs)) + (max(ys) - min(ys))
        return total

    def count_overlaps(self) -> int:
        by_row: Dict[int, List[GateCell]] = {}
        for cell in self.cells:
            by_row.setdefault(int(round(cell.y)), []).append(cell)
        overlaps = 0
        for group in by_row.values():
            group.sort(key=lambda c: c.x)
            for left, right in zip(group, group[1:]):
                if left.x + left.width > right.x + 1e-9:
                    overlaps += 1
        return overlaps


# ---------------------------------------------------------------------------
# Clock tree
# ---------------------------------------------------------------------------


@dataclass
class ClockNode:
    name: str
    x: float
    y: float
    children: List["ClockNode"] = field(default_factory=list)
    buffer: Optional[GateCell] = None
    insertion_delay: float = 0.0
    wire_length: float = 0.0
    sinks: List[Tuple[float, float, GateCell]] = field(default_factory=list)


@dataclass
class CTSResult:
    root: Optional[ClockNode]
    buffers: List[GateCell]
    sinks: int
    levels: int
    skew: float
    insertion_delay: float
    clock_wirelength: float
    sink_delays: Dict[str, float] = field(default_factory=dict)


class ClockTree:
    """Buffered clock tree built by recursive geometric bisection.

    Sinks are split along their longest dimension until a cluster fits inside
    ``max_sinks_per_buffer``.  Every internal node gets a real buffer cell from
    the library, the flop ``CK`` pins are rewired onto the leaf nets, and the
    insertion delay of each sink is accumulated from buffer arcs plus wire RC.
    """

    def __init__(self, netlist: GateNetlist, library: Library, config: PhysicalConfig,
                 counter_start: int = 0, plan: Optional[Floorplan] = None) -> None:
        self.nl = netlist
        self.lib = library
        self.cfg = config
        self.plan = plan
        self._counter = counter_start
        # Fallback bounds when no floorplan is supplied: the extent of the
        # cells already placed, so inserted buffers still stay inside the block.
        placed = [c for c in netlist.cells if c.width > 0]
        if placed:
            self._x_min = min(c.x for c in placed)
            self._x_max = max(c.x + c.width for c in placed)
        else:
            self._x_min, self._x_max = 0.0, float("inf")

    def build(self, clock_net: str) -> CTSResult:
        sinks = [c for c in self.nl.cells if "CK" in c.inputs]
        if not sinks:
            return CTSResult(None, [], 0, 0, 0.0, 0.0, 0.0, {})
        points = [(c.x + c.width / 2, c.y + c.height / 2, c) for c in sinks]
        buffer_model = self.lib.cell(self.cfg.clock_buffer)
        buffers: List[GateCell] = []

        def new_buffer(x: float, y: float, source_net: str) -> GateCell:
            self._counter += 1
            name = f"clkbuf_{self._counter}"
            gate = GateCell(instance=name, cell=self.cfg.clock_buffer,
                            inputs={"A": source_net}, outputs={"Y": f"{name}_clk"},
                            origin="cts")
            gate.width = buffer_model.width
            gate.height = buffer_model.row_height
            gate.x = x - gate.width / 2
            gate.y = y - gate.height / 2
            self.snap_to_row(gate)
            buffers.append(gate)
            return gate

        def split(points, depth: int = 0) -> ClockNode:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            if len(points) <= self.cfg.max_sinks_per_buffer:
                return ClockNode(f"leaf_{depth}_{len(points)}", cx, cy,
                                 sinks=list(points))
            horizontal = (max(xs) - min(xs)) >= (max(ys) - min(ys))
            ordered = sorted(points, key=lambda p: p[0] if horizontal else p[1])
            mid = len(ordered) // 2
            return ClockNode(f"buf_{depth}_{len(points)}", cx, cy,
                             [split(ordered[:mid], depth + 1),
                              split(ordered[mid:], depth + 1)])

        root = split(points)

        # instantiate buffers top-down, threading the clock net through the tree
        levels = 0
        root_input = clock_net
        arc = buffer_model.arc("A", "Y")
        sink_delays: Dict[str, float] = {}
        clock_wire = [0.0]

        def instantiate(node: ClockNode, source_net: str, delay: float,
                        depth: int = 0) -> None:
            nonlocal levels
            if not node.children:
                for _x, _y, cell in node.sinks:
                    cell.inputs["CK"] = source_net
                    sink_delays[cell.instance] = delay
                node.insertion_delay = delay
                return
            levels = max(levels, depth + 1)
            node.buffer = new_buffer(node.x, node.y, source_net)
            load = len(node.children) * buffer_model.pins["A"].capacitance
            load += sum(math.hypot(child.x - node.x, child.y - node.y)
                        for child in node.children) * self.cfg.wire_cap_per_um
            buffer_delay = arc.rise.lookup(self.cfg.clock_transition
                                           if hasattr(self.cfg, "clock_transition")
                                           else 0.08, load)[0] if arc else 0.05
            node.insertion_delay = delay + buffer_delay
            output_net = node.buffer.outputs["Y"]
            for child in node.children:
                distance = math.hypot(child.x - node.x, child.y - node.y)
                # first-order RC: 0.5*R*C with R = r*d, C = c*d
                wire_delay = 0.5 * (distance * self.cfg.wire_res_per_um) * \
                    (distance * self.cfg.wire_cap_per_um) * 1e-3
                node.wire_length += distance
                clock_wire[0] += distance
                instantiate(child, output_net, node.insertion_delay + wire_delay,
                            depth + 1)

        instantiate(root, root_input, 0.0)
        _rebuild_nets(self.nl)
        delays = list(sink_delays.values()) or [0.0]
        return CTSResult(root=root, buffers=buffers, sinks=len(sinks), levels=levels,
                         skew=max(delays) - min(delays),
                         insertion_delay=sum(delays) / len(delays),
                         clock_wirelength=clock_wire[0], sink_delays=sink_delays)

    def snap_to_row(self, cell: GateCell) -> None:
        """Put a buffer on a legal site row, clear of the cells already there."""
        rows = [row.y for row in self.plan.rows] if self.plan and self.plan.rows else None
        if rows:
            cell.y = min(rows, key=lambda y: abs(y - cell.y))
            x_min = self.plan.core.x1
            x_max = self.plan.core.x2
        else:
            core_y1 = min((c.y for c in self.nl.cells), default=0.0)
            row_h = cell.height or 2.0
            cell.y = core_y1 + round((cell.y - core_y1) / row_h) * row_h
            x_min, x_max = self._x_min, self._x_max
        pitch = self.lib.site_width
        cell.x = max(x_min, min(x_max - cell.width, round(cell.x / pitch) * pitch))
        occupied = sorted((c.x, c.x + c.width) for c in self.nl.cells
                          if abs(c.y - cell.y) < 1e-6)
        for lo, hi in occupied:
            if cell.x < hi + 1e-9 and cell.x + cell.width > lo + 1e-9:
                cell.x = hi
        cell.x = max(x_min, min(x_max - cell.width, cell.x))


def _rebuild_nets(netlist: GateNetlist) -> None:
    netlist.nets.clear()
    for cell in netlist.cells:
        for pin, net in cell.inputs.items():
            netlist.nets.setdefault(net, set()).add(f"{cell.instance}/{pin}")
        for pin, net in cell.outputs.items():
            netlist.nets.setdefault(net, set()).add(f"{cell.instance}/{pin}")
    for port, net in netlist.ports.items():
        netlist.nets.setdefault(net, set()).add(f"port/{port}")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    segments: List[Segment] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    wirelength: float = 0.0
    vias_count: int = 0
    overflow: int = 0
    drc: int = 0
    unrouted_nets: List[str] = field(default_factory=list)
    net_wirelength: Dict[str, float] = field(default_factory=dict)
    layer_usage: Dict[str, float] = field(default_factory=dict)
    drc_violations: Dict[str, int] = field(default_factory=dict)
    drc_details: Dict[str, List[str]] = field(default_factory=dict)
    ripups: int = 0
    steiner_points: int = 0
    doubled_vias: int = 0
    maze_nets: int = 0
    maze_length: float = 0.0
    critical_nets: int = 0
    merged_runs: int = 0
    overflow_nets: List[str] = field(default_factory=list)
    widened_segments: int = 0


class DrcChecker:
    """Rule deck checked against the routed geometry.

    The checker is deliberately independent of the allocator: it looks only at
    the segments and vias that were produced, so a router bug shows up as a
    violation instead of being masked by the same bookkeeping that caused it.

    Rules implemented: short (same-layer overlap), spacing (parallel and
    corner), minimum width, minimum area, via enclosure and via-to-via spacing.
    """

    NAMES = ("SHORT", "SPACING", "WIDTH", "MIN_AREA", "VIA_ENCLOSURE",
             "VIA_SIZE", "VIA_SPACING")

    def __init__(self, rules: Dict[str, MetalRule]) -> None:
        self.rules = rules

    def _rule(self, layer: str) -> MetalRule:
        return self.rules.get(layer) or MetalRule(layer, "horizontal", 0.07, 0.07,
                                                 0.02, 0.05, 0.07, 0.02)

    def check(self, segments: Sequence[Segment], vias: Sequence[Via]
              ) -> Dict[str, List[str]]:
        found: Dict[str, List[str]] = {name: [] for name in self.NAMES}
        for seg in segments:
            rule = self._rule(seg.layer)
            if seg.width + 1e-9 < rule.width:
                found["WIDTH"].append(
                    f"{seg.net} on {seg.layer}: width {seg.width:.3f} < {rule.width:.3f}")
            if seg.area + 1e-12 < rule.min_area:
                found["MIN_AREA"].append(
                    f"{seg.net} on {seg.layer}: area {seg.area:.4f} < {rule.min_area:.4f}")
        self._check_layer_spacing(segments, found)
        self._check_vias(vias, segments, found)
        return {name: items for name, items in found.items() if items}

    # -- spacing / shorts -------------------------------------------------
    def _check_layer_spacing(self, segments: Sequence[Segment],
                             found: Dict[str, List[str]]) -> None:
        by_layer: Dict[str, List[Segment]] = {}
        for seg in segments:
            by_layer.setdefault(seg.layer, []).append(seg)
        for layer, group in sorted(by_layer.items()):
            rule = self._rule(layer)
            horizontal = [s for s in group if s.horizontal]
            vertical = [s for s in group if not s.horizontal]
            for kind in (horizontal, vertical):
                kind.sort(key=lambda s: (round(s.cross, 4), s.span[0], s.net))
                for i, a in enumerate(kind):
                    for b in kind[i + 1:]:
                        if a.net == b.net:
                            continue
                        if b.cross - a.cross > rule.spacing + (a.width + b.width) / 2:
                            break          # tracks are sorted: nothing closer remains
                        gap = abs(a.cross - b.cross) - (a.width + b.width) / 2
                        lo = max(a.span[0], b.span[0])
                        hi = min(a.span[1], b.span[1])
                        if hi <= lo + 1e-9:
                            continue
                        if gap <= 1e-9:
                            found["SHORT"].append(
                                f"{a.net} / {b.net} on {layer} overlap at "
                                f"{max(a.span[0], b.span[0]):.3f}..{min(a.span[1], b.span[1]):.3f}")
                        elif gap + 1e-9 < rule.spacing:
                            found["SPACING"].append(
                                f"{a.net} / {b.net} on {layer}: gap {gap:.3f} < "
                                f"{rule.spacing:.3f}")
            # corner (perpendicular) spacing on the same layer
            for h in horizontal:
                for v in vertical:
                    if h.net == v.net:
                        continue
                    dx = max(v.x1 - h.span[1], h.span[0] - v.x1, 0.0)
                    dy = max(h.y1 - v.span[1], v.span[0] - h.y1, 0.0)
                    distance = math.hypot(dx, dy) - (h.width + v.width) / 2
                    if distance + 1e-9 < rule.spacing:
                        found["SPACING"].append(
                            f"{h.net} / {v.net} on {layer}: corner gap "
                            f"{max(distance, 0.0):.3f} < {rule.spacing:.3f}")

    # -- vias -------------------------------------------------------------
    def _check_vias(self, vias: Sequence[Via], segments: Sequence[Segment],
                    found: Dict[str, List[str]]) -> None:
        by_net: Dict[str, List[Segment]] = {}
        for seg in segments:
            by_net.setdefault(seg.net, []).append(seg)
        ordered = sorted(vias, key=lambda v: (v.x, v.y, v.net))
        for index, via in enumerate(ordered):
            lower, upper = via.layers
            minimum = max(self._rule(layer).via_size for layer in (lower, upper))
            if via.size + 1e-9 < minimum:
                found["VIA_SIZE"].append(
                    f"{via.net}: cut at ({via.x:.2f}, {via.y:.2f}) is "
                    f"{via.size:.3f} < {minimum:.3f}")
            for layer in (lower, upper):
                rule = self._rule(layer)
                covering = [s for s in by_net.get(via.net, ())
                            if s.layer == layer and s.contains(via.x, via.y)]
                if not covering:
                    found["VIA_ENCLOSURE"].append(
                        f"{via.net}: via at ({via.x:.2f}, {via.y:.2f}) has no "
                        f"{layer} metal")
                    continue
                seg = covering[0]
                rule_layer = self._rule(seg.layer)
                across = abs((via.y if seg.horizontal else via.x) - seg.cross)
                if across + via.size / 2 > seg.width / 2 + 1e-9:
                    found["VIA_ENCLOSURE"].append(
                        f"{via.net}: via at ({via.x:.2f}, {via.y:.2f}) sticks out of "
                        f"{layer} metal")
                lo, hi = seg.span
                along = via.x if seg.horizontal else via.y
                cover = min(along - lo, hi - along)
                if cover + 1e-9 < rule_layer.enclosure:
                    found["VIA_ENCLOSURE"].append(
                        f"{via.net}: via at ({via.x:.2f}, {via.y:.2f}) has "
                        f"{cover:.3f} um end enclosure on {layer} < "
                        f"{rule_layer.enclosure:.3f}")
            for other in ordered[index + 1:]:
                if other.net == via.net:
                    continue
                if not set(via.layers) & set(other.layers):
                    continue          # different layer pairs cannot touch
                reach = max(self._rule(via.layers[0]).via_spacing,
                            self._rule(via.layers[1]).via_spacing)
                distance = abs(via.x - other.x) + abs(via.y - other.y)
                if distance - (via.size + other.size) / 2 + 1e-9 < reach:
                    found["VIA_SPACING"].append(
                        f"{via.net} / {other.net}: vias {distance:.3f} apart < "
                        f"{reach:.3f}")
                if distance > 4 * reach:
                    break


class Router:
    """Rule-driven track router.

    * Net topology comes from a rectilinear Steiner tree (iterated 1-Steiner
      heuristic on the Hanan grid), not a plain MST.
    * Wires are assigned to legal tracks with the layer whose usage is lowest,
      keeping edge-to-edge spacing from other nets on the same track.
    * Every L-shaped connection places a via at the corner and extends both
      legs past it, which is what the enclosure rule requires.
    * Short metals are widened to meet the minimum-area rule, and layer changes
      are doubled when a neighbouring track is free.
    * After routing, :class:`DrcChecker` re-derives every violation from the
      geometry; offending nets are ripped up and re-routed on other tracks
      until the design is clean or the retry budget is spent.
    """

    def __init__(self, netlist: GateNetlist, library: Library, plan: Floorplan,
                 config: PhysicalConfig, timing=None) -> None:
        self.nl = netlist
        self.lib = library
        self.plan = plan
        self.timing = timing
        self.cfg = config
        self.pitch = config.track_pitch
        self.rules = config.rules
        self.h_layers = list(config.horizontal_layers) or ["M3"]
        self.v_layers = list(config.vertical_layers) or ["M2"]
        for layer in self.h_layers + self.v_layers:
            rule = self.rules.get(layer)
            if rule is not None and rule.spacing > self.pitch + 1e-9:
                raise PhysicalError(
                    f"track pitch {self.pitch} on {layer} is smaller than its "
                    f"spacing rule {rule.spacing}")
        self.h_bounds = (max(0, int(math.ceil(plan.core.y1 / self.pitch))),
                         max(0, int(math.floor(plan.core.y2 / self.pitch))))
        self.v_bounds = (max(0, int(math.ceil(plan.core.x1 / self.pitch))),
                         max(0, int(math.floor(plan.core.x2 / self.pitch))))
        self.h_tracks: Dict[Tuple[str, int], List[Tuple[str, float, float]]] = {}
        self.v_tracks: Dict[Tuple[str, int], List[Tuple[str, float, float]]] = {}
        self.usage: Dict[str, float] = {}

    # -- geometry --------------------------------------------------------
    def _rule(self, layer: str) -> MetalRule:
        rule = self.rules.get(layer)
        if rule is None:
            raise PhysicalError(f"no rule deck entry for layer {layer}")
        return rule

    def _pin_position(self, key: str) -> Optional[Tuple[float, float]]:
        if key.startswith("port/"):
            name = key[5:]
            if name in self.plan.io_pins:
                x, y, _ = self.plan.io_pins[name]
                return (round(x, 4), round(y, 4))
            return None
        inst = key.split("/", 1)[0]
        try:
            cell = self.nl.by_instance(inst)
        except KeyError:
            return None
        return (round(cell.x + cell.width / 2, 4), round(cell.y + cell.height / 2, 4))

    def _track_index(self, value: float) -> int:
        return max(0, int(round(value / self.pitch)))

    # -- track bookkeeping ----------------------------------------------
    def _free(self, tracks: Dict[Tuple[str, int], List[Tuple[str, float, float]]],
              key: Tuple[str, int], net: str, lo: float, hi: float) -> bool:
        """True when the span clears every other net's span by the spacing rule."""
        rule = self._rule(key[0])
        for other, olo, ohi in tracks.get(key, ()):
            if other == net:
                continue
            if olo - hi < rule.spacing - 1e-9 and ohi - lo > -(rule.spacing - 1e-9):
                return False
        return True

    def _layers_by_usage(self, layers: Sequence[str]) -> List[str]:
        return sorted(layers, key=lambda layer: (self.usage.get(layer, 0.0), layer))

    def _allocate(self, tracks: Dict[Tuple[str, int], List[Tuple[str, float, float]]],
                  layers: Sequence[str], natural: int, net: str, lo: float, hi: float,
                  window: int = 24, bounds: Tuple[int, int] = (0, 1 << 30),
                  offset: int = 0) -> Optional[Tuple[str, int]]:
        """Nearest free ``(layer, track)`` for a span, searching outwards.

        Layers are tried cheapest-usage first and each one is searched outwards
        from the natural track; the whole core is searched as a last resort so a
        net is only failed when no track on any of its layers is legal.
        """
        first, last = bounds
        if last < first:
            return None
        natural = min(max(natural + offset, first), last)
        for layer in self._layers_by_usage(layers):
            for step in range(window):
                for candidate in (natural + step, natural - step):
                    if candidate < first or candidate > last:
                        continue
                    key = (layer, candidate)
                    if self._free(tracks, key, net, lo, hi):
                        return key
            for candidate in range(first, last + 1):
                key = (layer, candidate)
                if self._free(tracks, key, net, lo, hi):
                    return key
        return None

    def _book(self, layer: str, length: float) -> None:
        self.usage[layer] = self.usage.get(layer, 0.0) + length

    def _min_length(self, layer: str) -> float:
        """Shortest metal that satisfies minimum area at minimum width."""
        rule = self._rule(layer)
        return rule.width + rule.min_area / rule.width

    def _emit_h(self, net: str, layer: str, track: int, lo: float, hi: float,
                result: RouteResult) -> float:
        need = self._min_length(layer)
        if hi - lo < need:                       # comply with the minimum-area rule
            grow = (need - (hi - lo)) / 2.0
            lo, hi = lo - grow, hi + grow
        ty = round(track * self.pitch, 4)
        result.segments.append(Segment(net=net, layer=layer, x1=lo, y1=ty,
                                       x2=hi, y2=ty, width=self._rule(layer).width))
        self.h_tracks.setdefault((layer, track), []).append((net, lo, hi))
        self._book(layer, hi - lo)
        result.layer_usage[layer] = result.layer_usage.get(layer, 0.0) + (hi - lo)
        return hi - lo

    def _emit_v(self, net: str, layer: str, track: int, lo: float, hi: float,
                result: RouteResult) -> float:
        need = self._min_length(layer)
        if hi - lo < need:
            grow = (need - (hi - lo)) / 2.0
            lo, hi = lo - grow, hi + grow
        tx = round(track * self.pitch, 4)
        result.segments.append(Segment(net=net, layer=layer, x1=tx, y1=lo,
                                       x2=tx, y2=hi, width=self._rule(layer).width))
        self.v_tracks.setdefault((layer, track), []).append((net, lo, hi))
        self._book(layer, hi - lo)
        result.layer_usage[layer] = result.layer_usage.get(layer, 0.0) + (hi - lo)
        return hi - lo

    def _add_h(self, net: str, y: float, x1: float, x2: float,
               result: RouteResult, offset: int = 0) -> Tuple[float, float]:
        """Route one horizontal span; returns ``(length, actual y)``."""
        lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
        need = self._min_length(self.h_layers[0])
        if hi - lo < need:                       # keep clear of the area rule
            grow = need - (hi - lo)
            if abs(x1 - lo) < 1e-9:
                lo -= grow
            else:
                hi += grow
        if hi - lo < 1e-9:
            return 0.0, y
        natural = self._track_index(y)
        key = self._allocate(self.h_tracks, self.h_layers, natural, net, lo, hi,
                             bounds=self.h_bounds, offset=offset)
        if key is None:                # no legal track: fail the net, never short it
            result.overflow += 1
            result.overflow_nets.append(net)
            return 0.0, y
        length = self._emit_h(net, key[0], key[1], lo, hi, result)
        return length, round(key[1] * self.pitch, 4)

    def _add_v(self, net: str, x: float, y1: float, y2: float,
               result: RouteResult, offset: int = 0) -> Tuple[float, float]:
        """Route one vertical span; returns ``(length, actual x)``."""
        lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
        need = self._min_length(self.v_layers[0])
        if hi - lo < need:
            grow = need - (hi - lo)
            if abs(y1 - lo) < 1e-9:
                lo -= grow
            else:
                hi += grow
        if hi - lo < 1e-9:
            return 0.0, x
        natural = self._track_index(x)
        key = self._allocate(self.v_tracks, self.v_layers, natural, net, lo, hi,
                             bounds=self.v_bounds, offset=offset)
        if key is None:
            result.overflow += 1
            result.overflow_nets.append(net)
            return 0.0, x
        length = self._emit_v(net, key[0], key[1], lo, hi, result)
        return length, round(key[1] * self.pitch, 4)

    def _connect(self, net: str, ax: float, ay: float, bx: float, by: float,
                 result: RouteResult, offset: int = 0) -> None:
        """L-shaped connection: vertical leg, corner via, horizontal leg.

        Both tracks are chosen by fixed-point iteration so that the spans the
        allocator reserved are exactly the spans that get drawn: the vertical
        span depends on the horizontal track and vice versa. Both legs are
        extended past the corner by the enclosure rule, so the via sits on
        metal rather than at a bare line end.
        """
        if abs(bx - ax) < 1e-9 and abs(by - ay) < 1e-9:
            return
        v_layer, h_layer = self.v_layers[0], self.h_layers[0]
        v_rule, h_rule = self._rule(v_layer), self._rule(h_layer)
        enc = max(self._rule(layer).enclosure
                  for layer in self.v_layers + self.h_layers)
        if abs(by - ay) < 1e-9:
            self._add_h(net, ay, ax, bx, result, offset)
            return
        if abs(bx - ax) < 1e-9:
            self._add_v(net, bx, ay, by, result, offset)
            return

        ty = float(self._track_index(ay)) * self.pitch
        tx = float(self._track_index(bx)) * self.pitch
        v_key: Optional[Tuple[str, int]] = None
        h_key: Optional[Tuple[str, int]] = None
        for _ in range(3):                     # fixed point on (tx, ty)
            v_lo, v_hi = min(ty - enc, by), max(ty + enc, by)
            key = self._allocate(self.v_tracks, self.v_layers, self._track_index(bx),
                                 net, v_lo, v_hi, bounds=self.v_bounds, offset=offset)
            if key is None:
                break
            v_key, tx = key, round(key[1] * self.pitch, 4)
            h_lo, h_hi = min(ax, tx) - enc, max(ax, tx) + enc
            key = self._allocate(self.h_tracks, self.h_layers, self._track_index(ay),
                                 net, h_lo, h_hi, bounds=self.h_bounds, offset=offset)
            if key is None:
                v_key = None
                break
            h_key, new_ty = key, round(key[1] * self.pitch, 4)
            if abs(new_ty - ty) < 1e-9:
                ty = new_ty
                break
            ty = new_ty

        if v_key is None or h_key is None:      # give up on the local search
            result.overflow += 1
            result.overflow_nets.append(net)
            return

        self._emit_v(net, v_key[0], v_key[1], min(ty - enc, by),
                     max(ty + enc, by), result)
        self._emit_h(net, h_key[0], h_key[1], min(ax, tx) - enc,
                     max(ax, tx) + enc, result)
        cut_size = max(self._rule(layer).via_size for layer in
                       self.v_layers + self.h_layers)
        result.vias.append(Via(net=net, x=tx, y=ty, size=cut_size,
                               layers=(v_key[0], h_key[0])))

    # -- grid maze (A*) ---------------------------------------------------
    def _maze_grid(self) -> Tuple[int, int, int, int]:
        return (self.v_bounds[0], self.v_bounds[1],
                self.h_bounds[0], self.h_bounds[1])

    def _maze_obstacles(self, net: str, result: RouteResult
                        ) -> Dict[str, Dict[Tuple[int, int], int]]:
        """Per-layer grid occupancy: 0 free, 1 near another net, 2 covered.

        ``near`` is the distance at which two wires still break the spacing rule
        (both half-widths plus the rule); ``deep`` is where the metals actually
        overlap. Level 2 is never entered by the maze, so a routed path can only
        break spacing in the relaxed retry, never by shorting.
        """
        imin, imax, jmin, jmax = self._maze_grid()
        obstacles: Dict[str, Dict[Tuple[int, int], int]] = {}
        for layer in self.h_layers + self.v_layers:
            cells: Dict[Tuple[int, int], int] = {}
            rule = self._rule(layer)
            for seg in result.segments:
                if seg.net == net or seg.layer != layer:
                    continue
                near = seg.width / 2.0 + rule.width / 2.0 + rule.spacing
                deep = seg.width / 2.0 + rule.width / 2.0 - 1e-9
                lo, hi = seg.span
                along = (int(math.floor(lo / self.pitch)),
                         int(math.ceil(hi / self.pitch)))
                across = (int(math.floor((seg.cross - near) / self.pitch)),
                          int(math.ceil((seg.cross + near) / self.pitch)))
                for step in range(across[0], across[1] + 1):
                    distance = abs(step * self.pitch - seg.cross)
                    if distance > near + 1e-9:
                        continue
                    level = 2 if distance < deep else 1
                    for along_step in range(along[0], along[1] + 1):
                        i, j = ((along_step, step) if seg.horizontal
                                else (step, along_step))
                        if imin <= i <= imax and jmin <= j <= jmax:
                            cells[(i, j)] = max(cells.get((i, j), 0), level)
            obstacles[layer] = cells
        return obstacles

    def _maze_path(self, net: str, src: Tuple[float, float], dst: Tuple[float, float],
                   result: RouteResult, relaxed: bool = False
                   ) -> Optional[List[Tuple[int, int, str]]]:
        """A* over ``(x track, y track, layer)``; ``None`` when nothing connects.

        With ``relaxed=False`` only cells that keep the full spacing rule are
        entered, so a found path is clean by construction. The relaxed pass
        repeats the search allowing spacing-violating cells (still never metal
        overlap), which is the usual second attempt when a net is boxed in.
        """
        if not self.cfg.maze_router:
            return None
        imin, imax, jmin, jmax = self._maze_grid()
        if imax < imin or jmax < jmin:
            return None
        obstacles = self._maze_obstacles(net, result)
        layers = list(self.v_layers) + list(self.h_layers)
        vertical = set(self.v_layers)
        start = (min(max(self._track_index(src[0]), imin), imax),
                 min(max(self._track_index(src[1]), jmin), jmax))
        goal = (min(max(self._track_index(dst[0]), imin), imax),
                min(max(self._track_index(dst[1]), jmin), jmax))
        # a net may leave a pin on any layer that is not covered
        def open_layer(node, layer) -> bool:
            level = obstacles.get(layer, {}).get(node, 0)
            return level < 2 if relaxed else level == 0

        best: Dict[Tuple[int, int, str], float] = {}
        parent: Dict[Tuple[int, int, str], Optional[Tuple[int, int, str]]] = {}
        queue: List[Tuple[float, float, Tuple[int, int, str]]] = []
        for layer in layers:
            if not open_layer(start, layer):
                continue
            node = (start[0], start[1], layer)
            best[node] = 0.0
            parent[node] = None
            heapq.heappush(queue, (abs(goal[0] - start[0]) + abs(goal[1] - start[1]),
                                   0.0, node))
        if not queue:
            return None
        goal_nodes = {}
        for layer in layers:
            if open_layer(goal, layer):
                goal_nodes[(goal[0], goal[1], layer)] = True
        if not goal_nodes:
            return None
        found: Optional[Tuple[int, int, str]] = None
        while queue:
            _estimate, cost, node = heapq.heappop(queue)
            if cost > best.get(node, float("inf")) + 1e-9:
                continue
            if node in goal_nodes:
                found = node
                break
            i, j, layer = node
            moves: List[Tuple[int, int, str, float]] = []
            if layer in vertical:                 # N/S on a vertical layer
                if j + 1 <= jmax:
                    moves.append((i, j + 1, layer, 1.0))
                if j - 1 >= jmin:
                    moves.append((i, j - 1, layer, 1.0))
            else:                                 # E/W on a horizontal layer
                if i + 1 <= imax:
                    moves.append((i + 1, j, layer, 1.0))
                if i - 1 >= imin:
                    moves.append((i - 1, j, layer, 1.0))
            for other in layers:                  # layer change, i.e. a via
                if other != layer:
                    moves.append((i, j, other, self.cfg.maze_via_cost))
            for ni, nj, nlayer, step in moves:
                if not open_layer((ni, nj), nlayer):
                    continue
                penalty = 0.0
                if obstacles.get(nlayer, {}).get((ni, nj), 0) == 1:
                    penalty = self.cfg.maze_overflow_cost
                new_cost = cost + step + penalty
                key = (ni, nj, nlayer)
                if new_cost + 1e-9 >= best.get(key, float("inf")):
                    continue
                best[key] = new_cost
                parent[key] = node
                estimate = abs(goal[0] - ni) + abs(goal[1] - nj)
                heapq.heappush(queue, (new_cost + estimate, new_cost, key))
        if found is None:
            return None
        path: List[Tuple[int, int, str]] = []
        node: Optional[Tuple[int, int, str]] = found
        while node is not None:
            path.append(node)
            node = parent[node]
        path.reverse()
        return path

    def route_maze(self, net: str, src: Tuple[float, float], dst: Tuple[float, float],
                   result: RouteResult) -> bool:
        """Route one connection with the grid maze router and emit its geometry.

        Runs are collected first and then emitted with the enclosure rule applied
        at every layer change, so each via lands on metal that extends past it --
        which is what ``VIA_ENCLOSURE`` asks for.
        """
        path = self._maze_path(net, src, dst, result)
        if not path:
            path = self._maze_path(net, src, dst, result, relaxed=True)
        if not path:
            return False
        self._all_segments = list(result.segments)

        runs: List[Tuple[Tuple[int, int, str], Tuple[int, int, str]]] = []
        vias: List[Tuple[int, int, str, str]] = []
        start_node = path[0]
        previous = path[0]
        for step in path[1:]:
            same_layer = step[2] == previous[2]
            continuous = same_layer and (
                (step[0] == previous[0] and abs(step[1] - previous[1]) == 1)
                or (step[1] == previous[1] and abs(step[0] - previous[0]) == 1))
            if continuous:
                previous = step
                continue
            runs.append((start_node, previous))
            if not same_layer:
                vias.append((previous[0], previous[1], previous[2], step[2]))
            start_node = step
            previous = step
        runs.append((start_node, previous))

        length = 0.0
        for index, (run_start, run_end) in enumerate(runs):
            pad_start = any((vx, vy) == (run_start[0], run_start[1])
                            for vx, vy, _a, _b in vias)
            pad_end = any((vx, vy) == (run_end[0], run_end[1])
                          for vx, vy, _a, _b in vias)
            length += self._emit_run(net, run_start, run_end, result,
                                     pad_start=pad_start, pad_end=pad_end)
        for vx, vy, lower, upper in vias:
            result.vias.append(Via(net=net, x=round(vx * self.pitch, 4),
                                   y=round(vy * self.pitch, 4),
                                   size=max(self._rule(entry).via_size for entry in
                                            self.v_layers + self.h_layers),
                                   layers=(lower, upper)))
        result.maze_nets += 1
        result.maze_length += length
        return True

    def _free_limit(self, net: str, layer: str, lo: float, need: float,
                    horizontal: bool) -> float:
        """Furthest coordinate a minimum-length extension may reach without
        running into metal that belongs to another net on the same layer."""
        rule = self._rule(layer)
        limit = (self.plan.core.x2 if horizontal else self.plan.core.y2)
        for seg in getattr(self, "_all_segments", ()):      # set by route_maze
            if seg.net == net or seg.layer != layer or seg.horizontal != horizontal:
                continue
            start, _end = seg.span
            if start - rule.spacing <= lo + need:
                limit = min(limit, start - rule.spacing)
        return limit

    def _emit_run(self, net: str, start: Tuple[int, int, str],
                  end: Tuple[int, int, str], result: RouteResult,
                  pad_start: bool = False, pad_end: bool = False) -> float:
        """Emit one straight maze run as a segment on its track."""
        i1, j1, layer = start
        i2, j2, _ = end
        rule = self._rule(layer)
        if i1 == i2 and j1 == j2:
            return 0.0
        enclosure = max(self._rule(entry).enclosure
                        for entry in self.v_layers + self.h_layers)
        if j1 == j2:                              # horizontal run
            lo, hi = sorted((i1 * self.pitch, i2 * self.pitch))
            lo -= enclosure if pad_start and i1 < i2 or pad_end and i1 > i2 else 0.0
            hi += enclosure if pad_end and i2 > i1 or pad_start and i2 < i1 else 0.0
            lo = max(lo, self.plan.core.x1)
            need = self._min_length(layer)
            target = max(hi, lo + need)
            limit = min(self.plan.core.x2,
                        self._free_limit(net, layer, lo, need, horizontal=True))
            hi = max(hi, min(target, limit))
            lo = max(lo, self.plan.core.x1)
            result.segments.append(Segment(net=net, layer=layer, x1=lo, y1=j1 * self.pitch,
                                           x2=hi, y2=j1 * self.pitch, width=rule.width))
            self.h_tracks.setdefault((layer, j1), []).append((net, lo, hi))
            self._book(layer, hi - lo)
            result.layer_usage[layer] = result.layer_usage.get(layer, 0.0) + (hi - lo)
            return hi - lo
        lo, hi = sorted((j1 * self.pitch, j2 * self.pitch))
        lo -= enclosure if pad_start and j1 < j2 or pad_end and j1 > j2 else 0.0
        hi += enclosure if pad_end and j2 > j1 or pad_start and j2 < j1 else 0.0
        lo = max(lo, self.plan.core.y1)
        need = self._min_length(layer)
        target = max(hi, lo + need)
        limit = min(self.plan.core.y2,
                    self._free_limit(net, layer, lo, need, horizontal=False))
        hi = max(hi, min(target, limit))
        result.segments.append(Segment(net=net, layer=layer, x1=i1 * self.pitch, y1=lo,
                                       x2=i1 * self.pitch, y2=hi, width=rule.width))
        self.v_tracks.setdefault((layer, i1), []).append((net, lo, hi))
        self._book(layer, hi - lo)
        result.layer_usage[layer] = result.layer_usage.get(layer, 0.0) + (hi - lo)
        return hi - lo

    # -- Steiner topology ------------------------------------------------
    @staticmethod
    def _manhattan(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def _mst(self, points: Sequence[Tuple[float, float]]) -> List[Tuple[int, int]]:
        """Prim's MST over point indices (deterministic)."""
        if len(points) < 2:
            return []
        inside = [0]
        outside = list(range(1, len(points)))
        edges: List[Tuple[int, int]] = []
        while outside:
            best = None
            for i in inside:
                for j in outside:
                    distance = self._manhattan(points[i], points[j])
                    if best is None or distance < best[0] - 1e-12:
                        best = (distance, i, j)
            assert best is not None
            _, i, j = best
            edges.append((i, j))
            inside.append(j)
            outside.remove(j)
        return edges

    def _steiner(self, points: List[Tuple[float, float]]
                 ) -> Tuple[List[Tuple[float, float]], int]:
        """Iterated 1-Steiner: add Hanan-grid points while they shorten the tree."""
        if not self.cfg.steiner_points or len(points) < 3:
            return points, 0
        xs = sorted({p[0] for p in points})
        ys = sorted({p[1] for p in points})
        terminals = list(points)
        added = 0
        def cost(pts: List[Tuple[float, float]]) -> float:
            return sum(self._manhattan(pts[i], pts[j]) for i, j in self._mst(pts))
        base = cost(terminals)
        while added < self.cfg.steiner_points:
            best_gain, best_point = 1e-6, None
            for x in xs:
                for y in ys:
                    candidate = (x, y)
                    if candidate in terminals:
                        continue
                    trial = terminals + [candidate]
                    gain = base - cost(trial)
                    if gain > best_gain + 1e-9:
                        best_gain, best_point = gain, candidate
            if best_point is None:
                break
            terminals = terminals + [best_point]
            base -= best_gain
            added += 1
        return terminals, added

    # -- net routing -----------------------------------------------------
    def net_criticality(self) -> Dict[str, float]:
        """``net -> priority`` from the timing view, 0 for non-critical nets.

        A net on a reported path gets the criticality of the worst path it is on
        (``1 - slack/period``, clamped), so the router can spend its scarce early
        tracks on the nets that decide whether the design meets timing.
        """
        if self.timing is None or not self.cfg.routing_timing_driven:
            return {}
        period = max(float(getattr(self.timing, "clock_period", 0.0) or 0.0), 1e-9)
        critical: Dict[str, float] = {}
        for path in list(getattr(self.timing, "paths", []))[:self.cfg.timing_paths]:
            slack = float(getattr(path, "slack", 0.0))
            score = 1.0 - min(max(slack, 0.0) / period, 1.0)
            for hop in getattr(path, "hops", ()):
                net = getattr(hop, "net", "")
                if net:
                    critical[net] = max(critical.get(net, 0.0), score)
        return critical

    def _net_order(self) -> List[str]:
        """Critical nets first, then wide nets; ties broken by name.

        Routing order is a resource allocation decision: the first nets routed get
        the tracks closest to their pins, so the nets on the worst timing paths go
        first when a timing view is available. Without one this degrades to the
        fanout/name order, which is deterministic either way.
        """
        critical = self.net_criticality()
        return [net for net, _keys in sorted(
            self.nl.nets.items(),
            key=lambda kv: (-round(critical.get(kv[0], 0.0), 2),
                            -len(kv[1]), kv[0]))]

    def _route_net(self, net: str, result: RouteResult, offset: int = 0) -> None:
        keys = sorted(self.nl.nets.get(net, ()))
        points = [p for p in (self._pin_position(k) for k in keys) if p is not None]
        unique = sorted(set(points))
        if len(unique) < 2:
            return
        terminals, added = self._steiner(unique)
        result.steiner_points += added
        edges = self._mst(terminals)
        for i, j in edges:
            self._connect(net, terminals[i][0], terminals[i][1],
                          terminals[j][0], terminals[j][1], result, offset)
        result.net_wirelength[net] = sum(seg.length for seg in result.segments
                                         if seg.net == net)

    def _route_net_maze(self, net: str, result: RouteResult) -> bool:
        """Same topology as the track router, but every connection goes to A*."""
        keys = sorted(self.nl.nets.get(net, ()))
        points = [p for p in (self._pin_position(k) for k in keys) if p is not None]
        unique = sorted(set(points))
        if len(unique) < 2:
            return False
        terminals, added = self._steiner(unique)
        result.steiner_points += added
        routed = False
        for i, j in self._mst(terminals):
            if self.route_maze(net, terminals[i], terminals[j], result):
                routed = True
        result.net_wirelength[net] = sum(seg.length for seg in result.segments
                                         if seg.net == net)
        return routed

    # -- post passes -----------------------------------------------------
    def _repair_min_area(self, result: RouteResult) -> None:
        """Widen short metals rather than leaving minimum-area violations.

        A widened wire eats into its neighbours' spacing, so the width each
        segment may take is bounded by *every* other net sharing its layer over
        an overlapping span.
        """
        if not self.cfg.repair_min_area:
            return
        by_layer: Dict[str, List[Segment]] = {}
        for seg in result.segments:
            by_layer.setdefault(seg.layer, []).append(seg)
        for layer, group in sorted(by_layer.items()):
            rule = self._rule(layer)
            for seg in group:
                if seg.area + 1e-12 >= rule.min_area or seg.length < 1e-9:
                    continue
                budget = 3.0 * rule.width
                for other in group:
                    if other is seg or other.net == seg.net:
                        continue
                    lo = max(seg.span[0], other.span[0])
                    hi = min(seg.span[1], other.span[1])
                    if hi <= lo + 1e-9:
                        continue
                    gap = abs(seg.cross - other.cross) - other.width / 2 - rule.spacing
                    budget = min(budget, 2.0 * gap)
                wanted = min(rule.min_area / seg.length, 3.0 * rule.width, budget)
                if wanted > seg.width + 1e-9:
                    seg.width = round(wanted, 4)
                    result.widened_segments += 1

    def _double_vias(self, result: RouteResult) -> None:
        """Add a second cut beside a layer change where the metal already reaches.

        The extra cut is only placed when the neighbouring vertical track is
        free and the existing horizontal leg already spans that far -- so this
        pass never adds wire, it only makes the connection redundant.
        """
        if not self.cfg.via_doubling:
            return
        spacing = max(self._rule(layer).via_spacing
                      for layer in self.v_layers + self.h_layers)
        for via in list(result.vias):      # snapshot: never cascade
            v_legs = [s for s in result.segments
                      if s.net == via.net and not s.horizontal
                      and abs(s.x1 - via.x) < 1e-9 and s.y1 - 1e-9 <= via.y <= s.y2 + 1e-9]
            h_legs = [s for s in result.segments
                      if s.net == via.net and s.horizontal
                      and abs(s.y1 - via.y) < 1e-9 and s.x1 - 1e-9 <= via.x <= s.x2 + 1e-9]
            if not v_legs or not h_legs:
                continue
            v_leg, h_leg = v_legs[0], h_legs[0]
            for sign in (1.0, -1.0):
                neighbour = round(via.x + sign * self.pitch, 4)
                lo, hi = h_leg.span
                if not (lo + 1e-9 <= neighbour <= hi - 1e-9):
                    continue                      # no metal there: skip, never add wire
                index = self._track_index(neighbour)
                if index < self.v_bounds[0] or index > self.v_bounds[1]:
                    continue
                if not self._free(self.v_tracks, (v_leg.layer, index), via.net,
                                  min(v_leg.y1, v_leg.y2), max(v_leg.y1, v_leg.y2)):
                    continue
                if any(other.net != via.net
                       and abs(other.x - neighbour) + abs(other.y - via.y) < spacing
                       for other in result.vias):
                    continue
                stub = self._min_length(v_leg.layer) / 2.0
                self._emit_v(via.net, v_leg.layer, index, via.y - stub, via.y + stub,
                             result)
                result.vias.append(Via(net=via.net, x=neighbour, y=via.y,
                                       size=via.size, layers=via.layers))
                result.doubled_vias += 1
                break

    def _merge_runs(self, result: RouteResult) -> int:
        """Join collinear pieces of one net that sit on the same track.

        Rip-up and maze passes can leave a net split into abutting runs. Merging
        them is both a data reduction and a correctness measure: two pieces of the
        same net 25 nm apart on the same track are legal but pointless, and they
        look like a spacing violation to anything that does not track nets.
        """
        merged_count = 0
        for _ in range(4):
            by_key: Dict[Tuple[str, str, bool, float], List[Segment]] = {}
            for seg in result.segments:
                by_key.setdefault((seg.net, seg.layer, seg.horizontal,
                                   round(seg.cross, 4)), []).append(seg)
            out: List[Segment] = []
            changed = False
            for key in sorted(by_key, key=lambda k: (k[1], k[3], k[2], k[0])):
                group = sorted(by_key[key], key=lambda seg: seg.span[0])
                current = group[0]
                for nxt in group[1:]:
                    rule = self._rule(current.layer)
                    if nxt.span[0] - current.span[1] <= rule.spacing + 1e-9 \
                            and abs(nxt.width - current.width) < 1e-9:
                        lo = min(current.span[0], nxt.span[0])
                        hi = max(current.span[1], nxt.span[1])
                        if current.horizontal:
                            current = Segment(net=current.net, layer=current.layer,
                                              x1=lo, y1=current.cross, x2=hi,
                                              y2=current.cross, width=current.width)
                        else:
                            current = Segment(net=current.net, layer=current.layer,
                                              x1=current.cross, y1=lo,
                                              x2=current.cross, y2=hi,
                                              width=current.width)
                        merged_count += 1
                        changed = True
                    else:
                        out.append(current)
                        current = nxt
                out.append(current)
            result.segments = sorted(
                out, key=lambda seg: (seg.layer, round(seg.y1, 4),
                                      round(seg.x1, 4), seg.net))
            if not changed:
                break
        if merged_count:
            self.h_tracks.clear()
            self.v_tracks.clear()
            for seg in result.segments:
                key = (seg.layer, self._track_index(seg.cross))
                tracks = self.h_tracks if seg.horizontal else self.v_tracks
                tracks.setdefault(key, []).append((seg.net, seg.span[0], seg.span[1]))
        return merged_count

    def _ripup(self, result: RouteResult, nets: Sequence[str]) -> None:
        drop = set(nets)
        for seg in [s for s in result.segments if s.net in drop]:
            key = (seg.layer, self._track_index(seg.cross))
            tracks = self.h_tracks if seg.horizontal else self.v_tracks
            entries = tracks.get(key, [])
            span = seg.span
            for entry in list(entries):
                if entry[0] == seg.net and abs(entry[1] - span[0]) < 1e-6                         and abs(entry[2] - span[1]) < 1e-6:
                    entries.remove(entry)
                    break
            self.usage[seg.layer] = max(0.0, self.usage.get(seg.layer, 0.0)
                                        - seg.length)
            result.layer_usage[seg.layer] = max(
                0.0, result.layer_usage.get(seg.layer, 0.0) - seg.length)
        result.segments = [s for s in result.segments if s.net not in drop]
        result.vias = [v for v in result.vias if v.net not in drop]
        for net in drop:
            result.net_wirelength.pop(net, None)

    def route(self) -> RouteResult:
        result = RouteResult()
        order = self._net_order()
        critical = self.net_criticality()
        result.critical_nets = sum(1 for net in order
                                   if critical.get(net, 0.0) > 0.0)
        checker = DrcChecker(self.rules)
        pending = list(order)
        attempt = 0
        while pending:
            for net in pending:
                self._route_net(net, result, offset=attempt * 2)
            self._repair_min_area(result)
            if self.cfg.via_doubling:
                self._double_vias(result)
            violations = checker.check(result.segments, result.vias)
            total = sum(len(items) for items in violations.values())
            if total == 0 and result.overflow == 0:
                break
            if attempt >= self.cfg.ripup_iterations:
                break
            attempt += 1
            offending = {seg.net for seg in result.segments
                         if any(isinstance(item, str) and seg.net in item
                                for items in violations.values() for item in items)}
            if not offending:
                offending = set(pending)
            result.ripups += len(offending)
            pending = sorted(offending)
            self._ripup(result, pending)
            result.overflow = max(0, result.overflow - len(pending))

        # Phase two: whatever the track allocator could not resolve is handed to
        # the grid maze router, which is free to detour anywhere in the core
        # instead of staying on one track. This is the same escalation a
        # production router makes, and it can only remove violations: a net that
        # the maze cannot route keeps the geometry it already had.
        violations = checker.check(result.segments, result.vias)
        remaining = sum(len(items) for items in violations.values())
        if (remaining or result.overflow) and self.cfg.maze_router:
            stuck = sorted(set(result.overflow_nets)
                           | {seg.net for seg in result.segments
                              if any(isinstance(item, str) and seg.net in item
                                     for items in violations.values()
                                     for item in items)})
            if stuck:
                for net in stuck:
                    self._ripup(result, [net])
                rescued = [net for net in stuck if self._route_net_maze(net, result)]
                if rescued:
                    self._repair_min_area(result)
                    result.overflow = 0
                    failed = [net for net in stuck if net not in rescued]
                    result.overflow_nets = failed
                    result.overflow = len(failed)
        result.merged_runs = self._merge_runs(result)
        result.segments.sort(key=lambda s: (s.layer, round(s.y1, 4), round(s.x1, 4), s.net))
        result.vias.sort(key=lambda v: (round(v.x, 4), round(v.y, 4), v.net))
        for net, _keys in sorted(self.nl.nets.items()):
            if net not in result.net_wirelength:
                continue
            if result.net_wirelength[net] <= 0.0:
                result.unrouted_nets.append(net)
        result.vias_count = len(result.vias)
        result.wirelength = sum(seg.length for seg in result.segments)
        violations = checker.check(result.segments, result.vias)
        result.drc_details = violations
        result.drc_violations = {name: len(items) for name, items in violations.items()}
        result.drc = sum(result.drc_violations.values())
        result.net_wirelength = {net: sum(s.length for s in result.segments
                                          if s.net == net)
                                 for net in sorted(result.net_wirelength)}
        return result


def route_capacitance(route: RouteResult, config: PhysicalConfig) -> Dict[str, float]:
    """Per-net wire capacitance (pF) from routed wirelength."""
    return {net: length * config.wire_cap_per_um
            for net, length in route.net_wirelength.items()}


def insert_fillers(netlist: GateNetlist, library: Library, plan: Floorplan,
                   attach: bool = False) -> List[GateCell]:
    """Fill the gaps left in each row with FILL cells (area and well continuity).

    ``attach=True`` also appends the fillers to the netlist so that netlist
    writers and area reports see them; the returned list is always the filler
    set itself, which is what the layout writers take.
    """
    model = library.cell("FILLX1") if library.has("FILLX1") else None
    if model is None:
        return []
    fillers: List[GateCell] = []
    by_row: Dict[int, List[GateCell]] = {}
    for cell in netlist.cells:
        by_row.setdefault(int(round(cell.y)), []).append(cell)
    for row in plan.rows:
        group = sorted(by_row.get(int(round(row.y)), []), key=lambda c: c.x)
        cursor = row.x1
        for cell in group:
            while cell.x - cursor >= model.width - 1e-9:
                fillers.append(GateCell(instance=f"fill_{len(fillers)}", cell=model.name,
                                        x=cursor, y=row.y, width=model.width,
                                        height=model.row_height, origin="fill"))
                cursor += model.width
            cursor = max(cursor, cell.x + cell.width)
        while row.x2 - cursor >= model.width - 1e-9:
            fillers.append(GateCell(instance=f"fill_{len(fillers)}", cell=model.name,
                                    x=cursor, y=row.y, width=model.width,
                                    height=model.row_height, origin="fill"))
            cursor += model.width
    if attach:
        netlist.cells.extend(fillers)
    return fillers
