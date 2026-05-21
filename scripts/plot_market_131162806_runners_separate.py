import json
import csv
from pathlib import Path
from datetime import datetime, timezone

import matplotlib.pyplot as plt


REPLAY_FILE = Path("replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

OUT_CSV = Path("replay/markets/MATCH_ODDS/csv/market_131162806_timeseries_clean.csv")
OUT_PLOTS_DIR = Path("replay/markets/MATCH_ODDS/plots")


# Перетворює timestamp у UTC-час
def to_dt(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# Бере перший рівень стакану [price, size]
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


# Перевіряє валідність ціни
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


# Перевіряє валідність обсягу
def valid_volume(volume):
    if volume is None:
        return False

    if not isinstance(volume, (int, float)):
        return False

    if volume < 0:
        return False

    return True


# Робить безпечну назву файла з назви runner-а
def safe_name(text):
    text = str(text)
    text = text.replace(" ", "_")
    text = text.replace("/", "_")
    text = text.replace("\\", "_")
    text = text.replace(":", "_")
    return text


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_names = {}
    runner_state = {}
    rows = []

    # Читання replay-файлу
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
            dt = to_dt(pt)

            for mc in data.get("mc", []):
                market_id = str(mc.get("id"))

                # тільки потрібний ринок
                if market_id != TARGET_MARKET_ID:
                    continue

                # runner-и з marketDefinition.runners
                definition = mc.get("marketDefinition")
                if isinstance(definition, dict):
                    runners = definition.get("runners", [])

                    for runner in runners:
                        runner_id = runner.get("id")
                        runner_name = runner.get("name")

                        if runner_id is None:
                            continue

                        runner_names[runner_id] = str(runner_name)

                        if runner_id not in runner_state:
                            runner_state[runner_id] = {
                                "ltp": None,
                                "tv": None,
                                "best_back_price": None,
                                "best_back_size": None,
                                "best_lay_price": None,
                                "best_lay_size": None,
                            }

                # оновлення стану по rc
                for rc in mc.get("rc", []):
                    runner_id = rc.get("id")
                    if runner_id is None:
                        continue

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

                # snapshot після кожного tick
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

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    OUT_PLOTS_DIR.mkdir(parents=True, exist_ok=True)

    # збереження CSV
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
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

    # серії по runner-ах
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

    # окремий рисунок для кожного runner-а
    for rid, s in series.items():
        fig, (ax_price, ax_volume) = plt.subplots(
            figsize=(16, 10),
            nrows=2,
            sharex=True,
        )

        # графік ціни
        ax_price.plot(s["t"], s["ltp"], label="LTP")
        ax_price.plot(s["t"], s["bb"], linestyle="--", label="BACK")
        ax_price.plot(s["t"], s["bl"], linestyle=":", label="LAY")

        ax_price.set_title(f"Market 1.131162806 — {s['name']} — Price")
        ax_price.set_ylabel("price")
        ax_price.set_ylim(1, 20)
        ax_price.grid(True)
        ax_price.legend()

        # графік обсягу
        ax_volume.plot(s["t"], s["tv"], label="TV")

        ax_volume.set_title(f"Market 1.131162806 — {s['name']} — Traded Volume")
        ax_volume.set_xlabel("timestamp_ms")
        ax_volume.set_ylabel("volume")
        ax_volume.grid(True)
        ax_volume.legend()

        plt.tight_layout()

        plot_file = OUT_PLOTS_DIR / f"market_131162806_{safe_name(s['name'])}_combined_clean.png"
        plt.savefig(plot_file, dpi=150)
        plt.close()

        print(f"saved: {plot_file}")

    print()
    print(f"rows saved: {len(rows)}")
    print(f"csv       : {OUT_CSV}")


if __name__ == "__main__":
    main()
