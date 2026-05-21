# =====================================================================
# check_replay_enums.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Знову витягує з нього всі ключі по рівнях:
#       ROOT
#       MARKET_CHANGE
#       MARKET_DEFINITION
#       RUNNER
#       RUNNER_CHANGE
#
# 3. Читає вже згенерований файл:
#       scripts/enums_replay.py
#
# 4. Забирає з нього всі enum-значення по тих самих рівнях
#
# 5. Порівнює:
#       - що є у replay-файлі, але нема в enums
#       - що є в enums, але нема у replay-файлі
#
# 6. Додатково перевіряє:
#       - дублікати значень усередині кожного class
#       - порожні значення
#
# 7. Виводить результат перевірки в консоль
#
# =====================================================================

import json
import importlib.util
from pathlib import Path


# ---------------------------------------------------------------------
# add_keys(target_set, data)
# ---------------------------------------------------------------------
# Додає всі ключі словника у вказаний набір.
#
# target_set — множина, куди складаємо ключі
# data       — словник, з якого беремо ключі
#
def add_keys(target_set, data):
    if not isinstance(data, dict):
        return

    for key in data.keys():
        target_set.add(key)


# ---------------------------------------------------------------------
# load_replay_keys()
# ---------------------------------------------------------------------
# Читає replay-файл і збирає ключі по рівнях.
#
# Повертає словник:
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

    root_keys = set()
    market_change_keys = set()
    market_definition_keys = set()
    runner_keys = set()
    runner_change_keys = set()

    with replay_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # ROOT
            add_keys(root_keys, data)

            # mc[]
            mc_list = data.get("mc", [])
            for mc in mc_list:
                add_keys(market_change_keys, mc)

                # marketDefinition
                definition = mc.get("marketDefinition")
                if isinstance(definition, dict):
                    add_keys(market_definition_keys, definition)

                    # runners[]
                    runners = definition.get("runners", [])
                    for runner in runners:
                        add_keys(runner_keys, runner)

                # rc[]
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
# Завантажує файл scripts/enums_replay.py як Python-модуль,
# щоб ми могли читати class ROOT, class MARKET_CHANGE і т.д.
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
#   1. словник {ENUM_NAME: raw_value}
#   2. список дублікатів значень
#   3. список порожніх значень
#
# Наприклад:
# class ROOT:
#     OP = 'op'
#     CLK = 'clk'
#
# Поверне:
#   {
#     "OP": "op",
#     "CLK": "clk"
#   }
#
def class_to_values(cls):
    values = {}
    seen_raw_values = {}
    duplicates = []
    empty_values = []

    for attr_name in dir(cls):
        # пропускаємо службові атрибути типу __class__
        if attr_name.startswith("__"):
            continue

        raw_value = getattr(cls, attr_name)

        # беремо тільки рядкові значення
        if not isinstance(raw_value, str):
            continue

        values[attr_name] = raw_value

        # порожні значення
        if raw_value.strip() == "":
            empty_values.append(attr_name)

        # дублікати значень
        if raw_value in seen_raw_values:
            duplicates.append((attr_name, seen_raw_values[raw_value], raw_value))
        else:
            seen_raw_values[raw_value] = attr_name

    return values, duplicates, empty_values


# ---------------------------------------------------------------------
# load_enum_keys(module)
# ---------------------------------------------------------------------
# Забирає всі raw-значення з class-ів:
# ROOT, MARKET_CHANGE, MARKET_DEFINITION, RUNNER, RUNNER_CHANGE
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
        if not hasattr(module, class_name):
            result[class_name] = set()
            duplicates_report[class_name] = []
            empty_report[class_name] = []
            continue

        cls = getattr(module, class_name)

        values, duplicates, empty_values = class_to_values(cls)

        result[class_name] = set(values.values())
        duplicates_report[class_name] = duplicates
        empty_report[class_name] = empty_values

    return result, duplicates_report, empty_report


# ---------------------------------------------------------------------
# print_section(title)
# ---------------------------------------------------------------------
# Красивий заголовок блоку в консолі
#
def print_section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------
# print_set(name, items)
# ---------------------------------------------------------------------
# Друк списку значень построчно
#
def print_set(name, items):
    print(f"{name}: {len(items)}")
    for item in sorted(items):
        print(f"  - {item}")


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------
# Головна логіка перевірки
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

    print_section("CHECK RESULT")

    all_ok = True

    for level in levels:
        replay_set = replay_keys.get(level, set())
        enum_set = enum_keys.get(level, set())

        missing_in_enums = replay_set - enum_set
        extra_in_enums = enum_set - replay_set
        duplicates = duplicates_report.get(level, [])
        empty_values = empty_report.get(level, [])

        print(f"\n[{level}]")
        print(f"replay keys : {len(replay_set)}")
        print(f"enum keys   : {len(enum_set)}")

        if not missing_in_enums and not extra_in_enums and not duplicates and not empty_values:
            print("status      : OK")
            continue

        all_ok = False
        print("status      : PROBLEMS FOUND")

        if missing_in_enums:
            print_set("missing in enums", missing_in_enums)

        if extra_in_enums:
            print_set("extra in enums", extra_in_enums)

        if duplicates:
            print(f"duplicates in enums: {len(duplicates)}")
            for current_name, first_name, raw_value in duplicates:
                print(f"  - {raw_value}  ({first_name} / {current_name})")

        if empty_values:
            print(f"empty values in enums: {len(empty_values)}")
            for enum_name in empty_values:
                print(f"  - {enum_name}")

    print_section("FINAL STATUS")

    if all_ok:
        print("ENUMS CHECK PASSED")
        print("нічого не пропущено")
        print("сміття не знайдено")
    else:
        print("ENUMS CHECK FAILED")
        print("є пропущені ключі або зайві значення")
        print("треба виправити enums_replay.py")


if __name__ == "__main__":
    main()
