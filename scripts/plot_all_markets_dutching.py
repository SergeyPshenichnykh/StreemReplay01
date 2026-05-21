#!/usr/bin/env python3
from __future__ import annotations

import os
import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re


def _default_path(*candidates: str) -> Path:
    for c in candidates:
        p = Path(c)
        if p.exists():
            return p
    return Path(candidates[0])


DEFAULT_CSV = _default_path("dutching_all_markets/all_markets_dutching.csv", "replay/dutching_all_markets/all_markets_dutching.csv")
DEFAULT_OUT_DIR = _default_path("dutching_all_markets/plots", "replay/dutching_all_markets/plots")

# Avoid matplotlib writing config under a potentially read-only home directory.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt


@dataclass(frozen=True)
class Point:
    t: datetime
    value: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Plot dutching metrics per market_id from all_markets_dutching.csv.")
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--metric", choices=("margin_pct", "predicted_profit", "book_pct"), default="margin_pct")
    p.add_argument("--top", type=int, default=12, help="How many markets to plot in the combined chart.")
    p.add_argument(
        "--resample-ms",
        type=int,
        default=250,
        help="Resample points onto a fixed grid in milliseconds (250 matches the source CSV). 0 disables.",
    )
    p.add_argument("--per-market", action="store_true", help="Also generate one PNG per market_id.")
    return p.parse_args()


def parse_time(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith(" UTC"):
        raw = raw[: -len(" UTC")]
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def plot_series(series: dict[str, list[Point]], title: str, metric: str, out_path: Path) -> None:
    plt.figure(figsize=(14, 7))
    for key, pts in series.items():
        xs = [p.t for p in pts]
        ys = [p.value for p in pts]
        plt.plot(xs, ys, linewidth=1.2, label=key)
    plt.title(title)
    plt.xlabel("Time (UTC)")
    plt.ylabel(metric)
    plt.grid(True, alpha=0.25)
    plt.legend(loc="best", fontsize=9)
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=160)
    plt.close()


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

    by_market: dict[str, list[Point]] = defaultdict(list)
    market_names: dict[str, str] = {}
    with args.csv.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise RuntimeError("CSV has no header.")
        if args.metric not in reader.fieldnames:
            raise RuntimeError(f"Metric '{args.metric}' not in columns: {reader.fieldnames}")

        for row in reader:
            market_id = (row.get("market_id") or "").strip()
            if not market_id:
                continue
            market_names.setdefault(market_id, (row.get("market_name") or "").strip())
            t = parse_time(row.get("time") or "")
            if t is None:
                continue
            try:
                v = float(row.get(args.metric) or "")
            except ValueError:
                continue
            by_market[market_id].append(Point(t, v))

    if not by_market:
        raise RuntimeError("No series loaded from CSV.")

    # Dedup by timestamp (keep max) and sort.
    series: dict[str, list[Point]] = {}
    for mid, pts in by_market.items():
        pts = sorted(pts, key=lambda p: p.t)
        dedup: dict[datetime, float] = {}
        for p in pts:
            prev = dedup.get(p.t)
            if prev is None or p.value > prev:
                dedup[p.t] = p.value
        series[mid] = resample_points([Point(t, dedup[t]) for t in sorted(dedup)], args.resample_ms)

    mids_sorted = sorted(series, key=lambda m: (-len(series[m]), m))
    mids_top = mids_sorted[: max(1, args.top)]

    combined = {}
    for mid in mids_top:
        name = market_names.get(mid) or ""
        label = f"{mid} {name}".strip()
        combined[label] = series[mid]

    plot_series(
        combined,
        title=f"All markets dutching: {args.metric} (top {len(mids_top)})",
        metric=args.metric,
        out_path=args.out_dir / f"all_markets_{args.metric}_top{len(mids_top)}.png",
    )
    print(f"Wrote {args.out_dir / f'all_markets_{args.metric}_top{len(mids_top)}.png'}")

    if args.per_market:
        for mid in mids_sorted:
            name = market_names.get(mid) or ""
            safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", f"{mid}_{name}".strip("_")).strip("_")
            out_path = args.out_dir / "per_market" / f"{safe}_{args.metric}.png"
            title = f"{mid} {name}".strip()
            plot_series({title: series[mid]}, title=title, metric=args.metric, out_path=out_path)
        print(f"Wrote per-market plots under {args.out_dir / 'per_market'}")


if __name__ == "__main__":
    main()
