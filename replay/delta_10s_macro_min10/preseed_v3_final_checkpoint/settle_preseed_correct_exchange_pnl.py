import csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime

FILES = [
    ("PRESEED_LAY_UNDER", Path("replay/delta_10s_macro_min10/engine_v3_preseed_execution_log.csv")),
    ("PRESEED_BACK_UNDER", Path("replay/delta_10s_macro_min10/engine_v3_preseed_back_under_execution_log.csv")),
]

ACTIONS = Path("replay/delta_10s/action_log.csv")
TAKE_TICKS = [1, 2, 3]
STOP_TICKS = [2, 3, 5]

def ts_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)

def f(x):
    try:
        return float(x)
    except Exception:
        return 0.0

LADDER = [round(x / 100, 2) for x in range(101, 201)]
IDX = {p: i for i, p in enumerate(LADDER)}

def move(price, n):
    p = round(float(price), 2)
    if p not in IDX:
        return None
    j = IDX[p] + n
    if j < 0 or j >= len(LADDER):
        return None
    return LADDER[j]

def exchange_pnl(entry_side, matched, entry, exitp):
    if entry_side == "BACK":
        return matched * (entry - exitp) / exitp
    if entry_side == "LAY":
        return matched * (exitp - entry) / exitp
    return 0.0

actions = list(csv.DictReader(open(ACTIONS, newline="", encoding="utf-8")))
for a in actions:
    a["_pt"] = ts_ms(a["utc"])
    a["_price"] = round(f(a["price"]), 2)
    a["_amount"] = f(a["amount"])
actions.sort(key=lambda r: r["_pt"])

for label, log_path in FILES:
    log = list(csv.DictReader(open(log_path, newline="", encoding="utf-8")))

    by_order = defaultdict(list)
    for r in log:
        by_order[r["order_id"]].append(r)

    fills = []
    for oid, xs in by_order.items():
        xs = sorted(xs, key=lambda r: int(r["pt"]))
        fill = next((r for r in xs if r["event"] in {"FILL", "PARTIAL_FILL"}), None)
        if fill and f(fill["matched"]) > 0:
            fills.append(fill)

    print()
    print("=" * 80)
    print(label)
    print("filled_or_partial =", len(fills))

    best = None

    for take in TAKE_TICKS:
        for stop in STOP_TICKS:
            pnl = 0.0
            wins = losses = no_exit = 0
            by_market = defaultdict(lambda: {"wins":0, "losses":0, "no_exit":0, "pnl":0.0})

            for fill in fills:
                side = fill["side"]
                entry = round(f(fill["price"]), 2)
                matched = f(fill["matched"])
                fill_pt = int(fill["pt"])
                mt = fill["market_type"]

                if side == "BACK":
                    target = move(entry, -take)
                    stop_p = move(entry, stop)
                    exit_side = "ATB"
                elif side == "LAY":
                    target = move(entry, take)
                    stop_p = move(entry, -stop)
                    exit_side = "ATL"
                else:
                    no_exit += 1
                    by_market[mt]["no_exit"] += 1
                    continue

                if target is None or stop_p is None:
                    no_exit += 1
                    by_market[mt]["no_exit"] += 1
                    continue

                exit_price = None
                result = None

                for a in actions:
                    if a["_pt"] <= fill_pt:
                        continue
                    if a["market_type"] != fill["market_type"]:
                        continue
                    if a["market_name"] != fill["market_name"]:
                        continue
                    if a["runner_name"] != fill["runner_name"]:
                        continue
                    if a["side"] != exit_side:
                        continue
                    if a["action"] not in {"MATCH", "VISIBLE_REMOVE"}:
                        continue
                    if a["_amount"] <= 0:
                        continue

                    px = a["_price"]

                    if side == "BACK":
                        if px <= target:
                            exit_price = px
                            result = "WIN"
                            break
                        if px >= stop_p:
                            exit_price = px
                            result = "LOSS"
                            break

                    if side == "LAY":
                        if px >= target:
                            exit_price = px
                            result = "WIN"
                            break
                        if px <= stop_p:
                            exit_price = px
                            result = "LOSS"
                            break

                if exit_price is None:
                    no_exit += 1
                    by_market[mt]["no_exit"] += 1
                    continue

                pnl_i = exchange_pnl(side, matched, entry, exit_price)
                pnl += pnl_i
                by_market[mt]["pnl"] += pnl_i

                if result == "WIN":
                    wins += 1
                    by_market[mt]["wins"] += 1
                else:
                    losses += 1
                    by_market[mt]["losses"] += 1

            print(f"take={take} stop={stop} wins={wins} losses={losses} no_exit={no_exit} pnl={pnl:.6f} final={1000+pnl:.6f}")

            if best is None or pnl > best[0]:
                best = (pnl, take, stop, wins, losses, no_exit, by_market)

    print()
    print("BEST")
    pnl, take, stop, wins, losses, no_exit, by_market = best
    print(f"take={take} stop={stop} wins={wins} losses={losses} no_exit={no_exit} pnl={pnl:.6f} final={1000+pnl:.6f}")
    print("BEST BY_MARKET")
    for mt, v in sorted(by_market.items()):
        print(f'{mt}: wins={v["wins"]} losses={v["losses"]} no_exit={v["no_exit"]} pnl={v["pnl"]:.6f}')
