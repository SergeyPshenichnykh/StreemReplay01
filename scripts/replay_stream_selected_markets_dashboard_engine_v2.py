#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import shutil
import sys
import time
import contextlib
import os
import select
import termios
import tty
import fcntl
import copy
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.dutching import calc_dutching  # noqa: E402
from bot.order_model import OrderModel  # noqa: E402
from bot.order_model import MyOrdersAtPrice  # noqa: E402

from replay_stream_match_odds_correct import (
    DEFAULT_REPLAY_FILE,
    DEFAULT_DELAY_SECONDS,
    FeatureContext,
    RunnerState,
    apply_market_definition,
    apply_runner_change,
    datetime_to_pt,
    format_pt,
    parse_market_time,
)
from replay_stream_selected_markets_features import (
    DEFAULT_TARGET_MARKETS_FILE,
    MarketState,
    ensure_market,
    is_target_market_type,
    parse_target_markets_file,
    update_market_metadata,
)


SIMULATE_ORDERS_ENABLED = False
MAKER_UNDER_LAY_GRID_ENABLED = False
MAKER_UNDER_LAY_GRID_MATCHING_ENABLED = False
MAKER_UNDER_LAY_GRID_MATCHED_TOTAL = 0.0
MAKER_UNDER_LAY_GRID_LIABILITY_TOTAL = 0.0
MAKER_UNDER_LAY_GRID_LAST_PT: int | None = None
MAKER_UNDER_LAY_GRID_APPLY_INDEX = 0
MAKER_UNDER_LAY_GRID_ORDER_STATE: dict[tuple[str, int, float, float], dict[str, float | str]] = {}
MAKER_UNDER_LAY_GRID_PLACED_RUNNERS: set[tuple[str, int, float]] = set()
MAKER_UNDER_LAY_GRID_ACTIVE_ORDERS = 0

MAKER_UNDER_LAY_GRID_STAKES: dict[float, float] = {
    1.25: 5.00,
    1.24: 5.50,
    1.23: 6.00,
    1.22: 6.50,
    1.21: 7.00,
    1.20: 7.50,
    1.19: 8.00,
    1.18: 8.50,
    1.17: 9.00,
    1.16: 9.50,
    1.15: 10.00,
    1.14: 10.50,
    1.13: 11.00,
    1.12: 11.50,
    1.11: 12.00,
    1.10: 12.50,
    1.09: 13.00,
    1.08: 13.50,
    1.07: 14.00,
    1.06: 14.50,
    1.05: 15.00,
    1.04: 15.50,
    1.03: 16.00,
    1.02: 16.50,
    1.01: 17.00,
}

DEFAULT_SNAPSHOTS_CSV = Path("replay/selected_markets_250ms.csv")


def clear_once() -> None:
    print("\033[2J", end="")


def move_top() -> None:
    print("\033[H", end="")

def clear_from_top() -> None:
    # Clear screen from cursor to end, then home.
    print("\033[H\033[J", end="")


def _print_header_lines(
    *,
    frame_index: int,
    cadence_ms: int,
    pt: int,
    utc: str,
    selected_markets: int,
    dedup_markets: int,
    showing: int,
    balance: float | None,
    paused: bool = False,
    err: str | None = None,
    key: str | None = None,
) -> None:
    cols = _terminal_cols()
    bal_txt = "-" if balance is None else f"{balance:.2f}"
    line1 = f"BALANCE: {bal_txt}"
    pause_txt = "  PAUSED" if paused else ""
    tags: list[str] = []
    if key:
        tags.append("KEY=" + _truncate_visible(repr(key), 12))
    if err:
        tags.append("ERR=" + _truncate_visible(str(err), 40))
    err_txt = ("  " + "  ".join(tags)) if tags else ""
    line2 = (
        f"FRAME: {frame_index}  cadence_ms={cadence_ms}  "
        f"PT: {pt}  UTC: {utc}  selected_markets={selected_markets}  dedup_markets={dedup_markets}  showing={showing}{pause_txt}{err_txt}"
    )
    engine_line = globals().get("ENGINE_V2_OVERLAY_LINE", "")
    engine_tape = globals().get("ENGINE_V2_TAPE_LINE", "")
    lines = [line1, line2]
    if engine_line:
        lines.append(str(engine_line))
    if engine_tape:
        lines.append(str(engine_tape))

    for line in lines:
        vis = _strip_ansi(line)
        if len(vis) < cols:
            line = line + (" " * (cols - len(vis)))
        print(line)


def _alt_screen_enter() -> None:
    # Alternate screen buffer + hide cursor.
    print("\033[?1049h\033[?25l", end="")


def _alt_screen_exit() -> None:
    # Show cursor + leave alternate buffer.
    print("\033[?25h\033[?1049l", end="")


_SMOOTH_LAST_LINES: list[str] = []


def _smooth_repaint(lines: list[str]) -> None:
    """
    Stable viewport repaint for Windows Terminal / WSL.

    Important:
    - never print long frames using raw newlines;
    - address each visible terminal row explicitly;
    - truncate to terminal width to prevent wrapping;
    - draw only the visible viewport height to prevent scroll.
    """
    global _SMOOTH_LAST_LINES

    size = shutil.get_terminal_size(fallback=(160, 45))
    cols = max(20, int(size.columns))
    rows = max(5, int(size.lines) - 1)

    visible = lines[:rows]
    clear_to = max(len(_SMOOTH_LAST_LINES), len(visible), rows)

    out: list[str] = ["\033[?25l"]

    for idx in range(min(clear_to, rows)):
        line = visible[idx] if idx < len(visible) else ""

        vis = _strip_ansi(line)
        if len(vis) >= cols:
            line = _truncate_visible(line, cols - 1)
        else:
            line = line + (" " * max(0, cols - len(vis) - 1))

        out.append(f"\033[{idx + 1};1H")
        out.append(line)
        out.append("\033[K")

    out.append(f"\033[{min(len(visible) + 1, rows)};1H")
    out.append("\033[J")

    data = "".join(out)

    for _ in range(5):
        try:
            sys.stdout.write(data)
            sys.stdout.flush()
            break
        except BlockingIOError:
            time.sleep(0.001)

    _SMOOTH_LAST_LINES = visible


def fmt_num(value: float | None, width: int = 9, decimals: int = 2) -> str:
    if value is None:
        return f"{'-':>{width}}"
    return f"{value:>{width}.{decimals}f}"


def fmt_text(value: str | None, width: int) -> str:
    s = "-" if value is None else str(value)
    if len(s) > width:
        s = s[: width - 1] + "…"
    return f"{s:<{width}}"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _truncate_visible(s: str, width: int) -> str:
    """Truncate string to visible width, preserving ANSI sequences (best-effort)."""
    if width <= 0:
        return ""
    plain = _strip_ansi(s)
    if len(plain) <= width:
        return s
    out: list[str] = []
    visible = 0
    i = 0
    while i < len(s) and visible < max(0, width - 1):
        ch = s[i]
        if ch == "\x1b":
            m = _ANSI_RE.match(s, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        out.append(ch)
        visible += 1
        i += 1
    if visible < len(plain):
        out.append("…")
    out.append("\033[0m")
    return "".join(out)


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _fmt_sz(value: float | None, width: int = 6) -> str:
    if value is None:
        return f"{'':>{width}}"
    if value >= 1000:
        s = f"{int(round(value)):d}"
    elif value >= 100:
        s = f"{value:.0f}"
    elif value >= 10:
        s = f"{value:.1f}"
    else:
        s = f"{value:.2f}"
    if len(s) > width:
        s = s[:width]
    return f"{s:>{width}}"

def _fmt_price_cell(price: float) -> str:
    # Fairbot-like compact formatting: drop trailing zeros where possible.
    if price < 10:
        s = f"{price:.2f}".rstrip("0").rstrip(".")
    elif price < 100:
        s = f"{price:.1f}".rstrip("0").rstrip(".")
    else:
        s = f"{price:.0f}"
    return f"{s:>4}"


def best_level(book: dict[float, float], *, side: str) -> tuple[float, float] | None:
    if not book:
        return None
    if side == "BACK":
        price = max(book)
    elif side == "LAY":
        price = min(book)
    else:
        raise ValueError(side)
    return float(price), float(book[price])


def calc_margin_pct_from_best_lay(runners: dict[tuple[int, float | None], RunnerState]) -> float | None:
    inv_sum = 0.0
    legs = 0
    for r in runners.values():
        if r.status not in (None, "ACTIVE"):
            continue
        best = best_level(r.available_to_lay, side="LAY")
        if best is None:
            continue
        price, size = best
        if not (1.01 <= price <= 1000.0) or not math.isfinite(price):
            continue
        if size <= 0 or not math.isfinite(size):
            continue
        inv_sum += 1.0 / price
        legs += 1
    if legs < 2 or inv_sum <= 0:
        return None
    margin = (1.0 / inv_sum) - 1.0
    return margin * 100.0


_RE_OU_NAME = re.compile(r"Over/Under\s+(\d+(?:\.\d+)?)\s+Goals", re.IGNORECASE)
_RE_SCORE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")


def is_match_odds(state: MarketState) -> bool:
    return (state.market_type or "") == "MATCH_ODDS" or (state.market_name or "").strip().lower() == "match odds"


def is_correct_score(state: MarketState) -> bool:
    return (state.market_type or "") == "CORRECT_SCORE" or (state.market_name or "").strip().lower() == "correct score"


def is_over_under(state: MarketState | None) -> bool:
    if state is None:
        return False
    mt = state.market_type or ""
    if mt.startswith("OVER_UNDER_"):
        return True
    return _RE_OU_NAME.search(state.market_name or "") is not None


def is_over_under_goals(state: MarketState | None) -> bool:
    """True for Goals over/under markets (excludes Corners etc)."""
    if state is None:
        return False
    name = (state.market_name or "").lower()
    if "over/under" in name and "goals" in name:
        return True
    mt = state.market_type or ""
    # Typical goals types are like OVER_UNDER_25, OVER_UNDER_45, etc.
    if mt.startswith("OVER_UNDER_"):
        suffix = mt[len("OVER_UNDER_") :]
        return suffix.isdigit()
    return False


def over_under_line(state: MarketState | None) -> float | None:
    if state is None:
        return None
    mt = state.market_type or ""
    if mt.startswith("OVER_UNDER_"):
        suffix = mt[len("OVER_UNDER_") :]
        # common forms: "25" for 2.5, "15" for 1.5
        if suffix.isdigit():
            return float(suffix) / 10.0
    m = _RE_OU_NAME.search(state.market_name or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def find_under_runner(state: MarketState) -> RunnerState | None:
    for r in state.runners.values():
        name = (r.name or "").lower()
        if name.startswith("under") or " under " in name:
            return r
    # fallback: sometimes runner names are just selection ids; no under runner then
    return None


def is_under_lay_in_range(state: MarketState, low: float, high: float) -> tuple[bool, float | None]:
    under = find_under_runner(state)
    if under is None:
        return False, None
    best = best_level(under.available_to_lay, side="LAY")
    if best is None:
        return False, None
    price, _size = best
    if not math.isfinite(price):
        return False, None
    return (low <= price <= high), price


def wanted_scorelines_from_ou_lines(lines: list[float]) -> set[tuple[int, int]]:
    wanted: set[tuple[int, int]] = set()
    for line in lines:
        # Under X.5 => total goals <= floor(X.5) == int(X)
        max_goals = int(math.floor(line + 1e-9))
        for home in range(max_goals + 1):
            for away in range(max_goals + 1):
                if home + away <= max_goals:
                    wanted.add((home, away))
    return wanted


def score_from_runner_name(name: str | None) -> tuple[int, int] | None:
    if not name:
        return None
    m = _RE_SCORE.match(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Multi-market dashboard (top-N by dutching margin) from Betfair historical stream. "
            "Renders at fixed 250ms cadence and can write snapshots CSV in parallel."
        )
    )
    p.add_argument("--replay-file", type=Path, default=DEFAULT_REPLAY_FILE)
    p.add_argument("--target-markets-file", type=Path, default=DEFAULT_TARGET_MARKETS_FILE)
    p.add_argument("--market-id", action="append", default=[], help="Explicit market id to include (repeatable).")
    p.add_argument("--discover-targets", action="store_true", help="Include every MATCH_ODDS/CORRECT_SCORE/OVER_UNDER_* market.")
    p.add_argument("--top", type=int, default=6, help="How many markets to render (by margin).")
    p.add_argument("--depth", type=int, default=1, help="How many price levels per side to show per runner (1 shows best only).")
    p.add_argument("--cadence-ms", type=int, default=250, help="Render cadence in milliseconds. Default 250.")
    p.add_argument("--start-minutes-before", type=float, default=10.0, help="Start rendering this many minutes before marketTime.")
    p.add_argument("--delay", type=float, default=0.0, help="Optional extra sleep after each rendered frame (for viewing).")
    p.add_argument("--max-frames", type=int, default=0, help="Stop after N rendered frames (0 = unlimited).")
    p.add_argument("--no-clear", action="store_true", help="Do not repaint terminal; print frames sequentially.")
    p.add_argument(
        "--smooth-ui",
        action="store_true",
        help="Use alternate screen + cursor-home repaint to reduce flicker (ignored with --no-clear).",
    )
    p.add_argument("--snapshots-csv", type=Path, default=DEFAULT_SNAPSHOTS_CSV, help="Write 250ms snapshots CSV here (0 disables).")
    p.add_argument("--no-snapshots-csv", action="store_true", help="Disable writing snapshots CSV.")
    p.add_argument("--ou-under-lay-min", type=float, default=1.01, help="Render OU markets only when Under best LAY >= this.")
    p.add_argument("--ou-under-lay-max", type=float, default=1.30, help="Render OU markets only when Under best LAY <= this.")
    p.add_argument("--ladder", action="store_true", help="Render runner ladders instead of summary tables.")
    p.add_argument("--center", choices=("ltp", "mid", "best"), default="mid", help="Ladder center mode.")
    p.add_argument("--ticks-above", type=int, default=12, help="Ladder window ticks above center.")
    p.add_argument("--ticks-below", type=int, default=12, help="Ladder window ticks below center.")
    p.add_argument("--col-width", type=int, default=46, help="Ladder column width (characters).")
    p.add_argument("--cs-cols", type=int, default=3, help="How many Correct Score ladders per row.")
    p.add_argument(
        "--cs-dutch-signals",
        action="store_true",
        help=(
            "In Correct Score numeric table, add dutching signal columns for taker/maker "
            "variants (cover <=1/<=2/<=3 goals and ALL runners)."
        ),
    )
    p.add_argument(
        "--ladder-nonempty-only",
        action="store_true",
        help="In ladder mode, show only price rows with any size (or best back/lay).",
    )
    p.add_argument(
        "--ladder-max-rows",
        type=int,
        default=0,
        help="In ladder mode, cap rows per runner ladder (0 = unlimited).",
    )
    p.add_argument(
        "--honest-cs",
        action="store_true",
        default=True,
        help="For Correct Score, compute dutching only on (near-)full runner set; otherwise show n/a.",
    )
    p.add_argument(
        "--no-honest-cs",
        action="store_false",
        dest="honest_cs",
        help="Allow Correct Score dutching on filtered subset (not recommended).",
    )
    p.add_argument(
        "--dutching-debug",
        action="store_true",
        help="Print exact odds used for dutching (inv_sum/book/margin) per market.",
    )
    p.add_argument("--stake-total", type=float, default=100.0, help="Total stake used for BACK dutching examples.")
    p.add_argument("--show-stakes", action="store_true", help="Print suggested BACK dutching stakes per market.")
    p.add_argument(
        "--lay-max-liability",
        type=float,
        default=100.0,
        help="Max liability used for LAY dutching examples.",
    )
    p.add_argument("--show-lay-stakes", action="store_true", help="Print suggested LAY dutching stakes/liabilities per market.")
    p.add_argument(
        "--lay-ui",
        action="store_true",
        help="Also print Betfair-style per-outcome P/L for LAY stakes equal to BACK stakes(T).",
    )
    p.add_argument(
        "--demo-orders",
        action="store_true",
        help="Populate MYL/MYB/MAT columns with deterministic demo values (UI check).",
    )
    p.add_argument(
        "--seed-under-lay-grid",
        action="store_true",
        help="Seed maker-only LAY grid orders for totals Under runner (experimental).",
    )
    p.add_argument(
        "--seed-under-lay-grid-line",
        type=float,
        default=5.5,
        help="Totals line to seed Under LAY grid for (e.g. 5.5).",
    )
    p.add_argument(
        "--seed-under-lay-grid-all-lines",
        action="store_true",
        help="Seed Under LAY grid on all visible Over/Under totals lines, not only --seed-under-lay-grid-line.",
    )
    p.add_argument(
        "--seed-under-lay-grid-low",
        type=float,
        default=1.01,
        help="Lowest odds for the Under LAY grid (inclusive).",
    )
    p.add_argument(
        "--seed-under-lay-grid-high",
        type=float,
        default=1.20,
        help="Highest odds for the Under LAY grid (inclusive).",
    )
    p.add_argument(
        "--seed-under-lay-grid-size",
        type=float,
        default=10.0,
        help="Stake size to place at each price in the Under LAY grid.",
    )
    p.add_argument(
        "--seed-under-lay-grid-cap-at-bl",
        action="store_true",
        default=True,
        help="Cap the LAY grid max price at current best_lay (BL) for that runner (default: on).",
    )
    p.add_argument(
        "--no-seed-under-lay-grid-cap-at-bl",
        action="store_false",
        dest="seed_under_lay_grid_cap_at_bl",
        help="Do not cap the LAY grid at best_lay (may place behind BL).",
    )
    p.add_argument(
        "--show-queue",
        action="store_true",
        help="In ladder view, show queue before/after for MYL/MYB rows (experimental).",
    )
    p.add_argument(
        "--list-totals",
        action="store_true",
        help="Print all Over/Under (totals) markets visible at the first rendered frame and exit.",
    )
    p.add_argument(
        "--list-totals-ladder",
        action="store_true",
        help="Print all Over/Under (totals) markets that would be shown in the dashboard at the first rendered frame, in ladder view, then exit.",
    )
    p.add_argument(
        "--totals-all",
        action="store_true",
        help="When rendering totals ladders, include all Over/Under markets (ignores ou_under_lay_min/max and --top).",
    )
    p.add_argument(
        "--totals-center-threshold",
        type=float,
        default=1.30,
        help="If totals runner center price > this, center the ladder window around that price instead of using fixed 1.01–1.40 grid.",
    )
    p.add_argument(
        "--totals-rows",
        type=int,
        default=1,
        help="When rendering multiple totals ladders, wrap them into this many terminal rows (default: 1).",
    )
    p.add_argument(
        "--totals-sticky",
        action="store_true",
        help="Render a fixed totals set U0.5..U8.5; show empty ladders with status for missing/closed markets.",
    )
    p.add_argument(
        "--list-totals-one-line",
        action="store_true",
        help="Print the same totals selection as --list-totals-ladder, but as a single summary line per market (Under only), then exit.",
    )
    p.add_argument(
        "--emit-json",
        action="store_true",
        help="Emit one JSON object per frame to stdout (for dev GUI). Disables console rendering.",
    )
    p.add_argument(
        "--emit-json-mode",
        choices=("totals", "cs", "totals+cs"),
        default="totals+cs",
        help="Which data to include in --emit-json frames.",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="Run internal consistency checks each frame (raises AssertionError on mismatch).",
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive replay controls: space pause/resume, n next-frame (paused), b back-frame (paused), q quit.",
    )
    p.add_argument(
        "--balance",
        type=float,
        default=1000.0,
        help="Starting balance used by future stake/order simulations (default: 1000).",
    )
    p.add_argument(
        "--engine-v2-overlay",
        action="store_true",
        help="Show ENGINE V2 detector / shadow order overlay.",
    )
    p.add_argument(
        "--engine-v2-signals",
        default="replay/delta_10s_macro_min10/profile_engine_detected_fast.csv",
        help="CSV with detected engine signals.",
    )
    p.add_argument(
        "--engine-v2-rules",
        default="replay/delta_10s_macro_min10/FILTERED_SHADOW_BOT_V2_RULES.json",
        help="JSON rules for filtered shadow bot V2.",
    )
    p.add_argument(
        "--maker-under-lay-grid",
        action="store_true",
        help="Show adaptive maker LAY grid on Under *.5 Goals from current price/1.25 down to 1.01. No matching simulation.",
    )
    p.add_argument(
        "--maker-under-lay-grid-match",
        action="store_true",
        help="Enable FIFO matching simulation for one-time maker Under LAY grid.",
    )
    p.add_argument(
        "--simulate-orders",
        action="store_true",
        help="Enable legacy synthetic order execution/matching simulation. Default OFF for clean-sheet strategy work.",
    )
    p.add_argument(
        "--engine-v2-orders",
        default="replay/delta_10s_macro_min10/filtered_shadow_bot_v2_no_asian.csv",
        help="CSV with V2 shadow orders.",
    )

    p.add_argument(
        "--second-leg-cs-debug",
        action="store_true",
        help="Read-only SECOND LEG CS package shadow/debug mode. Does not place orders.",
    )
    p.add_argument(
        "--second-leg-debug-jsonl",
        default="replay/second_leg_debug.jsonl",
        help="JSONL log for read-only SECOND LEG CS shadow/debug events.",
    )
    return p.parse_args()


@dataclass(frozen=True)
class MarketMeta:
    event_id: str | None
    market_type: str | None
    market_time: str | None
    cross_matching: bool | None
    regulators: tuple[str, ...]


def logical_market_key(state: MarketState, meta: MarketMeta | None) -> tuple[str | None, str | None, str | None]:
    mt = state.market_type or (meta.market_type if meta else None)
    event_id = meta.event_id if meta else None
    market_time = meta.market_time if meta else (state.market_time.isoformat() if state.market_time else None)
    return event_id, mt, market_time


def pick_canonical_market_id(market_ids: list[str], meta_by_id: dict[str, MarketMeta]) -> str:
    def score(mid: str) -> tuple[int, int, int]:
        meta = meta_by_id.get(mid)
        cross = 1 if (meta and meta.cross_matching) else 0
        mr_int = 1 if (meta and ("MR_INT" in meta.regulators)) else 0
        has_reg = 1 if (meta and meta.regulators) else 0
        return (cross, mr_int, has_reg)

    return max(market_ids, key=lambda mid: (score(mid), mid))


def betfair_tick_size(price: float) -> float:
    if price < 2:
        return 0.01
    if price < 3:
        return 0.02
    if price < 4:
        return 0.05
    if price < 6:
        return 0.10
    if price < 10:
        return 0.20
    if price < 20:
        return 0.50
    if price < 30:
        return 1.00
    if price < 50:
        return 2.00
    if price < 100:
        return 5.00
    return 10.00


def _round_price(price: float) -> float:
    return round(float(price), 2)

def _snap_to_tick(price: float) -> float:
    """Snap an arbitrary price to the nearest valid Betfair tick in its band."""
    p = float(price)
    p = max(1.01, min(1000.0, p))
    tick = betfair_tick_size(p)
    snapped = round(p / tick) * tick
    snapped = _round_price(snapped)
    if snapped < 1.01:
        return 1.01
    if snapped > 1000.0:
        return 1000.0
    return snapped


def next_tick(price: float) -> float:
    return _round_price(price + betfair_tick_size(price))


def prev_tick(price: float) -> float:
    return max(1.01, _round_price(price - betfair_tick_size(price)))


def ladder_window(center_price: float, ticks_above: int, ticks_below: int) -> list[float]:
    center = _snap_to_tick(center_price)
    up: list[float] = [center]
    p = center
    for _ in range(max(0, ticks_above)):
        p = next_tick(p)
        up.append(p)
    p = center
    down: list[float] = []
    for _ in range(max(0, ticks_below)):
        p = prev_tick(p)
        down.append(p)
    # Price ladder: high -> low like Betfair ladder view.
    return sorted(set(up + down), reverse=True)

def ladder_window_range(price_low: float, price_high: float) -> list[float]:
    lo = max(1.01, min(1000.0, _round_price(price_low)))
    hi = max(1.01, min(1000.0, _round_price(price_high)))
    if hi < lo:
        lo, hi = hi, lo
    prices: list[float] = []
    p = lo
    # Generate in ascending order then sort to high->low.
    while p <= hi + 1e-9:
        prices.append(_round_price(p))
        p = next_tick(p)
        if len(prices) > 20000:  # safety guard
            break
    return sorted(set(prices), reverse=True)


def ladder_center_price(runner: RunnerState, *, mode: str) -> float | None:
    bb = best_level(runner.available_to_back, side="BACK")
    bl = best_level(runner.available_to_lay, side="LAY")
    if mode == "ltp" and isinstance(runner.ltp, (int, float)):
        return float(runner.ltp)
    if mode == "mid" and bb is not None and bl is not None:
        return (float(bb[0]) + float(bl[0])) / 2.0
    if mode in ("mid", "best"):
        if bb is not None and bl is not None:
            return (float(bb[0]) + float(bl[0])) / 2.0 if mode == "mid" else float(bl[0])
        if bl is not None:
            return float(bl[0])
        if bb is not None:
            return float(bb[0])
        if isinstance(runner.ltp, (int, float)):
            return float(runner.ltp)
        if runner.traded:
            # Fallback when book is one-sided/empty: center around the most-traded price.
            try:
                price = max(runner.traded.items(), key=lambda kv: kv[1])[0]
                return float(price)
            except Exception:
                return None
    return None


def _truncate_prices_around_center(prices_desc: list[float], *, center_price: float, max_rows: int) -> list[float]:
    if max_rows <= 0 or len(prices_desc) <= max_rows:
        return prices_desc
    # prices_desc is high->low. Find index closest to center_price, then take a window around it.
    best_i = 0
    best_dist = float("inf")
    for i, p in enumerate(prices_desc):
        d = abs(p - center_price)
        if d < best_dist:
            best_dist = d
            best_i = i
    half = max_rows // 2
    start = max(0, best_i - half)
    end = start + max_rows
    if end > len(prices_desc):
        end = len(prices_desc)
        start = max(0, end - max_rows)
    return prices_desc[start:end]



def _maker_grid_is_under_runner(runner: RunnerState) -> bool:
    name = (runner.name or "").strip().lower()
    return name.startswith("under ") and " goals" in name


def _maker_grid_extract_prices(value) -> list[float]:
    prices: list[float] = []

    if value is None:
        return prices

    if isinstance(value, dict):
        for k, v in value.items():
            try:
                size = float(v)
            except Exception:
                size = 0.0
            if size > 0:
                try:
                    prices.append(float(k))
                except Exception:
                    pass
        return prices

    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                p = item.get("price")
                s = item.get("size", item.get("volume", 1.0))
                try:
                    if float(s) > 0:
                        prices.append(float(p))
                except Exception:
                    pass
            elif isinstance(item, (list, tuple)) and item:
                try:
                    p = float(item[0])
                    s = float(item[1]) if len(item) > 1 else 1.0
                    if s > 0:
                        prices.append(p)
                except Exception:
                    pass

    return prices


def _maker_grid_best_lay_for_runner(runner: RunnerState) -> float | None:
    """
    Start maker grid from the current visual L column.

    In this dashboard renderer:
    - visual L column = runner.available_to_back
    - visual B column = runner.available_to_lay

    For our maker LAY grid displayed in MYL, the adaptive start must follow
    the visible L column, not the Betfair available_to_lay side.
    """
    candidates: list[float] = []

    for attr in (
        "available_to_back",
        "availableToBack",
        "atb",
        "back",
        "backs",
        "back_prices",
        "available_to_back_prices",
    ):
        candidates.extend(_maker_grid_extract_prices(getattr(runner, attr, None)))

    candidates = [
        round(float(p), 2)
        for p in candidates
        if p is not None and 1.01 <= float(p) <= 1000.0
    ]

    if candidates:
        # Visual L column current price.
        return round(max(candidates), 2)

    return None


def _maker_grid_runner_key(
    market_id: str | None,
    runner: RunnerState,
) -> tuple[str, int, float]:
    return (
        str(market_id or ""),
        int(runner.selection_id),
        float(runner.handicap or 0.0),
    )


def _maker_grid_order_key(
    market_id: str | None,
    runner: RunnerState,
    price: float,
) -> tuple[str, int, float, float]:
    return (
        str(market_id or ""),
        int(runner.selection_id),
        float(runner.handicap or 0.0),
        round(float(price), 2),
    )


def _maker_grid_visual_l_qty_for_runner_price(runner: RunnerState, price: float) -> float:
    # In this dashboard, visual L column = runner.available_to_back.
    try:
        return float(runner.available_to_back.get(round(float(price), 2)) or 0.0)
    except Exception:
        return 0.0


def _maker_grid_base_stake_for_runner_price(runner: RunnerState, price: float) -> float | None:
    if not MAKER_UNDER_LAY_GRID_ENABLED:
        return None

    if not _maker_grid_is_under_runner(runner):
        return None

    price = round(float(price), 2)

    if price < 1.01 or price > 1.25:
        return None

    start_l = _maker_grid_best_lay_for_runner(runner)
    if start_l is None:
        return None

    start_price = min(1.25, max(1.01, round(float(start_l), 2)))

    if price > start_price:
        return None

    return MAKER_UNDER_LAY_GRID_STAKES.get(price)


def _maker_under_lay_grid_stake_for_runner_price(
    runner: RunnerState,
    price: float,
    market_id: str | None = None,
) -> float | None:
    if market_id is not None:
        okey = _maker_grid_order_key(market_id, runner, price)
        st = MAKER_UNDER_LAY_GRID_ORDER_STATE.get(okey)

        if st is not None:
            remaining = float(st.get("remaining", st.get("stake", 0.0)) or 0.0)
            return remaining if remaining > 0 else None

        rkey = _maker_grid_runner_key(market_id, runner)
        if rkey in MAKER_UNDER_LAY_GRID_PLACED_RUNNERS:
            return None

    return _maker_grid_base_stake_for_runner_price(runner, price)


def _maker_under_lay_grid_matched_for_runner_price(
    runner: RunnerState,
    price: float,
    market_id: str | None = None,
) -> float | None:
    """
    Display MAT for our LAY order as liability/cost.

    For BACK:
        cost = matched_stake

    For LAY:
        liability/cost = matched_stake * (price - 1)

    The internal matched value remains matched stake.
    The visible MAT column for this maker LAY grid shows LAY liability.
    """
    if market_id is None:
        return None

    price = round(float(price), 2)
    okey = _maker_grid_order_key(market_id, runner, price)
    st = MAKER_UNDER_LAY_GRID_ORDER_STATE.get(okey)

    if st is None:
        return None

    matched_stake = float(st.get("matched", 0.0) or 0.0)
    if matched_stake <= 0:
        return None

    lay_liability = matched_stake * max(0.0, price - 1.0)
    return lay_liability if lay_liability > 0 else None


def _maker_under_lay_grid_q_ahead_for_runner_price(
    runner: RunnerState,
    price: float,
    market_id: str | None = None,
) -> float | None:
    if market_id is None:
        return None

    okey = _maker_grid_order_key(market_id, runner, price)
    st = MAKER_UNDER_LAY_GRID_ORDER_STATE.get(okey)

    if st is None:
        return None

    return float(st.get("q_ahead", st.get("q0_at_place", 0.0)) or 0.0)



def _maker_under_lay_grid_runner_pnl(
    runner: RunnerState,
    market_id: str | None = None,
) -> float | None:
    """
    Per-runner clean maker-grid PnL proxy.

    Current maker grid is LAY-only:
      matched stake is potential win if runner loses;
      matched liability is current worst-case loss if runner wins.

    For the current conservative dashboard PnL proxy we show:
      pnl = - matched_liability
    """
    if not MAKER_UNDER_LAY_GRID_ENABLED:
        return None

    if not MAKER_UNDER_LAY_GRID_MATCHING_ENABLED:
        return None

    if market_id is None:
        return None

    target_market_id = str(market_id or "")
    target_selection_id = int(runner.selection_id)
    target_handicap = float(runner.handicap or 0.0)

    matched_liability = 0.0

    for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items():
        try:
            omarket_id, selection_id, handicap, price = okey
            if str(omarket_id) != target_market_id:
                continue
            if int(selection_id) != target_selection_id:
                continue
            if float(handicap) != target_handicap:
                continue

            matched = float(st.get("matched", 0.0) or 0.0)
            price_f = float(price)
        except Exception:
            continue

        if matched <= 0:
            continue

        matched_liability += matched * max(0.0, price_f - 1.0)

    if matched_liability <= 0:
        return 0.0

    return -matched_liability



def _maker_under_lay_grid_runner_exposure(
    runner: RunnerState,
    market_id: str | None = None,
) -> dict[str, float] | None:
    """
    Matched maker-grid exposure for one runner.

    Current model is LAY-only:
      stake     = total matched LAY stake
      liability = total matched LAY liability = sum(stake * (price - 1))

    If this Under runner wins:
      risk = -liability

    If this Under runner loses:
      win = +stake
    """
    if not MAKER_UNDER_LAY_GRID_ENABLED:
        return None

    if not MAKER_UNDER_LAY_GRID_MATCHING_ENABLED:
        return None

    if market_id is None:
        return None

    target_market_id = str(market_id or "")
    target_selection_id = int(runner.selection_id)
    target_handicap = float(runner.handicap or 0.0)

    matched_stake_total = 0.0
    matched_liability_total = 0.0

    for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items():
        try:
            omarket_id, selection_id, handicap, price = okey

            if str(omarket_id) != target_market_id:
                continue
            if int(selection_id) != target_selection_id:
                continue
            if float(handicap) != target_handicap:
                continue

            matched = float(st.get("matched", 0.0) or 0.0)
            price_f = float(price)
        except Exception:
            continue

        if matched <= 0:
            continue

        matched_stake_total += matched
        matched_liability_total += matched * max(0.0, price_f - 1.0)

    if matched_stake_total <= 0:
        return None

    avg_lay_price = 1.0 + matched_liability_total / matched_stake_total

    return {
        "stake": matched_stake_total,
        "liability": matched_liability_total,
        "avg_lay_price": avg_lay_price,
        "risk": -matched_liability_total,
        "win": matched_stake_total,
    }


def render_runner_ladder(
    runner: RunnerState,
    *,
    market_id: str,
    center_mode: str,
    ticks_above: int,
    ticks_below: int,
    nonempty_only: bool = False,
    max_rows: int = 0,
    my_col_width: int = 0,
    order_model: OrderModel | None = None,
    show_queue: bool = False,
    price_low: float | None = None,
    price_high: float | None = None,
) -> list[str]:
    if not SIMULATE_ORDERS_ENABLED:
        order_model = None

    center = ladder_center_price(runner, mode=center_mode)
    if center is None:
        return [f"{fmt_text(runner.name or str(runner.selection_id), 22)} (no ladder data)"]
    # Ensure center is always on the Betfair tick ladder to avoid half-ticks like 23.5.
    center = _snap_to_tick(float(center))
    bb_best = best_level(runner.available_to_back, side="BACK")
    bl_best = best_level(runner.available_to_lay, side="LAY")
    if price_low is not None and price_high is not None:
        window = ladder_window_range(float(price_low), float(price_high))
    else:
        window = ladder_window(center, ticks_above, ticks_below)
    window = _truncate_prices_around_center(window, center_price=center, max_rows=max_rows)
    out: list[str] = []
    maker_runner_exposure = _maker_under_lay_grid_runner_exposure(runner, market_id=market_id)

    runner_name_txt = fmt_text(runner.name or str(runner.selection_id), 18)
    if maker_runner_exposure is not None:
        runner_risk = float(maker_runner_exposure.get("risk", 0.0) or 0.0)
        runner_win = float(maker_runner_exposure.get("win", 0.0) or 0.0)
        out.append(f"{runner_name_txt}  R {runner_risk:+.2f} W {runner_win:+.2f}")
    else:
        out.append(f"{runner_name_txt}")

    out.append(f"center={center_mode}:{center:.2f} ltp={runner.ltp or '-'}")
    # Fairbot-like ladder:
    # MY_LAY (maker) | MKT_BACK | PRICE | MKT_LAY | MY_BACK (maker) | MY_MATCHED
    myw = max(0, int(my_col_width))
    my_hdr = (lambda s: f"{s:>{myw}}") if myw else (lambda _s: "")
    if myw:
        if show_queue:
            out.append(
                f"{my_hdr('MYL')}│{'Q0':>6}│{'Q1':>6}│{'L':>6}│{'P':>4}│{'B':>6}│{my_hdr('MYB')}│{'VOL':>6}│{my_hdr('MAT')}"
            )
        else:
            out.append(
                f"{my_hdr('MYL')}│{'L':>6}│{'P':>4}│{'B':>6}│{my_hdr('MYB')}│{'VOL':>6}│{my_hdr('MAT')}"
            )
    else:
        out.append(f"{'L':>6}│{'P':>4}│{'B':>6}")

    for row_i, price in enumerate(window):
        bsz = runner.available_to_back.get(price)
        lsz = runner.available_to_lay.get(price)
        mark = ""
        is_bb = bb_best is not None and abs(bb_best[0] - price) < 1e-9
        is_bl = bl_best is not None and abs(bl_best[0] - price) < 1e-9
        if is_bb:
            mark = "<BB"
        if is_bl:
            mark = "<BL" if not mark else "<BB/BL"
        if nonempty_only and (bsz is None or float(bsz) <= 0) and (lsz is None or float(lsz) <= 0) and not mark:
            continue
        # Fairbot ladder labels are from the *action* perspective:
        # - Column "L" (Lay) shows the sizes available to BACK (because your LAY order would sit there).
        # - Column "B" (Back) shows the sizes available to LAY (because your BACK order would sit there).
        back_plain = _fmt_sz(None if bsz is None else float(bsz), width=6)  # market BACK side size
        lay_plain = _fmt_sz(None if lsz is None else float(lsz), width=6)   # market LAY side size
        col_l_code = "45;97"   # pink-ish (shown under label L)
        col_b_code = "106;30"  # light cyan (shown under label B)
        col_l_txt = _c(back_plain, col_l_code) if back_plain.strip() else back_plain
        col_b_txt = _c(lay_plain, col_b_code) if lay_plain.strip() else lay_plain

        # Price block (blue), highlighted on best levels
        p_plain = _fmt_price_cell(price)
        p_code = "44;97"
        p_txt = _c(p_plain, p_code)

        # Default in Fairbot: highlight the whole row at best levels (price + relevant side block).
        if is_bb and back_plain.strip():
            col_l_txt = _c(back_plain, "1;" + col_l_code)
            p_txt = _c(p_plain, "7;" + p_code)
        if is_bl and lay_plain.strip():
            col_b_txt = _c(lay_plain, "1;" + col_b_code)
            # keep BB inverse if both; otherwise make BL price bold
            if not is_bb:
                p_txt = _c(p_plain, "1;" + p_code)

        # Bot columns (maker orders + matched), auto width.
        my_lay_txt = ""
        my_back_txt = ""
        q0_txt = ""
        q1_txt = ""
        vol_txt = ""
        my_mat_txt = ""
        if myw:
            my = None
            if order_model is not None:
                my = order_model.get(
                    market_id=market_id,
                    selection_id=int(runner.selection_id),
                    handicap=runner.handicap,
                    price=price,
                )

            my_lay_val = None
            my_back_val = None
            _clean_restore_order_display = False
            _clean_my_lay_val = 0.0
            _clean_my_back_val = 0.0
            if not SIMULATE_ORDERS_ENABLED and "my" in locals() and my is not None:
                _clean_restore_order_display = True
                _clean_my_lay_val = float(getattr(my, "my_lay", 0.0) or 0.0)
                _clean_my_back_val = float(getattr(my, "my_back", 0.0) or 0.0)
            _clean_restore_order_display = False
            _clean_my_lay_val = 0.0
            _clean_my_back_val = 0.0
            if not SIMULATE_ORDERS_ENABLED and "my" in locals() and my is not None:
                _clean_restore_order_display = True
                _clean_my_lay_val = float(getattr(my, "my_lay", 0.0) or 0.0)
                _clean_my_back_val = float(getattr(my, "my_back", 0.0) or 0.0)
            maker_grid_lay_val = _maker_under_lay_grid_stake_for_runner_price(runner, float(price), market_id=market_id)
            if maker_grid_lay_val is not None:
                # Maker LAY grid on Under totals.
                # Display our order size in MYL.
                my_lay_val = maker_grid_lay_val
            my_mat_val = None
            if my is not None:
                # ENGINE_V2 render-time lifecycle guard.
                # MYL is valid only while L side exists at this exact price.
                # MYB is valid only while B side exists at this exact price.
                if my.my_lay > 0:
                    if bsz is None or float(bsz) <= 0:
                        my.matched += my.my_lay
                        my.my_lay = 0.0
                    else:
                        my_lay_val = my.my_lay

                if my.my_back > 0:
                    if lsz is None or float(lsz) <= 0:
                        my.matched += my.my_back
                        my.my_back = 0.0
                    else:
                        my_back_val = my.my_back

                if my.matched > 0:
                    my_mat_val = my.matched

            
            if _clean_restore_order_display:

                if my is not None:

                    my.my_lay = _clean_my_lay_val

                    my.my_back = _clean_my_back_val

                    my.matched = 0.0

                my_lay_val = _clean_my_lay_val if _clean_my_lay_val > 0 else None

                my_back_val = _clean_my_back_val if _clean_my_back_val > 0 else None

                my_mat_txt = ""


            

            if _clean_restore_order_display:


                if my is not None:


                    my.my_lay = _clean_my_lay_val


                    my.my_back = _clean_my_back_val


                    my.matched = 0.0


                my_lay_val = _clean_my_lay_val if _clean_my_lay_val > 0 else None


                my_back_val = _clean_my_back_val if _clean_my_back_val > 0 else None


                my_mat_txt = ""



            maker_grid_matched_val = _maker_under_lay_grid_matched_for_runner_price(runner, float(price), market_id=market_id)
            if maker_grid_matched_val is not None and maker_grid_matched_val > 0:
                my_mat_val = maker_grid_matched_val

            my_lay_txt = _c(_fmt_sz(my_lay_val, width=myw), "100;97")
            my_back_txt = _c(_fmt_sz(my_back_val, width=myw), "100;97")
            if show_queue and my_lay_val is not None and my_lay_val > 0:
                maker_grid_q0_val = _maker_under_lay_grid_q_ahead_for_runner_price(
                    runner,
                    float(price),
                    market_id=market_id,
                )
                if maker_grid_q0_val is not None:
                    q_before = float(maker_grid_q0_val)
                else:
                    q_before = 0.0 if bsz is None else float(bsz)
                q_after = q_before + float(my_lay_val)
                q0_txt = _c(_fmt_sz(q_before, width=6), "90")
                q1_txt = _c(_fmt_sz(q_after, width=6), "90")
            traded_here = runner.traded.get(price)
            vol_txt = _c(_fmt_sz(None if traded_here is None else float(traded_here), width=6), "90")
            my_mat_txt = _c(_fmt_sz(my_mat_val, width=myw), "100;97")
        if myw:
            if show_queue:
                out.append(
                    f"{my_lay_txt}│{q0_txt:>6}│{q1_txt:>6}│{col_l_txt:>6}│{p_txt}│{col_b_txt:>6}│{my_back_txt}│{vol_txt:>6}│{my_mat_txt}"
                )
            else:
                out.append(
                    f"{my_lay_txt}│{col_l_txt:>6}│{p_txt}│{col_b_txt:>6}│{my_back_txt}│{vol_txt:>6}│{my_mat_txt}"
                )
        else:
            out.append(f"{col_l_txt:>6}│{p_txt}│{col_b_txt:>6}")
        if max_rows > 0 and (len(out) - 3) >= max_rows:
            # rows beyond header lines are capped
            break
    return out



def _visible_len_no_ansi(s: str) -> int:
    try:
        return len(_strip_ansi(s))
    except NameError:
        return len(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s))


