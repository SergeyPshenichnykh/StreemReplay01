#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import argparse
from pathlib import Path
from datetime import datetime

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def fnum(x):
    return f"{float(x):.10f}".rstrip("0").rstrip(".")

def phase(minute):
    if minute < 40:
        return "NORMAL_FIRST_HALF"
    if minute < 50:
        return "LATE_FIRST_HALF_OR_HALFTIME"
    if minute < 65:
        return "HALFTIME_OR_RESTART_TRANSITION"
    if minute < 90:
        return "NORMAL_SECOND_HALF"
    return "LATE_OR_FULLTIME_TRANSITION"

def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packages", default="replay/delta_10s_packages/package_trigger_report.csv")
    ap.add_argument("--base", default="replay/delta_10s_macro_min10")
    ap.add_argument("--out", default="replay/delta_10s_macro_min10/macro_events_summary.csv")
    ap.add_argument("--kickoff", default="2017-04-30T13:05:00Z")
    ap.add_argument("--gap-sec", type=float, default=15.0)
    args = ap.parse_args()

    packages_path = Path(args.packages)
    base = Path(args.base)
    kickoff = ts(args.kickoff)

    packages = read_csv(packages_path)

    rows = []

    for r in packages:
        pid = r["package_id"]
        remove_first = ts(r["remove_first_utc"])
        add_ts = ts(r["add_utc"])
        minute = (remove_first - kickoff) / 60.0

        summary_file = base / f"package_{pid}_module_combo_summary.csv"
        exact_file = base / f"package_{pid}_exact_restored.csv"

        exact_rm = 0.0
        if exact_file.exists():
            with exact_file.open(newline="", encoding="utf-8") as f:
                exact_rm = sum(float(x["remove_amount"]) for x in csv.DictReader(f))

        if summary_file.exists():
            with summary_file.open(newline="", encoding="utf-8") as f:
                s = next(csv.DictReader(f))

            one_rm = float(s["one_to_one_remove_amount"])
            one_add = float(s["one_to_one_add_amount"])
            combo_rm = float(s["combo_remove_amount"])
            combo_add = float(s["combo_add_amount"])
            un_rm = float(s["unlinked_removed_amount"])
            un_add = float(s["unlinked_added_amount"])
        else:
            one_rm = one_add = combo_rm = combo_add = un_rm = un_add = 0.0

        rows.append({
            "package_id": pid,
            "remove_first_utc": r["remove_first_utc"],
            "remove_last_utc": r["remove_last_utc"],
            "add_utc": r["add_utc"],
            "_start": remove_first,
            "_end": add_ts,
            "minute": minute,
            "phase": phase(minute),
            "lines": int(r["lines"]),
            "core_total": float(r["total_amount"]),
            "exact_rm": exact_rm,
            "one_rm": one_rm,
            "one_add": one_add,
            "combo_rm": combo_rm,
            "combo_add": combo_add,
            "unlinked_rm": un_rm,
            "unlinked_add": un_add,
        })

    rows.sort(key=lambda x: x["_start"])

    events = []
    current = []

    for r in rows:
        if not current:
            current = [r]
            continue

        prev_end = max(x["_end"] for x in current)
        same_phase = r["phase"] == current[-1]["phase"]
        close = r["_start"] - prev_end <= args.gap_sec

        if same_phase and close:
            current.append(r)
        else:
            events.append(current)
            current = [r]

    if current:
        events.append(current)

    out_rows = []

    for eid, ev in enumerate(events, 1):
        start = min(x["_start"] for x in ev)
        end = max(x["_end"] for x in ev)

        exact = sum(x["exact_rm"] for x in ev)
        one_rm = sum(x["one_rm"] for x in ev)
        one_add = sum(x["one_add"] for x in ev)
        combo_rm = sum(x["combo_rm"] for x in ev)
        combo_add = sum(x["combo_add"] for x in ev)
        un_rm = sum(x["unlinked_rm"] for x in ev)
        un_add = sum(x["unlinked_add"] for x in ev)

        explained_rm = exact + one_rm + combo_rm
        explained_add = exact + one_add + combo_add

        total_rm = explained_rm + un_rm
        total_add = explained_add + un_add

        out_rows.append({
            "macro_event_id": eid,
            "package_ids": "+".join(x["package_id"] for x in ev),
            "phase": ev[0]["phase"],
            "start_utc": datetime.utcfromtimestamp(start).isoformat() + "Z",
            "end_utc": datetime.utcfromtimestamp(end).isoformat() + "Z",
            "duration_sec": end - start,
            "minute_from_kickoff": min(x["minute"] for x in ev),

            "packages_count": len(ev),
            "core_lines": sum(x["lines"] for x in ev),
            "core_total": sum(x["core_total"] for x in ev),

            "exact_rm": exact,
            "one_rm": one_rm,
            "one_add": one_add,
            "combo_rm": combo_rm,
            "combo_add": combo_add,
            "unlinked_rm": un_rm,
            "unlinked_add": un_add,

            "total_rm": total_rm,
            "total_add": total_add,
            "net_change": total_add - total_rm,

            "explained_rm_pct": 100 * explained_rm / total_rm if total_rm else 0,
            "explained_add_pct": 100 * explained_add / total_add if total_add else 0,
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "macro_event_id",
        "package_ids",
        "phase",
        "start_utc",
        "end_utc",
        "duration_sec",
        "minute_from_kickoff",
        "packages_count",
        "core_lines",
        "core_total",
        "exact_rm",
        "one_rm",
        "one_add",
        "combo_rm",
        "combo_add",
        "unlinked_rm",
        "unlinked_add",
        "total_rm",
        "total_add",
        "net_change",
        "explained_rm_pct",
        "explained_add_pct",
    ]

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in out_rows:
            rr = dict(r)
            for k, v in list(rr.items()):
                if isinstance(v, float):
                    rr[k] = fnum(v)
            w.writerow(rr)

    print("DONE:", out)

if __name__ == "__main__":
    main()
