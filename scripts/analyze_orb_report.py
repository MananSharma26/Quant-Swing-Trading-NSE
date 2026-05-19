"""Analyze an ORB backtest report to understand P&L drivers.

Loads reports/orb_backtest_report.json (or a path passed via --report),
reconstructs trades from fill pairs, and produces breakdowns by:
  - symbol
  - month
  - day of week
  - entry hour (time bucket)
  - exit reason (if available)

For each breakdown it calculates:
  trade count, gross P&L, total fees, net P&L, win rate,
  avg win, avg loss, profit factor, avg holding time (minutes).

Saves full output to reports/orb_analysis.json and prints a concise
terminal summary.

Usage:
    python3 scripts/analyze_orb_report.py
    python3 scripts/analyze_orb_report.py --report path/to/report.json
    python3 scripts/analyze_orb_report.py --out path/to/output.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_DEFAULT_REPORT = ROOT / "reports" / "orb_backtest_report.json"
_DEFAULT_OUT = ROOT / "reports" / "orb_analysis.json"

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# Trade reconstruction
# ---------------------------------------------------------------------------


def reconstruct_trades(fills: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Pair BUY and SELL fills per symbol (FIFO) into round-trip trades.

    Returns:
        (trades, warnings)  where each trade is a dict with:
          symbol, entry_price, exit_price, quantity, gross_pnl,
          fees, net_pnl, entry_ts, exit_ts, holding_minutes, exit_reason
    """
    warnings: list[str] = []
    # Queue of open BUY fills per symbol.
    open_buys: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Queue of open SELL fills per symbol (short side, if ever present).
    open_sells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trades: list[dict[str, Any]] = []

    sorted_fills = sorted(fills, key=lambda f: f.get("timestamp", ""))

    for fill in sorted_fills:
        symbol = fill.get("symbol", "UNKNOWN")
        side = fill.get("side", "").upper()
        qty = int(fill.get("quantity", 0))
        price = float(fill.get("price", 0))
        fees = float(fill.get("fees", 0))
        ts_raw = fill.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = None

        if side == "BUY":
            open_buys[symbol].append(
                {"price": price, "fees": fees, "qty": qty, "ts": ts, "exit_reason": None}
            )
        elif side == "SELL":
            if open_buys[symbol]:
                buy = open_buys[symbol].pop(0)
                gross = (price - buy["price"]) * qty
                total_fees = fees + buy["fees"]
                holding = None
                if ts is not None and buy["ts"] is not None:
                    holding = (ts - buy["ts"]).total_seconds() / 60.0
                trades.append(
                    {
                        "symbol": symbol,
                        "entry_price": buy["price"],
                        "exit_price": price,
                        "quantity": qty,
                        "gross_pnl": round(gross, 2),
                        "fees": round(total_fees, 2),
                        "net_pnl": round(gross - total_fees, 2),
                        "entry_ts": buy["ts"],
                        "exit_ts": ts,
                        "holding_minutes": round(holding, 1) if holding is not None else None,
                        "exit_reason": fill.get("exit_reason"),
                    }
                )
            else:
                # Short sell or unmatched — record as open_sells for now.
                open_sells[symbol].append({"price": price, "fees": fees, "qty": qty, "ts": ts})

    # Warn about unmatched fills.
    unmatched_buys = sum(len(v) for v in open_buys.values())
    unmatched_sells = sum(len(v) for v in open_sells.values())
    if unmatched_buys:
        warnings.append(
            f"{unmatched_buys} BUY fill(s) have no matching SELL — excluded from trades."
        )
    if unmatched_sells:
        warnings.append(
            f"{unmatched_sells} SELL fill(s) have no matching BUY — excluded from trades."
        )

    return trades, warnings


# ---------------------------------------------------------------------------
# Breakdown computation
# ---------------------------------------------------------------------------


