#!/usr/bin/env python3
import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from replay_stream_match_odds_correct import DEFAULT_REPLAY_FILE, parse_market_time
from replay_stream_selected_markets_features import (
    DEFAULT_TARGET_MARKETS_FILE,
    is_target_market_type,
    parse_target_markets_file,
)


DEFAULT_OUTPUT_CSV = Path("replay/goal_timestamps.csv")
OU_TYPE_RE = re.compile(r"OVER_UNDER_(\d+)$")
SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


@dataclass
class MarketInfo:
    market_id: str
    market_type: str | None = None
    market_name: str | None = None
    event_name: str | None = None


@dataclass
class StatusTransition:
    pt: int
    seconds_from_start: float | None
    market_id: str
    market_type: str | None
    market_name: str | None
    old_status: str | None
    old_in_play: bool | None
    new_status: str | None
    new_in_play: bool | None


@dataclass
class SuspensionCluster:
    start_pt: int
    end_pt: int
    seconds_from_start: float | None
    reopen_pt: int | None
    market_ids: set[str]
    market_types: set[str]
    transitions: list[StatusTransition]


@dataclass
class OverUnderClose:
    pt: int
    seconds_from_start: float | None
    market_id: str
    market_type: str
    market_name: str | None
    goal_number: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Infer goal timestamps from Betfair football replay market suspensions, "
            "confirmed by Over/Under *.5 market closures."
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
        "--output-csv",
        type=Path,
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output CSV. Default: {DEFAULT_OUTPUT_CSV}",
    )
    parser.add_argument(
        "--cluster-gap-ms",
        type=int,
        default=5000,
        help="Group suspension transitions within this gap. Default: 5000",
    )
    parser.add_argument(
        "--max-confirmation-gap-minutes",
        type=float,
        default=10.0,
        help=(
            "Maximum gap from suspension start to O/U closure confirmation. "
            "Default: 10 minutes"
        ),
    )
    return parser.parse_args()


def format_pt(pt: int | None) -> str | None:
    if pt is None:
        return None
    return (
        datetime.fromtimestamp(pt / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        + " UTC"
    )


def goal_number_from_market_type(market_type: str | None) -> int | None:
    if not market_type:
        return None
    match = OU_TYPE_RE.fullmatch(market_type)
    if not match:
        return None
    # OVER_UNDER_05 closes after goal 1, 15 after goal 2, 25 after goal 3, etc.
    threshold_tenths = int(match.group(1))
    if threshold_tenths % 10 != 5:
        return None
    return threshold_tenths // 10 + 1


def total_goals_from_score(score: str | None) -> int | None:
    if not score:
        return None
    match = SCORE_RE.match(score)
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2))


def load_target_markets(path: Path) -> dict[str, MarketInfo]:
    targets: dict[str, MarketInfo] = {}
    for market_id, seed in parse_target_markets_file(path).items():
        if not is_target_market_type(seed.get("market_type")):
            continue
        targets[market_id] = MarketInfo(
            market_id=market_id,
            market_type=seed.get("market_type"),
            market_name=seed.get("market_name"),
            event_name=seed.get("event_name"),
        )
    return targets


def seconds_from_market_start(pt: int, market_time: datetime | None) -> float | None:
    if market_time is None:
        return None
    return pt / 1000 - market_time.timestamp()


def collect_transitions(
    replay_file: Path,
    targets: dict[str, MarketInfo],
) -> tuple[list[StatusTransition], list[OverUnderClose], str | None]:
    market_time: datetime | None = None
    states: dict[str, tuple[str | None, bool | None]] = {}
    transitions: list[StatusTransition] = []
    ou_closes: list[OverUnderClose] = []
    final_correct_score: str | None = None

    with replay_file.open("r", encoding="utf-8") as replay:
        for line in replay:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            pt = message.get("pt")
            if not isinstance(pt, (int, float)):
                continue
            pt = int(pt)

            for market_change in message.get("mc", []):
                market_id = str(market_change.get("id"))
                if market_id not in targets:
                    continue

                market_definition = market_change.get("marketDefinition")
                if not isinstance(market_definition, dict):
                    continue

                info = targets[market_id]
                info.market_type = market_definition.get("marketType", info.market_type)
                info.market_name = market_definition.get("name", info.market_name)
                info.event_name = market_definition.get("eventName", info.event_name)

                parsed_market_time = parse_market_time(
                    market_definition.get("marketTime")
                )
                if parsed_market_time is not None and market_time is None:
                    market_time = parsed_market_time

                new_state = (
                    market_definition.get("status"),
                    market_definition.get("inPlay"),
                )
                old_state = states.get(market_id)
                if old_state == new_state:
                    continue

                states[market_id] = new_state
                transition = StatusTransition(
                    pt=pt,
                    seconds_from_start=seconds_from_market_start(pt, market_time),
                    market_id=market_id,
                    market_type=info.market_type,
                    market_name=info.market_name,
                    old_status=old_state[0] if old_state else None,
                    old_in_play=old_state[1] if old_state else None,
                    new_status=new_state[0],
                    new_in_play=new_state[1],
                )
                transitions.append(transition)

                goal_number = goal_number_from_market_type(info.market_type)
                if (
                    goal_number is not None
                    and transition.new_status == "CLOSED"
                    and transition.seconds_from_start is not None
                    and transition.seconds_from_start > 0
                ):
                    ou_closes.append(
                        OverUnderClose(
                            pt=pt,
                            seconds_from_start=transition.seconds_from_start,
                            market_id=market_id,
                            market_type=info.market_type or "",
                            market_name=info.market_name,
                            goal_number=goal_number,
                        )
                    )

                if (
                    info.market_type == "CORRECT_SCORE"
                    and transition.new_status == "CLOSED"
                ):
                    for runner in market_definition.get("runners", []):
                        if isinstance(runner, dict) and runner.get("status") == "WINNER":
                            final_correct_score = runner.get("name")

    return transitions, ou_closes, final_correct_score


