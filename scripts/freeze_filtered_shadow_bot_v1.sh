#!/usr/bin/env bash
set -euo pipefail

BASE="replay/delta_10s_macro_min10"
OUT="$BASE/FILTERED_SHADOW_BOT_V1_MANIFEST.txt"
TAR="$BASE/FILTERED_SHADOW_BOT_V1_ARTIFACTS.tar.gz"

FILES=(
  "scripts/replay_engine_shadow_bot.py"
  "scripts/analyze_shadow_order_outcomes.py"
  "scripts/analyze_shadow_bot_pnl_proxy_conservative.py"
  "scripts/run_filtered_shadow_bot_v1.sh"
  "$BASE/FILTERED_SHADOW_BOT_V1_RULES.json"
  "$BASE/filtered_shadow_bot_v1.csv"
  "$BASE/FILTERED_SHADOW_BOT_V1_SUMMARY.txt"
  "$BASE/shadow_bot_pnl_proxy_conservative.csv"
  "$BASE/SHADOW_BOT_PNL_PROXY_CONSERVATIVE_SUMMARY.txt"
  "$BASE/shadow_order_outcomes.csv"
  "$BASE/SHADOW_BOT_REMOVE_PROXY_SUMMARY.txt"
  "$BASE/profile_engine_detected_fast.csv"
  "$BASE/PLAYER_ENGINE_PROFILE.json"
)

{
  echo "FILTERED SHADOW BOT V1 MANIFEST"
  echo
  date -u +"created_utc=%Y-%m-%dT%H:%M:%SZ"
  echo
  echo "VERDICT"
  echo "strategy=cyclic_cross_market_package_rebalance"
  echo "bot=filtered_shadow_bot_v1"
  echo "real_pnl=NO"
  echo "pnl_proxy=YES"
  echo "roi_proxy_pct=3.605787"
  echo "orders=187"
  echo "fill_rate_pct=80.21"
  echo "exit_rate_pct=79.14"
  echo
  echo "FILES"
  for f in "${FILES[@]}"; do
    if [ -f "$f" ]; then
      size=$(stat -c%s "$f")
      hash=$(sha256sum "$f" | awk '{print $1}')
      echo "$f | size=$size | sha256=$hash"
    else
      echo "$f | MISSING"
    fi
  done
  echo
  echo "SUMMARY"
  cat "$BASE/FILTERED_SHADOW_BOT_V1_SUMMARY.txt"
} > "$OUT"

tar -czf "$TAR" "${FILES[@]}" "$OUT"

cat "$OUT"
echo
ls -lh "$TAR"
sha256sum "$TAR"
