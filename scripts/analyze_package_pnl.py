#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from collections import defaultdict

def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")

def maker_bet_side(book_side):
    # ATB = availableToBack. Хтось дає BACK-гравцю можливість BACK,
    # тобто сам maker стоїть LAY.
    # ATL = availableToLay. Хтось дає LAY-гравцю можливість LAY,
    # тобто сам maker стоїть BACK.
    if book_side == "ATB":
        return "LAY"
    if book_side == "ATL":
        return "BACK"
    return "UNKNOWN"

def pnl_if_outcome(bets, outcome_runner):
    pnl = 0.0

    for b in bets:
        side = b["maker_side"]
        runner = b["runner_name"]
        price = float(b["price"])
        stake = float(b["amount"])

        if side == "BACK":
            if runner == outcome_runner:
                pnl += stake * (price - 1.0)
            else:
                pnl -= stake

        elif side == "LAY":
            if runner == outcome_runner:
                pnl -= stake * (price - 1.0)
            else:
                pnl += stake

    return pnl

def pnl_other_unquoted(bets):
    # outcome, якого немає серед quoted runners у цьому package.
    # Усі BACK програють stake, усі LAY виграють stake.
    pnl = 0.0

    for b in bets:
        side = b["maker_side"]
        stake = float(b["amount"])

        if side == "BACK":
            pnl -= stake
        elif side == "LAY":
            pnl += stake

    return pnl

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default="replay/delta_10s_packages/package_lines_merged.csv")
    ap.add_argument("--out", default="replay/delta_10s_packages")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    groups = defaultdict(list)

    with open(args.lines, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                float(r["price"])
                float(r["amount"])
            except Exception:
                continue

            r["maker_side"] = maker_bet_side(r["side"])
            if r["maker_side"] == "UNKNOWN":
                continue

            key = (r["package_id"], r["market_type"], r["market_name"])
            groups[key].append(r)

    summary = []
    detail = []

    for (pid, mt, mn), bets in groups.items():
        runners = sorted(set(b["runner_name"] for b in bets))

        pnl_rows = []

        for runner in runners:
            pnl = pnl_if_outcome(bets, runner)
            pnl_rows.append((runner, pnl))

        other_pnl = pnl_other_unquoted(bets)
        pnl_rows.append(("OTHER_UNQUOTED", other_pnl))

        min_runner, min_pnl = min(pnl_rows, key=lambda x: x[1])
        max_runner, max_pnl = max(pnl_rows, key=lambda x: x[1])

        total_back_stake = sum(float(b["amount"]) for b in bets if b["maker_side"] == "BACK")
        total_lay_stake = sum(float(b["amount"]) for b in bets if b["maker_side"] == "LAY")
        total_lay_liability = sum(float(b["amount"]) * (float(b["price"]) - 1.0) for b in bets if b["maker_side"] == "LAY")

        arb_like = "YES" if min_pnl > 0 else "NO"

        summary.append({
            "package_id": pid,
            "market_type": mt,
            "market_name": mn,
            "legs": len(bets),
            "quoted_runners": len(runners),
            "total_back_stake": total_back_stake,
            "total_lay_stake": total_lay_stake,
            "total_lay_liability": total_lay_liability,
            "min_pnl": min_pnl,
            "min_runner": min_runner,
            "max_pnl": max_pnl,
            "max_runner": max_runner,
            "arb_like_if_all_matched": arb_like,
        })

        for runner, pnl in pnl_rows:
            detail.append({
                "package_id": pid,
                "market_type": mt,
                "market_name": mn,
                "outcome_runner": runner,
                "pnl_if_all_package_legs_matched": pnl,
            })

    summary.sort(key=lambda r: (int(r["package_id"]), r["market_type"], r["market_name"]))
    detail.sort(key=lambda r: (int(r["package_id"]), r["market_type"], r["market_name"], r["outcome_runner"]))

    summary_file = out / "package_pnl_summary.csv"
    detail_file = out / "package_pnl_detail.csv"

    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id", "market_type", "market_name", "legs", "quoted_runners",
            "total_back_stake", "total_lay_stake", "total_lay_liability",
            "min_pnl", "min_runner", "max_pnl", "max_runner",
            "arb_like_if_all_matched",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in summary:
            rr = dict(r)
            for k in ["total_back_stake", "total_lay_stake", "total_lay_liability", "min_pnl", "max_pnl"]:
                rr[k] = fnum(rr[k])
            w.writerow(rr)

    with open(detail_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id", "market_type", "market_name",
            "outcome_runner", "pnl_if_all_package_legs_matched",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in detail:
            rr = dict(r)
            rr["pnl_if_all_package_legs_matched"] = fnum(rr["pnl_if_all_package_legs_matched"])
            w.writerow(rr)

    print("DONE")
    print("summary:", summary_file)
    print("detail:", detail_file)
    print("groups:", len(summary))

if __name__ == "__main__":
    main()
