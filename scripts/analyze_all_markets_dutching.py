#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path


def _default_path(*candidates: str) -> Path:
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return Path(candidates[0])


DEFAULT_INPUT = _default_path(
    "selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv",
    "replay/selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv",
)
DEFAULT_OUT_DIR = _default_path("dutching_all_markets", "replay/dutching_all_markets")


@dataclass(frozen=True)
class Runner:
    selection_id: str
    runner_name: str
    odds: float
    size: float | None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compute Bet Angel-style dutching metrics for all markets in the CSV.")
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--market-name-regex", default=".*", help="Filter markets by name (regex).")
    p.add_argument("--odds-source", choices=("best_lay", "best_back"), default="best_lay")
    p.add_argument("--min-odds", type=float, default=1.01)
    p.add_argument("--max-odds", type=float, default=1000.0)
    p.add_argument("--min-size", type=float, default=0.0, help="Require quoted size >= this (0 disables).")
    p.add_argument("--min-legs", type=int, default=2, help="Skip markets with fewer selections than this.")
    p.add_argument("--min-margin", type=float, default=-1e9, help="Filter by margin (decimal; e.g. 0.01 = 1%).")
    p.add_argument(
        "--staking-method",
        choices=("fixed-stake", "target-profit", "minimum-stake"),
        default="fixed-stake",
    )
    p.add_argument("--total-stake", type=float, default=100.0)
    p.add_argument("--target-profit", type=float, default=20.0)
    p.add_argument("--min-stake", type=float, default=2.0)
    p.add_argument("--stake-decimals", type=int, default=2)
    p.add_argument("--progress-rows", type=int, default=500_000)
    p.add_argument("--max-rows", type=int, default=0, help="Debug limit; 0 means full file.")
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--csv-safe", action="store_true", help="Use csv.reader; default uses line.split(',').")
    return p.parse_args()


