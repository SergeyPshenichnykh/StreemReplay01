from __future__ import annotations

import re
import time
from dataclasses import dataclass

from bot.config import BotConfig, MarketSpec
from bot.dutching import RunnerQuote, calc_dutching, filter_quotes
from bot.executor import Order, OrderExecutor, PrintExecutor
from bot.stream import MarketDataStream, MarketSnapshot


@dataclass
class MarketState:
    orders_sent: int = 0
    total_stake_sent: float = 0.0


class DutchingBot:
    def __init__(
        self,
        *,
        config: BotConfig,
        stream: MarketDataStream,
        executor: OrderExecutor | None = None,
        print_orders: bool = True,
        max_signals: int = 0,
    ):
        self.config = config
        self.stream = stream
        self.executor = executor or PrintExecutor(enabled=print_orders)
        self.market_state: dict[str, MarketState] = {}
        self.max_signals = max_signals
        self._signals_sent = 0

        self._allow_all_markets = len(config.markets) == 0
        self._market_specs: dict[str, MarketSpec] = {m.market_id: m for m in config.markets}
        self._name_res: dict[str, re.Pattern[str] | None] = {
            m.market_id: (re.compile(m.market_name_regex, re.IGNORECASE) if m.market_name_regex else None)
            for m in config.markets
        }

    def _state(self, market_id: str) -> MarketState:
        st = self.market_state.get(market_id)
        if st is None:
            st = MarketState()
            self.market_state[market_id] = st
        return st

    def _risk_allow(self, market_id: str, stake_total: float) -> bool:
        st = self._state(market_id)
        if st.orders_sent >= self.config.risk.max_orders_per_market:
            return False
        if st.total_stake_sent + stake_total > self.config.risk.max_total_stake_per_market:
            return False
        return True

    def _handle_snapshot(self, snap: MarketSnapshot) -> None:
        if self._allow_all_markets:
            spec = MarketSpec(market_id=snap.market_id)
            name_re = None
        else:
            spec = self._market_specs.get(snap.market_id)
            if spec is None:
                return
            name_re = self._name_res.get(snap.market_id)

        if name_re is not None and not name_re.search(snap.market_name):
            return

        # Evaluate configured sides independently (back-dutching via best_lay, lay-dutching via best_back).
        for odds_source in self.config.signal.odds_sources:
            if self.max_signals and self._signals_sent >= self.max_signals:
                raise SystemExit(f"Reached --max-signals={self.max_signals}")

            quotes: list[RunnerQuote] = []
            for r in snap.runners:
                if odds_source == "best_lay":
                    if r.best_lay is None:
                        continue
                    price, size = r.best_lay
                elif odds_source == "best_back":
                    if r.best_back is None:
                        continue
                    price, size = r.best_back
                else:
                    raise ValueError(f"Unknown odds_source: {odds_source}")

                quotes.append(RunnerQuote(r.selection_id, r.runner_name, float(price), float(size)))

            quotes = filter_quotes(
                quotes,
                min_odds=self.config.signal.min_odds,
                max_odds=self.config.signal.max_odds,
                min_size=self.config.signal.min_size,
            )
            min_legs = max(self.config.signal.min_legs, spec.min_legs)
            if len(quotes) < min_legs:
                continue

            odds = [q.odds for q in quotes]
            res = calc_dutching(
                odds,
                self.config.staking.method,
                self.config.staking.total_stake,
                self.config.staking.target_profit,
                self.config.staking.min_stake,
                self.config.staking.stake_decimals,
            )
            if res.margin_pct < self.config.signal.min_margin_pct:
                continue

            if not self._risk_allow(snap.market_id, res.stake_total):
                continue

            side = "BACK" if odds_source == "best_lay" else "LAY"
            orders: list[Order] = []
            for q, stake in zip(quotes, res.stakes, strict=False):
                if stake <= 0:
                    continue
                orders.append(Order(snap.market_id, q.selection_id, side, q.odds, stake))

            if not orders:
                continue

            exec_type = "TAKER"  # We cross the spread by taking best_*.
            queue = "N/A"
            print(
                "SIGNAL "
                f"time={snap.time or '-'} tick={snap.tick} "
                f"market={snap.market_id} name={snap.market_name!r} "
                f"odds_source={odds_source} side={side} exec={exec_type} queue={queue} "
                f"legs={len(quotes)} book_pct={res.book_pct:.3f} margin_pct={res.margin_pct:.3f} "
                f"stake_total={res.stake_total:.2f} pred_profit={res.predicted_profit:.2f} dry_run={self.config.dry_run}"
            )

            self.executor.place_orders(orders)
            st = self._state(snap.market_id)
            st.orders_sent += 1
            st.total_stake_sent += res.stake_total
            self._signals_sent += 1

    def run(self) -> None:
        for snap in self.stream.snapshots():
            self._handle_snapshot(snap)
            if self.config.poll_interval_s > 0:
                time.sleep(self.config.poll_interval_s)
