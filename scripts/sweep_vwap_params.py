"""VWAP Trend Pullback parameter sweep.

Runs BacktestEngine over a grid of VWAPPullbackConfig parameters using locally
stored Parquet candle data.  Results are saved to CSV and JSON.

No live trading.  No broker API calls.  No credentials required.

Usage:
    python3 scripts/sweep_vwap_params.py
    python3 scripts/sweep_vwap_params.py --symbols RELIANCE TCS --max-combinations 50
    python3 scripts/sweep_vwap_params.py --output-dir /tmp/results

WARNING: all results are IN-SAMPLE only.  Do not use to size or place live trades.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import time
from decimal import Decimal
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402 — after sys.path patch

from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.data_feed import HistoricalDataFeed  # noqa: E402
from trading_engine.backtest.engine import BacktestEngine  # noqa: E402
from trading_engine.backtest.portfolio import BacktestPortfolio  # noqa: E402
from trading_engine.backtest.simulated_broker import SimulatedBroker  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.strategies.vwap_pullback import (  # noqa: E402
    VWAPPullbackConfig,
    VWAPTrendPullbackStrategy,
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Parameter grid
# ---------------------------------------------------------------------------

PARAM_GRID: dict[str, list] = {
    "pullback_tolerance_bps": [5, 10, 20],
    "stop_loss_bps": [30, 40, 60],
    "target_bps": [60, 80, 120],
    "no_new_entries_after": [time(11, 30), time(13, 0), time(14, 30)],
    "max_trades_per_symbol_per_day": [1],
    "vwap_slope_lookback_bars": [5, 10, 15],
}
# 3 * 3 * 3 * 3 * 1 * 3 = 243 total combinations.


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------


def build_grid(
    grid: dict[str, list] | None = None,
    max_combinations: int | None = None,
) -> list[dict]:
    """Return list of parameter dicts for the full (or truncated) Cartesian product.

    Args:
        grid:             Custom grid dict; defaults to PARAM_GRID.
        max_combinations: If given, return only the first N combinations.

    Returns:
        List of dicts, each mapping parameter name → value.
    """
    g = grid if grid is not None else PARAM_GRID
    keys = list(g.keys())
    combos = [dict(zip(keys, combo, strict=True)) for combo in product(*g.values())]
    if max_combinations is not None and max_combinations < len(combos):
        combos = combos[:max_combinations]
    return combos


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_candles(
    symbols: list[str],
    data_dir: Path,
    interval: str,
) -> dict[str, pd.DataFrame]:
    """Load Parquet candle files for each symbol; skip missing or unreadable ones.

    Returns only the symbols that were successfully loaded.
    """
    candles: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = data_dir / "candles" / "NSE" / symbol / f"{interval}.parquet"
        if not path.exists():
            print(f"  [skip] No data file for {symbol} at {path}")
            continue
        try:
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] Failed to read {symbol}: {exc}")
    return candles


# ---------------------------------------------------------------------------
# Single-run helper
# ---------------------------------------------------------------------------


def _params_to_row(params: dict) -> dict:
    """Convert a params dict to a JSON-serialisable flat dict."""
    row = {}
    for k, v in params.items():
        row[k] = str(v) if isinstance(v, time) else v
    return row


def _safe_float(value) -> float | None:
    """Convert Decimal/float to float; return None for NaN or inf."""
    if value is None:
        return None
    try:
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def run_single(
    candles: dict[str, pd.DataFrame],
    params: dict,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
    run_index: int = 0,
) -> dict:
    """Run one backtest with the given params; return a result row dict.

    On config validation failure the row contains an ``error`` key and all
    metric fields are None.
    """
    no_new_entries_after = params.get("no_new_entries_after", time(14, 30))
    try:
        cfg = VWAPPullbackConfig(
            strategy_id=f"sweep_{run_index}",
            quantity=quantity,
            pullback_tolerance_bps=float(params.get("pullback_tolerance_bps", 20.0)),
            stop_loss_bps=float(params.get("stop_loss_bps", 40.0)),
            target_bps=float(params.get("target_bps", 80.0)),
            no_new_entries_after=no_new_entries_after,
            max_trades_per_symbol_per_day=int(params.get("max_trades_per_symbol_per_day", 1)),
            vwap_slope_lookback_bars=int(params.get("vwap_slope_lookback_bars", 5)),
        )
    except ValueError as exc:
        return {
            **_params_to_row(params),
            "error": str(exc),
            "total_return": None,
            "total_pnl": None,
            "gross_pnl": None,
            "total_fees": None,
            "max_drawdown": None,
            "win_rate": None,
            "profit_factor": None,
            "trade_count": None,
            "average_trade_pnl": None,
            "sharpe_ratio": None,
            "sortino_ratio": None,
        }

    strategy = VWAPTrendPullbackStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed(candles, interval=interval)
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=cfg.strategy_id,
        symbols=list(candles.keys()),
        parameters={k: str(v) for k, v in params.items()},
    )

    report = engine.run()
    m = report.metrics

    row = _params_to_row(params)
    row["error"] = None
    row["total_return"] = _safe_float(m.total_return)
    row["total_pnl"] = _safe_float(m.total_pnl)
    row["gross_pnl"] = _safe_float(m.realized_pnl)
    row["total_fees"] = _safe_float(m.total_fees)
    row["max_drawdown"] = _safe_float(m.max_drawdown)
    row["win_rate"] = _safe_float(m.win_rate)
    row["profit_factor"] = _safe_float(m.profit_factor)
    row["trade_count"] = m.trade_count
    row["average_trade_pnl"] = _safe_float(m.average_trade_pnl)
    row["sharpe_ratio"] = _safe_float(m.sharpe_ratio)
    row["sortino_ratio"] = _safe_float(m.sortino_ratio)
    return row


# ---------------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------------


def run_sweep(
    candles: dict[str, pd.DataFrame],
    combos: list[dict],
    initial_cash: Decimal,
    quantity: int,
    interval: str,
) -> list[dict]:
    """Run all parameter combinations; return list of result rows."""
    results: list[dict] = []
    total = len(combos)
    for i, params in enumerate(combos, start=1):
        label = _params_to_row(params)
        print(f"  [{i:3d}/{total}] {label} ...", end=" ", flush=True)
        row = run_single(candles, params, initial_cash, quantity, interval, run_index=i)
        if row.get("error"):
            print(f"SKIP ({row['error'][:60]})")
        else:
            pnl = row.get("total_pnl")
            trades = row.get("trade_count", 0)
            print(f"pnl={pnl:+.2f}  trades={trades}")
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Results output
# ---------------------------------------------------------------------------


def save_results(results: list[dict], output_dir: Path) -> tuple[Path, Path]:
    """Save results to CSV and JSON under output_dir.

    Returns:
        (csv_path, json_path)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "vwap_sweep_results.csv"
    json_path = output_dir / "vwap_sweep_results.json"

    df = pd.DataFrame(results)
    df.to_csv(csv_path, index=False)

    with json_path.open("w") as fh:
        json.dump(results, fh, indent=2, default=str)

    return csv_path, json_path


