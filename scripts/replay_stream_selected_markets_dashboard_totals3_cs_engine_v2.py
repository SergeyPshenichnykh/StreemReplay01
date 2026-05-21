#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\].*?(?:\x07|\x1b\\)")
TOTAL_RE = re.compile(r"\d+\.\d+\s+OVER_UNDER_\d+")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s).replace("\r", "")


def visible_len(s: str) -> int:
    return len(strip_ansi(s))


def ansi_slice(raw: str, start: int, end: int | None) -> str:
    out = []
    v = 0
    i = 0

    while i < len(raw):
        m = ANSI_RE.match(raw, i)
        if m:
            if v >= start and (end is None or v < end):
                out.append(m.group(0))
            i = m.end()
            continue

        ch = raw[i]
        if v >= start and (end is None or v < end):
            out.append(ch)

        v += 1
        i += 1

        if end is not None and v >= end:
            while i < len(raw):
                m = ANSI_RE.match(raw, i)
                if not m:
                    break
                out.append(m.group(0))
                i = m.end()
            break

    return "".join(out).rstrip()


def ansi_ljust(s: str, width: int) -> str:
    return s + (" " * max(0, width - visible_len(s)))


def is_sep(line: str) -> bool:
    s = strip_ansi(line).strip()
    return len(s) >= 20 and set(s) == {"-"}


def run(cmd: list[str]) -> str:
    env = os.environ.copy()
    env["COLUMNS"] = "320"
    env["TERM"] = "xterm-256color"
    env.pop("NO_COLOR", None)
    env.pop("PY_COLORS", None)

    p = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        sys.stderr.write(p.stdout)
        raise SystemExit(p.returncode)

    return p.stdout.rstrip("\n")


def header_through_first_sep(raw: str) -> str:
    lines = raw.splitlines()
    clean = [strip_ansi(x) for x in lines]

    for i, line in enumerate(clean):
        if is_sep(line):
            return "\n".join(lines[: i + 1]).rstrip()

    return "\n".join(lines[:3]).rstrip()


def section_after_first_sep_before_cs(raw: str) -> list[tuple[str, str]]:
    raw_lines = raw.splitlines()
    clean_lines = [strip_ansi(x) for x in raw_lines]

    start = 0
    for i, line in enumerate(clean_lines):
        if is_sep(line):
            start = i + 1
            break

    end = len(clean_lines)
    for i in range(start, len(clean_lines)):
        if "CORRECT_SCORE" in clean_lines[i]:
            end = i
            break

    while end > start and is_sep(clean_lines[end - 1]):
        end -= 1

    return list(zip(raw_lines[start:end], clean_lines[start:end]))


def split_blocks(lines: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    blocks = []
    cur = []

    for raw, clean in lines:
        if not clean.strip():
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append((raw, clean))

    if cur:
        blocks.append(cur)

    return blocks


def extract_total_columns(lines: list[tuple[str, str]]) -> list[list[str]]:
    cols: list[list[str]] = []

    for block in split_blocks(lines):
        header_idx = None

        for i, (_, clean) in enumerate(block):
            if "OVER_UNDER_" in clean:
                header_idx = i
                break

        if header_idx is None:
            continue

        clean_header = block[header_idx][1]
        starts = [m.start() for m in TOTAL_RE.finditer(clean_header)]

        if not starts:
            continue

        if len(starts) >= 2:
            step = min(b - a for a, b in zip(starts, starts[1:]))
        else:
            step = 64

        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else start + step

            col = []
            for raw, _clean in block[header_idx:]:
                col.append(ansi_slice(raw, start, end))

            text = strip_ansi("\n".join(col))

            if "OVER_UNDER_85" in text or "Over/Under 8.5 Goals" in text:
                continue

            if "OVER_UNDER_" in text:
                cols.append(col)

    if len(cols) != 8:
        raise SystemExit(f"ERROR: expected 8 OVER_UNDER columns without 8.5, got {len(cols)}")

    return cols

def render_totals_2_rows(cols: list[list[str]]) -> str:
    out = []

    for group in (cols[:4], cols[4:8]):
        height = max(len(col) for col in group)
        widths = [max(visible_len(x) for x in col) for col in group]

        for r in range(height):
            row = []
            for col, width in zip(group, widths):
                cell = col[r] if r < len(col) else ""
                row.append(ansi_ljust(cell, width))
            out.append("   ".join(row).rstrip())

        out.append("")

    return "\n".join(out).rstrip()


def correct_score_ladder_only(raw: str) -> str:
    lines = raw.splitlines()
    clean = [strip_ansi(x) for x in lines]

    cs_idx = None
    for i, line in enumerate(clean):
        if "CORRECT_SCORE" in line:
            cs_idx = i
            break

    if cs_idx is None:
        raise SystemExit("ERROR: CORRECT_SCORE section not found")

    start = cs_idx
    while start > 0 and is_sep(clean[start - 1]):
        start -= 1

    return "\n".join(lines[start:]).rstrip()


def main() -> int:
    totals_cmd = [
        PY,
        str(ROOT / "scripts" / "replay_stream_selected_markets_dashboard_sticky_totals_engine_v2.py"),
        "--discover-targets",
        "--start-minutes-before", "10",
        "--list-totals-ladder",
        "--self-check",
        "--no-snapshots-csv",
        "--ladder",
        "--ladder-max-rows", "30",
        "--ticks-above", "30",
        "--ticks-below", "30",
        "--col-width", "52",
        "--cs-cols", "3",
        "--delay", "0",
        "--balance", "1000",
        "--max-frames", "1",
        "--no-clear",
    ]

    cs_cmd = [
        PY,
        str(ROOT / "scripts" / "replay_stream_selected_markets_dashboard_engine_v2.py"),
        "--discover-targets",
        "--start-minutes-before", "10",
        "--ladder",
        "--self-check",
        "--engine-v2-overlay",
        "--show-queue",
        "--no-snapshots-csv",
        "--balance", "1000",
        "--ladder-max-rows", "30",
        "--ticks-above", "30",
        "--ticks-below", "30",
        "--col-width", "52",
        "--cs-cols", "3",
        "--max-frames", "1",
        "--no-clear",
    ]

    totals_out = run(totals_cmd)
    cs_out = run(cs_cmd)

    total_cols = extract_total_columns(section_after_first_sep_before_cs(totals_out))

    print(header_through_first_sep(cs_out))
    print(render_totals_2_rows(total_cols))
    print(correct_score_ladder_only(cs_out))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
