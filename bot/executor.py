from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    market_id: str
    selection_id: str
    side: str  # "BACK" or "LAY"
    price: float
    size: float


class OrderExecutor:
    def place_orders(self, orders: list[Order]) -> None:  # pragma: no cover
        raise NotImplementedError


class PrintExecutor(OrderExecutor):
    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    def place_orders(self, orders: list[Order]) -> None:
        if not self.enabled:
            return
        for o in orders:
            print(f"ORDER {o.market_id} {o.side} sel={o.selection_id} price={o.price:.2f} size={o.size:.2f}")
