#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except Exception:
    go = None
    make_subplots = None

INTERVAL_CSV = Path("replay/selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv")
FEATURES_CSV = Path("replay/match_odds_features.csv")
GOALS_CSV = Path("replay/goal_timestamps.csv")
OUTPUT_DIR = Path("replay/research_output_v3")

CHUNK_SIZE = 100_000
EVENT_WINDOW_SECONDS = 60
HORIZONS_MS = [250, 500, 1000, 2000, 5000]
OVERVIEW_FEATURES = [
    "best_back", "best_lay", "mid_price", "microprice", "ltp",
    "spread_ticks", "spread",
    "queue_imbalance_1", "queue_imbalance_3",
    "notional_imbalance_1", "notional_imbalance_3",
    "back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3",
    "traded_volume", "market_back_book_pct", "market_lay_book_pct",
    "market_queue_imbalance_3", "market_notional_imbalance_3",
]
MAX_FEATURE_PAGES_TOTAL = 120
FEATURE_BATCH_SIZE = 20

CSV_DTYPES = {
    "favourite_runner_name": "string",
    "runner_name": "string",
    "market_name": "string",
    "event_name": "string",
    "market_status": "string",
    "runner_status": "string",
    "snapshot_series": "string",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name))[:180]


def detect_time_col(cols: Sequence[str]) -> str:
    for c in ["pt_utc", "publish_time_utc", "timestamp_utc", "time_utc"]:
        if c in cols:
            return c
    for c in ["pt", "publish_time", "timestamp", "time"]:
        if c in cols:
            return c
    raise KeyError("No time column found")


def detect_optional(cols: Sequence[str], names: Sequence[str]) -> Optional[str]:
    for c in names:
        if c in cols:
            return c
    return None


