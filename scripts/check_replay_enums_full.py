# =====================================================================
# check_replay_enums_full.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Збирає всі ключі по рівнях:
#       ROOT
#       MARKET_CHANGE
#       MARKET_DEFINITION
#       RUNNER
#       RUNNER_CHANGE
#
# 3. Читає enums-файл:
#       scripts/enums_replay.py
#
# 4. Забирає з нього всі enum-значення по тих самих рівнях
#
# 5. Друкує ПО КОЖНОМУ РІВНЮ:
#       - replay keys
#       - enum keys
#       - missing in enums
#       - extra in enums
#       - duplicates in enums
#       - empty values in enums
#
# 6. В кінці показує загальний підсумок
#
# =====================================================================

import json
import importlib.util
from pathlib import Path


# ---------------------------------------------------------------------
# add_keys(target_set, data)
# ---------------------------------------------------------------------
# target_set — набір, куди ми складаємо знайдені ключі
# data       — словник, з якого беремо ключі
#
# ЩО РОБИТЬ:
# якщо data є словником, бере всі його ключі і додає в target_set
#
def add_keys(target_set, data):
    if not isinstance(data, dict):
        return

    for key in data.keys():
        target_set.add(key)


# ---------------------------------------------------------------------
# load_replay_keys()
# ---------------------------------------------------------------------
# Читає replay-файл і збирає реальні ключі по рівнях структури.
#
# ПОВЕРТАЄ:
# словник такого вигляду:
# {
#   "ROOT": {...},
#   "MARKET_CHANGE": {...},
#   "MARKET_DEFINITION": {...},
#   "RUNNER": {...},
#   "RUNNER_CHANGE": {...},
# }
#
def load_replay_keys():
    replay_file = Path("replay/football-pro-sample")

    if not replay_file.exists():
        print(f"File not found: {replay_file}")
        return None

    # Окремі набори для кожного рівня
    root_keys = set()
    market_change_keys = set()
    market_definition_keys = set()
    runner_keys = set()
    runner_change_keys = set()

    # Читаємо replay-файл построчно
    with replay_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # ROOT-рівень
            add_keys(root_keys, data)

            # Рівень mc[]
            mc_list = data.get("mc", [])
            for mc in mc_list:
                add_keys(market_change_keys, mc)

                # Рівень marketDefinition
                definition = mc.get("marketDefinition")
                if isinstance(definition, dict):
                    add_keys(market_definition_keys, definition)

                    # Рівень runners[]
                    runners = definition.get("runners", [])
                    for runner in runners:
                        add_keys(runner_keys, runner)

                # Рівень rc[]
                rc_list = mc.get("rc", [])
                for rc in rc_list:
                    add_keys(runner_change_keys, rc)

    return {
        "ROOT": root_keys,
        "MARKET_CHANGE": market_change_keys,
        "MARKET_DEFINITION": market_definition_keys,
        "RUNNER": runner_keys,
        "RUNNER_CHANGE": runner_change_keys,
    }


