import json
import time
from pathlib import Path


REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end")
TARGET_MARKET_ID = "1.131162806"

# Швидкість відтворення:
# 0.00 = максимально швидко
# 0.02 = швидко
# 0.05 = помірно
FRAME_DELAY_SECONDS = 0.05

# Якщо True — друкувати лише коли реально був апдейт по ринку
PRINT_ONLY_ON_MARKET_UPDATE = True


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


def fmt_text(value, width):
    if value is None:
        value = "-"
    value = str(value)
    if len(value) > width:
        value = value[:width]
    return f"{value:<{width}}"


def move_cursor_home():
    # курсор у верхній лівий кут
    print("\033[H", end="")


def clear_screen_once():
    # очистити екран один раз на старті
    print("\033[2J", end="")


def snapshot_signature(runner_names, runner_state):
    # Підпис стану ринку — щоб розуміти, чи реально щось змінилось
    items = []

    for runner_id in sorted(runner_state.keys(), key=lambda rid: runner_names.get(rid, str(rid))):
        state = runner_state[runner_id]
        items.append((
            runner_id,
            state.get("best_back_price"),
            state.get("best_back_size"),
            state.get("best_lay_price"),
            state.get("best_lay_size"),
            state.get("ltp"),
            state.get("tv"),
        ))

    return tuple(items)


def build_screen(pt, market_id, runner_names, runner_state, matched_ticks):
    lines = []

    lines.append(f"MARKET_ID : {market_id}")
    lines.append(f"TIME_PT   : {pt}")
    lines.append(f"TICKS     : {matched_ticks}")
    lines.append("")

    header = (
        f"{'RUNNER':<20}"
        f"{'BACK':>10}"
        f"{'B_SIZE':>12}"
        f"{'LAY':>10}"
        f"{'L_SIZE':>12}"
        f"{'LTP':>10}"
        f"{'TV':>14}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for runner_id in sorted(runner_state.keys(), key=lambda rid: runner_names.get(rid, str(rid))):
        name = runner_names.get(runner_id, str(runner_id))
        state = runner_state[runner_id]

        line = (
            f"{fmt_text(name, 20)}"
            f"{fmt_num(state.get('best_back_price'))}"
            f"{fmt_num(state.get('best_back_size'))}"
            f"{fmt_num(state.get('best_lay_price'))}"
            f"{fmt_num(state.get('best_lay_size'))}"
            f"{fmt_num(state.get('ltp'))}"
            f"{fmt_num(state.get('tv'), width=14)}"
        )
        lines.append(line)

    lines.append("")
    lines.append("Ctrl+C to stop")

    return "\n".join(lines)


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_names = {}
    runner_state = {}

    matched_ticks = 0
    last_signature = None
    first_frame = True

    clear_screen_once()

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
            market_updated = False

            for mc in data.get("mc", []):
                market_id = str(mc.get("id"))

                if market_id != TARGET_MARKET_ID:
                    continue

                matched_ticks += 1
                market_updated = True

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

            if not market_updated and PRINT_ONLY_ON_MARKET_UPDATE:
                continue

            current_signature = snapshot_signature(runner_names, runner_state)

            # Якщо хочемо уникнути зайвих однакових кадрів
            if PRINT_ONLY_ON_MARKET_UPDATE and current_signature == last_signature:
                continue

            last_signature = current_signature

            screen = build_screen(
                pt=pt,
                market_id=TARGET_MARKET_ID,
                runner_names=runner_names,
                runner_state=runner_state,
                matched_ticks=matched_ticks,
            )

            if first_frame:
                print(screen, end="", flush=True)
                first_frame = False
            else:
                move_cursor_home()
                print(screen, end="", flush=True)

            if FRAME_DELAY_SECONDS > 0:
                time.sleep(FRAME_DELAY_SECONDS)

    print()
    print()
    print(f"Replay finished. matched ticks: {matched_ticks}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped by user")
