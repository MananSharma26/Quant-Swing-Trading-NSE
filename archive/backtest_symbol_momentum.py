"""Per-Symbol Momentum Filter Backtest.

Before entering any signal, checks if the symbol is above its own N-day SMA.
Only enters if yes — filters out entries into weak/downtrending stocks.

This is different from the Nifty regime filter: that blocks ALL entries when
the broad market is weak. This blocks only the specific stock that is weak,
leaving other signals unaffected.

SMA periods tested: no filter, SMA50, SMA100, SMA200

Uses identical simulation engine as sensitivity_analysis.py (canonical).
Price data is taken from simulate_combinations._data_cache (already fetched).
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

PERIODS = {
    "No filter": None,
    "SMA 50":    50,
    "SMA 100":   100,
    "SMA 200":   200,
}


# ─────────────────────────────────────────────────────────────────────────────
# Build SMA lookup from already-fetched price data
# ─────────────────────────────────────────────────────────────────────────────

def build_sma_lookup(sma_period: int) -> dict[str, dict[str, float]]:
    """Return {symbol: {date_str: sma_value}} using the cached price data."""
    lookup: dict[str, dict[str, float]] = {}
    for symbol, df in sim._data_cache.items():
        if df is None or df.empty:
            continue
        # Handle pair symbols (Black Swan) — use first leg only for SMA check
        base_sym = symbol.split("/")[0] if "/" in symbol else symbol
        if base_sym != symbol:
            continue   # pairs handled separately below
        d = df.copy()
        d["sma"] = d["close"].rolling(sma_period, min_periods=sma_period).mean()
        d = d.dropna(subset=["sma"])
        d["date_str"] = d["timestamp"].dt.strftime("%Y-%m-%d")
        lookup[symbol] = dict(zip(d["date_str"], d["sma"].astype(float)))
    return lookup


def build_pair_sma_lookup(sma_period: int) -> dict[str, dict[str, float]]:
    """For pair symbols A/B, use the *bought* leg close vs its SMA.
    Since we don't know at build time which leg will be bought, store both legs.
    """
    lookup: dict[str, dict[str, float]] = {}
    for symbol, df in sim._data_cache.items():
        if df is None or df.empty or "/" in symbol:
            continue
        d = df.copy()
        d["sma"] = d["close"].rolling(sma_period, min_periods=sma_period).mean()
        d = d.dropna(subset=["sma"])
        d["date_str"] = d["timestamp"].dt.strftime("%Y-%m-%d")
        lookup[symbol] = dict(zip(d["date_str"], d["sma"].astype(float)))
    return lookup


def price_above_sma(symbol: str, entry_date: str,
                    sma_lookup: dict, pair_sma_lookup: dict,
                    entry_price: float) -> bool:
    """Return True if the symbol (or bought leg of a pair) is above its SMA."""
    if "/" in symbol:
        # For pairs, the entry_price is the bought leg's price.
        # We check if that bought leg is above its own SMA.
        # We try both legs and check which price matches.
        legs = symbol.split("/")
        for leg in legs:
            leg_sma = pair_sma_lookup.get(leg, {}).get(entry_date)
            leg_df  = sim._data_cache.get(leg)
            if leg_df is None or leg_sma is None:
                continue
            # Match entry_price to the leg close on entry_date
            row = leg_df[leg_df["timestamp"].dt.strftime("%Y-%m-%d") == entry_date]
            if not row.empty:
                leg_close = float(row["close"].iloc[0])
                if abs(leg_close - entry_price) / max(entry_price, 1) < 0.02:
                    return leg_close >= leg_sma
        return True   # can't determine — allow entry
    else:
        sma = sma_lookup.get(symbol, {}).get(entry_date)
        if sma is None:
            return True   # no SMA data — allow entry
        return entry_price >= sma


# ─────────────────────────────────────────────────────────────────────────────
# Core simulation
# ─────────────────────────────────────────────────────────────────────────────

def run(all_trades: dict, sma_period: int | None) -> dict:
    relevant = []
    for sn in STRATEGIES:
        for t in all_trades.get(sn, []):
            relevant.append(copy.copy(t))

    valid = [t for t in relevant if t.get("entry_date") and t.get("exit_date")]
    valid.sort(key=lambda x: x["entry_date"])
    if not valid:
        return {}

    # Build SMA lookup once per run
    sma_lookup      = build_sma_lookup(sma_period) if sma_period else {}
    pair_sma_lookup = build_pair_sma_lookup(sma_period) if sma_period else {}

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
    filtered_weak   = 0

    for date_str in all_dates:
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

        if realized_equity > peak_equity:
            peak_equity = realized_equity
        dd = (peak_equity - realized_equity) / peak_equity if peak_equity > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd

        signals = entries_by_date.get(date_str, [])
        if not signals:
            continue

        signals.sort(key=lambda t: PRIORITY.get(t.get("strategy", ""), 0), reverse=True)

        # ── Per-symbol momentum filter ─────────────────────────────────────────
        if sma_period is not None:
            passing = []
            for t in signals:
                sym = t.get("symbol", "")
                ep  = t.get("entry_price", 0.0)
                if price_above_sma(sym, date_str, sma_lookup, pair_sma_lookup, ep):
                    passing.append(t)
                else:
                    filtered_weak += 1
            signals = passing

        seen: set[str] = set()
        deduped = []
        for t in signals:
            sym = t.get("symbol", "")
            if sym not in seen:
                seen.add(sym)
                deduped.append(t)
        signals = deduped

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

    annual_returns: dict[str, float] = {}
    running = float(TOTAL_CAPITAL)
    for yr in sorted(yearly_pnl):
        pnl_yr = yearly_pnl[yr]
        annual_returns[yr] = (pnl_yr / running) * 100
        running += pnl_yr

    rets   = list(annual_returns.values())
    n      = len(rets)
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
        "neg_years": sum(1 for v in rets if v < 0), "n_years": n,
        "trades_taken": trades_taken, "filtered_weak": filtered_weak,
        "final_equity": round(final, 0), "annual": annual_returns,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    all_trades = sim.collect_all_trades()   # also warms _data_cache

    print("\nRunning per-symbol momentum filter comparison...")
    results = {label: run(all_trades, period) for label, period in PERIODS.items()}
    labels  = list(PERIODS.keys())
    col_w   = 14

    all_years = sorted(set(yr for r in results.values() for yr in r["annual"].keys()))
    first_5y  = [yr for yr in all_years if yr <= "2020"]
    last_5y   = [yr for yr in all_years if yr > "2020"]

    header = f"{'Year':<6}" + "".join(f"{n:>{col_w}}" for n in labels)
    sep    = "═" * len(header)
    print(f"\n{sep}\nANNUAL RETURNS\n{sep}\n{header}\n{'─'*len(header)}")

    for yr in all_years:
        if last_5y and yr == last_5y[0]:
            print("─ OOS (2021–) " + "─" * (len(header) - 13))
        row = f"{yr:<6}"
        for name in labels:
            val = results[name]["annual"].get(yr, 0.0)
            row += f"  {'+' if val>=0 else ''}{val:.1f}%".rjust(col_w)
        print(row)
    print("─" * len(header))

    def _sub_cagr(name, years):
        r = 100.0
        for v in [results[name]["annual"].get(yr, 0.0) for yr in years]:
            r *= (1 + v / 100)
        return f"{((r/100)**(1/len(years))-1)*100:+.1f}%" if years else "n/a"

    print(f"\n{'First 5y CAGR':<18}" + "".join(f"{_sub_cagr(n,first_5y):>{col_w}}" for n in labels))
    print(f"{'Last 5y CAGR':<18}"  + "".join(f"{_sub_cagr(n,last_5y):>{col_w}}"  for n in labels))

    print(f"\n{sep}\nSUMMARY METRICS\n{sep}")
    metrics = [
        ("CAGR",             lambda r: f"{r['cagr']:.1f}%"),
        ("Sharpe",           lambda r: f"{r['sharpe']:.3f}"),
        ("Max Drawdown",     lambda r: f"{r['max_dd']:.1f}%"),
        ("Calmar",           lambda r: f"{r['calmar']:.3f}"),
        ("Neg years",        lambda r: f"{r['neg_years']} / {r['n_years']}"),
        ("Trades taken",     lambda r: f"{r['trades_taken']}"),
        ("Weak signals skipped", lambda r: f"{r['filtered_weak']}"),
        ("Final equity",     lambda r: f"₹{r['final_equity']:,.0f}"),
    ]
    mh = f"{'Metric':<24}" + "".join(f"{n:>{col_w}}" for n in labels)
    print(mh + "\n" + "─" * len(mh))
    for lbl, fmt in metrics:
        print(f"{lbl:<24}" + "".join(f"{fmt(results[n]):>{col_w}}" for n in labels))

    print(f"\n{sep}\nVERDICT  (vs no filter)\n{sep}")
    base = results["No filter"]
    for name in labels[1:]:
        r = results[name]
        print(f"  {name:<10}: CAGR {r['cagr']-base['cagr']:+.1f}pp  |  "
              f"Max DD {r['max_dd']-base['max_dd']:+.1f}pp  |  "
              f"Sharpe {r['sharpe']-base['sharpe']:+.3f}  |  "
              f"Calmar {r['calmar']-base['calmar']:+.3f}  |  "
              f"{r['filtered_weak']} weak signals skipped")


if __name__ == "__main__":
    main()
