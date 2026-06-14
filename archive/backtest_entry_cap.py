"""Entry Cap Sensitivity — Full Accurate Backtest.

Tests the effect of capping the maximum capital deployed in a single
position. Compares no cap vs ₹1.5L / ₹1.0L / ₹75k caps.

This script uses the IDENTICAL simulation engine as sensitivity_analysis.py,
which produced the canonical 18.3% CAGR / 1.066 Sharpe / 35.0% Max DD numbers.

Parameters (fixed, matching live engine):
  - Total capital:  ₹2,00,000
  - MIN_CHUNK:      ₹30,000  (hard floor — no entry below this)
  - Priority:       MA Pullback (4) > Supertrend (3) > BB Squeeze (2) > Black Swan (1)
  - Same-day dedup: highest-priority strategy wins when two signal the same symbol
  - Held-symbol filter: no new entry in a symbol already held by any strategy
  - Sizing:         chunk = min(free_cash / n_signals, MAX_ENTRY_SIZE)

Cap values tested:
  - No cap  (chunk = free_cash / n_signals — current behaviour)
  - ₹1.5L   (cap single position at ₹1,50,000)
  - ₹1.0L   (cap single position at ₹1,00,000)
  - ₹75k    (cap single position at ₹75,000)
"""

from __future__ import annotations

import copy
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archive"))
sys.path.insert(0, str(ROOT / "src"))

import simulate_combinations as sim

TOTAL_CAPITAL = 2_00_000
MIN_CHUNK     = 30_000
PRIORITY      = {"MA Pullback": 4, "Supertrend": 3, "BB Squeeze": 2, "Black Swan": 1}
STRATEGIES    = list(PRIORITY.keys())

CAPS = {
    "No cap": None,
    "₹1.5L cap": 1_50_000,
    "₹1.0L cap": 1_00_000,
    "₹75k cap":    75_000,
}


# ─────────────────────────────────────────────────────────────────────────────
# Core simulation (identical to sensitivity_analysis.py + trades_taken counter)
# ─────────────────────────────────────────────────────────────────────────────