def _trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate stats for a list of trades."""
    if not trades:
        return {
            "trade_count": 0,
            "gross_pnl": 0.0,
            "total_fees": 0.0,
            "net_pnl": 0.0,
            "win_rate": None,
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
            "avg_holding_minutes": None,
        }

    gross = sum(t["gross_pnl"] for t in trades)
    fees = sum(t["fees"] for t in trades)
    net = sum(t["net_pnl"] for t in trades)
    wins = [t["net_pnl"] for t in trades if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in trades if t["net_pnl"] <= 0]

    holding_vals = [t["holding_minutes"] for t in trades if t["holding_minutes"] is not None]
    avg_hold = round(sum(holding_vals) / len(holding_vals), 1) if holding_vals else None

    total_win = sum(wins)
    total_loss = abs(sum(losses))
    pf = round(total_win / total_loss, 4) if total_loss > 0 else None

    return {
        "trade_count": len(trades),
        "gross_pnl": round(gross, 2),
        "total_fees": round(fees, 2),
        "net_pnl": round(net, 2),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_win": round(total_win / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": pf,
        "avg_holding_minutes": avg_hold,
    }


def _group_by(trades: list[dict[str, Any]], key_fn) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        groups[k].append(t)
    return dict(groups)


def compute_breakdowns(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute all required breakdowns. Returns a dict of breakdown_name → stats."""

    def by_symbol(t):
        return t["symbol"]

    def by_month(t):
        ts = t["entry_ts"]
        return ts.strftime("%Y-%m") if ts else "unknown"

    def by_dow(t):
        ts = t["entry_ts"]
        if ts is None:
            return "unknown"
        return _DAY_NAMES[ts.weekday()]

    def by_hour(t):
        ts = t["entry_ts"]
        if ts is None:
            return "unknown"
        # Convert to IST (+05:30) if tz-aware, else assume IST.
        if ts.tzinfo is not None:
            ist_offset = UTC
            ts_local = ts.astimezone(ist_offset)
            # +5:30 = 330 minutes
            from datetime import timedelta

            ts_local = ts + (ts.utcoffset() or timedelta(0))
            ts_local = ts.replace(tzinfo=None) + (ts.utcoffset() or timedelta(0))
        else:
            ts_local = ts
        return f"{ts_local.hour:02d}:00"

    def by_exit_reason(t):
        return t.get("exit_reason") or "unknown"

    def stats_for(key_fn) -> dict[str, Any]:
        groups = _group_by(trades, key_fn)
        return {k: _trade_stats(v) for k, v in sorted(groups.items())}

    return {
        "by_symbol": stats_for(by_symbol),
        "by_month": stats_for(by_month),
        "by_day_of_week": stats_for(by_dow),
        "by_entry_hour": stats_for(by_hour),
        "by_exit_reason": stats_for(by_exit_reason),
        "overall": _trade_stats(trades),
    }


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------


