#!/usr/bin/env bash
set -euo pipefail

ACTIONS="${1:-replay/delta_10s/action_log.csv}"
BASE="${2:-replay/delta_10s_macro_min10}"
STAKE="${3:-4}"

RULES="$BASE/FILTERED_SHADOW_BOT_V1_RULES.json"
SIGNALS="$BASE/profile_engine_detected_fast.csv"

ORDERS="$BASE/shadow_bot_orders.csv"
OUTCOMES="$BASE/shadow_order_outcomes.csv"
PNL="$BASE/shadow_bot_pnl_proxy_conservative.csv"

FILTERED="$BASE/filtered_shadow_bot_v1.csv"
SUMMARY="$BASE/FILTERED_SHADOW_BOT_V1_SUMMARY.txt"

python scripts/replay_engine_shadow_bot.py \
  --actions "$ACTIONS" \
  --signals "$SIGNALS" \
  --profile "$BASE/PLAYER_ENGINE_PROFILE.json" \
  --out "$ORDERS" \
  --summary "$BASE/SHADOW_BOT_SUMMARY.txt" \
  --stake "$STAKE" \
  --top-per-signal 8 \
  --min-add-level 500 \
  --horizon-sec 30

python scripts/analyze_shadow_order_outcomes.py \
  --actions "$ACTIONS" \
  --orders "$ORDERS" \
  --out "$OUTCOMES" \
  --horizon-sec 30 \
  --near-sec 1

python scripts/analyze_shadow_bot_pnl_proxy_conservative.py \
  --actions "$ACTIONS" \
  --outcomes "$OUTCOMES" \
  --out "$PNL" \
  --summary "$BASE/SHADOW_BOT_PNL_PROXY_CONSERVATIVE_SUMMARY.txt" \
  --fill-horizon-sec 30 \
  --exit-horizon-sec 60 \
  --max-exit-price-distance 0.20

python - <<PY
import csv, json
from pathlib import Path
from collections import defaultdict

rules = json.loads(Path("$RULES").read_text(encoding="utf-8"))
src = Path("$PNL")
out_csv = Path("$FILTERED")
out_sum = Path("$SUMMARY")

rows = list(csv.DictReader(open(src, newline="", encoding="utf-8")))

def band(price):
    p = float(price)
    if p < 1.10: return "1.00-1.09"
    if p < 1.20: return "1.10-1.19"
    if p < 1.30: return "1.20-1.29"
    if p < 1.40: return "1.30-1.39"
    if p < 1.50: return "1.40-1.49"
    if p < 1.75: return "1.50-1.74"
    if p < 2.00: return "1.75-1.99"
    return "2.00+"

def keep(r):
    mt = r["market_type"]
    side = r["entry_order_side"]
    b = band(r["price"])

    for rule in rules["entry_filters"]:
        if rule["market_type"] != mt:
            continue
        if rule["side"] not in {side, "ANY"}:
            continue
        if b in rule["bands"]:
            return True
    return False

filtered = [r for r in rows if keep(r)]

with out_csv.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(filtered)

def summary(xs):
    stake = sum(float(r["stake"]) for r in xs)
    pnl = sum(float(r["pnl_proxy"]) for r in xs)
    fills = sum(1 for r in xs if r["fill_proxy"] == "YES")
    exits = sum(1 for r in xs if r["pnl_status"] == "EXIT_FOUND")
    return stake, pnl, fills, exits

stake, pnl, fills, exits = summary(filtered)

by = defaultdict(lambda: {"count":0, "stake":0.0, "pnl":0.0})
for r in filtered:
    k = r["market_type"] + "|" + r["entry_order_side"] + "|" + band(r["price"])
    by[k]["count"] += 1
    by[k]["stake"] += float(r["stake"])
    by[k]["pnl"] += float(r["pnl_proxy"])

lines = []
lines.append("FILTERED SHADOW BOT V1 SUMMARY")
lines.append("")
lines.append(f"orders={len(filtered)}")
lines.append(f"stake={stake:.2f}")
lines.append(f"pnl_proxy={pnl:.6f}")
lines.append(f"roi_proxy_pct={100*pnl/stake:.6f}" if stake else "roi_proxy_pct=0")
lines.append(f"fill_rate={100*fills/len(filtered):.2f}" if filtered else "fill_rate=0")
lines.append(f"exit_rate={100*exits/len(filtered):.2f}" if filtered else "exit_rate=0")
lines.append("")
lines.append("BY_BUCKET")
for k, v in sorted(by.items(), key=lambda x: -x[1]["pnl"]):
    roi = 100*v["pnl"]/v["stake"] if v["stake"] else 0
    lines.append(f"{k},{v['count']},{v['stake']:.2f},{v['pnl']:.6f},{roi:.4f}")

out_sum.write_text("\\n".join(lines), encoding="utf-8")
print(out_sum.read_text(encoding="utf-8"))
PY

echo
echo "DONE"
echo "FILTERED_CSV=$FILTERED"
echo "SUMMARY=$SUMMARY"
