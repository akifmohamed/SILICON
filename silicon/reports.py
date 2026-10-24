"""Report formatting.

The layout deliberately resembles the reports a hardware engineer reads every
day (``Report : area``, ``Report : timing``, ``report_qor``) so the output is
familiar, while every number is computed from the netlist and library.
"""

from __future__ import annotations

import datetime
import os
from typing import Dict, List, Optional, Sequence

from .library import Library
from .netlist import GateNetlist
from .sta import PowerResult, TimingResult

TOOL = "silicon-dc 0.1"   # replaced by the calling shell
BANNER = "*" * 62


def timestamp() -> str:
    """Report timestamp, honouring ``SOURCE_DATE_EPOCH``.

    Reproducible-build convention: when ``SOURCE_DATE_EPOCH`` is set the stamp is
    derived from it instead of the wall clock, so two runs of the same inputs
    produce byte-identical reports. Without it the reports carry the real time,
    which is what a person reading them wants.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
    if epoch.strip():
        try:
            moment = datetime.datetime.fromtimestamp(int(epoch.strip()),
                                                     datetime.timezone.utc)
        except (ValueError, OverflowError, OSError):
            moment = datetime.datetime.now(datetime.timezone.utc)
        return moment.strftime("%a %b %d %H:%M:%S %Y")
    return datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")


def _header(title: str, design: str, extra: Optional[str] = None) -> str:
    stamp = timestamp()
    lines = [BANNER, f"Report : {title}",
             f"Design : {design}",
             f"Version: {TOOL}",
             f"Date   : {stamp}"]
    if extra:
        lines.append(f"        : {extra}")
    lines += [BANNER, ""]
    return "\n".join(lines)


def report_area(netlist: GateNetlist, library: Library, design: str) -> str:
    cells = netlist.cells
    comb = [c for c in cells if not c.is_sequential]
    seq = [c for c in cells if c.is_sequential]
    buf_inv = [c for c in cells if c.cell.startswith(("BUF", "INV"))]
    refs = sorted({c.cell for c in cells})
    area = lambda group: sum(library.cell(c.cell).area for c in group)
    out = [_header("area", design)]
    out.append("Library(s) Used:")
    out.append(f"    {library.name} (File: {library.name}.lib, corner {library.corner})")
    out.append("")
    out.append(f"{'Number of ports:':<40}{len(netlist.ports):>10}")
    out.append(f"{'Number of nets:':<40}{len(netlist.nets):>10}")
    out.append(f"{'Number of cells:':<40}{len(cells):>10}")
    out.append(f"{'Number of combinational cells:':<40}{len(comb):>10}")
    out.append(f"{'Number of sequential cells:':<40}{len(seq):>10}")
    out.append(f"{'Number of macros/black boxes:':<40}{0:>10}")
    out.append(f"{'Number of buf/inv:':<40}{len(buf_inv):>10}")
    out.append(f"{'Number of references:':<40}{len(refs):>10}")
    out.append("")
    out.append(f"{'Combinational area:':<40}{area(comb):>10.2f}")
    out.append(f"{'Buf/Inv area:':<40}{area(buf_inv):>10.2f}")
    out.append(f"{'Noncombinational area:':<40}{area(seq):>10.2f}")
    out.append(f"{'Net area:':<40}{0.0:>10.2f}")
    out.append("-" * 52)
    out.append(f"{'Total cell area:':<40}{area(cells):>10.2f}")
    out.append(f"{'Total area:':<40}{area(cells):>10.2f}")
    out.append("")
    out.append("Cell type breakdown:")
    counts: Dict[str, int] = {}
    for c in cells:
        counts[c.cell] = counts.get(c.cell, 0) + 1
    for name in sorted(counts):
        out.append(f"    {name:<16}{counts[name]:>6}   {library.cell(name).area * counts[name]:>10.2f}")
    return "\n".join(out)


def report_timing(timing: TimingResult, design: str, max_paths: int = 3,
                  clock_name: str = "clk") -> str:
    out = [_header("timing", design, f"worst {max_paths} path(s)")]
    if not timing.paths:
        out.append("No timing paths found (design has no constrained endpoints).")
        return "\n".join(out)
    for index, path in enumerate(timing.paths[:max_paths], start=1):
        status = "VIOLATED" if path.slack < 0 else "MET"
        out.append(BANNER)
        out.append(f"Startpoint: {path.startpoint}")
        out.append(f"Endpoint  : {path.endpoint}")
        out.append(f"Path Group: {path.group}")
        out.append(f"Path Type : {path.path_type}")
        out.append("-" * 62)
        out.append(f"{'Point':<30}{'Incr':>8}{'Path':>9}{'Wire':>8}")
        out.append("-" * 62)
        prev = 0.0
        for hop in path.hops:
            incr = hop.arrival - prev
            prev = hop.arrival
            label = hop.point if len(hop.point) <= 29 else hop.point[:26] + "..."
            out.append(f"{label:<30}{incr:>8.4f}{hop.arrival:>9.4f}"
                       f"{hop.wire:>8.4f}   ({hop.cell})")
        if path.hops:
            out.append(f"{'data arrival time':<34}{'':>8}{path.arrival:>10.4f}")
        out.append("")
        out.append(f"{'clock ' + clock_name + ' (rise edge)':<34}{'':>8}{0.0:>10.4f}")
        out.append(f"{'clock uncertainty':<34}{'':>8}{'':>10}")
        out.append(f"{'data required time':<34}{'':>8}{path.required:>10.4f}")
        out.append("-" * 62)
        out.append(f"{'data required time':<34}{'':>8}{path.required:>10.4f}")
        out.append(f"{'data arrival time':<34}{'':>8}{-path.arrival:>10.4f}")
        out.append("-" * 62)
        out.append(f"{'slack (' + status + ')':<34}{'':>8}{path.slack:>10.4f}")
        out.append("")
    return "\n".join(out)


def report_qor(timing: TimingResult, power: PowerResult, design: str,
               clock_period: float) -> str:
    out = [_header("qor", design)]
    out.append(f"{'Timing Path Group':<24}{'WNS':>10}{'TNS':>10}{'Violating':>12}{'Endpoints':>12}")
    out.append("-" * 68)
    out.append(f"{'clk (setup)':<24}{timing.wns:>10.3f}{timing.tns:>10.3f}"
               f"{timing.violating_endpoints:>12}{timing.endpoint_count:>12}")
    out.append("")
    out.append(f"{'Clock period:':<32}{clock_period:>10.3f} ns")
    out.append(f"{'Clock skew:':<32}{timing.clock_skew:>10.3f} ns")
    out.append(f"{'Hold WNS:':<32}{timing.hold_wns:>10.3f} ns")
    out.append(f"{'Hold violations:':<32}{timing.hold_violations:>10}")
    out.append(f"{'Max transition violations:':<32}{timing.max_slew_violations:>10}")
    out.append(f"{'Max capacitance violations:':<32}{timing.max_cap_violations:>10}")
    out.append(f"{'Worst net delay (RC):':<32}{timing.wire_delay:>10.4f} ns")
    out.append(f"{'OCV derates:':<32}"
               f"{timing.ocv_mode.upper() if timing.derated else 'off':>10}")
    if timing.derated:
        out.append(f"{'Logic depth (max):':<32}{timing.max_depth:>10}")
    if timing.corner:
        out.append(f"{'Corner:':<32}{timing.corner:>10}")
    for name, description in timing.corners.items():
        out.append(f"{'  ' + name + ':':<32}{description:>10}")
    out.append(f"{'Unclocked endpoints:':<32}{timing.unclocked_endpoints:>10}")
    out.append("")
    out.append(f"{'Cell area:':<32}{power.cell_area:>10.2f} um^2")
    out.append(f"{'Total power:':<32}{power.total_mw:>10.4f} mW")
    return "\n".join(out)


def report_constraints(timing: TimingResult, design: str,
                       max_items: int = 10) -> str:
    """Limit violations: max transition and max capacitance, worst first."""
    out = [_header("constraints", design)]
    out.append(f"{'Transition violations:':<32}{timing.max_slew_violations:>8}")
    out.append(f"{'Capacitance violations:':<32}{timing.max_cap_violations:>8}")
    out.append("")
    if timing.slew_violations:
        out.append(f"{'Net':<24}{'Slew (ns)':>12}{'Limit':>10}{'Over':>10}")
        out.append("-" * 56)
        for net, value, limit in timing.slew_violations[:max_items]:
            out.append(f"{net:<24}{value:>12.4f}{limit:>10.4f}"
                       f"{value - limit:>10.4f}")
        out.append("")
    if timing.cap_violations:
        out.append(f"{'Net':<24}{'Load (pF)':>12}{'Limit':>10}{'Over':>10}")
        out.append("-" * 56)
        for net, value, limit in timing.cap_violations[:max_items]:
            out.append(f"{net:<24}{value:>12.4f}{limit:>10.4f}"
                       f"{value - limit:>10.4f}")
        out.append("")
    if not timing.slew_violations and not timing.cap_violations:
        out.append("No design-rule violations: every net is inside the library")
        out.append("max_transition and max_capacitance limits.")
    return "\n".join(out)


def report_power(power: PowerResult, design: str, clock_period: float) -> str:
    freq = 1000.0 / max(clock_period, 1e-9)
    out = [_header("power", design)]
    out.append("Operating Conditions: ss_0p95V_125C   Library: stdcells_32nm")
    out.append(f"Clock frequency:     {freq:8.2f} MHz")
    out.append("")
    out.append(f"{'Dynamic (switching):':<34}{power.dynamic_mw:>10.4f} mW")
    out.append(f"{'  of which clock nets:':<34}{power.clock_mw:>10.4f} mW")
    out.append(f"{'Leakage:':<34}{power.leakage_mw:>10.4f} mW")
    out.append("-" * 46)
    out.append(f"{'Total:':<34}{power.total_mw:>10.4f} mW")
    out.append("")
    out.append("Note: activity-based estimate (activity 0.10, clock 2.0); not a measurement.")
    return "\n".join(out)


def report_check_design(netlist: GateNetlist) -> str:
    problems: List[str] = []
    drivers = netlist.driver_map()
    for net, pins in netlist.nets.items():
        sinks = [p for p in pins if not p.startswith("port/")]
        for key in sinks:
            inst, pin = key.split("/", 1)
            cell = netlist.by_instance(inst)
            if pin in cell.inputs and net not in drivers and net not in netlist.constants:
                problems.append(f"undriven input: {inst}/{pin} (net {net})")
        if len([p for p in pins if not p.startswith("port/")]) > 40:
            problems.append(f"high fanout: {net} drives "
                            f"{len(pins)} pins")
    for name, net in netlist.ports.items():
        if netlist.port_dirs.get(name) == "output" and net not in drivers:
            problems.append(f"output port {name} is not driven")
    header = _header("check_design", netlist.top)
    if not problems:
        return header + f"Design {netlist.top} is structurally consistent: "
        return header + "No problems found.\n"
    unique = sorted(set(problems))
    return header + f"{len(unique)} problem(s) found:\n" + "\n".join(f"    {p}" for p in unique)


def report_netlist_verilog(netlist: GateNetlist, library: Library,
                           module_name: Optional[str] = None) -> str:
    """Write a gate-level Verilog netlist (structural, library cells)."""
    name = module_name or netlist.top
    inputs = sorted(p for p, d in netlist.port_dirs.items() if d == "input")
    outputs = sorted(p for p, d in netlist.port_dirs.items() if d == "output")
    inout = sorted(p for p, d in netlist.port_dirs.items() if d == "inout")
    lines = [f"// Gate-level netlist for {name}",
             f"// Written by {TOOL} from {library.name} ({library.corner})",
             f"// Cells: {len(netlist.cells)}  Nets: {len(netlist.nets)}",
             "", f"module {name} ("]
    ports = []
    for p in inputs:
        ports.append(f"  input  {_sanitize(p)}")
    for p in outputs:
        ports.append(f"  output {_sanitize(p)}")
    for p in inout:
        ports.append(f"  inout  {_sanitize(p)}")
    lines.append(",\n".join(ports))
    lines.append(");")
    lines.append("")
    for net in sorted(netlist.nets):
        if net in netlist.constants:
            continue
        safe = _sanitize(net)
        lines.append(f"  wire {safe};")
    lines.append("")
    for cell in netlist.cells:
        conns = []
        for pin in sorted(cell.inputs):
            conns.append(f"    .{pin}({_sanitize(cell.inputs[pin])})")
        for pin in sorted(cell.outputs):
            conns.append(f"    .{pin}({_sanitize(cell.outputs[pin])})")
        lines.append(f"  {cell.cell} {cell.instance} (")
        lines.append(",\n".join(conns))
        lines.append("  );")
    lines.append("")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _sanitize(net: str) -> str:
    if net in ("1'b0", "1'b1"):
        return net
    out = net.replace("[", "_").replace("]", "").replace("'", "").replace(".", "_")
    out = out.replace("/", "_").replace("\\", "_").replace("~", "n").replace("$", "d")
    if out and out[0].isdigit():
        out = "n" + out
    return out
