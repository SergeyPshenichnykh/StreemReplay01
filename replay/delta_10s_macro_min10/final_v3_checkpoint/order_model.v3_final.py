from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderStatus(str, Enum):
    REQUESTED_PLACE = "requested_place"
    REQUESTED_CANCEL = "requested_cancel"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXITED = "exited"
    SETTLED = "settled"


@dataclass
class V3Order:
    order_id: str
    market_id: str
    selection_id: int
    handicap: float | None
    market_type: str | None
    market_name: str | None
    runner_name: str | None
    side: str
    price: float
    stake: float
    remaining: float
    book_side: str = ""
    matched: float = 0.0
    avg_price: float = 0.0
    queue_ahead_initial: float = 0.0
    queue_ahead_remaining: float = 0.0
    previous_queue_size: float | None = None
    current_queue_size: float | None = None
    fill_deadline_pt: int = 0
    cancel_requested_pt: int = 0
    cancel_effective_pt: int = 0
    inplay: bool = False
    first_fill_pt: int = 0
    late_fill_no_valid_exit_pt: int = 0
    exited_pt: int = 0
    settled_pt: int = 0
    pnl_v3: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    placed_pt: int = 0
    placed_utc: str = ""
    fill_source: str = ""

    def exposure(self) -> float:
        if self.side == "BACK":
            return max(0.0, float(self.stake))
        if self.side == "LAY":
            return max(0.0, float(self.stake)) * max(0.0, float(self.price) - 1.0)
        return 0.0

    def is_active(self) -> bool:
        return self.status in {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.REQUESTED_CANCEL,
        }

    def apply_queue_delta(self, delta: float) -> float:
        if delta <= 0 or not self.is_active() or self.remaining <= 0:
            return 0.0

        available = float(delta)
        queue_consumed = min(self.queue_ahead_remaining, available)
        self.queue_ahead_remaining -= queue_consumed
        available -= queue_consumed

        matched_now = min(self.remaining, available)
        if matched_now <= 0:
            return 0.0

        previous_matched = self.matched
        self.matched += matched_now
        self.remaining -= matched_now
        if self.matched > 0:
            self.avg_price = (
                (self.avg_price * previous_matched) + (self.price * matched_now)
            ) / self.matched

        if self.remaining <= 1e-9:
            self.remaining = 0.0
            self.status = OrderStatus.FILLED
        elif self.status == OrderStatus.REQUESTED_CANCEL:
            pass
        else:
            self.status = OrderStatus.PARTIALLY_FILLED

        self.fill_source = "queue"
        return matched_now

    def cancel_remaining(self) -> float:
        cancelled = max(0.0, float(self.remaining))
        self.remaining = 0.0
        if self.status in {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.REQUESTED_CANCEL,
        }:
            self.status = OrderStatus.CANCELLED
        return cancelled


@dataclass
class MyOrdersAtPrice:
    my_lay: float = 0.0
    my_back: float = 0.0
    matched: float = 0.0


@dataclass
class OrderModel:
    """
    Placeholder for future maker-order state.

    Keyed by (market_id, selection_id, handicap, price).
    Values are the user's own orders (my_lay/my_back) and matched amount.
    """

    by_key: dict[tuple[str, int, float | None, float], MyOrdersAtPrice] = field(default_factory=dict)

    def get(
        self,
        *,
        market_id: str,
        selection_id: int,
        handicap: float | None,
        price: float,
    ) -> MyOrdersAtPrice:
        return self.by_key.get((market_id, selection_id, handicap, float(price)), MyOrdersAtPrice())
