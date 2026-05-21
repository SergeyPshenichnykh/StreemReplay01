#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(".")
INPUT_DIR = ROOT / "replay" / "research_output_v3"
OUTPUT_DIR = INPUT_DIR / "rendered"
OVERVIEW_SAMPLE = INPUT_DIR / "overview_sample.parquet"
FEATURE_CATALOG = INPUT_DIR / "feature_catalog.csv"
PHASE_STATS = INPUT_DIR / "phase_stats.csv"
ML_DATASET = INPUT_DIR / "ml_dataset.csv"
EVENT_STUDY = INPUT_DIR / "events" / "event_study.csv"
GOALS_CSV = ROOT / "replay" / "goal_timestamps.csv"

MAX_FEATURE_PAGES = 120
FEATURE_BATCH_SIZE = 20

TIME_COL_CANDIDATES = [
    "snapshot_pt_utc",
    "pt_utc",
    "goal_time_estimate_utc",
    "goal_time_estimate_pt",
    "snapshot_pt",
    "pt",
]

PRICE_FEATURES = ["best_back", "best_lay", "mid_price", "microprice", "ltp"]
PRESSURE_FEATURES = [
    "queue_imbalance_1",
    "queue_imbalance_3",
    "notional_imbalance_1",
    "notional_imbalance_3",
    "market_queue_imbalance_3",
]
DEPTH_FEATURES = [
    "back_depth_1",
    "lay_depth_1",
    "back_depth_3",
    "lay_depth_3",
]
SPREAD_FEATURES = ["spread", "spread_ticks"]
DYNAMIC_FEATURES = ["mid_price_delta", "microprice_delta", "mid_price_velocity", "microprice_velocity"]


def ensure_dirs() -> None:
    (OUTPUT_DIR / "features").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "events").mkdir(parents=True, exist_ok=True)


