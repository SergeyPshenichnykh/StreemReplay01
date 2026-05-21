#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter
from bisect import bisect_left, bisect_right

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def read_actions(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = ts(r["utc"])
                r["_amount"] = float(r["amount"])
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])
    return rows

def split_action(rows, action):
    out = [r for r in rows if r["action"] == action]
    times = [r["_ts"] for r in out]

    pref = [0.0]
    for r in out:
        pref.append(pref[-1] + r["_amount"])

    return out, times, pref

def idx_range(times, start, end):
    return bisect_left(times, start), bisect_right(times, end)

def sum_range(pref, i, j):
    return pref[j] - pref[i]

def mt_summary(rows):
    c = Counter(r["market_type"] for r in rows)
    return "; ".join(f"{k}:{v}" for k, v in c.most_common(20))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/detected_rebalance_cycles_fast.csv")
    ap.add_argument("--min-trigger-match", type=float, default=500)
    ap.add_argument("--min-remove-amount", type=float, default=5000)
    ap.add_argument("--min-add-amount", type=float, default=3000)
    ap.add_argument("--dedup-sec", type=float, default=4)
    args = ap.parse_args()

    rows = read_actions(args.actions)

    matches, mt, mp = split_action(rows, "MATCH")
    removes, rt, rp = split_action(rows, "VISIBLE_REMOVE")
    adds, at, apref = split_action(rows, "VISIBLE_ADD")

    candidates = []
    last_t = -10**18

    for n, m in enumerate(matches, 1):
        t = m["_ts"]

        if t - last_t < args.dedup_sec:
            continue

        mi, mj = idx_range(mt, t - 10, t)
        match_amount = sum_range(mp, mi, mj)
        if match_amount < args.min_trigger_match:
            continue

        ri, rj = idx_range(rt, t, t + 3)
        remove_amount = sum_range(rp, ri, rj)
        if remove_amount < args.min_remove_amount:
            continue

        ai, aj = idx_range(at, t + 4, t + 11)
        add_amount = sum_range(apref, ai, aj)
        if add_amount < args.min_add_amount:
            continue

        remove_rows = removes[ri:rj]
        add_rows = adds[ai:aj]
        match_rows = matches[mi:mj]

        candidates.append({
            "trigger_utc": m["utc"],
            "trigger_market_type": m["market_type"],
            "trigger_runner_name": m["runner_name"],
            "trigger_price": m["price"],
            "trigger_amount": m["amount"],
            "match_amount_10s": match_amount,
            "remove_count": len(remove_rows),
            "remove_amount": remove_amount,
            "add_count": len(add_rows),
            "add_amount": add_amount,
            "net_change": add_amount - remove_amount,
            "remove_markets": mt_summary(remove_rows),
            "add_markets": mt_summary(add_rows),
            "trigger_markets": mt_summary(match_rows),
        })

        last_t = t

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "trigger_utc",
        "trigger_market_type",
        "trigger_runner_name",
        "trigger_price",
        "trigger_amount",
        "match_amount_10s",
        "remove_count",
        "remove_amount",
        "add_count",
        "add_amount",
        "net_change",
        "trigger_markets",
        "remove_markets",
        "add_markets",
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in candidates:
            rr = dict(r)
            for k in ["match_amount_10s", "remove_amount", "add_amount", "net_change"]:
                rr[k] = fnum(rr[k])
            w.writerow(rr)

    print("DONE:", out)
    print("cycles:", len(candidates))

if __name__ == "__main__":
    main()
