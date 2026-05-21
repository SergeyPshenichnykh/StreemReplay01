import os
import time
import logging
from pathlib import Path

from flumine import FlumineSimulation, BaseStrategy, clients
from flumine.markets.market import Market
from betfairlightweight.resources import MarketBook


# -------------------------------------------------------------------
# НАЛАШТУВАННЯ
# -------------------------------------------------------------------

# Можеш залишити bz2 або дати інший historical stream файл
DATA_FILE = "football-pro-sample.bz2"

# Якщо файл лежить у replay/, лишай так:
DATA_PATH = Path("replay") / DATA_FILE

TARGET_MARKET_ID = "1.131162806"

# True  -> тільки перед матчем
# False -> і preplay, і inplay
ONLY_PREPLAY = False

# Пауза між кадрами для візуального replay
FRAME_DELAY_SECONDS = 0.05


# -------------------------------------------------------------------
# СЛУЖБОВІ ФУНКЦІЇ
# -------------------------------------------------------------------

def clear_once():
    print("\033[2J", end="")


def move_top():
    print("\033[H", end="")


def best_back(runner):
    if runner.ex.available_to_back:
        return runner.ex.available_to_back[0]["price"], runner.ex.available_to_back[0]["size"]
    return None, None


def best_lay(runner):
    if runner.ex.available_to_lay:
        return runner.ex.available_to_lay[0]["price"], runner.ex.available_to_lay[0]["size"]
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


# -------------------------------------------------------------------
# СТРАТЕГІЯ
# -------------------------------------------------------------------

class MatchOddsReplayDashboard(BaseStrategy):
    def start(self) -> None:
        clear_once()

    def check_market_book(self, market: Market, market_book: MarketBook) -> bool:
        if market_book.market_id != TARGET_MARKET_ID:
            return False

        if market_book.status == "CLOSED":
            return False

        if ONLY_PREPLAY and market_book.inplay:
            return False

        return True

    def process_market_book(self, market: Market, market_book: MarketBook) -> None:
        move_top()

        print(f"MARKET_ID : {market_book.market_id}")
        print(f"STATUS    : {market_book.status}")
        print(f"INPLAY    : {market_book.inplay}")
        print(f"SECONDS   : {round(market.seconds_to_start, 3)}")
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

        for runner in market_book.runners:
            bb, bsz = best_back(runner)
            bl, lsz = best_lay(runner)

            print(
                f"{fmt_text(runner.selection_id, 20) if not hasattr(runner, 'name') else fmt_text(getattr(runner, 'name', runner.selection_id), 20)}"
                f"{fmt_num(bb)}"
                f"{fmt_num(bsz, width=12)}"
                f"{fmt_num(bl)}"
                f"{fmt_num(lsz, width=12)}"
                f"{fmt_num(runner.last_price_traded)}"
                f"{fmt_num(runner.total_matched, width=14)}"
            )

        print()
        print("Ctrl+C to stop")

        time.sleep(FRAME_DELAY_SECONDS)


# -------------------------------------------------------------------
# ЗАПУСК
# -------------------------------------------------------------------

def main():
    if not DATA_PATH.exists():
        print(f"File not found: {DATA_PATH}")
        return

    # менше шуму від логів flumine
    logging.getLogger().setLevel(logging.CRITICAL)

    client = clients.SimulatedClient()
    framework = FlumineSimulation(client=client)

    strategy = MatchOddsReplayDashboard(
        market_filter={
            "markets": [str(DATA_PATH)],
            # можна лишити пусто, але так швидше фільтрувати
            "listener_kwargs": {
                "inplay": False if ONLY_PREPLAY else None,
            },
        }
    )

    framework.add_strategy(strategy)
    framework.run()


if __name__ == "__main__":
    main()
