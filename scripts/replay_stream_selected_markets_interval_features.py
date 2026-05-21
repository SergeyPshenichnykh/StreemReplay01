#!/usr/bin/env python3
import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from replay_stream_match_odds_correct import (
    DEFAULT_REPLAY_FILE,
    FeatureContext,
    apply_market_definition,
    apply_runner_change,
    compute_features,
    format_pt,
)
from replay_stream_selected_markets_features import (
    DEFAULT_TARGET_MARKETS_FILE,
    MarketState,
    ensure_market,
    is_target_market_type,
    parse_target_markets_file,
    should_write_market,
    update_market_metadata,
)


DEFAULT_OUTPUT_DIR = Path("replay/selected_market_snapshots")
DEFAULT_INTERVALS_MS = (50, 100, 250, 500, 1000, 2000, 5000)


@dataclass
class CsvSeries:
    name: str
    path: Path
    file: Any
    fieldnames: list[str] | None = None
    resume_after_pt: int | None = None
    writer: csv.DictWriter[str] | None = None
    rows_written: int = 0
    snapshots_written: int = 0
    resumed_rows: list[dict[str, Any]] | None = None

    def write_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        if self.writer is None:
            fieldnames = self.fieldnames or list(rows[0].keys())
            self.writer = csv.DictWriter(
                self.file,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            if self.fieldnames is None:
                self.writer.writeheader()
        self.writer.writerows(rows)
        self.file.flush()
        self.rows_written += len(rows)
        self.snapshots_written += 1

    def close(self) -> None:
        self.file.close()


@dataclass
class SeriesMarketContext:
    context: FeatureContext
    tick: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate separate full-snapshot CSVs for selected football markets at "
            "real stream updates and fixed millisecond cadences."
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
        help=f"Selected market list. Default: {DEFAULT_TARGET_MARKETS_FILE}",
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
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for the 8 CSV files. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--prefix",
        default="selected_markets",
        help="Output filename prefix. Default: selected_markets",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        action="append",
        default=None,
        help=(
            "Fixed snapshot interval in milliseconds. Can be passed multiple times. "
            "Default: 50,100,250,500,1000,2000,5000"
        ),
    )
    parser.add_argument(
        "--no-update-csv",
        action="store_true",
        help="Do not create the event/update-cycle CSV.",
    )
    parser.add_argument(
        "--start-hours-before",
        type=float,
        default=2.0,
        help="Start writing after this many hours before marketTime. Default: 2.0",
    )
    parser.add_argument(
        "--max-snapshots",
        type=int,
        default=None,
        help="Stop after this many snapshots per series. Intended for tests.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress every N update snapshots. Use 0 to disable. Default: 100",
    )
    parser.add_argument(
        "--append-existing",
        action="store_true",
        help=(
            "Append to existing CSV files and skip snapshots already present in them. "
            "The replay is still warmed from the start so derivative context is rebuilt."
        ),
    )
    return parser.parse_args()


def load_selected_ids(args: argparse.Namespace) -> tuple[set[str], dict[str, dict[str, str | None]]]:
    seeded_targets: dict[str, dict[str, str | None]] = {}
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
    return selected_ids, seeded_targets


def ready_market_ids(
    markets: dict[str, MarketState],
    selected_ids: set[str],
    pt: Any,
) -> list[str]:
    return [
        market_id
        for market_id in sorted(selected_ids)
        if market_id in markets and should_write_market(markets[market_id], pt)
    ]


def all_selected_markets_ready(
    markets: dict[str, MarketState],
    selected_ids: set[str],
    pt: Any,
) -> bool:
    if not selected_ids:
        return False
    return len(ready_market_ids(markets, selected_ids, pt)) == len(selected_ids)