def _pad_visible_no_truncate(s: str, width: int) -> str:
    pad = max(0, width - _visible_len_no_ansi(s))
    return s + (" " * pad)


def print_columns_full(columns: list[list[str]], col_width: int, gap: str = "  ") -> None:
    """
    Print columns without truncating cell content.

    Important:
    - do NOT treat --col-width 999 as real width;
    - calculate real width from rendered ladder content;
    - keep VOL/MAT visible;
    - keep columns compact.
    """
    if not columns:
        return

    max_rows = max(len(c) for c in columns)

    widths: list[int] = []
    for col in columns:
        content_width = max([_visible_len_no_ansi(x) for x in col] + [1])

        # --col-width 999 is used by launch command as "auto/full".
        # Do not let it create huge empty gaps.
        requested = int(col_width or 0)
        if requested <= 0 or requested >= 200:
            w = content_width
        else:
            w = max(content_width, requested)

        widths.append(w)

    for row_i in range(max_rows):
        parts: list[str] = []
        for col_i, col in enumerate(columns):
            cell = col[row_i] if row_i < len(col) else ""
            parts.append(_pad_visible_no_truncate(cell, widths[col_i]))
        print(gap.join(parts).rstrip())


def print_columns(columns: list[list[str]], *, col_width: int = 42, gap: str = "  ") -> None:
    if not columns:
        return
    height = max(len(c) for c in columns)
    for i in range(height):
        parts: list[str] = []
        for col in columns:
            s = col[i] if i < len(col) else ""
            s = _truncate_visible(s, col_width)
            # Safety: if truncation removed ANSI, or the line is plain, keep the width logic below stable.
            if "\x1b[" in s and not s.endswith("\x1b[0m"):
                s = s + "\033[0m"
            pad = col_width - len(_strip_ansi(s))
            if pad > 0:
                s = s + (" " * pad)
            parts.append(s)
        print(gap.join(parts).rstrip())

def iter_levels(book: dict[float, float], *, side: str, depth: int) -> list[tuple[float, float]]:
    if not book or depth <= 0:
        return []
    prices = sorted(book.keys(), reverse=(side == "BACK"))
    out: list[tuple[float, float]] = []
    for price in prices[:depth]:
        out.append((float(price), float(book[price])))
    return out


def _terminal_cols(default: int = 160) -> int:
    try:
        return int(shutil.get_terminal_size((default, 40)).columns)
    except Exception:
        return default


def _traded_low_high(r: RunnerState) -> tuple[float | None, float | None]:
    if not r.traded:
        return None, None
    prices = [float(p) for p, v in r.traded.items() if v and v > 0]
    if not prices:
        return None, None
    return (min(prices), max(prices))


def _fmt_px(px: float | None) -> str:
    if px is None:
        return "-"
    # Betfair odds formatting: compact but stable.
    if px >= 100:
        return f"{px:>4.0f}"
    if px >= 10:
        return f"{px:>4.1f}"
    return f"{px:>4.2f}"


def _fmt_ip(px: float | None) -> str:
    if px is None or px <= 0:
        return "-"
    return f"{(100.0/px):>5.1f}"


def _render_cs_numeric_table(state: MarketState, *, under_lines: list[float], dutch_signals: bool = False) -> list[str]:
    """Render Correct Score as an aligned numeric table (TEST14 top table, without the chart panel)."""
    def min_total_for_runner(name: str) -> int | None:
        s = score_from_runner_name(name)
        if s is not None:
            return int(s[0]) + int(s[1])
        low = name.lower()
        if "any other" in low and "draw" in low:
            return 8
        if "any other" in low and "home" in low and "win" in low:
            return 4
        if "any other" in low and "away" in low and "win" in low:
            return 4
        return None

    def cs_sort_key(r: RunnerState) -> tuple[int, int, int, int, str]:
        nm = (r.name or "").strip()
        score = score_from_runner_name(nm)
        if score is not None:
            home, away = score
            # Stable, bookmaker-style order: total goals first, then home goals, then away goals.
            return (0, home + away, home, away, nm)

        low = nm.lower()
        # Keep "Any Other" buckets after the named scorelines, in a deterministic order.
        if "any other" in low and "draw" in low:
            return (1, 99, 0, 0, nm)
        if "any other" in low and "home" in low and "win" in low:
            return (2, 99, 0, 0, nm)
        if "any other" in low and "away" in low and "win" in low:
            return (3, 99, 0, 0, nm)
        return (9, 99, 0, 0, nm)

    runners = list(state.runners.values())
    runners.sort(key=cs_sort_key)
    def dutch_margin_pct_from_odds(odds: list[float]) -> float | None:
        odds = [float(x) for x in odds if x and x > 1.0]
        if len(odds) < 2:
            return None
        inv_sum = sum(1.0 / o for o in odds)
        if inv_sum <= 0:
            return None
        return (1.0 / inv_sum - 1.0) * 100.0

    # Name | BB px/sz | BL px/sz | vol | min_total | dutch signals | U{line}...
    name_w = 14
    w_px = 7
    w_sz = 10
    w_vol = 10
    w_total = 5
    # Keep YES/NO readable but add a bit of spacing so columns don't stick together.
    w_flag = 4
    # Signal column must fit strings like "+500.00%".
    w_sig = 9
    # Coverage flags align with YES/NO cells.
    w_cov = 4
    # Fixed columns always: U0.5..U8.5 (inclusive) regardless of which totals are selected above.
    under_lines_sorted = [float(x) + 0.5 for x in range(0, 9)]
    # Market-level dutching signals (summary). Taker BACK uses best LAY. Maker BACK uses best BACK.
    dutch_cols: list[tuple[str, str]] = []
    if dutch_signals:
        # Always compute; the caller decides whether to print.
        def _odds_for_runner(r: RunnerState, mode: str) -> float | None:
            if mode == "taker":
                bl = best_level(r.available_to_lay, side="LAY")
                return None if bl is None else float(bl[0])
            bb = best_level(r.available_to_back, side="BACK")
            return None if bb is None else float(bb[0])

        by_tot: list[tuple[int | None, RunnerState]] = []
        for rr in runners:
            nm_raw2 = (rr.name or "").strip()
            by_tot.append((min_total_for_runner(nm_raw2), rr))

        def _subset_odds(max_goals: int | None, mode: str) -> list[float]:
            odds: list[float] = []
            for tg, rr in by_tot:
                if max_goals is not None:
                    if tg is None or tg > max_goals:
                        continue
                px = _odds_for_runner(rr, mode)
                if px is not None and px > 1.0:
                    odds.append(px)
            return odds

        def _fmt_sig(pct: float | None) -> str:
            if pct is None or pct <= 0:
                return f"{'-':>{w_sig}}"
            s = "+" + f"{pct:.2f}%"
            if len(s) > w_sig:
                # Keep right side (…%) visible.
                s = s[-w_sig:]
            return f"{s:>{w_sig}}"

        for max_goals, label in ((1, "1"), (2, "2"), (3, "3"), (None, "ALL")):
            t_pct = dutch_margin_pct_from_odds(_subset_odds(max_goals, "taker"))
            m_pct = dutch_margin_pct_from_odds(_subset_odds(max_goals, "maker"))
            dutch_cols.append((f"DT{label}", _fmt_sig(t_pct)))
            dutch_cols.append((f"DM{label}", _fmt_sig(m_pct)))

    # Keep the table compact: show dutching summary as a separate line(s),
    # not as many empty columns repeated per row.
    out: list[str] = []
    if dutch_cols:
        parts = [f"{h}={v.strip()}" for (h, v) in dutch_cols]
        out.append("DUTCH: " + "  ".join(parts))

    header = (
        f"{'Name':<{name_w}}"
        f"│{'back1_p':>{w_px}}"
        f"│{'back1_v':>{w_sz}}"
        f"│{'lay1_p':>{w_px}}"
        f"│{'lay1_v':>{w_sz}}"
        f"│{'vol':>{w_vol}}"
        f"│{'tot':>{w_total}}"
        + (f"│{'C1':>{w_cov}}│{'C2':>{w_cov}}│{'C3':>{w_cov}}│{'CA':>{w_cov}}" if dutch_signals else "")
        + "".join([f"│{('U' + str(x).replace('.0','')):>{w_flag}}" for x in under_lines_sorted])
    )
    out.extend([header, "-" * len(_strip_ansi(header))])

    for r in runners:
        nm = (r.name or str(r.selection_id))
        if len(nm) > name_w:
            nm = nm[: name_w - 1] + "…"
        bb = best_level(r.available_to_back, side="BACK")
        bl = best_level(r.available_to_lay, side="LAY")
        bb_px = None if bb is None else float(bb[0])
        bb_sz = None if bb is None else float(bb[1])
        bl_px = None if bl is None else float(bl[0])
        bl_sz = None if bl is None else float(bl[1])
        vol = None if r.traded_volume is None else float(r.traded_volume)

        nm_raw = (r.name or "").strip()
        score = score_from_runner_name(nm_raw)

        tot_goals: int | None = min_total_for_runner(nm_raw)

        def _fmt_cell(s: str, w: int) -> str:
            # Compact-but-readable: fixed width with slight padding.
            if w == 4:
                if s == "YES":
                    return "YES "
                if s == "NO":
                    return " NO "
                return "  - "
            if s == "-":
                return f"{s:>{w}}"
            return f"{s:^{w}}"

        def yn(line: float) -> str:
            if tot_goals is None:
                return _fmt_cell("-", w_flag)
            thr = int(math.floor(float(line)))
            # Under N.5 wins if goals <= N. For Any Other buckets, we use the minimum possible total.
            return _fmt_cell(("YES" if tot_goals <= thr else "NO"), w_flag)

        def cov(max_goals: int | None) -> str:
            if tot_goals is None:
                return _fmt_cell("-", w_cov)
            if max_goals is None:
                return _fmt_cell("YES", w_cov)
            return _fmt_cell(("YES" if tot_goals <= max_goals else "NO"), w_cov)

        out.append(
            f"{nm:<{name_w}}"
            f"│{fmt_num(bb_px, width=w_px, decimals=2)}"
            f"│{fmt_num(bb_sz, width=w_sz, decimals=2)}"
            f"│{fmt_num(bl_px, width=w_px, decimals=2)}"
            f"│{fmt_num(bl_sz, width=w_sz, decimals=2)}"
            f"│{fmt_num(vol, width=w_vol, decimals=2)}"
            f"│{(('-' if tot_goals is None else str(tot_goals))):>{w_total}}"
            + ("".join([f"│{cov(1)}│{cov(2)}│{cov(3)}│{cov(None)}"]) if dutch_signals else "")
            + "".join([f"│{yn(x)}" for x in under_lines_sorted])
        )

    return out


def _render_cs_ladder_columns(
    state: MarketState,
    *,
    center_mode: str,
    ticks_above: int,
    ticks_below: int,
    ladder_nonempty_only: bool,
    ladder_max_rows: int,
    order_model: OrderModel,
    show_queue: bool,
    my_col_width: int,
) -> list[list[str]]:
    def cs_sort_key(r: RunnerState) -> tuple[int, int, int, int, str]:
        nm = (r.name or "").strip()
        score = score_from_runner_name(nm)
        if score is not None:
            home, away = score
            return (0, home + away, home, away, nm)

        low = nm.lower()
        if "any other" in low and "draw" in low:
            return (1, 99, 0, 0, nm)
        if "any other" in low and "home" in low and "win" in low:
            return (2, 99, 0, 0, nm)
        if "any other" in low and "away" in low and "win" in low:
            return (3, 99, 0, 0, nm)
        return (9, 99, 0, 0, nm)

    cols: list[list[str]] = []
    runners = sorted(state.runners.values(), key=cs_sort_key)
    for runner in runners:
        cols.append(
            render_runner_ladder(
                runner,
                market_id=state.market_id,
                center_mode=center_mode,
                ticks_above=ticks_above,
                ticks_below=ticks_below,
                nonempty_only=ladder_nonempty_only,
                max_rows=ladder_max_rows,
                my_col_width=my_col_width,
                order_model=order_model,
                show_queue=show_queue,
            )
        )
    return cols


def _render_empty_under_ladder(
    *,
    title: str,
    center_mode: str,
    ticks_above: int,
    ticks_below: int,
    ladder_max_rows: int,
    my_col_width: int,
    price_low: float = 1.01,
    price_high: float = 1.40,
) -> list[str]:
    # Build a placeholder ladder with a fixed price grid.
    window = ladder_window_range(price_low, price_high)
    window = _truncate_prices_around_center(window, center_price=price_high, max_rows=ladder_max_rows)
    out: list[str] = [title]
    myw = max(0, int(my_col_width))
    my_hdr = (lambda s: f"{s:>{myw}}") if myw else (lambda _s: "")
    out.append(
        f"{my_hdr('MYL')}"
        + ("│" if myw else "")
        + f"{'L':>6}│{'P':>4}│{'B':>6}"
        + ("│" + my_hdr('MYB') + f"│{'VOL':>6}│" + my_hdr('MAT') if myw else "")
    )
    for price in window:
        p_plain = _fmt_price_cell(price)
        p_txt = _c(p_plain, "44;97")
        if myw:
            out.append(f"{'':>{myw}}│{'':>6}│{p_txt}│{'':>6}│{'':>{myw}}│{'':>6}│{'':>{myw}}")
        else:
            out.append(f"{'':>6}│{p_txt}│{'':>6}")
    return out


def _self_check_under_monotonicity(state: MarketState) -> None:
    """Validate that Under YES/NO flags are monotonic for every CS runner."""
    under_lines_sorted = [float(x) + 0.5 for x in range(0, 9)]

    def min_total_for_runner(name: str) -> int | None:
        s = score_from_runner_name(name)
        if s is not None:
            return int(s[0]) + int(s[1])
        low = name.lower()
        if "any other" in low and "draw" in low:
            return 8
        if "any other" in low and "home" in low and "win" in low:
            return 4
        if "any other" in low and "away" in low and "win" in low:
            return 4
        return None

    for r in state.runners.values():
        name = (r.name or str(r.selection_id)).strip()
        tot = min_total_for_runner(name)
        if tot is None:
            # Skip unknown formats; table will show '-' anyway.
            continue
        prev_yes = False
        for line in under_lines_sorted:
            thr = int(math.floor(float(line)))
            is_yes = tot <= thr
            if prev_yes and not is_yes:
                raise AssertionError(f"Under monotonicity violated for {name!r}: was YES then NO at U{line}")
            prev_yes = prev_yes or is_yes


def build_emit_json_frame(
    *,
    pt: int,
    utc: str,
    markets: dict[str, MarketState],
    market_ids: set[str],
    top_n: int,
    ou_under_lay_min: float,
    ou_under_lay_max: float,
    price_low: float,
    price_high: float,
    emit_mode: str,
) -> dict[str, Any]:
    """Build a compact JSON frame for GUI streaming (start->end history)."""
    def under_runner(st: MarketState) -> RunnerState | None:
        for r in st.runners.values():
            if (r.name or "").lower().startswith("under"):
                return r
        return None

    def is_two_sided_under(st: MarketState) -> bool:
        r = under_runner(st)
        if r is None:
            return False
        bb = best_level(r.available_to_back, side="BACK")
        bl = best_level(r.available_to_lay, side="LAY")
        return bb is not None and bl is not None and bb[1] > 0 and bl[1] > 0

    # Select totals markets same way as list-totals-ladder (margin desc + under_lay range), then order by line asc.
    ou_candidates: list[tuple[float, float, MarketState]] = []
    for mid in sorted(market_ids):
        st = markets.get(mid)
        if st is None or not should_render(st, pt):
            continue
        if not is_over_under_goals(st):
            continue
        if not is_two_sided_under(st):
            continue
        margin = calc_margin_pct_from_best_lay(st.runners)
        if margin is None:
            continue
        ok, under_lay = is_under_lay_in_range(st, ou_under_lay_min, ou_under_lay_max)
        if not ok:
            continue
        ou_candidates.append((float(margin), float(under_lay or 0.0), st))

    ou_candidates.sort(key=lambda x: x[0], reverse=True)
    ou_show = [st for _m, _p, st in ou_candidates[: max(0, int(top_n))]]
    ou_show.sort(key=lambda st: (float(over_under_line(st) or 1e9), st.market_id))

    payload: dict[str, Any] = {"type": "frame", "frame": None, "pt": pt, "utc": utc}

    if "totals" in emit_mode:
        totals_out: list[dict[str, Any]] = []
        price_grid = ladder_window_range(price_low, price_high)
        for st in ou_show:
            line = over_under_line(st)
            r = under_runner(st)
            if r is None:
                continue
            rows: list[dict[str, Any]] = []
            for p in price_grid:
                L = r.available_to_back.get(p)  # action-perspective L column
                B = r.available_to_lay.get(p)   # action-perspective B column
                rows.append({"P": p, "L": None if L is None else float(L), "B": None if B is None else float(B)})
            totals_out.append(
                {
                    "market_id": st.market_id,
                    "line": float(line) if line is not None else None,
                    "under_rows": rows,
                }
            )
        payload["totals"] = totals_out

    if "cs" in emit_mode:
        # Pick first correct score market in selection set (your replay has one).
        cs_market = None
        for mid in sorted(market_ids):
            st = markets.get(mid)
            if st is None or not should_render(st, pt):
                continue
            if is_correct_score(st):
                cs_market = st
                break
        if cs_market is not None:
            rows: list[dict[str, Any]] = []
            runners = sorted(cs_market.runners.values(), key=lambda r: (r.sort_priority, (r.name or "")))
            for r in runners:
                bb = best_level(r.available_to_back, side="BACK")
                bl = best_level(r.available_to_lay, side="LAY")
                lo, hi = _traded_low_high(r)
                rows.append(
                    {
                        "name": r.name or str(r.selection_id),
                        "best_back_px": None if bb is None else float(bb[0]),
                        "best_back_sz": None if bb is None else float(bb[1]),
                        "best_lay_px": None if bl is None else float(bl[0]),
                        "best_lay_sz": None if bl is None else float(bl[1]),
                        "ltp": None if r.ltp is None else float(r.ltp),
                        "high": None if hi is None else float(hi),
                        "low": None if lo is None else float(lo),
                        "range": None if (hi is None or lo is None) else float(hi - lo),
                        "volume": None if r.traded_volume is None else float(r.traded_volume),
                    }
                )
            payload["cs"] = {"market_id": cs_market.market_id, "rows": rows}
        else:
            payload["cs"] = {"market_id": None, "rows": []}

    return payload


def write_snapshot_rows(
    writer: csv.DictWriter[str],
    *,
    pt: Any,
    markets: dict[str, MarketState],
    market_ids: set[str],
) -> int:
    rows: list[dict[str, Any]] = []
    for market_id in sorted(market_ids):
        state = markets.get(market_id)
        if state is None or not state.runners:
            continue
        for (selection_id, handicap), runner in state.runners.items():
            bb = best_level(runner.available_to_back, side="BACK")
            bl = best_level(runner.available_to_lay, side="LAY")
            rows.append(
                {
                    "tick": state.tick,
                    "pt": pt,
                    "pt_utc": format_pt(pt),
                    "market_id": state.market_id,
                    "market_type": state.market_type,
                    "market_name": state.market_name,
                    "market_status": state.market_status,
                    "in_play": state.in_play,
                    "market_time": state.market_time.isoformat() if state.market_time else None,
                    "selection_id": selection_id,
                    "handicap": handicap,
                    "runner_name": runner.name,
                    "runner_status": runner.status,
                    "best_back": None if bb is None else bb[0],
                    "best_back_size": None if bb is None else bb[1],
                    "best_lay": None if bl is None else bl[0],
                    "best_lay_size": None if bl is None else bl[1],
                    "ltp": runner.ltp,
                    "traded_volume": runner.traded_volume,
                }
            )
    if not rows:
        return 0
    writer.writerows(rows)
    return len(rows)


def should_render(state: MarketState, pt: Any) -> bool:
    if not state.runners:
        return False
    if state.stream_start_pt is not None and isinstance(pt, (int, float)) and pt < state.stream_start_pt:
        return False
    return True


def _render_dashboard_printing(
    *,
    pt: Any,
    markets: dict[str, MarketState],
    selected_ids: set[str],
    market_ids: set[str],
    top_n: int,
    depth: int,
    no_clear: bool,
    ou_under_lay_min: float,
    ou_under_lay_max: float,
    frame_index: int,
    cadence_ms: int,
    ladder: bool,
    center_mode: str,
    ticks_above: int,
    ticks_below: int,
    col_width: int,
    cs_cols: int,
    cs_dutch_signals: bool = False,
    ladder_nonempty_only: bool,
    ladder_max_rows: int,
    honest_cs: bool,
    dutching_debug: bool,
    stake_total: float,
    show_stakes: bool,
    lay_max_liability: float,
    show_lay_stakes: bool,
    lay_ui: bool,
    demo_orders: bool,
    list_totals: bool,
    list_totals_ladder: bool,
    list_totals_one_line: bool,
    totals_all: bool,
    totals_center_threshold: float,
    totals_rows: int,
    totals_sticky: bool,
    self_check: bool,
    smooth_ui: bool,
    balance: float | None,
    order_model: OrderModel,
    correct_score: list[MarketState] | None = None,
    show_queue: bool = False,
    paused: bool = False,
    err: str | None = None,
    key: str | None = None,
) -> None:
    ou_candidates: list[tuple[float, float, MarketState]] = []  # (margin, under_lay, state)
    correct_score = list(correct_score or [])

    for market_id in market_ids:
        state = markets.get(market_id)
        if state is None or not should_render(state, pt):
            continue
        if is_correct_score(state):
            correct_score.append(state)
            continue
        if is_over_under_goals(state):
            margin = calc_margin_pct_from_best_lay(state.runners)
            if margin is None:
                continue
            # Normal ladder mode should show the fixed 0.5..8.5 totals set, not a
            # subset filtered by the under-lay bounds. Keep the bounds only for the
            # dedicated totals submodes below.
            if list_totals or list_totals_ladder or list_totals_one_line:
                ok, under_lay = is_under_lay_in_range(state, ou_under_lay_min, ou_under_lay_max)
                if not ok:
                    continue
            else:
                under_lay = None
            ou_candidates.append((margin, float(under_lay or 0.0), state))

    ou_by_line: dict[float, MarketState] = {}
    for _margin, _under_lay, st in ou_candidates:
        line = over_under_line(st)
        if line is None:
            continue
        ou_by_line.setdefault(float(line), st)
    fixed_ou_lines = [float(x) + 0.5 for x in range(0, 9)]
    ou_show_fixed = [ou_by_line.get(line) for line in fixed_ou_lines]

    ou_lines: list[float] = []
    for st in ou_show_fixed:
        if st is None:
            continue
        line = over_under_line(st)
        if line is not None:
            ou_lines.append(line)

    if not no_clear:
        # Smooth repaint: keep a stable header to reduce flicker.
        if smooth_ui:
            if frame_index <= 1:
                move_top()
                print("\033[J", end="")  # clear once
            else:
                # Update header (lines 1-2) in-place, then clear below separator.
                print("\033[1;1H", end="")
                _print_header_lines(
                    frame_index=frame_index,
                    cadence_ms=cadence_ms,
                    pt=int(pt),
                    utc=format_pt(pt),
                    selected_markets=len(selected_ids),
                    dedup_markets=len(market_ids),
                    showing=0,
                    balance=balance,
                    paused=paused,
                    err=err,
                    key=key,
                )
                print("\033[4;1H\033[J", end="")
        else:
            clear_from_top()

    showing = len([st for st in ou_show_fixed if st is not None])
    if not (smooth_ui and frame_index > 1 and not no_clear):
        _print_header_lines(
            frame_index=frame_index,
            cadence_ms=cadence_ms,
            pt=int(pt),
            utc=format_pt(pt),
            selected_markets=len(selected_ids),
            dedup_markets=len(market_ids),
            showing=showing,
            balance=balance,
            paused=paused,
            err=err,
            key=key,
        )
        print("-" * 110)
    elif smooth_ui and not no_clear:
        # We updated header above with showing=0 placeholder; fix it now that we know showing.
        print("\033[1;1H", end="")
        _print_header_lines(
            frame_index=frame_index,
            cadence_ms=cadence_ms,
            pt=int(pt),
            utc=format_pt(pt),
            selected_markets=len(selected_ids),
            dedup_markets=len(market_ids),
            showing=showing,
            balance=balance,
            paused=paused,
            err=err,
            key=key,
        )
        print("\033[4;1H", end="")

    if list_totals:
        # Print all totals (Over/Under Goals) markets at this frame, then exit.
        def fmt_best(r: RunnerState) -> tuple[str, str]:
            bb = best_level(r.available_to_back, side="BACK")
            bl = best_level(r.available_to_lay, side="LAY")
            bb_s = "-" if bb is None else f"{bb[0]:.2f} ({bb[1]:.2f})"
            bl_s = "-" if bl is None else f"{bl[0]:.2f} ({bl[1]:.2f})"
            return bb_s, bl_s

        totals: list[tuple[str, float | None, MarketState]] = []
        for mid in sorted(market_ids):
            st = markets.get(mid)
            if st is None or not should_render(st, pt):
                continue
            if not is_over_under_goals(st):
                continue
            totals.append((mid, over_under_line(st), st))

        if not totals:
            print("No totals markets found.")
            return

        for mid, line, st in totals:
            mts = st.market_time.isoformat() if st.market_time else "?"
            print(f"{mid}  {st.market_name or st.market_type or ''}  line={line if line is not None else '?'}  market_time={mts}")
            # try identify Under/Over runners by name
            under = None
            over = None
            for r in st.runners.values():
                nm = (r.name or "").lower()
                if nm.startswith("under"):
                    under = r
                elif nm.startswith("over"):
                    over = r
            if under is not None:
                bb, bl = fmt_best(under)
                print(f"  Under: best_back={bb} best_lay={bl}")
            if over is not None:
                bb, bl = fmt_best(over)
                print(f"  Over : best_back={bb} best_lay={bl}")
        return

