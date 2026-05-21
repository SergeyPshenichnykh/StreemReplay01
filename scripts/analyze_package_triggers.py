#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, timezone

EPS = 1e-12


def parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")


def market_group(mt):
    mt = (mt or "").upper()
    if "CORRECT_SCORE" in mt:
        return "CS"
    if "OVER_UNDER" in mt:
        return "OU"
    if "TOTAL_GOALS" in mt:
        return "TG"
    return "OTHER"


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


def read_macro_packages(links_path, min_lines):
    raw = defaultdict(list)

    with open(links_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["relation"] != "VISIBLE_REMOVE_THEN_VISIBLE_ADD":
                continue
            if r["tag"] != "SAME_LEVEL":
                continue

            # macro grouping by ADD timestamp
            key = r["trg_utc"]
            raw[key].append(r)

    packages = []

    for add_utc, items in raw.items():
        if len(items) < min_lines:
            continue

        src_times = [parse_utc(x["src_utc"]) for x in items]
        add_ts = parse_utc(add_utc)

        groups = Counter()
        total = 0.0
        amounts = Counter()
        examples = []

        for x in items:
            mt = x["src_market_type"]
            groups[market_group(mt)] += 1
            amt = float(x["src_amount"])
            total += amt
            amounts[x["src_amount"]] += 1

            if len(examples) < 20:
                examples.append(
                    f'{x["src_market_type"]}|{x["src_market_name"]}|'
                    f'{x["src_runner_name"]}|@{x["src_price"]}|'
                    f'{x["src_side"]}|{x["src_amount"]}'
                )

        packages.append({
            "add_utc": add_utc,
            "add_ts": add_ts,
            "remove_first_ts": min(src_times),
            "remove_last_ts": max(src_times),
            "remove_first_utc": datetime.fromtimestamp(min(src_times), tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "remove_last_utc": datetime.fromtimestamp(max(src_times), tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "lines": len(items),
            "total_amount": total,
            "cs": groups["CS"],
            "ou": groups["OU"],
            "tg": groups["TG"],
            "other": groups["OTHER"],
            "repeated_amounts": "; ".join(f"{k}x{v}" for k, v in amounts.most_common(10) if v >= 2),
            "examples": " || ".join(examples),
            "items": items,
        })

    packages.sort(key=lambda p: (-p["lines"], -p["total_amount"]))
    return packages


def find_matches_near_package(actions, pkg, lookback, after):
    start = pkg["remove_first_ts"] - lookback
    end = pkg["remove_last_ts"] + after

    matches = []
    for r in actions:
        if r["_ts"] < start:
            continue
        if r["_ts"] > end:
            break
        if r["action"] != "MATCH":
            continue
        matches.append(r)

    return matches


def score_trigger(match, pkg):
    dt = pkg["remove_first_ts"] - match["_ts"]

    score = 100.0

    # match closer before package remove = stronger
    if dt >= 0:
        score -= min(40.0, dt * 10.0)
    else:
        score -= 50.0

    # related market bonus
    mg = market_group(match["market_type"])
    if mg == "OU" and pkg["ou"] > 0:
        score += 15
    if mg == "CS" and pkg["cs"] > 0:
        score += 15
    if mg == "TG" and pkg["tg"] > 0:
        score += 10

    return score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--links", default="replay/delta_10s_causal/causal_links_5_10s.csv")
    ap.add_argument("--out", default="replay/delta_10s_packages")
    ap.add_argument("--min-lines", type=int, default=5)
    ap.add_argument("--lookback-sec", type=float, default=10.0)
    ap.add_argument("--after-sec", type=float, default=0.5)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    actions = read_actions(args.actions)
    packages = read_macro_packages(args.links, args.min_lines)

    report = []
    line_rows = []

    for idx, pkg in enumerate(packages, 1):
        matches = find_matches_near_package(
            actions,
            pkg,
            lookback=args.lookback_sec,
            after=args.after_sec,
        )

        before_1 = [m for m in matches if 0 <= pkg["remove_first_ts"] - m["_ts"] <= 1]
        before_2 = [m for m in matches if 0 <= pkg["remove_first_ts"] - m["_ts"] <= 2]
        before_5 = [m for m in matches if 0 <= pkg["remove_first_ts"] - m["_ts"] <= 5]
        before_10 = [m for m in matches if 0 <= pkg["remove_first_ts"] - m["_ts"] <= 10]

        ranked = sorted(
            matches,
            key=lambda m: -score_trigger(m, pkg)
        )[:10]

        trigger_examples = []
        for m in ranked:
            dt = pkg["remove_first_ts"] - m["_ts"]
            trigger_examples.append(
                f'{m["utc"]}|dt={fnum(dt)}|{m["market_type"]}|'
                f'{m["market_name"]}|{m["runner_name"]}|@{m["price"]}|'
                f'amount={m["amount"]}'
            )

        report.append({
            "package_id": idx,
            "remove_first_utc": pkg["remove_first_utc"],
            "remove_last_utc": pkg["remove_last_utc"],
            "add_utc": pkg["add_utc"],
            "lines": pkg["lines"],
            "total_amount": pkg["total_amount"],
            "cs": pkg["cs"],
            "ou": pkg["ou"],
            "tg": pkg["tg"],
            "other": pkg["other"],
            "matches_before_1s": len(before_1),
            "matches_before_2s": len(before_2),
            "matches_before_5s": len(before_5),
            "matches_before_10s": len(before_10),
            "repeated_amounts": pkg["repeated_amounts"],
            "trigger_examples": " || ".join(trigger_examples),
            "package_examples": pkg["examples"],
        })

        for x in pkg["items"]:
            line_rows.append({
                "package_id": idx,
                "remove_utc": x["src_utc"],
                "add_utc": x["trg_utc"],
                "lag_sec": x["lag_sec"],
                "market_type": x["src_market_type"],
                "market_name": x["src_market_name"],
                "runner_name": x["src_runner_name"],
                "price": x["src_price"],
                "side": x["src_side"],
                "amount": x["src_amount"],
            })

    report_fields = [
        "package_id",
        "remove_first_utc",
        "remove_last_utc",
        "add_utc",
        "lines",
        "total_amount",
        "cs",
        "ou",
        "tg",
        "other",
        "matches_before_1s",
        "matches_before_2s",
        "matches_before_5s",
        "matches_before_10s",
        "repeated_amounts",
        "trigger_examples",
        "package_examples",
    ]

    line_fields = [
        "package_id",
        "remove_utc",
        "add_utc",
        "lag_sec",
        "market_type",
        "market_name",
        "runner_name",
        "price",
        "side",
        "amount",
    ]

    with open(out / "package_trigger_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=report_fields)
        w.writeheader()
        for r in report:
            rr = dict(r)
            rr["total_amount"] = fnum(rr["total_amount"])
            w.writerow(rr)

    with open(out / "package_lines_merged.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=line_fields)
        w.writeheader()
        w.writerows(line_rows)

    print("DONE")
    print("packages:", len(report))
    print("out:", out / "package_trigger_report.csv")
    print("lines:", out / "package_lines_merged.csv")


if __name__ == "__main__":
    main()
