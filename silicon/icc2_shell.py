"""``icc2_shell``: the physical-design shell (floorplan, place, CTS, route).

The session only needs a gate netlist in the JSON database format produced by
``dc_shell write -format json`` -- it does not import or call the synthesis
tool, so the two tools are genuinely independent.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from typing import Dict, List, Optional

from . import reports
from .lef import read_lef, write_block_lef, write_cell_lef, write_tech_lef
from .library import Library, default_library, fast_library, read_library
from .netlist import GateCell, GateNetlist
from .parasitics import Parasitics, extract, read_spef_summary, write_spef
from .pnr import (CTSResult, ClockTree, Floorplan, PhysicalConfig, PhysicalError,
                  PlacementResult, Placer, RouteResult, Router, build_floorplan,
                  insert_fillers, route_capacitance)
from .serialize import load as load_netlist
from .serialize import dump as dump_netlist
from .shell import Shell, ShellError, parse_options
from .sta import TimingConfig, TimingEngine, analyse_corners, estimate_power
from .writers import (ascii_layout, check_gds, read_gds_geometry,
                      read_gds_summary, write_def, write_gds, write_svg)

VERSION = "silicon-icc2 0.1"


class PnrSession:
    """State shared by the physical-design commands."""

    def __init__(self, library: Optional[Library] = None,
                 library_min: Optional[Library] = None) -> None:
        self.library = library or default_library()
        # fast corner, used for hold when min/max analysis is on
        self.library_min = library_min
        self.netlist: Optional[GateNetlist] = None
        self.source = ""
        self.constraints: Dict[str, float] = {}
        self.config = TimingConfig(clock_period=10.0, clock_name="clk")
        self.phys = PhysicalConfig()
        self.plan: Optional[Floorplan] = None
        self.placement: Optional[PlacementResult] = None
        self.cts: Optional[CTSResult] = None
        self.route: Optional[RouteResult] = None
        self.parasitics: Optional[Parasitics] = None
        self.fillers: List[GateCell] = []
        self.stages: List[str] = []
        self.corner_results: Dict[str, object] = {}
        self.timing = None
        self.power = None

    # -- guards ----------------------------------------------------------
    def require_netlist(self) -> None:
        if self.netlist is None:
            raise ShellError("no block loaded; run read_netlist <file.json>")

    def require_stage(self, stage: str) -> None:
        if stage not in self.stages:
            raise ShellError(f"stage '{stage}' has not run yet "
                             f"(completed: {', '.join(self.stages) or 'none'})")

    # -- timing ----------------------------------------------------------
    def analyse(self) -> None:
        caps = route_capacitance(self.route, self.phys) if self.route else None
        if self.library_min is not None:
            merged, power, per_corner = analyse_corners(
                self.netlist, {"max": self.library, "min": self.library_min},
                self.config, parasitics=self.parasitics)
            self.timing = merged
            self.power = power
            self.corner_results = per_corner
            return
        engine = TimingEngine(self.netlist, self.library, self.config,
                              parasitics=self.parasitics)
        arrivals: Dict[str, float] = {}
        if self.cts is not None:
            arrivals = {inst: delay for inst, delay in self.cts.sink_delays.items()}
        engine.set_clock_arrivals(arrivals)
        # after routing the extracted RC tree replaces the per-fanout estimate
        self.timing = engine.run(net_caps=caps, parasitics=self.parasitics)
        self.power = estimate_power(self.netlist, self.library, self.config,
                                    engine.load)


class Icc2Shell(Shell):
    prompt = "icc2_shell> "

    def __init__(self, session: Optional[PnrSession] = None, **kwargs) -> None:
        super().__init__(VERSION, **kwargs)
        reports.TOOL = VERSION
        self.s = session or PnrSession()
        self._register()

    # ------------------------------------------------------------------
    def _register(self) -> None:
        s = self.s

        @self.register("read_netlist", "read_netlist <file.json>",
                       "load a gate netlist database", aliases=("read_verilog", "link"))
        def read_netlist(args):
            if not args:
                raise ShellError("read_netlist needs a path")
            path = args[0]
            if not os.path.exists(path):
                raise ShellError(f"file not found: {path}")
            if path.endswith(".v"):
                raise ShellError(
                    "icc2 reads a gate netlist database, not RTL: use the JSON "
                    "file written by 'dc_shell write -format json'")
            netlist, lib_name, constraints, tool = load_netlist(path)
            s.netlist = netlist
            s.source = path
            s.constraints = dict(constraints)
            for key, value in constraints.items():
                if hasattr(s.config, key):
                    setattr(s.config, key, value)
            missing = sorted({c.cell for c in netlist.cells
                              if not s.library.has(c.cell)})
            if missing:
                raise ShellError("cells missing from the loaded library: "
                                 + ", ".join(missing[:8]))
            for cell in netlist.cells:
                model = s.library.cell(cell.cell)
                cell.width = model.width
                cell.height = model.row_height
            self.write(f"Information: Loaded block '{netlist.top}' from {path} "
                       f"({len(netlist.cells)} cells, written by {tool or 'unknown'}).")
            self.write(f"Information: Constraint inheritance: clock "
                       f"'{s.config.clock_name}' period {s.config.clock_period} ns, "
                       f"input delay {s.config.input_delay} ns, output delay "
                       f"{s.config.output_delay} ns.")
            return ""

        @self.register("set_library",
                       "set_library [-min] <file.lib>",
                       "load a Liberty library (default: the built-in slow corner); "
                       "-min sets the fast corner for hold analysis")
        def set_library(args):
            _flags, opts, positional = parse_options(
                args, options=("min",), flags=("min",))
            if not positional:
                raise ShellError("set_library needs a .lib path")
            if "min" in opts or _flags:
                s.library_min = read_library(positional[-1])
                self.write(f"Information: Fast corner '{s.library_min.name}' "
                           f"({len(s.library_min.cells)} cells) loaded for hold "
                           f"analysis.")
                return ""
            s.library = read_library(positional[-1])
            for cell in s.netlist.cells if s.netlist else ():
                model = s.library.cell(cell.cell)
                cell.width = model.width
                cell.height = model.row_height
            self.write(f"Information: Library '{s.library.name}' loaded "
                       f"({len(s.library.cells)} cells).")
            return ""

        @self.register("create_clock", "create_clock -period <ns> [-name <clock>]",
                       "override the clock used for physical timing")
        def create_clock(args):
            _flags, opts, _pos = parse_options(args, options=("period", "name"))
            if "period" in opts:
                s.config.clock_period = float(opts["period"])
            if "name" in opts:
                s.config.clock_name = opts["name"]
            self.write(f"Information: Clock '{s.config.clock_name}' period "
                       f"{s.config.clock_period} ns.")
            return ""

        @self.register("initialize_floorplan",
                       "initialize_floorplan [-core_utilization <u>] [-aspect_ratio <r>] "
                       "[-core_offset {x y}] [-track_pitch <um>]",
                       "size the core, build rows and place IO pins")
        def initialize_floorplan(args):
            s.require_netlist()
            _flags, opts, _pos = parse_options(
                args, options=("core_utilization", "aspect_ratio", "core_offset",
                               "track_pitch", "row_height", "site_pitch"))
            if "core_utilization" in opts:
                s.phys.core_utilization = float(opts["core_utilization"])
            if "aspect_ratio" in opts:
                s.phys.aspect_ratio = float(opts["aspect_ratio"])
            if "core_offset" in opts:
                parts = opts["core_offset"].strip("{}").replace(",", " ").split()
                if len(parts) != 2:
                    raise ShellError("core_offset needs two numbers: {x y}")
                s.phys.core_offset = (float(parts[0]), float(parts[1]))
            if "track_pitch" in opts:
                s.phys.track_pitch = float(opts["track_pitch"])
            if "row_height" in opts:
                s.phys.row_height = float(opts["row_height"])
            if "site_pitch" in opts:
                s.phys.site_pitch = float(opts["site_pitch"])
            s.plan = build_floorplan(s.netlist, s.library, s.phys)
            s.stages = ["floorplan"]
            core = s.plan.core
            self.write(f"Information: Core {core.width:.3f} x {core.height:.3f} um, "
                       f"die {s.plan.die.width:.3f} x {s.plan.die.height:.3f} um, "
                       f"{s.plan.row_count} rows, {s.plan.total_sites} sites.")
            self.write(f"Information: Utilisation target {s.phys.core_utilization:.2f}, "
                       f"{len(s.plan.io_pins)} IO pins placed on the core boundary.")
            return ""

        @self.register("place_opt", "place_opt",
                       "quadratic placement, row legalisation and HPWL optimisation")
        def place_opt(args):
            s.require_netlist()
            if s.plan is None:
                initialize_floorplan([])
            # timing-driven placement needs a timing view of the floorplan, so
            # run ideal-clock STA on the unplaced netlist first and weight the
            # nets on the worst paths.
            s.analyse()
            s.placement = Placer(s.netlist, s.library, s.plan, s.phys).place(
                timing=s.timing if s.phys.placement_timing_driven else None)
            s.stages = [stage for stage in s.stages if stage == "floorplan"]
            s.stages.append("place")
            result = s.placement
            self.write(f"Information: Placed {len(result.cells)} cells, "
                       f"HPWL {result.hpwl:.1f} um, utilisation {result.utilisation:.3f}, "
                       f"{result.overlaps} overlap(s), {result.iterations} iterations.")
            self.write(f"Information: Timing-driven: {result.timing_weighted_nets} "
                       f"critical net(s) weighted, weighted HPWL "
                       f"{result.weighted_hpwl:.1f} um.")
            self.write(f"Information: Congestion: peak bin utilisation "
                       f"{result.congestion_peak:.3f}, mean "
                       f"{result.congestion_mean:.3f} "
                       f"({s.phys.congestion_bins}x{s.phys.congestion_bins} bins).")
            if result.congestion_peak > 1.0:
                self.warn(f"congested bin at {result.congestion_peak:.2f}x capacity "
                          f"(see report_congestion)")
            if result.overlaps:
                self.error(f"{result.overlaps} placement overlap(s) remain")
            s.cts = None
            s.route = None
            s.parasitics = None
            s.analyse()
            self.write(f"Information: Pre-CTS timing (ideal clock): WNS "
                       f"{s.timing.wns:.3f} ns, TNS {s.timing.tns:.3f} ns.")
            return ""

        @self.register("clock_opt", "clock_opt [-max_sinks <n>] [-buffer <cell>]",
                       "build the buffered clock tree and propagate clocks")
        def clock_opt(args):
            s.require_stage("place")
            _flags, opts, _pos = parse_options(args, options=("max_sinks", "buffer"))
            if "max_sinks" in opts:
                s.phys.max_sinks_per_buffer = int(opts["max_sinks"])
            if "buffer" in opts:
                s.phys.clock_buffer = opts["buffer"]
            tree = ClockTree(s.netlist, s.library, s.phys,
                             counter_start=len(s.netlist.cells), plan=s.plan)
            s.cts = tree.build(s.config.clock_name)
            s.stages.append("cts")
            result = s.cts
            if result.sinks == 0:
                self.warn("no clock sinks found (the netlist has no flops)")
            self.write(f"Information: Clock tree: {result.sinks} sinks, "
                       f"{result.levels} buffer level(s), {len(result.buffers)} buffer(s), "
                       f"insertion delay {result.insertion_delay:.4f} ns, "
                       f"skew {result.skew:.4f} ns, "
                       f"wirelength {result.clock_wirelength:.1f} um.")
            s.analyse()
            self.write(f"Information: Post-CTS timing (propagated clock): WNS "
                       f"{s.timing.wns:.3f} ns, skew {s.timing.clock_skew:.4f} ns.")
            return ""

        @self.register("route_auto", "route_auto", "track-based detailed routing")
        def route_auto(args):
            s.require_netlist()
            s.require_stage("place")
            s.route = Router(s.netlist, s.library, s.plan, s.phys).route()
            s.fillers = insert_fillers(s.netlist, s.library, s.plan)
            s.parasitics = extract(s.netlist, s.library, s.plan, s.route, s.phys.rules)
            s.stages.append("route")
            result = s.route
            for layer in sorted(result.layer_usage):
                self.write(f"Information: {layer}: {result.layer_usage[layer]:.1f} um")
            self.write(f"Information: Routing: wirelength {result.wirelength:.1f} um, "
                       f"{result.vias_count} vias ({result.doubled_vias} doubled), "
                       f"{result.steiner_points} Steiner points, "
                       f"{result.ripups} ripups, {result.merged_runs} merged runs, "
                       f"{result.drc} DRC, "
                       f"{result.overflow} track overflow, "
                       f"{len(result.unrouted_nets)} unrouted net(s).")
            self.write(f"Information: {len(s.fillers)} filler cells inserted after routing.")
            s.analyse()
            self.write(f"Information: Post-route timing (extracted RC): WNS "
                       f"{s.timing.wns:.3f} ns, TNS {s.timing.tns:.3f} ns.")
            if result.drc:
                by_rule = ", ".join(f"{k}={v}" for k, v in
                                    sorted(result.drc_violations.items()))
                self.error(f"{result.drc} DRC violation(s): {by_rule} "
                           f"(see report_drc)")
            return ""

        @self.register("report_parasitics", "report_parasitics [-max_nets <n>]",
                       "extracted RC per net (worst offenders first)")
        def report_parasitics(args):
            _flags, opts, _pos = parse_options(args, options=("max_nets",))
            s.require_stage("route")
            if s.parasitics is None:
                s.parasitics = extract(s.netlist, s.library, s.plan, s.route,
                                       s.phys.rules)
            count = int(opts.get("max_nets", 10))
            stats = s.parasitics.summary()
            lines = [f"{'Nets with RC':<24}{int(stats['nets']):>10}",
                     f"{'Total capacitance (pF)':<24}{stats['cap']:>10.4f}",
                     f"{'Total resistance (ohm)':<24}{stats['res']:>10.2f}",
                     f"{'Worst net delay (ps)':<24}{stats['worst_delay'] * 1000:>10.2f}",
                     "",
                     f"{'Net':<20}{'Cap (fF)':>10}{'Res (ohm)':>11}"
                     f"{'Sinks':>7}{'Worst delay (ps)':>18}"]
            worst = sorted(s.parasitics.nets.values(),
                           key=lambda entry: -max(entry.sink_delays.values(),
                                                  default=0.0))[:count]
            for entry in worst:
                lines.append(f"{entry.net:<20}{entry.total_capacitance * 1000:>10.2f}"
                             f"{entry.total_resistance:>11.1f}"
                             f"{len(entry.sink_delays):>7}"
                             f"{max(entry.sink_delays.values(), default=0.0) * 1000:>18.2f}")
            return "\n".join(lines)

        @self.register("write_spef", "write_spef <file.spef>",
                       "write SPEF 1.0 for the extracted RC trees")
        def write_spef_cmd(args):
            if not args:
                raise ShellError("write_spef needs a path")
            s.require_stage("route")
            _ensure_parent(args[0])
            if s.parasitics is None:
                s.parasitics = extract(s.netlist, s.library, s.plan, s.route,
                                       s.phys.rules)
            write_spef(args[0], s.parasitics, s.netlist.top)
            summary = read_spef_summary(args[0])
            self.write(f"Information: SPEF written to '{args[0]}' "
                       f"({int(summary['nets'])} nets, "
                       f"{summary['cap']:.4f} pF, {summary['res']:.1f} ohm).")
            return ""

        @self.register("report_corners", "report_corners",
                       "per-corner setup and hold summary")
        def report_corners(args):
            s.require_netlist()
            if len(s.corner_results) < 2:
                raise ShellError("min/max analysis is off; load a fast corner with "
                                 "'set_library -min <file.lib>'")
            lines = [f"{'Corner':<22}{'Library':<20}{'Setup WNS':>11}"
                     f"{'Hold WNS':>11}{'TNS':>9}"]
            lines.append("-" * 73)
            for role in ("max", "min"):
                result = s.corner_results.get(role)
                if result is None:
                    continue
                lines.append(f"{role:<22}{result.corner:<20}{result.wns:>11.3f}"
                             f"{result.hold_wns:>11.3f}{result.tns:>9.3f}")
            lines.append("")
            lines.append("Reported setup slack comes from the max corner, hold slack")
            lines.append("from the min corner.")
            return "\n".join(lines)

        @self.register("report_qor", "report_qor", "summary of quality of results")
        def report_qor(args):
            s.require_netlist()
            if s.timing is None:
                s.analyse()
            text = reports.report_qor(s.timing, s.power, s.netlist.top,
                                      s.config.clock_period)
            extra = [f"{'Stages completed:':<32}{', '.join(s.stages):>20}"]
            if s.plan:
                extra.append(f"{'Core area (um^2):':<32}"
                             f"{s.plan.core.width * s.plan.core.height:>20.2f}")
            if s.placement:
                extra.append(f"{'HPWL (estimated):':<32}{s.placement.hpwl:>14.1f} um")
            if s.cts:
                extra.append(f"{'Clock skew (ns):':<32}{s.cts.skew:>20.4f}")
            if s.parasitics is not None:
                stats = s.parasitics.summary()
                extra.append(f"{'Extracted cap (pF):':<32}{stats['cap']:>20.4f}")
                extra.append(f"{'Worst net delay (ps):':<32}"
                             f"{stats['worst_delay'] * 1000:>20.2f}")
            if s.route:
                extra.append(f"{'Routed wirelength:':<32}{s.route.wirelength:>14.1f} um")
                extra.append(f"{'Vias:':<32}{s.route.vias_count:>20}")
                extra.append(f"{'DRC violations:':<32}{s.route.drc:>20}")
                extra.append(f"{'Steiner points:':<32}{s.route.steiner_points:>20}")
                extra.append(f"{'Doubled vias:':<32}{s.route.doubled_vias:>20}")
            return text + "\n" + "\n".join(extra) + "\n"

        @self.register("report_timing", "report_timing [-max_paths <n>] [-delay max|min]",
                       "report the worst timing paths after the last stage")
        def report_timing(args):
            s.require_netlist()
            _flags, opts, _pos = parse_options(args, options=("max_paths", "delay"))
            if s.timing is None:
                s.analyse()
            count = int(opts.get("max_paths", 5))
            timing = s.timing
            if opts.get("delay") == "min":
                # report the hold view: swap the path list the reporter sees
                timing = replace(timing, paths=timing.hold_paths)
            return reports.report_timing(timing, s.netlist.top, count,
                                         s.config.clock_name)

        @self.register("report_area", "report_area", "area report")
        def report_area(args):
            s.require_netlist()
            text = reports.report_area(s.netlist, s.library, s.netlist.top)
            if s.plan:
                core = s.plan.core
                text += (f"\nCore area: {core.width * core.height:.2f} um^2   "
                         f"Die area: {s.plan.die.width * s.plan.die.height:.2f} um^2")
            return text

        @self.register("report_power", "report_power", "power report")
        def report_power(args):
            s.require_netlist()
            if s.power is None:
                s.analyse()
            return reports.report_power(s.power, s.netlist.top, s.config.clock_period)

        @self.register("report_drc", "report_drc", "design-rule check summary")
        def report_drc(args):
            s.require_stage("route")
            result = s.route
            rules = s.phys.rules
            lines = [f"{'Rule':<28}{'Checked':>10}{'Violations':>12}"]
            lines.append("-" * 50)
            checks = {
                "SHORT": f"{len(result.segments)} segments",
                "SPACING": f"{len(rules)} layers",
                "WIDTH": f"{len(result.segments)} segments",
                "MIN_AREA": f"{result.widened_segments} widened",
                "VIA_ENCLOSURE": f"{result.vias_count} vias",
                "VIA_SIZE": f"{result.vias_count} vias",
                "VIA_SPACING": f"{result.vias_count} vias",
            }
            for rule in ("SHORT", "SPACING", "WIDTH", "MIN_AREA",
                         "VIA_ENCLOSURE", "VIA_SIZE", "VIA_SPACING"):
                count = result.drc_violations.get(rule, 0)
                lines.append(f"{rule:<28}{checks[rule]:>10}{count:>12}")
            lines.append("-" * 50)
            lines.append(f"{'Total DRC violations':<28}{'':>10}{result.drc:>12}")
            lines.append(f"{'Track overflow':<28}{'':>10}{result.overflow:>12}")
            lines.append(f"{'Unrouted nets':<28}{'':>10}"
                         f"{len(result.unrouted_nets):>12}")
            lines.append("")
            lines.append(f"Rule deck: min width {min(r.width for r in rules.values()):.3f} um, "
                         f"min spacing {min(r.spacing for r in rules.values()):.3f} um, "
                         f"min area {min(r.min_area for r in rules.values()):.3f} um^2")
            if result.drc_details:
                lines.append("")
                lines.append("Violations:")
                for rule, items in result.drc_details.items():
                    for item in items[:5]:
                        lines.append(f"  {rule}: {item}")
            if result.unrouted_nets:
                lines.append("Unrouted: " + ", ".join(result.unrouted_nets[:10]))
            return "\n".join(lines)

        @self.register("check_gds", "check_gds <file.gds>",
                       "design-rule check the written GDSII stream",
                       aliases=("report_gds_drc", "verify_gds"))
        def check_gds_cmd(args):
            if not args:
                raise ShellError("check_gds needs a GDSII path")
            if not os.path.exists(args[0]):
                raise ShellError(f"file not found: {args[0]}")
            found = check_gds(args[0], s.phys.rules,
                             layers=list(s.phys.vertical_layers)
                             + list(s.phys.horizontal_layers))
            total = sum(len(items) for items in found.values())
            shapes = read_gds_geometry(args[0])
            metal = [shape for shape in shapes
                     if shape.layer in set(s.phys.vertical_layers)
                     | set(s.phys.horizontal_layers)]
            lines = [f"GDSII stream   : {args[0]}",
                     f"Flattened metal: {len(metal)} shapes "
                     f"({len(shapes)} elements incl. cells and cuts)",
                     f"Layers checked : "
                     f"{', '.join(sorted(set(s.phys.vertical_layers)
                                        | set(s.phys.horizontal_layers)))}",
                     ""]
            for rule in ("SHORT", "SPACING", "WIDTH", "MIN_AREA",
                         "VIA_SIZE", "VIA_SPACING"):
                lines.append(f"{rule:<16}{len(found.get(rule, [])):>10}")
            lines.append(f"{'Total':<16}{total:>10}")
            for rule, items in found.items():
                lines.append("")
                lines.append(f"{rule}:")
                for item in items[:5]:
                    lines.append(f"  {item}")
            if total:
                self.error(f"{total} violation(s) in the GDSII stream "
                           f"(see check_gds)")
            else:
                self.write("Information: GDSII stream is clean on the routing layers.")
            return "\n".join(lines)

        @self.register("report_congestion", "report_congestion",
                       "routing utilisation per direction and per congestion bin")
        def report_congestion(args):
            s.require_stage("route")
            result, core = s.route, s.plan.core
            tracks_h = core.height / s.phys.track_pitch
            tracks_v = core.width / s.phys.track_pitch
            avail_h = tracks_h * core.width * len(s.phys.horizontal_layers)
            avail_v = tracks_v * core.height * len(s.phys.vertical_layers)
            used_h = sum(result.layer_usage.get(layer, 0.0)
                         for layer in s.phys.horizontal_layers)
            used_v = sum(result.layer_usage.get(layer, 0.0)
                         for layer in s.phys.vertical_layers)
            lines = [f"{'Direction':<12}{'Available (um)':>18}{'Used (um)':>14}{'Util':>9}",
                     "-" * 53,
                     f"{'horizontal':<12}{avail_h:>18.1f}{used_h:>14.1f}"
                     f"{used_h / avail_h:>9.3f}",
                     f"{'vertical':<12}{avail_v:>18.1f}{used_v:>14.1f}"
                     f"{used_v / avail_v:>9.3f}",
                     "",
                     f"Average wirelength per net: "
                     f"{result.wirelength / max(1, len(result.net_wirelength)):.2f} um",
                     "",
                     f"Congestion map ({s.phys.congestion_bins} x "
                     f"{s.phys.congestion_bins} bins, demand/supply; "
                     f". = <0.25, o = <0.5, O = <0.75, # = >=0.75):"]
            placer = Placer(s.netlist, s.library, s.plan, s.phys)
            grid = placer.congestion_map()
            for row in reversed(range(len(grid[0]))):          # y grows upwards
                cells = []
                for column in range(len(grid)):
                    value = grid[column][row]
                    cells.append("." if value < 0.25 else
                                 "o" if value < 0.5 else
                                 "O" if value < 0.75 else "#")
                lines.append("  " + "".join(cells))
            lines.append(f"Peak: {max(max(c) for c in grid):.3f}")
            return "\n".join(lines)

        @self.register("report_placement", "report_placement",
                       "placement statistics")
        def report_placement(args):
            s.require_stage("place")
            result = s.placement
            lines = [f"Placed cells       : {len(result.cells)}",
                     f"HPWL               : {result.hpwl:.1f} um",
                     f"Weighted HPWL      : {result.weighted_hpwl:.1f} um "
                     f"({result.timing_weighted_nets} critical nets)",
                     f"Utilisation        : {result.utilisation:.3f}",
                     f"Congestion peak    : {result.congestion_peak:.3f} "
                     f"(mean {result.congestion_mean:.3f})",
                     f"Legalised          : {result.legalized}",
                     f"Overlaps           : {result.overlaps}",
                     "", ascii_layout(s.netlist, s.plan)]
            return "\n".join(lines)

        @self.register("report_clock_tree", "report_clock_tree",
                       "clock-tree structure and per-sink insertion delay")
        def report_clock_tree(args):
            s.require_stage("cts")
            result = s.cts
            lines = [f"Clock              : {s.config.clock_name}",
                     f"Sinks              : {result.sinks}",
                     f"Buffer levels      : {result.levels}",
                     f"Buffers            : {len(result.buffers)}",
                     f"Insertion delay    : {result.insertion_delay:.4f} ns (average)",
                     f"Skew               : {result.skew:.4f} ns",
                     f"Clock wirelength   : {result.clock_wirelength:.2f} um",
                     "", f"{'Sink':<24}{'Insertion delay (ns)':>22}"]
            for name, delay in sorted(result.sink_delays.items())[:40]:
                lines.append(f"{name:<24}{delay:>22.4f}")
            return "\n".join(lines)

        @self.register("write_def", "write_def <file.def>", "write DEF")
        def write_def_cmd(args):
            if not args:
                raise ShellError("write_def needs a path")
            s.require_stage("place")
            _ensure_parent(args[0])
            write_def(args[0], s.netlist, s.library, s.plan,
                      s.route if "route" in s.stages else None, s.netlist.top,
                      fillers=s.fillers)
            self.write(f"Information: DEF written to '{args[0]}'.")
            return ""

        @self.register("write_gds", "write_gds <file.gds>",
                       "write GDSII (cells, routes and fillers)")
        def write_gds_cmd(args):
            if not args:
                raise ShellError("write_gds needs a path")
            s.require_stage("route")
            _ensure_parent(args[0])
            write_gds(args[0], s.netlist, s.library, s.plan, s.route,
                      s.netlist.top, fillers=s.fillers)
            summary = read_gds_summary(args[0])
            self.write(f"Information: GDSII written to '{args[0]}' "
                       f"({summary['structures']} structures, {summary['srefs']} "
                       f"cell placements, {summary['paths']} route paths, "
                       f"{summary['texts']} net labels).")
            return ""

        @self.register("write_svg", "write_svg <file.svg> [-width <px>]",
                       "write an SVG view of the implemented layout",
                       aliases=("write_layout",))
        def write_svg_cmd(args):
            if not args:
                raise ShellError("write_svg needs a path")
            s.require_stage("place")
            _ensure_parent(args[0])
            stats: Dict[str, str] = {}
            if s.timing is not None:
                stats["WNS"] = f"{s.timing.wns:.3f} ns"
            if s.placement is not None:
                stats["HPWL"] = f"{s.placement.hpwl:.0f} um"
            if s.route is not None:
                stats["wirelength"] = f"{s.route.wirelength:.0f} um"
                stats["DRC"] = str(s.route.drc)
            write_svg(args[0], s.netlist, s.library, s.plan,
                      s.route if "route" in s.stages else None, s.netlist.top,
                      title=s.netlist.top, stats=stats, fillers=s.fillers)
            self.write(f"Information: layout view written to '{args[0]}'.")
            return ""

        @self.register("write_lef", "write_lef <file.lef>",
                       "write a block LEF macro with the block's pins")
        def write_lef_cmd(args):
            if not args:
                raise ShellError("write_lef needs a path")
            s.require_stage("floorplan")
            _ensure_parent(args[0])
            write_block_lef(args[0], s.netlist, s.library, s.plan, s.netlist.top)
            macros = read_lef(args[0])
            macro = macros.get(s.netlist.top)
            self.write(f"Information: Block LEF written to '{args[0]}' "
                       f"({macro.width:.2f} x {macro.height:.2f} um, "
                       f"{len(macro.pins)} pins)." if macro else
                       f"Information: LEF written to '{args[0]}'.")
            return ""

        @self.register("write_library_lef", "write_library_lef <file.lef>",
                       "write the cell library as a LEF macro file",
                       aliases=("write_cell_lef",))
        def write_library_lef_cmd(args):
            if not args:
                raise ShellError("write_library_lef needs a path")
            _ensure_parent(args[0])
            write_cell_lef(args[0], s.library)
            macros = read_lef(args[0])
            self.write(f"Information: Library LEF written to '{args[0]}' "
                       f"({len(macros)} macros).")
            return ""

        @self.register("write_tech_lef", "write_tech_lef <file.lef>",
                       "write the technology LEF (layers, vias, site)")
        def write_tech_lef_cmd(args):
            if not args:
                raise ShellError("write_tech_lef needs a path")
            _ensure_parent(args[0])
            write_tech_lef(args[0], s.library, s.netlist.top if s.netlist else "")
            self.write(f"Information: Technology LEF written to '{args[0]}'.")
            return ""

        @self.register("write_netlist", "write_netlist <file.json>",
                       "write the current netlist database (post-CTS net names)")
        def write_netlist_cmd(args):
            if not args:
                raise ShellError("write_netlist needs a path")
            s.require_netlist()
            _ensure_parent(args[0])
            if args[0].endswith(".json"):
                dump_netlist(s.netlist, s.library, s.config, args[0], VERSION)
            elif args[0].endswith(".v"):
                with open(args[0], "w", encoding="utf-8") as handle:
                    handle.write(reports.report_netlist_verilog(
                        s.netlist, s.library, s.netlist.top))
            else:
                raise ShellError("write_netlist writes .json or .v")
            self.write(f"Information: netlist written to '{args[0]}'.")
            return ""

        @self.register("report_layout", "report_layout",
                       "print an ASCII map of the core")
        def report_layout(args):
            s.require_stage("place")
            return ascii_layout(s.netlist, s.plan)

        @self.register("help", "help [command]", "list commands or explain one")
        def help_cmd(args):
            return self.help_text(args[0] if args else None)

        @self.register("history", "history", "command history")
        def history_cmd(args):
            return "\n".join(f"{i:4d}  {line}"
                             for i, line in enumerate(self.history, 1))

        @self.register("version", "version", "tool version")
        def version_cmd(args):
            return (f"{VERSION} (library {s.library.name}, "
                    f"{len(s.library.cells)} cells)")


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    shell = Icc2Shell(PnrSession())
    script: Optional[str] = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-f", "-file", "--file"):
            index += 1
            if index >= len(argv):
                shell.error("option -f needs a script path")
                return 2
            script = argv[index]
        elif arg in ("-h", "--help"):
            print("usage: icc2_shell [-f script.tcl] [-x 'command']")
            return 0
        elif arg in ("-x", "-e"):
            index += 1
            if index < len(argv):
                shell.execute_line(argv[index])
        elif arg.startswith("-"):
            shell.error(f"unknown option {arg}")
            return 2
        else:
            script = arg
        index += 1
    if script:
        return shell.run_script_file(script)
    if not sys.stdin.isatty():
        return shell.run_script(sys.stdin.read())
    return shell.interact()


if __name__ == "__main__":                      # pragma: no cover
    raise SystemExit(main())
