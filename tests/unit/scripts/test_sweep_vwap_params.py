"""Tests for scripts/sweep_vwap_params.py.

All tests use synthetic in-memory data — no real files, no Zerodha calls.
"""

from __future__ import annotations

import json
import sys
from datetime import time
from decimal import Decimal
from pathlib import Path

import pandas as pd

# Ensure scripts/ is importable.
# File lives at tests/unit/scripts/test_*.py → parents[3] = project root.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from sweep_vwap_params import (  # noqa: E402
    PARAM_GRID,
    build_grid,
    load_candles,
    print_top_results,
    run_single,
    run_sweep,
    save_results,
)

# ---------------------------------------------------------------------------
# Synthetic candle data helpers
# ---------------------------------------------------------------------------


def _make_candles(n_bars: int = 25, close_start: float = 100.0) -> dict[str, pd.DataFrame]:
    """Build a minimal uptrend DataFrame usable with BacktestEngine."""
    rows = []
    for i in range(n_bars):
        c = close_start + i * 0.5
        rows.append(
            {
                "timestamp": pd.Timestamp(f"2024-01-15 09:{15 + i:02d}:00"),
                "open": c,
                "high": c + 1,
                "low": c - 1,
                "close": c,
                "volume": 1000,
            }
        )
    return {"RELIANCE": pd.DataFrame(rows)}


def _mini_grid() -> dict[str, list]:
    """A 1-combination grid for fast engine tests."""
    return {
        "pullback_tolerance_bps": [50.0],
        "stop_loss_bps": [50.0],
        "target_bps": [100.0],
        "no_new_entries_after": [time(14, 30)],
        "max_trades_per_symbol_per_day": [1],
        "vwap_slope_lookback_bars": [3],
    }


# ---------------------------------------------------------------------------
# Tests: build_grid
# ---------------------------------------------------------------------------


class TestBuildGrid:
    def test_full_grid_count(self):
        combos = build_grid()
        # 3 * 3 * 3 * 3 * 1 * 3 = 243
        assert len(combos) == 243

    def test_combo_has_all_expected_keys(self):
        combos = build_grid()
        expected_keys = set(PARAM_GRID.keys())
        for combo in combos:
            assert set(combo.keys()) == expected_keys

    def test_max_combinations_limits_count(self):
        combos = build_grid(max_combinations=10)
        assert len(combos) == 10

    def test_max_combinations_larger_than_grid_returns_full(self):
        combos = build_grid(max_combinations=9999)
        assert len(combos) == 243

    def test_custom_grid_produces_correct_count(self):
        custom = {"a": [1, 2], "b": [3, 4, 5]}
        combos = build_grid(grid=custom)
        assert len(combos) == 6  # 2 * 3

    def test_grid_values_are_correct_types(self):
        combos = build_grid()
        for combo in combos[:5]:
            assert isinstance(combo["pullback_tolerance_bps"], (int, float))
            assert isinstance(combo["stop_loss_bps"], (int, float))
            assert isinstance(combo["no_new_entries_after"], time)
            assert isinstance(combo["vwap_slope_lookback_bars"], int)

    def test_combos_are_unique(self):
        combos = build_grid()
        # Stringify each combo for hashability.
        strings = [str(sorted(c.items())) for c in combos]
        assert len(strings) == len(set(strings))


# ---------------------------------------------------------------------------
# Tests: load_candles
# ---------------------------------------------------------------------------


