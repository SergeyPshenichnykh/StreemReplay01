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

def load_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def amount(r):
    if "amount" in r and r["amount"] != "":
        return float(r["amount"])
    if "remove_amount" in r and r["remove_amount"] != "":
        return float(r["remove_amount"])
    if "add_amount" in r and r["add_amount"] != "":
        return float(r["add_amount"])
    return 0.0

def same_amount(a, b, abs_tol, rel_tol):
    diff = abs(a - b)
    rel = diff / max(a, b, 1e-9)
    return diff <= abs_tol or rel <= rel_tol, diff, rel

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="replay/delta_10s_macro")
    ap.add_argument("--package-id", required=True)
    ap.add_argument("--abs-tol", type=float, default=2.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--out", default="replay/delta_10s_macro")
    args = ap.parse_args()

    base = Path(args.base)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pid = args.package_id

    removed = load_csv(base / f"package_{pid}_removed_only.csv")
    added = load_csv(base / f"package_{pid}_added_only.csv")

    used_add = set()
    links = []

    # greedy: спочатку найточніші збіги по сумі
    candidates = []

    for i, r in enumerate(removed):
        ra = amount(r)

        for j, a in enumerate(added):
            aa = amount(a)
            ok, diff, rel = same_amount(ra, aa, args.abs_tol, args.rel_tol)

            if not ok:
                continue

            same_market = r["market_type"] == a["market_type"]
            same_runner = (
                r["market_name"] == a["market_name"]
                and r["runner_name"] == a["runner_name"]
            )
            same_price = r["price"] == a["price"]
            same_side = r["side"] == a["side"]

            score = 100.0
            score -= min(70.0, rel * 100.0)

            if same_market:
                score += 10
            if same_runner:
                score += 10
            if same_price:
                score += 5
            if same_side:
                score += 3

            candidates.append((score, diff, rel, i, j, r, a))

    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

    used_removed = set()

    for score, diff, rel, i, j, r, a in candidates:
        if i in used_removed or j in used_add:
            continue

        used_removed.add(i)
        used_add.add(j)

        links.append({
            "package_id": pid,
            "score": score,
            "diff": diff,
            "rel_diff": rel,

            "remove_utc": r["utc"],
            "remove_market_type": r["market_type"],
            "remove_market_name": r["market_name"],
            "remove_runner_name": r["runner_name"],
            "remove_price": r["price"],
            "remove_side": r["side"],
            "remove_amount": amount(r),

            "add_utc": a["utc"],
            "add_market_type": a["market_type"],
            "add_market_name": a["market_name"],
            "add_runner_name": a["runner_name"],
            "add_price": a["price"],
            "add_side": a["side"],
            "add_amount": amount(a),

            "same_market_type": r["market_type"] == a["market_type"],
            "same_market_name": r["market_name"] == a["market_name"],
            "same_runner": r["runner_name"] == a["runner_name"],
            "same_price": r["price"] == a["price"],
            "same_side": r["side"] == a["side"],
            "transition": f'{r["market_type"]} -> {a["market_type"]}',
        })

    unlinked_removed = [
        r for i, r in enumerate(removed)
        if i not in used_removed
    ]

    unlinked_added = [
        a for j, a in enumerate(added)
        if j not in used_add
    ]

    def write(path, rows, fields):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                rr = dict(r)
                for k, v in list(rr.items()):
                    if isinstance(v, float):
                        rr[k] = fnum(v)
                w.writerow(rr)

    link_fields = [
        "package_id", "score", "diff", "rel_diff",
        "remove_utc", "remove_market_type", "remove_market_name",
        "remove_runner_name", "remove_price", "remove_side", "remove_amount",
        "add_utc", "add_market_type", "add_market_name",
        "add_runner_name", "add_price", "add_side", "add_amount",
        "same_market_type", "same_market_name", "same_runner",
        "same_price", "same_side", "transition",
    ]

    write(out / f"package_{pid}_rotation_links.csv", links, link_fields)

    # transition summary
    trans = defaultdict(lambda: {"count": 0, "remove_amount": 0.0, "add_amount": 0.0})

    for x in links:
        key = x["transition"]
        trans[key]["count"] += 1
        trans[key]["remove_amount"] += float(x["remove_amount"])
        trans[key]["add_amount"] += float(x["add_amount"])

    trans_rows = []
    for k, v in trans.items():
        trans_rows.append({
            "package_id": pid,
            "transition": k,
            "count": v["count"],
            "remove_amount": v["remove_amount"],
            "add_amount": v["add_amount"],
        })

    trans_rows.sort(key=lambda r: -r["remove_amount"])

    write(out / f"package_{pid}_rotation_transition_summary.csv", trans_rows, [
        "package_id", "transition", "count", "remove_amount", "add_amount"
    ])

    print("PACKAGE", pid)
    print("removed_only_count", len(removed), "amount", fnum(sum(amount(x) for x in removed)))
    print("added_only_count", len(added), "amount", fnum(sum(amount(x) for x in added)))
    print("rotation_links", len(links), "remove_amount", fnum(sum(float(x["remove_amount"]) for x in links)), "add_amount", fnum(sum(float(x["add_amount"]) for x in links)))
    print("unlinked_removed", len(unlinked_removed), "amount", fnum(sum(amount(x) for x in unlinked_removed)))
    print("unlinked_added", len(unlinked_added), "amount", fnum(sum(amount(x) for x in unlinked_added)))
    print("out:", out / f"package_{pid}_rotation_links.csv")
    print("summary:", out / f"package_{pid}_rotation_transition_summary.csv")

if __name__ == "__main__":
    main()
