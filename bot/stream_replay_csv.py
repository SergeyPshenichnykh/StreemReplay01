from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bot.stream import MarketDataStream, MarketSnapshot, RunnerBook


@dataclass(frozen=True)
class ReplayCsvOptions:
    path: Path
    max_rows: int = 0


class ReplayCsvStream(MarketDataStream):
    """Reads snapshots from a replay CSV like `selected_markets_250ms.csv`.

    Expected columns (aliases handled):
    - tick
    - market_id, market_name
    - selection_id, runner_name
    - best_back, best_back_size
    - best_lay, best_lay_size
    """

    def __init__(self, opts: ReplayCsvOptions):
        self.opts = opts

    def snapshots(self) -> Iterable[MarketSnapshot]:
        path = self.opts.path
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                return

            def col(*names: str) -> str | None:
                for n in names:
                    if n in reader.fieldnames:
                        return n
                return None

            tick_col = col("tick")
            time_col = col("pt_utc", "snapshot_pt_utc", "pt", "snapshot_pt")
            market_id_col = col("market_id")
            market_name_col = col("market_name")
            selection_id_col = col("selection_id")
            runner_name_col = col("runner_name")
            bb_col = col("best_back")
            bb_size_col = col("best_back_size", "back_size_1")
            bl_col = col("best_lay")
            bl_size_col = col("best_lay_size", "lay_size_1")

            if not (market_id_col and market_name_col and selection_id_col and runner_name_col and bb_col and bl_col):
                raise RuntimeError(f"Replay CSV missing required columns; found: {reader.fieldnames}")

            current_key: tuple[str, str] | None = None
            runners: list[RunnerBook] = []
            market_id = ""
            market_name = ""
            inplay = False
            tick_value: str = ""
            time_value: str | None = None

            rows = 0
            for row in reader:
                rows += 1
                if self.opts.max_rows and rows > self.opts.max_rows:
                    break

                tick = (row.get(tick_col) if tick_col else "") or str(rows)
                mid = (row.get(market_id_col) or "").strip()
                if not mid:
                    continue
                key = (mid, tick)

                if current_key is None:
                    current_key = key
                    market_id = mid
                    market_name = (row.get(market_name_col) or "").strip()
                    tick_value = tick
                    time_value = (row.get(time_col) or "").strip() if time_col else None
                    runners = []
                elif key != current_key:
                    yield MarketSnapshot(
                        tick=tick_value,
                        time=time_value,
                        market_id=market_id,
                        market_name=market_name,
                        inplay=inplay,
                        runners=runners,
                    )
                    current_key = key
                    market_id = mid
                    market_name = (row.get(market_name_col) or "").strip()
                    tick_value = tick
                    time_value = (row.get(time_col) or "").strip() if time_col else None
                    runners = []

                sel = (row.get(selection_id_col) or "").strip()
                rname = (row.get(runner_name_col) or "").strip() or sel

                def fnum(v: str | None) -> float | None:
                    if v is None:
                        return None
                    v = v.strip()
                    if not v:
                        return None
                    try:
                        return float(v)
                    except ValueError:
                        return None

                bb = fnum(row.get(bb_col))
                bb_size = fnum(row.get(bb_size_col)) if bb_size_col else None
                bl = fnum(row.get(bl_col))
                bl_size = fnum(row.get(bl_size_col)) if bl_size_col else None

                runners.append(
                    RunnerBook(
                        selection_id=sel,
                        runner_name=rname,
                        best_back=None if bb is None else (bb, float(bb_size or 0.0)),
                        best_lay=None if bl is None else (bl, float(bl_size or 0.0)),
                    )
                )

            if current_key is not None and runners:
                yield MarketSnapshot(
                    tick=tick_value,
                    time=time_value,
                    market_id=market_id,
                    market_name=market_name,
                    inplay=inplay,
                    runners=runners,
                )
