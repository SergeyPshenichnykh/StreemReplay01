#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import json
import argparse
from pathlib import Path
from datetime import datetime
from bisect import bisect_left, bisect_right
from collections import defaultdict

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def phase_from_minute(m):
    if m < 0:
        return "PREMATCH"
    if m < 40:
        return "NORMAL_FIRST_HALF"
    if m < 65:
        return "HALFTIME_OR_RESTART"
    if m < 90:
        return "NORMAL_SECOND_HALF"
    return "LATE_OR_FULLTIME"

def group(mt):
    if "CORNR" in mt or mt == "CORNER_ODDS":
        return "CORNERS"
    if mt in {"MATCH_ODDS", "DRAW_NO_BET", "DOUBLE_CHANCE"}:
        return "MATCH_RESULT"
    if mt in {"CORRECT_SCORE", "CORRECT_SCORE2", "HALF_TIME_SCORE", "WINNING_MARGIN"}:
        return "SCORELINE"
    if mt == "ASIAN_HANDICAP" or mt.startswith("TEAM_A_") or mt.startswith("TEAM_B_"):
        return "HANDICAP"
    if mt.startswith("OVER_UNDER") or mt in {"TOTAL_GOALS", "TEAM_TOTAL_GOALS"}:
        return "GOALS_TOTALS"
    if mt.startswith("FIRST_HALF") or mt in {"HALF_TIME", "HALF_TIME_FULL_TIME"}:
        return "FIRST_HALF_RELATED"
    if mt in {"BOTH_TEAMS_TO_SCORE", "ODD_OR_EVEN", "MATCH_ODDS_AND_BTTS", "CLEAN_SHEET"}:
        return "DERIVATIVES"
    return "OTHER"

def read_actions(path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = ts(r["utc"])
                r["_amount"] = float(r["amount"])
                r["_price"] = float(r["price"])
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])
    return rows

def split(rows, action):
    xs = [r for r in rows if r["action"] == action]
    times = [r["_ts"] for r in xs]
    pref = [0.0]
    for r in xs:
        pref.append(pref[-1] + r["_amount"])
    return xs, times, pref

def rng(times, a, b):
    return bisect_left(times, a), bisect_right(times, b)

def psum(pref, i, j):
    return pref[j] - pref[i]

def group_sums(rows):
    d = defaultdict(float)
    for r in rows:
        d[group(r["market_type"])] += r["_amount"]
    return d

def fmt_group(d):
    return "; ".join(f"{k}:{round(v,2)}" for k, v in sorted(d.items(), key=lambda x: -abs(x[1])))

def key_runner(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["side"],
    )

