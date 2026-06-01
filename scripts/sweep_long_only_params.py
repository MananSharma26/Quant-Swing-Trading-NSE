"""Long-Only Black Swan parameter sweep for low-capital constraints."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import time
from decimal import Decimal
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from trading_engine.backtest.cost_model import CostModel
from trading_engine.backtest.data_feed import HistoricalDataFeed
from trading_engine.backtest.engine import BacktestEngine
from trading_engine.backtest.portfolio import BacktestPortfolio
from trading_engine.backtest.simulated_broker import SimulatedBroker
from trading_engine.backtest.slippage_model import SlippageModel
from trading_engine.strategies.long_only_swan import LongOnlySwanConfig, LongOnlySwanStrategy

DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
INITIAL_CASH = Decimal("100000") # 1 Lakh

PARAM_GRID = {
    "window_size": [30, 60, 90, 120, 180],
    "entry_z_score": [2.0, 2.5, 3.0, 3.5],
    "stop_loss_z_score": [4.0, 5.0, 6.0],
}

def build_grid() -> list[dict]:
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, combo, strict=True)) for combo in product(*PARAM_GRID.values())]

def load_candles(symbols: list[str]) -> dict[str, pd.DataFrame]:
    candles = {}
    for symbol in symbols:
        path = DATA_DIR / "candles" / "NSE" / symbol / "day.parquet"
        if path.exists():
            candles[symbol] = pd.read_parquet(path)
    return candles

def run_single(candles: dict[str, pd.DataFrame], params: dict, symbols: list[str], qty_a: int, qty_b: int) -> dict:
    try:
        cfg = LongOnlySwanConfig(
            strategy_id="long_only",
            symbol_a=symbols[0],
            symbol_b=symbols[1],
            quantity_a=qty_a,
            quantity_b=qty_b,
            window_size=int(params["window_size"]),
            entry_z_score=float(params["entry_z_score"]),
            stop_loss_z_score=float(params["stop_loss_z_score"]),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    strategy = LongOnlySwanStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=INITIAL_CASH)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed(candles, interval="day")
    
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=INITIAL_CASH,
        strategy_id=cfg.strategy_id,
        symbols=symbols,
        parameters={k: str(v) for k, v in params.items()},
    )

    report = engine.run()
    m = report.metrics

    row = {**params}
    row["error"] = None
    row["total_pnl"] = float(m.total_pnl) if m.total_pnl else None
    row["win_rate"] = float(m.win_rate) if m.win_rate else None
    row["trade_count"] = m.trade_count
    return row

def run_long_only_sweeper():
    pairs_file = REPORTS_DIR / "cointegrated_pairs.jsonl"
    if not pairs_file.exists():
        print("No cointegrated_pairs.jsonl found.")
        return
        
    pairs = []
    with open(pairs_file, "r") as f:
        for line in f:
            pairs.append(json.loads(line.strip()))
            
    # Sort by p_value and take top 15
    top_pairs = sorted([p for p in pairs if p["p_value"] < 0.05], key=lambda x: x["p_value"])[:15]
    print(f"Sweeping {len(top_pairs)} pairs with Long-Only Engine (1 Lakh Capital per leg)...")
    
    combos = build_grid()
    portfolio = []
    
    for rank, pair_info in enumerate(top_pairs, 1):
        sym_a = pair_info["symbol_a"]
        sym_b = pair_info["symbol_b"]
        
        candles = load_candles([sym_a, sym_b])
        if len(candles) < 2:
            continue
            
        # Get approximate price to size quantity to 1,00,000 INR
        price_a = candles[sym_a]["close"].iloc[-1]
        price_b = candles[sym_b]["close"].iloc[-1]
        
        qty_a = max(1, int(100000 / price_a))
        qty_b = max(1, int(100000 / price_b))
        
        print(f"\n[{rank}/15] {sym_a} / {sym_b} (Qty A: {qty_a}, Qty B: {qty_b})")
            
        best_pnl = 0
        best_result = None
        
        for i, params in enumerate(combos, 1):
            row = run_single(candles, params, [sym_a, sym_b], qty_a, qty_b)
            if not row.get("error"):
                pnl = row.get("total_pnl", 0) or 0
                trades = row.get("trade_count", 0) or 0
                
                if pnl > best_pnl and trades > 0:
                    best_pnl = pnl
                    best_result = row
                    
        if best_result:
            win_rate = best_result.get('win_rate') or 0.0
            print(f"  --> Best Config: win={best_result['window_size']} entZ={best_result['entry_z_score']} slZ={best_result['stop_loss_z_score']} "
                  f"| Trades: {best_result['trade_count']} | PnL: {best_result['total_pnl']:+.2f} | Win Rate: {win_rate:.3f}")
                  
            portfolio.append({
                "symbol_a": sym_a,
                "symbol_b": sym_b,
                "qty_a": qty_a,
                "qty_b": qty_b,
                "optimal_params": {
                    "window_size": best_result["window_size"],
                    "entry_z_score": best_result["entry_z_score"],
                    "stop_loss_z_score": best_result["stop_loss_z_score"],
                },
                "backtest_metrics": {
                    "pnl": best_result["total_pnl"],
                    "win_rate": best_result["win_rate"],
                    "trade_count": best_result["trade_count"],
                }
            })
        else:
            print("  --> No profitable config.")
            
    out_file = REPORTS_DIR / "optimal_long_only_portfolio.json"
    with open(out_file, "w") as f:
        json.dump(portfolio, f, indent=2)
        
    print(f"\nDone! {len(portfolio)} profitable Long-Only setups found. Saved to {out_file}")

if __name__ == "__main__":
    run_long_only_sweeper()
