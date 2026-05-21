#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def amt(r):
    return float(r.get("amount") or r.get("remove_amount") or r.get("add_amount") or 0)

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--residual", default="replay/delta_10s_macro_min10/package_1_true_residual_removed.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/package_1_residual_vs_match.csv")
    ap.add_argument("--lookback-sec", type=float, default=10.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--abs-tol", type=float, default=2.0)
    args = ap.parse_args()

    actions = read_csv(Path(args.actions))
    residual = read_csv(Path(args.residual))

    for r in actions:
        r["_ts"] = parse_utc(r["utc"])
        r["_amount"] = amt(r)

    for r in residual:
        r["_ts"] = parse_utc(r["utc"])
        r["_amount"] = amt(r)

    matches = [r for r in actions if r["action"] == "MATCH"]

    rows = []

    for res in residual:
        t0 = res["_ts"] - args.lookback_sec
        t1 = res["_ts"]

        candidates = [
            m for m in matches
            if t0 <= m["_ts"] <= t1
        ]

        same_market = []
        same_runner = []
        same_price = []
        amount_like = []

        for m in candidates:
            diff = abs(res["_amount"] - m["_amount"])
            rel = diff / max(res["_amount"], m["_amount"], 1e-9)

            if m["market_id"] == res.get("market_id", ""):
                same_market.append(m)

            if (
                m["market_type"] == res["market_type"]
                and m["market_name"] == res["market_name"]
                and m["runner_name"] == res["runner_name"]
            ):
                same_runner.append(m)

            if (
                m["market_type"] == res["market_type"]
                and m["market_name"] == res["market_name"]
                and m["runner_name"] == res["runner_name"]
                and m["price"] == res["price"]
            ):
                same_price.append(m)

            if diff <= args.abs_tol or rel <= args.rel_tol:
                amount_like.append(m)

        total_match_all = sum(m["_amount"] for m in candidates)
        total_match_same_runner = sum(m["_amount"] for m in same_runner)
        total_match_same_price = sum(m["_amount"] for m in same_price)
        total_match_amount_like = sum(m["_amount"] for m in amount_like)

        best = sorted(
            candidates,
            key=lambda m: (
                abs(res["_amount"] - m["_amount"]) / max(res["_amount"], m["_amount"], 1e-9),
                abs(res["_ts"] - m["_ts"])
            )
        )[:10]

        rows.append({
            "residual_utc": res["utc"],
            "residual_market_type": res["market_type"],
            "residual_market_name": res["market_name"],
            "residual_runner_name": res["runner_name"],
            "residual_price": res["price"],
            "residual_side": res["side"],
            "residual_amount": res["amount"],

            "matches_lookback_count": len(candidates),
            "matches_lookback_amount": fnum(total_match_all),
            "same_runner_match_count": len(same_runner),
            "same_runner_match_amount": fnum(total_match_same_runner),
            "same_price_match_count": len(same_price),
            "same_price_match_amount": fnum(total_match_same_price),
            "amount_like_match_count": len(amount_like),
            "amount_like_match_amount": fnum(total_match_amount_like),

            "best_matches": " || ".join(
                f'{m["utc"]}|{m["market_type"]}|{m["market_name"]}|{m["runner_name"]}|@{m["price"]}|{m["_amount"]}'
                for m in best
            ),
        })

    out = Path(args.out)
    with out.open("w", newline="", encoding="utf-8") as f:
        fields = list(rows[0].keys())
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print("DONE:", out)

if __name__ == "__main__":
    main()
