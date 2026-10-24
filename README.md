# Silicon — a from-scratch logic synthesis and physical design toolchain

Two tools, written from nothing in Python, that do the same two jobs as a
commercial RTL-to-GDS flow:

* **`dc`** — RTL in, gate netlist out. Verilog parser, elaborator, word-level
  optimiser, technology mapper, static timing analyser, constraint handling,
  reports and a netlist database.
* **`icc2`** — gate netlist in, layout out. Floorplanning, timing-driven
  placement with a congestion map, clock-tree synthesis, a rule-driven router
  with a grid maze fallback, fillers, design-rule checking, DEF/GDSII/LEF/SVG.

They are separate tools on purpose. `dc` writes a JSON netlist database; `icc2`
reads that database (or any file in the same format) and never imports the
synthesis engine. You can run either one alone.

```console
$ bin/dc   -f examples/dc_alu.tcl      # RTL -> netlist + timing/area/power reports
$ bin/icc2 -f examples/icc2_alu.tcl   # netlist -> DEF, GDSII, LEF, SVG
$ bin/silicon flow -r examples/rtl/alu.v -t alu --period 4    # both, in one run
```

Everything here is real code operating on real data structures: a parsed
Verilog AST, a mapped gate netlist, a Liberty library, a placement, routed
geometry with a rule deck, and a GDSII stream that is read back and
design-rule-checked. What it is *not* is a reproduction of commercial quality of
results, and the numbers below are from a **synthetic** 32 nm library that ships
in this repository. Nothing here is a claim about real silicon.

---

## What actually runs

```
RTL (.v)
  │  verilog.py     lexer + recursive-descent parser -> AST
  │  elaborate.py   parameters, widths, generate-free flattening, reset/latch handling
  ▼
Word-level design (signals, macro cells)
  │  synth.py       constant folding, DCE, width reduction, carry-chain mapping
  ▼
Gate netlist (AND2/NAND2/NOR2/AOI21/OAI21/MUX2/XOR/FA/DFF/DFFR…)
  │  sta.py         NLDM delay/slew, OCV derates, min/max corners, power
  │  serialize.py   JSON netlist database  ──────────────┐   ← the handoff
  ▼                                                      │
Reports (.rpt), Verilog out, SDC                          │
                                                          ▼
                                        icc2_shell / cli  reads the JSON database
                                          │  pnr.py        floorplan, placement, CTS, routing
                                          │  rules.py      metal rule deck (M1..M6)
                                          │  writers.py    DEF, GDSII, SVG, ASCII
                                          │  lef.py        tech/cell/block LEF (writer + reader)
                                          ▼
                                    DEF · GDSII · LEF · SVG · reports
```

Every stage is exercised by the equivalence gate: the mapped netlist is
simulated against the RTL for random vectors and must match bit-for-bit before
the flow is considered to have worked.

---

## Quick start

No dependencies beyond Python 3.9+.

```bash
git clone <this repo> && cd silicon
python3 -m pytest tests/ -q        # 84 tests

# whole flow: RTL -> gates -> placed and routed layout
./bin/silicon flow -r examples/rtl/alu.v -t alu --period 4

# or drive the tools individually, like the commercial shells
./bin/dc   -f examples/dc_alu.tcl
./bin/icc2 -f examples/icc2_alu.tcl

# one-liners
./bin/dc   -x 'read_verilog examples/rtl/mac.v' -x 'elaborate mac' -x 'compile' -x 'report_qor'
./bin/icc2 -x 'read_netlist build/alu.json' -x 'initialize_floorplan' -x 'place_opt' \
           -x 'clock_opt' -x 'route_auto' -x 'report_drc' -x 'check_gds build/alu.gds'
```

Artefacts land in `build/`: `<top>.{json,v,def,gds,lef,svg,layout.txt}` plus
`area/timing/qor` reports, `cells.lef`, `tech.lef`.

Install as a package if you prefer entry points:

```bash
pip install -e .        # provides silicon, dc_shell, icc2_shell
```

---

## `dc`: logic synthesis

`dc_shell` command set (all implemented, none stubbed):
`read_verilog`, `elaborate`, `current_design`, `list_designs`, `set_library`,
`create_clock`, `set_input_delay`, `set_output_delay`, `set_clock_uncertainty`,
`set_max_fanout`, `set_max_transition`, `compile`, `verify`, `report_timing`,
`report_area`, `report_power`, `report_qor`, `report_cell`, `report_design`,
`check_design`, `write`, `write_sdc`, `read_sdc`, `source`, `help`, `history`,
`version`.

