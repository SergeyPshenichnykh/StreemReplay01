# =====================================================================
# extract_events.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Проходить по кожному рядку файлу
#    (у цьому файлі 1 рядок = 1 JSON-об'єкт)
#
# 3. Шукає всередині кожного JSON:
#       mc[] -> marketDefinition -> eventId
#       mc[] -> marketDefinition -> eventName
#
# 4. Складає всі знайдені події в окремий список
#
# 5. Прибирає дублікати
#    (бо одна і та сама подія може зустрічатись багато разів)
#
# 6. Виводить результат у консоль
#
# 7. Додатково зберігає результат у файл:
#       replay/events.txt
#
# =====================================================================

import json
from pathlib import Path


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------
# Це головна функція скрипта.
# Саме тут виконується вся робота:
#   - відкриття файлу
#   - читання рядків
#   - пошук eventId / eventName
#   - збереження результату
#
def main():
    # Шлях до replay-файлу.
    # Тут ми жорстко вказуємо той файл, з яким зараз працюємо.
    replay_file = Path("replay/football-pro-sample")

    # Перевірка: чи існує файл.
    # Якщо файлу немає — далі працювати нема з чим.
    if not replay_file.exists():
        print(f"File not found: {replay_file}")
        return

    # events_set — це множина.
    # Множина автоматично НЕ допускає дублікати.
    #
    # Тобто якщо одна подія зустрінеться 1000 разів,
    # у результаті вона залишиться тільки 1 раз.
    events_set = set()

    # Відкриваємо replay-файл у режимі читання.
    with replay_file.open("r", encoding="utf-8") as f:

        # Читаємо файл построчно.
        for line in f:
            # Прибираємо пробіли і символ переносу рядка.
            line = line.strip()

            # Якщо рядок пустий — пропускаємо його.
            if not line:
                continue

            # Пробуємо перетворити рядок JSON у Python-об'єкт.
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                # Якщо рядок битий або не JSON — пропускаємо його.
                continue

            # Беремо список market changes.
            # Це список ринків, які є в поточному повідомленні.
            mc_list = data.get("mc", [])

            # Проходимо по кожному ринку всередині mc[].
            for mc in mc_list:
                # Беремо опис ринку.
                definition = mc.get("marketDefinition", {})

                # Якщо marketDefinition не словник — пропускаємо.
                if not isinstance(definition, dict):
                    continue

                # Витягуємо eventId.
                event_id = definition.get("eventId")

                # Витягуємо eventName.
                event_name = definition.get("eventName")

                # Якщо хоча б одного з цих полів немає —
                # пропускаємо такий запис.
                if event_id is None or event_name is None:
                    continue

                # Додаємо подію у множину.
                #
                # Ми зберігаємо подію як пару:
                #   (eventId, eventName)
                #
                # Це зручно, тому що:
                #   - eventId унікальний
                #   - eventName читабельний для людини
                events_set.add((str(event_id), str(event_name)))

    # Перетворюємо множину у список і сортуємо.
    # Сортування робимо по eventId.
    events_list = sorted(events_set, key=lambda x: x[0])

    # Виводимо кількість унікальних подій.
    print(f"events found: {len(events_list)}")
    print()

    # Виводимо всі події в консоль.
    for event_id, event_name in events_list:
        print(f"{event_id} | {event_name}")

    # Шлях до файлу, куди збережемо результат.
    out_file = Path("replay/events.txt")

    # Записуємо список подій у файл.
    with out_file.open("w", encoding="utf-8") as f:
        for event_id, event_name in events_list:
            f.write(f"{event_id} | {event_name}\n")

    # Повідомляємо, куди збережений результат.
    print()
    print(f"Saved: {out_file}")


# ---------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------
# Якщо файл запускається напряму командою:
#   python scripts/extract_events.py
# то буде виконана функція main()
#
if __name__ == "__main__":
    main()
