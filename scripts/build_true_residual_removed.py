#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from collections import Counter

def amount(r):
    return float(r.get("amount") or r.get("remove_amount") or r.get("add_amount") or 0)

def removed_key(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["price"],
        r["side"],
        round(amount(r), 2),
    )

def read_csv(path):
    if not path.exists():
        return [], []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="replay/delta_10s_macro_min10")
    ap.add_argument("--ids", nargs="+", required=True)
    args = ap.parse_args()

    base = Path(args.base)

    for pid in args.ids:
        removed, fields = read_csv(base / f"package_{pid}_removed_only.csv")
        rotation, _ = read_csv(base / f"package_{pid}_rotation_links.csv")
        combo, _ = read_csv(base / f"package_{pid}_module_combo_links.csv")

        used = Counter()

        for r in rotation:
            used[(
                r["remove_market_type"],
                r["remove_market_name"],
                r["remove_runner_name"],
                r["remove_price"],
                r["remove_side"],
                round(float(r["remove_amount"]), 2),
            )] += 1

        for r in combo:
            for part in r.get("remove_legs", "").split(" || "):
                p = part.split("|")
                if len(p) < 6:
                    continue
                mt, mn, rn = p[0], p[1], p[2]
                price = p[3].lstrip("@")
                side = p[4]
                amt = round(float(p[5]), 2)
                used[(mt, mn, rn, price, side, amt)] += 1

        residual = []

        for r in removed:
            k = removed_key(r)
            if used[k] > 0:
                used[k] -= 1
            else:
                residual.append(r)

        out = base / f"package_{pid}_true_residual_removed.csv"
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(residual)

        print(
            pid,
            "count=", len(residual),
            "amount=", round(sum(amount(r) for r in residual), 2),
            "out=", out,
        )

if __name__ == "__main__":
    main()
