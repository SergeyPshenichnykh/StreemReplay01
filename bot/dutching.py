from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RunnerQuote:
    selection_id: str
    runner_name: str
    odds: float
    size: float | None = None


@dataclass(frozen=True)
class DutchingResult:
    inv_sum: float
    book_pct: float
    margin: float
    margin_pct: float
    stake_total: float
    stakes: list[float]
    predicted_profit: float


def _round_stake(x: float, decimals: int) -> float:
    if decimals <= 0:
        return float(int(round(x)))
    return round(x, decimals)


def calc_dutching(
    odds: list[float],
    method: str,
    total_stake: float,
    target_profit: float,
    min_stake: float,
    stake_decimals: int,
) -> DutchingResult:
    inv = [1.0 / o for o in odds]
    inv_sum = sum(inv)
    if inv_sum <= 0:
        return DutchingResult(0.0, 0.0, 0.0, 0.0, 0.0, [], 0.0)

    book_pct = inv_sum * 100.0
    margin = (1.0 / inv_sum) - 1.0
    margin_pct = margin * 100.0

    if method == "fixed-stake":
        stake_total = max(0.0, float(total_stake))
    elif method == "target-profit":
        if margin <= 0:
            return DutchingResult(inv_sum, book_pct, margin, margin_pct, 0.0, [], 0.0)
        stake_total = max(0.0, float(target_profit)) / margin
    elif method == "minimum-stake":
        stake_total = 0.0
    else:
        raise ValueError(f"Unknown staking method: {method}")

    weights = [x / inv_sum for x in inv]

    if method != "minimum-stake":
        stakes = [_round_stake(stake_total * w, stake_decimals) for w in weights]
        stake_total_eff = sum(stakes)
        predicted_profit = stake_total_eff * margin
        return DutchingResult(inv_sum, book_pct, margin, margin_pct, stake_total_eff, stakes, predicted_profit)

    floor = max(0.0, float(min_stake))
    n = len(odds)
    constrained: set[int] = set()
    stakes_f = [0.0] * n
    while True:
        constrained_total = floor * len(constrained)
        unconstrained = [i for i in range(n) if i not in constrained]
        if not unconstrained:
            stake_total = constrained_total
            break
        remaining_weights_sum = sum(weights[i] for i in unconstrained)
        if remaining_weights_sum <= 0:
            stake_total = constrained_total
            break
        stake_total_candidate = constrained_total + (floor * len(unconstrained)) / remaining_weights_sum
        for i in range(n):
            stakes_f[i] = floor if i in constrained else stake_total_candidate * weights[i]
        newly_constrained = {i for i in unconstrained if stakes_f[i] < floor - 1e-12}
        if not newly_constrained:
            stake_total = stake_total_candidate
            break
        constrained |= newly_constrained

    stakes = [max(floor, _round_stake(s, stake_decimals)) for s in stakes_f]
    stake_total_eff = sum(stakes)
    predicted_profit = stake_total_eff * margin
    return DutchingResult(inv_sum, book_pct, margin, margin_pct, stake_total_eff, stakes, predicted_profit)


def filter_quotes(
    quotes: list[RunnerQuote],
    *,
    min_odds: float,
    max_odds: float,
    min_size: float,
) -> list[RunnerQuote]:
    out: list[RunnerQuote] = []
    for q in quotes:
        if not math.isfinite(q.odds) or q.odds < min_odds or q.odds > max_odds:
            continue
        if min_size > 0 and (q.size is None or q.size < min_size):
            continue
        out.append(q)
    return out

