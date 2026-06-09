"""RSI(2) Mean Reversion parameter sweep with Train/Test split and OOS validation.

Sweeps 10 Nifty large-caps over a grid of RSI(2) parameters.
Train  = all data before 2025-01-01
Test   = 2025 onwards (out-of-sample)

OOS pass criteria : test_pnl > 0 AND test_trades >= 2
Train gate        : train_trades >= 5 AND max_drawdown < 0.25
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
from trading_engine.strategies.rsi2_mean_reversion import RSI2Config, RSI2MeanReversionStrategy

REPORTS_DIR = ROOT / "reports"
INITIAL_CASH = Decimal("200000")   # 2 Lakhs (1 Lakh max per trade)
CAPITAL_PER_TRADE = 100_000

# Fixed (not swept)
TREND_MA_PERIOD = 200
RSI_PERIOD = 2

PARAM_GRID = {
    "rsi_entry_threshold": [5.0, 10.0, 15.0],
    "rsi_exit_threshold": [70.0, 80.0],
    "stop_loss_pct": [5.0, 8.0],
    "max_hold_days": [5, 10, 15],
}

SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "ITC", "LT", "BAJFINANCE", "BHARTIARTL", "SBIN",
]


# ---------------------------------------------------------------------------
# Grid builder
# ---------------------------------------------------------------------------

def build_grid() -> list[dict]:
    keys = list(PARAM_GRID.keys())
    return [dict(zip(keys, combo, strict=True)) for combo in product(*PARAM_GRID.values())]


# ---------------------------------------------------------------------------
# Data fetching & splitting
# ---------------------------------------------------------------------------

def fetch_yfinance_data(symbols: list[str]) -> dict[str, pd.DataFrame]:
    candles: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = yf.download(f"{sym}.NS", period="10y", interval="1d", progress=False)
            if df.empty:
                print(f"  [WARN] No data returned for {sym}")
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
    test: dict[str, pd.DataFrame] = {}
    split_ts = pd.Timestamp(split_date).tz_localize("Asia/Kolkata")
    for sym, df in candles.items():
        train[sym] = df[df["timestamp"] < split_ts].copy()
        test[sym] = df[df["timestamp"] >= split_ts].copy()
    return train, test


# ---------------------------------------------------------------------------
# Single backtest run — FRESH instances every call (no state leakage)
# ---------------------------------------------------------------------------

def run_single(
    candles: dict[str, pd.DataFrame], params: dict, symbol: str
) -> dict:
    try:
        cfg = RSI2Config(
            strategy_id="rsi2_mean_reversion",
            symbol=symbol,
            capital_per_trade=CAPITAL_PER_TRADE,
            trend_ma_period=TREND_MA_PERIOD,
            rsi_period=RSI_PERIOD,
            rsi_entry_threshold=float(params["rsi_entry_threshold"]),
            rsi_exit_threshold=float(params["rsi_exit_threshold"]),
            stop_loss_pct=float(params["stop_loss_pct"]),
            max_hold_days=int(params["max_hold_days"]),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    # Fresh instances — critical to avoid state leakage between sweep runs
    strategy = RSI2MeanReversionStrategy(config=cfg)
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

    row: dict = {**params}
    row["error"] = None
    row["realized_pnl"] = float(m.realized_pnl) if m.realized_pnl else 0.0
    row["win_rate"] = float(m.win_rate) if m.win_rate else 0.0
    row["trade_count"] = m.trade_count
    row["max_drawdown"] = float(m.max_drawdown) if m.max_drawdown else 0.0
    return row


# ---------------------------------------------------------------------------
# Main sweeper
# ---------------------------------------------------------------------------

def run_sweeper() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("Fetching 10 years of daily history from Yahoo Finance...")
    all_candles = fetch_yfinance_data(SYMBOLS)

    # Split: Train = before 2025-01-01 | Test = 2025 onwards
    train_candles, test_candles = split_train_test(all_candles, "2025-01-01")

    combos = build_grid()
    print(f"\nParameter grid: {len(combos)} combinations")
    print(f"Sweeping {len(SYMBOLS)} symbols (Train/Test split at 2025-01-01)...\n")

    portfolio_results: list[dict] = []

    for rank, sym in enumerate(SYMBOLS, 1):
        if sym not in all_candles:
            print(f"[{rank}/{len(SYMBOLS)}] {sym} — skipped (no data)")
            continue

        train_df = train_candles.get(sym)
        test_df = test_candles.get(sym)
        if train_df is None or train_df.empty:
            print(f"[{rank}/{len(SYMBOLS)}] {sym} — skipped (empty train set)")
            continue

        print(f"[{rank}/{len(SYMBOLS)}] {sym}")

        # ------------------------------------------------------------------
        # 1. In-sample: find best config
        # ------------------------------------------------------------------
        best_pnl = 0.0
        best_result: dict | None = None

        for params in combos:
            row = run_single(train_candles, params, sym)
            if row.get("error"):
                continue

            pnl = row.get("realized_pnl", 0.0)
            trades = row.get("trade_count", 0)
            drawdown = row.get("max_drawdown", 1.0)

            # Train gate: >= 5 trades AND max drawdown < 25%
            if pnl > best_pnl and trades >= 5 and drawdown < 0.25:
                best_pnl = pnl
                best_result = row

        if best_result is None:
            print("  --> No qualifying config found in train set. Skipped.\n")
            continue

        # ------------------------------------------------------------------
        # 2. Out-of-sample validation
        # ------------------------------------------------------------------
        if test_df is None or test_df.empty:
            print("  --> Empty test set. Skipped.\n")
            continue

        test_row = run_single(test_candles, best_result, sym)
        test_pnl = test_row.get("realized_pnl", 0.0)
        test_trades = test_row.get("trade_count", 0)

        train_label = (
            f"PnL: {best_result['realized_pnl']:+.2f} | "
            f"Trades: {best_result['trade_count']} | "
            f"MaxDD: {best_result['max_drawdown'] * 100:.2f}% | "
            f"Entry<{best_result['rsi_entry_threshold']} "
            f"Exit>{best_result['rsi_exit_threshold']} "
            f"SL={best_result['stop_loss_pct']}% "
            f"mHD={best_result['max_hold_days']}"
        )
        test_label = (
            f"PnL: {test_pnl:+.2f} | "
            f"Trades: {test_trades} | "
            f"MaxDD: {test_row.get('max_drawdown', 0.0) * 100:.2f}%"
        )
        print(f"  [Train] {train_label}")
        print(f"  [Test]  {test_label}")

        # OOS pass criteria: test_pnl > 0 AND test_trades >= 2
        if test_pnl > 0 and test_trades >= 2:
            print("  --> Passed Out-Of-Sample! Added to portfolio.\n")
            portfolio_results.append({
                "symbol": sym,
                "optimal_params": {
                    "trend_ma_period": TREND_MA_PERIOD,
                    "rsi_period": RSI_PERIOD,
                    "rsi_entry_threshold": best_result["rsi_entry_threshold"],
                    "rsi_exit_threshold": best_result["rsi_exit_threshold"],
                    "stop_loss_pct": best_result["stop_loss_pct"],
                    "max_hold_days": best_result["max_hold_days"],
                    "capital_per_trade": CAPITAL_PER_TRADE,
                },
                "train_metrics": best_result,
                "test_metrics": test_row,
            })
        else:
            print("  --> Failed Out-Of-Sample. Discarded.\n")

    # ------------------------------------------------------------------
    # Save results
    # ------------------------------------------------------------------
    out_file = REPORTS_DIR / "optimal_rsi2_portfolio.json"
    with open(out_file, "w") as fh:
        json.dump(portfolio_results, fh, indent=2)

    print(
        f"\nDone! {len(portfolio_results)} OOS-verified symbol(s) found. "
        f"Saved to {out_file}"
    )


if __name__ == "__main__":
    run_sweeper()
