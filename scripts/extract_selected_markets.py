# =====================================================================
# extract_selected_markets.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Проходить по кожному JSON-рядку
#
# 3. Дістає marketDefinition.marketType
#
# 4. Залишає тільки ринки типу:
#       OVER_UNDER_*5
#       TOTAL_GOALS
#       MATCH_ODDS
#       CORRECT_SCORE
#
# 5. Для кожного такого ринку витягує:
#       market_id
#       market_type
#       market_name
#       event_name
#       market_time
#       status
#       in_play
#
# 6. Прибирає дублікати
#
# 7. Виводить результат у консоль
#
# 8. Зберігає результат у файл:
#       replay/selected_markets.txt
#
# =====================================================================

import json
from pathlib import Path


# ---------------------------------------------------------------------
# is_target_market_type(market_type)
# ---------------------------------------------------------------------
# Перевіряє, чи належить market_type до потрібних нам ринків.
#
# market_type — текст типу ринку, наприклад:
#   OVER_UNDER_25
#   MATCH_ODDS
#   CORRECT_SCORE
#
# ПОВЕРТАЄ:
#   True  — якщо ринок потрібний
#   False — якщо ринок не потрібний
#
def is_target_market_type(market_type):
    # Якщо значення порожнє — такий ринок нам не підходить
    if not market_type:
        return False

    # TOTAL_GOALS / MATCH_ODDS / CORRECT_SCORE — беремо напряму
    if market_type in {"TOTAL_GOALS", "MATCH_ODDS", "CORRECT_SCORE"}:
        return True

    # OVER_UNDER_*5
    # Тут беремо тільки ті ринки OVER_UNDER,
    # які закінчуються на 5:
    #   OVER_UNDER_05
    #   OVER_UNDER_15
    #   OVER_UNDER_25
    #   OVER_UNDER_35
    #   ...
    if market_type.startswith("OVER_UNDER_") and market_type.endswith("5"):
        return True

    # Все інше не беремо
    return False


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------
# Головна функція:
# - відкриває replay-файл
# - читає його по рядках
# - відбирає тільки потрібні типи ринків
# - зберігає результат
#
def main():
    # Шлях до replay-файлу
    replay_file = Path("replay/football-pro-sample")

    # Перевірка існування файлу
    if not replay_file.exists():
        print(f"File not found: {replay_file}")
        return

    # Множина для унікальних ринків
    # set автоматично прибирає дублікати
    selected_markets = set()

    # Відкриваємо файл і читаємо його построчно
    with replay_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            # Пусті рядки пропускаємо
            if not line:
                continue

            # Пробуємо розібрати JSON
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Якщо рядок битий — пропускаємо
                continue

            # Беремо список змін ринків
            mc_list = data.get("mc", [])

            # Проходимо по кожному ринку
            for mc in mc_list:
                # ID ринку знаходиться на рівні mc
                market_id = mc.get("id")

                # Повний опис ринку лежить у marketDefinition
                definition = mc.get("marketDefinition", {})
                if not isinstance(definition, dict):
                    continue

                # Витягуємо тип ринку
                market_type = definition.get("marketType")

                # Перевіряємо, чи це ринок потрібного типу
                if not is_target_market_type(market_type):
                    continue

                # Дістаємо додаткову інформацію
                market_name = definition.get("name")
                event_name = definition.get("eventName")
                market_time = definition.get("marketTime")
                status = definition.get("status")
                in_play = definition.get("inPlay")

                # Якщо market_id немає — пропускаємо
                if market_id is None:
                    continue

                # Додаємо ринок у множину
                # Зберігаємо у вигляді кортежу:
                #   (id, type, name, event, time, status, inPlay)
                selected_markets.add((
                    str(market_id),
                    str(market_type),
                    str(market_name),
                    str(event_name),
                    str(market_time),
                    str(status),
                    str(in_play),
                ))

    # Сортуємо по типу ринку, потім по ID ринку
    markets_list = sorted(selected_markets, key=lambda x: (x[1], x[0]))

    # Виводимо кількість знайдених ринків
    print(f"selected markets found: {len(markets_list)}")
    print()

    # Виводимо всі знайдені ринки
    for item in markets_list:
        print(
            f"{item[0]} | {item[1]} | {item[2]} | "
            f"{item[3]} | {item[4]} | {item[5]} | {item[6]}"
        )

    # Зберігаємо результат у файл
    out_file = Path("replay/selected_markets.txt")

    with out_file.open("w", encoding="utf-8") as f:
        for item in markets_list:
            f.write(
                f"{item[0]} | {item[1]} | {item[2]} | "
                f"{item[3]} | {item[4]} | {item[5]} | {item[6]}\n"
            )

    print()
    print(f"Saved: {out_file}")


# Точка входу
if __name__ == "__main__":
    main()
