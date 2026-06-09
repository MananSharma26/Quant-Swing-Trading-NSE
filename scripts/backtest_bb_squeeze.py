"""
Bollinger Band Squeeze Breakout Swing Strategy Backtester
Signal on close of day N, fill at open of day N+1 (no look-ahead bias).
Long-only, 1 position per symbol at a time, realized PnL only.

Squeeze = band_width / middle < squeeze_threshold
Entry: squeeze condition true AND today's close breaks ABOVE upper band
Exit: price crosses below middle band (20-day SMA), OR stop loss, OR max hold days
"""

import sys
import json
import itertools
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import numpy as np
import yfinance as yf

REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "KOTAKBANK",
    "HINDUNILVR", "AXISBANK", "SBIN", "BAJFINANCE", "BHARTIARTL", "WIPRO",
    "LT", "MARUTI", "TITAN", "ASIANPAINT", "NESTLEIND", "ULTRACEMCO",
    "BAJAJFINSV", "SUNPHARMA", "TECHM", "POWERGRID", "NTPC", "COALINDIA",
    "ONGC", "BPCL", "ITC", "DRREDDY", "DIVISLAB", "HCLTECH", "TATASTEEL",
    "JSWSTEEL", "HINDALCO", "ADANIENT", "ADANIPORTS", "APOLLOHOSP",
    "GRASIM", "TATACONSUM", "BRITANNIA", "EICHERMOT"
]

PARAM_GRID = {
    "squeeze_threshold": [0.05, 0.08, 0.10],
    "stop_loss_pct": [3.0, 5.0, 8.0],
    "max_hold_days": [10, 20, 30],
}

TRAIN_END = "2025-01-01"
CAPITAL_PER_TRADE = 100_000
BB_WINDOW = 20
BB_STD_MULT = 2.0


