#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from bisect import bisect_left, bisect_right

ACTIONS = Path("replay/delta_10s/action_log.csv")
SIGNALS = Path("replay/delta_10s_macro_min10/final_engine_signals_unique_30s.csv")

OUT_SUMMARY = Path("replay/delta_10s_macro_min10/all_signal_price_migration_summary.csv")
OUT_LINKS = Path("replay/delta_10s_macro_min10/all_signal_price_migration_links.csv")

REMOVE_SEC = 8.0
ADD_MIN = 4.0
ADD_MAX = 11.0

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def key_runner(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["side"],
    )

def read_actions():
    rows = []
    with ACTIONS.open(newline="", encoding="utf-8") as f:
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

actions = read_actions()
removes, rt = split(actions, "VISIBLE_REMOVE")
adds, at = split(actions, "VISIBLE_ADD")

signals = list(csv.DictReader(SIGNALS.open(newline="", encoding="utf-8")))

summary_rows = []
link_rows = []

for sig in signals:
    sid = sig["unique_cluster_id"]
    t0 = ts(sig["utc"])

    ri, rj = rng(rt, t0, t0 + REMOVE_SEC)
    ai, aj = rng(at, t0 + ADD_MIN, t0 + ADD_MAX)

    rm_rows = removes[ri:rj]
    add_rows = adds[ai:aj]

    remove_total = sum(r["_amount"] for r in rm_rows)
    add_total = sum(r["_amount"] for r in add_rows)

    rm = defaultdict(float)
    ad = defaultdict(float)

    for r in rm_rows:
        rm[key_runner(r) + (r["price"],)] += r["_amount"]

    for r in add_rows:
        ad[key_runner(r) + (r["price"],)] += r["_amount"]

    rm_by_runner = defaultdict(list)
    ad_by_runner = defaultdict(list)

    for k, amount in rm.items():
        rm_by_runner[k[:4]].append({"price": float(k[4]), "amount": amount})

    for k, amount in ad.items():
        ad_by_runner[k[:4]].append({"price": float(k[4]), "amount": amount})

    linked_total = 0.0
    unpaired_remove = 0.0
    unpaired_add = 0.0
    link_count = 0

    for runner_key in sorted(set(rm_by_runner) | set(ad_by_runner)):
        rml = sorted(rm_by_runner.get(runner_key, []), key=lambda x: -x["amount"])
        adl = sorted(ad_by_runner.get(runner_key, []), key=lambda x: -x["amount"])

        used_add = [0.0] * len(adl)

        for r in rml:
            left = r["amount"]

            order = sorted(
                range(len(adl)),
                key=lambda i: (abs(adl[i]["price"] - r["price"]), -adl[i]["amount"])
            )

            for i in order:
                free = adl[i]["amount"] - used_add[i]
                if free <= 0:
                    continue

                take = min(left, free)
                if take <= 0:
                    continue

                linked_total += take
                link_count += 1

                link_rows.append({
                    "signal_id": sid,
                    "signal_utc": sig["utc"],
                    "minute": sig["minute_from_kickoff"],
                    "phase": sig["phase"],
                    "market_type": runner_key[0],
                    "market_name": runner_key[1],
                    "runner_name": runner_key[2],
                    "side": runner_key[3],
                    "remove_price": r["price"],
                    "add_price": adl[i]["price"],
                    "price_move": round(adl[i]["price"] - r["price"], 6),
                    "amount_linked": round(take, 2),
                })

                used_add[i] += take
                left -= take

                if left <= 0.000001:
                    break

            if left > 0.000001:
                unpaired_remove += left

        for i, a in enumerate(adl):
            left = a["amount"] - used_add[i]
            if left > 0.000001:
                unpaired_add += left

    linked_pct_remove = 100 * linked_total / remove_total if remove_total else 0.0
    linked_pct_add = 100 * linked_total / add_total if add_total else 0.0

    summary_rows.append({
        "signal_id": sid,
        "utc": sig["utc"],
        "minute": sig["minute_from_kickoff"],
        "phase": sig["phase"],
        "score": sig["score"],
        "remove_total": round(remove_total, 2),
        "add_total": round(add_total, 2),
        "linked_total": round(linked_total, 2),
        "linked_pct_remove": round(linked_pct_remove, 2),
        "linked_pct_add": round(linked_pct_add, 2),
        "unpaired_remove": round(unpaired_remove, 2),
        "unpaired_add": round(unpaired_add, 2),
        "link_count": link_count,
        "top_reduce_group": sig["top_reduce_group"],
        "top_increase_group": sig["top_increase_group"],
    })

OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)

with OUT_SUMMARY.open("w", newline="", encoding="utf-8") as f:
    fields = list(summary_rows[0].keys())
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(summary_rows)

with OUT_LINKS.open("w", newline="", encoding="utf-8") as f:
    fields = list(link_rows[0].keys())
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(link_rows)

print("OUT_SUMMARY:", OUT_SUMMARY)
print("OUT_LINKS:", OUT_LINKS)
print("signals:", len(summary_rows))

print()
print("GLOBAL")
total_rm = sum(float(r["remove_total"]) for r in summary_rows)
total_add = sum(float(r["add_total"]) for r in summary_rows)
total_linked = sum(float(r["linked_total"]) for r in summary_rows)
print("remove_total", round(total_rm, 2))
print("add_total", round(total_add, 2))
print("linked_total", round(total_linked, 2))
print("linked_pct_remove", round(100 * total_linked / total_rm, 2))
print("linked_pct_add", round(100 * total_linked / total_add, 2))

print()
print("LOWEST LINKED PCT")
for r in sorted(summary_rows, key=lambda x: float(x["linked_pct_remove"]))[:15]:
    print(
        r["signal_id"],
        r["utc"],
        "min=", r["minute"],
        "rm=", r["remove_total"],
        "add=", r["add_total"],
        "linked=", r["linked_total"],
        "pct_rm=", r["linked_pct_remove"],
        "pct_add=", r["linked_pct_add"],
        "rot=", r["top_reduce_group"], "->", r["top_increase_group"],
    )

print()
print("HIGHEST LINKED PCT")
for r in sorted(summary_rows, key=lambda x: -float(x["linked_pct_remove"]))[:15]:
    print(
        r["signal_id"],
        r["utc"],
        "min=", r["minute"],
        "rm=", r["remove_total"],
        "add=", r["add_total"],
        "linked=", r["linked_total"],
        "pct_rm=", r["linked_pct_remove"],
        "pct_add=", r["linked_pct_add"],
        "rot=", r["top_reduce_group"], "->", r["top_increase_group"],
    )
