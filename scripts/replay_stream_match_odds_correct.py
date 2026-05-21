#!/usr/bin/env python3
import argparse
import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_REPLAY_FILE = Path("replay/football-pro-sample")
DEFAULT_MARKET_ID = "1.131162806"
DEFAULT_DELAY_SECONDS = 0.05
DEFAULT_START_HOURS_BEFORE = 2.0
FEATURE_DEPTHS = (1, 3, 5, 10)
MAX_FEATURE_DEPTH = max(FEATURE_DEPTHS)
DERIVATIVE_SUFFIXES = ("_delta", "_velocity", "_acceleration")
DERIVATIVE_EXCLUDE_FIELDS = {
    "tick",
    "pt",
    "pt_utc",
    "market_id",
    "market_name",
    "market_status",
    "in_play",
    "market_time",
    "selection_id",
    "handicap",
    "runner_name",
    "runner_status",
    "sort_priority",
    "seconds_to_start",
    "minutes_to_start",
    "dt_seconds",
    "rank_by_best_back",
    "is_favourite",
    "favourite_selection_id",
    "favourite_runner_name",
}


@dataclass
class RunnerState:
    selection_id: int
    handicap: float | None = None
    name: str | None = None
    sort_priority: int = 999999
    status: str | None = None
    available_to_back: dict[float, float] = field(default_factory=dict)
    available_to_lay: dict[float, float] = field(default_factory=dict)
    traded: dict[float, float] = field(default_factory=dict)
    ltp: float | None = None
    spn: float | None = None
    spf: Any = None

    @property
    def traded_volume(self) -> float | None:
        if not self.traded:
            return None
        return sum(self.traded.values())


