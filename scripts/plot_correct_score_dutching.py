#!/usr/bin/env python3
from __future__ import annotations

import os
import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

def _default_path(*candidates: str) -> Path:
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return Path(candidates[0])


DEFAULT_HITS = _default_path(
    "dutching_correct_score/correct_score_dutching_hits_include-any-other.csv",
    "replay/dutching_correct_score/correct_score_dutching_hits_include-any-other.csv",
)
DEFAULT_OUT_DIR = _default_path("dutching_correct_score/plots", "replay/dutching_correct_score/plots")

# Avoid matplotlib writing config under a potentially read-only home directory.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt


@dataclass(frozen=True)
class Point:
    t: datetime
    value: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Plot Correct Score dutching metrics from hits CSV.")
    p.add_argument("--hits-csv", type=Path, default=DEFAULT_HITS)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument(
        "--metric",
        choices=("margin_pct", "predicted_profit"),
        default="margin_pct",
        help="Which metric to plot (must exist in the hits CSV).",
    )
    p.add_argument("--max-totals", type=int, default=12, help="Limit number of totals plotted (highest priority first).")
    p.add_argument(
        "--resample-ms",
        type=int,
        default=250,
        help="Resample points onto a fixed grid in milliseconds (250 matches source snapshots). 0 disables.",
    )
    return p.parse_args()


def parse_time(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None

    # Expected: "2017-04-30 13:05:11.592 UTC"
    if raw.endswith(" UTC"):
        raw = raw[: -len(" UTC")]
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    # Fallback: try ISO.
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def resample_points(points: list[Point], step_ms: int) -> list[Point]:
    if step_ms <= 0:
        return points
    buckets: dict[int, float] = {}
    for p in points:
        ms = int(p.t.timestamp() * 1000.0)
        b = (ms // step_ms) * step_ms
        prev = buckets.get(b)
        if prev is None or p.value > prev:
            buckets[b] = p.value
    return [Point(datetime.fromtimestamp(b / 1000.0, tz=timezone.utc), buckets[b]) for b in sorted(buckets)]


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    by_total: dict[int, list[Point]] = defaultdict(list)
    with args.hits_csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RuntimeError("CSV has no header.")
        if args.metric not in reader.fieldnames:
            raise RuntimeError(f"Metric '{args.metric}' not found in CSV columns: {reader.fieldnames}")

        for row in reader:
            try:
                total = int(float(row.get("total_goals") or ""))
            except ValueError:
                continue
            t = parse_time(row.get("time") or "")
            if t is None:
                continue
            try:
                v = float(row.get(args.metric) or "")
            except ValueError:
                continue
            by_total[total].append(Point(t, v))

    if not by_total:
        raise RuntimeError("No points parsed from hits CSV.")

    # Sort each series by time and keep only the best value per timestamp (max).
    series: dict[int, list[Point]] = {}
    for total, pts in by_total.items():
        pts = sorted(pts, key=lambda p: p.t)
        dedup: dict[datetime, float] = {}
        for p in pts:
            prev = dedup.get(p.t)
            if prev is None or p.value > prev:
                dedup[p.t] = p.value
        series[total] = resample_points([Point(t, dedup[t]) for t in sorted(dedup)], args.resample_ms)

    # Choose totals with most points (then by total).
    totals_sorted = sorted(series, key=lambda k: (-len(series[k]), k))[: max(1, args.max_totals)]

    plt.figure(figsize=(14, 7))
    for total in totals_sorted:
        pts = series[total]
        xs = [p.t for p in pts]
        ys = [p.value for p in pts]
        plt.plot(xs, ys, linewidth=1.2, label=f"Total {total} (n={len(pts)})")

    plt.title(f"Correct Score dutching: {args.metric}")
    plt.xlabel("Time (UTC)")
    plt.ylabel(args.metric)
    plt.grid(True, alpha=0.25)
    plt.legend(loc="best", fontsize=9)
    plt.tight_layout()

    out_path = args.out_dir / f"correct_score_dutching_{args.metric}.png"
    plt.savefig(out_path, dpi=160)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
