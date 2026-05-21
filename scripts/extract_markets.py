# =====================================================================
# extract_markets.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Проходить по кожному рядку (JSON)
#
# 3. Витягує з кожного mc[]:
#       id                — ID ринку
#       marketType        — тип ринку
#       eventName         — назва події
#       marketTime        — час початку
#       status            — статус ринку
#       inPlay            — чи ринок у live
#
# 4. Прибирає дублікати ринків
#
# 5. Виводить список у консоль
#
# 6. Зберігає результат у:
#       replay/markets.txt
#
# =====================================================================

import json
from pathlib import Path


def main():
    replay_file = Path("replay/football-pro-sample")

    if not replay_file.exists():
        print(f"File not found: {replay_file}")
        return

    # множина для унікальних ринків
    markets_set = set()

    with replay_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # список ринків
            mc_list = data.get("mc", [])

            for mc in mc_list:
                market_id = mc.get("id")

                definition = mc.get("marketDefinition", {})
                if not isinstance(definition, dict):
                    continue

                market_type = definition.get("marketType")
                event_name = definition.get("eventName")
                market_time = definition.get("marketTime")
                status = definition.get("status")
                in_play = definition.get("inPlay")

                # перевірка ключових полів
                if market_id is None:
                    continue

                # додаємо в множину
                markets_set.add((
                    str(market_id),
                    str(market_type),
                    str(event_name),
                    str(market_time),
                    str(status),
                    str(in_play)
                ))

    # сортуємо по market_id
    markets_list = sorted(markets_set, key=lambda x: x[0])

    print(f"markets found: {len(markets_list)}")
    print()

    # вивід
    for m in markets_list:
        print(f"{m[0]} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]}")

    # збереження
    out_file = Path("replay/markets.txt")

    with out_file.open("w", encoding="utf-8") as f:
        for m in markets_list:
            f.write(f"{m[0]} | {m[1]} | {m[2]} | {m[3]} | {m[4]} | {m[5]}\n")

    print()
    print(f"Saved: {out_file}")


if __name__ == "__main__":
    main()
