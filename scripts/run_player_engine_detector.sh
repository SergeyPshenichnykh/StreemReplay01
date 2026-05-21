#!/usr/bin/env bash
set -euo pipefail

ACTIONS="${1:-replay/delta_10s/action_log.csv}"
PROFILE="${2:-replay/delta_10s_macro_min10/PLAYER_ENGINE_PROFILE.json}"
OUT_DIR="${3:-replay/delta_10s_macro_min10}"
KICKOFF="${4:-2017-04-30T13:05:00+00:00}"

mkdir -p "$OUT_DIR"

OUT="$OUT_DIR/profile_engine_detected_fast.csv"
LINKS="$OUT_DIR/profile_engine_detected_fast_links.csv"
SUMMARY="$OUT_DIR/PROFILE_ENGINE_DETECTED_FAST_SUMMARY.txt"

echo "ACTIONS=$ACTIONS"
echo "PROFILE=$PROFILE"
echo "OUT=$OUT"
echo "LINKS=$LINKS"
echo

time python scripts/detect_from_profile_fast.py \
  --actions "$ACTIONS" \
  --profile "$PROFILE" \
  --out "$OUT" \
  --links-out "$LINKS" \
  --kickoff "$KICKOFF" \
  --only-normal-phases

python - <<PY
import csv
from pathlib import Path
from collections import defaultdict

p = Path("$OUT")
out = Path("$SUMMARY")

rows = list(csv.DictReader(open(p, newline="", encoding="utf-8")))

by_phase = defaultdict(lambda: {"count":0, "rm":0.0, "add":0.0, "linked":0.0, "net":0.0})

for r in rows:
    ph = r["phase"]
    by_phase[ph]["count"] += 1
    by_phase[ph]["rm"] += float(r["remove_amount"])
    by_phase[ph]["add"] += float(r["add_amount"])
    by_phase[ph]["linked"] += float(r["linked_total"])
    by_phase[ph]["net"] += float(r["net"])

total_rm = sum(float(r["remove_amount"]) for r in rows)
total_add = sum(float(r["add_amount"]) for r in rows)
total_linked = sum(float(r["linked_total"]) for r in rows)
total_net = sum(float(r["net"]) for r in rows)

lines = []
lines.append("PROFILE ENGINE DETECTED FAST SUMMARY")
lines.append("")
lines.append(f"signals = {len(rows)}")
lines.append(f"remove_total = {total_rm:.2f}")
lines.append(f"add_total = {total_add:.2f}")
lines.append(f"net = {total_net:.2f}")
lines.append(f"linked_total = {total_linked:.2f}")
lines.append(f"linked_pct_remove = {100*total_linked/total_rm:.2f}")
lines.append(f"linked_pct_add = {100*total_linked/total_add:.2f}")
lines.append("")
lines.append("BY_PHASE")

for ph, v in sorted(by_phase.items()):
    lines.append(
        f"{ph}: count={v['count']} "
        f"remove={v['rm']:.2f} add={v['add']:.2f} "
        f"net={v['net']:.2f} linked={v['linked']:.2f} "
        f"linked_pct_remove={100*v['linked']/v['rm']:.2f} "
        f"linked_pct_add={100*v['linked']/v['add']:.2f}"
    )

lines.append("")
lines.append("VERDICT")
lines.append("FAST_ENGINE_DETECTED = YES" if rows else "FAST_ENGINE_DETECTED = NO")
lines.append("strategy = cyclic_cross_market_package_rebalance")
lines.append("evidence = same_runner_same_side_price_migration_after_remove_add_cycle")

out.write_text("\\n".join(lines), encoding="utf-8")
print()
print(out.read_text(encoding="utf-8"))
PY

echo
echo "DONE"
echo "CSV: $OUT"
echo "LINKS: $LINKS"
echo "SUMMARY: $SUMMARY"
