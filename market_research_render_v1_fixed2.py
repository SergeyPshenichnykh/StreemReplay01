#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.graph_objects as go

BASE_DIR = Path("replay/research_output_v3")
RENDER_DIR = BASE_DIR / "rendered"
OVERVIEW_SAMPLE = BASE_DIR / "overview_sample.parquet"
GOALS_CSV = Path("replay/goal_timestamps.csv")
EVENT_STUDY_CSV = BASE_DIR / "events" / "event_study.csv"
FEATURE_CATALOG_CSV = BASE_DIR / "feature_catalog.csv"
MAX_FEATURE_PAGES = 120

TIME_CANDIDATES_UTC = [
    "snapshot_pt_utc",
    "pt_utc",
    "goal_time_estimate_utc",
    "goal_time_utc",
    "confirmation_utc",
    "reopen_utc",
]
TIME_CANDIDATES_PT = [
    "snapshot_pt",
    "pt",
    "goal_time_estimate_pt",
    "goal_time",
    "confirmation_pt",
    "reopen_pt",
]


def ensure_dirs() -> None:
    (RENDER_DIR / "features").mkdir(parents=True, exist_ok=True)
    (RENDER_DIR / "events").mkdir(parents=True, exist_ok=True)


def parse_any_time(df: pd.DataFrame) -> pd.Series:
    for col in TIME_CANDIDATES_UTC:
        if col in df.columns:
            s = pd.to_datetime(df[col], errors="coerce", utc=True)
            if s.notna().any():
                return s
    for col in TIME_CANDIDATES_PT:
        if col in df.columns:
            raw = pd.to_numeric(df[col], errors="coerce")
            if raw.notna().any():
                return pd.to_datetime(raw, unit="ms", errors="coerce", utc=True)
    raise ValueError(f"No time column detected. Available columns: {list(df.columns)[:50]}")


def load_overview_sample() -> pd.DataFrame:
    if not OVERVIEW_SAMPLE.exists():
        raise FileNotFoundError(f"Missing overview sample: {OVERVIEW_SAMPLE}")
    df = pd.read_parquet(OVERVIEW_SAMPLE)
    df["ts"] = parse_any_time(df)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def load_goals() -> pd.DataFrame:
    if not GOALS_CSV.exists():
        return pd.DataFrame(columns=["ts", "label"])
    df = pd.read_csv(GOALS_CSV)
    try:
        df["ts"] = parse_any_time(df)
    except Exception:
        return pd.DataFrame(columns=["ts", "label"])
    if "goal_number" in df.columns:
        df["label"] = df["goal_number"].apply(lambda x: f"GOAL {x}")
    else:
        df["label"] = "GOAL"
    df = df[df["ts"].notna()].copy()
    return df


def load_event_study() -> pd.DataFrame:
    if not EVENT_STUDY_CSV.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(EVENT_STUDY_CSV)
    except Exception:
        return pd.DataFrame()


def load_feature_catalog() -> pd.DataFrame:
    if not FEATURE_CATALOG_CSV.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(FEATURE_CATALOG_CSV)
    except Exception:
        return pd.DataFrame()


def add_goal_markers(fig: go.Figure, goals: pd.DataFrame) -> None:
    if goals.empty:
        return
    for _, row in goals.iterrows():
        fig.add_vline(x=row["ts"], line_dash="dash", line_width=1,
                      annotation_text=str(row.get("label", "GOAL")),
                      annotation_position="top left")


def numeric_cols(df: pd.DataFrame) -> list[str]:
    skip = {
        "ts", "market_id", "selection_id", "runner_name", "market_name", "event_name",
        "market_type", "market_status", "runner_status", "snapshot_series",
        "favourite_runner_name", "pt_utc", "snapshot_pt_utc", "goal_time_estimate_utc",
    }
    out = []
    for c in df.columns:
        if c in skip:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            out.append(c)
    return out


def choose_runner(df: pd.DataFrame) -> pd.DataFrame:
    if "is_favourite" in df.columns and df["is_favourite"].astype(str).isin(["True", "true", "1"]).any():
        fav = df[df["is_favourite"].astype(str).isin(["True", "true", "1"])].copy()
        if not fav.empty:
            return fav
    if "rank_by_best_back" in df.columns:
        ranked = df[df["rank_by_best_back"] == 1].copy()
        if not ranked.empty:
            return ranked
    if "selection_id" in df.columns:
        first_sel = df["selection_id"].dropna().iloc[0]
        return df[df["selection_id"] == first_sel].copy()
    return df.copy()


def write_html(fig: go.Figure, path: Path, title: str) -> None:
    fig.update_layout(title=title, xaxis_title="Time", hovermode="x unified")
    fig.write_html(path, include_plotlyjs="cdn")