def enrich_snapshot_row(
    row: dict[str, Any],
    state: MarketState,
    series_name: str,
    snapshot_pt: int,
    source_pt: Any,
) -> None:
    row["market_type"] = state.market_type
    row["event_name"] = state.event_name
    row["market_runner_count"] = len(state.runners)
    row["snapshot_series"] = series_name
    row["snapshot_pt"] = snapshot_pt
    row["snapshot_pt_utc"] = format_pt(snapshot_pt)
    row["source_pt"] = source_pt
    row["source_pt_utc"] = format_pt(source_pt)


def compute_snapshot_rows(
    markets: dict[str, MarketState],
    selected_ids: set[str],
    series_name: str,
    snapshot_pt: int,
    source_pt: Any,
    contexts: dict[tuple[str, str], SeriesMarketContext],
) -> list[dict[str, Any]]:
    output_rows: list[dict[str, Any]] = []
    for market_id in ready_market_ids(markets, selected_ids, snapshot_pt):
        state = markets[market_id]
        context_key = (series_name, market_id)
        series_context = contexts.setdefault(
            context_key,
            SeriesMarketContext(context=FeatureContext()),
        )
        series_context.tick += 1
        rows = compute_features(
            state.runners,
            series_context.tick,
            snapshot_pt,
            state.market_id,
            state.market_name,
            state.market_status,
            state.in_play,
            state.market_time,
            series_context.context,
        )
        for row in rows:
            enrich_snapshot_row(row, state, series_name, snapshot_pt, source_pt)
        output_rows.extend(rows)
    return output_rows


def read_existing_csv_state(path: Path) -> tuple[list[str] | None, int | None]:
    if not path.exists() or path.stat().st_size == 0:
        return None, None

    with path.open("r", encoding="utf-8", newline="") as file:
        fieldnames = next(csv.reader(file), None)

    with path.open("rb") as file:
        file.seek(0, 2)
        end = file.tell()
        file.seek(max(0, end - 1024 * 1024))
        lines = [
            line
            for line in file.read().decode("utf-8", errors="ignore").splitlines()
            if line.strip()
        ]

    if not fieldnames or len(lines) < 2:
        return fieldnames, None

    row = next(csv.DictReader([",".join(fieldnames), lines[-1]]))
    snapshot_pt = row.get("snapshot_pt")
    try:
        return fieldnames, int(float(snapshot_pt)) if snapshot_pt else None
    except ValueError:
        return fieldnames, None


def read_existing_snapshot_rows(path: Path) -> tuple[
    list[str] | None,
    int | None,
    list[dict[str, Any]],
]:
    if not path.exists() or path.stat().st_size == 0:
        return None, None, []

    with path.open("r", encoding="utf-8", newline="") as file:
        fieldnames = next(csv.reader(file), None)

    if not fieldnames:
        return None, None, []

    chunk_size = 1024 * 1024
    with path.open("rb") as file:
        file.seek(0, 2)
        end = file.tell()
        while chunk_size <= max(end * 2, 1):
            file.seek(max(0, end - chunk_size))
            lines = [
                line
                for line in file.read().decode("utf-8", errors="ignore").splitlines()
                if line.strip()
            ]
            if len(lines) < 2:
                return fieldnames, None, []
            rows = list(csv.DictReader([",".join(fieldnames), *lines[1:]]))
            if not rows:
                return fieldnames, None, []
            snapshot_pt = rows[-1].get("snapshot_pt")
            try:
                resume_after_pt = int(float(snapshot_pt)) if snapshot_pt else None
            except ValueError:
                return fieldnames, None, []
            snapshot_rows = [
                row for row in rows if row.get("snapshot_pt") == snapshot_pt
            ]
            # A full selected-market snapshot is currently 40 runner rows. Use a
            # conservative lower bound so appends can seed prior feature values.
            if len(snapshot_rows) >= 30 or chunk_size >= end:
                return fieldnames, resume_after_pt, snapshot_rows
            chunk_size *= 2

    return fieldnames, None, []


