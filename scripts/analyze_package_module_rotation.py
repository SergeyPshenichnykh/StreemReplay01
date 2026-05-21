#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from itertools import combinations

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

def desc(r):
    return (
        f'{r.get("market_type","")}|{r.get("market_name","")}|'
        f'{r.get("runner_name","")}|@{r.get("price","")}|'
        f'{r.get("side","")}|{fnum(amount(r))}'
    )

def same_amount(a, b, abs_tol, rel_tol):
    diff = abs(a - b)
    rel = diff / max(a, b, 1e-9)
    return diff <= abs_tol or rel <= rel_tol, diff, rel

def key(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["price"],
        r["side"],
    )

def greedy_one_to_one(removed, added, abs_tol, rel_tol):
    candidates = []

    for i, r in enumerate(removed):
        ra = amount(r)
        for j, a in enumerate(added):
            aa = amount(a)
            ok, diff, rel = same_amount(ra, aa, abs_tol, rel_tol)
            if not ok:
                continue

            same_market = r["market_type"] == a["market_type"]
            same_runner = r["market_name"] == a["market_name"] and r["runner_name"] == a["runner_name"]
            same_price = r["price"] == a["price"]
            same_side = r["side"] == a["side"]

            score = 100.0 - min(70.0, rel * 100.0)
            if same_market:
                score += 10
            if same_runner:
                score += 10
            if same_price:
                score += 5
            if same_side:
                score += 3

            candidates.append((score, diff, rel, i, j))

    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

    used_r = set()
    used_a = set()
    links = []

    for score, diff, rel, i, j in candidates:
        if i in used_r or j in used_a:
            continue
        used_r.add(i)
        used_a.add(j)
        links.append((i, j, score, diff, rel))

    return links, used_r, used_a

def find_combo(target, rows, used_idx, abs_tol, rel_tol, max_parts):
    available = [
        (i, amount(r), r)
        for i, r in enumerate(rows)
        if i not in used_idx and amount(r) > 0
    ]

    available.sort(key=lambda x: x[1], reverse=True)

    upper = target + max(abs_tol, target * rel_tol)
    best = None

    def dfs(start, chosen, s):
        nonlocal best

        if len(chosen) >= 2:
            diff = abs(target - s)
            rel = diff / max(target, s, 1e-9)
            if diff <= abs_tol or rel <= rel_tol:
                cand = (diff, rel, list(chosen), s)
                if best is None or cand[0] < best[0]:
                    best = cand

        if len(chosen) >= max_parts:
            return

        for k in range(start, len(available)):
            i, amt, r = available[k]
            ns = s + amt
            if ns > upper:
                continue
            dfs(k + 1, chosen + [(i, amt, r)], ns)

    dfs(0, [], 0.0)
    return best