def render_dashboard(
    *,
    pt: Any,
    markets: dict[str, MarketState],
    selected_ids: set[str],
    market_ids: set[str],
    top_n: int,
    depth: int,
    no_clear: bool,
    ou_under_lay_min: float,
    ou_under_lay_max: float,
    frame_index: int,
    cadence_ms: int,
    ladder: bool,
    center_mode: str,
    ticks_above: int,
    ticks_below: int,
    col_width: int,
    cs_cols: int,
    cs_dutch_signals: bool = False,
    ladder_nonempty_only: bool,
    ladder_max_rows: int,
    honest_cs: bool,
    dutching_debug: bool,
    stake_total: float,
    show_stakes: bool,
    lay_max_liability: float,
    show_lay_stakes: bool,
    lay_ui: bool,
    demo_orders: bool,
    list_totals: bool,
    list_totals_ladder: bool,
    list_totals_one_line: bool,
    totals_all: bool,
    totals_center_threshold: float,
    totals_rows: int,
    totals_sticky: bool,
    self_check: bool,
    smooth_ui: bool,
    balance: float | None,
    order_model: OrderModel | None = None,
    show_queue: bool = False,
    _internal_no_diff: bool = False,
    paused: bool = False,
    err: str | None = None,
    key: str | None = None,
) -> None:
    order_model = order_model or OrderModel()

    match_odds: list[MarketState] = []
    ou_candidates: list[tuple[float, float, MarketState]] = []  # (margin, under_lay, state)
    correct_score: list[MarketState] = []
    for market_id in market_ids:
        state = markets.get(market_id)
        if state is None or not should_render(state, pt):
            continue
        if is_match_odds(state):
            match_odds.append(state)
            continue
        if is_correct_score(state):
            correct_score.append(state)
            continue
        if is_over_under_goals(state):
            margin = calc_margin_pct_from_best_lay(state.runners)
            if margin is None:
                continue
            # Normal ladder mode must keep the full fixed OU set; the lay-range filter
            # is only for the dedicated totals submodes below.
            ou_candidates.append((float(margin), 0.0, state))

    ou_candidates.sort(key=lambda x: x[0], reverse=True)
    ou_by_line: dict[float, MarketState] = {}
    for _m, _p, st in ou_candidates:
        line = over_under_line(st)
        if line is None:
            continue
        ou_by_line.setdefault(float(line), st)
    ou_show = [ou_by_line.get(float(x) + 0.5) for x in range(0, 9)]
    ou_lines: list[float] = []
    for st in ou_show:
        if st is None:
            continue
        line = over_under_line(st)
        if line is not None:
            ou_lines.append(line)
    wanted_scores = wanted_scorelines_from_ou_lines(ou_lines)

    if smooth_ui and not no_clear and not _internal_no_diff:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _render_dashboard_printing(
                pt=pt,
                markets=markets,
                selected_ids=selected_ids,
                market_ids=market_ids,
                top_n=top_n,
                depth=depth,
                no_clear=True,
                ou_under_lay_min=ou_under_lay_min,
                ou_under_lay_max=ou_under_lay_max,
                frame_index=frame_index,
                cadence_ms=cadence_ms,
                ladder=ladder,
                center_mode=center_mode,
                ticks_above=ticks_above,
                ticks_below=ticks_below,
                col_width=col_width,
                cs_cols=cs_cols,
                cs_dutch_signals=cs_dutch_signals,
                ladder_nonempty_only=ladder_nonempty_only,
                ladder_max_rows=ladder_max_rows,
                honest_cs=honest_cs,
                dutching_debug=dutching_debug,
                stake_total=stake_total,
                show_stakes=show_stakes,
                lay_max_liability=lay_max_liability,
                show_lay_stakes=show_lay_stakes,
                lay_ui=lay_ui,
                demo_orders=demo_orders,
                list_totals=list_totals,
                list_totals_ladder=list_totals_ladder,
                list_totals_one_line=list_totals_one_line,
                totals_all=totals_all,
                totals_center_threshold=totals_center_threshold,
                totals_rows=totals_rows,
                totals_sticky=totals_sticky,
                self_check=self_check,
                smooth_ui=False,
                balance=balance,
                order_model=order_model,
                correct_score=correct_score,
                show_queue=show_queue,
                paused=paused,
                err=err,
                key=key,
            )
        lines = buf.getvalue().splitlines()
        _smooth_repaint(lines)
        return

    _render_dashboard_printing(
        pt=pt,
        markets=markets,
        selected_ids=selected_ids,
        market_ids=market_ids,
        top_n=top_n,
        depth=depth,
        no_clear=no_clear,
        ou_under_lay_min=ou_under_lay_min,
        ou_under_lay_max=ou_under_lay_max,
        frame_index=frame_index,
        cadence_ms=cadence_ms,
        ladder=ladder,
        center_mode=center_mode,
        ticks_above=ticks_above,
        ticks_below=ticks_below,
        col_width=col_width,
        cs_cols=cs_cols,
        cs_dutch_signals=cs_dutch_signals,
        ladder_nonempty_only=ladder_nonempty_only,
        ladder_max_rows=ladder_max_rows,
        honest_cs=honest_cs,
        dutching_debug=dutching_debug,
        stake_total=stake_total,
        show_stakes=show_stakes,
        lay_max_liability=lay_max_liability,
        show_lay_stakes=show_lay_stakes,
        lay_ui=lay_ui,
        demo_orders=demo_orders,
        list_totals=list_totals,
        list_totals_ladder=list_totals_ladder,
        list_totals_one_line=list_totals_one_line,
        totals_all=totals_all,
        totals_center_threshold=totals_center_threshold,
        totals_rows=totals_rows,
        totals_sticky=totals_sticky,
        self_check=self_check,
        smooth_ui=smooth_ui,
        balance=balance,
        order_model=order_model,
        correct_score=correct_score,
        show_queue=show_queue,
        paused=paused,
        err=err,
        key=key,
    )

    if list_totals_ladder or list_totals_one_line:
        # Mimic the same OU selection logic as the dashboard (ou_under_lay_min/max + top_n by margin),
        # then either render each qualifying totals market in ladder view or print one line per market and exit.
        def _find_under_runner(st: MarketState) -> RunnerState | None:
            for r in st.runners.values():
                if (r.name or "").lower().startswith("under"):
                    return r
            return None

        def _is_runner_liquid(r: RunnerState) -> bool:
            # "Liquid" here means we have a two-sided best quote (both best back and best lay exist).
            bb = best_level(r.available_to_back, side="BACK")
            bl = best_level(r.available_to_lay, side="LAY")
            return bb is not None and bl is not None and bb[1] > 0 and bl[1] > 0

        ou_candidates: list[tuple[float, float, MarketState]] = []  # (margin, under_lay, state)
        for mid in sorted(market_ids):
            st = markets.get(mid)
            if st is None or not should_render(st, pt):
                continue
            if not is_over_under_goals(st):
                continue
            margin = calc_margin_pct_from_best_lay(st.runners)
            if margin is None:
                if totals_all:
                    margin = 0.0
                else:
                    continue
            under_lay = None
            if not totals_all:
                ok, under_lay = is_under_lay_in_range(st, ou_under_lay_min, ou_under_lay_max)
                if not ok:
                    continue
            under_runner = _find_under_runner(st)
            if under_runner is None:
                continue
            if not totals_all:
                if not _is_runner_liquid(under_runner):
                    # Skip markets where Under side is one-sided (e.g. no best back at all).
                    continue
            else:
                # In totals_all mode, allow one-sided books; just require some ladder data.
                if not under_runner.available_to_back and not under_runner.available_to_lay and not under_runner.traded:
                    continue
            ou_candidates.append((float(margin), float(under_lay or 0.0), st))

        if totals_sticky:
            # Fixed set U0.5..U8.5 (inclusive), regardless of selection. Use any matching markets we saw.
            want_lines = [float(x) + 0.5 for x in range(0, 9)]
            by_line: dict[float, MarketState] = {}
            for _m, _p, st in ou_candidates:
                ln = over_under_line(st)
                if ln is None:
                    continue
                by_line.setdefault(float(ln), st)
            ou_show = [by_line.get(ln) for ln in want_lines]  # type: ignore[list-item]
        else:
            if totals_all:
                ou_show = [st for _m, _p, st in ou_candidates]
            else:
                ou_candidates.sort(key=lambda x: x[0], reverse=True)
                ou_show = [st for _m, _p, st in ou_candidates[: max(0, top_n)]]
        if not ou_show:
            print("No totals markets matched the dashboard filters.")
            return

        if not totals_sticky:
            # Print in a stable, human-friendly order (by totals line, then market id),
            # while keeping the same inclusion set as the dashboard (top_n by margin).
            def _line_key(st: MarketState) -> tuple[float, str]:
                line = over_under_line(st)
                return (float(line) if line is not None else float("inf"), st.market_id)

            ou_show = [st for st in ou_show if st is not None]
            ou_show.sort(key=_line_key)

        if list_totals_one_line:
            for st in ou_show:
                line = over_under_line(st)
                line_s = "?" if line is None else str(line)
                under = _find_under_runner(st)
                if under is None:
                    continue
                bb = best_level(under.available_to_back, side="BACK")
                bl = best_level(under.available_to_lay, side="LAY")
                bb_s = "-" if bb is None else f"{bb[0]:.2f}@{bb[1]:.2f}"
                bl_s = "-" if bl is None else f"{bl[0]:.2f}@{bl[1]:.2f}"
                ltp_s = "-" if under.ltp is None else str(under.ltp)
                market_name = (st.market_name or "").strip()
                if market_name:
                    print(f"{st.market_id}  line={line_s}  Under ltp={ltp_s}  bb={bb_s}  bl={bl_s}  {market_name}")
                else:
                    print(f"{st.market_id}  line={line_s}  Under ltp={ltp_s}  bb={bb_s}  bl={bl_s}")
            return

        # Ladder view: print all qualifying totals in one terminal row (multi-column), Under only.
        cols: list[list[str]] = []
        for idx, st in enumerate(ou_show):
            if st is None:
                ln = float(idx) + 0.5
                title = f"-  OVER_UNDER  Under {ln:g}  status=MISSING"
                col = _render_empty_under_ladder(
                    title=title,
                    center_mode=center_mode,
                    ticks_above=ticks_above,
                    ticks_below=ticks_below,
                    ladder_max_rows=ladder_max_rows,
                    my_col_width=6,
                )
                cols.append(col)
                continue

            line = over_under_line(st)
            line_s = "?" if line is None else str(line)
            status = (st.market_status or "-").strip()
            under = _find_under_runner(st)
            if under is None:
                title = f"{st.market_id}  {st.market_type or ''}  line={line_s}  status={status}  Under=MISSING"
                cols.append(
                    _render_empty_under_ladder(
                        title=title,
                        center_mode=center_mode,
                        ticks_above=ticks_above,
                        ticks_below=ticks_below,
                        ladder_max_rows=ladder_max_rows,
                        my_col_width=6,
                    )
                )
                continue
            under_center = ladder_center_price(under, mode=center_mode)
            def _has_any_in_fixed_grid(r: RunnerState) -> bool:
                for book in (r.available_to_back, r.available_to_lay, r.traded):
                    for px in book.keys():
                        try:
                            pxf = float(px)
                        except Exception:
                            continue
                        if 1.01 - 1e-9 <= pxf <= 1.40 + 1e-9:
                            return True
                if isinstance(r.ltp, (int, float)) and 1.01 - 1e-9 <= float(r.ltp) <= 1.40 + 1e-9:
                    return True
                return False

            use_fixed_grid = (
                under_center is not None
                and 1.01 <= float(under_center) <= float(totals_center_threshold)
                and _has_any_in_fixed_grid(under)
            )
            col = [
                f"{st.market_id}  {st.market_type or ''}  {st.market_name or ''}  line={line_s}  status={status}",
            ]
            col.extend(
                render_runner_ladder(
                    under,
                    market_id=st.market_id,
                    center_mode=center_mode,
                    ticks_above=ticks_above,
                    ticks_below=ticks_below,
                    nonempty_only=False,
                    max_rows=ladder_max_rows,
                    my_col_width=6,
                    order_model=order_model,
                    show_queue=bool(show_queue),
                    price_low=1.01 if use_fixed_grid else None,
                    price_high=1.40 if use_fixed_grid else None,
                )
            )
            cols.append(col)

        if not cols:
            print("No totals markets matched the dashboard filters.")
            return

        # Wrap totals ladders into fixed 5-column rows.
        # Totals layout: 3 columns per row, 30 ladder rows, full width.
        force_cols_per_row = 3
        gap = " "
        term_cols = int(shutil.get_terminal_size(fallback=(260, 70)).columns)
        effective_col_width = max(
            30,
            (term_cols - (len(gap) * (force_cols_per_row - 1))) // force_cols_per_row,
        )

        for start_i in range(0, len(cols), force_cols_per_row):
            print_columns_full(
                cols[start_i : start_i + force_cols_per_row],
                col_width=effective_col_width,
                gap=gap,
            )
            if start_i + force_cols_per_row < len(cols):
                print()

        # Below totals, append Correct Score as full ladder columns.
        # Force 4 runner ladders per row and auto-cap col_width to terminal width.
        cs_markets: list[MarketState] = []
        for mid in sorted(market_ids):
            st = markets.get(mid)
            if st is None or not should_render(st, pt):
                continue
            if is_correct_score(st):
                cs_markets.append(st)

        if cs_markets:
            print("-" * 110)
            print("-" * 110)

            # Correct Score layout: 3 columns per row, 20 ladder rows, full width.
            # This gives 4 visual rows for the current Correct Score runner set.
            cs_cols_per_row = 3
            cs_gap = "  "
            term_cols = int(shutil.get_terminal_size(fallback=(240, 70)).columns)
            cs_auto_col_width = max(
                18,
                (term_cols - (len(cs_gap) * (cs_cols_per_row - 1))) // cs_cols_per_row,
            )
            cs_effective_col_width = min(max(18, int(col_width)), cs_auto_col_width)

            for st in sorted(cs_markets, key=lambda s: s.market_id):
                print(f"{st.market_id}  {st.market_type or '-':<12}  {fmt_text(st.market_name, 34)}")

                runners_sorted = sorted(st.runners.values(), key=lambda r: (r.sort_priority, (r.name or "")))

                cols_cs: list[list[str]] = []
                for runner in runners_sorted:
                    cols_cs.append(
                        render_runner_ladder(
                            runner,
                            market_id=st.market_id,
                            center_mode=center_mode,
                            ticks_above=ticks_above,
                            ticks_below=ticks_below,
                            nonempty_only=ladder_nonempty_only,
                            max_rows=20,
                            my_col_width=6,
                            order_model=order_model,
                            show_queue=bool(show_queue),
                        )
                    )

                    if len(cols_cs) >= cs_cols_per_row:
                        print_columns_full(cols_cs, col_width=cs_effective_col_width, gap=cs_gap)
                        print()
                        cols_cs = []

                if cols_cs:
                    print_columns_full(cols_cs, col_width=cs_effective_col_width, gap=cs_gap)

                print("-" * 110)

        # In smooth-ui mode, clear any leftover from previous longer frame without flashing.
        if smooth_ui and not no_clear:
            print("\033[J", end="")
        return

    if demo_orders:
        # Deterministic demo fill: show MAKER orders only (orders that do not cross the spread).
        # Placing at best_back / best_lay is still maker because it does not cross the spread.
        for mid in market_ids:
            st = markets.get(mid)
            if st is None or not st.runners:
                continue
            for (selection_id, handicap), runner in st.runners.items():
                bb = best_level(runner.available_to_back, side="BACK")
                bl = best_level(runner.available_to_lay, side="LAY")
                if bl is not None and bb is not None:
                    best_back = float(bb[0])
                    best_lay = float(bl[0])

                    # Maker LAY at best_lay (doesn't cross).
                    seed = (abs(hash((mid, selection_id, handicap, best_lay, "ML"))) % 900) / 10.0
                    order_model.by_key[(mid, int(selection_id), handicap, float(best_lay))] = MyOrdersAtPrice(
                        my_lay=5.0 + (seed % 25.0),
                        my_back=0.0,
                        matched=0.0,
                    )

                    # Maker BACK at best_back (doesn't cross).
                    seed = (abs(hash((mid, selection_id, handicap, best_back, "MB"))) % 900) / 10.0
                    order_model.by_key[(mid, int(selection_id), handicap, float(best_back))] = MyOrdersAtPrice(
                        my_lay=0.0,
                        my_back=5.0 + (seed % 25.0),
                        matched=0.0,
                    )

    def dutching_summary(
        state: MarketState,
        *,
        runner_filter: callable | None = None,
        total_stake: float = 100.0,
        require_coverage: bool = False,
        min_runner_coverage: float = 0.98,
    ) -> list[str]:
        # Compute two "books": using best LAY prices (back-dutching taker) and best BACK prices (alt view).
        def odds_from(source: str) -> tuple[list[tuple[str, float]], int]:
            odds: list[tuple[str, float]] = []
            active = 0
            for r in state.runners.values():
                if r.status not in (None, "ACTIVE"):
                    continue
                active += 1
                if runner_filter is not None and not runner_filter(r):
                    continue
                best = best_level(r.available_to_lay, side="LAY") if source == "best_lay" else best_level(r.available_to_back, side="BACK")
                if best is None:
                    continue
                price, size = best
                if not (1.01 <= price <= 1000.0) or not math.isfinite(price):
                    continue
                if size <= 0 or not math.isfinite(size):
                    continue
                odds.append((r.name or str(r.selection_id), float(price)))
            return odds, active

        header_parts: list[str] = []
        debug_lines: list[str] = []
        for source in ("best_lay", "best_back"):
            odds, active = odds_from(source)
            if len(odds) < 2 or active < 2:
                continue
            if require_coverage:
                coverage = (len(odds) / float(active)) if active else 0.0
                if coverage < float(min_runner_coverage):
                    continue
            odds_only = [o for _n, o in odds]
            res = calc_dutching(
                odds_only,
                method="fixed-stake",
                total_stake=float(total_stake),
                target_profit=0.0,
                min_stake=0.0,
                stake_decimals=2,
            )
            header_parts.append(f"{source}: book={res.book_pct:.2f}% margin={res.margin_pct:+.3f}%")

            if show_stakes and res.inv_sum > 0 and len(odds) >= 2:
                # BACK dutching stakes for total stake T.
                t = max(0.0, float(stake_total))
                inv = [1.0 / o for o in odds_only]
                inv_sum = sum(inv)
                if inv_sum > 0 and t > 0:
                    stakes = [t * (x / inv_sum) for x in inv]
                    payout = t / inv_sum
                    profit = payout - t
                    debug_lines.append(
                        f"    BACK stakes(T={t:.2f}): "
                        + ", ".join([f"{name}={s:.2f}" for (name, _o), s in zip(odds, stakes)])
                        + f" | payout≈{payout:.2f} profit≈{profit:+.2f}"
                    )
                    if lay_ui:
                        # If you (mistakenly) use these BACK stakes as LAY stakes in Betfair UI,
                        # the per-outcome P/L becomes:
                        #   P_i = sum_{j!=i} layStake_j - (odds_i - 1)*layStake_i
                        total_lay_stake = sum(stakes)
                        pl_parts: list[str] = []
                        for (name, o), ls in zip(odds, stakes):
                            liability = (o - 1.0) * ls
                            profit_i = (total_lay_stake - ls) - liability
                            pl_parts.append(f"{name} {profit_i:+.2f}")
                        debug_lines.append("    LAY UI P/L (layStake=BACK stake): " + " | ".join(pl_parts))

            if show_lay_stakes and res.inv_sum > 0 and len(odds) >= 2:
                # LAY dutching (lay all outcomes) has equal-profit solution with lay_stake_i = C / odds_i.
                # Profit when any outcome wins: P = C * (inv_sum - 1). If inv_sum < 1, this is a guaranteed loss.
                L = max(0.0, float(lay_max_liability))
                inv_sum = res.inv_sum
                if L > 0:
                    worst = 0.0
                    for _name, o in odds:
                        worst = max(worst, 1.0 - (1.0 / o))
                    if worst > 0:
                        C = L / worst
                        lay_stakes = [C / o for _name, o in odds]
                        liabilities = [(o - 1.0) * ls for (_name, o), ls in zip(odds, lay_stakes)]
                        profit = C * (inv_sum - 1.0)
                        debug_lines.append(
                            f"    LAY stakes(maxL={L:.2f}): "
                            + ", ".join(
                                [
                                    f"{name}={ls:.2f}(L={liab:.2f})"
                                    for (name, _o), ls, liab in zip(odds, lay_stakes, liabilities)
                                ]
                            )
                            + f" | profit≈{profit:+.2f}"
                        )
            if dutching_debug:
                coverage_txt = ""
                if require_coverage:
                    coverage_txt = f" coverage={len(odds)}/{active} ({(len(odds)/active)*100.0:.1f}%)"
                debug_lines.append(
                    f"  {source}: inv_sum={res.inv_sum:.6f} book={res.book_pct:.2f}% margin={res.margin_pct:+.3f}%{coverage_txt}"
                )
                debug_lines.append("    odds: " + ", ".join([f"{name}={price:.2f}" for name, price in odds]))

        header = " | ".join(header_parts) if header_parts else "dutching: n/a"
        return [header, *debug_lines] if debug_lines else [header]

    def render_market(state: MarketState, *, margin: float | None, runner_filter: callable | None = None) -> None:
        market_line = (
            f"{state.market_id}  {state.market_type or '-':<12}  "
            f"{fmt_text(state.market_name, 34)}  status={state.market_status or '-':<7} "
            f"inplay={str(state.in_play):<5}  margin%={fmt_num(margin, width=7, decimals=3)}  runners={len(state.runners):>3}"
        )
        print(market_line)
        for line in dutching_summary(state, runner_filter=runner_filter):
            print(line)
        print(f"{'RUNNER':<22} {'BB':>8} {'BSZ':>9} {'BL':>8} {'LSZ':>9} {'LTP':>8} {'TV':>10}")
        print("-" * 110)
        runners_sorted = sorted(state.runners.values(), key=lambda r: (r.sort_priority, (r.name or "")))
        shown = 0
        for runner in runners_sorted:
            if runner_filter is not None and not runner_filter(runner):
                continue
            bb = best_level(runner.available_to_back, side="BACK")
            bl = best_level(runner.available_to_lay, side="LAY")
            print(
                f"{fmt_text(runner.name or str(runner.selection_id), 22)} "
                f"{fmt_num(None if bb is None else bb[0], width=8)} {fmt_num(None if bb is None else bb[1], width=9)} "
                f"{fmt_num(None if bl is None else bl[0], width=8)} {fmt_num(None if bl is None else bl[1], width=9)} "
                f"{fmt_num(runner.ltp, width=8)} {fmt_num(runner.traded_volume, width=10)}"
            )
            shown += 1
            if shown >= 30:
                break
        if shown == 0:
            print("(no runners to display)")
        print("-" * 110)

    if not ladder:
        # Over/Under: fixed U0.5..U8.5 ladder set
        for st in ou_show:
            if st is None:
                continue
            render_market(
                st,
                margin=calc_margin_pct_from_best_lay(st.runners),
                runner_filter=lambda r: (r.name or "").lower().startswith(("under", "over")),
            )

    else:
        # Ladder mode: print a compact per-runner ladder for each displayed market.
        for st in ou_show:
            if st is None:
                continue
            print(f"{st.market_id}  {st.market_type or '-':<12}  {fmt_text(st.market_name, 34)}")
            ou_filter = lambda r: (r.name or "").lower().startswith(("under", "over"))
            for line in dutching_summary(st, runner_filter=ou_filter):
                print(line)
            runners_sorted = sorted(st.runners.values(), key=lambda r: (r.sort_priority, (r.name or "")))
            myw = 6
            cols: list[list[str]] = []
            for runner in runners_sorted:
                if not ou_filter(runner):
                    continue
                cols.append(
                    render_runner_ladder(
                        runner,
                        market_id=st.market_id,
                        center_mode=center_mode,
                        ticks_above=ticks_above,
                        ticks_below=ticks_below,
                        nonempty_only=ladder_nonempty_only,
                        max_rows=ladder_max_rows,
                        my_col_width=myw,
                        order_model=order_model,
                        show_queue=bool(show_queue),
                    )
                )
            print_columns(cols, col_width=col_width)
            print("-" * 110)

    if correct_score:
        print("-" * 110)
        correct_score.sort(key=lambda s: s.market_id)
        for st in correct_score:
            print(f"{st.market_id}  {st.market_type or '-':<12}  {fmt_text(st.market_name, 34)}")
            if self_check:
                _self_check_under_monotonicity(st)
            cols = _render_cs_ladder_columns(
                st,
                center_mode=center_mode,
                ticks_above=ticks_above,
                ticks_below=ticks_below,
                ladder_nonempty_only=ladder_nonempty_only,
                ladder_max_rows=ladder_max_rows,
                order_model=order_model,
                show_queue=bool(show_queue),
                my_col_width=6,
            )
            rows = max(1, int(cs_cols))
            per_row = max(1, int(math.ceil(len(cols) / rows)))
            term_cols = int(shutil.get_terminal_size(fallback=(200, 60)).columns)
            gap = "   "
            max_cols_per_row = max(1, (term_cols + len(gap)) // (col_width + len(gap)))
            per_row = min(per_row, max_cols_per_row)
            for start in range(0, len(cols), per_row):
                print_columns(cols[start : start + per_row], col_width=col_width, gap=gap)
                if start + per_row < len(cols):
                    print()
            print("-" * 110)

    sys.stdout.flush()


def _engine_v2_find_runner(
    *,
    markets: dict[str, MarketState],
    row: dict[str, object],
) -> tuple[MarketState, RunnerState] | tuple[None, None]:
    mt = str(row.get("market_type") or "")
    mn = str(row.get("market_name") or "")
    rn = str(row.get("runner_name") or "")

    for st in markets.values():
        if mt and str(st.market_type or "") != mt:
            continue
        if mn and str(st.market_name or "") != mn:
            continue

        for runner in st.runners.values():
            if str(runner.name or "") == rn:
                return st, runner

    return None, None


def _engine_v2_current_locked_and_pnl(
    *,
    orders: list[dict[str, object]],
    balance: float | None,
) -> tuple[float, float, float]:
    bal = 0.0 if balance is None else float(balance)
    locked = 0.0
    pnl = 0.0

    for r in orders:
        if bool(r.get("_engine_v2_settled", False)):
            pnl += float(r.get("_pnl") or 0.0)
            continue

        if bool(r.get("_engine_v2_placed", False)):
            locked += float(r.get("_engine_v2_liability") or 0.0)

    free = bal + pnl - locked
    return locked, pnl, free


def _engine_v2_apply_orders_to_order_model(
    *,
    pt: int,
    markets: dict[str, MarketState],
    order_model: OrderModel,
    orders: list[dict[str, object]],
    balance: float | None,
) -> None:
    """
    ENGINE_V2 visual order layer.

    BACK = ЗА:
        exposure = stake
        shown as MYB

    LAY = ПРОТИ:
        exposure = stake * (price - 1)
        shown as MYL
    """
    for r in orders:
        entry_ms = int(r.get("_entry_ms") or 0)
        fill_ms = int(r.get("_fill_ms") or 0)
        exit_ms = int(r.get("_exit_ms") or 0)

        if entry_ms <= 0:
            continue

        # 1) settle after exit
        if exit_ms and pt >= exit_ms and bool(r.get("_engine_v2_placed", False)):
            key = r.get("_engine_v2_order_key")
            if key in order_model.by_key:
                my = order_model.by_key[key]
                if my.my_lay > 0:
                    my.matched += my.my_lay
                    my.my_lay = 0.0
                if my.my_back > 0:
                    my.matched += my.my_back
                    my.my_back = 0.0

            r["_engine_v2_settled"] = True
            continue

        # 2) fill after fill_utc
        if fill_ms and pt >= fill_ms and bool(r.get("_engine_v2_placed", False)):
            key = r.get("_engine_v2_order_key")
            if key in order_model.by_key:
                my = order_model.by_key[key]
                if my.my_lay > 0:
                    my.matched += my.my_lay
                    my.my_lay = 0.0
                if my.my_back > 0:
                    my.matched += my.my_back
                    my.my_back = 0.0
            r["_engine_v2_filled"] = True
            continue

        # 3) place at entry time
        if pt < entry_ms:
            continue
        if bool(r.get("_engine_v2_placed", False)) or bool(r.get("_engine_v2_skipped", False)):
            continue

        st, runner = _engine_v2_find_runner(markets=markets, row=r)
        if st is None or runner is None:
            r["_engine_v2_skipped"] = True
            r["_engine_v2_skip_reason"] = "MARKET_OR_RUNNER_NOT_FOUND"
            continue

        side = str(r.get("entry_order_side") or "")
        price = float(r.get("_price") or r.get("price") or 0.0)
        stake = float(r.get("_stake") or r.get("stake") or 0.0)

        if price <= 1.0 or stake <= 0.0:
            r["_engine_v2_skipped"] = True
            r["_engine_v2_skip_reason"] = "BAD_PRICE_OR_STAKE"
            continue

        if side == "LAY":
            liability = stake * max(0.0, price - 1.0)
        elif side == "BACK":
            liability = stake
        else:
            r["_engine_v2_skipped"] = True
            r["_engine_v2_skip_reason"] = "BAD_SIDE"
            continue

        _locked, _pnl, free = _engine_v2_current_locked_and_pnl(
            orders=orders,
            balance=balance,
        )

        if free + 1e-9 < liability:
            r["_engine_v2_skipped"] = True
            r["_engine_v2_skip_reason"] = "NO_FREE_BALANCE"
            continue

        key = (st.market_id, int(runner.selection_id), runner.handicap, float(price))
        my = order_model.by_key.get(key, MyOrdersAtPrice())

        if side == "LAY":
            my.my_lay += stake
        elif side == "BACK":
            my.my_back += stake

        order_model.by_key[key] = my

        r["_engine_v2_placed"] = True
        r["_engine_v2_order_key"] = key
        r["_engine_v2_liability"] = liability
        r["_engine_v2_skip_reason"] = ""

def update_order_model_from_current_ladder(
    *,
    markets: dict[str, MarketState],
    order_model: OrderModel,
) -> None:
    """
    ENGINE_V2 proxy lifecycle.

    MYL is tied to visible L side.
    MYB is tied to visible B side.

    If our visible queue side disappears at that price, treat own order as filled.
    This is not real Betfair matching yet; it is ladder-disappearance proxy.
    """
    for key, my in list(order_model.by_key.items()):
        market_id, selection_id, handicap, price = key
        st = markets.get(market_id)
        if st is None:
            continue

        runner = st.runners.get(int(selection_id))
        if runner is None:
            continue
        if runner.handicap != handicap:
            continue

        px = float(price)

        if my.my_lay > 0:
            l_q = runner.available_to_back.get(px)
            if l_q is None or float(l_q) <= 0:
                my.matched += my.my_lay
                my.my_lay = 0.0

        if my.my_back > 0:
            b_q = runner.available_to_lay.get(px)
            if b_q is None or float(b_q) <= 0:
                my.matched += my.my_back
                my.my_back = 0.0

ENGINE_V2_OVERLAY_LINE = ""
ENGINE_V2_TAPE_LINE = ""

def _engine_v2_ts_ms(s: str) -> int:
    from datetime import datetime
    if not s:
        return 0
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)

def _engine_v2_load_orders(path: str) -> list[dict[str, object]]:
    import csv
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []

    rows: list[dict[str, object]] = []
    with p.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_entry_ms"] = _engine_v2_ts_ms(r.get("first_add_utc", ""))
                r["_fill_ms"] = _engine_v2_ts_ms(r.get("fill_utc", ""))
                r["_exit_ms"] = _engine_v2_ts_ms(r.get("exit_utc", ""))
                r["_stake"] = float(r.get("stake") or 0.0)
                r["_price"] = float(r.get("price") or 0.0)
                r["_pnl"] = float(r.get("pnl_proxy") or 0.0)
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda x: int(x.get("_entry_ms") or 0))
    return rows

def _engine_v2_order_liability(r: dict[str, object]) -> float:
    side = str(r.get("entry_order_side") or "")
    stake = float(r.get("_stake") or 0.0)
    price = float(r.get("_price") or 0.0)

    if side == "BACK":
        return stake
    if side == "LAY":
        return stake * max(0.0, price - 1.0)
    return 0.0

def _engine_v2_tape_line(
    *,
    pt: int,
    orders: list[dict[str, object]],
) -> str:
    if not orders:
        return ""

    events = []

    for r in orders:
        entry = int(r.get("_entry_ms") or 0)
        fill = int(r.get("_fill_ms") or 0)
        exit_ms = int(r.get("_exit_ms") or 0)

        status = None
        event_ms = 0

        if entry and abs(pt - entry) <= 3000:
            status = "PLACE"
            event_ms = entry
        if fill and abs(pt - fill) <= 3000:
            status = "FILL"
            event_ms = fill
        if exit_ms and abs(pt - exit_ms) <= 3000:
            status = "EXIT"
            event_ms = exit_ms

        if status is None:
            continue

        events.append((abs(pt - event_ms), r, status))

    if not events:
        return "ENGINE_V2_TAPE: -"

    events.sort(key=lambda x: x[0])
    parts = []

    for _dt, r, status in events[:4]:
        side = str(r.get("entry_order_side") or "")
        stake = float(r.get("_stake") or r.get("stake") or 0.0)
        price = float(r.get("_price") or r.get("price") or 0.0)

        if side == "LAY":
            liab = stake * max(0.0, price - 1.0)
        elif side == "BACK":
            liab = stake
        else:
            liab = 0.0

        parts.append(
            f'{status} S{r.get("signal_id")} '
            f'{r.get("market_type")} {side}@{r.get("price")} '
            f'stake={stake:.2f} liab={liab:.2f} pnl={float(r.get("_pnl") or 0.0):.4f}'
        )

    return "ENGINE_V2_TAPE: " + " || ".join(parts)

def _engine_v2_overlay_line(
    *,
    pt: int,
    balance: float | None,
    orders: list[dict[str, object]],
) -> str:
    if not orders:
        return "ENGINE_V2: no orders loaded"

    bal = 0.0 if balance is None else float(balance)

    active = []
    next_rows = []
    closed_pnl = 0.0

    for r in orders:
        entry = int(r.get("_entry_ms") or 0)
        fill = int(r.get("_fill_ms") or 0)
        exit_ms = int(r.get("_exit_ms") or 0)

        if exit_ms and exit_ms <= pt:
            closed_pnl += float(r.get("_pnl") or 0.0)

        end = exit_ms if exit_ms else entry + 60000
        if entry <= pt <= end:
            active.append(r)

        if pt < entry <= pt + 10000:
            next_rows.append(r)

    locked = sum(_engine_v2_order_liability(r) for r in active)
    free = bal + closed_pnl - locked

    nxt = next_rows[0] if next_rows else None
    if nxt:
        next_txt = (
            f' NEXT={nxt.get("market_type")} '
            f'{nxt.get("entry_order_side")}@{nxt.get("price")} '
            f'stake={nxt.get("stake")}'
        )
    else:
        next_txt = " NEXT=-"

    return (
        f"ENGINE_V2: active={len(active)} next10s={len(next_rows)} "
        f"locked={locked:.2f} free={free:.2f} pnl_proxy={closed_pnl:.4f}"
        f"{next_txt}"
    )


