"""First-Hour Momentum diagnostic and out-of-sample validation script.

Loads fills from reports/first_hour_momentum_report.json, pairs them into
completed round-trip trades, and produces breakdowns by:
  - symbol, month, quarter, day of week, side, entry-hour bucket

Also runs a train/test split (configurable dates) and reports:
  - all-symbols train vs test
  - per-symbol train vs test
  - whether any single symbol is net-positive out-of-sample
  - whether excluding the worst symbols improves test P&L

If no report JSON is found, the script runs a backtest with the "best"
configuration discovered from the parameter sweep and saves the report first.

No live trading.  No broker API calls.  No credentials required.

Usage:
    python3 scripts/analyze_first_hour_momentum.py
    python3 scripts/analyze_first_hour_momentum.py --symbols RELIANCE TCS
    python3 scripts/analyze_first_hour_momentum.py --rerun
    python3 scripts/analyze_first_hour_momentum.py --output-dir /tmp/out
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402 — after sys.path patch

from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.data_feed import HistoricalDataFeed  # noqa: E402
from trading_engine.backtest.engine import BacktestEngine  # noqa: E402
from trading_engine.backtest.portfolio import BacktestPortfolio  # noqa: E402
from trading_engine.backtest.simulated_broker import SimulatedBroker  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.strategies.first_hour_momentum import (  # noqa: E402
    FirstHourMomentumConfig,
    FirstHourMomentumStrategy,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_REPORT = ROOT / "reports" / "first_hour_momentum_report.json"
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

# Best config from the parameter sweep (lowest drawdown, highest PF at
# adequate trade count):
_BEST_CONFIG_PARAMS: dict[str, Any] = {
    "momentum_window_minutes": 15,
    "min_first_window_return_bps": 60.0,
    "stop_loss_bps": 60.0,
    "target_bps": None,
    "latest_entry_time": time(10, 30),
    "allow_shorts": False,
    "max_trades_per_symbol_per_day": 1,
}

# Train/test date boundaries (inclusive on both ends).
_DEFAULT_TRAIN_START = date(2025, 1, 1)
_DEFAULT_TRAIN_END = date(2025, 9, 30)
_DEFAULT_TEST_START = date(2025, 10, 1)
_DEFAULT_TEST_END = date(2026, 1, 31)

_ZERO = Decimal("0")

# ---------------------------------------------------------------------------
# Fill loading
# ---------------------------------------------------------------------------


def load_fills_from_report(report_path: Path) -> list[dict]:
    """Load the 'fills' list from a saved BacktestReport JSON.

    Each returned dict has these keys (types already converted):
        symbol       str
        side         str  ("BUY" or "SELL")
        quantity     int
        price        Decimal
        fees         Decimal
        timestamp    datetime (timezone-aware UTC)

    Returns an empty list if the path does not exist or has no fills.
    """
    if not report_path.exists():
        return []
    try:
        with report_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] Could not read {report_path}: {exc}")
        return []

    raw_fills = data.get("fills", [])
    fills: list[dict] = []
    for f in raw_fills:
        try:
            ts_raw = f["timestamp"]
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is not None:
                    ts = ts.astimezone(UTC)
            else:
                ts = ts_raw
            fills.append(
                {
                    "symbol": f["symbol"],
                    "side": f["side"],
                    "quantity": int(f["quantity"]),
                    "price": Decimal(str(f["price"])),
                    "fees": Decimal(str(f["fees"])),
                    "timestamp": ts,
                }
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] Skipping malformed fill: {exc}")
    return fills


# ---------------------------------------------------------------------------
# Round-trip pairing
# ---------------------------------------------------------------------------


def pair_fills(fills: list[dict]) -> list[dict]:
    """Pair BUY/SELL fills into completed round-trip trade dicts.

    Uses a FIFO queue per symbol.  Each returned trade dict has:
        symbol          str
        side            str  ("LONG" or "SHORT")
        entry_time      datetime
        exit_time       datetime
        entry_price     Decimal
        exit_price      Decimal
        quantity        int
        gross_pnl       Decimal  (revenue − cost, before fees)
        entry_fees      Decimal
        exit_fees       Decimal
        net_pnl         Decimal  (gross_pnl − entry_fees − exit_fees)
        total_fees      Decimal  (entry_fees + exit_fees)

    Consistency: net_pnl + total_fees == gross_pnl (within Decimal precision).
    """
    buy_queue: dict[str, list[dict]] = defaultdict(list)
    trades: list[dict] = []

    for fill in fills:
        sym = fill["symbol"]
        if fill["side"] == "BUY":
            buy_queue[sym].append(fill)
        elif fill["side"] == "SELL":
            remaining = fill["quantity"]
            cost = _ZERO
            entry_time = fill["timestamp"]
            entry_fees = _ZERO
            new_queue: list[dict] = []

            for buy in buy_queue[sym]:
                if remaining <= 0:
                    new_queue.append(buy)
                    continue
                used = min(buy["quantity"], remaining)
                cost += Decimal(str(used)) * buy["price"]
                entry_fees += buy["fees"] * Decimal(str(used)) / Decimal(str(buy["quantity"]))
                entry_time = buy["timestamp"]  # take first matching buy's time
                remaining -= used
                leftover = buy["quantity"] - used
                if leftover > 0:
                    leftover_buy = dict(buy)
                    leftover_buy["quantity"] = leftover
                    leftover_buy["fees"] = (
                        buy["fees"] * Decimal(str(leftover)) / Decimal(str(buy["quantity"]))
                    )
                    new_queue.append(leftover_buy)

            buy_queue[sym] = new_queue
            qty = fill["quantity"]
            revenue = Decimal(str(qty)) * fill["price"]
            gross = revenue - cost
            exit_fees = fill["fees"]
            total_fees = entry_fees + exit_fees
            net = gross - total_fees
            trades.append(
                {
                    "symbol": sym,
                    "side": "LONG",  # shorts not implemented in v1
                    "entry_time": entry_time,
                    "exit_time": fill["timestamp"],
                    "entry_price": cost / Decimal(str(qty)) if qty else _ZERO,
                    "exit_price": fill["price"],
                    "quantity": qty,
                    "gross_pnl": gross,
                    "entry_fees": entry_fees,
                    "exit_fees": exit_fees,
                    "total_fees": total_fees,
                    "net_pnl": net,
                }
            )

    return trades


# ---------------------------------------------------------------------------
# Per-group metrics
# ---------------------------------------------------------------------------


def _safe_float(v: Decimal | float | None) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def compute_metrics(trades: list[dict]) -> dict:
    """Compute aggregate metrics for a list of round-trip trades.

    Returns a dict with:
        trade_count     int  (round-trips)
        total_net_pnl   float
        total_gross_pnl float
        total_fees      float
        win_rate        float | None
        profit_factor   float | None  (None if no losing trades)
        avg_net_pnl     float | None
        avg_gross_pnl   float | None
        max_drawdown    float | None
        warning         str | None
    """
    n = len(trades)
    if n == 0:
        return {
            "trade_count": 0,
            "total_net_pnl": 0.0,
            "total_gross_pnl": 0.0,
            "total_fees": 0.0,
            "win_rate": None,
            "profit_factor": None,
            "avg_net_pnl": None,
            "avg_gross_pnl": None,
            "max_drawdown": None,
            "warning": None,
        }

    net_pnls = [t["net_pnl"] for t in trades]
    gross_pnls = [t["gross_pnl"] for t in trades]
    fees = [t["total_fees"] for t in trades]

    total_net = sum(net_pnls, _ZERO)
    total_gross = sum(gross_pnls, _ZERO)
    total_fees_sum = sum(fees, _ZERO)

    wins = [p for p in net_pnls if p > _ZERO]
    losses = [p for p in net_pnls if p < _ZERO]

    win_rate: float | None = None
    profit_factor: float | None = None
    if n > 0:
        win_rate = len(wins) / n
    if losses:
        gross_loss = abs(sum(losses, _ZERO))
        if gross_loss > _ZERO:
            profit_factor = _safe_float(sum(wins, _ZERO) / gross_loss)

    # Drawdown from cumulative net P&L sequence
    max_dd: float | None = None
    cum = _ZERO
    peak = _ZERO
    dd = 0.0
    for p in net_pnls:
        cum += p
        if cum > peak:
            peak = cum
        if peak > _ZERO:
            dd = max(dd, float((peak - cum) / peak))
    max_dd = dd if trades else None

    warning: str | None = None
    if n < 30:
        warning = f"Small sample: only {n} trades — interpret results with caution."

    return {
        "trade_count": n,
        "total_net_pnl": _safe_float(total_net) or 0.0,
        "total_gross_pnl": _safe_float(total_gross) or 0.0,
        "total_fees": _safe_float(total_fees_sum) or 0.0,
        "win_rate": _safe_float(Decimal(str(win_rate))) if win_rate is not None else None,
        "profit_factor": profit_factor,
        "avg_net_pnl": _safe_float(total_net / Decimal(str(n))),
        "avg_gross_pnl": _safe_float(total_gross / Decimal(str(n))),
        "max_drawdown": max_dd,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# Breakdown helpers
# ---------------------------------------------------------------------------

_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _entry_date(trade: dict) -> date:
    return trade["entry_time"].date()


def _entry_month(trade: dict) -> str:
    d = trade["entry_time"]
    return f"{d.year}-{d.month:02d}"


def _entry_quarter(trade: dict) -> str:
    d = trade["entry_time"]
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


def _entry_dow(trade: dict) -> str:
    return _DOW_NAMES[trade["entry_time"].weekday()]


def _entry_hour(trade: dict) -> str:
    return f"{trade['entry_time'].hour:02d}:xx"


def _entry_side(trade: dict) -> str:
    return trade["side"]


def breakdown_by(trades: list[dict], key_fn: Any) -> dict[str, dict]:
    """Group trades by key_fn and compute metrics per group."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    return {k: compute_metrics(v) for k, v in sorted(groups.items())}


