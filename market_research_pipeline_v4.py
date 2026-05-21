#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go


# --- Hardcoded paths for the user's project structure ---
INTERVAL_CSV = Path("replay/selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv")
GOALS_CSV = Path("replay/goal_timestamps.csv")
OUTPUT_DIR = Path("replay/research_output_v4")

CHUNK_SIZE = 100_000

# Keep only the most useful fields for rendering and analysis.
BASE_COLUMNS = [
    "snapshot_pt",
    "snapshot_pt_utc",
    "pt",
    "pt_utc",
    "time",
    "market_id",
    "market_type",
    "market_name",
    "event_name",
    "market_status",
    "in_play",
    "market_runner_count",
    "selection_id",
    "runner_name",
    "runner_status",
    "rank_by_best_back",
    "is_favourite",
    "best_back",
    "best_lay",
    "mid_price",
    "microprice",
    "ltp",
    "spread",
    "spread_ticks",
    "traded_volume",
    "back_depth_1",
    "lay_depth_1",
    "back_depth_3",
    "lay_depth_3",
    "queue_imbalance_1",
    "queue_imbalance_3",
    "notional_imbalance_1",
    "notional_imbalance_3",
    "market_back_book_pct",
    "market_lay_book_pct",
    "market_queue_imbalance_3",
    "market_notional_imbalance_3",
]

TEXT_DTYPES = {
    "market_id": "string",
    "market_type": "string",
    "market_name": "string",
    "event_name": "string",
    "market_status": "string",
    "selection_id": "Int64",
    "runner_name": "string",
    "runner_status": "string",
    "snapshot_pt_utc": "string",
    "pt_utc": "string",
    "time": "string",
}


@dataclass
class RunnerInfo:
    market_id: str
    selection_id: int
    market_type: str | None = None
    market_name: str | None = None
    event_name: str | None = None
    runner_name: str | None = None
    runner_status: str | None = None
    rows_written: int = 0
    first_time: str | None = None
    last_time: str | None = None


def safe_name(value: Any) -> str:
    text = str(value)
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown"


def ensure_dirs() -> None:
    (OUTPUT_DIR / "data" / "markets").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "data" / "runners").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "rendered" / "markets").mkdir(parents=True, exist_ok=True)


def detect_available_columns(csv_path: Path) -> list[str]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        return next(reader)


def pick_columns(available: list[str]) -> list[str]:
    cols = [c for c in BASE_COLUMNS if c in available]
    # Always include these if present
    for c in ["snapshot_series", "source_pt", "source_pt_utc", "favourite_runner_name"]:
        if c in available and c not in cols:
            cols.append(c)
    return cols


def parse_time(df: pd.DataFrame) -> pd.Series:
    if "time" in df.columns:
        s = pd.to_datetime(df["time"], errors="coerce", utc=True)
        if s.notna().any():
            return s
    for col in ("snapshot_pt_utc", "pt_utc"):
        if col in df.columns:
            s = pd.to_datetime(df[col], errors="coerce", utc=True)
            if s.notna().any():
                return s
    for col in ("snapshot_pt", "pt"):
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            if s.notna().any():
                return pd.to_datetime(s, unit="ms", errors="coerce", utc=True)
    raise ValueError(f"No usable time column. Columns: {list(df.columns)}")


def load_goals() -> list[pd.Timestamp]:
    if not GOALS_CSV.exists():
        return []
    goals = pd.read_csv(GOALS_CSV)
    for col in ("goal_time_estimate_pt", "goal_time_estimate_utc", "goal_time", "pt"):
        if col not in goals.columns:
            continue
        if col.endswith("_pt") or col == "pt":
            s = pd.to_numeric(goals[col], errors="coerce")
            ts = pd.to_datetime(s, unit="ms", errors="coerce", utc=True)
        else:
            ts = pd.to_datetime(goals[col], errors="coerce", utc=True)
        ts = ts.dropna().sort_values().tolist()
        if ts:
            return ts
    return []


