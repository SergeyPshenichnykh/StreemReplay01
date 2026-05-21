#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter

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

def mt_summary(rows):
    c = Counter(r["market_type"] for r in rows)
    return "; ".join(f"{k}:{v}" for k, v in c.most_common(20))

def amount(rows):
    return sum(r["_amount"] for r in rows)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/detected_rebalance_cycles.csv")
    ap.add_argument("--min-trigger-match", type=float, default=500.0)
    ap.add_argument("--trigger-window-sec", type=float, default=10.0)
    ap.add_argument("--remove-after-sec", type=float, default=3.0)
    ap.add_argument("--add-delay-min-sec", type=float, default=4.0)
    ap.add_argument("--add-delay-max-sec", type=float, default=11.0)
    ap.add_argument("--min-remove-amount", type=float, default=5000.0)
    ap.add_argument("--min-add-amount", type=float, default=3000.0)
    ap.add_argument("--dedup-sec", type=float, default=4.0)
    args = ap.parse_args()

    rows = read_actions(args.actions)
    matches = [r for r in rows if r["action"] == "MATCH"]
    removes = [r for r in rows if r["action"] == "VISIBLE_REMOVE"]
    adds = [r for r in rows if r["action"] == "VISIBLE_ADD"]

    candidates = []

    for m in matches:
        t = m["_ts"]

        trigger_ms = [
            x for x in matches
            if t - args.trigger_window_sec <= x["_ts"] <= t
        ]

        trigger_amount = amount(trigger_ms)
        if trigger_amount < args.min_trigger_match:
            continue

        remove_rows = [
            x for x in removes
            if t <= x["_ts"] <= t + args.remove_after_sec
        ]

        remove_amount = amount(remove_rows)
        if remove_amount < args.min_remove_amount:
            continue

        add_rows = [
            x for x in adds
            if t + args.add_delay_min_sec <= x["_ts"] <= t + args.add_delay_max_sec
        ]

        add_amount = amount(add_rows)
        if add_amount < args.min_add_amount:
            continue

        first_remove_ts = min(x["_ts"] for x in remove_rows)
        first_add_ts = min(x["_ts"] for x in add_rows)

        candidates.append({
            "trigger_utc": m["utc"],
            "trigger_market_type": m["market_type"],
            "trigger_market_name": m["market_name"],
            "trigger_runner_name": m["runner_name"],
            "trigger_price": m["price"],
            "trigger_amount": m["amount"],

            "trigger_window_match_count": len(trigger_ms),
            "trigger_window_match_amount": trigger_amount,

            "remove_count": len(remove_rows),
            "remove_amount": remove_amount,
            "remove_first_lag_sec": first_remove_ts - t,

            "add_count": len(add_rows),
            "add_amount": add_amount,
            "add_first_lag_sec": first_add_ts - t,

            "net_change": add_amount - remove_amount,

            "remove_markets": mt_summary(remove_rows),
            "add_markets": mt_summary(add_rows),
            "trigger_markets": mt_summary(trigger_ms),
        })

    # dedup close triggers
    clean = []
    last_ts = -10**18

    for c in candidates:
        t = ts(c["trigger_utc"])
        if t - last_ts < args.dedup_sec:
            if clean and float(c["remove_amount"]) > float(clean[-1]["remove_amount"]):
                clean[-1] = c
                last_ts = t
            continue
        clean.append(c)
        last_ts = t

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "trigger_utc",
        "trigger_market_type",
        "trigger_market_name",
        "trigger_runner_name",
        "trigger_price",
        "trigger_amount",
        "trigger_window_match_count",
        "trigger_window_match_amount",
        "remove_count",
        "remove_amount",
        "remove_first_lag_sec",
        "add_count",
        "add_amount",
        "add_first_lag_sec",
        "net_change",
        "trigger_markets",
        "remove_markets",
        "add_markets",
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in clean:
            rr = dict(r)
            for k in [
                "trigger_window_match_amount",
                "remove_amount",
                "remove_first_lag_sec",
                "add_amount",
                "add_first_lag_sec",
                "net_change",
            ]:
                rr[k] = fnum(rr[k])
            w.writerow(rr)

    print("DONE:", out)
    print("cycles:", len(clean))

if __name__ == "__main__":
    main()
