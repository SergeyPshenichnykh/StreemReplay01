#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import re
from bisect import bisect_left, bisect_right
from pathlib import Path

EPS = 1e-12


def fnum(x):
    try:
        x = float(x)
    except:
        return x
    if abs(x) < EPS:
        return "0"
    return f"{x:.10f}".rstrip("0").rstrip(".")


def is_ou(r):
    s = (r.get("market_type", "") + " " + r.get("market_name", "")).upper()
    return "OVER_UNDER" in s or "OVER/UNDER" in s or "UNDER" in s


def is_cs(r):
    s = (r.get("market_type", "") + " " + r.get("market_name", "")).upper()
    return "CORRECT_SCORE" in s or "CORRECT SCORE" in s


def total_value(r):
    s = r.get("market_name", "") + " " + r.get("runner_name", "")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    return m.group(1) if m else ""


def relation(a, b):
    aa = a["action"]
    bb = b["action"]

    if aa == "MATCH" and bb == "VISIBLE_ADD":
        return "MATCH_THEN_ADD_CANDIDATE"
    if aa == "VISIBLE_REMOVE" and bb == "VISIBLE_ADD":
        return "REMOVE_THEN_ADD_REPOST"
    if aa == "VISIBLE_ADD" and bb == "MATCH":
        return "ADD_THEN_MATCH_CANDIDATE"
    if aa == "VISIBLE_ADD" and bb == "VISIBLE_REMOVE":
        return "ADD_THEN_REMOVE_CANCEL"
    if aa == "MATCH" and bb == "VISIBLE_REMOVE":
        return "MATCH_THEN_REMOVE"
    if aa == "VISIBLE_REMOVE" and bb == "MATCH":
        return "REMOVE_THEN_MATCH"
    return f"{aa}_THEN_{bb}"


def tag(a, b):
    same_market = a["market_id"] == b["market_id"]
    same_runner = a["runner_id"] == b["runner_id"]
    same_price = a["price"] == b["price"]
    same_side = a["side"] == b["side"]

    if same_market and same_runner and same_price and same_side:
        return "SAME_LEVEL"
    if same_market and same_runner and same_price:
        return "SAME_PRICE_OTHER_SIDE"
    if same_market and same_runner:
        return "SAME_RUNNER_PRICE_SHIFT"
    if same_market:
        return "SAME_MARKET_OTHER_RUNNER"
    if is_ou(a) and is_ou(b):
        return "CROSS_TOTAL_CANDIDATE"
    if is_cs(a) or is_cs(b):
        return "CORRECT_SCORE_CANDIDATE"
    return "CROSS_MARKET"


def score_link(a, b, lag, rel_diff, tg, rel):
    score = 100.0
    score -= min(70.0, rel_diff * 100.0)
    score -= min(20.0, abs(lag) / max(1.0, rel.latency_sec) * 20.0)

    if tg == "SAME_LEVEL":
        score += 30
    elif tg == "SAME_PRICE_OTHER_SIDE":
        score += 24
    elif tg == "SAME_RUNNER_PRICE_SHIFT":
        score += 18
    elif tg == "SAME_MARKET_OTHER_RUNNER":
        score += 10
    elif tg == "CROSS_TOTAL_CANDIDATE":
        score += 5
    elif tg == "CORRECT_SCORE_CANDIDATE":
        score += 3

    rr = relation(a, b)
    if rr in ("MATCH_THEN_ADD_CANDIDATE", "REMOVE_THEN_ADD_REPOST", "ADD_THEN_MATCH_CANDIDATE"):
        score += 8

    return score


