import argparse
import csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime

TAKE_TICKS = 3
STOP_TICKS = 2

def ts_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)

def to_float(x) -> float:
    try:
        return float(x or 0)
    except Exception:
        return 0.0

LADDER = [round(x / 100, 2) for x in range(101, 201)]
IDX = {p: i for i, p in enumerate(LADDER)}

def move(price: float, ticks: int):
    p = round(float(price), 2)
    if p not in IDX:
        return None
    j = IDX[p] + int(ticks)
    if j < 0 or j >= len(LADDER):
        return None
    return LADDER[j]

def exchange_pnl(side: str, matched: float, entry: float, exitp: float) -> float:
    if matched <= 0 or entry <= 1 or exitp <= 1:
        return 0.0
    if side == "BACK":
        return matched * (entry - exitp) / exitp
    if side == "LAY":
        return matched * (exitp - entry) / exitp
    return 0.0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--output", required=True)
    ap.add_argument("--take-ticks", type=int, default=TAKE_TICKS)
    ap.add_argument("--stop-ticks", type=int, default=STOP_TICKS)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.input, newline="", encoding="utf-8")))
    actions = list(csv.DictReader(open(args.actions, newline="", encoding="utf-8")))

    for a in actions:
        a["_pt"] = ts_ms(a["utc"])
        a["_price"] = round(to_float(a["price"]), 2)
        a["_amount"] = to_float(a["amount"])
    actions.sort(key=lambda r: r["_pt"])

    by_order = defaultdict(list)
    for r in rows:
        by_order[r["order_id"]].append(r)

    out = list(rows)
    settled_orders = set()

    for oid, xs in by_order.items():
        xs = sorted(xs, key=lambda r: int(r["pt"]))
        if any(r["event"] == "SETTLE" for r in xs):
            continue

        fill = next((r for r in xs if r["event"] in {"FILL", "PARTIAL_FILL"}), None)
        if not fill:
            continue

        side = fill["side"]
        entry = round(to_float(fill["price"]), 2)
        matched = to_float(fill["matched"])
        fill_pt = int(fill["pt"])

        if matched <= 0:
            continue

        if side == "LAY":
            target = move(entry, args.take_ticks)
            stop_price = move(entry, -args.stop_ticks)
            exit_side = "ATL"
        elif side == "BACK":
            target = move(entry, -args.take_ticks)
            stop_price = move(entry, args.stop_ticks)
            exit_side = "ATB"
        else:
            continue

        if target is None or stop_price is None:
            continue

        exit_ev = None
        exit_price = None

        for a in actions:
            if a["_pt"] <= fill_pt:
                continue
            if a["market_type"] != fill["market_type"]:
                continue
            if a["market_name"] != fill["market_name"]:
                continue
            if a["runner_name"] != fill["runner_name"]:
                continue
            if a["side"] != exit_side:
                continue
            if a["action"] not in {"MATCH", "VISIBLE_REMOVE"}:
                continue
            if a["_amount"] <= 0:
                continue

            px = a["_price"]

            if side == "LAY":
                if px >= target or px <= stop_price:
                    exit_ev = a
                    exit_price = px
                    break

            if side == "BACK":
                if px <= target or px >= stop_price:
                    exit_ev = a
                    exit_price = px
                    break

        if not exit_ev:
            continue

        pnl = exchange_pnl(side, matched, entry, exit_price)
        matched_fraction = min(1.0, matched / to_float(fill["stake"]))

        base = dict(fill)
        base["pt"] = str(exit_ev["_pt"])
        base["utc"] = exit_ev["utc"]
        base["queue_delta"] = "0.000000"
        base["matched_now"] = "0.000000"
        base["queue_ahead_remaining"] = "0.000000"
        base["exit_utc"] = exit_ev["utc"]
        base["exit_price"] = f"{exit_price:.6f}"
        base["pnl_v3"] = f"{pnl:.6f}"
        base["matched_fraction"] = f"{matched_fraction:.6f}"
        base["settlement"] = "preseed_tick_exit"

        exit_row = dict(base)
        exit_row["event"] = "EXIT"
        exit_row["status"] = "exited"
        exit_row["note"] = f"preseed_tick_exit take_ticks={args.take_ticks} stop_ticks={args.stop_ticks}"

        settle_row = dict(base)
        settle_row["event"] = "SETTLE"
        settle_row["status"] = "settled"
        settle_row["liability"] = "0.000000"
        settle_row["note"] = f"settlement=preseed_tick_exit take_ticks={args.take_ticks} stop_ticks={args.stop_ticks}"

        out.append(exit_row)
        out.append(settle_row)
        settled_orders.add(oid)

    out.sort(key=lambda r: int(float(r.get("pt") or 0)))

    fieldnames = list(rows[0].keys())
    for extra in ["exit_utc", "exit_price", "pnl_v3", "matched_fraction", "settlement"]:
        if extra not in fieldnames:
            fieldnames.append(extra)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out)

    print("input_rows =", len(rows))
    print("output_rows =", len(out))
    print("settled_orders =", len(settled_orders))
    print("output =", args.output)

if __name__ == "__main__":
    main()