# ---------------------------------------------------------------------
# load_enums_module()
# ---------------------------------------------------------------------
# Завантажує scripts/enums_replay.py як Python-модуль,
# щоб можна було читати його class-и.
#
def load_enums_module():
    enums_file = Path("scripts/enums_replay.py")

    if not enums_file.exists():
        print(f"File not found: {enums_file}")
        return None

    spec = importlib.util.spec_from_file_location("enums_replay", enums_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------
# class_to_values(cls)
# ---------------------------------------------------------------------
# Бере один class з enums_replay.py
# і повертає:
#   values       — словник {ENUM_NAME: raw_value}
#   duplicates   — список дублікатів значень
#   empty_values — список enum-полів з порожнім значенням
#
# ПРИКЛАД:
# class ROOT:
#     OP = 'op'
#     CLK = 'clk'
#
# values буде:
#   {"OP": "op", "CLK": "clk"}
#
def class_to_values(cls):
    values = {}
    seen_raw_values = {}
    duplicates = []
    empty_values = []

    # dir(cls) дає список усіх атрибутів class
    for attr_name in dir(cls):
        # службові атрибути типу __class__ пропускаємо
        if attr_name.startswith("__"):
            continue

        raw_value = getattr(cls, attr_name)

        # беремо тільки рядкові значення
        if not isinstance(raw_value, str):
            continue

        values[attr_name] = raw_value

        # перевірка на порожнє значення
        if raw_value.strip() == "":
            empty_values.append(attr_name)

        # перевірка на дубль raw-значення
        if raw_value in seen_raw_values:
            duplicates.append((attr_name, seen_raw_values[raw_value], raw_value))
        else:
            seen_raw_values[raw_value] = attr_name

    return values, duplicates, empty_values


# ---------------------------------------------------------------------
# load_enum_keys(module)
# ---------------------------------------------------------------------
# Забирає raw-значення з class-ів:
#   ROOT
#   MARKET_CHANGE
#   MARKET_DEFINITION
#   RUNNER
#   RUNNER_CHANGE
#
# ПОВЕРТАЄ:
#   result             — ключі по рівнях
#   duplicates_report  — дублікати по рівнях
#   empty_report       — порожні значення по рівнях
#
def load_enum_keys(module):
    result = {}
    duplicates_report = {}
    empty_report = {}

    class_names = [
        "ROOT",
        "MARKET_CHANGE",
        "MARKET_DEFINITION",
        "RUNNER",
        "RUNNER_CHANGE",
    ]

    for class_name in class_names:
        # якщо потрібного class немає — повертаємо пусті значення
        if not hasattr(module, class_name):
            result[class_name] = set()
            duplicates_report[class_name] = []
            empty_report[class_name] = []
            continue

        cls = getattr(module, class_name)

        values, duplicates, empty_values = class_to_values(cls)

        # values.values() — це raw-значення типу 'op', 'clk', 'marketType'
        result[class_name] = set(values.values())
        duplicates_report[class_name] = duplicates
        empty_report[class_name] = empty_values

    return result, duplicates_report, empty_report


# ---------------------------------------------------------------------
# print_section(title)
# ---------------------------------------------------------------------
# Красивий заголовок блоку у консолі
#
def print_section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------
# print_list(title, items)
# ---------------------------------------------------------------------
# Друкує список елементів построчно з коротким заголовком
#
def print_list(title, items):
    print(f"{title}: {len(items)}")
    for item in sorted(items):
        print(f"  - {item}")


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------
# Основна логіка:
#   1. читаємо реальні ключі з replay
#   2. читаємо ключі з enums
#   3. друкуємо повне порівняння
#
def main():
    replay_keys = load_replay_keys()
    if replay_keys is None:
        return

    enums_module = load_enums_module()
    if enums_module is None:
        return

    enum_keys, duplicates_report, empty_report = load_enum_keys(enums_module)

    levels = [
        "ROOT",
        "MARKET_CHANGE",
        "MARKET_DEFINITION",
        "RUNNER",
        "RUNNER_CHANGE",
    ]

    all_ok = True

    print_section("FULL ENUMS CHECK")

    for level in levels:
        replay_set = replay_keys.get(level, set())
        enum_set = enum_keys.get(level, set())

        missing_in_enums = replay_set - enum_set
        extra_in_enums = enum_set - replay_set
        duplicates = duplicates_report.get(level, [])
        empty_values = empty_report.get(level, [])

        print()
        print(f"[{level}]")
        print("-" * 70)

        # Повний список ключів із replay
        print_list("replay keys", replay_set)
        print()

        # Повний список ключів з enums
        print_list("enum keys", enum_set)
        print()

        # Пропущені у enums
        print_list("missing in enums", missing_in_enums)
        print()

        # Зайві у enums
        print_list("extra in enums", extra_in_enums)
        print()

        # Дублікати у enums
        print(f"duplicates in enums: {len(duplicates)}")
        for current_name, first_name, raw_value in duplicates:
            print(f"  - raw value '{raw_value}' used in {first_name} and {current_name}")
        print()

        # Порожні значення
        print(f"empty values in enums: {len(empty_values)}")
        for enum_name in empty_values:
            print(f"  - {enum_name}")
        print()

        # Статус рівня
        if not missing_in_enums and not extra_in_enums and not duplicates and not empty_values:
            print("status: OK")
        else:
            print("status: PROBLEMS FOUND")
            all_ok = False

    print_section("FINAL STATUS")

    if all_ok:
        print("ENUMS CHECK PASSED")
        print("нічого не пропущено")
        print("сміття не знайдено")
        print("дублікатів немає")
        print("порожніх значень немає")
    else:
        print("ENUMS CHECK FAILED")
        print("є пропущені ключі або зайві значення")
        print("є дублікати або порожні значення")
        print("треба виправити scripts/enums_replay.py")


if __name__ == "__main__":
    main()
