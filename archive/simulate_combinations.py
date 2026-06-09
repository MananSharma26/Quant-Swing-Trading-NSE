"""Portfolio Combination Simulator — 10-Year Backtest.

Tests 4 strategy combinations over 10 years using a ₹2L account with
the Master Risk Engine's priority-based allocation logic.

Strategies available: MA Pullback, BB Squeeze, Supertrend, Black Swan

Combinations tested:
  1. BB + MA + Swan  (currently deployed)
  2. ST + MA + Swan
  3. BB + ST + Swan
  4. All 4
"""

from __future__ import annotations

import copy
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"

TOTAL_CAPITAL = 2_00_000
MIN_CHUNK = 50_000
CAPITAL_PER_TRADE = 1_00_000  # used in replay functions to compute per-signal qty


# ============================================================
# Data fetching
# ============================================================

_data_cache: dict[str, pd.DataFrame | None] = {}


def fetch(symbol: str) -> pd.DataFrame | None:
    if symbol in _data_cache:
        return _data_cache[symbol]
    try:
        df = yf.download(
            f"{symbol}.NS", period="10y", interval="1d",
            progress=False, auto_adjust=True,
        )
        if df.empty:
            _data_cache[symbol] = None
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index.name = "timestamp"
        df = df.reset_index()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True).ffill().dropna(subset=["close"])
        _data_cache[symbol] = df
        return df
    except Exception as exc:
        print(f"  WARNING: Failed to fetch {symbol}: {exc}")
        _data_cache[symbol] = None
        return None


# ============================================================
# Replay — MA Pullback
# ============================================================

def replay_ma_pullback(df: pd.DataFrame, params: dict) -> list[dict]:
    trend_period = int(params["trend_ma_period"])
    pullback_period = int(params["pullback_ma_period"])
    rsi_period = int(params.get("rsi_period", 14))
    rsi_oversold = float(params["rsi_oversold"])
    sl_pct = float(params["stop_loss_pct"])
    tp_pct = float(params["target_pct"])
    max_hold = int(params["max_hold_days"])

    closes = df["close"].values.astype(float)
    opens = df["open"].values.astype(float)
    dates = df["timestamp"].values
    n = len(closes)

    avg_gain = avg_loss = 0.0
    close_history: list[float] = []
    in_pos = False
    qty = 0
    entry_price = stop_price = target_price = 0.0
    entry_date: pd.Timestamp | None = None
    trades: list[dict] = []

    for i in range(n):
        c = closes[i]
        d = pd.Timestamp(dates[i])

        # Wilder's RSI update
        if close_history:
            chg = c - close_history[-1]
            g, l = max(0.0, chg), max(0.0, -chg)
            h = len(close_history)
            if h < rsi_period:
                avg_gain += g; avg_loss += l
            elif h == rsi_period:
                avg_gain = (avg_gain + g) / rsi_period
                avg_loss = (avg_loss + l) / rsi_period
            else:
                avg_gain = (avg_gain * (rsi_period - 1) + g) / rsi_period
                avg_loss = (avg_loss * (rsi_period - 1) + l) / rsi_period

        close_history.append(c)
        if len(close_history) > trend_period + 1:
            close_history.pop(0)
        if len(close_history) < trend_period:
            continue

        trend_sma = sum(close_history[-trend_period:]) / trend_period
        pull_sma = sum(close_history[-pullback_period:]) / pullback_period
        rsi = 100.0 if avg_loss == 0 and avg_gain > 0 else (
            50.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        )

        if in_pos:
            days_held = (d - entry_date).days
            triggered = None
            if c <= stop_price:
                triggered = "stop_loss"
            elif c >= target_price:
                triggered = "target_hit"
            elif days_held >= max_hold:
                triggered = "time_exit"

            if triggered:
                fill = float(opens[i + 1]) if i + 1 < n else c
                fill_d = pd.Timestamp(dates[i + 1]) if i + 1 < n else d
                trades.append({
                    "entry_date": str(entry_date.date()),
                    "exit_date": str(fill_d.date()),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(fill, 4),
                    "qty": qty,
                    "pnl": round((fill - entry_price) * qty, 2),
                    "reason": triggered,
                })
                in_pos = False
                continue

        if not in_pos and c > trend_sma and c <= pull_sma and rsi <= rsi_oversold:
            fill = float(opens[i + 1]) if i + 1 < n else c
            fill_d = pd.Timestamp(dates[i + 1]) if i + 1 < n else d
            qty = max(1, int(CAPITAL_PER_TRADE / fill))
            entry_price = fill
            entry_date = fill_d
            stop_price = entry_price * (1 - sl_pct / 100.0)
            target_price = entry_price * (1 + tp_pct / 100.0)
            in_pos = True

    return trades


