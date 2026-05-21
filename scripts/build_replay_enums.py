
# =====================================================================
# build_replay_enums.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Проходить по JSON-структурі
#
# 3. Знаходить всі ключі
#
# 4. Розкладає ключі по рівнях:
#       ROOT
#       MARKET_CHANGE
#       MARKET_DEFINITION
#       RUNNER
#       RUNNER_CHANGE
#
# 5. Приводить назви до єдиного формату:
#       UPPER_CASE
#
# 6. Створює файл:
#       scripts/enums_replay.py
#
# =====================================================================

import json
import re
from pathlib import Path


# ---------------------------------------------------------------------
# to_enum_name(text)
# ---------------------------------------------------------------------
# ЦЯ ФУНКЦІЯ ПЕРЕТВОРЮЄ БУДЬ-ЯКИЙ КЛЮЧ У ФОРМАТ ENUM
#
# Наприклад:
#   marketType   -> MARKET_TYPE
#   eventName    -> EVENT_NAME
#   pt           -> PT
#
def to_enum_name(text):
    # додаємо "_" перед великими літерами
    # Наприклад:
    #   marketType -> market_Type
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)

    # все переводимо у верхній регістр
    # Наприклад:
    #   market_Type -> MARKET_TYPE
    return text.upper()


# ---------------------------------------------------------------------
# add_keys(target_set, data)
# ---------------------------------------------------------------------
# ЦЯ ФУНКЦІЯ БЕРЕ СЛОВНИК І ДОДАЄ ВСІ ЙОГО КЛЮЧІ У ВКАЗАНИЙ НАБІР
#
# target_set — куди складати знайдені ключі
# data       — словник, з якого беремо ключі
#
def add_keys(target_set, data):
    # якщо це не словник — нічого не робимо
    if not isinstance(data, dict):
        return

    # беремо всі ключі словника і додаємо в target_set
    for key in data.keys():
        target_set.add(key)


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------
# ГОЛОВНА ФУНКЦІЯ:
# - читає replay-файл
# - збирає ключі по рівнях
# - створює enums_replay.py
#
def main():
    # шлях до replay-файлу
    replay_file = Path("replay/football-pro-sample")

    # перевірка існування файлу
    if not replay_file.exists():
        print(f"File not found: {replay_file}")
        return

    # створюємо окремі набори для різних рівнів структури
    # set = множина, вона автоматично прибирає дублікати
    root_keys = set()
    market_change_keys = set()
    market_definition_keys = set()
    runner_keys = set()
    runner_change_keys = set()

    # відкриваємо replay-файл
    with replay_file.open("r", encoding="utf-8") as f:
        # читаємо його построчно
        for line in f:
            line = line.strip()

            # пропускаємо пусті рядки
            if not line:
                continue

            # перетворюємо рядок JSON у Python-словник
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # якщо JSON битий — пропускаємо
                continue

            # ---------------------------------------------------------
            # РІВЕНЬ ROOT
            # ---------------------------------------------------------
            # Це верхній рівень:
            #   op, clk, pt, mc
            add_keys(root_keys, data)

            # дістаємо список market changes
            # якщо mc відсутній — беремо пустий список
            mc_list = data.get("mc", [])

            # проходимо по кожному ринку
            for mc in mc_list:
                # -----------------------------------------------------
                # РІВЕНЬ MARKET_CHANGE
                # -----------------------------------------------------
                # Тут зазвичай:
                #   id, marketDefinition, rc, con, img, tv
                add_keys(market_change_keys, mc)

                # -----------------------------------------------------
                # РІВЕНЬ MARKET_DEFINITION
                # -----------------------------------------------------
                market_definition = mc.get("marketDefinition")
                if isinstance(market_definition, dict):
                    add_keys(market_definition_keys, market_definition)

                    # -------------------------------------------------
                    # РІВЕНЬ RUNNER
                    # -------------------------------------------------
                    runners = market_definition.get("runners", [])
                    for runner in runners:
                        add_keys(runner_keys, runner)

                # -----------------------------------------------------
                # РІВЕНЬ RUNNER_CHANGE
                # -----------------------------------------------------
                rc_list = mc.get("rc", [])
                for rc in rc_list:
                    add_keys(runner_change_keys, rc)

    # -------------------------------------------------------------
    # ФОРМУЄМО ТЕКСТ ВИХІДНОГО ФАЙЛУ
    # -------------------------------------------------------------
    lines = []

    # технічний заголовок
    lines.append("# =====================================================================")
    lines.append("# enums_replay.py")
    lines.append("# =====================================================================")
    lines.append("#")
    lines.append("# АВТОМАТИЧНО ЗГЕНЕРОВАНО З replay/football-pro-sample")
    lines.append("#")
    lines.append("# ЦЕЙ ФАЙЛ МІСТИТЬ ЄДИНИЙ ФОРМАТ НАЗВ КЛЮЧІВ REPLAY-ФАЙЛУ")
    lines.append("#")
    lines.append("# Формат:")
    lines.append("#   ENUM_NAME = 'rawKey'")
    lines.append("#")
    lines.append("# =====================================================================")
    lines.append("")

    # -------------------------------------------------------------
    # ROOT
    # -------------------------------------------------------------
    lines.append("# Верхній рівень JSON")
    lines.append("class ROOT:")
    if root_keys:
        for key in sorted(root_keys):
            enum_name = to_enum_name(key)
            lines.append(f"    {enum_name} = '{key}'")
    else:
        lines.append("    pass")
    lines.append("")

    # -------------------------------------------------------------
    # MARKET_CHANGE
    # -------------------------------------------------------------
    lines.append("# Рівень mc[]")
    lines.append("class MARKET_CHANGE:")
    if market_change_keys:
        for key in sorted(market_change_keys):
            enum_name = to_enum_name(key)
            lines.append(f"    {enum_name} = '{key}'")
    else:
        lines.append("    pass")
    lines.append("")

    # -------------------------------------------------------------
    # MARKET_DEFINITION
    # -------------------------------------------------------------
    lines.append("# Рівень marketDefinition")
    lines.append("class MARKET_DEFINITION:")
    if market_definition_keys:
        for key in sorted(market_definition_keys):
            enum_name = to_enum_name(key)
            lines.append(f"    {enum_name} = '{key}'")
    else:
        lines.append("    pass")
    lines.append("")

    # -------------------------------------------------------------
    # RUNNER
    # -------------------------------------------------------------
    lines.append("# Рівень marketDefinition.runners[]")
    lines.append("class RUNNER:")
    if runner_keys:
        for key in sorted(runner_keys):
            enum_name = to_enum_name(key)
            lines.append(f"    {enum_name} = '{key}'")
    else:
        lines.append("    pass")
    lines.append("")

    # -------------------------------------------------------------
    # RUNNER_CHANGE
    # -------------------------------------------------------------
    lines.append("# Рівень rc[]")
    lines.append("class RUNNER_CHANGE:")
    if runner_change_keys:
        for key in sorted(runner_change_keys):
            enum_name = to_enum_name(key)
            lines.append(f"    {enum_name} = '{key}'")
    else:
        lines.append("    pass")
    lines.append("")

    # шлях до вихідного enums-файлу
    out_file = Path("scripts/enums_replay.py")

    # записуємо файл
    out_file.write_text("\n".join(lines), encoding="utf-8")

    # повідомляємо результат
    print(f"Generated: {out_file}")


# точка входу
if __name__ == "__main__":
    main()
