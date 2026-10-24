"""Parameter substitution, width inference and structural elaboration.

Turns a parsed Verilog AST into a :class:`~silicon.netlist.WordDesign`:

* every module instance is flattened, with flattened names (``u_dp.sum[0]``)
* buses become explicit bit lists (LSB first)
* slices, concatenations, replications and port connections become wire aliases
* ``always`` blocks become multiplexer clouds plus a known set of flip-flops
* asynchronous resets are recognised and handed to the library as DFFR cells,
  which is what makes reset reachable-path timing meaningful later

Semantics that the engine deliberately does not model (see the limitations
section of README.md):
signed arithmetic, ``initial`` blocks, latches inferred from incomplete
sensitivity, memory arrays, ``for``/``generate`` loops and X-propagation. Each
one raises :class:`~silicon.verilog.VerilogError` rather than producing a
silently wrong netlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .netlist import CONST0, CONST1, MacroCell, Signal, UnionFind, WordDesign
from .verilog import (
    Always, Assign, AssignStmt, Binary, Call, Case, Concat, Decl, Ident, If,
    Index, Inst, Module, Num, Param, Range, Replicate, Slice, Ternary, Unary,
    VerilogError, _static_eval,
)

_BITWISE = {"&": "AND", "|": "OR", "^": "XOR", "~^": "XNOR", "^~": "XNOR"}
_ARITH = {"+": "ADD", "-": "SUB", "*": "MUL", "<<": "SHL", ">>": "SHR"}
_COMPARE = {"==": "EQ", "!=": "NE", "<": "LT", "<=": "LE", ">": "GT", ">=": "GE"}


@dataclass
class Scope:
    """One flattened module instance: its params and its signal bits."""

    params: Dict[str, int] = field(default_factory=dict)
    signals: Dict[str, List[str]] = field(default_factory=dict)     # name -> bits
    widths: Dict[str, int] = field(default_factory=dict)
    reads: Dict[str, List[str]] = field(default_factory=dict)       # override map
    path: str = ""

    def bits_of(self, name: str) -> List[str]:
        if name in self.reads:
            return self.reads[name]
        if name in self.signals:
            return self.signals[name]
        if name in self.params:
            value = self.params[name]
            width = max(1, value.bit_length())
            return [CONST1 if (value >> i) & 1 else CONST0 for i in range(width)]
        raise VerilogError(f"unknown identifier {name!r}")

    def width_of(self, name: str) -> int:
        if name in self.reads:
            return len(self.reads[name])
        if name in self.widths:
            return self.widths[name]
        if name in self.params:
            return max(1, self.params[name].bit_length())
        raise VerilogError(f"unknown identifier {name!r}")


def _rep_body(expr: "Replicate") -> Any:
    """Replication carries either one expression or a concatenation body."""
    body = expr.inner
    if isinstance(body, (list, tuple)):
        if len(body) == 1:
            return body[0]
        return Concat(list(body), expr.line)
    return body


class Elaborator:
    def __init__(self, modules: Sequence[Module], top: str, params: Optional[Dict[str, int]] = None,
                 where: str = "") -> None:
        self.modules = {m.name: m for m in modules}
        if top not in self.modules:
            raise VerilogError(
                f"top module {top!r} not found (have: {', '.join(sorted(self.modules))})",
                where=where,
            )
        self.top = top
        self.top_params = dict(params or {})
        self.uf = UnionFind()
        self.signals: Dict[str, Signal] = {}
        self.cells: List[MacroCell] = []
        self.clocks: Dict[str, float] = {}
        self.async_resets: set = set()
        self.ports: List[str] = []
        self.port_dirs: Dict[str, str] = {}
        self._cell_no = 0
        self._where = where

    # -- public ----------------------------------------------------------
    def run(self) -> WordDesign:
        mod = self.modules[self.top]
        scope = self._make_scope(mod, "", None, self.top_params)
        # expose the top-level ports as design ports
        for p in mod.ports:
            self.ports.append(p.names[0])
            self.port_dirs[p.names[0]] = p.kind
        self._elaborate_body(mod, scope, None)
        design = WordDesign(
            top=self.top,
            signals=self.signals,
            cells=self.cells,
            aliases=list(getattr(self, '_alias_pairs', [])),
            clocks=self.clocks,
            async_resets=self.async_resets,
            params=scope.params,
            ports=self.ports,
            port_dirs=self.port_dirs,
        )
        return design

    # -- scope construction ----------------------------------------------
    def _make_scope(self, mod: Module, path: str, parent: Optional[Scope],
                    overrides: Optional[Dict[str, int] | List[int]] = None) -> Scope:
        scope = Scope(path=path)
        # module parameters, evaluated in order so later params may use earlier
        counters = {"positional": 0}
        for p in mod.params:
            if isinstance(overrides, list):
                idx = counters["positional"]
                if idx < len(overrides):
                    scope.params[p.name] = overrides[idx]
                    counters["positional"] += 1
                    continue
            if isinstance(overrides, dict) and p.name in overrides:
                scope.params[p.name] = overrides[p.name]
                continue
            scope.params[p.name] = self._const(p.expr, scope)

        def width_from(rng: Optional[Range]) -> int:
            if rng is None:
                return 1
            msb = self._const(rng.msb, scope)
            lsb = self._const(rng.lsb, scope)
            return abs(msb - lsb) + 1

        for d in list(mod.ports):
            for name in d.names:
                self._declare(scope, name, width_from(d.width))
        for item in mod.items:
            decls = item if isinstance(item, list) else [item]
            for d in decls:
                if isinstance(d, Decl):
                    if d.is_array:
                        raise VerilogError(
                            f"memory array {d.names[0]!r} is not supported by this engine; "
                            "use flop arrays with constant indices or an SRAM macro",
                            d.line, self._where,
                        )
                    for name in d.names:
                        if name not in scope.signals:
                            self._declare(scope, name, width_from(d.width))
        return scope

    def _declare(self, scope: Scope, name: str, width: int) -> None:
        flat = scope.path + name
        sig = Signal(flat, width)
        self.signals[flat] = sig
        scope.signals[name] = sig.bits
        scope.widths[name] = width
        for bit in sig.bits:
            self.uf.add(bit)

    # -- body ------------------------------------------------------------
    def _elaborate_body(self, mod: Module, scope: Scope, conns) -> None:
        # port connections: alias caller bits onto local bits
        if conns:
            for port, caller_bits in conns.items():
                local = scope.signals.get(port)
                if local is None:
                    raise VerilogError(f"module {mod.name} has no port {port!r}")
                if caller_bits is not None:
                    self._alias(local, caller_bits)

        for item in mod.items:
            if isinstance(item, list):
                for sub in item:
                    self._elaborate_item(sub, scope)
            else:
                self._elaborate_item(item, scope)

    def _elaborate_item(self, item, scope: Scope) -> None:
        if isinstance(item, Decl) or isinstance(item, Param):
            return
        if isinstance(item, Assign):
            target = self._lvalue(item.lhs, scope, for_clocked=False, state=None)
            value = self._emit(item.rhs, scope)
            self._write(target, value, scope)
            return
        if isinstance(item, Always):
            self._elaborate_always(item, scope)
            return
        if isinstance(item, Inst):
            self._elaborate_instance(item, scope)
            return
        raise VerilogError(f"unsupported item {type(item).__name__} in module body")

    def _elaborate_instance(self, inst: Inst, scope: Scope) -> None:
        if inst.module not in self.modules:
            raise VerilogError(
                f"instance {inst.name!r} refers to unknown module {inst.module!r} "
                f"(have: {', '.join(sorted(self.modules))})"
            )
        mod = self.modules[inst.module]
        path = f"{scope.path}{inst.name}."
        overrides: Dict[str, int] = {}
        positional: List[int] = []
        for pname, pexpr in inst.params:
            value = self._const(pexpr, scope)
            if pname:
                overrides[pname] = value
            else:
                positional.append(value)
        child = self._make_scope(mod, path, scope, overrides or positional or None)

        conns: Dict[str, Optional[List[str]]] = {}
        port_names = [p.names[0] for p in mod.ports]
        for idx, (pname, expr) in enumerate(inst.connections):
            name = pname or (port_names[idx] if idx < len(port_names) else None)
            if name is None:
                raise VerilogError(f"too many connections on instance {inst.name!r}")
            conns[name] = None if expr is None else self._emit(expr, scope)
        self._elaborate_body(mod, child, conns)

    # -- always blocks ---------------------------------------------------
    def _elaborate_always(self, always: Always, scope: Scope) -> None:
        edges = [s for s in always.sensitivity if isinstance(s, tuple)]
        if not edges:
            self._elaborate_combinational(always, scope)
            return
        posedge = [name for edge, name in edges if edge == "posedge"]
        negedge = [name for edge, name in edges if edge == "negedge"]
        if len(posedge) != 1:
            raise VerilogError(
                "exactly one posedge clock per always block is supported "
                f"(found {posedge})", always.line, self._where,
            )
        clock = scope.path + posedge[0]
        self.clocks.setdefault(clock, 0.0)
        reset_names = [scope.path + n for n in negedge]
        for r in reset_names:
            self.async_resets.add(r)

        state: Dict[str, List[str]] = {}
        reset_state: Optional[Dict[str, List[str]]] = None

        if reset_names:
            reset_local = negedge[0]
            body = always.body
            if len(body) != 1 or not isinstance(body[0], If):
                raise VerilogError(
                    "an always block with an asynchronous reset must begin with "
                    "'if (<reset>) ... else ...'", always.line, self._where,
                )
            top_if = body[0]
            active_low = self._is_active_low_reset(top_if.cond, reset_local)
            reset_branch = top_if.then if active_low else top_if.els
            work_branch = top_if.els if active_low else top_if.then
            reset_state = {}
            self._exec_block(reset_branch, scope, reset_state)
            self._exec_block(work_branch, scope, state)
        else:
            self._exec_block(always.body, scope, state)

        for target, bits in state.items():
            reset_value = None
            if reset_state is not None:
                reset_value = reset_state.get(target)
            self._emit_flop(target, bits, scope, clock, reset_value)

    def _elaborate_combinational(self, always: Always, scope: Scope) -> None:
        """``always @*`` / ``always @(a or b)``: pure combinational logic."""
        state: Dict[str, List[str]] = {}
        self._exec_block(always.body, scope, state)
        for target, bits in state.items():
            own = self.signals[target].bits
            if any(bit in own for bit in bits):
                raise VerilogError(
                    f"latch inferred for {target!r}: assign it on every path of the "
                    "always block or add a default branch", always.line, self._where)
            self._write((target, None), bits, scope)

    @staticmethod
    def _is_active_low_reset(cond, reset_name: str) -> bool:
        if isinstance(cond, Unary) and cond.op == "!" and isinstance(cond.operand, Ident):
            return cond.operand.name == reset_name
        if isinstance(cond, Binary) and cond.op == "==" and isinstance(cond.left, Ident):
            try:
                return cond.left.name == reset_name and _static_eval(cond.right) == 0
            except VerilogError:
                return False
        return False

    def _exec_block(self, stmts: Sequence, scope: Scope, state: Dict[str, List[str]]) -> None:
        saved_reads = scope.reads
        scope.reads = dict(state)          # blocking / conditional visibility
        try:
            for st in stmts:
                self._exec_stmt(st, scope, state)
        finally:
            scope.reads = saved_reads

    def _exec_stmt(self, st, scope: Scope, state: Dict[str, List[str]]) -> None:
        if isinstance(st, AssignStmt):
            target = self._lvalue(st.lhs, scope, for_clocked=False, state=state)
            value = self._emit(st.rhs, scope)
            self._store(state, target, value)
            return
        if isinstance(st, If):
            sel = self._reduce(self._emit(st.cond, scope), scope)
            then_state = dict(state)
            self._exec_block(st.then, scope, then_state)
            else_state = dict(state)
            if st.els:
                self._exec_block(st.els, scope, else_state)
            merged = self._merge(state, then_state, else_state, sel, scope)
            state.clear()
            state.update(merged)
            return
        if isinstance(st, Case):
            self._exec_case(st, scope, state)
            return
        raise VerilogError(f"unsupported statement {type(st).__name__}", where=self._where)

    def _exec_case(self, case: Case, scope: Scope, state: Dict[str, List[str]]) -> None:
        subject = self._emit(case.expr, scope)
        branches: List[Tuple[Optional[List[str]], Dict[str, List[str]]]] = []
        default_state: Optional[Dict[str, List[str]]] = None
        for item in case.items:
            branch_state = dict(state)
            self._exec_block(item.body, scope, branch_state)
            if not item.labels:
                default_state = branch_state
                continue
            cond_bits: Optional[List[str]] = None
            for label in item.labels:
                label_bits = self._emit(label, scope)
                cmp = self._new_cell("EQ", 1, {"A": subject, "B": label_bits}, scope)
                cond_bits = cmp if cond_bits is None else self._or(cond_bits, cmp, scope)
            branches.append((cond_bits, branch_state))
        acc = default_state if default_state is not None else dict(state)
        for cond_bits, branch_state in reversed(branches):
            acc = self._merge(acc, branch_state, acc, self._reduce(cond_bits, scope), scope)
        state.clear()
        state.update(acc)

    def _merge(self, base: Dict[str, List[str]], then_state: Dict[str, List[str]],
               else_state: Dict[str, List[str]], sel: List[str],
               scope: Scope) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for target in set(base) | set(then_state) | set(else_state):
            a = then_state.get(target, base.get(target))
            b = else_state.get(target, base.get(target))
            if a is None and b is None:
                continue
            if a is None:
                a = scope.bits_of(self._local_name(target, scope))
            if b is None:
                b = scope.bits_of(self._local_name(target, scope))
            width = max(len(a), len(b))
            a = self._extend(a, width)
            b = self._extend(b, width)
            if a == b:
                out[target] = a
                continue
            cell = self._new_cell("MUX2", width,
                                  {"A": b, "B": a, "S": sel}, scope)
            out[target] = cell
        return out

    def _local_name(self, flat_target: str, scope: Scope) -> str:
        if flat_target.startswith(scope.path):
            return flat_target[len(scope.path):]
        return flat_target

    # -- lvalues ---------------------------------------------------------
    def _lvalue(self, lhs, scope: Scope, for_clocked: bool,
                state: Optional[Dict[str, List[str]]]) -> Tuple[str, Optional[Tuple[int, int]]]:
        """Return (flat signal name, bit range or None) for an assignment target."""
        if isinstance(lhs, Ident):
            return scope.path + lhs.name, None
        if isinstance(lhs, Slice) and isinstance(lhs.base, Ident):
            msb = self._const(lhs.msb, scope)
            lsb = self._const(lhs.lsb, scope)
            sig = scope.path + lhs.base.name
            lo, hi = min(msb, lsb), max(msb, lsb)
            return sig, (lo, hi)
        if isinstance(lhs, Index) and isinstance(lhs.base, Ident):
            idx = self._const(lhs.index, scope)
            return scope.path + lhs.base.name, (idx, idx)
        raise VerilogError("unsupported assignment target", where=self._where)

    def _store(self, state: Dict[str, List[str]], target: Tuple[str, Optional[Tuple[int, int]]],
               value: List[str]) -> None:
        sig, span = target
        current = state.get(sig)
        if current is None:
            current = self.signals[sig].bits
        new_bits = list(current)
        if span is None:
            width = len(current)
            value = self._extend(value, width)
            new_bits = value[:width]
        else:
            lo, hi = span
            for i in range(lo, hi + 1):
                if i - lo < len(value):
                    new_bits[i] = value[i - lo]
        state[sig] = new_bits

    def _write(self, target, value: List[str], scope: Scope) -> None:
        """Continuous assignment: pure aliasing, no logic."""
        sig, span = target
        current = self.signals[sig].bits
        if span is None:
            value = self._extend(value, len(current))
            self._alias(current, value)
        else:
            lo, hi = span
            self._alias(current[lo:hi + 1], value)

    # -- flops -----------------------------------------------------------
    def _emit_flop(self, target: str, d_bits: List[str], scope: Scope, clock: str,
                   reset_bits: Optional[List[str]]) -> None:
        self.uf.add(clock)
        width = len(self.signals[target].bits)
        q_bits = self.signals[target].bits
        d_bits = self._extend(d_bits, width)
        if reset_bits is not None and all(b in (CONST0, CONST1) for b in reset_bits):
            reset_value = sum((1 << i) for i, b in enumerate(reset_bits) if b == CONST1)
            cell_type = "DFFR"
            inputs = {"D": d_bits, "CK": [clock]}
            self.cells.append(MacroCell(
                self._next_name("DFFR", target), cell_type, width, inputs, {"Q": q_bits}))
            self.cells[-1].inputs["RST_VAL"] = [str(reset_value)]   # metadata, not a net
            # the reset signal itself becomes structurally reachable
            if self.async_resets:
                self.cells[-1].inputs["RST"] = [sorted(self.async_resets)[0]]
            return
        # a reset that is not a constant: model as a mux feeding a plain flop
        if reset_bits is not None:
            rst = sorted(self.async_resets)[0]
            sel = [rst]
            d_bits = self._new_cell("MUX2", width,
                                    {"A": d_bits, "B": self._extend(reset_bits, width),
                                     "S": sel}, scope)
        self.cells.append(MacroCell(
            self._next_name("DFF", target), "DFF", width,
            {"D": d_bits, "CK": [clock]}, {"Q": q_bits}))

    # -- expressions -----------------------------------------------------
    def _extend(self, bits: List[str], width: int) -> List[str]:
        if len(bits) >= width:
            return bits[:width]
        return bits + [CONST0] * (width - len(bits))

    def _new_cell(self, cell_type: str, width: int,
                  inputs: Dict[str, List[str]], scope: Scope) -> List[str]:
        self._cell_no += 1
        base = f"{scope.path}n{self._cell_no}"
        out = [f"{base}[{i}]" for i in range(width)] if width > 1 else [base]
        for bit in out:
            self.uf.add(bit)
        self.cells.append(MacroCell(f"{cell_type.lower()}_{self._cell_no}", cell_type,
                                    width, inputs, {"Y": out}))
        return out

    def _next_name(self, kind: str, target: str) -> str:
        self._cell_no += 1
        return f"{kind.lower()}_{self._cell_no}"

    def _or(self, a: List[str], b: List[str], scope: Scope) -> List[str]:
        return self._new_cell("OR", max(len(a), len(b)), {"A": a, "B": b}, scope)

    def _reduce(self, bits: List[str], scope: Scope) -> List[str]:
        if len(bits) == 1:
            return bits
        return self._new_cell("REDOR", 1, {"A": bits}, scope)

    def _const(self, expr, scope: Scope) -> int:
        if isinstance(expr, Num):
            return expr.value
        if isinstance(expr, Ident):
            if expr.name in scope.params:
                return scope.params[expr.name]
            raise VerilogError(f"{expr.name!r} is not a constant", expr.line, self._where)
        if isinstance(expr, Binary):
            return _apply(expr.op, self._const(expr.left, scope), self._const(expr.right, scope))
        if isinstance(expr, Unary):
            v = self._const(expr.operand, scope)
            return {"~": ~v, "-": -v, "+": v, "!": int(not v)}[expr.op]
        if isinstance(expr, Call) and expr.name == "$clog2":
            return max(1, (self._const(expr.args[0], scope) - 1).bit_length())
        if isinstance(expr, Ternary):
            return (self._const(expr.a, scope) if self._const(expr.cond, scope)
                    else self._const(expr.b, scope))
        raise VerilogError("expression is not constant", getattr(expr, "line", None), self._where)

    def _width(self, expr, scope: Scope) -> int:
        if isinstance(expr, Num):
            return expr.width if expr.width else max(1, expr.value.bit_length())
        if isinstance(expr, Ident):
            return scope.width_of(expr.name)
        if isinstance(expr, Index):
            return 1
        if isinstance(expr, Slice):
            msb = self._const(expr.msb, scope)
            lsb = self._const(expr.lsb, scope)
            return abs(msb - lsb) + 1
        if isinstance(expr, Unary):
            return 1 if expr.op in ("!", "&", "|", "^", "~&", "~|") else self._width(expr.operand, scope)
        if isinstance(expr, Binary):
            if expr.op in _COMPARE or expr.op in ("&&", "||"):
                return 1
            if expr.op in ("<<", ">>"):
                return self._width(expr.left, scope)
            return max(self._width(expr.left, scope), self._width(expr.right, scope))
        if isinstance(expr, Ternary):
            return max(self._width(expr.a, scope), self._width(expr.b, scope))
        if isinstance(expr, Concat):
            return sum(self._width(p, scope) for p in expr.parts)
        if isinstance(expr, Replicate):
            return self._const(expr.count, scope) * self._width(_rep_body(expr), scope)
        if isinstance(expr, Call) and expr.name == "$clog2":
            return max(1, self._const(expr, scope).bit_length())
        raise VerilogError(f"cannot infer width of {type(expr).__name__}")

    def _emit(self, expr, scope: Scope) -> List[str]:
        """Materialise an expression as a bit list (LSB first)."""
        if isinstance(expr, Num):
            width = expr.width if expr.width else max(1, expr.value.bit_length())
            return [CONST1 if (expr.value >> i) & 1 else CONST0 for i in range(width)]
        if isinstance(expr, Ident):
            return scope.bits_of(expr.name)
        if isinstance(expr, Index):
            base = self._emit(expr.base, scope)
            try:
                idx = self._const(expr.index, scope)
                return [base[idx]]
            except VerilogError:
                sel = self._reduce(self._emit(expr.index, scope))
                return self._mux_tree(base, sel, scope)
        if isinstance(expr, Slice):
            base = self._emit(expr.base, scope)
            msb = self._const(expr.msb, scope)
            lsb = self._const(expr.lsb, scope)
            lo, hi = min(msb, lsb), max(msb, lsb)
            return base[lo:hi + 1]
        if isinstance(expr, Unary):
            if expr.op == "!":
                bit = self._reduce(self._emit(expr.operand, scope), scope)
                return self._new_cell("NOT", 1, {"A": bit}, scope)
            if expr.op in ("&", "|", "^", "~&", "~|"):
                kind = {"&": "REDAND", "|": "REDOR", "^": "REDXOR",
                        "~&": "REDNAND", "~|": "REDNOR"}[expr.op]
                return self._new_cell(kind, 1, {"A": self._emit(expr.operand, scope)}, scope)
            if expr.op == "-":
                bits = self._emit(expr.operand, scope)
                zero = [CONST0] * len(bits)
                return self._new_cell("SUB", len(bits), {"A": zero, "B": bits}, scope)
            bits = self._emit(expr.operand, scope)
            if expr.op == "+":
                return bits
            return self._new_cell("NOT", len(bits), {"A": bits}, scope)
        if isinstance(expr, Binary):
            return self._emit_binary(expr, scope)
        if isinstance(expr, Ternary):
            sel = self._reduce(self._emit(expr.cond, scope), scope)
            a = self._emit(expr.a, scope)
            b = self._emit(expr.b, scope)
            width = max(len(a), len(b))
            return self._new_cell("MUX2", width,
                                  {"A": self._extend(b, width), "B": self._extend(a, width),
                                   "S": sel}, scope)
        if isinstance(expr, Concat):
            bits: List[str] = []
            for part in reversed(expr.parts):        # right-most part is LSB
                bits.extend(self._emit(part, scope))
            return bits
        if isinstance(expr, Replicate):
            count = self._const(expr.count, scope)
            inner = self._emit(_rep_body(expr), scope)
            out: List[str] = []
            for _ in range(count):
                out.extend(inner)
            return out
        if isinstance(expr, Call) and expr.name == "$clog2":
            value = self._const(expr, scope)
            width = max(1, value.bit_length())
            return [CONST1 if (value >> i) & 1 else CONST0 for i in range(width)]
        raise VerilogError(f"unsupported expression {type(expr).__name__}")

    def _emit_binary(self, expr: Binary, scope: Scope) -> List[str]:
        op = expr.op
        left = self._emit(expr.left, scope)
        right = self._emit(expr.right, scope)
        if op in _COMPARE:
            width = max(len(left), len(right))
            return self._new_cell(_COMPARE[op], 1,
                                  {"A": self._extend(left, width), "B": self._extend(right, width)},
                                  scope)
        if op in ("&&", "||"):
            a = self._reduce(left, scope)
            b = self._reduce(right, scope)
            return self._new_cell("AND" if op == "&&" else "OR", 1, {"A": a, "B": b}, scope)
        if op in _BITWISE:
            width = max(len(left), len(right))
            return self._new_cell(_BITWISE[op], width,
                                  {"A": self._extend(left, width), "B": self._extend(right, width)},
                                  scope)
        if op in _ARITH:
            width = max(len(left), len(right))
            if op in ("<<", ">>"):
                return self._new_cell(_ARITH[op], len(left), {"A": left, "B": right}, scope)
            return self._new_cell(_ARITH[op], width,
                                  {"A": self._extend(left, width), "B": self._extend(right, width)},
                                  scope)
        raise VerilogError(f"unsupported operator {op!r}", expr.line, self._where)

    def _mux_tree(self, bits: List[str], sel: List[str], scope: Scope) -> List[str]:
        """Select one bit from a bus with a variable index."""
        width = max(1, (len(bits) - 1).bit_length())
        sel = self._extend(sel, width)
        level = [[b] for b in bits]
        for s in sel:
            merged: List[List[str]] = []
            for i in range(0, len(level) - 1, 2):
                merged.append(self._new_cell("MUX2", 1,
                                             {"A": level[i], "B": level[i + 1], "S": [s]}, scope))
            if len(level) % 2:
                merged.append(level[-1])
            level = merged
        return level[0]

    # -- aliasing --------------------------------------------------------
    def _alias_pairs_store(self) -> List[Tuple[str, str]]:
        return []

    def _alias(self, a: List[str], b: List[str]) -> None:
        if not hasattr(self, "_alias_pairs"):
            self._alias_pairs: List[Tuple[str, str]] = []
        width = min(len(a), len(b))
        for i in range(width):
            self.uf.union(a[i], b[i])
            self._alias_pairs.append((a[i], b[i]))


def _apply(op: str, l: int, r: int) -> int:
    from .verilog import _apply_const
    return _apply_const(op, l, r)


def _check_combinational_loops(design: WordDesign) -> None:
    """Reject combinational feedback (an inferred latch or a real oscillation).

    Register outputs are graph sources, so ordinary feedback through flops is
    fine; a loop that contains no register is a latch.
    """
    uf = UnionFind()
    for a, b in design.aliases:
        uf.union(a, b)
    edges: Dict[str, List[str]] = {}
    for cell in design.cells:
        if cell.type in ("DFF", "DFFR"):
            continue
        outs = [uf.find(b) for bits in cell.outputs.values() for b in bits]
        for bits in cell.inputs.values():
            for bit in bits:
                if bit in ("1'b0", "1'b1"):
                    continue
                src = uf.find(bit)
                for out in outs:
                    edges.setdefault(src, []).append(out)
    white, grey, black = 0, 1, 2
    colour: Dict[str, int] = {}
    for start in list(edges):
        if colour.get(start, white) != white:
            continue
        stack = [(start, iter(edges.get(start, ())))]
        colour[start] = grey
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                state = colour.get(child, white)
                if state == grey:
                    raise VerilogError(
                        "combinational loop (inferred latch) through net "
                        f"{child!r}: assign the signal on every path, or use a "
                        "register")
                if state == white:
                    colour[child] = grey
                    stack.append((child, iter(edges.get(child, ()))))
                    advanced = True
                    break
            if not advanced:
                colour[node] = black
                stack.pop()


def elaborate(modules: Sequence[Module], top: str, params: Optional[Dict[str, int]] = None,
              where: str = "") -> WordDesign:
    """Elaborate ``top`` from ``modules`` into a flattened word design."""
    design = Elaborator(modules, top, params, where).run()
    _check_combinational_loops(design)
    return design
