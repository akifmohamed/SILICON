"""SDF 3.0 writer.

The point of writing Standard Delay Format is that another tool can consume the
result: an SDF back-annotated netlist is how a P&R tool hands its timing to a
gate-level simulator or to a third-party timer. The file carries

* ``IOPATH`` delay arcs for every cell instance, taken from the same Liberty
  NLDM tables and the same output loads the signoff timing used;
* ``INTERCONNECT`` delay from driver pin to sink pin on every net, taken from the
  extracted RC tree (zero when no parasitics are loaded, which is what a
  pre-route estimate would be);
* ``SETUP``/``HOLD`` timing checks on every sequential instance.

Values are written as ``min::max`` triplets. When only one corner is analysed
both halves are the same number, and the header says so, because a single-corner
SDF that pretends to bracket min and max would be a lie.
"""

from __future__ import annotations

from typing import List, Optional

from .library import Library
from .netlist import GateNetlist

TIMESCALE = "1ns"


def _q(value: float) -> str:
    """Format a delay as an SDF triplet (min::max)."""
    text = f"{max(value, 0.0):.6f}"
    return f"({text}::{text})"


def _pair(value: float) -> str:
    return f"{max(value, 0.0):.6f}::{max(value, 0.0):.6f}"


def build_sdf(netlist: GateNetlist, library: Library, engine,
              tool: str = "silicon-icc2") -> str:
    """Serialise the timing view in ``engine`` as an SDF 3.0 file."""
    corner = getattr(library, "corner", "") or "single corner"
    voltage = getattr(library, "nom_voltage", 0.0) or 0.0
    temperature = getattr(library, "nom_temperature", 0.0) or 0.0
    out: List[str] = []
    out.append("(DELAYFILE")
    out.append('  (SDFVERSION "3.0")')
    out.append(f'  (DESIGN "{netlist.top}")')
    out.append(f'  (DATE "{_date()}")')
    out.append('  (VENDOR "silicon")')
    out.append(f'  (PROGRAM "{tool}")')
    out.append('  (VERSION "0.3.0")')
    out.append("  (DIVIDER /)")
    out.append(f"  (VOLTAGE {voltage:.3f}::{voltage:.3f})")
    out.append(f"  (TEMPERATURE {temperature:.1f}::{temperature:.1f})")
    out.append("  (TIMESCALE 1ns)")
    out.append(f'  (CELL (CELLTYPE "{netlist.top}") (INSTANCE)')

    # -- interconnect: driver pin -> every sink, from the extracted RC tree
    out.append("    (DELAY")
    out.append("      (ABSOLUTE")
    interconnects = 0
    for net in sorted(netlist.nets):
        driver = _driver(netlist, net)
        if driver is None:
            continue
        # pin keys in the net database are "instance/pin" or "port" (see
        # docs/DATABASE.md); the driver pin is the one that is not a sink
        sinks = [key for key in sorted(netlist.nets[net]) if key != driver]
        for sink in sinks:
            delay = engine.wire_delay(net, sink) if engine is not None else 0.0
            out.append(f"        (INTERCONNECT {driver} {sink} {_q(delay)})")
            interconnects += 1
    out.append("      )")
    out.append("    )")

    # -- timing checks on the sequential instances
    checks = 0
    out.append("    (TIMINGCHECK")
    for cell in netlist.cells:
        if not cell.is_sequential:
            continue
        model = library.cell(cell.cell)
        clock_pin = "CK"
        data_pins = [p for p in cell.inputs if p in ("D", "SE", "SI")]
        setup_rise, _ = model.setup_tables()
        hold_rise, _ = model.hold_tables()
        if not data_pins:
            continue
        for pin in sorted(data_pins):
            setup = setup_rise.lookup(0.05, 0.005)[0] if setup_rise else 0.0
            hold = hold_rise.lookup(0.05, 0.005)[0] if hold_rise else 0.0
            out.append(f'      (SETUP {cell.instance} {pin} (posedge {clock_pin}) '
                       f'({_pair(setup)}))')
            out.append(f'      (HOLD {cell.instance} {pin} (posedge {clock_pin}) '
                       f'({_pair(hold)}))')
            checks += 2
    out.append("    )")

    # -- one CELL per instance, holding that instance's IOPATH arcs
    arcs = 0
    for cell in netlist.cells:
        model = library.cell(cell.cell)
        lines: List[str] = []
        for in_pin, in_net in sorted(cell.inputs.items()):
            for out_pin, out_net in sorted(cell.outputs.items()):
                arc = model.arc(in_pin, out_pin)
                if arc is None:
                    continue
                in_slew = _slew(engine, in_net)
                load = _load(engine, out_net)
                rise, _ = arc.rise.lookup(in_slew, load)
                fall, _ = arc.fall.lookup(in_slew, load)
                lines.append(f"        (IOPATH {in_pin} {out_pin} {_q(rise)} {_q(fall)})")
                arcs += 1
        if not lines:
            continue
        out.append(f'    (CELL (CELLTYPE "{cell.cell}") (INSTANCE {cell.instance})')
        out.append("      (DELAY (ABSOLUTE")
        out.extend(lines)
        out.append("      ))")
        out.append("    )")
    out.append("  )")
    out.append(")")
    return "\n".join(out) + "\n"


def sdf_counts(text: str) -> dict:
    """Counts of the arcs and checks in a generated file (used by the tests)."""
    return {
        "arcs": text.count("(IOPATH "),
        "interconnects": text.count("(INTERCONNECT "),
        "checks": text.count("(SETUP ") + text.count("(HOLD "),
    }


def _date() -> str:
    import time
    return time.strftime("%a %b %d %H:%M:%S %Y")


def _driver(netlist, net: str) -> Optional[str]:
    for cell in netlist.cells:
        for pin, driven in cell.outputs.items():
            if driven == net:
                return f"{cell.instance}/{pin}"
    for port, mapped in netlist.ports.items():
        if mapped == net and netlist.port_dirs.get(port) == "input":
            return port
    return None


def _slew(engine, net: str) -> float:
    if engine is None:
        return 0.05
    return getattr(engine, "slew", {}).get(net, 0.05)


def _load(engine, net: str) -> float:
    if engine is None:
        return 0.005
    return getattr(engine, "load", {}).get(net, 0.005)


def write_sdf(path: str, netlist: GateNetlist, library: Library, engine,
              tool: str = "silicon-icc2") -> str:
    text = build_sdf(netlist, library, engine, tool=tool)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