def parse_time_series(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    out = df.copy()
    if time_col == "pt_utc":
        out["time"] = pd.to_datetime(out[time_col], errors="coerce", utc=True)
    elif pd.api.types.is_numeric_dtype(out[time_col]):
        vals = pd.to_numeric(out[time_col], errors="coerce")
        med = vals.dropna().median() if vals.notna().any() else np.nan
        unit = "ms" if pd.notna(med) and med > 10_000_000_000 else "s"
        out["time"] = pd.to_datetime(vals, errors="coerce", utc=True, unit=unit)
    else:
        out["time"] = pd.to_datetime(out[time_col], errors="coerce", utc=True)
    return out.dropna(subset=["time"])


def read_goals(goals_csv: Path) -> pd.DataFrame:
    if not goals_csv.exists():
        log(f"[warn] Goals CSV not found, continuing without it: {goals_csv}")
        return pd.DataFrame(columns=["goal_time"])
    goals = pd.read_csv(goals_csv)
    if goals.empty:
        return pd.DataFrame(columns=["goal_time"])

    time_col = None
    for c in [
        "goal_time_estimate_pt",
        "goal_time_estimate_utc",
        "goal_time",
        "publish_time",
        "pt_utc",
        "pt",
    ]:
        if c in goals.columns:
            time_col = c
            break

    if time_col is None:
        log(f"[warn] Could not detect goal time column in {goals_csv}")
        return pd.DataFrame(columns=["goal_time"])

    series = goals[time_col]
    if pd.api.types.is_numeric_dtype(series):
        vals = pd.to_numeric(series, errors="coerce")
        med = vals.dropna().median() if vals.notna().any() else np.nan
        unit = "ms" if pd.notna(med) and med > 10_000_000_000 else "s"
        goals["goal_time"] = pd.to_datetime(vals, errors="coerce", utc=True, unit=unit)
    else:
        goals["goal_time"] = pd.to_datetime(series, errors="coerce", utc=True)

    out = goals.dropna(subset=["goal_time"]).sort_values("goal_time").copy()
    log(f"[info] Loaded {len(out):,} goal markers using column: {time_col}")
    return out


def sample_evenly(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if len(df) <= max_points:
        return df
    idx = np.linspace(0, len(df) - 1, num=max_points, dtype=int)
    return df.iloc[idx].copy()


def choose_primary_runner(df: pd.DataFrame, selection_col: Optional[str]) -> Optional[str]:
    if selection_col is None or selection_col not in df.columns or df.empty:
        return None
    if "is_favourite" in df.columns:
        fav = df[df["is_favourite"].fillna(False) == True]
        if not fav.empty:
            return str(fav.iloc[0][selection_col])
    if "rank_by_best_back" in df.columns:
        ranked = df.sort_values(["rank_by_best_back", "time"], na_position="last")
        if not ranked.empty:
            return str(ranked.iloc[0][selection_col])
    vc = df[selection_col].astype(str).value_counts()
    return str(vc.index[0]) if not vc.empty else None


def build_phase_flags(df: pd.DataFrame, goals: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["phase"] = "unknown"
    if "in_play" in out.columns:
        mask = out["in_play"].fillna(False).astype(bool)
        out.loc[~mask, "phase"] = "pre_play"
        out.loc[mask, "phase"] = "in_play"
    if "market_status" in out.columns:
        ms = out["market_status"].astype(str)
        out.loc[ms.eq("SUSPENDED"), "phase"] = "suspended"
        out.loc[ms.eq("CLOSED"), "phase"] = "closed"
    if not goals.empty:
        for gt in goals["goal_time"]:
            pre = (out["time"] >= gt - pd.Timedelta(seconds=30)) & (out["time"] < gt)
            post = (out["time"] >= gt) & (out["time"] <= gt + pd.Timedelta(seconds=60))
            out.loc[pre & out["phase"].eq("in_play"), "phase"] = "pre_goal_window"
            out.loc[post, "phase"] = "post_goal_window"
    return out


@dataclass
class RunningStats:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    min: float = math.inf
    max: float = -math.inf
    nan_count: int = 0

    def update(self, arr: np.ndarray) -> None:
        if arr.size == 0:
            return
        finite = arr[np.isfinite(arr)]
        self.nan_count += int(arr.size - finite.size)
        if finite.size == 0:
            return
        local_n = finite.size
        local_mean = float(np.mean(finite))
        local_m2 = float(np.sum((finite - local_mean) ** 2))
        self.min = min(self.min, float(np.min(finite)))
        self.max = max(self.max, float(np.max(finite)))
        if self.n == 0:
            self.n = local_n
            self.mean = local_mean
            self.m2 = local_m2
            return
        delta = local_mean - self.mean
        total_n = self.n + local_n
        self.mean = self.mean + delta * local_n / total_n
        self.m2 = self.m2 + local_m2 + delta * delta * self.n * local_n / total_n
        self.n = total_n

    def as_dict(self) -> Dict[str, float]:
        std = math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else np.nan
        return {"count": self.n, "mean": self.mean if self.n else np.nan, "std": std,
                "min": self.min if self.n else np.nan, "max": self.max if self.n else np.nan,
                "nan_count": self.nan_count}


def detect_meta(csv_path: Path) -> Dict[str, object]:
    head = pd.read_csv(csv_path, nrows=200)
    cols = list(head.columns)
    time_col = detect_time_col(cols)
    return {
        "time_col": time_col,
        "market_id_col": detect_optional(cols, ["market_id", "marketId"]),
        "selection_id_col": detect_optional(cols, ["selection_id", "selectionId"]),
        "runner_name_col": detect_optional(cols, ["runner_name", "selection_name", "name"]),
        "market_status_col": detect_optional(cols, ["market_status"]),
        "in_play_col": detect_optional(cols, ["in_play"]),
        "runner_status_col": detect_optional(cols, ["runner_status"]),
        "all_cols": cols,
    }


def write_overview_html(df: pd.DataFrame, goals: pd.DataFrame, out_html: Path) -> None:
    if go is None or make_subplots is None or df.empty:
        return
    panels = [
        [c for c in ["best_back", "best_lay", "mid_price", "microprice", "ltp"] if c in df.columns],
        [c for c in ["spread_ticks", "spread"] if c in df.columns],
        [c for c in ["queue_imbalance_1", "queue_imbalance_3", "notional_imbalance_1", "notional_imbalance_3"] if c in df.columns],
        [c for c in ["back_depth_1", "lay_depth_1", "back_depth_3", "lay_depth_3", "traded_volume"] if c in df.columns],
    ]
    panels = [p for p in panels if p]
    if not panels:
        return
    fig = make_subplots(rows=len(panels), cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        subplot_titles=[", ".join(p) for p in panels])
    for r, cols in enumerate(panels, start=1):
        for c in cols:
            fig.add_trace(go.Scattergl(x=df["time"], y=df[c], name=c, mode="lines"), row=r, col=1)
    if "goal_time" in goals.columns:
        for gt in goals["goal_time"]:
            fig.add_vline(x=gt, line_dash="dash", annotation_text="GOAL", opacity=0.6)
    fig.update_layout(height=300 * len(panels), title="Market research overview", hovermode="x unified")
    fig.write_html(str(out_html), include_plotlyjs="cdn")


def write_feature_html(df: pd.DataFrame, feature: str, goals: pd.DataFrame, out_html: Path) -> None:
    if go is None or make_subplots is None or df.empty or feature not in df.columns:
        return
    fig = make_subplots(rows=2, cols=1, subplot_titles=[f"{feature} over time", f"{feature} distribution"])
    fig.add_trace(go.Scattergl(x=df["time"], y=df[feature], name=feature, mode="lines"), row=1, col=1)
    if "goal_time" in goals.columns:
        for gt in goals["goal_time"]:
            fig.add_vline(x=gt, line_dash="dash", annotation_text="GOAL", opacity=0.5, row=1, col=1)
    vals = pd.to_numeric(df[feature], errors="coerce")
    vals = vals[np.isfinite(vals)]
    if len(vals):
        fig.add_trace(go.Histogram(x=vals, name=feature, nbinsx=60), row=2, col=1)
    fig.update_layout(height=800, title=feature, hovermode="x unified")
    fig.write_html(str(out_html), include_plotlyjs="cdn")


def estimate_rows(csv_path: Path) -> int:
    log("[PASS 0] Counting rows...")
    n = 0
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        next(f, None)
        for _ in f:
            n += 1
    return n


def pass1_catalog_and_sample(csv_path: Path, goals: pd.DataFrame, out_dir: Path, meta: Dict[str, object]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    log("[PASS 1] Sampling timeline and collecting catalog stats...")
    time_col = meta["time_col"]
    selection_col = meta["selection_id_col"]
    market_col = meta["market_id_col"]

    sample_parts: List[pd.DataFrame] = []
    stats: Dict[str, RunningStats] = {}
    phase_rows: List[Dict[str, object]] = []

    for i, chunk in enumerate(pd.read_csv(csv_path, chunksize=CHUNK_SIZE, low_memory=True, dtype=CSV_DTYPES)):
        chunk = parse_time_series(chunk, time_col)
        if chunk.empty:
            continue
        chunk = build_phase_flags(chunk, goals)

        exclude = {"time", time_col, selection_col or "", market_col or "", meta["runner_name_col"] or "",
                   meta["market_status_col"] or "", meta["in_play_col"] or "", meta["runner_status_col"] or "", "phase"}
        numeric_cols = [c for c in chunk.columns if c not in exclude and pd.api.types.is_numeric_dtype(chunk[c])]
        for c in numeric_cols:
            stats.setdefault(c, RunningStats()).update(pd.to_numeric(chunk[c], errors="coerce").to_numpy(dtype=float))

        keep = ["time", "phase"] + [c for c in OVERVIEW_FEATURES if c in chunk.columns]
        for c in [selection_col, market_col, meta["runner_name_col"], meta["market_status_col"], meta["in_play_col"]]:
            if c and c in chunk.columns:
                keep.append(c)
        keep = list(dict.fromkeys(keep))
        sample_parts.append(sample_evenly(chunk[keep], 3000))

        row = {"rows": len(chunk)}
        for phase, sub in chunk.groupby("phase", dropna=False):
            row2 = {"phase": phase, "rows": len(sub)}
            for feat in [c for c in OVERVIEW_FEATURES if c in sub.columns]:
                vals = pd.to_numeric(sub[feat], errors="coerce")
                row2[f"{feat}_mean"] = float(vals.mean()) if vals.notna().any() else np.nan
            phase_rows.append(row2)

        if (i + 1) % 10 == 0:
            log(f"[PASS 1] processed {(i + 1) * CHUNK_SIZE:,}+ rows")

    sample_df = pd.concat(sample_parts, ignore_index=True) if sample_parts else pd.DataFrame()
    if not sample_df.empty:
        sample_df = sample_df.sort_values("time")
        chosen = choose_primary_runner(sample_df, selection_col)
        if chosen is not None and selection_col in sample_df.columns:
            sub = sample_df[sample_df[selection_col].astype(str) == chosen]
            if not sub.empty:
                sample_df = sub.copy()
        sample_df = sample_evenly(sample_df, 8000)
        sample_df.to_parquet(out_dir / "overview_sample.parquet", index=False)

    cat_rows = []
    for feat, st in stats.items():
        rec = {"feature": feat}
        rec.update(st.as_dict())
        cat_rows.append(rec)
    catalog = pd.DataFrame(cat_rows).sort_values(["count", "feature"], ascending=[False, True]) if cat_rows else pd.DataFrame()
    if not catalog.empty:
        catalog.to_csv(out_dir / "feature_catalog.csv", index=False)

    phase_df = pd.DataFrame(phase_rows)
    if not phase_df.empty:
        phase_df.groupby("phase", dropna=False).mean(numeric_only=True).reset_index().to_csv(out_dir / "phase_stats.csv", index=False)

    return sample_df, catalog


def pass2_event_study(csv_path: Path, goals: pd.DataFrame, out_dir: Path, meta: Dict[str, object]) -> None:
    log("[PASS 2] Event study...")
    if goals.empty:
        log("[PASS 2] No goals CSV content, skipping")
        return
    time_col = meta["time_col"]
    selection_col = meta["selection_id_col"]
    cols_needed = [time_col] + [c for c in OVERVIEW_FEATURES if c in meta["all_cols"]]
    if selection_col:
        cols_needed.append(selection_col)
    parts = []
    for i, chunk in enumerate(pd.read_csv(csv_path, chunksize=CHUNK_SIZE, usecols=lambda c: c in cols_needed, low_memory=True, dtype=CSV_DTYPES)):
        chunk = parse_time_series(chunk, time_col)
        if chunk.empty:
            continue
        if selection_col and selection_col in chunk.columns:
            chosen = choose_primary_runner(chunk, selection_col)
            if chosen is not None:
                sub = chunk[chunk[selection_col].astype(str) == chosen]
                if not sub.empty:
                    chunk = sub
        for gt in goals["goal_time"]:
            mask = (chunk["time"] >= gt - pd.Timedelta(seconds=EVENT_WINDOW_SECONDS)) & (chunk["time"] <= gt + pd.Timedelta(seconds=EVENT_WINDOW_SECONDS))
            sub = chunk.loc[mask].copy()
            if sub.empty:
                continue
            sub["seconds_from_goal"] = (sub["time"] - gt).dt.total_seconds().round().astype(int)
            parts.append(sub)
        if (i + 1) % 10 == 0:
            log(f"[PASS 2] processed {(i + 1) * CHUNK_SIZE:,}+ rows")
    if not parts:
        return
    ev = pd.concat(parts, ignore_index=True)
    agg_cols = [c for c in OVERVIEW_FEATURES if c in ev.columns]
    if not agg_cols:
        return
    res = ev.groupby("seconds_from_goal")[agg_cols].mean(numeric_only=True).reset_index()
    res.to_csv(out_dir / "events" / "event_study.csv", index=False)
    if go is not None:
        for feat in agg_cols:
            fig = go.Figure()
            fig.add_trace(go.Scattergl(x=res["seconds_from_goal"], y=res[feat], mode="lines", name=feat))
            fig.add_vline(x=0, line_dash="dash", annotation_text="GOAL")
            fig.update_layout(title=f"{feat} around goals", xaxis_title="Seconds from goal", yaxis_title=feat)
            fig.write_html(str(out_dir / "events" / f"{sanitize_filename(feat)}_around_goals.html"), include_plotlyjs="cdn")


def pass3_ml(csv_path: Path, out_dir: Path, meta: Dict[str, object]) -> None:
    log("[PASS 3] ML dataset...")
    time_col = meta["time_col"]
    selection_col = meta["selection_id_col"]
    cols_needed = [time_col] + [c for c in OVERVIEW_FEATURES if c in meta["all_cols"]]
    for c in [selection_col, meta["market_id_col"], meta["runner_name_col"], meta["market_status_col"], meta["in_play_col"]]:
        if c:
            cols_needed.append(c)
    cols_needed = list(dict.fromkeys(cols_needed))
    parts = []
    for i, chunk in enumerate(pd.read_csv(csv_path, chunksize=CHUNK_SIZE, usecols=lambda c: c in cols_needed, low_memory=True, dtype=CSV_DTYPES)):
        chunk = parse_time_series(chunk, time_col)
        if chunk.empty:
            continue
        parts.append(sample_evenly(chunk, 20000))
        if (i + 1) % 10 == 0:
            log(f"[PASS 3] sampled {(i + 1) * CHUNK_SIZE:,}+ rows")
    if not parts:
        return
    df = pd.concat(parts, ignore_index=True).sort_values("time")
    if selection_col and selection_col in df.columns:
        chosen = choose_primary_runner(df, selection_col)
        if chosen is not None:
            sub = df[df[selection_col].astype(str) == chosen]
            if not sub.empty:
                df = sub.copy()
    df = sample_evenly(df, 50000).sort_values("time").reset_index(drop=True)
    price_col = "mid_price" if "mid_price" in df.columns else ("ltp" if "ltp" in df.columns else None)
    if price_col is None:
        df.to_csv(out_dir / "ml_dataset.csv", index=False)
        return
    time_ns = df["time"].astype("int64")
    price = pd.to_numeric(df[price_col], errors="coerce")
    arr_t = time_ns.to_numpy()
    arr_p = price.to_numpy(dtype=float)
    for h in HORIZONS_MS:
        future_t = arr_t + h * 1_000_000
        idx = np.searchsorted(arr_t, future_t, side="left")
        fut = np.full(len(df), np.nan, dtype=float)
        valid = idx < len(df)
        fut[valid] = arr_p[idx[valid]]
        df[f"future_{price_col}_{h}ms"] = fut
        df[f"target_{price_col}_delta_{h}ms"] = fut - arr_p
    df.to_csv(out_dir / "ml_dataset.csv", index=False)

    rel_rows = []
    targets = [c for c in df.columns if c.startswith("target_")]
    for feat in [c for c in OVERVIEW_FEATURES if c in df.columns]:
        x = pd.to_numeric(df[feat], errors="coerce")
        for tgt in targets:
            y = pd.to_numeric(df[tgt], errors="coerce")
            sub = pd.DataFrame({"x": x, "y": y}).dropna()
            if len(sub) >= 10:
                rel_rows.append({"feature": feat, "target": tgt, "corr": sub["x"].corr(sub["y"]), "n": len(sub)})
    if rel_rows:
        pd.DataFrame(rel_rows).sort_values(["target", "corr"], ascending=[True, False]).to_csv(out_dir / "feature_target_relationships.csv", index=False)


def pass4_feature_pages(csv_path: Path, out_dir: Path, meta: Dict[str, object], goals: pd.DataFrame, catalog: pd.DataFrame) -> None:
    log("[PASS 4] Feature pages by batches...")
    if go is None or catalog.empty:
        log("[PASS 4] Skipped")
        return
    features = [f for f in catalog["feature"].tolist() if "_acceleration" not in f and "_velocity" not in f][:MAX_FEATURE_PAGES_TOTAL]
    if not features:
        return
    time_col = meta["time_col"]
    selection_col = meta["selection_id_col"]
    index_rows = []
    for bi in range(0, len(features), FEATURE_BATCH_SIZE):
        batch = features[bi:bi + FEATURE_BATCH_SIZE]
        log(f"[PASS 4] batch {bi // FEATURE_BATCH_SIZE + 1} / {math.ceil(len(features)/FEATURE_BATCH_SIZE)}")
        cols_needed = [time_col] + batch
        for c in [selection_col, meta["market_id_col"], meta["runner_name_col"], meta["market_status_col"], meta["in_play_col"]]:
            if c:
                cols_needed.append(c)
        cols_needed = list(dict.fromkeys(cols_needed))
        parts = []
        for i, chunk in enumerate(pd.read_csv(csv_path, chunksize=CHUNK_SIZE, usecols=lambda c: c in cols_needed, low_memory=True, dtype=CSV_DTYPES)):
            chunk = parse_time_series(chunk, time_col)
            if chunk.empty:
                continue
            parts.append(sample_evenly(chunk, 4000))
            if (i + 1) % 10 == 0:
                log(f"[PASS 4] batch sampled {(i + 1) * CHUNK_SIZE:,}+ rows")
        if not parts:
            continue
        df = pd.concat(parts, ignore_index=True).sort_values("time")
        if selection_col and selection_col in df.columns:
            chosen = choose_primary_runner(df, selection_col)
            if chosen is not None:
                sub = df[df[selection_col].astype(str) == chosen]
                if not sub.empty:
                    df = sub.copy()
        df = sample_evenly(df, 8000)
        for feat in batch:
            if feat not in df.columns:
                continue
            out_html = out_dir / "features" / f"{sanitize_filename(feat)}.html"
            try:
                write_feature_html(df[["time", feat]].copy(), feat, goals, out_html)
                index_rows.append({"feature": feat, "file": f"features/{sanitize_filename(feat)}.html"})
            except Exception as e:
                log(f"[warn] failed feature page {feat}: {e}")
    if index_rows:
        pd.DataFrame(index_rows).to_csv(out_dir / "feature_pages_index.csv", index=False)


def write_index_html(out_dir: Path) -> None:
    html = '''<!doctype html>
<html><head><meta charset="utf-8"><title>Market Research Output v3</title></head>
<body>
<h1>Market Research Output v3</h1>
<ul>
  <li><a href="overview.html">overview.html</a></li>
  <li><a href="feature_catalog.csv">feature_catalog.csv</a></li>
  <li><a href="phase_stats.csv">phase_stats.csv</a></li>
  <li><a href="ml_dataset.csv">ml_dataset.csv</a></li>
  <li><a href="feature_target_relationships.csv">feature_target_relationships.csv</a></li>
  <li><a href="feature_pages_index.csv">feature_pages_index.csv</a></li>
  <li><a href="events/event_study.csv">events/event_study.csv</a></li>
</ul>
</body></html>'''
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main() -> None:
    safe_mkdir(OUTPUT_DIR)
    safe_mkdir(OUTPUT_DIR / "features")
    safe_mkdir(OUTPUT_DIR / "events")

    if not INTERVAL_CSV.exists():
        raise FileNotFoundError(f"Interval CSV not found: {INTERVAL_CSV}")
    if not FEATURES_CSV.exists():
        log(f"[warn] Features CSV not found, continuing without it: {FEATURES_CSV}")
    if not GOALS_CSV.exists():
        log(f"[warn] Goals CSV not found, continuing without it: {GOALS_CSV}")

    meta = detect_meta(INTERVAL_CSV)
    goals = read_goals(GOALS_CSV)
    row_count = estimate_rows(INTERVAL_CSV)
    (OUTPUT_DIR / "run_meta.json").write_text(json.dumps({
        "interval_csv": str(INTERVAL_CSV),
        "features_csv": str(FEATURES_CSV),
        "goals_csv": str(GOALS_CSV),
        "output_dir": str(OUTPUT_DIR),
        "row_count_estimate": row_count,
        "chunk_size": CHUNK_SIZE,
        "detected_time_col": meta["time_col"],
    }, indent=2), encoding="utf-8")

    sample_df, catalog = pass1_catalog_and_sample(INTERVAL_CSV, goals, OUTPUT_DIR, meta)
    if not sample_df.empty:
        log("[PASS 1b] Writing overview...")
        write_overview_html(sample_df, goals, OUTPUT_DIR / "overview.html")
    pass2_event_study(INTERVAL_CSV, goals, OUTPUT_DIR, meta)
    pass3_ml(INTERVAL_CSV, OUTPUT_DIR, meta)
    pass4_feature_pages(INTERVAL_CSV, OUTPUT_DIR, meta, goals, catalog)
    write_index_html(OUTPUT_DIR)
    log(f"[done] Wrote output to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