def _apply_maker_under_lay_grid_orders(
    *,
    markets: dict[str, MarketState],
    order_model: OrderModel,
    pt: int | None = None,
    frame_no: int | None = None,
) -> None:
    """
    One-time stationary maker Under LAY grid + FIFO matching.

    Matching rules for MYL LAY orders:
    - FRAME 1 cannot have matching;
    - order cannot match on the same frame where it was placed;
    - visual L column = runner.available_to_back;
    - q_ahead is consumed by L-volume decreases before our order;
    - our order can match ONLY when best visual L price has reached/crossed order price:
        best_visual_l_price <= order_price
    - lower prices cannot match while higher grid prices for same runner remain open.
    """
    global MAKER_UNDER_LAY_GRID_ACTIVE_ORDERS
    global MAKER_UNDER_LAY_GRID_MATCHED_TOTAL
    global MAKER_UNDER_LAY_GRID_LIABILITY_TOTAL
    global MAKER_UNDER_LAY_GRID_LAST_PT

    MAKER_UNDER_LAY_GRID_ACTIVE_ORDERS = 0
    MAKER_UNDER_LAY_GRID_MATCHED_TOTAL = 0.0
    MAKER_UNDER_LAY_GRID_LIABILITY_TOTAL = 0.0

    order_model.by_key.clear()

    if not MAKER_UNDER_LAY_GRID_ENABLED:
        return

    frame_i = int(frame_no or 0)

    if pt is not None:
        if MAKER_UNDER_LAY_GRID_LAST_PT is not None and int(pt) < int(MAKER_UNDER_LAY_GRID_LAST_PT):
            MAKER_UNDER_LAY_GRID_ORDER_STATE.clear()
            MAKER_UNDER_LAY_GRID_PLACED_RUNNERS.clear()
        MAKER_UNDER_LAY_GRID_LAST_PT = int(pt)

    placed_now_keys: set[tuple[str, int, float, float]] = set()

    for state in markets.values():
        if not is_over_under_goals(state):
            continue

        for runner in state.runners.values():
            if not _maker_grid_is_under_runner(runner):
                continue

            rkey = _maker_grid_runner_key(str(state.market_id), runner)

            # One-time placement only.
            if rkey not in MAKER_UNDER_LAY_GRID_PLACED_RUNNERS:
                start_l = _maker_grid_best_lay_for_runner(runner)
                if start_l is None:
                    continue

                start_price = min(1.25, max(1.01, round(float(start_l), 2)))
                placed_any = False

                for price, stake in sorted(MAKER_UNDER_LAY_GRID_STAKES.items(), reverse=True):
                    price = round(float(price), 2)

                    if price > start_price:
                        continue

                    okey = _maker_grid_order_key(str(state.market_id), runner, price)
                    q0 = _maker_grid_visual_l_qty_for_runner_price(runner, price)

                    MAKER_UNDER_LAY_GRID_ORDER_STATE[okey] = {
                        "price": float(price),
                        "stake": float(stake),
                        "remaining": float(stake),
                        "matched": 0.0,
                        "q0_at_place": float(q0),
                        "q_ahead": float(q0),
                        "prev_l_qty": float(q0),
                        "current_l_qty": float(q0),
                        "placed_pt": int(pt) if pt is not None else -1,
                        "placed_frame": float(frame_i),
                        "status": "OPEN",
                    }

                    placed_now_keys.add(okey)
                    placed_any = True

                if placed_any:
                    MAKER_UNDER_LAY_GRID_PLACED_RUNNERS.add(rkey)

            visual_l_prices = [
                float(px)
                for px, qty in runner.available_to_back.items()
                if qty is not None and float(qty) > 0
            ]
            best_visual_l_price = max(visual_l_prices) if visual_l_prices else None

            runner_orders = [
                (okey, st)
                for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items()
                if okey[0] == str(state.market_id)
                and int(okey[1]) == int(runner.selection_id)
                and float(okey[2]) == float(runner.handicap or 0.0)
            ]
            runner_orders.sort(key=lambda item: float(item[0][3]), reverse=True)

            for okey, st in runner_orders:
                _market_id, _selection_id, _handicap, price = okey
                price = float(price)

                current_l_qty = _maker_grid_visual_l_qty_for_runner_price(runner, price)
                prev_l_qty = float(st.get("prev_l_qty", st.get("current_l_qty", current_l_qty)) or 0.0)

                placed_frame = int(float(st.get("placed_frame", frame_i) or frame_i))

                decrease = max(0.0, prev_l_qty - current_l_qty)

                # Queue ahead can shrink before price is reached, but that is NOT a fill.
                if decrease > 0:
                    q_ahead = float(st.get("q_ahead", st.get("q0_at_place", 0.0)) or 0.0)
                    consume_ahead = min(q_ahead, decrease)
                    q_ahead -= consume_ahead
                    decrease_after_q = decrease - consume_ahead
                    st["q_ahead"] = float(q_ahead)
                else:
                    decrease_after_q = 0.0

                higher_open_exists = any(
                    float(other_key[3]) > price
                    and float(other_state.get("remaining", 0.0) or 0.0) > 0
                    for other_key, other_state in runner_orders
                )

                price_reached = (
                    best_visual_l_price is not None
                    and float(best_visual_l_price) <= price + 1e-9
                )

                can_match = (
                    MAKER_UNDER_LAY_GRID_MATCHING_ENABLED
                    and frame_i > 1
                    and frame_i > placed_frame
                    and okey not in placed_now_keys
                    and price_reached
                    and not higher_open_exists
                )

                if can_match:
                    remaining_before = float(st.get("remaining", 0.0) or 0.0)

                    if remaining_before > 0 and decrease_after_q > 0:
                        matched_now = min(remaining_before, decrease_after_q)

                        if matched_now > 0:
                            st["matched"] = float(st.get("matched", 0.0) or 0.0) + matched_now
                            st["remaining"] = remaining_before - matched_now
                            st["status"] = "MATCHED" if float(st["remaining"]) <= 1e-9 else "PARTIAL"

                st["prev_l_qty"] = float(current_l_qty)
                st["current_l_qty"] = float(current_l_qty)

                remaining = float(st.get("remaining", 0.0) or 0.0)
                matched = float(st.get("matched", 0.0) or 0.0)

                MAKER_UNDER_LAY_GRID_MATCHED_TOTAL += matched

                if remaining > 0:
                    MAKER_UNDER_LAY_GRID_ACTIVE_ORDERS += 1
                    MAKER_UNDER_LAY_GRID_LIABILITY_TOTAL += remaining * max(0.0, price - 1.0)

                    order_model.by_key[okey] = MyOrdersAtPrice(
                        my_lay=float(remaining),
                        my_back=0.0,
                        matched=float(matched),
                    )


def _emit_stable_frame_end() -> None:
    if os.environ.get("STREEM_STABLE_WRAPPER") == "1":
        print("__STREEM_FRAME_END__", flush=True)





def _maker_under_lay_grid_canonical_totals_market_ids(
    markets: dict[str, MarketState] | None = None,
) -> set[str]:
    """
    Canonical visible totals market ids used for maker-grid portfolio stats.

    Sticky totals UI shows one canonical Over/Under market per line.
    This helper mirrors that idea:
      - only Over/Under Goals markets
      - only Under *.5 lines from 0.5 to 8.5
      - first market id per line in sorted order
    """
    if not markets:
        return set()

    by_line: dict[float, str] = {}

    for mid in sorted(markets.keys()):
        st = markets.get(mid)
        if st is None:
            continue
        if not is_over_under_goals(st):
            continue

        line = over_under_line(st)
        if line is None:
            continue

        line_f = float(line)
        if line_f < 0.5 or line_f > 8.5:
            continue

        # Keep only x.5 totals lines.
        if abs((line_f % 1.0) - 0.5) > 1e-9:
            continue

        under_runner_exists = any(
            _maker_grid_is_under_runner(r)
            for r in st.runners.values()
        )
        if not under_runner_exists:
            continue

        by_line.setdefault(line_f, str(st.market_id or mid))

    return set(by_line.values())


def _maker_under_lay_grid_visible_totals_exposure(
    markets: dict[str, MarketState] | None = None,
) -> dict[str, float]:
    """
    Maker-grid exposure restricted to canonical visible totals markets.

    This keeps:
      runner R/W
      ENGINE_V2 overlay
      OUTCOME line
    on the same source set.
    """
    visible_market_ids = _maker_under_lay_grid_canonical_totals_market_ids(markets)

    matched_stake_total = 0.0
    matched_liability_total = 0.0
    open_liability_total = 0.0
    active_orders = 0.0

    if not visible_market_ids:
        return {
            "matched_stake": 0.0,
            "matched_liability": 0.0,
            "open_liability": 0.0,
            "active_orders": 0.0,
        }

    for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items():
        try:
            market_id, _selection_id, _handicap, price = okey
            if str(market_id) not in visible_market_ids:
                continue

            price_f = float(price)
            matched = float(st.get("matched", 0.0) or 0.0)
            remaining = float(st.get("remaining", 0.0) or 0.0)
        except Exception:
            continue

        if matched > 0:
            matched_stake_total += matched
            matched_liability_total += matched * max(0.0, price_f - 1.0)

        if remaining > 0:
            active_orders += 1.0
            open_liability_total += remaining * max(0.0, price_f - 1.0)

    return {
        "matched_stake": matched_stake_total,
        "matched_liability": matched_liability_total,
        "open_liability": open_liability_total,
        "active_orders": active_orders,
    }



def _maker_under_lay_grid_outcome_pnls(
    markets: dict[str, MarketState] | None = None,
    max_goals: int = 9,
) -> dict[int, float]:
    """
    Portfolio outcome PnL by final total goals.

    Current strategy is LAY-only on Under *.5 Goals.

    For each matched LAY:
      if final_goals < line: Under wins, our LAY loses liability
      if final_goals >= line: Under loses, our LAY wins stake

    Uses only canonical visible totals markets, so OUTCOME matches visible R/W.
    """
    if not MAKER_UNDER_LAY_GRID_ENABLED:
        return {}

    if not MAKER_UNDER_LAY_GRID_MATCHING_ENABLED:
        return {}

    markets = markets or {}
    visible_market_ids = _maker_under_lay_grid_canonical_totals_market_ids(markets)
    if not visible_market_ids:
        return {}

    out = {g: 0.0 for g in range(int(max_goals) + 1)}
    has_matched = False

    for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items():
        try:
            market_id, _selection_id, _handicap, price = okey
            market_id_s = str(market_id)

            if market_id_s not in visible_market_ids:
                continue

            market_state = markets.get(market_id_s)
            if market_state is None:
                continue

            line = over_under_line(market_state)
            if line is None:
                continue

            line_f = float(line)
            price_f = float(price)
            matched = float(st.get("matched", 0.0) or 0.0)
        except Exception:
            continue

        if matched <= 0:
            continue

        has_matched = True
        liability = matched * max(0.0, price_f - 1.0)

        for goals in out:
            if float(goals) < line_f:
                out[goals] -= liability
            else:
                out[goals] += matched

    return out if has_matched else {}

def _maker_under_lay_grid_outcome_line(outcome_pnls: dict[int, float]) -> str:
    if not outcome_pnls:
        return ""

    max_goals = max(outcome_pnls)
    worst = min(outcome_pnls.values())
    best = max(outcome_pnls.values())

    parts = []
    for goals in sorted(outcome_pnls):
        label = f"G{goals}" if goals < max_goals else f"G{goals}+"
        parts.append(f"{label}={outcome_pnls[goals]:+.2f}")

    return (
        f"OUTCOME: worst={worst:+.2f} best={best:+.2f} "
        + " ".join(parts)
    )




SECOND_LEG_CS_DEBUG_STATE: dict[str, object] = {
    "epoch": 0,
    "profile_key": None,
    "state": "IDLE",
    "reason": "-",
    "decision_pt": None,
    "place_due_pt": None,
    "stale_due_pt": None,
    "last_logged_key": None,
    "package_preview": None,
    "package_decision_frame": None,
    "package_decision_pt": None,
    "filled_second_leg_actions": [],
    "filled_second_leg_seq": 0,
    "filled_totals_greenup_actions": [],
    "filled_totals_greenup_seq": 0,
    "filled_totals_risk_reduction_actions": [],
    "filled_totals_risk_reduction_seq": 0,
}

SECOND_LEG_CS_LAY_MAX_PRICE = 3.5
SECOND_LEG_CS_RECOVERY_LAY_MAX_PRICE = 3.5
SECOND_LEG_CS_RECOVERY_MAX_STAKE = 0.25
SECOND_LEG_CS_RECOVERY_MAX_LIABILITY = 0.50
SECOND_LEG_CS_RECOVERY_MIN_WORST_IMPROVEMENT = 0.005
SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_PRICE = 3.5
SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_STAKE = 0.25
SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_LIABILITY = 0.50
SECOND_LEG_CS_MAKER_LAY_RECOVERY_MIN_WORST_IMPROVEMENT = 0.005

SECOND_LEG_CS_TAKER_BACK_RECOVERY_MAX_STAKE = 0.25
SECOND_LEG_CS_TAKER_BACK_RECOVERY_MIN_WORST_IMPROVEMENT = 0.005
SECOND_LEG_CS_TAKER_BACK_RECOVERY_MAX_STAKE_LOSS = 0.25

SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MAX_CAPITAL = 2.00
SECOND_LEG_CS_BACK_BUCKET_RECOVERY_TARGET_IMPROVEMENT = 0.25
SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MIN_WORST_IMPROVEMENT = 0.005
SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MIN_ACTION_STAKE = 0.0001
SECOND_LEG_CS_RECOVERY_SHADOW_MAX_TOTAL_CAPITAL = 20.00
SECOND_LEG_MAX_CAPITAL_RATIO = 20.0
SECOND_LEG_PLACE_DELAY_MS = 5000
SECOND_LEG_REBUILD_DELAY_MS = 10000
SECOND_LEG_STALE_AFTER_PLACE_MS = 10000
SECOND_LEG_PREVIEW_MAX_ACTIONS = 8
SECOND_LEG_PREVIEW_MAX_BACK_STAKE_PER_ACTION = 2.0
SECOND_LEG_PREVIEW_SAFE_STAKE_HAIRCUT = 0.98
SECOND_LEG_PREVIEW_WORST_EPS = 0.01
SECOND_LEG_MAX_SINGLE_PARTIAL_WORST_DROP = 0.25
SECOND_LEG_MAX_TOTAL_FILLED_WORST_DROP = 0.50
SECOND_LEG_TOTALS_GREENUP_MIN_GREENUP = 0.005
SECOND_LEG_TOTALS_GREENUP_MIN_WORST_IMPROVEMENT = 0.005
SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE = 0.01

SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_WORST_IMPROVEMENT = 0.10
SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_COST = 1.00
SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_IMPROVEMENT_PER_COST = 0.25
SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_STAKE = 25.00
SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST = 2.00


def _second_leg_score_goals(name: str | None) -> int | None:
    if not name:
        return None
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(name))
    if not m:
        return None
    return int(m.group(1)) + int(m.group(2))


def _second_leg_runner_buckets(name: str | None, goal_bucket: int | None = None) -> list[str]:
    if goal_bucket is None:
        goal_bucket = _second_leg_score_goals(name)

    if goal_bucket is not None:
        g = int(goal_bucket)
        return ["G9+" if g >= 9 else f"G{g}"]

    low = (name or "").lower()

    if "any other home win" in low:
        return ["G4", "G5", "G6", "G7", "G8", "G9+"]

    if "any other away win" in low:
        return ["G4", "G5", "G6", "G7", "G8", "G9+"]

    if "any other draw" in low:
        return ["G8", "G9+"]

    return []


def _second_leg_cs_shadow_summary(
    markets: dict[str, MarketState] | None,
) -> dict[str, object]:
    markets = markets or {}

    runners = 0
    two_sided = 0
    one_sided = 0
    maker_back_candidates = 0
    taker_back_candidates = 0
    safe_lay_candidates = 0
    any_other = 0

    sample: list[dict[str, object]] = []

    for st in markets.values():
        if st is None:
            continue
        if not is_correct_score(st):
            continue

        for r in st.runners.values():
            runners += 1
            name = r.name or str(r.selection_id)
            low = name.lower()

            if "any other" in low:
                any_other += 1

            bb = best_level(r.available_to_back, side="BACK")
            bl = best_level(r.available_to_lay, side="LAY")

            has_bb = bool(bb is not None and float(bb[1]) > 0)
            has_bl = bool(bl is not None and float(bl[1]) > 0)

            if has_bb and has_bl:
                two_sided += 1
            elif has_bb or has_bl:
                one_sided += 1

            if has_bb:
                maker_back_candidates += 1

            if has_bl:
                taker_back_candidates += 1

            if has_bb and float(bb[0]) <= SECOND_LEG_CS_LAY_MAX_PRICE:
                safe_lay_candidates += 1

            if len(sample) < 8:
                goals = _second_leg_score_goals(name)
                sample.append(
                    {
                        "runner": name,
                        "buckets": _second_leg_runner_buckets(name, goals),
                        "bb": None if bb is None else [float(bb[0]), float(bb[1])],
                        "bl": None if bl is None else [float(bl[0]), float(bl[1])],
                    }
                )

    return {
        "runners": runners,
        "two_sided": two_sided,
        "one_sided": one_sided,
        "maker_back_candidates": maker_back_candidates,
        "taker_back_candidates": taker_back_candidates,
        "safe_lay_candidates": safe_lay_candidates,
        "any_other": any_other,
        "sample": sample,
    }



def _second_leg_bucket_label_from_goals(goals: int) -> str:
    return "G9+" if int(goals) >= 9 else f"G{int(goals)}"


