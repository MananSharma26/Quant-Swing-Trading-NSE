"""VIX Filter Backtest.

Tests whether blocking new entries on high-volatility days improves
risk-adjusted returns. Uses India VIX (^INDIAVIX) from yfinance.

Filter logic: if VIX >= threshold on entry date, skip all new entries
for that day. Existing open positions are NOT affected — they continue
to their natural exit (stop, target, time, signal reversal).

Thresholds tested: no filter, VIX≥20, VIX≥25, VIX≥30

Uses identical simulation engine as sensitivity_analysis.py (canonical).
"""

from __future__ import annotations

import copy
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archive"))
sys.path.insert(0, str(ROOT / "src"))

import simulate_combinations as sim

TOTAL_CAPITAL = 2_00_000
MIN_CHUNK     = 30_000
PRIORITY      = {"MA Pullback": 4, "Supertrend": 3, "BB Squeeze": 2, "Black Swan": 1}
STRATEGIES    = list(PRIORITY.keys())

THRESHOLDS = {
    "No filter": None,
    "VIX ≥ 20":  20,
    "VIX ≥ 25":  25,
    "VIX ≥ 30":  30,
}


# ─────────────────────────────────────────────────────────────────────────────
# Fetch India VIX
# ─────────────────────────────────────────────────────────────────────────────

def fetch_vix() -> dict[str, float]:
    """Return {date_str: vix_close} for the full available history."""
    print("Fetching India VIX (^INDIAVIX)...")
    df = yf.download("^INDIAVIX", period="10y", interval="1d",
                     progress=False, auto_adjust=True)
    if df.empty:
        print("  WARNING: Could not fetch India VIX — filter will be disabled.")
        return {}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df = df.reset_index()
    df["date_str"] = df["Date"].dt.strftime("%Y-%m-%d")
    vix = dict(zip(df["date_str"], df["close"].astype(float)))
    print(f"  VIX data: {min(vix)} → {max(vix)}  ({len(vix)} trading days)")
    return vix


# ─────────────────────────────────────────────────────────────────────────────
# Core simulation (identical to sensitivity_analysis.py + VIX gate)
# ─────────────────────────────────────────────────────────────────────────────

