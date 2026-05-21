import json
import time
from pathlib import Path


# ============================================================
# SETTINGS
# ============================================================

REPLAY_FILE = Path("/home/nafanya/projects/betfair_bot/replay/football-pro-sample")

TARGET_MARKET_ID = "1.131162806"
TARGET_RUNNER_ID = 47999
TITLE = "ADAPTIVE LADDER FINAL"

FRAME_DELAY_SECONDS = 0.05

# how many ticks to show above / below center
TICKS_ABOVE = 15
TICKS_BELOW = 15

# True  -> center on spread midpoint when both sides exist
# False -> center on ltp first
CENTER_ON_MID = True

# your orders
MY_BACK_ORDERS = {
    # 1.15: 25,
    # 1.14: 252,
    # 1.13: 25,
    # 1.12: 235,
    # 1.11: 346,
    # 1.10: 325,
    # 1.09: 613,
    # 1.08: 613,
    # 1.07: 41,
    # 1.06: 643,
    # 1.05: 242,
    # 1.04: 316,
    # 1.03: 313,
    # 1.02: 34,
    # 1.01: 136,
}
MY_LAY_ORDERS = {
    # 1.20: 50,
    # 1.21: 75,
}


# ============================================================
# BETFAIR TICKS
# ============================================================

def tick_size(price: float) -> float:
    if price < 2:
        return 0.01
    if price < 3:
        return 0.02
    if price < 4:
        return 0.05
    if price < 6:
        return 0.10
    if price < 10:
        return 0.20
    if price < 20:
        return 0.50
    if price < 30:
        return 1.00
    if price < 50:
        return 2.00
    if price < 100:
        return 5.00
    return 10.00


def round_price(price: float) -> float:
    return round(float(price), 2)


def next_tick(price: float) -> float:
    return round_price(price + tick_size(price))


def prev_tick(price: float) -> float:
    out = round_price(price - tick_size(price))
    return max(1.01, out)


def build_all_ticks(min_price: float = 1.01, max_price: float = 1000.0) -> list[float]:
    prices = []
    p = round_price(min_price)

    while p <= max_price + 1e-9:
        prices.append(p)
        p = next_tick(p)
        if len(prices) > 10000:
            break

    return prices


ALL_TICKS_ASC = build_all_ticks()


def nearest_tick(price: float) -> float:
    if price <= 1.01:
        return 1.01
    if price >= 1000:
        return 1000.0

    best = ALL_TICKS_ASC[0]
    best_dist = abs(best - price)

    for p in ALL_TICKS_ASC:
        d = abs(p - price)
        if d < best_dist:
            best = p
            best_dist = d

    return best


def ladder_window(center_price: float, ticks_above: int, ticks_below: int) -> list[float]:
    center = nearest_tick(center_price)

    prices = [center]

    p = center
    for _ in range(ticks_above):
        p = next_tick(p)
        prices.append(p)

    p = center
    below = []
    for _ in range(ticks_below):
        p = prev_tick(p)
        below.append(p)

    prices.extend(below)
    prices = sorted(set(prices), reverse=True)
    return prices


# ============================================================
# BOOK ENGINE
# ============================================================

def is_number(value) -> bool:
    return isinstance(value, (int, float))


def valid_price(price) -> bool:
    return is_number(price) and 1.01 <= float(price) <= 1000


def valid_size(size) -> bool:
    return is_number(size)


def apply_offer_levels(book: dict[float, float], levels) -> None:
    """
    For atb / atl:
      size == 0 -> delete
      size > 0  -> set/update
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

        price = round_price(price)
        size = float(size)

        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size


def apply_traded_levels(traded_book: dict[float, float], levels) -> None:
    """
    For trd:
      treat as cumulative traded ladder by price.
      overwrite exact price with provided cumulative size.
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

        price = round_price(price)
        size = float(size)

        if size <= 0:
            traded_book.pop(price, None)
        else:
            traded_book[price] = size


def cleanup_book(book: dict[float, float]) -> None:
    bad = [p for p, s in book.items() if not is_number(s) or s <= 0]
    for p in bad:
        book.pop(p, None)


def best_back(back_book: dict[float, float]):
    valid = [(p, s) for p, s in back_book.items() if valid_price(p) and is_number(s) and s > 0]
    if not valid:
        return None, None
    p, s = max(valid, key=lambda x: x[0])
    return p, s


def best_lay(lay_book: dict[float, float]):
    valid = [(p, s) for p, s in lay_book.items() if valid_price(p) and is_number(s) and s > 0]
    if not valid:
        return None, None
    p, s = min(valid, key=lambda x: x[0])
    return p, s


# ============================================================
# FORMAT
# ============================================================

def clear_once() -> None:
    print("\033[2J", end="")


