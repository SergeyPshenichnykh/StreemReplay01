#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from pathlib import Path
from datetime import datetime
from bisect import bisect_left, bisect_right
from collections import Counter

actions_p = Path("replay/delta_10s/action_log.csv")
out = Path("replay/delta_10s_macro_min10/detected_remove_train_cycles.csv")

REMOVE_TRAIN_SEC = 8.0
ADD_MIN = 4.0
ADD_MAX = 11.0
MATCH_LOOKBACK = 10.0

MIN_REMOVE = 20000
MIN_ADD = 3000
DEDUP_SEC = 10.0

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def mt_summary(rows):
    c = Counter(r["market_type"] for r in rows)
    return "; ".join(f"{k}:{v}" for k, v in c.most_common(25))

rows = []
with actions_p.open(newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        try:
            r["_ts"] = ts(r["utc"])
            r["_amount"] = float(r["amount"])
        except Exception:
            continue
        rows.append(r)

rows.sort(key=lambda r: r["_ts"])

def subset(action):
    xs = [r for r in rows if r["action"] == action]
    times = [r["_ts"] for r in xs]
    pref = [0.0]
    for r in xs:
        pref.append(pref[-1] + r["_amount"])
    return xs, times, pref

removes, rt, rp = subset("VISIBLE_REMOVE")
adds, at, ap = subset("VISIBLE_ADD")
matches, mt, mp = subset("MATCH")

def rng(times, a, b):
    return bisect_left(times, a), bisect_right(times, b)

def sm(pref, i, j):
    return pref[j] - pref[i]

candidates = []

for r in removes:
    t = r["_ts"]

    ri, rj = rng(rt, t, t + REMOVE_TRAIN_SEC)
    rm_rows = removes[ri:rj]
    rm_amt = sm(rp, ri, rj)

    if rm_amt < MIN_REMOVE:
        continue

    ai, aj = rng(at, t + ADD_MIN, t + ADD_MAX)
    add_rows = adds[ai:aj]
    add_amt = sm(ap, ai, aj)

    if add_amt < MIN_ADD:
        continue

    mi, mj = rng(mt, t - MATCH_LOOKBACK, t)
    match_rows = matches[mi:mj]
    match_amt = sm(mp, mi, mj)

    candidates.append({
        "remove_start_utc": r["utc"],
        "remove_count": len(rm_rows),
        "remove_amount": rm_amt,
        "add_count": len(add_rows),
        "add_amount": add_amt,
        "net_change": add_amt - rm_amt,
        "match_lookback_count": len(match_rows),
        "match_lookback_amount": match_amt,
        "remove_markets": mt_summary(rm_rows),
        "add_markets": mt_summary(add_rows),
        "match_markets": mt_summary(match_rows),
    })

clean = []
for c in sorted(candidates, key=lambda x: ts(x["remove_start_utc"])):
    t = ts(c["remove_start_utc"])
    if clean and t - ts(clean[-1]["remove_start_utc"]) <= DEDUP_SEC:
        if c["remove_amount"] > clean[-1]["remove_amount"]:
            clean[-1] = c
    else:
        clean.append(c)

fields = [
    "remove_start_utc",
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
    for c in clean:
        cc = dict(c)
        for k in ["remove_amount","add_amount","net_change","match_lookback_amount"]:
            cc[k] = fnum(cc[k])
        w.writerow(cc)

print("OUT:", out)
print("cycles:", len(clean))
