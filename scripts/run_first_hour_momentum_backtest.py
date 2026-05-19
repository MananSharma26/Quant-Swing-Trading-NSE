"""Run First-Hour Momentum to Close backtest on locally stored Parquet candle data.

No broker API calls are made.  No live orders are placed.
Reads candle files from data/candles/NSE/{SYMBOL}/{interval}.parquet.

Usage:
    python3 scripts/run_first_hour_momentum_backtest.py
    python3 scripts/run_first_hour_momentum_backtest.py --symbols RELIANCE TCS
    python3 scripts/run_first_hour_momentum_backtest.py --allow-shorts
    python3 scripts/run_first_hour_momentum_backtest.py --output report.json

If no candle files are found the script exits with a clear message.
"""

from __future__ import annotations

import argparse
import sys
from datetime import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402 — after sys.path patch

from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.data_feed import HistoricalDataFeed  # noqa: E402
from trading_engine.backtest.engine import BacktestEngine  # noqa: E402
from trading_engine.backtest.portfolio import BacktestPortfolio  # noqa: E402
from trading_engine.backtest.simulated_broker import SimulatedBroker  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.strategies.first_hour_momentum import (  # noqa: E402
    FirstHourMomentumConfig,
    FirstHourMomentumStrategy,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT = ROOT / "reports" / "first_hour_momentum_report.json"


def _build_config(args: argparse.Namespace) -> FirstHourMomentumConfig:
    h, m = map(int, args.latest_entry_time.split(":"))
    latest_entry = time(h, m)
    # earliest_entry = session_start (09:15) + momentum_window_minutes
    session_minutes = 9 * 60 + 15 + args.momentum_window_minutes
    earliest_entry = time(session_minutes // 60, session_minutes % 60)

    return FirstHourMomentumConfig(
        strategy_id="first_hour_momentum_v1",
        quantity=args.quantity,
        momentum_window_minutes=args.momentum_window_minutes,
        earliest_entry_time=earliest_entry,
        latest_entry_time=latest_entry,
        min_first_window_return_bps=args.min_first_window_return_bps,
        allow_shorts=args.allow_shorts,
        min_bars_before_signal=args.momentum_window_minutes,
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run First-Hour Momentum to Close backtest on local Parquet data."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=_DEFAULT_SYMBOLS,
        help=f"Symbols to backtest (default: {_DEFAULT_SYMBOLS})",
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        default=str(_DEFAULT_DATA_DIR),
        help=f"Root data directory (default: {_DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--interval",
        default=_DEFAULT_INTERVAL,
        help=f"Candle interval (default: {_DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--initial-cash",
        dest="initial_cash",
        type=float,
        default=float(_DEFAULT_INITIAL_CASH),
        help=f"Starting cash in INR (default: {_DEFAULT_INITIAL_CASH})",
    )
    parser.add_argument(
        "--quantity",
        type=int,
        default=_DEFAULT_QUANTITY,
        help=f"Shares per trade (default: {_DEFAULT_QUANTITY})",
    )
    parser.add_argument(
        "--momentum-window-minutes",
        dest="momentum_window_minutes",
        type=int,
        default=30,
        help="First-window length in bars (default: 30)",
    )
    parser.add_argument(
        "--min-first-window-return-bps",
        dest="min_first_window_return_bps",
        type=float,
        default=60.0,
        help="Minimum first-window return in bps to qualify for entry (default: 60)",
    )
    parser.add_argument(
        "--latest-entry-time",
        dest="latest_entry_time",
        default="12:00",
        help="No new entries at or after this time HH:MM (default: 12:00)",
    )
    parser.add_argument(
        "--allow-shorts",
        dest="allow_shorts",
        action="store_true",
        default=False,
        help="Allow short entries on negative first-window momentum (default: False)",
    )
    parser.add_argument(
        "--output",
        default=str(_DEFAULT_OUTPUT),
        help=f"Path to save the JSON report (default: {_DEFAULT_OUTPUT})",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    initial_cash = Decimal(str(args.initial_cash))
    output_path = Path(args.output)
    interval = args.interval

    print("\nFirst-Hour Momentum to Close Backtest")
    print(f"  Requested symbols: {args.symbols}")
    print(f"  Data dir:          {data_dir}")
    print(f"  Interval:          {interval}")
    print(f"  Initial cash:      {initial_cash}")
    print(f"  Quantity:          {args.quantity}")
    print(f"  Window minutes:    {args.momentum_window_minutes}")
    print(f"  Min return bps:    {args.min_first_window_return_bps}")
    print(f"  Latest entry:      {args.latest_entry_time}")
    print(f"  Allow shorts:      {args.allow_shorts}")

    candles: dict[str, pd.DataFrame] = {}
    for symbol in args.symbols:
        path = data_dir / "candles" / "NSE" / symbol / f"{interval}.parquet"
        if not path.exists():
            print(f"  [skip] No data file for {symbol} at {path}")
            continue
        try:
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] Failed to read {symbol}: {exc}")

    if not candles:
        print("\nNo candle data found. Download historical data first, then re-run.\n")
        sys.exit(0)

    print(f"\nLoaded symbols: {list(candles.keys())}")

    config = _build_config(args)
    strategy = FirstHourMomentumStrategy(config=config)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed(candles, interval=interval)

    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=config.strategy_id,
        symbols=list(candles.keys()),
        parameters={
            "interval": interval,
            "momentum_window_minutes": config.momentum_window_minutes,
            "min_first_window_return_bps": config.min_first_window_return_bps,
            "stop_loss_bps": config.stop_loss_bps,
            "target_bps": str(config.target_bps),
            "allow_shorts": str(config.allow_shorts),
        },
    )

    print(f"\nRunning backtest on {list(candles.keys())} ...")
    report = engine.run()

    m = report.metrics
    print(f"\n{'=' * 55}")
    print(f"Strategy : {report.strategy_id}")
    print(f"Period   : {report.start_time} → {report.end_time}")
    print(f"Symbols  : {report.symbols}")
    print(f"Fills    : {len(report.fills)}")
    print(f"Equity   : {report.initial_cash} → {report.final_equity}")
    print(f"Return   : {m.total_return:.4f}  ({m.total_pnl:+.2f} INR)")
    print(f"Max DD   : {m.max_drawdown:.4f}")
    print(f"Win rate : {m.win_rate:.4f}  ({m.winning_trades}W / {m.losing_trades}L)")
    print(f"Fees     : {m.total_fees:.2f}")
    print(f"{'=' * 55}\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report.save_json(output_path)
    print(f"Report saved to {output_path}")


if __name__ == "__main__":
    main()