def build_suspension_clusters(
    transitions: list[StatusTransition],
    cluster_gap_ms: int,
) -> list[SuspensionCluster]:
    suspend_transitions = [
        transition
        for transition in transitions
        if transition.old_status == "OPEN"
        and transition.new_status == "SUSPENDED"
        and transition.new_in_play is True
        and transition.seconds_from_start is not None
        and transition.seconds_from_start > 0
    ]

    clusters: list[SuspensionCluster] = []
    for transition in suspend_transitions:
        if not clusters or transition.pt - clusters[-1].end_pt > cluster_gap_ms:
            clusters.append(
                SuspensionCluster(
                    start_pt=transition.pt,
                    end_pt=transition.pt,
                    seconds_from_start=transition.seconds_from_start,
                    reopen_pt=None,
                    market_ids={transition.market_id},
                    market_types={transition.market_type or ""},
                    transitions=[transition],
                )
            )
        else:
            cluster = clusters[-1]
            cluster.end_pt = transition.pt
            cluster.market_ids.add(transition.market_id)
            cluster.market_types.add(transition.market_type or "")
            cluster.transitions.append(transition)

    for cluster in clusters:
        reopen_pts = [
            transition.pt
            for transition in transitions
            if transition.pt >= cluster.end_pt
            and transition.pt <= cluster.end_pt + 5 * 60 * 1000
            and transition.old_status == "SUSPENDED"
            and transition.new_status == "OPEN"
            and transition.market_id in cluster.market_ids
        ]
        cluster.reopen_pt = max(reopen_pts) if reopen_pts else None

    return clusters


def match_goal_confirmations(
    clusters: list[SuspensionCluster],
    ou_closes: list[OverUnderClose],
    max_gap_minutes: float,
    final_correct_score: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    max_gap_ms = max_gap_minutes * 60 * 1000
    used_cluster_ids: set[int] = set()
    final_total_goals = total_goals_from_score(final_correct_score)

    for close in sorted(ou_closes, key=lambda item: item.goal_number):
        if final_total_goals is not None and close.goal_number > final_total_goals:
            continue
        candidates = [
            (index, cluster)
            for index, cluster in enumerate(clusters)
            if index not in used_cluster_ids
            and cluster.start_pt <= close.pt
            and close.pt - cluster.start_pt <= max_gap_ms
            and "MATCH_ODDS" in cluster.market_types
            and "CORRECT_SCORE" in cluster.market_types
        ]
        if not candidates:
            continue

        index, cluster = max(candidates, key=lambda item: item[1].start_pt)
        used_cluster_ids.add(index)

        rows.append(
            {
                "goal_number": close.goal_number,
                "goal_time_estimate_pt": cluster.start_pt,
                "goal_time_estimate_utc": format_pt(cluster.start_pt),
                "goal_minute_from_scheduled_start": (
                    cluster.seconds_from_start / 60
                    if cluster.seconds_from_start is not None
                    else None
                ),
                "suspension_cluster_end_pt": cluster.end_pt,
                "suspension_cluster_end_utc": format_pt(cluster.end_pt),
                "reopen_pt": cluster.reopen_pt,
                "reopen_utc": format_pt(cluster.reopen_pt),
                "confirmation_pt": close.pt,
                "confirmation_utc": format_pt(close.pt),
                "confirmation_market_id": close.market_id,
                "confirmation_market_type": close.market_type,
                "confirmation_market_name": close.market_name,
                "confirmation_goal_number": close.goal_number,
                "confirmation_delay_seconds": (close.pt - cluster.start_pt) / 1000,
                "suspended_market_count": len(cluster.market_ids),
                "suspended_market_types": "|".join(sorted(cluster.market_types)),
                "final_correct_score": final_correct_score,
                "confidence": "confirmed_by_over_under_close",
            }
        )

    return rows


def write_goal_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        if not rows:
            return
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    targets = load_target_markets(args.target_markets_file)
    transitions, ou_closes, final_correct_score = collect_transitions(
        args.replay_file,
        targets,
    )
    clusters = build_suspension_clusters(transitions, args.cluster_gap_ms)
    goal_rows = match_goal_confirmations(
        clusters,
        ou_closes,
        args.max_confirmation_gap_minutes,
        final_correct_score,
    )
    write_goal_rows(args.output_csv, goal_rows)

    print(f"goals={len(goal_rows)} csv={args.output_csv}")
    for row in goal_rows:
        print(
            f"goal {row['goal_number']}: {row['goal_time_estimate_utc']} "
            f"minute={float(row['goal_minute_from_scheduled_start']):.2f} "
            f"confirmed_by={row['confirmation_market_type']} "
            f"delay={float(row['confirmation_delay_seconds']):.1f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
