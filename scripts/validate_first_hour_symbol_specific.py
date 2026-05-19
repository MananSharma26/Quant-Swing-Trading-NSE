"""Parallelized First-Hour Momentum symbol-specific validation.

Evaluates parameter combinations across symbols in parallel, saving results to CSV/JSON.
Uses local Parquet data only. No live trading or broker calls.

Usage:
    python3 scripts/validate_first_hour_symbol_specific.py --workers 4
    python3 scripts/validate_first_hour_symbol_specific.py --fast
    python3 scripts/validate_first_hour_symbol_specific.py --sample-months 2025-01 2025-06
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time as time_mod
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import time
from decimal import Decimal
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

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

_ALL_SYMBOLS = [
    "RELIANCE",
    "HDFCBANK",
    "ICICIBANK",
    "INFY",
    "TCS",
    "LT",
    "SBIN",
    "AXISBANK",
    "BHARTIARTL",
    "ITC",
]
_FAST_SYMBOLS = ["TCS", "INFY", "ICICIBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

PARAM_GRID: dict[str, list] = {
    "momentum_window_minutes": [15, 30, 60],
    "min_first_window_return_bps": [40, 60, 80, 120],
    "latest_entry_time": [time(10, 30), time(11, 30), time(12, 0)],
    "stop_loss_bps": [60, 80, 120],
    "target_bps": [None, 120, 200],
    "allow_shorts": [False],
    "max_trades_per_symbol_per_day": [1],
}


# ---------------------------------------------------------------------------
# Helpers (from sweep script)
# ---------------------------------------------------------------------------


def _params_to_row(params: dict) -> dict:
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
    if value is None:
        return None
    try:
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _trade_level_metrics(fills: list) -> dict:
    from trading_engine.domain.enums import Side  # noqa: PLC0415

    buy_queue: dict[str, list[tuple[int, Decimal]]] = {}
    gross_pnls: list[Decimal] = []

    for fill in fills:
        sym = fill.symbol
        if fill.side == Side.BUY:
            buy_queue.setdefault(sym, []).append((fill.quantity, fill.price))
        elif fill.side == Side.SELL:
            entries = buy_queue.get(sym, [])
            remaining = fill.quantity
            cost = Decimal("0")
            new_entries = []
            for qty, price in entries:
                if remaining <= 0:
                    new_entries.append((qty, price))
                    continue
                used = min(qty, remaining)
                cost += Decimal(str(used)) * price
                remaining -= used
                leftover = qty - used
                if leftover > 0:
                    new_entries.append((leftover, price))
            buy_queue[sym] = new_entries
            gross_pnls.append(Decimal(str(fill.quantity)) * fill.price - cost)

    n = len(gross_pnls)
    if n == 0:
        return {
            "round_trips": 0,
            "gross_pnl": None,
            "profit_factor": None,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": None,
            "average_gross_pnl": None,
        }

    wins = [p for p in gross_pnls if p > 0]
    losses = [p for p in gross_pnls if p < 0]
    total_gross = sum(gross_pnls, Decimal("0"))

    pf = None
    if losses:
        gross_loss = abs(sum(losses, Decimal("0")))
        if gross_loss > 0:
            pf = _safe_float(sum(wins, Decimal("0")) / gross_loss)

    return {
        "round_trips": n,
        "gross_pnl": _safe_float(total_gross),
        "profit_factor": pf,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": _safe_float(Decimal(str(len(wins))) / Decimal(str(n))),
        "average_gross_pnl": _safe_float(total_gross / Decimal(str(n))),
    }


def _derive_config_times(momentum_window_minutes: int, latest_entry_time: time) -> tuple[time, int]:
    session_minutes = 9 * 60 + 15 + momentum_window_minutes
    earliest_entry = time(session_minutes // 60, session_minutes % 60)
    return earliest_entry, momentum_window_minutes


# ---------------------------------------------------------------------------
# Core Task Logic
# ---------------------------------------------------------------------------


def evaluate_task(
    symbol: str,
    params: dict,
    symbol_candles: pd.DataFrame,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
) -> dict:
    """Run backtest for ONE symbol and ONE config."""
    mwm = int(params.get("momentum_window_minutes", 30))
    latest_entry = params.get("latest_entry_time", time(12, 0))
    earliest_entry, min_bars = _derive_config_times(mwm, latest_entry)

    try:
        cfg = FirstHourMomentumConfig(
            strategy_id=f"fhm_val_{symbol}",
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
    except Exception as exc:
        res = _params_to_row(params)
        res.update({"symbol": symbol, "error": str(exc)})
        return res

    strategy = FirstHourMomentumStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    broker = SimulatedBroker(portfolio, CostModel(), SlippageModel(bps=Decimal("2")))
    feed = HistoricalDataFeed({symbol: symbol_candles}, interval=interval)
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=cfg.strategy_id,
        symbols=[symbol],
    )

    report = engine.run()
    m = report.metrics
    tl = _trade_level_metrics(report.fills)

    net_pnl = m.total_pnl
    total_fees_val = m.total_fees
    gross_pnl_formula = _safe_float(net_pnl + total_fees_val)

    avg_trade_pnl = None
    if tl["round_trips"] > 0:
        avg_trade_pnl = _safe_float(net_pnl / Decimal(str(tl["round_trips"])))

    row = _params_to_row(params)
    row.update(
        {
            "symbol": symbol,
            "error": None,
            "total_pnl": _safe_float(net_pnl),
            "gross_pnl": gross_pnl_formula,
            "total_fees": _safe_float(total_fees_val),
            "trade_count": m.trade_count,
            "win_rate": tl["win_rate"],
            "profit_factor": tl["profit_factor"],
            "average_trade_pnl": avg_trade_pnl,
            "max_drawdown": _safe_float(m.max_drawdown),
        }
    )
    return row


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_tasks(
    symbols: list[str],
    grid: dict[str, list],
    max_combos: int | None = None,
) -> list[tuple[str, dict]]:
    keys = list(grid.keys())
    combos = [dict(zip(keys, c, strict=True)) for c in product(*grid.values())]
    if max_combos and max_combos < len(combos):
        combos = combos[:max_combos]

    tasks = []
    for sym in symbols:
        for combo in combos:
            tasks.append((sym, combo))
    return tasks


def filter_candles(candles: dict[str, pd.DataFrame], months: list[str]) -> dict[str, pd.DataFrame]:
    """Filter bars to only those matching YYYY-MM strings."""
    if not months:
        return candles

    filtered = {}
    for sym, df in candles.items():
        # Ensure timestamp is datetime
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        mask = df["timestamp"].dt.strftime("%Y-%m").isin(months)
        filtered_df = df[mask].copy()
        if not filtered_df.empty:
            filtered[sym] = filtered_df
            print(f"  {sym}: filtered to {len(filtered_df)} bars ({months})")
        else:
            print(f"  {sym}: NO BARS MATCHED {months}")
    return filtered


def load_all_candles(symbols: list[str], data_dir: Path, interval: str) -> dict[str, pd.DataFrame]:
    candles = {}
    for sym in symbols:
        path = data_dir / "candles" / "NSE" / sym / f"{interval}.parquet"
        if path.exists():
            try:
                candles[sym] = pd.read_parquet(path)
                print(f"  Loaded {sym}: {len(candles[sym])} bars")
            except Exception as exc:
                print(f"  Failed to load {sym}: {exc}")
    return candles


def run_parallel(
    tasks: list[tuple[str, dict]],
    candles: dict[str, pd.DataFrame],
    workers: int,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
) -> list[dict]:
    results = []
    total = len(tasks)
    start_time = time_mod.time()

    print(f"\nStarting {total} tasks with {workers} workers...")

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = []
            for sym, params in tasks:
                futures.append(
                    executor.submit(
                        evaluate_task,
                        sym,
                        params,
                        candles[sym],
                        initial_cash,
                        quantity,
                        interval,
                    )
                )

            done_count = 0
            for future in as_completed(futures):
                results.append(future.result())
                done_count += 1
                _report_progress(done_count, total, start_time)
    else:
        # Sequential fallback
        for i, (sym, params) in enumerate(tasks, start=1):
            results.append(
                evaluate_task(sym, params, candles[sym], initial_cash, quantity, interval)
            )
            _report_progress(i, total, start_time)

    print()  # Final newline after progress
    return results


def _report_progress(done: int, total: int, start_time: float):
    elapsed = time_mod.time() - start_time
    avg = elapsed / done if done > 0 else 0
    rem = (total - done) * avg
    print(
        f"\r  Progress: {done}/{total} ({done / total:.1%}) | "
        f"Elapsed: {elapsed:.1f}s | Avg: {avg:.2f}s/t | ETA: {rem:.1f}s",
        end="",
        flush=True,
    )


def save_final(results: list[dict], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    # Deterministic sort: symbol, then net_pnl desc, then config keys
    config_keys = sorted(PARAM_GRID.keys())

    def sort_key(r):
        return (
            r.get("symbol", ""),
            -(r.get("total_pnl") if r.get("total_pnl") is not None else -1e9),
            *[str(r.get(k)) for k in config_keys],
        )

    sorted_results = sorted(results, key=sort_key)

    csv_path = output_dir / "first_hour_symbol_validation.csv"
    json_path = output_dir / "first_hour_symbol_validation.json"

    pd.DataFrame(sorted_results).to_csv(csv_path, index=False)
    with open(json_path, "w") as f:
        json.dump(sorted_results, f, indent=2, default=str)

    print(f"\nSaved {len(results)} results to:")
    print(f"  {csv_path}")
    print(f"  {json_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--sample-months", nargs="+", default=[])
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--max-combos", type=int)
    parser.add_argument("--data-dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR))

    args = parser.parse_args(argv)

    symbols = args.symbols if args.symbols else _ALL_SYMBOLS
    max_combos = args.max_combos
    if args.fast:
        print("\nFAST MODE: exploratory only")
        symbols = _FAST_SYMBOLS
        max_combos = 25

    print(f"Symbols: {symbols}")
    print(f"Workers: {args.workers}")

    data_dir = Path(args.data_dir)
    candles = load_all_candles(symbols, data_dir, _DEFAULT_INTERVAL)
    if not candles:
        print("No data found.")
        sys.exit(1)

    if args.sample_months:
        candles = filter_candles(candles, args.sample_months)
        # Refresh symbols list in case some were filtered out
        symbols = list(candles.keys())

    tasks = build_tasks(symbols, PARAM_GRID, max_combos)
    results = run_parallel(
        tasks,
        candles,
        args.workers,
        _DEFAULT_INITIAL_CASH,
        _DEFAULT_QUANTITY,
        _DEFAULT_INTERVAL,
    )

    save_final(results, Path(args.output_dir))


if __name__ == "__main__":
    main()
