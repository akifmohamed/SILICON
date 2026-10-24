"""Shared command-shell plumbing for ``dc_shell`` and ``icc2_shell``.

The shells read the same Tcl-ish command language the commercial tools use:
``command -option value file``, ``;`` separated statements, ``#`` comments,
``set VAR value`` and ``$VAR`` expansion. This is not a Tcl interpreter -- it is
a command loop with Tcl-shaped syntax, which is what scripts written for these
tools look like.
"""

from __future__ import annotations

import shlex
import sys
from typing import Callable, Dict, List, Optional, Sequence, TextIO


class ShellError(Exception):
    pass


class Command:
    def __init__(self, name: str, handler: Callable[..., object],
                 usage: str, summary: str, aliases: Sequence[str] = ()) -> None:
        self.name = name
        self.handler = handler
        self.usage = usage
        self.summary = summary
        self.aliases = tuple(aliases)


class Shell:
    prompt = "shell> "

    def __init__(self, name: str, out: Optional[TextIO] = None) -> None:
        self.name = name
        self.out = out or sys.stdout
        self.commands: Dict[str, Command] = {}
        self.variables: Dict[str, str] = {}
        self.history: List[str] = []
        self.colors = self.out.isatty()

    # -- registration -----------------------------------------------------
    def register(self, name: str, usage: str, summary: str,
                 aliases: Sequence[str] = ()) -> Callable:
        def decorator(fn: Callable) -> Callable:
            command = Command(name, fn, usage, summary, aliases)
            self.commands[name] = command
            for alias in aliases:
                self.commands[alias] = command
            return fn
        return decorator

    # -- io ---------------------------------------------------------------
    def write(self, text: str = "") -> None:
        print(text, file=self.out)
        self.out.flush()

    def error(self, text: str) -> None:
        print(f"Error: {text}", file=self.out)
        self.out.flush()

    def warn(self, text: str) -> None:
        print(f"Warning: {text}", file=self.out)
        self.out.flush()

    # -- execution --------------------------------------------------------
    def execute_line(self, line: str) -> bool:
        """Run one line. Returns False when the shell should exit."""
        for statement in _split_statements(line):
            if not self._execute_statement(statement):
                return False
        return True

    def _execute_statement(self, statement: str) -> bool:
        try:
            tokens = _merge_braces(shlex.split(statement, posix=True))
        except ValueError as exc:
            self.error(f"cannot parse command: {exc}")
            return True
        if not tokens:
            return True
        tokens = [self._expand(tok) for tok in tokens]
        self.history.append(statement)
        name = tokens[0]
        if name in ("quit", "exit"):
            return False
        if name == "set":
            if len(tokens) < 3:
                self.error("usage: set <variable> <value>")
                return True
            self.variables[tokens[1]] = " ".join(tokens[2:])
            return True
        if name in ("echo", "puts"):
            self.write(" ".join(tokens[1:]))
            return True
        command = self.commands.get(name)
        if command is None:
            suggestion = self._suggest(name)
            self.error(f"unknown command {name!r}" + (f" (did you mean {suggestion!r}?)"
                                                      if suggestion else ""))
            return True
        try:
            result = command.handler(tokens[1:])
        except ShellError as exc:
            self.error(str(exc))
            return True
        except Exception as exc:                       # tool errors surface as text
            self.error(f"{type(exc).__name__}: {exc}")
            return True
        if isinstance(result, str) and result:
            self.write(result)
        return True

    def _expand(self, token: str) -> str:
        if not token.startswith("$"):
            return token
        key = token[1:].strip("{}")
        return self.variables.get(key, "")

    def _suggest(self, name: str) -> Optional[str]:
        import difflib
        matches = difflib.get_close_matches(name, list(self.commands), n=1, cutoff=0.6)
        return matches[0] if matches else None

    # -- entry points -----------------------------------------------------
    def run_script_file(self, path: str) -> int:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                script = handle.read()
        except OSError as exc:
            self.error(f"cannot read script {path}: {exc}")
            return 2
        return self.run_script(script)

    def run_script(self, script: str) -> int:
        """Execute a script, honouring line continuations and block comments."""
        script = _strip_block_comments(script)
        buffer = ""
        for raw in script.splitlines():
            line = raw.rstrip()
            if line.endswith("\\"):
                buffer += line[:-1] + " "
                continue
            buffer += line
            if not buffer.strip():
                buffer = ""
                continue
            if not self.execute_line(buffer):
                return 0
            if self._fatal:
                return 1
            buffer = ""
        if buffer.strip():
            self.execute_line(buffer)
        return 1 if self._fatal else 0

    _fatal = False

    def interact(self) -> int:
        self.write(f"{self.name} (silicon 0.1) -- type 'help' for commands, 'quit' to exit")
        while True:
            try:
                line = input(self.prompt)
            except EOFError:
                self.write("")
                return 0
            except KeyboardInterrupt:
                self.write("^C")
                continue
            if not self.execute_line(line):
                return 0

    # -- help -------------------------------------------------------------
    def help_text(self, command: Optional[str] = None) -> str:
        if command:
            cmd = self.commands.get(command)
            if cmd is None:
                return f"unknown command {command!r}"
            return f"{cmd.name}\n    usage  : {cmd.usage}\n    summary: {cmd.summary}"
        seen = set()
        lines = [f"{self.name} commands:", ""]
        for name in sorted(self.commands):
            cmd = self.commands[name]
            if cmd.name in seen or name != cmd.name:
                continue
            seen.add(cmd.name)
            lines.append(f"  {cmd.usage:<58}{cmd.summary}")
        return "\n".join(lines)


