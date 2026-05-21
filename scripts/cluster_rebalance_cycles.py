#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from pathlib import Path
from datetime import datetime

src = Path("replay/delta_10s_macro_min10/detected_rebalance_cycles_scored.csv")
out = Path("replay/delta_10s_macro_min10/detected_rebalance_cycles_clustered.csv")

MIN_SCORE = 9
MIN_REMOVE = 50000
MIN_ADD = 20000
GAP_SEC = 20

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

rows = []
with src.open(newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        if r["phase"] not in {"NORMAL_FIRST_HALF", "NORMAL_SECOND_HALF"}:
            continue
        if int(r["score"]) < MIN_SCORE:
            continue
        if float(r["remove_amount"]) < MIN_REMOVE:
            continue
        if float(r["add_amount"]) < MIN_ADD:
            continue
        r["_ts"] = ts(r["trigger_utc"])
        r["_remove"] = float(r["remove_amount"])
        r["_score"] = int(r["score"])
        rows.append(r)

rows.sort(key=lambda r: r["_ts"])

clusters = []
cur = []

for r in rows:
    if not cur:
        cur = [r]
        continue

    if r["_ts"] - cur[-1]["_ts"] <= GAP_SEC:
        cur.append(r)
    else:
        clusters.append(cur)
        cur = [r]

if cur:
    clusters.append(cur)

picked = []

for i, c in enumerate(clusters, 1):
    best = sorted(c, key=lambda r: (-r["_score"], -r["_remove"]))[0]
    best["cluster_id"] = str(i)
    best["cluster_size"] = str(len(c))
    best["cluster_start_utc"] = c[0]["trigger_utc"]
    best["cluster_end_utc"] = c[-1]["trigger_utc"]
    picked.append(best)

fields = [
    "cluster_id",
    "cluster_size",
    "cluster_start_utc",
    "cluster_end_utc",
    "trigger_utc",
    "minute_from_kickoff",
    "phase",
    "score",
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
    for r in picked:
        w.writerow({k: r.get(k, "") for k in fields})

print("OUT:", out)
print("clusters:", len(picked))

for r in picked:
    print(
        "CL", r["cluster_id"],
        "size", r["cluster_size"],
        "time", r["trigger_utc"],
        "min", r["minute_from_kickoff"],
        "phase", r["phase"],
        "score", r["score"],
        "rm", round(float(r["remove_amount"]),2),
        "add", round(float(r["add_amount"]),2),
        "net", round(float(r["net_change"]),2),
        "trigger", r["trigger_market_type"], r["trigger_runner_name"], "@", r["trigger_price"], r["trigger_amount"],
    )
