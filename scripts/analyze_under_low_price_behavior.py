#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

import pandas as pd


DEFAULT_INPUT = Path("replay/selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv")
DEFAULT_GOALS = Path("replay/goal_timestamps.csv")
DEFAULT_OUT_DIR = Path("replay/under_low_price_research")

MARKET_RE = re.compile(r"Over/Under\s+(\d+(?:\.\d+)?)\s+Goals", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze Over/Under Under-runner behavior when price is in a low band."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--goals-csv", type=Path, default=DEFAULT_GOALS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--chunk-size", type=int, default=250_000)
    parser.add_argument("--price-col", choices=("mid_price", "best_back", "best_lay", "ltp"), default="mid_price")
    parser.add_argument("--low", type=float, default=1.05)
    parser.add_argument("--high", type=float, default=1.20)
    parser.add_argument("--max-gap-s", type=float, default=1.0)
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser.parse_args()


def setup_logging(out_dir: Path, level: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "under_low_price_behavior.log"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level))

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    return log_path


def load_goals(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["goal_number", "goal_pt", "goal_time"])
    goals = pd.read_csv(path)
    out = pd.DataFrame()
    out["goal_number"] = pd.to_numeric(goals["goal_number"], errors="coerce").astype("Int64")
    out["goal_pt"] = pd.to_numeric(goals["goal_time_estimate_pt"], errors="coerce")
    out["goal_time"] = pd.to_datetime(goals["goal_time_estimate_utc"], errors="coerce", utc=True)
    return out.dropna(subset=["goal_pt"]).sort_values("goal_pt").reset_index(drop=True)


def current_goals_from_pt(pt: pd.Series, goal_pts: list[float]) -> pd.Series:
    result = pd.Series(0, index=pt.index, dtype="int64")
    for goal_pt in goal_pts:
        result += (pt >= goal_pt).astype("int64")
    return result


def next_goal_delta_s(pt: pd.Series, goal_pts: list[float]) -> pd.Series:
    deltas = pd.Series(pd.NA, index=pt.index, dtype="Float64")
    for goal_pt in goal_pts:
        candidate = (goal_pt - pt) / 1000.0
        mask = candidate >= 0
        deltas = deltas.mask(mask & deltas.isna(), candidate)
    return deltas


def line_from_market_name(name: object) -> float | None:
    m = MARKET_RE.search(str(name))
    if not m:
        return None
    return float(m.group(1))


def load_under_rows(args: argparse.Namespace, goal_pts: list[float], final_goals: int) -> pd.DataFrame:
    header = pd.read_csv(args.input, nrows=0).columns.tolist()
    wanted = [
        "tick",
        "pt",
        "pt_utc",
        "market_id",
        "market_name",
        "market_status",
        "in_play",
        "seconds_to_start",
        "selection_id",
        "runner_name",
        "runner_status",
        "best_back",
        "best_back_size",
        "best_lay",
        "best_lay_size",
        "spread",
        "spread_ticks",
        "mid_price",
        "microprice",
        "ltp",
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
        "best_back_velocity",
        "best_lay_velocity",
        "mid_price_velocity",
        "ltp_velocity",
        "traded_volume_velocity",
    ]
    usecols = [c for c in wanted if c in header]
    logging.info("Using columns: %s", usecols)

    frames: list[pd.DataFrame] = []
    processed = 0
    under_rows = 0
    band_rows = 0

    for chunk_idx, chunk in enumerate(
        pd.read_csv(args.input, usecols=usecols, chunksize=args.chunk_size, low_memory=False),
        start=1,
    ):
        processed += len(chunk)
        mask = (
            chunk["market_name"].astype(str).str.contains("Over/Under", case=False, na=False)
            & chunk["runner_name"].astype(str).str.startswith("Under")
        )
        under = chunk.loc[mask].copy()
        under_rows += len(under)
        if under.empty:
            continue

        under["line"] = under["market_name"].map(line_from_market_name)
        under = under[under["line"].notna()].copy()
        for col in ["pt", args.price_col, "best_back", "best_lay", "mid_price", "ltp"]:
            if col in under.columns:
                under[col] = pd.to_numeric(under[col], errors="coerce")
        under = under[under[args.price_col].between(args.low, args.high, inclusive="both")].copy()
        if under.empty:
            continue

        band_rows += len(under)
        under["time"] = pd.to_datetime(under["pt_utc"], errors="coerce", utc=True)
        under["current_goals"] = current_goals_from_pt(under["pt"], goal_pts)
        under["next_goal_s"] = next_goal_delta_s(under["pt"], goal_pts)
        under["under_win_final"] = final_goals < under["line"]
        under["line_headroom_goals"] = under["line"].sub(under["current_goals"])
        under["price_used"] = under[args.price_col]
        frames.append(under)

        logging.info(
            "chunk=%d processed=%d under_rows=%d band_rows=%d",
            chunk_idx,
            processed,
            under_rows,
            band_rows,
        )

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["market_id", "selection_id", "pt"]).reset_index(drop=True)
    logging.info("Loaded band rows: %d", len(out))
    return out