def _second_leg_outcome_bucket_map(outcome_pnls: dict[int, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    if not outcome_pnls:
        return out

    max_goals = max(int(k) for k in outcome_pnls)
    for goals, val in sorted(outcome_pnls.items()):
        label = f"G{int(goals)}" if int(goals) < max_goals else f"G{int(goals)}+"
        out[label] = float(val)

    return out


def _second_leg_cs_virtual_rows(
    markets: dict[str, MarketState] | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    for st in (markets or {}).values():
        if st is None:
            continue
        if not is_correct_score(st):
            continue

        for r in st.runners.values():
            name = r.name or str(r.selection_id)
            goals = _second_leg_score_goals(name)
            buckets = _second_leg_runner_buckets(name, goals)

            if not buckets:
                continue

            bb = best_level(r.available_to_back, side="BACK")
            bl = best_level(r.available_to_lay, side="LAY")

            kind = "exact_score" if goals is not None else "any_other"

            rows.append(
                {
                    "market_id": str(st.market_id),
                    "selection_id": int(r.selection_id),
                    "runner": name,
                    "kind": kind,
                    "buckets": buckets,
                    "bb_price": None if bb is None else float(bb[0]),
                    "bb_size": None if bb is None else float(bb[1]),
                    "bl_price": None if bl is None else float(bl[0]),
                    "bl_size": None if bl is None else float(bl[1]),
                }
            )

    return rows


def _second_leg_apply_back_preview(
    *,
    virtual_rows: list[dict[str, object]],
    base_bucket: dict[str, float],
    package: list[dict[str, object]],
) -> dict[str, float]:
    # Build virtual profile from current bucket profile.
    virtual_profile: list[dict[str, object]] = []

    for row in virtual_rows:
        buckets = list(row.get("buckets") or [])
        if not buckets:
            continue

        # For Any Other mapped to multiple buckets, keep one virtual row per bucket.
        for bucket in buckets:
            virtual_profile.append(
                {
                    "runner": str(row["runner"]),
                    "bucket": str(bucket),
                    "value": float(base_bucket.get(str(bucket), 0.0)),
                }
            )

    for action in package:
        runner = str(action["runner"])
        price = float(action["price"])
        stake = float(action["stake"])

        for vr in virtual_profile:
            if str(vr["runner"]) == runner:
                vr["value"] = float(vr["value"]) + (price - 1.0) * stake
            else:
                vr["value"] = float(vr["value"]) - stake

    by_bucket: dict[str, list[float]] = {}
    for vr in virtual_profile:
        by_bucket.setdefault(str(vr["bucket"]), []).append(float(vr["value"]))

    out: dict[str, float] = {}
    for bucket in ["G0", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9+"]:
        vals = by_bucket.get(bucket) or []
        out[bucket] = min(vals) if vals else float(base_bucket.get(bucket, 0.0))

    return out


def _second_leg_candidate_package_preview(
    *,
    markets: dict[str, MarketState] | None,
    outcome_pnls: dict[int, float],
    filled_second_leg_actions: list[dict[str, object]] | None = None,
    filled_totals_greenup_actions: list[dict[str, object]] | None = None,
    filled_totals_risk_reduction_actions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """
    Read-only virtual-outcome MAKER BACK package validator.

    This does NOT place orders and does NOT modify order_model.

    Important:
      - bucket coverage is NOT enough
      - G1 means both 0-1 and 1-0
      - G2 means 0-2, 1-1, 2-0
      - Any Other runners are split into virtual bucket rows
      - package is rejected if any critical virtual outcome is uncovered
      - package is rejected if maker-back dutch condition is infeasible

    Current shadow rule:
      accept maker-back preview only if it can be filled without worsening current worst-case.
    """
    first_leg_bucket = _second_leg_outcome_bucket_map(outcome_pnls)
    base_bucket = dict(first_leg_bucket)

    # Conservative combined profile:
    # FIRST LEG + filled TOTALS GREENUP + already filled SECOND LEG BACK actions.
    filled_totals_greenup_actions = list(filled_totals_greenup_actions or [])
    if base_bucket and filled_totals_greenup_actions:
        base_bucket = _second_leg_apply_totals_back_actions(
            base_bucket=base_bucket,
            actions=filled_totals_greenup_actions,
        )

    filled_totals_risk_reduction_actions = list(filled_totals_risk_reduction_actions or [])
    if base_bucket and filled_totals_risk_reduction_actions:
        base_bucket = _second_leg_apply_totals_back_actions(
            base_bucket=base_bucket,
            actions=filled_totals_risk_reduction_actions,
        )

    filled_second_leg_actions = list(filled_second_leg_actions or [])
    if base_bucket and filled_second_leg_actions:
        base_bucket = _second_leg_apply_back_preview(
            virtual_rows=_second_leg_cs_virtual_rows(markets),
            base_bucket=base_bucket,
            package=filled_second_leg_actions,
        )

    total_worst_drop = 0.0

    if first_leg_bucket and base_bucket and filled_second_leg_actions:
        first_leg_worst = min(float(v) for v in first_leg_bucket.values())
        combined_worst = min(float(v) for v in base_bucket.values())
        total_worst_drop = float(first_leg_worst) - float(combined_worst)

        if total_worst_drop > SECOND_LEG_MAX_TOTAL_FILLED_WORST_DROP + SECOND_LEG_PREVIEW_WORST_EPS:
            return {
                "mode": "NO_SAFE_MAKER_BACK_PACKAGE",
                "reason": "total_filled_worst_drop_guard",
                "actions": [],
                "action_count": 0,
                "capital": 0.0,
                "worst": round(float(combined_worst), 6),
                "worst_if_full": round(float(combined_worst), 6),
                "best_if_full": round(max(float(v) for v in base_bucket.values()), 6),
                "profile_if_full": {k: round(float(v), 6) for k, v in base_bucket.items()},
                "first_leg_worst": round(float(first_leg_worst), 6),
                "combined_worst": round(float(combined_worst), 6),
                "total_worst_drop": round(float(total_worst_drop), 6),
                "max_total_filled_worst_drop": round(float(SECOND_LEG_MAX_TOTAL_FILLED_WORST_DROP), 6),
                "filled_second_leg_count": len(filled_second_leg_actions),
                "filled_second_leg_stake": round(sum(float(x.get("stake", 0.0) or 0.0) for x in filled_second_leg_actions), 6),
            }

    remaining_total_worst_budget = max(
        0.0,
        float(SECOND_LEG_MAX_TOTAL_FILLED_WORST_DROP) - max(0.0, float(total_worst_drop)),
    )
    effective_single_partial_worst_drop = min(
        float(SECOND_LEG_MAX_SINGLE_PARTIAL_WORST_DROP),
        float(remaining_total_worst_budget),
    )

    if filled_second_leg_actions and effective_single_partial_worst_drop <= SECOND_LEG_PREVIEW_WORST_EPS:
        current_worst = min(float(v) for v in base_bucket.values()) if base_bucket else 0.0
        return {
            "mode": "NO_SAFE_MAKER_BACK_PACKAGE",
            "reason": "total_filled_worst_budget_exhausted",
            "actions": [],
            "action_count": 0,
            "capital": 0.0,
            "worst": round(float(current_worst), 6),
            "worst_if_full": round(float(current_worst), 6),
            "best_if_full": round(max(float(v) for v in base_bucket.values()), 6) if base_bucket else 0.0,
            "profile_if_full": {k: round(float(v), 6) for k, v in base_bucket.items()},
            "total_worst_drop": round(float(total_worst_drop), 6),
            "remaining_total_worst_budget": round(float(remaining_total_worst_budget), 6),
            "effective_single_partial_worst_drop": round(float(effective_single_partial_worst_drop), 6),
            "max_total_filled_worst_drop": round(float(SECOND_LEG_MAX_TOTAL_FILLED_WORST_DROP), 6),
            "filled_second_leg_count": len(filled_second_leg_actions),
            "filled_second_leg_stake": round(sum(float(x.get("stake", 0.0) or 0.0) for x in filled_second_leg_actions), 6),
        }

    if not base_bucket:
        return {
            "mode": "IDLE",
            "reason": "no_outcome",
            "actions": [],
            "action_count": 0,
            "capital": 0.0,
        }

    worst = min(float(v) for v in base_bucket.values())
    best = max(float(v) for v in base_bucket.values())

    if worst >= 0:
        return {
            "mode": "CLOSED",
            "reason": "profile_non_negative",
            "actions": [],
            "action_count": 0,
            "capital": 0.0,
            "worst": round(worst, 6),
            "worst_if_full": round(worst, 6),
            "best_if_full": round(best, 6),
            "profile_if_full": {k: round(v, 6) for k, v in base_bucket.items()},
        }

    residual_abs = abs(float(worst))
    max_capital = residual_abs * SECOND_LEG_MAX_CAPITAL_RATIO

    source_rows = _second_leg_cs_virtual_rows(markets)

    virtual_rows: list[dict[str, object]] = []
    for row in source_rows:
        buckets = list(row.get("buckets") or [])
        for bucket in buckets:
            bucket_s = str(bucket)
            virtual_rows.append(
                {
                    "runner": str(row.get("runner")),
                    "bucket": bucket_s,
                    "kind": str(row.get("kind") or ""),
                    "base": float(base_bucket.get(bucket_s, 0.0)),
                    "bb_price": row.get("bb_price"),
                    "bb_size": row.get("bb_size"),
                    "bl_price": row.get("bl_price"),
                    "bl_size": row.get("bl_size"),
                }
            )

    # Critical rows are the exact virtual outcomes currently equal to worst-case.
    # If any of them is unbacked while total BACK stake > 0, worst-case worsens.
    critical_rows = [
        r for r in virtual_rows
        if float(r["base"]) <= float(worst) + 1e-9
    ]

    critical_by_bucket: dict[str, int] = {}
    for r in critical_rows:
        critical_by_bucket[str(r["bucket"])] = critical_by_bucket.get(str(r["bucket"]), 0) + 1

    missing: list[dict[str, object]] = []
    selected_by_runner: dict[str, dict[str, object]] = {}

    for r in critical_rows:
        runner = str(r["runner"])
        price = r.get("bb_price")

        if price is None or float(price) <= 1.0:
            missing.append(
                {
                    "runner": runner,
                    "bucket": str(r["bucket"]),
                    "reason": "missing_maker_back_price",
                }
            )
            continue

        # One BACK order on an Any Other runner covers all its virtual bucket rows.
        if runner not in selected_by_runner:
            selected_by_runner[runner] = {
                "mode": "MAKER",
                "side": "BACK",
                "runner": runner,
                "price": float(price),
                "queue": 0.0 if r.get("bb_size") is None else float(r.get("bb_size") or 0.0),
                "buckets": set(),
                "critical_buckets": set(),
                "kind": str(r.get("kind") or ""),
            }

        selected_by_runner[runner]["buckets"].add(str(r["bucket"]))
        selected_by_runner[runner]["critical_buckets"].add(str(r["bucket"]))

    inverse_sum = 0.0
    for x in selected_by_runner.values():
        inverse_sum += 1.0 / max(1e-12, float(x["price"]))

    # MAKER BACK only can preserve current worst only if sum(1/price) < 1.
    # If >= 1, the minimum required stakes on critical runners exceed total stake.
    if missing or inverse_sum >= 1.0:
        reason = "critical_virtual_outcomes_missing_maker_back" if missing else "maker_back_dutch_infeasible"

        return {
            "mode": "NO_SAFE_MAKER_BACK_PACKAGE",
            "reason": reason,
            "actions": [],
            "action_count": 0,
            "capital": 0.0,
            "max_capital": round(max_capital, 6),
            "worst": round(worst, 6),
            "worst_if_full": round(worst, 6),
            "best_if_full": round(best, 6),
            "profile_if_full": {k: round(v, 6) for k, v in base_bucket.items()},
            "virtual_row_count": len(virtual_rows),
            "critical_virtual_row_count": len(critical_rows),
            "critical_by_bucket": dict(sorted(critical_by_bucket.items())),
            "maker_back_runner_count": len(selected_by_runner),
            "maker_back_inverse_sum": round(inverse_sum, 6),
            "missing": missing[:20],
            "diagnostic": {
                "rule": "virtual_outcome_maker_back_validator",
                "explain": "Every critical virtual outcome must be covered. Maker BACK is feasible only if sum(1 / maker_price) < 1.",
            },
        }

    # If we ever get here, maker-back-only is mathematically feasible for
    # preserving current worst-case. Build a tiny safe shadow package.
    # This branch is expected to be rare.
    non_selected_buffer = None
    selected_runners = set(selected_by_runner.keys())

    for r in virtual_rows:
        if str(r["runner"]) in selected_runners:
            continue

        buffer = float(r["base"]) - float(worst)
        if buffer <= 0:
            non_selected_buffer = 0.0 if non_selected_buffer is None else min(non_selected_buffer, 0.0)
        else:
            non_selected_buffer = buffer if non_selected_buffer is None else min(non_selected_buffer, buffer)

    if non_selected_buffer is None:
        non_selected_buffer = residual_abs

    total_stake = min(
        residual_abs,
        max_capital,
        float(non_selected_buffer),
    )

    # Shadow preview safety:
    # keep a tiny buffer so rounding/printing precision does not create
    # artificial REJECTED_WORSE_PROFILE when mathematically the package
    # only preserves the current worst-case.
    total_stake *= SECOND_LEG_PREVIEW_SAFE_STAKE_HAIRCUT

    if total_stake <= 1e-9:
        return {
            "mode": "NO_SAFE_MAKER_BACK_PACKAGE",
            "reason": "no_positive_safe_stake_size",
            "actions": [],
            "action_count": 0,
            "capital": 0.0,
            "max_capital": round(max_capital, 6),
            "worst": round(worst, 6),
            "worst_if_full": round(worst, 6),
            "best_if_full": round(best, 6),
            "profile_if_full": {k: round(v, 6) for k, v in base_bucket.items()},
            "virtual_row_count": len(virtual_rows),
            "critical_virtual_row_count": len(critical_rows),
            "critical_by_bucket": dict(sorted(critical_by_bucket.items())),
            "maker_back_runner_count": len(selected_by_runner),
            "maker_back_inverse_sum": round(inverse_sum, 6),
        }

    actions: list[dict[str, object]] = []
    min_total = 0.0

    # Minimum stake per runner to avoid worsening critical rows:
    # price * stake >= total_stake
    for runner, x in selected_by_runner.items():
        price = float(x["price"])
        stake = total_stake / price
        min_total += stake

        actions.append(
            {
                "mode": "MAKER",
                "side": "BACK",
                "runner": runner,
                "buckets": sorted(str(b) for b in x["buckets"]),
                "critical_buckets": sorted(str(b) for b in x["critical_buckets"]),
                "price": round(price, 6),
                "stake": round(stake, 6),
                "queue": round(float(x["queue"]), 6),
                "capital": round(stake, 6),
            }
        )

    # Allocate the small leftover to the best-price runner.
    leftover = max(0.0, total_stake - min_total)
    if leftover > 1e-9 and actions:
        best_i = max(range(len(actions)), key=lambda i: float(actions[i]["price"]))
        actions[best_i]["stake"] = round(float(actions[best_i]["stake"]) + leftover, 6)
        actions[best_i]["capital"] = round(float(actions[best_i]["capital"]) + leftover, 6)

    profile_if_full = _second_leg_apply_back_preview(
        virtual_rows=source_rows,
        base_bucket=base_bucket,
        package=actions,
    )

    worst_if_full = min(profile_if_full.values()) if profile_if_full else worst
    best_if_full = max(profile_if_full.values()) if profile_if_full else best

    # Maker packages are NOT atomic.
    # Stress every possible single-action partial fill.
    # If one isolated fill worsens current worst-case, the package is unsafe,
    # because live matching may fill exactly that one order first.
    partial_fill_tests: list[dict[str, object]] = []
    partial_worst_min = float(worst)

    for action in actions:
        single_profile = _second_leg_apply_back_preview(
            virtual_rows=source_rows,
            base_bucket=base_bucket,
            package=[action],
        )
        single_worst = min(single_profile.values()) if single_profile else float(worst)
        partial_worst_min = min(partial_worst_min, float(single_worst))

        partial_fill_tests.append(
            {
                "runner": action.get("runner"),
                "side": action.get("side"),
                "mode": action.get("mode"),
                "price": action.get("price"),
                "stake": action.get("stake"),
                "worst_if_single_fill": round(float(single_worst), 6),
            }
        )

    if float(partial_worst_min) < float(worst) - SECOND_LEG_PREVIEW_WORST_EPS:
        observed_drop = float(worst) - float(partial_worst_min)

        if observed_drop > 0:
            scale = min(1.0, effective_single_partial_worst_drop / observed_drop)
        else:
            scale = 1.0

        micro_actions: list[dict[str, object]] = []
        for action in actions:
            a = dict(action)
            old_stake = float(a.get("stake", 0.0) or 0.0)
            new_stake = old_stake * scale
            if new_stake <= 1e-9:
                continue
            a["stake"] = round(new_stake, 6)
            a["capital"] = round(new_stake, 6)
            micro_actions.append(a)

        micro_profile_if_full = _second_leg_apply_back_preview(
            virtual_rows=source_rows,
            base_bucket=base_bucket,
            package=micro_actions,
        )
        micro_worst_if_full = min(micro_profile_if_full.values()) if micro_profile_if_full else worst
        micro_best_if_full = max(micro_profile_if_full.values()) if micro_profile_if_full else best

        micro_partial_tests: list[dict[str, object]] = []
        micro_partial_worst_min = float(worst)

        for action in micro_actions:
            single_profile = _second_leg_apply_back_preview(
                virtual_rows=source_rows,
                base_bucket=base_bucket,
                package=[action],
            )
            single_worst = min(single_profile.values()) if single_profile else float(worst)
            micro_partial_worst_min = min(micro_partial_worst_min, float(single_worst))
            micro_partial_tests.append(
                {
                    "runner": action.get("runner"),
                    "side": action.get("side"),
                    "mode": action.get("mode"),
                    "price": action.get("price"),
                    "stake": action.get("stake"),
                    "worst_if_single_fill": round(float(single_worst), 6),
                }
            )

        if float(micro_partial_worst_min) < float(worst) - effective_single_partial_worst_drop - SECOND_LEG_PREVIEW_WORST_EPS:
            return {
                "mode": "REJECTED_PARTIAL_FILL_UNSAFE",
                "reason": "micro_scale_failed_partial_worst_guard",
                "rejected_actions": actions,
                "actions": [],
                "action_count": 0,
                "capital": 0.0,
                "rejected_capital": round(sum(float(a.get("capital", 0.0)) for a in actions), 6),
                "max_capital": round(max_capital, 6),
                "worst": round(worst, 6),
                "worst_if_full": round(float(worst_if_full), 6),
                "partial_worst_min": round(float(partial_worst_min), 6),
                "micro_partial_worst_min": round(float(micro_partial_worst_min), 6),
                "effective_single_partial_worst_drop": round(float(effective_single_partial_worst_drop), 6),
                "remaining_total_worst_budget": round(float(remaining_total_worst_budget), 6),
                "total_worst_drop": round(float(total_worst_drop), 6),
                "best_if_full": round(float(best_if_full), 6),
                "profile_if_full": {k: round(v, 6) for k, v in profile_if_full.items()},
                "virtual_row_count": len(virtual_rows),
                "critical_virtual_row_count": len(critical_rows),
                "critical_by_bucket": dict(sorted(critical_by_bucket.items())),
                "maker_back_runner_count": len(selected_by_runner),
                "maker_back_inverse_sum": round(inverse_sum, 6),
                "partial_fill_tests": partial_fill_tests[:20],
                "micro_partial_fill_tests": micro_partial_tests[:20],
            }

        return {
            "mode": "MAKER_BACK_MICRO_PREVIEW",
            "reason": "scaled_to_single_partial_worst_guard",
            "actions": micro_actions,
            "action_count": len(micro_actions),
            "capital": round(sum(float(a.get("capital", 0.0)) for a in micro_actions), 6),
            "max_capital": round(max_capital, 6),
            "worst": round(worst, 6),
            "worst_if_full": round(float(micro_worst_if_full), 6),
            "best_if_full": round(float(micro_best_if_full), 6),
            "partial_worst_min": round(float(micro_partial_worst_min), 6),
            "max_single_partial_worst_drop": round(float(effective_single_partial_worst_drop), 6),
            "configured_max_single_partial_worst_drop": round(float(SECOND_LEG_MAX_SINGLE_PARTIAL_WORST_DROP), 6),
            "remaining_total_worst_budget": round(float(remaining_total_worst_budget), 6),
            "total_worst_drop": round(float(total_worst_drop), 6),
            "scale_from_full_package": round(float(scale), 6),
            "full_package_rejected_capital": round(sum(float(a.get("capital", 0.0)) for a in actions), 6),
            "profile_if_full": {k: round(v, 6) for k, v in micro_profile_if_full.items()},
            "virtual_row_count": len(virtual_rows),
            "critical_virtual_row_count": len(critical_rows),
            "critical_by_bucket": dict(sorted(critical_by_bucket.items())),
            "maker_back_runner_count": len(selected_by_runner),
            "maker_back_inverse_sum": round(inverse_sum, 6),
            "partial_fill_tests": micro_partial_tests[:20],
        }

    if float(worst_if_full) < float(worst) - SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "REJECTED_WORSE_PROFILE",
            "reason": "virtual_outcome_validation_failed_after_stake_build",
            "rejected_actions": actions,
            "actions": [],
            "action_count": 0,
            "capital": 0.0,
            "rejected_capital": round(sum(float(a.get("capital", 0.0)) for a in actions), 6),
            "max_capital": round(max_capital, 6),
            "worst": round(worst, 6),
            "worst_if_full": round(float(worst_if_full), 6),
            "best_if_full": round(float(best_if_full), 6),
            "profile_if_full": {k: round(v, 6) for k, v in profile_if_full.items()},
            "virtual_row_count": len(virtual_rows),
            "critical_virtual_row_count": len(critical_rows),
            "critical_by_bucket": dict(sorted(critical_by_bucket.items())),
            "maker_back_runner_count": len(selected_by_runner),
            "maker_back_inverse_sum": round(inverse_sum, 6),
        }

    return {
        "mode": "MAKER_BACK_VIRTUAL_PREVIEW",
        "reason": "virtual_outcome_maker_back_feasible_shadow",
        "actions": actions,
        "action_count": len(actions),
        "capital": round(sum(float(a.get("capital", 0.0)) for a in actions), 6),
        "max_capital": round(max_capital, 6),
        "worst": round(worst, 6),
        "worst_if_full": round(float(worst_if_full), 6),
        "best_if_full": round(float(best_if_full), 6),
        "profile_if_full": {k: round(v, 6) for k, v in profile_if_full.items()},
        "virtual_row_count": len(virtual_rows),
        "critical_virtual_row_count": len(critical_rows),
        "critical_by_bucket": dict(sorted(critical_by_bucket.items())),
        "maker_back_runner_count": len(selected_by_runner),
        "maker_back_inverse_sum": round(inverse_sum, 6),
    }

def _second_leg_profile_key(outcome_pnls: dict[int, float]) -> tuple[float, ...]:
    return tuple(round(float(outcome_pnls.get(i, 0.0)), 6) for i in range(10))






def _second_leg_combined_bucket_profile(
    *,
    markets: dict[str, MarketState] | None,
    outcome_pnls: dict[int, float],
    filled_second_leg_actions: list[dict[str, object]] | None,
    filled_totals_greenup_actions: list[dict[str, object]] | None = None,
    filled_totals_risk_reduction_actions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    base_bucket = _second_leg_outcome_bucket_map(outcome_pnls)
    filled = list(filled_second_leg_actions or [])
    filled_totals = list(filled_totals_greenup_actions or [])
    filled_trr = list(filled_totals_risk_reduction_actions or [])

    if not base_bucket:
        return {
            "ok": False,
            "reason": "no_base_bucket",
            "profile": {},
            "worst": None,
            "best": None,
        }

    combined = dict(base_bucket)

    if filled_totals:
        combined = _second_leg_apply_totals_back_actions(
            base_bucket=combined,
            actions=filled_totals,
        )

    if filled_trr:
        combined = _second_leg_apply_totals_back_actions(
            base_bucket=combined,
            actions=filled_trr,
        )

    if filled:
        combined = _second_leg_apply_back_preview(
            virtual_rows=_second_leg_cs_virtual_rows(markets),
            base_bucket=combined,
            package=filled,
        )

    worst = min(float(v) for v in combined.values()) if combined else None
    best = max(float(v) for v in combined.values()) if combined else None

    base_worst = min(float(v) for v in base_bucket.values()) if base_bucket else None
    base_best = max(float(v) for v in base_bucket.values()) if base_bucket else None

    worst_drop = None
    if worst is not None and base_worst is not None:
        worst_drop = float(base_worst) - float(worst)

    return {
        "ok": True,
        "reason": "first_leg_plus_filled_second_leg" if filled else "first_leg_only",
        "filled_count": len(filled),
        "filled_stake": round(sum(float(x.get("stake", 0.0) or 0.0) for x in filled), 6),
        "filled_totals_greenup_count": len(filled_totals),
        "filled_totals_greenup_stake": round(sum(float(x.get("stake", 0.0) or 0.0) for x in filled_totals), 6),
        "filled_totals_greenup_greenup": round(sum(float(x.get("greenup", 0.0) or 0.0) for x in filled_totals), 6),
        "filled_totals_risk_reduction_count": len(filled_trr),
        "filled_totals_risk_reduction_stake": round(sum(float(x.get("stake", 0.0) or 0.0) for x in filled_trr), 6),
        "filled_totals_risk_reduction_cost": round(sum(float(x.get("cost", 0.0) or 0.0) for x in filled_trr), 6),
        "worst": None if worst is None else round(float(worst), 6),
        "best": None if best is None else round(float(best), 6),
        "base_worst": None if base_worst is None else round(float(base_worst), 6),
        "base_best": None if base_best is None else round(float(base_best), 6),
        "worst_drop_from_first_leg": None if worst_drop is None else round(float(worst_drop), 6),
        "max_total_filled_worst_drop": round(float(SECOND_LEG_MAX_TOTAL_FILLED_WORST_DROP), 6),
        "profile": {k: round(float(v), 6) for k, v in combined.items()},
        "base_profile": {k: round(float(v), 6) for k, v in base_bucket.items()},
    }



def _second_leg_bucket_goal_number(bucket: str) -> int:
    b = str(bucket)
    if b == "G9+":
        return 9
    if b.startswith("G"):
        try:
            return int(b[1:])
        except Exception:
            return 9
    return 9


def _second_leg_apply_totals_back_preview(
    *,
    base_bucket: dict[str, float],
    line: float,
    price: float,
    stake: float,
) -> dict[str, float]:
    """
    Preview BACK on Under line at given price/stake.

    If final goals < line:
      BACK wins stake * (price - 1)
    Else:
      BACK loses stake
    """
    out = dict(base_bucket)

    for bucket in list(out.keys()):
        goals = _second_leg_bucket_goal_number(bucket)
        if float(goals) < float(line):
            out[bucket] = float(out[bucket]) + float(stake) * (float(price) - 1.0)
        else:
            out[bucket] = float(out[bucket]) - float(stake)

    return out


def _second_leg_totals_under_runner_for_market(
    market: MarketState,
    selection_id: int | None = None,
    handicap: float | None = None,
) -> RunnerState | None:
    if market is None:
        return None

    # Prefer exact selection/handicap from maker order key.
    for r in market.runners.values():
        try:
            if selection_id is not None and int(r.selection_id) != int(selection_id):
                continue
            if handicap is not None and abs(float(r.handicap or 0.0) - float(handicap or 0.0)) > 1e-9:
                continue
            return r
        except Exception:
            continue

    # Fallback: Under runner by name.
    for r in market.runners.values():
        name = str(r.name or "")
        if name.lower().startswith("under "):
            return r

    return None


def _second_leg_totals_greenup_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
    filled_totals_greenup_actions: list[dict[str, object]] | None = None,
    filled_totals_risk_reduction_actions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """
    Read-only totals greenup preview v0.

    Priority use-case:
      - goal / dangerous move / totals shock
      - current UNDER LAY first-leg can be closed by BACK taker
      - show candidates that either:
          a) have positive line greenup
          b) improve current SLC worst

    Does NOT place orders.
    Does NOT mutate maker state.
    """
    profile = second_leg_combined_profile.get("profile")
    if not isinstance(profile, dict) or not profile:
        return {
            "mode": "NO_TOTALS_GREENUP",
            "reason": "missing_combined_profile",
            "actions": [],
            "action_count": 0,
        }

    base_bucket = {str(k): float(v) for k, v in profile.items()}
    current_worst = min(base_bucket.values())
    current_best = max(base_bucket.values())

    if not MAKER_UNDER_LAY_GRID_ENABLED or not MAKER_UNDER_LAY_GRID_MATCHING_ENABLED:
        return {
            "mode": "NO_TOTALS_GREENUP",
            "reason": "maker_under_lay_grid_not_active",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    markets = markets or {}

    try:
        visible_market_ids = _maker_under_lay_grid_canonical_totals_market_ids(markets)
    except Exception:
        visible_market_ids = set(str(k) for k in markets.keys())

    grouped: dict[tuple[str, int, float], dict[str, float]] = {}

    filled_totals_by_key = _second_leg_totals_back_stake_by_key(
        filled_totals_greenup_actions,
        filled_totals_risk_reduction_actions,
    )

    for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items():
        try:
            market_id, selection_id, handicap, lay_price = okey
            market_id_s = str(market_id)

            if visible_market_ids and market_id_s not in visible_market_ids:
                continue

            matched = float(st.get("matched", 0.0) or 0.0)
            if matched <= 1e-9:
                continue

            lay_price_f = float(lay_price)
            key = (market_id_s, int(selection_id), float(handicap or 0.0))

            row = grouped.setdefault(
                key,
                {
                    "matched_stake": 0.0,
                    "matched_liability": 0.0,
                    "matched_orders": 0.0,
                },
            )

            row["matched_stake"] += matched
            row["matched_liability"] += matched * max(0.0, lay_price_f - 1.0)
            row["matched_orders"] += 1.0

        except Exception:
            continue

    candidates: list[dict[str, object]] = []

    for (market_id, selection_id, handicap), row in grouped.items():
        market = markets.get(market_id)
        if market is None:
            continue

        try:
            line = float(over_under_line(market))
        except Exception:
            continue

        runner = _second_leg_totals_under_runner_for_market(
            market,
            selection_id=selection_id,
            handicap=handicap,
        )
        if runner is None:
            continue

        bl = best_level(runner.available_to_lay, side="LAY")
        if bl is None:
            continue

        try:
            close_price = float(bl[0])
            close_size = float(bl[1])
        except Exception:
            continue

        if close_price <= 1.0 or close_size <= 1e-9:
            continue

        matched_stake = float(row.get("matched_stake", 0.0) or 0.0)
        matched_liability = float(row.get("matched_liability", 0.0) or 0.0)

        if matched_stake <= 1e-9:
            continue

        avg_lay_price = 1.0 + matched_liability / matched_stake
        full_hedge_back_stake = (matched_stake + matched_liability) / close_price
        already_back_stake = float(filled_totals_by_key.get((market_id, int(selection_id), float(handicap)), 0.0))
        residual_hedge_back_stake = max(0.0, float(full_hedge_back_stake) - already_back_stake)

        hedge_back_stake = min(float(residual_hedge_back_stake), float(close_size))

        if hedge_back_stake <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
            continue

        line_greenup_if_full = matched_stake - full_hedge_back_stake
        residual_after_action = max(0.0, float(residual_hedge_back_stake) - float(hedge_back_stake))
        hedge_completion = residual_after_action <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE

        # Count greenup only when this action completes the residual hedge.
        # Partial hedge can still be useful for SLC improvement, but it is not realized greenup.
        greenup = line_greenup_if_full if hedge_completion else 0.0

        trial_profile = _second_leg_apply_totals_back_preview(
            base_bucket=base_bucket,
            line=line,
            price=close_price,
            stake=hedge_back_stake,
        )

        worst_if_full = min(float(v) for v in trial_profile.values())
        best_if_full = max(float(v) for v in trial_profile.values())
        worst_improvement = float(worst_if_full) - float(current_worst)

        candidates.append(
            {
                "mode": "TAKER_BACK_TOTALS_GREENUP",
                "side": "BACK",
                "market_id": market_id,
                "runner": str(runner.name or runner.selection_id),
                "selection_id": int(selection_id),
                "handicap": float(handicap),
                "line": round(float(line), 6),
                "price": round(float(close_price), 6),
                "available": round(float(close_size), 6),
                "stake": round(float(hedge_back_stake), 6),
                "full_hedge_back_stake": round(float(full_hedge_back_stake), 6),
                "already_back_stake": round(float(already_back_stake), 6),
                "residual_hedge_back_stake": round(float(residual_hedge_back_stake), 6),
                "residual_after_action": round(float(residual_after_action), 6),
                "hedge_completion": bool(hedge_completion),
                "line_greenup_if_full": round(float(line_greenup_if_full), 6),
                "matched_lay_stake": round(float(matched_stake), 6),
                "matched_lay_liability": round(float(matched_liability), 6),
                "avg_lay_price": round(float(avg_lay_price), 6),
                "greenup": round(float(greenup), 6),
                "current_worst": round(float(current_worst), 6),
                "worst_if_full": round(float(worst_if_full), 6),
                "best_if_full": round(float(best_if_full), 6),
                "worst_improvement": round(float(worst_improvement), 6),
                "profile_if_full": {k: round(float(v), 6) for k, v in trial_profile.items()},
            }
        )

    candidates.sort(
        key=lambda x: (
            float(x.get("greenup") or 0.0) > SECOND_LEG_TOTALS_GREENUP_MIN_GREENUP,
            float(x.get("worst_improvement") or 0.0),
            float(x.get("greenup") or 0.0),
            float(x.get("stake") or 0.0),
        ),
        reverse=True,
    )

    accepted = [
        c for c in candidates
        if (
            float(c.get("stake") or 0.0) > SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE
            and float(c.get("line_greenup_if_full", c.get("greenup")) or 0.0) >= SECOND_LEG_TOTALS_GREENUP_MIN_GREENUP
            and float(c.get("worst_improvement") or 0.0) >= -SECOND_LEG_PREVIEW_WORST_EPS
        )
    ]

    if not accepted:
        return {
            "mode": "NO_TOTALS_GREENUP",
            "reason": "no_positive_safe_totals_greenup",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "top_candidates": candidates[:10],
        }

    best = accepted[0]

    return {
        "mode": "TAKER_BACK_TOTALS_GREENUP_PREVIEW",
        "reason": "positive_totals_greenup_or_worst_improvement",
        "actions": [best],
        "action_count": 1,
        "current_worst": round(float(current_worst), 6),
        "current_best": round(float(current_best), 6),
        "greenup": best.get("greenup"),
        "worst_if_full": best.get("worst_if_full"),
        "best_if_full": best.get("best_if_full"),
        "worst_improvement": best.get("worst_improvement"),
        "top_candidates": candidates[:10],
    }



def _second_leg_apply_totals_back_actions(
    *,
    base_bucket: dict[str, float],
    actions: list[dict[str, object]] | None,
) -> dict[str, float]:
    out = dict(base_bucket)

    for a in list(actions or []):
        try:
            line = float(a.get("line"))
            price = float(a.get("price"))
            stake = float(a.get("stake"))
        except Exception:
            continue

        if price <= 1.0 or stake <= 0:
            continue

        out = _second_leg_apply_totals_back_preview(
            base_bucket=out,
            line=line,
            price=price,
            stake=stake,
        )

    return out


def _second_leg_compact_totals_greenup_actions(actions: list[dict[str, object]] | None) -> dict[str, object]:
    rows = [
        x for x in list(actions or [])
        if float(x.get("stake", 0.0) or 0.0) > SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE
    ]
    total_stake = sum(float(x.get("stake", 0.0) or 0.0) for x in rows)

    # Do NOT sum full line greenup on every partial action.
    # Count latest completed hedge greenup per line key only.
    latest_completed_greenup_by_key: dict[tuple[str, int, float], float] = {}
    for x in rows:
        try:
            key = (
                str(x.get("market_id")),
                int(x.get("selection_id")),
                float(x.get("handicap") or 0.0),
            )
            if bool(x.get("hedge_completion")):
                latest_completed_greenup_by_key[key] = float(x.get("line_greenup_if_full", x.get("greenup", 0.0)) or 0.0)
        except Exception:
            continue

    total_greenup = sum(float(v) for v in latest_completed_greenup_by_key.values())

    top = []
    for a in rows[-20:]:
        top.append(
            {
                "seq": a.get("seq"),
                "frame": a.get("frame"),
                "pt": a.get("pt"),
                "market_id": a.get("market_id"),
                "runner": a.get("runner"),
                "line": a.get("line"),
                "price": a.get("price"),
                "stake": a.get("stake"),
                "greenup": a.get("greenup"),
                "line_greenup_if_full": a.get("line_greenup_if_full"),
                "hedge_completion": a.get("hedge_completion"),
                "residual_after_action": a.get("residual_after_action"),
                "worst_improvement": a.get("worst_improvement"),
                "reason": a.get("reason"),
            }
        )

    return {
        "count": len(rows),
        "stake": round(total_stake, 6),
        "greenup": round(total_greenup, 6),
        "top": top,
    }



def _second_leg_totals_back_stake_by_key(
    *action_lists: list[dict[str, object]] | None,
) -> dict[tuple[str, int, float], float]:
    out: dict[tuple[str, int, float], float] = {}

    for actions in action_lists:
        for a in list(actions or []):
            try:
                stake = float(a.get("stake", 0.0) or 0.0)
                if stake <= 0:
                    continue

                key = (
                    str(a.get("market_id")),
                    int(a.get("selection_id")),
                    float(a.get("handicap") or 0.0),
                )
                out[key] = out.get(key, 0.0) + stake
            except Exception:
                continue

    return out


def _second_leg_compact_totals_risk_reduction_actions(actions: list[dict[str, object]] | None) -> dict[str, object]:
    rows = [
        x for x in list(actions or [])
        if float(x.get("stake", 0.0) or 0.0) > SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE
    ]
    total_stake = sum(float(x.get("stake", 0.0) or 0.0) for x in rows)
    total_cost = sum(float(x.get("cost", 0.0) or 0.0) for x in rows)

    top = []
    for a in rows[-20:]:
        top.append(
            {
                "seq": a.get("seq"),
                "frame": a.get("frame"),
                "pt": a.get("pt"),
                "market_id": a.get("market_id"),
                "runner": a.get("runner"),
                "line": a.get("line"),
                "price": a.get("price"),
                "stake": a.get("stake"),
                "cost": a.get("cost"),
                "line_greenup_if_full": a.get("line_greenup_if_full"),
                "worst_improvement": a.get("worst_improvement"),
                "improvement_per_cost": a.get("improvement_per_cost"),
                "hedge_completion": a.get("hedge_completion"),
                "residual_after_action": a.get("residual_after_action"),
                "reason": a.get("reason"),
            }
        )

    return {
        "count": len(rows),
        "stake": round(total_stake, 6),
        "cost": round(total_cost, 6),
        "top": top,
    }


def _second_leg_compact_filled_actions(actions: list[dict[str, object]] | None) -> dict[str, object]:
    rows = list(actions or [])
    total_stake = sum(float(x.get("stake", 0.0) or 0.0) for x in rows)

    top = []
    for a in rows[-20:]:
        top.append(
            {
                "seq": a.get("seq"),
                "frame": a.get("frame"),
                "pt": a.get("pt"),
                "mode": a.get("mode"),
                "side": a.get("side"),
                "runner": a.get("runner"),
                "price": a.get("price"),
                "stake": a.get("stake"),
                "reason": a.get("reason"),
            }
        )

    return {
        "count": len(rows),
        "stake": round(total_stake, 6),
        "top": top,
    }


def _second_leg_compact_package(pkg: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(pkg, dict):
        return {
            "mode": "-",
            "actions": 0,
            "capital": 0.0,
            "worst_if_full": None,
            "top": [],
        }

    actions = list(pkg.get("actions") or [])

    top = []
    for a in actions[:12]:
        top.append(
            {
                "mode": a.get("mode"),
                "side": a.get("side"),
                "runner": a.get("runner"),
                "price": a.get("price"),
                "stake": a.get("stake"),
                "queue": a.get("queue"),
                "buckets": a.get("buckets"),
                "critical_buckets": a.get("critical_buckets"),
            }
        )

    return {
        "mode": pkg.get("mode"),
        "reason": pkg.get("reason"),
        "actions": int(pkg.get("action_count") or 0),
        "capital": float(pkg.get("capital") or 0.0),
        "worst_if_full": pkg.get("worst_if_full"),
        "partial_worst_min": pkg.get("partial_worst_min"),
        "scale_from_full_package": pkg.get("scale_from_full_package"),
        "maker_back_inverse_sum": pkg.get("maker_back_inverse_sum"),
        "critical_virtual_row_count": pkg.get("critical_virtual_row_count"),
        "top": top,
    }



def _second_leg_level_size_at_price(levels: object, price: float) -> float | None:
    try:
        target = float(price)
    except Exception:
        return None

    for lvl in levels or []:
        p = None
        s = None

        try:
            if isinstance(lvl, dict):
                p = lvl.get("price")
                s = lvl.get("size")
            else:
                try:
                    p = lvl[0]
                    s = lvl[1]
                except Exception:
                    p = getattr(lvl, "price")
                    s = getattr(lvl, "size")
        except Exception:
            continue

        try:
            p_f = float(p)
            s_f = float(s)
        except Exception:
            continue

        if abs(p_f - target) < 1e-9:
            return s_f

    return None


def _second_leg_find_cs_runner_by_name(
    markets: dict[str, MarketState] | None,
    runner_name: str,
) -> RunnerState | None:
    target = str(runner_name)

    for st in (markets or {}).values():
        if st is None:
            continue
        if not is_correct_score(st):
            continue

        for r in st.runners.values():
            if str(r.name or r.selection_id) == target:
                return r

    return None


def _second_leg_shadow_fill_probe(
    *,
    markets: dict[str, MarketState] | None,
    package_preview: dict[str, object] | None,
    active: bool,
) -> dict[str, object]:
    """
    Read-only SECOND LEG fill probe v0.

    Does NOT change exposure.
    Does NOT modify order_model.
    Does NOT trigger real cancel/place.

    v0 fill rule:
      MAKER BACK is considered fill-signaled only if current best_lay <= order price.

    Queue movement is logged but not trusted as fill yet.
    """
    if not active:
        return {
            "status": "INACTIVE",
            "reason": "package_not_placed",
            "fills": [],
            "filled_actions": 0,
            "filled_stake": 0.0,
        }

    if not isinstance(package_preview, dict):
        return {
            "status": "NO_PACKAGE",
            "reason": "missing_package",
            "fills": [],
            "filled_actions": 0,
            "filled_stake": 0.0,
        }

    actions = list(package_preview.get("actions") or [])
    if not actions:
        return {
            "status": "NO_ACTIONS",
            "reason": "package_has_no_actions",
            "fills": [],
            "filled_actions": 0,
            "filled_stake": 0.0,
        }

    fills: list[dict[str, object]] = []
    probes: list[dict[str, object]] = []

    for a in actions:
        mode = str(a.get("mode") or "")
        side = str(a.get("side") or "")
        runner = str(a.get("runner") or "")

        try:
            order_price = float(a.get("price") or 0.0)
            order_stake = float(a.get("stake") or 0.0)
            queue_at_decision = float(a.get("queue") or 0.0)
        except Exception:
            continue

        r = _second_leg_find_cs_runner_by_name(markets, runner)
        if r is None:
            probes.append(
                {
                    "runner": runner,
                    "mode": mode,
                    "side": side,
                    "price": order_price,
                    "stake": order_stake,
                    "status": "RUNNER_MISSING",
                }
            )
            continue

        bb = best_level(r.available_to_back, side="BACK")
        bl = best_level(r.available_to_lay, side="LAY")

        current_bb_price = None if bb is None else float(bb[0])
        current_bb_size = None if bb is None else float(bb[1])
        current_bl_price = None if bl is None else float(bl[0])
        current_bl_size = None if bl is None else float(bl[1])
        current_size_at_order_price = _second_leg_level_size_at_price(
            r.available_to_back,
            order_price,
        )

        # fallback to current best BACK size when our order price is the visible best back.
        # This is enough for queue diagnostics on top-of-book maker BACK orders even if
        # the raw ladder level container is not directly parseable here.
        if (
            current_size_at_order_price is None
            and current_bb_price is not None
            and abs(float(current_bb_price) - float(order_price)) < 1e-9
        ):
            current_size_at_order_price = current_bb_size

        fill_size = 0.0
        fill_reason = "NONE"

        if mode == "MAKER" and side == "BACK":
            if current_bl_price is not None and current_bl_price <= order_price:
                fill_size = order_stake
                fill_reason = "PRICE_CROSSED_BY_BEST_LAY"

        probe = {
            "runner": runner,
            "mode": mode,
            "side": side,
            "price": order_price,
            "stake": order_stake,
            "queue_at_decision": queue_at_decision,
            "current_bb": current_bb_price,
            "current_bb_size": current_bb_size,
            "current_bl": current_bl_price,
            "current_bl_size": current_bl_size,
            "current_size_at_order_price": current_size_at_order_price,
            "fill_size": round(fill_size, 6),
            "fill_reason": fill_reason,
        }

        probes.append(probe)

        if fill_size > 1e-9:
            fills.append(probe)

    filled_stake = sum(float(x.get("fill_size", 0.0) or 0.0) for x in fills)
    total_stake = sum(float(a.get("stake", 0.0) or 0.0) for a in actions)

    if filled_stake <= 1e-9:
        status = "NONE"
    elif filled_stake + 1e-9 >= total_stake:
        status = "FULL_SIGNAL"
    else:
        status = "PARTIAL_SIGNAL"

    return {
        "status": status,
        "reason": "maker_back_price_cross_probe_v0",
        "filled_actions": len(fills),
        "action_count": len(actions),
        "filled_stake": round(filled_stake, 6),
        "total_stake": round(total_stake, 6),
        "fills": fills[:20],
        "probes": probes[:20],
    }




def _second_leg_totals_risk_reduction_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
    filled_totals_greenup_actions: list[dict[str, object]] | None = None,
    filled_totals_risk_reduction_actions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """
    Read-only paid totals risk-reduction preview v0.

    This is NOT greenup.

    It allows closing part of the totals first-leg at negative greenup only if:
      - SLC worst improves materially
      - cost is capped
      - improvement/cost is good enough
      - action does not worsen SLC worst

    Does NOT execute.
    Does NOT mutate state.
    """
    profile = second_leg_combined_profile.get("profile")
    if not isinstance(profile, dict) or not profile:
        return {
            "mode": "NO_TOTALS_RISK_REDUCTION",
            "reason": "missing_combined_profile",
            "actions": [],
            "action_count": 0,
        }

    base_bucket = {str(k): float(v) for k, v in profile.items()}
    current_worst = min(base_bucket.values())
    current_best = max(base_bucket.values())

    filled_trr_cost_used = sum(
        float(x.get("cost", 0.0) or 0.0)
        for x in list(filled_totals_risk_reduction_actions or [])
    )
    remaining_trr_cost_budget = max(
        0.0,
        float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST) - float(filled_trr_cost_used),
    )

    if remaining_trr_cost_budget <= SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "NO_TOTALS_RISK_REDUCTION",
            "reason": "total_trr_cost_budget_exhausted",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "filled_trr_cost_used": round(float(filled_trr_cost_used), 6),
            "remaining_trr_cost_budget": round(float(remaining_trr_cost_budget), 6),
            "max_total_trr_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST), 6),
        }

    if current_worst >= -SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "NO_TOTALS_RISK_REDUCTION",
            "reason": "slc_worst_not_negative",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    if not MAKER_UNDER_LAY_GRID_ENABLED or not MAKER_UNDER_LAY_GRID_MATCHING_ENABLED:
        return {
            "mode": "NO_TOTALS_RISK_REDUCTION",
            "reason": "maker_under_lay_grid_not_active",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    markets = markets or {}

    try:
        visible_market_ids = _maker_under_lay_grid_canonical_totals_market_ids(markets)
    except Exception:
        visible_market_ids = set(str(k) for k in markets.keys())

    filled_totals_by_key = _second_leg_totals_back_stake_by_key(
        filled_totals_greenup_actions,
        filled_totals_risk_reduction_actions,
    )

    grouped: dict[tuple[str, int, float], dict[str, float]] = {}

    for okey, st in MAKER_UNDER_LAY_GRID_ORDER_STATE.items():
        try:
            market_id, selection_id, handicap, lay_price = okey
            market_id_s = str(market_id)

            if visible_market_ids and market_id_s not in visible_market_ids:
                continue

            matched = float(st.get("matched", 0.0) or 0.0)
            if matched <= 1e-9:
                continue

            lay_price_f = float(lay_price)
            key = (market_id_s, int(selection_id), float(handicap or 0.0))

            row = grouped.setdefault(
                key,
                {
                    "matched_stake": 0.0,
                    "matched_liability": 0.0,
                    "matched_orders": 0.0,
                },
            )

            row["matched_stake"] += matched
            row["matched_liability"] += matched * max(0.0, lay_price_f - 1.0)
            row["matched_orders"] += 1.0

        except Exception:
            continue

    candidates: list[dict[str, object]] = []

    for (market_id, selection_id, handicap), row in grouped.items():
        market = markets.get(market_id)
        if market is None:
            continue

        try:
            line = float(over_under_line(market))
        except Exception:
            continue

        runner = _second_leg_totals_under_runner_for_market(
            market,
            selection_id=selection_id,
            handicap=handicap,
        )
        if runner is None:
            continue

        bl = best_level(runner.available_to_lay, side="LAY")
        if bl is None:
            continue

        try:
            close_price = float(bl[0])
            close_size = float(bl[1])
        except Exception:
            continue

        if close_price <= 1.0 or close_size <= 1e-9:
            continue

        matched_stake = float(row.get("matched_stake", 0.0) or 0.0)
        matched_liability = float(row.get("matched_liability", 0.0) or 0.0)

        if matched_stake <= 1e-9:
            continue

        full_hedge_back_stake = (matched_stake + matched_liability) / close_price
        if full_hedge_back_stake <= 1e-9:
            continue

        line_greenup_if_full = matched_stake - full_hedge_back_stake

        # This module is paid risk reduction only.
        # Positive greenup belongs to TOTALS_GREENUP_PREVIEW.
        if line_greenup_if_full >= -SECOND_LEG_TOTALS_GREENUP_MIN_GREENUP:
            continue

        key = (market_id, int(selection_id), float(handicap))
        already_back_stake = float(filled_totals_by_key.get(key, 0.0))
        residual_hedge_back_stake = max(0.0, float(full_hedge_back_stake) - already_back_stake)

        if residual_hedge_back_stake <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
            continue

        line_cost_if_full = max(0.0, -float(line_greenup_if_full))
        cost_per_back_stake = line_cost_if_full / full_hedge_back_stake

        if cost_per_back_stake <= 1e-12:
            continue

        per_action_cost_budget = min(
            float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_COST),
            float(remaining_trr_cost_budget),
        )
        max_stake_by_cost = float(per_action_cost_budget) / cost_per_back_stake

        hedge_back_stake = min(
            float(residual_hedge_back_stake),
            float(close_size),
            float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_STAKE),
            float(max_stake_by_cost),
        )

        if hedge_back_stake <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
            continue

        action_cost = hedge_back_stake * cost_per_back_stake
        residual_after_action = max(0.0, float(residual_hedge_back_stake) - float(hedge_back_stake))
        hedge_completion = residual_after_action <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE

        trial_profile = _second_leg_apply_totals_back_preview(
            base_bucket=base_bucket,
            line=line,
            price=close_price,
            stake=hedge_back_stake,
        )

        worst_if_full = min(float(v) for v in trial_profile.values())
        best_if_full = max(float(v) for v in trial_profile.values())
        worst_improvement = float(worst_if_full) - float(current_worst)

        if worst_improvement < SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_WORST_IMPROVEMENT:
            continue

        if action_cost > SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_COST + SECOND_LEG_PREVIEW_WORST_EPS:
            continue

        if action_cost > remaining_trr_cost_budget + SECOND_LEG_PREVIEW_WORST_EPS:
            continue

        improvement_per_cost = worst_improvement / max(action_cost, 1e-12)

        if improvement_per_cost < SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_IMPROVEMENT_PER_COST:
            continue

        candidates.append(
            {
                "mode": "TAKER_BACK_TOTALS_RISK_REDUCTION",
                "side": "BACK",
                "market_id": market_id,
                "runner": str(runner.name or runner.selection_id),
                "selection_id": int(selection_id),
                "handicap": float(handicap),
                "line": round(float(line), 6),
                "price": round(float(close_price), 6),
                "available": round(float(close_size), 6),
                "stake": round(float(hedge_back_stake), 6),
                "full_hedge_back_stake": round(float(full_hedge_back_stake), 6),
                "already_back_stake": round(float(already_back_stake), 6),
                "residual_hedge_back_stake": round(float(residual_hedge_back_stake), 6),
                "residual_after_action": round(float(residual_after_action), 6),
                "hedge_completion": bool(hedge_completion),
                "matched_lay_stake": round(float(matched_stake), 6),
                "matched_lay_liability": round(float(matched_liability), 6),
                "line_greenup_if_full": round(float(line_greenup_if_full), 6),
                "cost": round(float(action_cost), 6),
                "filled_trr_cost_used": round(float(filled_trr_cost_used), 6),
                "remaining_trr_cost_budget": round(float(remaining_trr_cost_budget), 6),
                "max_total_trr_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST), 6),
                "current_worst": round(float(current_worst), 6),
                "worst_if_full": round(float(worst_if_full), 6),
                "best_if_full": round(float(best_if_full), 6),
                "worst_improvement": round(float(worst_improvement), 6),
                "improvement_per_cost": round(float(improvement_per_cost), 6),
                "profile_if_full": {k: round(float(v), 6) for k, v in trial_profile.items()},
            }
        )

    candidates.sort(
        key=lambda x: (
            float(x.get("worst_improvement") or 0.0),
            float(x.get("improvement_per_cost") or 0.0),
            -float(x.get("cost") or 0.0),
        ),
        reverse=True,
    )

    if not candidates:
        return {
            "mode": "NO_TOTALS_RISK_REDUCTION",
            "reason": "no_paid_risk_reduction_candidate",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "max_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_COST), 6),
            "filled_trr_cost_used": round(float(filled_trr_cost_used), 6),
            "remaining_trr_cost_budget": round(float(remaining_trr_cost_budget), 6),
            "max_total_trr_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST), 6),
            "min_worst_improvement": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_WORST_IMPROVEMENT), 6),
            "min_improvement_per_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_IMPROVEMENT_PER_COST), 6),
        }

    best = candidates[0]

    return {
        "mode": "TAKER_BACK_TOTALS_RISK_REDUCTION_PREVIEW",
        "reason": "paid_totals_risk_reduction_improves_slc_worst",
        "actions": [best],
        "action_count": 1,
        "current_worst": round(float(current_worst), 6),
        "current_best": round(float(current_best), 6),
        "cost": best.get("cost"),
        "filled_trr_cost_used": round(float(filled_trr_cost_used), 6),
        "remaining_trr_cost_budget": round(float(remaining_trr_cost_budget), 6),
        "max_total_trr_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST), 6),
        "worst_if_full": best.get("worst_if_full"),
        "best_if_full": best.get("best_if_full"),
        "worst_improvement": best.get("worst_improvement"),
        "improvement_per_cost": best.get("improvement_per_cost"),
        "top_candidates": candidates[:10],
    }




def _second_leg_apply_cs_lay_preview(
    *,
    virtual_rows: list[dict[str, object]],
    base_bucket: dict[str, float],
    runner: str,
    price: float,
    stake: float,
) -> dict[str, float]:
    """
    Preview CS LAY as negative BACK:
      selected runner outcomes: -stake * (price - 1)
      all other outcomes: +stake
    """
    return _second_leg_apply_back_preview(
        virtual_rows=virtual_rows,
        base_bucket=base_bucket,
        package=[
            {
                "mode": "TAKER",
                "side": "LAY",
                "runner": runner,
                "price": float(price),
                "stake": -float(stake),
            }
        ],
    )


def _second_leg_cs_taker_lay_recovery_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
) -> dict[str, object]:
    """
    Read-only CS recovery preview v0.

    Looks for TAKER LAY on Correct Score:
      - use current best BACK liquidity
      - price <= SECOND_LEG_CS_RECOVERY_LAY_MAX_PRICE
      - only when SLC worst is negative
      - action must improve SLC worst
      - stake/liability capped

    Does NOT execute.
    Does NOT mutate state.
    """
    profile = second_leg_combined_profile.get("profile")
    if not isinstance(profile, dict) or not profile:
        return {
            "mode": "NO_CS_RECOVERY",
            "reason": "missing_combined_profile",
            "actions": [],
            "action_count": 0,
        }

    base_bucket = {str(k): float(v) for k, v in profile.items()}
    current_worst = min(base_bucket.values())
    current_best = max(base_bucket.values())

    if current_worst >= -SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "NO_CS_RECOVERY",
            "reason": "slc_worst_not_negative",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    virtual_rows = _second_leg_cs_virtual_rows(markets)
    candidates: list[dict[str, object]] = []

    for st in (markets or {}).values():
        if st is None:
            continue
        if not is_correct_score(st):
            continue

        for r in st.runners.values():
            runner_name = str(r.name or r.selection_id)

            bb = best_level(r.available_to_back, side="BACK")
            if bb is None:
                continue

            try:
                price = float(bb[0])
                avail = float(bb[1])
            except Exception:
                continue

            if price <= 1.0:
                continue
            if price > SECOND_LEG_CS_RECOVERY_LAY_MAX_PRICE:
                continue
            if avail <= 1e-9:
                continue

            max_stake_by_liability = float(SECOND_LEG_CS_RECOVERY_MAX_LIABILITY) / max(price - 1.0, 1e-12)
            max_stake = min(
                float(avail),
                float(SECOND_LEG_CS_RECOVERY_MAX_STAKE),
                float(max_stake_by_liability),
            )

            if max_stake <= 1e-9:
                continue

            lo = 0.0
            hi = max_stake
            best_profile = None
            best_stake = 0.0
            best_worst = current_worst
            best_best = current_best

            for _ in range(24):
                mid = (lo + hi) / 2.0
                trial = _second_leg_apply_cs_lay_preview(
                    virtual_rows=virtual_rows,
                    base_bucket=base_bucket,
                    runner=runner_name,
                    price=price,
                    stake=mid,
                )

                if not trial:
                    hi = mid
                    continue

                trial_worst = min(float(v) for v in trial.values())
                trial_best = max(float(v) for v in trial.values())

                # Safe: never worsen current SLC worst.
                if trial_worst >= current_worst - SECOND_LEG_PREVIEW_WORST_EPS:
                    lo = mid
                    best_profile = trial
                    best_stake = mid
                    best_worst = trial_worst
                    best_best = trial_best
                else:
                    hi = mid

            if best_profile is None or best_stake <= 1e-9:
                continue

            worst_improvement = float(best_worst) - float(current_worst)

            if worst_improvement < SECOND_LEG_CS_RECOVERY_MIN_WORST_IMPROVEMENT:
                continue

            liability = best_stake * max(0.0, price - 1.0)

            candidates.append(
                {
                    "mode": "TAKER_LAY_CS_RECOVERY",
                    "side": "LAY",
                    "runner": runner_name,
                    "price": round(float(price), 6),
                    "stake": round(float(best_stake), 6),
                    "available": round(float(avail), 6),
                    "liability": round(float(liability), 6),
                    "current_worst": round(float(current_worst), 6),
                    "current_best": round(float(current_best), 6),
                    "worst_if_full": round(float(best_worst), 6),
                    "best_if_full": round(float(best_best), 6),
                    "worst_improvement": round(float(worst_improvement), 6),
                    "profile_if_full": {k: round(float(v), 6) for k, v in best_profile.items()},
                }
            )

    candidates.sort(
        key=lambda x: (
            float(x.get("worst_improvement") or 0.0),
            -float(x.get("liability") or 0.0),
            float(x.get("stake") or 0.0),
        ),
        reverse=True,
    )

    if not candidates:
        return {
            "mode": "NO_CS_RECOVERY",
            "reason": "no_safe_taker_lay_cs_candidate",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "lay_max_price": round(float(SECOND_LEG_CS_RECOVERY_LAY_MAX_PRICE), 6),
            "max_stake": round(float(SECOND_LEG_CS_RECOVERY_MAX_STAKE), 6),
            "max_liability": round(float(SECOND_LEG_CS_RECOVERY_MAX_LIABILITY), 6),
        }

    best = candidates[0]

    return {
        "mode": "TAKER_LAY_CS_RECOVERY_PREVIEW",
        "reason": "safe_taker_lay_cs_improves_slc_worst",
        "actions": [best],
        "action_count": 1,
        "current_worst": round(float(current_worst), 6),
        "current_best": round(float(current_best), 6),
        "worst_if_full": best.get("worst_if_full"),
        "best_if_full": best.get("best_if_full"),
        "worst_improvement": best.get("worst_improvement"),
        "liability": best.get("liability"),
        "top_candidates": candidates[:10],
    }



def _second_leg_cs_maker_lay_recovery_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
) -> dict[str, object]:
    """
    Read-only CS maker LAY recovery preview v0.

    This is NOT executed.
    It only checks whether a queued maker LAY could reduce negative SLC risk.

    Candidate price:
      - current best LAY if available
      - price <= SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_PRICE

    Safety:
      - only when SLC worst is negative
      - action must improve SLC worst
      - liability capped
    """
    profile = second_leg_combined_profile.get("profile")
    if not isinstance(profile, dict) or not profile:
        return {
            "mode": "NO_CS_MAKER_LAY_RECOVERY",
            "reason": "missing_combined_profile",
            "actions": [],
            "action_count": 0,
        }

    base_bucket = {str(k): float(v) for k, v in profile.items()}
    current_worst = min(base_bucket.values())
    current_best = max(base_bucket.values())

    if current_worst >= -SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "NO_CS_MAKER_LAY_RECOVERY",
            "reason": "slc_worst_not_negative",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    virtual_rows = _second_leg_cs_virtual_rows(markets)
    candidates: list[dict[str, object]] = []

    for st in (markets or {}).values():
        if st is None:
            continue
        if not is_correct_score(st):
            continue

        for r in st.runners.values():
            runner_name = str(r.name or r.selection_id)

            bl = best_level(r.available_to_lay, side="LAY")
            if bl is None:
                continue

            try:
                price = float(bl[0])
                queue_ahead = float(bl[1])
            except Exception:
                continue

            if price <= 1.0:
                continue
            if price > SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_PRICE:
                continue

            max_stake_by_liability = float(SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_LIABILITY) / max(price - 1.0, 1e-12)
            max_stake = min(
                float(SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_STAKE),
                float(max_stake_by_liability),
            )

            if max_stake <= 1e-9:
                continue

            lo = 0.0
            hi = max_stake
            best_profile = None
            best_stake = 0.0
            best_worst = current_worst
            best_best = current_best

            for _ in range(24):
                mid = (lo + hi) / 2.0
                trial = _second_leg_apply_cs_lay_preview(
                    virtual_rows=virtual_rows,
                    base_bucket=base_bucket,
                    runner=runner_name,
                    price=price,
                    stake=mid,
                )

                if not trial:
                    hi = mid
                    continue

                trial_worst = min(float(v) for v in trial.values())
                trial_best = max(float(v) for v in trial.values())

                if trial_worst >= current_worst - SECOND_LEG_PREVIEW_WORST_EPS:
                    lo = mid
                    best_profile = trial
                    best_stake = mid
                    best_worst = trial_worst
                    best_best = trial_best
                else:
                    hi = mid

            if best_profile is None or best_stake <= 1e-9:
                continue

            worst_improvement = float(best_worst) - float(current_worst)

            if worst_improvement < SECOND_LEG_CS_MAKER_LAY_RECOVERY_MIN_WORST_IMPROVEMENT:
                continue

            liability = best_stake * max(0.0, price - 1.0)

            candidates.append(
                {
                    "mode": "MAKER_LAY_CS_RECOVERY",
                    "side": "LAY",
                    "runner": runner_name,
                    "price": round(float(price), 6),
                    "stake": round(float(best_stake), 6),
                    "queue_ahead": round(float(queue_ahead), 6),
                    "liability": round(float(liability), 6),
                    "current_worst": round(float(current_worst), 6),
                    "current_best": round(float(current_best), 6),
                    "worst_if_full": round(float(best_worst), 6),
                    "best_if_full": round(float(best_best), 6),
                    "worst_improvement": round(float(worst_improvement), 6),
                    "profile_if_full": {k: round(float(v), 6) for k, v in best_profile.items()},
                }
            )

    candidates.sort(
        key=lambda x: (
            float(x.get("worst_improvement") or 0.0),
            -float(x.get("queue_ahead") or 0.0),
            -float(x.get("liability") or 0.0),
            float(x.get("stake") or 0.0),
        ),
        reverse=True,
    )

    if not candidates:
        return {
            "mode": "NO_CS_MAKER_LAY_RECOVERY",
            "reason": "no_safe_maker_lay_cs_candidate",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "lay_max_price": round(float(SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_PRICE), 6),
            "max_stake": round(float(SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_STAKE), 6),
            "max_liability": round(float(SECOND_LEG_CS_MAKER_LAY_RECOVERY_MAX_LIABILITY), 6),
        }

    best = candidates[0]

    return {
        "mode": "MAKER_LAY_CS_RECOVERY_PREVIEW",
        "reason": "safe_maker_lay_cs_improves_slc_worst",
        "actions": [best],
        "action_count": 1,
        "current_worst": round(float(current_worst), 6),
        "current_best": round(float(current_best), 6),
        "worst_if_full": best.get("worst_if_full"),
        "best_if_full": best.get("best_if_full"),
        "worst_improvement": best.get("worst_improvement"),
        "liability": best.get("liability"),
        "queue_ahead": best.get("queue_ahead"),
        "top_candidates": candidates[:10],
    }



