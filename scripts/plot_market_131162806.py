# =====================================================================
# plot_market_131162806.py
# =====================================================================
#
# ЩО РОБИТЬ ЦЕЙ СКРИПТ:
#
# 1. Читає replay-файл:
#       replay/football-pro-sample
#
# 2. Шукає тільки ринок:
#       1.131162806
#
# 3. Дістає runner-ів цього ринку з marketDefinition.runners
#
# 4. Далі проходить по всіх rc-оновленнях цього ринку
#
# 5. По кожному runner-у накопичує стан:
#       ltp
#       tv
#       best_back_price
#       best_back_size
#       best_lay_price
#       best_lay_size
#
# 6. На кожному tick формує snapshot стану
#
# 7. Записує все у CSV:
#       replay/market_131162806_timeseries.csv
#
# 8. Будує 2 графіки:
#       replay/market_131162806_price.png
#       replay/market_131162806_volume.png
#
# =====================================================================

import json
from pathlib import Path
from datetime import datetime, timezone

import csv
import matplotlib.pyplot as plt


# -------------------------------------------------------------
# НАЛАШТУВАННЯ
# -------------------------------------------------------------
# TARGET_MARKET_ID — той ринок, який ми хочемо витягнути
TARGET_MARKET_ID = "1.131162806"

# REPLAY_FILE — шлях до файлу запису
REPLAY_FILE = Path("replay/football-pro-sample")