def html_wrap(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 24px; }}
h1,h2,h3 {{ margin-bottom: 0.3em; }}
ul {{ line-height: 1.5; }}
code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 4px; }}
a {{ text-decoration: none; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def detect_time_col(df: pd.DataFrame) -> str | None:
    for col in TIME_COL_CANDIDATES:
        if col in df.columns:
            return col
    return None


def parse_time_series(df: pd.DataFrame) -> pd.Series:
    time_col = detect_time_col(df)
    if time_col is None:
        raise ValueError("No time column detected")
    if time_col.endswith("_utc"):
        return pd.to_datetime(df[time_col], utc=True, errors="coerce")
    # pt in ms
    return pd.to_datetime(df[time_col], unit="ms", utc=True, errors="coerce")


def load_goals() -> pd.DataFrame:
    if not GOALS_CSV.exists():
        return pd.DataFrame()
    goals = pd.read_csv(GOALS_CSV)
    if "goal_time_estimate_utc" in goals.columns:
        goals["goal_ts"] = pd.to_datetime(goals["goal_time_estimate_utc"], utc=True, errors="coerce")
    elif "goal_time_estimate_pt" in goals.columns:
        goals["goal_ts"] = pd.to_datetime(goals["goal_time_estimate_pt"], unit="ms", utc=True, errors="coerce")
    else:
        return pd.DataFrame()
    return goals.dropna(subset=["goal_ts"]).copy()


def load_overview_sample() -> pd.DataFrame:
    if OVERVIEW_SAMPLE.exists():
        try:
            df = pd.read_parquet(OVERVIEW_SAMPLE)
        except Exception:
            df = pd.read_csv(OVERVIEW_SAMPLE)
    else:
        raise FileNotFoundError(f"Missing {OVERVIEW_SAMPLE}")
    df["ts"] = parse_time_series(df)
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


def infer_match_phases(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["phase"] = "unknown"

    if "in_play" in out.columns:
        in_play = out["in_play"].fillna(False).astype(bool)
    else:
        in_play = pd.Series(False, index=out.index)

    if "market_status" in out.columns:
        status = out["market_status"].astype("string")
    else:
        status = pd.Series(pd.NA, index=out.index, dtype="string")

    out.loc[~in_play, "phase"] = "pre_play"
    out.loc[in_play, "phase"] = "in_play"

    susp = status.eq("SUSPENDED")
    out.loc[susp & ~in_play, "phase"] = "pre_play_suspended"
    out.loc[susp & in_play, "phase"] = "in_play_suspended"

    goals = load_goals()
    if not goals.empty:
        for goal_ts in goals["goal_ts"]:
            mask = (out["ts"] >= goal_ts - pd.Timedelta(seconds=20)) & (out["ts"] <= goal_ts)
            out.loc[mask, "phase"] = "pre_goal_window"
            mask2 = (out["ts"] > goal_ts) & (out["ts"] <= goal_ts + pd.Timedelta(seconds=60))
            out.loc[mask2, "phase"] = "post_goal_window"

    if "minutes_to_start" in out.columns:
        mts = pd.to_numeric(out["minutes_to_start"], errors="coerce")
        out.loc[mts.between(0, 10, inclusive="both") & ~in_play, "phase"] = "pre_kickoff_10m"

    return out


def add_goal_lines(fig: go.Figure, goals: pd.DataFrame) -> None:
    if goals.empty:
        return
    for _, row in goals.iterrows():
        label = f"GOAL {row['goal_number']}" if "goal_number" in goals.columns else "GOAL"
        fig.add_vline(x=row["goal_ts"], line_dash="dash", annotation_text=label, opacity=0.7)


def write_html_figure(fig: go.Figure, path: Path, title: str) -> None:
    html = fig.to_html(full_html=False, include_plotlyjs="cdn")
    path.write_text(html_wrap(title, html), encoding="utf-8")


def choose_feature_list(df: pd.DataFrame, limit: int = MAX_FEATURE_PAGES) -> list[str]:
    numeric_cols = []
    excluded = {"tick", "selection_id", "handicap", "snapshot_pt", "pt"}
    for c in df.columns:
        if c in excluded or c == "ts":
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            numeric_cols.append(c)

    priority = []
    for col in PRICE_FEATURES + PRESSURE_FEATURES + DEPTH_FEATURES + SPREAD_FEATURES + DYNAMIC_FEATURES:
        if col in numeric_cols and col not in priority:
            priority.append(col)

    for col in numeric_cols:
        if col not in priority:
            priority.append(col)

    return priority[:limit]


def build_overview(df: pd.DataFrame, goals: pd.DataFrame) -> None:
    fig = go.Figure()
    for col in PRICE_FEATURES:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig, goals)
    fig.update_layout(title="Full Match Overview: Prices", xaxis_title="Time", yaxis_title="Price")
    write_html_figure(fig, OUTPUT_DIR / "overview_prices.html", "Overview Prices")

    fig2 = go.Figure()
    for col in PRESSURE_FEATURES:
        if col in df.columns:
            fig2.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig2, goals)
    fig2.update_layout(title="Full Match Overview: Pressure / Imbalance", xaxis_title="Time", yaxis_title="Value")
    write_html_figure(fig2, OUTPUT_DIR / "overview_pressure.html", "Overview Pressure")

    fig3 = go.Figure()
    for col in DEPTH_FEATURES:
        if col in df.columns:
            fig3.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig3, goals)
    fig3.update_layout(title="Full Match Overview: Depth", xaxis_title="Time", yaxis_title="Depth")
    write_html_figure(fig3, OUTPUT_DIR / "overview_depth.html", "Overview Depth")

    fig4 = go.Figure()
    for col in SPREAD_FEATURES + DYNAMIC_FEATURES:
        if col in df.columns:
            fig4.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig4, goals)
    fig4.update_layout(title="Full Match Overview: Spread & Dynamics", xaxis_title="Time", yaxis_title="Value")
    write_html_figure(fig4, OUTPUT_DIR / "overview_dynamics.html", "Overview Dynamics")

    links = [
        "<h1>Market Research Render</h1>",
        "<ul>",
        '<li><a href="overview_prices.html">Overview Prices</a></li>',
        '<li><a href="overview_pressure.html">Overview Pressure</a></li>',
        '<li><a href="overview_depth.html">Overview Depth</a></li>',
        '<li><a href="overview_dynamics.html">Overview Dynamics</a></li>',
        '<li><a href="phase_summary.html">Phase Summary</a></li>',
        '<li><a href="events/event_study.html">Event Study</a></li>',
        '<li><a href="features/index.html">Feature Pages</a></li>',
        "</ul>",
    ]
    (OUTPUT_DIR / "index.html").write_text(html_wrap("Market Research Render", "\n".join(links)), encoding="utf-8")