def _second_leg_cs_taker_back_recovery_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
) -> dict[str, object]:
    """
    Read-only CS TAKER BACK recovery preview v0.

    This is NOT the normal maker BACK package.
    It is allowed only if BACKing a CS runner improves current SLC worst.

    Execution price = current best LAY.
    Cost/risk = stake, capped.
    Does NOT execute.
    Does NOT mutate state.
    """
    profile = second_leg_combined_profile.get("profile")
    if not isinstance(profile, dict) or not profile:
        return {
            "mode": "NO_CS_TAKER_BACK_RECOVERY",
            "reason": "missing_combined_profile",
            "actions": [],
            "action_count": 0,
        }

    base_bucket = {str(k): float(v) for k, v in profile.items()}
    current_worst = min(base_bucket.values())
    current_best = max(base_bucket.values())

    if current_worst >= -SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "NO_CS_TAKER_BACK_RECOVERY",
            "reason": "slc_worst_not_negative",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    virtual_rows = _second_leg_cs_virtual_rows(markets)
    candidates: list[dict[str, object]] = []

    for st in (markets or {}).values():
        if st is None:
            continue
        if not is_correct_score(st):
            continue

        for r in st.runners.values():
            runner_name = str(r.name or r.selection_id)

            bl = best_level(r.available_to_lay, side="LAY")
            if bl is None:
                continue

            try:
                price = float(bl[0])
                avail = float(bl[1])
            except Exception:
                continue

            if price <= 1.0 or avail <= 1e-9:
                continue

            max_stake = min(
                float(avail),
                float(SECOND_LEG_CS_TAKER_BACK_RECOVERY_MAX_STAKE),
                float(SECOND_LEG_CS_TAKER_BACK_RECOVERY_MAX_STAKE_LOSS),
            )

            if max_stake <= 1e-9:
                continue

            lo = 0.0
            hi = max_stake
            best_profile = None
            best_stake = 0.0
            best_worst = current_worst
            best_best = current_best

            for _ in range(24):
                mid = (lo + hi) / 2.0
                trial = _second_leg_apply_back_preview(
                    virtual_rows=virtual_rows,
                    base_bucket=base_bucket,
                    package=[
                        {
                            "mode": "TAKER",
                            "side": "BACK",
                            "runner": runner_name,
                            "price": price,
                            "stake": mid,
                        }
                    ],
                )

                if not trial:
                    hi = mid
                    continue

                trial_worst = min(float(v) for v in trial.values())
                trial_best = max(float(v) for v in trial.values())

                # Safe: never worsen current SLC worst.
                if trial_worst >= current_worst - SECOND_LEG_PREVIEW_WORST_EPS:
                    lo = mid
                    best_profile = trial
                    best_stake = mid
                    best_worst = trial_worst
                    best_best = trial_best
                else:
                    hi = mid

            if best_profile is None or best_stake <= 1e-9:
                continue

            worst_improvement = float(best_worst) - float(current_worst)

            if worst_improvement < SECOND_LEG_CS_TAKER_BACK_RECOVERY_MIN_WORST_IMPROVEMENT:
                continue

            candidates.append(
                {
                    "mode": "TAKER_BACK_CS_RECOVERY",
                    "side": "BACK",
                    "runner": runner_name,
                    "price": round(float(price), 6),
                    "stake": round(float(best_stake), 6),
                    "available": round(float(avail), 6),
                    "stake_loss": round(float(best_stake), 6),
                    "current_worst": round(float(current_worst), 6),
                    "current_best": round(float(current_best), 6),
                    "worst_if_full": round(float(best_worst), 6),
                    "best_if_full": round(float(best_best), 6),
                    "worst_improvement": round(float(worst_improvement), 6),
                    "profile_if_full": {k: round(float(v), 6) for k, v in best_profile.items()},
                }
            )

    candidates.sort(
        key=lambda x: (
            float(x.get("worst_improvement") or 0.0),
            -float(x.get("stake_loss") or 0.0),
            float(x.get("price") or 0.0),
        ),
        reverse=True,
    )

    if not candidates:
        return {
            "mode": "NO_CS_TAKER_BACK_RECOVERY",
            "reason": "no_safe_taker_back_cs_candidate",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "max_stake": round(float(SECOND_LEG_CS_TAKER_BACK_RECOVERY_MAX_STAKE), 6),
            "max_stake_loss": round(float(SECOND_LEG_CS_TAKER_BACK_RECOVERY_MAX_STAKE_LOSS), 6),
        }

    best = candidates[0]

    return {
        "mode": "TAKER_BACK_CS_RECOVERY_PREVIEW",
        "reason": "safe_taker_back_cs_improves_slc_worst",
        "actions": [best],
        "action_count": 1,
        "current_worst": round(float(current_worst), 6),
        "current_best": round(float(current_best), 6),
        "worst_if_full": best.get("worst_if_full"),
        "best_if_full": best.get("best_if_full"),
        "worst_improvement": best.get("worst_improvement"),
        "stake_loss": best.get("stake_loss"),
        "top_candidates": candidates[:10],
    }



def _second_leg_bucket_name_from_goals(goals: int) -> str:
    return "G9+" if int(goals) >= 9 else f"G{int(goals)}"


def _second_leg_runner_exact_goal_bucket(runner_name: str) -> str | None:
    s = str(runner_name or "").strip()
    if " - " not in s:
        return None

    parts = s.split(" - ", 1)
    if len(parts) != 2:
        return None

    try:
        a = int(parts[0].strip())
        b = int(parts[1].strip())
    except Exception:
        return None

    return _second_leg_bucket_name_from_goals(a + b)


def _second_leg_virtual_row_runner(row: dict[str, object]) -> str | None:
    for key in ("runner", "runner_name", "name", "selection_name"):
        v = row.get(key)
        if v:
            return str(v)
    return None


def _second_leg_virtual_row_buckets(row: dict[str, object]) -> list[str]:
    for key in ("buckets", "mapped_buckets", "bucket_names", "goal_buckets"):
        v = row.get(key)
        if isinstance(v, (list, tuple, set)):
            out = [str(x) for x in v if str(x)]
            if out:
                return out

    for key in ("bucket", "goal_bucket", "bucket_name"):
        v = row.get(key)
        if v is None:
            continue

        if isinstance(v, str):
            vv = v.strip()
            if vv.startswith("G"):
                return [vv]
            try:
                return [_second_leg_bucket_name_from_goals(int(float(vv)))]
            except Exception:
                pass

        try:
            return [_second_leg_bucket_name_from_goals(int(v))]
        except Exception:
            pass

    runner = _second_leg_virtual_row_runner(row)
    bucket = _second_leg_runner_exact_goal_bucket(str(runner or ""))
    return [bucket] if bucket else []


def _second_leg_find_cs_runner_price(
    *,
    markets: dict[str, MarketState] | None,
    runner_name: str,
    side: str,
) -> tuple[float, float] | None:
    target = str(runner_name)

    for st in (markets or {}).values():
        if st is None or not is_correct_score(st):
            continue

        for r in st.runners.values():
            if str(r.name or r.selection_id) != target:
                continue

            if str(side).upper() == "BACK":
                lvl = best_level(r.available_to_lay, side="LAY")
            else:
                lvl = best_level(r.available_to_back, side="BACK")

            if lvl is None:
                return None

            try:
                return float(lvl[0]), float(lvl[1])
            except Exception:
                return None

    return None


def _second_leg_cs_back_bucket_recovery_package_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
) -> dict[str, object]:
    """
    Read-only CS BACK bucket recovery package preview v0.

    Finds the bucket currently driving SLC worst and tries to BACK all CS
    runners mapped to that bucket as one taker dutch package.

    Does NOT execute.
    Does NOT mutate state.
    """
    profile = second_leg_combined_profile.get("profile")
    if not isinstance(profile, dict) or not profile:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "missing_combined_profile",
            "actions": [],
            "action_count": 0,
        }

    base_bucket = {str(k): float(v) for k, v in profile.items()}
    current_worst = min(base_bucket.values())
    current_best = max(base_bucket.values())

    if current_worst >= -SECOND_LEG_PREVIEW_WORST_EPS:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "slc_worst_not_negative",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
        }

    true_worst_buckets = [
        str(k)
        for k, v in base_bucket.items()
        if float(v) <= float(current_worst) + SECOND_LEG_PREVIEW_WORST_EPS
    ]

    # Guard-band:
    # BACKing only the exact worst bucket can improve that bucket but push nearby
    # buckets below it because BACK loses stake on all non-selected outcomes.
    # Therefore, cover all buckets that are close enough to become the next worst
    # within the max package capital.
    worst_buckets = [
        str(k)
        for k, v in base_bucket.items()
        if float(v) <= (
            float(current_worst)
            + float(SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MAX_CAPITAL)
            + SECOND_LEG_PREVIEW_WORST_EPS
        )
    ] or list(true_worst_buckets)

    virtual_rows = _second_leg_cs_virtual_rows(markets)
    target_runners: dict[str, dict[str, object]] = {}

    for row in virtual_rows:
        if not isinstance(row, dict):
            continue

        runner = _second_leg_virtual_row_runner(row)
        if not runner:
            continue

        buckets = _second_leg_virtual_row_buckets(row)
        if not buckets:
            continue

        if any(b in worst_buckets for b in buckets):
            target_runners[str(runner)] = {
                "runner": str(runner),
                "buckets": buckets,
            }

    if not target_runners:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "no_virtual_runners_for_worst_buckets",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "worst_buckets": worst_buckets,
            "virtual_row_count": len(virtual_rows),
        }

    priced: list[dict[str, object]] = []
    missing: list[str] = []
    maker_fallback_count = 0
    taker_count = 0

    for runner, meta in sorted(target_runners.items()):
        px = _second_leg_find_cs_runner_price(
            markets=markets,
            runner_name=runner,
            side="BACK",
        )

        execution = "TAKER"
        queue_ahead = 0.0

        if px is None:
            # Fallback: if there is no taker BACK price, preview a MAKER BACK
            # at current best BACK. This is still read-only and assumes full fill
            # only for feasibility diagnostics.
            px = _second_leg_find_cs_runner_price(
                markets=markets,
                runner_name=runner,
                side="MAKER_BACK",
            )
            execution = "MAKER"

        if px is None:
            missing.append(runner)
            continue

        price, avail = px
        if price <= 1.0:
            missing.append(runner)
            continue

        if execution == "TAKER":
            if avail <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
                missing.append(runner)
                continue
            usable_available = float(avail)
            taker_count += 1
        else:
            # For maker fallback, `avail` means queue ahead, not taker liquidity.
            # Do not cap stake by queue ahead in preview.
            queue_ahead = float(avail)
            usable_available = float(SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MAX_CAPITAL)
            maker_fallback_count += 1

        priced.append(
            {
                "runner": runner,
                "buckets": meta.get("buckets") or [],
                "price": float(price),
                "available": float(usable_available),
                "queue_ahead": float(queue_ahead),
                "execution": execution,
            }
        )

    if missing:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "missing_taker_or_maker_back_price_for_worst_bucket_runner",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "worst_buckets": worst_buckets,
            "true_worst_buckets": true_worst_buckets,
            "target_count": len(target_runners),
            "priced_count": len(priced),
            "taker_count": taker_count,
            "maker_fallback_count": maker_fallback_count,
            "missing": missing[:30],
        }

    inv_sum = sum(1.0 / float(x["price"]) for x in priced)

    if inv_sum >= 1.0 - 1e-12:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "worst_bucket_back_dutch_not_positive",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "worst_buckets": worst_buckets,
            "true_worst_buckets": true_worst_buckets,
            "target_count": len(target_runners),
            "priced_count": len(priced),
            "inv_sum": round(float(inv_sum), 6),
        }

    # global_safe_sizing_grid_v1
    #
    # Previous version sized only against the worst bucket. That can still worsen
    # the global SLC because every BACK action loses stake on all non-selected
    # virtual outcomes. Here we search package capital directly and accept only
    # if the GLOBAL bucket-worst improves.
    max_capital = float(SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MAX_CAPITAL)

    weights: list[dict[str, object]] = []
    for x in priced:
        price = float(x["price"])
        weight = (1.0 / price) / max(inv_sum, 1e-12)
        weights.append(
            {
                "runner": str(x["runner"]),
                "buckets": list(x.get("buckets") or []),
                "price": price,
                "available": float(x.get("available", 0.0) or 0.0),
                "queue_ahead": float(x.get("queue_ahead", 0.0) or 0.0),
                "execution": str(x.get("execution") or "TAKER"),
                "weight": float(weight),
            }
        )

    max_capital_by_liquidity = max_capital
    for x in weights:
        w = float(x["weight"])
        if w <= 1e-12:
            continue
        max_capital_by_liquidity = min(
            max_capital_by_liquidity,
            float(x["available"]) / w,
        )

    max_search_capital = min(float(max_capital), float(max_capital_by_liquidity))

    if max_search_capital <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "global_safe_liquidity_cap_too_small",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "worst_buckets": worst_buckets,
            "inv_sum": round(float(inv_sum), 6),
            "max_capital": round(float(max_capital), 6),
            "max_search_capital": round(float(max_search_capital), 6),
        }

    def _build_actions(total_capital: float) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for x in weights:
            stake = float(total_capital) * float(x["weight"])
            if stake <= SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MIN_ACTION_STAKE:
                return []

            out.append(
                {
                    "mode": str(x["execution"]),
                    "side": "BACK",
                    "runner": str(x["runner"]),
                    "buckets": list(x.get("buckets") or []),
                    "price": round(float(x["price"]), 6),
                    "stake": round(float(stake), 6),
                    "available": round(float(x["available"]), 6),
                    "queue_ahead": round(float(x.get("queue_ahead", 0.0) or 0.0), 6),
                }
            )
        return out

    best_actions: list[dict[str, object]] = []
    best_profile: dict[str, float] | None = None
    best_capital = 0.0
    best_worst = float(current_worst)
    best_best = float(current_best)
    best_probe = None

    best_failed_actions: list[dict[str, object]] = []
    best_failed_profile: dict[str, float] | None = None
    best_failed_capital = 0.0
    best_failed_worst = -10**18
    best_failed_best = float(current_best)
    best_failed_probe = None

    # Coarse grid is enough for preview. It avoids false positives from a
    # package that improves one bucket but damages another.
    grid_points = 200
    for i in range(1, grid_points + 1):
        capital_probe = float(max_search_capital) * float(i) / float(grid_points)
        actions_probe = _build_actions(capital_probe)
        if not actions_probe:
            continue

        trial = _second_leg_apply_back_preview(
            virtual_rows=virtual_rows,
            base_bucket=base_bucket,
            package=actions_probe,
        )

        if not trial:
            continue

        trial_worst = min(float(v) for v in trial.values())
        trial_best = max(float(v) for v in trial.values())

        if trial_worst > best_failed_worst + SECOND_LEG_PREVIEW_WORST_EPS:
            best_failed_actions = actions_probe
            best_failed_profile = trial
            best_failed_capital = capital_probe
            best_failed_worst = trial_worst
            best_failed_best = trial_best
            best_failed_probe = i

        # Maximize global worst. Tie-breaker: lower capital.
        if (
            trial_worst > best_worst + SECOND_LEG_PREVIEW_WORST_EPS
            or (
                abs(trial_worst - best_worst) <= SECOND_LEG_PREVIEW_WORST_EPS
                and best_profile is not None
                and capital_probe < best_capital
            )
        ):
            best_actions = actions_probe
            best_profile = trial
            best_capital = capital_probe
            best_worst = trial_worst
            best_best = trial_best
            best_probe = i

    if best_profile is None:
        failed_profile_out = {}
        failed_worst_buckets = []
        if isinstance(best_failed_profile, dict):
            failed_profile_out = {k: round(float(v), 6) for k, v in best_failed_profile.items()}
            if failed_profile_out:
                bw = min(float(v) for v in failed_profile_out.values())
                failed_worst_buckets = [
                    str(k) for k, v in failed_profile_out.items()
                    if float(v) <= bw + SECOND_LEG_PREVIEW_WORST_EPS
                ]

        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "global_safe_bucket_package_no_positive_worst_probe",
            "actions": [],
            "action_count": 0,
            "current_worst": round(float(current_worst), 6),
            "current_best": round(float(current_best), 6),
            "current_profile": {k: round(float(v), 6) for k, v in base_bucket.items()},
            "worst_buckets": worst_buckets,
            "target_count": len(target_runners),
            "priced_count": len(priced),
            "taker_count": taker_count,
            "maker_fallback_count": maker_fallback_count,
            "inv_sum": round(float(inv_sum), 6),
            "max_capital": round(float(max_capital), 6),
            "max_search_capital": round(float(max_search_capital), 6),
            "probe_count": grid_points,
            "best_failed_probe": best_failed_probe,
            "best_failed_capital": round(float(best_failed_capital), 6),
            "best_failed_worst": round(float(best_failed_worst), 6),
            "best_failed_best": round(float(best_failed_best), 6),
            "best_failed_worst_improvement": round(float(best_failed_worst - current_worst), 6),
            "best_failed_worst_buckets": failed_worst_buckets,
            "best_failed_probe_profile": failed_profile_out,
            "best_failed_actions": best_failed_actions[:20],
        }

    actions = best_actions
    trial_profile = best_profile
    worst_if_full = float(best_worst)
    best_if_full = float(best_best)
    worst_improvement = float(worst_if_full) - float(current_worst)
    capital = sum(float(a.get("stake", 0.0) or 0.0) for a in actions)

    worst_if_full = min(float(v) for v in trial_profile.values())
    best_if_full = max(float(v) for v in trial_profile.values())
    worst_improvement = float(worst_if_full) - float(current_worst)
    capital = sum(float(a.get("stake", 0.0) or 0.0) for a in actions)

    if worst_improvement < SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MIN_WORST_IMPROVEMENT:
        return {
            "mode": "NO_CS_BACK_BUCKET_RECOVERY",
            "reason": "package_does_not_improve_bucket_worst",
            "actions": actions[:20],
            "action_count": len(actions),
            "capital": round(float(capital), 6),
            "current_worst": round(float(current_worst), 6),
            "worst_if_full": round(float(worst_if_full), 6),
            "worst_improvement": round(float(worst_improvement), 6),
            "worst_buckets": worst_buckets,
            "inv_sum": round(float(inv_sum), 6),
            "scale": 1.0,  # global_safe_sizing_grid_v1 fixed legacy scale field
            "profile_if_full": {k: round(float(v), 6) for k, v in trial_profile.items()},
        }

    has_maker_fallback = any(str(a.get("mode") or "") == "MAKER" for a in actions)

    return {
        "mode": "HYBRID_BACK_CS_BUCKET_RECOVERY_PACKAGE_PREVIEW" if has_maker_fallback else "TAKER_BACK_CS_BUCKET_RECOVERY_PACKAGE_PREVIEW",
        "reason": "safe_hybrid_back_bucket_package_improves_slc_worst" if has_maker_fallback else "safe_taker_back_bucket_package_improves_slc_worst",
        "actions": actions,
        "action_count": len(actions),
        "capital": round(float(capital), 6),
        "current_worst": round(float(current_worst), 6),
        "current_best": round(float(current_best), 6),
        "worst_if_full": round(float(worst_if_full), 6),
        "best_if_full": round(float(best_if_full), 6),
        "worst_improvement": round(float(worst_improvement), 6),
        "worst_buckets": worst_buckets,
        "target_count": len(target_runners),
        "priced_count": len(priced),
        "taker_count": sum(1 for a in actions if str(a.get("mode") or "") == "TAKER"),
        "maker_fallback_count": sum(1 for a in actions if str(a.get("mode") or "") == "MAKER"),
        "inv_sum": round(float(inv_sum), 6),
        "scale": 1.0,  # global_safe_sizing_grid_v1 fixed legacy scale field
        "profile_if_full": {k: round(float(v), 6) for k, v in trial_profile.items()},
        "top_actions": actions[:20],
    }


def _second_leg_cs_recovery_preview(
    *,
    markets: dict[str, MarketState] | None,
    second_leg_combined_profile: dict[str, object],
) -> dict[str, object]:
    """
    Combined CS recovery preview:
      1. TAKER LAY recovery first
      2. MAKER LAY recovery if taker has no safe candidate
    """
    taker = _second_leg_cs_taker_lay_recovery_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
    )

    if str(taker.get("mode") or "") == "TAKER_LAY_CS_RECOVERY_PREVIEW":
        taker["priority"] = "TAKER_FIRST"
        return taker

    maker = _second_leg_cs_maker_lay_recovery_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
    )

    if str(maker.get("mode") or "") == "MAKER_LAY_CS_RECOVERY_PREVIEW":
        maker["priority"] = "MAKER_FALLBACK_AFTER_NO_TAKER"
        maker["taker_result"] = {
            "mode": taker.get("mode"),
            "reason": taker.get("reason"),
        }
        return maker

    taker_back = _second_leg_cs_taker_back_recovery_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
    )

    if str(taker_back.get("mode") or "") == "TAKER_BACK_CS_RECOVERY_PREVIEW":
        taker_back["priority"] = "TAKER_BACK_FALLBACK_AFTER_NO_LAY"
        taker_back["taker_lay_result"] = {
            "mode": taker.get("mode"),
            "reason": taker.get("reason"),
        }
        taker_back["maker_lay_result"] = {
            "mode": maker.get("mode"),
            "reason": maker.get("reason"),
        }
        return taker_back

    bucket_back = _second_leg_cs_back_bucket_recovery_package_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
    )

    if str(bucket_back.get("mode") or "") in ("TAKER_BACK_CS_BUCKET_RECOVERY_PACKAGE_PREVIEW", "HYBRID_BACK_CS_BUCKET_RECOVERY_PACKAGE_PREVIEW"):
        bucket_back["priority"] = "BUCKET_BACK_PACKAGE_AFTER_SINGLE_ACTIONS_FAILED"
        bucket_back["taker_lay_result"] = {
            "mode": taker.get("mode"),
            "reason": taker.get("reason"),
        }
        bucket_back["maker_lay_result"] = {
            "mode": maker.get("mode"),
            "reason": maker.get("reason"),
        }
        bucket_back["taker_back_result"] = {
            "mode": taker_back.get("mode"),
            "reason": taker_back.get("reason"),
        }
        return bucket_back

    return {
        "mode": "NO_CS_RECOVERY",
        "reason": "no_safe_single_or_bucket_cs_recovery_candidate",
        "actions": [],
        "action_count": 0,
        "taker_lay_result": {
            "mode": taker.get("mode"),
            "reason": taker.get("reason"),
        },
        "maker_lay_result": {
            "mode": maker.get("mode"),
            "reason": maker.get("reason"),
        },
        "taker_back_result": {
            "mode": taker_back.get("mode"),
            "reason": taker_back.get("reason"),
        },
        "bucket_back_result": {
            "mode": bucket_back.get("mode"),
            "reason": bucket_back.get("reason"),
            "current_worst": bucket_back.get("current_worst"),
            "current_best": bucket_back.get("current_best"),
            "current_profile": bucket_back.get("current_profile"),
            "worst_buckets": bucket_back.get("worst_buckets"),
            "target_count": bucket_back.get("target_count"),
            "priced_count": bucket_back.get("priced_count"),
            "taker_count": bucket_back.get("taker_count"),
            "maker_fallback_count": bucket_back.get("maker_fallback_count"),
            "virtual_row_count": bucket_back.get("virtual_row_count"),
            "missing": bucket_back.get("missing"),
            "inv_sum": bucket_back.get("inv_sum"),
            "capital": bucket_back.get("capital"),
            "worst_improvement": bucket_back.get("worst_improvement"),
            "max_capital": bucket_back.get("max_capital"),
            "max_search_capital": bucket_back.get("max_search_capital"),
            "probe_count": bucket_back.get("probe_count"),
            "best_failed_probe": bucket_back.get("best_failed_probe"),
            "best_failed_capital": bucket_back.get("best_failed_capital"),
            "best_failed_worst": bucket_back.get("best_failed_worst"),
            "best_failed_best": bucket_back.get("best_failed_best"),
            "best_failed_worst_improvement": bucket_back.get("best_failed_worst_improvement"),
            "best_failed_worst_buckets": bucket_back.get("best_failed_worst_buckets"),
            "best_failed_probe_profile": bucket_back.get("best_failed_probe_profile"),
            "best_failed_actions": bucket_back.get("best_failed_actions"),
        },
    }


def _second_leg_package_recovery_blocked(pkg: dict[str, object] | None) -> bool:
    if not isinstance(pkg, dict):
        return False
    return (
        str(pkg.get("mode") or "") == "NO_SAFE_MAKER_BACK_PACKAGE"
        and str(pkg.get("reason") or "") == "recovery_blocked_trr_budget_exhausted_negative_slc"
    )


def _second_leg_package_is_maker_back_risk(pkg: dict[str, object] | None) -> bool:
    if not isinstance(pkg, dict):
        return False

    mode = str(pkg.get("mode") or "")
    if mode.startswith("MAKER_BACK"):
        return True

    for a in list(pkg.get("actions") or []):
        try:
            if str(a.get("mode") or "").upper() == "MAKER" and str(a.get("side") or "").upper() == "BACK":
                return True
        except Exception:
            continue

    return False


