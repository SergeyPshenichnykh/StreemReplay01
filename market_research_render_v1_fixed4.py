#!/usr/bin/env python3
from pathlib import Path
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

BASE_DIR = Path("replay/research_output_v3")
RENDER_DIR = BASE_DIR / "rendered"
FEATURES_DIR = RENDER_DIR / "features"
EVENTS_DIR = RENDER_DIR / "events"
OVERVIEW_SAMPLE = BASE_DIR / "overview_sample.parquet"
GOALS_CSV = Path("replay/goal_timestamps.csv")
EVENT_STUDY_CSV = BASE_DIR / "events" / "event_study.csv"
FEATURE_CATALOG_CSV = BASE_DIR / "feature_catalog.csv"

MAX_FEATURE_PAGES = 120

def ensure_dirs():
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)

def parse_any_time(df: pd.DataFrame) -> pd.Series:
    # First prefer normalized time column if already present
    if "time" in df.columns:
        s = pd.to_datetime(df["time"], errors="coerce", utc=True)
        if s.notna().any():
            return s

    for col in ["snapshot_pt_utc", "pt_utc", "goal_time_estimate_utc"]:
        if col in df.columns:
            s = pd.to_datetime(
                df[col].astype(str).str.replace(" UTC", "", regex=False),
                errors="coerce",
                utc=True,
            )
            if s.notna().any():
                return s

    for col in ["snapshot_pt", "pt", "goal_time_estimate_pt"]:
        if col in df.columns:
            s = pd.to_datetime(pd.to_numeric(df[col], errors="coerce"), unit="ms", errors="coerce", utc=True)
            if s.notna().any():
                return s

    raise ValueError(f"No time column detected. Available columns: {list(df.columns)[:50]}")

def load_overview_sample() -> pd.DataFrame:
    if not OVERVIEW_SAMPLE.exists():
        raise FileNotFoundError(f"Missing overview sample: {OVERVIEW_SAMPLE}")
    df = pd.read_parquet(OVERVIEW_SAMPLE)
    df["ts"] = parse_any_time(df)
    df = df[df["ts"].notna()].sort_values("ts").copy()
    return df

def load_goals() -> list[pd.Timestamp]:
    if not GOALS_CSV.exists():
        return []
    g = pd.read_csv(GOALS_CSV)
    if "goal_time_estimate_pt" in g.columns:
        s = pd.to_datetime(pd.to_numeric(g["goal_time_estimate_pt"], errors="coerce"), unit="ms", errors="coerce", utc=True)
        return [t for t in s.dropna().tolist()]
    if "goal_time_estimate_utc" in g.columns:
        s = pd.to_datetime(g["goal_time_estimate_utc"].astype(str).str.replace(" UTC", "", regex=False), errors="coerce", utc=True)
        return [t for t in s.dropna().tolist()]
    return []

def add_goal_lines(fig: go.Figure, goal_times: list[pd.Timestamp]) -> None:
    # Avoid Plotly add_vline(datetime) + annotation bug by using shapes manually
    for i, t in enumerate(goal_times, start=1):
        fig.add_shape(
            type="line",
            x0=t, x1=t,
            y0=0, y1=1,
            xref="x", yref="paper",
            line=dict(dash="dash", width=1),
        )
        fig.add_annotation(
            x=t, y=1, xref="x", yref="paper",
            text=f"GOAL {i}",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
        )

def write_html(fig: go.Figure, path: Path, title: str):
    fig.update_layout(title=title, hovermode="x unified")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path), include_plotlyjs="cdn")

def plot_lines(df: pd.DataFrame, y_cols: list[str], title: str, out_path: Path, goal_times: list[pd.Timestamp]):
    fig = go.Figure()
    for col in y_cols:
        if col in df.columns:
            fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines", name=col))
    add_goal_lines(fig, goal_times)
    write_html(fig, out_path, title)

def build_overviews(df: pd.DataFrame, goal_times: list[pd.Timestamp]):
    plot_lines(
        df,
        ["best_back", "best_lay", "mid_price", "microprice", "ltp"],
        "Full Match Overview — Prices",
        RENDER_DIR / "overview_prices.html",
        goal_times,
    )
    plot_lines(
        df,
        ["queue_imbalance_1", "queue_imbalance_3", "notional_imbalance_1", "notional_imbalance_3",
         "market_queue_imbalance_3", "market_notional_imbalance_3"],
        "Full Match Overview — Pressure / Imbalance",
        RENDER_DIR / "overview_pressure.html",
        goal_times,
    )
    plot_lines(
        df,
        ["back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3", "traded_volume"],
        "Full Match Overview — Depth / Volume",
        RENDER_DIR / "overview_depth.html",
        goal_times,
    )
    plot_lines(
        df,
        ["spread_ticks", "spread", "mid_price", "microprice"],
        "Full Match Overview — Dynamics",
        RENDER_DIR / "overview_dynamics.html",
        goal_times,
    )