# ---------------------------------------------------------------------------
# Train / test split
# ---------------------------------------------------------------------------


def split_by_date(
    trades: list[dict],
    start: date,
    end: date,
) -> list[dict]:
    """Return trades whose entry date is in [start, end] inclusive."""
    return [t for t in trades if start <= _entry_date(t) <= end]


# ---------------------------------------------------------------------------
# Running a fresh backtest
# ---------------------------------------------------------------------------


def load_candles(symbols: list[str], data_dir: Path, interval: str) -> dict[str, pd.DataFrame]:
    """Load Parquet candle files for the given symbols."""
    candles: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        path = data_dir / "candles" / "NSE" / sym / f"{interval}.parquet"
        if not path.exists():
            print(f"  [skip] No data file for {sym} at {path}")
            continue
        try:
            df = pd.read_parquet(path)
            candles[sym] = df
            print(f"  Loaded {sym}: {len(df)} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] Cannot read {sym}: {exc}")
    return candles


def run_backtest(
    candles: dict[str, pd.DataFrame],
    params: dict,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
) -> list[dict]:
    """Run a backtest with params and return the raw fills list."""
    mwm = int(params.get("momentum_window_minutes", 30))
    latest_entry = params.get("latest_entry_time", time(12, 0))
    session_minutes = 9 * 60 + 15 + mwm
    earliest_entry = time(session_minutes // 60, session_minutes % 60)

    cfg = FirstHourMomentumConfig(
        strategy_id="fhm_analysis",
        quantity=quantity,
        momentum_window_minutes=mwm,
        earliest_entry_time=earliest_entry,
        latest_entry_time=latest_entry,
        min_bars_before_signal=mwm,
        min_first_window_return_bps=float(params.get("min_first_window_return_bps", 60.0)),
        stop_loss_bps=float(params.get("stop_loss_bps", 80.0)),
        target_bps=(float(params["target_bps"]) if params.get("target_bps") is not None else None),
        allow_shorts=bool(params.get("allow_shorts", False)),
        max_trades_per_symbol_per_day=int(params.get("max_trades_per_symbol_per_day", 1)),
    )

    strategy = FirstHourMomentumStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    broker = SimulatedBroker(portfolio, CostModel(), SlippageModel(bps=Decimal("2")))
    feed = HistoricalDataFeed(candles, interval=interval)
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=cfg.strategy_id,
        symbols=list(candles.keys()),
    )
    report = engine.run()

    fills = []
    for f in report.fills:
        ts = f.timestamp
        if ts.tzinfo is not None:
            ts = ts.astimezone(UTC)
        fills.append(
            {
                "symbol": f.symbol,
                "side": str(f.side),
                "quantity": f.quantity,
                "price": f.price,
                "fees": f.fees,
                "timestamp": ts,
            }
        )
    return fills