def _second_leg_apply_recovery_block_gate(
    *,
    package_preview: dict[str, object] | None,
    second_leg_combined_profile: dict[str, object],
    second_leg_totals_greenup_preview: dict[str, object],
    second_leg_totals_risk_reduction_preview: dict[str, object],
    filled_totals_risk_reduction_actions: list[dict[str, object]] | None,
) -> dict[str, object]:
    """
    Hard safety gate:

    If SLC is negative, TRR budget is exhausted, and no positive TG is available,
    do not allow new risk-increasing CS maker BACK package.

    Recovery modules may still be shown/executed separately.
    """
    pkg = dict(package_preview or {})

    if not _second_leg_package_is_maker_back_risk(pkg):
        return pkg

    slc_worst = second_leg_combined_profile.get("worst")
    slc_best = second_leg_combined_profile.get("best")
    profile = second_leg_combined_profile.get("profile")

    if slc_worst is None:
        return pkg

    try:
        slc_worst_f = float(slc_worst)
    except Exception:
        return pkg

    if slc_worst_f >= -SECOND_LEG_PREVIEW_WORST_EPS:
        return pkg

    # Positive totals greenup has priority and remains allowed.
    if str(second_leg_totals_greenup_preview.get("mode") or "") == "TAKER_BACK_TOTALS_GREENUP_PREVIEW":
        return pkg

    trr_cost_used = sum(
        float(x.get("cost", 0.0) or 0.0)
        for x in list(filled_totals_risk_reduction_actions or [])
    )
    trr_reason = str(second_leg_totals_risk_reduction_preview.get("reason") or "")

    trr_exhausted = (
        trr_reason == "total_trr_cost_budget_exhausted"
        or trr_cost_used >= float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST) - SECOND_LEG_PREVIEW_WORST_EPS
    )

    if not trr_exhausted:
        return pkg

    profile_out = {}
    if isinstance(profile, dict):
        profile_out = {str(k): round(float(v), 6) for k, v in profile.items()}

    return {
        "mode": "NO_SAFE_MAKER_BACK_PACKAGE",
        "reason": "recovery_blocked_trr_budget_exhausted_negative_slc",
        "actions": [],
        "action_count": 0,
        "capital": 0.0,
        "worst": round(float(slc_worst_f), 6),
        "worst_if_full": round(float(slc_worst_f), 6),
        "best_if_full": None if slc_best is None else round(float(slc_best), 6),
        "profile_if_full": profile_out,
        "slc_worst": round(float(slc_worst_f), 6),
        "trr_cost_used": round(float(trr_cost_used), 6),
        "max_total_trr_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST), 6),
        "trr_reason": trr_reason,
        "tg_mode": str(second_leg_totals_greenup_preview.get("mode") or ""),
        "blocked_original_mode": str(pkg.get("mode") or ""),
        "blocked_original_capital": round(float(pkg.get("capital", 0.0) or 0.0), 6),
    }


def _second_leg_package_budget_exhausted(pkg: dict[str, object] | None) -> bool:
    if not isinstance(pkg, dict):
        return False
    return (
        str(pkg.get("mode") or "") == "NO_SAFE_MAKER_BACK_PACKAGE"
        and str(pkg.get("reason") or "") == "total_filled_worst_budget_exhausted"
    )


def _second_leg_package_score(pkg: dict[str, object] | None) -> tuple[int, float, float]:
    """
    Higher is better.
    Tuple:
      accepted_rank, capital, worst_if_full
    """
    if not isinstance(pkg, dict):
        return (0, 0.0, -10**9)

    mode = str(pkg.get("mode") or "")

    if mode in ("MAKER_BACK_MICRO_PREVIEW", "MAKER_BACK_VIRTUAL_PREVIEW"):
        rank = 2
    elif mode == "CLOSED":
        rank = 3
    elif mode in ("NO_SAFE_MAKER_BACK_PACKAGE", "NO_SAFE_BACK_PACKAGE", "REJECTED_WORSE_PROFILE", "REJECTED_PARTIAL_FILL_UNSAFE"):
        rank = 0
    else:
        rank = 1

    capital = float(pkg.get("capital") or 0.0)
    worst_if_full = float(pkg.get("worst_if_full") or -10**9)

    return (rank, capital, worst_if_full)


def _second_leg_package_signal(
    *,
    frozen_pkg: dict[str, object] | None,
    fresh_pkg: dict[str, object] | None,
) -> dict[str, object]:
    frozen_score = _second_leg_package_score(frozen_pkg)
    fresh_score = _second_leg_package_score(fresh_pkg)

    frozen_mode = None if not isinstance(frozen_pkg, dict) else str(frozen_pkg.get("mode") or "")
    fresh_mode = None if not isinstance(fresh_pkg, dict) else str(fresh_pkg.get("mode") or "")

    # Fresh became tradable while frozen is no-package/rejected.
    if fresh_score[0] > frozen_score[0]:
        return {
            "signal": "BETTER_FRESH",
            "reason": "fresh_package_rank_improved",
            "frozen_mode": frozen_mode,
            "fresh_mode": fresh_mode,
            "frozen_score": frozen_score,
            "fresh_score": fresh_score,
        }

    # Frozen package is tradable but current fresh validation says unsafe/no-safe.
    if frozen_score[0] > fresh_score[0]:
        return {
            "signal": "FRESH_WORSE",
            "reason": "fresh_package_rank_worse",
            "frozen_mode": frozen_mode,
            "fresh_mode": fresh_mode,
            "frozen_score": frozen_score,
            "fresh_score": fresh_score,
        }

    # Same rank, but fresh capital differs materially.
    frozen_cap = frozen_score[1]
    fresh_cap = fresh_score[1]

    if abs(fresh_cap - frozen_cap) >= 0.25:
        return {
            "signal": "FRESH_SIZE_CHANGED",
            "reason": "fresh_capital_changed",
            "frozen_mode": frozen_mode,
            "fresh_mode": fresh_mode,
            "frozen_capital": frozen_cap,
            "fresh_capital": fresh_cap,
            "frozen_score": frozen_score,
            "fresh_score": fresh_score,
        }

    return {
        "signal": "OK",
        "reason": "frozen_package_still_current_enough",
        "frozen_mode": frozen_mode,
        "fresh_mode": fresh_mode,
        "frozen_score": frozen_score,
        "fresh_score": fresh_score,
    }


def _second_leg_debug_write_jsonl(
    *,
    debug_jsonl: str | None,
    payload: dict[str, object],
) -> None:
    if not debug_jsonl:
        return

    try:
        p = Path(str(debug_jsonl))
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return


def _second_leg_cs_debug_line(
    *,
    markets: dict[str, MarketState] | None,
    outcome_pnls: dict[int, float],
    frame_index: int | None,
    pt: int | None,
    debug_jsonl: str | None,
) -> str:
    state = SECOND_LEG_CS_DEBUG_STATE

    if not outcome_pnls:
        state["state"] = "IDLE"
        state["reason"] = "-"
        return "SL: state=IDLE"

    now_pt = int(pt or 0)
    frame_i = int(frame_index or 0)

    vals = [float(v) for v in outcome_pnls.values()]
    worst = min(vals)
    best = max(vals)
    residual_abs = max(0.0, -worst)
    max_capital = residual_abs * SECOND_LEG_MAX_CAPITAL_RATIO

    profile_key = _second_leg_profile_key(outcome_pnls)
    prev_profile_key = state.get("profile_key")

    reason = str(state.get("reason") or "-")
    package_rebuild_event = False

    if prev_profile_key != profile_key:
        package_rebuild_event = True
        state["epoch"] = int(state.get("epoch") or 0) + 1
        state["profile_key"] = profile_key
        state["decision_pt"] = now_pt
        state["place_due_pt"] = now_pt + SECOND_LEG_PLACE_DELAY_MS
        state["stale_due_pt"] = now_pt + SECOND_LEG_PLACE_DELAY_MS + SECOND_LEG_STALE_AFTER_PLACE_MS
        state["state"] = "WAITING_PLACE_DELAY"
        state["reason"] = "FIRST_LEG_CHANGED"
        reason = "FIRST_LEG_CHANGED"
    else:
        place_due = int(state.get("place_due_pt") or 0)
        stale_due = int(state.get("stale_due_pt") or 0)

        if stale_due and now_pt >= stale_due:
            package_rebuild_event = True
            state["epoch"] = int(state.get("epoch") or 0) + 1
            state["decision_pt"] = now_pt
            state["place_due_pt"] = now_pt + SECOND_LEG_PLACE_DELAY_MS
            state["stale_due_pt"] = now_pt + SECOND_LEG_PLACE_DELAY_MS + SECOND_LEG_STALE_AFTER_PLACE_MS
            state["state"] = "WAITING_PLACE_DELAY"
            state["reason"] = "STALE_10S_NO_FILL"
            reason = "STALE_10S_NO_FILL"
        elif place_due and now_pt >= place_due:
            state["state"] = "PACKAGE_PLACED_SHADOW"
            reason = "SHADOW_PACKAGE_ACTIVE"
        else:
            state["state"] = "WAITING_PLACE_DELAY"
            reason = str(state.get("reason") or "WAITING_PLACE_DELAY")

    cs = _second_leg_cs_shadow_summary(markets)

    filled_second_leg_actions = list(state.get("filled_second_leg_actions") or [])
    filled_totals_greenup_actions = list(state.get("filled_totals_greenup_actions") or [])
    filled_totals_risk_reduction_actions = list(state.get("filled_totals_risk_reduction_actions") or [])

    second_leg_combined_profile = _second_leg_combined_bucket_profile(
        markets=markets,
        outcome_pnls=outcome_pnls,
        filled_second_leg_actions=filled_second_leg_actions,
        filled_totals_greenup_actions=filled_totals_greenup_actions,
        filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
    )

    second_leg_totals_greenup_preview = _second_leg_totals_greenup_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
        filled_totals_greenup_actions=filled_totals_greenup_actions,
        filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
    )

    totals_greenup_exec: dict[str, object] = {
        "status": "NONE",
        "reason": "no_totals_greenup_execution",
        "actions": [],
    }

    if str(second_leg_totals_greenup_preview.get("mode") or "") == "TAKER_BACK_TOTALS_GREENUP_PREVIEW":
        tg_actions = list(second_leg_totals_greenup_preview.get("actions") or [])
        if tg_actions:
            raw_action = dict(tg_actions[0])
            raw_stake = float(raw_action.get("stake", 0.0) or 0.0)
            raw_greenup = float(raw_action.get("greenup", 0.0) or 0.0)
            raw_line_greenup = float(raw_action.get("line_greenup_if_full", raw_greenup) or 0.0)
            raw_worst_improvement = float(raw_action.get("worst_improvement", 0.0) or 0.0)

            if raw_stake <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
                totals_greenup_exec = {
                    "status": "SKIPPED_ZERO_STAKE",
                    "reason": "totals_greenup_residual_too_small",
                    "actions": [raw_action],
                }
            elif raw_line_greenup < SECOND_LEG_TOTALS_GREENUP_MIN_GREENUP:
                totals_greenup_exec = {
                    "status": "SKIPPED_NEGATIVE_GREENUP",
                    "reason": "totals_greenup_must_be_positive_only",
                    "actions": [raw_action],
                }
            elif raw_worst_improvement < -SECOND_LEG_PREVIEW_WORST_EPS:
                totals_greenup_exec = {
                    "status": "SKIPPED_WORSE_PROFILE",
                    "reason": "totals_greenup_would_worsen_slc_worst",
                    "actions": [raw_action],
                }
            else:
                seq = int(state.get("filled_totals_greenup_seq") or 0) + 1
                state["filled_totals_greenup_seq"] = seq

                tg_action = dict(raw_action)
                tg_action["seq"] = seq
                tg_action["frame"] = frame_i
                tg_action["pt"] = now_pt
                tg_action["reason"] = "SHADOW_TAKER_TOTALS_GREENUP_EXECUTED"

                filled_totals_greenup_actions = list(state.get("filled_totals_greenup_actions") or [])
                filled_totals_greenup_actions.append(tg_action)
                state["filled_totals_greenup_actions"] = filled_totals_greenup_actions

                second_leg_combined_profile = _second_leg_combined_bucket_profile(
                    markets=markets,
                    outcome_pnls=outcome_pnls,
                    filled_second_leg_actions=filled_second_leg_actions,
                    filled_totals_greenup_actions=filled_totals_greenup_actions,
                )

                second_leg_totals_greenup_preview = _second_leg_totals_greenup_preview(
                    markets=markets,
                    second_leg_combined_profile=second_leg_combined_profile,
                    filled_totals_greenup_actions=filled_totals_greenup_actions,
                )

                totals_greenup_exec = {
                    "status": "EXECUTED_SHADOW",
                    "reason": "taker_totals_greenup_preview_accepted",
                    "actions": [tg_action],
                }

    second_leg_totals_risk_reduction_preview = _second_leg_totals_risk_reduction_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
        filled_totals_greenup_actions=filled_totals_greenup_actions,
        filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
    )

    totals_risk_reduction_exec: dict[str, object] = {
        "status": "NONE",
        "reason": "no_totals_risk_reduction_execution",
        "actions": [],
    }

    if str(second_leg_totals_risk_reduction_preview.get("mode") or "") == "TAKER_BACK_TOTALS_RISK_REDUCTION_PREVIEW":
        trr_actions = list(second_leg_totals_risk_reduction_preview.get("actions") or [])
        slc_w = second_leg_combined_profile.get("worst")

        if trr_actions and slc_w is not None and float(slc_w) < -SECOND_LEG_PREVIEW_WORST_EPS:
            raw_action = dict(trr_actions[0])

            raw_stake = float(raw_action.get("stake", 0.0) or 0.0)
            raw_cost = float(raw_action.get("cost", 0.0) or 0.0)
            raw_imp = float(raw_action.get("worst_improvement", 0.0) or 0.0)

            trr_cost_used_now = sum(
                float(x.get("cost", 0.0) or 0.0)
                for x in list(filled_totals_risk_reduction_actions or [])
            )
            trr_cost_remaining_now = max(
                0.0,
                float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST) - float(trr_cost_used_now),
            )

            if raw_stake <= SECOND_LEG_TOTALS_GREENUP_MIN_EXEC_STAKE:
                totals_risk_reduction_exec = {
                    "status": "SKIPPED_ZERO_STAKE",
                    "reason": "totals_risk_reduction_residual_too_small",
                    "actions": [raw_action],
                }
            elif raw_cost > SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_COST + SECOND_LEG_PREVIEW_WORST_EPS:
                totals_risk_reduction_exec = {
                    "status": "SKIPPED_COST_CAP",
                    "reason": "totals_risk_reduction_cost_cap",
                    "actions": [raw_action],
                }
            elif raw_cost > trr_cost_remaining_now + SECOND_LEG_PREVIEW_WORST_EPS:
                totals_risk_reduction_exec = {
                    "status": "SKIPPED_TOTAL_COST_BUDGET",
                    "reason": "totals_risk_reduction_total_cost_budget",
                    "cost_used": round(float(trr_cost_used_now), 6),
                    "cost_remaining": round(float(trr_cost_remaining_now), 6),
                    "max_total_cost": round(float(SECOND_LEG_TOTALS_RISK_REDUCTION_MAX_TOTAL_COST), 6),
                    "actions": [raw_action],
                }
            elif raw_imp < SECOND_LEG_TOTALS_RISK_REDUCTION_MIN_WORST_IMPROVEMENT:
                totals_risk_reduction_exec = {
                    "status": "SKIPPED_LOW_IMPROVEMENT",
                    "reason": "totals_risk_reduction_improvement_too_small",
                    "actions": [raw_action],
                }
            else:
                seq = int(state.get("filled_totals_risk_reduction_seq") or 0) + 1
                state["filled_totals_risk_reduction_seq"] = seq

                trr_action = dict(raw_action)
                trr_action["seq"] = seq
                trr_action["frame"] = frame_i
                trr_action["pt"] = now_pt
                trr_action["reason"] = "SHADOW_TAKER_TOTALS_RISK_REDUCTION_EXECUTED"

                filled_totals_risk_reduction_actions = list(state.get("filled_totals_risk_reduction_actions") or [])
                filled_totals_risk_reduction_actions.append(trr_action)
                state["filled_totals_risk_reduction_actions"] = filled_totals_risk_reduction_actions

                second_leg_combined_profile = _second_leg_combined_bucket_profile(
                    markets=markets,
                    outcome_pnls=outcome_pnls,
                    filled_second_leg_actions=filled_second_leg_actions,
                    filled_totals_greenup_actions=filled_totals_greenup_actions,
                    filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
                )

                second_leg_totals_greenup_preview = _second_leg_totals_greenup_preview(
                    markets=markets,
                    second_leg_combined_profile=second_leg_combined_profile,
                    filled_totals_greenup_actions=filled_totals_greenup_actions,
                    filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
                )

                second_leg_totals_risk_reduction_preview = _second_leg_totals_risk_reduction_preview(
                    markets=markets,
                    second_leg_combined_profile=second_leg_combined_profile,
                    filled_totals_greenup_actions=filled_totals_greenup_actions,
                    filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
                )

                totals_risk_reduction_exec = {
                    "status": "EXECUTED_SHADOW",
                    "reason": "paid_totals_risk_reduction_preview_accepted",
                    "actions": [trr_action],
                }

    second_leg_recovery_preview = _second_leg_cs_recovery_preview(
        markets=markets,
        second_leg_combined_profile=second_leg_combined_profile,
    )

    second_leg_recovery_exec: dict[str, object] = {
        "status": "NONE",
        "reason": "no_second_leg_recovery_shadow_execution",
        "actions": [],
    }

    recovery_mode = str(second_leg_recovery_preview.get("mode") or "")
    recovery_back_modes = (
        "TAKER_BACK_CS_RECOVERY_PREVIEW",
        "TAKER_BACK_CS_BUCKET_RECOVERY_PACKAGE_PREVIEW",
        "HYBRID_BACK_CS_BUCKET_RECOVERY_PACKAGE_PREVIEW",
    )

    recovery_exec_active = str(state.get("state") or "") == "PACKAGE_PLACED_SHADOW"

    if recovery_mode in recovery_back_modes and not recovery_exec_active:
        second_leg_recovery_exec = {
            "status": "WAITING_PLACE_DELAY",
            "reason": "recovery_preview_waiting_place_delay",
            "mode": recovery_mode,
            "state": str(state.get("state") or ""),
            "actions": list(second_leg_recovery_preview.get("actions") or []),
            "action_count": int(second_leg_recovery_preview.get("action_count") or 0),
            "capital": second_leg_recovery_preview.get("capital"),
            "worst_if_full": second_leg_recovery_preview.get("worst_if_full"),
            "worst_improvement": second_leg_recovery_preview.get("worst_improvement"),
        }

    elif recovery_mode in recovery_back_modes:
        current_slc_worst = float(second_leg_combined_profile.get("worst", worst) or worst)
        preview_worst_if_full = float(second_leg_recovery_preview.get("worst_if_full", current_slc_worst) or current_slc_worst)
        preview_worst_improvement = float(second_leg_recovery_preview.get("worst_improvement", 0.0) or 0.0)

        raw_recovery_actions = []
        for a in list(second_leg_recovery_preview.get("actions") or []):
            if str(a.get("side") or "").upper() != "BACK":
                continue

            try:
                a_price = float(a.get("price") or 0.0)
                a_stake = float(a.get("stake") or 0.0)
            except Exception:
                continue

            if a_price <= 1.0 or a_stake <= SECOND_LEG_CS_BACK_BUCKET_RECOVERY_MIN_ACTION_STAKE:
                continue

            raw_recovery_actions.append(dict(a))

        maker_recovery_actions = [
            a for a in raw_recovery_actions
            if str(a.get("mode") or "").upper() == "MAKER"
        ]

        recovery_capital = sum(float(a.get("stake", 0.0) or 0.0) for a in raw_recovery_actions)
        existing_recovery_capital = sum(
            float(a.get("stake", 0.0) or 0.0)
            for a in list(state.get("filled_second_leg_actions") or [])
            if str(a.get("reason") or "").startswith("SHADOW_CS_RECOVERY")
            or str(a.get("source_mode") or "").endswith("RECOVERY_PACKAGE_PREVIEW")
            or str(a.get("source_mode") or "") == "TAKER_BACK_CS_RECOVERY_PREVIEW"
        )

        if not raw_recovery_actions:
            second_leg_recovery_exec = {
                "status": "SKIPPED_NO_BACK_ACTIONS",
                "reason": "recovery_preview_has_no_valid_back_actions",
                "mode": recovery_mode,
                "actions": [],
            }
        elif maker_recovery_actions:
            second_leg_recovery_exec = {
                "status": "WAITING_MAKER_FILL",
                "reason": "recovery_package_has_maker_fallback_wait_for_queue_model",
                "mode": recovery_mode,
                "maker_action_count": len(maker_recovery_actions),
                "taker_action_count": len(raw_recovery_actions) - len(maker_recovery_actions),
                "capital": round(float(recovery_capital), 6),
                "actions": raw_recovery_actions,
                "maker_actions": maker_recovery_actions,
            }
        elif preview_worst_improvement <= SECOND_LEG_PREVIEW_WORST_EPS:
            second_leg_recovery_exec = {
                "status": "SKIPPED_NO_WORST_IMPROVEMENT",
                "reason": "recovery_preview_does_not_improve_slc_worst",
                "mode": recovery_mode,
                "worst_improvement": round(float(preview_worst_improvement), 6),
                "actions": raw_recovery_actions,
            }
        elif preview_worst_if_full < current_slc_worst - SECOND_LEG_PREVIEW_WORST_EPS:
            second_leg_recovery_exec = {
                "status": "SKIPPED_WORSE_PROFILE",
                "reason": "recovery_preview_would_worsen_slc_worst",
                "mode": recovery_mode,
                "current_worst": round(float(current_slc_worst), 6),
                "worst_if_full": round(float(preview_worst_if_full), 6),
                "actions": raw_recovery_actions,
            }
        elif (
            existing_recovery_capital + recovery_capital
            > float(SECOND_LEG_CS_RECOVERY_SHADOW_MAX_TOTAL_CAPITAL) + SECOND_LEG_PREVIEW_WORST_EPS
        ):
            second_leg_recovery_exec = {
                "status": "SKIPPED_RECOVERY_CAP_EXHAUSTED",
                "reason": "second_leg_recovery_shadow_total_capital_exhausted",
                "mode": recovery_mode,
                "capital": round(float(recovery_capital), 6),
                "existing_recovery_capital": round(float(existing_recovery_capital), 6),
                "max_total_recovery_capital": round(float(SECOND_LEG_CS_RECOVERY_SHADOW_MAX_TOTAL_CAPITAL), 6),
                "actions": raw_recovery_actions,
            }
        else:
            new_recovery_actions: list[dict[str, object]] = []

            for raw_action in raw_recovery_actions:
                seq = int(state.get("filled_second_leg_seq") or 0) + 1
                state["filled_second_leg_seq"] = seq

                rec_action = dict(raw_action)
                rec_action["seq"] = seq
                rec_action["frame"] = frame_i
                rec_action["pt"] = now_pt
                rec_action["side"] = "BACK"
                rec_action["source_mode"] = recovery_mode
                rec_action["source_reason"] = str(second_leg_recovery_preview.get("reason") or "")
                rec_action["reason"] = "SHADOW_CS_RECOVERY_BACK_EXECUTED"

                try:
                    rec_action["price"] = round(float(rec_action.get("price") or 0.0), 6)
                    rec_action["stake"] = round(float(rec_action.get("stake") or 0.0), 6)
                    rec_action["queue_ahead"] = round(float(rec_action.get("queue_ahead", 0.0) or 0.0), 6)
                except Exception:
                    pass

                new_recovery_actions.append(rec_action)

            filled_second_leg_actions = list(state.get("filled_second_leg_actions") or [])
            filled_second_leg_actions.extend(new_recovery_actions)
            state["filled_second_leg_actions"] = filled_second_leg_actions

            second_leg_combined_profile = _second_leg_combined_bucket_profile(
                markets=markets,
                outcome_pnls=outcome_pnls,
                filled_second_leg_actions=filled_second_leg_actions,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )

            second_leg_totals_greenup_preview = _second_leg_totals_greenup_preview(
                markets=markets,
                second_leg_combined_profile=second_leg_combined_profile,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )

            second_leg_totals_risk_reduction_preview = _second_leg_totals_risk_reduction_preview(
                markets=markets,
                second_leg_combined_profile=second_leg_combined_profile,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )

            second_leg_recovery_after_exec = _second_leg_cs_recovery_preview(
                markets=markets,
                second_leg_combined_profile=second_leg_combined_profile,
            )

            state["epoch"] = int(state.get("epoch") or 0) + 1
            state["decision_pt"] = now_pt
            state["place_due_pt"] = now_pt + SECOND_LEG_REBUILD_DELAY_MS
            state["stale_due_pt"] = now_pt + SECOND_LEG_REBUILD_DELAY_MS + SECOND_LEG_STALE_AFTER_PLACE_MS
            state["state"] = "WAITING_PLACE_DELAY"
            state["reason"] = "SECOND_LEG_RECOVERY_REBUILD"
            package_rebuild_event = True

            reason = str(state["reason"])

            second_leg_recovery_exec = {
                "status": "EXECUTED_SHADOW",
                "reason": "second_leg_recovery_back_preview_accepted",
                "mode": recovery_mode,
                "capital": round(float(recovery_capital), 6),
                "existing_recovery_capital_before": round(float(existing_recovery_capital), 6),
                "max_total_recovery_capital": round(float(SECOND_LEG_CS_RECOVERY_SHADOW_MAX_TOTAL_CAPITAL), 6),
                "current_worst_before": round(float(current_slc_worst), 6),
                "worst_if_full_preview": round(float(preview_worst_if_full), 6),
                "worst_improvement_preview": round(float(preview_worst_improvement), 6),
                "actions": new_recovery_actions,
                "preview_before_exec": second_leg_recovery_preview,
                "preview_after_exec": second_leg_recovery_after_exec,
            }

            second_leg_recovery_preview = second_leg_recovery_after_exec

    fresh_package_preview = _second_leg_candidate_package_preview(
        markets=markets,
        outcome_pnls=outcome_pnls,
        filled_second_leg_actions=filled_second_leg_actions,
        filled_totals_greenup_actions=filled_totals_greenup_actions,
        filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
    )

    fresh_package_preview = _second_leg_apply_recovery_block_gate(
        package_preview=fresh_package_preview,
        second_leg_combined_profile=second_leg_combined_profile,
        second_leg_totals_greenup_preview=second_leg_totals_greenup_preview,
        second_leg_totals_risk_reduction_preview=second_leg_totals_risk_reduction_preview,
        filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
    )

    # Freeze package per epoch.
    # In real in-play execution, a package calculated at decision time arrives after delay;
    # it is not continuously replaced every frame unless a rebuild event starts a new epoch.
    if state.get("package_preview") is None or package_rebuild_event:
        state["package_preview"] = fresh_package_preview
        state["package_decision_frame"] = frame_i
        state["package_decision_pt"] = now_pt

    package_preview = state.get("package_preview")
    if not isinstance(package_preview, dict):
        package_preview = fresh_package_preview

    if (
        str(state.get("state") or "") == "WAITING_PLACE_DELAY"
        and _second_leg_package_budget_exhausted(package_preview)
    ):
        state["state"] = "BUDGET_BLOCKED_SHADOW"
        state["reason"] = "SECOND_LEG_BUDGET_EXHAUSTED"
        state["place_due_pt"] = None
        state["stale_due_pt"] = None
        reason = str(state["reason"])
        sl_state = str(state["state"])

    if (
        str(state.get("state") or "") == "WAITING_PLACE_DELAY"
        and _second_leg_package_recovery_blocked(package_preview)
    ):
        state["state"] = "RECOVERY_BLOCKED_SHADOW"
        state["reason"] = "RECOVERY_BLOCKED_NO_RISK_INCREASING_MAKER_BACK"
        state["place_due_pt"] = None
        state["stale_due_pt"] = None
        reason = str(state["reason"])
        sl_state = str(state["state"])

    package_signal = _second_leg_package_signal(
        frozen_pkg=package_preview,
        fresh_pkg=fresh_package_preview,
    )

    # Shadow auto-rebuild:
    # If the active frozen package becomes invalid/stale versus fresh market validation,
    # start a new shadow epoch. This still does NOT place/cancel real orders.
    sig_name = str(package_signal.get("signal") or "OK")
    active_package_actions = 0
    if isinstance(package_preview, dict):
        active_package_actions = int(package_preview.get("action_count") or 0)

    if (
        str(state.get("state") or "") == "PACKAGE_PLACED_SHADOW"
        and sig_name in ("BETTER_FRESH", "FRESH_WORSE", "FRESH_SIZE_CHANGED")
    ):
        rebuild_delay_ms = (
            SECOND_LEG_REBUILD_DELAY_MS
            if active_package_actions > 0
            else SECOND_LEG_PLACE_DELAY_MS
        )

        prev_signal = dict(package_signal)

        state["epoch"] = int(state.get("epoch") or 0) + 1
        state["decision_pt"] = now_pt
        state["place_due_pt"] = now_pt + rebuild_delay_ms
        state["stale_due_pt"] = now_pt + rebuild_delay_ms + SECOND_LEG_STALE_AFTER_PLACE_MS
        state["state"] = "WAITING_PLACE_DELAY"
        state["reason"] = "MARKET_REBUILD_" + sig_name
        state["package_preview"] = fresh_package_preview
        state["package_decision_frame"] = frame_i
        state["package_decision_pt"] = now_pt

        reason = str(state["reason"])
        epoch = int(state.get("epoch") or 0)
        sl_state = str(state.get("state") or "IDLE")
        package_preview = fresh_package_preview

        package_signal = {
            "signal": "REBUILD_TRIGGERED",
            "reason": "active_package_invalidated_by_fresh_signal",
            "trigger": prev_signal,
            "rebuild_delay_ms": rebuild_delay_ms,
            "active_package_actions": active_package_actions,
        }

    shadow_fill = _second_leg_shadow_fill_probe(
        markets=markets,
        package_preview=package_preview,
        active=str(state.get("state") or "") == "PACKAGE_PLACED_SHADOW",
    )

    if str(shadow_fill.get("status") or "") in ("PARTIAL_SIGNAL", "FULL_SIGNAL"):
        new_filled_actions: list[dict[str, object]] = []

        for fill in list(shadow_fill.get("fills") or []):
            try:
                fill_stake = float(fill.get("fill_size") or 0.0)
                fill_price = float(fill.get("price") or 0.0)
            except Exception:
                continue

            if fill_stake <= 1e-9 or fill_price <= 1.0:
                continue

            seq = int(state.get("filled_second_leg_seq") or 0) + 1
            state["filled_second_leg_seq"] = seq

            new_filled_actions.append(
                {
                    "seq": seq,
                    "frame": frame_i,
                    "pt": now_pt,
                    "mode": "TAKER_SIGNAL" if fill.get("fill_reason") == "PRICE_CROSSED_BY_BEST_LAY" else "MAKER_SIGNAL",
                    "side": "BACK",
                    "runner": str(fill.get("runner") or ""),
                    "price": round(fill_price, 6),
                    "stake": round(fill_stake, 6),
                    "reason": str(fill.get("fill_reason") or ""),
                }
            )

        if new_filled_actions:
            filled_second_leg_actions = list(state.get("filled_second_leg_actions") or [])
            filled_second_leg_actions.extend(new_filled_actions)
            state["filled_second_leg_actions"] = filled_second_leg_actions

            fresh_package_preview = _second_leg_candidate_package_preview(
                markets=markets,
                outcome_pnls=outcome_pnls,
                filled_second_leg_actions=filled_second_leg_actions,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )

            state["epoch"] = int(state.get("epoch") or 0) + 1
            state["decision_pt"] = now_pt
            state["place_due_pt"] = now_pt + SECOND_LEG_REBUILD_DELAY_MS
            state["stale_due_pt"] = now_pt + SECOND_LEG_REBUILD_DELAY_MS + SECOND_LEG_STALE_AFTER_PLACE_MS
            state["state"] = "WAITING_PLACE_DELAY"
            state["reason"] = "SECOND_LEG_FILL_REBUILD"
            state["package_preview"] = fresh_package_preview
            state["package_decision_frame"] = frame_i
            state["package_decision_pt"] = now_pt

            reason = str(state["reason"])
            epoch = int(state.get("epoch") or 0)
            sl_state = str(state.get("state") or "IDLE")
            package_preview = fresh_package_preview

            filled_totals_greenup_actions = list(state.get("filled_totals_greenup_actions") or [])
            filled_totals_risk_reduction_actions = list(state.get("filled_totals_risk_reduction_actions") or [])  # fill_rebuild_trr_budget_pass_fix_marker

            second_leg_combined_profile = _second_leg_combined_bucket_profile(
                markets=markets,
                outcome_pnls=outcome_pnls,
                filled_second_leg_actions=filled_second_leg_actions,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )
            second_leg_totals_greenup_preview = _second_leg_totals_greenup_preview(
                markets=markets,
                second_leg_combined_profile=second_leg_combined_profile,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )
            second_leg_totals_risk_reduction_preview = _second_leg_totals_risk_reduction_preview(
                markets=markets,
                second_leg_combined_profile=second_leg_combined_profile,
                filled_totals_greenup_actions=filled_totals_greenup_actions,
                filled_totals_risk_reduction_actions=filled_totals_risk_reduction_actions,
            )

            package_signal = {
                "signal": "FILL_REBUILD_TRIGGERED",
                "reason": "second_leg_shadow_fill_signal",
                "new_filled_actions": new_filled_actions,
                "rebuild_delay_ms": SECOND_LEG_REBUILD_DELAY_MS,
            }

    place_due = int(state.get("place_due_pt") or 0)
    stale_due = int(state.get("stale_due_pt") or 0)

    place_in = None
    stale_in = None
    if place_due:
        place_in = max(0.0, (place_due - now_pt) / 1000.0)
    if stale_due and now_pt >= place_due:
        stale_in = max(0.0, (stale_due - now_pt) / 1000.0)

    epoch = int(state.get("epoch") or 0)
    sl_state = str(state.get("state") or "IDLE")

    compact_log_key = (epoch, sl_state, reason)
    if state.get("last_logged_key") != compact_log_key:
        state["last_logged_key"] = compact_log_key
        _second_leg_debug_write_jsonl(
            debug_jsonl=debug_jsonl,
            payload={
                "type": "second_leg_shadow",
                "frame": frame_i,
                "pt": now_pt,
                "utc": format_pt(now_pt) if now_pt else None,
                "epoch": epoch,
                "state": sl_state,
                "reason": reason,
                "worst": worst,
                "best": best,
                "residual_abs": residual_abs,
                "max_capital": max_capital,
                "place_due_pt": state.get("place_due_pt"),
                "stale_due_pt": state.get("stale_due_pt"),
                "cs": cs,
                "package_preview": package_preview,
                "fresh_package_preview": fresh_package_preview,
                "package_compact": _second_leg_compact_package(package_preview),
                "fresh_package_compact": _second_leg_compact_package(fresh_package_preview),
                "package_signal": package_signal,
                "shadow_fill": shadow_fill,
                "filled_second_leg": _second_leg_compact_filled_actions(filled_second_leg_actions),
                "second_leg_combined_profile": second_leg_combined_profile,
                "second_leg_totals_greenup_preview": second_leg_totals_greenup_preview,
                "second_leg_totals_risk_reduction_preview": second_leg_totals_risk_reduction_preview,
                "second_leg_recovery_preview": second_leg_recovery_preview,
                "second_leg_recovery_exec": second_leg_recovery_exec,
                "totals_risk_reduction_exec": totals_risk_reduction_exec,
                "filled_totals_risk_reduction": _second_leg_compact_totals_risk_reduction_actions(filled_totals_risk_reduction_actions),
                "totals_greenup_exec": totals_greenup_exec,
                "filled_totals_greenup": _second_leg_compact_totals_greenup_actions(filled_totals_greenup_actions),
                "package_decision_frame": state.get("package_decision_frame"),
                "package_decision_pt": state.get("package_decision_pt"),
                "outcome": {
                    ("G9+" if int(k) == 9 else f"G{int(k)}"): float(v)
                    for k, v in sorted(outcome_pnls.items())
                },
            },
        )

    place_txt = "-" if place_in is None else f"{place_in:.0f}s"
    stale_txt = "-" if stale_in is None else f"{stale_in:.0f}s"

    pkg_mode = str(package_preview.get("mode") or "-")
    pkg_actions = int(package_preview.get("action_count") or 0)
    pkg_capital = float(package_preview.get("capital") or 0.0)
    pkg_worst_if_full = package_preview.get("worst_if_full")
    pkg_worst_txt = "-" if pkg_worst_if_full is None else f"{float(pkg_worst_if_full):+.2f}"
    pkg_signal_txt = str(package_signal.get("signal") or "-")
    fill_status_txt = str(shadow_fill.get("status") or "-")
    fill_actions_txt = int(shadow_fill.get("filled_actions") or 0)
    fill_stake_txt = float(shadow_fill.get("filled_stake") or 0.0)
    filled_second_leg_stake_txt = sum(
        float(x.get("stake", 0.0) or 0.0)
        for x in list(filled_second_leg_actions or [])
    )
    filled_second_leg_count_txt = len(list(filled_second_leg_actions or []))
    filled_totals_greenup_stake_txt = sum(
        float(x.get("stake", 0.0) or 0.0)
        for x in list(filled_totals_greenup_actions or [])
    )
    filled_totals_greenup_count_txt = len(list(filled_totals_greenup_actions or []))
    filled_totals_greenup_greenup_txt = sum(
        float(x.get("greenup", 0.0) or 0.0)
        for x in list(filled_totals_greenup_actions or [])
    )
    tg_exec_status_txt = str(totals_greenup_exec.get("status") or "-")
    trr_mode_txt = str(second_leg_totals_risk_reduction_preview.get("mode") or "-")
    trr_actions_txt = int(second_leg_totals_risk_reduction_preview.get("action_count") or 0)
    trr_cost = second_leg_totals_risk_reduction_preview.get("cost")
    trr_imp = second_leg_totals_risk_reduction_preview.get("worst_improvement")
    trr_cost_txt = "-" if trr_cost is None else f"{float(trr_cost):.2f}"
    trr_imp_txt = "-" if trr_imp is None else f"{float(trr_imp):+.2f}"
    trr_exec_status_txt = str(totals_risk_reduction_exec.get("status") or "-")
    filled_totals_risk_reduction_count_txt = len(list(filled_totals_risk_reduction_actions or []))
    filled_totals_risk_reduction_stake_txt = sum(
        float(x.get("stake", 0.0) or 0.0)
        for x in list(filled_totals_risk_reduction_actions or [])
    )
    filled_totals_risk_reduction_cost_txt = sum(
        float(x.get("cost", 0.0) or 0.0)
        for x in list(filled_totals_risk_reduction_actions or [])
    )
    slc_worst = second_leg_combined_profile.get("worst")
    slc_best = second_leg_combined_profile.get("best")
    slc_drop = second_leg_combined_profile.get("worst_drop_from_first_leg")
    slc_worst_txt = "-" if slc_worst is None else f"{float(slc_worst):+.2f}"
    slc_best_txt = "-" if slc_best is None else f"{float(slc_best):+.2f}"
    slc_drop_txt = "-" if slc_drop is None else f"{float(slc_drop):.2f}"
    tg_mode_txt = str(second_leg_totals_greenup_preview.get("mode") or "-")
    tg_actions_txt = int(second_leg_totals_greenup_preview.get("action_count") or 0)
    tg_greenup = second_leg_totals_greenup_preview.get("greenup")
    tg_imp = second_leg_totals_greenup_preview.get("worst_improvement")
    tg_greenup_txt = "-" if tg_greenup is None else f"{float(tg_greenup):+.2f}"
    tg_imp_txt = "-" if tg_imp is None else f"{float(tg_imp):+.2f}"

    state_short = {
        "WAITING_PLACE_DELAY": "WAIT",
        "PACKAGE_PLACED_SHADOW": "PLACED",
        "BUDGET_BLOCKED_SHADOW": "BLOCK",
        "RECOVERY_BLOCKED_SHADOW": "RBLOCK",
        "IDLE": "IDLE",
    }.get(sl_state, sl_state)

    reason_short = {
        "FIRST_LEG_CHANGED": "FL",
        "STALE_10S_NO_FILL": "STALE",
        "SHADOW_PACKAGE_ACTIVE": "ACTIVE",
        "WAITING_PLACE_DELAY": "WAIT",
    }.get(reason, reason)

    return (
        f"SL ep={epoch} {state_short}/{reason_short} "
        f"w={worst:+.2f} cap={max_capital:.0f} "
        f"PKG={pkg_mode}:{pkg_actions} pc={pkg_capital:.2f} wf={pkg_worst_txt} "
        f"sig={pkg_signal_txt} fill={fill_status_txt}:{fill_actions_txt}/{fill_stake_txt:.2f} "
        f"slf={filled_second_leg_count_txt}/{filled_second_leg_stake_txt:.2f} "
        f"SLC={slc_worst_txt}/{slc_best_txt}/d{slc_drop_txt} "
        f"TG={tg_mode_txt}:{tg_actions_txt}/g{tg_greenup_txt}/w{tg_imp_txt} "
        f"tgf={filled_totals_greenup_count_txt}/{filled_totals_greenup_stake_txt:.2f}/g{filled_totals_greenup_greenup_txt:.2f}/{tg_exec_status_txt} "
        f"TRR={trr_mode_txt}:{trr_actions_txt}/c{trr_cost_txt}/w{trr_imp_txt}/{trr_exec_status_txt} "
        f"trrf={filled_totals_risk_reduction_count_txt}/{filled_totals_risk_reduction_stake_txt:.2f}/c{filled_totals_risk_reduction_cost_txt:.2f} "
        f"df={state.get('package_decision_frame')} "
        f"CS={int(cs['runners'])}/{int(cs['two_sided'])} "
        f"MB={int(cs['maker_back_candidates'])} "
        f"TB={int(cs['taker_back_candidates'])} "
        f"L<={SECOND_LEG_CS_LAY_MAX_PRICE:g}:{int(cs['safe_lay_candidates'])} "
        f"p={place_txt} s={stale_txt}"
    )


