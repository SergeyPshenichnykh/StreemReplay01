import pandas as pd
from pathlib import Path
import plotly.graph_objects as go

CSV_PATH = "replay/selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv"
OUT_DIR = Path("replay/research_output_v4_simple")
OUT_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE = 200_000

TIME_CANDIDATES = [
    "time",
    "snapshot_pt_utc",
    "pt_utc",
    "snapshot_pt",
    "pt",
]

BASE_COLS = [
    "market_id",
    "selection_id",
    "mid_price",
    "ltp",
    "queue_imbalance_3",
    "notional_imbalance_3",
    "back_depth_3",
    "lay_depth_3",
    "runner_name",
    "market_status",
    "in_play",
]


def detect_time_col(columns):
    for c in TIME_CANDIDATES:
        if c in columns:
            return c
    raise ValueError(f"No supported time column found. Columns: {list(columns)}")


def normalize_time(df, time_col):
    if time_col in ("snapshot_pt", "pt"):
        return pd.to_datetime(df[time_col], unit="ms", errors="coerce")
    return pd.to_datetime(df[time_col], errors="coerce")


def save_runner_csvs():
    print("[PASS 1] Splitting by market_id + selection_id")

    first = True
    time_col = None

    for chunk in pd.read_csv(CSV_PATH, chunksize=CHUNK_SIZE, low_memory=True):
        if first:
            time_col = detect_time_col(chunk.columns)
            print(f"[info] Using time column: {time_col}")
            first = False

        keep = [c for c in BASE_COLS if c in chunk.columns]
        keep = [time_col] + keep
        chunk = chunk[keep].copy()

        chunk["time"] = normalize_time(chunk, time_col)
        chunk = chunk.drop(columns=[time_col], errors="ignore")

        chunk = chunk.dropna(subset=["time", "market_id", "selection_id"])

        for (m, s), g in chunk.groupby(["market_id", "selection_id"], sort=False):
            out_dir = OUT_DIR / "data" / str(m)
            out_dir.mkdir(parents=True, exist_ok=True)

            f = out_dir / f"{s}.csv"
            g.to_csv(f, mode="a", header=not f.exists(), index=False)

    print("[PASS 1] done")


def build_html():
    print("[PASS 2] Building HTML")

    base = OUT_DIR / "data"
    rendered_base = OUT_DIR / "rendered"
    rendered_base.mkdir(parents=True, exist_ok=True)

    top_index_links = []

    for market_dir in sorted(base.glob("*")):
        market_id = market_dir.name
        render_dir = rendered_base / market_id
        render_dir.mkdir(parents=True, exist_ok=True)

        index_links = []
        market_series = []

        for f in sorted(market_dir.glob("*.csv")):
            df = pd.read_csv(f)
            if len(df) < 20:
                continue

            df["time"] = pd.to_datetime(df["time"], errors="coerce")
            df = df.dropna(subset=["time"]).sort_values("time")

            runner_name = None
            if "runner_name" in df.columns:
                vals = df["runner_name"].dropna().astype(str).unique()
                runner_name = vals[0] if len(vals) else None

            # runner prices
            fig = go.Figure()
            if "mid_price" in df.columns:
                fig.add_trace(go.Scatter(x=df["time"], y=df["mid_price"], name="mid_price"))
            if "ltp" in df.columns:
                fig.add_trace(go.Scatter(x=df["time"], y=df["ltp"], name="ltp"))

            title = f"Market {market_id} — Runner {f.stem}"
            if runner_name:
                title += f" — {runner_name}"
            fig.update_layout(title=title, xaxis_title="Time", yaxis_title="Price")

            out_file = render_dir / f"{f.stem}.html"
            fig.write_html(out_file)

            # runner pressure
            fig2 = go.Figure()
            if "queue_imbalance_3" in df.columns:
                fig2.add_trace(go.Scatter(x=df["time"], y=df["queue_imbalance_3"], name="queue_imbalance_3"))
            if "notional_imbalance_3" in df.columns:
                fig2.add_trace(go.Scatter(x=df["time"], y=df["notional_imbalance_3"], name="notional_imbalance_3"))
            fig2.update_layout(title=f"{title} — Pressure", xaxis_title="Time", yaxis_title="Imbalance")
            fig2.write_html(render_dir / f"{f.stem}_pressure.html")

            # runner depth
            fig3 = go.Figure()
            if "back_depth_3" in df.columns:
                fig3.add_trace(go.Scatter(x=df["time"], y=df["back_depth_3"], name="back_depth_3"))
            if "lay_depth_3" in df.columns:
                fig3.add_trace(go.Scatter(x=df["time"], y=df["lay_depth_3"], name="lay_depth_3"))
            fig3.update_layout(title=f"{title} — Depth", xaxis_title="Time", yaxis_title="Depth")
            fig3.write_html(render_dir / f"{f.stem}_depth.html")

            label = f.stem if not runner_name else f"{f.stem} — {runner_name}"
            index_links.append(
                f'<li>{label}: '
                f'<a href="{f.stem}.html">prices</a> | '
                f'<a href="{f.stem}_pressure.html">pressure</a> | '
                f'<a href="{f.stem}_depth.html">depth</a></li>'
            )

            if "mid_price" in df.columns:
                market_series.append((label, df[["time", "mid_price"]].copy()))

        # market overview
        if market_series:
            figm = go.Figure()
            for label, d in market_series:
                figm.add_trace(go.Scatter(x=d["time"], y=d["mid_price"], name=label))
            figm.update_layout(
                title=f"Market {market_id} — All runners mid_price",
                xaxis_title="Time",
                yaxis_title="mid_price",
            )
            figm.write_html(render_dir / "market_overview.html")

        index_html = f"""
        <h1>Market {market_id}</h1>
        <ul>
        <li><a href="market_overview.html">Market overview</a></li>
        {''.join(index_links)}
        </ul>
        """
        (render_dir / "index.html").write_text(index_html, encoding="utf-8")

        top_index_links.append(f'<li><a href="{market_id}/index.html">{market_id}</a></li>')

    top_html = f"""
    <h1>Research Output V4 Simple</h1>
    <ul>
    {''.join(top_index_links)}
    </ul>
    """
    (rendered_base / "index.html").write_text(top_html, encoding="utf-8")

    print("[PASS 2] done")


def main():
    save_runner_csvs()
    build_html()
    print("[DONE]")


if __name__ == "__main__":
    main()