def build_phase_summary(df: pd.DataFrame) -> None:
    if "phase" not in df.columns:
        return
    numeric_cols = [c for c in ["mid_price", "microprice", "spread_ticks", "queue_imbalance_3", "notional_imbalance_3"] if c in df.columns]
    if not numeric_cols:
        return

    parts = ["<h1>Phase Summary</h1>"]
    grouped = df.groupby("phase")[numeric_cols].agg(["mean", "median", "std"]).round(6)
    parts.append(grouped.to_html())
    (OUTPUT_DIR / "phase_summary.html").write_text(html_wrap("Phase Summary", "\n".join(parts)), encoding="utf-8")


def build_event_study_page() -> None:
    if not EVENT_STUDY.exists():
        return
    ev = pd.read_csv(EVENT_STUDY)
    if ev.empty:
        return

    time_col = None
    for c in ["seconds_from_event", "event_offset_s", "event_offset_seconds"]:
        if c in ev.columns:
            time_col = c
            break
    if time_col is None:
        # fallback static table
        body = "<h1>Event Study</h1>" + ev.head(200).to_html(index=False)
        (OUTPUT_DIR / "events" / "event_study.html").write_text(html_wrap("Event Study", body), encoding="utf-8")
        return

    fig = go.Figure()
    preferred = [c for c in ["mid_price", "microprice", "spread_ticks", "queue_imbalance_3", "notional_imbalance_3"] if c in ev.columns]
    for col in preferred:
        fig.add_trace(go.Scatter(x=ev[time_col], y=ev[col], mode="lines", name=col))
    fig.update_layout(title="Event Study Around Goals", xaxis_title="Seconds from Event", yaxis_title="Value")
    write_html_figure(fig, OUTPUT_DIR / "events" / "event_study.html", "Event Study")


def build_feature_pages(df: pd.DataFrame, goals: pd.DataFrame) -> None:
    features = choose_feature_list(df, MAX_FEATURE_PAGES)
    pages = []
    for idx, col in enumerate(features, start=1):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
        add_goal_lines(fig, goals)
        fig.update_layout(title=f"{col} Across Full Match", xaxis_title="Time", yaxis_title=col)
        out = OUTPUT_DIR / "features" / f"{idx:03d}_{col}.html"
        write_html_figure(fig, out, col)
        pages.append((col, out.name))

    items = ["<h1>Feature Pages</h1>", "<ul>"]
    for col, name in pages:
        items.append(f'<li><a href="{name}">{col}</a></li>')
    items.append("</ul>")
    (OUTPUT_DIR / "features" / "index.html").write_text(html_wrap("Feature Pages", "\n".join(items)), encoding="utf-8")


def main() -> None:
    ensure_dirs()
    df = load_overview_sample()
    df = infer_match_phases(df)
    goals = load_goals()
    build_overview(df, goals)
    build_phase_summary(df)
    build_event_study_page()
    build_feature_pages(df, goals)
    meta = {
        "input_dir": str(INPUT_DIR),
        "output_dir": str(OUTPUT_DIR),
        "rows_in_overview_sample": int(len(df)),
        "goal_count": int(len(goals)),
        "max_feature_pages": MAX_FEATURE_PAGES,
    }
    (OUTPUT_DIR / "render_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[done] Rendered research pages to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