class TestLoadCandles:
    def test_loads_existing_parquet(self, tmp_path: Path):
        # Create a fake parquet file.
        sym_dir = tmp_path / "candles" / "NSE" / "RELIANCE"
        sym_dir.mkdir(parents=True)
        df = pd.DataFrame([{"timestamp": pd.Timestamp("2024-01-15 09:15:00"), "close": 100.0}])
        df.to_parquet(sym_dir / "minute.parquet")

        candles = load_candles(["RELIANCE"], tmp_path, "minute")
        assert "RELIANCE" in candles
        assert len(candles["RELIANCE"]) == 1

    def test_missing_symbol_is_skipped(self, tmp_path: Path, capsys):
        candles = load_candles(["GHOST_SYMBOL"], tmp_path, "minute")
        assert "GHOST_SYMBOL" not in candles
        captured = capsys.readouterr()
        assert "skip" in captured.out.lower()

    def test_missing_symbol_prints_warning(self, tmp_path: Path, capsys):
        load_candles(["MISSING"], tmp_path, "minute")
        captured = capsys.readouterr()
        assert "MISSING" in captured.out

    def test_only_loaded_symbols_returned(self, tmp_path: Path):
        # RELIANCE has data; INFY does not.
        sym_dir = tmp_path / "candles" / "NSE" / "RELIANCE"
        sym_dir.mkdir(parents=True)
        df = pd.DataFrame([{"timestamp": pd.Timestamp("2024-01-15 09:15:00"), "close": 100.0}])
        df.to_parquet(sym_dir / "minute.parquet")

        candles = load_candles(["RELIANCE", "INFY"], tmp_path, "minute")
        assert set(candles.keys()) == {"RELIANCE"}

    def test_unreadable_file_is_skipped(self, tmp_path: Path, capsys):
        sym_dir = tmp_path / "candles" / "NSE" / "BADFILE"
        sym_dir.mkdir(parents=True)
        # Write a corrupt file (not valid parquet).
        (sym_dir / "minute.parquet").write_bytes(b"not parquet data")

        candles = load_candles(["BADFILE"], tmp_path, "minute")
        assert "BADFILE" not in candles
        captured = capsys.readouterr()
        assert "skip" in captured.out.lower()

    def test_empty_symbol_list_returns_empty(self, tmp_path: Path):
        candles = load_candles([], tmp_path, "minute")
        assert candles == {}


# ---------------------------------------------------------------------------
# Tests: run_single (with synthetic data)
# ---------------------------------------------------------------------------