def open_csv_series(args: argparse.Namespace, name: str) -> CsvSeries:
    path = args.output_dir / f"{args.prefix}_{name}.csv"
    fieldnames = None
    resume_after_pt = None
    resumed_rows: list[dict[str, Any]] | None = None
    mode = "w"
    if args.append_existing and path.exists() and path.stat().st_size > 0:
        fieldnames, resume_after_pt, resumed_rows = read_existing_snapshot_rows(path)
        mode = "a"
        print(
            f"append {name}: resume_after_pt={resume_after_pt} "
            f"resume_rows={len(resumed_rows or [])} path={path}"
        )
    return CsvSeries(
        name=name,
        path=path,
        file=path.open(mode, encoding="utf-8", newline=""),
        fieldnames=fieldnames,
        resume_after_pt=resume_after_pt,
        resumed_rows=resumed_rows,
    )


def open_series(args: argparse.Namespace, intervals_ms: list[int]) -> dict[str, CsvSeries]:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    series: dict[str, CsvSeries] = {}
    if not args.no_update_csv:
        name = "update"
        series[name] = open_csv_series(args, name)
    for interval_ms in intervals_ms:
        name = f"{interval_ms}ms"
        series[name] = open_csv_series(args, name)
    return series


def close_series(series: dict[str, CsvSeries]) -> None:
    for item in series.values():
        item.close()


def series_reached_limit(series: CsvSeries, max_snapshots: int | None) -> bool:
    return max_snapshots is not None and series.snapshots_written >= max_snapshots


def series_should_skip_snapshot(series: CsvSeries, snapshot_pt: int) -> bool:
    return series.resume_after_pt is not None and snapshot_pt <= series.resume_after_pt


def apply_message_to_markets(
    message: dict[str, Any],
    markets: dict[str, MarketState],
    selected_ids: set[str],
    seeded_targets: dict[str, dict[str, str | None]],
    args: argparse.Namespace,
) -> bool:
    changed = False
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
            update_market_metadata(state, market_definition, args.start_hours_before)
            apply_market_definition(state.runners, market_definition)

        for runner_change in market_change.get("rc", []):
            if isinstance(runner_change, dict):
                apply_runner_change(state.runners, runner_change)

        changed = True
    return changed