def run(all_trades: dict, vix: dict[str, float], threshold: int | None) -> dict:
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
    vix_blocked     = 0   # days where entries were blocked by VIX

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

        # ── Drawdown on realized equity ────────────────────────────────────────
        if realized_equity > peak_equity:
            peak_equity = realized_equity
        dd = (peak_equity - realized_equity) / peak_equity if peak_equity > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

        signals = entries_by_date.get(date_str, [])
        if not signals:
            continue

        # ── VIX gate: block entries if VIX >= threshold ────────────────────────
        if threshold is not None:
            day_vix = vix.get(date_str)
            if day_vix is not None and day_vix >= threshold:
                vix_blocked += 1
                continue   # exits already processed above; just skip entries

        signals.sort(key=lambda t: PRIORITY.get(t.get("strategy", ""), 0), reverse=True)

        # Same-day dedup
        seen: set[str] = set()
        deduped = []
        for t in signals:
            sym = t.get("symbol", "")
            if sym not in seen:
                seen.add(sym)
                deduped.append(t)
        signals = deduped

        # Held-symbol filter
        held = {slot["trade"].get("symbol", "") for slot in active}
        signals = [t for t in signals if t.get("symbol", "") not in held]

        if not signals or free_cash < MIN_CHUNK:
            continue

        max_slots = int(free_cash // MIN_CHUNK)
        selected  = signals[:max_slots]
        chunk     = free_cash / len(selected)

        for t in selected:
            qty = max(1, int(chunk / t["entry_price"]))
            free_cash -= qty * t["entry_price"]
            active.append({"trade": t, "actual_qty": qty, "entry_price": t["entry_price"]})
            trades_taken += 1

    # ── Metrics ────────────────────────────────────────────────────────────────
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
        "vix_blocked":  vix_blocked,
        "final_equity": round(final, 0),
        "annual":       annual_returns,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    vix        = fetch_vix()
    all_trades = sim.collect_all_trades()

    print("\nRunning VIX filter comparison...")
    results: dict[str, dict] = {}
    for label, threshold in THRESHOLDS.items():
        results[label] = run(all_trades, vix, threshold)

    labels = list(THRESHOLDS.keys())

    # ── Year-by-year table ────────────────────────────────────────────────────
    all_years = sorted(set(yr for r in results.values() for yr in r["annual"].keys()))
    col_w = 14

    header = f"{'Year':<6}" + "".join(f"{n:>{col_w}}" for n in labels)
    sep    = "═" * len(header)

    print(f"\n{sep}")
    print("ANNUAL RETURNS  (% of account equity at start of each year)")
    print(sep)
    print(header)
    print("─" * len(header))

    first_5y = [yr for yr in all_years if yr <= "2020"]
    last_5y  = [yr for yr in all_years if yr > "2020"]

    for yr in all_years:
        if last_5y and yr == last_5y[0]:
            print("─ OOS period (2021–) " + "─" * (len(header) - 20))
        row = f"{yr:<6}"
        for name in labels:
            val = results[name]["annual"].get(yr, 0.0)
            sign = "+" if val >= 0 else ""
            row += f"  {sign}{val:.1f}%".rjust(col_w)
        print(row)

    print("─" * len(header))

    def _sub_cagr(name: str, years: list[str]) -> str:
        rets_sub = [results[name]["annual"].get(yr, 0.0) for yr in years]
        if not rets_sub:
            return "n/a"
        running = 100.0
        for r in rets_sub:
            running *= (1 + r / 100)
        cagr_sub = (running / 100) ** (1 / len(rets_sub)) - 1
        return f"{cagr_sub * 100:+.1f}%"

    print(f"\n{'First 5y CAGR':<20}" + "".join(f"{_sub_cagr(n, first_5y):>{col_w}}" for n in labels))
    print(f"{'Last 5y CAGR':<20}"  + "".join(f"{_sub_cagr(n, last_5y):>{col_w}}"  for n in labels))

    # ── Summary metrics ───────────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"SUMMARY METRICS  —  ₹2L account  —  MIN_CHUNK ₹{MIN_CHUNK:,}")
    print(sep)

    metrics = [
        ("CAGR",          lambda r: f"{r['cagr']:.1f}%"),
        ("Sharpe",        lambda r: f"{r['sharpe']:.3f}"),
        ("Max Drawdown",  lambda r: f"{r['max_dd']:.1f}%"),
        ("Calmar",        lambda r: f"{r['calmar']:.3f}"),
        ("Neg years",     lambda r: f"{r['neg_years']} / {r['n_years']}"),
        ("Trades taken",  lambda r: f"{r['trades_taken']}"),
        ("Days VIX-blocked", lambda r: f"{r['vix_blocked']}"),
        ("Final equity",  lambda r: f"₹{r['final_equity']:,.0f}"),
    ]

    mheader = f"{'Metric':<20}" + "".join(f"{n:>{col_w}}" for n in labels)
    print(mheader)
    print("─" * len(mheader))
    for label, fmt in metrics:
        row = f"{label:<20}"
        for name in labels:
            row += f"{fmt(results[name]):>{col_w}}"
        print(row)

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("VERDICT  (vs no filter)")
    print(sep)
    baseline = results["No filter"]
    for name in labels[1:]:
        r = results[name]
        print(
            f"  {name:<12}:  "
            f"CAGR {r['cagr'] - baseline['cagr']:+.1f}pp  |  "
            f"Max DD {r['max_dd'] - baseline['max_dd']:+.1f}pp  |  "
            f"Sharpe {r['sharpe'] - baseline['sharpe']:+.3f}  |  "
            f"Calmar {r['calmar'] - baseline['calmar']:+.3f}  |  "
            f"Trades {r['trades_taken']} (blocked {r['vix_blocked']} entry-days)"
        )


if __name__ == "__main__":
    main()
