"""Tests for sweep_first_hour_momentum_params.py.

Focuses on: grid construction, symbol loading, result structure, saving,
top-results printing, and absence of live-trading imports.
All tests use synthetic in-memory data — no real files, no broker calls.
"""

from __future__ import annotations

import json
import sys
from datetime import time
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from sweep_first_hour_momentum_params import (  # noqa: E402
    PARAM_GRID,
    build_grid,
    load_candles,
    print_top_results,
    run_single,
    run_sweep,
    save_results,
)

# ---------------------------------------------------------------------------
# Synthetic candle data
# ---------------------------------------------------------------------------


def _make_candles(n_bars: int = 30) -> dict[str, pd.DataFrame]:
    """5 uptrend window bars then flat bars — enough to test sweep mechanics."""
    rows = []
    for i in range(n_bars):
        c = 100.0 + i * 0.5
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
    """1-combination grid for fast engine tests."""
    return {
        "momentum_window_minutes": [5],
        "min_first_window_return_bps": [100.0],
        "latest_entry_time": [time(12, 0)],
        "stop_loss_bps": [100.0],
        "target_bps": [None],
        "allow_shorts": [False],
        "max_trades_per_symbol_per_day": [1],
    }


# ---------------------------------------------------------------------------
# Tests: build_grid
# ---------------------------------------------------------------------------


class TestBuildGrid:
    def test_full_grid_count(self):
        combos = build_grid()
        # 3 * 4 * 3 * 3 * 3 * 1 * 1 = 324
        assert len(combos) == 324

    def test_combo_has_all_expected_keys(self):
        combos = build_grid()
        expected = set(PARAM_GRID.keys())
        for combo in combos:
            assert set(combo.keys()) == expected

    def test_max_combinations_limits_count(self):
        combos = build_grid(max_combinations=10)
        assert len(combos) == 10

    def test_max_combinations_larger_than_grid_returns_all(self):
        combos = build_grid(max_combinations=9999)
        assert len(combos) == 324

    def test_custom_grid_produces_correct_count(self):
        custom = {"a": [1, 2], "b": [3, 4, 5]}
        combos = build_grid(grid=custom)
        assert len(combos) == 6

    def test_grid_contains_none_target(self):
        combos = build_grid()
        none_count = sum(1 for c in combos if c["target_bps"] is None)
        assert none_count > 0

    def test_combos_are_unique(self):
        combos = build_grid()
        strings = [str(sorted((k, str(v)) for k, v in c.items())) for c in combos]
        assert len(strings) == len(set(strings))


# ---------------------------------------------------------------------------
# Tests: load_candles
# ---------------------------------------------------------------------------


class TestLoadCandles:
    def test_loads_existing_parquet(self, tmp_path: Path):
        sym_dir = tmp_path / "candles" / "NSE" / "RELIANCE"
        sym_dir.mkdir(parents=True)
        df = pd.DataFrame([{"timestamp": pd.Timestamp("2024-01-15 09:15:00"), "close": 100.0}])
        df.to_parquet(sym_dir / "minute.parquet")
        candles = load_candles(["RELIANCE"], tmp_path, "minute")
        assert "RELIANCE" in candles

    def test_missing_symbol_skipped(self, tmp_path: Path, capsys):
        candles = load_candles(["GHOST"], tmp_path, "minute")
        assert "GHOST" not in candles
        assert "skip" in capsys.readouterr().out.lower()

    def test_only_loaded_symbols_returned(self, tmp_path: Path):
        sym_dir = tmp_path / "candles" / "NSE" / "RELIANCE"
        sym_dir.mkdir(parents=True)
        df = pd.DataFrame([{"timestamp": pd.Timestamp("2024-01-15 09:15:00"), "close": 100.0}])
        df.to_parquet(sym_dir / "minute.parquet")
        candles = load_candles(["RELIANCE", "INFY"], tmp_path, "minute")
        assert set(candles.keys()) == {"RELIANCE"}

    def test_corrupt_file_skipped(self, tmp_path: Path, capsys):
        sym_dir = tmp_path / "candles" / "NSE" / "BADFILE"
        sym_dir.mkdir(parents=True)
        (sym_dir / "minute.parquet").write_bytes(b"not parquet")
        candles = load_candles(["BADFILE"], tmp_path, "minute")
        assert "BADFILE" not in candles
        assert "skip" in capsys.readouterr().out.lower()

    def test_empty_symbol_list_returns_empty(self, tmp_path: Path):
        assert load_candles([], tmp_path, "minute") == {}


# ---------------------------------------------------------------------------
# Tests: run_single
# ---------------------------------------------------------------------------


