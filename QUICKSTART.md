# Getting started: running `silicon` like a real tool

`silicon` is two command-line tools that behave like the ones they are modelled
on: `dc_shell` (RTL → gate netlist) and `icc2_shell` (netlist → placed, routed,
GDSII). You can drive them interactively, from Tcl-style scripts, or non-interactively
with one command. Everything runs locally, offline, in pure Python.

- **Requirements:** Python 3.9 or newer. That is the whole list — no NumPy, no
  compilers, no Java, no licences, no network.
- **Size:** the package is under 1 MB; a full ALU flow (synthesis → place → CTS →
  route → GDSII) takes about **one second** on a laptop.
- **Platform:** Linux, macOS, Windows (anywhere Python runs). Nothing is
  macOS- or Linux-specific.

---

## 1. Install

Pick one. All three end up at the same tools.

### a. No install at all (fastest)

```bash
git clone https://github.com/akifmohamed/SILICON.git
cd SILICON
./bin/silicon flow -r examples/rtl/alu.v -t alu --period 4
```

`bin/silicon`, `bin/dc` and `bin/icc2` are self-contained launchers; `bin/dc` *is*
`dc_shell`. Nothing to build, no `pip install` needed.

### b. Install the commands on your PATH (recommended for daily use)

```bash
git clone https://github.com/akifmohamed/SILICON.git
cd SILICON
pip install --user .
# or, if you use pipx:
pipx install .
```

You now have three commands available from any directory, like the real thing:

```bash
$ dc_shell -x "version"
silicon-dc 0.2 (library stdcells_32nm, ss_0p95V_125C)

$ icc2_shell -x "version"
silicon-icc2 0.2 (library stdcells_32nm, 21 cells)

$ silicon --help
```

`pip install --user` may warn that `~/.local/bin` is not on `PATH`; add it once:

```bash
export PATH="$HOME/.local/bin:$PATH"      # put this in ~/.bashrc or ~/.zshrc
```

### c. Editable install (if you want to change the code)

```bash
cd SILICON
pip install -e .        # your edits to silicon/*.py take effect immediately
python3 -m pytest tests/ -q     # 112 passed
```

---

## 2. The 30-second tour

```bash
$ ./bin/silicon flow -r examples/rtl/alu.v -t alu --period 4
[dc] design      : alu (8 signals, 22 operations)
[dc] cells       : 218  nets: 255
[dc] area        : 545.24 um^2
[dc] WNS / TNS   : 1.743 ns / 0.000 ns @ 4.0 ns
[dc] power       : 0.2575 mW (dynamic 0.0199)
[icc2] block     : alu (219 cells)
[icc2] core/die  : 29.07x30.00 / 35.07x36.00 um, 15 rows
[icc2] placement : HPWL 3836.7 um, overlaps 0, util 0.639
[icc2] cts       : 9 sinks, 1 buffers, skew 0.0000 ns
[icc2] route     : 4122.8 um, 208 vias, 0 DRC, 0 overflow, 63 Steiner points, 0 maze net(s)
[icc2] timing    : WNS 1.147 ns, hold 1.094 ns (extracted RC + FLAT OCV, depth 22)
[icc2] parasitics: 250 nets, 2.325 pF, worst net delay 8.7 ps, SPEF written
[icc2] corners   : max ss_0p95V_125C setup 1.147 ns; min ff_1p10V_m40C hold 1.094 ns
[icc2] gdsii     : 14 structures, 493 placements, 713 paths, 0 stream DRC
```

RTL in, GDSII out, with timing, area, power and DRC in between. Look at what it
produced:

```
build/alu.json          netlist database (the dc -> icc2 handoff, human-readable)
build/alu.v             gate-level Verilog netlist
build/alu.area.rpt      cell/net area breakdown
build/alu.timing.rpt    worst paths, per-stage increments
build/alu.qor.rpt       summary: WNS/TNS/hold/area/power/wirelength/vias/DRC
build/alu.def           placement + routing in DEF 5.8
build/alu.lef           block abstract (pins, blockage)
build/cells.lef         standard-cell library abstract
build/tech.lef          layer stack and rules
build/alu.gds           the layout, GDSII stream
build/alu.svg           a picture of the layout (open in any browser)
build/alu.spef          extracted RC parasitics (SPEF 1.0)
build/alu.sdf           delays for back-annotating a gate-level simulation
build/alu.layout.txt    ASCII map of the layout, for a terminal
```

