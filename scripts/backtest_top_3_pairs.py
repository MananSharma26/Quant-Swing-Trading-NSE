import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from trading_engine.backtest.cost_model import CostModel
from trading_engine.backtest.data_feed import HistoricalDataFeed
from trading_engine.backtest.engine import BacktestEngine
from trading_engine.backtest.portfolio import BacktestPortfolio
from trading_engine.backtest.simulated_broker import SimulatedBroker
from trading_engine.backtest.slippage_model import SlippageModel
from trading_engine.strategies.pairs_trading import PairsTradingConfig, PairsTradingStrategy

DATA_DIR = ROOT / "data"
INTERVAL = "minute"
INITIAL_CASH = Decimal("1000000")

PAIRS_TO_TEST = [
    {
        "symbol_a": "HDFCBANK", "symbol_b": "HDFCLIFE", "qty_a": 100, "qty_b": 90,
        "report_path": ROOT / "reports" / "pairs_backtest_hdfc.json"
    },
    {
        "symbol_a": "BAJFINANCE", "symbol_b": "BAJAJFINSV", "qty_a": 100, "qty_b": 68,
        "report_path": ROOT / "reports" / "pairs_backtest_bajaj.json"
    },
    {
        "symbol_a": "ADANIPORTS", "symbol_b": "RELIANCE", "qty_a": 100, "qty_b": 117,
        "report_path": ROOT / "reports" / "pairs_backtest_adani_rel.json"
    }
]

def run_backtest_for_pair(pair_info):
    sym_a = pair_info["symbol_a"]
    sym_b = pair_info["symbol_b"]
    symbols = [sym_a, sym_b]
    
    candles = {}
    for symbol in symbols:
        path = DATA_DIR / "candles" / "NSE" / symbol / f"{INTERVAL}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} bars")
        else:
            print(f"  [skip] No data for {symbol} at {path}")
            return
            
    if len(candles) < 2:
        return
        
    config = PairsTradingConfig(
        strategy_id=f"pairs_{sym_a}_{sym_b}",
        symbol_a=sym_a,
        symbol_b=sym_b,
        quantity_a=pair_info["qty_a"],
        quantity_b=pair_info["qty_b"],
        window_size=360,
        entry_z_score=2.5,
        exit_z_score=0.0,
        stop_loss_z_score=4.0,
    )
    
    print(f"\nRunning backtest for {sym_a} and {sym_b} ...")
    strategy = PairsTradingStrategy(config=config)
    portfolio = BacktestPortfolio(initial_cash=INITIAL_CASH)
    broker = SimulatedBroker(portfolio, CostModel(), SlippageModel(bps=Decimal("2")))
    feed = HistoricalDataFeed(candles, interval=INTERVAL)
    
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=INITIAL_CASH,
        strategy_id=config.strategy_id,
        symbols=symbols,
        parameters={"interval": INTERVAL}
    )
    
    report = engine.run()
    m = report.metrics
    print(f"--- Results for {sym_a} / {sym_b} ---")
    print(f"Return: {m.total_return:.4f} ({m.total_pnl:+.2f} INR)")
    print(f"Win Rate: {m.win_rate:.4f} ({m.winning_trades}W / {m.losing_trades}L)")
    print(f"Max DD: {m.max_drawdown:.4f}")
    report.save_json(pair_info["report_path"])

def main():
    for p in PAIRS_TO_TEST:
        run_backtest_for_pair(p)

if __name__ == "__main__":
    main()