def calc_migration(signal_id, sig_utc, minute, phase, rm_rows, add_rows):
    rm = defaultdict(float)
    ad = defaultdict(float)

    for r in rm_rows:
        rm[key_runner(r) + (r["price"],)] += r["_amount"]

    for r in add_rows:
        ad[key_runner(r) + (r["price"],)] += r["_amount"]

    rm_by_runner = defaultdict(list)
    ad_by_runner = defaultdict(list)

    for k, amount in rm.items():
        rm_by_runner[k[:4]].append({"price": float(k[4]), "amount": amount})

    for k, amount in ad.items():
        ad_by_runner[k[:4]].append({"price": float(k[4]), "amount": amount})

    linked_total = 0.0
    unpaired_remove = 0.0
    unpaired_add = 0.0
    link_count = 0
    link_rows = []

    for runner_key in sorted(set(rm_by_runner) | set(ad_by_runner)):
        rml = sorted(rm_by_runner.get(runner_key, []), key=lambda x: -x["amount"])
        adl = sorted(ad_by_runner.get(runner_key, []), key=lambda x: -x["amount"])
        used_add = [0.0] * len(adl)

        for r in rml:
            left = r["amount"]

            order = sorted(
                range(len(adl)),
                key=lambda i: (abs(adl[i]["price"] - r["price"]), -adl[i]["amount"])
            )

            for i in order:
                free = adl[i]["amount"] - used_add[i]
                if free <= 0:
                    continue

                take = min(left, free)
                if take <= 0:
                    continue

                linked_total += take
                link_count += 1

                link_rows.append({
                    "signal_id": signal_id,
                    "signal_utc": sig_utc,
                    "minute": minute,
                    "phase": phase,
                    "market_type": runner_key[0],
                    "market_name": runner_key[1],
                    "runner_name": runner_key[2],
                    "side": runner_key[3],
                    "remove_price": r["price"],
                    "add_price": adl[i]["price"],
                    "price_move": round(adl[i]["price"] - r["price"], 6),
                    "amount_linked": round(take, 2),
                })

                used_add[i] += take
                left -= take

                if left <= 0.000001:
                    break

            if left > 0.000001:
                unpaired_remove += left

        for i, a in enumerate(adl):
            left = a["amount"] - used_add[i]
            if left > 0.000001:
                unpaired_add += left

    return linked_total, unpaired_remove, unpaired_add, link_count, link_rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--profile", default="replay/delta_10s_macro_min10/PLAYER_ENGINE_PROFILE.json")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/profile_engine_detected.csv")
    ap.add_argument("--links-out", default="replay/delta_10s_macro_min10/profile_engine_detected_links.csv")
    ap.add_argument("--kickoff", default="2017-04-30T13:05:00+00:00")
    ap.add_argument("--only-normal-phases", action="store_true")
    ap.add_argument("--min-linked-pct-remove", type=float, default=35.0)
    ap.add_argument("--min-linked-total", type=float, default=30000.0)
    args = ap.parse_args()

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    cfg = profile["detector_config"]

    remove_sec = float(cfg["remove_window_sec"])
    add_min = float(cfg["add_delay_min_sec"])
    add_max = float(cfg["add_delay_max_sec"])
    match_lookback = float(cfg["match_lookback_sec"])
    cluster_gap = float(cfg["unique_cluster_gap_sec"])
    min_remove = float(cfg["min_remove"])
    min_add = float(cfg["min_add"])
    min_score = int(cfg["min_score"])

    kickoff_ts = ts(args.kickoff)

    actions = read_actions(args.actions)

    removes, rt, rp = split(actions, "VISIBLE_REMOVE")
    adds, at, apref = split(actions, "VISIBLE_ADD")
    matches, mt, mp = split(actions, "MATCH")

    candidates = []
    all_links = []

    for r in removes:
        t = r["_ts"]

        minute = round((t - kickoff_ts) / 60.0, 2)
        phase = phase_from_minute(minute)

        if args.only_normal_phases and phase not in {"NORMAL_FIRST_HALF", "NORMAL_SECOND_HALF"}:
            continue

        ri, rj = rng(rt, t, t + remove_sec)
        ai, aj = rng(at, t + add_min, t + add_max)
        mi, mj = rng(mt, t - match_lookback, t)

        rm_amt = psum(rp, ri, rj)
        add_amt = psum(apref, ai, aj)
        match_amt = psum(mp, mi, mj)

        if rm_amt < min_remove or add_amt < min_add:
            continue

        rm_rows = removes[ri:rj]
        add_rows = adds[ai:aj]

        rm_g = group_sums(rm_rows)
        add_g = group_sums(add_rows)
        all_groups = sorted(set(rm_g) | set(add_g))

        if not all_groups:
            continue

        shift = {g: add_g.get(g, 0.0) - rm_g.get(g, 0.0) for g in all_groups}
        top_reduce = min(shift.items(), key=lambda x: x[1])
        top_increase = max(shift.items(), key=lambda x: x[1])

        score = 0
        if rm_amt >= min_remove:
            score += 3
        if rm_amt >= 100000:
            score += 2
        if add_amt >= min_add:
            score += 3
        if add_amt >= 75000:
            score += 2
        if match_amt >= 500:
            score += 1
        if match_amt >= 1000:
            score += 1
        if len(all_groups) >= 5:
            score += 1
        if abs(add_amt - rm_amt) / max(add_amt, rm_amt, 1) <= 0.35:
            score += 2
        if top_reduce[0] != top_increase[0] and abs(top_reduce[1]) >= 1000 and abs(top_increase[1]) >= 1000:
            score += 2

        if score < min_score:
            continue

        signal_id = f"CAND_{len(candidates) + 1}"

        linked, un_rm, un_add, link_count, link_rows = calc_migration(
            signal_id=signal_id,
            sig_utc=r["utc"],
            minute=minute,
            phase=phase,
            rm_rows=rm_rows,
            add_rows=add_rows,
        )

        linked_pct_remove = 100 * linked / rm_amt if rm_amt else 0.0
        linked_pct_add = 100 * linked / add_amt if add_amt else 0.0

        if linked < args.min_linked_total:
            continue

        if linked_pct_remove < args.min_linked_pct_remove:
            continue

        c = {
            "_ts": t,
            "_score": score,
            "_rm": rm_amt,
            "_add": add_amt,
            "_linked": linked,

            "candidate_id": signal_id,
            "utc": r["utc"],
            "minute_from_kickoff": minute,
            "phase": phase,
            "score": score,

            "remove_count": rj - ri,
            "remove_amount": rm_amt,
            "add_count": aj - ai,
            "add_amount": add_amt,
            "net": add_amt - rm_amt,

            "match_count": mj - mi,
            "match_amount": match_amt,

            "linked_total": linked,
            "linked_pct_remove": linked_pct_remove,
            "linked_pct_add": linked_pct_add,
            "unpaired_remove": un_rm,
            "unpaired_add": un_add,
            "link_count": link_count,

            "top_reduce_group": top_reduce[0],
            "top_reduce_net": top_reduce[1],
            "top_increase_group": top_increase[0],
            "top_increase_net": top_increase[1],
            "remove_groups": fmt_group(rm_g),
            "add_groups": fmt_group(add_g),
            "verdict": "ENGINE_DETECTED",
        }

        candidates.append(c)
        all_links.extend(link_rows)

    candidates.sort(key=lambda x: x["_ts"])

    clusters = []
    cur = []

    for c in candidates:
        if not cur:
            cur = [c]
            continue

        if c["_ts"] - cur[-1]["_ts"] <= cluster_gap:
            cur.append(c)
        else:
            clusters.append(cur)
            cur = [c]

    if cur:
        clusters.append(cur)

    picked = []
    picked_candidate_ids = set()

    for i, cl in enumerate(clusters, 1):
        best = sorted(cl, key=lambda x: (-x["_score"], -x["_linked"], -x["_rm"], -x["_add"]))[0]
        best["engine_signal_id"] = str(i)
        best["cluster_size"] = str(len(cl))
        best["cluster_start_utc"] = cl[0]["utc"]
        best["cluster_end_utc"] = cl[-1]["utc"]
        picked.append(best)
        picked_candidate_ids.add(best["candidate_id"])

    fields = [
        "engine_signal_id",
        "cluster_size",
        "cluster_start_utc",
        "cluster_end_utc",
        "candidate_id",
        "utc",
        "minute_from_kickoff",
        "phase",
        "score",
        "remove_count",
        "remove_amount",
        "add_count",
        "add_amount",
        "net",
        "match_count",
        "match_amount",
        "linked_total",
        "linked_pct_remove",
        "linked_pct_add",
        "unpaired_remove",
        "unpaired_add",
        "link_count",
        "top_reduce_group",
        "top_reduce_net",
        "top_increase_group",
        "top_increase_net",
        "remove_groups",
        "add_groups",
        "verdict",
    ]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        for r in picked:
            row = {}
            for k in fields:
                v = r.get(k, "")
                if isinstance(v, float):
                    v = fnum(v)
                row[k] = v
            w.writerow(row)

    kept_links = [r for r in all_links if r["signal_id"] in picked_candidate_ids]

    link_fields = [
        "signal_id",
        "signal_utc",
        "minute",
        "phase",
        "market_type",
        "market_name",
        "runner_name",
        "side",
        "remove_price",
        "add_price",
        "price_move",
        "amount_linked",
    ]

    links_out = Path(args.links_out)
    with links_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=link_fields)
        w.writeheader()
        w.writerows(kept_links)

    print("PROFILE:", args.profile)
    print("ACTIONS:", args.actions)
    print("OUT:", out)
    print("LINKS_OUT:", links_out)
    print("raw_candidates:", len(candidates))
    print("engine_signals:", len(picked))

    if picked:
        print("ENGINE_DETECTED=YES")
    else:
        print("ENGINE_DETECTED=NO")

    print()
    print("SIGNALS")
    for r in picked:
        print(
            r["engine_signal_id"],
            r["utc"],
            "min=", r["minute_from_kickoff"],
            "phase=", r["phase"],
            "score=", r["score"],
            "rm=", round(r["remove_amount"], 2),
            "add=", round(r["add_amount"], 2),
            "linked=", round(r["linked_total"], 2),
            "pct_rm=", round(r["linked_pct_remove"], 2),
            "pct_add=", round(r["linked_pct_add"], 2),
            "rot=", r["top_reduce_group"], "->", r["top_increase_group"],
        )

if __name__ == "__main__":
    main()
