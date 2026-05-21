#!/usr/bin/env python3
import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from replay_stream_match_odds_correct import (
    DEFAULT_DELAY_SECONDS,
    DEFAULT_REPLAY_FILE,
    FeatureContext,
    RunnerState,
    apply_market_definition,
    apply_runner_change,
    compute_features,
    datetime_to_pt,
    parse_market_time,
)


DEFAULT_TARGET_MARKETS_FILE = Path("replay/target_markets.txt")
DEFAULT_FEATURES_CSV = Path("replay/selected_markets_features.csv")
TARGET_MARKET_TYPES = ("MATCH_ODDS", "CORRECT_SCORE")
TARGET_MARKET_TYPE_PREFIXES = ("OVER_UNDER_",)


@dataclass
class MarketState:
    market_id: str
    market_type: str | None = None
    market_name: str | None = None
    event_name: str | None = None
    market_status: str | None = None
    in_play: bool | None = None
    market_time: datetime | None = None
    stream_start_pt: int | None = None
    runners: dict[tuple[int, float | None], RunnerState] = field(default_factory=dict)
    feature_context: FeatureContext = field(default_factory=FeatureContext)
    tick: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate full trading feature CSV for Match Odds, Correct Score, "
            "and Over/Under *.5 goal markets from a Betfair historical stream."
        )
    )
    parser.add_argument(
        "--replay-file",
        type=Path,
        default=DEFAULT_REPLAY_FILE,
        help=f"Historical stream file. Default: {DEFAULT_REPLAY_FILE}",
    )
    parser.add_argument(
        "--target-markets-file",
        type=Path,
        default=DEFAULT_TARGET_MARKETS_FILE,
        help=(
            "File with selected market ids. Default uses replay/target_markets.txt. "
            "Each line format: market_id | market_type | market_name | event_name"
        ),
    )
    parser.add_argument(
        "--market-id",
        action="append",
        default=[],
        help="Explicit market id to include. Can be passed multiple times.",
    )
    parser.add_argument(
        "--discover-targets",
        action="store_true",
        help=(
            "Ignore target-markets-file and include every encountered MATCH_ODDS, "
            "CORRECT_SCORE, and OVER_UNDER_* market id."
        ),
    )
    parser.add_argument(
        "--features-csv",
        type=Path,
        default=DEFAULT_FEATURES_CSV,
        help=f"Output CSV path. Default: {DEFAULT_FEATURES_CSV}",
    )
    parser.add_argument(
        "--start-hours-before",
        type=float,
        default=2.0,
        help="Start writing rows this many hours before each marketTime. Default: 2.0",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after this many selected market updates.",
    )
    parser.add_argument(
        "--snapshot-all-markets",
        action="store_true",
        help=(
            "On every selected update, write a full snapshot for all selected markets "
            "that already have runner state. Default writes only changed selected markets."
        ),
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print progress every N selected updates. Use 0 to disable. Default: 1000",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help=f"Optional delay between selected updates. Default: 0.0",
    )
    return parser.parse_args()


def parse_target_markets_file(path: Path) -> dict[str, dict[str, str | None]]:
    targets: dict[str, dict[str, str | None]] = {}
    if not path.exists():
        return targets

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if not parts or not parts[0]:
                continue
            targets[parts[0]] = {
                "market_type": parts[1] if len(parts) > 1 else None,
                "market_name": parts[2] if len(parts) > 2 else None,
                "event_name": parts[3] if len(parts) > 3 else None,
            }
    return targets


def is_target_market_type(market_type: str | None) -> bool:
    if not market_type:
        return False

    mt = str(market_type)

    if mt in {
        "MATCH_ODDS",
        "CORRECT_SCORE",
        "TOTAL_GOALS",
        "TEAM_TOTAL_GOALS",
        "FIRST_HALF_GOALS_05",
    }:
        return True

    if mt.startswith("OVER_UNDER_"):
        return True

    return False


def ensure_market(
    markets: dict[str, MarketState],
    market_id: str,
    seed: dict[str, str | None] | None = None,
) -> MarketState:
    if market_id not in markets:
        markets[market_id] = MarketState(market_id=market_id)
    state = markets[market_id]
    if seed:
        state.market_type = seed.get("market_type") or state.market_type
        state.market_name = seed.get("market_name") or state.market_name
        state.event_name = seed.get("event_name") or state.event_name
    return state


def update_market_metadata(
    state: MarketState,
    market_definition: dict[str, Any],
    start_hours_before: float,
) -> None:
    state.market_type = market_definition.get("marketType", state.market_type)
    state.market_name = market_definition.get("name", state.market_name)
    state.event_name = market_definition.get("eventName", state.event_name)
    state.market_status = market_definition.get("status", state.market_status)
    state.in_play = market_definition.get("inPlay", state.in_play)

    parsed_market_time = parse_market_time(market_definition.get("marketTime"))
    if parsed_market_time is not None:
        state.market_time = parsed_market_time
        stream_start_time = state.market_time - timedelta(
            hours=max(start_hours_before, 0)
        )
        state.stream_start_pt = datetime_to_pt(stream_start_time)