def move_top() -> None:
    print("\033[H", end="")


def fmt_num(value, width=8, decimals=2) -> str:
    if value is None:
        return f"{'':>{width}}"

    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return f"{int(round(value)):>{width}}"
        return f"{value:>{width}.{decimals}f}"

    if isinstance(value, int):
        return f"{value:>{width}}"

    return f"{str(value):>{width}}"


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if not REPLAY_FILE.exists():
        print(f"File not found: {REPLAY_FILE}")
        return

    runner_name = str(TARGET_RUNNER_ID)
    ltp = None
    tv = None

    back_book: dict[float, float] = {}
    lay_book: dict[float, float] = {}
    traded_book: dict[float, float] = {}

    ticks = 0

    clear_once()

    with REPLAY_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            pt = data.get("pt")
            market_seen = False

            for mc in data.get("mc", []):
                if str(mc.get("id")) != TARGET_MARKET_ID:
                    continue

                market_seen = True
                ticks += 1
                is_img = bool(mc.get("img", False))

                market_definition = mc.get("marketDefinition")
                if isinstance(market_definition, dict):
                    for runner in market_definition.get("runners", []):
                        if runner.get("id") == TARGET_RUNNER_ID:
                            runner_name = str(runner.get("name", TARGET_RUNNER_ID))

                for rc in mc.get("rc", []):
                    if rc.get("id") != TARGET_RUNNER_ID:
                        continue

                    # IMAGE:
                    # reset only the sides that actually arrived
                    if is_img:
                        if "atb" in rc:
                            back_book = {}
                            apply_offer_levels(back_book, rc["atb"])

                        if "atl" in rc:
                            lay_book = {}
                            apply_offer_levels(lay_book, rc["atl"])

                        if "trd" in rc:
                            traded_book = {}
                            apply_traded_levels(traded_book, rc["trd"])

                    # UPDATE:
                    # update only the sides that actually arrived
                    else:
                        if "atb" in rc:
                            apply_offer_levels(back_book, rc["atb"])

                        if "atl" in rc:
                            apply_offer_levels(lay_book, rc["atl"])

                        if "trd" in rc:
                            apply_traded_levels(traded_book, rc["trd"])

                    cleanup_book(back_book)
                    cleanup_book(lay_book)
                    cleanup_book(traded_book)

                    if "ltp" in rc and is_number(rc["ltp"]):
                        ltp = round_price(rc["ltp"])

                    if "tv" in rc and is_number(rc["tv"]):
                        tv = float(rc["tv"])

            if not market_seen:
                continue

            bb_price, bb_size = best_back(back_book)
            bl_price, bl_size = best_lay(lay_book)

            if CENTER_ON_MID and bb_price is not None and bl_price is not None:
                center_price = round_price((bb_price + bl_price) / 2.0)
            elif ltp is not None:
                center_price = ltp
            elif bb_price is not None and bl_price is not None:
                center_price = round_price((bb_price + bl_price) / 2.0)
            elif bb_price is not None:
                center_price = bb_price
            elif bl_price is not None:
                center_price = bl_price
            else:
                center_price = 2.00

            ladder_prices = ladder_window(center_price, TICKS_ABOVE, TICKS_BELOW)

            move_top()

            print(f"FILE      : {REPLAY_FILE.name}")
            print(f"MARKET_ID : {TARGET_MARKET_ID}")
            print(f"RUNNER_ID : {TARGET_RUNNER_ID}")
            print(f"RUNNER    : {runner_name}")
            print(f"TITLE     : {TITLE}")
            print(f"TIME_PT   : {pt}")
            print(f"TICKS     : {ticks}")
            print(f"LTP       : {ltp}")
            print(f"TV        : {tv}")
            print(f"BEST_BACK : {bb_price} @ {bb_size}")
            print(f"BEST_LAY  : {bl_price} @ {bl_size}")
            print(f"CENTER    : {center_price}")
            print()

            print(
                f"{'MY_BACK':>8} "
                f"{'BACK':>8} "
                f"{'PRICE':>8} "
                f"{'LAY':>8} "
                f"{'TRADED':>10} "
                f"{'MY_LAY':>8}"
            )
            print("-" * 60)

            for price in ladder_prices:
                my_back = MY_BACK_ORDERS.get(price)
                back_size = back_book.get(price)
                lay_size = lay_book.get(price)
                traded_size = traded_book.get(price)
                my_lay = MY_LAY_ORDERS.get(price)

                print(
                    f"{fmt_num(my_back, 8)} "
                    f"{fmt_num(back_size, 8)} "
                    f"{fmt_num(price, 8)} "
                    f"{fmt_num(lay_size, 8)} "
                    f"{fmt_num(traded_size, 10)} "
                    f"{fmt_num(my_lay, 8)}"
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
