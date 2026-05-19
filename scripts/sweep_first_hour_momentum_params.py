"""First-Hour Momentum to Close parameter sweep.

Runs BacktestEngine over a grid of FirstHourMomentumConfig parameters using
locally stored Parquet candle data.  Results are saved to CSV and JSON.

No live trading.  No broker API calls.  No credentials required.

Usage:
    python3 scripts/sweep_first_hour_momentum_params.py
    python3 scripts/sweep_first_hour_momentum_params.py --max-combinations 50
    python3 scripts/sweep_first_hour_momentum_params.py --output-dir /tmp/results

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
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Parameter grid
# ---------------------------------------------------------------------------

PARAM_GRID: dict[str, list] = {
    "momentum_window_minutes": [15, 30, 60],
    "min_first_window_return_bps": [40, 60, 80, 120],
    "latest_entry_time": [time(10, 30), time(11, 30), time(12, 0)],
    "stop_loss_bps": [60, 80, 120],
    "target_bps": [None, 120, 200],
    "allow_shorts": [False],
    "max_trades_per_symbol_per_day": [1],
}
# 3 * 4 * 3 * 3 * 3 * 1 * 1 = 324 total combinations.


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------


def build_grid(
    grid: dict[str, list] | None = None,
    max_combinations: int | None = None,
) -> list[dict]:
    """Return list of parameter dicts for the Cartesian product of the grid.

    Args:
        grid:             Custom grid dict; defaults to PARAM_GRID.
        max_combinations: If given, return only the first N combinations.
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
    """Load Parquet candle files; skip missing or unreadable symbols."""
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
    """Convert params dict to JSON-serialisable flat dict."""
    row = {}
    for k, v in params.items():
        if isinstance(v, time):
            row[k] = str(v)
        elif v is None:
            row[k] = None
        else:
            row[k] = v
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


def _derive_config_times(momentum_window_minutes: int, latest_entry_time: time) -> tuple[time, int]:
    """Compute earliest_entry_time and min_bars_before_signal from window length."""
    session_minutes = 9 * 60 + 15 + momentum_window_minutes
    earliest_entry = time(session_minutes // 60, session_minutes % 60)
    min_bars = momentum_window_minutes
    return earliest_entry, min_bars


def run_single(
    candles: dict[str, pd.DataFrame],
    params: dict,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
    run_index: int = 0,
) -> dict:
    """Run one backtest with the given params; return a result row dict."""
    mwm = int(params.get("momentum_window_minutes", 30))
    latest_entry = params.get("latest_entry_time", time(12, 0))
    earliest_entry, min_bars = _derive_config_times(mwm, latest_entry)

    try:
        cfg = FirstHourMomentumConfig(
            strategy_id=f"fhm_sweep_{run_index}",
            quantity=quantity,
            momentum_window_minutes=mwm,
            earliest_entry_time=earliest_entry,
            latest_entry_time=latest_entry,
            min_bars_before_signal=min_bars,
            min_first_window_return_bps=float(params.get("min_first_window_return_bps", 60.0)),
            stop_loss_bps=float(params.get("stop_loss_bps", 80.0)),
            target_bps=(
                float(params["target_bps"]) if params.get("target_bps") is not None else None
            ),
            allow_shorts=bool(params.get("allow_shorts", False)),
            max_trades_per_symbol_per_day=int(params.get("max_trades_per_symbol_per_day", 1)),
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

    strategy = FirstHourMomentumStrategy(config=cfg)
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
            pnl = row.get("total_pnl") or 0.0
            trades = row.get("trade_count", 0)
            print(f"pnl={pnl:+.2f}  trades={trades}")
        results.append(row)
    return results


# ---------------------------------------------------------------------------
# Results output
# ---------------------------------------------------------------------------


def save_results(results: list[dict], output_dir: Path) -> tuple[Path, Path]:
    """Save results to CSV and JSON under output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "first_hour_momentum_sweep_results.csv"
    json_path = output_dir / "first_hour_momentum_sweep_results.json"

    df = pd.DataFrame(results)
    df.to_csv(csv_path, index=False)

    with json_path.open("w") as fh:
        json.dump(results, fh, indent=2, default=str)

    return csv_path, json_path


def print_top_results(results: list[dict], min_trades_for_dd: int = 30) -> None:
    """Print ranked summaries to stdout with an in-sample warning."""
    valid = [r for r in results if r.get("error") is None and r.get("trade_count") is not None]

    sep = "-" * 80
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
            f"  win={r.get('momentum_window_minutes')}min"
            f"  ret={r.get('min_first_window_return_bps')}bps"
            f"  sl={r.get('stop_loss_bps')}"
            f"  tgt={r.get('target_bps')}"
            f"  latest={r.get('latest_entry_time')}"
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
        description="First-Hour Momentum parameter sweep on local Parquet candle data."
    )
    parser.add_argument("--symbols", nargs="+", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument(
        "--initial-cash",
        dest="initial_cash",
        type=float,
        default=float(_DEFAULT_INITIAL_CASH),
    )
    parser.add_argument("--quantity", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument(
        "--max-combinations",
        dest="max_combinations",
        type=int,
        default=None,
        help="Limit sweep to first N combinations (default: run all 324)",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        default=str(_DEFAULT_OUTPUT_DIR),
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

    print("\nFirst-Hour Momentum Parameter Sweep")
    print(f"  Requested symbols: {args.symbols}")
    print(f"  Data dir:          {data_dir}")
    print(f"  Interval:          {args.interval}")
    print(f"  Initial cash:      {initial_cash}")
    print(f"  Quantity:          {args.quantity}")
    if args.max_combinations:
        print(f"  Max combinations:  {args.max_combinations}")

    candles = load_candles(args.symbols, data_dir, args.interval)
    if not candles:
        print("\nNo candle data found. Download historical data first, then re-run.\n")
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