def unit_multiple(x, unit):
    if unit <= 0:
        return ""
    m = x / unit
    common = [0.125, 0.25, 0.5, 1, 2, 3, 4, 5, 6, 8]
    nearest = min(common, key=lambda z: abs(z - m))
    return f"{fnum(m)}≈{nearest}x"

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            rr = dict(r)
            for k, v in list(rr.items()):
                if isinstance(v, float):
                    rr[k] = fnum(v)
            w.writerow(rr)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="replay/delta_10s_macro")
    ap.add_argument("--package-id", required=True)
    ap.add_argument("--out", default="replay/delta_10s_macro")
    ap.add_argument("--abs-tol", type=float, default=2.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--max-parts", type=int, default=5)
    ap.add_argument("--unit", type=float, default=841.18)
    args = ap.parse_args()

    base = Path(args.base)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pid = args.package_id

    removed = load_csv(base / f"package_{pid}_removed_only.csv")
    added = load_csv(base / f"package_{pid}_added_only.csv")

    one_links, used_r, used_a = greedy_one_to_one(
        removed, added, args.abs_tol, args.rel_tol
    )

    combo_links = []

    # 1 removed -> many added
    for i, r in enumerate(removed):
        if i in used_r:
            continue

        target = amount(r)
        best = find_combo(
            target=target,
            rows=added,
            used_idx=used_a,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
            max_parts=args.max_parts,
        )

        if not best:
            continue

        diff, rel, chosen, combo_sum = best

        for j, amt, a in chosen:
            used_a.add(j)
        used_r.add(i)

        combo_links.append({
            "package_id": pid,
            "type": "ONE_REMOVE_TO_MANY_ADDS",
            "remove_count": 1,
            "add_count": len(chosen),
            "remove_sum": target,
            "add_sum": combo_sum,
            "diff": diff,
            "rel_diff": rel,
            "remove_unit": unit_multiple(target, args.unit),
            "add_unit": unit_multiple(combo_sum, args.unit),
            "remove_legs": desc(r),
            "add_legs": " || ".join(desc(a) for _, _, a in chosen),
        })

    # many removed -> 1 added
    for j, a in enumerate(added):
        if j in used_a:
            continue

        target = amount(a)
        best = find_combo(
            target=target,
            rows=removed,
            used_idx=used_r,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
            max_parts=args.max_parts,
        )

        if not best:
            continue

        diff, rel, chosen, combo_sum = best

        for i, amt, r in chosen:
            used_r.add(i)
        used_a.add(j)

        combo_links.append({
            "package_id": pid,
            "type": "MANY_REMOVES_TO_ONE_ADD",
            "remove_count": len(chosen),
            "add_count": 1,
            "remove_sum": combo_sum,
            "add_sum": target,
            "diff": diff,
            "rel_diff": rel,
            "remove_unit": unit_multiple(combo_sum, args.unit),
            "add_unit": unit_multiple(target, args.unit),
            "remove_legs": " || ".join(desc(r) for _, _, r in chosen),
            "add_legs": desc(a),
        })

    unlinked_removed = [r for i, r in enumerate(removed) if i not in used_r]
    unlinked_added = [a for j, a in enumerate(added) if j not in used_a]

    fields = [
        "package_id", "type",
        "remove_count", "add_count",
        "remove_sum", "add_sum", "diff", "rel_diff",
        "remove_unit", "add_unit",
        "remove_legs", "add_legs",
    ]

    write_csv(out / f"package_{pid}_module_combo_links.csv", combo_links, fields)

    summary = [{
        "package_id": pid,
        "removed_only_count": len(removed),
        "removed_only_amount": sum(amount(x) for x in removed),
        "added_only_count": len(added),
        "added_only_amount": sum(amount(x) for x in added),
        "one_to_one_count": len(one_links),
        "one_to_one_remove_amount": sum(amount(removed[i]) for i, _, _, _, _ in one_links),
        "one_to_one_add_amount": sum(amount(added[j]) for _, j, _, _, _ in one_links),
        "combo_count": len(combo_links),
        "combo_remove_amount": sum(float(x["remove_sum"]) for x in combo_links),
        "combo_add_amount": sum(float(x["add_sum"]) for x in combo_links),
        "unlinked_removed_count": len(unlinked_removed),
        "unlinked_removed_amount": sum(amount(x) for x in unlinked_removed),
        "unlinked_added_count": len(unlinked_added),
        "unlinked_added_amount": sum(amount(x) for x in unlinked_added),
    }]

    write_csv(out / f"package_{pid}_module_combo_summary.csv", summary, [
        "package_id",
        "removed_only_count", "removed_only_amount",
        "added_only_count", "added_only_amount",
        "one_to_one_count", "one_to_one_remove_amount", "one_to_one_add_amount",
        "combo_count", "combo_remove_amount", "combo_add_amount",
        "unlinked_removed_count", "unlinked_removed_amount",
        "unlinked_added_count", "unlinked_added_amount",
    ])

    print("PACKAGE", pid)
    for k, v in summary[0].items():
        print(k, fnum(v) if isinstance(v, float) else v)

if __name__ == "__main__":
    main()