def read_actions(path, min_amount, exact_only):
    rows = []
    summary = {}

    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                amt = float(r["amount"])
                pt = int(float(r["pt_ms"]))
            except:
                continue

            if amt < min_amount:
                continue

            if exact_only and r.get("proof") != "EXACT":
                continue

            r["_amount"] = amt
            r["_pt"] = pt
            r["_total"] = total_value(r)

            rows.append(r)

            key = (r.get("action", ""), r.get("side", ""), r.get("proof", ""))
            if key not in summary:
                summary[key] = {"count": 0, "sum": 0.0, "max": 0.0}
            summary[key]["count"] += 1
            summary[key]["sum"] += amt
            summary[key]["max"] = max(summary[key]["max"], amt)

    rows.sort(key=lambda x: x["_pt"])
    return rows, summary


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
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--out", default="replay/delta_10s_links_exact")
    ap.add_argument("--latency-sec", type=float, default=10.0)
    ap.add_argument("--min-amount", type=float, default=100.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--abs-tol", type=float, default=2.0)
    ap.add_argument("--max-links-per-action", type=int, default=10)
    ap.add_argument("--include-if-hit", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows, summary = read_actions(
        args.actions,
        min_amount=args.min_amount,
        exact_only=not args.include_if_hit,
    )

    print("loaded_actions=", len(rows))

    times = [r["_pt"] for r in rows]
    links = []

    latency_ms = int(args.latency_sec * 1000)

    for i, a in enumerate(rows):
        ta = a["_pt"]
        left = bisect_left(times, ta - latency_ms)
        right = bisect_right(times, ta + latency_ms)

        cand = []

        for j in range(left, right):
            if i == j:
                continue

            b = rows[j]
            lag = (b["_pt"] - ta) / 1000.0

            aa = a["_amount"]
            bb = b["_amount"]
            diff = abs(aa - bb)
            rel_diff = diff / max(abs(aa), abs(bb), EPS)

            if not (diff <= args.abs_tol or rel_diff <= args.rel_tol):
                continue

            tg = tag(a, b)
            reln = relation(a, b)
            sc = score_link(a, b, lag, rel_diff, tg, args)

            cand.append((sc, abs(lag), diff, rel_diff, tg, reln, b, lag))

        cand.sort(key=lambda x: (-x[0], x[1], x[2]))

        for sc, abs_lag, diff, rel_diff, tg, reln, b, lag in cand[:args.max_links_per_action]:
            links.append({
                "score": sc,
                "lag_sec": lag,
                "tag": tg,
                "relation": reln,
                "amount_diff": diff,
                "amount_rel_diff": rel_diff,

                "src_utc": a["utc"],
                "src_market_id": a["market_id"],
                "src_market_type": a["market_type"],
                "src_market_name": a["market_name"],
                "src_runner_id": a["runner_id"],
                "src_runner_name": a["runner_name"],
                "src_total": a["_total"],
                "src_price": a["price"],
                "src_action": a["action"],
                "src_side": a["side"],
                "src_amount": a["amount"],
                "src_proof": a["proof"],

                "trg_utc": b["utc"],
                "trg_market_id": b["market_id"],
                "trg_market_type": b["market_type"],
                "trg_market_name": b["market_name"],
                "trg_runner_id": b["runner_id"],
                "trg_runner_name": b["runner_name"],
                "trg_total": b["_total"],
                "trg_price": b["price"],
                "trg_action": b["action"],
                "trg_side": b["side"],
                "trg_amount": b["amount"],
                "trg_proof": b["proof"],
            })

    links.sort(key=lambda r: (-float(r["score"]), r["src_utc"]))

    fields = [
        "score", "lag_sec", "tag", "relation", "amount_diff", "amount_rel_diff",
        "src_utc", "src_market_id", "src_market_type", "src_market_name",
        "src_runner_id", "src_runner_name", "src_total", "src_price",
        "src_action", "src_side", "src_amount", "src_proof",
        "trg_utc", "trg_market_id", "trg_market_type", "trg_market_name",
        "trg_runner_id", "trg_runner_name", "trg_total", "trg_price",
        "trg_action", "trg_side", "trg_amount", "trg_proof",
    ]

    write_csv(out / "action_links_10s.csv", links, fields)

    top = sorted(rows, key=lambda r: r["_amount"], reverse=True)[:5000]
    top_fields = [
        "utc", "market_id", "market_type", "market_name",
        "runner_id", "runner_name", "price",
        "action", "side", "amount", "proof", "detail",
        "atb_before", "atb_after", "atl_before", "atl_after",
        "trd_before", "trd_after", "d_atb", "d_atl", "d_trd",
    ]
    write_csv(out / "top_actions.csv", top, top_fields)

    summary_rows = []
    for (action, side, proof), v in sorted(summary.items()):
        summary_rows.append({
            "action": action,
            "side": side,
            "proof": proof,
            "count": v["count"],
            "sum_amount": v["sum"],
            "max_amount": v["max"],
        })

    write_csv(out / "summary_actions.csv", summary_rows, [
        "action", "side", "proof", "count", "sum_amount", "max_amount"
    ])

    print("DONE")
    print("out:", out)
    print("links:", len(links))
    print("files:")
    print(out / "action_links_10s.csv")
    print(out / "top_actions.csv")
    print(out / "summary_actions.csv")


if __name__ == "__main__":
    main()
