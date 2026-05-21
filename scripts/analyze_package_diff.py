#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from collections import defaultdict
from pathlib import Path

def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")

def key(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["price"],
        r["side"],
    )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legs", default="replay/delta_10s_macro/burst_legs.csv")
    ap.add_argument("--package-id", default="1")
    ap.add_argument("--out", default="replay/delta_10s_macro")
    ap.add_argument("--abs-tol", type=float, default=2.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    remove = []
    add = []

    with open(args.legs, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["package_id"] != args.package_id:
                continue
            if r["group"] == "REMOVE_BURST":
                remove.append(r)
            elif r["group"] == "ADD_BURST":
                add.append(r)

    add_by_key = defaultdict(list)
    for a in add:
        add_by_key[key(a)].append(a)

    used_add = set()
    exact = []
    removed_only = []

    for r in remove:
        rk = key(r)
        r_amt = float(r["amount"])
        best = None

        for i, a in enumerate(add_by_key.get(rk, [])):
            aid = id(a)
            if aid in used_add:
                continue

            a_amt = float(a["amount"])
            diff = abs(r_amt - a_amt)
            rel = diff / max(r_amt, a_amt, 1e-9)

            if diff <= args.abs_tol or rel <= args.rel_tol:
                cand = (diff, rel, a)
                if best is None or cand[0] < best[0]:
                    best = cand

        if best:
            diff, rel, a = best
            used_add.add(id(a))
            exact.append({
                "market_type": r["market_type"],
                "market_name": r["market_name"],
                "runner_name": r["runner_name"],
                "price": r["price"],
                "side": r["side"],
                "remove_utc": r["utc"],
                "add_utc": a["utc"],
                "remove_amount": r["amount"],
                "add_amount": a["amount"],
                "diff": diff,
                "rel": rel,
            })
        else:
            removed_only.append(r)

    added_only = [a for a in add if id(a) not in used_add]

    def write(path, rows, fields):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                rr = dict(r)
                for k, v in rr.items():
                    if isinstance(v, float):
                        rr[k] = fnum(v)
                w.writerow(rr)

    write(out / f"package_{args.package_id}_exact_restored.csv", exact, [
        "market_type", "market_name", "runner_name", "price", "side",
        "remove_utc", "add_utc", "remove_amount", "add_amount", "diff", "rel",
    ])

    write(out / f"package_{args.package_id}_removed_only.csv", removed_only, [
        "package_id", "group", "utc", "dt_to_remove_first", "dt_to_add",
        "market_type", "market_name", "runner_name", "price",
        "action", "side", "amount", "proof",
    ])

    write(out / f"package_{args.package_id}_added_only.csv", added_only, [
        "package_id", "group", "utc", "dt_to_remove_first", "dt_to_add",
        "market_type", "market_name", "runner_name", "price",
        "action", "side", "amount", "proof",
    ])

    print("PACKAGE", args.package_id)
    print("remove_count", len(remove), "remove_amount", fnum(sum(float(x["amount"]) for x in remove)))
    print("add_count", len(add), "add_amount", fnum(sum(float(x["amount"]) for x in add)))
    print("exact_count", len(exact), "exact_remove_amount", fnum(sum(float(x["remove_amount"]) for x in exact)))
    print("removed_only_count", len(removed_only), "removed_only_amount", fnum(sum(float(x["amount"]) for x in removed_only)))
    print("added_only_count", len(added_only), "added_only_amount", fnum(sum(float(x["amount"]) for x in added_only)))
    print("files written to", out)

if __name__ == "__main__":
    main()