def print_top_results(results: list[dict], min_trades_for_dd: int = 30) -> None:
    """Print ranked summaries to stdout.

    Prints:
      - Top 10 by highest net P&L
      - Top 10 by highest profit factor
      - Top 10 by lowest max drawdown (only configs with >= min_trades_for_dd)

    Always prints an in-sample warning.
    """
    valid = [r for r in results if r.get("error") is None and r.get("trade_count") is not None]

    sep = "-" * 78
    print(f"\n{sep}")
    print("  WARNING: All results are IN-SAMPLE only. Do not use for live trading.")
    print(sep)

    def _header(title: str) -> None:
        print(f"\n{sep}")
        print(f"  {title}")
        print(sep)

    def _fmt(r: dict) -> str:
        return (
            f"  pnl={r.get('total_pnl', 0):+10.2f}"
            f"  pf={r.get('profit_factor') or 0:6.3f}"
            f"  dd={r.get('max_drawdown') or 0:.4f}"
            f"  wr={r.get('win_rate') or 0:.3f}"
            f"  trades={r.get('trade_count', 0):4d}"
            f"  pb={r.get('pullback_tolerance_bps')}"
            f"  sl={r.get('stop_loss_bps')}"
            f"  tgt={r.get('target_bps')}"
            f"  lkbk={r.get('vwap_slope_lookback_bars')}"
            f"  after={r.get('no_new_entries_after')}"
        )

    _header("Top 10 by highest net P&L")
    for r in sorted(valid, key=lambda x: x.get("total_pnl") or 0, reverse=True)[:10]:
        print(_fmt(r))

    _header("Top 10 by highest profit factor")
    for r in sorted(valid, key=lambda x: x.get("profit_factor") or 0, reverse=True)[:10]:
        print(_fmt(r))

    enough = [r for r in valid if (r.get("trade_count") or 0) >= min_trades_for_dd]
    _header(f"Top 10 lowest max drawdown (>= {min_trades_for_dd} trades)")
    if enough:
        for r in sorted(enough, key=lambda x: x.get("max_drawdown") or 1)[:10]:
            print(_fmt(r))
    else:
        print(f"  No configs with >= {min_trades_for_dd} trades found.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VWAP Pullback parameter sweep on local Parquet candle data."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=_DEFAULT_SYMBOLS,
        help=f"Symbols to include (default: {_DEFAULT_SYMBOLS})",
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        default=str(_DEFAULT_DATA_DIR),
        help=f"Root data directory (default: {_DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--interval",
        default=_DEFAULT_INTERVAL,
        help=f"Candle interval (default: {_DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--initial-cash",
        dest="initial_cash",
        type=float,
        default=float(_DEFAULT_INITIAL_CASH),
        help=f"Starting cash in INR (default: {_DEFAULT_INITIAL_CASH})",
    )
    parser.add_argument(
        "--quantity",
        type=int,
        default=_DEFAULT_QUANTITY,
        help=f"Shares per trade (default: {_DEFAULT_QUANTITY})",
    )
    parser.add_argument(
        "--max-combinations",
        dest="max_combinations",
        type=int,
        default=None,
        help="Limit sweep to first N combinations (default: run all 243)",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        default=str(_DEFAULT_OUTPUT_DIR),
        help=f"Directory to save results (default: {_DEFAULT_OUTPUT_DIR})",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    initial_cash = Decimal(str(args.initial_cash))

    print("\nVWAP Pullback Parameter Sweep")
    print(f"  Requested symbols: {args.symbols}")
    print(f"  Data dir:          {data_dir}")
    print(f"  Interval:          {args.interval}")
    print(f"  Initial cash:      {initial_cash}")
    print(f"  Quantity:          {args.quantity}")
    if args.max_combinations:
        print(f"  Max combinations:  {args.max_combinations}")

    candles = load_candles(args.symbols, data_dir, args.interval)
    if not candles:
        print("\nNo candle data found.\nDownload historical data first, then re-run this sweep.\n")
        sys.exit(0)

    print(f"\nLoaded symbols: {list(candles.keys())}")

    combos = build_grid(max_combinations=args.max_combinations)
    total_grid = len(build_grid())
    print(
        f"\nRunning {len(combos)} of {total_grid} parameter combinations "
        f"on {list(candles.keys())} ...\n"
    )

    results = run_sweep(candles, combos, initial_cash, args.quantity, args.interval)

    csv_path, json_path = save_results(results, output_dir)
    print(f"\nSaved {len(results)} results to:")
    print(f"  {csv_path}")
    print(f"  {json_path}")

    print_top_results(results)


if __name__ == "__main__":
    main()
