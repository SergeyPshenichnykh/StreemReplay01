#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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
DEFAULT_OUT_DIR = _default_path("dutching_correct_score", "replay/dutching_correct_score")
DEFAULT_GOALS = _default_path("goal_timestamps.csv", "replay/goal_timestamps.csv")

SCORE_RE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")


@dataclass(frozen=True)
class Selection:
    selection_id: str
    runner_name: str
    kind: str
    home: int | None
    away: int | None
    best_lay: float
    lay_size: float | None

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (
            self.home if self.home is not None else 99,
            self.away if self.away is not None else 99,
            self.runner_name,
        )


@dataclass(frozen=True)
class RunnerKind:
    kind: str
    home: int | None
    away: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find positive taker BACK dutching packages on Correct Score market."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--goals-csv", type=Path, default=DEFAULT_GOALS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--mode", choices=("exact-only", "include-any-other", "both"), default="both")
    parser.add_argument("--totals", default="2-10", help="Range like 2-10 or comma list like 2,3,4.")
    parser.add_argument("--min-lay-size", type=float, default=0.0)
    parser.add_argument("--min-margin", type=float, default=0.0)
    parser.add_argument(
        "--staking-method",
        choices=("fixed-stake", "target-profit", "minimum-stake"),
        default="fixed-stake",
        help="How to size stakes for the dutching package (Bet Angel style).",
    )
    parser.add_argument("--total-stake", type=float, default=100.0, help="Used with --staking-method fixed-stake.")
    parser.add_argument("--target-profit", type=float, default=20.0, help="Used with --staking-method target-profit.")
    parser.add_argument("--min-stake", type=float, default=2.0, help="Used with --staking-method minimum-stake.")
    parser.add_argument(
        "--stake-decimals",
        type=int,
        default=2,
        help="Round computed stakes to this many decimals (Betfair is typically 2).",
    )
    parser.add_argument("--market-name-regex", default="Correct Score")
    parser.add_argument("--progress-rows", type=int, default=500_000)
    parser.add_argument("--max-rows", type=int, default=0, help="Debug limit; 0 means full file.")
    parser.add_argument(
        "--allow-impossible-totals",
        action="store_true",
        help="Do not filter out target totals that are already below current goal count.",
    )
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument(
        "--csv-safe",
        action="store_true",
        help="Use Python csv.reader. Default uses faster line.split(',') for locally generated CSV.",
    )
    return parser.parse_args()