def seed_contexts_from_existing_snapshots(
    series: dict[str, CsvSeries],
    contexts: dict[tuple[str, str], SeriesMarketContext],
) -> None:
    for series_name, csv_series in series.items():
        if not csv_series.resumed_rows:
            continue

        rows_by_market: dict[str, list[dict[str, Any]]] = {}
        for row in csv_series.resumed_rows:
            market_id = row.get("market_id")
            if market_id:
                rows_by_market.setdefault(market_id, []).append(row)

        for market_id, rows in rows_by_market.items():
            context = FeatureContext()
            max_tick = 0
            for row in rows:
                try:
                    selection_id = int(row["selection_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                handicap_raw = row.get("handicap")
                handicap: float | None
                if handicap_raw in (None, ""):
                    handicap = None
                else:
                    try:
                        handicap = float(handicap_raw)
                    except ValueError:
                        handicap = None
                parsed_row: dict[str, Any] = {}
                for key, value in row.items():
                    if value == "":
                        parsed_row[key] = None
                        continue
                    try:
                        parsed_row[key] = float(value)
                    except (TypeError, ValueError):
                        parsed_row[key] = value
                parsed_row["selection_id"] = selection_id
                parsed_row["handicap"] = handicap
                context.previous[(selection_id, handicap)] = parsed_row
                try:
                    max_tick = max(max_tick, int(float(row.get("tick") or 0)))
                except ValueError:
                    pass
            contexts[(series_name, market_id)] = SeriesMarketContext(
                context=context,
                tick=max_tick,
            )


def stream_replay(args: argparse.Namespace) -> int:
    if not args.replay_file.exists():
        print(f"File not found: {args.replay_file}")
        return 1

    selected_ids, seeded_targets = load_selected_ids(args)
    if not selected_ids and not args.discover_targets:
        print(
            "No target markets configured. Pass --market-id, create "
            f"{args.target_markets_file}, or use --discover-targets."
        )
        return 1

    intervals_ms = sorted(set(args.interval_ms or DEFAULT_INTERVALS_MS))
    series = open_series(args, intervals_ms)
    markets: dict[str, MarketState] = {}
    contexts: dict[tuple[str, str], SeriesMarketContext] = {}
    seed_contexts_from_existing_snapshots(series, contexts)
    next_due_by_interval: dict[int, int | None] = {interval: None for interval in intervals_ms}
    update_snapshots = 0
    last_applied_pt: int | None = None

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
                if not isinstance(pt, (int, float)):
                    continue
                pt = int(pt)

                if (
                    last_applied_pt is not None
                    and all_selected_markets_ready(markets, selected_ids, pt)
                ):
                    for interval_ms in intervals_ms:
                        name = f"{interval_ms}ms"
                        if series_reached_limit(series[name], args.max_snapshots):
                            continue
                        next_due = next_due_by_interval[interval_ms]
                        while next_due is not None and next_due < pt:
                            if series_should_skip_snapshot(series[name], next_due):
                                next_due += interval_ms
                                continue
                            rows = compute_snapshot_rows(
                                markets,
                                selected_ids,
                                name,
                                next_due,
                                last_applied_pt,
                                contexts,
                            )
                            if not series_should_skip_snapshot(series[name], next_due):
                                series[name].write_rows(rows)
                            next_due += interval_ms
                            if series_reached_limit(series[name], args.max_snapshots):
                                next_due = None
                                break
                        next_due_by_interval[interval_ms] = next_due

                changed = apply_message_to_markets(
                    message,
                    markets,
                    selected_ids,
                    seeded_targets,
                    args,
                )

                if not changed or not all_selected_markets_ready(markets, selected_ids, pt):
                    continue

                last_applied_pt = pt

                for interval_ms in intervals_ms:
                    name = f"{interval_ms}ms"
                    if (
                        next_due_by_interval[interval_ms] is None
                        and not series_reached_limit(series[name], args.max_snapshots)
                    ):
                        next_due_by_interval[interval_ms] = pt
                    if (
                        next_due_by_interval[interval_ms] == pt
                        and not series_reached_limit(series[name], args.max_snapshots)
                    ):
                        if not series_should_skip_snapshot(series[name], pt):
                            rows = compute_snapshot_rows(
                                markets,
                                selected_ids,
                                name,
                                pt,
                                pt,
                                contexts,
                            )
                            series[name].write_rows(rows)
                        next_due_by_interval[interval_ms] = pt + interval_ms

                if (
                    "update" in series
                    and not series_reached_limit(series["update"], args.max_snapshots)
                ):
                    previous_update_snapshots = series["update"].snapshots_written
                    if not series_should_skip_snapshot(series["update"], pt):
                        rows = compute_snapshot_rows(
                            markets,
                            selected_ids,
                            "update",
                            pt,
                            pt,
                            contexts,
                        )
                        series["update"].write_rows(rows)
                    update_snapshots = series["update"].snapshots_written
                    if (
                        update_snapshots != previous_update_snapshots
                        and args.progress_every
                        and update_snapshots % args.progress_every == 0
                    ):
                        print(
                            f"update_snapshots={update_snapshots} "
                            f"selected_markets={len(selected_ids)} "
                            f"rows_update={series['update'].rows_written}"
                        )

                if args.max_snapshots is not None:
                    done = all(
                        item.snapshots_written >= args.max_snapshots
                        for item in series.values()
                    )
                    if done:
                        break
    finally:
        close_series(series)

    written_any = False
    for name, item in series.items():
        if item.rows_written:
            written_any = True
        print(
            f"{name}: snapshots={item.snapshots_written}, "
            f"rows={item.rows_written}, csv={item.path}"
        )

    if not written_any:
        print("No rows written.")
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
