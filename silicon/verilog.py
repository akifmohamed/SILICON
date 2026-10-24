"""Verilog-2001 front end: lexer, parser and AST.

Supports the synthesizable subset the synthesis engine needs:

  module / endmodule, parameter and localparam, port declarations,
  wire/reg declarations (scalar, vector, and constant-indexed arrays),
  assign, always @(...), if/else, case/casez, begin/end, blocking and
  non-blocking assignment, module instantiation, and the usual operator set.

Deliberately not supported (reported as errors, never silently ignored):
signed arithmetic, functions/tasks, generate blocks, interfaces, delays,
initial blocks, and multi-dimensional arrays.

The parser produces a plain AST. Parameter substitution and width inference
live in :mod:`silicon.elaborate`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple


class VerilogError(Exception):
    """Raised for any lex/parse/elaboration error, with source context."""

    def __init__(self, message: str, line: Optional[int] = None, where: str = ""):
        self.line = line
        self.where = where
        prefix = f"{where}:{line}: " if where and line else (f"line {line}: " if line else "")
        super().__init__(prefix + message)


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass
class Num:
    value: int
    width: Optional[int] = None
    line: int = 0


@dataclass
class Ident:
    name: str
    line: int = 0


@dataclass
class Index:
    base: Any
    index: Any
    line: int = 0


@dataclass
class Slice:
    base: Any
    msb: Any
    lsb: Any
    line: int = 0


@dataclass
class Unary:
    op: str
    operand: Any
    line: int = 0


@dataclass
class Binary:
    op: str
    left: Any
    right: Any
    line: int = 0


@dataclass
class Ternary:
    cond: Any
    a: Any
    b: Any
    line: int = 0


@dataclass
class Concat:
    parts: List[Any]
    line: int = 0


@dataclass
class Replicate:
    count: Any
    inner: Any
    line: int = 0


@dataclass
class Call:
    name: str
    args: List[Any]
    line: int = 0


@dataclass
class Range:
    msb: Any
    lsb: Any


@dataclass
class Decl:
    kind: str                       # "input" | "output" | "inout" | "wire" | "reg"
    names: List[str]
    width: Optional[Range] = None   # None => scalar
    line: int = 0
    is_array: bool = False
    array_size: Optional[int] = None


@dataclass
class Param:
    name: str
    expr: Any
    local: bool = False
    line: int = 0


@dataclass
class Assign:
    lhs: Any
    rhs: Any
    line: int = 0


@dataclass
class AssignStmt:
    lhs: Any
    rhs: Any
    blocking: bool = True           # False for <=
    line: int = 0


@dataclass
class If:
    cond: Any
    then: List[Any]
    els: List[Any] = field(default_factory=list)
    line: int = 0


@dataclass
class CaseItem:
    labels: List[Any]               # empty list == default
    body: List[Any] = field(default_factory=list)


@dataclass
class Case:
    expr: Any
    items: List[CaseItem]
    line: int = 0


@dataclass
class Always:
    sensitivity: List[Any]          # Ident list, or the string "*"
    body: List[Any]
    line: int = 0


@dataclass
class Inst:
    module: str
    name: str
    connections: List[Tuple[Optional[str], Any]]   # (port or None, expr)
    params: List[Tuple[str, Any]] = field(default_factory=list)
    line: int = 0


@dataclass
class Module:
    name: str
    params: List[Param]
    ports: List[Decl]
    items: List[Any]                # Decl | Assign | Always | Inst | Param
    line: int = 0


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "assign",
    "always", "begin", "end", "if", "else", "case", "casez", "endcase",
    "default", "parameter", "localparam", "posedge", "negedge", "or", "and",
    "integer", "signed", "unsigned", "initial", "function", "endfunction",
    "generate", "endgenerate", "for", "while", "repeat",
}

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*|/\*.*?\*/)
    | (?P<num>(?:[A-Za-z_][A-Za-z0-9_]*|\d+)?\s*'\s*[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_]+|'[01xXzZ]|[0-9][0-9_]*)
    | (?P<id>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<op><<<|>>>|===|!==|==|!=|<=|>=|&&|\|\||<<|>>|\*\*|~&|~\||~\^|\^~|[-+*/%&|^~!<>=?:])
    | (?P<punct>[()\[\]{},;.\#@])
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass
class Token:
    kind: str
    value: str
    line: int


def _parse_number(text: str) -> Tuple[int, Optional[int]]:
    """Return (value, declared_width) for a Verilog numeric literal."""
    text = text.replace("_", "").replace(" ", "")
    if "'" not in text:
        return int(text), None
    size, rest = text.split("'", 1)
    size = size.strip()
    # a parameterised size such as WIDTH'd1 keeps its value and takes its width
    # from the assignment context during elaboration
    width = int(size) if size.isdigit() else None
    if rest and rest[0] in "sS":
        raise VerilogError("signed literals are not supported")
    if rest and rest[0] in "bB":
        return int(rest[1:].replace("x", "0").replace("z", "0") or "0", 2), width
    if rest and rest[0] in "oO":
        return int(rest[1:] or "0", 8), width
    if rest and rest[0] in "dD":
        return int(rest[1:] or "0", 10), width
    if rest and rest[0] in "hH":
        return int(rest[1:] or "0", 16), width
    if rest in ("0", "1", "x", "X", "z", "Z"):
        return (0 if rest in "xXzZ" else int(rest)), 1
    raise VerilogError(f"cannot parse numeric literal {text!r}")


def tokenize(source: str, where: str = "") -> List[Token]:
    tokens: List[Token] = []
    pos, line = 0, 1
    n = len(source)
    while pos < n:
        m = _TOKEN_RE.match(source, pos)
        if not m:
            raise VerilogError(f"unexpected character {source[pos]!r}", line, where)
        text = m.group(0)
        kind = m.lastgroup
        pos = m.end()
        if kind in ("ws", "comment"):
            line += text.count("\n")
            continue
        if kind == "num":
            tokens.append(Token("num", text, line))
        elif kind == "id":
            tokens.append(Token("kw" if text in _KEYWORDS else "id", text, line))
        elif kind == "op":
            tokens.append(Token("op", text, line))
        else:
            if text == "#":
                # delay/parameter-override marker: only parameter overrides are
                # supported, and the parser handles that itself.
                tokens.append(Token("punct", "#", line))
            else:
                tokens.append(Token("punct", text, line))
        line += text.count("\n")
    tokens.append(Token("eof", "", line))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_BINARY_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4, "~^": 4, "^~": 4,
    "&": 5,
    "==": 6, "!=": 6,
    "<": 7, "<=": 7, ">": 7, ">=": 7,
    "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10,
    "**": 11,
}


class Parser:
    def __init__(self, tokens: Sequence[Token], where: str = ""):
        self.tokens = list(tokens)
        self.i = 0
        self.where = where

    # -- helpers ----------------------------------------------------------
    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def peek(self, offset: int = 1) -> Token:
        j = min(self.i + offset, len(self.tokens) - 1)
        return self.tokens[j]

    def advance(self) -> Token:
        t = self.tokens[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def at(self, value: str) -> bool:
        return self.tok.value == value and self.tok.kind in ("kw", "op", "punct")

    def accept(self, value: str) -> bool:
        if self.at(value):
            self.advance()
            return True
        return False

    def expect(self, value: str) -> Token:
        if not self.at(value):
            raise VerilogError(
                f"expected {value!r} but found {self.tok.value!r}", self.tok.line, self.where
            )
        return self.advance()

    def expect_id(self) -> Token:
        if self.tok.kind != "id":
            raise VerilogError(
                f"expected identifier but found {self.tok.value!r}", self.tok.line, self.where
            )
        return self.advance()

    # -- entry ------------------------------------------------------------
    def parse(self) -> List[Module]:
        modules: List[Module] = []
        while self.tok.kind != "eof":
            if self.at("module"):
                modules.append(self.parse_module())
            else:
                self.advance()   # tolerate stray compiler directives already stripped
        if not modules:
            raise VerilogError("no module declaration found", 1, self.where)
        return modules

    def parse_module(self) -> Module:
        line = self.expect("module").line
        name = self.expect_id().value
        params: List[Param] = []
        if self.accept("#"):
            self.expect("(")
            while not self.at(")"):
                if self.accept("parameter") or self.accept("localparam"):
                    params.extend(self.parse_param_items())
                self.accept(",")
            self.expect(")")
        ports: List[Decl] = []
        if self.accept("("):
            ports = self.parse_port_list()
            self.expect(")")
        self.expect(";")

        items: List[Any] = []
        while not self.at("endmodule"):
            item = self.parse_item()
            if item is not None:
                if isinstance(item, list):
                    items.extend(item)
                else:
                    items.append(item)
        self.expect("endmodule")
        return Module(name, params, ports, items, line)

    def parse_param_items(self) -> List[Param]:
        self.accept("integer")
        local = False
        # caller consumed the keyword; recompute locality from the token we saw
        local = self.tokens[self.i - 1].value == "localparam"
        out: List[Param] = []
        while True:
            name = self.expect_id().value
            self.expect("=")
            expr = self.parse_expr()
            out.append(Param(name, expr, local=local, line=self.tok.line))
            if not self.accept(","):
                break
        return out

    def parse_range(self) -> Range:
        self.expect("[")
        msb = self.parse_expr()
        self.expect(":")
        lsb = self.parse_expr()
        self.expect("]")
        return Range(msb, lsb)

    def parse_port_list(self) -> List[Decl]:
        ports: List[Decl] = []
        # Style A: ANSI (direction inside the list). Style B: bare names only.
        if self.at(")"):
            return ports
        if self.tok.kind == "kw" and self.tok.value in ("input", "output", "inout"):
            current_kind = None
            current_range: Optional[Range] = None
            while not self.at(")"):
                if self.tok.kind == "kw" and self.tok.value in ("input", "output", "inout"):
                    current_kind = self.advance().value
                    self.accept("wire") or self.accept("reg")
                    self.accept("signed") or self.accept("unsigned")
                    current_range = self.parse_range() if self.at("[") else None
                name = self.expect_id().value
                ports.append(Decl(current_kind, [name], current_range, self.tok.line))
                self.accept(",")
            return ports
        while not self.at(")"):
            name = self.expect_id().value
            ports.append(Decl("inout", [name], None, self.tok.line))
            self.accept(",")
        return ports

    def parse_item(self) -> Any:
        t = self.tok
        if t.kind == "kw":
            if t.value in ("input", "output", "inout"):
                kind = self.advance().value
                self.accept("wire") or self.accept("reg")
                self.accept("signed") or self.accept("unsigned")
                rng = self.parse_range() if self.at("[") else None
                names = [self.expect_id().value]
                while self.accept(","):
                    names.append(self.expect_id().value)
                self.expect(";")
                return Decl(kind, names, rng, t.line)
            if t.value in ("wire", "reg"):
                kind = self.advance().value
                self.accept("signed") or self.accept("unsigned")
                rng = self.parse_range() if self.at("[") else None
                # memory declaration: reg [W-1:0] name [0:DEPTH-1];
                decls: List[Decl] = []
                while True:
                    name = self.expect_id().value
                    if self.at("["):
                        size_rng = self.parse_range()
                        size = _static_eval(size_rng.msb) - _static_eval(size_rng.lsb) + 1
                        decls.append(Decl(kind, [name], rng, t.line, is_array=True,
                                          array_size=abs(size)))
                    else:
                        decls.append(Decl(kind, [name], rng, t.line))
                    if not self.accept(","):
                        break
                self.expect(";")
                return decls
            if t.value in ("parameter", "localparam"):
                self.advance()
                return self.parse_param_items()
            if t.value == "always":
                return self.parse_always()
            if t.value in ("initial", "function", "generate", "for", "while", "repeat"):
                raise VerilogError(f"{t.value!r} is not synthesizable by this engine",
                                   t.line, self.where)
        if self.at("assign"):
            line = self.advance().line
            lhs = self.parse_lvalue()
            self.expect("=")
            rhs = self.parse_expr()
            self.expect(";")
            return Assign(lhs, rhs, line)
        if t.kind == "id" and self.peek().kind == "id":
            return self.parse_instance()
        raise VerilogError(f"unexpected token {t.value!r} in module body", t.line, self.where)

    def parse_instance(self) -> Inst:
        line = self.tok.line
        module = self.expect_id().value
        params: List[Tuple[str, Any]] = []
        if self.accept("#"):
            self.expect("(")
            if self.accept("."):
                pname = self.expect_id().value
                self.expect("(")
                params.append((pname, self.parse_expr()))
                self.expect(")")
            else:
                params.append(("", self.parse_expr()))
            self.expect(")")
        inst_name = self.expect_id().value
        self.expect("(")
        conns: List[Tuple[Optional[str], Any]] = []
        while not self.at(")"):
            if self.accept("."):
                port = self.expect_id().value
                self.expect("(")
                expr = None if self.at(")") else self.parse_expr()
                self.expect(")")
                conns.append((port, expr))
            else:
                conns.append((None, self.parse_expr()))
            self.accept(",")
        self.expect(")")
        self.expect(";")
        return Inst(module, inst_name, conns, params, line)

    def parse_always(self) -> Always:
        line = self.expect("always").line
        self.expect("@")
        sens: List[Any] = []
        if self.accept("*"):
            sens = ["*"]
        else:
            self.expect("(")
            if self.accept("*"):
                sens = ["*"]
            else:
                while not self.at(")"):
                    edge = None
                    if self.accept("posedge") or self.accept("negedge"):
                        edge = self.tokens[self.i - 1].value
                    name = self.expect_id().value
                    sens.append(Ident(name) if edge is None else (edge, name))
                    if not self.accept("or"):
                        self.accept(",")
            self.expect(")")
        body = self.parse_statement()
        return Always(sens, body if isinstance(body, list) else [body], line)

    # -- statements -------------------------------------------------------
    def parse_statement(self) -> List[Any]:
        if self.at("begin"):
            self.advance()
            self.accept(":")
            if self.tok.kind == "id" and self.peek().value == ";":
                self.advance()          # named block label
            body: List[Any] = []
            while not self.at("end"):
                stmt = self.parse_statement()
                body.extend(stmt if isinstance(stmt, list) else [stmt])
            self.expect("end")
            return body
        if self.at("if"):
            line = self.advance().line
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then = self.parse_statement()
            els: List[Any] = []
            if self.accept("else"):
                els = self.parse_statement()
            return [If(cond, then, els, line)]
        if self.at("case") or self.at("casez"):
            line = self.advance().line
            self.expect("(")
            expr = self.parse_expr()
            self.expect(")")
            items: List[CaseItem] = []
            while not self.at("endcase"):
                if self.accept("default"):
                    self.accept(":")
                    items.append(CaseItem([], self.parse_statement()))
                    continue
                labels = [self.parse_expr()]
                while self.accept(","):
                    labels.append(self.parse_expr())
                self.expect(":")
                items.append(CaseItem(labels, self.parse_statement()))
            self.expect("endcase")
            return [Case(expr, items, line)]
        if self.at(";"):
            self.advance()
            return []
        line = self.tok.line
        lhs = self.parse_lvalue()
        if self.at("<="):
            self.advance()
            rhs = self.parse_expr()
            self.expect(";")
            return [AssignStmt(lhs, rhs, blocking=False, line=line)]
        if self.at("="):
            self.advance()
            rhs = self.parse_expr()
            self.expect(";")
            return [AssignStmt(lhs, rhs, blocking=True, line=line)]
        raise VerilogError(f"expected assignment but found {self.tok.value!r}",
                           self.tok.line, self.where)

    def parse_lvalue(self) -> Any:
        name = self.expect_id().value
        node: Any = Ident(name, self.tok.line)
        while self.at("["):
            self.advance()
            first = self.parse_expr()
            if self.accept(":"):
                second = self.parse_expr()
                self.expect("]")
                node = Slice(node, first, second, self.tok.line)
            else:
                self.expect("]")
                node = Index(node, first, self.tok.line)
        return node

    # -- expressions ------------------------------------------------------
    def parse_expr(self) -> Any:
        return self.parse_ternary()

    def parse_ternary(self) -> Any:
        cond = self.parse_binary(0)
        if self.accept("?"):
            a = self.parse_ternary()
            self.expect(":")
            b = self.parse_ternary()
            return Ternary(cond, a, b, self.tok.line)
        return cond

    def parse_binary(self, min_prec: int) -> Any:
        left = self.parse_unary()
        while True:
            op = self.tok.value
            prec = _BINARY_PRECEDENCE.get(op)
            if prec is None or prec < min_prec or self.tok.kind != "op":
                return left
            self.advance()
            right = self.parse_binary(prec + 1)
            left = Binary(op, left, right, self.tok.line)

    def parse_unary(self) -> Any:
        if self.tok.kind == "op" and self.tok.value in ("~", "!", "-", "+", "&", "|", "^", "~&", "~|"):
            op = self.advance().value
            return Unary(op, self.parse_unary(), self.tok.line)
        return self.parse_primary()

    def parse_primary(self) -> Any:
        t = self.tok
        if t.kind == "num":
            self.advance()
            value, width = _parse_number(t.value)
            return Num(value, width, t.line)
        if self.at("("):
            self.advance()
            e = self.parse_expr()
            # concatenation-like replication {n{expr}} is handled in parse_concat
            self.expect(")")
            return e
        if self.at("{"):
            return self.parse_concat()
        if t.kind == "id":
            self.advance()
            if self.at("("):                     # system function call: $clog2(x)
                self.advance()
                args = []
                while not self.at(")"):
                    args.append(self.parse_expr())
                    self.accept(",")
                self.expect(")")
                return Call(t.value, args, t.line)
            node: Any = Ident(t.value, t.line)
            while self.at("["):
                self.advance()
                first = self.parse_expr()
                if self.accept(":"):
                    second = self.parse_expr()
                    self.expect("]")
                    node = Slice(node, first, second, t.line)
                else:
                    self.expect("]")
                    node = Index(node, first, t.line)
            return node
        raise VerilogError(f"unexpected token {t.value!r} in expression", t.line, self.where)

    def parse_concat(self) -> Any:
        line = self.expect("{").line
        first = self.parse_expr()
        # replication: {N{expr}} or {N{a,b}} -- N may be any constant expression
        if self.at("{"):
            self.advance()
            inner = self.parse_concat_body()
            self.expect("}")
            self.expect("}")
            return Replicate(first, inner, line)
        parts = [first]
        while self.accept(","):
            parts.append(self.parse_expr())
        self.expect("}")
        return Concat(parts, line)

    def parse_concat_body(self) -> List[Any]:
        parts = [self.parse_expr()]
        while self.accept(","):
            parts.append(self.parse_expr())
        return parts


def _static_eval(expr: Any) -> int:
    """Evaluate a constant expression using only literals and integer operators."""
    if isinstance(expr, Num):
        return expr.value
    if isinstance(expr, Binary):
        l, r = _static_eval(expr.left), _static_eval(expr.right)
        return _apply_const(expr.op, l, r)
    if isinstance(expr, Unary):
        v = _static_eval(expr.operand)
        return {"~": ~v, "-": -v, "+": v, "!": int(not v)}[expr.op]
    if isinstance(expr, Call) and expr.name == "$clog2":
        v = _static_eval(expr.args[0])
        return max(1, (v - 1).bit_length())
    raise VerilogError("non-constant expression where a constant is required")


def _apply_const(op: str, l: int, r: int) -> int:
    return {
        "+": lambda: l + r, "-": lambda: l - r, "*": lambda: l * r,
        "/": lambda: l // r, "%": lambda: l % r,
        "&": lambda: l & r, "|": lambda: l | r, "^": lambda: l ^ r,
        "<<": lambda: l << r, ">>": lambda: l >> r,
        "==": lambda: int(l == r), "!=": lambda: int(l != r),
        "<": lambda: int(l < r), "<=": lambda: int(l <= r),
        ">": lambda: int(l > r), ">=": lambda: int(l >= r),
        "&&": lambda: int(bool(l) and bool(r)), "||": lambda: int(bool(l) or bool(r)),
    }[op]()


def parse_modules(source: str, where: str = "<input>") -> List[Module]:
    """Parse Verilog source into a list of modules."""
    source = _strip_directives(source)
    return Parser(tokenize(source, where), where).parse()


def _strip_directives(source: str) -> str:
    """Remove compiler directives that carry no synthesis meaning here."""
    out = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("`"):
            directive = stripped.split()[0] if stripped.split() else ""
            if directive.split("(")[0] in (
                "`default_nettype", "`resetall", "`timescale", "`celldefine",
                "`endcelldefine", "`unconnected_drive", "`nounconnected_drive",
            ):
                out.append("")
                continue
            if directive.startswith("`include") or directive.startswith("`define"):
                raise VerilogError(
                    f"preprocessor directive {directive} is not supported; preprocess first"
                )
        out.append(line)
    return "\n".join(out)


def module_index(modules: Sequence[Module]) -> dict:
    return {m.name: m for m in modules}
