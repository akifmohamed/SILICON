# The `dc` -> `icc2` database

`dc_shell write -format json` writes this file and `icc2_shell read_netlist` reads
it. It is the handoff between the two tools: plain JSON so it can be diffed,
inspected and regenerated, and it carries everything `icc2` needs to run without
`synthesis` ever being installed.

A real netlist has no geometry in it -- placement is `icc2`'s job -- so `cells`
carries connectivity only.

```json
{
  "format": "silicon-netlist",
  "version": 1,
  "tool": "silicon-dc",
  "design": "alu",
  "library": {
    "name": "stdcells_32nm",
    "corner": "ss_0p95V_125C"
  },
  "constraints": {
    "clock_name": "clk",
    "clock_period": 10.0,
    "input_delay": 1.0,
    "output_delay": 1.0,
    "uncertainty": 0.15,
    "clock_transition": 0.08,
    "voltage": 0.95
  },
  "ports": [
    {
      "name": "a[0]",
      "direction": "input",
      "net": "a[0]"
    },
    {
      "name": "a[1]",
      "direction": "input",
      "net": "a[1]"
    }
  ],
  "cells": [
    {
      "instance": "tie_1",
      "cell": "TIELO",
      "inputs": {},
      "outputs": {
        "Y": "1'b0"
      },
      "origin": ""
    }
  ]
}
```

## Fields

| Field | Meaning |
|---|---|
| `format`, `version` | `silicon-netlist`, currently `1`. A reader rejects anything else. |
| `tool` | Which tool produced the file (`silicon-dc`), for provenance. |
| `design` | Top-level module name; every writer uses it as the block name. |
| `library.name`, `library.corner` | The library the netlist was mapped to. `icc2` checks that every mapped cell exists in the library it loaded. |
| `constraints.*` | Clock name and period, input/output delay, uncertainty, clock transition, voltage. `icc2` inherits these instead of asking again, which is what constraint handoff means in practice. |
| `ports[]` | `{name, direction, net}` for every top-level port. Directions drive IO pin placement. |
| `cells[]` | `{instance, cell, inputs, outputs, origin}`. `origin` is the RTL-level operation the cell came from (`ADD8_0`, `MUX2_3`), which is what makes a mapped netlist readable and lets `report_cell` group by origin. |

## Guarantees

* Bit-level: every entry in `inputs`/`outputs` is a net name, one per bit, e.g.
  `sum[3]`, so bus slicing survives the handoff.
* Fully mapped: every `cell` exists in the library; a file that references an
  unknown cell is rejected with the cell names listed.
* Deterministic: cells and ports are written in a stable order, so the same RTL
  and constraints produce byte-identical JSON.