def _set_clean_maker_overlay_before_render(
    balance: float | None,
    enabled: bool,
    markets: dict[str, MarketState] | None = None,
    second_leg_debug: bool = False,
    frame_index: int | None = None,
    pt: int | None = None,
    second_leg_debug_jsonl: str | None = None,
) -> None:
    global ENGINE_V2_OVERLAY_LINE
    global ENGINE_V2_TAPE_LINE

    if not enabled:
        return

    if not MAKER_UNDER_LAY_GRID_ENABLED:
        return

    if SIMULATE_ORDERS_ENABLED:
        return

    base_balance = float(balance or 0.0)

    exposure = _maker_under_lay_grid_visible_totals_exposure(markets)

    matched_stake_total = float(exposure.get("matched_stake", 0.0) or 0.0)
    matched_liability_total = float(exposure.get("matched_liability", 0.0) or 0.0)
    open_liability = float(exposure.get("open_liability", 0.0) or 0.0)
    active_orders = int(float(exposure.get("active_orders", 0.0) or 0.0))

    locked = open_liability + matched_liability_total
    free = base_balance - locked

    outcome_pnls = _maker_under_lay_grid_outcome_pnls(markets)
    pnl_proxy = min(outcome_pnls.values()) if outcome_pnls else -matched_liability_total

    ENGINE_V2_OVERLAY_LINE = (
        f"ENGINE_V2: active=0 next10s=0 "
        f"locked={locked:.2f} "
        f"free={free:.2f} "
        f"pnl_proxy={pnl_proxy:.4f} NEXT=-"
        f" maker_grid_active={active_orders}"
        + (" maker_matching=ON" if MAKER_UNDER_LAY_GRID_MATCHING_ENABLED else " maker_matching=OFF")
        + f" maker_matched={matched_stake_total:.2f}"
        + f" maker_matched_liability={matched_liability_total:.2f}"
        + f" maker_liability={open_liability:.2f}"
    )

    outcome_line = _maker_under_lay_grid_outcome_line(outcome_pnls)
    second_leg_line = ""
    if second_leg_debug:
        second_leg_line = _second_leg_cs_debug_line(
            markets=markets,
            outcome_pnls=outcome_pnls,
            frame_index=frame_index,
            pt=pt,
            debug_jsonl=second_leg_debug_jsonl,
        )

    if outcome_line and second_leg_line:
        ENGINE_V2_TAPE_LINE = outcome_line + " || " + second_leg_line
    elif second_leg_line:
        ENGINE_V2_TAPE_LINE = second_leg_line
    else:
        ENGINE_V2_TAPE_LINE = outcome_line

def stream_replay(args: argparse.Namespace) -> int:
    if not args.replay_file.exists():
        print(f"File not found: {args.replay_file}")
        return 1

    balance: float | None = float(args.balance) if getattr(args, "balance", None) is not None else None
    interactive = bool(getattr(args, "interactive", False))
    paused = False
    step_frames = 0
    input_fd: int | None = None
    input_termios_old: list[int] | None = None
    input_old_flags: int | None = None
    history_max = 5000

    @dataclass
    class _FrameSnap:
        file_pos: int
        frame_index: int
        frame_pt: int
        earliest_start_pt: int | None
        selected_ids: set[str]
        dedup_market_ids: set[str]
        markets: dict[str, MarketState]
        meta_by_market_id: dict[str, MarketMeta]

    history: list[_FrameSnap] = []
    hist_i = -1
    interactive_err: str | None = None
    last_key: str | None = None

    seeded_targets: dict[str, dict[str, str | None]] = {}
    selected_ids = set(str(market_id) for market_id in args.market_id)
    if not args.discover_targets:
        seeded_targets = {
            market_id: seed
            for market_id, seed in parse_target_markets_file(args.target_markets_file).items()
            if is_target_market_type(seed.get("market_type"))
        }
        selected_ids.update(seeded_targets)

    if not selected_ids and not args.discover_targets:
        print(
            "No target markets configured. Pass --market-id, create "
            f"{args.target_markets_file}, or use --discover-targets."
        )
        return 1

    markets: dict[str, MarketState] = {}
    meta_by_market_id: dict[str, MarketMeta] = {}
    order_model = OrderModel()

    engine_v2_orders = []
    if bool(getattr(args, "engine_v2_overlay", False)):
        engine_v2_orders = _engine_v2_load_orders(str(getattr(args, "engine_v2_orders", "")))

    seeded_under_lay_grid: set[tuple[str, int, float | None, float]] = set()
    frames = 0
    next_frame_pt: int | None = None
    cadence_ms = max(1, int(args.cadence_ms))
    earliest_start_pt: int | None = None

    snapshots_csv_file = None
    snapshots_writer: csv.DictWriter[str] | None = None
    snapshots_rows = 0
    if not args.no_snapshots_csv:
        args.snapshots_csv.parent.mkdir(parents=True, exist_ok=True)
        snapshots_csv_file = args.snapshots_csv.open("w", encoding="utf-8", newline="")
        snapshots_writer = csv.DictWriter(
            snapshots_csv_file,
            fieldnames=[
                "tick",
                "pt",
                "pt_utc",
                "market_id",
                "market_type",
                "market_name",
                "market_status",
                "in_play",
                "market_time",
                "selection_id",
                "handicap",
                "runner_name",
                "runner_status",
                "best_back",
                "best_back_size",
                "best_lay",
                "best_lay_size",
                "ltp",
                "traded_volume",
            ],
            extrasaction="ignore",
        )
        snapshots_writer.writeheader()

    if not args.no_clear and not bool(args.emit_json):
        if bool(args.smooth_ui):
            _alt_screen_enter()
            move_top()
        else:
            clear_once()

    if interactive:
        # Pick exactly one input FD to avoid leaving the terminal in a weird state.
        # Prefer stdin when it's a TTY; otherwise fall back to /dev/tty.
        tty_fd = None
        stdin_fd = None
        try:
            stdin_fd = sys.stdin.fileno()
        except Exception:
            stdin_fd = None
        if stdin_fd is not None and os.isatty(stdin_fd):
            input_fd = stdin_fd
        else:
            try:
                tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
                input_fd = tty_fd
            except OSError:
                input_fd = stdin_fd

        if input_fd is not None:
            try:
                input_termios_old = termios.tcgetattr(input_fd)
                control_fd = None
                control_fifo = os.environ.get("STREEM_REPLAY_CONTROL_FIFO")
                if control_fifo:
                    try:
                        control_fd = os.open(control_fifo, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError:
                        control_fd = None

                tty.setraw(input_fd)
            except Exception:
                input_termios_old = None
            try:
                input_old_flags = fcntl.fcntl(input_fd, fcntl.F_GETFL)
                fcntl.fcntl(input_fd, fcntl.F_SETFL, input_old_flags | os.O_NONBLOCK)
            except Exception:
                input_old_flags = None

    try:
        with args.replay_file.open("r", encoding="utf-8") as replay:
            line_number = 0
            while True:
                pos_before = replay.tell()
                line = replay.readline()
                if not line:
                    break
                line_number += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"Skipping invalid JSON at line {line_number}: {exc}")
                    continue

                pt = message.get("pt")
                if not isinstance(pt, (int, float)):
                    continue
                pt_ms = int(pt)
                if next_frame_pt is None:
                    # Align the first frame to the next cadence boundary (exclusive),
                    # so each cycle is exactly cadence_ms apart.
                    next_frame_pt = ((pt_ms // cadence_ms) + 1) * cadence_ms

                changed_selected_ids: list[str] = []
                for market_change in message.get("mc", []):
                    market_id = str(market_change.get("id"))
                    if not market_id or market_id == "None":
                        continue

                    market_definition = market_change.get("marketDefinition")
                    if args.discover_targets and isinstance(market_definition, dict):
                        if is_target_market_type(market_definition.get("marketType")):
                            selected_ids.add(market_id)

                    if market_id not in selected_ids:
                        continue

                    state = ensure_market(markets, market_id, seeded_targets.get(market_id))
                    if market_change.get("img") is True:
                        state.runners = {}

                    if isinstance(market_definition, dict):
                        meta_by_market_id[market_id] = MarketMeta(
                            event_id=str(market_definition.get("eventId")) if market_definition.get("eventId") is not None else None,
                            market_type=str(market_definition.get("marketType")) if market_definition.get("marketType") is not None else None,
                            market_time=str(market_definition.get("marketTime")) if market_definition.get("marketTime") is not None else None,
                            cross_matching=bool(market_definition.get("crossMatching")) if market_definition.get("crossMatching") is not None else None,
                            regulators=tuple(str(x) for x in (market_definition.get("regulators") or []) if x is not None),
                        )
                        # Override start window to N minutes before.
                        update_market_metadata(state, market_definition, start_hours_before=0.0)
                        parsed_market_time = parse_market_time(market_definition.get("marketTime"))
                        if parsed_market_time is not None:
                            state.market_time = parsed_market_time
                            start_time = state.market_time - timedelta(minutes=max(args.start_minutes_before, 0.0))
                            state.stream_start_pt = datetime_to_pt(start_time)
                            if state.stream_start_pt is not None:
                                if earliest_start_pt is None or state.stream_start_pt < earliest_start_pt:
                                    earliest_start_pt = state.stream_start_pt
                        apply_market_definition(state.runners, market_definition)

                    for runner_change in market_change.get("rc", []):
                        if isinstance(runner_change, dict):
                            apply_runner_change(state.runners, runner_change)

                    if market_id not in changed_selected_ids:
                        changed_selected_ids.append(market_id)

                # advance tick counters for changed markets (monotonic per market)
                for market_id in changed_selected_ids:
                    st = markets.get(market_id)
                    if st is not None:
                        st.tick += 1

                # Fixed cadence loop: for every 250ms boundary we crossed, emit a frame
                # using the latest known market states (carry-forward).
                while next_frame_pt is not None and pt_ms >= next_frame_pt:
                    # If we know we shouldn't start yet (10min pre-event), fast-forward
                    # the frame clock to the earliest start boundary.
                    if earliest_start_pt is not None and next_frame_pt < earliest_start_pt:
                        next_frame_pt = ((earliest_start_pt // cadence_ms) * cadence_ms)
                        if next_frame_pt < earliest_start_pt:
                            next_frame_pt += cadence_ms
                        continue

                    grouped: dict[tuple[str | None, str | None, str | None], list[str]] = {}
                    for mid in selected_ids:
                        st = markets.get(mid)
                        if st is None:
                            continue
                        key = logical_market_key(st, meta_by_market_id.get(mid))
                        grouped.setdefault(key, []).append(mid)
                    dedup_market_ids: set[str] = set()
                    for mids in grouped.values():
                        if len(mids) == 1:
                            dedup_market_ids.add(mids[0])
                        else:
                            dedup_market_ids.add(pick_canonical_market_id(mids, meta_by_market_id))

                    frames += 1

                    # ENGINE_V2: seed maker-only LAY grid on totals Under runner.
                    # Safe:
                    #   - one selected line OR all visible Over/Under lines;
                    #   - never crosses best_back;
                    #   - never overwrites existing level, so queue position is preserved.
                    if bool(getattr(args, "seed_under_lay_grid", False)):
                        want_line = float(getattr(args, "seed_under_lay_grid_line", 5.5))
                        all_lines = bool(getattr(args, "seed_under_lay_grid_all_lines", False))
                        px_lo = float(getattr(args, "seed_under_lay_grid_low", 1.01))
                        px_hi = float(getattr(args, "seed_under_lay_grid_high", 1.20))
                        stake_each = float(getattr(args, "seed_under_lay_grid_size", 10.0))
                        cap_at_bl = bool(getattr(args, "seed_under_lay_grid_cap_at_bl", True))

                        for mid in sorted(dedup_market_ids):
                            st = markets.get(mid)
                            if st is None or not should_render(st, next_frame_pt):
                                continue
                            if not is_over_under_goals(st):
                                continue

                            line = over_under_line(st)
                            if line is None:
                                continue
                            if (not all_lines) and abs(float(line) - want_line) > 1e-9:
                                continue

                            under: RunnerState | None = None
                            for r in st.runners.values():
                                if (r.name or "").lower().startswith("under"):
                                    under = r
                                    break
                            if under is None:
                                continue

                            bl = best_level(under.available_to_lay, side="LAY")
                            bb = best_level(under.available_to_back, side="BACK")
                            if bl is None or bb is None:
                                continue

                            best_lay = float(bl[0])
                            best_back = float(bb[0])
                            if px_hi < px_lo:
                                continue

                            for px in ladder_window_range(px_lo, px_hi):
                                px = float(px)

                                # STRICT maker-only LAY for this dashboard:
                                # - MYL must sit ONLY on visual L column;
                                # - visual L column = available_to_back / bsz;
                                # - visual B column = available_to_lay / lsz;
                                # - never place where Q0 would be 0;
                                # - never place where visual B side exists at same price.
                                l_q = under.available_to_back.get(px)
                                b_q = under.available_to_lay.get(px)

                                if l_q is None or float(l_q) <= 0:
                                    continue
                                if b_q is not None and float(b_q) > 0:
                                    continue

                                seed_key = (mid, int(under.selection_id), under.handicap, px)
                                order_key = (mid, int(under.selection_id), under.handicap, px)

                                existing = order_model.by_key.get(order_key)

                                # Do not overwrite active queue position.
                                if existing is not None and existing.my_lay > 0:
                                    continue

                                # Refill only if there is no active MYL left at this level.
                                matched_keep = 0.0 if existing is None else float(existing.matched)

                                seeded_under_lay_grid.add(seed_key)
                                order_model.by_key[order_key] = MyOrdersAtPrice(
                                    my_lay=max(0.0, stake_each),
                                    my_back=0.0,
                                    matched=matched_keep,
                                )

                    if bool(getattr(args, "engine_v2_overlay", False)):
                        if SIMULATE_ORDERS_ENABLED:
                            _engine_v2_apply_orders_to_order_model(
                                pt=int(next_frame_pt),
                                markets=markets,
                                order_model=order_model,
                                orders=engine_v2_orders,
                                balance=balance,
                            )

                    if SIMULATE_ORDERS_ENABLED:
                        update_order_model_from_current_ladder(
                            markets=markets,
                            order_model=order_model,
                        )

                    if MAKER_UNDER_LAY_GRID_ENABLED:
                        _apply_maker_under_lay_grid_orders(
                            markets=markets,
                            order_model=order_model,
                            pt=int(next_frame_pt),
                            frame_no=int(frames),
                        )

                    global ENGINE_V2_OVERLAY_LINE
                    if bool(getattr(args, "engine_v2_overlay", False)):
                        ENGINE_V2_OVERLAY_LINE = _engine_v2_overlay_line(
                            pt=int(next_frame_pt),
                            balance=balance,
                            orders=engine_v2_orders,
                        )
                        if MAKER_UNDER_LAY_GRID_ENABLED:
                            ENGINE_V2_OVERLAY_LINE = (
                                ENGINE_V2_OVERLAY_LINE
                                + f" maker_grid_active={MAKER_UNDER_LAY_GRID_ACTIVE_ORDERS}"
                                + (" maker_matching=ON" if MAKER_UNDER_LAY_GRID_MATCHING_ENABLED else " maker_matching=OFF") + f" maker_matched={MAKER_UNDER_LAY_GRID_MATCHED_TOTAL:.2f}" + f" maker_liability={MAKER_UNDER_LAY_GRID_LIABILITY_TOTAL:.2f}"
                            )
                        ENGINE_V2_TAPE_LINE = _engine_v2_tape_line(
                            pt=int(next_frame_pt),
                            orders=engine_v2_orders,
                        )
                    else:
                        ENGINE_V2_OVERLAY_LINE = ""
                        ENGINE_V2_TAPE_LINE = ""

                    if args.emit_json:
                        payload = build_emit_json_frame(
                            pt=next_frame_pt,
                            utc=format_pt(next_frame_pt),
                            markets=markets,
                            market_ids=dedup_market_ids,
                            top_n=int(args.top),
                            ou_under_lay_min=float(args.ou_under_lay_min),
                            ou_under_lay_max=float(args.ou_under_lay_max),
                            price_low=1.01,
                            price_high=1.40,
                            emit_mode=str(args.emit_json_mode),
                        )
                        print(json.dumps(payload, ensure_ascii=False))
                    else:
	                        _set_clean_maker_overlay_before_render(
	                            balance,
	                            bool(getattr(args, "engine_v2_overlay", False)),
	                            markets=markets,
	                            second_leg_debug=bool(getattr(args, "second_leg_cs_debug", False)),
	                            frame_index=frames,
	                            pt=int(next_frame_pt) if next_frame_pt is not None else None,
	                            second_leg_debug_jsonl=str(getattr(args, "second_leg_debug_jsonl", "")),
	                        )
	                        render_dashboard(
	                            pt=next_frame_pt,
	                            markets=markets,
	                            selected_ids=selected_ids,
	                            market_ids=dedup_market_ids,
                            top_n=args.top,
                            depth=args.depth,
                            no_clear=args.no_clear,
                            ou_under_lay_min=args.ou_under_lay_min,
                            ou_under_lay_max=args.ou_under_lay_max,
                            frame_index=frames,
                            cadence_ms=cadence_ms,
                            ladder=args.ladder,
                            center_mode=args.center,
                            ticks_above=args.ticks_above,
                            ticks_below=args.ticks_below,
	                            col_width=args.col_width,
	                            cs_cols=args.cs_cols,
	                            cs_dutch_signals=bool(getattr(args, "cs_dutch_signals", False)),
	                            ladder_nonempty_only=bool(args.ladder_nonempty_only),
                            ladder_max_rows=int(args.ladder_max_rows or 0),
                            honest_cs=bool(args.honest_cs),
                            dutching_debug=bool(args.dutching_debug),
                            stake_total=float(args.stake_total),
                            show_stakes=bool(args.show_stakes),
                            lay_max_liability=float(args.lay_max_liability),
                            show_lay_stakes=bool(args.show_lay_stakes),
                            lay_ui=bool(args.lay_ui),
                            demo_orders=bool(args.demo_orders),
                            list_totals=bool(args.list_totals),
                            list_totals_ladder=bool(args.list_totals_ladder),
                            list_totals_one_line=bool(args.list_totals_one_line),
                            totals_all=bool(getattr(args, "totals_all", False)),
                            totals_center_threshold=float(getattr(args, "totals_center_threshold", 1.30)),
                            totals_rows=int(getattr(args, "totals_rows", 1)),
                            totals_sticky=bool(getattr(args, "totals_sticky", False)),
                            self_check=bool(args.self_check),
                            smooth_ui=bool(args.smooth_ui),
                            balance=balance,
                            order_model=order_model,
                            show_queue=bool(getattr(args, "show_queue", False)),
                            paused=paused,
                            err=interactive_err,
                            key=last_key,
                        )
	                        _emit_stable_frame_end()

                    # Save snapshot for backward stepping (only when not browsing history).
                    if interactive:
                        pos_after = replay.tell()
                        snap = _FrameSnap(
                            file_pos=pos_after,
                            frame_index=frames,
                            frame_pt=int(next_frame_pt),
                            earliest_start_pt=earliest_start_pt,
                            selected_ids=set(selected_ids),
                            dedup_market_ids=set(dedup_market_ids),
                            markets=copy.deepcopy(markets),
                            meta_by_market_id=copy.deepcopy(meta_by_market_id),
                        )
                        if hist_i < len(history) - 1:
                            history[:] = history[: hist_i + 1]
                        history.append(snap)
                        if len(history) > history_max:
                            drop = len(history) - history_max
                            history[:] = history[drop:]
                            hist_i = max(-1, hist_i - drop)
                        hist_i = len(history) - 1

                    if snapshots_writer is not None:
                        snapshots_rows += write_snapshot_rows(
                            snapshots_writer,
                            pt=next_frame_pt,
                            markets=markets,
                            market_ids=dedup_market_ids,
                        )
                        if snapshots_csv_file is not None:
                            snapshots_csv_file.flush()

                    if args.max_frames and frames >= args.max_frames:
                        return 0

                    if interactive:
                        def _rebuild_maker_grid_to_history_index(target_idx: int) -> None:
                            _maker_grid_hard_reset_state(order_model)

                            if not MAKER_UNDER_LAY_GRID_ENABLED:
                                return

                            for _replay_idx in range(0, int(target_idx) + 1):
                                _hs = history[_replay_idx]
                                _apply_maker_under_lay_grid_orders(
                                    markets=copy.deepcopy(_hs.markets),
                                    order_model=order_model,
                                    pt=int(_hs.frame_pt),
                                    frame_no=int(_hs.frame_index),
                                )

                            if bool(getattr(args, "engine_v2_overlay", False)):
                                _overlay = _engine_v2_overlay_line(
                                    pt=int(history[target_idx].frame_pt),
                                    balance=balance,
                                    orders=engine_v2_orders,
                                )
                                _overlay += (
                                    f" maker_grid_active={MAKER_UNDER_LAY_GRID_ACTIVE_ORDERS}"
                                    + (" maker_matching=ON" if MAKER_UNDER_LAY_GRID_MATCHING_ENABLED else " maker_matching=OFF")
                                    + f" maker_matched={MAKER_UNDER_LAY_GRID_MATCHED_TOTAL:.2f}"
                                    + f" maker_liability={MAKER_UNDER_LAY_GRID_LIABILITY_TOTAL:.2f}"
                                )
                                globals()["ENGINE_V2_OVERLAY_LINE"] = _overlay


                        def _apply_snap(idx: int) -> None:
                            nonlocal markets, meta_by_market_id, frames, next_frame_pt, earliest_start_pt, hist_i, selected_ids
                            nonlocal interactive_err
                            s = history[idx]
                            try:
                                replay.seek(s.file_pos)
                                markets = copy.deepcopy(s.markets)
                                meta_by_market_id = copy.deepcopy(s.meta_by_market_id)
                                selected_ids = set(s.selected_ids)
                                frames = int(s.frame_index)
                                next_frame_pt = int(s.frame_pt + cadence_ms)
                                earliest_start_pt = s.earliest_start_pt
                                hist_i = idx
                                _rebuild_maker_grid_to_history_index(hist_i)
                                interactive_err = None
                                _set_clean_maker_overlay_before_render(
                                    balance,
                                    bool(getattr(args, "engine_v2_overlay", False)),
                                    markets=markets,
                                    second_leg_debug=bool(getattr(args, "second_leg_cs_debug", False)),
                                    frame_index=frames,
                                    pt=int(next_frame_pt) if next_frame_pt is not None else None,
                                    second_leg_debug_jsonl=str(getattr(args, "second_leg_debug_jsonl", "")),
                                )
                                render_dashboard(
                                    pt=int(s.frame_pt),
                                    markets=markets,
                                    selected_ids=selected_ids,
                                    market_ids=set(s.dedup_market_ids),
                                    top_n=args.top,
                                    depth=args.depth,
                                    no_clear=args.no_clear,
                                    ou_under_lay_min=args.ou_under_lay_min,
                                    ou_under_lay_max=args.ou_under_lay_max,
                                    frame_index=frames,
                                    cadence_ms=cadence_ms,
                                    ladder=args.ladder,
                                    center_mode=args.center,
                                    ticks_above=args.ticks_above,
                                    ticks_below=args.ticks_below,
                                    col_width=args.col_width,
                                    cs_cols=args.cs_cols,
                                    cs_dutch_signals=bool(getattr(args, "cs_dutch_signals", False)),
                                    ladder_nonempty_only=bool(args.ladder_nonempty_only),
                                    ladder_max_rows=int(args.ladder_max_rows or 0),
                                    honest_cs=bool(args.honest_cs),
                                    dutching_debug=bool(args.dutching_debug),
                                    stake_total=float(args.stake_total),
                                    show_stakes=bool(args.show_stakes),
                                    lay_max_liability=float(args.lay_max_liability),
                                    show_lay_stakes=bool(args.show_lay_stakes),
                                    lay_ui=bool(args.lay_ui),
                                    demo_orders=bool(args.demo_orders),
                                    list_totals=bool(args.list_totals),
                                    list_totals_ladder=bool(args.list_totals_ladder),
                                    list_totals_one_line=bool(args.list_totals_one_line),
                                    totals_all=bool(getattr(args, "totals_all", False)),
                                    totals_center_threshold=float(getattr(args, "totals_center_threshold", 1.30)),
                                    totals_rows=int(getattr(args, "totals_rows", 1)),
                                    totals_sticky=bool(getattr(args, "totals_sticky", False)),
                                    self_check=bool(args.self_check),
                                    smooth_ui=bool(args.smooth_ui),
                                    balance=balance,
                                    order_model=order_model,
                                    show_queue=bool(getattr(args, "show_queue", False)),
                                    paused=True,
                                    err=interactive_err,
                                    key=last_key,
                                )
                                _emit_stable_frame_end()
                            except Exception as exc:
                                interactive_err = f"{type(exc).__name__}: {exc}"
                                # Don't crash the replay; keep paused so the user can continue.
                                return

                        def _read_key_now() -> str | None:
                            # Stable wrapper control channel has priority.
                            if "control_fd" in locals() and control_fd is not None:
                                if not hasattr(_read_key_now, "_ctrl_buf"):
                                    setattr(_read_key_now, "_ctrl_buf", "")
                                ctrl_buf: str = getattr(_read_key_now, "_ctrl_buf")
                                if ctrl_buf:
                                    ch, ctrl_buf = ctrl_buf[0], ctrl_buf[1:]
                                    setattr(_read_key_now, "_ctrl_buf", ctrl_buf)
                                    return ch
                                try:
                                    data = os.read(control_fd, 64)
                                except (BlockingIOError, InterruptedError, OSError):
                                    data = b""
                                if data:
                                    decoded = data.decode("utf-8", errors="ignore")
                                    if decoded:
                                        setattr(_read_key_now, "_ctrl_buf", decoded[1:])
                                        return decoded[0]

                            if input_fd is None:
                                return None
                            # We read in chunks (not 1 byte) to correctly handle UTF-8 multi-byte keys
                            # (e.g. Cyrillic layouts) and escape sequences.
                            if not hasattr(_read_key_now, "_buf"):
                                setattr(_read_key_now, "_buf", "")
                            buf: str = getattr(_read_key_now, "_buf")
                            if buf:
                                ch, buf = buf[0], buf[1:]
                                setattr(_read_key_now, "_buf", buf)
                                return ch

                            r, _w, _e = select.select([input_fd], [], [], 0)
                            if not r:
                                return None
                            try:
                                data = os.read(input_fd, 64)
                            except (BlockingIOError, InterruptedError):
                                return None
                            if not data:
                                return None
                            decoded = data.decode("utf-8", errors="ignore")
                            if not decoded:
                                return None
                            setattr(_read_key_now, "_buf", decoded[1:])
                            return decoded[0]

                        # Handle all pending keys (non-blocking).
                        forced_pause = False
                        while True:
                            k = _read_key_now()
                            if k is None:
                                break
                            last_key = k
                            if k in ("q", "Q", "й", "Й"):
                                return 0
                            if k == " ":
                                paused = not paused
                                if not paused:
                                    step_frames = 0
                            # Step controls: make them usable even while running by auto-pausing.
                            if k in ("n", "N", "т", "Т", "f", "F", "а", "А"):
                                paused = True
                                forced_pause = True
                                # If we already have a future snapshot (rare), jump to it; else request one-step.
                                if history and hist_i < len(history) - 1:
                                    _apply_snap(hist_i + 1)
                                else:
                                    step_frames = max(step_frames, 1)
                                break
                            if k in ("b", "B", "и", "И"):
                                paused = True
                                forced_pause = True
                                if history and hist_i > 0:
                                    _apply_snap(hist_i - 1)
                                else:
                                    # No history yet; show a hint in the header on the next repaint.
                                    interactive_err = "BACK: no history yet"
                                break

                        # When paused, block until resume/step/quit.
                        while paused and step_frames <= 0:
                            if input_fd is None:
                                break
                            r, _w, _e = select.select([input_fd], [], [], 0.25)
                            if not r:
                                continue
                            k = _read_key_now()
                            if k is None:
                                continue
                            last_key = k
                            if k in ("q", "Q", "й", "Й"):
                                return 0
                            if k == " ":
                                paused = False
                                step_frames = 0
                                break
                            if k in ("n", "N", "т", "Т", "f", "F", "а", "А"):
                                if history and hist_i < len(history) - 1:
                                    _apply_snap(hist_i + 1)
                                    continue
                                step_frames = 1
                                break
                            if k in ("b", "B", "и", "И") and history and hist_i > 0:
                                _apply_snap(hist_i - 1)
                                continue

                        if paused and step_frames > 0:
                            step_frames -= 1

                    if args.delay and args.delay > 0:
                        time.sleep(args.delay or DEFAULT_DELAY_SECONDS)

                    next_frame_pt += cadence_ms

        return 0
    except KeyboardInterrupt:
        print("\nStopped by user")
        return 130
    finally:
        if input_fd is not None and input_termios_old is not None:
            try:
                termios.tcsetattr(input_fd, termios.TCSADRAIN, input_termios_old)
            except Exception:
                pass
        if input_fd is not None and input_old_flags is not None:
            try:
                fcntl.fcntl(input_fd, fcntl.F_SETFL, input_old_flags)
            except Exception:
                pass
        if "control_fd" in locals() and control_fd is not None:
            try:
                os.close(control_fd)
            except OSError:
                pass

        # Close /dev/tty fd if we opened it (don't close stdin).
        if "tty_fd" in locals() and tty_fd is not None:
            try:
                os.close(tty_fd)
            except Exception:
                pass
        if not args.no_clear and not bool(args.emit_json) and bool(args.smooth_ui):
            _alt_screen_exit()
        if snapshots_csv_file is not None:
            snapshots_csv_file.close()


def main() -> int:
    args = parse_args()

    global SIMULATE_ORDERS_ENABLED
    global MAKER_UNDER_LAY_GRID_ENABLED
    global MAKER_UNDER_LAY_GRID_MATCHING_ENABLED

    SIMULATE_ORDERS_ENABLED = bool(getattr(args, "simulate_orders", False))
    MAKER_UNDER_LAY_GRID_ENABLED = bool(getattr(args, "maker_under_lay_grid", False))
    MAKER_UNDER_LAY_GRID_MATCHING_ENABLED = bool(getattr(args, "maker_under_lay_grid_match", False))

    return stream_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
