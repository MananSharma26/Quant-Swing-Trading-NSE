"""Sensitivity Analysis — Train / Test split.

Train: 2016-01-01 → 2020-12-31  (5 years, in-sample)
Test:  2021-01-01 → 2026-06-14  (5 years, out-of-sample)

For every combination of MIN_CHUNK × priority order (96 total):
  - Run on train period → rank params
  - Run on test period  → check if ranking holds

Key question: do the params that win in-sample also win out-of-sample?
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

TOTAL_CAPITAL = 2_00_000
STRATEGIES    = ["BB Squeeze", "MA Pullback", "Supertrend", "Black Swan"]
MIN_CHUNKS    = [30_000, 40_000, 50_000, 60_000]
TRAIN_END     = "2020-12-31"
TEST_START    = "2021-01-01"
TEST_END      = "2026-06-14"
TRAIN_START   = "2016-01-01"

SHORT = {"BB Squeeze": "BB", "MA Pullback": "MA", "Supertrend": "ST", "Black Swan": "Swan"}


def run(all_trades: dict, min_chunk: int, priority: dict[str, int],
        start: str, end: str) -> dict:

    relevant = []
    for sn in STRATEGIES:
        for t in all_trades.get(sn, []):
            tc = copy.copy(t)
            if tc.get("entry_date", "") >= start and tc.get("exit_date", "") <= end:
                relevant.append(tc)

    valid = [t for t in relevant if t.get("entry_date") and t.get("exit_date")]
    valid.sort(key=lambda x: x["entry_date"])
    if not valid:
        return {"cagr": 0.0, "sharpe": 0.0, "max_dd": 0.0, "calmar": 0.0}

    entries_by_date: dict[str, list] = defaultdict(list)
    for t in valid:
        entries_by_date[t["entry_date"]].append(t)

    all_dates = [str(d.date()) for d in pd.date_range(start, end, freq="B")]

    free_cash       = float(TOTAL_CAPITAL)
    active: list    = []
    yearly_pnl: dict = defaultdict(float)
    realized_equity = float(TOTAL_CAPITAL)
    peak_equity     = float(TOTAL_CAPITAL)
    max_dd_pct      = 0.0
    total_pnl       = 0.0

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

        signals.sort(key=lambda t: priority.get(t.get("strategy", ""), 0), reverse=True)

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

        if not signals or free_cash < min_chunk:
            continue

        max_slots = int(free_cash // min_chunk)
        selected  = signals[:max_slots]
        chunk     = free_cash / len(selected)

        for t in selected:
            qty = max(1, int(chunk / t["entry_price"]))
            free_cash -= qty * t["entry_price"]
            active.append({"trade": t, "actual_qty": qty, "entry_price": t["entry_price"]})

    rets = []
    running = float(TOTAL_CAPITAL)
    for yr in sorted(yearly_pnl):
        pnl_yr = yearly_pnl[yr]
        rets.append((pnl_yr / running) * 100)
        running += pnl_yr

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
    }


def main() -> None:
    all_trades = sim.collect_all_trades()

    all_perms = list(itertools.permutations(STRATEGIES))

    print("\nRunning all 96 combinations on train (2016–2020) and test (2021–2026)...")

    results = []
    for mc in MIN_CHUNKS:
        for perm in all_perms:
            priority = {s: (4 - i) for i, s in enumerate(perm)}
            label    = " > ".join(SHORT[s] for s in perm)
            tr = run(all_trades, mc, priority, TRAIN_START, TRAIN_END)
            te = run(all_trades, mc, priority, TEST_START, TEST_END)
            results.append({
                "mc": mc, "label": label, "perm": perm,
                "train": tr, "test": te,
            })

    # ── Rank by train Sharpe, show test performance ───────────────────────────
    results.sort(key=lambda x: x["train"]["sharpe"], reverse=True)

    print("\n" + "═" * 100)
    print("RANKED BY TRAIN SHARPE — does the ranking hold out-of-sample?")
    print("═" * 100)
    print(f"{'Rank':<5} {'Chunk':>7}  {'Priority Order':42}  "
          f"{'TRAIN':>18}  {'TEST':>18}")
    print(f"{'':5} {'':7}  {'':42}  "
          f"{'CAGR  Sharpe':>18}  {'CAGR  Sharpe':>18}")
    print("─" * 100)

    for i, r in enumerate(results[:20]):
        is_current = r["perm"] == ("Supertrend", "MA Pullback", "BB Squeeze", "Black Swan") and r["mc"] == 40_000
        marker = " ◄ current" if is_current else ""
        print(
            f"  {i+1:<3} ₹{r['mc']//1000:>2}k  {r['label']:42}  "
            f"  {r['train']['cagr']:>5.1f}%  {r['train']['sharpe']:>5.3f}  "
            f"  {r['test']['cagr']:>5.1f}%  {r['test']['sharpe']:>5.3f}{marker}"
        )

    # ── Rank by test Sharpe ───────────────────────────────────────────────────
    results.sort(key=lambda x: x["test"]["sharpe"], reverse=True)

    print("\n" + "═" * 100)
    print("RANKED BY TEST SHARPE — what actually worked OOS?")
    print("═" * 100)
    print(f"{'Rank':<5} {'Chunk':>7}  {'Priority Order':42}  "
          f"{'TRAIN':>18}  {'TEST':>18}")
    print(f"{'':5} {'':7}  {'':42}  "
          f"{'CAGR  Sharpe':>18}  {'CAGR  Sharpe':>18}")
    print("─" * 100)

    for i, r in enumerate(results[:20]):
        is_current = r["perm"] == ("Supertrend", "MA Pullback", "BB Squeeze", "Black Swan") and r["mc"] == 40_000
        marker = " ◄ current" if is_current else ""
        print(
            f"  {i+1:<3} ₹{r['mc']//1000:>2}k  {r['label']:42}  "
            f"  {r['train']['cagr']:>5.1f}%  {r['train']['sharpe']:>5.3f}  "
            f"  {r['test']['cagr']:>5.1f}%  {r['test']['sharpe']:>5.3f}{marker}"
        )

    # ── Stability check: top-10 in-sample, where do they rank OOS? ───────────
    results.sort(key=lambda x: x["train"]["sharpe"], reverse=True)
    top10_train = results[:10]

    results_by_test = sorted(results, key=lambda x: x["test"]["sharpe"], reverse=True)
    test_rank = {(r["mc"], r["label"]): i + 1 for i, r in enumerate(results_by_test)}

    print("\n" + "═" * 70)
    print("STABILITY — top 10 in-sample, their out-of-sample rank")
    print("═" * 70)
    print(f"{'Train rank':<12} {'OOS rank':<12} {'Chunk':>7}  Priority")
    print("─" * 70)
    for i, r in enumerate(top10_train):
        oos_r = test_rank[(r["mc"], r["label"])]
        stability = "✓ stable" if oos_r <= 20 else "✗ degraded"
        print(f"  #{i+1:<9}  #{oos_r:<9}  ₹{r['mc']//1000:>2}k  {r['label']}  {stability}")

    # ── Best robust params (good on both train AND test) ─────────────────────
    results_both = sorted(results, key=lambda x: x["train"]["sharpe"] + x["test"]["sharpe"], reverse=True)

    print("\n" + "═" * 100)
    print("MOST ROBUST — ranked by (train Sharpe + test Sharpe) combined")
    print("═" * 100)
    print(f"{'Rank':<5} {'Chunk':>7}  {'Priority Order':42}  "
          f"{'TRAIN':>18}  {'TEST':>18}  {'Combined':>10}")
    print("─" * 100)

    for i, r in enumerate(results_both[:10]):
        combined = r["train"]["sharpe"] + r["test"]["sharpe"]
        is_current = r["perm"] == ("Supertrend", "MA Pullback", "BB Squeeze", "Black Swan") and r["mc"] == 40_000
        marker = " ◄ current" if is_current else ""
        print(
            f"  {i+1:<3} ₹{r['mc']//1000:>2}k  {r['label']:42}  "
            f"  {r['train']['cagr']:>5.1f}%  {r['train']['sharpe']:>5.3f}  "
            f"  {r['test']['cagr']:>5.1f}%  {r['test']['sharpe']:>5.3f}  "
            f"  {combined:>8.3f}{marker}"
        )


if __name__ == "__main__":
    main()
