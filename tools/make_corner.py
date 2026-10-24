#!/usr/bin/env python3
"""Generate the fast corner library from the bundled slow corner library.

The two libraries shipped in ``silicon/lib`` are synthetic, and this script is
how the second one is made: it scales the NLDM tables, transitions, constraints
and leakage of ``stdcells_32nm.lib`` (ss / 0.95 V / 125 C) into a fast corner
(ff / 1.10 V / -40 C). Keeping the generator in the repository means the corner
data is reproducible instead of being magic numbers nobody can regenerate.

    python3 tools/make_corner.py            # writes silicon/lib/stdcells_32nm_ff.lib

Nothing about a real process is implied by these numbers.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "silicon", "lib", "stdcells_32nm.lib")
TARGET = os.path.join(ROOT, "silicon", "lib", "stdcells_32nm_ff.lib")

# how a fast corner differs from the slow corner, as simple ratios
DELAY_SCALE = 0.62          # cells switch faster
TRANSITION_SCALE = 0.75     # slews are shorter too
LEAKAGE_SCALE = 2.40        # more leakage at the fast corner
CONSTRAINT_SCALE = 0.70     # setup/hold windows shrink

# which table groups hold delays, and which hold constraint windows
DELAY_GROUPS = ("cell_rise", "cell_fall", "rise_transition", "fall_transition")
CONSTRAINT_GROUPS = ("rise_constraint", "fall_constraint")

# a values() block can span several quoted lines joined by continuations, e.g.
#   values ("1, 2, 3",\
#           "4, 5, 6");
_VALUES = re.compile(r'(?P<lead>\bvalues\s*\()(?P<body>(?:"[^"]*"|[^()])*)(?P<tail>\))',
                     re.S)
_GROUP_START = re.compile(r"^\s*(?P<name>\w+)\s*\(\s*\)\s*\{", re.M)


def _scale_values(text: str, factor: float, decimals: int = 4) -> str:
    def replace(match: re.Match) -> str:
        body = match.group("body")
        numbers = re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", body)
        scaled = [f"{float(value) * factor:.{decimals}f}" for value in numbers]
        rebuilt = body
        for old, new in zip(numbers, scaled):
            rebuilt = rebuilt.replace(old, new, 1)
        return match.group("lead") + rebuilt + match.group("tail")

    return _VALUES.sub(replace, text)


def _scale_group(text: str, group: str, factor: float) -> str:
    """Scale the values() inside every ``group () { ... }`` block."""
    out = []
    index = 0
    for match in _GROUP_START.finditer(text):
        if match.group("name") != group:
            continue
        start = match.end()
        depth = 1
        cursor = start
        while cursor < len(text) and depth:
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        block = text[start:cursor]
        out.append((start, cursor, _scale_values(block, factor)))
    for start, end, replacement in reversed(out):
        text = text[:start] + replacement + text[end:]
        index += 1
    return text


def build(source: str) -> str:
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()

    for group in DELAY_GROUPS:
        text = _scale_group(text, group, DELAY_SCALE)
    for group in CONSTRAINT_GROUPS:
        text = _scale_group(text, group, CONSTRAINT_SCALE)

    text = re.sub(r"(cell_leakage_power\s*:\s*)([-+0-9.eE]+)",
                  lambda m: m.group(1) + f"{float(m.group(2)) * LEAKAGE_SCALE:.3f}",
                  text)
    text = re.sub(r"(max_transition\s*:\s*)([-+0-9.eE]+)",
                  lambda m: m.group(1) + f"{float(m.group(2)) * TRANSITION_SCALE:.4f}",
                  text)
    text = re.sub(r"library\s*\(\s*stdcells_32nm\s*\)", "library (stdcells_32nm_ff)", text)
    text = re.sub(r"(nom_voltage\s*:\s*)[-+0-9.eE]+",
                  lambda m: m.group(1) + "1.10", text)
    text = re.sub(r"(nom_temperature\s*:\s*)[-+0-9.eE]+",
                  lambda m: m.group(1) + "-40.0", text)

    header = (
        "/*\n"
        " * stdcells_32nm_ff.lib -- synthetic FAST corner of the bundled library.\n"
        " *\n"
        " * Generated from stdcells_32nm.lib by tools/make_corner.py:\n"
        f" *   delays x{DELAY_SCALE}, transitions x{TRANSITION_SCALE}, "
        f"constraints x{CONSTRAINT_SCALE}, leakage x{LEAKAGE_SCALE}\n"
        " *   corner ff / 1.10 V / -40 C\n"
        " *\n"
        " * Synthetic data. Both corners are derived from the same invented cell\n"
        " * set; they exist so the timing engine can run a real min/max (hold/ setup)\n"
        " * analysis, not because they model any silicon process.\n"
        " */\n"
    )
    # drop the original file's own header comment and prepend the new one
    body = text
    if body.lstrip().startswith("/*"):
        end = body.index("*/") + 2
        body = body[end:].lstrip("\n")
    return header + body


def main(argv: list) -> int:
    source = argv[1] if len(argv) > 1 else SOURCE
    target = argv[2] if len(argv) > 2 else TARGET
    text = build(source)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {target} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
