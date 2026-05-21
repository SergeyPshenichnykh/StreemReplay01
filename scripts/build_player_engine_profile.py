#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
from pathlib import Path
from collections import defaultdict

base = Path("replay/delta_10s_macro_min10")

summary_p = base / "all_signal_price_migration_summary.csv"
links_p = base / "all_signal_price_migration_links.csv"
unique_p = base / "final_engine_signals_unique_30s.csv"

out = base / "PLAYER_ENGINE_PROFILE.json"

summary = list(csv.DictReader(open(summary_p, newline="", encoding="utf-8")))
links = list(csv.DictReader(open(links_p, newline="", encoding="utf-8")))
unique = list(csv.DictReader(open(unique_p, newline="", encoding="utf-8")))

def f(x):
    return float(x)

def move_bin(x):
    x = f(x)
    ax = abs(x)
    if ax == 0:
        return "same_price"
    if ax <= 0.01:
        return "move_0.01"
    if ax <= 0.02:
        return "move_0.02"
    if ax <= 0.05:
        return "move_0.05"
    if ax <= 0.10:
        return "move_0.10"
    return "move_gt_0.10"

total_rm = sum(f(r["remove_total"]) for r in summary)
total_add = sum(f(r["add_total"]) for r in summary)
total_linked = sum(f(r["linked_total"]) for r in summary)

by_market = defaultdict(float)
by_move_bin = defaultdict(float)
by_direction = defaultdict(float)

for r in links:
    amt = f(r["amount_linked"])
    mv = f(r["price_move"])

    by_market[r["market_type"]] += amt
    by_move_bin[move_bin(mv)] += amt

    if mv > 0:
        by_direction["price_up"] += amt
    elif mv < 0:
        by_direction["price_down"] += amt
    else:
        by_direction["same_price"] += amt

profile = {
    "profile_name": "cyclic_cross_market_package_rebalance",
    "verdict": {
        "one_player_or_one_engine": "VERY_LIKELY",
        "simple_market_maker": "NO",
        "simple_scalper": "NO",
        "cross_market_dutching_arb_package": "YES",
        "cyclic_rebalance_after_partial_match": "YES",
        "price_level_migration_proven": "YES",
    },
    "detector_config": {
        "remove_window_sec": 8.0,
        "add_delay_min_sec": 4.0,
        "add_delay_max_sec": 11.0,
        "match_lookback_sec": 10.0,
        "unique_cluster_gap_sec": 30.0,
        "min_remove": 50000.0,
        "min_add": 30000.0,
        "min_score": 15,
    },
    "global_stats": {
        "unique_cycles": len(unique),
        "migration_signals": len(summary),
        "remove_total": round(total_rm, 2),
        "add_total": round(total_add, 2),
        "linked_total": round(total_linked, 2),
        "linked_pct_remove": round(100 * total_linked / total_rm, 2),
        "linked_pct_add": round(100 * total_linked / total_add, 2),
    },
    "price_move_direction": {
        k: {
            "amount": round(v, 2),
            "pct": round(100 * v / total_linked, 2),
        }
        for k, v in sorted(by_direction.items(), key=lambda x: -x[1])
    },
    "price_move_bins": {
        k: {
            "amount": round(v, 2),
            "pct": round(100 * v / total_linked, 2),
        }
        for k, v in sorted(by_move_bin.items(), key=lambda x: -x[1])
    },
    "top_linked_markets": [
        {
            "market_type": k,
            "amount": round(v, 2),
            "pct": round(100 * v / total_linked, 2),
        }
        for k, v in sorted(by_market.items(), key=lambda x: -x[1])[:30]
    ],
    "live_interpretation": {
        "signal_meaning": "mass remove old matrix -> wait/reprice -> add recalculated matrix",
        "main_evidence": "same runner/side price migration across related football markets",
        "primary_markets": [
            "ASIAN_HANDICAP",
            "MATCH_ODDS",
            "TOTAL_GOALS",
            "OVER_UNDER_15",
            "OVER_UNDER_25",
            "OVER_UNDER_35",
            "TEAM_TOTAL_GOALS",
            "CORRECT_SCORE",
            "HALF_TIME_SCORE",
        ],
    },
}

out.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")

print("OUT:", out)
print(json.dumps(profile, indent=2, ensure_ascii=False)[:12000])
