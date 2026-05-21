import json
import time
from pathlib import Path

REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end_with_image")
TARGET_MARKET_ID = "1.131162806"

FRAME_DELAY_SECONDS = 0.05


def tick_size(price):
    if price < 2: return 0.01
    if price < 3: return 0.02
    if price < 4: return 0.05
    if price < 6: return 0.1
    if price < 10: return 0.2
    if price < 20: return 0.5
    if price < 30: return 1
    if price < 50: return 2
    if price < 100: return 5
    return 10


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
                            state[rid] = {"ltp": None, "tv": None}

                for rc in mc.get("rc", []):
                    rid = rc.get("id")

                    if rid not in state:
                        state[rid] = {"ltp": None, "tv": None}

                    if "ltp" in rc:
                        state[rid]["ltp"] = rc["ltp"]

                    if "tv" in rc:
                        state[rid]["tv"] = rc["tv"]

            move_top()

            print(f"TIME: {pt}")
            print()

            print(f"{'RUNNER':<20}{'BACK':>10}{'LAY':>10}{'LTP':>10}{'TV':>12}")
            print("-" * 62)

            for rid in state:
                s = state[rid]
                name = names.get(rid, rid)

                ltp = s["ltp"]

                if ltp is None:
                    back = None
                    lay = None
                else:
                    t = tick_size(ltp)
                    back = round(ltp - t, 2)
                    lay = round(ltp + t, 2)

                print(
                    f"{name:<20}"
                    f"{str(back):>10}"
                    f"{str(lay):>10}"
                    f"{str(ltp):>10}"
                    f"{str(s['tv']):>12}"
                )

            time.sleep(FRAME_DELAY_SECONDS)


if __name__ == "__main__":
    main()
