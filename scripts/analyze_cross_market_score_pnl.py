#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import re
import argparse
from pathlib import Path
from collections import defaultdict

def fnum(x):
    try:
        x = float(x)
    except Exception:
        return x
    return f"{x:.10f}".rstrip("0").rstrip(".")

def maker_side(book_side):
    # ATB = availableToBack => maker is LAY
    # ATL = availableToLay  => maker is BACK
    if book_side == "ATB":
        return "LAY"
    if book_side == "ATL":
        return "BACK"
    return "UNKNOWN"

def bet_pnl(side, price, stake, wins):
    price = float(price)
    stake = float(stake)

    if side == "BACK":
        return stake * (price - 1.0) if wins else -stake

    if side == "LAY":
        return -stake * (price - 1.0) if wins else stake

    return 0.0

def parse_ou_threshold(market_type, market_name, runner_name):
    s = f"{market_type} {market_name} {runner_name}"
    m = re.search(r"Over/Under\s+(\d+(?:\.\d+)?)", s, re.I)
    if not m:
        m = re.search(r"OVER_UNDER_(\d+)", market_type.upper())
        if m:
            raw = m.group(1)
            if len(raw) == 2:
                return float(raw[0] + "." + raw[1])
            return float(raw)
    return float(m.group(1)) if m else None

