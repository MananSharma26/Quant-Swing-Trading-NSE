import sys
import numpy as np
import pandas as pd
from datetime import datetime
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_paper_trader as pt_swan
import run_bb_squeeze_trader as pt_bb
import run_ma_pullback_trader as pt_ma
import run_supertrend_trader as pt_st

def run_simulation():
    # Bypass the "PAPER_TRADING_START" limit
    pt_swan.PAPER_TRADING_START = "2000-01-01"
    pt_bb.PAPER_TRADING_START = "2000-01-01"
    pt_ma.PAPER_TRADING_START = "2000-01-01"
    pt_st.PAPER_TRADING_START = "2000-01-01"
    
    # Force 12 years of data to get full 10 years of signals (after 1-2y warmup)
    import yfinance as yf
    def forced_fetch(symbol: str) -> pd.DataFrame | None:
        try:
            df = yf.download(f"{symbol}.NS", period="10y", interval="1d", progress=False, auto_adjust=True)
            if df.empty: return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
            df.index.name = "timestamp"
            df = df.reset_index()
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            return df.sort_values("timestamp").reset_index(drop=True).ffill().dropna(subset=["close"])
        except Exception:
            return None
            
    pt_swan.fetch = forced_fetch
    pt_bb.fetch_data = forced_fetch
    pt_ma.fetch_data = forced_fetch
    pt_st.fetch_data = forced_fetch

    all_trades = []
    
    print("Fetching Swan...")
    for pair in pt_swan.load_portfolio():
        r = pt_swan.replay_pair(pair["symbol_a"], pair["symbol_b"], pair["optimal_params"])
        for t in r.get("closed_trades", []):
            t["strategy"] = "Black Swan"
            t["symbol"] = r["pair"]
            all_trades.append(t)

    print("Fetching BB Squeeze...")
    for entry in pt_bb.load_portfolio():
        df = pt_bb.fetch_data(entry["symbol"])
        if df is not None and len(df) >= pt_bb.BB_WINDOW + 2:
            st = pt_bb.replay_symbol(df, entry["best_params"])
            for t in st.get("closed_trades", []):
                t["strategy"] = "BB Squeeze"
                t["symbol"] = entry["symbol"]
                all_trades.append(t)

    print("Fetching MA Pullback...")
    for entry in pt_ma.load_portfolio():
        df = pt_ma.fetch_data(entry["symbol"])
        if df is not None and len(df) >= int(entry["optimal_params"]["trend_ma_period"]) + 2:
            st = pt_ma.replay_symbol(df, entry["optimal_params"])
            for t in st.get("closed_trades", []):
                t["strategy"] = "MA Pullback"
                t["symbol"] = entry["symbol"]
                all_trades.append(t)

    print("Fetching Supertrend...")
    for entry in pt_st.load_portfolio():
        df = pt_st.fetch_data(entry["symbol"])
        if df is not None and len(df) >= int(entry["optimal_params"]["atr_period"]) + 2:
            st = pt_st.replay_symbol(df, entry["optimal_params"])
            for t in st.get("closed_trades", []):
                t["strategy"] = "Supertrend"
                t["symbol"] = entry["symbol"]
                all_trades.append(t)

    valid_trades = [t for t in all_trades if t.get("entry_date")]
    if not valid_trades:
        print("No trades found.")
        return

    dates = sorted(list(set([t["entry_date"] for t in valid_trades] + [t["exit_date"] for t in valid_trades])))
    
    TOTAL_ACCOUNT_CAPITAL = 2_00_000
    MIN_CHUNK_SIZE = 30_000
    free_cash = TOTAL_ACCOUNT_CAPITAL
    
    active_portfolio = []  
    daily_equity = []
    
    for current_date in dates:
        # Exit trades
        still_open = []
        for trade in active_portfolio:
            if trade["exit_date"] == current_date:
                pnl = (trade["exit_price"] - trade["entry_price"]) * trade["actual_qty"]
                free_cash += (trade["entry_price"] * trade["actual_qty"]) + pnl
            else:
                still_open.append(trade)
        active_portfolio = still_open

        # Enter trades
        signals_today = [t for t in valid_trades if t["entry_date"] == current_date]
        if signals_today:
            def strat_score(t):
                if t["strategy"] == "MA Pullback": return 4
                if t["strategy"] == "Supertrend": return 3
                if t["strategy"] == "BB Squeeze": return 2
                return 1
            signals_today.sort(key=strat_score, reverse=True)
            
            seen = set([t["symbol"] for t in active_portfolio])
            deduped = []
            for t in signals_today:
                if t["symbol"] not in seen:
                    seen.add(t["symbol"])
                    deduped.append(t)
            
            if free_cash >= MIN_CHUNK_SIZE and deduped:
                max_slots = int(free_cash // MIN_CHUNK_SIZE)
                selected = deduped[:max_slots]
                chunk_size = free_cash / len(selected)
                
                for t in selected:
                    actual_qty = max(1, int(chunk_size / t["entry_price"]))
                    t["actual_qty"] = actual_qty
                    locked = actual_qty * t["entry_price"]
                    free_cash -= locked
                    active_portfolio.append(t)
        
        locked_value = sum([t["entry_price"] * t["actual_qty"] for t in active_portfolio])
        total_equity = free_cash + locked_value
        daily_equity.append((current_date, total_equity))

    df_eq = pd.DataFrame(daily_equity, columns=["date", "equity"])
    df_eq["date"] = pd.to_datetime(df_eq["date"])
    
    # Resample to business days to fix Sharpe
    df_eq = df_eq.set_index("date").resample("B").ffill().reset_index()
    
    end_date = df_eq["date"].max()
    start_date = end_date - pd.DateOffset(years=10)
    df_eq_10y = df_eq[df_eq["date"] >= start_date].copy()
    if df_eq_10y.empty:
        df_eq_10y = df_eq

    initial_eq = df_eq_10y["equity"].iloc[0]
    final_eq = df_eq_10y["equity"].iloc[-1]
    
    years = float(df_eq_10y["date"].dt.year.nunique())
    cagr = (final_eq / initial_eq) ** (1 / years) - 1 if years > 0 else 0

    df_eq_10y["peak"] = df_eq_10y["equity"].cummax()
    df_eq_10y["drawdown"] = (df_eq_10y["equity"] - df_eq_10y["peak"]) / df_eq_10y["peak"]
    max_dd = df_eq_10y["drawdown"].min()
    
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("inf")
    
    df_eq_10y["year"] = df_eq_10y["date"].dt.year
    annual_eq = df_eq_10y.groupby("year")["equity"].last()
    annual_returns = annual_eq.pct_change()
    annual_returns.iloc[0] = (annual_eq.iloc[0] - initial_eq) / initial_eq
    
    mean_ret = annual_returns.mean()
    std_ret = annual_returns.std()
    sharpe = mean_ret / std_ret if std_ret > 0 else 0

    print("=== 10-YEAR BACKTEST RESULTS ===")
    print(f"Start Date   : {df_eq_10y['date'].iloc[0].strftime('%Y-%m-%d')}")
    print(f"End Date     : {df_eq_10y['date'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"Start Equity : {initial_eq:,.2f}")
    print(f"End Equity   : {final_eq:,.2f}")
    print(f"CAGR         : {cagr * 100:.2f}%")
    print(f"Max Drawdown : {max_dd * 100:.2f}%")
    print(f"Calmar Ratio : {calmar:.2f}")
    print(f"Sharpe Ratio : {sharpe:.2f}")

if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    run_simulation()
