import json
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest
from scripts.validate_first_hour_symbol_specific import (
    PARAM_GRID,
    build_tasks,
    filter_candles,
    run_parallel,
    save_final,
)


@pytest.fixture
def sample_candles():
    """Two symbols with 3 bars each in different months."""
    df1 = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-01-01 09:15:00", "2025-01-01 09:16:00", "2025-01-01 09:17:00"]
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        }
    )
    df2 = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-02-01 09:15:00", "2025-02-01 09:16:00", "2025-02-01 09:17:00"]
            ),
            "open": [200.0, 201.0, 202.0],
            "high": [200.5, 201.5, 202.5],
            "low": [199.5, 200.5, 201.5],
            "close": [201.0, 202.0, 203.0],
            "volume": [2000, 2100, 2200],
        }
    )
    return {"SYM1": df1, "SYM2": df2}


class TestValidationScript:
    def test_build_tasks_count(self):
        symbols = ["S1", "S2"]
        max_combos = 10
        tasks = build_tasks(symbols, PARAM_GRID, max_combos=max_combos)
        # 2 symbols * 10 combos = 20 tasks
        assert len(tasks) == 20
        assert tasks[0][0] == "S1"
        assert isinstance(tasks[0][1], dict)

    def test_filter_candles_months(self, sample_candles):
        filtered = filter_candles(sample_candles, ["2025-01"])
        assert "SYM1" in filtered
        assert "SYM2" not in filtered
        assert len(filtered["SYM1"]) == 3

    def test_filter_candles_empty(self, sample_candles):
        filtered = filter_candles(sample_candles, [])
        assert len(filtered) == 2

    def test_run_parallel_workers_1_vs_2(self, sample_candles):
        """Ensure results are consistent between serial and parallel runs."""
        tasks = [("SYM1", list(build_tasks(["SYM1"], PARAM_GRID, max_combos=2))[0][1])]

        # We need to mock evaluate_task to avoid actual backtest overhead in unit tests
        # or just run it since it's only 1 task.
        # But Requirement 9 says 'workers=2 produces same results as workers=1 on synthetic data'.
        # Let's use real evaluate_task but with very small data.

        initial_cash = Decimal("100000")
        quantity = 10
        interval = "minute"

        res1 = run_parallel(tasks, sample_candles, 1, initial_cash, quantity, interval)
        res2 = run_parallel(tasks, sample_candles, 2, initial_cash, quantity, interval)

        assert len(res1) == len(res2) == 1
        assert res1[0]["symbol"] == res2[0]["symbol"] == "SYM1"
        assert res1[0]["total_pnl"] == res2[0]["total_pnl"]

    def test_save_final_writes_files(self, tmp_path):
        results = [
            {"symbol": "SYM1", "total_pnl": 100.0, "momentum_window_minutes": 15},
            {"symbol": "SYM2", "total_pnl": 50.0, "momentum_window_minutes": 30},
        ]
        save_final(results, tmp_path)

        csv_path = tmp_path / "first_hour_symbol_validation.csv"
        json_path = tmp_path / "first_hour_symbol_validation.json"

        assert csv_path.exists()
        assert json_path.exists()

        with open(json_path) as f:
            data = json.load(f)
        assert len(data) == 2
        # Check deterministic sort (by symbol in this case)
        assert data[0]["symbol"] == "SYM1"
        assert data[1]["symbol"] == "SYM2"

    @patch("scripts.validate_first_hour_symbol_specific.save_final")
    @patch("scripts.validate_first_hour_symbol_specific.run_parallel")
    @patch("scripts.validate_first_hour_symbol_specific.load_all_candles")
    def test_fast_mode_changes_defaults(self, mock_load, mock_run, mock_save, sample_candles):
        from scripts.validate_first_hour_symbol_specific import main

        mock_load.return_value = sample_candles
        mock_run.return_value = []

        # Simulate --fast
        main(["--fast", "--output-dir", "fake_out"])

        # Verify symbols used were the fast ones
        # Actually our mock_load returned sample_candles, so it uses SYM1, SYM2
        # BUT the logic sets 'symbols = _FAST_SYMBOLS'
        # Let's check the tasks passed to run_parallel
        args, _ = mock_run.call_args
        # max_combos should be 25. Symbols should be _FAST_SYMBOLS.
        # However, since they aren't in mock_load's return, only those that intersect would stay?
        # Actually load_all_candles is called with _FAST_SYMBOLS.
        assert mock_load.call_args[0][0] == ["TCS", "INFY", "ICICIBANK"]

    def test_no_live_imports(self):
        """Ensure script doesn't import live trading or zerodha modules."""
        import sys

        # Check if any forbidden modules are in sys.modules after importing our script
        forbidden = [
            "kiteconnect",
            "trading_engine.broker.zerodha",
            "trading_engine.live_execution",
        ]
        for f in forbidden:
            assert f not in sys.modules, f"Forbidden module {f} found in sys.modules"