The SVG and the ASCII map need no tools at all. For the GDS, **KLayout** (free)
opens it: `klayout build/alu.gds build/cells.lef build/alu.lef`.

---

## 3. Drive it like the real tool (interactive)

### Synthesis

```bash
$ dc_shell
dc_shell (silicon 0.2) -- type 'help' for commands, 'quit' to exit
dc_shell> read_verilog examples/rtl/counter.v
Information: Parsing 'examples/rtl/counter.v' -- 1 module(s): counter
dc_shell> elaborate counter
Information: Building the design 'counter'. (4 word-level operations, 6 signals)
Information: Inferred clock(s): clk
dc_shell> create_clock -period 3 -name clk
Information: Clock 'clk' created with period 3.0 ns (333.3 MHz).
dc_shell> compile_ultra
Information: Beginning mapping to library 'stdcells_32nm' (ss_0p95V_125C).
Information: Mapped 42 cells, 54 nets, area 151.25 um^2.
Information: Timing WNS 1.275 ns, TNS 0.000 ns at 3.000 ns period.
dc_shell> report_timing -max_paths 2
...
dc_shell> report_constraints
dc_shell> write -format json -output build/counter.json
dc_shell> quit
```

Useful inside `dc_shell`:

| command | what it does |
|---|---|
| `read_verilog a.v b.v` | parse sources (`analyze` and `read_file` are aliases) |
| `elaborate top` | build the design, infer clocks and async resets |
| `create_clock -period 4 -name clk` | set the constraint |
| `set_input_delay`/`set_output_delay`/`set_clock_uncertainty` | IO and margin |
| `set_max_fanout 8`, `set_max_transition 0.30` | drive constraints |
| `compile` / `compile_ultra` | map to the library |
| `set_ocv -cell_late 1.1 -mode aocv` | change the OCV derates, re-time |
| `set_library -min /path/fast.lib` | add a min corner for hold |
| `report_timing`, `report_area`, `report_power`, `report_qor`, `report_constraints` | reports |
| `verify -vectors 256 -seed 7` | mapped netlist vs RTL, random vectors |
| `write -format json -output b.json` | the handoff file for `icc2_shell` |
| `write -format verilog`, `write_sdc` | netlist and constraints |
| `help`, `help <cmd>`, `history` | self-documentation |

`Tab` completion is not implemented — `help` lists everything.

### Place and route

```bash
$ icc2_shell
icc2_shell> read_netlist build/counter.json
Information: Loaded block 'counter' from build/counter.json (42 cells, written by silicon-dc).
Information: Constraint inheritance: clock 'clk' period 3.0 ns, input delay 1.0 ns, ...
icc2_shell> initialize_floorplan -util 0.6 -aspect 1.2
Information: Core 15.390 x 16.000 um, die 21.390 x 22.000 um, 8 rows, 648 sites.
icc2_shell> place_opt
Information: Placed 42 cells, HPWL 497.1 um, utilisation 0.625, 0 overlap(s), 60 iterations.
Information: Congestion: peak bin utilisation 0.324, mean 0.101 (6x6 bins).
icc2_shell> clock_opt
Information: Clock tree: 8 sinks, 1 buffer level(s), 1 buffer(s), insertion delay 0.0380 ns.
icc2_shell> route_auto
Information: Routing: wirelength 535.4 um, 37 vias, 0 DRC, 0 unrouted net(s).
icc2_shell> report_qor
icc2_shell> report_parasitics
icc2_shell> report_corners
icc2_shell> write_gds build/counter.gds
icc2_shell> write_spef build/counter.spef
icc2_shell> write_sdf  build/counter.sdf
icc2_shell> check_gds build/counter.gds      # DRC re-derived from the stream itself
icc2_shell> quit
```

`icc2_shell` takes **any** netlist in the JSON database format — it does not call
`dc_shell` and does not need it. Constraints can come from the handoff file or be
overridden with `create_clock` inside the session.

`place_opt` will build a default floorplan if you skip `initialize_floorplan`
(useful for a quick look); `clock_opt` and `route_auto` refuse to run before
placement, with an error that names the missing stage. `report_corners` needs a
min corner loaded — `set_library -min <path to stdcells_32nm_ff.lib>`; the error
message prints the bundled path for you.

