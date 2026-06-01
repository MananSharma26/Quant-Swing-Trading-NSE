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
from trading_engine.strategies.black_swan_pairs import BlackSwanPairsConfig, BlackSwanPairsStrategy

DATA_DIR = ROOT / "data"
INTERVAL = "day"
INITIAL_CASH = Decimal("1000000")

def run_black_swan():
    symbols = ["HDFCBANK", "HDFCLIFE"]
    candles = {}
    
    for symbol in symbols:
        path = DATA_DIR / "candles" / "NSE" / symbol / f"{INTERVAL}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} daily bars")
        else:
            print(f"  [ERROR] No {INTERVAL} data for {symbol} at {path}")
            return
            
    if len(candles) < 2:
        return
        
    config = BlackSwanPairsConfig(
        strategy_id=f"blackswan_{symbols[0]}_{symbols[1]}",
        symbol_a=symbols[0],
        symbol_b=symbols[1],
        quantity_a=100,
        quantity_b=90,  # 0.90 hedge ratio
        window_size=30, # 30 days
        entry_z_score=2.0, # Lowered from 3.5 to force trades
        exit_z_score=0.0,
        stop_loss_z_score=5.0, # Give it extreme room to breathe
    )
    
    print(f"\nRunning Black Swan Backtest for {symbols[0]} and {symbols[1]} on Daily data ...")
    strategy = BlackSwanPairsStrategy(config=config)
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
    print(f"\n{'=' * 50}")
    print(f"--- Results for {symbols[0]} / {symbols[1]} ---")
    print(f"Return: {m.total_return:.4f} ({m.total_pnl:+.2f} INR)")
    print(f"Win Rate: {m.win_rate:.4f} ({m.winning_trades}W / {m.losing_trades}L)")
    print(f"Max DD: {m.max_drawdown:.4f}")
    print(f"Total Trades: {m.trade_count}")
    print(f"{'=' * 50}\n")
    
    report_path = ROOT / "reports" / "black_swan_hdfc.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report.save_json(report_path)

if __name__ == "__main__":
    run_black_swan()