def build_episodes(rows: pd.DataFrame, max_gap_s: float) -> pd.DataFrame:
    if rows.empty:
        return rows

    rows = rows.copy()
    rows["prev_pt"] = rows.groupby(["market_id", "selection_id"])["pt"].shift()
    rows["gap_s"] = (rows["pt"] - rows["prev_pt"]) / 1000.0
    rows["new_episode"] = rows["gap_s"].isna() | (rows["gap_s"] > max_gap_s)
    rows["episode_id"] = rows.groupby(["market_id", "selection_id"])["new_episode"].cumsum().astype(int)

    episode_rows = []
    for (market_id, selection_id, episode_id), g in rows.groupby(["market_id", "selection_id", "episode_id"], sort=False):
        first = g.iloc[0]
        last = g.iloc[-1]
        duration_s = (last["pt"] - first["pt"]) / 1000.0
        next_goal_s = first["next_goal_s"]
        goal_within_episode = pd.notna(next_goal_s) and float(next_goal_s) <= duration_s
        episode_rows.append(
            {
                "market_id": market_id,
                "selection_id": selection_id,
                "episode_id": episode_id,
                "market_name": first["market_name"],
                "runner_name": first["runner_name"],
                "line": first["line"],
                "start_time": first["time"],
                "end_time": last["time"],
                "start_pt": first["pt"],
                "end_pt": last["pt"],
                "duration_s": duration_s,
                "rows": len(g),
                "in_play_start": first.get("in_play"),
                "current_goals_start": first["current_goals"],
                "line_headroom_start": first["line_headroom_goals"],
                "start_price": first["price_used"],
                "end_price": last["price_used"],
                "min_price": g["price_used"].min(),
                "max_price": g["price_used"].max(),
                "mean_price": g["price_used"].mean(),
                "best_back_start": first.get("best_back"),
                "best_lay_start": first.get("best_lay"),
                "best_back_size_start": first.get("best_back_size"),
                "best_lay_size_start": first.get("best_lay_size"),
                "spread_ticks_start": first.get("spread_ticks"),
                "queue_imbalance_3_start": first.get("queue_imbalance_3"),
                "notional_imbalance_3_start": first.get("notional_imbalance_3"),
                "market_queue_imbalance_3_start": first.get("market_queue_imbalance_3"),
                "next_goal_s_from_start": next_goal_s,
                "goal_within_30s": pd.notna(next_goal_s) and float(next_goal_s) <= 30,
                "goal_within_60s": pd.notna(next_goal_s) and float(next_goal_s) <= 60,
                "goal_within_120s": pd.notna(next_goal_s) and float(next_goal_s) <= 120,
                "goal_within_300s": pd.notna(next_goal_s) and float(next_goal_s) <= 300,
                "goal_within_episode": goal_within_episode,
                "under_win_final": first["under_win_final"],
            }
        )

    return pd.DataFrame(episode_rows)


