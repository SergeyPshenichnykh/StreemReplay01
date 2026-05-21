
#!/usr/bin/env python3
"""
Market research pipeline for Betfair replay-derived CSVs.

What it does:
- Loads interval CSV (recommended 250ms), feature CSV, and optional goal CSV
- Aligns data on a unified time axis
- Infers match phases and event windows
- Builds interactive HTML charts for match overview and per-feature research
- Produces statistics, event studies, and ML-ready targets

Designed to work with CSVs produced by:
- replay_stream_match_odds_correct.py
- replay_stream_selected_markets_features.py
- replay_stream_selected_markets_interval_features.py
- extract_goal_timestamps.py
"""
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Any

import numpy as np
import pandas as pd

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    PLOTLY_AVAILABLE = True
except Exception:
    PLOTLY_AVAILABLE = False

try:
    from sklearn.feature_selection import mutual_info_regression
    SKLEARN_AVAILABLE = True
except Exception:
    SKLEARN_AVAILABLE = False


TIME_CANDIDATES = ["pt_utc", "timestamp", "datetime", "time", "ts", "pt"]
MARKET_ID_CANDIDATES = ["market_id"]
SELECTION_ID_CANDIDATES = ["selection_id"]
STATUS_CANDIDATES = ["market_status", "status"]
RUNNER_STATUS_CANDIDATES = ["runner_status"]
IN_PLAY_CANDIDATES = ["in_play"]

PRICE_COLUMNS_PRIORITY = [
    "best_back", "best_lay", "mid_price", "microprice", "ltp",
]
PRESSURE_COLUMNS_PRIORITY = [
    "queue_imbalance_1", "queue_imbalance_3", "notional_imbalance_3",
    "market_queue_imbalance_3", "market_notional_imbalance_3",
]
DEPTH_COLUMNS_PRIORITY = [
    "back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3",
    "back_notional_3", "lay_notional_3",
]
DERIVATIVE_COLUMNS_PRIORITY = [
    "mid_price_delta", "microprice_delta", "spread_ticks_delta",
    "queue_imbalance_3_delta", "notional_imbalance_3_delta",
]
FEATURE_GROUP_PATTERNS = [
    (r"^(best_|mid_|microprice|ltp|spread)", "price"),
    (r"(depth|notional_level|notional_)", "depth_notional"),
    (r"(imbalance|pressure_ratio)", "pressure"),
    (r"(book_pct|probability|share_)", "probability_book"),
    (r"(_delta|_velocity|_acceleration)$", "derivative"),
    (r"^(market_)", "market_level"),
    (r"^(back_price_|lay_price_|back_size_|lay_size_|level_)", "ladder_level"),
]
DEFAULT_INTERVAL_CSV = Path("replay/selected_market_snapshots/selected_markets_250ms.csv")
DEFAULT_FEATURES_CSV = Path("replay/selected_markets_features.csv")
DEFAULT_GOALS_CSV = Path("replay/goal_timestamps.csv")
DEFAULT_OUTPUT_DIR = Path("replay/research_output")



@dataclass
class DatasetBundle:
    interval: pd.DataFrame
    features: pd.DataFrame | None
    goals: pd.DataFrame | None
    merged: pd.DataFrame
    time_col: str
    market_id_col: str | None
    selection_id_col: str | None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research pipeline for replay-derived market CSVs.")
    p.add_argument("--interval-csv", type=Path, default=Path("replay/selected_market_snapshots/selected_markets_250ms.csv"), help="250ms interval CSV")
    p.add_argument("--features-csv", type=Path, default=Path("replay/selected_markets_features.csv"), help="Event/update-based features CSV")
    p.add_argument("--goals-csv", type=Path, default=Path("replay/goal_timestamps.csv"), help="Goal timestamps CSV")
    p.add_argument("--output-dir", type=Path, default=Path("replay/research_output"))
    p.add_argument("--market-id", default=None, help="Optional market_id filter")
    p.add_argument("--selection-id", default=None, help="Optional selection_id filter")
    p.add_argument("--max-feature-pages", type=int, default=200, help="Cap generated per-feature HTML pages")
    p.add_argument("--event-window-seconds", type=int, default=60, help="Window around goals for event studies")
    p.add_argument("--horizons-ms", default="250,500,1000,2000,5000", help="Future target horizons in milliseconds")
    p.add_argument("--top-mi-features", type=int, default=50, help="Top features by MI/correlation")
    p.add_argument("--sample-rows-per-feature-plot", type=int, default=50000, help="Max rows used for per-feature plotting")
    return p.parse_args()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def detect_time_col(df: pd.DataFrame) -> str:
    for c in TIME_CANDIDATES:
        if c in df.columns:
            return c
    raise ValueError(f"Could not find time column. Available columns: {list(df.columns)[:20]}...")


