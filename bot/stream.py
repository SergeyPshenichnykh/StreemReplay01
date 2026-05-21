from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class RunnerBook:
    selection_id: str
    runner_name: str
    best_back: tuple[float, float] | None  # (price, size)
    best_lay: tuple[float, float] | None


@dataclass(frozen=True)
class MarketSnapshot:
    tick: str
    time: str | None
    market_id: str
    market_name: str
    inplay: bool
    runners: list[RunnerBook]


class MarketDataStream:
    def snapshots(self) -> Iterable[MarketSnapshot]:  # pragma: no cover
        raise NotImplementedError