def append_chunk_to_runner_files(chunk: pd.DataFrame, registry: dict[tuple[str, int], RunnerInfo]) -> None:
    if chunk.empty:
        return

    chunk = chunk.copy()
    chunk["ts"] = parse_time(chunk)
    chunk = chunk[chunk["ts"].notna()]
    if chunk.empty:
        return

    group_cols = ["market_id", "selection_id"]
    for (market_id, selection_id), g in chunk.groupby(group_cols, dropna=True):
        try:
            sel = int(selection_id)
        except Exception:
            continue

        g = g.sort_values("ts")
        market_id_str = str(market_id)
        market_slug = safe_name(market_id_str)
        runner_slug = safe_name(sel)
        out_path = OUTPUT_DIR / "data" / "runners" / f"market_{market_slug}__runner_{runner_slug}.csv"

        write_header = not out_path.exists()
        cols = ["ts"] + [c for c in g.columns if c != "ts"]
        g[cols].to_csv(out_path, mode="a", index=False, header=write_header)

        key = (market_id_str, sel)
        info = registry.get(key)
        if info is None:
            info = RunnerInfo(
                market_id=market_id_str,
                selection_id=sel,
                market_type=_first_nonnull(g.get("market_type")),
                market_name=_first_nonnull(g.get("market_name")),
                event_name=_first_nonnull(g.get("event_name")),
                runner_name=_first_nonnull(g.get("runner_name")),
                runner_status=_first_nonnull(g.get("runner_status")),
                rows_written=0,
            )
            registry[key] = info

        info.rows_written += len(g)
        first_ts = str(g["ts"].iloc[0])
        last_ts = str(g["ts"].iloc[-1])
        info.first_time = info.first_time or first_ts
        info.last_time = last_ts
        if not info.market_type:
            info.market_type = _first_nonnull(g.get("market_type"))
        if not info.market_name:
            info.market_name = _first_nonnull(g.get("market_name"))
        if not info.event_name:
            info.event_name = _first_nonnull(g.get("event_name"))
        if not info.runner_name:
            info.runner_name = _first_nonnull(g.get("runner_name"))
        if not info.runner_status:
            info.runner_status = _first_nonnull(g.get("runner_status"))


def _first_nonnull(series: Any) -> str | None:
    if series is None:
        return None
    try:
        vals = series.dropna()
    except Exception:
        return None
    if len(vals) == 0:
        return None
    value = vals.iloc[0]
    if pd.isna(value):
        return None
    return str(value)


def build_registry() -> dict[tuple[str, int], RunnerInfo]:
    ensure_dirs()
    available = detect_available_columns(INTERVAL_CSV)
    usecols = pick_columns(available)

    registry: dict[tuple[str, int], RunnerInfo] = {}
    print("[PASS 1] Extracting per-runner slim CSVs...")
    processed = 0
    for chunk in pd.read_csv(
        INTERVAL_CSV,
        usecols=usecols,
        chunksize=CHUNK_SIZE,
        low_memory=True,
        dtype=TEXT_DTYPES,
    ):
        append_chunk_to_runner_files(chunk, registry)
        processed += len(chunk)
        if processed and processed % 1_000_000 < CHUNK_SIZE:
            print(f"[PASS 1] processed {processed:,}+ rows")

    registry_json = {
        f"{market_id}__{selection_id}": asdict(info)
        for (market_id, selection_id), info in sorted(registry.items())
    }
    with (OUTPUT_DIR / "registry.json").open("w", encoding="utf-8") as f:
        json.dump(registry_json, f, ensure_ascii=False, indent=2)
    return registry


def build_market_index(registry: dict[tuple[str, int], RunnerInfo]) -> dict[str, list[RunnerInfo]]:
    by_market: dict[str, list[RunnerInfo]] = {}
    for (_, _), info in registry.items():
        by_market.setdefault(info.market_id, []).append(info)
    for market_id in by_market:
        by_market[market_id] = sorted(
            by_market[market_id],
            key=lambda x: (
                0 if x.runner_name else 1,
                x.runner_name or "",
                x.selection_id,
            ),
        )
    return by_market