def detect_first_existing(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def normalize_boolean_series(s: pd.Series) -> pd.Series:
    if s.dtype == bool:
        return s
    as_str = s.astype(str).str.strip().str.lower()
    mapping = {
        "true": True, "1": True, "yes": True, "y": True,
        "false": False, "0": False, "no": False, "n": False,
    }
    out = as_str.map(mapping)
    return out.where(~s.isna(), np.nan)


def parse_time_column(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    out = df.copy()
    if time_col == "pt":
        # Betfair pt is usually milliseconds since epoch.
        vals = pd.to_numeric(out[time_col], errors="coerce")
        if vals.notna().any():
            unit = "ms" if vals.dropna().median() > 1e12 else "s"
            out["_time"] = pd.to_datetime(vals, unit=unit, utc=True, errors="coerce")
        else:
            out["_time"] = pd.to_datetime(out[time_col], utc=True, errors="coerce")
    else:
        out["_time"] = pd.to_datetime(out[time_col], utc=True, errors="coerce")
        if out["_time"].isna().all() and time_col in df.columns:
            vals = pd.to_numeric(out[time_col], errors="coerce")
            if vals.notna().any():
                unit = "ms" if vals.dropna().median() > 1e12 else "s"
                out["_time"] = pd.to_datetime(vals, unit=unit, utc=True, errors="coerce")
    if out["_time"].isna().all():
        raise ValueError(f"Failed to parse timestamps from column '{time_col}'")
    return out.sort_values("_time").reset_index(drop=True)


def read_csv_any(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin1")


def coerce_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        if c == "_time":
            continue
        if out[c].dtype == object:
            converted = pd.to_numeric(out[c], errors="ignore")
            out[c] = converted
    return out


def filter_market_runner(df: pd.DataFrame, market_id: str | None, selection_id: str | None) -> pd.DataFrame:
    out = df.copy()
    if market_id is not None and "market_id" in out.columns:
        out = out[out["market_id"].astype(str) == str(market_id)]
    if selection_id is not None and "selection_id" in out.columns:
        out = out[out["selection_id"].astype(str) == str(selection_id)]
    return out.reset_index(drop=True)


def find_goals_time_col(df: pd.DataFrame) -> str:
    candidates = [
        "goal_time", "goal_time_utc", "pt_utc", "pt", "timestamp", "reopen_time", "start_pt"
    ]
    for c in candidates:
        if c in df.columns:
            return c
    raise ValueError("Could not detect a time column in goals CSV")


def load_bundle(
    interval_csv: Path,
    features_csv: Path | None,
    goals_csv: Path | None,
    market_id: str | None,
    selection_id: str | None,
) -> DatasetBundle:
    interval = coerce_numeric_columns(parse_time_column(read_csv_any(interval_csv), detect_time_col(read_csv_any(interval_csv))))
    interval = filter_market_runner(interval, market_id, selection_id)

    features = None
    if features_csv:
        feat_raw = read_csv_any(features_csv)
        feat_time_col = detect_time_col(feat_raw)
        features = coerce_numeric_columns(parse_time_column(feat_raw, feat_time_col))
        features = filter_market_runner(features, market_id, selection_id)

    goals = None
    if goals_csv and goals_csv.exists():
        goals_raw = read_csv_any(goals_csv)
        goals_time_col = find_goals_time_col(goals_raw)
        goals = parse_time_column(goals_raw, goals_time_col).rename(columns={"_time": "goal_time"})
        if market_id is not None and "market_id" in goals.columns:
            goals = goals[goals["market_id"].astype(str) == str(market_id)].reset_index(drop=True)

    time_col = detect_time_col(interval)
    market_id_col = detect_first_existing(interval, MARKET_ID_CANDIDATES)
    selection_id_col = detect_first_existing(interval, SELECTION_ID_CANDIDATES)

    merged = interval.copy()
    if features is not None and not features.empty:
        feature_cols = [c for c in features.columns if c not in {"_time"}]
        merged = pd.merge_asof(
            merged.sort_values("_time"),
            features.sort_values("_time")[["_time"] + feature_cols],
            on="_time",
            direction="nearest",
            tolerance=pd.Timedelta("500ms"),
            suffixes=("", "_event"),
        )

    merged = add_status_features(merged)
    merged = add_goal_features(merged, goals)
    merged = add_phase_features(merged)
    merged = add_time_features(merged)

    return DatasetBundle(
        interval=interval,
        features=features,
        goals=goals,
        merged=merged,
        time_col=time_col,
        market_id_col=market_id_col,
        selection_id_col=selection_id_col,
    )


def add_status_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    in_play_col = detect_first_existing(out, IN_PLAY_CANDIDATES)
    if in_play_col:
        out["in_play_bool"] = normalize_boolean_series(out[in_play_col]).ffill()
    else:
        out["in_play_bool"] = np.nan

    market_status_col = detect_first_existing(out, STATUS_CANDIDATES)
    if market_status_col:
        out["market_status_norm"] = out[market_status_col].astype(str).str.upper()
    else:
        out["market_status_norm"] = np.nan

    runner_status_col = detect_first_existing(out, RUNNER_STATUS_CANDIDATES)
    if runner_status_col:
        out["runner_status_norm"] = out[runner_status_col].astype(str).str.upper()
    else:
        out["runner_status_norm"] = np.nan

    out["is_suspended"] = out["market_status_norm"].eq("SUSPENDED")
    out["status_change"] = out["market_status_norm"].ne(out["market_status_norm"].shift(1))
    out["runner_status_change"] = out["runner_status_norm"].ne(out["runner_status_norm"].shift(1))
    out["in_play_change"] = out["in_play_bool"].ne(out["in_play_bool"].shift(1))
    return out


def add_goal_features(df: pd.DataFrame, goals: pd.DataFrame | None) -> pd.DataFrame:
    out = df.copy()
    out["is_goal"] = False
    out["goal_number"] = np.nan
    out["seconds_to_nearest_goal"] = np.nan
    out["seconds_since_last_goal"] = np.nan
    out["seconds_until_next_goal"] = np.nan
    out["goal_window_pre"] = False
    out["goal_window_post"] = False

    if goals is None or goals.empty:
        return out

    goal_times = goals["goal_time"].sort_values().tolist()
    goal_nums = list(range(1, len(goal_times) + 1))
    out_times = out["_time"]

    nearest_deltas = []
    since_last = []
    until_next = []

    for t in out_times:
        deltas = [(t - gt).total_seconds() for gt in goal_times]
        if deltas:
            abs_idx = int(np.argmin(np.abs(deltas)))
            nearest_deltas.append(deltas[abs_idx])
            past = [d for d in deltas if d >= 0]
            future = [d for d in deltas if d < 0]
            since_last.append(min(past) if past else np.nan)
            until_next.append(-max(future) if future else np.nan)
        else:
            nearest_deltas.append(np.nan)
            since_last.append(np.nan)
            until_next.append(np.nan)

    out["seconds_to_nearest_goal"] = nearest_deltas
    out["seconds_since_last_goal"] = since_last
    out["seconds_until_next_goal"] = until_next
    out["goal_window_pre"] = out["seconds_until_next_goal"].between(0, 30, inclusive="both")
    out["goal_window_post"] = out["seconds_since_last_goal"].between(0, 60, inclusive="both")

    for i, gt in enumerate(goal_times, start=1):
        hit = (out["_time"] - gt).abs() <= pd.Timedelta("500ms")
        out.loc[hit, "is_goal"] = True
        out.loc[hit, "goal_number"] = i
    return out


def add_phase_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["phase_structural"] = "unknown"
    out["phase_event"] = "normal"

    # kickoff detection: first transition to in_play=True
    kickoff_time = None
    if out["in_play_bool"].notna().any():
        kickoff_rows = out.index[(out["in_play_bool"] == True) & (out["in_play_bool"].shift(1) != True)].tolist()
        kickoff_time = out.loc[kickoff_rows[0], "_time"] if kickoff_rows else None

    if kickoff_time is not None:
        out.loc[out["_time"] < kickoff_time, "phase_structural"] = "pre_match"
        out.loc[out["_time"] >= kickoff_time, "phase_structural"] = "in_play"

        # halftime heuristic: longest suspended block after kickoff and before last quarter of match
        susp = out[out["is_suspended"]].copy()
        if not susp.empty:
            grp = (susp.index.to_series().diff().fillna(1) != 1).cumsum()
            blocks = susp.groupby(grp).agg(start=("_time", "min"), end=("_time", "max"), n=("_time", "size"))
            blocks["dur_s"] = (blocks["end"] - blocks["start"]).dt.total_seconds()
            candidate = blocks[blocks["dur_s"] >= 60].sort_values("dur_s", ascending=False)
            if not candidate.empty:
                ht_start = candidate.iloc[0]["start"]
                ht_end = candidate.iloc[0]["end"]
                out.loc[(out["_time"] >= ht_start) & (out["_time"] <= ht_end), "phase_structural"] = "halftime"
                out.loc[(out["_time"] > kickoff_time) & (out["_time"] < ht_start), "phase_structural"] = "first_half"
                out.loc[out["_time"] > ht_end, "phase_structural"] = "second_half"
            else:
                out.loc[out["_time"] >= kickoff_time, "phase_structural"] = "in_play"

    out.loc[out["goal_window_pre"], "phase_event"] = "pre_goal_window"
    out.loc[out["goal_window_post"], "phase_event"] = "post_goal_window"
    out.loc[out["is_suspended"], "phase_event"] = "suspended"
    out.loc[out["is_goal"], "phase_event"] = "goal"
    return out


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["elapsed_s"] = (out["_time"] - out["_time"].iloc[0]).dt.total_seconds()
    dt = out["_time"].diff().dt.total_seconds()
    out["dt_seconds"] = dt
    return out


def infer_numeric_feature_columns(df: pd.DataFrame) -> list[str]:
    ignore = {
        "_time", "tick", "pt", "pt_utc",
        "market_id", "market_name", "market_status", "market_status_norm",
        "in_play", "in_play_bool", "market_time",
        "selection_id", "handicap", "runner_name", "runner_status", "runner_status_norm",
        "sort_priority", "favourite_selection_id", "favourite_runner_name",
        "event_name", "market_type", "phase_structural", "phase_event",
    }
    cols = []
    for c in df.columns:
        if c in ignore:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    return cols


def categorize_feature(name: str) -> str:
    for pattern, group in FEATURE_GROUP_PATTERNS:
        if re.search(pattern, name):
            return group
    return "other"


def build_feature_catalog(df: pd.DataFrame) -> pd.DataFrame:
    numeric_cols = infer_numeric_feature_columns(df)
    rows = []
    for c in numeric_cols:
        s = df[c]
        rows.append({
            "feature_name": c,
            "group": categorize_feature(c),
            "n_non_null": int(s.notna().sum()),
            "missing_rate": float(s.isna().mean()),
            "zero_rate": float((s.fillna(0) == 0).mean()),
            "n_unique": int(s.nunique(dropna=True)),
            "mean": safe_float(s.mean()),
            "std": safe_float(s.std()),
            "min": safe_float(s.min()),
            "p01": safe_float(s.quantile(0.01)),
            "p50": safe_float(s.quantile(0.50)),
            "p99": safe_float(s.quantile(0.99)),
            "max": safe_float(s.max()),
            "lag1_autocorr": safe_float(s.corr(s.shift(1))),
            "is_derivative": bool(re.search(r"(_delta|_velocity|_acceleration)$", c)),
            "is_market_level": c.startswith("market_"),
        })
    return pd.DataFrame(rows).sort_values(["group", "feature_name"]).reset_index(drop=True)


def safe_float(x: Any) -> float | None:
    if pd.isna(x):
        return None
    try:
        return float(x)
    except Exception:
        return None


def add_targets(df: pd.DataFrame, horizons_ms: Iterable[int]) -> pd.DataFrame:
    out = df.copy()
    out = out.sort_values("_time").reset_index(drop=True)
    mid_col = "mid_price" if "mid_price" in out.columns else None
    bb_col = "best_back" if "best_back" in out.columns else None
    bl_col = "best_lay" if "best_lay" in out.columns else None
    spread_col = "spread_ticks" if "spread_ticks" in out.columns else None

    for h in horizons_ms:
        future_time = out["_time"] + pd.to_timedelta(h, unit="ms")
        aligned = pd.merge_asof(
            out[["_time"]].assign(target_time=future_time).sort_values("target_time"),
            out[["_time", *(c for c in [mid_col, bb_col, bl_col, spread_col] if c is not None)]].sort_values("_time"),
            left_on="target_time",
            right_on="_time",
            direction="forward",
            suffixes=("", "_future"),
        )
        if mid_col:
            out[f"target_mid_price_change_{h}ms"] = aligned[mid_col].values - out[mid_col].values
            out[f"target_mid_price_direction_{h}ms"] = np.sign(out[f"target_mid_price_change_{h}ms"]).astype("float")
        if bb_col:
            out[f"target_best_back_change_{h}ms"] = aligned[bb_col].values - out[bb_col].values
        if bl_col:
            out[f"target_best_lay_change_{h}ms"] = aligned[bl_col].values - out[bl_col].values
        if spread_col:
            out[f"target_spread_ticks_change_{h}ms"] = aligned[spread_col].values - out[spread_col].values
    return out


def compute_phase_stats(df: pd.DataFrame, catalog: pd.DataFrame) -> pd.DataFrame:
    rows = []
    features = catalog["feature_name"].tolist()
    for phase_col in ["phase_structural", "phase_event"]:
        if phase_col not in df.columns:
            continue
        gb = df.groupby(phase_col)
        for feat in features:
            if feat not in df.columns:
                continue
            stats = gb[feat].agg(["mean", "std", "median"]).reset_index()
            for _, r in stats.iterrows():
                rows.append({
                    "phase_type": phase_col,
                    "phase": r[phase_col],
                    "feature_name": feat,
                    "mean": safe_float(r["mean"]),
                    "std": safe_float(r["std"]),
                    "median": safe_float(r["median"]),
                })
    return pd.DataFrame(rows)


def compute_event_study(df: pd.DataFrame, goals: pd.DataFrame | None, features: list[str], window_seconds: int) -> pd.DataFrame:
    if goals is None or goals.empty:
        return pd.DataFrame()
    rows = []
    goal_times = goals["goal_time"].sort_values().tolist()
    for goal_idx, gt in enumerate(goal_times, start=1):
        window = df[(df["_time"] >= gt - pd.Timedelta(seconds=window_seconds)) &
                    (df["_time"] <= gt + pd.Timedelta(seconds=window_seconds))].copy()
        if window.empty:
            continue
        window["rel_s"] = (window["_time"] - gt).dt.total_seconds().round(3)
        for feat in features:
            if feat not in window.columns:
                continue
            sub = window[["rel_s", feat]].rename(columns={feat: "value"}).copy()
            sub["feature_name"] = feat
            sub["goal_number"] = goal_idx
            rows.append(sub)
    if not rows:
        return pd.DataFrame()
    raw = pd.concat(rows, ignore_index=True)
    agg = raw.groupby(["feature_name", "rel_s"], as_index=False)["value"].agg(["mean", "median", "std", "count"]).reset_index()
    agg.columns = ["feature_name", "rel_s", "mean", "median", "std", "count"]
    return agg


def feature_target_relationships(df: pd.DataFrame, catalog: pd.DataFrame, top_n: int = 50) -> pd.DataFrame:
    target_cols = [c for c in df.columns if c.startswith("target_mid_price_change_")]
    if not target_cols:
        return pd.DataFrame()
    target = target_cols[0]
    numeric_features = catalog["feature_name"].tolist()
    work = df[numeric_features + [target]].replace([np.inf, -np.inf], np.nan).dropna()
    if work.empty:
        return pd.DataFrame()

    corrs = []
    target_s = work[target]
    for feat in numeric_features:
        s = work[feat]
        corr = s.corr(target_s)
        corrs.append({"feature_name": feat, "target": target, "pearson_corr": safe_float(corr)})

    result = pd.DataFrame(corrs)
    if SKLEARN_AVAILABLE and len(work) > 100:
        X = work[numeric_features].clip(lower=work[numeric_features].quantile(0.01), upper=work[numeric_features].quantile(0.99), axis=1).fillna(0.0)
        y = target_s.fillna(0.0)
        try:
            mi = mutual_info_regression(X, y, random_state=42)
            mi_df = pd.DataFrame({"feature_name": numeric_features, "mutual_info": mi})
            result = result.merge(mi_df, on="feature_name", how="left")
        except Exception:
            result["mutual_info"] = np.nan
    else:
        result["mutual_info"] = np.nan

    result["abs_pearson_corr"] = result["pearson_corr"].abs()
    return result.sort_values(["mutual_info", "abs_pearson_corr"], ascending=False).head(top_n).reset_index(drop=True)


def choose_overview_columns(df: pd.DataFrame) -> dict[str, list[str]]:
    return {
        "prices": [c for c in PRICE_COLUMNS_PRIORITY if c in df.columns],
        "pressure": [c for c in PRESSURE_COLUMNS_PRIORITY if c in df.columns],
        "depth": [c for c in DEPTH_COLUMNS_PRIORITY if c in df.columns],
        "derivatives": [c for c in DERIVATIVE_COLUMNS_PRIORITY if c in df.columns],
    }


def save_overview_html(df: pd.DataFrame, goals: pd.DataFrame | None, output_path: Path) -> None:
    if not PLOTLY_AVAILABLE:
        return
    cols = choose_overview_columns(df)
    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.02,
        subplot_titles=("Prices", "Pressure / Imbalance", "Depth / Notional", "Derivatives", "Status / Events"),
        row_heights=[0.28, 0.18, 0.18, 0.18, 0.18],
    )

    for c in cols["prices"]:
        fig.add_trace(go.Scatter(x=df["_time"], y=df[c], mode="lines", name=c), row=1, col=1)
    for c in cols["pressure"]:
        fig.add_trace(go.Scatter(x=df["_time"], y=df[c], mode="lines", name=c), row=2, col=1)
    for c in cols["depth"]:
        fig.add_trace(go.Scatter(x=df["_time"], y=df[c], mode="lines", name=c), row=3, col=1)
    for c in cols["derivatives"]:
        fig.add_trace(go.Scatter(x=df["_time"], y=df[c], mode="lines", name=c), row=4, col=1)

    if "is_suspended" in df.columns:
        fig.add_trace(go.Scatter(x=df["_time"], y=df["is_suspended"].astype(int), mode="lines", name="is_suspended"), row=5, col=1)
    if "in_play_bool" in df.columns:
        fig.add_trace(go.Scatter(x=df["_time"], y=df["in_play_bool"].astype(float), mode="lines", name="in_play"), row=5, col=1)
    if "rank_by_best_back" in df.columns:
        fig.add_trace(go.Scatter(x=df["_time"], y=df["rank_by_best_back"], mode="lines", name="rank_by_best_back"), row=5, col=1)

    if goals is not None and not goals.empty:
        for _, row in goals.iterrows():
            fig.add_vline(x=row["goal_time"], line_dash="dash", annotation_text="GOAL", line_width=1)

    changes = df.index[df["status_change"].fillna(False)].tolist() if "status_change" in df.columns else []
    for idx in changes:
        fig.add_vline(x=df.loc[idx, "_time"], line_dash="dot", line_width=1)

    fig.update_layout(
        title="Market replay research overview",
        height=1300,
        hovermode="x unified",
        legend=dict(orientation="h"),
    )
    fig.write_html(str(output_path), include_plotlyjs="cdn")


def sample_df_for_plotting(df: pd.DataFrame, max_rows: int) -> pd.DataFrame:
    if len(df) <= max_rows:
        return df
    idx = np.linspace(0, len(df) - 1, num=max_rows, dtype=int)
    return df.iloc[idx].copy()


def save_feature_pages(df: pd.DataFrame, catalog: pd.DataFrame, output_dir: Path, max_pages: int, sample_rows: int) -> None:
    if not PLOTLY_AVAILABLE:
        return

    features = catalog["feature_name"].tolist()[:max_pages]
    sampled = sample_df_for_plotting(df, sample_rows)

    for feat in features:
        if feat not in sampled.columns:
            continue
        fig = make_subplots(
            rows=4, cols=1, shared_xaxes=False, vertical_spacing=0.08,
            subplot_titles=(
                f"{feat} over time",
                f"{feat} distribution",
                f"{feat} by structural phase",
                f"{feat} vs first target",
            ),
            row_heights=[0.36, 0.18, 0.20, 0.26],
        )
        fig.add_trace(go.Scatter(x=sampled["_time"], y=sampled[feat], mode="lines", name=feat), row=1, col=1)
        fig.add_trace(go.Histogram(x=sampled[feat], name=f"{feat}_hist"), row=2, col=1)

        if "phase_structural" in sampled.columns:
            for phase, g in sampled.groupby("phase_structural"):
                fig.add_trace(go.Box(y=g[feat], name=str(phase), boxpoints=False), row=3, col=1)

        target_cols = [c for c in sampled.columns if c.startswith("target_mid_price_change_")]
        if target_cols:
            target = target_cols[0]
            paired = sampled[[feat, target]].replace([np.inf, -np.inf], np.nan).dropna()
            if not paired.empty:
                paired = paired.sample(min(len(paired), 5000), random_state=42)
                fig.add_trace(
                    go.Scatter(
                        x=paired[feat], y=paired[target], mode="markers", name=f"{feat} vs {target}",
                        marker=dict(size=4, opacity=0.35),
                    ),
                    row=4, col=1,
                )

        fig.update_layout(height=1400, title=f"Feature research: {feat}", showlegend=True)
        fig.write_html(str(output_dir / f"{sanitize_filename(feat)}.html"), include_plotlyjs="cdn")


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def save_event_study_pages(event_study: pd.DataFrame, output_dir: Path) -> None:
    if not PLOTLY_AVAILABLE or event_study.empty:
        return
    for feat, g in event_study.groupby("feature_name"):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=g["rel_s"], y=g["mean"], mode="lines", name="mean"))
        fig.add_trace(go.Scatter(x=g["rel_s"], y=g["median"], mode="lines", name="median"))
        fig.add_vline(x=0, line_dash="dash", annotation_text="goal")
        fig.update_layout(
            title=f"Goal event study: {feat}",
            xaxis_title="Seconds relative to goal",
            yaxis_title=feat,
            height=700,
        )
        fig.write_html(str(output_dir / f"goal_event_{sanitize_filename(feat)}.html"), include_plotlyjs="cdn")


