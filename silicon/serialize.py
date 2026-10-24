"""Netlist serialisation used to hand a design from ``dc_shell`` to ``icc2_shell``.

JSON is used rather than an opaque binary so the handoff can be inspected,
diffed and checked into a repository. It is the equivalent of Design Compiler's
``.ddc`` for this toolchain: it carries the gate netlist, port directions,
library identity and the constraint set.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .library import Library
from .netlist import GateCell, GateNetlist
from .sta import TimingConfig

FORMAT = "silicon-netlist"
VERSION = 1


def to_dict(netlist: GateNetlist, library: Library, config: TimingConfig,
            tool: str = "silicon-dc") -> Dict[str, Any]:
    return {
        "format": FORMAT,
        "version": VERSION,
        "tool": tool,
        "design": netlist.top,
        "library": {"name": library.name, "corner": library.corner},
        "constraints": {
            "clock_name": config.clock_name,
            "clock_period": config.clock_period,
            "input_delay": config.input_delay,
            "output_delay": config.output_delay,
            "uncertainty": config.uncertainty,
            "clock_transition": config.clock_transition,
            "voltage": config.voltage,
        },
        "ports": [{"name": name, "direction": netlist.port_dirs.get(name, "input"),
                   "net": net} for name, net in sorted(netlist.ports.items())],
        "cells": [
            {
                "instance": c.instance,
                "cell": c.cell,
                "inputs": c.inputs,
                "outputs": c.outputs,
                "origin": c.origin,
            }
            for c in netlist.cells
        ],
    }


def dump(netlist: GateNetlist, library: Library, config: TimingConfig, path: str,
         tool: str = "silicon-dc") -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(to_dict(netlist, library, config, tool), handle, indent=1,
                  sort_keys=False)
        handle.write("\n")


def load(path: str) -> tuple:
    """Return ``(netlist, library_name, constraints, tool)``."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("format") != FORMAT:
        raise ValueError(f"{path} is not a {FORMAT} file")
    netlist = GateNetlist(top=data["design"])
    for port in data["ports"]:
        netlist.ports[port["name"]] = port["net"]
        netlist.port_dirs[port["name"]] = port["direction"]
        netlist.nets.setdefault(port["net"], set()).add(f"port/{port['name']}")
    for entry in data["cells"]:
        cell = GateCell(instance=entry["instance"], cell=entry["cell"],
                        inputs=dict(entry["inputs"]), outputs=dict(entry["outputs"]),
                        origin=entry.get("origin", ""))
        netlist.add_cell(cell)
    constraints = data.get("constraints", {})
    return netlist, data["library"]["name"], constraints, data.get("tool", "")
