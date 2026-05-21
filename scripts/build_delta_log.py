#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import bz2
import csv
import gzip
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

EPS = 1e-9


def open_any(path):
    path = str(path)

    with open(path, "rb") as f:
        magic = f.read(3)

    # bzip2 magic: BZh
    if magic.startswith(b"BZh") or path.endswith(".bz2"):
        return bz2.open(path, "rt", encoding="utf-8", errors="replace")

    # gzip magic: 1f 8b
    if magic[:2] == b"\\x1f\\x8b" or path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")

    return open(path, "rt", encoding="utf-8", errors="replace")


def utc_ms(ms):
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def fnum(x):
    if x is None or x == "":
        return ""
    x = float(x)
    if abs(x) < EPS:
        return "0"
    return f"{x:.10f}".rstrip("0").rstrip(".")


def norm_levels(arr):
    out = []
    for x in arr or []:
        if isinstance(x, (list, tuple)) and len(x) >= 2:
            out.append((float(x[0]), float(x[1])))
    return out


def read_json_lines(path):
    with open_any(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                p = line.find("{")
                if p >= 0:
                    yield json.loads(line[p:])
                else:
                    raise RuntimeError(f"Bad JSON line {n}")


def classify_actions(row, min_amount):
    actions = []

    d_atb = row["d_atb"]
    d_atl = row["d_atl"]
    d_trd = row["d_trd"]

    base = {
        "pt_ms": row["pt_ms"],
        "utc": row["utc"],
        "market_id": row["market_id"],
        "market_type": row["market_type"],
        "market_name": row["market_name"],
        "runner_id": row["runner_id"],
        "runner_name": row["runner_name"],
        "price": row["price"],
        "atb_before": row["atb_before"],
        "atb_after": row["atb_after"],
        "atl_before": row["atl_before"],
        "atl_after": row["atl_after"],
        "trd_before": row["trd_before"],
        "trd_after": row["trd_after"],
        "d_atb": d_atb,
        "d_atl": d_atl,
        "d_trd": d_trd,
    }

    def add(action, side, amount, proof, detail):
        if amount + EPS < min_amount:
            return
        r = dict(base)
        r.update({
            "action": action,
            "side": side,
            "amount": amount,
            "proof": proof,
            "detail": detail,
        })
        actions.append(r)

    if d_atb > EPS:
        add("VISIBLE_ADD", "ATB", d_atb, "EXACT", "availableToBack increased")
    elif d_atb < -EPS:
        add("VISIBLE_REMOVE", "ATB", -d_atb, "EXACT", "availableToBack decreased")

    if d_atl > EPS:
        add("VISIBLE_ADD", "ATL", d_atl, "EXACT", "availableToLay increased")
    elif d_atl < -EPS:
        add("VISIBLE_REMOVE", "ATL", -d_atl, "EXACT", "availableToLay decreased")

    if d_trd > EPS:
        add("MATCH", "UNKNOWN", d_trd, "EXACT", "traded volume increased at price")

        # IF_HIT-модель:
        # Якщо цей side був з’їдений, очікуване падіння availability = d_trd.
        # net = d_available + d_trd
        # net > 0  => match + доставили
        # net < 0  => match + ще щось зняли
        for side, d_avail, before, after in (
            ("ATB", d_atb, row["atb_before"], row["atb_after"]),
            ("ATL", d_atl, row["atl_before"], row["atl_after"]),
        ):
            if before <= EPS and after <= EPS and abs(d_avail) <= EPS:
                continue

            net = d_avail + d_trd

            if net > EPS:
                add("REPLENISH_IF_HIT", side, net, "IF_HIT", "if this side was hit: matched volume was replenished")
            elif net < -EPS:
                add("EXTRA_CANCEL_IF_HIT", side, -net, "IF_HIT", "if this side was hit: extra cancel/remove beyond match")
            else:
                add("PURE_CONSUME_IF_HIT", side, d_trd, "IF_HIT", "if this side was hit: pure consume")

    return actions


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


def tag_link(a, b):
    same_market = a["market_id"] == b["market_id"]
    same_runner = a["runner_id"] == b["runner_id"]
    same_price = a["price"] == b["price"]
    same_side = a["side"] == b["side"]

    if same_market and same_runner and same_price and same_side:
        return "SAME_LEVEL"
    if same_market and same_runner and same_side:
        return "SAME_RUNNER_SHIFT"
    if same_market and same_runner:
        return "SAME_RUNNER_OTHER_SIDE"
    if same_market:
        return "SAME_MARKET"

    mt_a = str(a["market_type"]).upper()
    mt_b = str(b["market_type"]).upper()

    if "CORRECT_SCORE" in mt_a or "CORRECT_SCORE" in mt_b:
        return "CS_CANDIDATE"
    if "OVER_UNDER" in mt_a and "OVER_UNDER" in mt_b:
        return "CROSS_TOTAL_CANDIDATE"

    return "CROSS_MARKET"


def build_links(actions, latency_sec, rel_tol, abs_tol, top_n):
    rows = sorted(actions, key=lambda r: int(r["pt_ms"]))
    links = []

    for i, a in enumerate(rows):
        ta = int(a["pt_ms"])
        cand = []

        for j, b in enumerate(rows):
            if i == j:
                continue

            tb = int(b["pt_ms"])
            lag = (tb - ta) / 1000.0

            if abs(lag) > latency_sec:
                continue

            aa = float(a["amount"])
            bb = float(b["amount"])
            diff = abs(aa - bb)
            rel = diff / max(abs(aa), abs(bb), EPS)

            if diff <= abs_tol or rel <= rel_tol:
                tag = tag_link(a, b)

                score = 100.0
                score -= min(70.0, rel * 100.0)
                score -= min(20.0, abs(lag) / max(latency_sec, EPS) * 20.0)

                if tag == "SAME_LEVEL":
                    score += 25
                elif tag == "SAME_RUNNER_SHIFT":
                    score += 15
                elif tag == "SAME_MARKET":
                    score += 8
                elif tag == "CS_CANDIDATE":
                    score += 3

                cand.append((score, lag, diff, rel, tag, i, j, a, b))

        cand.sort(key=lambda x: (-x[0], abs(x[1])))

        for score, lag, diff, rel, tag, i0, j0, a, b in cand[:top_n]:
            links.append({
                "source_idx": i0,
                "target_idx": j0,
                "lag_sec": lag,
                "tag": tag,
                "score": score,

                "source_utc": a["utc"],
                "source_market_id": a["market_id"],
                "source_market_type": a["market_type"],
                "source_market_name": a["market_name"],
                "source_runner_id": a["runner_id"],
                "source_runner_name": a["runner_name"],
                "source_price": a["price"],
                "source_action": a["action"],
                "source_side": a["side"],
                "source_amount": a["amount"],
                "source_proof": a["proof"],

                "target_utc": b["utc"],
                "target_market_id": b["market_id"],
                "target_market_type": b["market_type"],
                "target_market_name": b["market_name"],
                "target_runner_id": b["runner_id"],
                "target_runner_name": b["runner_name"],
                "target_price": b["price"],
                "target_action": b["action"],
                "target_side": b["side"],
                "target_amount": b["amount"],
                "target_proof": b["proof"],

                "amount_diff": diff,
                "amount_rel_diff": rel,
            })

    return links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--out", default="replay/delta_10s")
    ap.add_argument("--latency-sec", type=float, default=10.0)
    ap.add_argument("--rel-tol", type=float, default=0.10)
    ap.add_argument("--abs-tol", type=float, default=2.0)
    ap.add_argument("--min-action-amount", type=float, default=1.0)
    ap.add_argument("--top-n-links", type=int, default=5)
    ap.add_argument("--no-links", action="store_true")
    args = ap.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    market_meta = defaultdict(lambda: {
        "market_type": "",
        "market_name": "",
        "event_id": "",
        "event_name": "",
        "status": "",
        "runners": defaultdict(lambda: {"name": "", "status": ""}),
    })

    state = defaultdict(lambda: defaultdict(lambda: {
        "atb": {},
        "atl": {},
        "trd": {},
        "ltp": "",
        "tv": "",
    }))

    delta_rows = []
    action_rows = []

    for msg in read_json_lines(input_path):
        if "mc" not in msg:
            continue

        pt_ms = int(msg.get("pt") or 0)
        utc = utc_ms(pt_ms)

        for mc in msg.get("mc") or []:
            market_id = str(mc.get("id") or "")
            if not market_id:
                continue

            md = mc.get("marketDefinition") or {}
            meta = market_meta[market_id]

            if md:
                meta["market_type"] = md.get("marketType", meta["market_type"]) or meta["market_type"]
                meta["market_name"] = md.get("name", meta["market_name"]) or meta["market_name"]
                meta["event_id"] = str(md.get("eventId", meta["event_id"]) or meta["event_id"])
                meta["event_name"] = md.get("eventName", meta["event_name"]) or meta["event_name"]
                meta["status"] = md.get("status", meta["status"]) or meta["status"]

                for r in md.get("runners") or []:
                    rid = int(r["id"])
                    meta["runners"][rid]["name"] = r.get("name", meta["runners"][rid]["name"]) or meta["runners"][rid]["name"]
                    meta["runners"][rid]["status"] = r.get("status", meta["runners"][rid]["status"]) or meta["runners"][rid]["status"]

            for rc in mc.get("rc") or []:
                rid = int(rc["id"])
                rs = state[market_id][rid]

                if "ltp" in rc:
                    rs["ltp"] = rc["ltp"]
                if "tv" in rc:
                    rs["tv"] = rc["tv"]

                atb_upd = dict(norm_levels(rc.get("atb")))
                atl_upd = dict(norm_levels(rc.get("atl")))
                trd_upd = dict(norm_levels(rc.get("trd")))

                prices = sorted(set(atb_upd) | set(atl_upd) | set(trd_upd))

                for price in prices:
                    atb_before = float(rs["atb"].get(price, 0.0))
                    atl_before = float(rs["atl"].get(price, 0.0))
                    trd_before = float(rs["trd"].get(price, 0.0))

                    if price in atb_upd:
                        size = atb_upd[price]
                        if abs(size) < EPS:
                            rs["atb"].pop(price, None)
                        else:
                            rs["atb"][price] = size

                    if price in atl_upd:
                        size = atl_upd[price]
                        if abs(size) < EPS:
                            rs["atl"].pop(price, None)
                        else:
                            rs["atl"][price] = size

                    if price in trd_upd:
                        rs["trd"][price] = trd_upd[price]

                    atb_after = float(rs["atb"].get(price, 0.0))
                    atl_after = float(rs["atl"].get(price, 0.0))
                    trd_after = float(rs["trd"].get(price, 0.0))

                    d_atb = atb_after - atb_before
                    d_atl = atl_after - atl_before
                    d_trd = trd_after - trd_before

                    if abs(d_atb) < EPS and abs(d_atl) < EPS and abs(d_trd) < EPS:
                        continue

                    rmeta = meta["runners"][rid]

                    row = {
                        "pt_ms": pt_ms,
                        "utc": utc,
                        "market_id": market_id,
                        "market_type": meta["market_type"],
                        "market_name": meta["market_name"],
                        "event_id": meta["event_id"],
                        "event_name": meta["event_name"],
                        "market_status": meta["status"],
                        "runner_id": rid,
                        "runner_name": rmeta["name"],
                        "runner_status": rmeta["status"],
                        "price": price,

                        "atb_before": atb_before,
                        "atb_after": atb_after,
                        "d_atb": d_atb,

                        "atl_before": atl_before,
                        "atl_after": atl_after,
                        "d_atl": d_atl,

                        "trd_before": trd_before,
                        "trd_after": trd_after,
                        "d_trd": d_trd,

                        "ltp": rs["ltp"],
                        "tv": rs["tv"],
                    }

                    delta_rows.append(row)
                    action_rows.extend(classify_actions(row, args.min_action_amount))

    delta_fields = [
        "pt_ms", "utc",
        "market_id", "market_type", "market_name", "event_id", "event_name", "market_status",
        "runner_id", "runner_name", "runner_status", "price",
        "atb_before", "atb_after", "d_atb",
        "atl_before", "atl_after", "d_atl",
        "trd_before", "trd_after", "d_trd",
        "ltp", "tv",
    ]

    action_fields = [
        "pt_ms", "utc",
        "market_id", "market_type", "market_name",
        "runner_id", "runner_name", "price",
        "action", "side", "amount", "proof", "detail",
        "atb_before", "atb_after", "atl_before", "atl_after",
        "trd_before", "trd_after", "d_atb", "d_atl", "d_trd",
    ]

    link_fields = [
        "source_idx", "target_idx", "lag_sec", "tag", "score",
        "source_utc", "source_market_id", "source_market_type", "source_market_name",
        "source_runner_id", "source_runner_name", "source_price", "source_action",
        "source_side", "source_amount", "source_proof",
        "target_utc", "target_market_id", "target_market_type", "target_market_name",
        "target_runner_id", "target_runner_name", "target_price", "target_action",
        "target_side", "target_amount", "target_proof",
        "amount_diff", "amount_rel_diff",
    ]

    write_csv(out_dir / "price_level_delta.csv", delta_rows, delta_fields)
    write_csv(out_dir / "action_log.csv", action_rows, action_fields)

    links = []
    if not args.no_links:
        links = build_links(
            action_rows,
            latency_sec=args.latency_sec,
            rel_tol=args.rel_tol,
            abs_tol=args.abs_tol,
            top_n=args.top_n_links,
        )
        write_csv(out_dir / f"window_links_{int(args.latency_sec)}s.csv", links, link_fields)

    print("DONE")
    print(f"delta:   {out_dir / 'price_level_delta.csv'}")
    print(f"actions: {out_dir / 'action_log.csv'}")
    if args.no_links:
        print("links:   SKIPPED")
    else:
        print(f"links:   {out_dir / f'window_links_{int(args.latency_sec)}s.csv'}")
    print(f"delta_rows={len(delta_rows)} action_rows={len(action_rows)} links={len(links)}")


if __name__ == "__main__":
    main()