def summarize(rows: pd.DataFrame, episodes: pd.DataFrame, out_dir: Path) -> None:
    snapshot_summary = (
        rows.groupby(["market_name", "runner_name", "line"], dropna=False)
        .agg(
            band_rows=("price_used", "size"),
            first_time=("time", "min"),
            last_time=("time", "max"),
            min_price=("price_used", "min"),
            max_price=("price_used", "max"),
            mean_price=("price_used", "mean"),
            current_goals_min=("current_goals", "min"),
            current_goals_max=("current_goals", "max"),
            under_win_final=("under_win_final", "first"),
        )
        .reset_index()
    )
    snapshot_summary.to_csv(out_dir / "under_low_price_snapshot_summary_by_market.csv", index=False)

    bins = [-999, 0.5, 1.5, 2.5, 3.5, 999]
    labels = ["<=0.5", "1.5", "2.5", "3.5", ">=4.5"]
    episodes = episodes.copy()
    episodes["line_bucket"] = pd.cut(episodes["line"], bins=bins, labels=labels)

    episode_summary = (
        episodes.groupby(["line", "market_name", "runner_name"], dropna=False)
        .agg(
            episodes=("episode_id", "count"),
            total_duration_s=("duration_s", "sum"),
            median_duration_s=("duration_s", "median"),
            mean_start_price=("start_price", "mean"),
            min_start_price=("start_price", "min"),
            max_start_price=("start_price", "max"),
            current_goals_min=("current_goals_start", "min"),
            current_goals_max=("current_goals_start", "max"),
            goal_30_rate=("goal_within_30s", "mean"),
            goal_60_rate=("goal_within_60s", "mean"),
            goal_120_rate=("goal_within_120s", "mean"),
            goal_300_rate=("goal_within_300s", "mean"),
            under_win_final=("under_win_final", "first"),
        )
        .reset_index()
        .sort_values(["line", "market_name"])
    )
    episode_summary.to_csv(out_dir / "under_low_price_episode_summary_by_market.csv", index=False)

    by_goals = (
        episodes.groupby(["line", "current_goals_start"], dropna=False)
        .agg(
            episodes=("episode_id", "count"),
            total_duration_s=("duration_s", "sum"),
            median_duration_s=("duration_s", "median"),
            goal_30_rate=("goal_within_30s", "mean"),
            goal_60_rate=("goal_within_60s", "mean"),
            goal_120_rate=("goal_within_120s", "mean"),
            goal_300_rate=("goal_within_300s", "mean"),
            under_win_final=("under_win_final", "mean"),
        )
        .reset_index()
        .sort_values(["line", "current_goals_start"])
    )
    by_goals.to_csv(out_dir / "under_low_price_episode_summary_by_current_goals.csv", index=False)

    top_long = episodes.sort_values("duration_s", ascending=False).head(100)
    top_long.to_csv(out_dir / "under_low_price_top_longest_episodes.csv", index=False)


def main() -> None:
    args = parse_args()
    log_path = setup_logging(args.out_dir, args.log_level)
    logging.info("Log path: %s", log_path)
    logging.info("Args: %s", vars(args))

    goals = load_goals(args.goals_csv)
    goal_pts = goals["goal_pt"].dropna().tolist()
    final_goals = len(goal_pts)
    logging.info("Loaded goals: %s", goals.to_dict("records"))
    logging.info("Final goals inferred from goals CSV: %d", final_goals)

    rows = load_under_rows(args, goal_pts, final_goals)
    if rows.empty:
        logging.warning("No Under rows found in price band")
        return

    band_path = args.out_dir / "under_low_price_band_snapshots.csv"
    rows.to_csv(band_path, index=False)
    logging.info("Wrote %s rows=%d", band_path, len(rows))

    episodes = build_episodes(rows, args.max_gap_s)
    episodes_path = args.out_dir / "under_low_price_episodes.csv"
    episodes.to_csv(episodes_path, index=False)
    logging.info("Wrote %s rows=%d", episodes_path, len(episodes))

    summarize(rows, episodes, args.out_dir)
    logging.info("Wrote summary CSV files")

    logging.info("Snapshot count in band: %d", len(rows))
    logging.info("Episode count in band: %d", len(episodes))
    logging.info("Markets in band: %s", sorted(rows["market_name"].dropna().unique().tolist()))
    logging.info(
        "Episodes by line: %s",
        episodes.groupby("line")["episode_id"].count().to_dict(),
    )
    logging.info(
        "Goal within 300s by line: %s",
        episodes.groupby("line")["goal_within_300s"].mean().round(4).to_dict(),
    )


if __name__ == "__main__":
    main()
