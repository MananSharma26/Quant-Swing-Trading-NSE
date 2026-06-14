"""Fixed ₹50k vs Dynamic sizing — All 4 strategies, 10-year backtest.

Dynamic (current):  chunk = free_cash / n_signals_today  (min ₹40k floor)
Fixed ₹50k:         chunk = ₹50,000 always, enter only if free_cash >= ₹50k
"""

from __future__ import annotations

import copy
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archive"))
sys.path.insert(0, str(ROOT / "src"))

import simulate_combinations as sim
from trading_engine.strategy_priority import strategy_score

TOTAL_CAPITAL  = 2_00_000
DYNAMIC_FLOOR  = 40_000   # current MIN_CHUNK
FIXED_CHUNK    = 50_000   # fixed position size to test


def run(all_trades: dict, mode: str) -> dict:
    """
    mode: 'dynamic' or 'fixed'
    dynamic — chunk = free_cash / n_selected  (floor ₹40k)
    fixed   — chunk = ₹50k always
    """
    strategy_names = ["BB Squeeze", "MA Pullback", "Supertrend", "Black Swan"]

    relevant = []
    for sn in strategy_names:
        for t in all_trades.get(sn, []):
            relevant.append(copy.copy(t))

    valid = [t for t in relevant if t.get("entry_date") and t.get("exit_date")]
    valid.sort(key=lambda x: x["entry_date"])

    entries_by_date: dict[str, list[dict]] = defaultdict(list)
    for t in valid:
        entries_by_date[t["entry_date"]].append(t)

    start = pd.Timestamp(valid[0]["entry_date"])
    end   = pd.Timestamp(valid[-1]["exit_date"])
    all_dates = [str(d.date()) for d in pd.date_range(start, end, freq="B")]

    free_cash  = float(TOTAL_CAPITAL)
    active: list[dict] = []
    yearly_pnl: dict[str, float] = defaultdict(float)
    total_pnl  = 0.0
    trades_taken   = 0
    trades_skipped = 0

    for date_str in all_dates:

        # 1. Close exits
        still_open = []
        for slot in active:
            t = slot["trade"]
            if t["exit_date"] == date_str:
                pnl = (t["exit_price"] - slot["entry_price"]) * slot["actual_qty"]
                free_cash += slot["entry_price"] * slot["actual_qty"] + pnl
                yearly_pnl[date_str[:4]] += pnl
                total_pnl += pnl
            else:
                still_open.append(slot)
        active = still_open

        # 2. New signals
        signals = entries_by_date.get(date_str, [])
        if not signals:
            continue

        signals.sort(key=lambda t: strategy_score(t.get("strategy", "")), reverse=True)

        # Dedup by symbol
        seen: set[str] = set()
        deduped = []
        for t in signals:
            sym = t.get("symbol", "")
            if sym not in seen:
                seen.add(sym)
                deduped.append(t)
        signals = deduped

        # Filter held symbols
        held = {slot["trade"].get("symbol", "") for slot in active}
        signals = [t for t in signals if t.get("symbol", "") not in held]

        if not signals:
            continue

        if mode == "dynamic":
            floor = DYNAMIC_FLOOR
            max_slots = int(free_cash // floor)
            if max_slots == 0:
                trades_skipped += len(signals)
                continue
            selected = signals[:max_slots]
            trades_skipped += len(signals) - len(selected)
            chunk = free_cash / len(selected)
            for t in selected:
                qty = max(1, int(chunk / t["entry_price"]))
                free_cash -= qty * t["entry_price"]
                active.append({"trade": t, "actual_qty": qty, "entry_price": t["entry_price"]})
                trades_taken += 1

        else:  # fixed
            max_slots = int(free_cash // FIXED_CHUNK)
            if max_slots == 0:
                trades_skipped += len(signals)
                continue
            selected = signals[:max_slots]
            trades_skipped += len(signals) - len(selected)
            for t in selected:
                qty = max(1, int(FIXED_CHUNK / t["entry_price"]))
                free_cash -= qty * t["entry_price"]
                active.append({"trade": t, "actual_qty": qty, "entry_price": t["entry_price"]})
                trades_taken += 1

    return {
        "total_pnl":      round(total_pnl, 0),
        "yearly_pnl":     {k: round(v, 0) for k, v in yearly_pnl.items()},
        "trades_taken":   trades_taken,
        "trades_skipped": trades_skipped,
    }


def main() -> None:
    all_trades = sim.collect_all_trades()

    print("\nRunning dynamic sizing (current)...")
    dyn = run(all_trades, mode="dynamic")

    print("Running fixed ₹50k sizing...")
    fix = run(all_trades, mode="fixed")

    all_years = sorted(set(dyn["yearly_pnl"]) | set(fix["yearly_pnl"]))

    col = 16
    header = f"{'Year':<6}{'Dynamic':>{col}}{'Fixed ₹50k':>{col}}{'Diff':>{col}}"
    sep = "─" * len(header)
    print(f"\n{sep}")
    print("ANNUAL RETURNS  (% of capital at start of that year, compounding)")
    print(sep)
    print(header)
    print("─" * len(header))

    running_d = running_f = float(TOTAL_CAPITAL)
    for year in all_years:
        d_pnl = dyn["yearly_pnl"].get(year, 0.0)
        f_pnl = fix["yearly_pnl"].get(year, 0.0)
        d_pct = (d_pnl / running_d) * 100 if running_d > 0 else 0.0
        f_pct = (f_pnl / running_f) * 100 if running_f > 0 else 0.0
        running_d += d_pnl
        running_f += f_pnl
        diff = f_pct - d_pct
        sd, sf, sdiff = ("+" if x >= 0 else "" for x in (d_pct, f_pct, diff))
        d_col = f"  {sd}{d_pct:.1f}%".rjust(col)
        f_col = f"  {sf}{f_pct:.1f}%".rjust(col)
        diff_col = f"  {sdiff}{diff:.1f}%".rjust(col)
        print(f"{year:<6}{d_col}{f_col}{diff_col}")

    print("─" * len(header))

    n_years = max(1, len(all_years) - 1)
    print(f"\n{'':6}{'Dynamic':>{col}}{'Fixed ₹50k':>{col}}")
    print(f"{'Total PnL':<22} ₹{dyn['total_pnl']:>10,.0f}   ₹{fix['total_pnl']:>10,.0f}")
    print(f"{'CAGR (~)':<22} {(dyn['total_pnl']/TOTAL_CAPITAL/n_years*100):>9.1f}%   {(fix['total_pnl']/TOTAL_CAPITAL/n_years*100):>9.1f}%")
    print(f"{'Trades taken':<22} {dyn['trades_taken']:>10}   {fix['trades_taken']:>10}")
    print(f"{'Trades skipped':<22} {dyn['trades_skipped']:>10}   {fix['trades_skipped']:>10}")


if __name__ == "__main__":
    main()