def parse_totals(value: str) -> list[int]:
    value = value.strip()
    if "-" in value:
        lo, hi = value.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def setup_logging(out_dir: Path, level: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "correct_score_dutching.log"

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


def load_goal_pts(path: Path) -> list[int]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        pts = []
        for row in reader:
            raw = row.get("goal_time_estimate_pt") or row.get("pt") or ""
            try:
                pts.append(int(float(raw)))
            except ValueError:
                continue
    return sorted(pts)


def count_goals_so_far(goal_pts: list[int], pt_text: str) -> int:
    try:
        pt = int(float(pt_text))
    except ValueError:
        return 0
    goals = 0
    for goal_pt in goal_pts:
        if goal_pt <= pt:
            goals += 1
        else:
            break
    return goals


def parse_runner_kind(runner_name: str) -> RunnerKind | None:
    name = runner_name.strip()
    m = SCORE_RE.match(name)
    if m:
        return RunnerKind("exact", int(m.group(1)), int(m.group(2)))

    norm = re.sub(r"[^a-z]+", " ", name.lower()).strip()
    if norm in {"any other home win", "any unquoted home win"}:
        return RunnerKind("any_other_home", None, None)
    if norm in {"any other away win", "any unquoted away win"}:
        return RunnerKind("any_other_away", None, None)
    if norm in {"any other draw", "any unquoted draw"}:
        return RunnerKind("any_other_draw", None, None)
    return None


def parse_selection(selection_id: str, runner_name: str, best_lay_text: str, lay_size_text: str) -> Selection | None:
    kind = parse_runner_kind(runner_name)
    if kind is None:
        return None

    try:
        best_lay = float(best_lay_text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(best_lay) or best_lay <= 1.0:
        return None

    lay_size = None
    try:
        lay_size = float(lay_size_text) if lay_size_text != "" else None
    except (TypeError, ValueError):
        lay_size = None

    return Selection(selection_id, runner_name.strip(), kind.kind, kind.home, kind.away, best_lay, lay_size)


def exact_score_set(total_goals: int) -> set[tuple[int, int]]:
    return {(home, total_goals - home) for home in range(total_goals + 1)}


def build_legs(
    selections: list[Selection],
    total_goals: int,
    mode: str,
    exact_catalog: set[tuple[int, int]],
) -> list[Selection]:
    exact = [
        s for s in selections
        if s.kind == "exact" and s.home is not None and s.away is not None and s.home + s.away == total_goals
    ]
    covered = {(s.home, s.away) for s in exact}
    missing = exact_score_set(total_goals) - covered
    if not missing:
        return sorted(exact, key=lambda s: s.sort_key)
    if mode == "exact-only":
        return []

    needed = set()
    for home, away in missing:
        # Any Other covers scorelines outside the quoted Correct Score grid.
        # It must not replace a quoted scoreline that simply has no executable
        # lay price at this snapshot.
        if (home, away) in exact_catalog:
            return []
        if home == away:
            needed.add("any_other_draw")
        elif home > away:
            needed.add("any_other_home")
        else:
            needed.add("any_other_away")

    broad = [s for s in selections if s.kind in needed]
    broad_kinds = {s.kind for s in broad}
    if needed - broad_kinds:
        return []

    dedup = {s.selection_id: s for s in exact + broad}
    return sorted(dedup.values(), key=lambda s: s.sort_key)


def analyze_snapshot(
    market_id: str,
    market_name: str,
    tick: str,
    time_value: str,
    selections: list[Selection],
    totals: Iterable[int],
    modes: list[str],
    min_lay_size: float,
    min_margin: float,
    exact_catalog: set[tuple[int, int]],
    current_goals: int,
    allow_impossible_totals: bool,
    staking_method: str,
    total_stake: float,
    target_profit: float,
    min_stake: float,
    stake_decimals: int,
) -> dict[str, list[dict[str, object]]]:
    hits = {mode: [] for mode in modes}

    if min_lay_size > 0:
        selections = [s for s in selections if s.lay_size is not None and s.lay_size >= min_lay_size]

    def round_stake(x: float) -> float:
        if stake_decimals <= 0:
            return float(int(round(x)))
        return round(x, stake_decimals)

    def calc_stakes(odds: list[float], method: str) -> tuple[float, list[float], float]:
        inv = [1.0 / o for o in odds]
        inv_sum_local = sum(inv)
        if inv_sum_local <= 0:
            return 0.0, [], 0.0
        margin_local = (1.0 / inv_sum_local) - 1.0

        if method == "fixed-stake":
            stake_total = max(0.0, float(total_stake))
        elif method == "target-profit":
            if margin_local <= 0:
                return 0.0, [], margin_local
            stake_total = max(0.0, float(target_profit)) / margin_local
        elif method == "minimum-stake":
            stake_total = 0.0
        else:
            raise ValueError(f"Unknown staking method: {method}")

        weights = [x / inv_sum_local for x in inv]

        if method != "minimum-stake":
            stakes_raw = [stake_total * w for w in weights]
            stakes = [round_stake(s) for s in stakes_raw]
            stake_total_eff = sum(stakes)
            margin_eff = (1.0 / inv_sum_local) - 1.0
            predicted_profit = stake_total_eff * margin_eff
            return stake_total_eff, stakes, predicted_profit

        # Minimum-stake per leg: enforce floor and redistribute remaining stake
        floor = max(0.0, float(min_stake))
        n = len(odds)
        constrained = set()
        stakes = [0.0] * n
        while True:
            for i in range(n):
                if i in constrained:
                    stakes[i] = floor
                else:
                    stakes[i] = 0.0

            constrained_total = floor * len(constrained)
            unconstrained = [i for i in range(n) if i not in constrained]
            if not unconstrained:
                break

            remaining_weights_sum = sum(weights[i] for i in unconstrained)
            if remaining_weights_sum <= 0:
                break

            # Start with the minimal total stake that gives each unconstrained leg its floor.
            stake_total_candidate = constrained_total + (floor * len(unconstrained)) / remaining_weights_sum
            for i in unconstrained:
                stakes[i] = stake_total_candidate * weights[i]

            newly_constrained = {i for i in unconstrained if stakes[i] < floor - 1e-12}
            if not newly_constrained:
                stake_total = stake_total_candidate
                break
            constrained |= newly_constrained

        stakes = [max(floor, round_stake(s)) for s in stakes]
        stake_total_eff = sum(stakes)
        predicted_profit = stake_total_eff * margin_local
        return stake_total_eff, stakes, predicted_profit

    for mode in modes:
        for total in totals:
            if not allow_impossible_totals and current_goals > total:
                continue
            legs = build_legs(selections, total, mode, exact_catalog)
            if not legs:
                continue
            inv_sum = sum(1.0 / s.best_lay for s in legs)
            if inv_sum <= 0:
                continue
            margin = (1.0 / inv_sum) - 1.0
            if margin <= min_margin:
                continue

            odds = [s.best_lay for s in legs]
            stake_total_eff, stakes, predicted_profit = calc_stakes(odds, staking_method)
            hits[mode].append(
                {
                    "market_id": market_id,
                    "market_name": market_name,
                    "tick": tick,
                    "time": time_value,
                    "mode": mode,
                    "current_goals": current_goals,
                    "total_goals": total,
                    "legs": len(legs),
                    "inv_sum": inv_sum,
                    "book_pct": inv_sum * 100.0,
                    "margin": margin,
                    "margin_pct": margin * 100.0,
                    "staking_method": staking_method,
                    "total_stake": stake_total_eff,
                    "predicted_profit": predicted_profit,
                    "runner_names": " | ".join(s.runner_name for s in legs),
                    "prices": " | ".join(f"{s.best_lay:.2f}" for s in legs),
                    "stakes": " | ".join(f"{x:.{stake_decimals}f}" for x in stakes),
                    "lay_sizes": " | ".join("" if s.lay_size is None else f"{s.lay_size:.2f}" for s in legs),
                }
            )

    return hits


def flush_tick(
    tick_rows: dict[tuple[str, str], tuple[str, str, str, list[Selection]]],
    totals: list[int],
    modes: list[str],
    min_lay_size: float,
    min_margin: float,
    hits_by_mode: dict[str, list[dict[str, object]]],
    market_exact_catalog: dict[str, set[tuple[int, int]]],
    goal_pts: list[int],
    allow_impossible_totals: bool,
    staking_method: str,
    total_stake: float,
    target_profit: float,
    min_stake: float,
    stake_decimals: int,
) -> int:
    snapshots = 0
    for (market_id, tick), (market_name, time_value, pt_value, selections) in tick_rows.items():
        snapshots += 1
        hits = analyze_snapshot(
            market_id,
            market_name,
            tick,
            time_value,
            selections,
            totals,
            modes,
            min_lay_size,
            min_margin,
            market_exact_catalog.get(market_id, set()),
            count_goals_so_far(goal_pts, pt_value),
            allow_impossible_totals,
            staking_method,
            total_stake,
            target_profit,
            min_stake,
            stake_decimals,
        )
        for mode, rows in hits.items():
            hits_by_mode[mode].extend(rows)
    tick_rows.clear()
    return snapshots


def write_outputs(out_dir: Path, hits_by_mode: dict[str, list[dict[str, object]]], totals: list[int]) -> None:
    fieldnames = [
        "market_id",
        "market_name",
        "tick",
        "time",
        "mode",
        "current_goals",
        "total_goals",
        "legs",
        "inv_sum",
        "book_pct",
        "margin",
        "margin_pct",
        "staking_method",
        "total_stake",
        "predicted_profit",
        "runner_names",
        "prices",
        "stakes",
        "lay_sizes",
    ]
    summary_fields = [
        "total_goals",
        "found_positive",
        "best_margin_pct",
        "market_id",
        "tick",
        "time",
        "legs",
        "current_goals",
        "prices",
        "runner_names",
    ]

    for mode, hits in hits_by_mode.items():
        hits = sorted(hits, key=lambda r: (int(r["total_goals"]), -float(r["margin_pct"]), str(r["tick"])))
        hits_path = out_dir / f"correct_score_dutching_hits_{mode}.csv"
        with hits_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(hits)

        summary_rows = []
        for total in totals:
            total_hits = [r for r in hits if int(r["total_goals"]) == total]
            if not total_hits:
                summary_rows.append(
                    {
                        "total_goals": total,
                        "found_positive": False,
                        "best_margin_pct": "",
                        "market_id": "",
                        "tick": "",
                        "time": "",
                        "legs": "",
                        "current_goals": "",
                        "prices": "",
                        "runner_names": "",
                    }
                )
                continue
            best = total_hits[0]
            summary_rows.append(
                {
                    "total_goals": total,
                    "found_positive": True,
                    "best_margin_pct": f"{float(best['margin_pct']):.8f}",
                    "market_id": best["market_id"],
                    "tick": best["tick"],
                    "time": best["time"],
                    "legs": best["legs"],
                    "current_goals": best["current_goals"],
                    "prices": best["prices"],
                    "runner_names": best["runner_names"],
                }
            )

        summary_path = out_dir / f"correct_score_dutching_summary_{mode}.csv"
        with summary_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=summary_fields)
            writer.writeheader()
            writer.writerows(summary_rows)

        logging.info("Wrote %s rows=%d", hits_path, len(hits))
        logging.info("Wrote %s", summary_path)
        for row in summary_rows:
            if row["found_positive"]:
                logging.info(
                    "SUMMARY %s total=%s YES best_margin_pct=%s tick=%s time=%s legs=%s current_goals=%s",
                    mode,
                    row["total_goals"],
                    row["best_margin_pct"],
                    row["tick"],
                    row["time"],
                    row["legs"],
                    row["current_goals"],
                )
            else:
                logging.info("SUMMARY %s total=%s NO", mode, row["total_goals"])


def required_indices(header: list[str]) -> dict[str, int]:
    aliases = {
        "tick": ["tick"],
        "time": ["pt_utc", "snapshot_pt_utc", "pt", "snapshot_pt"],
        "pt": ["pt", "snapshot_pt"],
        "market_id": ["market_id"],
        "market_name": ["market_name"],
        "selection_id": ["selection_id"],
        "runner_name": ["runner_name"],
        "best_lay": ["best_lay"],
        "lay_size": ["best_lay_size", "lay_size_1"],
    }
    out = {}
    for key, names in aliases.items():
        for name in names:
            if name in header:
                out[key] = header.index(name)
                break
    missing = [k for k in ["market_id", "market_name", "selection_id", "runner_name", "best_lay"] if k not in out]
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


def main() -> None:
    args = parse_args()
    totals = parse_totals(args.totals)
    modes = ["exact-only", "include-any-other"] if args.mode == "both" else [args.mode]
    log_path = setup_logging(args.out_dir, args.log_level)

    logging.info("Log path: %s", log_path)
    logging.info("Input: %s", args.input)
    logging.info("Output dir: %s", args.out_dir)
    logging.info("Modes: %s", modes)
    logging.info("Totals: %s", totals)
    logging.info("Min lay size: %s", args.min_lay_size)
    logging.info("Min margin: %s", args.min_margin)
    logging.info(
        "Staking: method=%s total_stake=%s target_profit=%s min_stake=%s stake_decimals=%s",
        args.staking_method,
        args.total_stake,
        args.target_profit,
        args.min_stake,
        args.stake_decimals,
    )
    logging.info("Goals CSV: %s", args.goals_csv)
    logging.info("Allow impossible totals: %s", args.allow_impossible_totals)
    logging.info("Reader: %s", "csv.reader" if args.csv_safe else "fast split")

    market_re = re.compile(args.market_name_regex, re.IGNORECASE)
    goal_pts = load_goal_pts(args.goals_csv)
    logging.info("Loaded goal timestamps: %s", goal_pts)
    hits_by_mode: dict[str, list[dict[str, object]]] = {mode: [] for mode in modes}
    tick_rows: dict[tuple[str, str], tuple[str, str, str, list[Selection]]] = {}
    current_tick: str | None = None

    processed_rows = 0
    correct_score_rows = 0
    parsed_rows = 0
    processed_snapshots = 0
    markets: set[str] = set()
    market_exact_catalog: dict[str, set[tuple[int, int]]] = {}

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
            processed_snapshots += flush_tick(
                tick_rows,
                totals,
                modes,
                args.min_lay_size,
                args.min_margin,
                hits_by_mode,
                market_exact_catalog,
                goal_pts,
                args.allow_impossible_totals,
                args.staking_method,
                args.total_stake,
                args.target_profit,
                args.min_stake,
                args.stake_decimals,
            )
            current_tick = tick

        market_name = get(parts, idx, "market_name")
        if not market_re.search(market_name):
            if processed_rows % args.progress_rows == 0:
                logging.info(
                    "rows=%d correct_score_rows=%d parsed_rows=%d snapshots=%d hits=%s markets=%s",
                    processed_rows,
                    correct_score_rows,
                    parsed_rows,
                    processed_snapshots,
                    {m: len(h) for m, h in hits_by_mode.items()},
                    sorted(markets),
                )
            continue

        correct_score_rows += 1
        market_id = get(parts, idx, "market_id")
        runner_name = get(parts, idx, "runner_name")
        kind = parse_runner_kind(runner_name)
        if kind is not None and kind.kind == "exact" and kind.home is not None and kind.away is not None:
            market_exact_catalog.setdefault(market_id, set()).add((kind.home, kind.away))

        selection = parse_selection(
            get(parts, idx, "selection_id"),
            runner_name,
            get(parts, idx, "best_lay"),
            get(parts, idx, "lay_size"),
        )
        if selection is None:
            continue

        parsed_rows += 1
        time_value = get(parts, idx, "time")
        pt_value = get(parts, idx, "pt")
        markets.add(market_id)
        key = (market_id, tick)
        if key not in tick_rows:
            tick_rows[key] = (market_name, time_value, pt_value, [])
        tick_rows[key][3].append(selection)

        if processed_rows % args.progress_rows == 0:
            logging.info(
                "rows=%d correct_score_rows=%d parsed_rows=%d snapshots=%d active_tick_rows=%d hits=%s markets=%s",
                processed_rows,
                correct_score_rows,
                parsed_rows,
                processed_snapshots,
                sum(len(v[3]) for v in tick_rows.values()),
                {m: len(h) for m, h in hits_by_mode.items()},
                sorted(markets),
            )

    if tick_rows:
        processed_snapshots += flush_tick(
            tick_rows,
            totals,
            modes,
            args.min_lay_size,
            args.min_margin,
            hits_by_mode,
            market_exact_catalog,
            goal_pts,
            args.allow_impossible_totals,
            args.staking_method,
            args.total_stake,
            args.target_profit,
            args.min_stake,
            args.stake_decimals,
        )

    logging.info("Finished")
    logging.info("Processed rows: %d", processed_rows)
    logging.info("Correct Score rows: %d", correct_score_rows)
    logging.info("Parsed score rows: %d", parsed_rows)
    logging.info("Processed snapshots: %d", processed_snapshots)
    logging.info("Markets: %s", sorted(markets))
    logging.info(
        "Exact score catalog sizes: %s",
        {market_id: len(scores) for market_id, scores in sorted(market_exact_catalog.items())},
    )
    for mode, hits in hits_by_mode.items():
        logging.info("Positive hits %s: %d", mode, len(hits))

    write_outputs(args.out_dir, hits_by_mode, totals)


if __name__ == "__main__":
    main()