# ============================================================
# Replay — Supertrend
# ============================================================

def replay_supertrend(df: pd.DataFrame, params: dict) -> list[dict]:
    atr_period = int(params["atr_period"])
    multiplier = float(params["multiplier"])
    sl_pct = float(params["stop_loss_pct"])
    max_hold = int(params["max_hold_days"])

    closes = df["close"].values.astype(float)
    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    opens = df["open"].values.astype(float)
    dates = df["timestamp"].values
    n = len(closes)

    tr_buf: list[float] = []
    atr = 0.0
    atr_seeded = False
    prev_fu = float("inf")
    prev_fl = 0.0
    bands_ready = False
    trend = 0

    in_pos = False
    qty = 0
    entry_price = stop_price = 0.0
    entry_date: pd.Timestamp | None = None
    trades: list[dict] = []

    for i in range(n):
        hi = highs[i]; lo = lows[i]; c = closes[i]
        d = pd.Timestamp(dates[i])

        # ATR
        tr = (hi - lo) if i == 0 else max(hi - lo, abs(hi - closes[i-1]), abs(lo - closes[i-1]))
        if not atr_seeded:
            tr_buf.append(tr)
            if len(tr_buf) == atr_period:
                atr = sum(tr_buf) / atr_period
                atr_seeded = True
        else:
            atr = (atr * (atr_period - 1) + tr) / atr_period

        if not atr_seeded or i == 0:
            continue

        hl2 = (hi + lo) / 2.0
        bu = hl2 + multiplier * atr
        bl = hl2 - multiplier * atr

        prev_c = closes[i - 1]
        if not bands_ready:
            fu, fl = bu, bl
            bands_ready = True
        else:
            fu = bu if (bu < prev_fu or prev_c > prev_fu) else prev_fu
            fl = bl if (bl > prev_fl or prev_c < prev_fl) else prev_fl

        new_trend = 1 if c > fu else (-1 if c < fl else trend)

        if in_pos:
            days_held = (d - entry_date).days
            sp = entry_price * (1 - sl_pct / 100.0)
            triggered = None
            if new_trend == -1 and trend == 1:
                triggered = "bearish_flip"
            elif c <= sp:
                triggered = "stop_loss"
            elif days_held >= max_hold:
                triggered = "time_exit"

            if triggered:
                fill = float(opens[i + 1]) if i + 1 < n else c
                fill_d = pd.Timestamp(dates[i + 1]) if i + 1 < n else d
                trades.append({
                    "entry_date": str(entry_date.date()),
                    "exit_date": str(fill_d.date()),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(fill, 4),
                    "qty": qty,
                    "pnl": round((fill - entry_price) * qty, 2),
                    "reason": triggered,
                })
                in_pos = False

        if not in_pos and new_trend == 1 and trend != 1:
            fill = float(opens[i + 1]) if i + 1 < n else c
            fill_d = pd.Timestamp(dates[i + 1]) if i + 1 < n else d
            qty = max(1, int(CAPITAL_PER_TRADE / fill))
            entry_price = fill
            entry_date = fill_d
            stop_price = entry_price * (1 - sl_pct / 100.0)
            in_pos = True

        prev_fu = fu
        prev_fl = fl
        trend = new_trend

    return trades


