"""``dc_shell``: the synthesis shell.

A working clone of the Design Compiler command surface this toolchain needs.
Commands read and elaborate RTL, apply constraints, optimise, map to a library
and report QoR. Run it interactively or in script mode.

    dc_shell                       # interactive
    dc_shell -f scripts/synth.tcl  # script
    echo "read_verilog a.v; elaborate top; compile" | dc_shell
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

from . import reports
from .elaborate import elaborate
from .library import Library, default_library, read_library
from .netlist import GateNetlist, WordDesign
from .serialize import dump as dump_netlist
from .shell import Shell, ShellError, parse_options
from .sim import equivalence_check
from .sta import (PowerResult, TimingConfig, TimingResult, analyse,
                  analyse_corners)
from .synth import OptStats, TechmapOptions, synthesize
from .verilog import VerilogError, parse_modules

VERSION = "silicon-dc 0.1"


class DcSession:
    """State shared by the commands: source, design, constraints, results."""

    def __init__(self, library: Optional[Library] = None) -> None:
        self.library = library or default_library()
        self.library_min = None            # fast corner for hold, when loaded
        self.corner_results = {}
        self.sources: Dict[str, object] = {}
        self.modules: List = []
        self.design: Optional[WordDesign] = None
        self.netlist: Optional[GateNetlist] = None
        self.top: Optional[str] = None
        self.config = TimingConfig()
        self.timing: Optional[TimingResult] = None
        self.power: Optional[PowerResult] = None
        self.opt_stats: Optional[OptStats] = None
        self.tech_options = TechmapOptions()
        self.compiled = False
        self.library_path = "built-in"

    def require_rtl(self) -> None:
        if not self.modules:
            raise ShellError("no RTL loaded; run read_verilog first")

    def require_compiled(self) -> None:
        if self.netlist is None or self.timing is None:
            raise ShellError("design not compiled; run compile or compile_ultra first")

    def analyse(self) -> None:
        """Re-run timing on the mapped netlist against the current settings.

        Constraint and derate changes call this instead of re-synthesising: the
        netlist is already mapped, so only the timing view is stale.
        """
        self.require_compiled()
        libraries = {"max": self.library}
        if self.library_min is not None:
            libraries["min"] = self.library_min
        self.timing, self.power, self.corner_results = analyse_corners(
            self.netlist, libraries, self.config)


class DcShell(Shell):
    prompt = "dc_shell> "

    def __init__(self, session: Optional[DcSession] = None, **kwargs) -> None:
        super().__init__(VERSION, **kwargs)
        reports.TOOL = VERSION
        self.s = session or DcSession()
        self._register()

    # -- helpers ---------------------------------------------------------
    def _paths(self, args) -> List[str]:
        out = []
        for arg in args:
            clean = arg.strip("[]{}")
            if clean.startswith("get_ports") or clean.startswith("get_files"):
                continue
            if clean in ("get_ports", "get_files", "-of_objects"):
                continue
            if os.path.exists(clean):
                out.append(clean)
            elif clean.endswith(".v") or clean.endswith(".sv"):
                raise ShellError(f"file not found: {clean}")
        return out

    # -- commands --------------------------------------------------------
    def _register(self) -> None:
        s = self.s

        @self.register("read_verilog", "read_verilog <file.v> [<file.v> ...]",
                       "read and parse Verilog sources", aliases=("analyze", "read_file"))
        def read_verilog(args):
            paths = self._paths(args)
            if not paths:
                raise ShellError("read_verilog needs at least one existing .v file")
            added = 0
            for path in paths:
                with open(path, "r", encoding="utf-8") as handle:
                    modules = parse_modules(handle.read(), path)
                s.modules.extend(modules)
                s.sources[path] = [m.name for m in modules]
                added += len(modules)
                self.write(f"Information: Parsing '{path}' -- {len(modules)} module(s): "
                           + ", ".join(m.name for m in modules))
            self.write(f"Information: {added} module(s) in the design library "
                       f"({len(s.modules)} total).")
            return ""

        @self.register("elaborate", "elaborate <top> [-parameters {NAME=VALUE ...}]",
                       "build the hierarchical design for <top>")
        def elaborate_cmd(args):
            s.require_rtl()
            flags, opts, pos = parse_options(args, options=("parameters", "arch"))
            if not pos:
                raise ShellError("elaborate needs a top-level module name")
            top = pos[0].strip("{}")
            params = {}
            if "parameters" in opts:
                for pair in opts["parameters"].strip("{}").split():
                    if "=" in pair:
                        key, value = pair.split("=", 1)
                        params[key] = int(value, 0)
            try:
                s.design = elaborate(s.modules, top, params, where=top)
            except VerilogError as exc:
                raise ShellError(str(exc))
            s.top = top
            s.compiled = False
            clock_ports = sorted({c for c in s.design.clocks})
            if clock_ports:
                s.config.clock_name = clock_ports[0].split(".")[-1]
            self.write(f"Information: Building the design '{top}'. "
                       f"({len(s.design.cells)} word-level operations, "
                       f"{len(s.design.signals)} signals)")
            if s.design.clocks:
                self.write("Information: Inferred clock(s): "
                           + ", ".join(sorted(s.design.clocks)))
            if s.design.async_resets:
                self.write("Information: Inferred asynchronous reset(s): "
                           + ", ".join(sorted(s.design.async_resets)))
            return ""

        @self.register("current_design", "current_design [<name>]",
                       "report or set the current design")
        def current_design(args):
            if args:
                s.top = args[0]
                self.write(f"Information: Current design set to '{s.top}'.")
                return ""
            return f"Current design: {s.top or '(none)'}"

        @self.register("list_designs", "list_designs", "list parsed modules")
        def list_designs(args):
            if not s.modules:
                return "No designs in the library."
            rows = [f"{'Module':<24}{'Ports':>6}{'Parameters':>12}{'Source':>28}"]
            for module in s.modules:
                source = next((p for p, names in s.sources.items() if module.name in names), "")
                rows.append(f"{module.name:<24}{len(module.ports):>6}"
                            f"{len(module.params):>12}{source:>28}")
            return "\n".join(rows)

        @self.register("set_library", "set_library [-min] <file.lib>",
                       "load a Liberty library; -min loads the fast corner used "
                       "for hold analysis")
        def set_library(args):
            flags, opts, positional = parse_options(args, options=("min",),
                                                    flags=("min",))
            if not positional:
                raise ShellError("set_library needs a .lib path")
            path = positional[-1].strip("{}")
            if "min" in opts or flags:
                s.library_min = read_library(path)
                self.write(f"Information: Loaded fast corner '{s.library_min.name}' "
                           f"({s.library_min.corner}, "
                           f"{len(s.library_min.cells)} cells) from {path}")
                return ""
            s.library = read_library(path)
            s.library_path = path
            self.write(f"Information: Loaded library '{s.library.name}' "
                       f"({len(s.library.cells)} cells) from {path}")
            return ""

        @self.register("create_clock", "create_clock -period <ns> [-name <clock>] [get_ports <port>]",
                       "define the clock waveform used for timing")
        def create_clock(args):
            flags, opts, pos = parse_options(args, options=("period", "name", "waveform"))
            if "period" not in opts:
                raise ShellError("create_clock requires -period")
            period = float(opts["period"])
            if period <= 0:
                raise ShellError("clock period must be positive")
            s.config.clock_period = period
            name = opts.get("name", s.config.clock_name or "clk")
            candidates = [p.strip("[]{}") for p in pos]
            candidates = [c for c in candidates
                          if c and c not in ("get_ports", "get_clocks", "get_pins",
                                             "all_inputs", "all_clocks", "get_nets")
                          and c.replace("_", "").replace(".", "").isalnum()]
            if candidates:
                name = candidates[0]
            elif s.design is not None and s.design.clocks:
                name = sorted(s.design.clocks)[0]
            s.config.clock_name = name
            self.write(f"Information: Clock '{name}' created with period {period} ns "
                       f"({1000.0 / period:.1f} MHz).")
            return ""

        @self.register("set_input_delay", "set_input_delay <ns> [get_ports ...]",
                       "set input arrival relative to the clock")
        def set_input_delay(args):
            _f, _o, pos = parse_options(args)
            if not pos:
                raise ShellError("set_input_delay needs a delay value in ns")
            s.config.input_delay = float(pos[0].strip("{}"))
            self.write(f"Information: Input delay = {s.config.input_delay} ns.")
            return ""

        @self.register("set_output_delay", "set_output_delay <ns> [get_ports ...]",
                       "set output required time relative to the clock")
        def set_output_delay(args):
            _f, _o, pos = parse_options(args)
            if not pos:
                raise ShellError("set_output_delay needs a delay value in ns")
            s.config.output_delay = float(pos[0].strip("{}"))
            self.write(f"Information: Output delay = {s.config.output_delay} ns.")
            return ""

        @self.register("set_clock_uncertainty", "set_clock_uncertainty <ns> [get_clocks ...]",
                       "set clock uncertainty (jitter + skew margin)")
        def set_clock_uncertainty(args):
            _f, _o, pos = parse_options(args)
            if not pos:
                raise ShellError("set_clock_uncertainty needs a value in ns")
            s.config.uncertainty = float(pos[0].strip("{}"))
            self.write(f"Information: Clock uncertainty = {s.config.uncertainty} ns.")
            return ""

        @self.register("set_max_fanout", "set_max_fanout <count> [current_design]",
                       "buffer fans out wider than <count> during mapping")
        def set_max_fanout(args):
            _f, _o, pos = parse_options(args)
            if not pos:
                raise ShellError("set_max_fanout needs a numeric limit")
            s.tech_options.max_fanout = int(pos[0].strip("{}"))
            self.write(f"Information: Max fanout = {s.tech_options.max_fanout}.")
            return ""

        @self.register("set_max_transition", "set_max_transition <ns> [current_design]",
                       "record the transition limit used by reports")
        def set_max_transition(args):
            _f, _o, pos = parse_options(args)
            if not pos:
                raise ShellError("set_max_transition needs a value in ns")
            self.write(f"Information: Max transition = {pos[0]} ns (reported by check_design).")
            return ""

        @self.register("compile", "compile [-no_autoungroup]",
                       "optimise and map the design to library cells",
                       aliases=("compile_ultra",))
        def compile_cmd(args):
            s.require_rtl()
            if s.design is None:
                if s.top is None:
                    raise ShellError("no current design; run elaborate <top> first")
                elaborate_cmd([s.top])
            self.write(f"Information: Beginning mapping to library '{s.library.name}' "
                       f"({s.library.corner}).")
            try:
                netlist, stats = synthesize(s.design, s.library, s.tech_options)
            except Exception as exc:
                raise ShellError(str(exc))
            s.netlist = netlist
            s.opt_stats = stats
            s.timing = None
            self.write(f"Information: Optimisation removed {stats.dead_removed} "
                       f"unconnected cell(s), folded {stats.folded} constant network(s), "
                       f"collapsed {stats.mux_collapsed} multiplexer(s).")
            libraries = {"max": s.library}
            if s.library_min is not None:
                libraries["min"] = s.library_min
            timing, power, s.corner_results = analyse_corners(
                netlist, libraries, s.config)
            s.timing, s.power = timing, power
            s.compiled = True
            area = power.cell_area
            self.write(f"Information: Mapped {len(netlist.cells)} cells, "
                       f"{len(netlist.nets)} nets, area {area:.2f} um^2.")
            self.write(f"Information: Timing WNS {timing.wns:.3f} ns, "
                       f"TNS {timing.tns:.3f} ns at {s.config.clock_period:.3f} ns period.")
            if timing.wns < 0:
                self.warn("design does not meet timing; see report_timing")
            return ""

        @self.register("report_timing", "report_timing [-max_paths <n>] [-delay max|min]",
                       "report the worst timing paths")
        def report_timing(args):
            flags, opts, pos = parse_options(args, options=("max_paths", "delay", "to", "from"))
            s.require_compiled()
            count = int(opts.get("max_paths", 3))
            if opts.get("delay") == "min":
                text = reports.report_timing(_min_result(s.timing), s.top or "design",
                                             count, s.config.clock_name)
                return text
            return reports.report_timing(s.timing, s.top or "design", count,
                                         s.config.clock_name)

        @self.register("report_area", "report_area", "report cell and net area")
        def report_area(args):
            s.require_compiled()
            return reports.report_area(s.netlist, s.library, s.top or "design")

        @self.register("set_ocv",
                       "set_ocv [-cell_late x] [-cell_early x] [-net_late x] "
                       "[-net_early x] [-off]",
                       "set on-chip-variation derates and re-run timing")
        def set_ocv(args):
            flags, opts, _pos = parse_options(
                args, flags=("off",),
                options=("cell_late", "cell_early", "net_late", "net_early"))
            if "off" in flags:
                for key in ("derate_cell_late", "derate_cell_early",
                            "derate_net_late", "derate_net_early"):
                    setattr(s.config, key, 1.0)
            for key, value in opts.items():
                setattr(s.config, "derate_" + key, float(value))
            if not s.netlist:
                raise ShellError("no netlist; run compile first")
            s.analyse()
            self.write(f"Information: OCV derates cell {s.config.derate_cell_late:.3f}"
                       f"/{s.config.derate_cell_early:.3f}, net "
                       f"{s.config.derate_net_late:.3f}/{s.config.derate_net_early:.3f}"
                       f" (late/early); WNS {s.timing.wns:.3f} ns.")
            return ""

        @self.register("report_constraints", "report_constraints [-max_items <n>]",
                       "max transition and max capacitance limit violations")
        def report_constraints(args):
            s.require_compiled()
            _flags, opts, _pos = parse_options(args, options=("max_items",))
            return reports.report_constraints(s.timing, s.top or "design",
                                              int(opts.get("max_items", 10)))

        @self.register("report_power", "report_power", "report dynamic and leakage power")
        def report_power(args):
            s.require_compiled()
            return reports.report_power(s.power, s.top or "design", s.config.clock_period)

        @self.register("report_qor", "report_qor", "report quality of results")
        def report_qor(args):
            s.require_compiled()
            return reports.report_qor(s.timing, s.power, s.top or "design",
                                      s.config.clock_period)

        @self.register("check_design", "check_design", "check structural consistency")
        def check_design(args):
            s.require_compiled()
            return reports.report_check_design(s.netlist)

        @self.register("verify", "verify [-vectors <n>] [-seed <n>]",
                       "compare the mapped netlist against the elaborated RTL "
                       "on random vectors (0/1/X gate simulation)")
        def verify_cmd(args):
            s.require_compiled()
            _f, opts, _p = parse_options(args, options=("vectors", "seed"))
            reset = sorted(s.design.async_resets)[0] if s.design.async_resets else None
            clock = s.config.clock_name
            if clock not in s.design.ports and s.design.clocks:
                clock = sorted(s.design.clocks)[0]
            report = equivalence_check(
                s.netlist, s.design, clock=clock, reset=reset,
                vectors=int(opts.get("vectors", 64)), seed=int(opts.get("seed", 1)))
            return report.summary()

        @self.register("report_cell", "report_cell [<cell>]",
                       "report library cell attributes")
        def report_cell(args):
            name = args[0] if args else sorted(s.library.cells)[0]
            cell = s.library.cell(name)
            lines = [f"Cell: {cell.name}",
                     f"  area            : {cell.area:.3f}",
                     f"  leakage         : {cell.leakage:.3f} nW",
                     f"  width x height  : {cell.width:.3f} x {cell.row_height:.3f} um"]
            for pin, info in cell.pins.items():
                lines.append(f"  pin {pin:<6} {info.direction:<7} cap={info.capacitance:.5f}")
            for arc in cell.arcs:
                lines.append(f"  arc {arc.from_pin}->{arc.to_pin:<4} {arc.timing_type}")
            return "\n".join(lines)

        @self.register("report_design", "report_design", "summary of the current design")
        def report_design(args):
            if s.design is None:
                return "No elaborated design."
            counts = s.design.cell_counts()
            rows = [f"Design: {s.design.top}", f"  ports    : {', '.join(s.design.ports)}",
                    f"  clocks   : {', '.join(sorted(s.design.clocks)) or 'none'}",
                    f"  resets   : {', '.join(sorted(s.design.async_resets)) or 'none'}",
                    "  word-level operations:"]
            for name, count in counts.items():
                rows.append(f"    {name:<10}{count:>6}")
            return "\n".join(rows)

        @self.register("write", "write -format verilog|json|sdc -output <path>",
                       "write the netlist, handoff database or constraints")
        def write(args):
            flags, opts, pos = parse_options(args, options=("format", "output", "hierarchy"))
            fmt = opts.get("format", "verilog")
            out = opts.get("output") or (pos[0] if pos else None)
            if out is None:
                raise ShellError("write needs -output <path>")
            s.require_compiled()
            if fmt in ("verilog", "v"):
                text = reports.report_netlist_verilog(s.netlist, s.library, s.top)
                _ensure_parent(out)
                with open(out, "w", encoding="utf-8") as handle:
                    handle.write(text)
                self.write(f"Information: Verilog netlist written to '{out}'.")
            elif fmt in ("json", "ddc"):
                _ensure_parent(out)
                dump_netlist(s.netlist, s.library, s.config, out)
                self.write(f"Information: Netlist database written to '{out}'.")
            elif fmt == "sdc":
                _ensure_parent(out)
                with open(out, "w", encoding="utf-8") as handle:
                    handle.write(self._sdc_text())
                self.write(f"Information: Constraints written to '{out}'.")
            else:
                raise ShellError(f"unsupported format {fmt!r}")
            return ""

        @self.register("write_sdc", "write_sdc <path>", "write constraints")
        def write_sdc(args):
            if not args:
                raise ShellError("write_sdc needs a path")
            _ensure_parent(args[0])
            with open(args[0], "w", encoding="utf-8") as handle:
                handle.write(self._sdc_text())
            self.write(f"Information: Constraints written to '{args[0]}'.")
            return ""

        @self.register("read_sdc", "read_sdc <path>", "apply constraints from a script")
        def read_sdc(args):
            if not args:
                raise ShellError("read_sdc needs a path")
            try:
                with open(args[0], "r", encoding="utf-8") as handle:
                    script = handle.read()
            except OSError as exc:
                raise ShellError(str(exc))
            for line in script.splitlines():
                if line.strip() and not line.strip().startswith(("#", "//")):
                    self.execute_line(line)
            return ""

        @self.register("source", "source <script.tcl>", "run a command script")
        def source_cmd(args):
            if not args:
                raise ShellError("source needs a script path")
            code = self.run_script_file(args[0])
            if code not in (0, None):
                raise ShellError(f"script {args[0]} failed")
            return ""

        @self.register("help", "help [command]", "list commands or explain one")
        def help_cmd(args):
            return self.help_text(args[0] if args else None)

        @self.register("history", "history", "show command history")
        def history_cmd(args):
            return "\n".join(f"{i:4d}  {line}" for i, line in enumerate(self.history, 1))

        @self.register("version", "version", "print tool version")
        def version_cmd(args):
            return f"{VERSION} (library {self.s.library.name}, {self.s.library.corner})"

    # -- output helpers ---------------------------------------------------
    def _sdc_text(self) -> str:
        c = self.s.config
        return "\n".join([
            "# constraints written by silicon-dc",
            f"create_clock -name {c.clock_name} -period {c.clock_period} "
            f"[get_ports {c.clock_name}]",
            f"set_clock_uncertainty {c.uncertainty} [get_clocks {c.clock_name}]",
            f"set_input_delay {c.input_delay} -clock {c.clock_name} [all_inputs]",
            f"set_output_delay {c.output_delay} -clock {c.clock_name} [all_outputs]",
            f"set_load 0.05 [all_outputs]",
            "",
        ])


def _min_result(timing: TimingResult) -> TimingResult:
    """Build a hold-report view from the min-delay paths."""
    clone = TimingResult()
    clone.paths = timing.hold_paths
    clone.wns = timing.hold_wns
    clone.endpoint_count = len(timing.hold_paths)
    return clone


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    session = DcSession()
    shell = DcShell(session)
    script: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-f", "-file", "--file"):
            i += 1
            if i >= len(argv):
                shell.error("option -f needs a script path")
                return 2
            script = argv[i]
        elif arg in ("-h", "--help"):
            print("usage: dc_shell [-f script.tcl] [-no_init] [-x 'command']")
            print("       with no arguments, starts an interactive session")
            return 0
        elif arg in ("-x", "-e"):
            i += 1
            if i < len(argv):
                shell.execute_line(argv[i])
        elif arg in ("-no_init", "-noinit"):
            pass
        elif arg.startswith("-"):
            shell.error(f"unknown option {arg}")
            return 2
        else:
            script = arg
        i += 1

    if script:
        return shell.run_script_file(script)
    if not sys.stdin.isatty():
        return shell.run_script(sys.stdin.read())
    return shell.interact()


if __name__ == "__main__":                      # pragma: no cover
    raise SystemExit(main())
