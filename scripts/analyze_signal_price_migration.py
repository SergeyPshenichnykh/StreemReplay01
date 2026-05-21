#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

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
    return rows

def read_signals(path):
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def key_runner(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["side"],
    )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--signals", default="replay/delta_10s_macro_min10/final_engine_signals_unique_30s.csv")
    ap.add_argument("--signal-id", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--top", type=int, default=60)
    args = ap.parse_args()

    signals = read_signals(args.signals)
    sig = next(r for r in signals if r["unique_cluster_id"] == str(args.signal_id))
    t0 = ts(sig["utc"])

    actions = read_actions(args.actions)

    removes = [
        r for r in actions
        if r["action"] == "VISIBLE_REMOVE"
        and t0 <= r["_ts"] <= t0 + 8
    ]

    adds = [
        r for r in actions
        if r["action"] == "VISIBLE_ADD"
        and t0 + 4 <= r["_ts"] <= t0 + 11
    ]

    rm = defaultdict(float)
    ad = defaultdict(float)

    for r in removes:
        rm[key_runner(r) + (r["price"],)] += r["_amount"]

    for r in adds:
        ad[key_runner(r) + (r["price"],)] += r["_amount"]

    rm_by_runner = defaultdict(list)
    ad_by_runner = defaultdict(list)

    for k, amount in rm.items():
        runner_key = k[:4]
        price = float(k[4])
        rm_by_runner[runner_key].append({"price": price, "amount": amount})

    for k, amount in ad.items():
        runner_key = k[:4]
        price = float(k[4])
        ad_by_runner[runner_key].append({"price": price, "amount": amount})

    migrations = []
    unpaired_rm = []
    unpaired_add = []

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

                migrations.append({
                    "market_type": runner_key[0],
                    "market_name": runner_key[1],
                    "runner_name": runner_key[2],
                    "side": runner_key[3],
                    "remove_price": r["price"],
                    "add_price": adl[i]["price"],
                    "amount_linked": take,
                    "price_move": adl[i]["price"] - r["price"],
                    "remove_amount_at_price": r["amount"],
                    "add_amount_at_price": adl[i]["amount"],
                })

                used_add[i] += take
                left -= take

                if left <= 0.000001:
                    break

            if left > 0.000001:
                unpaired_rm.append({
                    "market_type": runner_key[0],
                    "market_name": runner_key[1],
                    "runner_name": runner_key[2],
                    "side": runner_key[3],
                    "price": r["price"],
                    "amount": left,
                })

        for i, a in enumerate(adl):
            left = a["amount"] - used_add[i]
            if left > 0.000001:
                unpaired_add.append({
                    "market_type": runner_key[0],
                    "market_name": runner_key[1],
                    "runner_name": runner_key[2],
                    "side": runner_key[3],
                    "price": a["price"],
                    "amount": left,
                })

    migrations.sort(key=lambda x: -x["amount_linked"])
    unpaired_rm.sort(key=lambda x: -x["amount"])
    unpaired_add.sort(key=lambda x: -x["amount"])

    out = Path(args.out or f"replay/delta_10s_macro_min10/signal_{args.signal_id}_price_migration.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "market_type","market_name","runner_name","side",
        "remove_price","add_price","price_move",
        "amount_linked","remove_amount_at_price","add_amount_at_price"
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in migrations:
            w.writerow({
                **r,
                "price_move": round(r["price_move"], 6),
                "amount_linked": round(r["amount_linked"], 2),
                "remove_amount_at_price": round(r["remove_amount_at_price"], 2),
                "add_amount_at_price": round(r["add_amount_at_price"], 2),
            })

    print("SIGNAL", args.signal_id, sig["utc"])
    print("remove_total", round(sum(r["_amount"] for r in removes), 2))
    print("add_total", round(sum(r["_amount"] for r in adds), 2))
    print("linked_total", round(sum(r["amount_linked"] for r in migrations), 2))
    print("out", out)

    print()
    print("TOP MIGRATIONS")
    for r in migrations[:args.top]:
        print(
            r["market_type"], "|", r["market_name"], "|", r["runner_name"],
            "|", r["side"],
            "@", r["remove_price"], "->", r["add_price"],
            "move=", round(r["price_move"], 6),
            "amount=", round(r["amount_linked"], 2),
        )

    print()
    print("TOP UNPAIRED REMOVE")
    for r in unpaired_rm[:20]:
        print(
            r["market_type"], "|", r["market_name"], "|", r["runner_name"],
            "|", r["side"], "@", r["price"],
            "amount=", round(r["amount"], 2),
        )

    print()
    print("TOP UNPAIRED ADD")
    for r in unpaired_add[:20]:
        print(
            r["market_type"], "|", r["market_name"], "|", r["runner_name"],
            "|", r["side"], "@", r["price"],
            "amount=", round(r["amount"], 2),
        )

if __name__ == "__main__":
    main()
