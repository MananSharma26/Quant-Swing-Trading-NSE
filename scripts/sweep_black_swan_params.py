"""Black Swan strategy parameter sweep.

Runs BacktestEngine over a grid of BlackSwanPairsConfig parameters using locally
stored Parquet daily candle data.
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

import pandas as pd

from trading_engine.backtest.cost_model import CostModel
from trading_engine.backtest.data_feed import HistoricalDataFeed
from trading_engine.backtest.engine import BacktestEngine
from trading_engine.backtest.portfolio import BacktestPortfolio
from trading_engine.backtest.simulated_broker import SimulatedBroker
from trading_engine.backtest.slippage_model import SlippageModel
from trading_engine.strategies.black_swan_pairs import BlackSwanPairsConfig, BlackSwanPairsStrategy

_DEFAULT_SYMBOLS = ["BAJAJFINSV", "HDFCBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "day"
_DEFAULT_INITIAL_CASH = Decimal("1000000")
_DEFAULT_QUANTITY = 100
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

PARAM_GRID: dict[str, list] = {
    "window_size": [30, 60, 90, 120, 180],
    "entry_z_score": [2.0, 2.5, 3.0, 3.5],
    "stop_loss_z_score": [4.0, 5.0, 6.0],
}

def build_grid(grid: dict[str, list] | None = None, max_combinations: int | None = None) -> list[dict]:
    g = grid if grid is not None else PARAM_GRID
    keys = list(g.keys())
    combos = [dict(zip(keys, combo, strict=True)) for combo in product(*g.values())]
    if max_combinations is not None and max_combinations < len(combos):
        combos = combos[:max_combinations]
    return combos

def load_candles(symbols: list[str], data_dir: Path, interval: str) -> dict[str, pd.DataFrame]:
    candles: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = data_dir / "candles" / "NSE" / symbol / f"{interval}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
            candles[symbol] = df
        except Exception:
            pass
    return candles

def _params_to_row(params: dict) -> dict:
    row = {}
    for k, v in params.items():
        row[k] = str(v) if isinstance(v, time) else v
    return row

def _safe_float(value) -> float | None:
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
    symbols: list[str],
    interval: str,
    run_index: int = 0,
    qty_a: int = 100,
    qty_b: int = 100,
) -> dict:
    try:
        cfg = BlackSwanPairsConfig(
            strategy_id=f"sweep_{run_index}",
            symbol_a=symbols[0],
            symbol_b=symbols[1],
            quantity_a=qty_a,
            quantity_b=qty_b,
            window_size=int(params.get("window_size", 120)),
            entry_z_score=float(params.get("entry_z_score", 3.0)),
            stop_loss_z_score=float(params.get("stop_loss_z_score", 5.0)),
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
        }

    strategy = BlackSwanPairsStrategy(config=cfg)
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
        symbols=symbols,
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
    return row

def run_sweep(
    candles: dict[str, pd.DataFrame],
    combos: list[dict],
    initial_cash: Decimal,
    symbols: list[str],
    interval: str,
    qty_a: int = 100,
    qty_b: int = 100,
) -> list[dict]:
    results: list[dict] = []
    total = len(combos)
    for i, params in enumerate(combos, start=1):
        label = _params_to_row(params)
        print(f"  [{i:3d}/{total}] {label} ...", end=" ", flush=True)
        row = run_single(candles, params, initial_cash, symbols, interval, run_index=i, qty_a=qty_a, qty_b=qty_b)
        if row.get("error"):
            print(f"SKIP ({row['error']})")
        else:
            pnl = row.get("total_pnl")
            trades = row.get("trade_count", 0)
            print(f"pnl={pnl:+.2f}  trades={trades}")
        results.append(row)
    return results

def save_results(results: list[dict], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "black_swan_sweep_results.csv"
    json_path = output_dir / "black_swan_sweep_results.json"
    df = pd.DataFrame(results)
    df.to_csv(csv_path, index=False)
    with json_path.open("w") as fh:
        json.dump(results, fh, indent=2, default=str)
    return csv_path, json_path

def print_top_results(results: list[dict], min_trades: int = 1) -> None:
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
            f"  wr={r.get('win_rate') or 0:.3f}"
            f"  trades={r.get('trade_count', 0):4d}"
            f"  win={r.get('window_size')}"
            f"  entZ={r.get('entry_z_score')}"
            f"  slZ={r.get('stop_loss_z_score')}"
        )

    _header("Top 10 by highest net P&L")
    for r in sorted(valid, key=lambda x: x.get("total_pnl") or 0, reverse=True)[:10]:
        print(_fmt(r))

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--data-dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument("--initial-cash", type=float, default=float(_DEFAULT_INITIAL_CASH))
    parser.add_argument("--max-combinations", type=int, default=None)
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR))
    parser.add_argument("--qty-a", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument("--qty-b", type=int, default=_DEFAULT_QUANTITY)
    return parser.parse_args(argv)

def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    initial_cash = Decimal(str(args.initial_cash))

    candles = load_candles(args.symbols, data_dir, args.interval)
    if len(candles) < 2:
        print("Need at least 2 symbols.")
        return

    combos = build_grid(max_combinations=args.max_combinations)
    results = run_sweep(candles, combos, initial_cash, args.symbols, args.interval, args.qty_a, args.qty_b)
    save_results(results, output_dir)
    print_top_results(results)

if __name__ == "__main__":
    main()