Engines:

* **Verilog front end** — modules, ports (ANSI and non-ANSI), parameters,
  continuous assignments, `always @(posedge clk)`, async reset, `case`/`if`/
  `for` (statically bounded), concatenation, replication, part-select, signed-free
  arithmetic, function-free expressions.
* **Elaboration** — width inference with Verilog sign/extension rules, parameter
  overrides at instantiation, hierarchy flattening, latch inference detection,
  multiple-driver detection, and reset-value propagation into flops.
* **Optimisation** — constant folding and propagation, dead-code elimination,
  identity/annihilation rules on the word operators, common subexpression reuse
  per word, and carry-chain selection between ripple and skip structures.
* **Technology mapping** — arithmetic to full-adder chains, comparisons to
  AOI21/OAI21 chains, muxes to MUX2X1, with the standard cells of the library;
  `FILLX1`/`TIEHI`/`TIELO` handled at the right stage.
* **STA** — per-arc delay and output slew from the Liberty NLDM tables at
  (input transition, output load), worst-of-rise/fall arcs, net delay from the
  extracted RC tree, OCV derates (separate late/early factors for cells and
  nets), library `max_transition`/`max_capacitance` limits, setup and hold
  checks, clock skew from the extracted clock tree, WNS/TNS, and reportable
  worst paths with per-stage increments and the wire component of each stage.
* **Min/max corners** — `set_library -min fast.lib` switches the hold analysis
  to the fast corner; `report_corners` shows setup at the slow corner and hold
  at the fast one. Both bundled corners are synthetic and the fast one is
  generated from the slow one by `tools/make_corner.py`.
* **Power** — switching (`0.5·C·V²·f·α`) per net plus Liberty leakage, clock
  nets at twice the clock frequency.
* **Equivalence gate** — `verify` (also run by the CLI) simulates the mapped
  netlist against the elaborated RTL on random vectors and reports mismatches.

---

## `icc2`: physical design

`icc2_shell` command set: `read_netlist`, `set_library`, `create_clock`,
`initialize_floorplan`, `place_opt`, `clock_opt`, `route_auto`, `report_drc`,
`check_gds`, `report_congestion`, `report_placement`, `report_clock_tree`,
`report_parasitics`, `report_corners`, `report_timing`, `report_area`,
`report_power`, `report_qor`, `report_layout`, `write_def`, `write_gds`,
`write_spef`, `write_lef`, `write_library_lef`, `write_tech_lef`, `write_svg`,
`write_netlist`, `help`, `history`, `version`.

Engines:

* **Floorplan** — core sizing from cell area and a utilisation target, aspect
  ratio, site/row snapping, IO pins distributed on the core boundary.
* **Placement** — star-model quadratic placement solved with conjugate gradient,
  net weights scaled by timing criticality (`timing_weight`), row legalisation
  with real fit checks, HPWL-driven swap improvement, and a congestion step that
  relieves the worst bin. Reports HPWL, weighted HPWL, peak/mean bin utilisation.
* **Congestion map** — probabilistic demand (net bounding boxes) against track
  supply per bin; printed by `report_congestion`, used by placement.
* **CTS** — geometric bisection clock tree with real buffer cells, per-sink
  insertion delay from buffer arcs plus wire RC, fed back into STA as a
  propagated clock.
* **Routing** — iterated 1-Steiner topology on the Hanan grid, Prim MST
  decomposition, spacing-aware track allocation across two layers per
  direction, L/Z connections with corner vias extended past the via (enclosure),
  minimum-area widening, optional via doubling, and rip-up/retry on violation.
* **Maze routing** — A* over `(x track, y track, layer)` with a per-layer
  occupancy map; a strict pass that respects spacing, then a relaxed pass, then a
  second cut everywhere it can. Used for nets the track allocator cannot place:
  a net is never shared onto an occupied track to hide congestion — it is failed,
  handed to the maze, and reported as unrouted if the maze cannot help.
* **Parasitic extraction** — `parasitics.py` splits the routed geometry at
  junctions and pin positions, assigns R and C per layer from the rule deck,
  computes the Elmore delay to every sink and the total load, and writes
  SPEF 1.0 (`write_spef`, plus `report_parasitics` for the worst nets). Post-route
  timing then uses the wires that were actually drawn.
* **DRC** — `DrcChecker` re-derives every violation from the geometry
  (SHORT, SPACING, WIDTH, MIN_AREA, VIA_ENCLOSURE, VIA_SIZE, VIA_SPACING)
  independently of the allocator's bookkeeping.
