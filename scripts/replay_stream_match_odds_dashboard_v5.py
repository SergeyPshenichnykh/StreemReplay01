import json
import time
from pathlib import Path


REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end")
TARGET_MARKET_ID = "1.131162806"

# Швидкість відтворення
FRAME_DELAY_SECONDS = 0.05

# Якщо True — друкувати тільки при реальній зміні стану
PRINT_ONLY_ON_STATE_CHANGE = True


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


def valid_size(size):
    if size is None:
        return False
    if not isinstance(size, (int, float)):
        return False
    if size < 0:
        return False
    return True


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


def clear_screen_once():
    print("\033[2J", end="")


def move_cursor_home():
    print("\033[H", end="")


def update_book(book, levels):
    """
    Delta-оновлення стакану.
    levels = [[price, size], ...]
    Якщо size == 0 -> рівень видаляється
    Інакше рівень додається/оновлюється
    """
    if not isinstance(levels, list):
        return

    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            continue

        price, size = level[0], level[1]

        if not valid_price(price):
            continue

        if not valid_size(size):
            continue

        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size


def reset_book(book, levels):
    """
    Повний reset стакану.
    Старий стакан очищається, далі записуються тільки нові рівні.
    """
    book.clear()
    update_book(book, levels)


def get_best_back(book):
    """
    Найкращий BACK = максимальна ціна з back_book
    """
    valid_prices = [p for p in book.keys() if valid_price(p)]
    if not valid_prices:
        return None, None

    best_price = max(valid_prices)
    best_size = book.get(best_price)

    if not valid_size(best_size) or best_size == 0:
        return best_price, None

    return best_price, best_size


def get_best_lay(book):
    """
    Найкращий LAY = мінімальна ціна з lay_book
    """
    valid_prices = [p for p in book.keys() if valid_price(p)]
    if not valid_prices:
        return None, None

    best_price = min(valid_prices)
    best_size = book.get(best_price)

    if not valid_size(best_size) or best_size == 0:
        return best_price, None

    return best_price, best_size


def snapshot_signature(runner_names, runner_state):
    items = []

    for runner_id in sorted(runner_state.keys(), key=lambda rid: runner_names.get(rid, str(rid))):
        state = runner_state[runner_id]

        best_back_price, best_back_size = get_best_back(state["back_book"])
        best_lay_price, best_lay_size = get_best_lay(state["lay_book"])

        items.append((
            runner_id,
            best_back_price,
            best_back_size,
            best_lay_price,
            best_lay_size,
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

        best_back_price, best_back_size = get_best_back(state["back_book"])
        best_lay_price, best_lay_size = get_best_lay(state["lay_book"])

        line = (
            f"{fmt_text(name, 20)}"
            f"{fmt_num(best_back_price)}"
            f"{fmt_num(best_back_size)}"
            f"{fmt_num(best_lay_price)}"
            f"{fmt_num(best_lay_size)}"
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

                # 1. Runner-и з marketDefinition
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
                                "back_book": {},
                                "lay_book": {},
                            }

                # 2. Оновлення по rc
                for rc in mc.get("rc", []):
                    runner_id = rc.get("id")
                    if runner_id is None:
                        continue

                    if runner_id not in runner_state:
                        runner_state[runner_id] = {
                            "ltp": None,
                            "tv": None,
                            "back_book": {},
                            "lay_book": {},
                        }

                    state = runner_state[runner_id]

                    # LTP
                    if "ltp" in rc:
                        p = rc.get("ltp")
                        if valid_price(p):
                            state["ltp"] = p

                    # TV
                    if "tv" in rc:
                        tv = rc.get("tv")
                        if isinstance(tv, (int, float)) and tv >= 0:
                            state["tv"] = tv

                    # FULL RESET BACK BOOK
                    if "batb" in rc:
                        reset_book(state["back_book"], rc.get("batb"))

                    # FULL RESET LAY BOOK
                    if "batl" in rc:
                        reset_book(state["lay_book"], rc.get("batl"))

                    # DELTA UPDATE BACK BOOK
                    if "atb" in rc:
                        update_book(state["back_book"], rc.get("atb"))

                    # DELTA UPDATE LAY BOOK
                    if "atl" in rc:
                        update_book(state["lay_book"], rc.get("atl"))

            if not market_updated:
                continue

            current_signature = snapshot_signature(runner_names, runner_state)

            if PRINT_ONLY_ON_STATE_CHANGE and current_signature == last_signature:
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
