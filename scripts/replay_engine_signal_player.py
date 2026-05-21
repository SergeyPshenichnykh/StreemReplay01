#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from bisect import bisect_left, bisect_right

def ts(s):
    return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_actions(path):
    rows = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_ts"] = ts(r["utc"])
                r["_amount"] = float(r["amount"])
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_ts"])
    return rows

def split(rows, action):
    xs = [r for r in rows if r["action"] == action]
    times = [r["_ts"] for r in xs]
    return xs, times

def rng(times, a, b):
    return bisect_left(times, a), bisect_right(times, b)

def side_price_key(r):
    return (
        r["market_type"],
        r["market_name"],
        r["runner_name"],
        r["side"],
        r["price"],
    )

def summarize_price_levels(removes, adds, top):
    d = defaultdict(lambda: {"rm":0.0, "add":0.0, "rm_n":0, "add_n":0})

    for r in removes:
        k = side_price_key(r)
        d[k]["rm"] += r["_amount"]
        d[k]["rm_n"] += 1

    for r in adds:
        k = side_price_key(r)
        d[k]["add"] += r["_amount"]
        d[k]["add_n"] += 1

    rows = []
    for k, v in d.items():
        mt, mn, rn, side, price = k
        net = v["add"] - v["rm"]
        rows.append({
            "market_type": mt,
            "market_name": mn,
            "runner_name": rn,
            "side": side,
            "price": price,
            "remove_count": v["rm_n"],
            "remove_amount": v["rm"],
            "add_count": v["add_n"],
            "add_amount": v["add"],
            "net": net,
        })

    reduce_rows = sorted(rows, key=lambda x: x["net"])[:top]
    increase_rows = sorted(rows, key=lambda x: -x["net"])[:top]
    return reduce_rows, increase_rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--actions", default="replay/delta_10s/action_log.csv")
    ap.add_argument("--signals", default="replay/delta_10s_macro_min10/final_engine_signals_unique_30s.csv")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/final_engine_replay_player_events.csv")
    ap.add_argument("--log", default="replay/delta_10s_macro_min10/final_engine_replay_player.log")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--from-min", type=float, default=None)
    ap.add_argument("--to-min", type=float, default=None)
    args = ap.parse_args()

    actions = load_actions(args.actions)
    removes, rt = split(actions, "VISIBLE_REMOVE")
    adds, at = split(actions, "VISIBLE_ADD")
    matches, mt = split(actions, "MATCH")

    signals = read_csv(args.signals)

    out_rows = []
    log_lines = []

    for s in signals:
        minute = float(s["minute_from_kickoff"])
        if args.from_min is not None and minute < args.from_min:
            continue
        if args.to_min is not None and minute > args.to_min:
            continue

        t = ts(s["utc"])

        ri, rj = rng(rt, t, t + 8)
        ai, aj = rng(at, t + 4, t + 11)
        mi, mj = rng(mt, t - 10, t)

        rm_rows = removes[ri:rj]
        add_rows = adds[ai:aj]
        match_rows = matches[mi:mj]

        reduce_rows, increase_rows = summarize_price_levels(rm_rows, add_rows, args.top)

        header = (
            f'\nSIGNAL {s["unique_cluster_id"]} | {s["utc"]} | min={s["minute_from_kickoff"]} | '
            f'score={s["score"]} | phase={s["phase"]}\n'
            f'RM={float(s["remove_amount"]):.2f} ADD={float(s["add_amount"]):.2f} NET={float(s["net"]):.2f} '
            f'MATCH10={float(s["match_amount"]):.2f}\n'
            f'ROTATION: {s["top_reduce_group"]}:{s["top_reduce_net"]} -> {s["top_increase_group"]}:{s["top_increase_net"]}'
        )
        print(header)
        log_lines.append(header)

        print("TOP REMOVE PRICE LEVELS:")
        log_lines.append("TOP REMOVE PRICE LEVELS:")
        for r in reduce_rows:
            line = (
                f'  {r["market_type"]} | {r["market_name"]} | {r["runner_name"]} | '
                f'{r["side"]} @{r["price"]} | rm={r["remove_amount"]:.2f} '
                f'add={r["add_amount"]:.2f} net={r["net"]:.2f}'
            )
            print(line)
            log_lines.append(line)

            out_rows.append({
                "signal_id": s["unique_cluster_id"],
                "signal_utc": s["utc"],
                "minute": s["minute_from_kickoff"],
                "kind": "TOP_REMOVE",
                **{k: fnum(v) if isinstance(v, float) else v for k, v in r.items()},
            })

        print("TOP ADD PRICE LEVELS:")
        log_lines.append("TOP ADD PRICE LEVELS:")
        for r in increase_rows:
            line = (
                f'  {r["market_type"]} | {r["market_name"]} | {r["runner_name"]} | '
                f'{r["side"]} @{r["price"]} | rm={r["remove_amount"]:.2f} '
                f'add={r["add_amount"]:.2f} net={r["net"]:.2f}'
            )
            print(line)
            log_lines.append(line)

            out_rows.append({
                "signal_id": s["unique_cluster_id"],
                "signal_utc": s["utc"],
                "minute": s["minute_from_kickoff"],
                "kind": "TOP_ADD",
                **{k: fnum(v) if isinstance(v, float) else v for k, v in r.items()},
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if out_rows:
        fields = list(out_rows[0].keys())
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(out_rows)

    Path(args.log).write_text("\n".join(log_lines), encoding="utf-8")

    print()
    print("OUT:", args.out)
    print("LOG:", args.log)
    print("signals_played:", len(set(r["signal_id"] for r in out_rows)))

if __name__ == "__main__":
    main()
