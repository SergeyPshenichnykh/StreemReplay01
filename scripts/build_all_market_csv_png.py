import json
import csv
from pathlib import Path

import matplotlib.pyplot as plt


REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end")
TARGET_MARKETS_FILE = Path("replay/target_markets.txt")
MARKETS_BASE_DIR = Path("replay/markets")


# Перевіряє валідність ціни.
# Відсікаємо сміття типу 1000.
def valid_price(price):
    if price is None:
        return False

    if not isinstance(price, (int, float)):
        return False

    if price < 1.01:
        return False

    if price > 100:
        return False

    return True


# Перевіряє валідність обсягу.
def valid_volume(volume):
    if volume is None:
        return False

    if not isinstance(volume, (int, float)):
        return False

    if volume < 0:
        return False

    return True


# Бере перший рівень стакану [price, size].
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


# Безпечна назва файла.
def safe_name(text):
    text = str(text)
    text = text.replace(" ", "_")
    text = text.replace("/", "_")
    text = text.replace("\\", "_")
    text = text.replace(":", "_")
    return text


# Читає replay/target_markets.txt
# і повертає список ринків:
# market_id | market_type | market_name | event_name
def load_target_markets():
    markets = []

    if not TARGET_MARKETS_FILE.exists():
        print(f"File not found: {TARGET_MARKETS_FILE}")
        return markets

    with TARGET_MARKETS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            parts = [p.strip() for p in line.split("|")]

            if len(parts) < 4:
                continue

            markets.append({
                "market_id": parts[0],
                "market_type": parts[1],
                "market_name": parts[2],
                "event_name": parts[3],
            })

    return markets


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    target_markets = load_target_markets()

    if not target_markets:
        print("target markets not found")
        return

    # market_map:
    # ключ = market_id
    # значення = службова інформація про ринок
    market_map = {}

    for m in target_markets:
        market_id = str(m["market_id"])

        market_map[market_id] = {
            "market_type": m["market_type"],
            "market_name": m["market_name"],
            "event_name": m["event_name"],
            "runner_names": {},
            "runner_state": {},
            "rows": [],
        }

    # Читаємо обрізаний replay-файл один раз.
    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")

            for mc in data.get("mc", []):
                market_id = str(mc.get("id"))

                # Беремо тільки потрібні ринки.
                if market_id not in market_map:
                    continue

                market_info = market_map[market_id]

                # -------------------------------------------------
                # 1. Назви runner-ів з marketDefinition.runners
                # -------------------------------------------------
                definition = mc.get("marketDefinition")
                if isinstance(definition, dict):
                    for runner in definition.get("runners", []):
                        runner_id = runner.get("id")
                        runner_name = runner.get("name")

                        if runner_id is None:
                            continue

                        market_info["runner_names"][runner_id] = str(runner_name)

                        if runner_id not in market_info["runner_state"]:
                            market_info["runner_state"][runner_id] = {
                                "ltp": None,
                                "tv": None,
                                "best_back_price": None,
                                "best_back_size": None,
                                "best_lay_price": None,
                                "best_lay_size": None,
                            }

                # -------------------------------------------------
                # 2. Оновлення стану runner-ів з rc[]
                # -------------------------------------------------
                for rc in mc.get("rc", []):
                    runner_id = rc.get("id")

                    if runner_id is None:
                        continue

                    if runner_id not in market_info["runner_state"]:
                        market_info["runner_state"][runner_id] = {
                            "ltp": None,
                            "tv": None,
                            "best_back_price": None,
                            "best_back_size": None,
                            "best_lay_price": None,
                            "best_lay_size": None,
                        }

                    state = market_info["runner_state"][runner_id]

                    if "ltp" in rc:
                        ltp = rc.get("ltp")
                        state["ltp"] = ltp if valid_price(ltp) else None

                    if "tv" in rc:
                        tv = rc.get("tv")
                        state["tv"] = tv if valid_volume(tv) else None

                    if "atb" in rc:
                        bb_price, bb_size = best_price_size(rc.get("atb"))
                        state["best_back_price"] = bb_price if valid_price(bb_price) else None
                        state["best_back_size"] = bb_size if valid_volume(bb_size) else None

                    if "atl" in rc:
                        bl_price, bl_size = best_price_size(rc.get("atl"))
                        state["best_lay_price"] = bl_price if valid_price(bl_price) else None
                        state["best_lay_size"] = bl_size if valid_volume(bl_size) else None

                # -------------------------------------------------
                # 3. Snapshot поточного стану ринку
                # -------------------------------------------------
                for runner_id, state in market_info["runner_state"].items():
                    market_info["rows"].append({
                        "market_id": market_id,
                        "timestamp_ms": pt,
                        "runner_id": runner_id,
                        "runner_name": market_info["runner_names"].get(runner_id, str(runner_id)),
                        "ltp": state["ltp"],
                        "tv": state["tv"],
                        "best_back_price": state["best_back_price"],
                        "best_back_size": state["best_back_size"],
                        "best_lay_price": state["best_lay_price"],
                        "best_lay_size": state["best_lay_size"],
                    })

    # -------------------------------------------------------------
    # 4. Для кожного ринку створюємо csv і png
    # -------------------------------------------------------------
    for market_id, market_info in market_map.items():
        rows = market_info["rows"]
        market_type = market_info["market_type"]

        if not rows:
            print(f"skip no rows : {market_id} | {market_type}")
            continue

        market_dir = MARKETS_BASE_DIR / market_type
        csv_dir = market_dir / "csv"
        plots_dir = market_dir / "plots"

        csv_dir.mkdir(parents=True, exist_ok=True)
        plots_dir.mkdir(parents=True, exist_ok=True)

        out_csv = csv_dir / f"market_{market_id.split('.')[-1]}_timeseries_clean.csv"
        out_png = plots_dir / f"market_{market_id.split('.')[-1]}_combined_clean.png"

        # Зберігаємо CSV
        with out_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "market_id",
                    "timestamp_ms",
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

        # Готуємо серії по runner-ах
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

            series[rid]["t"].append(row["timestamp_ms"])
            series[rid]["ltp"].append(row["ltp"])
            series[rid]["bb"].append(row["best_back_price"])
            series[rid]["bl"].append(row["best_lay_price"])
            series[rid]["tv"].append(row["tv"])

        # Будуємо один рисунок:
        # зверху — ціна
        # знизу — обсяг
        fig, (ax_price, ax_volume) = plt.subplots(
            figsize=(16, 10),
            nrows=2,
            sharex=True,
        )

        for rid, s in series.items():
            ax_price.plot(s["t"], s["ltp"], label=f"{s['name']} | LTP")
            ax_price.plot(s["t"], s["bb"], linestyle="--", label=f"{s['name']} | BACK")
            ax_price.plot(s["t"], s["bl"], linestyle=":", label=f"{s['name']} | LAY")

        ax_price.set_title(f"{market_id} — {market_type} — Price")
        ax_price.set_ylabel("price")
        ax_price.grid(True)
        ax_price.legend()

        for rid, s in series.items():
            ax_volume.plot(s["t"], s["tv"], label=f"{s['name']} | TV")

        ax_volume.set_title(f"{market_id} — {market_type} — Volume")
        ax_volume.set_xlabel("timestamp_ms")
        ax_volume.set_ylabel("volume")
        ax_volume.grid(True)
        ax_volume.legend()

        plt.tight_layout()
        plt.savefig(out_png, dpi=150)
        plt.close()

        print(f"done: {market_id} | {market_type}")
        print(f"csv : {out_csv}")
        print(f"png : {out_png}")
        print()


if __name__ == "__main__":
    main()