def build_phase_summary(df: pd.DataFrame):
    if "phase" not in df.columns:
        return
    num_cols = [c for c in ["spread_ticks","queue_imbalance_3","notional_imbalance_3","back_depth_3","lay_depth_3","traded_volume","mid_price","microprice"] if c in df.columns]
    rows = []
    for ph, g in df.groupby("phase", dropna=False):
        row = {"phase": str(ph)}
        for c in num_cols:
            row[f"{c}_mean"] = pd.to_numeric(g[c], errors="coerce").mean()
        rows.append(row)
    if not rows:
        return
    pdf = pd.DataFrame(rows)
    fig = px.bar(pdf, x="phase", y=[c for c in pdf.columns if c != "phase"], barmode="group", title="Phase Summary")
    write_html(fig, RENDER_DIR / "phase_summary.html", "Phase Summary")

def build_event_study_page():
    if not EVENT_STUDY_CSV.exists():
        return
    df = pd.read_csv(EVENT_STUDY_CSV)
    if df.empty:
        return
    time_col = None
    for c in ["seconds_from_goal", "seconds_to_goal", "event_time_sec", "offset_seconds", "t"]:
        if c in df.columns:
            time_col = c
            break
    if time_col is None:
        # try first numeric-ish column
        for c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce")
            if s.notna().any():
                time_col = c
                break
    if time_col is None:
        return
    candidate_features = [c for c in ["mid_price","microprice","spread_ticks","queue_imbalance_3","notional_imbalance_3","back_depth_3","lay_depth_3"] if c in df.columns]
    if not candidate_features:
        numeric = [c for c in df.columns if c != time_col and pd.to_numeric(df[c], errors="coerce").notna().any()]
        candidate_features = numeric[:8]
    fig = go.Figure()
    for col in candidate_features:
        fig.add_trace(go.Scatter(x=df[time_col], y=pd.to_numeric(df[col], errors="coerce"), mode="lines", name=col))
    fig.add_shape(type="line", x0=0, x1=0, y0=0, y1=1, xref="x", yref="paper", line=dict(dash="dash", width=1))
    fig.add_annotation(x=0, y=1, xref="x", yref="paper", text="GOAL", showarrow=False, xanchor="left", yanchor="bottom")
    write_html(fig, EVENTS_DIR / "event_study.html", "Event Study Around Goals")

def build_feature_pages(df: pd.DataFrame, goal_times: list[pd.Timestamp]):
    excluded = {"ts","time","phase","selection_id","market_id","runner_name","market_status","in_play"}
    numeric_cols = []
    for c in df.columns:
        if c in excluded:
            continue
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().any():
            numeric_cols.append(c)
    feature_pages = []
    for col in numeric_cols[:MAX_FEATURE_PAGES]:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["ts"], y=pd.to_numeric(df[col], errors="coerce"), mode="lines", name=col))
        add_goal_lines(fig, goal_times)
        out = FEATURES_DIR / f"{col}.html"
        write_html(fig, out, f"Feature — {col}")
        feature_pages.append((col, out.name))
    idx = ["<html><body><h1>Feature Pages</h1><ul>"]
    for col, name in feature_pages:
        idx.append(f'<li><a href="{name}">{col}</a></li>')
    idx.append("</ul></body></html>")
    (FEATURES_DIR / "index.html").write_text("\n".join(idx), encoding="utf-8")

def write_index():
    items = [
        ("overview_prices.html", "Overview — Prices"),
        ("overview_pressure.html", "Overview — Pressure"),
        ("overview_depth.html", "Overview — Depth / Volume"),
        ("overview_dynamics.html", "Overview — Dynamics"),
        ("phase_summary.html", "Phase Summary"),
        ("events/event_study.html", "Event Study"),
        ("features/index.html", "Feature Pages"),
    ]
    html = ["<html><body><h1>Market Research Rendered Output</h1><ul>"]
    for href, label in items:
        html.append(f'<li><a href="{href}">{label}</a></li>')
    html.append("</ul></body></html>")
    (RENDER_DIR / "index.html").write_text("\n".join(html), encoding="utf-8")

def main():
    ensure_dirs()
    print("[render] loading overview sample...")
    df = load_overview_sample()
    goal_times = load_goals()
    print(f"[render] loaded {len(df):,} rows; goals={len(goal_times)}")
    print("[render] building overview pages...")
    build_overviews(df, goal_times)
    build_phase_summary(df)
    build_event_study_page()
    print("[render] building feature pages...")
    build_feature_pages(df, goal_times)
    write_index()
    print(f"[done] rendered to {RENDER_DIR}")

if __name__ == "__main__":
    main()
