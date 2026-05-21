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

def read_actions(path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = ts(r["utc"])
                r["_amount"] = float(r["amount"])
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

def k(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["side"],
        r["price"],
    )

def opposite_side(side):
    if side == "ATB":
        return "ATL"
    if side == "ATL":
        return "ATB"
    return side

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--orders", default="replay/delta_10s_macro_min10/shadow_bot_orders.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/shadow_order_outcomes.csv")
    ap.add_argument("--horizon-sec", type=float, default=30.0)
    ap.add_argument("--near-sec", type=float, default=1.0)
    args = ap.parse_args()

    actions = read_actions(args.actions)
    removes, rt = split(actions, "VISIBLE_REMOVE")
    matches, mt = split(actions, "MATCH")

    orders = list(csv.DictReader(open(args.orders, newline="", encoding="utf-8")))

    out_rows = []

    for o in orders:
        t0 = ts(o["first_add_utc"])
        t1 = t0 + args.horizon_sec

        key = (
            o["market_type"],
            o["market_name"],
            o["runner_name"],
            o["side"],
            o["price"],
        )

        opp_key = (
            o["market_type"],
            o["market_name"],
            o["runner_name"],
            opposite_side(o["side"]),
            o["price"],
        )

        ri, rj = rng(rt, t0, t1)
        mi, mj = rng(mt, t0, t1)

        same_remove = []
        same_match = []
        opp_match = []

        for r in removes[ri:rj]:
            if k(r) == key:
                same_remove.append(r)

        for m in matches[mi:mj]:
            if k(m) == key:
                same_match.append(m)
            if k(m) == opp_key:
                opp_match.append(m)

        first_remove_ts = min([r["_ts"] for r in same_remove], default=None)

        near_match_same = 0.0
        near_match_opp = 0.0

        if first_remove_ts is not None:
            for m in matches[mi:mj]:
                if abs(m["_ts"] - first_remove_ts) <= args.near_sec:
                    mk = k(m)
                    if mk == key:
                        near_match_same += m["_amount"]
                    if mk == opp_key:
                        near_match_opp += m["_amount"]

        same_remove_amt = sum(r["_amount"] for r in same_remove)
        same_match_amt = sum(m["_amount"] for m in same_match)
        opp_match_amt = sum(m["_amount"] for m in opp_match)

        if same_match_amt > 0 or opp_match_amt > 0 or near_match_same > 0 or near_match_opp > 0:
            verdict = "MATCH_CONFIRMED"
        elif same_remove_amt >= float(o["stake"]):
            verdict = "LEVEL_DISAPPEARED_NO_MATCH_TAG"
        elif same_remove_amt > 0:
            verdict = "PARTIAL_LEVEL_DISAPPEARED_NO_MATCH_TAG"
        else:
            verdict = "NO_TOUCH"

        out_rows.append({
            **o,
            "same_level_remove_amount": round(same_remove_amt, 2),
            "same_level_match_amount": round(same_match_amt, 2),
            "opposite_side_match_amount": round(opp_match_amt, 2),
            "near_remove_match_same_amount": round(near_match_same, 2),
            "near_remove_match_opp_amount": round(near_match_opp, 2),
            "outcome": verdict,
        })

    fields = list(out_rows[0].keys())

    with Path(args.out).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    print("OUT:", args.out)

    total = len(out_rows)
    by = defaultdict(int)
    amount_by = defaultdict(float)

    for r in out_rows:
        by[r["outcome"]] += 1
        amount_by[r["outcome"]] += float(r["same_level_remove_amount"])

    print("orders:", total)
    for k2 in sorted(by, key=lambda x: -by[x]):
        print(k2, "count=", by[k2], "same_level_remove=", round(amount_by[k2], 2), "pct=", round(100*by[k2]/total, 2))

    print()
    print("TOP LEVEL DISAPPEARED")
    xs = [r for r in out_rows if r["outcome"] != "NO_TOUCH"]
    xs.sort(key=lambda r: -float(r["same_level_remove_amount"]))

    for r in xs[:50]:
        print(
            "S", r["signal_id"],
            r["market_type"], "|", r["runner_name"],
            "|", r["side"], "@", r["price"],
            "stake=", r["stake"],
            "engine_add=", r["engine_add_amount"],
            "same_remove=", r["same_level_remove_amount"],
            "match=", r["same_level_match_amount"],
            "opp_match=", r["opposite_side_match_amount"],
            "outcome=", r["outcome"],
        )

if __name__ == "__main__":
    main()
