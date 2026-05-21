#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "scripts" / "replay_stream_selected_markets_dashboard_stationary_totals.py"
    args = sys.argv[1:]
    # Sticky totals: fixed U0.5..U8.5 list and show empty ladders for missing/closed.
    if "--totals-sticky" not in args:
        args = ["--totals-sticky", *args]
    return subprocess.call([sys.executable, str(target), *args])


if __name__ == "__main__":
    raise SystemExit(main())