def save_summary_markdown(
    bundle: DatasetBundle,
    catalog: pd.DataFrame,
    relationships: pd.DataFrame,
    output_path: Path,
    horizons: list[int],
) -> None:
    df = bundle.merged
    lines = []
    lines.append("# Market Research Summary")
    lines.append("")
    lines.append("## Dataset")
    lines.append(f"- Rows: {len(df):,}")
    lines.append(f"- Columns: {len(df.columns):,}")
    lines.append(f"- Time range: {df['_time'].min()} → {df['_time'].max()}")
    if "market_id" in df.columns and df["market_id"].nunique(dropna=True):
        lines.append(f"- market_id values: {df['market_id'].nunique(dropna=True)}")
    if "selection_id" in df.columns and df["selection_id"].nunique(dropna=True):
        lines.append(f"- selection_id values: {df['selection_id'].nunique(dropna=True)}")
    if bundle.goals is not None:
        lines.append(f"- Goal markers: {len(bundle.goals):,}")
    lines.append("")
    lines.append("## Phase counts")
    if "phase_structural" in df.columns:
        lines.append(df["phase_structural"].value_counts(dropna=False).to_markdown())
    if "phase_event" in df.columns:
        lines.append("")
        lines.append(df["phase_event"].value_counts(dropna=False).to_markdown())
    lines.append("")
    lines.append("## Top feature groups")
    lines.append(catalog["group"].value_counts().to_markdown())
    lines.append("")
    lines.append("## Top 20 lowest-missing features")
    lines.append(catalog.sort_values(["missing_rate", "std"], ascending=[True, False]).head(20).to_markdown(index=False))
    if not relationships.empty:
        lines.append("")
        lines.append("## Top feature/target relationships")
        lines.append(relationships.head(20).to_markdown(index=False))
    lines.append("")
    lines.append("## Horizons")
    lines.append(", ".join(f"{h}ms" for h in horizons))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_index_html(output_dir: Path, catalog: pd.DataFrame, relationships: pd.DataFrame) -> None:
    feature_links = "\n".join(
        f'<li><a href="features/{sanitize_filename(f)}.html">{f}</a></li>'
        for f in catalog["feature_name"].head(300).tolist()
    )
    event_links = "\n".join(
        f'<li><a href="events/goal_event_{sanitize_filename(f)}.html">{f}</a></li>'
        for f in catalog["feature_name"].head(300).tolist()
    )
    top_rel_html = relationships.head(30).to_html(index=False) if not relationships.empty else "<p>No target relationships available.</p>"
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Market Research Index</title></head>
<body>
<h1>Market Research Index</h1>
<ul>
<li><a href="overview.html">Match overview</a></li>
<li><a href="summary.md">Summary markdown</a></li>
<li><a href="feature_catalog.csv">Feature catalog</a></li>
<li><a href="phase_stats.csv">Phase statistics</a></li>
<li><a href="event_study.csv">Event study (goal-centered)</a></li>
<li><a href="ml_dataset.csv">ML-ready dataset</a></li>
<li><a href="feature_target_relationships.csv">Feature/target relationships</a></li>
</ul>