def add_goal_lines(fig: go.Figure, goals: list[pd.Timestamp]) -> None:
    for t in goals:
        fig.add_shape(
            type="line",
            x0=t,
            x1=t,
            y0=0,
            y1=1,
            xref="x",
            yref="paper",
            line=dict(dash="dash", width=1),
        )
        fig.add_annotation(
            x=t,
            y=1,
            xref="x",
            yref="paper",
            text="GOAL",
            showarrow=False,
            yshift=-10,
            textangle=-90,
        )


def write_html(fig: go.Figure, path: Path, title: str) -> None:
    fig.update_layout(title=title, hovermode="x unified")
    fig.write_html(path, include_plotlyjs="cdn")


def build_runner_page(runner_csv: Path, info: RunnerInfo, goals: list[pd.Timestamp], market_dir: Path) -> str:
    df = pd.read_csv(runner_csv)
    if df.empty:
        return ""
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df = df[df["ts"].notna()].sort_values("ts")

    base = f"runner_{safe_name(info.selection_id)}"
    html_path = market_dir / f"{base}.html"

    fig = go.Figure()
    for col in ("best_back", "best_lay", "mid_price", "microprice", "ltp"):
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig, goals)
    fig.update_yaxes(title="Price")
    fig.update_xaxes(title="Time")
    fig.update_layout(
        title=(
            f"{info.market_id} | {info.market_name or info.market_type or ''} | "
            f"{info.runner_name or info.selection_id}"
        )
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(html_path, include_plotlyjs="cdn")

    # Additional panels
    pressure_path = market_dir / f"{base}_pressure.html"
    fig2 = go.Figure()
    for col in ("queue_imbalance_1", "queue_imbalance_3", "notional_imbalance_1", "notional_imbalance_3"):
        if col in df.columns:
            fig2.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig2, goals)
    write_html(fig2, pressure_path, f"Pressure | {info.runner_name or info.selection_id}")

    depth_path = market_dir / f"{base}_depth.html"
    fig3 = go.Figure()
    for col in ("back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3", "traded_volume"):
        if col in df.columns:
            fig3.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig3, goals)
    write_html(fig3, depth_path, f"Depth | {info.runner_name or info.selection_id}")

    return html_path.name


