
#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.graph_objects as go

BASE_DIR = Path("replay/research_output_v3")
RENDER_DIR = BASE_DIR / "rendered"
OVERVIEW_PARQUET = BASE_DIR / "overview_sample.parquet"
EVENT_STUDY_CSV = BASE_DIR / "events" / "event_study.csv"
GOALS_CSV = Path("replay/goal_timestamps.csv")

MAX_FEATURE_PAGES = 120


def ensure_dirs() -> None:
    (RENDER_DIR / "events").mkdir(parents=True, exist_ok=True)
    (RENDER_DIR / "features").mkdir(parents=True, exist_ok=True)


def write_html(fig: go.Figure, path: Path, title: str) -> None:
    fig.update_layout(title=title, hovermode="x unified")
    fig.write_html(str(path), include_plotlyjs="cdn")


def parse_goal_times() -> list[pd.Timestamp]:
    if not GOALS_CSV.exists():
        return []
    goals = pd.read_csv(GOALS_CSV)
    candidates = [
        "goal_time_estimate_pt",
        "goal_time_estimate_utc",
        "goal_time",
        "pt",
        "timestamp",
    ]
    for col in candidates:
        if col not in goals.columns:
            continue
        if col.endswith("_pt") or col == "pt":
            ts = pd.to_datetime(goals[col], unit="ms", utc=True, errors="coerce")
        else:
            ts = pd.to_datetime(goals[col], utc=True, errors="coerce")
        ts = ts.dropna()
        if len(ts):
            return list(ts)
    return []


def parse_any_time(df: pd.DataFrame) -> pd.Series:
    candidates = [
        "time",  # first: already normalized by pipeline
        "ts",
        "snapshot_pt_utc",
        "pt_utc",
        "snapshot_pt",
        "pt",
        "goal_time_estimate_pt",
        "goal_time_estimate_utc",
    ]
    for col in candidates:
        if col not in df.columns:
            continue
        s = df[col]
        if col in {"time", "ts"}:
            parsed = pd.to_datetime(s, utc=True, errors="coerce")
        elif col.endswith("_pt") or col == "pt":
            parsed = pd.to_datetime(s, unit="ms", utc=True, errors="coerce")
        else:
            parsed = pd.to_datetime(s, utc=True, errors="coerce")
        if parsed.notna().any():
            return parsed
    raise ValueError(f"No usable time column detected. Available columns: {list(df.columns)}")


def load_overview_sample() -> pd.DataFrame:
    if not OVERVIEW_PARQUET.exists():
        raise FileNotFoundError(f"Missing overview sample: {OVERVIEW_PARQUET}")
    df = pd.read_parquet(OVERVIEW_PARQUET)
    df["ts"] = parse_any_time(df)
    df = df[df["ts"].notna()].copy()
    df = df.sort_values("ts")
    return df


def add_goal_lines(fig: go.Figure, goal_times: Iterable[pd.Timestamp]) -> None:
    for t in goal_times:
        fig.add_vline(x=t, line_dash="dash", annotation_text="GOAL", annotation_position="top left")


def plot_lines(df: pd.DataFrame, cols: list[str], out: Path, title: str, goal_times: list[pd.Timestamp]) -> None:
    fig = go.Figure()
    for col in cols:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig, goal_times)
    write_html(fig, out, title)


def build_overviews(df: pd.DataFrame, goal_times: list[pd.Timestamp]) -> None:
    plot_lines(
        df,
        ["best_back", "best_lay", "mid_price", "microprice", "ltp"],
        RENDER_DIR / "overview_prices.html",
        "Full Match Overview: Prices",
        goal_times,
    )
    plot_lines(
        df,
        ["queue_imbalance_1", "queue_imbalance_3", "notional_imbalance_1", "notional_imbalance_3",
         "market_queue_imbalance_3", "market_notional_imbalance_3"],
        RENDER_DIR / "overview_pressure.html",
        "Full Match Overview: Pressure / Imbalance",
        goal_times,
    )
    plot_lines(
        df,
        ["back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3", "traded_volume"],
        RENDER_DIR / "overview_depth.html",
        "Full Match Overview: Depth / Volume",
        goal_times,
    )
    plot_lines(
        df,
        ["spread_ticks", "spread"],
        RENDER_DIR / "overview_dynamics.html",
        "Full Match Overview: Spread / Dynamics",
        goal_times,
    )

    phase_cols = [c for c in ["phase", "market_status", "in_play"] if c in df.columns]
    if phase_cols:
        html = ["<html><body><h1>Phase Summary</h1>"]
        for col in phase_cols:
            html.append(f"<h2>{col}</h2>")
            vc = df[col].astype(str).value_counts(dropna=False)
            html.append(vc.to_frame("count").to_html())
        html.append("</body></html>")
        (RENDER_DIR / "phase_summary.html").write_text("\n".join(html), encoding="utf-8")


def build_event_page() -> None:
    if not EVENT_STUDY_CSV.exists():
        return
    ev = pd.read_csv(EVENT_STUDY_CSV)
    if ev.empty:
        return
    if "event_time" in ev.columns:
        ev["event_time"] = pd.to_datetime(ev["event_time"], utc=True, errors="coerce")
    html = ["<html><body><h1>Event Study</h1>", "<p>Generated from event_study.csv</p>"]
    html.append(ev.head(500).to_html(index=False))
    html.append("</body></html>")
    (RENDER_DIR / "events" / "event_study.html").write_text("\n".join(html), encoding="utf-8")


def build_feature_pages(df: pd.DataFrame, goal_times: list[pd.Timestamp]) -> None:
    exclude = {"ts", "time"}
    numeric_cols = []
    for c in df.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            numeric_cols.append(c)
    feature_cols = numeric_cols[:MAX_FEATURE_PAGES]

    links = ["<html><body><h1>Feature Pages</h1><ul>"]
    for col in feature_cols:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
        add_goal_lines(fig, goal_times)
        out = RENDER_DIR / "features" / f"{col}.html"
        write_html(fig, out, f"Feature: {col}")
        links.append(f'<li><a href="{col}.html">{col}</a></li>')
    links.append("</ul></body></html>")
    (RENDER_DIR / "features" / "index.html").write_text("\n".join(links), encoding="utf-8")


def build_index() -> None:
    html = """
<html><body>
<h1>Market Research Rendered Output</h1>
<ul>
  <li><a href="overview_prices.html">Overview: Prices</a></li>
  <li><a href="overview_pressure.html">Overview: Pressure</a></li>
  <li><a href="overview_depth.html">Overview: Depth / Volume</a></li>
  <li><a href="overview_dynamics.html">Overview: Spread / Dynamics</a></li>
  <li><a href="phase_summary.html">Phase Summary</a></li>
  <li><a href="events/event_study.html">Event Study</a></li>
  <li><a href="features/index.html">Feature Pages</a></li>
</ul>
</body></html>
"""
    (RENDER_DIR / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    ensure_dirs()
    print("[render] loading overview sample...")
    df = load_overview_sample()
    goal_times = parse_goal_times()
    print(f"[render] loaded {len(df):,} rows; goals={len(goal_times)}")
    print("[render] building overview pages...")
    build_overviews(df, goal_times)
    print("[render] building event page...")
    build_event_page()
    print("[render] building feature pages...")
    build_feature_pages(df, goal_times)
    build_index()
    print(f"[done] rendered to {RENDER_DIR}")


if __name__ == "__main__":
    main()