def print_summary(
    report: dict[str, Any],
    trades: list[dict[str, Any]],
    breakdowns: dict[str, Any],
    warnings: list[str],
    missing_fields: list[str],
) -> None:
    metrics = report.get("metrics", {})

    print("\n" + "=" * 60)
    print("ORB BACKTEST ANALYSIS SUMMARY")
    print("=" * 60)

    # Overall
    print(f"\nPeriod:         {report.get('start_time', '')} → {report.get('end_time', '')}")
    print(f"Symbols:        {report.get('symbols', [])}")
    print(f"Initial cash:   {report.get('initial_cash', '')}")
    print(f"Final equity:   {report.get('final_equity', '')}")
    print(f"Net P&L:        {metrics.get('total_pnl', '')}")
    print(f"Total fees:     {metrics.get('total_fees', '')}")
    print(f"Win rate:       {float(metrics.get('win_rate', 0)):.1%}")
    print(f"Profit factor:  {metrics.get('profit_factor', '')}")
    print(f"Max drawdown:   {float(metrics.get('max_drawdown', 0)):.2%}")
    print(f"Trades (reconstructed): {len(trades)}")

    # Fees vs gross edge
    overall = breakdowns.get("overall", {})
    gross = overall.get("gross_pnl", 0)
    total_fees = overall.get("total_fees", 0)
    print(f"\nGross edge:     {gross:+.2f}")
    print(f"Total fees:     {total_fees:.2f}")
    if gross is not None and total_fees > 0:
        if total_fees > abs(gross):
            print("  *** FEES EXCEED GROSS EDGE — fees are the primary cause of loss ***")
        elif gross < 0:
            print("  *** Strategy has negative gross edge even before fees ***")
        else:
            print("  Strategy has positive gross edge; fees push it negative.")

    # Best/worst by symbol
    sym_stats = breakdowns.get("by_symbol", {})
    if sym_stats:
        sym_sorted = sorted(sym_stats.items(), key=lambda x: x[1]["net_pnl"] or 0)
        worst_sym = sym_sorted[0]
        best_sym = sym_sorted[-1]
        print(
            f"\nBest symbol:    {best_sym[0]} net={best_sym[1]['net_pnl']:+.2f}"
            f"  trades={best_sym[1]['trade_count']}"
        )
        print(
            f"Worst symbol:   {worst_sym[0]} net={worst_sym[1]['net_pnl']:+.2f}"
            f"  trades={worst_sym[1]['trade_count']}"
        )

    # Best/worst by month
    month_stats = breakdowns.get("by_month", {})
    if month_stats:
        m_sorted = sorted(month_stats.items(), key=lambda x: x[1]["net_pnl"] or 0)
        worst_m = m_sorted[0]
        best_m = m_sorted[-1]
        print(
            f"\nBest month:     {best_m[0]} net={best_m[1]['net_pnl']:+.2f}"
            f"  trades={best_m[1]['trade_count']}"
        )
        print(
            f"Worst month:    {worst_m[0]} net={worst_m[1]['net_pnl']:+.2f}"
            f"  trades={worst_m[1]['trade_count']}"
        )

    # Top 5 largest losing trades
    losing = sorted(trades, key=lambda t: t["net_pnl"])[:5]
    if losing:
        print("\nTop 5 largest losing trades:")
        for t in losing:
            ts_str = t["entry_ts"].isoformat() if t["entry_ts"] else "?"
            print(
                f"  {t['symbol']:12s} {ts_str}  net={t['net_pnl']:+.2f}"
                f"  hold={t['holding_minutes']}min"
            )

    # Warnings / missing fields
    if missing_fields:
        print(f"\n[WARN] Missing report fields: {missing_fields}")
    if warnings:
        print("\n[WARN] Reconstruction warnings:")
        for w in warnings:
            print(f"  {w}")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _check_missing_fields(report: dict[str, Any]) -> list[str]:
    """Report fields we'd like but that are absent."""
    missing = []
    if not report.get("fills"):
        missing.append("fills")
    if not report.get("trades"):
        missing.append("trades (will reconstruct from fills — exit_reason will be unavailable)")
    metrics = report.get("metrics", {})
    for f in ("total_pnl", "total_fees", "win_rate", "profit_factor"):
        if f not in metrics:
            missing.append(f"metrics.{f}")
    return missing


def analyze(report_path: Path) -> dict[str, Any]:
    """Load a report, reconstruct trades, compute breakdowns. Returns analysis dict."""
    with report_path.open() as fh:
        report = json.load(fh)

    missing_fields = _check_missing_fields(report)
    for mf in missing_fields:
        print(f"[WARN] Missing field: {mf}")

    fills = report.get("fills", [])
    if not fills:
        print("[ERROR] No fills in report — cannot reconstruct trades.")
        return {"error": "no fills", "missing_fields": missing_fields}

    trades, warnings = reconstruct_trades(fills)
    breakdowns = compute_breakdowns(trades)

    # Serialise for JSON (datetimes → isoformat, None stays None).
    def _serialise(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        return obj

    trades_serial = [{k: _serialise(v) for k, v in t.items()} for t in trades]

    analysis = {
        "source_report": str(report_path),
        "strategy_id": report.get("strategy_id"),
        "symbols": report.get("symbols"),
        "period": {
            "start": report.get("start_time"),
            "end": report.get("end_time"),
        },
        "source_metrics": report.get("metrics"),
        "missing_fields": missing_fields,
        "reconstruction_warnings": warnings,
        "reconstructed_trade_count": len(trades),
        "breakdowns": breakdowns,
        "trades": trades_serial,
    }

    print_summary(report, trades, breakdowns, warnings, missing_fields)
    return analysis


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze an ORB backtest report.")
    parser.add_argument(
        "--report",
        default=str(_DEFAULT_REPORT),
        help=f"Path to the backtest JSON report (default: {_DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_OUT),
        help=f"Where to save the analysis JSON (default: {_DEFAULT_OUT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report_path = Path(args.report)
    out_path = Path(args.out)

    if not report_path.exists():
        print(f"[ERROR] Report not found: {report_path}")
        return 1

    analysis = analyze(report_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        json.dump(analysis, fh, indent=2, default=str)
    print(f"\nAnalysis saved → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
