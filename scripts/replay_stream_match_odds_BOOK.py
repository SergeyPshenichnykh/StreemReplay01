import json
import time
from pathlib import Path


REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

FRAME_DELAY_SECONDS = 0.05


def clear_once():
    print("\033[2J", end="")


def move_top():
    print("\033[H", end="")


def fmt(v, w=10):
    return f"{v:>{w}}" if v is not None else f"{'-':>{w}}"


def apply_levels(book, levels):
    """
    book: dict price -> size
    levels: [[price, size], ...]
    """
    if not isinstance(levels, list):
        return

    for lvl in levels:
        if not isinstance(lvl, list) or len(lvl) < 2:
            continue

        price, size = lvl[0], lvl[1]

        if not isinstance(price, (int, float)):
            continue

        if not isinstance(size, (int, float)):
            continue

        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size


def best_back(book):
    return max(book.keys()) if book else None


def best_lay(book):
    return min(book.keys()) if book else None


def main():
    if not REPLAY_FILE.exists():
        print("file not found")
        return

    clear_once()

    names = {}
    state = {}

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

                is_img = mc.get("img", False)

                md = mc.get("marketDefinition")
                if md:
                    for r in md.get("runners", []):
                        rid = r["id"]
                        names[rid] = r["name"]

                        if rid not in state:
                            state[rid] = {
                                "back": {},
                                "lay": {},
                                "ltp": None,
                                "tv": None,
                            }

                for rc in mc.get("rc", []):
                    rid = rc.get("id")
                    if rid is None:
                        continue

                    if rid not in state:
                        state[rid] = {
                            "back": {},
                            "lay": {},
                            "ltp": None,
                            "tv": None,
                        }

                    s = state[rid]

                    # 🔴 IMAGE → ПОВНИЙ RESET КНИГИ
                    if is_img:
                        if "atb" in rc:
                            s["back"] = {}
                            apply_levels(s["back"], rc["atb"])

                        if "atl" in rc:
                            s["lay"] = {}
                            apply_levels(s["lay"], rc["atl"])

                    else:
                        if "atb" in rc:
                            apply_levels(s["back"], rc["atb"])

                        if "atl" in rc:
                            apply_levels(s["lay"], rc["atl"])

                    if "ltp" in rc:
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc:
                        s["tv"] = rc["tv"]

            move_top()

            print(f"TIME: {pt}")
            print()

            print(f"{'RUNNER':<20}{'BACK':>10}{'LAY':>10}{'LTP':>10}{'TV':>12}")
            print("-" * 60)

            for rid in state:
                s = state[rid]

                back = best_back(s["back"])
                lay = best_lay(s["lay"])

                print(
                    f"{names.get(rid, rid):<20}"
                    f"{fmt(back)}"
                    f"{fmt(lay)}"
                    f"{fmt(s['ltp'])}"
                    f"{fmt(s['tv'], 12)}"
                )

            time.sleep(FRAME_DELAY_SECONDS)


if __name__ == "__main__":
    main()