class TestRunSingle:
    def _params(self) -> dict:
        return {
            "momentum_window_minutes": 5,
            "min_first_window_return_bps": 100.0,
            "latest_entry_time": time(12, 0),
            "stop_loss_bps": 100.0,
            "target_bps": None,
            "allow_shorts": False,
            "max_trades_per_symbol_per_day": 1,
        }

    def test_returns_dict(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert isinstance(row, dict)

    def test_result_has_required_keys(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        for key in ("total_pnl", "total_fees", "max_drawdown", "trade_count"):
            assert key in row

    def test_no_error_on_valid_params(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert row.get("error") is None

    def test_invalid_config_returns_error_row(self):
        params = self._params()
        params["stop_loss_bps"] = 0.0  # invalid
        candles = _make_candles()
        row = run_single(candles, params, Decimal("100000"), 10, "minute")
        assert row.get("error") is not None
        assert row.get("total_pnl") is None

    def test_time_serialised_as_string(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert isinstance(row.get("latest_entry_time"), str)

    def test_target_none_preserved(self):
        candles = _make_candles()
        row = run_single(candles, self._params(), Decimal("100000"), 10, "minute")
        assert row.get("target_bps") is None


# ---------------------------------------------------------------------------
# Tests: run_sweep
# ---------------------------------------------------------------------------


class TestRunSweep:
    def test_returns_one_result_per_combo(self):
        candles = _make_candles()
        combos = build_grid(grid=_mini_grid())
        results = run_sweep(candles, combos, Decimal("100000"), 10, "minute")
        assert len(results) == 1

    def test_results_are_dicts(self):
        candles = _make_candles()
        combos = build_grid(grid=_mini_grid())
        results = run_sweep(candles, combos, Decimal("100000"), 10, "minute")
        assert all(isinstance(r, dict) for r in results)

    def test_trade_count_non_negative(self):
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
                "momentum_window_minutes": 30,
                "min_first_window_return_bps": 60,
                "latest_entry_time": "12:00:00",
                "stop_loss_bps": 80,
                "target_bps": None,
                "allow_shorts": False,
                "max_trades_per_symbol_per_day": 1,
                "error": None,
                "total_return": -0.03,
                "total_pnl": -1500.0,
                "gross_pnl": -800.0,
                "total_fees": 700.0,
                "max_drawdown": 0.04,
                "win_rate": 0.4,
                "profit_factor": 0.6,
                "trade_count": 40,
                "average_trade_pnl": -37.5,
                "sharpe_ratio": None,
                "sortino_ratio": None,
            }
        ]

    def test_csv_created(self, tmp_path: Path):
        csv_path, _ = save_results(self._fake_results(), tmp_path)
        assert csv_path.exists()

    def test_json_created(self, tmp_path: Path):
        _, json_path = save_results(self._fake_results(), tmp_path)
        assert json_path.exists()

    def test_json_is_valid(self, tmp_path: Path):
        _, json_path = save_results(self._fake_results(), tmp_path)
        with json_path.open() as fh:
            data = json.load(fh)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_csv_row_count(self, tmp_path: Path):
        results = self._fake_results() * 3
        csv_path, _ = save_results(results, tmp_path)
        df = pd.read_csv(csv_path)
        assert len(df) == 3

    def test_output_dir_created_if_missing(self, tmp_path: Path):
        subdir = tmp_path / "nested" / "dir"
        save_results(self._fake_results(), subdir)
        assert subdir.exists()


# ---------------------------------------------------------------------------
# Tests: print_top_results
# ---------------------------------------------------------------------------


class TestPrintTopResults:
    def _results(self, n: int = 5) -> list[dict]:
        return [
            {
                "momentum_window_minutes": 30,
                "min_first_window_return_bps": 60 + i * 10,
                "latest_entry_time": "12:00:00",
                "stop_loss_bps": 80,
                "target_bps": None,
                "allow_shorts": False,
                "max_trades_per_symbol_per_day": 1,
                "error": None,
                "total_pnl": (i - 2) * 1000.0,
                "profit_factor": 0.5 + i * 0.1,
                "max_drawdown": 0.05 + i * 0.01,
                "win_rate": 0.4,
                "trade_count": 10 + i * 10,
                "average_trade_pnl": (i - 2) * 20.0,
                "sharpe_ratio": None,
                "sortino_ratio": None,
            }
            for i in range(n)
        ]

    def test_no_crash(self, capsys):
        print_top_results(self._results())

    def test_prints_in_sample_warning(self, capsys):
        print_top_results(self._results())
        assert "IN-SAMPLE" in capsys.readouterr().out

    def test_no_crash_empty_results(self, capsys):
        print_top_results([])

    def test_few_trades_message(self, capsys):
        results = self._results(n=2)  # trade_counts: 10, 20; both < 30
        print_top_results(results, min_trades_for_dd=30)
        assert "30" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Tests: no Zerodha dependency
# ---------------------------------------------------------------------------


class TestNoZerodhaDependency:
    def test_sweep_module_no_kiteconnect(self):
        import inspect

        import sweep_first_hour_momentum_params as mod

        assert "kiteconnect" not in inspect.getsource(mod)

    def test_sweep_module_no_zerodha(self):
        import inspect

        import sweep_first_hour_momentum_params as mod

        assert "zerodha" not in inspect.getsource(mod).lower()

    def test_sweep_module_no_live_execution(self):
        import inspect

        import sweep_first_hour_momentum_params as mod

        src = inspect.getsource(mod)
        assert "live_execution" not in src
        assert "place_order" not in src
