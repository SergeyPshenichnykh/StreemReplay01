import json
import time
from pathlib import Path

REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end_with_image")
TARGET_MARKET_ID = "1.131162806"
RUNNERS_FILE = Path("replay/markets/MATCH_ODDS/raw/market_131162806_runners.txt")

FRAME_DELAY_SECONDS = 0.05


def valid_price(p):
    return isinstance(p, (int, float)) and 1.01 <= p <= 100


def valid_size(size):
    return isinstance(size, (int, float)) and size >= 0


def update_book(book, levels):
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
    book.clear()
    update_book(book, levels)


def best_back(book):
    if not book:
        return None, None
    p = max(book.keys())
    return p, book.get(p)


def best_lay(book):
    if not book:
        return None, None
    p = min(book.keys())
    return p, book.get(p)


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


def clear_once():
    print("\033[2J", end="")


def move_top():
    print("\033[H", end="")


def load_runners_from_file():
    runner_names = {}

    if not RUNNERS_FILE.exists():
        return runner_names

    with RUNNERS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                continue

            try:
                rid = int(parts[0])
            except ValueError:
                continue

            name = parts[1]
            runner_names[rid] = name

    return runner_names


def new_runner_state():
    return {
        "ltp": None,
        "tv": None,
        "back": {},
        "lay": {},
    }


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_names = load_runners_from_file()
    state = {}

    # якщо є попередньо збережені runner-и — одразу створюємо state
    for rid in runner_names:
        state[rid] = new_runner_state()

    clear_once()

    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except Exception:
                continue

            pt = data.get("pt")
            market_seen = False

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                market_seen = True

                md = mc.get("marketDefinition")
                if isinstance(md, dict):
                    for r in md.get("runners", []):
                        rid = r.get("id")
                        name = r.get("name")

                        if rid is None:
                            continue

                        runner_names[rid] = str(name)

                        if rid not in state:
                            state[rid] = new_runner_state()

                for rc in mc.get("rc", []):
                    rid = rc.get("id")
                    if rid is None:
                        continue

                    # КРИТИЧНИЙ ФІКС:
                    # якщо runner прийшов спочатку через rc,
                    # а не через marketDefinition.runners,
                    # все одно створюємо його state
                    if rid not in state:
                        state[rid] = new_runner_state()

                    s = state[rid]

                    if "ltp" in rc and valid_price(rc["ltp"]):
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc and isinstance(rc["tv"], (int, float)) and rc["tv"] >= 0:
                        s["tv"] = rc["tv"]

                    if "batb" in rc:
                        reset_book(s["back"], rc["batb"])

                    if "batl" in rc:
                        reset_book(s["lay"], rc["batl"])

                    if "atb" in rc:
                        update_book(s["back"], rc["atb"])

                    if "atl" in rc:
                        update_book(s["lay"], rc["atl"])

            if not market_seen:
                continue

            move_top()

            print(f"TIME: {pt}")
            print()

            print(f"{'RUNNER':<20}{'BACK':>10}{'LAY':>10}{'LTP':>10}{'TV':>12}")
            print("-" * 62)

            for rid in sorted(state.keys(), key=lambda x: runner_names.get(x, str(x))):
                s = state[rid]
                name = runner_names.get(rid, str(rid))

                bb, _ = best_back(s["back"])
                bl, _ = best_lay(s["lay"])

                print(
                    f"{name:<20}"
                    f"{str(bb):>10}"
                    f"{str(bl):>10}"
                    f"{str(s['ltp']):>10}"
                    f"{str(s['tv']):>12}"
                )

            time.sleep(FRAME_DELAY_SECONDS)


if __name__ == "__main__":
    main()
