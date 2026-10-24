"""``silicon`` command line: one-shot synthesis, place-and-route, or either shell.

    silicon synth -t alu -r examples/rtl/alu.v --clock clk --period 4 -o build/
    silicon pnr   -i build/alu.json --util 0.62 -o build/
    silicon flow  -t alu -r examples/rtl/alu.v --period 4 -o build/
    silicon dc            # drop into dc_shell
    silicon icc2          # drop into icc2_shell
    silicon lib           # describe a library
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import reports
from .elaborate import elaborate
from .library import Library, bundled_corners, default_library, fast_library, read_library
from .netlist import GateNetlist
from .pnr import (
    ClockTree, PhysicalConfig, Placer, Router, build_floorplan, insert_fillers,
    route_capacitance,
)
from .serialize import dump as dump_netlist
from .serialize import load as load_netlist
from .sim import equivalence_check
from .sta import TimingConfig, TimingEngine, analyse, analyse_corners, estimate_power
from .synth import TechmapOptions, synthesize
from .verilog import parse_modules
from .lef import write_block_lef, write_cell_lef, write_tech_lef
from .parasitics import extract, read_spef_summary, write_spef
from .sdf import write_sdf
from .writers import (ascii_layout, check_gds, read_gds_summary, write_def,
                      write_gds, write_svg)


def _library(path: Optional[str]) -> Library:
    return read_library(path) if path else default_library()


def _mkdir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def cmd_lib(args) -> int:
    library = _library(args.lib)
    print(f"library        : {library.name}")
    print(f"corner         : {library.corner or 'unspecified'}")
    print(f"cells          : {len(library.cells)}")
    print(f"site / row     : {library.site_width} um / {library.row_height} um")
    print(f"total area     : {library.total_area():.2f} um^2")
    print("")
    print(f"{'cell':<12}{'area':>8}{'leak(nW)':>10}{'pins':>6}{'arcs':>6}")
    for name in sorted(library.cells):
        cell = library.cell(name)
        print(f"{name:<12}{cell.area:>8.2f}{cell.leakage:>10.3f}"
              f"{len(cell.pins):>6}{len(cell.arcs):>6}")
    return 0


def _synthesize(args, library: Library):
    modules = []
    for path in args.rtl:
        with open(path, "r", encoding="utf-8") as handle:
            modules.extend(parse_modules(handle.read(), path))
    design = elaborate(modules, args.top, where=args.top)
    config = TimingConfig(
        clock_period=args.period,
        clock_name=args.clock,
        input_delay=args.input_delay,
        output_delay=args.output_delay,
        uncertainty=args.uncertainty,
    )
    options = TechmapOptions(max_fanout=args.max_fanout)
    netlist, stats = synthesize(design, library, options)
    timing, power = analyse(netlist, library, config)
    return design, netlist, timing, power, stats, config


def cmd_synth(args) -> int:
    library = _library(args.lib)
    design, netlist, timing, power, stats, config = _synthesize(args, library)
    out = args.output
    _mkdir(out)
    base = os.path.join(out, netlist.top)
    dump_netlist(netlist, library, config, f"{base}.json")
    with open(f"{base}.v", "w", encoding="utf-8") as handle:
        handle.write(reports.report_netlist_verilog(netlist, library, netlist.top))
    with open(f"{base}.area.rpt", "w", encoding="utf-8") as handle:
        handle.write(reports.report_area(netlist, library, netlist.top))
    with open(f"{base}.timing.rpt", "w", encoding="utf-8") as handle:
        handle.write(reports.report_timing(timing, netlist.top, 10, config.clock_name))
    with open(f"{base}.qor.rpt", "w", encoding="utf-8") as handle:
        handle.write(reports.report_qor(timing, power, netlist.top, config.clock_period))
    print(f"[dc] design      : {netlist.top} "
          f"({len(design.signals)} signals, {stats.cells_before} operations)")
    print(f"[dc] cells       : {len(netlist.cells)}  nets: {len(netlist.nets)}")
    print(f"[dc] area        : {power.cell_area:.2f} um^2")
    print(f"[dc] WNS / TNS   : {timing.wns:.3f} ns / {timing.tns:.3f} ns "
          f"@ {config.clock_period} ns")
    print(f"[dc] power       : {power.total_mw:.4f} mW (dynamic {power.dynamic_mw:.4f})")
    print(f"[dc] outputs     : {base}.json, {base}.v, {base}.{{area,timing,qor}}.rpt")
    min_wns = getattr(args, "min_wns", None)
    if min_wns is not None and timing.wns < min_wns:
        return 3
    return 0


def cmd_verify(args) -> int:
    library = _library(args.lib)
    design, netlist, timing, power, stats, config = _synthesize(args, library)
    report = equivalence_check(netlist, design, clock=config.clock_name,
                               reset=args.reset or _first_reset(design),
                               vectors=args.vectors, seed=args.seed)
    print(f"[dc] design      : {netlist.top} ({len(netlist.cells)} cells)")
    print(report.summary() if hasattr(report, "summary") else report)
    return 0 if report.ok else 4


def _first_reset(design) -> Optional[str]:
    resets = sorted(design.async_resets)
    return resets[0] if resets else None


def cmd_pnr(args) -> int:
    library = _library(args.lib)
    netlist, libname, constraints, tool = load_netlist(args.input)
    config = TimingConfig()
    for key, value in constraints.items():
        if hasattr(config, key):
            setattr(config, key, value)
    if args.period:
        config.clock_period = args.period
    phys = PhysicalConfig(core_utilization=args.util, aspect_ratio=args.aspect,
                          routing_timing_driven=getattr(args, "td_route", True))
    plan = build_floorplan(netlist, library, phys)
    placement = Placer(netlist, library, plan, phys).place(
        timing=TimingEngine(netlist, library, config).run()
        if phys.placement_timing_driven else None)
    cts = ClockTree(netlist, library, phys, counter_start=len(netlist.cells),
                    plan=plan).build(config.clock_name)
    for buffer in cts.buffers:
        netlist.add_cell(buffer)
    # the router sees the pre-route timing view so nets on the critical paths
    # get the first choice of tracks (see Router.net_criticality)
    clock_delays = cts.sink_delays if phys.routing_timing_driven else None
    pre_route = TimingEngine(netlist, library, config)
    if clock_delays:
        pre_route.set_clock_arrivals(clock_delays)
    route = Router(netlist, library, plan, phys,
                   timing=pre_route.run() if phys.routing_timing_driven else None
                   ).route()
    fillers = insert_fillers(netlist, library, plan)
    net_caps = route_capacitance(route, phys)
    parasitics = extract(netlist, library, plan, route, phys.rules)
    # min/max analysis by default: setup at the slow corner, hold at the fast one
    libraries = {"max": library}
    if args.min_lib != "":                       # "" means --no-mcmm
        libraries["min"] = (read_library(args.min_lib) if args.min_lib
                            else fast_library())
    timing, power, corner_results = analyse_corners(netlist, libraries, config,
                                                    parasitics=parasitics)

    out = args.output
    _mkdir(out)
    base = os.path.join(out, netlist.top)
    write_def(f"{base}.def", netlist, library, plan, route, netlist.top,
              fillers=fillers)
    write_gds(f"{base}.gds", netlist, library, plan, route, netlist.top,
              fillers=fillers)
    # design-rule check the stream itself, not just the in-memory model
    stream_drc = check_gds(f"{base}.gds", phys.rules,
                           layers=list(phys.vertical_layers)
                           + list(phys.horizontal_layers))
    stream_total = sum(len(items) for items in stream_drc.values())
    write_svg(f"{base}.svg", netlist, library, plan, route, netlist.top,
              title=netlist.top, fillers=fillers,
              stats={"WNS": f"{timing.wns:.3f} ns", "DRC": str(route.drc),
                     "wirelength": f"{route.wirelength:.0f} um"})
    write_spef(f"{base}.spef", parasitics, netlist.top)
    # SDF for back-annotating a gate-level simulation of the routed design
    sdf_engine = TimingEngine(netlist, library, config, parasitics=parasitics)
    sdf_engine.set_clock_arrivals(dict(cts.sink_delays))
    sdf_engine.run(parasitics=parasitics)
    write_sdf(f"{base}.sdf", netlist, library, sdf_engine, tool="silicon-icc2")
    write_tech_lef(os.path.join(out, "tech.lef"), library)
    write_cell_lef(os.path.join(out, "cells.lef"), library)
    write_block_lef(f"{base}.lef", netlist, library, plan, netlist.top)
    qor_lines = [reports.report_qor(timing, power, netlist.top,
                                    config.clock_period)]
    qor_lines.append(f"{'Core area (um^2):':<32}"
                     f"{plan.core.width * plan.core.height:>20.2f}")
    qor_lines.append(f"{'Rows:':<32}{plan.row_count:>20}")
    qor_lines.append(f"{'HPWL (estimated):':<32}{placement.hpwl:>14.1f} um")
    qor_lines.append(f"{'Clock skew (ns):':<32}{cts.skew:>20.4f}")
    qor_lines.append(f"{'Routed wirelength:':<32}{route.wirelength:>14.1f} um")
    qor_lines.append(f"{'Vias:':<32}{route.vias_count:>20}")
    qor_lines.append(f"{'DRC violations:':<32}{route.drc:>20}")
    for name, count in sorted(route.drc_violations.items()):
        qor_lines.append(f"{'  ' + name + ':':<32}{count:>20}")
    qor_lines.append(f"{'Steiner points:':<32}{route.steiner_points:>20}")
    qor_lines.append(f"{'Doubled vias:':<32}{route.doubled_vias:>20}")
    qor_lines.append(f"{'Ripups:':<32}{route.ripups:>20}")
    qor_lines.append(f"{'Filler cells:':<32}{len(fillers):>20}")
    qor_lines.append(f"{'Merged runs:':<32}{route.merged_runs:>20}")
    qor_lines.append(f"{'Maze-routed nets:':<32}{route.maze_nets:>20}")
    qor_lines.append(f"{'Critical nets routed first:':<32}{route.critical_nets:>20}")
    qor_lines.append(f"{'Congestion peak:':<32}"
                     f"{placement.congestion_peak:>20.3f}")
    qor_lines.append(f"{'Timing-weighted nets:':<32}"
                     f"{placement.timing_weighted_nets:>20}")
    qor_lines.append(f"{'GDS stream DRC:':<32}{stream_total:>20}")
    stats = parasitics.summary()
    qor_lines.append(f"{'Extracted cap (pF):':<32}{stats['cap']:>20.4f}")
    qor_lines.append(f"{'Extracted res (ohm):':<32}{stats['res']:>20.1f}")
    qor_lines.append(f"{'Worst net delay (ps):':<32}"
                     f"{stats['worst_delay'] * 1000:>20.2f}")
    qor_lines.append(f"{'Worst path wire (ps):':<32}{timing.wire_delay * 1000:>20.2f}")
    qor_lines.append(f"{'OCV derates:':<32}"
                     f"{'applied' if timing.derated else 'off':>20}")
    for role in ("max", "min"):
        result = corner_results.get(role)
        if result is None:
            continue
        qor_lines.append(f"{'Corner ' + role + ' setup WNS:':<32}{result.wns:>20.3f}")
        qor_lines.append(f"{'Corner ' + role + ' hold WNS:':<32}"
                         f"{result.hold_wns:>20.3f}")
    with open(f"{base}.qor.rpt", "w", encoding="utf-8") as handle:
        handle.write("\n".join(qor_lines) + "\n")
    with open(f"{base}.timing.rpt", "w", encoding="utf-8") as handle:
        handle.write(reports.report_timing(timing, netlist.top, 5, config.clock_name))
    with open(f"{base}.layout.txt", "w", encoding="utf-8") as handle:
        handle.write(ascii_layout(netlist, plan))
    summary = read_gds_summary(f"{base}.gds")
    print(f"[icc2] block     : {netlist.top} ({len(netlist.cells)} cells)")
    print(f"[icc2] core/die  : {plan.core.width:.2f}x{plan.core.height:.2f} / "
          f"{plan.die.width:.2f}x{plan.die.height:.2f} um, {plan.row_count} rows")
    print(f"[icc2] placement : HPWL {placement.hpwl:.1f} um, "
          f"overlaps {placement.overlaps}, util {placement.utilisation:.3f}")
    print(f"[icc2] cts       : {cts.sinks} sinks, {len(cts.buffers)} buffers, "
          f"skew {cts.skew:.4f} ns")
    print(f"[icc2] route     : {route.wirelength:.1f} um, {route.vias_count} vias, "
          f"{route.drc} DRC, {route.overflow} overflow, "
          f"{route.steiner_points} Steiner points, {route.maze_nets} maze net(s)")
    if route.drc_violations:
        print("[icc2] drc rules : " + ", ".join(
            f"{name}={count}" for name, count in sorted(route.drc_violations.items())))
    print(f"[icc2] timing    : WNS {timing.wns:.3f} ns, hold {timing.hold_wns:.3f} ns "
          f"(extracted RC + OCV)")
    print(f"[icc2] parasitics: {int(stats['nets'])} nets, {stats['cap']:.3f} pF, "
          f"worst net delay {stats['worst_delay'] * 1000:.1f} ps, SPEF written")
    if len(corner_results) > 1:
        print(f"[icc2] corners   : max {corner_results['max'].corner} setup "
              f"{corner_results['max'].wns:.3f} ns; min "
              f"{corner_results['min'].corner} hold "
              f"{corner_results['min'].hold_wns:.3f} ns")
    print(f"[icc2] gdsii     : {summary['structures']} structures, "
          f"{summary['srefs']} placements, {summary['paths']} paths, "
          f"{stream_total} stream DRC")
    if stream_total:
        print("[icc2] gds rules : " + ", ".join(
            f"{name}={len(items)}" for name, items in sorted(stream_drc.items())))
    print(f"[icc2] outputs   : {base}.{{def,lef,gds,svg,spef,sdf,qor.rpt,timing.rpt}}")
    min_wns = getattr(args, "min_wns", None)
    if min_wns is not None and timing.wns < min_wns:
        return 3
    max_drc = getattr(args, "max_drc", None)
    if max_drc is not None and route.drc > max_drc:
        return 3
    return 0


def cmd_flow(args) -> int:
    args.output = args.output
    code = cmd_synth(args)
    if code not in (0, 3):
        return code
    args.input = os.path.join(args.output, f"{args.top}.json")
    return cmd_pnr(args)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return shell_main("dc")
    command, rest = argv[0], argv[1:]
    if command in ("dc", "dc_shell", "synth-shell"):
        return shell_main("dc", rest)
    if command in ("icc2", "icc2_shell", "pnr-shell"):
        return shell_main("icc2", rest)

    parser = argparse.ArgumentParser(
        prog="silicon",
        description="Open synthesis (dc) and place-and-route (icc2) engines.")
    sub = parser.add_subparsers(dest="command")

    def add_common(p, synth: bool, pnr: bool) -> None:
        p.add_argument("--lib", help="Liberty library (default: bundled 32 nm)")
        if synth:
            p.add_argument("-r", "--rtl", nargs="+", required=True,
                           help="Verilog source files")
            p.add_argument("-t", "--top", required=True, help="top module")
            p.add_argument("--clock", default="clk", help="clock port name")
            p.add_argument("--period", type=float, default=10.0,
                           help="clock period in ns")
            p.add_argument("--input-delay", type=float, default=1.0, dest="input_delay")
            p.add_argument("--output-delay", type=float, default=1.0, dest="output_delay")
            p.add_argument("--uncertainty", type=float, default=0.15)
            p.add_argument("--max-fanout", type=int, default=8, dest="max_fanout")
            p.add_argument("--min-wns", type=float, default=None, dest="min_wns")
        if pnr:
            p.add_argument("--min-lib", dest="min_lib",
                           help="fast-corner Liberty for hold analysis "
                                "(default: bundled fast corner)")
            p.add_argument("--no-mcmm", dest="min_lib", action="store_const",
                           const="", help="single-corner analysis (max corner only)")
            p.add_argument("--util", type=float, default=0.65,
                           help="core utilisation target")
            p.add_argument("--aspect", type=float, default=1.0, help="core aspect ratio")
            p.add_argument("--max-drc", type=int, default=None, dest="max_drc")
            p.add_argument("--no-td-route", dest="td_route", action="store_false",
                           help="route nets in fanout order instead of by slack")

    p_synth = sub.add_parser("synth", help="run the synthesis engine")
    add_common(p_synth, True, False)
    p_synth.add_argument("-o", "--output", default="build", help="output directory")
    p_synth.set_defaults(func=cmd_synth)

    p_pnr = sub.add_parser("pnr", help="run the place-and-route engine")
    add_common(p_pnr, False, True)
    p_pnr.add_argument("-i", "--input", required=True,
                       help="gate netlist JSON written by 'silicon synth'")
    p_pnr.add_argument("--period", type=float, default=None,
                       help="override the clock period in ns")
    p_pnr.add_argument("-o", "--output", default="build", help="output directory")
    p_pnr.set_defaults(func=cmd_pnr)

    p_verify = sub.add_parser("verify",
                              help="synthesise and equivalence-check against the RTL")
    add_common(p_verify, True, False)
    p_verify.add_argument("--reset", help="asynchronous reset port (default: detected)")
    p_verify.add_argument("--vectors", type=int, default=128)
    p_verify.add_argument("--seed", type=int, default=1)
    p_verify.set_defaults(func=cmd_verify)

    p_flow = sub.add_parser("flow", help="synthesis followed by place-and-route")
    add_common(p_flow, True, True)
    p_flow.add_argument("-o", "--output", default="build", help="output directory")
    p_flow.set_defaults(func=cmd_flow)

    p_lib = sub.add_parser("lib", help="describe a library")
    p_lib.add_argument("--lib", help="Liberty library (default: bundled 32 nm)")
    p_lib.set_defaults(func=cmd_lib)

    sub.add_parser("dc", help="start dc_shell")
    sub.add_parser("icc2", help="start icc2_shell")
    sub.add_parser("version", help="print version")

    args = parser.parse_args(argv)
    if args.command in ("dc", "icc2"):
        return shell_main(args.command)
    if args.command == "version":
        from . import VERSION
        print(VERSION)
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def shell_main(which: str, argv: Optional[List[str]] = None) -> int:
    from . import dc_shell, icc2_shell
    return (dc_shell.main(argv) if which == "dc" else icc2_shell.main(argv))


if __name__ == "__main__":                      # pragma: no cover
    raise SystemExit(main())
