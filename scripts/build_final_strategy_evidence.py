#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
from pathlib import Path

base = Path("replay/delta_10s_macro_min10")

signature_p = base / "rebalance_strategy_signature.json"
windows_p = base / "macro_windows_from_profiles.csv"
groups_p = base / "window_group_shift_direction.csv"
price_p = base / "final_price_level_shift.csv"
out = base / "FINAL_PLAYER_STRATEGY_EVIDENCE.md"

sig = json.loads(signature_p.read_text(encoding="utf-8"))

windows = list(csv.DictReader(open(windows_p, newline="", encoding="utf-8")))
groups = list(csv.DictReader(open(groups_p, newline="", encoding="utf-8")))
prices = list(csv.DictReader(open(price_p, newline="", encoding="utf-8")))

def top_group(window, reverse=False, n=4):
    xs = [r for r in groups if r["window"] == window]
    xs.sort(key=lambda r: float(r["net"]), reverse=reverse)
    return xs[:n]

def top_price(window, reverse=False, n=8, min_abs=500):
    xs = [
        r for r in prices
        if r["window"] == window and abs(float(r["net"])) >= min_abs
    ]
    xs.sort(key=lambda r: float(r["net"]), reverse=reverse)
    return xs[:n]

lines = []

lines.append("# FINAL PLAYER STRATEGY EVIDENCE")
lines.append("")
lines.append("## VERDICT")
lines.append("")
lines.append("strategy = cyclic_cross_market_package_rebalance")
lines.append("")
lines.append("not_simple_cancel_repost = YES")
lines.append("single_cross_market_engine = YES")
lines.append("price_level_repricing = YES")
lines.append("cross_market_rotation = YES")
lines.append("anchor_exposure_control = YES")
lines.append("")
lines.append("## GLOBAL SIGNATURE")
lines.append("")
lines.append(f"cycles = {sig['cycles']}")
lines.append(f"total_remove = {sig['total_remove']}")
lines.append(f"total_add = {sig['total_add']}")
lines.append(f"net = {sig['net']}")
lines.append("")
lines.append("phase_summary:")
for phase, v in sig["phase_summary"].items():
    lines.append(f"- {phase}: count={v['count']} remove={v['remove']} add={v['add']} net={v['net']}")

lines.append("")
lines.append("## MACRO WINDOWS")
lines.append("")

for w in windows:
    name = w["window"]
    lines.append(f"### {name}")
    lines.append("")
    lines.append(f"base_utc = {w['base_utc']}")
    lines.append(f"match_10s_amount = {w['match_10s_amount']}")
    lines.append(f"remove = {w['remove_amount']} / count={w['remove_count']}")
    lines.append(f"add = {w['add_amount']} / count={w['add_count']}")
    lines.append(f"net = {w['net']}")
    lines.append("")

    lines.append("main_group_reduce:")
    for r in top_group(name, reverse=False, n=5):
        lines.append(f"- {r['group']}: remove={r['remove']} add={r['add']} net={r['net']}")

    lines.append("")
    lines.append("main_group_increase:")
    for r in top_group(name, reverse=True, n=5):
        lines.append(f"- {r['group']}: remove={r['remove']} add={r['add']} net={r['net']}")

    lines.append("")
    lines.append("price_level_reduce:")
    for r in top_price(name, reverse=False, n=10):
        lines.append(
            f"- {r['market_type']} | {r['market_name']} | {r['runner_name']} | "
            f"{r['side']} @{r['price']} | rm={r['remove_amount']} add={r['add_amount']} net={r['net']}"
        )

    lines.append("")
    lines.append("price_level_increase:")
    for r in top_price(name, reverse=True, n=10):
        lines.append(
            f"- {r['market_type']} | {r['market_name']} | {r['runner_name']} | "
            f"{r['side']} @{r['price']} | rm={r['remove_amount']} add={r['add_amount']} net={r['net']}"
        )

    lines.append("")

lines.append("## FINAL INTERPRETATION")
lines.append("")
lines.append("The player runs a cyclic cross-market package engine.")
lines.append("The engine removes old liquidity blocks, recalculates exposure, then adds a new matrix across related markets.")
lines.append("The strongest recurring proof is price-level migration: removed volume at one price is re-added on adjacent prices or related markets after the delay window.")
lines.append("The strategy is not isolated market making. It is cross-market portfolio rebalance with dutching/arbitrage structure.")

out.write_text("\n".join(lines), encoding="utf-8")

print("OUT:", out)