def should_write_market(state: MarketState, pt: Any) -> bool:
    if not state.runners:
        return False
    if (
        state.stream_start_pt is not None
        and isinstance(pt, (int, float))
        and pt < state.stream_start_pt
    ):
        return False
    return True


def enrich_row_with_multi_market_metadata(row: dict[str, Any], state: MarketState) -> None:
    row["market_type"] = state.market_type
    row["event_name"] = state.event_name
    row["market_runner_count"] = len(state.runners)


def compute_market_rows(state: MarketState, pt: Any) -> list[dict[str, Any]]:
    state.tick += 1
    rows = compute_features(
        state.runners,
        state.tick,
        pt,
        state.market_id,
        state.market_name,
        state.market_status,
        state.in_play,
        state.market_time,
        state.feature_context,
    )
    for row in rows:
        enrich_row_with_multi_market_metadata(row, state)
    return rows


def selected_markets_for_snapshot(
    markets: dict[str, MarketState],
    selected_ids: set[str],
    changed_selected_ids: list[str],
    snapshot_all_markets: bool,
) -> list[MarketState]:
    if snapshot_all_markets:
        return [
            markets[market_id]
            for market_id in sorted(selected_ids)
            if market_id in markets and markets[market_id].runners
        ]

    return [
        markets[market_id]
        for market_id in changed_selected_ids
        if market_id in markets and markets[market_id].runners
    ]


def stream_replay(args: argparse.Namespace) -> int:
    if not args.replay_file.exists():
        print(f"File not found: {args.replay_file}")
        return 1

    seeded_targets = {}
    selected_ids = set(str(market_id) for market_id in args.market_id)
    if not args.discover_targets:
        seeded_targets = {
            market_id: seed
            for market_id, seed in parse_target_markets_file(
                args.target_markets_file
            ).items()
            if is_target_market_type(seed.get("market_type"))
        }
        selected_ids.update(seeded_targets)

    if not selected_ids and not args.discover_targets:
        print(
            "No target markets configured. Pass --market-id, create "
            f"{args.target_markets_file}, or use --discover-targets."
        )
        return 1

    args.features_csv.parent.mkdir(parents=True, exist_ok=True)
    markets: dict[str, MarketState] = {}
    csv_file = args.features_csv.open("w", encoding="utf-8", newline="")
    csv_writer: csv.DictWriter[str] | None = None
    selected_updates = 0
    rows_written = 0

    try:
        with args.replay_file.open("r", encoding="utf-8") as replay:
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
                        update_market_metadata(
                            state,
                            market_definition,
                            args.start_hours_before,
                        )
                        apply_market_definition(state.runners, market_definition)

                    for runner_change in market_change.get("rc", []):
                        if isinstance(runner_change, dict):
                            apply_runner_change(state.runners, runner_change)

                    if market_id not in changed_selected_ids:
                        changed_selected_ids.append(market_id)

                if not changed_selected_ids:
                    continue

                output_rows: list[dict[str, Any]] = []
                for state in selected_markets_for_snapshot(
                    markets,
                    selected_ids,
                    changed_selected_ids,
                    args.snapshot_all_markets,
                ):
                    if should_write_market(state, pt):
                        output_rows.extend(compute_market_rows(state, pt))

                if output_rows:
                    selected_updates += 1
                    if csv_writer is None:
                        csv_writer = csv.DictWriter(
                            csv_file,
                            fieldnames=list(output_rows[0].keys()),
                            extrasaction="ignore",
                        )
                        csv_writer.writeheader()
                    csv_writer.writerows(output_rows)
                    rows_written += len(output_rows)

                    if (
                        args.progress_every
                        and selected_updates % args.progress_every == 0
                    ):
                        print(
                            f"selected_updates={selected_updates} "
                            f"rows_written={rows_written} "
                            f"selected_markets={len(selected_ids)}"
                        )

                if (
                    args.max_ticks is not None
                    and selected_updates >= args.max_ticks
                ):
                    break

                if args.delay > 0:
                    import time

                    time.sleep(args.delay or DEFAULT_DELAY_SECONDS)
    finally:
        csv_file.close()

    if rows_written == 0:
        print(f"No rows written to {args.features_csv}")
        return 1

    print(
        f"Done: selected_updates={selected_updates}, rows_written={rows_written}, "
        f"selected_markets={len(selected_ids)}, csv={args.features_csv}"
    )
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
