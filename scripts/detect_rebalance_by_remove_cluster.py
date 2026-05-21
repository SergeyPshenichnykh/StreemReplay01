#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter
from bisect import bisect_left, bisect_right

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

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

def split(rows, action):
    xs = [r for r in rows if r["action"] == action]
    times = [r["_ts"] for r in xs]
    pref = [0.0]
    for r in xs:
        pref.append(pref[-1] + r["_amount"])
    return xs, times, pref

def idx(times, a, b):
    return bisect_left(times, a), bisect_right(times, b)

def sum_pref(pref, i, j):
    return pref[j] - pref[i]

def mt_summary(rows):
    c = Counter(r["market_type"] for r in rows)
    return "; ".join(f"{k}:{v}" for k, v in c.most_common(25))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/detected_remove_cluster_cycles.csv")
    ap.add_argument("--cluster-gap-sec", type=float, default=0.50)
    ap.add_argument("--min-remove", type=float, default=5000)
    ap.add_argument("--min-add", type=float, default=3000)
    ap.add_argument("--add-delay-min", type=float, default=4)
    ap.add_argument("--add-delay-max", type=float, default=11)
    ap.add_argument("--match-lookback", type=float, default=10)
    args = ap.parse_args()

    rows = read_actions(args.actions)

    removes = [r for r in rows if r["action"] == "VISIBLE_REMOVE"]
    adds, at, apref = split(rows, "VISIBLE_ADD")
    matches, mt, mpref = split(rows, "MATCH")

    clusters = []
    cur = []

    for r in removes:
        if not cur:
            cur = [r]
            continue
        if r["_ts"] - cur[-1]["_ts"] <= args.cluster_gap_sec:
            cur.append(r)
        else:
            clusters.append(cur)
            cur = [r]
    if cur:
        clusters.append(cur)

    out_rows = []

    for cid, c in enumerate(clusters, 1):
        start = c[0]["_ts"]
        end = c[-1]["_ts"]
        rm_amount = sum(r["_amount"] for r in c)

        if rm_amount < args.min_remove:
            continue

        ai, aj = idx(at, start + args.add_delay_min, start + args.add_delay_max)
        add_rows = adds[ai:aj]
        add_amount = sum_pref(apref, ai, aj)

        if add_amount < args.min_add:
            continue

        mi, mj = idx(mt, start - args.match_lookback, start)
        match_rows = matches[mi:mj]
        match_amount = sum_pref(mpref, mi, mj)

        out_rows.append({
            "cluster_id": cid,
            "remove_start_utc": c[0]["utc"],
            "remove_end_utc": c[-1]["utc"],
            "remove_duration_sec": end - start,
            "remove_count": len(c),
            "remove_amount": rm_amount,
            "add_count": len(add_rows),
            "add_amount": add_amount,
            "net_change": add_amount - rm_amount,
            "match_lookback_count": len(match_rows),
            "match_lookback_amount": match_amount,
            "remove_markets": mt_summary(c),
            "add_markets": mt_summary(add_rows),
            "match_markets": mt_summary(match_rows),
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "cluster_id",
        "remove_start_utc",
        "remove_end_utc",
        "remove_duration_sec",
        "remove_count",
        "remove_amount",
        "add_count",
        "add_amount",
        "net_change",
        "match_lookback_count",
        "match_lookback_amount",
        "remove_markets",
        "add_markets",
        "match_markets",
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in out_rows:
            rr = dict(r)
            for k in ["remove_duration_sec","remove_amount","add_amount","net_change","match_lookback_amount"]:
                rr[k] = fnum(rr[k])
            w.writerow(rr)

    print("OUT:", out)
    print("cycles:", len(out_rows))

if __name__ == "__main__":
    main()