# ============================================================
# Replay — BB Squeeze
# ============================================================

def replay_bb_squeeze(df: pd.DataFrame, params: dict) -> list[dict]:
    sq_thresh = float(params["squeeze_threshold"])
    sl_pct = float(params["stop_loss_pct"])
    max_hold = int(params["max_hold_days"])
    BB_WIN = 20
    BB_STD = 2.0

    closes = df["close"].values.astype(float)
    opens = df["open"].values.astype(float)
    lows = df["low"].values.astype(float)
    dates = df["timestamp"].values
    n = len(closes)

    in_pos = False
    qty = 0
    entry_price = stop_price = 0.0
    entry_date: pd.Timestamp | None = None
    trades: list[dict] = []

    for i in range(BB_WIN, n):
        wc = closes[i - BB_WIN + 1: i + 1]
        mid = float(np.mean(wc))
        std = float(np.std(wc, ddof=1))
        upper = mid + BB_STD * std
        c = float(closes[i])
        lo = float(lows[i])
        d = pd.Timestamp(dates[i])

        if in_pos:
            days_held = (d - entry_date).days
            triggered = None
            if lo <= stop_price:
                triggered = ("stop_loss", stop_price, d)
            elif c < mid:
                triggered = ("below_midband", None, None)
            elif days_held >= max_hold:
                triggered = ("time_exit", None, None)

            if triggered:
                reason, forced_price, forced_date = triggered
                if forced_price is not None:
                    fill, fill_d = forced_price, forced_date
                else:
                    fill = float(opens[i + 1]) if i + 1 < n else c
                    fill_d = pd.Timestamp(dates[i + 1]) if i + 1 < n else d
                trades.append({
                    "entry_date": str(entry_date.date()),
                    "exit_date": str(fill_d.date()),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(fill, 4),
                    "qty": qty,
                    "pnl": round((fill - entry_price) * qty, 2),
                    "reason": reason,
                })
                in_pos = False
                continue

        if not in_pos:
            pw = closes[i - BB_WIN: i]
            pm = float(np.mean(pw))
            ps = float(np.std(pw, ddof=1))
            pu = pm + BB_STD * ps
            pl = pm - BB_STD * ps
            bw = (pu - pl) / pm if pm > 0 else 999.0
            if bw < sq_thresh and c > upper:
                fill = float(opens[i + 1]) if i + 1 < n else c
                fill_d = pd.Timestamp(dates[i + 1]) if i + 1 < n else d
                qty = max(1, int(CAPITAL_PER_TRADE / fill))
                entry_price = fill
                entry_date = fill_d
                stop_price = entry_price * (1 - sl_pct / 100.0)
                in_pos = True

    return trades


# ============================================================
# Replay — Black Swan (Long-Only Pairs)
# ============================================================

