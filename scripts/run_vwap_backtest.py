"""Run VWAP Trend Pullback backtest on locally stored Parquet candle data.

No Zerodha calls are made.  No live orders are placed.
Reads candle files from data/candles/NSE/{SYMBOL}/{interval}.parquet.

Usage:
    python3 scripts/run_vwap_backtest.py
    python3 scripts/run_vwap_backtest.py --symbols RELIANCE TCS
    python3 scripts/run_vwap_backtest.py --data-dir /path/to/data --output report.json

If no candle files are found the script exits with a clear message.
"""

from __future__ import annotations

import argparse
import sys
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
from trading_engine.strategies.vwap_pullback import (  # noqa: E402
    VWAPPullbackConfig,
    VWAPTrendPullbackStrategy,
)

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_OUTPUT = ROOT / "reports" / "vwap_backtest_report.json"
_DEFAULT_QUANTITY = 10


def _build_config(quantity: int) -> VWAPPullbackConfig:
    return VWAPPullbackConfig(
        strategy_id="vwap_pullback_v1",
        quantity=quantity,
        # Defaults from VWAPPullbackConfig:
        #   no_trade_before=09:30, no_new_entries_after=14:30, square_off_time=15:15
        #   vwap_slope_lookback_bars=5, min_bars_before_trading=15
        #   pullback_tolerance_bps=20.0, stop_loss_bps=40.0, target_bps=80.0
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run VWAP Trend Pullback backtest on local Parquet candle data."
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
    interval = args.interval
    initial_cash = Decimal(str(args.initial_cash))
    output_path = Path(args.output)

    print("\nVWAP Trend Pullback Backtest")
    print(f"  Symbols:      {args.symbols}")
    print(f"  Data dir:     {data_dir}")
    print(f"  Interval:     {interval}")
    print(f"  Initial cash: {initial_cash}")
    print(f"  Quantity:     {args.quantity}")

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
        print(
            "\nNo candle data found.\n"
            "Download historical data first:\n"
            "  python3 scripts/download_zerodha_historical.py \\\n"
            "    --config configs/default.yaml \\\n"
            "    --interval minute \\\n"
            "    --from-date 2025-01-01 \\\n"
            "    --to-date 2026-01-30 \\\n"
            "    --chunk-days 60\n"
        )
        sys.exit(0)

    config = _build_config(quantity=args.quantity)
    strategy = VWAPTrendPullbackStrategy(config=config)
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
            "vwap_slope_lookback_bars": config.vwap_slope_lookback_bars,
            "min_bars_before_trading": config.min_bars_before_trading,
            "pullback_tolerance_bps": config.pullback_tolerance_bps,
            "stop_loss_bps": config.stop_loss_bps,
            "target_bps": config.target_bps,
        },
    )

    print(f"\nRunning backtest on {list(candles.keys())} …")
    report = engine.run()

    print(f"\n{'=' * 50}")
    print(f"Strategy : {report.strategy_id}")
    print(f"Period   : {report.start_time} → {report.end_time}")
    print(f"Symbols  : {report.symbols}")
    print(f"Fills    : {len(report.fills)}")
    print(f"Equity   : {report.initial_cash} → {report.final_equity}")
    m = report.metrics
    print(f"Return   : {m.total_return:.4f}  ({m.total_pnl:+.2f} INR)")
    print(f"Max DD   : {m.max_drawdown:.4f}")
    print(f"Win rate : {m.win_rate:.4f}  ({m.winning_trades}W / {m.losing_trades}L)")
    print(f"Fees     : {m.total_fees:.2f}")
    print(f"{'=' * 50}\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report.save_json(output_path)
    print(f"Report saved to {output_path}")


if __name__ == "__main__":
    main()