def run(all_trades: dict, max_entry: int | None) -> dict:
    """Simulate the master risk engine with an optional per-position cap.

    max_entry=None → no cap (current behaviour: chunk = free_cash / n_signals)
    max_entry=N    → chunk = min(free_cash / n_signals, N)
    """
    relevant = []
    for sn in STRATEGIES:
        for t in all_trades.get(sn, []):
            relevant.append(copy.copy(t))

    valid = [t for t in relevant if t.get("entry_date") and t.get("exit_date")]
    valid.sort(key=lambda x: x["entry_date"])

    if not valid:
        return {}

    entries_by_date: dict[str, list] = defaultdict(list)
    for t in valid:
        entries_by_date[t["entry_date"]].append(t)

    start     = pd.Timestamp(valid[0]["entry_date"])
    end       = pd.Timestamp(max(t["exit_date"] for t in valid))
    all_dates = [str(d.date()) for d in pd.date_range(start, end, freq="B")]

    free_cash       = float(TOTAL_CAPITAL)
    active: list    = []
    yearly_pnl: dict[str, float] = defaultdict(float)
    realized_equity = float(TOTAL_CAPITAL)
    peak_equity     = float(TOTAL_CAPITAL)
    max_dd_pct      = 0.0
    total_pnl       = 0.0
    trades_taken    = 0

    for date_str in all_dates:
        # ── Close exits ────────────────────────────────────────────────────────
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

        # ── Track drawdown on realized equity ──────────────────────────────────
        if realized_equity > peak_equity:
            peak_equity = realized_equity
        dd = (peak_equity - realized_equity) / peak_equity if peak_equity > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

        signals = entries_by_date.get(date_str, [])
        if not signals:
            continue

        # Sort by priority (highest first)
        signals.sort(key=lambda t: PRIORITY.get(t.get("strategy", ""), 0), reverse=True)

        # Same-day dedup: one signal per symbol, highest priority wins
        seen: set[str] = set()
        deduped = []
        for t in signals:
            sym = t.get("symbol", "")
            if sym not in seen:
                seen.add(sym)
                deduped.append(t)
        signals = deduped

        # Held-symbol filter: skip symbols already open in any strategy
        held = {slot["trade"].get("symbol", "") for slot in active}
        signals = [t for t in signals if t.get("symbol", "") not in held]

        if not signals or free_cash < MIN_CHUNK:
            continue

        # How many slots can we fund?
        max_slots = int(free_cash // MIN_CHUNK)
        selected  = signals[:max_slots]

        # Dynamic sizing with optional cap
        chunk = free_cash / len(selected)
        if max_entry is not None:
            chunk = min(chunk, float(max_entry))

        for t in selected:
            qty = max(1, int(chunk / t["entry_price"]))
            capital_used = qty * t["entry_price"]
            free_cash -= capital_used
            active.append({"trade": t, "actual_qty": qty, "entry_price": t["entry_price"]})
            trades_taken += 1

    # ── Compute metrics ────────────────────────────────────────────────────────
    annual_returns: dict[str, float] = {}
    running = float(TOTAL_CAPITAL)
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
    neg_years = sum(1 for v in rets if v < 0)

    return {
        "cagr":         round(cagr, 2),
        "sharpe":       round(sharpe, 3),
        "max_dd":       round(max_dd, 1),
        "calmar":       round(calmar, 3),
        "neg_years":    neg_years,
        "n_years":      n,
        "trades_taken": trades_taken,
        "final_equity": round(final, 0),
        "total_pnl":    round(total_pnl, 0),
        "annual":       annual_returns,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    all_trades = sim.collect_all_trades()

    print("\nRunning entry cap comparison...")
    results: dict[str, dict] = {}
    for label, cap_val in CAPS.items():
        results[label] = run(all_trades, cap_val)

    cap_names = list(CAPS.keys())

    # ── Year-by-year table ────────────────────────────────────────────────────
    all_years = sorted(
        set(yr for r in results.values() for yr in r["annual"].keys())
    )

    col_w = 14
    header = f"{'Year':<6}" + "".join(f"{n:>{col_w}}" for n in cap_names)
    sep    = "═" * len(header)

    print(f"\n{sep}")
    print("ANNUAL RETURNS  (% of account equity at start of each year)")
    print(sep)
    print(header)
    print("─" * len(header))

    first_5y = [yr for yr in all_years if yr <= "2020"]
    last_5y  = [yr for yr in all_years if yr > "2020"]

    def _row(yr: str) -> str:
        row = f"{yr:<6}"
        for name in cap_names:
            val = results[name]["annual"].get(yr, 0.0)
            sign = "+" if val >= 0 else ""
            row += f"  {sign}{val:.1f}%".rjust(col_w)
        return row

    for yr in all_years:
        if yr == last_5y[0] if last_5y else False:
            print("─ OOS period (2021–) " + "─" * (len(header) - 20))
        print(_row(yr))

    print("─" * len(header))

    # ── Sub-period CAGR lines ─────────────────────────────────────────────────
    def _sub_cagr(name: str, years: list[str]) -> str:
        rets_sub = [results[name]["annual"].get(yr, 0.0) for yr in years]
        if not rets_sub:
            return "n/a"
        running = 100.0
        for r in rets_sub:
            running *= (1 + r / 100)
        n = len(rets_sub)
        cagr_sub = (running / 100) ** (1 / n) - 1
        return f"{cagr_sub * 100:+.1f}%"

    print(f"\n{'First 5y CAGR':<20}" + "".join(f"{_sub_cagr(n, first_5y):>{col_w}}" for n in cap_names))
    print(f"{'Last 5y CAGR':<20}" + "".join(f"{_sub_cagr(n, last_5y):>{col_w}}" for n in cap_names))

    # ── Summary metrics table ─────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"SUMMARY METRICS  —  ₹2L account  —  full period  —  MIN_CHUNK ₹{MIN_CHUNK:,}")
    print(sep)

    metrics = [
        ("CAGR",         lambda r: f"{r['cagr']:.1f}%"),
        ("Sharpe",       lambda r: f"{r['sharpe']:.3f}"),
        ("Max Drawdown", lambda r: f"{r['max_dd']:.1f}%"),
        ("Calmar",       lambda r: f"{r['calmar']:.3f}"),
        ("Neg years",    lambda r: f"{r['neg_years']} / {r['n_years']}"),
        ("Trades taken", lambda r: f"{r['trades_taken']}"),
        ("Final equity", lambda r: f"₹{r['final_equity']:,.0f}"),
    ]

    metric_col = 14
    mheader = f"{'Metric':<16}" + "".join(f"{n:>{col_w}}" for n in cap_names)
    print(mheader)
    print("─" * len(mheader))
    for label, fmt in metrics:
        row = f"{label:<16}"
        for name in cap_names:
            row += f"{fmt(results[name]):>{col_w}}"
        print(row)

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("VERDICT")
    print(sep)
    baseline = results["No cap"]
    for name in cap_names[1:]:
        r = results[name]
        cagr_delta  = r["cagr"]  - baseline["cagr"]
        dd_delta    = r["max_dd"] - baseline["max_dd"]
        calmar_delta = r["calmar"] - baseline["calmar"]
        print(f"  {name:<12}:  CAGR {cagr_delta:+.1f}pp  |  "
              f"Max DD {dd_delta:+.1f}pp  |  "
              f"Calmar {calmar_delta:+.3f}  |  "
              f"Trades {r['trades_taken']} (vs {baseline['trades_taken']})")


if __name__ == "__main__":
    main()
