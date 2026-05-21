#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict

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
            r["_remove_first_ts"] = parse_utc(r["remove_first_utc"])
            r["_remove_last_ts"] = parse_utc(r["remove_last_utc"])
            r["_add_ts"] = parse_utc(r["add_utc"])
            rows.append(r)
    return rows

def same_key(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["price"],
        r["side"],
    )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--packages", default="replay/delta_10s_packages/package_trigger_report.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro")
    ap.add_argument("--burst-sec", type=float, default=0.35)
    ap.add_argument("--trigger-lookback-sec", type=float, default=2.0)
    ap.add_argument("--min-amount", type=float, default=100.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--abs-tol", type=float, default=2.0)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    actions = read_actions(args.actions)
    packages = read_packages(args.packages)

    summary = []
    legs = []
    links = []
    triggers = []

    for p in packages:
        pid = p["package_id"]

        remove_start = p["_remove_first_ts"] - args.burst_sec
        remove_end = p["_remove_last_ts"] + args.burst_sec

        add_start = p["_add_ts"] - args.burst_sec
        add_end = p["_add_ts"] + args.burst_sec

        trigger_start = p["_remove_first_ts"] - args.trigger_lookback_sec
        trigger_end = p["_remove_first_ts"]

        remove_rows = [
            a for a in actions
            if remove_start <= a["_ts"] <= remove_end
            and a["action"] == "VISIBLE_REMOVE"
            and a["_amount"] >= args.min_amount
        ]

        add_rows = [
            a for a in actions
            if add_start <= a["_ts"] <= add_end
            and a["action"] == "VISIBLE_ADD"
            and a["_amount"] >= args.min_amount
        ]

        trigger_rows = [
            a for a in actions
            if trigger_start <= a["_ts"] <= trigger_end
            and a["action"] == "MATCH"
            and a["_amount"] >= args.min_amount
        ]

        for group_name, rows in [
            ("REMOVE_BURST", remove_rows),
            ("ADD_BURST", add_rows),
            ("TRIGGER_MATCH", trigger_rows),
        ]:
            for r in rows:
                legs.append({
                    "package_id": pid,
                    "group": group_name,
                    "utc": r["utc"],
                    "dt_to_remove_first": r["_ts"] - p["_remove_first_ts"],
                    "dt_to_add": r["_ts"] - p["_add_ts"],
                    "market_type": r["market_type"],
                    "market_name": r["market_name"],
                    "runner_name": r["runner_name"],
                    "price": r["price"],
                    "action": r["action"],
                    "side": r["side"],
                    "amount": r["amount"],
                    "proof": r["proof"],
                })

        remove_by_key = defaultdict(list)
        add_by_key = defaultdict(list)

        for r in remove_rows:
            remove_by_key[same_key(r)].append(r)

        for r in add_rows:
            add_by_key[same_key(r)].append(r)

        linked_count = 0
        linked_remove_amount = 0.0
        linked_add_amount = 0.0

        for k, rr in remove_by_key.items():
            aa = add_by_key.get(k, [])
            if not aa:
                continue

            for r in rr:
                best = None
                for a in aa:
                    diff = abs(r["_amount"] - a["_amount"])
                    rel = diff / max(r["_amount"], a["_amount"], 1e-9)
                    if diff <= args.abs_tol or rel <= args.rel_tol:
                        score = 100 - min(70, rel * 100)
                        cand = (score, diff, rel, a)
                        if best is None or cand[0] > best[0]:
                            best = cand

                if best:
                    score, diff, rel, a = best
                    linked_count += 1
                    linked_remove_amount += r["_amount"]
                    linked_add_amount += a["_amount"]

                    links.append({
                        "package_id": pid,
                        "score": score,
                        "amount_diff": diff,
                        "amount_rel_diff": rel,
                        "remove_utc": r["utc"],
                        "add_utc": a["utc"],
                        "lag_sec": a["_ts"] - r["_ts"],
                        "market_type": r["market_type"],
                        "market_name": r["market_name"],
                        "runner_name": r["runner_name"],
                        "price": r["price"],
                        "side": r["side"],
                        "remove_amount": r["amount"],
                        "add_amount": a["amount"],
                    })

        mt_remove = Counter(r["market_type"] for r in remove_rows)
        mt_add = Counter(r["market_type"] for r in add_rows)
        mt_trig = Counter(r["market_type"] for r in trigger_rows)

        summary.append({
            "package_id": pid,
            "remove_first_utc": p["remove_first_utc"],
            "remove_last_utc": p["remove_last_utc"],
            "add_utc": p["add_utc"],

            "trigger_match_count": len(trigger_rows),
            "trigger_match_amount": sum(r["_amount"] for r in trigger_rows),
            "remove_burst_count": len(remove_rows),
            "remove_burst_amount": sum(r["_amount"] for r in remove_rows),
            "add_burst_count": len(add_rows),
            "add_burst_amount": sum(r["_amount"] for r in add_rows),

            "linked_remove_add_count": linked_count,
            "linked_remove_amount": linked_remove_amount,
            "linked_add_amount": linked_add_amount,

            "trigger_markets": "; ".join(f"{k}:{v}" for k, v in mt_trig.most_common(20)),
            "remove_markets": "; ".join(f"{k}:{v}" for k, v in mt_remove.most_common(20)),
            "add_markets": "; ".join(f"{k}:{v}" for k, v in mt_add.most_common(20)),
        })

        for r in trigger_rows:
            triggers.append({
                "package_id": pid,
                "utc": r["utc"],
                "dt_to_remove_first": r["_ts"] - p["_remove_first_ts"],
                "market_type": r["market_type"],
                "market_name": r["market_name"],
                "runner_name": r["runner_name"],
                "price": r["price"],
                "amount": r["amount"],
            })

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

    write(out / "burst_summary.csv", summary, [
        "package_id",
        "remove_first_utc", "remove_last_utc", "add_utc",
        "trigger_match_count", "trigger_match_amount",
        "remove_burst_count", "remove_burst_amount",
        "add_burst_count", "add_burst_amount",
        "linked_remove_add_count", "linked_remove_amount", "linked_add_amount",
        "trigger_markets", "remove_markets", "add_markets",
    ])

    write(out / "burst_legs.csv", legs, [
        "package_id", "group", "utc", "dt_to_remove_first", "dt_to_add",
        "market_type", "market_name", "runner_name", "price",
        "action", "side", "amount", "proof",
    ])

    write(out / "burst_remove_add_links.csv", links, [
        "package_id", "score", "amount_diff", "amount_rel_diff",
        "remove_utc", "add_utc", "lag_sec",
        "market_type", "market_name", "runner_name", "price", "side",
        "remove_amount", "add_amount",
    ])

    write(out / "burst_triggers.csv", triggers, [
        "package_id", "utc", "dt_to_remove_first",
        "market_type", "market_name", "runner_name", "price", "amount",
    ])

    print("DONE")
    print("summary:", out / "burst_summary.csv")
    print("legs:", out / "burst_legs.csv")
    print("links:", out / "burst_remove_add_links.csv")
    print("triggers:", out / "burst_triggers.csv")

if __name__ == "__main__":
    main()