def _split_statements(line: str) -> List[str]:
    parts: List[str] = []
    current = ""
    quote: Optional[str] = None
    for ch in line:
        if quote:
            current += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current += ch
            continue
        if ch == "#":
            break
        if ch == ";":
            parts.append(current)
            current = ""
            continue
        current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def _strip_block_comments(script: str) -> str:
    out: List[str] = []
    in_comment = False
    for line in script.splitlines():
        if in_comment:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_comment = False
            else:
                out.append("")
                continue
        if "/*" in line and "*/" not in line.split("/*", 1)[1]:
            line = line.split("/*", 1)[0]
            in_comment = True
        out.append(line)
    return "\n".join(out)


def _merge_braces(tokens: Sequence[str]) -> List[str]:
    """Re-join ``{ ... }`` groups that ``shlex`` split on whitespace.

    Tcl passes ``{3 3}`` as a single word; shlex gives ``["{3", "3}"]``. Merging
    keeps option parsing identical to how the command language is documented.
    """
    merged: List[str] = []
    pending: List[str] = []
    for token in tokens:
        if pending:
            pending.append(token)
            if "}" in token:
                merged.append(" ".join(pending))
                pending = []
            continue
        if token.startswith("{") and token.count("{") > token.count("}"):
            pending = [token]
            continue
        merged.append(token)
    if pending:
        merged.extend(pending)
    return merged


def parse_options(args: Sequence[str], flags: Sequence[str] = (),
                  options: Sequence[str] = ()) -> tuple:
    """Split ``args`` into (flags, options, positionals).

    ``flags`` are boolean switches, ``options`` take one value each. Unknown
    ``-something`` tokens are reported rather than silently dropped, because a
    typo in a synthesis script should never change the result quietly.
    """
    found_flags = set()
    found_options: Dict[str, str] = {}
    pos: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("-") and tok != "-":
            name = tok.lstrip("-")
            if name in flags:
                found_flags.add(name)
                i += 1
                continue
            if name in options:
                if i + 1 >= len(args):
                    raise ShellError(f"option -{name} needs a value")
                found_options[name] = args[i + 1]
                i += 2
                continue
            raise ShellError(f"unknown option {tok!r}")
        pos.append(tok)
        i += 1
    return found_flags, found_options, pos
