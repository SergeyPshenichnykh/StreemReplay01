import json
import time
from pathlib import Path

REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

FRAME_DELAY_SECONDS = 0.05


def valid_level(level):
    return (
        isinstance(level, list)
        and len(level) >= 2
        and isinstance(level[0], (int, float))
        and isinstance(level[1], (int, float))
        and level[1] > 0
    )


def best_back(levels):
    if not isinstance(levels, list):
        return None, None

    valid = [lvl for lvl in levels if valid_level(lvl)]
    if not valid:
        return None, None

    lvl = max(valid, key=lambda x: x[0])  # 🔴 ключ
    return lvl[0], lvl[1]


def best_lay(levels):
    if not isinstance(levels, list):
        return None, None

    valid = [lvl for lvl in levels if valid_level(lvl)]
    if not valid:
        return None, None

    lvl = min(valid, key=lambda x: x[0])  # 🔴 ключ
    return lvl[0], lvl[1]


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
                        names[r["id"]] = r["name"]

                for rc in mc.get("rc", []):
                    rid = rc.get("id")

                    if rid not in state:
                        state[rid] = {
                            "ltp": None,
                            "tv": None,
                            "back": None,
                            "lay": None,
                        }

                    s = state[rid]

                    if "ltp" in rc:
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc:
                        s["tv"] = rc["tv"]

                    if "atb" in rc:
                        s["back"], _ = best_back(rc["atb"])

                    if "atl" in rc:
                        s["lay"], _ = best_lay(rc["atl"])

            move_top()

            print(f"TIME: {pt}")
            print()

            print(f"{'RUNNER':<20}{'BACK':>10}{'LAY':>10}{'LTP':>10}{'TV':>12}")
            print("-" * 62)

            for rid in state:
                s = state[rid]
                name = names.get(rid, rid)

                print(
                    f"{name:<20}"
                    f"{str(s['back']):>10}"
                    f"{str(s['lay']):>10}"
                    f"{str(s['ltp']):>10}"
                    f"{str(s['tv']):>12}"
                )

            time.sleep(FRAME_DELAY_SECONDS)


if __name__ == "__main__":
    main()
