import json
import time
from pathlib import Path


REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end")
TARGET_MARKET_ID = "1.131162806"

# Режим відтворення:
# 0.0   = без пауз, максимально швидко
# 0.05  = 50 мс між кадрами
# 0.20  = 200 мс між кадрами
FRAME_DELAY_SECONDS = 0.05

# Якщо True — очищати екран і оновлювати одну "живу" таблицю
# Якщо False — просто друкувати потік рядків вниз
CLEAR_SCREEN_EACH_FRAME = True


def best_price_size(levels):
    if not isinstance(levels, list) or len(levels) == 0:
        return None, None

    first = levels[0]

    if not isinstance(first, list) or len(first) < 2:
        return None, None

    return first[0], first[1]


def fmt_num(value, width=10, decimals=2):
    if value is None:
        return f"{'-':>{width}}"

    if isinstance(value, int):
        return f"{value:>{width}}"

    if isinstance(value, float):
        return f"{value:>{width}.{decimals}f}"

    return f"{str(value):>{width}}"


def clear_screen():
    print("\033[2J\033[H", end="")


def print_market_state(pt, market_id, runner_names, runner_state):
    if CLEAR_SCREEN_EACH_FRAME:
        clear_screen()

    print(f"MARKET_ID : {market_id}")
    print(f"TIME_PT   : {pt}")
    print()

    header = (
        f"{'RUNNER':<20}"
        f"{'BACK':>10}"
        f"{'B_SIZE':>12}"
        f"{'LAY':>10}"
        f"{'L_SIZE':>12}"
        f"{'LTP':>10}"
        f"{'TV':>14}"
    )
    print(header)
    print("-" * len(header))

    # сортуємо runner-ів за назвою, щоб порядок був стабільний
    for runner_id in sorted(runner_state.keys(), key=lambda rid: runner_names.get(rid, str(rid))):
        name = runner_names.get(runner_id, str(runner_id))
        state = runner_state[runner_id]

        line = (
            f"{name:<20}"
            f"{fmt_num(state['best_back_price'])}"
            f"{fmt_num(state['best_back_size'])}"
            f"{fmt_num(state['best_lay_price'])}"
            f"{fmt_num(state['best_lay_size'])}"
            f"{fmt_num(state['ltp'])}"
            f"{fmt_num(state['tv'], width=14)}"
        )
        print(line)

    print()
    print("Ctrl+C to stop")


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_names = {}
    runner_state = {}

    matched_ticks = 0

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

                if market_id != TARGET_MARKET_ID:
                    continue

                matched_ticks += 1

                # 1. Назви runner-ів
                definition = mc.get("marketDefinition")
                if isinstance(definition, dict):
                    for runner in definition.get("runners", []):
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

                # 2. Оновлення стану по rc
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
                        state["ltp"] = rc.get("ltp")

                    if "tv" in rc:
                        state["tv"] = rc.get("tv")

                    if "atb" in rc:
                        bb_price, bb_size = best_price_size(rc.get("atb"))
                        state["best_back_price"] = bb_price
                        state["best_back_size"] = bb_size

                    if "atl" in rc:
                        bl_price, bl_size = best_price_size(rc.get("atl"))
                        state["best_lay_price"] = bl_price
                        state["best_lay_size"] = bl_size

                # 3. Показати поточний стан ринку
                print_market_state(
                    pt=pt,
                    market_id=TARGET_MARKET_ID,
                    runner_names=runner_names,
                    runner_state=runner_state,
                )

                if FRAME_DELAY_SECONDS > 0:
                    time.sleep(FRAME_DELAY_SECONDS)

    print()
    print(f"Replay finished. matched ticks: {matched_ticks}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user")