def build_market_page(market_id: str, infos: list[RunnerInfo], goals: list[pd.Timestamp], by_market: dict[str, list[RunnerInfo]]) -> None:
    market_slug = safe_name(market_id)
    market_dir = OUTPUT_DIR / "rendered" / "markets" / market_slug
    market_dir.mkdir(parents=True, exist_ok=True)

    # Combined market overview from all runner slim files
    combined: list[pd.DataFrame] = []
    for info in infos:
        runner_csv = OUTPUT_DIR / "data" / "runners" / f"market_{market_slug}__runner_{safe_name(info.selection_id)}.csv"
        if runner_csv.exists():
            usecols = ["ts", "runner_name", "selection_id", "mid_price", "best_back", "best_lay", "ltp", "market_queue_imbalance_3", "market_notional_imbalance_3", "market_back_book_pct", "market_lay_book_pct"]
            avail = pd.read_csv(runner_csv, nrows=0).columns.tolist()
            cols = [c for c in usecols if c in avail]
            dfr = pd.read_csv(runner_csv, usecols=cols)
            dfr["ts"] = pd.to_datetime(dfr["ts"], errors="coerce", utc=True)
            dfr = dfr[dfr["ts"].notna()]
            combined.append(dfr)

    if not combined:
        return

    mdf = pd.concat(combined, ignore_index=True)
    mdf = mdf.sort_values("ts")

    # Market overview prices across runners
    fig = go.Figure()
    for info in infos:
        runner_df = mdf[mdf["selection_id"] == info.selection_id]
        if runner_df.empty or "mid_price" not in runner_df.columns:
            continue
        fig.add_trace(
            go.Scatter(
                x=runner_df["ts"],
                y=runner_df["mid_price"],
                mode="lines",
                name=f"{info.runner_name or info.selection_id} mid",
            )
        )
    add_goal_lines(fig, goals)
    title = f"{market_id} | {infos[0].market_name or infos[0].market_type or ''} | all runners"
    write_html(fig, market_dir / "market_overview.html", title)

    # Market book / pressure
    first_runner = infos[0].selection_id
    base_df = mdf[mdf["selection_id"] == first_runner].copy()
    fig2 = go.Figure()
    for col in ("market_back_book_pct", "market_lay_book_pct", "market_queue_imbalance_3", "market_notional_imbalance_3"):
        if col in base_df.columns:
            fig2.add_trace(go.Scatter(x=base_df["ts"], y=base_df[col], mode="lines", name=col))
    add_goal_lines(fig2, goals)
    write_html(fig2, market_dir / "market_pressure.html", f"{market_id} market-level features")

    # Write runner pages and market index
    rows = []
    for info in infos:
        runner_page = build_runner_page(
            OUTPUT_DIR / "data" / "runners" / f"market_{market_slug}__runner_{safe_name(info.selection_id)}.csv",
            info,
            goals,
            market_dir,
        )
        rows.append(
            f"<tr><td>{info.selection_id}</td><td>{info.runner_name or ''}</td>"
            f"<td>{info.rows_written}</td><td><a href='{runner_page}'>prices</a></td>"
            f"<td><a href='runner_{safe_name(info.selection_id)}_pressure.html'>pressure</a></td>"
            f"<td><a href='runner_{safe_name(info.selection_id)}_depth.html'>depth</a></td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>{market_id}</title></head>
<body>
<h1>Market {market_id}</h1>
<p>{infos[0].event_name or ''} | {infos[0].market_name or infos[0].market_type or ''}</p>
<ul>
  <li><a href='market_overview.html'>Market overview (all runners)</a></li>
  <li><a href='market_pressure.html'>Market-level pressure</a></li>
</ul>
<table border='1' cellspacing='0' cellpadding='4'>
<tr><th>selection_id</th><th>runner_name</th><th>rows</th><th>prices</th><th>pressure</th><th>depth</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    (market_dir / "index.html").write_text(html, encoding="utf-8")


def build_top_index(by_market: dict[str, list[RunnerInfo]]) -> None:
    rows = []
    for market_id, infos in sorted(by_market.items()):
        market_slug = safe_name(market_id)
        market_name = infos[0].market_name or infos[0].market_type or ""
        event_name = infos[0].event_name or ""
        rows.append(
            f"<tr><td>{market_id}</td><td>{event_name}</td><td>{market_name}</td>"
            f"<td>{len(infos)}</td><td><a href='markets/{market_slug}/index.html'>open</a></td></tr>"
        )
    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Research Output v4</title></head>
<body>
<h1>Research Output v4</h1>
<p>Per-market and per-runner rendering from 250ms snapshots.</p>
<table border='1' cellspacing='0' cellpadding='4'>
<tr><th>market_id</th><th>event_name</th><th>market_name</th><th>runners</th><th>link</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    (OUTPUT_DIR / "rendered" / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    if not INTERVAL_CSV.exists():
        raise FileNotFoundError(f"Interval CSV not found: {INTERVAL_CSV}")

    ensure_dirs()
    goals = load_goals()
    print(f"[info] Loaded {len(goals)} goal markers")

    registry = build_registry()
    by_market = build_market_index(registry)
    print(f"[PASS 2] Rendering {len(by_market)} markets and {len(registry)} runners...")

    for idx, (market_id, infos) in enumerate(sorted(by_market.items()), start=1):
        build_market_page(market_id, infos, goals, by_market)
        if idx % 5 == 0 or idx == len(by_market):
            print(f"[PASS 2] rendered {idx}/{len(by_market)} markets")

    build_top_index(by_market)
    print(f"[done] Wrote output to {OUTPUT_DIR / 'rendered'}")


if __name__ == "__main__":
    main()
