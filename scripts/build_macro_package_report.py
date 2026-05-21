#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime

def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            out = {}
            for k in fields:
                v = r.get(k, "")
                if isinstance(v, float):
                    out[k] = fnum(v)
                else:
                    out[k] = v
            w.writerow(out)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context-detail", default="replay/delta_10s_packages/package_context_detail.csv")
    ap.add_argument("--core-lines", default="replay/delta_10s_packages/package_lines_merged.csv")
    ap.add_argument("--trigger-report", default="replay/delta_10s_packages/package_trigger_report.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro")
    ap.add_argument("--min-amount", type=float, default=100.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    context = read_csv(args.context_detail)
    core = read_csv(args.core_lines)
    triggers = read_csv(args.trigger_report)

    core_by_pkg = defaultdict(list)
    for r in core:
        core_by_pkg[r["package_id"]].append(r)

    trig_by_pkg = {r["package_id"]: r for r in triggers}

    package_rows = []
    market_matrix_rows = []
    candidate_rows = []

    for pid in sorted(set(r["package_id"] for r in context), key=lambda x: int(x)):
        rows = [r for r in context if r["package_id"] == pid]

        exact_rows = []
        for r in rows:
            try:
                amt = float(r["amount"])
            except Exception:
                continue
            if amt >= args.min_amount:
                exact_rows.append(r)

        action_cnt = Counter(r["action"] for r in exact_rows)
        market_cnt = Counter(r["market_type"] for r in exact_rows)
        match_market_cnt = Counter(r["market_type"] for r in exact_rows if r["action"] == "MATCH")
        remove_market_cnt = Counter(r["market_type"] for r in exact_rows if r["action"] == "VISIBLE_REMOVE")
        add_market_cnt = Counter(r["market_type"] for r in exact_rows if r["action"] == "VISIBLE_ADD")

        total_match = sum(float(r["amount"]) for r in exact_rows if r["action"] == "MATCH")
        total_remove = sum(float(r["amount"]) for r in exact_rows if r["action"] == "VISIBLE_REMOVE")
        total_add = sum(float(r["amount"]) for r in exact_rows if r["action"] == "VISIBLE_ADD")

        core_rows = core_by_pkg.get(pid, [])
        trigger = trig_by_pkg.get(pid, {})

        package_rows.append({
            "package_id": pid,
            "remove_first_utc": trigger.get("remove_first_utc", ""),
            "remove_last_utc": trigger.get("remove_last_utc", ""),
            "add_utc": trigger.get("add_utc", ""),
            "core_lines": len(core_rows),
            "core_total": sum(float(x["amount"]) for x in core_rows) if core_rows else 0,
            "context_lines_ge_min": len(exact_rows),
            "total_match_ge_min": total_match,
            "total_remove_ge_min": total_remove,
            "total_add_ge_min": total_add,
            "actions": "; ".join(f"{k}:{v}" for k, v in action_cnt.most_common()),
            "top_match_markets": "; ".join(f"{k}:{v}" for k, v in match_market_cnt.most_common(15)),
            "top_remove_markets": "; ".join(f"{k}:{v}" for k, v in remove_market_cnt.most_common(15)),
            "top_add_markets": "; ".join(f"{k}:{v}" for k, v in add_market_cnt.most_common(15)),
            "all_markets": "; ".join(f"{k}:{v}" for k, v in market_cnt.most_common(30)),
            "matches_before_1s": trigger.get("matches_before_1s", ""),
            "matches_before_2s": trigger.get("matches_before_2s", ""),
            "matches_before_5s": trigger.get("matches_before_5s", ""),
            "matches_before_10s": trigger.get("matches_before_10s", ""),
        })

        for mt in sorted(market_cnt):
            mt_rows = [r for r in exact_rows if r["market_type"] == mt]
            market_matrix_rows.append({
                "package_id": pid,
                "market_type": mt,
                "actions_total": len(mt_rows),
                "match_count": sum(1 for r in mt_rows if r["action"] == "MATCH"),
                "remove_count": sum(1 for r in mt_rows if r["action"] == "VISIBLE_REMOVE"),
                "add_count": sum(1 for r in mt_rows if r["action"] == "VISIBLE_ADD"),
                "match_amount": sum(float(r["amount"]) for r in mt_rows if r["action"] == "MATCH"),
                "remove_amount": sum(float(r["amount"]) for r in mt_rows if r["action"] == "VISIBLE_REMOVE"),
                "add_amount": sum(float(r["amount"]) for r in mt_rows if r["action"] == "VISIBLE_ADD"),
            })

        # candidate full-package legs: only visible remove/add >= min_amount, close to package window
        for r in exact_rows:
            if r["action"] not in {"VISIBLE_REMOVE", "VISIBLE_ADD", "MATCH"}:
                continue
            candidate_rows.append({
                "package_id": pid,
                "utc": r["utc"],
                "dt_to_remove_first": r["dt_to_remove_first"],
                "dt_to_add": r["dt_to_add"],
                "market_type": r["market_type"],
                "market_name": r["market_name"],
                "runner_name": r["runner_name"],
                "price": r["price"],
                "action": r["action"],
                "side": r["side"],
                "amount": r["amount"],
                "proof": r["proof"],
            })

    write_csv(
        out / "macro_package_summary.csv",
        package_rows,
        [
            "package_id", "remove_first_utc", "remove_last_utc", "add_utc",
            "core_lines", "core_total",
            "context_lines_ge_min",
            "total_match_ge_min", "total_remove_ge_min", "total_add_ge_min",
            "actions",
            "top_match_markets", "top_remove_markets", "top_add_markets",
            "all_markets",
            "matches_before_1s", "matches_before_2s", "matches_before_5s", "matches_before_10s",
        ],
    )

    write_csv(
        out / "macro_package_market_matrix.csv",
        market_matrix_rows,
        [
            "package_id", "market_type", "actions_total",
            "match_count", "remove_count", "add_count",
            "match_amount", "remove_amount", "add_amount",
        ],
    )

    write_csv(
        out / "macro_package_candidate_legs.csv",
        candidate_rows,
        [
            "package_id", "utc", "dt_to_remove_first", "dt_to_add",
            "market_type", "market_name", "runner_name", "price",
            "action", "side", "amount", "proof",
        ],
    )

    print("DONE")
    print("out:", out)
    print("summary:", out / "macro_package_summary.csv")
    print("matrix:", out / "macro_package_market_matrix.csv")
    print("legs:", out / "macro_package_candidate_legs.csv")

if __name__ == "__main__":
    main()
