#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter

def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")

def read_actions(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = parse_utc(r["utc"])
                r["_amount"] = float(r["amount"])
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])
    return rows

def read_packages(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["_remove_ts"] = parse_utc(r["remove_first_utc"])
            r["_add_ts"] = parse_utc(r["add_utc"])
            rows.append(r)
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--packages", default="replay/delta_10s_packages/package_trigger_report.csv")
    ap.add_argument("--out", default="replay/delta_10s_packages")
    ap.add_argument("--before-remove-sec", type=float, default=10)
    ap.add_argument("--after-add-sec", type=float, default=2)
    ap.add_argument("--min-amount", type=float, default=1)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    actions = read_actions(args.actions)
    packages = read_packages(args.packages)

    summary = []
    detail = []

    for p in packages:
        pid = p["package_id"]

        t0 = p["_remove_ts"] - args.before_remove_sec
        t1 = p["_add_ts"] + args.after_add_sec

        selected = [
            a for a in actions
            if t0 <= a["_ts"] <= t1 and a["_amount"] >= args.min_amount
        ]

        market_types = Counter(a["market_type"] for a in selected)
        actions_cnt = Counter(a["action"] for a in selected)

        remove_market_types = Counter(a["market_type"] for a in selected if a["action"] == "VISIBLE_REMOVE")
        add_market_types = Counter(a["market_type"] for a in selected if a["action"] == "VISIBLE_ADD")
        match_market_types = Counter(a["market_type"] for a in selected if a["action"] == "MATCH")

        total_match = sum(a["_amount"] for a in selected if a["action"] == "MATCH")
        total_remove = sum(a["_amount"] for a in selected if a["action"] == "VISIBLE_REMOVE")
        total_add = sum(a["_amount"] for a in selected if a["action"] == "VISIBLE_ADD")

        summary.append({
            "package_id": pid,
            "remove_first_utc": p["remove_first_utc"],
            "remove_last_utc": p["remove_last_utc"],
            "add_utc": p["add_utc"],
            "package_lines": p["lines"],
            "package_total": p["total_amount"],
            "context_actions": len(selected),
            "total_match": total_match,
            "total_remove": total_remove,
            "total_add": total_add,
            "all_market_types": "; ".join(f"{k}:{v}" for k, v in market_types.most_common()),
            "match_market_types": "; ".join(f"{k}:{v}" for k, v in match_market_types.most_common()),
            "remove_market_types": "; ".join(f"{k}:{v}" for k, v in remove_market_types.most_common()),
            "add_market_types": "; ".join(f"{k}:{v}" for k, v in add_market_types.most_common()),
            "actions_count": "; ".join(f"{k}:{v}" for k, v in actions_cnt.most_common()),
        })

        for a in selected:
            detail.append({
                "package_id": pid,
                "utc": a["utc"],
                "dt_to_remove_first": a["_ts"] - p["_remove_ts"],
                "dt_to_add": a["_ts"] - p["_add_ts"],
                "market_type": a["market_type"],
                "market_name": a["market_name"],
                "runner_name": a["runner_name"],
                "price": a["price"],
                "action": a["action"],
                "side": a["side"],
                "amount": a["amount"],
                "proof": a["proof"],
            })

    summary_file = out / "package_context_summary.csv"
    detail_file = out / "package_context_detail.csv"

    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id",
            "remove_first_utc",
            "remove_last_utc",
            "add_utc",
            "package_lines",
            "package_total",
            "context_actions",
            "total_match",
            "total_remove",
            "total_add",
            "all_market_types",
            "match_market_types",
            "remove_market_types",
            "add_market_types",
            "actions_count",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in summary:
            rr = dict(r)
            for k in ["total_match", "total_remove", "total_add"]:
                rr[k] = fnum(rr[k])
            w.writerow(rr)

    with open(detail_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id",
            "utc",
            "dt_to_remove_first",
            "dt_to_add",
            "market_type",
            "market_name",
            "runner_name",
            "price",
            "action",
            "side",
            "amount",
            "proof",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in detail:
            rr = dict(r)
            rr["dt_to_remove_first"] = fnum(rr["dt_to_remove_first"])
            rr["dt_to_add"] = fnum(rr["dt_to_add"])
            w.writerow(rr)

    print("DONE")
    print("summary:", summary_file)
    print("detail:", detail_file)

if __name__ == "__main__":
    main()
