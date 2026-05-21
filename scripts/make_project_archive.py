#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import os
import tarfile
from pathlib import Path


def _should_exclude(rel: str, *, with_replay: bool) -> bool:
    rel = rel.replace("\\", "/")
    if rel.startswith(".git/"):
        return True
    if rel.startswith(".venv/"):
        return True
    if rel.startswith("__pycache__/") or "/__pycache__/" in rel:
        return True
    if rel.endswith((".pyc", ".pyo")):
        return True
    if rel.endswith(":Zone.Identifier"):
        return True
    if not with_replay and rel.startswith("replay/"):
        return True
    # Dev screenshots / scratch
    if fnmatch.fnmatch(rel, "TEST*.bmp") or fnmatch.fnmatch(rel, "TEST*.png"):
        return True
    if fnmatch.fnmatch(rel, "dutching_*.jpg"):
        return True
    return False


def build_archive(*, root: Path, out_path: Path, with_replay: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:gz" if out_path.suffixes[-2:] == [".tar", ".gz"] or out_path.suffix == ".gz" else "w"
    with tarfile.open(out_path, mode) as tf:
        for p in sorted(root.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(root).as_posix()
            if _should_exclude(rel, with_replay=with_replay):
                continue
            tf.add(p, arcname=rel, recursive=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a portable tar.gz of the project.")
    ap.add_argument("--out", required=True, help="Output archive path, e.g. dist/betfair_bot-src.tar.gz")
    ap.add_argument("--with-replay", action="store_true", help="Include replay/ folder (can be very large).")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    out_path = Path(args.out).expanduser().resolve()
    # Prevent writing archive inside replay/ etc in-place weirdness
    if str(out_path).startswith(str(root.resolve())):
        # ok to write into repo (e.g. dist/)
        pass
    build_archive(root=root, out_path=out_path, with_replay=bool(args.with_replay))
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"OK: wrote {out_path} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

