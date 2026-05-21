#!/usr/bin/env bash
set -euo pipefail

BASE="replay/delta_10s_macro_min10"
OUT="$BASE/FILTERED_SHADOW_BOT_V2_MANIFEST.txt"
TAR="$BASE/FILTERED_SHADOW_BOT_V2_ARTIFACTS.tar.gz"

FILES=(
  "scripts/replay_engine_shadow_bot.py"
  "scripts/analyze_shadow_order_outcomes.py"
  "scripts/analyze_shadow_bot_pnl_proxy_conservative.py"
  "scripts/run_filtered_shadow_bot_v1.sh"
  "$BASE/FILTERED_SHADOW_BOT_V2_RULES.json"
  "$BASE/filtered_shadow_bot_v2_no_asian.csv"
  "$BASE/FILTERED_SHADOW_BOT_V2_NO_ASIAN_SUMMARY.txt"
  "$BASE/shadow_bot_pnl_proxy_conservative.csv"
  "$BASE/SHADOW_BOT_PNL_PROXY_CONSERVATIVE_SUMMARY.txt"
  "$BASE/shadow_order_outcomes.csv"
  "$BASE/profile_engine_detected_fast.csv"
  "$BASE/PLAYER_ENGINE_PROFILE.json"
)

{
  echo "FILTERED SHADOW BOT V2 MANIFEST"
  echo
  date -u +"created_utc=%Y-%m-%dT%H:%M:%SZ"
  echo
  echo "VERDICT"
  echo "strategy=cyclic_cross_market_package_rebalance"
  echo "bot=filtered_shadow_bot_v2_no_asian"
  echo "blocked=ASIAN_HANDICAP"
  echo "real_pnl=NO"
  echo "pnl_proxy=YES"
  echo "orders=101"
  echo "stake=404.00"
  echo "pnl_proxy=18.277677"
  echo "roi_proxy_pct=4.524177"
  echo "fill_rate_pct=86.14"
  echo "exit_rate_pct=86.14"
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
  cat "$BASE/FILTERED_SHADOW_BOT_V2_NO_ASIAN_SUMMARY.txt"
} > "$OUT"

tar -czf "$TAR" "${FILES[@]}" "$OUT"

cat "$OUT"
echo
ls -lh "$TAR"
sha256sum "$TAR"
