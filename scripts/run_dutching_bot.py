#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bot.bot import DutchingBot
from bot.config import load_config
from bot.executor import PrintExecutor
from bot.stream import MarketDataStream
from bot.stream_replay_csv import ReplayCsvOptions, ReplayCsvStream


class NoopStream(MarketDataStream):
    def snapshots(self):
        raise SystemExit(
            "No live stream is wired yet.\n"
            "Next step: add a Betfair live MarketDataStream (betfairlightweight/flumine).\n"
            "For now you can keep using replay/CSV analysis scripts."
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run dutching bot (currently dry-run + stub stream).")
    p.add_argument("--config", type=Path, default=Path("configs/dutching_bot.example.json"))
    p.add_argument(
        "--replay-csv",
        type=Path,
        default=Path("replay/selected_market_snapshots_30m_update_250ms/selected_markets_250ms.csv"),
        help="Optional: run bot against replay CSV instead of live stream.",
    )
    p.add_argument("--max-rows", type=int, default=0, help="Replay-only debug limit (0 = full file).")
    p.add_argument(
        "--poll-interval-s",
        type=float,
        default=None,
        help="Override config poll interval. If omitted in replay mode, defaults to 0 (no sleep).",
    )
    p.add_argument("--max-signals", type=int, default=0, help="Stop after emitting this many signals (0 = unlimited).")
    p.add_argument("--print-orders", action="store_true", help="Also print per-runner ORDER lines.")
    p.add_argument(
        "--all-markets",
        action="store_true",
        help="Replay-only: ignore config markets and run on every market_id present in the replay CSV.",
    )
    p.add_argument("--live", action="store_true", help="Force live mode (currently not implemented).")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)

    if args.live:
        stream: MarketDataStream = NoopStream()
    else:
        if args.replay_csv.exists():
            stream = ReplayCsvStream(ReplayCsvOptions(path=args.replay_csv, max_rows=args.max_rows))
        else:
            stream = NoopStream()

    # Replay is file-based; sleeping makes runs look "hung". Default to no sleep unless overridden.
    if not args.live and args.poll_interval_s is None:
        cfg = cfg.__class__(
            markets=cfg.markets,
            signal=cfg.signal,
            staking=cfg.staking,
            risk=cfg.risk,
            poll_interval_s=0.0,
            dry_run=cfg.dry_run,
        )

    if args.all_markets:
        if not args.replay_csv.exists():
            raise SystemExit(f"--all-markets requires replay CSV to exist: {args.replay_csv}")
        cfg = cfg.__class__(
            markets=[],
            signal=cfg.signal,
            staking=cfg.staking,
            risk=cfg.risk,
            poll_interval_s=cfg.poll_interval_s,
            dry_run=cfg.dry_run,
        )
        print(f"Replay all-markets: enabled (no market_id filter)")
    elif args.poll_interval_s is not None:
        cfg = cfg.__class__(
            markets=cfg.markets,
            signal=cfg.signal,
            staking=cfg.staking,
            risk=cfg.risk,
            poll_interval_s=float(args.poll_interval_s),
            dry_run=cfg.dry_run,
        )

    bot = DutchingBot(
        config=cfg,
        stream=stream,
        executor=PrintExecutor(enabled=args.print_orders),
        print_orders=args.print_orders,
        max_signals=args.max_signals,
    )
    bot.run()


if __name__ == "__main__":
    main()