class TestRunSingle:
    def _params(self) -> dict:
        return {
            "pullback_tolerance_bps": 50.0,
            "stop_loss_bps": 50.0,
            "target_bps": 100.0,
            "no_new_entries_after": time(14, 30),
            "max_trades_per_symbol_per_day": 1,
            "vwap_slope_lookback_bars": 3,
        }

    def test_returns_dict(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert isinstance(row, dict)

    def test_result_has_required_keys(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        for key in (
            "total_pnl",
            "total_fees",
            "max_drawdown",
            "win_rate",
            "profit_factor",
            "trade_count",
            "total_return",
        ):
            assert key in row

    def test_no_error_on_valid_params(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert row.get("error") is None

    def test_invalid_config_returns_error_row(self):
        # stop_loss_bps=0 → VWAPPullbackConfig raises ValueError.
        params = self._params()
        params["stop_loss_bps"] = 0.0
        candles = _make_candles()
        row = run_single(candles, params, Decimal("100000"), 10, "minute")
        assert row.get("error") is not None
        assert row.get("total_pnl") is None

    def test_time_params_serialised_as_strings(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert isinstance(row.get("no_new_entries_after"), str)


# ---------------------------------------------------------------------------
# Tests: run_sweep (with synthetic data and mini grid)
# ---------------------------------------------------------------------------


class TestRunSweep:
    def test_returns_one_result_per_combo(self):
        candles = _make_candles()
        combos = build_grid(grid=_mini_grid())
        results = run_sweep(candles, combos, Decimal("100000"), 10, "minute")
        assert len(results) == len(combos)

    def test_results_are_dicts(self):
        candles = _make_candles()
        combos = build_grid(grid=_mini_grid())
        results = run_sweep(candles, combos, Decimal("100000"), 10, "minute")
        assert all(isinstance(r, dict) for r in results)

    def test_trade_count_is_non_negative(self):
        candles = _make_candles()
        combos = build_grid(grid=_mini_grid())
        results = run_sweep(candles, combos, Decimal("100000"), 10, "minute")
        for r in results:
            if r.get("error") is None:
                assert (r.get("trade_count") or 0) >= 0


# ---------------------------------------------------------------------------
# Tests: save_results
# ---------------------------------------------------------------------------


class TestSaveResults:
    def _fake_results(self) -> list[dict]:
        return [
            {
                "pullback_tolerance_bps": 10,
                "stop_loss_bps": 40,
                "target_bps": 80,
                "no_new_entries_after": "14:30:00",
                "max_trades_per_symbol_per_day": 1,
                "vwap_slope_lookback_bars": 5,
                "error": None,
                "total_return": -0.05,
                "total_pnl": -2500.0,
                "gross_pnl": -1000.0,
                "total_fees": 1500.0,
                "max_drawdown": 0.06,
                "win_rate": 0.4,
                "profit_factor": 0.5,
                "trade_count": 50,
                "average_trade_pnl": -50.0,
                "sharpe_ratio": -0.5,
                "sortino_ratio": None,
            },
        ]

    def test_csv_is_created(self, tmp_path: Path):
        csv_path, _ = save_results(self._fake_results(), tmp_path)
        assert csv_path.exists()

    def test_json_is_created(self, tmp_path: Path):
        _, json_path = save_results(self._fake_results(), tmp_path)
        assert json_path.exists()

    def test_csv_has_correct_row_count(self, tmp_path: Path):
        results = self._fake_results() * 3
        csv_path, _ = save_results(results, tmp_path)
        df = pd.read_csv(csv_path)
        assert len(df) == 3

    def test_json_is_valid(self, tmp_path: Path):
        csv_path, json_path = save_results(self._fake_results(), tmp_path)
        with json_path.open() as fh:
            data = json.load(fh)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_output_dir_created_if_missing(self, tmp_path: Path):
        subdir = tmp_path / "new" / "nested" / "dir"
        save_results(self._fake_results(), subdir)
        assert subdir.exists()


# ---------------------------------------------------------------------------
# Tests: print_top_results (smoke / no crash)
# ---------------------------------------------------------------------------


class TestPrintTopResults:
    def _results(self, n: int = 5) -> list[dict]:
        return [
            {
                "pullback_tolerance_bps": i * 5,
                "stop_loss_bps": 40,
                "target_bps": 80,
                "no_new_entries_after": "14:30:00",
                "max_trades_per_symbol_per_day": 1,
                "vwap_slope_lookback_bars": 5,
                "error": None,
                "total_return": (i - 2) * 0.01,
                "total_pnl": (i - 2) * 1000.0,
                "gross_pnl": (i - 2) * 1200.0,
                "total_fees": 200.0,
                "max_drawdown": 0.05 + i * 0.01,
                "win_rate": 0.4 + i * 0.05,
                "profit_factor": 0.8 + i * 0.1,
                "trade_count": 10 + i * 10,
                "average_trade_pnl": (i - 2) * 20.0,
                "sharpe_ratio": None,
                "sortino_ratio": None,
            }
            for i in range(n)
        ]

    def test_no_crash_with_valid_results(self, capsys):
        print_top_results(self._results())
        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_prints_in_sample_warning(self, capsys):
        print_top_results(self._results())
        captured = capsys.readouterr()
        assert "IN-SAMPLE" in captured.out

    def test_no_crash_with_empty_results(self, capsys):
        print_top_results([])

    def test_few_trades_message_when_none_meet_threshold(self, capsys):
        results = self._results(n=2)  # trade_counts are 10 and 20, below 30
        print_top_results(results, min_trades_for_dd=30)
        captured = capsys.readouterr()
        assert "30" in captured.out


# ---------------------------------------------------------------------------
# Tests: no Zerodha dependency
# ---------------------------------------------------------------------------


class TestNoZerodhaDependency:
    def test_sweep_module_has_no_kiteconnect_import(self):
        import inspect

        import sweep_vwap_params as mod

        src = inspect.getsource(mod)
        assert "kiteconnect" not in src

    def test_sweep_module_has_no_zerodha_import(self):
        import inspect

        import sweep_vwap_params as mod

        src = inspect.getsource(mod)
        assert "zerodha" not in src.lower()

    def test_sweep_module_has_no_live_execution_import(self):
        import inspect

        import sweep_vwap_params as mod

        src = inspect.getsource(mod)
        assert "live_execution" not in src
        assert "place_order" not in src
