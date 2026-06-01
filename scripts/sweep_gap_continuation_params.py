"""Gap Continuation parameter sweep.

Runs BacktestEngine over a grid of GapContinuationConfig parameters using
locally stored Parquet candle data. Results are saved to CSV and JSON.

No live trading. No broker API calls. No credentials required.

Usage:
    python3 scripts/sweep_gap_continuation_params.py
    python3 scripts/sweep_gap_continuation_params.py --fast
    python3 scripts/sweep_gap_continuation_params.py --max-combinations 50
    python3 scripts/sweep_gap_continuation_params.py --symbols INDHOTEL MPHASIS

WARNING: all results are IN-SAMPLE only.
"""

from __future__ import annotations

import argparse
import math
import sys
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
from trading_engine.strategies.gap_continuation import (  # noqa: E402
    GapContinuationConfig,
    GapContinuationStrategy,
)

_DEFAULT_SYMBOLS = [
    "INDHOTEL", "MPHASIS", "COFORGE", "LTTS", "BANDHANBNK",
    "IDFCFIRSTB", "TATACOMM", "ABFRL", "NYKAA",
]
_FAST_SYMBOLS = ["INDHOTEL", "MPHASIS", "COFORGE"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Parameter grid — 4 * 3 * 3 * 3 = 108 total combinations.
# ---------------------------------------------------------------------------

PARAM_GRID: dict[str, list] = {
    "min_gap_bps": [40, 60, 80, 120],
    "max_gap_bps": [200, 300, 500],
    "continuation_trigger_bps": [10, 20, 40],
    "stop_loss_bps": [60, 80, 120],
}


def build_grid(
    grid: dict[str, list] | None = None,
    max_combinations: int | None = None,
) -> list[dict]:
    """Return list of parameter dicts for the Cartesian product of the grid."""
    g = grid if grid is not None else PARAM_GRID
    keys = list(g.keys())
    combos = [dict(zip(keys, combo, strict=True)) for combo in product(*g.values())]
    if max_combinations is not None and max_combinations < len(combos):
        combos = combos[:max_combinations]
    return combos


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
    quantity: int,
    interval: str,
    run_index: int = 0,
) -> dict:
    """Run one backtest with the given params; return a result row dict."""
    allow_long = params.get("allow_long_continuations", True)
    allow_short = params.get("allow_short_continuations", True)
    try:
        cfg = GapContinuationConfig(
            strategy_id=f"gc_sweep_{run_index}",
            quantity=quantity,
            min_gap_bps=float(params["min_gap_bps"]),
            max_gap_bps=float(params["max_gap_bps"]),
            continuation_trigger_bps=float(params["continuation_trigger_bps"]),
            stop_loss_bps=float(params["stop_loss_bps"]),
            allow_long_continuations=allow_long,
            allow_short_continuations=allow_short,
        )
    except ValueError as exc:
        return {**params, "error": str(exc), "total_pnl": None, "trade_count": None}

    strategy = GapContinuationStrategy(config=cfg)
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
    trade_count = m.winning_trades + m.losing_trades

    return {
        **params,
        "error": None,
        "total_return": _safe_float(m.total_return),
        "total_pnl": _safe_float(m.total_pnl),
        "total_fees": _safe_float(m.total_fees),
        "max_drawdown": _safe_float(m.max_drawdown),
        "win_rate": _safe_float(m.win_rate),
        "trade_count": trade_count,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gap Continuation parameter sweep.")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument("--initial-cash", dest="initial_cash", type=float, default=float(_DEFAULT_INITIAL_CASH))
    parser.add_argument("--quantity", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument("--fast", action="store_true", default=False,
                        help="Fast mode: 3 symbols, up to 30 combinations")
    parser.add_argument("--max-combinations", dest="max_combinations", type=int, default=None)
    parser.add_argument("--output-dir", dest="output_dir", default=str(_DEFAULT_OUTPUT_DIR))
    parser.add_argument("--long-only", dest="long_only", action="store_true", default=False,
                        help="Only take gap-up LONG continuations")
    parser.add_argument("--short-only", dest="short_only", action="store_true", default=False,
                        help="Only take gap-down SHORT continuations")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    initial_cash = Decimal(str(args.initial_cash))
    interval = args.interval

    symbols = args.symbols
    max_combinations = args.max_combinations
    if args.fast:
        symbols = symbols or _FAST_SYMBOLS
        max_combinations = max_combinations or 30

    symbols = symbols or _DEFAULT_SYMBOLS

    print("\nGap Continuation Parameter Sweep")
    print(f"  Symbols:          {symbols}")
    print(f"  Data dir:         {data_dir}")
    print(f"  Fast mode:        {args.fast}")
    print(f"  Max combinations: {max_combinations}")

    candles = load_candles(symbols, data_dir, interval)
    if not candles:
        print("\nNo candle data found. Download historical data first, then re-run.\n")
        sys.exit(0)

    combos = build_grid(max_combinations=max_combinations)
    # Inject direction flags into each combo
    for c in combos:
        c["allow_long_continuations"] = not args.short_only
        c["allow_short_continuations"] = not args.long_only
    print(f"\nRunning {len(combos)} combinations on {list(candles.keys())} ...")
    print(f"  Long-only: {args.long_only}  Short-only: {args.short_only}")

    results = []
    for i, params in enumerate(combos):
        row = run_single(candles, params, initial_cash, args.quantity, interval, run_index=i)
        results.append(row)
        if (i + 1) % 10 == 0 or (i + 1) == len(combos):
            print(f"  {i + 1}/{len(combos)} done")

    df_results = pd.DataFrame(results)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "gap_continuation_sweep_results.csv"
    json_path = output_dir / "gap_continuation_sweep_results.json"
    df_results.to_csv(csv_path, index=False)
    df_results.to_json(json_path, orient="records", indent=2)

    print(f"\nSaved: {csv_path}")
    print(f"Saved: {json_path}")

    valid = df_results[df_results["error"].isna() & df_results["total_pnl"].notna()]
    if valid.empty:
        print("\nNo valid results to rank.")
        return

    top10 = valid.nlargest(10, "total_pnl")
    print(f"\n{'=' * 60}")
    print("Top 10 combos by total_pnl:")
    print(
        top10[[
            "min_gap_bps", "max_gap_bps", "continuation_trigger_bps",
            "stop_loss_bps", "total_pnl", "win_rate", "trade_count",
        ]].to_string(index=False)
    )
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