* **GDS DR Check** — `check_gds` reads the written stream back, flattens it,
  extracts connectivity (touching shapes plus cut-connected layers) and names
  components from the net labels written into the stream, then checks the same
  rule deck. The stream therefore has to be legal on its own, not just in memory.
* **Fillers** — `FILLX1` in every row gap, written into DEF/GDS/SVG.
* **Writers** — DEF 5.8 (design, rows, components, pins, routed nets), GDSII
  (real stream format: BGNLIB/STRUCT/SREF/PATH/BOUNDARY/TEXT, 1 nm database unit,
  excess-64 reals, per-net labels, cut layers), LEF 5.8 (tech, cells, block macro),
  and a self-contained SVG view.

Rule deck (`silicon/rules.py`, synthetic): M1–M6 with width, spacing, minimum
area, cut size, cut spacing, enclosure, resistance and capacitance.
See [docs/DESIGN_RULES.md](docs/DESIGN_RULES.md).
The router refuses to start if a spacing rule is wider than the track pitch,
rather than producing illegal geometry.

---

## Results (synthetic library, 4 ns clock)

`./bin/silicon flow -r examples/rtl/<top>.v -t <top> --period 4`

Timing is reported the way a signoff flow reports it: setup at the slow corner
with extracted RC and OCV derates, hold at the fast corner.

| design | cells | area (µm²) | WNS pre-route (ns) | routed wire (µm) | vias | DRC | stream DRC | setup WNS (ns) | hold WNS (ns) | core (µm) × rows |
|---|---|---|---|---|---|---|---|---|---|---|
| `alu` (8-bit, 5 ops + MAC) | 218 | 545.24 | +1.743 | 4101.7 | 208 | 0 | 0 | +1.148 | +1.095 | 29.07 × 30.00, 15 |
| `counter` (8-bit, enable/load) | 42 | 151.25 | +2.275 | 584.4 | 41 | 0 | 0 | +2.131 | +0.168 | 15.39 × 16.00, 8 |
| `mac` (8×8 accumulate) | 163 | 660.80 | +0.959 | 4385.2 | 199 | 0 | 0 | −0.001 | +0.211 | 31.92 × 32.00, 16 |

The `mac` result is the honest one to look at: with a per-fanout wire estimate it
looked comfortable, and once the extracted RC of the real wires and the OCV
derates go in it lands 1 ps short of 4 ns. The tool reports that instead of
softening the model, and `--min-wns 0` exits non-zero.

Extraction gives 250 nets / 2.32 pF / 350 Ω on the ALU with a 5.9 ps worst net
delay; the SPEF is written next to the layout.

Timing-driven placement is measurable rather than decorative. On the ALU, with
the old per-fanout wire model so the comparison is not mixing effects:

| | HPWL (µm) | weighted HPWL (µm) | routed wire (µm) | post-route WNS (ns) |
|---|---|---|---|---|
| wirelength only | 4097 | 4097 | 4388 | +1.596 |
| timing-weighted (default) | 3935 | 5251 | 4222 | +1.617 |

At a 2 ns period, where the design genuinely misses timing, the same comparison is
WNS −0.404 → −0.384 ns and TNS −0.625 → −0.594 ns. The gain is small because the
weight is the only lever placement has here — and the tool still reports the
negative slack instead of hiding it.

Determinism: the same inputs produce byte-identical outputs across processes and
hash seeds. `md5sum` of every artefact is stable under `PYTHONHASHSEED=1` and
`PYTHONHASHSEED=7`.

```
$ PYTHONHASHSEED=1 ./bin/silicon flow -r examples/rtl/alu.v -t alu -o /tmp/a
$ PYTHONHASHSEED=7 ./bin/silicon flow -r examples/rtl/alu.v -t alu -o /tmp/b
$ diff -r /tmp/a /tmp/b && echo identical
identical
```

Equivalence (`verify`, part of every `flow` run): alu 2304 output bits over 256
vectors, counter and mac likewise — **0 mismatches**.

## Repository layout