# -------------------------------------------------------------
# to_dt(ms)
# -------------------------------------------------------------
# Перетворює timestamp у мілісекундах
# у нормальний час UTC
#
# Приклад:
#   1493129993571 -> 2017-04-25 12:59:53.571 UTC
#
def to_dt(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# -------------------------------------------------------------
# best_price_size(levels)
# -------------------------------------------------------------
# levels — це масив типу:
#   [[price, size], [price, size], ...]
#
# Ми беремо тільки перший рівень стакану:
#   best_back = перший елемент atb
#   best_lay  = перший елемент atl
#
# Повертає:
#   (price, size)
#
def best_price_size(levels):
    if not isinstance(levels, list):
        return None, None

    if len(levels) == 0:
        return None, None

    first = levels[0]

    if not isinstance(first, list):
        return None, None

    if len(first) < 2:
        return None, None

    return first[0], first[1]


# -------------------------------------------------------------
# main()
# -------------------------------------------------------------
def main():
    # Перевірка, чи існує replay-файл
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    # runner_names:
    # словник виду:
    #   {selection_id: runner_name}
    #
    # приклад:
    #   {58805: "Middlesbrough", 47999: "Man City", 123456: "The Draw"}
    #
    runner_names = {}

    # runner_state:
    # поточний накопичений стан кожного runner-а
    #
    # структура:
    #   {
    #       runner_id: {
    #           "ltp": ...,
    #           "tv": ...,
    #           "best_back_price": ...,
    #           "best_back_size": ...,
    #           "best_lay_price": ...,
    #           "best_lay_size": ...,
    #       }
    #   }
    #
    runner_state = {}

    # rows:
    # сюди будемо складати часовий ряд для CSV
    rows = []

    # Проходимо replay-файл построчно
    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            # пусті рядки пропускаємо
            if not line:
                continue

            # якщо JSON битий — пропускаємо
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            # pt = час поточного повідомлення
            pt = data.get("pt")
            dt = to_dt(pt)

            # mc = список ринків у поточному повідомленні
            mc_list = data.get("mc", [])

            for mc in mc_list:
                market_id = mc.get("id")

                # залишаємо тільки потрібний ринок
                if str(market_id) != TARGET_MARKET_ID:
                    continue

                # -------------------------------------------------
                # 1. ДІСТАТИ НАЗВИ RUNNER-ІВ
                # -------------------------------------------------
                # Назви runner-ів беремо з marketDefinition.runners
                definition = mc.get("marketDefinition")
                if isinstance(definition, dict):
                    runners = definition.get("runners", [])
                    for runner in runners:
                        runner_id = runner.get("id")
                        runner_name = runner.get("name")

                        if runner_id is None:
                            continue

                        # запам'ятовуємо назву
                        runner_names[runner_id] = str(runner_name)

                        # якщо цього runner-а ще нема в стані — створюємо
                        if runner_id not in runner_state:
                            runner_state[runner_id] = {
                                "ltp": None,
                                "tv": None,
                                "best_back_price": None,
                                "best_back_size": None,
                                "best_lay_price": None,
                                "best_lay_size": None,
                            }

                # -------------------------------------------------
                # 2. ОНОВИТИ СТАН RUNNER-ІВ З rc[]
                # -------------------------------------------------
                rc_list = mc.get("rc", [])

                for rc in rc_list:
                    runner_id = rc.get("id")
                    if runner_id is None:
                        continue

                    # якщо runner не був описаний раніше —
                    # все одно створюємо технічний запис
                    if runner_id not in runner_state:
                        runner_state[runner_id] = {
                            "ltp": None,
                            "tv": None,
                            "best_back_price": None,
                            "best_back_size": None,
                            "best_lay_price": None,
                            "best_lay_size": None,
                        }

                    state = runner_state[runner_id]

                    # ltp оновлюємо тільки якщо воно є в rc
                    if "ltp" in rc:
                        state["ltp"] = rc.get("ltp")

                    # tv оновлюємо тільки якщо воно є в rc
                    if "tv" in rc:
                        state["tv"] = rc.get("tv")

                    # atb = available to back
                    # беремо тільки найкращий рівень
                    if "atb" in rc:
                        bb_price, bb_size = best_price_size(rc.get("atb"))
                        state["best_back_price"] = bb_price
                        state["best_back_size"] = bb_size

                    # atl = available to lay
                    # беремо тільки найкращий рівень
                    if "atl" in rc:
                        bl_price, bl_size = best_price_size(rc.get("atl"))
                        state["best_lay_price"] = bl_price
                        state["best_lay_size"] = bl_size

                # -------------------------------------------------
                # 3. ПІСЛЯ ОНОВЛЕННЯ ЗБЕРЕГТИ SNAPSHOT
                # -------------------------------------------------
                # На поточному tick записуємо стан всіх runner-ів,
                # які вже відомі
                for runner_id, state in runner_state.items():
                    rows.append({
                        "market_id": TARGET_MARKET_ID,
                        "timestamp_ms": pt,
                        "timestamp_utc": dt.isoformat() if dt else "",
                        "runner_id": runner_id,
                        "runner_name": runner_names.get(runner_id, str(runner_id)),
                        "ltp": state["ltp"],
                        "tv": state["tv"],
                        "best_back_price": state["best_back_price"],
                        "best_back_size": state["best_back_size"],
                        "best_lay_price": state["best_lay_price"],
                        "best_lay_size": state["best_lay_size"],
                    })

    # -------------------------------------------------------------
    # 4. ЗБЕРЕГТИ CSV
    # -------------------------------------------------------------
    out_csv = Path("replay/market_131162806_timeseries.csv")

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "market_id",
                "timestamp_ms",
                "timestamp_utc",
                "runner_id",
                "runner_name",
                "ltp",
                "tv",
                "best_back_price",
                "best_back_size",
                "best_lay_price",
                "best_lay_size",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    # -------------------------------------------------------------
    # 5. ПІДГОТУВАТИ ДАНІ ДЛЯ ГРАФІКІВ
    # -------------------------------------------------------------
    # розкладемо рядки по runner-ах
    series = {}

    for row in rows:
        rid = row["runner_id"]
        rname = row["runner_name"]

        if rid not in series:
            series[rid] = {
                "name": rname,
                "t": [],
                "ltp": [],
                "bb": [],
                "bl": [],
                "tv": [],
            }

        # час
        series[rid]["t"].append(row["timestamp_ms"])

        # ціни
        series[rid]["ltp"].append(row["ltp"])
        series[rid]["bb"].append(row["best_back_price"])
        series[rid]["bl"].append(row["best_lay_price"])

        # обсяг
        series[rid]["tv"].append(row["tv"])

    # -------------------------------------------------------------
    # 6. ГРАФІК ЦІНИ
    # -------------------------------------------------------------
    plt.figure(figsize=(14, 8))

    for rid, s in series.items():
        # LTP
        plt.plot(s["t"], s["ltp"], label=f"{s['name']} | LTP")

        # BEST BACK
        plt.plot(s["t"], s["bb"], linestyle="--", label=f"{s['name']} | BACK")

        # BEST LAY
        plt.plot(s["t"], s["bl"], linestyle=":", label=f"{s['name']} | LAY")

    plt.title("Market 1.131162806 — Price")
    plt.xlabel("timestamp_ms")
    plt.ylabel("price")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig("replay/market_131162806_price.png", dpi=150)
    plt.close()

    # -------------------------------------------------------------
    # 7. ГРАФІК ОБСЯГУ
    # -------------------------------------------------------------
    plt.figure(figsize=(14, 8))

    for rid, s in series.items():
        plt.plot(s["t"], s["tv"], label=f"{s['name']} | TV")

    plt.title("Market 1.131162806 — Traded Volume")
    plt.xlabel("timestamp_ms")
    plt.ylabel("tv")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig("replay/market_131162806_volume.png", dpi=150)
    plt.close()

    # -------------------------------------------------------------
    # 8. ПІДСУМОК
    # -------------------------------------------------------------
    print(f"rows saved: {len(rows)}")
    print(f"csv       : {out_csv}")
    print("price png : replay/market_131162806_price.png")
    print("volume png: replay/market_131162806_volume.png")


if __name__ == "__main__":
    main()