def fetch_data(symbol: str) -> pd.DataFrame | None:
    ticker = f"{symbol}.NS"
    try:
        df = yf.download(ticker, period="10y", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index.name = "timestamp"
        df = df.reset_index()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        return df
    except Exception as e:
        print(f"  [ERROR] fetch {symbol}: {e}")
        return None


def backtest_bb_squeeze(df: pd.DataFrame, params: dict, capital_per_trade: int = 100_000) -> dict:
    """
    Backtest Bollinger Band Squeeze Breakout strategy on a single symbol.
    Returns dict with realized_pnl, win_rate, trade_count, max_drawdown, trades list.
    """
    squeeze_threshold = params["squeeze_threshold"]
    stop_loss_pct = params["stop_loss_pct"]
    max_hold_days = params["max_hold_days"]

    closes = df["close"].values
    opens = df["open"].values
    lows = df["low"].values
    dates = df["timestamp"].values
    n = len(df)

    WARMUP = BB_WINDOW  # need 20 bars for BB

    trades = []
    equity_curve = []

    in_position = False
    qty = 0
    entry_price = 0.0
    entry_date = None
    entry_idx = -1
    stop_price = 0.0

    cash = 0.0  # realized cash

    # Track if squeeze was active on previous bar
    squeeze_active_prev = False

    for i in range(WARMUP, n):
        # Compute Bollinger Bands using data up to and including bar i
        window_closes = closes[i - BB_WINDOW + 1: i + 1]  # 20-bar window ending today
        middle = np.mean(window_closes)
        std = np.std(window_closes, ddof=1)
        upper = middle + BB_STD_MULT * std
        lower = middle - BB_STD_MULT * std
        band_width = (upper - lower) / middle if middle != 0 else 999.0

        close_i = closes[i]
        date_i = dates[i]

        squeeze_now = band_width < squeeze_threshold

        if in_position:
            # Check exit: price crosses below middle band
            days_held = (pd.Timestamp(date_i) - pd.Timestamp(entry_date)).days
            exit_signal = False
            exit_reason = ""

            if close_i < middle:
                exit_signal = True
                exit_reason = "below_middle_bb"

            if days_held >= max_hold_days:
                exit_signal = True
                exit_reason = "max_hold"

            if exit_signal:
                if i + 1 < n:
                    exit_price = opens[i + 1]
                    exit_date = dates[i + 1]
                else:
                    exit_price = close_i
                    exit_date = date_i

                pnl = (exit_price - entry_price) * qty
                trades.append({
                    "entry_date": str(pd.Timestamp(entry_date).date()),
                    "exit_date": str(pd.Timestamp(exit_date).date()),
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "qty": qty,
                    "pnl": round(pnl, 2),
                    "exit_reason": exit_reason,
                })
                cash += pnl
                in_position = False
                qty = 0
                entry_price = 0.0
                entry_date = None
                entry_idx = -1

        # Check stop loss (intra-bar)
        if in_position:
            if lows[i] <= stop_price:
                exit_price = stop_price
                exit_date = date_i
                pnl = (exit_price - entry_price) * qty
                trades.append({
                    "entry_date": str(pd.Timestamp(entry_date).date()),
                    "exit_date": str(pd.Timestamp(exit_date).date()),
                    "entry_price": round(entry_price, 2),
                    "exit_price": round(exit_price, 2),
                    "qty": qty,
                    "pnl": round(pnl, 2),
                    "exit_reason": "stop_loss",
                })
                cash += pnl
                in_position = False
                qty = 0
                entry_price = 0.0
                entry_date = None
                entry_idx = -1

        # Entry signal:
        # Squeeze was active on PREVIOUS bar AND today's close breaks above upper band
        if not in_position:
            # Re-compute previous bar's band width for squeeze check
            if i >= WARMUP:
                prev_window = closes[i - BB_WINDOW: i]  # 20 bars ending at i-1
                prev_middle = np.mean(prev_window)
                prev_std = np.std(prev_window, ddof=1)
                prev_upper = prev_middle + BB_STD_MULT * prev_std
                prev_lower = prev_middle - BB_STD_MULT * prev_std
                prev_band_width = (prev_upper - prev_lower) / prev_middle if prev_middle != 0 else 999.0
                prev_squeeze = prev_band_width < squeeze_threshold
            else:
                prev_squeeze = False

            # Signal: previous bar had squeeze AND today broke above upper band
            if prev_squeeze and close_i > upper:
                if i + 1 < n:
                    fill_price = opens[i + 1]
                    fill_date = dates[i + 1]
                    fill_idx = i + 1
                else:
                    squeeze_active_prev = squeeze_now
                    equity_curve.append(cash)
                    continue

                qty = max(1, int(capital_per_trade / fill_price))
                entry_price = fill_price
                entry_date = fill_date
                entry_idx = fill_idx
                stop_price = entry_price * (1 - stop_loss_pct / 100.0)
                in_position = True

        squeeze_active_prev = squeeze_now

        # Track equity for drawdown
        unrealized = (close_i - entry_price) * qty if in_position else 0.0
        equity_curve.append(cash + unrealized)

    # Compute metrics
    realized_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0

    if equity_curve:
        eq = np.array(equity_curve)
        running_max = np.maximum.accumulate(eq)
        drawdowns = eq - running_max
        max_drawdown = float(np.min(drawdowns))
    else:
        max_drawdown = 0.0

    return {
        "realized_pnl": round(realized_pnl, 2),
        "win_rate": round(win_rate, 4),
        "trade_count": len(trades),
        "max_drawdown": round(max_drawdown, 2),
        "trades": trades,
    }


def run_sweep_for_symbol(symbol: str, df: pd.DataFrame):
    """Run full param sweep for one symbol, return best config for train and test result."""
    df_train = df[df["timestamp"] < TRAIN_END].reset_index(drop=True)
    df_test = df[df["timestamp"] >= TRAIN_END].reset_index(drop=True)

    param_keys = list(PARAM_GRID.keys())
    param_values = list(PARAM_GRID.values())
    combos = list(itertools.product(*param_values))

    best_train_pnl = -np.inf
    best_params = None
    best_train_result = None

    for combo in combos:
        params = dict(zip(param_keys, combo))
        result = backtest_bb_squeeze(df_train, params, CAPITAL_PER_TRADE)
        if result["trade_count"] < 8:
            continue
        if result["realized_pnl"] > best_train_pnl:
            best_train_pnl = result["realized_pnl"]
            best_params = params
            best_train_result = result

    if best_params is None:
        return None

    test_result = backtest_bb_squeeze(df_test, best_params, CAPITAL_PER_TRADE)

    return {
        "symbol": symbol,
        "best_params": best_params,
        "train": best_train_result,
        "test": test_result,
    }


def pass_oos(train_result, test_result) -> bool:
    return test_result["realized_pnl"] > 0 and test_result["trade_count"] >= 2


def main():
    print("=" * 80)
    print("BOLLINGER BAND SQUEEZE BREAKOUT SWING STRATEGY — BACKTEST")
    print(f"Train: before {TRAIN_END} | Test: {TRAIN_END} onwards")
    print(f"Capital per trade: ₹{CAPITAL_PER_TRADE:,}")
    print("=" * 80)

    all_results = []

    for symbol in NIFTY50:
        print(f"\n[{symbol}] Fetching data...")
        df = fetch_data(symbol)
        if df is None or len(df) < 50:
            print(f"  Skipping {symbol} — insufficient data")
            continue

        print(f"  {len(df)} bars loaded. Running param sweep...")
        result = run_sweep_for_symbol(symbol, df)
        if result is None:
            print(f"  No valid config (< 8 trades in train period)")
            continue

        all_results.append(result)

        p = result["best_params"]
        tr = result["train"]
        te = result["test"]
        oos = pass_oos(tr, te)
        oos_mark = "PASS" if oos else "FAIL"

        print(
            f"  Best: sq={p['squeeze_threshold']} sl={p['stop_loss_pct']}% hold={p['max_hold_days']}d | "
            f"Train: {tr['trade_count']} trades, {tr['win_rate']*100:.0f}% WR, PnL=₹{tr['realized_pnl']:,.0f} | "
            f"Test: {te['trade_count']} trades, {te['win_rate']*100:.0f}% WR, PnL=₹{te['realized_pnl']:,.0f} | "
            f"OOS: {oos_mark}"
        )

    # Summary table
    print("\n\n" + "=" * 120)
    print("BOLLINGER BAND SQUEEZE BREAKOUT — RESULTS SUMMARY")
    print("=" * 120)
    header = (
        f"{'Symbol':<14} {'Best Params':<30} {'Train Trades':>12} {'Train WR':>10} "
        f"{'Train PnL':>12} {'Test Trades':>11} {'Test WR':>9} {'Test PnL':>12} {'Pass OOS?':>10}"
    )
    print(header)
    print("-" * 120)

    oos_pass_count = 0
    for r in all_results:
        p = r["best_params"]
        tr = r["train"]
        te = r["test"]
        oos = pass_oos(tr, te)
        if oos:
            oos_pass_count += 1
        oos_str = "YES" if oos else "NO"
        param_str = f"sq={p['squeeze_threshold']} sl={p['stop_loss_pct']}% hold={p['max_hold_days']}d"
        print(
            f"{r['symbol']:<14} {param_str:<30} {tr['trade_count']:>12} {tr['win_rate']*100:>9.0f}% "
            f"{tr['realized_pnl']:>+12,.0f} {te['trade_count']:>11} {te['win_rate']*100:>8.0f}% "
            f"{te['realized_pnl']:>+12,.0f} {oos_str:>10}"
        )

    print("-" * 120)
    total_valid = len(all_results)
    print(f"\nSymbols with valid train config: {total_valid}")
    print(f"Symbols passing OOS test: {oos_pass_count} / {total_valid}")

    # Save results
    out_path = REPORTS_DIR / "bb_squeeze_results.json"
    summary_results = []
    for r in all_results:
        summary_results.append({
            "symbol": r["symbol"],
            "best_params": r["best_params"],
            "train": {k: v for k, v in r["train"].items() if k != "trades"},
            "test": {k: v for k, v in r["test"].items() if k != "trades"},
            "pass_oos": bool(pass_oos(r["train"], r["test"])),
        })

    with open(out_path, "w") as f:
        json.dump({
            "strategy": "bb_squeeze_breakout",
            "train_end": TRAIN_END,
            "capital_per_trade": CAPITAL_PER_TRADE,
            "run_date": datetime.now().isoformat(),
            "results": summary_results,
        }, f, indent=2)

    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