def replay_black_swan(sym_a: str, sym_b: str, params: dict) -> list[dict]:
    df_a = fetch(sym_a)
    df_b = fetch(sym_b)
    if df_a is None or df_b is None:
        return []

    window = int(params["window_size"])
    entry_z = float(params["entry_z_score"])
    exit_z = float(params.get("exit_z_score", 0.0))
    stop_z = float(params["stop_loss_z_score"])
    max_hold = int(params.get("max_hold_days", 30))

    fa = df_a.set_index("timestamp")
    fb = df_b.set_index("timestamp")
    common = fa.index.intersection(fb.index)
    fa = fa.loc[common].reset_index()
    fb = fb.loc[common].reset_index()

    if len(common) < window + 2:
        return []

    ratio_hist: list[float] = []
    position: str | None = None
    entry_price = 0.0
    entry_qty = 0
    entry_ts: pd.Timestamp | None = None
    trades: list[dict] = []

    for i in range(len(fa)):
        pa = float(fa["close"].iloc[i])
        pb = float(fb["close"].iloc[i])
        ts = pd.Timestamp(fa["timestamp"].iloc[i])

        if pb == 0:
            continue

        ratio_hist.append(pa / pb)
        if len(ratio_hist) > window:
            ratio_hist.pop(0)
        if len(ratio_hist) < window:
            continue

        mean_r = statistics.mean(ratio_hist)
        std_r = statistics.stdev(ratio_hist)
        if std_r == 0:
            continue
        z = (ratio_hist[-1] - mean_r) / std_r
        days_held = (ts - entry_ts).days if entry_ts else 0

        if position == "LONG_A":
            triggered = None
            if z <= -stop_z:
                triggered = ("stop_loss", pa)
            elif z >= -exit_z:
                triggered = ("mean_reverted", pa)
            elif days_held >= max_hold:
                triggered = ("max_hold", pa)
            if triggered:
                reason, close_p = triggered
                pnl = (close_p - entry_price) * entry_qty
                trades.append({
                    "entry_date": str(entry_ts.date()),
                    "exit_date": str(ts.date()),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(close_p, 4),
                    "qty": entry_qty,
                    "pnl": round(pnl, 2),
                    "reason": reason,
                })
                position = None

        elif position == "LONG_B":
            triggered = None
            if z >= stop_z:
                triggered = ("stop_loss", pb)
            elif z <= exit_z:
                triggered = ("mean_reverted", pb)
            elif days_held >= max_hold:
                triggered = ("max_hold", pb)
            if triggered:
                reason, close_p = triggered
                pnl = (close_p - entry_price) * entry_qty
                trades.append({
                    "entry_date": str(entry_ts.date()),
                    "exit_date": str(ts.date()),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(close_p, 4),
                    "qty": entry_qty,
                    "pnl": round(pnl, 2),
                    "reason": reason,
                })
                position = None

        if position is None:
            if z <= -entry_z:
                entry_qty = max(1, int(CAPITAL_PER_TRADE / pa))
                position = "LONG_A"
                entry_price = pa
                entry_ts = ts
            elif z >= entry_z:
                entry_qty = max(1, int(CAPITAL_PER_TRADE / pb))
                position = "LONG_B"
                entry_price = pb
                entry_ts = ts

    return trades


# ============================================================
# Collect all strategy trades
# ============================================================

