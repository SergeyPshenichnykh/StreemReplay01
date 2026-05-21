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
    """
    Delta update стакану:
    levels = [[price, size], ...]
    size == 0 -> delete level
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
    Full reset стакану
    """
    book.clear()
    update_book(book, levels)


def first_valid_level(levels):
    """
    Беремо ПЕРШИЙ валідний рівень з atb/atl.
    Саме він і є актуальним top-of-book у цьому update.
    """
    if not isinstance(levels, list):
        return None, None

    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            continue

        price, size = level[0], level[1]

        if not valid_price(price):
            continue

        if not isinstance(size, (int, float)) or size <= 0:
            continue

        return price, size

    return None, None


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

            runner_names[rid] = parts[1]

    return runner_names


def new_runner_state():
    return {
        "ltp": None,
        "tv": None,
        "back": {},
        "lay": {},
        "top_back_price": None,
        "top_back_size": None,
        "top_lay_price": None,
        "top_lay_size": None,
    }


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_names = load_runners_from_file()
    state = {}

    # якщо runner-и вже відомі з txt — одразу створюємо state
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

                # runners з marketDefinition
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

                # updates по rc
                for rc in mc.get("rc", []):
                    rid = rc.get("id")
                    if rid is None:
                        continue

                    if rid not in state:
                        state[rid] = new_runner_state()

                    s = state[rid]

                    if "ltp" in rc and valid_price(rc["ltp"]):
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc and isinstance(rc["tv"], (int, float)) and rc["tv"] >= 0:
                        s["tv"] = rc["tv"]

                    # full reset back
                    if "batb" in rc:
                        reset_book(s["back"], rc["batb"])
                        p, sz = first_valid_level(rc["batb"])
                        if p is not None:
                            s["top_back_price"] = p
                            s["top_back_size"] = sz

                    # full reset lay
                    if "batl" in rc:
                        reset_book(s["lay"], rc["batl"])
                        p, sz = first_valid_level(rc["batl"])
                        if p is not None:
                            s["top_lay_price"] = p
                            s["top_lay_size"] = sz

                    # delta back
                    if "atb" in rc:
                        update_book(s["back"], rc["atb"])
                        p, sz = first_valid_level(rc["atb"])
                        if p is not None:
                            s["top_back_price"] = p
                            s["top_back_size"] = sz

                    # delta lay
                    if "atl" in rc:
                        update_book(s["lay"], rc["atl"])
                        p, sz = first_valid_level(rc["atl"])
                        if p is not None:
                            s["top_lay_price"] = p
                            s["top_lay_size"] = sz

            if not market_seen:
                continue

            move_top()

            print(f"TIME: {pt}")
            print()

            print(f"{'RUNNER':<20}{'BACK':>10}{'B_SIZE':>12}{'LAY':>10}{'L_SIZE':>12}{'LTP':>10}{'TV':>12}")
            print("-" * 76)

            for rid in sorted(state.keys(), key=lambda x: runner_names.get(x, str(x))):
                s = state[rid]
                name = runner_names.get(rid, str(rid))

                bb = s["top_back_price"]
                bsz = s["top_back_size"]
                bl = s["top_lay_price"]
                lsz = s["top_lay_size"]

                print(
                    f"{fmt_text(name, 20)}"
                    f"{fmt_num(bb)}"
                    f"{fmt_num(bsz, width=12)}"
                    f"{fmt_num(bl)}"
                    f"{fmt_num(lsz, width=12)}"
                    f"{fmt_num(s['ltp'])}"
                    f"{fmt_num(s['tv'], width=12)}"
                )

            time.sleep(FRAME_DELAY_SECONDS)


if __name__ == "__main__":
    main()
