# Limitations

This file is the contract about what this code does *not* do. It is deliberately
specific, because a plausible-sounding tool that quietly skips a rule is worse
than a small tool with a written scope.

Two things to keep in mind throughout:

1. **The library is synthetic.** `silicon/lib/stdcells_32nm.lib` contains 21
   cells with invented areas and NLDM tables. Any area, power or timing number
   produced by this toolchain describes *this* library and *this* code, not a
   foundry process.
2. **Optimisation quality is not the goal.** The engines implement the structure
   of a synthesis and a P&R tool — real parsing, real mapping, real legalisation,
   real routing with a real rule deck — not commercial QoR.

## Design rule checking

Implemented (`pnr.DrcChecker`, in-memory): SHORT, SPACING (parallel and corner),
WIDTH, MIN_AREA, VIA_ENCLOSURE, VIA_SIZE, VIA_SPACING, plus track overflow and
unrouted nets.

Implemented (`writers.check_gds`, on the GDSII stream): the same rule deck
re-checked against the flattened stream, with connectivity extracted from
touching shapes and cut-connected layers, and net names taken from the labels
written into the stream.

Not implemented:

* **Cell-internal DRC.** The library's cells have placeholder geometry (an
  outline plus one rectangle), so M1 and below are excluded from the stream
  check. Real cell layouts, and therefore cell-level LVS/DRC, are out of scope.
* **Antenna rules**, density/CMP rules, well/implant/diffusion rules,
  multi-patterning colouring, and stress/ESD rules.
* **LVS** (layout vs schematic) and **ERC**. Connectivity extraction exists only
  to keep the DRC honest about which pieces belong to the same net.
* **Off-grid/width-dependent rules**, min-step, notch, end-of-line, and any rule
  that needs a real 2-D shape engine. Geometry is Manhattan rectangles and paths.

## Routing

Implemented: iterated 1-Steiner topology, MST decomposition, per-layer track
allocation with spacing-aware legality, L/Z connections with enclosure-correct
corner vias, minimum-area widening, optional via doubling, rip-up/retry, wire
merging, slack-ordered net sequencing (`routing_timing_driven`, `--no-td-route`
to disable), and a grid A* maze router used as the fallback for nets the track
allocator cannot place legally. A net that cannot be routed legally is *failed
and reported*, never forced onto an occupied track.

Not implemented:

* **Negotiated-congestion rip-up** (PathFinder-style history costs). Retry is a
  small fixed number of rounds with an offset, not a converging negotiation.
* **Timing-driven routing decisions beyond net order.** The router does not
  size wires, insert buffers or trade wirelength for slack inside a net: a
  critical net gets the first pick of track, not a different topology.
* **Detailed wire sizing / buffer insertion during routing**, and redundant-via
  insertion is off by default (`via_doubling=False`) because it trades wirelength
  for via count.
* **Grid pitch is uniform** and coarse (0.20 µm). Real advanced-node routing uses
  per-layer pitches, non-default rules and route pitches finer than the spacing
  rule allows here; the router refuses to start when a spacing rule exceeds the
  pitch rather than emitting illegal geometry.
* **No track-pattern/stitching rules, no off-track pins, no jogs for pin access**
  beyond the L/Z shapes and the maze fallback.

## Placement

Implemented: quadratic placement with conjugate-gradient solve, timing-criticality
net weights, row legalisation with real fit checks, HPWL-driven swaps, congestion
map (probabilistic demand vs track supply) with a peak-relief step, and reporting
of HPWL, weighted HPWL and bin utilisation.

Not implemented:

* Detailed placement (wirelength-driven cell movement at site granularity),
  scan-order or datapath placement, multi-row/dual-height cells, macros, keepouts,
  regions, bounds, movebounds, pin assignment, and pin-access optimisation.
* Timing-driven *legalisation*: criticality influences the quadratic solve, but
  legalisation and swaps optimise weighted wirelength, not path slack directly.
* Congestion-driven *rip-up* of the placement; the peak-relief step is a local
  swap, not a global spreading pass.

## Clock tree

Implemented: geometric bisection tree with real buffer cells, per-sink insertion
delay from buffer arcs plus wire RC, clock skew reported and propagated into STA.

Not implemented: useful-skew scheduling, buffer sizing/inverting pairs, latch-based
trees, clock gating, mesh/H-tree templates, shielding, and clock-route DRC as a
separate pass (the clock nets go through the same router as data nets).

## Parasitics

Implemented: `parasitics.py` builds a distributed RC tree from the routed
geometry (split at junctions and pins), takes R and C per layer from the rule
deck, evaluates Elmore delays to every sink and the total load, and writes SPEF
1.0 that `read_spef_summary` can read back. Post-route timing uses this instead of
a per-fanout estimate, and `report_parasitics` lists the worst nets.

Not implemented: coupling capacitance (no `*CAP` coupling section, no crosstalk
driver), inductance, shielding, via resistance, temperature/width-dependent
resistivity, and reduction of long RC trees before they reach the timer.

## Timing and power

Implemented: Liberty NLDM delay and output-slew tables at (input transition,
output load), worst-of-rise/fall arcs, net delay and loads from the extracted RC
tree, OCV derates with separate late/early factors for cells and nets, min/max
corner analysis (setup at the slow corner, hold at the fast one), library
`max_transition`/`max_capacitance` limits, setup and hold checks with clock skew,
WNS/TNS, per-endpoint slack, worst-path reporting with per-stage increments and
the wire component of each stage, switching plus leakage power.

Not implemented: internal power tables, CCS/ECSM, AOCV/POCV (the derates are
flat), crosstalk delay and noise, IR drop and electromigration, clock-gating
power, SDF/VCD/SPEF-driven handoff to another timer (SPEF is written for *other*
tools, not consumed by this one), and statistical timing.

## Synthesis

Implemented: full Verilog-subset parse, width inference, parameter overrides,
flattening, constant folding/propagation, dead-code elimination, identity rules,
carry-chain selection, technology mapping onto the library, fanout repair, and a
simulation-based equivalence gate.

Not implemented: retiming, sequential optimisation beyond constant/identity
folding, resource sharing, Booth/Wallace multipliers, carry-lookahead synthesis,
signed arithmetic, memories, `generate`, `initial`, functions/tasks, SystemVerilog,
and formal equivalence checking.

## Handoff and file formats

Implemented: JSON netlist database (`docs/DATABASE.md`), Verilog netlist out, SDC
write/read, DEF 5.8 write, GDSII write **and** read-back for DRC, LEF 5.8 write and
read, SPEF 1.0 write and read, SVG layout view.

Not implemented: DEF/LEF *import* into the database (only the LEF reader, used to
check the writer), OASIS, SDF, Verilog-A/UPF, and physical hierarchy (the block
macro is written, but a hierarchical P&R flow is not).

## Practicalities

* Single-threaded, pure Python. The ALU flow is under a second; a design 100x
  larger would be slow and would need better data structures (grid indices,
  incremental timing) rather than more memory tweaks.
* No GUI. Layout inspection is the SVG view and the ASCII map.
* Two synthetic corners ship with the repository (slow and fast). They are
  generated from one cell set by `tools/make_corner.py`, which is why the
  min/max analysis is meaningful as *code* but says nothing about real silicon.
* Error handling is explicit (a `ShellError`/`PhysicalError` with a message) rather
  than a crash, but the tool does not attempt to recover from a malformed netlist
  by guessing.