def collect_all_trades() -> dict[str, list[dict]]:
    print("\nLoading portfolios...")
    ma_portfolio = json.loads((REPORTS_DIR / "optimal_ma_pullback_portfolio.json").read_text())
    bb_raw = json.loads((REPORTS_DIR / "bb_squeeze_results.json").read_text())
    bb_portfolio = [r for r in bb_raw["results"] if r["pass_oos"]]
    swan_portfolio = json.loads((REPORTS_DIR / "optimal_long_only_portfolio.json").read_text())
    st_portfolio = json.loads((REPORTS_DIR / "optimal_supertrend_portfolio.json").read_text())

    print(f"  MA Pullback: {len(ma_portfolio)} symbols | BB Squeeze: {len(bb_portfolio)} symbols | "
          f"Black Swan: {len(swan_portfolio)} pairs | Supertrend: {len(st_portfolio)} symbols")

    strategy_trades: dict[str, list[dict]] = {
        "MA Pullback": [],
        "BB Squeeze": [],
        "Black Swan": [],
        "Supertrend": [],
    }

    print("\nFetching data + replaying MA Pullback...")
    for entry in ma_portfolio:
        sym = entry["symbol"]
        df = fetch(sym)
        if df is not None:
            t = replay_ma_pullback(df, entry["optimal_params"])
            for trade in t:
                trade["strategy"] = "MA Pullback"
                trade["symbol"] = sym
            strategy_trades["MA Pullback"].extend(t)
            print(f"  {sym}: {len(t)} trades")

    print("\nFetching data + replaying BB Squeeze...")
    for entry in bb_portfolio:
        sym = entry["symbol"]
        df = fetch(sym)
        if df is not None:
            t = replay_bb_squeeze(df, entry["best_params"])
            for trade in t:
                trade["strategy"] = "BB Squeeze"
                trade["symbol"] = sym
            strategy_trades["BB Squeeze"].extend(t)
            print(f"  {sym}: {len(t)} trades")

    print("\nReplaying Black Swan pairs...")
    for pair in swan_portfolio:
        sym_a, sym_b = pair["symbol_a"], pair["symbol_b"]
        fetch(sym_a); fetch(sym_b)  # pre-warm cache
        t = replay_black_swan(sym_a, sym_b, pair["optimal_params"])
        for trade in t:
            trade["strategy"] = "Black Swan"
            trade["symbol"] = f"{sym_a}/{sym_b}"
        strategy_trades["Black Swan"].extend(t)
        print(f"  {sym_a}/{sym_b}: {len(t)} trades")

    print("\nFetching data + replaying Supertrend...")
    for entry in st_portfolio:
        sym = entry["symbol"]
        df = fetch(sym)
        if df is not None:
            t = replay_supertrend(df, entry["optimal_params"])
            for trade in t:
                trade["strategy"] = "Supertrend"
                trade["symbol"] = sym
            strategy_trades["Supertrend"].extend(t)
            print(f"  {sym}: {len(t)} trades")

    # Print summary
    print("\nTrade counts:")
    for name, trades in strategy_trades.items():
        if trades:
            first = min(t["entry_date"] for t in trades)
            last = max(t["exit_date"] for t in trades)
            print(f"  {name:12}: {len(trades):4} trades  |  {first} → {last}")
        else:
            print(f"  {name:12}: 0 trades")

    return strategy_trades


# ============================================================
# Simulate ₹2L account with Master Risk Engine logic
# ============================================================

STRAT_PRIORITY = {"MA Pullback": 3, "BB Squeeze": 2, "Supertrend": 2, "Black Swan": 1}


