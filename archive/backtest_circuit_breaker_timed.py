"""Portfolio Drawdown Circuit Breaker — Time-Based Reset.

If account realized equity drops X% from peak, block all new entries for N days
then automatically resume (regardless of whether equity has recovered).

This fixes the death spiral in the original circuit breaker where "block until
equity recovers to peak" caused the system to stop trading indefinitely.

Configurations tested:
  DD trigger × reset window: 10%/15d, 10%/30d, 15%/15d, 15%/30d, 20%/15d, 20%/30d

Uses identical simulation engine as sensitivity_analysis.py (canonical).
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

CONFIGS = {
    "No breaker":    (None, None),
    "10% / 15d":     (0.10, 15),
    "10% / 30d":     (0.10, 30),
    "15% / 15d":     (0.15, 15),
    "15% / 30d":     (0.15, 30),
    "20% / 15d":     (0.20, 15),
    "20% / 30d":     (0.20, 30),
}


def run(all_trades: dict, dd_trigger: float | None, reset_days: int | None) -> dict:
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
    days_blocked    = 0
    times_triggered = 0

    cooldown_until: pd.Timestamp | None = None   # entries blocked until this date

    for date_str in all_dates:
        current_ts = pd.Timestamp(date_str)

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

        # ── Track peak and current drawdown ────────────────────────────────────
        if realized_equity > peak_equity:
            peak_equity = realized_equity
        current_dd = (peak_equity - realized_equity) / peak_equity if peak_equity > 0 else 0.0
        if current_dd > max_dd_pct:
            max_dd_pct = current_dd

        signals = entries_by_date.get(date_str, [])
        if not signals:
            continue

        # ── Circuit breaker logic ──────────────────────────────────────────────
        if dd_trigger is not None:
            # Check if we should trigger a new cooldown
            if cooldown_until is None and current_dd >= dd_trigger:
                cooldown_until = current_ts + pd.offsets.BDay(reset_days)
                times_triggered += 1

            # Check if we're still in cooldown
            if cooldown_until is not None:
                if current_ts <= cooldown_until:
                    days_blocked += 1
                    continue   # block entries today
                else:
                    cooldown_until = None   # cooldown expired — resume trading

        signals.sort(key=lambda t: PRIORITY.get(t.get("strategy", ""), 0), reverse=True)

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
        "trades_taken": trades_taken, "days_blocked": days_blocked,
        "times_triggered": times_triggered,
        "final_equity": round(final, 0), "annual": annual_returns,
    }


def main() -> None:
    all_trades = sim.collect_all_trades()

    print("\nRunning timed circuit breaker comparison...")
    results = {label: run(all_trades, dd, rd) for label, (dd, rd) in CONFIGS.items()}
    labels  = list(CONFIGS.keys())
    col_w   = 13

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
        ("CAGR",          lambda r: f"{r['cagr']:.1f}%"),
        ("Sharpe",        lambda r: f"{r['sharpe']:.3f}"),
        ("Max Drawdown",  lambda r: f"{r['max_dd']:.1f}%"),
        ("Calmar",        lambda r: f"{r['calmar']:.3f}"),
        ("Neg years",     lambda r: f"{r['neg_years']} / {r['n_years']}"),
        ("Trades taken",  lambda r: f"{r['trades_taken']}"),
        ("Times triggered", lambda r: f"{r['times_triggered']}"),
        ("Days blocked",  lambda r: f"{r['days_blocked']}"),
        ("Final equity",  lambda r: f"₹{r['final_equity']:,.0f}"),
    ]
    mh = f"{'Metric':<18}" + "".join(f"{n:>{col_w}}" for n in labels)
    print(mh + "\n" + "─" * len(mh))
    for lbl, fmt in metrics:
        print(f"{lbl:<18}" + "".join(f"{fmt(results[n]):>{col_w}}" for n in labels))

    print(f"\n{sep}\nVERDICT  (vs no breaker)\n{sep}")
    base = results["No breaker"]
    for name in labels[1:]:
        r = results[name]
        print(f"  {name:<14}: CAGR {r['cagr']-base['cagr']:+.1f}pp  |  "
              f"Max DD {r['max_dd']-base['max_dd']:+.1f}pp  |  "
              f"Sharpe {r['sharpe']-base['sharpe']:+.3f}  |  "
              f"Calmar {r['calmar']-base['calmar']:+.3f}  |  "
              f"triggered {r['times_triggered']}× / {r['days_blocked']} days blocked")


if __name__ == "__main__":
    main()
