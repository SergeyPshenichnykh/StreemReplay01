#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from bisect import bisect_left, bisect_right
from collections import defaultdict

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def read_actions(path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = ts(r["utc"])
                r["_amount"] = float(r["amount"])
                r["_price"] = float(r["price"])
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])
    return rows

def split(rows, action):
    xs = [r for r in rows if r["action"] == action]
    times = [r["_ts"] for r in xs]
    return xs, times

def rng(times, a, b):
    return bisect_left(times, a), bisect_right(times, b)

def opposite_side(side):
    return "ATL" if side == "ATB" else "ATB" if side == "ATL" else side

def implied_order_side(book_side):
    if book_side == "ATB":
        return "LAY"
    if book_side == "ATL":
        return "BACK"
    return "UNKNOWN"

def same_level(o, r):
    return (
        o["market_type"] == r["market_type"]
        and o["market_name"] == r["market_name"]
        and o["runner_name"] == r["runner_name"]
        and o["side"] == r["side"]
        and float(o["price"]) == float(r["price"])
    )

def same_runner(o, r):
    return (
        o["market_type"] == r["market_type"]
        and o["market_name"] == r["market_name"]
        and o["runner_name"] == r["runner_name"]
    )

def pnl_for_trade(order_side, entry, exit_price, stake):
    if order_side == "BACK":
        return stake * ((entry / exit_price) - 1.0)
    if order_side == "LAY":
        return stake * (1.0 - (entry / exit_price))
    return 0.0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--outcomes", default="replay/delta_10s_macro_min10/shadow_order_outcomes.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/shadow_bot_pnl_proxy_conservative.csv")
    ap.add_argument("--summary", default="replay/delta_10s_macro_min10/SHADOW_BOT_PNL_PROXY_CONSERVATIVE_SUMMARY.txt")
    ap.add_argument("--fill-horizon-sec", type=float, default=30.0)
    ap.add_argument("--exit-horizon-sec", type=float, default=60.0)
    ap.add_argument("--max-exit-price-distance", type=float, default=0.20)
    args = ap.parse_args()

    actions = read_actions(args.actions)
    adds, at = split(actions, "VISIBLE_ADD")
    removes, rt = split(actions, "VISIBLE_REMOVE")
    orders = read_csv(args.outcomes)

    out_rows = []

    for o in orders:
        entry_price = float(o["price"])
        stake = float(o["stake"])
        order_side = implied_order_side(o["side"])
        t0 = ts(o["first_add_utc"])

        ri, rj = rng(rt, t0, t0 + args.fill_horizon_sec)
        same_removes = [r for r in removes[ri:rj] if same_level(o, r)]

        if not same_removes:
            out_rows.append({
                **o,
                "entry_order_side": order_side,
                "fill_utc": "",
                "fill_proxy": "NO",
                "exit_utc": "",
                "exit_proxy": "NO",
                "exit_side": "",
                "exit_price": "",
                "price_diff": "",
                "pnl_proxy": 0.0,
                "pnl_status": "NO_FILL_PROXY",
            })
            continue

        fill = same_removes[0]
        fill_ts = fill["_ts"]

        ai, aj = rng(at, fill_ts, fill_ts + args.exit_horizon_sec)
        opp = opposite_side(o["side"])

        exits = []
        for a in adds[ai:aj]:
            if not same_runner(o, a):
                continue
            if a["side"] != opp:
                continue
            if abs(a["_price"] - entry_price) > args.max_exit_price_distance:
                continue
            exits.append(a)

        if not exits:
            out_rows.append({
                **o,
                "entry_order_side": order_side,
                "fill_utc": fill["utc"],
                "fill_proxy": "YES",
                "exit_utc": "",
                "exit_proxy": "NO",
                "exit_side": "",
                "exit_price": "",
                "price_diff": "",
                "pnl_proxy": 0.0,
                "pnl_status": "NO_EXIT_PROXY",
            })
            continue

        exit_r = exits[0]
        exit_price = exit_r["_price"]
        pnl = pnl_for_trade(order_side, entry_price, exit_price, stake)

        out_rows.append({
            **o,
            "entry_order_side": order_side,
            "fill_utc": fill["utc"],
            "fill_proxy": "YES",
            "exit_utc": exit_r["utc"],
            "exit_proxy": "YES",
            "exit_side": exit_r["side"],
            "exit_price": exit_price,
            "price_diff": round(exit_price - entry_price, 6),
            "pnl_proxy": round(pnl, 6),
            "pnl_status": "EXIT_FOUND",
        })

    fields = list(out_rows[0].keys())
    with Path(args.out).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    by_status = defaultdict(lambda: {"count":0, "stake":0.0, "pnl":0.0})
    by_market = defaultdict(lambda: {"count":0, "stake":0.0, "pnl":0.0})

    for r in out_rows:
        st = r["pnl_status"]
        by_status[st]["count"] += 1
        by_status[st]["stake"] += float(r["stake"])
        by_status[st]["pnl"] += float(r["pnl_proxy"])

        if st == "EXIT_FOUND":
            mt = r["market_type"]
            by_market[mt]["count"] += 1
            by_market[mt]["stake"] += float(r["stake"])
            by_market[mt]["pnl"] += float(r["pnl_proxy"])

    total_pnl = sum(float(r["pnl_proxy"]) for r in out_rows)
    total_stake = sum(float(r["stake"]) for r in out_rows)
    exits = sum(1 for r in out_rows if r["pnl_status"] == "EXIT_FOUND")
    fills = sum(1 for r in out_rows if r["fill_proxy"] == "YES")

    lines = []
    lines.append("SHADOW BOT CONSERVATIVE PNL-PROXY SUMMARY")
    lines.append("")
    lines.append("fill_proxy = first_same_level_visible_remove")
    lines.append("exit_proxy = first_opposite_side_visible_add_same_runner_after_fill")
    lines.append("real_pnl = NO")
    lines.append("")
    lines.append(f"orders = {len(out_rows)}")
    lines.append(f"stake_total = {total_stake:.2f}")
    lines.append(f"fill_proxy_orders = {fills}")
    lines.append(f"fill_proxy_rate = {100*fills/len(out_rows):.2f}")
    lines.append(f"exit_found_orders = {exits}")
    lines.append(f"exit_found_rate = {100*exits/len(out_rows):.2f}")
    lines.append(f"pnl_proxy_total = {total_pnl:.6f}")
    lines.append(f"pnl_proxy_roi_on_total_stake_pct = {100*total_pnl/total_stake:.6f}")
    lines.append("")
    lines.append("BY_STATUS")
    for k, v in sorted(by_status.items(), key=lambda x: -x[1]["count"]):
        lines.append(f"{k}: count={v['count']} stake={v['stake']:.2f} pnl_proxy={v['pnl']:.6f}")

    lines.append("")
    lines.append("BY_MARKET_EXIT_FOUND")
    for k, v in sorted(by_market.items(), key=lambda x: -x[1]["pnl"])[:30]:
        lines.append(f"{k}: count={v['count']} stake={v['stake']:.2f} pnl_proxy={v['pnl']:.6f}")

    Path(args.summary).write_text("\n".join(lines), encoding="utf-8")
    print(Path(args.summary).read_text(encoding="utf-8"))

    print()
    print("TOP PNL")
    for r in sorted(out_rows, key=lambda x: -float(x["pnl_proxy"]))[:30]:
        print("S", r["signal_id"], r["market_type"], "|", r["runner_name"], "|", r["entry_order_side"], "@", r.get("entry_price", r.get("price", "")), "exit=", r["exit_price"], "pnl=", r["pnl_proxy"], "status=", r["pnl_status"])

    print()
    print("WORST PNL")
    for r in sorted(out_rows, key=lambda x: float(x["pnl_proxy"]))[:30]:
        print("S", r["signal_id"], r["market_type"], "|", r["runner_name"], "|", r["entry_order_side"], "@", r.get("entry_price", r.get("price", "")), "exit=", r["exit_price"], "pnl=", r["pnl_proxy"], "status=", r["pnl_status"])

if __name__ == "__main__":
    main()
