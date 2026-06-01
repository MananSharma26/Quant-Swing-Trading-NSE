import sys
import json
import math
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT)) # Needed to import from scripts folder

from trading_engine.backtest.cost_model import CostModel
from trading_engine.backtest.data_feed import HistoricalDataFeed
from trading_engine.backtest.engine import BacktestEngine
from trading_engine.backtest.portfolio import BacktestPortfolio
from trading_engine.backtest.simulated_broker import SimulatedBroker
from trading_engine.backtest.slippage_model import SlippageModel
from trading_engine.strategies.black_swan_pairs import BlackSwanPairsConfig, BlackSwanPairsStrategy

from scripts.sweep_black_swan_params import build_grid, run_single

DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
INTERVAL = "day"
INITIAL_CASH = Decimal("1000000")

def ensure_daily_data(symbol: str) -> bool:
    """Resample minute data to daily if daily doesn't exist."""
    day_path = DATA_DIR / "candles" / "NSE" / symbol / "day.parquet"
    if day_path.exists():
        return True
        
    min_path = DATA_DIR / "candles" / "NSE" / symbol / "minute.parquet"
    if not min_path.exists():
        return False
        
    print(f"  Resampling {symbol} from minute to daily...")
    df = pd.read_parquet(min_path)
    df = df.set_index("timestamp")
    
    daily_df = df.resample("D").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()
    
    daily_df = daily_df.reset_index()
    daily_df.to_parquet(day_path, index=False)
    return True

def load_candles(symbols: list[str]) -> dict[str, pd.DataFrame]:
    candles = {}
    for symbol in symbols:
        if ensure_daily_data(symbol):
            path = DATA_DIR / "candles" / "NSE" / symbol / "day.parquet"
            candles[symbol] = pd.read_parquet(path)
    return candles

def run_master_sweeper():
    pairs_file = REPORTS_DIR / "cointegrated_pairs.jsonl"
    if not pairs_file.exists():
        print("No cointegrated_pairs.jsonl found.")
        return
        
    pairs = []
    with open(pairs_file, "r") as f:
        for line in f:
            pairs.append(json.loads(line.strip()))
            
    # Sort by p_value and take top 15 (must be < 0.05)
    top_pairs = sorted([p for p in pairs if p["p_value"] < 0.05], key=lambda x: x["p_value"])[:15]
    print(f"Found {len(top_pairs)} highly cointegrated pairs.")
    
    # Grid of parameters to sweep for each pair
    combos = build_grid() # uses the grid from sweep_black_swan_params
    
    portfolio = []
    
    for rank, pair_info in enumerate(top_pairs, 1):
        sym_a = pair_info["symbol_a"]
        sym_b = pair_info["symbol_b"]
        hr = pair_info["hedge_ratio"]
        pval = pair_info["p_value"]
        
        print(f"\n[{rank}/15] Sweeping {sym_a} / {sym_b} (HR: {hr:.4f}, p={pval:.6f})")
        
        # Calculate quantities
        qty_a = 1000
        qty_b = max(1, int(abs(hr) * 1000))
        
        candles = load_candles([sym_a, sym_b])
        if len(candles) < 2:
            print(f"  Missing data. Skipping.")
            continue
            
        best_pnl = 0
        best_result = None
        
        for i, params in enumerate(combos, 1):
            row = run_single(
                candles=candles,
                params=params,
                initial_cash=INITIAL_CASH,
                symbols=[sym_a, sym_b],
                interval="day",
                run_index=i,
                qty_a=qty_a,
                qty_b=qty_b,
            )
            
            if not row.get("error"):
                pnl = row.get("total_pnl", 0) or 0
                trades = row.get("trade_count", 0) or 0
                
                # We only want results with positive PnL and >0 trades
                if pnl > best_pnl and trades > 0:
                    best_pnl = pnl
                    best_result = row
                    
        if best_result:
            print(f"  --> Best Config: win={best_result['window_size']} entZ={best_result['entry_z_score']} slZ={best_result['stop_loss_z_score']} "
                  f"| Trades: {best_result['trade_count']} | PnL: {best_result['total_pnl']:+.2f} | Win Rate: {best_result['win_rate']:.3f}")
                  
            portfolio.append({
                "symbol_a": sym_a,
                "symbol_b": sym_b,
                "qty_a": qty_a,
                "qty_b": qty_b,
                "hedge_ratio": hr,
                "p_value": pval,
                "optimal_params": {
                    "window_size": best_result["window_size"],
                    "entry_z_score": best_result["entry_z_score"],
                    "stop_loss_z_score": best_result["stop_loss_z_score"],
                },
                "backtest_metrics": {
                    "pnl": best_result["total_pnl"],
                    "win_rate": best_result["win_rate"],
                    "max_drawdown": best_result["max_drawdown"],
                    "trade_count": best_result["trade_count"],
                }
            })
        else:
            print("  --> No profitable configuration found for this pair.")
            
    # Save the optimized portfolio
    out_file = REPORTS_DIR / "optimal_portfolio.json"
    with open(out_file, "w") as f:
        json.dump(portfolio, f, indent=2)
        
    print(f"\n========================================================")
    print(f"Master Sweep Complete! {len(portfolio)} out of 15 pairs found profitable configs.")
    print(f"Saved optimal portfolio to: {out_file}")

if __name__ == "__main__":
    run_master_sweeper()
