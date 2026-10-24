"""Four-state simulation and formal-ish equivalence checking for the tools.

Two simulators live here, and they are deliberately independent of each other:

``GateSim``
    Cycle-accurate 0/1/X simulation of a *mapped gate netlist* (``GateNetlist``).
    Every library cell gets its boolean function from :data:`CELL_FUNCTIONS`, so
    this engine exercises the technology mapper, the optimiser, the fanout
    buffer pass and the serialised netlist database -- not the RTL.

``WordSim``
    Simulation of the *word-level elaborated design* (``WordDesign``) that comes
    straight out of :mod:`silicon.elaborate`, before any mapping. Adders,
    comparators and multipliers are evaluated as whole words here.

Comparing the two is the correctness gate for ``dc``: if a mapped netlist does
not match the elaborated behaviour on random vectors, the mapper is wrong.
Run it with ``silicon sim`` or ``verify`` from the shell.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .netlist import GateNetlist, WordDesign

X = 2                                       # third logic value: unknown
_HIGH_Z = X


# ---------------------------------------------------------------------------
# Four-state primitives
# ---------------------------------------------------------------------------


def b_not(a: int) -> int:
    return X if a == X else 1 - a


def b_and(*values: int) -> int:
    result = 1
    for v in values:
        if v == 0:
            return 0
        if v == X:
            result = X
    return result


def b_or(*values: int) -> int:
    result = 0
    for v in values:
        if v == 1:
            return 1
        if v == X:
            result = X
    return result


def b_xor(*values: int) -> int:
    result = 0
    for v in values:
        if v == X:
            return X
        result ^= v
    return result


def b_mux(sel: int, a: int, b: int) -> int:
    """``sel ? b : a`` -- the convention used by ``MUX2X1`` in this library."""
    if sel == X:
        return a if a == b else X
    return b if sel else a


def bits_to_int(bits: Sequence[int]) -> Optional[int]:
    """LSB-first bit list to an integer, or ``None`` if any bit is unknown."""
    value = 0
    for i, bit in enumerate(bits):
        if bit == X:
            return None
        if bit:
            value |= (1 << i)
    return value


def int_to_bits(value: int, width: int) -> List[int]:
    return [(value >> i) & 1 for i in range(width)]


# ---------------------------------------------------------------------------
# Library cell behaviour (the function table the mapper must respect)
# ---------------------------------------------------------------------------


def _gate_function(cell: str, ins: Dict[str, int]) -> Dict[str, int]:
    name = cell.upper()
    if name.startswith("FILL"):
        return {}
    if name == "TIEHI":
        return {"Y": 1}
    if name == "TIELO":
        return {"Y": 0}
    if name.startswith("INV"):
        return {"Y": b_not(ins["A"])}
    if name.startswith("BUF"):
        return {"Y": ins["A"]}
    if name.startswith("NAND"):
        return {"Y": b_not(b_and(*[ins[p] for p in ("A", "B", "C") if p in ins]))}
    if name.startswith("NOR"):
        return {"Y": b_not(b_or(*[ins[p] for p in ("A", "B", "C") if p in ins]))}
    if name.startswith("XNOR"):
        return {"Y": b_not(b_xor(ins["A"], ins["B"]))}
    if name.startswith("XOR"):
        return {"Y": b_xor(ins["A"], ins["B"])}
    if name.startswith("AND"):
        return {"Y": b_and(*[ins[p] for p in ("A", "B", "C") if p in ins])}
    if name.startswith("OR"):
        return {"Y": b_or(*[ins[p] for p in ("A", "B", "C") if p in ins])}
    if name.startswith("MUX"):
        return {"Y": b_mux(ins["S"], ins["A"], ins["B"])}
    if name.startswith("AOI"):
        # ~((A & B) | C)
        return {"Y": b_not(b_or(b_and(ins["A"], ins["B"]), ins["C"]))}
    if name.startswith("OAI"):
        # ~((A | B) & C)
        return {"Y": b_not(b_and(b_or(ins["A"], ins["B"]), ins["C"]))}
    if name.startswith("FA"):
        a, b, ci = ins["A"], ins["B"], ins["CI"]
        return {"S": b_xor(a, b, ci),
                "CO": b_or(b_and(a, b), b_and(a, ci), b_and(b, ci))}
    raise KeyError(f"no simulation model for library cell {cell!r}")


# ---------------------------------------------------------------------------
# Gate-level simulator
# ---------------------------------------------------------------------------


@dataclass
class SimResult:
    inputs: Dict[str, List[int]] = field(default_factory=dict)
    outputs: Dict[str, List[int]] = field(default_factory=dict)
    x_propagation: int = 0


class GateSim:
    """Cycle-based 0/1/X simulator for a mapped gate netlist."""

    def __init__(self, netlist: GateNetlist, clock: str = "clk",
                 max_iterations: int = 400) -> None:
        self.netlist = netlist
        self.clock = clock
        self.max_iterations = max_iterations
        self.values: Dict[str, int] = {}
        self.state: Dict[str, int] = {}
        self.combinational = []
        self.flops = []
        for cell in netlist.cells:
            if cell.cell.upper().startswith("DFF"):
                self.flops.append(cell)
                for net in cell.outputs.values():
                    self.state[net] = X
            elif not cell.cell.upper().startswith("FILL"):
                self.combinational.append(cell)
        self.input_bits = {p: [] for p, d in netlist.port_dirs.items() if d == "input"}
        self.output_bits = {p: [] for p, d in netlist.port_dirs.items() if d == "output"}
        self.x_count = 0

    # -- net resolution --------------------------------------------------
    def _net(self, net: str) -> int:
        base = self.netlist.constants.get(net)
        if base is not None:
            return base
        return self.values.get(net, X)

    def _drive(self, net: str, bit: int) -> bool:
        if net in self.netlist.constants:
            return False
        if self.values.get(net, X) != bit:
            self.values[net] = bit
            return True
        return False

    def _output_net(self, cell, pin: str) -> str:
        return cell.outputs[pin]

    # -- evaluation ------------------------------------------------------
    def settle(self) -> int:
        """Iterate the combinational cloud to a fixed point; returns sweeps used."""
        for _ in range(self.max_iterations):
            changed = False
            for cell in self.combinational:
                ins = {pin: self._net(net) for pin, net in cell.inputs.items()
                       if net in self.netlist.nets or net in self.netlist.constants
                       or pin in ("A", "B", "C", "S", "CI")}
                try:
                    outs = _gate_function(cell.cell, ins)
                except KeyError:
                    outs = {}
                for pin, bit in outs.items():
                    if pin in cell.outputs:
                        changed |= self._drive(cell.outputs[pin], bit)
            if not changed:
                # a second sweep catches cells that read their own stale value
                changed_again = False
                for cell in self.combinational:
                    ins = {pin: self._net(net) for pin, net in cell.inputs.items()}
                    try:
                        outs = _gate_function(cell.cell, ins)
                    except KeyError:
                        continue
                    for pin, bit in outs.items():
                        if pin in cell.outputs:
                            changed_again |= self._drive(cell.outputs[pin], bit)
                if not changed_again:
                    return 1
                continue
        raise RuntimeError("combinational logic did not converge "
                           "(oscillating netlist?)")

    def set_inputs(self, values: Dict[str, int]) -> None:
        """``values`` maps input *bit* names (``a[0]``) to 0/1/X."""
        for net, bit in values.items():
            self.values[net] = bit
        self.settle()
        self._apply_async_reset()

    def _apply_async_reset(self) -> None:
        changed = True
        while changed:
            changed = False
            for cell in self.flops:
                if cell.cell.upper().startswith("DFFRX") or "RN" in cell.inputs:
                    rn = self._net(cell.inputs["RN"]) if "RN" in cell.inputs else 1
                    if rn == 0:
                        for net in cell.outputs.values():
                            if self.state.get(net) != 0:
                                self.state[net] = 0
                                changed = True
            if changed:
                self._publish_state()
                self.settle()

    def _publish_state(self) -> None:
        for net, bit in self.state.items():
            self.values[net] = bit

    def rising_edge(self) -> None:
        """Advance one clock cycle: capture ``D`` then publish ``Q``."""
        self.settle()
        captured = {}
        for cell in self.flops:
            d_net = cell.inputs.get("D")
            d = self._net(d_net) if d_net is not None else X
            if "RN" in cell.inputs and self._net(cell.inputs["RN"]) == 0:
                d = 0
            captured[cell.outputs["Q"]] = d
        self.state.update(captured)
        self._publish_state()
        self.settle()
        self._apply_async_reset()

    # -- port helpers ----------------------------------------------------
    def read_port(self, name: str) -> List[int]:
        """Bits of an output port, LSB first."""
        bits = [p for p, d in self.netlist.port_dirs.items() if d == "output"
                and (p == name or p.startswith(name + "["))]
        if len(bits) > 1:
            bits.sort(key=lambda p: int(p[p.index("[") + 1:-1]))
        return [self._net(self.netlist.ports[b]) for b in bits]

    def input_net(self, name: str) -> List[str]:
        bits = [p for p, d in self.netlist.port_dirs.items() if d == "input"
                and (p == name or p.startswith(name + "["))]
        if len(bits) > 1:
            bits.sort(key=lambda p: int(p[p.index("[") + 1:-1]))
        return [self.netlist.ports[b] for b in bits]

    def unknown_outputs(self) -> int:
        count = 0
        for name in {p.split("[")[0] for p, d in self.netlist.port_dirs.items()
                     if d == "output"}:
            count += sum(1 for bit in self.read_port(name) if bit == X)
        return count


# ---------------------------------------------------------------------------
# Word-level (pre-mapping) simulator: the reference model
# ---------------------------------------------------------------------------


class WordSim:
    """Evaluate the elaborated word-level design (adders as whole words)."""

    def __init__(self, design: WordDesign, clock: str = "clk") -> None:
        self.design = design
        self.clock = clock
        self.values: Dict[str, int] = {}
        self.state: Dict[str, int] = {}
        self.combinational = [c for c in design.cells if c.type not in ("DFF", "DFFR")]
        self.flops = [c for c in design.cells if c.type in ("DFF", "DFFR")]
        for cell in self.flops:
            for bit in cell.outputs["Q"]:
                self.state[bit] = X
        self.aliases = list(design.aliases)

    def _net(self, net: str) -> int:
        if net == "1'b0" or net == "0":
            return 0
        if net == "1'b1" or net == "1":
            return 1
        return self.values.get(net, X)

    def _drive(self, net: str, bit: int) -> bool:
        if net in ("1'b0", "1'b1"):
            return False
        if self.values.get(net, X) != bit:
            self.values[net] = bit
            return True
        return False

    def _word(self, bits: Iterable[str]) -> Optional[int]:
        return bits_to_int([self._net(b) for b in bits])

    def _eval(self, cell) -> Dict[str, List[int]]:
        t = cell.type
        width = cell.width
        ins = cell.inputs
        if t in ("AND", "OR", "XOR", "XNOR"):
            fn = {"AND": b_and, "OR": b_or, "XOR": b_xor,
                  "XNOR": lambda a, b: b_not(b_xor(a, b))}[t]
            return {"Y": [fn(self._net(a), self._net(b))
                          for a, b in zip(ins["A"], ins["B"])]}
        if t == "NOT":
            return {"Y": [b_not(self._net(a)) for a in ins["A"]]}
        if t == "MUX2":
            return {"Y": [b_mux(self._net(ins["S"][0]), self._net(a), self._net(b))
                          for a, b in zip(ins["A"], ins["B"])]}
        if t == "BUF":
            return {"Y": [self._net(a) for a in ins["A"]]}
        if t in ("REDAND", "REDOR", "REDXOR", "REDNAND", "REDNOR"):
            fn = {"REDAND": b_and, "REDOR": b_or, "REDXOR": b_xor}[t.split("N")[0]]
            value = fn(*[self._net(a) for a in ins["A"]])
            if t in ("REDNAND", "REDNOR"):
                value = b_not(value)
            return {"Y": [value]}
        if t in ("ADD", "SUB", "MUL", "SHL", "SHR"):
            a = self._word(ins["A"])
            b = self._word(ins["B"])
            if a is None or b is None:
                return {"Y": [X] * width}
            mask = (1 << width) - 1
            if t == "ADD":
                value = a + b
            elif t == "SUB":
                value = a - b
            elif t == "MUL":
                value = a * b
            elif t == "SHL":
                value = a << b
            else:
                value = a >> b
            return {"Y": int_to_bits(value & mask, width)}
        if t in ("EQ", "NE", "LT", "LE", "GT", "GE"):
            a = self._word(ins["A"])
            b = self._word(ins["B"])
            if a is None or b is None:
                return {"Y": [X]}
            result = {"EQ": a == b, "NE": a != b, "LT": a < b,
                      "LE": a <= b, "GT": a > b, "GE": a >= b}[t]
            return {"Y": [1 if result else 0]}
        raise KeyError(f"no word-level model for macro cell {t!r}")

    def settle(self) -> None:
        for _ in range(64):
            changed = False
            for a, b in self.aliases:
                if a in self.design.signals or True:
                    v = self._net(b)
                    if v != X:
                        changed |= self._drive(a, v)
                    v = self._net(a)
                    if v != X:
                        changed |= self._drive(b, v)
            for cell in self.combinational:
                for pin, bits in self._eval(cell).items():
                    for net, bit in zip(cell.outputs[pin], bits):
                        changed |= self._drive(net, bit)
            if not changed:
                return
        raise RuntimeError("word-level logic did not converge")

    def signal_bits(self, name: str) -> List[str]:
        if name not in self.design.signals:
            matches = [s for s in self.design.signals
                       if s.rsplit(".", 1)[-1] == name]
            if not matches:
                raise KeyError(name)
            name = matches[0]
        return self.design.signals[name].bits

    def set_inputs(self, values: Dict[str, int]) -> None:
        for net, bit in values.items():
            self.values[net] = bit
        self.settle()
        self._apply_async_reset()

    def _apply_async_reset(self) -> None:
        changed = True
        while changed:
            changed = False
            for cell in self.flops:
                if cell.type != "DFFR":
                    continue
                rst = cell.inputs.get("RST")
                if rst and self._net(rst[0]) == 0:
                    reset_value = int(cell.inputs.get("RST_VAL", ["0"])[0])
                    for i, bit in enumerate(cell.outputs["Q"]):
                        want = (reset_value >> i) & 1
                        if self.state.get(bit) != want:
                            self.state[bit] = want
                            changed = True
            if changed:
                self._publish()
                self.settle()

    def _publish(self) -> None:
        for net, bit in self.state.items():
            self.values[net] = bit

    def rising_edge(self) -> None:
        self.settle()
        captured = {}
        for cell in self.flops:
            d = [self._net(b) for b in cell.inputs["D"]]
            if cell.type == "DFFR":
                rst = cell.inputs.get("RST")
                rst_val = int(cell.inputs.get("RST_VAL", ["0"])[0])
                if rst and self._net(rst[0]) == 0:
                    d = int_to_bits(rst_val, len(d))
            for net, bit in zip(cell.outputs["Q"], d):
                captured[net] = bit
        self.state.update(captured)
        self._publish()
        self.settle()
        self._apply_async_reset()

    def read_port(self, name: str) -> List[int]:
        return [self._net(b) for b in self.signal_bits(name)]


# ---------------------------------------------------------------------------
# Equivalence check: mapped netlist vs elaborated design
# ---------------------------------------------------------------------------


@dataclass
class CheckReport:
    vectors: int = 0
    mismatches: int = 0
    output_bits: int = 0
    x_free: bool = True
    examples: List[str] = field(default_factory=list)
    ok: bool = False

    def summary(self) -> str:
        lines = [
            f"Simulation vectors       : {self.vectors}",
            f"Outputs compared         : {self.output_bits}",
            f"Mismatches               : {self.mismatches}",
            f"Unknown values (X) seen  : {'no' if self.x_free else 'yes'}",
            f"Result                   : {'PASS' if self.ok else 'FAIL'}",
        ]
        if self.examples:
            lines.append("First mismatches:")
            lines.extend("  " + e for e in self.examples[:5])
        return "\n".join(lines)


def equivalence_check(netlist: GateNetlist, design: WordDesign, *,
                      clock: str = "clk", reset: Optional[str] = None,
                      reset_active_low: bool = True, vectors: int = 64,
                      seed: int = 1) -> CheckReport:
    """Randomly compare a mapped netlist against the elaborated design."""
    gate = GateSim(netlist, clock)
    word = WordSim(design, clock)
    report = CheckReport()

    outputs = sorted({p.split("[")[0] for p, d in netlist.port_dirs.items()
                      if d == "output"})
    inputs = sorted({p.split("[")[0] for p, d in netlist.port_dirs.items()
                     if d == "input" and p != clock and p != reset})

    def drive(sim, values: Dict[str, List[int]]) -> None:
        flat = {}
        for name, bits in values.items():
            target = (sim.input_net(name) if isinstance(sim, GateSim)
                      else sim.signal_bits(name))
            for net, bit in zip(target, bits):
                flat[net] = bit
        sim.set_inputs(flat)

    def clocks(sim, low: bool) -> None:
        target = (sim.input_net(clock) if isinstance(sim, GateSim)
                  else sim.signal_bits(clock))
        for net in target:
            sim.set_inputs({net: 0 if low else 1})

    def resets(sim, asserted: bool) -> None:
        if not reset:
            return
        target = (sim.input_net(reset) if isinstance(sim, GateSim)
                  else sim.signal_bits(reset))
        level = (0 if asserted else 1) if reset_active_low else (1 if asserted else 0)
        for net in target:
            sim.set_inputs({net: level})

    # power-on reset
    clocks(gate, True)
    clocks(word, True)
    resets(gate, True)
    resets(word, True)
    gate.settle()
    word.settle()
    clocks(gate, False)
    clocks(word, False)
    gate.rising_edge()
    word.rising_edge()

    rng = random.Random(seed)
    widths = {}
    for name in inputs:
        bits = (gate.input_net(name) if True else [])
        widths[name] = len(bits)

    for vector in range(vectors):
        values = {}
        for name in inputs:
            width = widths[name]
            values[name] = [rng.randint(0, 1) for _ in range(width)]
        if reset:
            resets(gate, False)
            resets(word, False)
        drive(gate, values)
        drive(word, values)
        gate.settle()
        word.settle()

        for out in outputs:
            g = gate.read_port(out)
            w = word.read_port(out)
            report.output_bits += len(g)
            if any(bit == X for bit in g):
                report.x_free = False
            if g != w:
                report.mismatches += 1
                if len(report.examples) < 8:
                    report.examples.append(
                        f"cycle {vector} port {out}: gate={g} rtl={w} "
                        f"inputs={{{', '.join(f'{k}={v}' for k, v in values.items())}}}")
        report.vectors += 1
        clocks(gate, True)
        clocks(word, True)
        gate.rising_edge()
        word.rising_edge()
        clocks(gate, False)
        clocks(word, False)
        gate.settle()
        word.settle()

    report.ok = report.mismatches == 0 and report.x_free
    return report
