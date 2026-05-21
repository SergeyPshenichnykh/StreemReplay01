#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    target = root / "scripts" / "replay_stream_selected_markets_dashboard.py"
    args = sys.argv[1:]
    if "--smooth-ui" not in args and "--no-clear" not in args:
        args = ["--smooth-ui", *args]
    return subprocess.call([sys.executable, str(target), *args])


if __name__ == "__main__":
    raise SystemExit(main())

