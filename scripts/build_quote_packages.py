#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from collections import defaultdict, Counter

def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--links", default="replay/delta_10s_causal/causal_links_5_10s.csv")
    ap.add_argument("--out", default="replay/delta_10s_packages")
    ap.add_argument("--min-package-lines", type=int, default=5)
    ap.add_argument("--relation", default="VISIBLE_REMOVE_THEN_VISIBLE_ADD")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    packages = defaultdict(list)

    with open(args.links, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["relation"] != args.relation:
                continue
            if r["tag"] != "SAME_LEVEL":
                continue

            key = (
                r["src_utc"],
                r["trg_utc"],
                r["lag_sec"],
            )
            packages[key].append(r)

    rows = []

    for (src_utc, trg_utc, lag), items in packages.items():
        if len(items) < args.min_package_lines:
            continue

        markets = Counter()
        market_types = Counter()
        amounts = Counter()
        sides = Counter()

        total_amount = 0.0
        cs_count = 0
        ou_count = 0
        tg_count = 0

        examples = []

        for r in items:
            mt = r["src_market_type"]
            mn = r["src_market_name"]
            rn = r["src_runner_name"]
            price = r["src_price"]
            side = r["src_side"]
            amt = float(r["src_amount"])

            markets[mn] += 1
            market_types[mt] += 1
            amounts[r["src_amount"]] += 1
            sides[side] += 1
            total_amount += amt

            if "CORRECT_SCORE" in mt:
                cs_count += 1
            if "OVER_UNDER" in mt:
                ou_count += 1
            if "TOTAL_GOALS" in mt:
                tg_count += 1

            if len(examples) < 12:
                examples.append(f"{mt}|{mn}|{rn}|@{price}|{side}|{r['src_amount']}")

        repeated_amounts = [
            f"{amt}x{cnt}"
            for amt, cnt in amounts.most_common(10)
            if cnt >= 2
        ]

        rows.append({
            "src_utc": src_utc,
            "trg_utc": trg_utc,
            "lag_sec": lag,
            "package_lines": len(items),
            "total_amount": total_amount,
            "market_types": "; ".join(f"{k}:{v}" for k, v in market_types.most_common()),
            "markets_count": len(markets),
            "cs_count": cs_count,
            "ou_count": ou_count,
            "tg_count": tg_count,
            "sides": "; ".join(f"{k}:{v}" for k, v in sides.most_common()),
            "repeated_amounts": "; ".join(repeated_amounts),
            "examples": " || ".join(examples),
        })

    rows.sort(key=lambda r: (-int(r["package_lines"]), -float(r["total_amount"])))

    fields = [
        "src_utc",
        "trg_utc",
        "lag_sec",
        "package_lines",
        "total_amount",
        "market_types",
        "markets_count",
        "cs_count",
        "ou_count",
        "tg_count",
        "sides",
        "repeated_amounts",
        "examples",
    ]

    out_file = out / "quote_packages_remove_add_5_10s.csv"

    with open(out_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            rr = dict(r)
            rr["total_amount"] = fnum(rr["total_amount"])
            w.writerow(rr)

    print("DONE")
    print("packages:", len(rows))
    print("out:", out_file)

if __name__ == "__main__":
    main()
