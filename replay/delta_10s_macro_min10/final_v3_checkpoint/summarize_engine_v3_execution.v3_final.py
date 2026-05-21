#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_INPUT = Path("replay/delta_10s_macro_min10/engine_v3_execution_log.csv")
DEFAULT_OUTPUT = Path("replay/delta_10s_macro_min10/ENGINE_V3_SUMMARY.txt")
EVENTS = (
    "REQUEST_PLACE",
    "PLACE",
    "FILL",
    "PARTIAL_FILL",
    "REQUEST_CANCEL",
    "CANCEL",
    "CANCEL_REMAINING",
    "EXIT",
    "SETTLE",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Engine V3 execution log.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--starting-balance", type=float, default=1000.0)
    return parser.parse_args()


def _float_value(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def summarize(input_path: Path, output_path: Path, starting_balance: float) -> None:
    event_counts: Counter[str] = Counter()
    order_ids: set[str] = set()
    by_market: dict[str, Counter[str]] = defaultdict(Counter)
    by_order_status: Counter[str] = Counter()
    pnl_v3 = 0.0

    if not input_path.exists():
        raise FileNotFoundError(f"Input not found: {input_path}")

    with input_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            event = str(row.get("event") or "")
            order_id = str(row.get("order_id") or "")
            market_type = str(row.get("market_type") or "")
            status = str(row.get("status") or "")

            if event:
                event_counts[event] += 1
                if market_type:
                    by_market[market_type][event] += 1
            if order_id:
                order_ids.add(order_id)
            if status:
                by_order_status[status] += 1
            if event == "SETTLE":
                pnl_v3 += _float_value(row.get("pnl_v3"))

    final_balance_v3 = float(starting_balance) + pnl_v3

    lines: list[str] = []
    lines.append("ENGINE_V3_SUMMARY")
    lines.append(f"input={input_path}")
    lines.append(f"orders={len(order_ids)}")
    for event in EVENTS:
        lines.append(f"{event}={event_counts.get(event, 0)}")
    lines.append(f"pnl_v3={pnl_v3:+.6f}")
    lines.append(f"final_balance_v3={final_balance_v3:.6f}")

    lines.append("")
    lines.append("BY_MARKET")
    if by_market:
        for market_type in sorted(by_market):
            counts = by_market[market_type]
            parts = [f"{event}={counts.get(event, 0)}" for event in EVENTS if counts.get(event, 0)]
            lines.append(f"{market_type}: " + " ".join(parts))
    else:
        lines.append("-")

    lines.append("")
    lines.append("BY_ORDER_STATUS")
    if by_order_status:
        for status in sorted(by_order_status):
            lines.append(f"{status}={by_order_status[status]}")
    else:
        lines.append("-")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    summarize(args.input, args.output, args.starting_balance)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
