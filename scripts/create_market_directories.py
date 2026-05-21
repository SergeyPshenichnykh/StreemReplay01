from pathlib import Path
import json


# Перевіряє, чи належить тип ринку до потрібних.
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

    # Базова папка для всіх ринків.
    base_dir = Path("replay/markets")
    base_dir.mkdir(parents=True, exist_ok=True)

    # Унікальні типи ринків.
    market_types = set()

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

                if is_target_market_type(market_type):
                    market_types.add(market_type)

    # Для кожного типу ринку створюємо папку і підпапки.
    for market_type in sorted(market_types):
        market_dir = base_dir / market_type

        # Головна папка ринку.
        market_dir.mkdir(exist_ok=True)

        # Папка для сирих витягнутих файлів.
        (market_dir / "raw").mkdir(exist_ok=True)

        # Папка для таблиць / CSV.
        (market_dir / "csv").mkdir(exist_ok=True)

        # Папка для графіків.
        (market_dir / "plots").mkdir(exist_ok=True)

        print(f"created: {market_dir}")
        print(f"created: {market_dir / 'raw'}")
        print(f"created: {market_dir / 'csv'}")
        print(f"created: {market_dir / 'plots'}")

    print()
    print(f"market type directories created: {len(market_types)}")


if __name__ == "__main__":
    main()
