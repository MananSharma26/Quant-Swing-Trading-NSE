"""Supertrend parameter sweep with Train/Test split and OOS validation.

Train  : data before 2025-01-01
Test   : data from  2025-01-01 onwards

Selection criteria:
  Train pass : train_trades >= 5  AND  max_drawdown < 0.25
  OOS pass   : test_pnl > 0  AND  test_trades >= 2

Output: reports/optimal_supertrend_portfolio.json
"""

from __future__ import annotations

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
from trading_engine.strategies.supertrend import SupertrendConfig, SupertrendStrategy

REPORTS_DIR = ROOT / "reports"
INITIAL_CASH = Decimal("200000")
CAPITAL_PER_TRADE = 100_000

PARAM_GRID = {
    "atr_period":    [7, 10, 14],
    "multiplier":    [2.0, 3.0, 4.0],
    "stop_loss_pct": [5.0, 8.0, 12.0],
    "max_hold_days": [30, 60, 90],
}

SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "ITC", "LT", "BAJFINANCE", "BHARTIARTL", "SBIN",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_grid() -> list[dict]:
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, combo, strict=True)) for combo in product(*PARAM_GRID.values())]


def fetch_yfinance_data(symbols: list[str]) -> dict[str, pd.DataFrame]:
    candles: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = yf.download(f"{sym}.NS", period="10y", interval="1d", progress=False)
            if df.empty:
                print(f"  [WARN] Empty data for {sym}.NS — skipping.")
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df.index.name = "timestamp"
            df = df.ffill().dropna().reset_index()

            if df["timestamp"].dt.tz is None:
                df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")
            else:
                df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")

            candles[sym] = df
        except Exception as exc:
            print(f"  [ERROR] Failed to fetch {sym}: {exc}")
    return candles


def split_train_test(
    candles: dict[str, pd.DataFrame], split_date: str
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    train: dict[str, pd.DataFrame] = {}
    test:  dict[str, pd.DataFrame] = {}
    split_ts = pd.Timestamp(split_date).tz_localize("Asia/Kolkata")
    for sym, df in candles.items():
        train[sym] = df[df["timestamp"] < split_ts].copy()
        test[sym]  = df[df["timestamp"] >= split_ts].copy()
    return train, test


def run_single(candles: dict[str, pd.DataFrame], params: dict, symbol: str) -> dict:
    """Run a single backtest and return a metrics dict. Fresh instances every call."""
    try:
        cfg = SupertrendConfig(
            strategy_id="supertrend",
            symbol=symbol,
            capital_per_trade=CAPITAL_PER_TRADE,
            atr_period=int(params["atr_period"]),
            multiplier=float(params["multiplier"]),
            stop_loss_pct=float(params["stop_loss_pct"]),
            max_hold_days=int(params["max_hold_days"]),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    # Fresh instances — no state leakage between calls
    strategy  = SupertrendStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=INITIAL_CASH)
    cost_model     = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed   = HistoricalDataFeed({symbol: candles[symbol]}, interval="day")

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

    row = dict(params)
    row["error"]        = None
    row["realized_pnl"] = float(m.realized_pnl) if m.realized_pnl else 0.0
    row["win_rate"]     = float(m.win_rate)      if m.win_rate     else 0.0
    row["trade_count"]  = m.trade_count
    row["max_drawdown"] = float(m.max_drawdown)  if m.max_drawdown else 0.0
    return row


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------

def run_sweeper() -> None:
    print("Fetching 10 years of daily history from Yahoo Finance...")
    all_candles = fetch_yfinance_data(SYMBOLS)

    train_candles, test_candles = split_train_test(all_candles, "2025-01-01")

    combos = build_grid()
    portfolio_results: list[dict] = []

    print(f"\nSweeping {len(SYMBOLS)} symbols × {len(combos)} parameter combos "
          f"(Train < 2025-01-01 / Test >= 2025-01-01)...\n")

    for rank, sym in enumerate(SYMBOLS, 1):
        if sym not in all_candles:
            print(f"[{rank}/{len(SYMBOLS)}] {sym} — no data, skipped.")
            continue

        print(f"[{rank}/{len(SYMBOLS)}] {sym}")

        # ---- 1. In-sample grid search ----------------------------------
        best_pnl    = 0.0
        best_result: dict | None = None

        for params in combos:
            row = run_single(train_candles, params, sym)
            if row.get("error"):
                continue

            pnl      = row.get("realized_pnl", 0.0)
            trades   = row.get("trade_count", 0)
            drawdown = row.get("max_drawdown", 1.0)

            # Train pass criteria: >= 5 trades AND max_drawdown < 25 %
            if pnl > best_pnl and trades >= 5 and drawdown < 0.25:
                best_pnl    = pnl
                best_result = row

        if best_result is None:
            print("  --> No config met train criteria (trades>=5, dd<25%). Skipped.\n")
            continue

        # ---- 2. Out-of-sample validation --------------------------------
        test_row   = run_single(test_candles, best_result, sym)
        test_pnl   = test_row.get("realized_pnl", 0.0)
        test_trades = test_row.get("trade_count", 0)

        print(
            f"  [Train] PnL: {best_result['realized_pnl']:+.2f} | "
            f"Trades: {best_result['trade_count']} | "
            f"MaxDD: {best_result['max_drawdown']*100:.2f}% | "
            f"atr={best_result['atr_period']} "
            f"mult={best_result['multiplier']} "
            f"sl={best_result['stop_loss_pct']} "
            f"mhd={best_result['max_hold_days']}"
        )
        print(
            f"  [Test]  PnL: {test_pnl:+.2f} | "
            f"Trades: {test_trades} | "
            f"MaxDD: {test_row.get('max_drawdown', 0.0)*100:.2f}%"
        )

        # OOS pass: test_pnl > 0 AND test_trades >= 2
        if test_pnl > 0 and test_trades >= 2:
            print("  --> Passed Out-Of-Sample! Added to portfolio.\n")
            portfolio_results.append({
                "symbol": sym,
                "optimal_params": {
                    "atr_period":       best_result["atr_period"],
                    "multiplier":       best_result["multiplier"],
                    "stop_loss_pct":    best_result["stop_loss_pct"],
                    "max_hold_days":    best_result["max_hold_days"],
                    "capital_per_trade": CAPITAL_PER_TRADE,
                },
                "train_metrics": best_result,
                "test_metrics":  test_row,
            })
        else:
            print("  --> Failed Out-Of-Sample. Discarded.\n")

    # ---- 3. Save results ------------------------------------------------
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = REPORTS_DIR / "optimal_supertrend_portfolio.json"
    with open(out_file, "w") as fh:
        json.dump(portfolio_results, fh, indent=2)

    print(
        f"\nDone! {len(portfolio_results)} symbol(s) passed OOS validation. "
        f"Results saved to {out_file}"
    )


if __name__ == "__main__":
    run_sweeper()