def setup_logging(out_dir: Path, level: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "all_markets_dutching.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    return log_path


def required_indices(header: list[str]) -> dict[str, int]:
    aliases = {
        "tick": ["tick"],
        "time": ["pt_utc", "snapshot_pt_utc", "pt", "snapshot_pt"],
        "market_id": ["market_id"],
        "market_name": ["market_name"],
        "selection_id": ["selection_id"],
        "runner_name": ["runner_name"],
        "best_back": ["best_back"],
        "best_back_size": ["best_back_size", "back_size_1"],
        "best_lay": ["best_lay"],
        "best_lay_size": ["best_lay_size", "lay_size_1"],
    }
    out: dict[str, int] = {}
    for key, names in aliases.items():
        for name in names:
            if name in header:
                out[key] = header.index(name)
                break
    missing = [k for k in ["market_id", "market_name", "selection_id", "runner_name", "best_lay", "best_back"] if k not in out]
    if missing:
        raise RuntimeError(f"Missing required columns: {missing}")
    return out


def get(parts: list[str], idx: dict[str, int], key: str) -> str:
    i = idx.get(key)
    if i is None or i >= len(parts):
        return ""
    return parts[i].strip().strip('"')


def iter_rows(path: Path, csv_safe: bool):
    with path.open("r", encoding="utf-8", newline="") as f:
        header_line = f.readline()
        header = next(csv.reader([header_line]))
        idx = required_indices(header)
        yield header, idx, None

        if csv_safe:
            reader = csv.reader(f)
            for row in reader:
                yield header, idx, row
        else:
            for line in f:
                yield header, idx, line.rstrip("\n").rstrip("\r").split(",")


def round_stake(x: float, decimals: int) -> float:
    if decimals <= 0:
        return float(int(round(x)))
    return round(x, decimals)


def calc_stakes(
    odds: list[float],
    method: str,
    total_stake: float,
    target_profit: float,
    min_stake: float,
    stake_decimals: int,
) -> tuple[float, list[float], float, float, float]:
    inv = [1.0 / o for o in odds]
    inv_sum = sum(inv)
    if inv_sum <= 0:
        return 0.0, [], 0.0, 0.0, 0.0

    book_pct = inv_sum * 100.0
    margin = (1.0 / inv_sum) - 1.0

    if method == "fixed-stake":
        stake_total = max(0.0, float(total_stake))
    elif method == "target-profit":
        if margin <= 0:
            return 0.0, [], inv_sum, book_pct, margin
        stake_total = max(0.0, float(target_profit)) / margin
    elif method == "minimum-stake":
        stake_total = 0.0
    else:
        raise ValueError(f"Unknown staking method: {method}")

    weights = [x / inv_sum for x in inv]

    if method != "minimum-stake":
        stakes = [round_stake(stake_total * w, stake_decimals) for w in weights]
        stake_total_eff = sum(stakes)
        predicted_profit = stake_total_eff * margin
        return stake_total_eff, stakes, inv_sum, book_pct, predicted_profit

    floor = max(0.0, float(min_stake))
    n = len(odds)
    constrained: set[int] = set()
    stakes_f = [0.0] * n
    while True:
        constrained_total = floor * len(constrained)
        unconstrained = [i for i in range(n) if i not in constrained]
        if not unconstrained:
            stake_total = constrained_total
            break

        remaining_weights_sum = sum(weights[i] for i in unconstrained)
        if remaining_weights_sum <= 0:
            stake_total = constrained_total
            break

        stake_total_candidate = constrained_total + (floor * len(unconstrained)) / remaining_weights_sum
        for i in range(n):
            stakes_f[i] = floor if i in constrained else stake_total_candidate * weights[i]

        newly_constrained = {i for i in unconstrained if stakes_f[i] < floor - 1e-12}
        if not newly_constrained:
            stake_total = stake_total_candidate
            break
        constrained |= newly_constrained

    stakes = [max(floor, round_stake(s, stake_decimals)) for s in stakes_f]
    stake_total_eff = sum(stakes)
    predicted_profit = stake_total_eff * margin
    return stake_total_eff, stakes, inv_sum, book_pct, predicted_profit


def flush_tick(
    tick_rows: dict[tuple[str, str], tuple[str, str, list[Runner]]],
    out_rows: list[dict[str, object]],
    args: argparse.Namespace,
) -> int:
    snapshots = 0
    for (market_id, tick), (market_name, time_value, runners) in tick_rows.items():
        snapshots += 1
        if len(runners) < args.min_legs:
            continue

        odds = [r.odds for r in runners]
        stake_total_eff, stakes, inv_sum, book_pct, predicted_profit = calc_stakes(
            odds,
            args.staking_method,
            args.total_stake,
            args.target_profit,
            args.min_stake,
            args.stake_decimals,
        )
        if inv_sum <= 0:
            continue
        margin = (1.0 / inv_sum) - 1.0
        if margin <= args.min_margin:
            continue
        if not stakes:
            continue

        out_rows.append(
            {
                "market_id": market_id,
                "market_name": market_name,
                "tick": tick,
                "time": time_value,
                "legs": len(runners),
                "inv_sum": inv_sum,
                "book_pct": book_pct,
                "margin": margin,
                "margin_pct": margin * 100.0,
                "staking_method": args.staking_method,
                "odds_source": args.odds_source,
                "total_stake": stake_total_eff,
                "predicted_profit": predicted_profit,
                "runner_names": " | ".join(r.runner_name for r in runners),
                "prices": " | ".join(f'{r.odds:.2f}' for r in runners),
                "stakes": " | ".join(f'{s:.{args.stake_decimals}f}' for s in stakes),
                "sizes": " | ".join("" if r.size is None else f"{r.size:.2f}" for r in runners),
            }
        )

    tick_rows.clear()
    return snapshots


def main() -> None:
    args = parse_args()
    log_path = setup_logging(args.out_dir, args.log_level)
    logging.info("Log path: %s", log_path)
    logging.info("Input: %s", args.input)
    logging.info("Output dir: %s", args.out_dir)
    logging.info("Market name regex: %s", args.market_name_regex)
    logging.info("Odds source: %s", args.odds_source)
    logging.info("Min odds: %s", args.min_odds)
    logging.info("Max odds: %s", args.max_odds)
    logging.info("Min size: %s", args.min_size)
    logging.info("Min legs: %s", args.min_legs)
    logging.info("Min margin: %s", args.min_margin)
    logging.info(
        "Staking: method=%s total_stake=%s target_profit=%s min_stake=%s stake_decimals=%s",
        args.staking_method,
        args.total_stake,
        args.target_profit,
        args.min_stake,
        args.stake_decimals,
    )
    logging.info("Reader: %s", "csv.reader" if args.csv_safe else "fast split")

    market_re = re.compile(args.market_name_regex, re.IGNORECASE)
    odds_key = args.odds_source
    size_key = f"{args.odds_source}_size"

    tick_rows: dict[tuple[str, str], tuple[str, str, list[Runner]]] = {}
    out_rows: list[dict[str, object]] = []
    current_tick: str | None = None

    processed_rows = 0
    kept_rows = 0
    processed_snapshots = 0
    markets: set[str] = set()

    row_iter = iter_rows(args.input, args.csv_safe)
    header, idx, _ = next(row_iter)
    logging.info("Header columns: %d", len(header))
    logging.info("Column indices: %s", idx)

    for _, _, parts in row_iter:
        processed_rows += 1
        if args.max_rows and processed_rows > args.max_rows:
            break

        tick = get(parts, idx, "tick") or str(processed_rows)
        if current_tick is None:
            current_tick = tick
        elif tick != current_tick:
            processed_snapshots += flush_tick(tick_rows, out_rows, args)
            current_tick = tick

        market_name = get(parts, idx, "market_name")
        if not market_re.search(market_name):
            if processed_rows % args.progress_rows == 0:
                logging.info(
                    "rows=%d kept_rows=%d snapshots=%d out_rows=%d markets=%d",
                    processed_rows,
                    kept_rows,
                    processed_snapshots,
                    len(out_rows),
                    len(markets),
                )
            continue

        market_id = get(parts, idx, "market_id")
        selection_id = get(parts, idx, "selection_id")
        runner_name = get(parts, idx, "runner_name")

        odds_text = get(parts, idx, odds_key)
        try:
            odds = float(odds_text)
        except ValueError:
            continue
        if not math.isfinite(odds) or odds < args.min_odds or odds > args.max_odds:
            continue

        size_text = get(parts, idx, size_key)
        size: float | None
        try:
            size = float(size_text) if size_text != "" else None
        except ValueError:
            size = None
        if args.min_size > 0 and (size is None or size < args.min_size):
            continue

        kept_rows += 1
        time_value = get(parts, idx, "time")
        markets.add(market_id)
        key = (market_id, tick)
        if key not in tick_rows:
            tick_rows[key] = (market_name, time_value, [])
        tick_rows[key][2].append(Runner(selection_id, runner_name.strip(), odds, size))

        if processed_rows % args.progress_rows == 0:
            logging.info(
                "rows=%d kept_rows=%d snapshots=%d out_rows=%d markets=%d",
                processed_rows,
                kept_rows,
                processed_snapshots,
                len(out_rows),
                len(markets),
            )

    if tick_rows:
        processed_snapshots += flush_tick(tick_rows, out_rows, args)

    out_path = args.out_dir / "all_markets_dutching.csv"
    fieldnames = [
        "market_id",
        "market_name",
        "tick",
        "time",
        "legs",
        "inv_sum",
        "book_pct",
        "margin",
        "margin_pct",
        "staking_method",
        "odds_source",
        "total_stake",
        "predicted_profit",
        "runner_names",
        "prices",
        "stakes",
        "sizes",
    ]
    out_rows = sorted(out_rows, key=lambda r: (str(r["market_id"]), int(r["tick"])))
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    logging.info("Wrote %s rows=%d", out_path, len(out_rows))
    logging.info("Processed rows: %d", processed_rows)
    logging.info("Kept rows: %d", kept_rows)
    logging.info("Processed snapshots: %d", processed_snapshots)
    logging.info("Markets: %d", len(markets))


if __name__ == "__main__":
    main()

