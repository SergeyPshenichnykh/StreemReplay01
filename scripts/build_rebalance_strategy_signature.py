#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
from pathlib import Path
from collections import Counter, defaultdict

src = Path("replay/delta_10s_macro_min10/detected_rebalance_cycles_clustered.csv")
out_json = Path("replay/delta_10s_macro_min10/rebalance_strategy_signature.json")
out_csv = Path("replay/delta_10s_macro_min10/rebalance_strategy_signature.csv")

rows = []
with src.open(newline="", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        r["rm"] = float(r["remove_amount"])
        r["add"] = float(r["add_amount"])
        r["net"] = float(r["net_change"])
        r["match"] = float(r["match_amount_10s"])
        r["score_i"] = int(r["score"])
        rows.append(r)

def market_counter(field):
    c = Counter()
    for r in rows:
        for part in r[field].split(";"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            k, v = part.rsplit(":", 1)
            try:
                c[k] += int(v)
            except:
                pass
    return c

remove_markets = market_counter("remove_markets")
add_markets = market_counter("add_markets")
trigger_markets = Counter(r["trigger_market_type"] for r in rows)

by_phase = defaultdict(lambda: {"count":0, "rm":0.0, "add":0.0, "net":0.0})
for r in rows:
    p = r["phase"]
    by_phase[p]["count"] += 1
    by_phase[p]["rm"] += r["rm"]
    by_phase[p]["add"] += r["add"]
    by_phase[p]["net"] += r["net"]

signature = {
    "strategy": "cyclic_cross_market_package_rebalance",
    "cycles": len(rows),
    "total_remove": round(sum(r["rm"] for r in rows), 2),
    "total_add": round(sum(r["add"] for r in rows), 2),
    "net": round(sum(r["net"] for r in rows), 2),
    "phase_summary": {
        k: {
            "count": v["count"],
            "remove": round(v["rm"], 2),
            "add": round(v["add"], 2),
            "net": round(v["net"], 2),
        }
        for k, v in by_phase.items()
    },
    "top_trigger_markets": trigger_markets.most_common(20),
    "top_remove_markets": remove_markets.most_common(30),
    "top_add_markets": add_markets.most_common(30),
    "live_detection_rules": {
        "match_window_sec": 10,
        "remove_window_after_trigger_sec": 3,
        "add_delay_window_sec": [4, 11],
        "min_match_10s": 500,
        "min_remove": 50000,
        "min_add": 20000,
        "min_score": 9,
        "cluster_gap_sec": 20,
    }
}

out_json.write_text(json.dumps(signature, indent=2, ensure_ascii=False), encoding="utf-8")

with out_csv.open("w", newline="", encoding="utf-8") as f:
    fields = [
        "strategy", "cycles", "total_remove", "total_add", "net",
        "phase", "phase_count", "phase_remove", "phase_add", "phase_net"
    ]
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for phase, v in signature["phase_summary"].items():
        w.writerow({
            "strategy": signature["strategy"],
            "cycles": signature["cycles"],
            "total_remove": signature["total_remove"],
            "total_add": signature["total_add"],
            "net": signature["net"],
            "phase": phase,
            "phase_count": v["count"],
            "phase_remove": v["remove"],
            "phase_add": v["add"],
            "phase_net": v["net"],
        })

print("JSON:", out_json)
print("CSV:", out_csv)
print(json.dumps(signature, indent=2, ensure_ascii=False))
