#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
from bisect import bisect_left, bisect_right
from pathlib import Path
from datetime import datetime, timezone

EPS = 1e-12


def parse_utc(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    if abs(x) < EPS:
        return "0"
    return f"{x:.10f}".rstrip("0").rstrip(".")


def market_ok(r):
    mt = (r.get("market_type", "") + " " + r.get("market_name", "")).upper()
    return (
        "OVER_UNDER" in mt
        or "CORRECT_SCORE" in mt
        or "CORRECT SCORE" in mt
        or "TOTAL_GOALS" in mt
    )


def read_rows(path, min_amount, time_min, time_max):
    rows = []

    tmin = parse_utc(time_min) if time_min else None
    tmax = parse_utc(time_max) if time_max else None

    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                amount = float(r["amount"])
                pt = int(float(r["pt_ms"]))
            except Exception:
                continue

            if amount < min_amount:
                continue

            if r.get("proof") != "EXACT":
                continue

            if not market_ok(r):
                continue

            ts = pt / 1000.0
            if tmin is not None and ts < tmin:
                continue
            if tmax is not None and ts > tmax:
                continue

            r["_amount"] = amount
            r["_pt"] = pt
            rows.append(r)

    rows.sort(key=lambda x: x["_pt"])
    return rows


def same_amount(a, b, abs_tol, rel_tol):
    aa = a["_amount"]
    bb = b["_amount"]
    diff = abs(aa - bb)
    rel = diff / max(aa, bb, EPS)
    return diff <= abs_tol or rel <= rel_tol, diff, rel


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

    sa = a["market_type"].upper()
    sb = b["market_type"].upper()

    if "OVER_UNDER" in sa and "OVER_UNDER" in sb:
        return "CROSS_TOTAL"
    if "CORRECT_SCORE" in sa or "CORRECT_SCORE" in sb:
        return "CS_LINK_CANDIDATE"
    if "TOTAL_GOALS" in sa or "TOTAL_GOALS" in sb:
        return "TOTAL_GOALS_LINK_CANDIDATE"

    return "OTHER"


def relation(a, b):
    return f'{a["action"]}_THEN_{b["action"]}'


def score(a, b, lag, diff, rel, tg):
    s = 100
    s -= min(40, rel * 100)
    s -= min(20, abs(lag) * 2)

    if tg == "SAME_LEVEL":
        s += 30
    elif tg == "SAME_PRICE_OTHER_SIDE":
        s += 22
    elif tg == "SAME_RUNNER_PRICE_SHIFT":
        s += 16
    elif tg == "CROSS_TOTAL":
        s += 8
    elif tg == "CS_LINK_CANDIDATE":
        s += 5

    reln = relation(a, b)
    if reln == "MATCH_THEN_VISIBLE_ADD":
        s += 12
    elif reln == "VISIBLE_REMOVE_THEN_VISIBLE_ADD":
        s += 10
    elif reln == "VISIBLE_ADD_THEN_MATCH":
        s += 8

    return s


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


def build(rows, lag_min, lag_max, abs_tol, rel_tol, topn):
    times = [r["_pt"] for r in rows]
    links = []

    lag_min_ms = int(lag_min * 1000)
    lag_max_ms = int(lag_max * 1000)

    for i, a in enumerate(rows):
        ta = a["_pt"]

        left = bisect_left(times, ta + lag_min_ms)
        right = bisect_right(times, ta + lag_max_ms)

        candidates = []

        for j in range(left, right):
            if i == j:
                continue

            b = rows[j]
            ok, diff, rel = same_amount(a, b, abs_tol, rel_tol)
            if not ok:
                continue

            tg = tag(a, b)
            reln = relation(a, b)
            lag = (b["_pt"] - a["_pt"]) / 1000.0

            # залишаємо тільки корисні типи зв'язків
            if reln not in {
                "MATCH_THEN_VISIBLE_ADD",
                "VISIBLE_REMOVE_THEN_VISIBLE_ADD",
                "VISIBLE_ADD_THEN_MATCH",
                "MATCH_THEN_VISIBLE_REMOVE",
            }:
                continue

            sc = score(a, b, lag, diff, rel, tg)

            candidates.append((sc, lag, diff, rel, tg, reln, b))

        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

        for sc, lag, diff, rel, tg, reln, b in candidates[:topn]:
            links.append({
                "score": sc,
                "lag_sec": lag,
                "tag": tg,
                "relation": reln,
                "amount_diff": diff,
                "amount_rel_diff": rel,

                "src_utc": a["utc"],
                "src_market_type": a["market_type"],
                "src_market_name": a["market_name"],
                "src_runner_name": a["runner_name"],
                "src_price": a["price"],
                "src_action": a["action"],
                "src_side": a["side"],
                "src_amount": a["amount"],

                "trg_utc": b["utc"],
                "trg_market_type": b["market_type"],
                "trg_market_name": b["market_name"],
                "trg_runner_name": b["runner_name"],
                "trg_price": b["price"],
                "trg_action": b["action"],
                "trg_side": b["side"],
                "trg_amount": b["amount"],
            })

    links.sort(key=lambda r: (-float(r["score"]), r["src_utc"]))
    return links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--out", default="replay/delta_10s_causal")
    ap.add_argument("--min-amount", type=float, default=100)
    ap.add_argument("--abs-tol", type=float, default=2)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--time-min", default="2017-04-30T13:05:00Z")
    ap.add_argument("--time-max", default="2017-04-30T14:57:00Z")
    ap.add_argument("--topn", type=int, default=5)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows = read_rows(
        args.actions,
        min_amount=args.min_amount,
        time_min=args.time_min,
        time_max=args.time_max,
    )

    fields = [
        "score", "lag_sec", "tag", "relation", "amount_diff", "amount_rel_diff",
        "src_utc", "src_market_type", "src_market_name", "src_runner_name",
        "src_price", "src_action", "src_side", "src_amount",
        "trg_utc", "trg_market_type", "trg_market_name", "trg_runner_name",
        "trg_price", "trg_action", "trg_side", "trg_amount",
    ]

    immediate = build(rows, 0, 1, args.abs_tol, args.rel_tol, args.topn)
    delayed = build(rows, 5, 10, args.abs_tol, args.rel_tol, args.topn)
    all10 = build(rows, 0, 10, args.abs_tol, args.rel_tol, args.topn)

    write_csv(out / "causal_links_0_1s.csv", immediate, fields)
    write_csv(out / "causal_links_5_10s.csv", delayed, fields)
    write_csv(out / "causal_links_0_10s.csv", all10, fields)

    print("DONE")
    print("filtered_actions:", len(rows))
    print("0_1s:", len(immediate))
    print("5_10s:", len(delayed))
    print("0_10s:", len(all10))
    print("out:", out)


if __name__ == "__main__":
    main()
