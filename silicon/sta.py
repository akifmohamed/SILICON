"""Static timing analysis and power estimation.

Block-based single-corner STA over the mapped netlist:

* arrivals propagate through library arcs with NLDM table lookups
  (input transition, output load), slews propagate with the transition tables
* setup checks subtract library setup constraints; hold checks use the hold
  constraint and the minimum arrival
* clock insertion delay is supplied per flop, so the same engine reports
  pre-CTS ideal clocks and post-CTS propagated clocks
* endpoint reports reconstruct the worst path pin by pin

Power is activity-based: ``P = sum(0.5 * C * V^2 * f * activity)`` over nets plus
library leakage. It is an *estimate from switching activity*, not a measurement,
and the report says so.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .library import Library
from .netlist import CONST0, CONST1, GateCell, GateNetlist


@dataclass
class TimingConfig:
    clock_period: float = 10.0
    input_delay: float = 1.0
    output_delay: float = 1.0
    uncertainty: float = 0.15
    default_slew: float = 0.05
    clock_transition: float = 0.08
    wire_cap: float = 0.0008           # pF per fanout, pre-route estimate
    voltage: float = 0.95
    activity: float = 0.10
    clock_activity: float = 2.0
    max_paths: int = 10
    clock_name: str = "clk"
    # On-chip variation: late derates stretch the setup path, early derates
    # shrink the hold path. 1.0 disables OCV.
    derate_cell_late: float = 1.05
    derate_cell_early: float = 0.95
    derate_net_late: float = 1.08
    derate_net_early: float = 0.92
    max_transition: float = 0.0          # 0 => use the library limits only
    max_capacitance: float = 0.0         # 0 => use the library limits only


@dataclass
class Hop:
    point: str
    cell: str
    pin: str
    net: str
    delay: float
    arrival: float
    slew: float
    wire: float = 0.0


@dataclass
class TimingPath:
    startpoint: str
    endpoint: str
    path_type: str                 # max | min
    group: str
    arrival: float
    required: float
    slack: float
    hops: List[Hop] = field(default_factory=list)

    @property
    def violated(self) -> bool:
        return self.slack < 0


@dataclass
class TimingResult:
    wns: float = 0.0
    clock_period: float = 0.0
    tns: float = 0.0
    violating_endpoints: int = 0
    endpoint_count: int = 0
    hold_wns: float = 0.0
    hold_violations: int = 0
    max_slew_violations: int = 0
    max_cap_violations: int = 0
    paths: List[TimingPath] = field(default_factory=list)
    hold_paths: List[TimingPath] = field(default_factory=list)
    unclocked_endpoints: int = 0
    clock_skew: float = 0.0
    wire_delay: float = 0.0             # worst net delay on the critical path
    corner: str = ""
    corners: Dict[str, str] = field(default_factory=dict)
    derated: bool = False

    def summary(self) -> Dict[str, float]:
        return {
            "wns": round(self.wns, 4),
            "tns": round(self.tns, 4),
            "violating_endpoints": self.violating_endpoints,
            "endpoints": self.endpoint_count,
            "hold_wns": round(self.hold_wns, 4),
            "hold_violations": self.hold_violations,
            "clock_skew": round(self.clock_skew, 4),
        }


class TimingEngine:
    def __init__(self, netlist: GateNetlist, library: Library,
                 config: Optional[TimingConfig] = None,
                 parasitics=None) -> None:
        self.nl = netlist
        self.lib = library
        self.cfg = config or TimingConfig()
        self.parasitics = parasitics
        self.driver = netlist.driver_map()
        self.load: Dict[str, float] = {}
        self.slew: Dict[str, float] = {}
        self.arrival: Dict[str, float] = {}
        self.arrival_min: Dict[str, float] = {}
        self.pred: Dict[str, Tuple[str, str, str, str, float]] = {}
        self.clock_arrival: Dict[str, float] = {}
        self._is_clock_net: Dict[str, bool] = {}

    # -- setup -----------------------------------------------------------
    def wire_delay(self, net: str, sink: str) -> float:
        """Delay along the extracted RC tree from the driver to one sink."""
        if self.parasitics is None:
            return 0.0
        return self.parasitics.delay(net, sink)

    def _slew_limit(self, model) -> float:
        limits = [pin.max_transition for pin in model.pins.values()
                  if pin.max_transition > 0]
        if limits:
            return max(limits)
        return self.cfg.max_transition

    def _compute_loads(self, wire_cap: Optional[float] = None,
                       net_caps: Optional[Dict[str, float]] = None) -> None:
        per_sink = self.cfg.wire_cap if wire_cap is None else wire_cap
        self.load = {}
        for net, pins in self.nl.nets.items():
            total = 0.0
            sinks = 0
            for key in pins:
                if key.startswith("port/"):
                    if self.nl.port_dirs.get(key[5:]) in ("output", "inout"):
                        total += 0.005          # pad/pin capacitance placeholder
                        sinks += 1
                    continue
                inst, pin = key.split("/", 1)
                cell = self.nl.by_instance(inst)
                if pin in cell.inputs:
                    total += self.lib.cell(cell.cell).pins[pin].capacitance
                    sinks += 1
            if self.parasitics is not None:
                # extracted wire capacitance replaces the per-fanout estimate
                extra = self.parasitics.cap(net)
                self.load[net] = total + extra
            else:
                extra = (net_caps or {}).get(net, 0.0)
                self.load[net] = total + sinks * per_sink + extra

    def set_clock_arrivals(self, arrivals: Dict[str, float]) -> None:
        """``instance -> insertion delay at its clock pin`` (post-CTS propagation)."""
        self.clock_arrival = dict(arrivals)

    def _clock_net(self, cell: GateCell) -> str:
        ck = cell.inputs.get("CK")
        return ck or ""

    def _ck_arrival(self, cell: GateCell) -> float:
        if cell.instance in self.clock_arrival:
            return self.clock_arrival[cell.instance]
        net = self._clock_net(cell)
        return self.clock_arrival.get(f"net:{net}", 0.0)

    # -- propagation -----------------------------------------------------
    def _combinational_order(self) -> List[GateCell]:
        """Topological order over combinational cells (Kahn)."""
        cells = [c for c in self.nl.cells if not c.is_sequential and c.cell not in
                 ("TIELO", "TIEHI", "FILLX1")]
        drivers = {net: c for c in cells for net in c.outputs.values()}
        indeg: Dict[str, int] = {c.instance: 0 for c in cells}
        succ: Dict[str, List[str]] = {c.instance: [] for c in cells}
        for cell in cells:
            seen = set()
            for net in cell.inputs.values():
                src = drivers.get(net)
                if src is not None and src.instance != cell.instance and src.instance not in seen:
                    seen.add(src.instance)
                    succ[src.instance].append(cell.instance)
                    indeg[cell.instance] += 1
        queue = [inst for inst, deg in indeg.items() if deg == 0]
        order: List[GateCell] = []
        by_inst = {c.instance: c for c in cells}
        while queue:
            inst = queue.pop(0)
            order.append(by_inst[inst])
            for nxt in succ[inst]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        if len(order) != len(cells):
            # combinational feedback: keep the acyclic prefix, report the rest
            missing = len(cells) - len(order)
            order.extend(c for c in cells if c.instance not in {o.instance for o in order})
            self.combinational_loop = missing
        return order

    def _arc_delay(self, cell: GateCell, in_pin: str, out_pin: str,
                   in_slew: float, out_load: float) -> Tuple[float, float]:
        model = self.lib.cell(cell.cell)
        arc = model.arc(in_pin, out_pin)
        if arc is None:
            return 0.0, in_slew
        # worst of the rise and fall arcs: a single-corner analysis that does not
        # split polarity has to take the slower of the two transitions
        rise_delay, _ = arc.rise.lookup(in_slew, out_load)
        fall_delay, _ = arc.fall.lookup(in_slew, out_load)
        rise_slew, _ = arc.rise.lookup(in_slew, out_load)
        fall_slew, _ = arc.fall.lookup(in_slew, out_load)
        return max(rise_delay, fall_delay), max(rise_slew, fall_slew)

    def run(self, net_caps: Optional[Dict[str, float]] = None,
            parasitics=None) -> TimingResult:
        if parasitics is not None:
            self.parasitics = parasitics
        self._compute_loads(net_caps=net_caps)
        result = TimingResult()
        order = self._combinational_order()
        cfg = self.cfg

        # --- sources -----------------------------------------------------
        for name, net in self.nl.ports.items():
            if self.nl.port_dirs.get(name) == "input":
                is_clock = name == cfg.clock_name or name.startswith(cfg.clock_name)
                self.slew[net] = cfg.clock_transition if is_clock else cfg.default_slew
                if not is_clock:
                    self.arrival[net] = cfg.input_delay
                    self.arrival_min[net] = cfg.input_delay

        for cell in self.nl.sequential_cells():
            q = cell.outputs.get("Q")
            if not q:
                continue
            clk_arrival = self._ck_arrival(cell)
            model = self.lib.cell(cell.cell)
            arc = model.arc("CK", "Q")
            delay = 0.0
            slew = cfg.clock_transition
            if arc is not None:
                delay, _ = arc.rise.lookup(cfg.clock_transition, self.load.get(q, 0.0))
                slew, _ = arc.rise.lookup(cfg.clock_transition, self.load.get(q, 0.0))
            self.arrival[q] = clk_arrival + delay * cfg.derate_cell_late
            self.arrival_min[q] = clk_arrival + delay * cfg.derate_cell_early
            self.slew[q] = slew
            self.pred[q] = (cell.instance, "Q", "CK", self._clock_net(cell), delay, 0.0)

        # --- forward propagation ----------------------------------------
        for cell in order:
            out_arcs = [(p, n) for p, n in cell.outputs.items()]
            for out_pin, out_net in out_arcs:
                best = None
                best_min = None
                best_slew = cfg.default_slew
                for in_pin, in_net in cell.inputs.items():
                    if in_net not in self.arrival:
                        continue
                    if in_net in (CONST0, CONST1):
                        continue
                    delay, out_slew = self._arc_delay(cell, in_pin, out_pin,
                                                      self.slew.get(in_net, cfg.default_slew),
                                                      self.load.get(out_net, 0.0))
                    wire = self.wire_delay(in_net, f"{cell.instance}/{in_pin}")
                    cand = self.arrival[in_net] + (delay * cfg.derate_cell_late
                                                   + wire * cfg.derate_net_late)
                    cand_min = (self.arrival_min.get(in_net, 0.0)
                                + delay * cfg.derate_cell_early
                                + wire * cfg.derate_net_early)
                    if best is None or cand > best:
                        best = cand
                        best_slew = out_slew
                        self.pred[out_net] = (cell.instance, out_pin, in_pin,
                                              in_net, delay, wire)
                    best_min = cand_min if best_min is None else min(best_min, cand_min)
                if best is not None:
                    self.arrival[out_net] = best
                    self.arrival_min[out_net] = best_min if best_min is not None else best
                    self.slew[out_net] = best_slew

        # --- endpoints and checks ---------------------------------------
        period = cfg.clock_period
        result.clock_period = period
        endpoints: List[TimingPath] = []
        hold_paths: List[TimingPath] = []

        for cell in self.nl.sequential_cells():
            d_net = cell.inputs.get("D")
            if d_net is None or d_net not in self.arrival:
                result.unclocked_endpoints += 1
                continue
            clk_arrival = self._ck_arrival(cell)
            model = self.lib.cell(cell.cell)
            setup_tbl, _ = model.setup_tables()
            hold_tbl, _ = model.hold_tables()
            d_slew = self.slew.get(d_net, cfg.default_slew)
            d_load = self.load.get(d_net, 0.0)
            setup = setup_tbl.lookup(d_slew, d_load)[0] if setup_tbl else 0.05
            hold = hold_tbl.lookup(d_slew, d_load)[0] if hold_tbl else 0.01
            required = clk_arrival + period - setup - cfg.uncertainty
            slack = required - self.arrival[d_net]
            endpoints.append(TimingPath(
                startpoint=self._start_of(d_net),
                endpoint=f"{cell.instance}/D",
                path_type="max", group=cfg.clock_name,
                arrival=self.arrival[d_net], required=required, slack=slack,
                hops=self._trace(d_net)))
            hold_slack = self.arrival_min.get(d_net, 0.0) - (clk_arrival + hold)
            hold_paths.append(TimingPath(
                startpoint=self._start_of(d_net),
                endpoint=f"{cell.instance}/D",
                path_type="min", group=cfg.clock_name,
                arrival=self.arrival_min.get(d_net, 0.0),
                required=clk_arrival + hold, slack=hold_slack,
                hops=self._trace(d_net, minimum=True)))

        for name, net in self.nl.ports.items():
            if self.nl.port_dirs.get(name) != "output":
                continue
            if net not in self.arrival:
                continue
            required = period - cfg.output_delay - cfg.uncertainty
            slack = required - self.arrival[net]
            endpoints.append(TimingPath(
                startpoint=self._start_of(net), endpoint=name, path_type="max",
                group=cfg.clock_name, arrival=self.arrival[net], required=required,
                slack=slack, hops=self._trace(net)))

        # --- skew ---------------------------------------------------------
        arrivals = [self.clock_arrival.get(c.instance, 0.0)
                    for c in self.nl.sequential_cells()]
        if arrivals:
            result.clock_skew = max(arrivals) - min(arrivals)

        # --- results ------------------------------------------------------
        result.endpoint_count = len(endpoints)
        if endpoints:
            result.wns = min(p.slack for p in endpoints)
            result.tns = sum(p.slack for p in endpoints if p.slack < 0)
            result.violating_endpoints = sum(1 for p in endpoints if p.slack < 0)
            result.paths = sorted(endpoints, key=lambda p: p.slack)[: cfg.max_paths]
        if hold_paths:
            result.hold_wns = min(p.slack for p in hold_paths)
            result.hold_violations = sum(1 for p in hold_paths if p.slack < 0)
            result.hold_paths = sorted(hold_paths, key=lambda p: p.slack)[: cfg.max_paths]

        # --- design rule checks ------------------------------------------
        for net, slew in self.slew.items():
            driver = self.driver.get(net)
            if driver is None:
                continue
            limit = self._slew_limit(self.lib.cell(driver.cell)) or cfg.max_transition
            if limit and slew > limit:
                result.max_slew_violations += 1
        for net, load in self.load.items():
            driver = self.driver.get(net)
            if driver is None:
                continue
            model = self.lib.cell(driver.cell)
            limits = [pin.max_capacitance for pin in model.pins.values()
                      if pin.max_capacitance > 0]
            cap = max(limits, default=0.0) or cfg.max_capacitance
            if cap and load > cap:
                result.max_cap_violations += 1
        result.corner = self.lib.corner
        result.derated = any(value != 1.0 for value in
                             (cfg.derate_cell_late, cfg.derate_cell_early,
                              cfg.derate_net_late, cfg.derate_net_early))
        if result.paths:
            result.wire_delay = max((sum(hop.wire for hop in path.hops)
                                     for path in result.paths), default=0.0)
        return result

    # -- path reconstruction ---------------------------------------------
    def _start_of(self, net: str) -> str:
        seen = set()
        cur = net
        while cur in self.pred and cur not in seen:
            seen.add(cur)
            inst, out_pin, in_pin, in_net, _delay = self.pred[cur][:5]
            cell = self.nl.by_instance(inst)
            if cell.is_sequential:
                return f"{cell.instance}/Q"
            if in_net not in self.pred and in_net not in self.arrival and \
                    not any(n == in_net for n in self.nl.nets):
                return in_pin
            cur = in_net
            if cur not in self.pred:
                for name, port_net in self.nl.ports.items():
                    if port_net == cur and self.nl.port_dirs.get(name) == "input":
                        return name
                return cur
        return net

    def _trace(self, net: str, minimum: bool = False) -> List[Hop]:
        hops: List[Hop] = []
        seen = set()
        cur = net
        while cur in self.pred and cur not in seen:
            seen.add(cur)
            entry = self.pred[cur]
            inst, out_pin, in_pin, in_net, delay = entry[:5]
            wire = entry[5] if len(entry) > 5 else 0.0
            cell = self.nl.by_instance(inst)
            hops.append(Hop(point=f"{inst}/{out_pin}", cell=cell.cell, pin=out_pin,
                            net=cur, delay=delay, wire=wire,
                            arrival=self.arrival.get(cur, 0.0),
                            slew=self.slew.get(cur, 0.0)))
            cur = in_net
        for name, port_net in self.nl.ports.items():
            if port_net == cur and self.nl.port_dirs.get(name) == "input":
                hops.append(Hop(point=name, cell="port", pin="", net=cur, delay=0.0,
                                arrival=self.arrival.get(cur, 0.0), slew=0.0))
                break
        hops.reverse()
        return hops


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------


@dataclass
class PowerResult:
    dynamic_mw: float = 0.0
    leakage_mw: float = 0.0
    clock_mw: float = 0.0
    cell_area: float = 0.0

    @property
    def total_mw(self) -> float:
        return self.dynamic_mw + self.leakage_mw

    def summary(self) -> Dict[str, float]:
        return {
            "dynamic_mw": round(self.dynamic_mw, 4),
            "leakage_mw": round(self.leakage_mw, 4),
            "clock_mw": round(self.clock_mw, 4),
            "total_mw": round(self.total_mw, 4),
        }


def estimate_power(netlist: GateNetlist, library: Library,
                   config: Optional[TimingConfig] = None,
                   loads: Optional[Dict[str, float]] = None,
                   net_caps: Optional[Dict[str, float]] = None) -> PowerResult:
    """Switching-activity power: 0.5*C*V^2*f*activity per net plus leakage."""
    cfg = config or TimingConfig()
    if loads is None:
        engine = TimingEngine(netlist, library, cfg)
        engine._compute_loads(net_caps=net_caps)
        loads = engine.load
    clock_nets = set()
    for cell in netlist.sequential_cells():
        if cell.inputs.get("CK"):
            clock_nets.add(cell.inputs["CK"])
    freq = 1.0 / max(cfg.clock_period, 1e-9)      # GHz when period is in ns
    dynamic = 0.0
    clock_power = 0.0
    for net, cap in loads.items():
        activity = cfg.clock_activity if net in clock_nets else cfg.activity
        power = 0.5 * cap * cfg.voltage ** 2 * freq * activity
        dynamic += power
        if net in clock_nets:
            clock_power += power
    leakage = sum(library.cell(c.cell).leakage for c in netlist.cells) * 1e-3   # nW -> mW
    return PowerResult(dynamic_mw=dynamic, leakage_mw=leakage,
                       clock_mw=clock_power,
                       cell_area=sum(library.cell(c.cell).area for c in netlist.cells))


def analyse_corners(netlist: GateNetlist,
                    libraries: Dict[str, Library],
                    config: Optional[TimingConfig] = None,
                    parasitics=None) -> Tuple[TimingResult, PowerResult, Dict[str, TimingResult]]:
    """Min/max corner analysis: setup at the slow corner, hold at the fast one.

    ``libraries`` maps a corner role (``"max"``/``"min"``) to a library. Each
    corner is timed with the same netlist and the same extracted parasitics, and
    the reported result takes setup data from the max corner and hold data from
    the min corner -- which is the point of a min/max analysis: setup is worst
    when cells are slow, hold is worst when cells are fast.

    Returns ``(merged, power, per_corner)``.
    """
    cfg = config or TimingConfig()
    per_corner: Dict[str, TimingResult] = {}
    powers: Dict[str, PowerResult] = {}
    for role in ("max", "min"):
        lib = libraries.get(role)
        if lib is None:
            continue
        engine = TimingEngine(netlist, lib, cfg, parasitics=parasitics)
        per_corner[role] = engine.run(parasitics=parasitics)
        powers[role] = estimate_power(netlist, lib, cfg, engine.load)
    if not per_corner:
        raise ValueError("no libraries given for corner analysis")

    setup_role = "max" if "max" in per_corner else next(iter(per_corner))
    hold_role = "min" if "min" in per_corner else setup_role
    merged = copy.copy(per_corner[setup_role])
    merged.corners = {role: result.corner for role, result in per_corner.items()}
    if hold_role != setup_role:
        fast = per_corner[hold_role]
        merged.hold_wns = fast.hold_wns
        merged.hold_violations = fast.hold_violations
        merged.hold_paths = fast.hold_paths
    power = powers[setup_role]
    return merged, power, per_corner


def analyse(netlist: GateNetlist, library: Library,
            config: Optional[TimingConfig] = None,
            net_caps: Optional[Dict[str, float]] = None
            ) -> Tuple[TimingResult, PowerResult]:
    """Timing + power in one pass. ``net_caps`` adds post-route wire capacitance."""
    engine = TimingEngine(netlist, library, config)
    timing = engine.run(net_caps=net_caps)
    power = estimate_power(netlist, library, config, engine.load)
    return timing, power
