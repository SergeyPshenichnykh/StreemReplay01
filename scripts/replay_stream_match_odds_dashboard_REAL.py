import json
import time
from pathlib import Path

REPLAY_FILE = Path("replay/football-pro-sample_cut_4h_to_end_with_image")
TARGET_MARKET_ID = "1.131162806"

FRAME_DELAY_SECONDS = 0.05


def valid_price(p):
    return isinstance(p, (int, float)) and 1.01 <= p <= 100


def first_valid(levels):
    if not isinstance(levels, list):
        return None

    for lvl in levels:
        if isinstance(lvl, list) and len(lvl) >= 2:
            p, s = lvl
            if valid_price(p) and isinstance(s, (int, float)) and s > 0:
                return p
    return None


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
                                "back": None,
                                "lay": None,
                            }

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

                    if "ltp" in rc and valid_price(rc["ltp"]):
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc:
                        s["tv"] = rc["tv"]

                    # 🔴 беремо тільки реальні ціни з потоку
                    if "atb" in rc:
                        p = first_valid(rc["atb"])
                        if p is not None:
                            s["back"] = p

                    if "atl" in rc:
                        p = first_valid(rc["atl"])
                        if p is not None:
                            s["lay"] = p

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
