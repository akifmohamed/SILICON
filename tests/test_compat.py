"""Syntax and reproducibility guards the CI would otherwise catch for us."""

import io
import os
import pathlib
import subprocess
import sys
import tokenize

import pytest

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sources():
    for pattern in ("silicon/*.py", "tests/*.py", "tools/*.py"):
        yield from sorted(ROOT.glob(pattern))


@pytest.mark.skipif(not hasattr(tokenize, "FSTRING_START"),
                    reason="tokenizer cannot see inside f-strings before 3.12")
def test_no_fstring_syntax_that_needs_python_312():
    """Single-quoted f-strings must not span lines, and a replacement field may
    not contain a backslash or a quote of the same kind as its literal.

    All of that became legal in Python 3.12 (PEP 701) and is a SyntaxError on
    3.9-3.11, which the project supports. A nested-quote f-string shipped once
    and only CI on the older interpreters caught it.
    """
    problems = []
    for path in _sources():
        tokens = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
        stack = []
        for tok in tokens:
            if tok.type == tokenize.FSTRING_START:
                text = tok.string
                triple = text.endswith('"""') or text.endswith("'''")
                stack.append((tok.start[0], text, triple))
            elif tok.type == tokenize.FSTRING_END:
                row, text, triple = stack.pop()
                if not triple and tok.end[0] != row:
                    problems.append(f"{path.name}:{row} single-quoted f-string "
                                    f"spans lines")
            elif stack and not stack[-1][2]:
                quote = stack[-1][1][-1]
                if tok.type == tokenize.STRING and quote in tok.string:
                    problems.append(f"{path.name}:{tok.start[0]} nested quote "
                                    f"inside an f-string field")
    assert not problems, "\n".join(problems)


def test_reports_honour_source_date_epoch(monkeypatch):
    from silicon import reports

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = reports.timestamp()
    assert "Nov" in first and "2023" in first          # 2023-11-14 UTC
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1600000000")
    assert reports.timestamp() != first
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "not-a-number")
    assert reports.timestamp()                            # falls back, never raises


def _flow(out: pathlib.Path, seed: str) -> None:
    env = dict(os.environ, PYTHONHASHSEED=seed, SOURCE_DATE_EPOCH="1700000000")
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "silicon"), "flow",
         "-r", str(ROOT / "examples" / "rtl" / "counter.v"), "-t", "counter",
         "--period", "4", "-o", str(out)],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_whole_flow_is_byte_identical_across_hash_seeds(tmp_path):
    """Every artefact, including the reports, must not depend on hash order."""
    first, second = tmp_path / "a", tmp_path / "b"
    _flow(first, "1")
    _flow(second, "7")
    names = sorted(p.name for p in first.iterdir())
    assert names == sorted(p.name for p in second.iterdir())
    assert "counter.sdf" in names and "counter.spef" in names
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
