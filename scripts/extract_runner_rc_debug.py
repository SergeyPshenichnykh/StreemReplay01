import json
import csv
from pathlib import Path


REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")

TARGET_MARKET_ID = "1.131162806"
TARGET_RUNNER_ID = 47999
TARGET_RUNNER_NAME = "Man City"

# скільки update-ів витягнути максимум
MAX_UPDATES = 200

OUT_CSV = Path("/home/nafanya/projects/betfair_bot/replay/runner_47999_rc_debug.csv")


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    rows = []
    count = 0

    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            raw = line.strip()

            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")

            for mc in data.get("mc", []):
                market_id = str(mc.get("id"))

                if market_id != TARGET_MARKET_ID:
                    continue

                rc_list = mc.get("rc", [])
                if not isinstance(rc_list, list):
                    continue

                for rc in rc_list:
                    runner_id = rc.get("id")

                    if runner_id != TARGET_RUNNER_ID:
                        continue

                    row = {
                        "line_number": line_number,
                        "pt": pt,
                        "market_id": market_id,
                        "runner_id": runner_id,
                        "runner_name": TARGET_RUNNER_NAME,
                        "ltp": rc.get("ltp"),
                        "tv": rc.get("tv"),
                        "atb": json.dumps(rc.get("atb", None), ensure_ascii=False),
                        "atl": json.dumps(rc.get("atl", None), ensure_ascii=False),
                        "spb": json.dumps(rc.get("spb", None), ensure_ascii=False),
                        "spl": json.dumps(rc.get("spl", None), ensure_ascii=False),
                        "trd": json.dumps(rc.get("trd", None), ensure_ascii=False),
                        "full_rc": json.dumps(rc, ensure_ascii=False),
                    }

                    rows.append(row)
                    count += 1

                    if count >= MAX_UPDATES:
                        break

                if count >= MAX_UPDATES:
                    break

            if count >= MAX_UPDATES:
                break

    if not rows:
        print("No matching rc updates found")
        return

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "line_number",
                "pt",
                "market_id",
                "runner_id",
                "runner_name",
                "ltp",
                "tv",
                "atb",
                "atl",
                "spb",
                "spl",
                "trd",
                "full_rc",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"updates saved : {len(rows)}")
    print(f"file          : {OUT_CSV}")


if __name__ == "__main__":
    main()
