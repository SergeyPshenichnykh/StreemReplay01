#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from bisect import bisect_left, bisect_right

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def read_actions(path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as f:
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

def level_key(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["side"],
        r["price"],
    )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--signals", default="replay/delta_10s_macro_min10/profile_engine_detected_fast.csv")
    ap.add_argument("--profile", default="replay/delta_10s_macro_min10/PLAYER_ENGINE_PROFILE.json")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/shadow_bot_orders.csv")
    ap.add_argument("--summary", default="replay/delta_10s_macro_min10/SHADOW_BOT_SUMMARY.txt")
    ap.add_argument("--stake", type=float, default=4.0)
    ap.add_argument("--top-per-signal", type=int, default=8)
    ap.add_argument("--min-add-level", type=float, default=500.0)
    ap.add_argument("--horizon-sec", type=float, default=30.0)
    ap.add_argument("--all-markets", action="store_true")
    args = ap.parse_args()

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    primary_markets = {r["market_type"] for r in profile["top_linked_markets"][:12]}

    actions = read_actions(args.actions)
    adds, add_times = split(actions, "VISIBLE_ADD")
    matches, match_times = split(actions, "MATCH")
    removes, remove_times = split(actions, "VISIBLE_REMOVE")

    signals = list(csv.DictReader(open(args.signals, newline="", encoding="utf-8")))

    orders = []

    for s in signals:
        signal_id = s["engine_signal_id"]
        t0 = ts(s["utc"])

        add_start = t0 + 4.0
        add_end = t0 + 11.0

        ai, aj = rng(add_times, add_start, add_end)
        add_rows = adds[ai:aj]

        levels = defaultdict(lambda: {
            "add_amount": 0.0,
            "add_count": 0,
            "first_ts": None,
            "last_ts": None,
        })

        meta = {}

        for r in add_rows:
            if not args.all_markets and r["market_type"] not in primary_markets:
                continue

            k = level_key(r)
            levels[k]["add_amount"] += r["_amount"]
            levels[k]["add_count"] += 1
            levels[k]["first_ts"] = r["_ts"] if levels[k]["first_ts"] is None else min(levels[k]["first_ts"], r["_ts"])
            levels[k]["last_ts"] = r["_ts"] if levels[k]["last_ts"] is None else max(levels[k]["last_ts"], r["_ts"])
            meta[k] = r

        ranked = [
            (k, v) for k, v in levels.items()
            if v["add_amount"] >= args.min_add_level
        ]
        ranked.sort(key=lambda kv: -kv[1]["add_amount"])
        ranked = ranked[:args.top_per_signal]

        for k, v in ranked:
            mt, mn, rn, side, price = k
            start = v["first_ts"]
            end = start + args.horizon_sec

            mi, mj = rng(match_times, start, end)
            ri, rj = rng(remove_times, start, end)

            future_match = 0.0
            future_remove = 0.0

            for m in matches[mi:mj]:
                if level_key(m) == k:
                    future_match += m["_amount"]

            for r in removes[ri:rj]:
                if level_key(r) == k:
                    future_remove += r["_amount"]

            fill_proxy_amount = min(args.stake, future_match)
            fill_proxy = "FULL" if future_match >= args.stake else ("PARTIAL" if future_match > 0 else "NO")
            cancel_proxy = "YES" if future_remove > 0 else "NO"

            orders.append({
                "signal_id": signal_id,
                "signal_utc": s["utc"],
                "minute": s["minute_from_kickoff"],
                "phase": s["phase"],
                "score": s["score"],
                "market_type": mt,
                "market_name": mn,
                "runner_name": rn,
                "side": side,
                "price": price,
                "stake": args.stake,
                "engine_add_amount": round(v["add_amount"], 2),
                "engine_add_count": v["add_count"],
                "first_add_utc": datetime.utcfromtimestamp(start).isoformat(timespec="milliseconds") + "Z",
                "future_match_amount": round(future_match, 2),
                "future_remove_amount": round(future_remove, 2),
                "fill_proxy": fill_proxy,
                "fill_proxy_amount": round(fill_proxy_amount, 2),
                "cancel_proxy": cancel_proxy,
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "signal_id","signal_utc","minute","phase","score",
        "market_type","market_name","runner_name","side","price",
        "stake","engine_add_amount","engine_add_count","first_add_utc",
        "future_match_amount","future_remove_amount",
        "fill_proxy","fill_proxy_amount","cancel_proxy",
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(orders)

    total_orders = len(orders)
    exposure = total_orders * args.stake
    full = sum(1 for r in orders if r["fill_proxy"] == "FULL")
    partial = sum(1 for r in orders if r["fill_proxy"] == "PARTIAL")
    any_fill = full + partial
    fill_stake = sum(float(r["fill_proxy_amount"]) for r in orders)
    total_engine_add = sum(float(r["engine_add_amount"]) for r in orders)
    total_future_match = sum(float(r["future_match_amount"]) for r in orders)
    total_future_remove = sum(float(r["future_remove_amount"]) for r in orders)

    lines = []
    lines.append("SHADOW BOT SUMMARY")
    lines.append("")
    lines.append("mode = follow_engine_add_matrix")
    lines.append("note = fill_proxy_only_no_queue_no_settlement_no_real_pnl")
    lines.append("")
    lines.append(f"signals = {len(signals)}")
    lines.append(f"orders = {total_orders}")
    lines.append(f"stake_per_order = {args.stake:.2f}")
    lines.append(f"shadow_exposure = {exposure:.2f}")
    lines.append(f"full_fill_proxy_orders = {full}")
    lines.append(f"partial_fill_proxy_orders = {partial}")
    lines.append(f"any_fill_proxy_orders = {any_fill}")
    lines.append(f"any_fill_proxy_rate = {100*any_fill/total_orders:.2f}" if total_orders else "any_fill_proxy_rate = 0")
    lines.append(f"full_fill_proxy_rate = {100*full/total_orders:.2f}" if total_orders else "full_fill_proxy_rate = 0")
    lines.append(f"fill_proxy_stake = {fill_stake:.2f}")
    lines.append(f"fill_proxy_stake_pct = {100*fill_stake/exposure:.2f}" if exposure else "fill_proxy_stake_pct = 0")
    lines.append(f"selected_engine_add_amount = {total_engine_add:.2f}")
    lines.append(f"future_match_amount_same_levels = {total_future_match:.2f}")
    lines.append(f"future_remove_amount_same_levels = {total_future_remove:.2f}")
    lines.append("")
    lines.append("OUTPUT")
    lines.append(f"orders_csv = {out}")

    summary = Path(args.summary)
    summary.write_text("\n".join(lines), encoding="utf-8")

    print(summary.read_text(encoding="utf-8"))

    print()
    print("TOP ORDERS BY FUTURE MATCH")
    for r in sorted(orders, key=lambda x: -float(x["future_match_amount"]))[:40]:
        print(
            "S", r["signal_id"],
            r["market_type"], "|", r["runner_name"],
            "|", r["side"], "@", r["price"],
            "engine_add=", r["engine_add_amount"],
            "future_match=", r["future_match_amount"],
            "future_remove=", r["future_remove_amount"],
            "fill=", r["fill_proxy"],
        )

if __name__ == "__main__":
    main()