@dataclass
class FeatureContext:
    previous: dict[tuple[int, float | None], dict[str, Any]] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a Betfair historical replay market with a correct order-book state."
    )
    parser.add_argument(
        "--replay-file",
        type=Path,
        default=DEFAULT_REPLAY_FILE,
        help=f"Historical stream file. Default: {DEFAULT_REPLAY_FILE}",
    )
    parser.add_argument(
        "--market-id",
        default=DEFAULT_MARKET_ID,
        help=f"Market id to stream. Default: {DEFAULT_MARKET_ID}",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay between rendered market updates. Default: {DEFAULT_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after this many matching market updates.",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=3,
        help="Number of back/lay price levels to show per runner. Default: 3",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Print frames one after another instead of repainting the terminal.",
    )
    parser.add_argument(
        "--start-hours-before",
        type=float,
        default=DEFAULT_START_HOURS_BEFORE,
        help=(
            "Start rendering this many hours before marketTime, while still warming "
            f"the book from earlier data. Default: {DEFAULT_START_HOURS_BEFORE}"
        ),
    )
    parser.add_argument(
        "--features-csv",
        type=Path,
        default=None,
        help="Write all computed trading indices to this CSV file.",
    )
    parser.add_argument(
        "--show-features",
        action="store_true",
        help="Show compact trading indices in the live stream table.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not render frames; useful when generating a feature CSV.",
    )
    return parser.parse_args()


def runner_key(data: dict[str, Any]) -> tuple[int, float | None]:
    return data.get("id"), data.get("hc")


def valid_price(price: Any) -> bool:
    return isinstance(price, (int, float)) and 1.01 <= price <= 1000


def valid_size(size: Any) -> bool:
    return isinstance(size, (int, float)) and size >= 0


def apply_price_levels(book: dict[float, float], levels: Any) -> None:
    if not isinstance(levels, list):
        return

    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            continue

        price, size = level[0], level[1]
        if not valid_price(price) or not valid_size(size):
            continue

        price = float(price)
        size = float(size)
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size


def best_back_levels(book: dict[float, float], depth: int) -> list[tuple[float, float]]:
    return sorted(book.items(), reverse=True)[:depth]


def best_lay_levels(book: dict[float, float], depth: int) -> list[tuple[float, float]]:
    return sorted(book.items())[:depth]


def total_size(levels: list[tuple[float, float]]) -> float:
    return sum(size for _, size in levels)


def total_notional(levels: list[tuple[float, float]]) -> float:
    return sum(price * size for price, size in levels)


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def normalized_diff(left: float, right: float) -> float | None:
    total = left + right
    if total == 0:
        return None
    return (left - right) / total


def betfair_tick_size(price: float) -> float:
    if price < 2:
        return 0.01
    if price < 3:
        return 0.02
    if price < 4:
        return 0.05
    if price < 6:
        return 0.1
    if price < 10:
        return 0.2
    if price < 20:
        return 0.5
    if price < 30:
        return 1
    if price < 50:
        return 2
    if price < 100:
        return 5
    return 10


def betfair_ticks_between(lower: float | None, upper: float | None) -> int | None:
    if lower is None or upper is None or upper < lower:
        return None

    ticks = 0
    price = lower
    while price < upper and ticks < 10000:
        price = round(price + betfair_tick_size(price), 10)
        ticks += 1
    return ticks


def value_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def value_velocity(delta: float | None, seconds: float | None) -> float | None:
    return safe_div(delta, seconds)


def is_numeric_feature(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def should_add_derivatives(name: str, value: Any) -> bool:
    if name in DERIVATIVE_EXCLUDE_FIELDS:
        return False
    if name.endswith(DERIVATIVE_SUFFIXES):
        return False
    return is_numeric_feature(value) or value is None


def add_derivative_features(
    row: dict[str, Any],
    previous: dict[str, Any],
    dt_seconds: float | None,
) -> None:
    base_names = [
        name
        for name, value in row.items()
        if should_add_derivatives(name, value)
    ]
    for name in base_names:
        delta = value_delta(row.get(name), previous.get(name))
        velocity = value_velocity(delta, dt_seconds)
        previous_velocity = previous.get(f"{name}_velocity")
        acceleration = value_velocity(value_delta(velocity, previous_velocity), dt_seconds)
        row[f"{name}_delta"] = delta
        row[f"{name}_velocity"] = velocity
        row[f"{name}_acceleration"] = acceleration


def add_ladder_level_features(
    row: dict[str, Any],
    backs: dict[int, list[tuple[float, float]]],
    lays: dict[int, list[tuple[float, float]]],
) -> None:
    deepest_backs = backs[MAX_FEATURE_DEPTH]
    deepest_lays = lays[MAX_FEATURE_DEPTH]
    for index in range(MAX_FEATURE_DEPTH):
        level = index + 1
        back_price, back_size = (
            deepest_backs[index] if index < len(deepest_backs) else (None, None)
        )
        lay_price, lay_size = (
            deepest_lays[index] if index < len(deepest_lays) else (None, None)
        )
        row[f"back_price_{level}"] = back_price
        row[f"back_size_{level}"] = back_size
        row[f"back_notional_level_{level}"] = (
            back_price * back_size
            if back_price is not None and back_size is not None
            else None
        )
        row[f"lay_price_{level}"] = lay_price
        row[f"lay_size_{level}"] = lay_size
        row[f"lay_notional_level_{level}"] = (
            lay_price * lay_size
            if lay_price is not None and lay_size is not None
            else None
        )
        row[f"level_{level}_spread"] = value_delta(lay_price, back_price)
        row[f"level_{level}_spread_ticks"] = betfair_ticks_between(back_price, lay_price)
        if back_price is not None and lay_price is not None:
            row[f"level_{level}_mid_price"] = (back_price + lay_price) / 2
        else:
            row[f"level_{level}_mid_price"] = None
        row[f"level_{level}_queue_imbalance"] = (
            normalized_diff(back_size, lay_size)
            if back_size is not None and lay_size is not None
            else None
        )
        row[f"level_{level}_queue_pressure_ratio"] = safe_div(back_size, lay_size)


def weighted_average_price(levels: list[tuple[float, float]]) -> float | None:
    size = total_size(levels)
    if size == 0:
        return None
    return total_notional(levels) / size


def compute_runner_features(
    key: tuple[int, float | None],
    runner: RunnerState,
    tick: int,
    pt: Any,
    market_id: str,
    market_name: str | None,
    market_status: str | None,
    in_play: bool | None,
    market_time: datetime | None,
    context: FeatureContext,
) -> dict[str, Any]:
    backs = {depth: best_back_levels(runner.available_to_back, depth) for depth in FEATURE_DEPTHS}
    lays = {depth: best_lay_levels(runner.available_to_lay, depth) for depth in FEATURE_DEPTHS}

    best_back = backs[1][0][0] if backs[1] else None
    best_back_size = backs[1][0][1] if backs[1] else None
    best_lay = lays[1][0][0] if lays[1] else None
    best_lay_size = lays[1][0][1] if lays[1] else None
    spread = value_delta(best_lay, best_back)
    spread_ticks = betfair_ticks_between(best_back, best_lay)
    mid_price = (best_back + best_lay) / 2 if best_back is not None and best_lay is not None else None

    top_size_sum = (
        best_back_size + best_lay_size
        if best_back_size is not None and best_lay_size is not None
        else None
    )
    microprice = None
    if top_size_sum:
        microprice = (best_lay * best_back_size + best_back * best_lay_size) / top_size_sum

    previous = context.previous.get(key, {})
    previous_pt = previous.get("pt")
    dt_seconds = None
    if isinstance(pt, (int, float)) and isinstance(previous_pt, (int, float)):
        dt_seconds = (pt - previous_pt) / 1000
        if dt_seconds <= 0:
            dt_seconds = None

    traded_volume = runner.traded_volume
    seconds_to_start = None
    if market_time is not None and isinstance(pt, (int, float)):
        seconds_to_start = (datetime_to_pt(market_time) - pt) / 1000

    row: dict[str, Any] = {
        "tick": tick,
        "pt": pt,
        "pt_utc": format_pt(pt),
        "market_id": market_id,
        "market_name": market_name,
        "market_status": market_status,
        "in_play": in_play,
        "market_time": market_time.isoformat() if market_time else None,
        "seconds_to_start": seconds_to_start,
        "minutes_to_start": safe_div(seconds_to_start, 60),
        "selection_id": runner.selection_id,
        "handicap": runner.handicap,
        "runner_name": runner.name,
        "runner_status": runner.status,
        "sort_priority": runner.sort_priority,
        "best_back": best_back,
        "best_back_size": best_back_size,
        "best_lay": best_lay,
        "best_lay_size": best_lay_size,
        "spread": spread,
        "spread_ticks": spread_ticks,
        "mid_price": mid_price,
        "microprice": microprice,
        "ltp": runner.ltp,
        "traded_volume": traded_volume,
        "dt_seconds": dt_seconds,
    }

    add_ladder_level_features(row, backs, lays)

    for depth in FEATURE_DEPTHS:
        back_size = total_size(backs[depth])
        lay_size = total_size(lays[depth])
        back_notional = total_notional(backs[depth])
        lay_notional = total_notional(lays[depth])
        row[f"back_depth_{depth}"] = back_size
        row[f"lay_depth_{depth}"] = lay_size
        row[f"back_notional_{depth}"] = back_notional
        row[f"lay_notional_{depth}"] = lay_notional
        row[f"queue_imbalance_{depth}"] = normalized_diff(back_size, lay_size)
        row[f"queue_pressure_ratio_{depth}"] = safe_div(back_size, lay_size)
        row[f"notional_imbalance_{depth}"] = normalized_diff(back_notional, lay_notional)
        row[f"back_weighted_avg_price_{depth}"] = weighted_average_price(backs[depth])
        row[f"lay_weighted_avg_price_{depth}"] = weighted_average_price(lays[depth])
        row[f"depth_ratio_{depth}_to_1_back"] = safe_div(back_size, row.get("back_depth_1"))
        row[f"depth_ratio_{depth}_to_1_lay"] = safe_div(lay_size, row.get("lay_depth_1"))
        row[f"notional_ratio_{depth}_to_1_back"] = safe_div(
            back_notional, row.get("back_notional_1")
        )
        row[f"notional_ratio_{depth}_to_1_lay"] = safe_div(
            lay_notional, row.get("lay_notional_1")
        )

    all_back_size = sum(runner.available_to_back.values())
    all_lay_size = sum(runner.available_to_lay.values())
    all_back_notional = sum(
        price * size for price, size in runner.available_to_back.items()
    )
    all_lay_notional = sum(price * size for price, size in runner.available_to_lay.items())
    row["back_depth_all"] = all_back_size
    row["lay_depth_all"] = all_lay_size
    row["back_notional_all"] = all_back_notional
    row["lay_notional_all"] = all_lay_notional
    row["queue_imbalance_all"] = normalized_diff(all_back_size, all_lay_size)
    row["queue_pressure_ratio_all"] = safe_div(all_back_size, all_lay_size)
    row["notional_imbalance_all"] = normalized_diff(all_back_notional, all_lay_notional)
    row["book_levels_back"] = len(runner.available_to_back)
    row["book_levels_lay"] = len(runner.available_to_lay)
    row["book_level_imbalance"] = normalized_diff(
        row["book_levels_back"], row["book_levels_lay"]
    )
    row["back_weighted_avg_price_all"] = safe_div(all_back_notional, all_back_size)
    row["lay_weighted_avg_price_all"] = safe_div(all_lay_notional, all_lay_size)
    row["depth_ratio_all_to_1_back"] = safe_div(all_back_size, row.get("back_depth_1"))
    row["depth_ratio_all_to_1_lay"] = safe_div(all_lay_size, row.get("lay_depth_1"))
    row["notional_ratio_all_to_1_back"] = safe_div(
        all_back_notional, row.get("back_notional_1")
    )
    row["notional_ratio_all_to_1_lay"] = safe_div(
        all_lay_notional, row.get("lay_notional_1")
    )
    row["ltp_minus_best_back"] = value_delta(runner.ltp, best_back)
    row["best_lay_minus_ltp"] = value_delta(best_lay, runner.ltp)
    row["ltp_minus_mid_price"] = value_delta(runner.ltp, mid_price)
    row["ltp_minus_microprice"] = value_delta(runner.ltp, microprice)
    row["mid_minus_microprice"] = value_delta(mid_price, microprice)
    row["ltp_to_best_back_ticks"] = betfair_ticks_between(best_back, runner.ltp)
    row["ltp_to_best_lay_ticks"] = betfair_ticks_between(runner.ltp, best_lay)
    row["spread_pct_mid"] = safe_div(spread, mid_price)
    row["microprice_minus_mid_pct"] = safe_div(row["mid_minus_microprice"], mid_price)
    return row


def compute_features(
    runners: dict[tuple[int, float | None], RunnerState],
    tick: int,
    pt: Any,
    market_id: str,
    market_name: str | None,
    market_status: str | None,
    in_play: bool | None,
    market_time: datetime | None,
    context: FeatureContext,
) -> list[dict[str, Any]]:
    rows = [
        compute_runner_features(
            key,
            runner,
            tick,
            pt,
            market_id,
            market_name,
            market_status,
            in_play,
            market_time,
            context,
        )
        for key, runner in sorted(
            runners.items(),
            key=lambda item: (
                item[1].sort_priority,
                item[1].name or "",
                item[1].selection_id,
                item[1].handicap or 0,
            ),
        )
    ]
    enrich_market_features(rows)
    for row in rows:
        key = (row["selection_id"], row["handicap"])
        previous = context.previous.get(key, {})
        add_derivative_features(row, previous, row.get("dt_seconds"))
        context.previous[key] = row
    return rows


def enrich_market_features(rows: list[dict[str, Any]]) -> None:
    back_probs = [
        1 / row["best_back"] for row in rows if isinstance(row.get("best_back"), float)
    ]
    lay_probs = [
        1 / row["best_lay"] for row in rows if isinstance(row.get("best_lay"), float)
    ]
    mid_probs = [
        1 / row["mid_price"] for row in rows if isinstance(row.get("mid_price"), float)
    ]

    market_back_book_pct = sum(back_probs) if back_probs else None
    market_lay_book_pct = sum(lay_probs) if lay_probs else None
    market_mid_book_pct = sum(mid_probs) if mid_probs else None
    market_book_spread_pct = value_delta(market_back_book_pct, market_lay_book_pct)

    market_totals: dict[str, float] = {}
    for depth in FEATURE_DEPTHS:
        market_totals[f"total_back_depth_{depth}"] = sum(
            row.get(f"back_depth_{depth}") or 0 for row in rows
        )
        market_totals[f"total_lay_depth_{depth}"] = sum(
            row.get(f"lay_depth_{depth}") or 0 for row in rows
        )
        market_totals[f"total_back_notional_{depth}"] = sum(
            row.get(f"back_notional_{depth}") or 0 for row in rows
        )
        market_totals[f"total_lay_notional_{depth}"] = sum(
            row.get(f"lay_notional_{depth}") or 0 for row in rows
        )
    market_totals["total_back_depth_all"] = sum(
        row.get("back_depth_all") or 0 for row in rows
    )
    market_totals["total_lay_depth_all"] = sum(
        row.get("lay_depth_all") or 0 for row in rows
    )
    market_totals["total_back_notional_all"] = sum(
        row.get("back_notional_all") or 0 for row in rows
    )
    market_totals["total_lay_notional_all"] = sum(
        row.get("lay_notional_all") or 0 for row in rows
    )

    ranked = sorted(
        [row for row in rows if isinstance(row.get("best_back"), float)],
        key=lambda row: row["best_back"],
    )
    rank_by_selection = {
        (row["selection_id"], row["handicap"]): index + 1
        for index, row in enumerate(ranked)
    }
    favourite = ranked[0] if ranked else None
    favourite_key = (
        (favourite["selection_id"], favourite["handicap"]) if favourite else None
    )

    for row in rows:
        back_prob = safe_div(1, row.get("best_back"))
        lay_prob = safe_div(1, row.get("best_lay"))
        mid_prob = safe_div(1, row.get("mid_price"))
        key = (row["selection_id"], row["handicap"])

        row["back_implied_probability"] = back_prob
        row["lay_implied_probability"] = lay_prob
        row["mid_implied_probability"] = mid_prob
        row["market_back_book_pct"] = market_back_book_pct
        row["market_lay_book_pct"] = market_lay_book_pct
        row["market_mid_book_pct"] = market_mid_book_pct
        row["market_book_spread_pct"] = market_book_spread_pct
        for depth in FEATURE_DEPTHS:
            total_back_depth = market_totals[f"total_back_depth_{depth}"]
            total_lay_depth = market_totals[f"total_lay_depth_{depth}"]
            total_back_notional = market_totals[f"total_back_notional_{depth}"]
            total_lay_notional = market_totals[f"total_lay_notional_{depth}"]
            row[f"market_back_depth_{depth}"] = total_back_depth
            row[f"market_lay_depth_{depth}"] = total_lay_depth
            row[f"market_back_notional_{depth}"] = total_back_notional
            row[f"market_lay_notional_{depth}"] = total_lay_notional
            row[f"market_queue_imbalance_{depth}"] = normalized_diff(
                total_back_depth, total_lay_depth
            )
            row[f"market_notional_imbalance_{depth}"] = normalized_diff(
                total_back_notional, total_lay_notional
            )
            row[f"market_queue_pressure_ratio_{depth}"] = safe_div(
                total_back_depth, total_lay_depth
            )
            row[f"market_back_depth_share_{depth}"] = safe_div(
                row.get(f"back_depth_{depth}"), total_back_depth
            )
            row[f"market_lay_depth_share_{depth}"] = safe_div(
                row.get(f"lay_depth_{depth}"), total_lay_depth
            )
            row[f"market_back_notional_share_{depth}"] = safe_div(
                row.get(f"back_notional_{depth}"), total_back_notional
            )
            row[f"market_lay_notional_share_{depth}"] = safe_div(
                row.get(f"lay_notional_{depth}"), total_lay_notional
            )
        total_back_depth_all = market_totals["total_back_depth_all"]
        total_lay_depth_all = market_totals["total_lay_depth_all"]
        total_back_notional_all = market_totals["total_back_notional_all"]
        total_lay_notional_all = market_totals["total_lay_notional_all"]
        row["market_back_depth_all"] = total_back_depth_all
        row["market_lay_depth_all"] = total_lay_depth_all
        row["market_back_notional_all"] = total_back_notional_all
        row["market_lay_notional_all"] = total_lay_notional_all
        row["market_queue_imbalance_all"] = normalized_diff(
            total_back_depth_all, total_lay_depth_all
        )
        row["market_notional_imbalance_all"] = normalized_diff(
            total_back_notional_all, total_lay_notional_all
        )
        row["market_queue_pressure_ratio_all"] = safe_div(
            total_back_depth_all, total_lay_depth_all
        )
        row["market_back_depth_share_all"] = safe_div(
            row.get("back_depth_all"), total_back_depth_all
        )
        row["market_lay_depth_share_all"] = safe_div(
            row.get("lay_depth_all"), total_lay_depth_all
        )
        row["market_back_notional_share_all"] = safe_div(
            row.get("back_notional_all"), total_back_notional_all
        )
        row["market_lay_notional_share_all"] = safe_div(
            row.get("lay_notional_all"), total_lay_notional_all
        )
        row["rank_by_best_back"] = rank_by_selection.get(key)
        row["is_favourite"] = key == favourite_key
        row["favourite_selection_id"] = favourite["selection_id"] if favourite else None
        row["favourite_runner_name"] = favourite["runner_name"] if favourite else None
        row["probability_share_mid"] = safe_div(mid_prob, market_mid_book_pct)


def format_price_size(levels: list[tuple[float, float]]) -> str:
    if not levels:
        return "-"
    return " | ".join(f"{price:g}@{format_amount(size)}" for price, size in levels)


def format_amount(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_num(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return format_amount(value)
    return str(value)


def fit(value: str, width: int) -> str:
    if len(value) > width:
        return value[: max(width - 1, 0)] + " "
    return f"{value:<{width}}"


def format_pt(pt: Any) -> str:
    if not isinstance(pt, (int, float)):
        return "-"
    dt = datetime.fromtimestamp(pt / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"


def parse_market_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def datetime_to_pt(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def clear_screen() -> None:
    print("\033[2J\033[H", end="")


def ensure_runner(
    runners: dict[tuple[int, float | None], RunnerState],
    key: tuple[int, float | None],
) -> RunnerState:
    if key not in runners:
        runners[key] = RunnerState(selection_id=key[0], handicap=key[1])
    return runners[key]


def apply_market_definition(
    runners: dict[tuple[int, float | None], RunnerState],
    market_definition: dict[str, Any],
) -> None:
    for runner in market_definition.get("runners", []):
        if not isinstance(runner, dict) or runner.get("id") is None:
            continue

        key = runner_key(runner)
        state = ensure_runner(runners, key)
        state.name = runner.get("name", state.name)
        state.status = runner.get("status", state.status)
        state.sort_priority = runner.get("sortPriority", state.sort_priority)


def apply_runner_change(
    runners: dict[tuple[int, float | None], RunnerState],
    change: dict[str, Any],
) -> None:
    if change.get("id") is None:
        return

    state = ensure_runner(runners, runner_key(change))

    apply_price_levels(state.available_to_back, change.get("atb"))
    apply_price_levels(state.available_to_lay, change.get("atl"))
    apply_price_levels(state.traded, change.get("trd"))

    if valid_price(change.get("ltp")):
        state.ltp = float(change["ltp"])
    if valid_price(change.get("spn")):
        state.spn = float(change["spn"])
    if "spf" in change:
        state.spf = change["spf"]


def render(
    replay_file: Path,
    market_id: str,
    market_name: str | None,
    market_status: str | None,
    in_play: bool | None,
    market_time: datetime | None,
    stream_start_time: datetime | None,
    pt: Any,
    tick: int,
    runners: dict[tuple[int, float | None], RunnerState],
    feature_rows: list[dict[str, Any]],
    depth: int,
    show_features: bool,
) -> None:
    runner_width = 24
    ladder_width = 46
    print(f"FILE      : {replay_file}")
    print(f"MARKET_ID : {market_id}")
    print(f"MARKET    : {market_name or '-'}")
    print(f"STATUS    : {market_status or '-'}")
    print(f"IN_PLAY   : {format_num(in_play)}")
    print(f"MATCH     : {market_time.isoformat() if market_time else '-'}")
    print(f"STREAM_AT : {stream_start_time.isoformat() if stream_start_time else '-'}")
    print(f"PT        : {pt} ({format_pt(pt)})")
    print(f"TICK      : {tick}")
    print()

    if show_features:
        print(
            f"{'RUNNER':<{runner_width}}"
            f"{'BACK':<{ladder_width}}"
            f"{'LAY':<{ladder_width}}"
            f"{'LTP':>8}"
            f"{'SPRD':>6}"
            f"{'QI3':>8}"
            f"{'MID_V':>10}"
            f"{'LTP_A':>10}"
            f"{'TRD_V':>10}"
            f"{'STATUS':>10}"
        )
        print("-" * (runner_width + ladder_width + ladder_width + 72))
    else:
        print(
            f"{'RUNNER':<{runner_width}}"
            f"{'BACK':<{ladder_width}}"
            f"{'LAY':<{ladder_width}}"
            f"{'LTP':>8}"
            f"{'TRADED':>12}"
            f"{'STATUS':>10}"
        )
        print("-" * (runner_width + ladder_width + ladder_width + 30))

    features_by_key = {
        (row["selection_id"], row["handicap"]): row for row in feature_rows
    }
    ordered = sorted(
        runners.values(),
        key=lambda r: (r.sort_priority, r.name or "", r.selection_id, r.handicap or 0),
    )
    for runner in ordered:
        label = runner.name or str(runner.selection_id)
        if runner.handicap is not None:
            label = f"{label} ({runner.handicap:g})"
        backs = format_price_size(best_back_levels(runner.available_to_back, depth))
        lays = format_price_size(best_lay_levels(runner.available_to_lay, depth))
        row = features_by_key.get((runner.selection_id, runner.handicap), {})
        if show_features:
            print(
                f"{fit(label, runner_width)}"
                f"{fit(backs, ladder_width)}"
                f"{fit(lays, ladder_width)}"
                f"{format_num(runner.ltp):>8}"
                f"{format_num(row.get('spread_ticks')):>6}"
                f"{format_num(row.get('queue_imbalance_3')):>8}"
                f"{format_num(row.get('mid_price_velocity')):>10}"
                f"{format_num(row.get('ltp_acceleration')):>10}"
                f"{format_num(row.get('traded_volume_velocity')):>10}"
                f"{format_num(runner.status):>10}"
            )
        else:
            print(
                f"{fit(label, runner_width)}"
                f"{fit(backs, ladder_width)}"
                f"{fit(lays, ladder_width)}"
                f"{format_num(runner.ltp):>8}"
                f"{format_num(runner.traded_volume):>12}"
                f"{format_num(runner.status):>10}"
            )


def stream_replay(args: argparse.Namespace) -> int:
    if not args.replay_file.exists():
        print(f"File not found: {args.replay_file}")
        return 1

    runners: dict[tuple[int, float | None], RunnerState] = {}
    feature_context = FeatureContext()
    market_name = None
    market_status = None
    in_play = None
    market_time = None
    stream_start_time = None
    stream_start_pt = None
    tick = 0

    if not args.quiet and not args.no_clear:
        clear_screen()

    csv_file = None
    csv_writer = None
    if args.features_csv is not None:
        args.features_csv.parent.mkdir(parents=True, exist_ok=True)
        csv_file = args.features_csv.open("w", encoding="utf-8", newline="")

    try:
        replay = args.replay_file.open("r", encoding="utf-8")
    except OSError as exc:
        if csv_file is not None:
            csv_file.close()
        print(f"Could not open replay file: {exc}")
        return 1

    with replay:
        for line_number, line in enumerate(replay, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"Skipping invalid JSON at line {line_number}: {exc}")
                continue

            pt = message.get("pt")
            for market_change in message.get("mc", []):
                if str(market_change.get("id")) != args.market_id:
                    continue

                if market_change.get("img") is True:
                    runners = {}

                market_definition = market_change.get("marketDefinition")
                if isinstance(market_definition, dict):
                    market_name = market_definition.get("name", market_name)
                    market_status = market_definition.get("status", market_status)
                    in_play = market_definition.get("inPlay", in_play)
                    parsed_market_time = parse_market_time(
                        market_definition.get("marketTime")
                    )
                    if parsed_market_time is not None:
                        market_time = parsed_market_time
                        stream_start_time = market_time - timedelta(
                            hours=max(args.start_hours_before, 0)
                        )
                        stream_start_pt = datetime_to_pt(stream_start_time)
                    apply_market_definition(runners, market_definition)

                for runner_change in market_change.get("rc", []):
                    if isinstance(runner_change, dict):
                        apply_runner_change(runners, runner_change)

                if (
                    stream_start_pt is not None
                    and isinstance(pt, (int, float))
                    and pt < stream_start_pt
                ):
                    continue

                tick += 1
                feature_rows = compute_features(
                    runners,
                    tick,
                    pt,
                    args.market_id,
                    market_name,
                    market_status,
                    in_play,
                    market_time,
                    feature_context,
                )

                if csv_file is not None and feature_rows:
                    if csv_writer is None:
                        csv_writer = csv.DictWriter(
                            csv_file, fieldnames=list(feature_rows[0].keys())
                        )
                        csv_writer.writeheader()
                    csv_writer.writerows(feature_rows)
                    csv_file.flush()

                if args.quiet and args.features_csv is None:
                    continue

                if not args.quiet and not args.no_clear:
                    print("\033[H", end="")

                if not args.quiet:
                    render(
                        args.replay_file,
                        args.market_id,
                        market_name,
                        market_status,
                        in_play,
                        market_time,
                        stream_start_time,
                        pt,
                        tick,
                        runners,
                        feature_rows,
                        max(args.depth, 1),
                        args.show_features,
                    )
                    print()
                    print("Ctrl+C to stop")

                if args.max_ticks is not None and tick >= args.max_ticks:
                    if csv_file is not None:
                        csv_file.close()
                    return 0

                if args.delay > 0:
                    time.sleep(args.delay)

    if csv_file is not None:
        csv_file.close()

    if tick == 0:
        print(f"No updates found for market {args.market_id} in {args.replay_file}")
        return 1

    return 0


def main() -> int:
    args = parse_args()
    try:
        return stream_replay(args)
    except KeyboardInterrupt:
        print("\nStopped by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
