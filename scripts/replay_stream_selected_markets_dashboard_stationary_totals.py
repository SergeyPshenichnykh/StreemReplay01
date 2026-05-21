#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "scripts" / "replay_stream_selected_markets_dashboard.py"
    args = sys.argv[1:]
    # Default to smooth repaint unless the caller explicitly disables it.
    if "--smooth-ui" not in args and "--no-clear" not in args:
        args = ["--smooth-ui", *args]
    # Experimental: show all totals, and center the ladder window when Under mid/best drifts above 1.30.
    if "--totals-all" not in args:
        args = ["--totals-all", *args]
    if "--totals-center-threshold" not in args:
        args = ["--totals-center-threshold", "1.30", *args]
    if "--totals-rows" not in args:
        args = ["--totals-rows", "3", *args]
    return subprocess.call([sys.executable, str(target), *args])


if __name__ == "__main__":
    raise SystemExit(main())