def build_overview_pages(df: pd.DataFrame, goals: pd.DataFrame) -> list[str]:
    files = []
    groups = {
        "overview_prices.html": ["best_back", "best_lay", "mid_price", "microprice", "ltp"],
        "overview_pressure.html": ["queue_imbalance_1", "queue_imbalance_3", "notional_imbalance_1", "notional_imbalance_3"],
        "overview_depth.html": ["back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3"],
        "overview_dynamics.html": ["mid_price_delta", "microprice_delta", "spread_ticks", "spread"],
    }
    for fname, cols in groups.items():
        present = [c for c in cols if c in df.columns]
        if not present:
            continue
        fig = go.Figure()
        for col in present:
            fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
        add_goal_markers(fig, goals)
        path = RENDER_DIR / fname
        write_html(fig, path, fname.replace(".html", "").replace("_", " ").title())
        files.append(fname)

    phase_rows = []
    if "in_play" in df.columns:
        phase_rows.append(("preplay_vs_inplay_counts", df["in_play"].astype(str).value_counts().to_dict()))
    if "market_status" in df.columns:
        phase_rows.append(("market_status_counts", df["market_status"].astype(str).value_counts().to_dict()))
    if phase_rows:
        html = ["<html><body><h1>Phase Summary</h1>"]
        for title, content in phase_rows:
            html.append(f"<h2>{title}</h2><pre>{json.dumps(content, indent=2)}</pre>")
        html.append("</body></html>")
        (RENDER_DIR / "phase_summary.html").write_text("\n".join(html), encoding="utf-8")
        files.append("phase_summary.html")
    return files


def build_event_page(event_df: pd.DataFrame) -> list[str]:
    if event_df.empty:
        return []
    cols = [c for c in event_df.columns if pd.api.types.is_numeric_dtype(event_df[c]) and c not in {"goal_number"}]
    preferred = [c for c in ["mid_price", "microprice", "spread_ticks", "queue_imbalance_3", "notional_imbalance_3"] if c in cols]
    if not preferred:
        preferred = cols[:5]
    html_parts = ["<html><body><h1>Event Study</h1>"]
    files = []
    for col in preferred:
        if "seconds_from_event" in event_df.columns:
            x = event_df["seconds_from_event"]
        elif "event_offset_s" in event_df.columns:
            x = event_df["event_offset_s"]
        else:
            continue
        fig = go.Figure()
        if "goal_number" in event_df.columns:
            for goal_number, sub in event_df.groupby("goal_number"):
                fig.add_trace(go.Scatter(x=x.loc[sub.index], y=sub[col], mode="lines", name=f"goal_{goal_number}"))
        else:
            fig.add_trace(go.Scatter(x=x, y=event_df[col], mode="lines", name=col))
        path = RENDER_DIR / "events" / f"event_{col}.html"
        fig.update_layout(title=f"Event Study - {col}", xaxis_title="Seconds from event")
        fig.write_html(path, include_plotlyjs="cdn")
        files.append(str(Path("events") / f"event_{col}.html"))
        html_parts.append(f'<p><a href="event_{col}.html">{col}</a></p>')
    html_parts.append("</body></html>")
    (RENDER_DIR / "events" / "event_study.html").write_text("\n".join(html_parts), encoding="utf-8")
    files.append(str(Path("events") / "event_study.html"))
    return files


def build_feature_pages(df: pd.DataFrame, goals: pd.DataFrame, catalog: pd.DataFrame) -> list[str]:
    num = numeric_cols(df)
    preferred_order = []
    if not catalog.empty and "feature_name" in catalog.columns:
        preferred_order = [c for c in catalog["feature_name"].tolist() if c in num]
    ordered = preferred_order + [c for c in num if c not in preferred_order]
    ordered = ordered[:MAX_FEATURE_PAGES]
    files = []
    index_lines = ["<html><body><h1>Feature Pages</h1>"]
    for col in ordered:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
        add_goal_markers(fig, goals)
        fname = f"{col}.html"
        path = RENDER_DIR / "features" / fname
        write_html(fig, path, f"Feature - {col}")
        files.append(str(Path("features") / fname))
        index_lines.append(f'<p><a href="{fname}">{col}</a></p>')
    index_lines.append("</body></html>")
    (RENDER_DIR / "features" / "index.html").write_text("\n".join(index_lines), encoding="utf-8")
    files.append(str(Path("features") / "index.html"))
    return files


def build_root_index(overview_files: Iterable[str], event_files: Iterable[str], feature_files: Iterable[str]) -> None:
    html = ["<html><body><h1>Market Research Render</h1><h2>Overview</h2>"]
    for f in overview_files:
        html.append(f'<p><a href="{f}">{f}</a></p>')
    html.append("<h2>Events</h2>")
    for f in event_files:
        html.append(f'<p><a href="{f}">{f}</a></p>')
    html.append('<h2>Features</h2><p><a href="features/index.html">features/index.html</a></p>')
    html.append("</body></html>")
    (RENDER_DIR / "index.html").write_text("\n".join(html), encoding="utf-8")


def main() -> None:
    ensure_dirs()
    print("[render] loading overview sample...")
    df = load_overview_sample()
    print(f"[render] loaded {len(df):,} rows from overview sample")
    goals = load_goals()
    if not goals.empty:
        print(f"[render] loaded {len(goals)} goal markers")
    event_df = load_event_study()
    catalog = load_feature_catalog()

    print("[render] building overview pages...")
    df_main = choose_runner(df)
    overview_files = build_overview_pages(df_main, goals)

    print("[render] building event pages...")
    event_files = build_event_page(event_df)

    print("[render] building feature pages...")
    feature_files = build_feature_pages(df_main, goals, catalog)

    build_root_index(overview_files, event_files, feature_files)
    print(f"[done] rendered to {RENDER_DIR}")


if __name__ == "__main__":
    main()
