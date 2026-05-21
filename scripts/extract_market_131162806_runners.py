import json
import csv
from pathlib import Path


REPLAY_FILE = Path("replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

OUT_CSV = Path("replay/markets/MATCH_ODDS/csv/market_131162806_runners.csv")
OUT_TXT = Path("replay/markets/MATCH_ODDS/raw/market_131162806_runners.txt")


def main():
    runners = {}

    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            for mc in data.get("mc", []):
                market_id = str(mc.get("id"))

                if market_id != TARGET_MARKET_ID:
                    continue

                definition = mc.get("marketDefinition", {})
                if not isinstance(definition, dict):
                    continue

                for runner in definition.get("runners", []):
                    runner_id = runner.get("id")
                    runner_name = runner.get("name")
                    sort_priority = runner.get("sortPriority")

                    if runner_id is None:
                        continue

                    runners[runner_id] = {
                        "runner_id": str(runner_id),
                        "runner_name": str(runner_name),
                        "sort_priority": str(sort_priority),
                    }

    result = sorted(runners.values(), key=lambda x: int(x["sort_priority"]))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)

    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["runner_id", "runner_name", "sort_priority"],
        )
        writer.writeheader()
        writer.writerows(result)

    with OUT_TXT.open("w", encoding="utf-8") as f:
        for item in result:
            f.write(
                f"{item['runner_id']} | "
                f"{item['runner_name']} | "
                f"{item['sort_priority']}\n"
            )

    for item in result:
        print(
            f"{item['runner_id']} | "
            f"{item['runner_name']} | "
            f"{item['sort_priority']}"
        )

    print()
    print(f"saved: {OUT_CSV}")
    print(f"saved: {OUT_TXT}")


if __name__ == "__main__":
    main()
