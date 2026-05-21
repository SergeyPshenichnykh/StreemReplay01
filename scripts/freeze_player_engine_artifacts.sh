#!/usr/bin/env bash
set -euo pipefail

BASE="replay/delta_10s_macro_min10"
OUT="$BASE/FINAL_ENGINE_ARTIFACTS_MANIFEST.txt"

FILES=(
  "scripts/detect_from_profile_fast.py"
  "scripts/run_player_engine_detector.sh"
  "$BASE/PLAYER_ENGINE_PROFILE.json"
  "$BASE/profile_engine_detected_fast.csv"
  "$BASE/profile_engine_detected_fast_links.csv"
  "$BASE/PROFILE_ENGINE_DETECTED_FAST_SUMMARY.txt"
  "$BASE/FINAL_VERDICT.txt"
  "$BASE/FINAL_MIGRATION_SIGNATURE.txt"
)

{
  echo "FINAL ENGINE ARTIFACTS MANIFEST"
  echo
  date -u +"created_utc=%Y-%m-%dT%H:%M:%SZ"
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
  cat "$BASE/PROFILE_ENGINE_DETECTED_FAST_SUMMARY.txt"
} > "$OUT"

cat "$OUT"

