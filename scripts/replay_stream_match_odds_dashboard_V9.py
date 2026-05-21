import json
import time
from pathlib import Path

REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end_with_image")
TARGET_MARKET_ID = "1.131162806"

FRAME_DELAY_SECONDS = 0.05


def valid_price(p):
    return isinstance(p, (int, float)) and 1.01 <= p <= 100


def valid_size(s):
    return isinstance(s, (int, float)) and s > 0


def update_book(book, levels):
    if not isinstance(levels, list):
        return

    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            continue

        price, size = level

        if not valid_price(price):
            continue

        if not isinstance(size, (int, float)):
            continue

        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size


def normalize_book(book):
    # 🔴 ключовий фікс
    invalid = [p for p, s in book.items() if not valid_size(s)]
    for p in invalid:
        book.pop(p, None)


def best_back(book):
    prices = [p for p, s in book.items() if valid_price(p) and valid_size(s)]
    if not prices:
        return None
    return max(prices)


def best_lay(book):
    prices = [p for p, s in book.items() if valid_price(p) and valid_size(s)]
    if not prices:
        return None
    return min(prices)


def clear_once():
    print("\033[2J", end="")


def move_top():
    print("\033[H", end="")


def main():
    state = {}
    names = {}

    clear_once()

    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
            except:
                continue

            pt = data.get("pt")

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                md = mc.get("marketDefinition")
                if md:
                    for r in md.get("runners", []):
                        rid = r["id"]
                        names[rid] = r["name"]

                        if rid not in state:
                            state[rid] = {
                                "ltp": None,
                                "tv": None,
                                "back": {},
                                "lay": {}
                            }

                for rc in mc.get("rc", []):
                    rid = rc.get("id")

                    if rid not in state:
                        state[rid] = {
                            "ltp": None,
                            "tv": None,
                            "back": {},
                            "lay": {}
                        }

                    s = state[rid]

                    if "ltp" in rc and valid_price(rc["ltp"]):
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc:
                        s["tv"] = rc["tv"]

                    if "batb" in rc:
                        s["back"].clear()
                        update_book(s["back"], rc["batb"])

                    if "batl" in rc:
                        s["lay"].clear()
                        update_book(s["lay"], rc["batl"])

                    if "atb" in rc:
                        update_book(s["back"], rc["atb"])

                    if "atl" in rc:
                        update_book(s["lay"], rc["atl"])

                    # 🔴 ОСЬ ГОЛОВНЕ
                    normalize_book(s["back"])
                    normalize_book(s["lay"])

            move_top()

            print(f"TIME: {pt}")
            print()

            print(f"{'RUNNER':<20}{'BACK':>10}{'LAY':>10}{'LTP':>10}{'TV':>12}")
            print("-" * 62)

            for rid in state:
                s = state[rid]
                name = names.get(rid, rid)

                bb = best_back(s["back"])
                bl = best_lay(s["lay"])

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
