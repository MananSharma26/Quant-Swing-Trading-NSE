"""Example script: run Pairs Trading backtest on locally stored Parquet candle data.

Usage:
    python scripts/run_pairs_backtest.py

Output:
    - Prints a summary to stdout.
    - Saves a JSON report to reports/pairs_backtest_report.json.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.data_feed import HistoricalDataFeed  # noqa: E402
from trading_engine.backtest.engine import BacktestEngine  # noqa: E402
from trading_engine.backtest.portfolio import BacktestPortfolio  # noqa: E402
from trading_engine.backtest.simulated_broker import SimulatedBroker  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.strategies.pairs_trading import PairsTradingConfig, PairsTradingStrategy  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Let's try two private banks that trade at very similar price points
SYMBOLS = ["AXISBANK", "ICICIBANK"]
DATA_DIR = ROOT / "data"
# Pairs usually run on 1m or 5m. Let's use 5m for fewer false signals,
# but the parquet files are in 'minute' and '5minute'. Let's try 'minute'.
INTERVAL = "minute"
INITIAL_CASH = Decimal("1000000")
REPORT_PATH = ROOT / "reports" / "pairs_backtest_report.json"

PAIRS_CONFIG = PairsTradingConfig(
    strategy_id="pairs_v1",
    symbol_a="AXISBANK",
    symbol_b="ICICIBANK",
    quantity_a=100,
    quantity_b=100,  # 1:1 weighting since prices are very similar
    window_size=360,  # 360-minute rolling window (1 full trading day)
    entry_z_score=2.5,
    exit_z_score=0.0,
    stop_loss_z_score=4.0,
)

def main() -> None:
    candles: dict[str, pd.DataFrame] = {}

    for symbol in SYMBOLS:
        # Check if we have data for the symbol
        path = DATA_DIR / "candles" / "NSE" / symbol / f"{INTERVAL}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} bars")
        else:
            print(f"  [skip] No data for {symbol} at {path}")

    if not candles or len(candles) < 2:
        print("\nNeed data for both symbols to run pairs backtest.")
        sys.exit(0)

    print(f"\nRunning Pairs Trading backtest on {SYMBOLS} ...")

    strategy = PairsTradingStrategy(config=PAIRS_CONFIG)
    portfolio = BacktestPortfolio(initial_cash=INITIAL_CASH)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed(candles, interval=INTERVAL)

    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=INITIAL_CASH,
        strategy_id=PAIRS_CONFIG.strategy_id,
        symbols=SYMBOLS,
        parameters={
            "interval": INTERVAL,
            "window_size": PAIRS_CONFIG.window_size,
            "entry_z_score": PAIRS_CONFIG.entry_z_score,
            "exit_z_score": PAIRS_CONFIG.exit_z_score,
            "stop_loss_z_score": PAIRS_CONFIG.stop_loss_z_score,
        },
    )

    report = engine.run()

    print(f"\n{'=' * 50}")
    print(f"Strategy : {report.strategy_id}")
    print(f"Period   : {report.start_time} -> {report.end_time}")
    print(f"Fills    : {len(report.fills)}")
    print(f"Equity   : {report.initial_cash} -> {report.final_equity}")
    m = report.metrics
    print(f"Return   : {m.total_return:.4f}  ({m.total_pnl:+.2f} INR)")
    print(f"Max DD   : {m.max_drawdown:.4f}")
    print(f"Win rate : {m.win_rate:.4f}  ({m.winning_trades}W / {m.losing_trades}L)")
    print(f"Fees     : {m.total_fees:.2f}")
    print(f"{'=' * 50}\n")

    # Ensure reports dir exists
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report.save_json(REPORT_PATH)
    print(f"Report saved to {REPORT_PATH}")

if __name__ == "__main__":
    main()
