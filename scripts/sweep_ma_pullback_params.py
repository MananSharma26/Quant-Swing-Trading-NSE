"""MA Pullback parameter sweep with Train/Test split and robust OOS validation."""

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
from trading_engine.strategies.ma_pullback import MAPullbackConfig, MAPullbackStrategy

REPORTS_DIR = ROOT / "reports"
INITIAL_CASH = Decimal("200000") # 2 Lakhs (with 1 Lakh max per trade)
CAPITAL_PER_TRADE = 100000

PARAM_GRID = {
    "pullback_ma_period": [20, 50],
    "rsi_oversold": [35.0, 40.0, 45.0],
    "stop_loss_pct": [5.0, 8.0, 12.0],
    "target_pct": [10.0, 15.0, 20.0],
    "max_hold_days": [30, 45, 60],
}

SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", 
    "ITC", "LT", "BAJFINANCE", "BHARTIARTL", "SBIN"
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

def run_single(candles: dict[str, pd.DataFrame], params: dict, symbol: str) -> dict:
    try:
        cfg = MAPullbackConfig(
            strategy_id="ma_pullback",
            symbol=symbol,
            capital_per_trade=CAPITAL_PER_TRADE,
            trend_ma_period=100, # Relaxed from 200
            pullback_ma_period=int(params["pullback_ma_period"]),
            rsi_period=14, # Fixed
            rsi_oversold=float(params["rsi_oversold"]),
            stop_loss_pct=float(params["stop_loss_pct"]),
            target_pct=float(params["target_pct"]),
            max_hold_days=int(params["max_hold_days"]),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    strategy = MAPullbackStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=INITIAL_CASH)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed({symbol: candles[symbol]}, interval="day")
    
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=INITIAL_CASH,
        strategy_id=cfg.strategy_id,
        symbols=[symbol],
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
    all_candles = fetch_yfinance_data(SYMBOLS)
    
    # Split Train: Before 2025-01-01, Test: 2025 onwards
    train_candles, test_candles = split_train_test(all_candles, "2025-01-01")
    
    combos = build_grid()
    portfolio = []
    
    print(f"\nSweeping {len(SYMBOLS)} Symbols (Train/Test Split)...")
    
    for rank, sym in enumerate(SYMBOLS, 1):
        if sym not in all_candles:
            continue
            
        print(f"\n[{rank}/{len(SYMBOLS)}] {sym}")
            
        # 1. Train on In-Sample (pre-2025)
        best_pnl = 0
        best_result = None
        
        for i, params in enumerate(combos, 1):
            row = run_single(train_candles, params, sym)
            if not row.get("error"):
                pnl = row.get("realized_pnl", 0)
                trades = row.get("trade_count", 0)
                drawdown = row.get("max_drawdown", 1.0)
                
                # REQUIRE at least 5 closed trades over 4 years AND Max DD < 20%
                if pnl > best_pnl and trades >= 5 and drawdown < 0.20:
                    best_pnl = pnl
                    best_result = row
                    
        if best_result:
            # 2. Test on Out-Of-Sample (2025 onwards)
            test_row = run_single(test_candles, best_result, sym)
            test_pnl = test_row.get("realized_pnl", 0)
            test_trades = test_row.get("trade_count", 0)
            
            print(f"  [Train] PnL: {best_result['realized_pnl']:+.2f} | Trades: {best_result['trade_count']} | MaxDD: {best_result['max_drawdown']*100:.2f}% | Config: P={best_result['pullback_ma_period']} RSI={best_result['rsi_oversold']} SL={best_result['stop_loss_pct']} TP={best_result['target_pct']} mHD={best_result['max_hold_days']}")
            print(f"  [Test]  PnL: {test_pnl:+.2f} | Trades: {test_trades} | MaxDD: {test_row.get('max_drawdown', 0)*100:.2f}%")
            
            if test_pnl >= 0 and test_trades >= 1:
                print("  --> Passed Out-Of-Sample! Added to portfolio.")
                portfolio.append({
                    "symbol": sym,
                    "optimal_params": {
                        "trend_ma_period": 100,
                        "pullback_ma_period": best_result["pullback_ma_period"],
                        "rsi_oversold": best_result["rsi_oversold"],
                        "stop_loss_pct": best_result["stop_loss_pct"],
                        "target_pct": best_result["target_pct"],
                        "max_hold_days": best_result["max_hold_days"],
                    },
                    "train_metrics": best_result,
                    "test_metrics": test_row
                })
            else:
                print("  --> Failed Out-Of-Sample! Discarded.")
        else:
            print("  --> No profitable & safe config found in Train set.")
            
    out_file = REPORTS_DIR / "optimal_ma_pullback_portfolio.json"
    with open(out_file, "w") as f:
        json.dump(portfolio, f, indent=2)
        
    print(f"\nDone! {len(portfolio)} verified Robust Symbols found. Saved to {out_file}")

if __name__ == "__main__":
    run_sweeper()
