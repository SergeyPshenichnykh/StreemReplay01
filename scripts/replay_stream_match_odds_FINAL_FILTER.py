import json
import time
from pathlib import Path


REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

FRAME_DELAY_SECONDS = 0.05


def valid_price(price):
    return isinstance(price, (int, float)) and 1.01 <= price <= 1000


def valid_size(size):
    return isinstance(size, (int, float)) and size > 0


def valid_level(level):
    return (
        isinstance(level, list)
        and len(level) >= 2
        and valid_price(level[0])
        and valid_size(level[1])
    )


def best_back(levels, ltp):
    """
    BACK:
    1. беремо максимум серед рівнів <= LTP
    2. якщо таких нема — fallback на загальний максимум
    """
    if not isinstance(levels, list):
        return None, None

    valid = [lvl for lvl in levels if valid_level(lvl)]
    if not valid:
        return None, None

    if isinstance(ltp, (int, float)):
        filtered = [lvl for lvl in valid if lvl[0] <= ltp]
        if filtered:
            price, size = max(filtered, key=lambda x: x[0])
            return price, size

    price, size = max(valid, key=lambda x: x[0])
    return price, size


def best_lay(levels, ltp):
    """
    LAY:
    1. беремо мінімум серед рівнів >= LTP
    2. якщо таких нема — fallback на загальний мінімум
    """
    if not isinstance(levels, list):
        return None, None

    valid = [lvl for lvl in levels if valid_level(lvl)]
    if not valid:
        return None, None

    if isinstance(ltp, (int, float)):
        filtered = [lvl for lvl in valid if lvl[0] >= ltp]
        if filtered:
            price, size = min(filtered, key=lambda x: x[0])
            return price, size

    price, size = min(valid, key=lambda x: x[0])
    return price, size


def clear_once():
    print("\033[2J", end="")


def move_top():
    print("\033[H", end="")


def fmt_num(value, width=10, decimals=2):
    if value is None:
        return f"{'-':>{width}}"
    if isinstance(value, int):
        return f"{value:>{width}}"
    if isinstance(value, float):
        return f"{value:>{width}.{decimals}f}"
    return f"{str(value):>{width}}"


def fmt_text(value, width):
    value = "-" if value is None else str(value)
    if len(value) > width:
        value = value[:width]
    return f"{value:<{width}}"


def new_runner_state():
    return {
        "ltp": None,
        "tv": None,
        "back_price": None,
        "back_size": None,
        "lay_price": None,
        "lay_size": None,
    }


def main():
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_names = {}
    state = {}
    ticks = 0

    clear_once()

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
            market_seen = False

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                market_seen = True
                ticks += 1

                md = mc.get("marketDefinition")
                if isinstance(md, dict):
                    for runner in md.get("runners", []):
                        rid = runner.get("id")
                        name = runner.get("name")

                        if rid is None:
                            continue

                        runner_names[rid] = str(name)

                        if rid not in state:
                            state[rid] = new_runner_state()

                for rc in mc.get("rc", []):
                    rid = rc.get("id")
                    if rid is None:
                        continue

                    if rid not in state:
                        state[rid] = new_runner_state()

                    s = state[rid]

                    if "ltp" in rc and isinstance(rc["ltp"], (int, float)):
                        s["ltp"] = rc["ltp"]

                    if "tv" in rc and isinstance(rc["tv"], (int, float)):
                        s["tv"] = rc["tv"]

                    if "atb" in rc:
                        bp, bs = best_back(rc["atb"], s["ltp"])
                        if bp is not None:
                            s["back_price"] = bp
                            s["back_size"] = bs

                    if "atl" in rc:
                        lp, ls = best_lay(rc["atl"], s["ltp"])
                        if lp is not None:
                            s["lay_price"] = lp
                            s["lay_size"] = ls

            if not market_seen:
                continue

            move_top()

            print(f"FILE      : {REPLAY_FILE.name}")
            print(f"MARKET_ID : {TARGET_MARKET_ID}")
            print(f"TIME_PT   : {pt}")
            print(f"TICKS     : {ticks}")
            print()

            print(
                f"{'RUNNER':<20}"
                f"{'BACK':>10}"
                f"{'B_SIZE':>12}"
                f"{'LAY':>10}"
                f"{'L_SIZE':>12}"
                f"{'LTP':>10}"
                f"{'TV':>14}"
            )
            print("-" * 88)

            for rid in sorted(state.keys(), key=lambda x: runner_names.get(x, str(x))):
                s = state[rid]
                name = runner_names.get(rid, str(rid))

                print(
                    f"{fmt_text(name, 20)}"
                    f"{fmt_num(s['back_price'])}"
                    f"{fmt_num(s['back_size'], width=12)}"
                    f"{fmt_num(s['lay_price'])}"
                    f"{fmt_num(s['lay_size'], width=12)}"
                    f"{fmt_num(s['ltp'])}"
                    f"{fmt_num(s['tv'], width=14)}"
                )

            print()
            print("Ctrl+C to stop")

            if FRAME_DELAY_SECONDS > 0:
                time.sleep(FRAME_DELAY_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped by user")