def simulate_combo(strategy_names: list[str], all_trades: dict) -> dict:
    # Deep copy trades to avoid mutation across combo runs
    relevant = []
    for sn in strategy_names:
        for t in all_trades.get(sn, []):
            relevant.append(copy.copy(t))

    valid = [t for t in relevant if t.get("entry_date") and t.get("exit_date")]
    valid.sort(key=lambda x: x["entry_date"])

    if not valid:
        return {"total_pnl": 0.0, "yearly_pnl": {}, "trades_taken": 0, "trades_skipped": 0}

    all_dates = sorted(set(t["entry_date"] for t in valid) | set(t["exit_date"] for t in valid))

    free_cash = float(TOTAL_CAPITAL)
    active: list[dict] = []     # list of {trade, actual_qty, entry_price_actual}
    yearly_pnl: dict[str, float] = defaultdict(float)
    total_pnl = 0.0
    trades_taken = 0
    trades_skipped = 0

    for current_date in all_dates:
        # A. Close trades exiting today
        still_open = []
        for slot in active:
            t, aq = slot["trade"], slot["actual_qty"]
            if t["exit_date"] == current_date:
                pnl = (t["exit_price"] - slot["entry_price"]) * aq
                free_cash += slot["entry_price"] * aq + pnl
                yr = current_date[:4]
                yearly_pnl[yr] += pnl
                total_pnl += pnl
            else:
                still_open.append(slot)
        active = still_open

        # B. Handle new signals today
        signals = [t for t in valid if t["entry_date"] == current_date]
        if not signals:
            continue

        signals.sort(key=lambda t: STRAT_PRIORITY.get(t.get("strategy", ""), 0), reverse=True)

        if free_cash < MIN_CHUNK:
            trades_skipped += len(signals)
            continue

        max_slots = int(free_cash // MIN_CHUNK)
        selected = signals[:max_slots]
        trades_skipped += len(signals) - len(selected)

        chunk = free_cash / len(selected)

        for t in selected:
            actual_qty = max(1, int(chunk / t["entry_price"]))
            capital_used = actual_qty * t["entry_price"]
            free_cash -= capital_used
            active.append({"trade": t, "actual_qty": actual_qty, "entry_price": t["entry_price"]})
            trades_taken += 1

    return {
        "total_pnl": round(total_pnl, 0),
        "yearly_pnl": {k: round(v, 0) for k, v in yearly_pnl.items()},
        "trades_taken": trades_taken,
        "trades_skipped": trades_skipped,
        "final_cash": round(free_cash, 0),
    }


# ============================================================
# Main
# ============================================================

COMBOS = {
    "BB+MA+Swan": ["BB Squeeze", "MA Pullback", "Black Swan"],
    "ST+MA+Swan": ["Supertrend", "MA Pullback", "Black Swan"],
    "BB+ST+Swan": ["BB Squeeze", "Supertrend", "Black Swan"],
    "All 4":      ["BB Squeeze", "MA Pullback", "Supertrend", "Black Swan"],
}


def main() -> None:
    all_trades = collect_all_trades()

    print(f"\n{'=' * 65}")
    print("  COMBINATION SIMULATION  —  ₹2L ACCOUNT  —  10 YEARS")
    print(f"{'=' * 65}")

    results: dict[str, dict] = {}
    for combo_name, strategies in COMBOS.items():
        print(f"\nSimulating: {combo_name}  ({', '.join(strategies)})")
        r = simulate_combo(strategies, all_trades)
        results[combo_name] = r
        print(f"  Trades taken: {r['trades_taken']}  |  "
              f"Skipped: {r['trades_skipped']}  |  "
              f"Total PnL: ₹{r['total_pnl']:,.0f}")

    # ── Year-by-year table ──────────────────────────────────────────
    all_years = sorted(set(yr for r in results.values() for yr in r["yearly_pnl"].keys()))

    col_w = 16
    names = list(COMBOS.keys())

    header = f"{'Year':<6}" + "".join(f"{n:>{col_w}}" for n in names)
    print(f"\n{'=' * len(header)}")
    print("ANNUAL RETURNS  (% of account value at start of that year)")
    print(f"{'=' * len(header)}")
    print(header)
    print("-" * len(header))

    running = {n: float(TOTAL_CAPITAL) for n in names}
    for year in all_years:
        row = f"{year:<6}"
        for name in names:
            yr_pnl = results[name]["yearly_pnl"].get(year, 0.0)
            pct = (yr_pnl / running[name]) * 100 if running[name] > 0 else 0.0
            running[name] += yr_pnl
            sign = "+" if pct >= 0 else ""
            row += f"  {sign}{pct:.1f}%".rjust(col_w)
        print(row)

    print("-" * len(header))
    neg_row = f"{'Neg yrs':<6}"
    for name in names:
        neg = sum(1 for v in results[name]["yearly_pnl"].values() if v < 0)
        neg_row += f"  {neg}".rjust(col_w)
    print(neg_row)

    print(f"\n{'=' * len(header)}")
    print("TOTAL RETURN  (on ₹2,00,000 starting capital, compounding)")
    print(f"{'=' * len(header)}")
    for name in names:
        final = TOTAL_CAPITAL + results[name]["total_pnl"]
        total_pct = ((final - TOTAL_CAPITAL) / TOTAL_CAPITAL) * 100
        yrs = len(all_years)
        cagr = ((final / TOTAL_CAPITAL) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0
        print(f"  {name:<14}: ₹{final:>8,.0f}  |  "
              f"Total {total_pct:+.1f}%  |  "
              f"CAGR ~{cagr:.1f}%/yr  |  "
              f"Trades: {results[name]['trades_taken']}")


if __name__ == "__main__":
    main()