<h2>Top feature/target relationships</h2>
{top_rel_html}

<h2>Feature pages</h2>
<ul>{feature_links}</ul>

<h2>Goal event pages</h2>
<ul>{event_links}</ul>
</body></html>"""
    (output_dir / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    args = parse_args()

    if not args.interval_csv.exists():
        raise FileNotFoundError(
            f"Interval CSV not found: {args.interval_csv}\n"
            "Expected by default: replay/selected_market_snapshots/selected_markets_250ms.csv"
        )
    if args.features_csv is not None and not args.features_csv.exists():
        print(f"[warn] Features CSV not found, continuing without it: {args.features_csv}")
        args.features_csv = None
    if args.goals_csv is not None and not args.goals_csv.exists():
        print(f"[warn] Goals CSV not found, continuing without it: {args.goals_csv}")
        args.goals_csv = None
    ensure_dir(args.output_dir)
    ensure_dir(args.output_dir / "features")
    ensure_dir(args.output_dir / "events")

    horizons = [int(x.strip()) for x in args.horizons_ms.split(",") if x.strip()]
    bundle = load_bundle(
        interval_csv=args.interval_csv,
        features_csv=args.features_csv,
        goals_csv=args.goals_csv,
        market_id=args.market_id,
        selection_id=args.selection_id,
    )

    research = add_targets(bundle.merged, horizons)
    catalog = build_feature_catalog(research)
    phase_stats = compute_phase_stats(research, catalog)
    event_features = [c for c in PRICE_COLUMNS_PRIORITY + PRESSURE_COLUMNS_PRIORITY + DEPTH_COLUMNS_PRIORITY + DERIVATIVE_COLUMNS_PRIORITY if c in research.columns]
    if not event_features:
        event_features = catalog["feature_name"].head(50).tolist()
    event_study = compute_event_study(research, bundle.goals, event_features, args.event_window_seconds)
    relationships = feature_target_relationships(research, catalog, top_n=args.top_mi_features)

    catalog.to_csv(args.output_dir / "feature_catalog.csv", index=False)
    phase_stats.to_csv(args.output_dir / "phase_stats.csv", index=False)
    event_study.to_csv(args.output_dir / "event_study.csv", index=False)
    research.to_csv(args.output_dir / "ml_dataset.csv", index=False)
    relationships.to_csv(args.output_dir / "feature_target_relationships.csv", index=False)

    save_summary_markdown(bundle, catalog, relationships, args.output_dir / "summary.md", horizons)
    save_overview_html(research, bundle.goals, args.output_dir / "overview.html")
    save_feature_pages(research, catalog, args.output_dir / "features", args.max_feature_pages, args.sample_rows_per_feature_plot)
    save_event_study_pages(event_study, args.output_dir / "events")
    write_index_html(args.output_dir, catalog, relationships)

    manifest = {
        "output_dir": str(args.output_dir.resolve()),
        "rows": int(len(research)),
        "columns": int(len(research.columns)),
        "features_in_catalog": int(len(catalog)),
        "phase_stats_rows": int(len(phase_stats)),
        "event_study_rows": int(len(event_study)),
        "relationships_rows": int(len(relationships)),
        "plotly_available": PLOTLY_AVAILABLE,
        "sklearn_available": SKLEARN_AVAILABLE,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
