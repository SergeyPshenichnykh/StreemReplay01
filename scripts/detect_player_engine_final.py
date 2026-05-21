#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from pathlib import Path
from datetime import datetime
from bisect import bisect_left, bisect_right
from collections import defaultdict, Counter

ACTIONS = Path("replay/delta_10s/action_log.csv")
OUT = Path("replay/delta_10s_macro_min10/final_engine_signals.csv")

REMOVE_SEC = 8.0
ADD_MIN = 4.0
ADD_MAX = 11.0
MATCH_LOOKBACK = 10.0
DEDUP_SEC = 20.0

MIN_REMOVE = 50000.0
MIN_ADD = 30000.0
MIN_SCORE = 10

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def group(mt):
    if "CORNR" in mt or mt == "CORNER_ODDS":
        return "CORNERS"
    if mt in {"MATCH_ODDS", "DRAW_NO_BET", "DOUBLE_CHANCE"}:
        return "MATCH_RESULT"
    if mt in {"CORRECT_SCORE", "CORRECT_SCORE2", "HALF_TIME_SCORE", "WINNING_MARGIN"}:
        return "SCORELINE"
    if mt == "ASIAN_HANDICAP" or mt.startswith("TEAM_A_") or mt.startswith("TEAM_B_"):
        return "HANDICAP"
    if mt.startswith("OVER_UNDER") or mt in {"TOTAL_GOALS", "TEAM_TOTAL_GOALS"}:
        return "GOALS_TOTALS"
    if mt.startswith("FIRST_HALF") or mt in {"HALF_TIME", "HALF_TIME_FULL_TIME"}:
        return "FIRST_HALF_RELATED"
    if mt in {"BOTH_TEAMS_TO_SCORE", "ODD_OR_EVEN", "MATCH_ODDS_AND_BTTS", "CLEAN_SHEET"}:
        return "DERIVATIVES"
    return "OTHER"

def read_actions(path):
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
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

def rng(times, a, b):
    return bisect_left(times, a), bisect_right(times, b)

def psum(pref, i, j):
    return pref[j] - pref[i]

def group_sums(rows):
    d = defaultdict(float)
    for r in rows:
        d[group(r["market_type"])] += r["_amount"]
    return d

def fmt_group(d):
    return "; ".join(f"{k}:{round(v,2)}" for k,v in sorted(d.items(), key=lambda x: -abs(x[1])))

rows = read_actions(ACTIONS)

removes, rt, rp = split(rows, "VISIBLE_REMOVE")
adds, at, ap = split(rows, "VISIBLE_ADD")
matches, mt, mp = split(rows, "MATCH")

candidates = []

for r in removes:
    t = r["_ts"]

    ri, rj = rng(rt, t, t + REMOVE_SEC)
    rm_amt = psum(rp, ri, rj)
    if rm_amt < MIN_REMOVE:
        continue

    ai, aj = rng(at, t + ADD_MIN, t + ADD_MAX)
    add_amt = psum(ap, ai, aj)
    if add_amt < MIN_ADD:
        continue

    mi, mj = rng(mt, t - MATCH_LOOKBACK, t)
    match_amt = psum(mp, mi, mj)

    rm_rows = removes[ri:rj]
    add_rows = adds[ai:aj]

    rm_g = group_sums(rm_rows)
    add_g = group_sums(add_rows)

    all_groups = sorted(set(rm_g) | set(add_g))
    shift = {g: add_g.get(g, 0.0) - rm_g.get(g, 0.0) for g in all_groups}

    top_reduce = min(shift.items(), key=lambda x: x[1])
    top_increase = max(shift.items(), key=lambda x: x[1])

    score = 0
    if rm_amt >= 50000: score += 3
    if rm_amt >= 100000: score += 2
    if add_amt >= 30000: score += 3
    if add_amt >= 75000: score += 2
    if match_amt >= 500: score += 1
    if match_amt >= 1000: score += 1
    if len(all_groups) >= 5: score += 1
    if abs(add_amt - rm_amt) / max(add_amt, rm_amt, 1) <= 0.35: score += 2
    if top_reduce[0] != top_increase[0] and abs(top_reduce[1]) >= 1000 and abs(top_increase[1]) >= 1000:
        score += 2

    if score < MIN_SCORE:
        continue

    candidates.append({
        "utc": r["utc"],
        "score": score,
        "remove_count": rj - ri,
        "remove_amount": rm_amt,
        "add_count": aj - ai,
        "add_amount": add_amt,
        "net": add_amt - rm_amt,
        "match_count": mj - mi,
        "match_amount": match_amt,
        "top_reduce_group": top_reduce[0],
        "top_reduce_net": top_reduce[1],
        "top_increase_group": top_increase[0],
        "top_increase_net": top_increase[1],
        "remove_groups": fmt_group(rm_g),
        "add_groups": fmt_group(add_g),
    })

# dedup: keep strongest signal per 20 sec
clean = []
for c in sorted(candidates, key=lambda x: ts(x["utc"])):
    t = ts(c["utc"])
    if clean and t - ts(clean[-1]["utc"]) <= DEDUP_SEC:
        old = clean[-1]
        if (c["score"], c["remove_amount"], c["add_amount"]) > (old["score"], old["remove_amount"], old["add_amount"]):
            clean[-1] = c
    else:
        clean.append(c)

OUT.parent.mkdir(parents=True, exist_ok=True)

fields = [
    "utc","score",
    "remove_count","remove_amount",
    "add_count","add_amount",
    "net",
    "match_count","match_amount",
    "top_reduce_group","top_reduce_net",
    "top_increase_group","top_increase_net",
    "remove_groups","add_groups",
]

with OUT.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for c in clean:
        cc = dict(c)
        for k in ["remove_amount","add_amount","net","match_amount","top_reduce_net","top_increase_net"]:
            cc[k] = fnum(cc[k])
        w.writerow(cc)

print("OUT:", OUT)
print("signals:", len(clean))

print()
print("TOP SIGNALS")
for c in sorted(clean, key=lambda x: (-x["score"], -x["remove_amount"]))[:60]:
    print(
        c["utc"],
        "score=", c["score"],
        "rm=", round(c["remove_amount"],2),
        "add=", round(c["add_amount"],2),
        "net=", round(c["net"],2),
        "match=", round(c["match_amount"],2),
        "reduce=", c["top_reduce_group"], round(c["top_reduce_net"],2),
        "increase=", c["top_increase_group"], round(c["top_increase_net"],2),
    )
