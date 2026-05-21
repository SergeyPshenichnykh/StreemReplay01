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
    global _SMOOTH_LAST_LINES
    cols = _terminal_cols()
    max_lines = max(len(_SMOOTH_LAST_LINES), len(lines))
    # Paint only changed lines; clear leftover tail if previous frame was longer.
    for idx in range(max_lines):
        new = lines[idx] if idx < len(lines) else ""
        old = _SMOOTH_LAST_LINES[idx] if idx < len(_SMOOTH_LAST_LINES) else None
        if old == new:
            continue
        vis = _strip_ansi(new)
        if len(vis) < cols:
            new = new + (" " * (cols - len(vis)))
        else:
            new = _truncate_visible(new, cols)
        # 1-based cursor positions. On some terminals stdout can be temporarily non-blocking;
        # retry a few times on EAGAIN to avoid crashing the stream.
        seq = f"\033[{idx + 1};1H{new}\033[K"
        for _ in range(5):
            try:
                sys.stdout.write(seq)
                break
            except BlockingIOError:
                time.sleep(0.001)
    _SMOOTH_LAST_LINES = lines
    for _ in range(5):
        try:
            sys.stdout.flush()
            break
        except BlockingIOError:
            time.sleep(0.001)


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
    # Best-effort: drop ANSI and truncate; re-apply no styling beyond what was in s.
    return plain[: max(0, width - 1)] + "…"


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
        "--engine-v2-orders",
        default="replay/delta_10s_macro_min10/filtered_shadow_bot_v2_no_asian.csv",
        help="CSV with V2 shadow orders.",
    )
    p.add_argument(
        "--engine-v2-show-markets",
        action="store_true",
        help="Show extra ladder columns for markets used by ENGINE V2 orders.",
    )
    p.add_argument(
        "--engine-v2-max-ladders",
        type=int,
        default=3,
        help="Maximum ENGINE V2 ladder columns shown on pause.",
    )
    p.add_argument(
        "--engine-v2-exec-log",
        default="replay/delta_10s_macro_min10/engine_v2_execution_log.csv",
        help="CSV log for ENGINE V2 simulated execution events.",
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
    out.append(f"{fmt_text(runner.name or str(runner.selection_id), 22)}  center={center_mode}:{center:.2f}  ltp={runner.ltp or '-'}")
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

            my_lay_txt = _c(_fmt_sz(my_lay_val, width=myw), "100;97")
            my_back_txt = _c(_fmt_sz(my_back_val, width=myw), "100;97")
            if show_queue and my_lay_val is not None and my_lay_val > 0:
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
        if max_rows > 0 and (len(out) - 2) >= max_rows:
            # rows beyond header lines are capped
            break
    return out


def print_columns(columns: list[list[str]], *, col_width: int = 42, gap: str = "  ") -> None:
    if not columns:
        return
    height = max(len(c) for c in columns)
    for i in range(height):
        parts: list[str] = []
        for col in columns:
            s = col[i] if i < len(col) else ""
            s = _truncate_visible(s, col_width)
            # Safety: if truncation cuts off ANSI reset, force-reset to avoid background bleed.
            if "\x1b[" in s and not s.endswith("\x1b[0m"):
                s = s + "\x1b[0m"
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

    runners = list(state.runners.values())
    runners.sort(
        key=lambda r: (
            min_total_for_runner(((r.name or str(r.selection_id)).strip())) if min_total_for_runner(((r.name or str(r.selection_id)).strip())) is not None else 999,
            (r.name or str(r.selection_id)),
        )
    )
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
    show_queue: bool = False,
    paused: bool = False,
    err: str | None = None,
    key: str | None = None,
) -> None:
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
            ok, under_lay = is_under_lay_in_range(state, ou_under_lay_min, ou_under_lay_max)
            if not ok:
                continue
            ou_candidates.append((margin, float(under_lay or 0.0), state))

    ou_candidates.sort(key=lambda x: x[0], reverse=True)
    ou_show = [st for _m, _p, st in ou_candidates[: max(0, top_n)]]

    ou_lines: list[float] = []
    for st in ou_show:
        line = over_under_line(st)
        if line is not None:
            ou_lines.append(line)
    wanted_scores = wanted_scorelines_from_ou_lines(ou_lines)

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

    showing = len(match_odds) + len(ou_show) + len(correct_score)
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

    _engine_v2_render_ladders_if_needed(
        pt=int(pt),
        markets=markets,
        order_model=order_model,
        show_queue=show_queue,
        center_mode=center_mode,
        ticks_above=ticks_above,
        ticks_below=ticks_below,
        ladder_max_rows=ladder_max_rows,
        col_width=col_width,
        paused=paused,
    )

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
    if smooth_ui and not no_clear and not _internal_no_diff:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render_dashboard(
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
                show_queue=show_queue,
                _internal_no_diff=True,
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

        # Wrap totals ladders into multiple terminal rows. Respect the user's requested
        # `--totals-rows` as a preference, but never print more columns per row than fit
        # the current terminal width (otherwise the terminal wraps and the UI becomes unreadable).
        rows = max(1, int(totals_rows))
        preferred_per_row = max(1, int(math.ceil(len(cols) / rows)))
        term_cols = int(shutil.get_terminal_size(fallback=(200, 60)).columns)
        gap = "   "
        max_cols_per_row = max(1, (term_cols + len(gap)) // (col_width + len(gap)))
        per_row = min(preferred_per_row, max_cols_per_row)
        for start in range(0, len(cols), per_row):
            print_columns(cols[start : start + per_row], col_width=col_width, gap="   ")
            if start + per_row < len(cols):
                print()

        # Below totals, add Correct Score market(s) in a compact, aligned table.
        # For now, show all scorelines (no filtering) as requested.
        wanted_scores: set[tuple[int, int]] = set()
        cs_markets: list[MarketState] = []
        for mid in sorted(market_ids):
            st = markets.get(mid)
            if st is None or not should_render(st, pt):
                continue
            if is_correct_score(st):
                cs_markets.append(st)

        if cs_markets:
            print("-" * 110)
            for st in sorted(cs_markets, key=lambda s: s.market_id):
                print(f"{st.market_id}  {st.market_type or '-':<12}  {fmt_text(st.market_name, 34)}")
                if self_check:
                    _self_check_under_monotonicity(st)
                ou_lines = [float(over_under_line(x)) for x in ou_show if over_under_line(x) is not None]
                for line in _render_cs_numeric_table(st, under_lines=ou_lines, dutch_signals=bool(cs_dutch_signals)):
                    print(line)
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
        # Match Odds: show all 3 runners (or whatever exists)
        for st in sorted(match_odds, key=lambda s: s.market_id):
            render_market(st, margin=calc_margin_pct_from_best_lay(st.runners))

        # Over/Under: show only those where Under LAY in range, top-N by margin
        for st in ou_show:
            render_market(
                st,
                margin=calc_margin_pct_from_best_lay(st.runners),
                runner_filter=lambda r: (r.name or "").lower().startswith(("under", "over")),
            )

        # Correct Score: show only runners that are covered by any displayed OU market line(s)
        if wanted_scores:
            for st in sorted(correct_score, key=lambda s: s.market_id):
                render_market(
                    st,
                    margin=calc_margin_pct_from_best_lay(st.runners),
                    runner_filter=lambda r: (score_from_runner_name(r.name) in wanted_scores),
                )
        else:
            for st in sorted(correct_score, key=lambda s: s.market_id):
                render_market(st, margin=calc_margin_pct_from_best_lay(st.runners))
    else:
        # Ladder mode: print a compact per-runner ladder for each displayed market.
        for st in sorted(match_odds, key=lambda s: s.market_id):
            print(f"{st.market_id}  {st.market_type or '-':<12}  {fmt_text(st.market_name, 34)}")
            for line in dutching_summary(st):
                print(line)
            runners_sorted = sorted(st.runners.values(), key=lambda r: (r.sort_priority, (r.name or "")))
            # Auto layout: when multiple runners are side-by-side (Match Odds), keep MY columns compact.
            myw = 0 if len(runners_sorted) >= 3 else 6
            cols = [
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
                for runner in runners_sorted
            ]
            print_columns(cols, col_width=col_width)
            print("-" * 110)

        for st in ou_show:
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

        for st in sorted(correct_score, key=lambda s: s.market_id):
            print(f"{st.market_id}  {st.market_type or '-':<12}  {fmt_text(st.market_name, 34)}")
            # "Honest" CS dutching must be computed over the full outcome set, not the displayed subset.
            for line in dutching_summary(st, runner_filter=None, require_coverage=bool(honest_cs)):
                print(line)
            runners_sorted = sorted(st.runners.values(), key=lambda r: (r.sort_priority, (r.name or "")))
            # Many CS columns in a row -> keep MY columns compact.
            myw = 0 if max(1, int(cs_cols)) >= 2 else 6
            cols: list[list[str]] = []
            for runner in runners_sorted:
                if wanted_scores and (score_from_runner_name(runner.name) not in wanted_scores):
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
                # Keep the console readable: limit CS to a few columns; remaining go next row.
                if len(cols) >= max(1, int(cs_cols)):
                    print_columns(cols, col_width=col_width)
                    print()
                    cols = []
            if cols:
                print_columns(cols, col_width=col_width)
            print("-" * 110)

    sys.stdout.flush()


def _engine_v2_log_event(
    *,
    event: str,
    pt: int,
    row: dict[str, object],
    liability: float = 0.0,
    free_balance: float = 0.0,
    note: str = "",
) -> None:
    import csv
    from pathlib import Path
    from datetime import datetime, timezone

    path = str(globals().get("ENGINE_V2_EXEC_LOG_PATH") or "")
    if not path:
        return

    event_key = (
        event,
        str(row.get("signal_id") or ""),
        str(row.get("market_type") or ""),
        str(row.get("market_name") or ""),
        str(row.get("runner_name") or ""),
        str(row.get("entry_order_side") or ""),
        str(row.get("price") or ""),
        str(row.get("first_add_utc") or ""),
        str(row.get("fill_utc") or ""),
        str(row.get("exit_utc") or ""),
    )

    logged = globals().setdefault("ENGINE_V2_LOGGED_EVENTS", set())
    if event_key in logged:
        return
    logged.add(event_key)

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    write_header = not p.exists() or p.stat().st_size == 0

    with p.open("a", newline="", encoding="utf-8") as f:
        fields = [
            "event",
            "pt",
            "utc",
            "signal_id",
            "market_type",
            "market_name",
            "runner_name",
            "entry_order_side",
            "book_side",
            "price",
            "stake",
            "liability",
            "free_balance",
            "fill_utc",
            "exit_utc",
            "pnl_proxy",
            "pnl_status",
            "note",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()

        utc = datetime.fromtimestamp(pt / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        w.writerow({
            "event": event,
            "pt": pt,
            "utc": utc,
            "signal_id": row.get("signal_id", ""),
            "market_type": row.get("market_type", ""),
            "market_name": row.get("market_name", ""),
            "runner_name": row.get("runner_name", ""),
            "entry_order_side": row.get("entry_order_side", ""),
            "book_side": row.get("side", ""),
            "price": row.get("price", ""),
            "stake": row.get("stake", ""),
            "liability": f"{float(liability):.6f}",
            "free_balance": f"{float(free_balance):.6f}",
            "fill_utc": row.get("fill_utc", ""),
            "exit_utc": row.get("exit_utc", ""),
            "pnl_proxy": row.get("pnl_proxy", ""),
            "pnl_status": row.get("pnl_status", ""),
            "note": note,
        })

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


def _engine_v2_render_ladders_if_needed(
    *,
    pt: int,
    markets: dict[str, MarketState],
    order_model: OrderModel,
    show_queue: bool,
    center_mode: str,
    ticks_above: int,
    ticks_below: int,
    ladder_max_rows: int,
    col_width: int,
    paused: bool = False,
) -> None:
    if not bool(globals().get("ENGINE_V2_SHOW_MARKETS", False)):
        return

    # Do not redraw variable-size V2 ladder block while stream is running.
    # This prevents dashboard jumping. Press PAUSE to inspect V2 ladders.
    if not paused:
        return

    orders = globals().get("ENGINE_V2_RUNTIME_ORDERS", [])
    if not orders:
        return

    allowed = {
        "MATCH_ODDS",
        "OVER_UNDER_15",
        "OVER_UNDER_25",
        "OVER_UNDER_35",
        "OVER_UNDER_45",
        "TOTAL_GOALS",
        "TEAM_TOTAL_GOALS",
        "FIRST_HALF_GOALS_05",
    }

    cols: list[list[str]] = []
    seen: set[tuple[str, str, str]] = set()

    for r in orders:
        mt = str(r.get("market_type") or "")
        if mt not in allowed:
            continue

        entry = int(r.get("_entry_ms") or 0)
        fill = int(r.get("_fill_ms") or 0)
        exit_ms = int(r.get("_exit_ms") or 0)

        if entry <= 0:
            continue

        # Show shortly before entry, while active, and shortly after exit.
        end_ms = exit_ms if exit_ms else (fill if fill else entry + 60000)
        if not (entry - 10000 <= pt <= end_ms + 5000):
            continue

        st, runner = _engine_v2_find_runner(markets=markets, row=r)
        if st is None or runner is None:
            continue

        price = float(r.get("_price") or r.get("price") or 0.0)
        if price <= 1.0:
            continue

        key = (
            str(r.get("market_type") or ""),
            str(r.get("market_name") or ""),
            str(r.get("runner_name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)

        if pt < entry:
            status = "NEXT"
        elif fill and pt >= fill and (not exit_ms or pt < exit_ms):
            status = "FILLED"
        elif exit_ms and pt >= exit_ms:
            status = "EXIT"
        else:
            status = "LIVE"

        side = str(r.get("entry_order_side") or "")
        stake = float(r.get("_stake") or r.get("stake") or 0.0)
        if side == "LAY":
            liab = stake * max(0.0, price - 1.0)
        elif side == "BACK":
            liab = stake
        else:
            liab = 0.0

        lo = max(1.01, price - 0.25)
        hi = price + 0.25

        title = (
            f"ENGINE_V2 {status} S{r.get('signal_id')} "
            f"{mt} {side}@{price:g} stake={stake:g} liab={liab:.2f}"
        )

        ladder_lines = render_runner_ladder(
            runner,
            market_id=st.market_id,
            center_mode=center_mode,
            ticks_above=ticks_above,
            ticks_below=ticks_below,
            nonempty_only=False,
            max_rows=int(ladder_max_rows or 12),
            my_col_width=5,
            order_model=order_model,
            show_queue=show_queue,
            price_low=lo,
            price_high=hi,
        )

        cols.append([title] + ladder_lines)

        if len(cols) >= int(globals().get("ENGINE_V2_MAX_LADDERS", 3)):
            break

    if not cols:
        return

    print()
    print("ENGINE V2 LADDERS")
    print_columns(cols, col_width=max(42, int(col_width)), gap="  ")
    print("-" * 110)

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
        if bool(r.get("_engine_v2_settled", False)):
            continue

        entry_ms = int(r.get("_entry_ms") or 0)
        fill_ms = int(r.get("_fill_ms") or 0)
        exit_ms = int(r.get("_exit_ms") or 0)

        if entry_ms <= 0:
            continue

        # 1) settle after exit
        if (
            exit_ms
            and pt >= exit_ms
            and bool(r.get("_engine_v2_placed", False))
            and (not fill_ms or bool(r.get("_engine_v2_filled", False)))
        ):
            key = r.get("_engine_v2_order_key")
            if key in order_model.by_key:
                my = order_model.by_key[key]
                if my.my_lay > 0:
                    my.matched += my.my_lay
                    my.my_lay = 0.0
                if my.my_back > 0:
                    my.matched += my.my_back
                    my.my_back = 0.0

            _engine_v2_log_event(
                event="EXIT",
                pt=pt,
                row=r,
                liability=float(r.get("_engine_v2_liability") or 0.0),
                free_balance=_engine_v2_current_locked_and_pnl(orders=orders, balance=balance)[2],
                note="exit_proxy",
            )
            r["_engine_v2_settled"] = True
            continue

        # 2) fill after fill_utc
        if fill_ms and pt >= fill_ms and bool(r.get("_engine_v2_placed", False)) and not bool(r.get("_engine_v2_filled", False)):
            key = r.get("_engine_v2_order_key")
            if key in order_model.by_key:
                my = order_model.by_key[key]
                if my.my_lay > 0:
                    my.matched += my.my_lay
                    my.my_lay = 0.0
                if my.my_back > 0:
                    my.matched += my.my_back
                    my.my_back = 0.0
            _engine_v2_log_event(
                event="FILL",
                pt=pt,
                row=r,
                liability=float(r.get("_engine_v2_liability") or 0.0),
                free_balance=_engine_v2_current_locked_and_pnl(orders=orders, balance=balance)[2],
                note="fill_proxy",
            )
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
            _engine_v2_log_event(
                event="SKIP",
                pt=pt,
                row=r,
                liability=0.0,
                free_balance=_engine_v2_current_locked_and_pnl(orders=orders, balance=balance)[2],
                note="MARKET_OR_RUNNER_NOT_FOUND",
            )
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
            _engine_v2_log_event(
                event="SKIP",
                pt=pt,
                row=r,
                liability=liability,
                free_balance=free,
                note="NO_FREE_BALANCE",
            )
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

        _engine_v2_log_event(
            event="PLACE",
            pt=pt,
            row=r,
            liability=liability,
            free_balance=free,
            note="placed",
        )

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
ENGINE_V2_RUNTIME_ORDERS = []
ENGINE_V2_SHOW_MARKETS = False
ENGINE_V2_MAX_LADDERS = 3
ENGINE_V2_EXEC_LOG_PATH = ""
ENGINE_V2_LOGGED_EVENTS = set()

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

    globals()["ENGINE_V2_RUNTIME_ORDERS"] = engine_v2_orders
    globals()["ENGINE_V2_SHOW_MARKETS"] = bool(getattr(args, "engine_v2_show_markets", False))
    globals()["ENGINE_V2_MAX_LADDERS"] = max(1, int(getattr(args, "engine_v2_max_ladders", 3)))
    globals()["ENGINE_V2_EXEC_LOG_PATH"] = str(getattr(args, "engine_v2_exec_log", ""))
    globals()["ENGINE_V2_MAX_LADDERS"] = max(1, int(getattr(args, "engine_v2_max_ladders", 3)))

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
                        _engine_v2_apply_orders_to_order_model(
                            pt=int(next_frame_pt),
                            markets=markets,
                            order_model=order_model,
                            orders=engine_v2_orders,
                            balance=balance,
                        )

                    update_order_model_from_current_ladder(
                        markets=markets,
                        order_model=order_model,
                    )

                    if bool(getattr(args, "engine_v2_overlay", False)):
                        globals()["ENGINE_V2_OVERLAY_LINE"] = _engine_v2_overlay_line(
                            pt=int(next_frame_pt),
                            balance=balance,
                            orders=engine_v2_orders,
                        )
                        globals()["ENGINE_V2_TAPE_LINE"] = _engine_v2_tape_line(
                            pt=int(next_frame_pt),
                            orders=engine_v2_orders,
                        )
                    else:
                        globals()["ENGINE_V2_OVERLAY_LINE"] = ""
                        globals()["ENGINE_V2_TAPE_LINE"] = ""

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
                                interactive_err = None
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
                            except Exception as exc:
                                interactive_err = f"{type(exc).__name__}: {exc}"
                                # Don't crash the replay; keep paused so the user can continue.
                                return

                        def _read_key_now() -> str | None:
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
                            if k in ("n", "N", "т", "Т"):
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
                            if k in ("n", "N", "т", "Т"):
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
    return stream_replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
