import json
from pathlib import Path


def is_target_market_type(market_type):
    if not market_type:
        return False

    if market_type in {"TOTAL_GOALS", "MATCH_ODDS", "CORRECT_SCORE"}:
        return True

    if market_type.startswith("OVER_UNDER_") and market_type.endswith("5"):
        return True

    return False


def main():
    replay_file = Path("replay/football-pro-sample")

    if not replay_file.exists():
        print(f"File not found: {replay_file}")
        return

    # ключ = market_type
    # значення = дані ринку
    markets = {}

    with replay_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            for mc in data.get("mc", []):
                definition = mc.get("marketDefinition", {})

                if not isinstance(definition, dict):
                    continue

                market_type = definition.get("marketType")

                if not is_target_market_type(market_type):
                    continue

                # один тип ринку = один запис
                if market_type in markets:
                    continue

                market_id = mc.get("id")
                market_name = definition.get("name")
                event_name = definition.get("eventName")

                markets[market_type] = {
                    "market_id": str(market_id),
                    "market_type": str(market_type),
                    "market_name": str(market_name),
                    "event_name": str(event_name),
                }

    result = sorted(markets.values(), key=lambda x: x["market_type"])

    print(f"markets found: {len(result)}")
    print()

    for item in result:
        print(
            f"{item['market_id']} | "
            f"{item['market_type']} | "
            f"{item['market_name']} | "
            f"{item['event_name']}"
        )

    out_file = Path("replay/target_markets.txt")

    with out_file.open("w", encoding="utf-8") as f:
        for item in result:
            f.write(
                f"{item['market_id']} | "
                f"{item['market_type']} | "
                f"{item['market_name']} | "
                f"{item['event_name']}\n"
            )

    print()
    print(f"Saved: {out_file}")


if __name__ == "__main__":
    main()