---

## 4. Script mode (what you would keep in the repo)

```bash
dc_shell   -f examples/dc_alu.tcl        # synthesis script
icc2_shell -f examples/icc2_alu.tcl      # P&R script
dc_shell   -x "read_verilog a.v" -x "elaborate a" -x "compile"   # one-shot lines
```

A synthesis script of your own looks like this:

```tcl
read_verilog rtl/mydesign.v
elaborate mydesign
create_clock -period 5.0 -name clk
set_input_delay 1.0
set_output_delay 1.0
compile_ultra
check_design
report_timing -max_paths 5
report_area
report_qor
verify -vectors 256
write -format json -output build/mydesign.json
```

Exit codes are meaningful for CI: `0` success, `3` timing violation
(`--min-wns`), `4` equivalence mismatch, `2` bad usage, `1` shell error.

---

## 5. Your own RTL

```bash
mkdir -p ~/mydesign && cd ~/mydesign
cat > gray.v <<'V'
module gray #(parameter W = 8) (
    input  wire         clk, rst_n, en,
    output reg  [W-1:0] q,
    output wire [W-1:0] g
);
    reg [W-1:0] next;
    always @(*) next = en ? q + 8'd1 : q;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) q <= {W{1'b0}}; else q <= next;
    assign g = q ^ (q >> 1);
endmodule
V
silicon flow -r gray.v -t gray --period 5 -o out
```

Output (measured, not illustrative):

```
[dc] cells       : 35  nets: 47
[dc] WNS / TNS   : 3.523 ns / 0.000 ns @ 5.0 ns
[icc2] route     : 561.6 um, 42 vias, 0 DRC, 0 overflow, 18 Steiner points
[icc2] timing    : WNS 3.465 ns, hold 0.168 ns (extracted RC + FLAT OCV, depth 10)
[icc2] stream DRC: 0
```

Then equivalence-check it independently:

```bash
silicon verify -r gray.v -t gray --vectors 64
# Outputs compared : 1024      Mismatches : 0      Result : PASS
```

### What Verilog is accepted

Modules, parameters, ANSI and non-ANSI ports, `assign`, `always @(*)`,
`always @(posedge clk [or negedge rst_n])`, `if`/`case`/statically-bounded `for`,
concatenation, replication, part-select, `+ - * & | ^ ~ ! << >> == < >` on
unsigned vectors. Arithmetic is word-level: adders become carry chains,
multipliers become a partial-product array, `case` becomes a mux tree.

Rejected loudly (never silently mis-synthesised):

| you write | you get |
|---|---|
| `initial begin ... end` | `'initial' is not synthesizable by this engine` |
| `reg [7:0] mem [0:255];` | memory arrays are not supported |
| `always @(*) if (en) q = d;` | `combinational loop (inferred latch)` — assign on every path |
| `generate`, functions, tasks, signed arithmetic, SystemVerilog | parse error naming the line |

That is the honest boundary: anything it accepts, it simulates against your RTL
with `verify`; anything it cannot do, it refuses.

---

## 6. Knobs worth turning

Everything below is a real lever, not decoration. Try them on the bundled
examples and watch the reports move.

| goal | CLI | shell |
|---|---|---|
| tighter clock | `--period 3` (default 10 ns) | `create_clock -period 3` |
| denser/holier floorplan | `--util 0.75`, `--aspect 1.5` | `initialize_floorplan -util 0.75 -aspect 1.5` |
| more/less OCV pessimism | `--ocv aocv` | `set_ocv -mode aocv`, `set_ocv -off` |
| min/max corners or single corner | `--no-mcmm`, `--min-lib <fast.lib>` | `set_library -min <fast.lib>` |
| routing order (A/B) | `--no-td-route` | `PhysicalConfig(routing_timing_driven=False)` |
| fail the run on timing | `--min-wns 0` (exit code 3) | — |
| fanout repair | `--max-fanout 6` | `set_max_fanout 6` |

Two examples that show the tool responding:

```bash
# too fast: the tool says so instead of rounding it away
$ silicon flow -r examples/rtl/alu.v -t alu --period 2 --min-wns 0 ; echo "exit=$?"
[dc] WNS / TNS   : -0.257 ns / -0.340 ns @ 2.0 ns
[icc2] timing    : WNS -1.010 ns, hold 1.083 ns (extracted RC + FLAT OCV, depth 22)
exit=3

# sweep the period until it breaks; each run is ~1 s (measured)
$ for p in 1.5 2 2.5 4; do
    silicon flow -r examples/rtl/alu.v -t alu --period $p | grep "timing    :"
  done
[icc2] timing    : WNS -1.484 ns, hold 1.086 ns ...
[icc2] timing    : WNS -1.010 ns, hold 1.083 ns ...
[icc2] timing    : WNS -0.581 ns, hold 1.105 ns ...
[icc2] timing    : WNS  1.147 ns, hold 1.094 ns ...
```

---

## 7. Using the outputs in other tools

| artefact | open with |
|---|---|
| `.gds` | KLayout (`klayout out/x.gds out/x.lef`), or any GDS viewer |
| `.def` | KLayout / OpenROAD (`read_def`) |
| `.lef` (`tech.lef`, `cells.lef`, `x.lef`) | KLayout, OpenROAD, TritonRoute |
| `.spef` | any timing tool that reads SPEF 1.0 |
| `.sdf` | gate-level simulation back-annotation, e.g. Icarus Verilog: `$sdf_annotate("x.sdf", tb.dut);` |
| `.v` | any simulator (the netlist uses only the library cells) |
| `.svg`, `.layout.txt` | a browser / a terminal |
| `.rpt` | a text editor (they are plain aligned text) |

---

## 8. "Can I run a complete ASIC flow with this?"

**RTL to GDSII, yes, end to end, in one command.** Synthesis → timing-driven
placement → CTS → routing → extraction → STA → DRC (in memory *and* on the GDS
stream) → GDSII/DEF/LEF/SPEF/SDF, with equivalence as a gate. There are no stubs,
no `TODO: implement`, no dead commands — every command in `help` does the work it
names.

**A tapeout-ready signoff flow, no.** Be clear-eyed about the boundary:

| part of a real ASIC flow | here |
|---|---|
| Verilog front end, elaboration, mapping | implemented, but a Verilog subset (no memories, `generate`, signed arithmetic, functions) |
| Constraints (SDC) | clock period, IO delay, uncertainty, max fanout/transition; not the full SDC language |
| Timing | NLDM STA with RC, flat/AOCV OCV, min/max corners; **one** clock domain, no MMMC matrix, no crosstalk, no CRPR |
| Placement / CTS / routing | real algorithms, small-scale: quadratic + legalisation, bisection CTS, track + maze routing; no negotiated-congestion rip-up, no useful skew, no timing-driven wire sizing |
| DRC | metal rules on routed layers, checked in memory and on the stream; **no LVS**, no antenna, no density/CMP, no well/implant, placeholder cell internals |
| DFT | absent — no scan insertion, no BIST, no ATPG |
| Power | activity-based estimate from Liberty leakage + switching; no internal power tables, no IR/EM, no UPF |
| Library | **synthetic** 32 nm, 21 cells, generated fast corner |

So: use it to *learn and demonstrate the flow*, to experiment with algorithms, to
generate real GDS/DEF/SPEF/SDF files for a viewer or a course project, and as a
reference implementation you can read end to end (about 10 k lines of Python).
Do **not** use its numbers as a claim about any silicon, and do not tape out from
it — it has no LVS, no DFT and no signoff rules. `docs/LIMITATIONS.md` is the
complete, itemised list.

If you need actual signoff, the same scripts map onto OpenROAD/Yosys/OpenSTA
concepts one-for-one, which is the intended use of this repository as a teaching
and prototyping vehicle.

---

## 9. Troubleshooting

| symptom | fix |
|---|---|
| `command not found: dc_shell` | `pip install --user .` then add `~/.local/bin` to `PATH`, or use `./bin/dc` |
| `no block loaded; run read_netlist` | `icc2_shell` needs `read_netlist <file.json>` first |
| `stage 'place' has not run yet` | run `initialize_floorplan` then `place_opt` |
| `Error: unknown option '-x'` | the shells are strict on purpose; `help <command>` shows the accepted options |
| `fatal: repository ... does not exist` on clone | check the URL; the repo is `akifmohamed/SILICON` |
| flow is slow | it is single-threaded Python; the ALU is ~1 s, a 10× larger design is ~10× slower |
| numbers differ from the README | period/utilisation/OCV differ, or you are on a different library |
