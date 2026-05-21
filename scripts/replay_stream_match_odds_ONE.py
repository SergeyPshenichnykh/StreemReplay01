import json
import time
from pathlib import Path

REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")
TARGET_MARKET_ID = "1.131162806"

# 0.00 = максимально швидко
# 0.02 = швидко
# 0.05 = помірно
FRAME_DELAY_SECONDS = 0.05


def valid_price(price):
    return isinstance(price, (int, float)) and 1.01 <= price <= 1000


def valid_size(size):
    return isinstance(size, (int, float)) and size > 0


def best_back_from_levels(levels):
    if not isinstance(levels, list):
        return None, None

    valid = [
        lvl for lvl in levels
        if isinstance(lvl, list)
        and len(lvl) >= 2
        and valid_price(lvl[0])
        and valid_size(lvl[1])
    ]
    if not valid:
        return None, None

    # BACK = максимальна ціна
    price, size = max(valid, key=lambda x: x[0])
    return price, size


def best_lay_from_levels(levels):
    if not isinstance(levels, list):
        return None, None

    valid = [
        lvl for lvl in levels
        if isinstance(lvl, list)
        and len(lvl) >= 2
        and valid_price(lvl[0])
        and valid_size(lvl[1])
    ]
    if not valid:
        return None, None

    # LAY = мінімальна ціна
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

                # Якщо це image-кадр і є marketDefinition,
                # оновлюємо імена runner-ів.
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

                # Критично:
                # на img=True НЕ скидаємо стан повністю,
                # бо top-of-book / ltp / tv приходять окремими rc-блоками,
                # а нам потрібен повний кадр після проходу всіх rc.
                for rc in mc.get("rc", []):
                    rid = rc.get("id")
                    if rid is None:
                        continue

                    if rid not in state:
                        state[rid] = new_runner_state()

                    s = state[rid]

                    # LTP
                    if "ltp" in rc and isinstance(rc["ltp"], (int, float)):
                        s["ltp"] = rc["ltp"]

                    # TV
                    if "tv" in rc and isinstance(rc["tv"], (int, float)):
                        s["tv"] = rc["tv"]

                    # BACK from atb
                    if "atb" in rc:
                        bp, bs = best_back_from_levels(rc["atb"])
                        if bp is not None:
                            s["back_price"] = bp
                            s["back_size"] = bs

                    # LAY from atl
                    if "atl" in rc:
                        lp, ls = best_lay_from_levels(rc["atl"])
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