# ---------------------------------------------------------------------------
# Train / test analysis
# ---------------------------------------------------------------------------


def train_test_analysis(
    trades: list[dict],
    train_start: date,
    train_end: date,
    test_start: date,
    test_end: date,
    symbols: list[str],
) -> dict:
    """Run full train/test breakdown.

    Returns a dict with keys:
        all_symbols.train / .test
        by_symbol.<SYM>.train / .test
        best_test_symbol   str | None
        worst_test_symbol  str | None
        excluded_worst_test dict  (metrics after dropping worst train symbol)
        net_positive_oos   list[str]  (symbols positive in test)
    """
    train_trades = split_by_date(trades, train_start, train_end)
    test_trades = split_by_date(trades, test_start, test_end)

    result: dict[str, Any] = {
        "train_period": f"{train_start} to {train_end}",
        "test_period": f"{test_start} to {test_end}",
        "all_symbols": {
            "train": compute_metrics(train_trades),
            "test": compute_metrics(test_trades),
        },
        "by_symbol": {},
    }

    sym_test_pnl: dict[str, float] = {}
    sym_train_pnl: dict[str, float] = {}

    all_syms = symbols or sorted({t["symbol"] for t in trades})
    for sym in all_syms:
        sym_train = [t for t in train_trades if t["symbol"] == sym]
        sym_test = [t for t in test_trades if t["symbol"] == sym]
        result["by_symbol"][sym] = {
            "train": compute_metrics(sym_train),
            "test": compute_metrics(sym_test),
        }
        sym_test_pnl[sym] = result["by_symbol"][sym]["test"]["total_net_pnl"]
        sym_train_pnl[sym] = result["by_symbol"][sym]["train"]["total_net_pnl"]

    # Best / worst in test period
    if sym_test_pnl:
        best_sym = max(sym_test_pnl, key=sym_test_pnl.__getitem__)
        worst_sym = min(sym_test_pnl, key=sym_test_pnl.__getitem__)
    else:
        best_sym = worst_sym = None

    result["best_test_symbol"] = best_sym
    result["worst_test_symbol"] = worst_sym

    # Symbols that are net-positive out-of-sample
    result["net_positive_oos"] = [s for s, p in sym_test_pnl.items() if p > 0]

    # Would excluding worst *train* symbol improve test P&L?
    if sym_train_pnl:
        worst_train = min(sym_train_pnl, key=sym_train_pnl.__getitem__)
        excl_test = [t for t in test_trades if t["symbol"] != worst_train]
        result["excluded_worst_train_symbol"] = worst_train
        result["excluded_worst_test"] = compute_metrics(excl_test)
    else:
        result["excluded_worst_train_symbol"] = None
        result["excluded_worst_test"] = compute_metrics([])

    return result


