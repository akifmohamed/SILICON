"""Synthesis: optimisation and technology mapping.

Two stages, mirroring what a real synthesis tool reports as ``compile``:

``optimize``
    Word-level optimisation on the elaborated design: constant folding,
    multiplexer collapse, wire-alias resolution and removal of unconnected
    logic. Anything left over is real logic.

``techmap``
    Structural mapping of each macro cell (``ADD``, ``LT``, ``MUX2``, ``DFFR``)
    onto library cells. Adders become ripple-carry full-adder chains,
    comparators become magnitude chains, multiplexers become ``MUX2X1`` per bit,
    and flops become ``DFF*`` cells from the library. This is the same
    decomposition DesignWare performs, written out explicitly so the timing
    engine sees every gate.

The mapping is deliberately *structural* rather than clever: there is no
retiming, no boolean restructuring, no datapath sharing. What it produces is
however a real, simulatable, place-and-routable netlist whose area and delay are
computed from library data rather than estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .library import Library
from .netlist import (
    CONST0, CONST1, GateCell, GateNetlist, MacroCell, Signal, UnionFind,
    WordDesign,
)


class SynthesisError(Exception):
    pass


# ---------------------------------------------------------------------------
# Net resolution
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCell:
    cell: MacroCell
    inputs: Dict[str, List[str]]        # port -> canonical nets
    outputs: Dict[str, List[str]]


def resolve_nets(design: WordDesign) -> Tuple[List[ResolvedCell], UnionFind]:
    """Collapse wire aliases (slices, concatenations, port connections)."""
    uf = UnionFind()
    for sig in design.signals.values():
        for bit in sig.bits:
            uf.add(bit)
    uf.add(CONST0)
    uf.add(CONST1)
    for a, b in design.aliases:
        uf.union(a, b)

    drivers: Dict[str, str] = {}
    resolved: List[ResolvedCell] = []
    for cell in design.cells:
        ins: Dict[str, List[str]] = {}
        for port, bits in cell.inputs.items():
            if port in ("RST_VAL", "RST"):
                ins[port] = bits
                continue
            ins[port] = [uf.find(b) for b in bits]
        outs: Dict[str, List[str]] = {}
        for port, bits in cell.outputs.items():
            outs[port] = []
            for pos, bit in enumerate(bits):
                net = uf.find(bit)
                previous = drivers.get(net)
                where = f"{cell.name}.{port}[{pos}]"
                if previous is not None:
                    raise SynthesisError(
                        f"multiple drivers on net {net!r}: {previous} and {where}"
                    )
                drivers[net] = where
                outs[port].append(net)
        resolved.append(ResolvedCell(cell, ins, outs))
    return resolved, uf


def _constant_value(bits: Sequence[str]) -> Optional[int]:
    value = 0
    for i, bit in enumerate(bits):
        if bit == CONST1:
            value |= 1 << i
        elif bit != CONST0:
            return None
    return value


# ---------------------------------------------------------------------------
# Optimisation
# ---------------------------------------------------------------------------


@dataclass
class OptStats:
    cells_before: int = 0
    cells_after: int = 0
    folded: int = 0
    mux_collapsed: int = 0
    dead_removed: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "macro_cells_before": self.cells_before,
            "macro_cells_after": self.cells_after,
            "constant_folded": self.folded,
            "mux_collapsed": self.mux_collapsed,
            "unconnected_removed": self.dead_removed,
        }


def optimize(design: WordDesign) -> OptStats:
    """Word-level optimisation in place. Returns statistics for the report."""
    stats = OptStats(cells_before=len(design.cells))
    # worklist: rewrite aliases so later cells see folded constants immediately
    alias_uf = UnionFind()
    for a, b in design.aliases:
        alias_uf.union(a, b)

    def canon(bit: str) -> str:
        return alias_uf.find(bit)

    kept: List[MacroCell] = []
    for cell in design.cells:
        ins = {port: [canon(b) for b in bits]
               for port, bits in cell.inputs.items() if port not in ("RST_VAL", "RST")}
        outs = {port: [canon(b) for b in bits] for port, bits in cell.outputs.items()}
        out_bits = next(iter(outs.values()))

        if cell.type == "MUX2":
            sel = ins["S"][0]
            if sel == CONST1:
                design.aliases.append((out_bits[0], ins["B"][0]))
                stats.mux_collapsed += 1
                continue
            if sel == CONST0:
                design.aliases.append((out_bits[0], ins["A"][0]))
                stats.mux_collapsed += 1
                continue
            if ins["A"] == ins["B"]:
                for o, i in zip(out_bits, ins["A"]):
                    design.aliases.append((o, i))
                stats.mux_collapsed += 1
                continue

        if cell.type.startswith("DFF"):
            kept.append(cell)                    # never fold state
            continue

        # all-constant inputs -> fold
        values = {port: _constant_value(bits) for port, bits in ins.items()}
        if all(v is not None for v in values.values()) and values:
            folded = _fold(cell.type, values, cell.width)
            if folded is not None:
                for i, out_bit in enumerate(out_bits):
                    design.aliases.append(
                        (out_bit, CONST1 if (folded >> i) & 1 else CONST0))
                stats.folded += 1
                continue

        # partial simplification for two-input bitwise/mux logic
        simplified = _partial_fold(cell, ins, out_bits)
        if simplified is not None:
            for o, i in zip(out_bits, simplified):
                design.aliases.append((o, i))
            stats.folded += 1
            continue
        kept.append(cell)

    design.cells = kept
    _remove_unconnected(design, stats)
    stats.cells_after = len(design.cells)
    return stats


def _fold(cell_type: str, values: Dict[str, int], width: int) -> Optional[int]:
    mask = (1 << width) - 1
    a = values.get("A", 0)
    b = values.get("B", 0)
    if cell_type == "AND":
        return a & b
    if cell_type == "OR":
        return a | b
    if cell_type == "XOR":
        return a ^ b
    if cell_type == "XNOR":
        return (~(a ^ b)) & mask
    if cell_type == "NOT":
        return (~a) & mask
    if cell_type == "ADD":
        return (a + b) & mask
    if cell_type == "SUB":
        return (a - b) & mask
    if cell_type == "MUL":
        return (a * b) & mask
    if cell_type == "SHL":
        return (a << b) & mask
    if cell_type == "SHR":
        return (a >> b) & mask
    if cell_type == "EQ":
        return int(a == b)
    if cell_type == "NE":
        return int(a != b)
    if cell_type in ("LT", "LE", "GT", "GE"):
        return int({"LT": a < b, "LE": a <= b, "GT": a > b, "GE": a >= b}[cell_type])
    if cell_type == "MUX2":
        return values["B"] if values["S"] else values["A"]
    if cell_type == "REDOR":
        return int(a != 0)
    if cell_type == "REDAND":
        return int(a == mask)
    if cell_type == "REDXOR":
        return bin(a).count("1") & 1
    if cell_type in ("REDNAND", "REDNOR"):
        base = int(a == mask) if cell_type == "REDNAND" else int(a != 0)
        return 1 - base
    return None


def _partial_fold(cell: MacroCell, ins: Dict[str, List[str]],
                  out_bits: List[str]) -> Optional[List[str]]:
    """Identity/absorbing rewrites for bitwise logic with one constant operand."""
    if cell.type not in ("AND", "OR", "XOR", "XNOR"):
        return None
    a_bits, b_bits = ins["A"], ins["B"]
    width = min(len(a_bits), len(b_bits))
    out: List[str] = []
    changed = False
    for i in range(width):
        a, b = a_bits[i], b_bits[i]
        const = CONST1 if (a in (CONST0, CONST1) and b not in (CONST0, CONST1)) else None
        other = b if const else a
        if const is None and b in (CONST0, CONST1) and a not in (CONST0, CONST1):
            const, other = b, a
        if const is None:
            out.append(a)
            continue
        changed = True
        if cell.type == "AND":
            out.append(other if const == CONST1 else CONST0)
        elif cell.type == "OR":
            out.append(CONST1 if const == CONST1 else other)
        elif cell.type == "XOR":
            out.append(other if const == CONST0 else f"~{other}")
        else:                                     # XNOR
            out.append(f"~{other}" if const == CONST0 else other)
    if not changed:
        return None
    # materialise the few inverted cases as real inverters later; a "~net"
    # marker is resolved by _expand_inversions after optimisation
    return out


def _expand_inversions(design: WordDesign) -> None:
    """Turn ``~net`` markers produced by partial folding into INV cells."""
    for cell in design.cells:
        for port, bits in list(cell.inputs.items()):
            if port in ("RST_VAL", "RST"):
                continue
            new_bits = []
            for bit in bits:
                if bit.startswith("~"):
                    target = bit[1:]
                    inv = MacroCell(f"inv_{len(design.cells)}", "NOT", 1,
                                    {"A": [target]}, {"Y": [f"{target}__n"]})
                    design.cells.append(inv)
                    design.aliases.append((f"{target}__n", f"{target}__n"))
                    new_bits.append(f"{target}__n")
                else:
                    new_bits.append(bit)
            cell.inputs[port] = new_bits
    for a, b in list(design.aliases):
        pass


def _remove_unconnected(design: WordDesign, stats: OptStats) -> None:
    """Drop cells whose outputs reach no observable point."""
    uf = UnionFind()
    for a, b in design.aliases:
        uf.union(a, b)
    used = set()
    for port in design.ports:
        sig = design.signals.get(port)
        if sig:
            used.update(uf.find(b) for b in sig.bits)
    # a first pass: any cell that drives a used net makes its inputs used
    changed = True
    kept = list(design.cells)
    while changed:
        changed = False
        for cell in kept:
            outs = [uf.find(b) for bits in cell.outputs.values() for b in bits]
            if any(o in used for o in outs):
                for port, bits in cell.inputs.items():
                    if port in ("RST_VAL", "RST"):
                        continue
                    for b in bits:
                        net = uf.find(b)
                        if net not in used:
                            used.add(net)
                            changed = True
    survivors = []
    for cell in kept:
        outs = [uf.find(b) for bits in cell.outputs.values() for b in bits]
        if any(o in used for o in outs) or cell.type.startswith("DFF"):
            survivors.append(cell)
        else:
            stats.dead_removed += 1
    design.cells = survivors


# ---------------------------------------------------------------------------
# Technology mapping
# ---------------------------------------------------------------------------


@dataclass
class TechmapOptions:
    max_fanout: int = 8
    buffer_cell: str = "BUFX4"
    clock_buffer_cell: str = "BUFX8"
    cell_budget: int = 40000


class Mapper:
    def __init__(self, library: Library, options: Optional[TechmapOptions] = None) -> None:
        self.lib = library
        self.opt = options or TechmapOptions()
        self.netlist = GateNetlist(top="")
        self._inst = 0
        self._ties: Dict[str, str] = {}
        self.remap: Dict[str, str] = {}          # gate-level net renaming

    # -- helpers ---------------------------------------------------------
    def _name(self, hint: str) -> str:
        self._inst += 1
        return f"{hint}_{self._inst}"

    def _cell(self, cell_name: str, hint: str, inputs: Dict[str, str],
              outputs: Dict[str, str], origin: str = "") -> GateCell:
        model = self.lib.cell(cell_name)
        gate = GateCell(instance=self._name(hint), cell=cell_name,
                        inputs=dict(inputs), outputs=dict(outputs), origin=origin)
        gate.width = model.width
        gate.height = model.row_height
        return self.netlist.add_cell(gate)

    def _tie(self, value: int) -> str:
        key = CONST1 if value else CONST0
        if key not in self._ties:
            name = f"{'TIEHI' if value else 'TIELO'}_tie"
            net = key
            self._cell("TIEHI" if value else "TIELO", "tie", {}, {"Y": net})
            self._ties[key] = net
        return key

    def _fresh(self, base: str, width: int = 1) -> List[str]:
        self._inst += 1
        if width == 1:
            return [f"{base}_{self._inst}"]
        return [f"{base}_{self._inst}[{i}]" for i in range(width)]

    def _gate1(self, cell: str, a: str, base: str) -> str:
        out = self._fresh(base)[0]
        self._cell(cell, base, {"A": a}, {"Y": out})
        return out

    def _gate2(self, cell: str, a: str, b: str, base: str) -> str:
        out = self._fresh(base)[0]
        pins = {p: (a if i == 0 else b) for i, p in enumerate(self.lib.cell(cell).input_pins())}
        self._cell(cell, base, pins, {"Y": out})
        return out

    def _and(self, a: str, b: str) -> str:
        return self._gate2("AND2X1", a, b, "and")

    def _or(self, a: str, b: str) -> str:
        return self._gate2("OR2X1", a, b, "or")

    def _xor(self, a: str, b: str) -> str:
        return self._gate2("XOR2X1", a, b, "xor")

    def _inv(self, a: str) -> str:
        return self._gate1("INVX1", a, "inv")

    def _reduce(self, nets: Sequence[str], kind: str) -> str:
        items = list(nets)
        if not items:
            return self._tie(1 if kind == "and" else 0)
        while len(items) > 1:
            merged = []
            for i in range(0, len(items) - 1, 2):
                a, b = items[i], items[i + 1]
                merged.append({"and": self._and, "or": self._or,
                               "xor": self._xor}[kind](a, b))
            if len(items) % 2:
                merged.append(items[-1])
            items = merged
        return items[0]

    # -- mapping ---------------------------------------------------------
    def map_design(self, design: WordDesign, stats: OptStats) -> GateNetlist:
        resolved, uf = resolve_nets(design)
        self.netlist.top = design.top
        for cell in resolved:
            self._map_macro(cell)
        self._apply_remap()
        # primary ports
        for port in design.ports:
            sig = design.signals[port]
            direction = design.port_dirs.get(port, "internal")
            if sig.width == 1:
                self.netlist.ports[port] = self._resolve(uf.find(sig.bits[0]))
                self.netlist.port_dirs[port] = direction
                self.netlist.nets.setdefault(self.netlist.ports[port], set()).add(f"port/{port}")
            else:
                for i, bit in enumerate(sig.bits):
                    name = f"{port}[{i}]"
                    self.netlist.ports[name] = self._resolve(uf.find(bit))
                    self.netlist.port_dirs[name] = direction
                    self.netlist.nets.setdefault(self.netlist.ports[name], set()).add(f"port/{name}")
        self._fix_fanout()
        self._check_budget()
        return self.netlist

    def _map_macro(self, rc: ResolvedCell) -> None:
        cell = rc.cell
        kind = cell.type
        width = cell.width
        ins, outs = rc.inputs, rc.outputs
        y = outs.get("Y") or outs.get("Q") or []

        if kind == "DFFR" or kind == "DFF":
            self._cell("DFFRX1" if kind == "DFFR" else "DFFX1", "ff",
                       {"D": ins["D"][0], "CK": ins["CK"][0]}, {"Q": y[0]},
                       origin=cell.name)
            for i in range(1, width):
                self._cell("DFFRX1" if kind == "DFFR" else "DFFX1", "ff",
                           {"D": ins["D"][i], "CK": ins["CK"][0]},
                           {"Q": y[i]}, origin=cell.name)
            if kind == "DFFR":
                rst = ins.get("RST", [None])[0]
                reset_val = int(ins.get("RST_VAL", ["0"])[0])
                if rst:
                    for gate in self.netlist.cells[-width:]:
                        gate.inputs["RN"] = rst
                if reset_val:
                    # The library resets to 0, so bits whose reset value is 1 are
                    # built as an inverter around a flop that resets to 0.
                    for i in range(width):
                        if (reset_val >> i) & 1:
                            gate = self.netlist.cells[-width + i]
                            d_net = gate.inputs["D"]
                            q_net = gate.outputs["Q"]
                            gate.inputs["D"] = self._inv(d_net)
                            inv_q = self._inv(q_net)
                            self.remap[q_net] = inv_q
            return

        if kind in ("AND", "OR", "XOR", "XNOR"):
            names = {"AND": "AND2X1", "OR": "OR2X1", "XOR": "XOR2X1", "XNOR": "XNOR2X1"}
            for i in range(width):
                self._cell(names[kind], kind.lower(),
                           {"A": ins["A"][i], "B": ins["B"][i]}, {"Y": y[i]},
                           origin=cell.name)
            return

        if kind == "NOT":
            for i in range(width):
                self._cell("INVX1", "inv", {"A": ins["A"][i]}, {"Y": y[i]},
                           origin=cell.name)
            return

        if kind == "MUX2":
            for i in range(width):
                self._cell("MUX2X1", "mux",
                           {"A": ins["A"][i], "B": ins["B"][i], "S": ins["S"][0]},
                           {"Y": y[i]}, origin=cell.name)
            return

        if kind in ("REDOR", "REDAND", "REDXOR", "REDNAND", "REDNOR"):
            base = {"REDOR": "or", "REDAND": "and", "REDXOR": "xor",
                    "REDNAND": "and", "REDNOR": "or"}[kind]
            net = self._reduce(ins["A"], base)
            if kind in ("REDNAND", "REDNOR"):
                net = self._inv(net)
            self._alias_net(y[0], net)
            return

        if kind in ("ADD", "SUB"):
            carry = self._tie(1 if kind == "SUB" else 0)
            b_bits = list(ins["B"])
            if kind == "SUB":
                b_bits = [self._inv(b) for b in b_bits]
            for i in range(width):
                carry_net = self._fresh("co", 1)[0]
                self._cell("FAX1", "fa", {"A": ins["A"][i], "B": b_bits[i], "CI": carry},
                           {"S": y[i], "CO": carry_net}, origin=cell.name)
                carry = carry_net
            return

        if kind == "MUL":
            self._map_multiplier(ins["A"], ins["B"], y, width, cell.name)
            return

        if kind in ("SHL", "SHR"):
            if not all(b in (CONST0, CONST1) for b in ins["B"]):
                raise SynthesisError(
                    f"variable shift amount in {cell.name}: only constant shifts are "
                    "supported by this engine")
            amount = _constant_value(ins["B"]) or 0
            a_bits = ins["A"]
            for i in range(width):
                src = i - amount if kind == "SHL" else i + amount
                self._alias_net(y[i], a_bits[src] if 0 <= src < len(a_bits) else CONST0)
            return

        if kind in ("EQ", "NE", "LT", "LE", "GT", "GE"):
            self._map_compare(kind, ins["A"], ins["B"], y[0], cell.name)
            return

        if kind == "BUF":
            self._alias_net(y[0], ins["A"][0])
            return

        raise SynthesisError(f"no mapping rule for macro cell {kind}")

    def _alias_net(self, dst: str, src: str) -> None:
        """Connect two nets with no logic: record a rename, applied at the end."""
        if dst != src:
            self.remap[dst] = src

    def _resolve(self, net: str) -> str:
        seen = set()
        while net in self.remap and net not in seen:
            seen.add(net)
            net = self.remap[net]
        return net

    def _apply_remap(self) -> None:
        """Rewrite sink nets (inputs and input ports) through the rename table.

        Driver outputs keep their own names, so a renamed net is still driven
        exactly once.
        """
        for gate in self.netlist.cells:
            for pin in list(gate.inputs):
                gate.inputs[pin] = self._resolve(gate.inputs[pin])
        for port, net in list(self.netlist.ports.items()):
            if self.netlist.port_dirs.get(port) in ("input", "inout"):
                self.netlist.ports[port] = self._resolve(net)
        self.netlist.nets.clear()
        self.netlist.constants = {CONST0: 0, CONST1: 1}
        for gate in self.netlist.cells:
            for pin, net in gate.inputs.items():
                self.netlist.nets.setdefault(net, set()).add(f"{gate.instance}/{pin}")
            for pin, net in gate.outputs.items():
                self.netlist.nets.setdefault(net, set()).add(f"{gate.instance}/{pin}")
        for port, net in self.netlist.ports.items():
            self.netlist.nets.setdefault(net, set()).add(f"port/{port}")

    def _map_compare(self, kind: str, a_bits: List[str], b_bits: List[str],
                     out: str, origin: str) -> None:
        width = max(len(a_bits), len(b_bits))
        a = list(a_bits) + [CONST0] * (width - len(a_bits))
        b = list(b_bits) + [CONST0] * (width - len(b_bits))
        if kind in ("GT", "GE"):
            a, b = b, a
        eq_bits = [self._gate2("XNOR2X1", a[i], b[i], "xno") for i in range(width)]
        eq_all = self._reduce(eq_bits, "and")
        if kind == "EQ":
            self._alias_net(out, eq_all)
            return
        if kind == "NE":
            self._alias_net(out, self._inv(eq_all))
            return
        # Unsigned magnitude compare, MSB first: the highest differing bit
        # decides.  ``higher_equal`` is the equality of all bits above bit i.
        acc = self._tie(0)
        higher_equal = self._tie(1)
        for i in range(width - 1, -1, -1):
            lt_i = self._and(self._inv(a[i]), b[i])
            acc = self._or(acc, self._and(higher_equal, lt_i))
            higher_equal = self._and(higher_equal, eq_bits[i])
        if kind in ("LT", "LE"):
            result = acc if kind == "LT" else self._or(acc, eq_all)
        else:                                        # GT / GE, operands swapped above
            result = acc if kind == "GT" else self._or(acc, eq_all)
        self._alias_net(out, result)

    def _map_multiplier(self, a_bits: List[str], b_bits: List[str],
                        y: List[str], width: int, origin: str) -> None:
        if width > 16:
            raise SynthesisError(
                f"multiplier of width {width} exceeds the 16-bit limit of this engine")
        partials: List[List[str]] = []
        for j in range(width):
            row = [CONST0] * j
            for i in range(width - j):
                row.append(self._and(a_bits[i], b_bits[j]))
            partials.append(row)
        acc = [CONST0] * width
        for row in partials:
            row = row + [CONST0] * (width - len(row))
            carry = self._tie(0)
            new_acc = []
            for i in range(width):
                s = self._fresh("msum", 1)[0]
                co = self._fresh("mcarry", 1)[0]
                self._cell("FAX1", "fa", {"A": acc[i], "B": row[i], "CI": carry},
                           {"S": s, "CO": co}, origin=origin)
                new_acc.append(s)
                carry = co
            acc = new_acc
        for i in range(width):
            self._alias_net(y[i], acc[i])

    # -- post-mapping cleanup --------------------------------------------
    def _fix_fanout(self) -> None:
        limit = self.opt.max_fanout
        model = self.lib.cell(self.opt.buffer_cell)
        for _ in range(4):                       # bounded number of repair passes
            overloaded = []
            for net, pins in self.netlist.nets.items():
                sinks = sorted(p for p in pins if self._is_sink(p))
                if len(sinks) > limit:
                    overloaded.append((net, sinks))
            if not overloaded:
                return
            for net, sinks in overloaded:
                groups = [sinks[i:i + limit] for i in range(0, len(sinks), limit)]
                for group in groups:
                    buf_net = self._fresh("buf", 1)[0]
                    self._cell(self.opt.buffer_cell, "buf", {"A": net}, {"Y": buf_net})
                    for pin_key in group:
                        if pin_key.startswith("port/"):
                            self.netlist.ports[pin_key[5:]] = buf_net
                        else:
                            inst, pin = pin_key.split("/", 1)
                            self.netlist.by_instance(inst).inputs[pin] = buf_net
                        self.netlist.nets.setdefault(buf_net, set()).add(pin_key)
                        self.netlist.nets[net].discard(pin_key)

    def _is_sink(self, pin_key: str) -> bool:
        if pin_key.startswith("port/"):
            # input ports drive their net; only output ports load it
            return self.netlist.port_dirs.get(pin_key[5:], "") in ("output", "inout")
        inst, pin = pin_key.split("/", 1)
        return pin in self.netlist.by_instance(inst).inputs

    def _check_budget(self) -> None:
        if len(self.netlist.cells) > self.opt.cell_budget:
            raise SynthesisError(
                f"mapped netlist has {len(self.netlist.cells)} cells, above the "
                f"{self.opt.cell_budget}-cell budget of this engine")


def synthesize(design: WordDesign, library: Library,
               options: Optional[TechmapOptions] = None) -> Tuple[GateNetlist, OptStats]:
    """Optimise then map. Returns the gate netlist and optimisation statistics."""
    stats = optimize(design)
    _expand_inversions(design)
    mapper = Mapper(library, options)
    netlist = mapper.map_design(design, stats)
    return netlist, stats
