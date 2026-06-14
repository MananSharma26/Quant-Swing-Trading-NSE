"""Sensitivity Analysis — MIN_CHUNK and strategy priority ordering.

Sweeps:
  MIN_CHUNK:  ₹30k, ₹40k, ₹50k, ₹60k
  Priority:   all 24 permutations of (BB Squeeze, MA Pullback, Supertrend, Black Swan)

Dedup rule stays ON in all runs (correctness, not a tunable param).
Reports CAGR, Sharpe, Max Drawdown, Calmar for each combo.
"""

from __future__ import annotations

import copy
import itertools
import sys
import statistics
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archive"))
sys.path.insert(0, str(ROOT / "src"))

import simulate_combinations as sim

TOTAL_CAPITAL  = 2_00_000
STRATEGIES     = ["BB Squeeze", "MA Pullback", "Supertrend", "Black Swan"]
MIN_CHUNKS     = [30_000, 40_000, 50_000, 60_000]


# ── Core simulation ───────────────────────────────────────────────────────────

def run(all_trades: dict, min_chunk: int, priority: dict[str, int]) -> dict:
    relevant = []
    for sn in STRATEGIES:
        for t in all_trades.get(sn, []):
            relevant.append(copy.copy(t))

    valid = [t for t in relevant if t.get("entry_date") and t.get("exit_date")]
    valid.sort(key=lambda x: x["entry_date"])

    entries_by_date: dict[str, list] = defaultdict(list)
    for t in valid:
        entries_by_date[t["entry_date"]].append(t)

    start = pd.Timestamp(valid[0]["entry_date"])
    end   = pd.Timestamp(valid[-1]["exit_date"])
    all_dates = [str(d.date()) for d in pd.date_range(start, end, freq="B")]

    free_cash        = float(TOTAL_CAPITAL)
    active: list     = []
    yearly_pnl: dict = defaultdict(float)
    realized_equity  = float(TOTAL_CAPITAL)
    peak_equity      = float(TOTAL_CAPITAL)
    max_dd_pct       = 0.0
    total_pnl        = 0.0

    for date_str in all_dates:
        # Close exits
        still_open = []
        for slot in active:
            t = slot["trade"]
            if t["exit_date"] == date_str:
                pnl = (t["exit_price"] - slot["entry_price"]) * slot["actual_qty"]
                free_cash += slot["entry_price"] * slot["actual_qty"] + pnl
                yearly_pnl[date_str[:4]] += pnl
                total_pnl += pnl
                realized_equity += pnl
            else:
                still_open.append(slot)
        active = still_open

        # Drawdown
        if realized_equity > peak_equity:
            peak_equity = realized_equity
        dd = (peak_equity - realized_equity) / peak_equity if peak_equity > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

        signals = entries_by_date.get(date_str, [])
        if not signals:
            continue

        signals.sort(key=lambda t: priority.get(t.get("strategy", ""), 0), reverse=True)

        # Dedup by symbol
        seen: set[str] = set()
        deduped = []
        for t in signals:
            sym = t.get("symbol", "")
            if sym not in seen:
                seen.add(sym)
                deduped.append(t)
        signals = deduped

        # Filter held
        held = {slot["trade"].get("symbol", "") for slot in active}
        signals = [t for t in signals if t.get("symbol", "") not in held]

        if not signals or free_cash < min_chunk:
            continue

        max_slots = int(free_cash // min_chunk)
        selected  = signals[:max_slots]
        chunk     = free_cash / len(selected)

        for t in selected:
            qty = max(1, int(chunk / t["entry_price"]))
            free_cash -= qty * t["entry_price"]
            active.append({"trade": t, "actual_qty": qty, "entry_price": t["entry_price"]})

    # Metrics
    running = float(TOTAL_CAPITAL)
    annual_returns = {}
    for yr in sorted(yearly_pnl):
        pnl_yr = yearly_pnl[yr]
        annual_returns[yr] = (pnl_yr / running) * 100
        running += pnl_yr

    rets = list(annual_returns.values())
    n    = len(rets)
    mean_r = statistics.mean(rets) if n > 1 else 0.0
    std_r  = statistics.stdev(rets) if n > 1 else 1.0
    sharpe = mean_r / std_r if std_r > 0 else 0.0
    final  = TOTAL_CAPITAL + total_pnl
    cagr   = ((final / TOTAL_CAPITAL) ** (1.0 / n) - 1.0) * 100 if final > 0 and n > 0 else 0.0
    max_dd = max_dd_pct * 100
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    return {
        "cagr": round(cagr, 2), "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 1), "calmar": round(calmar, 3),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    all_trades = sim.collect_all_trades()

    # ── Part 1: MIN_CHUNK sensitivity (fixed priority: current live) ──────────
    live_priority = {"Supertrend": 4, "MA Pullback": 3, "BB Squeeze": 2, "Black Swan": 1}

    print("\n" + "═" * 72)
    print("PART 1 — MIN_CHUNK SENSITIVITY  (priority: ST > MA > BB > Swan)")
    print("═" * 72)
    print(f"{'MIN_CHUNK':>12}  {'CAGR':>8}  {'Sharpe':>8}  {'Max DD':>8}  {'Calmar':>8}")
    print("─" * 72)

    chunk_results = {}
    for mc in MIN_CHUNKS:
        r = run(all_trades, mc, live_priority)
        chunk_results[mc] = r
        print(f"  ₹{mc:>6,}    {r['cagr']:>7.1f}%  {r['sharpe']:>8.3f}  {r['max_dd']:>7.1f}%  {r['calmar']:>8.3f}")

    best_chunk = max(chunk_results, key=lambda k: chunk_results[k]["sharpe"])
    print(f"\n  → Best by Sharpe: ₹{best_chunk:,}")

    # ── Part 2: Priority ordering sensitivity (fixed MIN_CHUNK: ₹40k) ────────
    print("\n" + "═" * 72)
    print("PART 2 — PRIORITY ORDERING  (all 24 permutations, MIN_CHUNK=₹40k)")
    print("═" * 72)
    print(f"  {'Order (1st=highest)':42}  {'CAGR':>7}  {'Sharpe':>7}  {'Max DD':>7}  {'Calmar':>7}")
    print("─" * 72)

    short = {"BB Squeeze": "BB", "MA Pullback": "MA", "Supertrend": "ST", "Black Swan": "Swan"}

    perm_results = []
    for perm in itertools.permutations(STRATEGIES):
        priority = {s: (4 - i) for i, s in enumerate(perm)}
        r = run(all_trades, 40_000, priority)
        label = " > ".join(short[s] for s in perm)
        perm_results.append((label, perm, r))

    # Sort by Sharpe descending
    perm_results.sort(key=lambda x: x[2]["sharpe"], reverse=True)

    for i, (label, perm, r) in enumerate(perm_results):
        marker = " ◄ current" if perm == ("Supertrend", "MA Pullback", "BB Squeeze", "Black Swan") else ""
        marker = " ◄ old" if perm == ("MA Pullback", "BB Squeeze", "Supertrend", "Black Swan") else marker
        print(f"  {label:42}  {r['cagr']:>6.1f}%  {r['sharpe']:>7.3f}  {r['max_dd']:>6.1f}%  {r['calmar']:>7.3f}{marker}")

    print("\n  Top 5 by Sharpe:")
    for label, _, r in perm_results[:5]:
        print(f"    {label:42}  Sharpe {r['sharpe']:.3f}  CAGR {r['cagr']:.1f}%  MaxDD {r['max_dd']:.1f}%")

    print("\n  Top 5 by CAGR:")
    for label, _, r in sorted(perm_results, key=lambda x: x[2]["cagr"], reverse=True)[:5]:
        print(f"    {label:42}  CAGR {r['cagr']:.1f}%  Sharpe {r['sharpe']:.3f}  MaxDD {r['max_dd']:.1f}%")

    print("\n  Top 5 by Calmar:")
    for label, _, r in sorted(perm_results, key=lambda x: x[2]["calmar"], reverse=True)[:5]:
        print(f"    {label:42}  Calmar {r['calmar']:.3f}  CAGR {r['cagr']:.1f}%  MaxDD {r['max_dd']:.1f}%")

    # ── Part 3: Best combo of chunk + priority ────────────────────────────────
    print("\n" + "═" * 72)
    print("PART 3 — BEST CHUNK × PRIORITY COMBO  (sweep all 96 combinations)")
    print("═" * 72)

    all_combos = []
    for mc in MIN_CHUNKS:
        for label, perm, _ in perm_results:
            priority = {s: (4 - i) for i, s in enumerate(perm)}
            r = run(all_trades, mc, priority)
            all_combos.append((mc, label, r))

    print("\n  Top 5 by Sharpe:")
    for mc, label, r in sorted(all_combos, key=lambda x: x[2]["sharpe"], reverse=True)[:5]:
        print(f"    ₹{mc:,}  {label:42}  Sharpe {r['sharpe']:.3f}  CAGR {r['cagr']:.1f}%  MaxDD {r['max_dd']:.1f}%")

    print("\n  Top 5 by CAGR:")
    for mc, label, r in sorted(all_combos, key=lambda x: x[2]["cagr"], reverse=True)[:5]:
        print(f"    ₹{mc:,}  {label:42}  CAGR {r['cagr']:.1f}%  Sharpe {r['sharpe']:.3f}  MaxDD {r['max_dd']:.1f}%")

    print("\n  Top 5 by Calmar:")
    for mc, label, r in sorted(all_combos, key=lambda x: x[2]["calmar"], reverse=True)[:5]:
        print(f"    ₹{mc:,}  {label:42}  Calmar {r['calmar']:.3f}  CAGR {r['cagr']:.1f}%  MaxDD {r['max_dd']:.1f}%")


if __name__ == "__main__":
    main()