# ---------------------------------------------------------------------------
# Full analysis
# ---------------------------------------------------------------------------


def analyze(
    fills: list[dict],
    symbols: list[str],
    train_start: date,
    train_end: date,
    test_start: date,
    test_end: date,
    config_params: dict,
) -> dict:
    """Run all diagnostics on the given fills.

    Returns a serialisable analysis dict.
    """
    trades = pair_fills(fills)

    analysis: dict[str, Any] = {
        "config": {k: str(v) for k, v in config_params.items()},
        "fill_count": len(fills),
        "trade_count": len(trades),
        "symbols": sorted({t["symbol"] for t in trades}),
    }

    if not trades:
        analysis["warning"] = "No completed trades found — nothing to analyse."
        return analysis

    analysis["overall"] = compute_metrics(trades)
    analysis["by_symbol"] = breakdown_by(trades, lambda t: t["symbol"])
    analysis["by_month"] = breakdown_by(trades, _entry_month)
    analysis["by_quarter"] = breakdown_by(trades, _entry_quarter)
    analysis["by_day_of_week"] = breakdown_by(trades, _entry_dow)
    analysis["by_side"] = breakdown_by(trades, _entry_side)
    analysis["by_entry_hour"] = breakdown_by(trades, _entry_hour)
    analysis["train_test"] = train_test_analysis(
        trades, train_start, train_end, test_start, test_end, symbols
    )

    return analysis


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def save_outputs(analysis: dict, output_dir: Path) -> tuple[Path, Path]:
    """Save analysis JSON and symbol-level CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "first_hour_momentum_analysis.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(analysis, fh, indent=2, default=_json_default_ext)

    # Symbol CSV — flatten by_symbol and train_test.by_symbol
    rows = []
    by_sym = analysis.get("by_symbol", {})
    tt = analysis.get("train_test", {}).get("by_symbol", {})
    all_syms = sorted(set(list(by_sym.keys()) + list(tt.keys())))
    for sym in all_syms:
        overall = by_sym.get(sym, {})
        train = tt.get(sym, {}).get("train", {})
        test = tt.get(sym, {}).get("test", {})
        rows.append(
            {
                "symbol": sym,
                "trade_count": overall.get("trade_count", 0),
                "total_net_pnl": overall.get("total_net_pnl"),
                "total_gross_pnl": overall.get("total_gross_pnl"),
                "total_fees": overall.get("total_fees"),
                "win_rate": overall.get("win_rate"),
                "profit_factor": overall.get("profit_factor"),
                "avg_net_pnl": overall.get("avg_net_pnl"),
                "max_drawdown": overall.get("max_drawdown"),
                "train_trade_count": train.get("trade_count", 0),
                "train_net_pnl": train.get("total_net_pnl"),
                "train_win_rate": train.get("win_rate"),
                "train_profit_factor": train.get("profit_factor"),
                "test_trade_count": test.get("trade_count", 0),
                "test_net_pnl": test.get("total_net_pnl"),
                "test_win_rate": test.get("win_rate"),
                "test_profit_factor": test.get("profit_factor"),
            }
        )
    csv_path = output_dir / "first_hour_momentum_symbol_results.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    return json_path, csv_path


def _json_default_ext(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(obj).__name__}")


# ---------------------------------------------------------------------------
# Printing summary
# ---------------------------------------------------------------------------

_SEP = "-" * 72


def _fmt_metrics(m: dict, prefix: str = "") -> str:
    if not m:
        return f"{prefix}  (no data)"
    tc = m.get("trade_count", 0)
    pnl = m.get("total_net_pnl", 0.0) or 0.0
    gross = m.get("total_gross_pnl", 0.0) or 0.0
    fees = m.get("total_fees", 0.0) or 0.0
    wr = m.get("win_rate")
    pf = m.get("profit_factor")
    dd = m.get("max_drawdown")
    warn = m.get("warning", "")
    lines = [
        f"{prefix}  trades={tc}  net={pnl:+.2f}  gross={gross:+.2f}  fees={fees:.2f}",
        f"{prefix}  win_rate={'n/a' if wr is None else f'{wr:.3f}'}"
        f"  profit_factor={'n/a' if pf is None else f'{pf:.4f}'}"
        f"  max_dd={'n/a' if dd is None else f'{dd:.4f}'}",
    ]
    if warn:
        lines.append(f"{prefix}  ⚠  {warn}")
    return "\n".join(lines)


def print_summary(analysis: dict) -> None:  # noqa: PLR0912
    """Print human-readable diagnostics."""
    print(f"\n{_SEP}")
    print("  FIRST-HOUR MOMENTUM — DIAGNOSTIC REPORT")
    print("  WARNING: All results are IN-SAMPLE for train set, OOS for test set.")
    print(_SEP)

    overall = analysis.get("overall", {})
    print(
        f"\nOverall ({analysis.get('fill_count', 0)} fills → "
        f"{analysis.get('trade_count', 0)} trades):"
    )
    print(_fmt_metrics(overall))

    # By symbol
    print(f"\n{'— By Symbol ':-<72}")
    by_sym = analysis.get("by_symbol", {})
    for sym, m in sorted(by_sym.items(), key=lambda x: -(x[1].get("total_net_pnl") or 0)):
        print(f"  {sym}:")
        print(_fmt_metrics(m, "  "))

    # By quarter
    print(f"\n{'— By Quarter ':-<72}")
    for q, m in analysis.get("by_quarter", {}).items():
        print(
            f"  {q}: net={m.get('total_net_pnl', 0.0):+.2f}  trades={m.get('trade_count', 0)}"
            f"  pf={m.get('profit_factor') or 0:.4f}"
        )

    # Train / test
    tt = analysis.get("train_test", {})
    if tt:
        print(f"\n{'— Train / Test Split ':-<72}")
        print(f"  Train period: {tt.get('train_period')}")
        print(f"  Test period:  {tt.get('test_period')}")
        print("\n  All symbols — TRAIN:")
        print(_fmt_metrics(tt.get("all_symbols", {}).get("train", {}), "  "))
        print("\n  All symbols — TEST (out-of-sample):")
        print(_fmt_metrics(tt.get("all_symbols", {}).get("test", {}), "  "))

        print("\n  Per-symbol train → test:")
        for sym, d in sorted(tt.get("by_symbol", {}).items()):
            train_pnl = d.get("train", {}).get("total_net_pnl", 0.0) or 0.0
            test_pnl = d.get("test", {}).get("total_net_pnl", 0.0) or 0.0
            train_tc = d.get("train", {}).get("trade_count", 0)
            test_tc = d.get("test", {}).get("trade_count", 0)
            warn = ""
            if test_tc < 10:
                warn = "  ⚠ too few OOS trades"
            print(
                f"    {sym:12s}  train={train_pnl:+8.2f} ({train_tc}t)"
                f"  test={test_pnl:+8.2f} ({test_tc}t){warn}"
            )

        positive = tt.get("net_positive_oos", [])
        if positive:
            print(f"\n  Symbols net-POSITIVE out-of-sample: {positive}")
        else:
            print("\n  No symbols are net-positive out-of-sample.")

        best = tt.get("best_test_symbol")
        worst = tt.get("worst_test_symbol")
        excl_worst_train = tt.get("excluded_worst_train_symbol")
        print(f"\n  Best OOS symbol:  {best}")
        print(f"  Worst OOS symbol: {worst}")

        if excl_worst_train:
            orig_test = tt.get("all_symbols", {}).get("test", {}).get("total_net_pnl") or 0.0
            excl_test = tt.get("excluded_worst_test", {}).get("total_net_pnl") or 0.0
            print(
                f"\n  Excluding worst train symbol ({excl_worst_train}):"
                f" test P&L {orig_test:+.2f} → {excl_test:+.2f}"
            )
            if excl_test > orig_test:
                print("  → Excluding the worst train symbol IMPROVES test P&L.")
            else:
                print("  → Excluding the worst train symbol does NOT improve test P&L.")

    print(f"\n{_SEP}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="First-Hour Momentum diagnostics and OOS validation."
    )
    parser.add_argument("--symbols", nargs="+", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument(
        "--report",
        default=str(_DEFAULT_REPORT),
        help="Path to the existing BacktestReport JSON to load fills from.",
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        default=False,
        help="Ignore existing report and re-run backtest with best config.",
    )
    parser.add_argument(
        "--train-start",
        dest="train_start",
        default=str(_DEFAULT_TRAIN_START),
        help="Train period start YYYY-MM-DD",
    )
    parser.add_argument(
        "--train-end",
        dest="train_end",
        default=str(_DEFAULT_TRAIN_END),
        help="Train period end YYYY-MM-DD",
    )
    parser.add_argument(
        "--test-start",
        dest="test_start",
        default=str(_DEFAULT_TEST_START),
        help="Test period start YYYY-MM-DD",
    )
    parser.add_argument(
        "--test-end",
        dest="test_end",
        default=str(_DEFAULT_TEST_END),
        help="Test period end YYYY-MM-DD",
    )
    parser.add_argument("--output-dir", dest="output_dir", default=str(_DEFAULT_OUTPUT_DIR))
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    report_path = Path(args.report)

    train_start = date.fromisoformat(args.train_start)
    train_end = date.fromisoformat(args.train_end)
    test_start = date.fromisoformat(args.test_start)
    test_end = date.fromisoformat(args.test_end)

    print("\nFirst-Hour Momentum Diagnostics")
    print(f"  Symbols:      {args.symbols}")
    print(f"  Data dir:     {data_dir}")
    print(f"  Report path:  {report_path}")
    print(f"  Train:        {train_start} → {train_end}")
    print(f"  Test:         {test_start} → {test_end}")

    fills: list[dict] = []
    if not args.rerun and report_path.exists():
        print(f"\nLoading fills from {report_path} ...")
        fills = load_fills_from_report(report_path)
        print(f"  Loaded {len(fills)} fills.")
    else:
        print("\nRunning backtest with best config ...")
        candles = load_candles(args.symbols, data_dir, args.interval)
        if not candles:
            print("No candle data found. Download historical data first.\n")
            return
        fills = run_backtest(
            candles,
            _BEST_CONFIG_PARAMS,
            Decimal("500000"),
            _DEFAULT_QUANTITY,
            args.interval,
        )
        print(f"  Backtest produced {len(fills)} fills.")

    if not fills:
        print("\nNo fills to analyse.\n")
        return

    analysis = analyze(
        fills,
        symbols=args.symbols,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        config_params=_BEST_CONFIG_PARAMS,
    )

    print_summary(analysis)

    json_path, csv_path = save_outputs(analysis, output_dir)
    print("Saved outputs to:")
    print(f"  {json_path}")
    print(f"  {csv_path}")


if __name__ == "__main__":
    main()
