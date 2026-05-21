#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

python - <<'PY'
from pathlib import Path
import hashlib
import json

root = Path(".").resolve()
manifest_path = root / "_oversize_split" / "MANIFEST.json"

if not manifest_path.exists():
    raise SystemExit("No _oversize_split/MANIFEST.json found")

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

for item in manifest:
    target = root / item["path"]
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("wb") as out:
        for part in item["parts"]:
            out.write((root / part).read_bytes())

    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    if sha != item["sha256"]:
        raise SystemExit(f"SHA256 mismatch: {item['path']}")

    print(f"restored: {item['path']}")

print("done")
PY
