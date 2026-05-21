from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class MarketSpec:
    market_id: str
    market_name_regex: str | None = None
    min_legs: int = 2


@dataclass(frozen=True)
class RiskLimits:
    max_orders_per_market: int = 1
    max_total_stake_per_market: float = 100.0
    max_total_stake_per_event: float = 200.0
    max_total_stake_per_day: float = 500.0


@dataclass(frozen=True)
class DutchingSignal:
    odds_sources: list[str] = None  # set in __post_init__
    min_margin_pct: float = 0.2
    min_size: float = 10.0
    min_legs: int = 2
    min_odds: float = 1.01
    max_odds: float = 1000.0

    def __post_init__(self) -> None:
        # Back-dutching uses best_lay (we BACK at lay), lay-dutching uses best_back (we LAY at back).
        if self.odds_sources is None:
            object.__setattr__(self, "odds_sources", ["best_lay"])


@dataclass(frozen=True)
class Staking:
    method: str = "fixed-stake"  # "fixed-stake" | "target-profit" | "minimum-stake"
    total_stake: float = 20.0
    target_profit: float = 2.0
    min_stake: float = 2.0
    stake_decimals: int = 2


@dataclass(frozen=True)
class BotConfig:
    markets: list[MarketSpec]
    signal: DutchingSignal = DutchingSignal()
    staking: Staking = Staking()
    risk: RiskLimits = RiskLimits()
    poll_interval_s: float = 0.25
    dry_run: bool = True


def load_config(path: Path) -> BotConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))

    markets_raw = raw.get("markets", [])
    markets: list[MarketSpec] = []
    for m in markets_raw:
        if not isinstance(m, dict):
            continue
        mid = m.get("market_id")
        if mid is None or str(mid).strip() == "":
            continue
        markets.append(
            MarketSpec(
                market_id=str(mid),
                market_name_regex=m.get("market_name_regex"),
                min_legs=int(m.get("min_legs", 2)),
            )
        )

    signal_raw = raw.get("signal", {})
    staking_raw = raw.get("staking", {})
    risk_raw = raw.get("risk", {})

    odds_sources = signal_raw.get("odds_sources")
    if odds_sources is None:
        # Backward compatible with older single-field config.
        single = signal_raw.get("odds_source")
        odds_sources = [str(single)] if single else None

    signal = DutchingSignal(
        odds_sources=None if odds_sources is None else [str(x) for x in odds_sources],
        min_margin_pct=float(signal_raw.get("min_margin_pct", DutchingSignal.min_margin_pct)),
        min_size=float(signal_raw.get("min_size", DutchingSignal.min_size)),
        min_legs=int(signal_raw.get("min_legs", DutchingSignal.min_legs)),
        min_odds=float(signal_raw.get("min_odds", DutchingSignal.min_odds)),
        max_odds=float(signal_raw.get("max_odds", DutchingSignal.max_odds)),
    )

    staking = Staking(
        method=str(staking_raw.get("method", Staking.method)),
        total_stake=float(staking_raw.get("total_stake", Staking.total_stake)),
        target_profit=float(staking_raw.get("target_profit", Staking.target_profit)),
        min_stake=float(staking_raw.get("min_stake", Staking.min_stake)),
        stake_decimals=int(staking_raw.get("stake_decimals", Staking.stake_decimals)),
    )

    risk = RiskLimits(
        max_orders_per_market=int(risk_raw.get("max_orders_per_market", RiskLimits.max_orders_per_market)),
        max_total_stake_per_market=float(risk_raw.get("max_total_stake_per_market", RiskLimits.max_total_stake_per_market)),
        max_total_stake_per_event=float(risk_raw.get("max_total_stake_per_event", RiskLimits.max_total_stake_per_event)),
        max_total_stake_per_day=float(risk_raw.get("max_total_stake_per_day", RiskLimits.max_total_stake_per_day)),
    )

    return BotConfig(
        markets=markets,
        signal=signal,
        staking=staking,
        risk=risk,
        poll_interval_s=float(raw.get("poll_interval_s", 0.25)),
        dry_run=bool(raw.get("dry_run", True)),
    )
