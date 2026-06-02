"""Long-Only Black Swan parameter sweep for low-capital constraints.
Refactored to include Train/Test split, 5-year yfinance data, and realized PnL optimization.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from itertools import product
from pathlib import Path

import pandas as pd
import yfinance as yf

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

REPORTS_DIR = ROOT / "reports"
INITIAL_CASH = Decimal("100000") # 1 Lakh

PARAM_GRID = {
    "window_size": [30, 60, 90, 120],
    "entry_z_score": [2.0, 2.5, 3.0, 3.5],
    "stop_loss_z_score": [4.0, 5.0, 6.0],
}

# Structurally/Economically Cointegrated Pairs
SENSIBLE_PAIRS = [
    ("HDFCBANK", "HDFCLIFE"),
    ("BAJAJFINSV", "BAJFINANCE"),
    ("INFY", "TCS"),
    ("INFY", "WIPRO"),
    ("TCS", "WIPRO"),
    ("ICICIBANK", "AXISBANK"),
    ("ICICIBANK", "KOTAKBANK"),
    ("AXISBANK", "KOTAKBANK"),
    ("RELIANCE", "ONGC"),
    ("ITC", "HINDUNILVR")
]

def build_grid() -> list[dict]:
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, combo, strict=True)) for combo in product(*PARAM_GRID.values())]

def fetch_yfinance_data(symbols: list[str]) -> dict[str, pd.DataFrame]:
    candles = {}
    for sym in symbols:
        try:
            df = yf.download(f"{sym}.NS", period="5y", interval="1d", progress=False)
            if df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume"
            })
            df.index.name = "timestamp"
            df = df.ffill().dropna().reset_index()
            
            # Localize timezone to avoid issues with data feed
            if df["timestamp"].dt.tz is None:
                df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")
            else:
                df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
                
            candles[sym] = df
        except Exception as e:
            print(f"Failed to fetch {sym}: {e}")
    return candles

def split_train_test(candles: dict[str, pd.DataFrame], split_date: str) -> tuple[dict, dict]:
    train, test = {}, {}
    split_ts = pd.Timestamp(split_date).tz_localize("Asia/Kolkata")
    for sym, df in candles.items():
        train[sym] = df[df["timestamp"] < split_ts].copy()
        test[sym] = df[df["timestamp"] >= split_ts].copy()
    return train, test

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
    row["realized_pnl"] = float(m.realized_pnl) if m.realized_pnl else 0.0
    row["win_rate"] = float(m.win_rate) if m.win_rate else 0.0
    row["trade_count"] = m.trade_count
    row["max_drawdown"] = float(m.max_drawdown) if m.max_drawdown else 0.0
    return row

def run_sweeper():
    print("Fetching 5 years of history from Yahoo Finance...")
    all_symbols = list(set([sym for pair in SENSIBLE_PAIRS for sym in pair]))
    all_candles = fetch_yfinance_data(all_symbols)
    
    # Split Train: Before 2025-01-01, Test: 2025 onwards
    train_candles, test_candles = split_train_test(all_candles, "2025-01-01")
    
    combos = build_grid()
    portfolio = []
    
    print(f"\nSweeping {len(SENSIBLE_PAIRS)} Sensible Pairs (Train/Test Split)...")
    
    for rank, (sym_a, sym_b) in enumerate(SENSIBLE_PAIRS, 1):
        if sym_a not in all_candles or sym_b not in all_candles:
            continue
            
        price_a = all_candles[sym_a]["close"].iloc[-1]
        price_b = all_candles[sym_b]["close"].iloc[-1]
        
        qty_a = max(1, int(100000 / price_a))
        qty_b = max(1, int(100000 / price_b))
        
        print(f"\n[{rank}/{len(SENSIBLE_PAIRS)}] {sym_a} / {sym_b} (Qty A: {qty_a}, Qty B: {qty_b})")
            
        # 1. Train on In-Sample (pre-2025)
        best_pnl = 0
        best_result = None
        
        for i, params in enumerate(combos, 1):
            row = run_single(train_candles, params, [sym_a, sym_b], qty_a, qty_b)
            if not row.get("error"):
                pnl = row.get("realized_pnl", 0)
                trades = row.get("trade_count", 0)
                
                # REQUIRE at least 8 closed trades over 4 years
                if pnl > best_pnl and trades >= 8:
                    best_pnl = pnl
                    best_result = row
                    
        if best_result:
            # 2. Test on Out-Of-Sample (2025 onwards)
            test_row = run_single(test_candles, best_result, [sym_a, sym_b], qty_a, qty_b)
            test_pnl = test_row.get("realized_pnl", 0)
            test_trades = test_row.get("trade_count", 0)
            
            print(f"  [Train] PnL: {best_result['realized_pnl']:+.2f} | Trades: {best_result['trade_count']} | Config: w={best_result['window_size']} eZ={best_result['entry_z_score']} sZ={best_result['stop_loss_z_score']}")
            print(f"  [Test]  PnL: {test_pnl:+.2f} | Trades: {test_trades} | MaxDD: {test_row.get('max_drawdown', 0):.2f}%")
            
            if test_pnl >= 0:
                print("  --> Passed Out-Of-Sample! Added to portfolio.")
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
                    "train_metrics": best_result,
                    "test_metrics": test_row
                })
            else:
                print("  --> Failed Out-Of-Sample! Discarded.")
        else:
            print("  --> No profitable config found with minimum trade threshold in Train set.")
            
    out_file = REPORTS_DIR / "optimal_long_only_portfolio.json"
    with open(out_file, "w") as f:
        json.dump(portfolio, f, indent=2)
        
    print(f"\nDone! {len(portfolio)} verified Robust Pairs found. Saved to {out_file}")

if __name__ == "__main__":
    run_sweeper()