```
bin/                    dc, icc2, silicon launchers
silicon/
  verilog.py            lexer + parser
  elaborate.py          widths, parameters, flattening, reset handling
  netlist.py            WordDesign / GateNetlist models
  library.py            Liberty parser + NLDM lookup
  lib/stdcells_32nm.lib    synthetic library, slow corner (21 cells)
  lib/stdcells_32nm_ff.lib synthetic fast corner, generated (21 cells)
  synth.py              optimisation + technology mapping
  sta.py                static timing + power
  rules.py              metal rule deck + GDSII layer map
  parasitics.py         RC extraction (Elmore), SPEF writer and reader
  pnr.py                floorplan, placement, congestion, CTS, router, maze, DRC
  writers.py            DEF, GDSII, SVG, ASCII, GDS read-back + stream DRC
  lef.py                LEF writer + reader
  reports.py            report formatting
  sim.py                gate-level and word-level 4-state simulators
  serialize.py          JSON netlist database (the dc -> icc2 handoff)
  shell.py              shared command-shell plumbing
  dc_shell.py           synthesis shell
  icc2_shell.py         physical-design shell
  cli.py                silicon synth|pnr|flow|verify|lib|dc|icc2|version
docs/
  DESIGN_RULES.md       the rule deck, what is checked where, how to change it
  DATABASE.md           the JSON handoff format
  LIMITATIONS.md        what is deliberately not implemented, and why
tools/
  make_corner.py        regenerates the fast corner from the slow one
examples/
  rtl/{alu,counter,mac}.v
  dc_alu.tcl, icc2_alu.tcl    scripted tool runs
  alu_layout_preview.svg      rendered ALU layout
tests/                  96 tests
```

---

## Tests

```bash
python3 -m pytest tests/ -q     # 96 passed
```

The suite covers the parser, elaboration, the Liberty parser, synthesis and its
equivalence gate, STA, floorplan/place/CTS/route invariants, the rule deck and
the DRC checker (each rule has a test that makes it fire), the maze router,
timing-driven and congestion-aware placement, parasitic extraction and the SPEF
round trip, OCV derates, library limits, min/max corner analysis, regeneration of
the fast corner from the slow one, all four writers, the GDS read-back and stream
DRC, and both shells end-to-end — including that `icc2` runs from the JSON file
alone with `dc` nowhere in the picture.

---

## Honest limitations

The engines are real; the numbers and the library are not. Read
[docs/LIMITATIONS.md](docs/LIMITATIONS.md) for the full list. The short version:

* The 32 nm library is **synthetic** — 21 cells, made-up areas and NLDM tables.
  QoR says what *this* code does on *that* library, nothing about a real process.
* Routing layers only get full DRC. Cell-internal M1 is a placeholder rectangle
  per cell, so it is excluded from the GDS check instead of producing thousands
  of false violations.
* No antenna, density/CMP, multi-patterning, or well/implant rules; no LVS.
* Liberty is a subset: NLDM delay/slew, capacitance, leakage, setup/hold and
  the max-transition/max-capacitance limits. No internal power tables, no
  CCS/ECSM, no crosstalk, no SDF output.
* Placement is quadratic + legalisation + swaps. No wirelength-driven detailed
  placement, no region/boundary constraints, no multi-row cells, no pin
  assignment optimisation.
* CTS is a geometric bisection tree: no useful-skew scheduling, no buffer
  sizing, no latch-based trees, no clock gating.
* Routing is track-based with a grid maze fallback; no negotiated-congestion
  rip-up, no timing-driven routing, no redundant-via insertion by default, no
  wire sizing beyond minimum-area repair.
* Synthesis is word-level with carry-chain mapping: no retiming, no sequential
  optimisation beyond constant/identity folding, no signed arithmetic, no
  memories, no `generate`, no `initial`, no functions/tasks.
* No physical hierarchy, no ECO flow, no scan insertion/DFT, no formal
  equivalence (simulation-based equivalence only), no power integrity.

---

## Publishing this repository to GitHub

The working tree here is already a git repository with a real commit history on
`main`. It was created in a sandbox with no GitHub credentials, so nothing has
been pushed from it — pushing is the one step you have to run yourself:

```bash
# with an authenticated GitHub CLI
scripts/publish.sh your-user/silicon            # public
scripts/publish.sh your-user/silicon --private  # private

# or by hand
git remote add origin git@github.com:your-user/silicon.git
git push -u origin main
```

`scripts/publish.sh` refuses to run on a dirty tree, runs the test suite first,
sets a git identity if the checkout has none, creates the repository through
`gh repo create` when it can, and pushes. It never writes credentials into the
repository.

`.github/workflows/ci.yml` runs on every push and pull request: the unit tests on
Python 3.9–3.12, the three example flows with `--max-drc 0`, the equivalence
gate, a cross-hash-seed determinism check, both scripted shells and the GDSII
stream check.

---

## License

MIT. The cell library, the rule deck and all data in this repository are
synthetic and were created for this project.