def wins_leg(row, home, away):
    mt = row["market_type"].upper()
    mn = row["market_name"]
    rn = row["runner_name"]
    total = home + away

    # Over/Under goals
    if "OVER_UNDER" in mt and "CORNR" not in mt:
        th = parse_ou_threshold(mt, mn, rn)
        if th is None:
            return None
        if rn.lower().startswith("over"):
            return total > th
        if rn.lower().startswith("under"):
            return total < th
        return None

    # Correct Score
    if "CORRECT_SCORE" in mt:
        s = rn.strip()

        m = re.match(r"^(\d+)\s*-\s*(\d+)$", s)
        if m:
            return home == int(m.group(1)) and away == int(m.group(2))

        sl = s.lower()

        # Approximation for Betfair CS "Any Other ..."
        if "any other home" in sl:
            return home > away and not (home <= 3 and away <= 3)
        if "any other away" in sl:
            return away > home and not (home <= 3 and away <= 3)
        if "any other draw" in sl:
            return home == away and not (home <= 3 and away <= 3)

        return None

    # Total Goals, runners like "2 goals or more"
    if mt == "TOTAL_GOALS":
        m = re.search(r"(\d+)\s+goals?\s+or\s+more", rn, re.I)
        if m:
            return total >= int(m.group(1))

        m = re.search(r"(\d+)\s+goals?$", rn, re.I)
        if m:
            return total == int(m.group(1))

        return None

    # Team Total Goals
    if mt == "TEAM_TOTAL_GOALS":
        team_goals = None
        ml = mn.lower()

        if "man city" in ml:
            team_goals = home
        elif "middlesbrough" in ml:
            team_goals = away

        if team_goals is None:
            return None

        m = re.search(r"(\d+)\s+goals?\s+or\s+more", rn, re.I)
        if m:
            return team_goals >= int(m.group(1))

        m = re.search(r"(\d+)\s+goals?$", rn, re.I)
        if m:
            return team_goals == int(m.group(1))

        return None

    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", default="replay/delta_10s_packages/package_lines_merged.csv")
    ap.add_argument("--out", default="replay/delta_10s_packages")
    ap.add_argument("--max-goals", type=int, default=7)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    packages = defaultdict(list)

    with open(args.lines, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            side = maker_side(r["side"])
            if side == "UNKNOWN":
                continue

            # only scoreline-dependent markets
            mt = r["market_type"].upper()
            if not (
                ("OVER_UNDER" in mt and "CORNR" not in mt)
                or "CORRECT_SCORE" in mt
                or mt in {"TOTAL_GOALS", "TEAM_TOTAL_GOALS"}
            ):
                continue

            r["maker_side"] = side

            try:
                float(r["price"])
                float(r["amount"])
            except Exception:
                continue

            packages[r["package_id"]].append(r)

    summary_rows = []
    detail_rows = []
    ignored_rows = []

    for pid, legs in sorted(packages.items(), key=lambda x: int(x[0])):
        pnl_by_score = []

        for home in range(args.max_goals + 1):
            for away in range(args.max_goals + 1):
                pnl = 0.0
                used = 0
                ignored = 0

                for leg in legs:
                    wins = wins_leg(leg, home, away)
                    if wins is None:
                        ignored += 1
                        continue

                    pnl += bet_pnl(
                        leg["maker_side"],
                        float(leg["price"]),
                        float(leg["amount"]),
                        wins
                    )
                    used += 1

                pnl_by_score.append({
                    "package_id": pid,
                    "score": f"{home}-{away}",
                    "home_goals": home,
                    "away_goals": away,
                    "total_goals": home + away,
                    "pnl": pnl,
                    "used_legs": used,
                    "ignored_legs": ignored,
                })

        min_row = min(pnl_by_score, key=lambda x: x["pnl"])
        max_row = max(pnl_by_score, key=lambda x: x["pnl"])
        avg_pnl = sum(x["pnl"] for x in pnl_by_score) / len(pnl_by_score)

        total_back = sum(float(x["amount"]) for x in legs if x["maker_side"] == "BACK")
        total_lay = sum(float(x["amount"]) for x in legs if x["maker_side"] == "LAY")
        total_liability = sum(
            float(x["amount"]) * (float(x["price"]) - 1.0)
            for x in legs if x["maker_side"] == "LAY"
        )

        summary_rows.append({
            "package_id": pid,
            "legs": len(legs),
            "total_back_stake": total_back,
            "total_lay_stake": total_lay,
            "total_lay_liability": total_liability,
            "min_pnl": min_row["pnl"],
            "min_score": min_row["score"],
            "max_pnl": max_row["pnl"],
            "max_score": max_row["score"],
            "avg_pnl_grid": avg_pnl,
            "arb_like_score_grid": "YES" if min_row["pnl"] > 0 else "NO",
        })

        detail_rows.extend(pnl_by_score)

        for leg in legs:
            known_any = False
            for home in range(args.max_goals + 1):
                for away in range(args.max_goals + 1):
                    if wins_leg(leg, home, away) is not None:
                        known_any = True
                        break
                if known_any:
                    break

            if not known_any:
                ignored_rows.append(leg)

    summary_file = out / "cross_market_score_pnl_summary.csv"
    detail_file = out / "cross_market_score_pnl_detail.csv"
    ignored_file = out / "cross_market_score_pnl_ignored_legs.csv"

    with open(summary_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id", "legs",
            "total_back_stake", "total_lay_stake", "total_lay_liability",
            "min_pnl", "min_score", "max_pnl", "max_score",
            "avg_pnl_grid", "arb_like_score_grid",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in summary_rows:
            rr = dict(r)
            for k in ["total_back_stake", "total_lay_stake", "total_lay_liability", "min_pnl", "max_pnl", "avg_pnl_grid"]:
                rr[k] = fnum(rr[k])
            w.writerow(rr)

    with open(detail_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id", "score", "home_goals", "away_goals",
            "total_goals", "pnl", "used_legs", "ignored_legs"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in detail_rows:
            rr = dict(r)
            rr["pnl"] = fnum(rr["pnl"])
            w.writerow(rr)

    with open(ignored_file, "w", newline="", encoding="utf-8") as f:
        fields = [
            "package_id", "market_type", "market_name",
            "runner_name", "price", "side", "amount", "maker_side"
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in ignored_rows:
            w.writerow({k: r.get(k, "") for k in fields})

    print("DONE")
    print("summary:", summary_file)
    print("detail:", detail_file)
    print("ignored:", ignored_file)

if __name__ == "__main__":
    main()
