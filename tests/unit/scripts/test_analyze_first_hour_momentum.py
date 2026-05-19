"""Tests for analyze_first_hour_momentum.py.

All tests use synthetic in-memory data — no real files, no broker calls,
no Zerodha dependency.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_first_hour_momentum import (  # noqa: E402
    _BEST_CONFIG_PARAMS,
    analyze,
    breakdown_by,
    compute_metrics,
    load_fills_from_report,
    pair_fills,
    save_outputs,
    split_by_date,
    train_test_analysis,
)

# ---------------------------------------------------------------------------
# Synthetic fill factories
# ---------------------------------------------------------------------------

_SYMBOL_A = "RELIANCE"
_SYMBOL_B = "TCS"


def _ts(dt_str: str) -> datetime:
    """Parse ISO datetime string to UTC-aware datetime."""
    return datetime.fromisoformat(dt_str).replace(tzinfo=UTC)


def _fill(
    symbol: str,
    side: str,
    price: float,
    fees: float = 10.0,
    ts: str = "2025-03-15T09:30:00",
    quantity: int = 10,
) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": Decimal(str(price)),
        "fees": Decimal(str(fees)),
        "timestamp": _ts(ts),
    }


def _round_trip(
    symbol: str = _SYMBOL_A,
    buy_price: float = 100.0,
    sell_price: float = 105.0,
    buy_fees: float = 10.0,
    sell_fees: float = 10.0,
    buy_ts: str = "2025-03-15T09:30:00",
    sell_ts: str = "2025-03-15T15:15:00",
    quantity: int = 10,
) -> list[dict]:
    return [
        _fill(symbol, "BUY", buy_price, buy_fees, buy_ts, quantity),
        _fill(symbol, "SELL", sell_price, sell_fees, sell_ts, quantity),
    ]


# ---------------------------------------------------------------------------
# Tests: pair_fills
# ---------------------------------------------------------------------------


class TestPairFills:
    def test_single_round_trip_produces_one_trade(self):
        fills = _round_trip()
        trades = pair_fills(fills)
        assert len(trades) == 1

    def test_gross_pnl_is_revenue_minus_cost(self):
        fills = _round_trip(buy_price=100, sell_price=110, quantity=10)
        trades = pair_fills(fills)
        assert len(trades) == 1
        # gross = 10 * (110 - 100) = 100
        assert abs(float(trades[0]["gross_pnl"]) - 100.0) < 0.01

    def test_net_pnl_is_gross_minus_fees(self):
        fills = _round_trip(buy_price=100, sell_price=110, buy_fees=5, sell_fees=7, quantity=10)
        trades = pair_fills(fills)
        assert len(trades) == 1
        # gross=100, fees=12, net=88
        assert abs(float(trades[0]["net_pnl"]) - 88.0) < 0.01

    def test_gross_pnl_identity(self):
        """net_pnl + total_fees == gross_pnl."""
        fills = _round_trip(buy_price=100, sell_price=105, buy_fees=8, sell_fees=9)
        trades = pair_fills(fills)
        t = trades[0]
        assert abs(float(t["net_pnl"] + t["total_fees"] - t["gross_pnl"])) < 0.001

    def test_two_symbols_produce_two_trades(self):
        fills = _round_trip(_SYMBOL_A) + _round_trip(_SYMBOL_B)
        trades = pair_fills(fills)
        assert len(trades) == 2

    def test_symbol_attribution_correct(self):
        fills = _round_trip(_SYMBOL_A) + _round_trip(_SYMBOL_B, buy_price=200, sell_price=195)
        trades = pair_fills(fills)
        syms = {t["symbol"] for t in trades}
        assert syms == {_SYMBOL_A, _SYMBOL_B}

    def test_fifo_queue_consumed_correctly(self):
        """Second round-trip must not reuse cost from first buy."""
        fills = _round_trip(
            buy_price=100,
            sell_price=110,
            buy_ts="2025-01-10T09:30:00",
            sell_ts="2025-01-10T15:15:00",
        ) + _round_trip(
            buy_price=200,
            sell_price=220,
            buy_ts="2025-01-11T09:30:00",
            sell_ts="2025-01-11T15:15:00",
        )
        trades = pair_fills(fills)
        assert len(trades) == 2
        first_gross = float(trades[0]["gross_pnl"])
        second_gross = float(trades[1]["gross_pnl"])
        # First: 10*(110-100)=100; Second: 10*(220-200)=200
        assert abs(first_gross - 100.0) < 0.1
        assert abs(second_gross - 200.0) < 0.1

    def test_unpaired_buy_produces_no_trade(self):
        fills = [_fill(_SYMBOL_A, "BUY", 100.0)]
        trades = pair_fills(fills)
        assert trades == []

    def test_empty_fills_returns_empty(self):
        assert pair_fills([]) == []

    def test_losing_trade_has_negative_net_pnl(self):
        fills = _round_trip(buy_price=110, sell_price=100, buy_fees=5, sell_fees=5)
        trades = pair_fills(fills)
        assert trades[0]["net_pnl"] < 0


# ---------------------------------------------------------------------------
# Tests: compute_metrics
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def _make_trades(self, net_pnls: list[float]) -> list[dict]:
        trades = []
        for i, p in enumerate(net_pnls):
            gross = p + 10.0  # synthetic: fees = 10
            # Spread across Jan (day 1–31 capped at 28) to avoid month-overflow.
            day = (i % 28) + 1
            month = (i // 28) + 1
            trades.append(
                {
                    "symbol": _SYMBOL_A,
                    "side": "LONG",
                    "entry_time": _ts(f"2025-{month:02d}-{day:02d}T09:30:00"),
                    "exit_time": _ts(f"2025-{month:02d}-{day:02d}T15:15:00"),
                    "entry_price": Decimal("100"),
                    "exit_price": Decimal("110"),
                    "quantity": 10,
                    "gross_pnl": Decimal(str(gross)),
                    "entry_fees": Decimal("5"),
                    "exit_fees": Decimal("5"),
                    "total_fees": Decimal("10"),
                    "net_pnl": Decimal(str(p)),
                }
            )
        return trades

    def test_empty_returns_zero_trade_count(self):
        m = compute_metrics([])
        assert m["trade_count"] == 0

    def test_trade_count_correct(self):
        trades = self._make_trades([10.0, -5.0, 20.0])
        assert compute_metrics(trades)["trade_count"] == 3

    def test_total_net_pnl_correct(self):
        trades = self._make_trades([10.0, -5.0, 20.0])
        m = compute_metrics(trades)
        assert abs(m["total_net_pnl"] - 25.0) < 0.01

    def test_win_rate_correct(self):
        trades = self._make_trades([10.0, -5.0, 20.0])
        m = compute_metrics(trades)
        assert abs(m["win_rate"] - 2 / 3) < 0.001

    def test_profit_factor_none_when_no_losses(self):
        trades = self._make_trades([10.0, 20.0, 5.0])
        m = compute_metrics(trades)
        assert m["profit_factor"] is None

    def test_profit_factor_computed_when_losses_exist(self):
        # wins=30, losses=-10 → PF=3.0
        trades = self._make_trades([30.0, -10.0])
        m = compute_metrics(trades)
        assert m["profit_factor"] is not None
        assert abs(m["profit_factor"] - 3.0) < 0.01

    def test_warning_on_small_sample(self):
        trades = self._make_trades([1.0] * 5)
        m = compute_metrics(trades)
        assert m["warning"] is not None
        assert "5" in m["warning"]

    def test_no_warning_for_30_plus_trades(self):
        trades = self._make_trades([1.0] * 30)
        m = compute_metrics(trades)
        assert m["warning"] is None

    def test_gross_pnl_equals_net_plus_fees(self):
        fills = _round_trip(buy_price=100, sell_price=110, buy_fees=8, sell_fees=9)
        trades = pair_fills(fills)
        m = compute_metrics(trades)
        # gross = net + fees within 1 INR
        assert abs(m["total_gross_pnl"] - (m["total_net_pnl"] + m["total_fees"])) < 1.0


# ---------------------------------------------------------------------------
# Tests: split_by_date
# ---------------------------------------------------------------------------


class TestSplitByDate:
    def _trades(self) -> list[dict]:
        trade = {
            "symbol": _SYMBOL_A,
            "side": "LONG",
            "entry_price": Decimal("100"),
            "exit_price": Decimal("110"),
            "quantity": 10,
            "gross_pnl": Decimal("100"),
            "entry_fees": Decimal("5"),
            "exit_fees": Decimal("5"),
            "total_fees": Decimal("10"),
            "net_pnl": Decimal("90"),
        }
        result = []
        for ts_str in [
            "2025-01-15T09:30:00",
            "2025-06-15T09:30:00",
            "2025-10-15T09:30:00",
            "2026-01-15T09:30:00",
        ]:
            t = dict(trade)
            t["entry_time"] = _ts(ts_str)
            t["exit_time"] = _ts(ts_str)
            result.append(t)
        return result

    def test_train_split_includes_only_train_dates(self):
        train = split_by_date(self._trades(), date(2025, 1, 1), date(2025, 9, 30))
        assert len(train) == 2  # Jan and Jun

    def test_test_split_includes_only_test_dates(self):
        test = split_by_date(self._trades(), date(2025, 10, 1), date(2026, 1, 31))
        assert len(test) == 2  # Oct and Jan

    def test_boundary_dates_inclusive(self):
        trades = self._trades()
        result = split_by_date(trades, date(2025, 1, 15), date(2025, 1, 15))
        assert len(result) == 1

    def test_empty_range_returns_empty(self):
        result = split_by_date(self._trades(), date(2024, 1, 1), date(2024, 12, 31))
        assert result == []

    def test_no_overlap_with_second_period(self):
        train = split_by_date(self._trades(), date(2025, 1, 1), date(2025, 9, 30))
        test = split_by_date(self._trades(), date(2025, 10, 1), date(2026, 1, 31))
        train_times = {t["entry_time"] for t in train}
        test_times = {t["entry_time"] for t in test}
        assert train_times.isdisjoint(test_times)


# ---------------------------------------------------------------------------
# Tests: breakdown_by (symbol split)
# ---------------------------------------------------------------------------


class TestBreakdownBy:
    def _trades(self) -> list[dict]:
        result = []
        for sym, pnl in [(_SYMBOL_A, 100.0), (_SYMBOL_A, -50.0), (_SYMBOL_B, 200.0)]:
            gross = pnl + 10.0
            result.append(
                {
                    "symbol": sym,
                    "side": "LONG",
                    "entry_time": _ts("2025-03-15T09:30:00"),
                    "exit_time": _ts("2025-03-15T15:15:00"),
                    "entry_price": Decimal("100"),
                    "exit_price": Decimal("110"),
                    "quantity": 10,
                    "gross_pnl": Decimal(str(gross)),
                    "entry_fees": Decimal("5"),
                    "exit_fees": Decimal("5"),
                    "total_fees": Decimal("10"),
                    "net_pnl": Decimal(str(pnl)),
                }
            )
        return result

    def test_symbol_split_has_correct_keys(self):
        groups = breakdown_by(self._trades(), lambda t: t["symbol"])
        assert set(groups.keys()) == {_SYMBOL_A, _SYMBOL_B}

    def test_symbol_trade_counts_correct(self):
        groups = breakdown_by(self._trades(), lambda t: t["symbol"])
        assert groups[_SYMBOL_A]["trade_count"] == 2
        assert groups[_SYMBOL_B]["trade_count"] == 1

    def test_symbol_pnl_correct(self):
        groups = breakdown_by(self._trades(), lambda t: t["symbol"])
        assert abs(groups[_SYMBOL_A]["total_net_pnl"] - 50.0) < 0.01
        assert abs(groups[_SYMBOL_B]["total_net_pnl"] - 200.0) < 0.01


# ---------------------------------------------------------------------------
# Tests: train_test_analysis
# ---------------------------------------------------------------------------


class TestTrainTestAnalysis:
    def _make_trades_for_two_periods(self) -> list[dict]:
        result = []
        dates_and_pnls = [
            ("2025-03-15", _SYMBOL_A, 50.0),
            ("2025-06-10", _SYMBOL_A, -20.0),
            ("2025-11-01", _SYMBOL_A, 30.0),
            ("2025-12-15", _SYMBOL_B, -10.0),
        ]
        for dt_str, sym, pnl in dates_and_pnls:
            gross = pnl + 10.0
            result.append(
                {
                    "symbol": sym,
                    "side": "LONG",
                    "entry_time": _ts(f"{dt_str}T09:30:00"),
                    "exit_time": _ts(f"{dt_str}T15:15:00"),
                    "entry_price": Decimal("100"),
                    "exit_price": Decimal("110"),
                    "quantity": 10,
                    "gross_pnl": Decimal(str(gross)),
                    "entry_fees": Decimal("5"),
                    "exit_fees": Decimal("5"),
                    "total_fees": Decimal("10"),
                    "net_pnl": Decimal(str(pnl)),
                }
            )
        return result

    def test_train_count_correct(self):
        trades = self._make_trades_for_two_periods()
        result = train_test_analysis(
            trades,
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            [_SYMBOL_A, _SYMBOL_B],
        )
        assert result["all_symbols"]["train"]["trade_count"] == 2

    def test_test_count_correct(self):
        trades = self._make_trades_for_two_periods()
        result = train_test_analysis(
            trades,
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            [_SYMBOL_A, _SYMBOL_B],
        )
        assert result["all_symbols"]["test"]["trade_count"] == 2

    def test_by_symbol_keys_present(self):
        trades = self._make_trades_for_two_periods()
        result = train_test_analysis(
            trades,
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            [_SYMBOL_A, _SYMBOL_B],
        )
        assert _SYMBOL_A in result["by_symbol"]
        assert _SYMBOL_B in result["by_symbol"]

    def test_positive_oos_symbols_identified(self):
        trades = self._make_trades_for_two_periods()
        result = train_test_analysis(
            trades,
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            [_SYMBOL_A, _SYMBOL_B],
        )
        positive = result["net_positive_oos"]
        # SYMBOL_A has net 30 in test, SYMBOL_B has net -10
        assert _SYMBOL_A in positive
        assert _SYMBOL_B not in positive

    def test_best_worst_test_symbol_identified(self):
        trades = self._make_trades_for_two_periods()
        result = train_test_analysis(
            trades,
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            [_SYMBOL_A, _SYMBOL_B],
        )
        assert result["best_test_symbol"] == _SYMBOL_A
        assert result["worst_test_symbol"] == _SYMBOL_B

    def test_excluded_worst_train_result_present(self):
        trades = self._make_trades_for_two_periods()
        result = train_test_analysis(
            trades,
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            [_SYMBOL_A, _SYMBOL_B],
        )
        assert "excluded_worst_test" in result
        assert isinstance(result["excluded_worst_test"], dict)


# ---------------------------------------------------------------------------
# Tests: analyze (end-to-end)
# ---------------------------------------------------------------------------


class TestAnalyze:
    def _fills(self) -> list[dict]:
        fills = []
        # SYMBOL_A: 5 round-trips spanning Jan–Oct 2025
        months = [1, 3, 5, 7, 11]
        buy_prices = [100, 110, 105, 120, 130]
        sell_prices = [108, 107, 112, 115, 140]
        for m, bp, sp in zip(months, buy_prices, sell_prices, strict=True):
            day = "15"
            fills += _round_trip(
                _SYMBOL_A,
                bp,
                sp,
                buy_ts=f"2025-{m:02d}-{day}T09:30:00",
                sell_ts=f"2025-{m:02d}-{day}T15:15:00",
            )
        # SYMBOL_B: 2 round-trips in test period
        for m, bp, sp in [(10, 200, 205), (12, 210, 208)]:
            fills += _round_trip(
                _SYMBOL_B,
                bp,
                sp,
                buy_ts=f"2025-{m:02d}-15T09:30:00",
                sell_ts=f"2025-{m:02d}-15T15:15:00",
            )
        return fills

    def test_analyze_returns_dict(self):
        result = analyze(
            self._fills(),
            [_SYMBOL_A, _SYMBOL_B],
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            _BEST_CONFIG_PARAMS,
        )
        assert isinstance(result, dict)

    def test_trade_count_equals_fill_pairs(self):
        fills = self._fills()
        result = analyze(
            fills,
            [_SYMBOL_A, _SYMBOL_B],
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            _BEST_CONFIG_PARAMS,
        )
        assert result["trade_count"] == len(fills) // 2

    def test_by_symbol_keys_present(self):
        result = analyze(
            self._fills(),
            [_SYMBOL_A, _SYMBOL_B],
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            _BEST_CONFIG_PARAMS,
        )
        assert _SYMBOL_A in result["by_symbol"]
        assert _SYMBOL_B in result["by_symbol"]

    def test_train_test_present(self):
        result = analyze(
            self._fills(),
            [_SYMBOL_A, _SYMBOL_B],
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            _BEST_CONFIG_PARAMS,
        )
        assert "train_test" in result
        assert "all_symbols" in result["train_test"]

    def test_gross_pnl_identity_in_overall(self):
        result = analyze(
            self._fills(),
            [_SYMBOL_A, _SYMBOL_B],
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            _BEST_CONFIG_PARAMS,
        )
        m = result["overall"]
        gp = m["total_gross_pnl"]
        np_ = m["total_net_pnl"]
        fees = m["total_fees"]
        assert abs(gp - (np_ + fees)) < 1.0, f"gross={gp} != net+fees={np_ + fees}"

    def test_empty_fills_returns_warning(self):
        result = analyze(
            [],
            [_SYMBOL_A],
            date(2025, 1, 1),
            date(2025, 9, 30),
            date(2025, 10, 1),
            date(2026, 1, 31),
            _BEST_CONFIG_PARAMS,
        )
        assert "warning" in result


# ---------------------------------------------------------------------------
# Tests: load_fills_from_report
# ---------------------------------------------------------------------------


class TestLoadFillsFromReport:
    def test_returns_empty_list_for_missing_file(self, tmp_path: Path):
        fills = load_fills_from_report(tmp_path / "nonexistent.json")
        assert fills == []

    def test_loads_fills_from_valid_json(self, tmp_path: Path):
        report = {
            "fills": [
                {
                    "fill_id": "f1",
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 10,
                    "price": "100.50",
                    "fees": "15.25",
                    "timestamp": "2025-03-15T09:30:00+05:30",
                }
            ]
        }
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        fills = load_fills_from_report(p)
        assert len(fills) == 1
        assert fills[0]["symbol"] == "RELIANCE"
        assert fills[0]["price"] == Decimal("100.50")

    def test_returns_empty_for_corrupt_json(self, tmp_path: Path, capsys):
        p = tmp_path / "bad.json"
        p.write_bytes(b"not json{{")
        fills = load_fills_from_report(p)
        assert fills == []

    def test_malformed_fill_skipped(self, tmp_path: Path, capsys):
        report = {
            "fills": [
                {"fill_id": "bad", "symbol": "X"},  # missing required fields
                {
                    "fill_id": "ok",
                    "symbol": "RELIANCE",
                    "side": "BUY",
                    "quantity": 10,
                    "price": "100",
                    "fees": "10",
                    "timestamp": "2025-03-15T09:30:00",
                },
            ]
        }
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        fills = load_fills_from_report(p)
        assert len(fills) == 1  # bad fill skipped


# ---------------------------------------------------------------------------
# Tests: save_outputs
# ---------------------------------------------------------------------------


class TestSaveOutputs:
    def _analysis(self) -> dict:
        return {
            "config": {"momentum_window_minutes": "15"},
            "fill_count": 4,
            "trade_count": 2,
            "symbols": [_SYMBOL_A, _SYMBOL_B],
            "overall": {
                "trade_count": 2,
                "total_net_pnl": 50.0,
                "total_gross_pnl": 70.0,
                "total_fees": 20.0,
                "win_rate": 0.5,
                "profit_factor": None,
                "avg_net_pnl": 25.0,
                "avg_gross_pnl": 35.0,
                "max_drawdown": 0.02,
                "warning": None,
            },
            "by_symbol": {
                _SYMBOL_A: {
                    "trade_count": 1,
                    "total_net_pnl": 30.0,
                    "total_gross_pnl": 40.0,
                    "total_fees": 10.0,
                    "win_rate": 1.0,
                    "profit_factor": None,
                    "avg_net_pnl": 30.0,
                    "avg_gross_pnl": 40.0,
                    "max_drawdown": 0.0,
                    "warning": "Small sample: only 1 trades — interpret results with caution.",
                },
                _SYMBOL_B: {
                    "trade_count": 1,
                    "total_net_pnl": 20.0,
                    "total_gross_pnl": 30.0,
                    "total_fees": 10.0,
                    "win_rate": 1.0,
                    "profit_factor": None,
                    "avg_net_pnl": 20.0,
                    "avg_gross_pnl": 30.0,
                    "max_drawdown": 0.0,
                    "warning": None,
                },
            },
            "by_month": {},
            "by_quarter": {},
            "by_day_of_week": {},
            "by_side": {},
            "by_entry_hour": {},
            "train_test": {
                "train_period": "2025-01-01 to 2025-09-30",
                "test_period": "2025-10-01 to 2026-01-31",
                "all_symbols": {
                    "train": {
                        "trade_count": 1,
                        "total_net_pnl": 30.0,
                        "total_gross_pnl": 40.0,
                        "total_fees": 10.0,
                        "win_rate": 1.0,
                        "profit_factor": None,
                        "avg_net_pnl": 30.0,
                        "avg_gross_pnl": 40.0,
                        "max_drawdown": 0.0,
                        "warning": None,
                    },
                    "test": {
                        "trade_count": 1,
                        "total_net_pnl": 20.0,
                        "total_gross_pnl": 30.0,
                        "total_fees": 10.0,
                        "win_rate": 1.0,
                        "profit_factor": None,
                        "avg_net_pnl": 20.0,
                        "avg_gross_pnl": 30.0,
                        "max_drawdown": 0.0,
                        "warning": None,
                    },
                },
                "by_symbol": {
                    _SYMBOL_A: {
                        "train": {
                            "trade_count": 1,
                            "total_net_pnl": 30.0,
                            "total_gross_pnl": 40.0,
                            "total_fees": 10.0,
                            "win_rate": 1.0,
                            "profit_factor": None,
                            "avg_net_pnl": 30.0,
                            "avg_gross_pnl": 40.0,
                            "max_drawdown": 0.0,
                            "warning": None,
                        },
                        "test": {
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
                        },
                    },
                    _SYMBOL_B: {
                        "train": {
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
                        },
                        "test": {
                            "trade_count": 1,
                            "total_net_pnl": 20.0,
                            "total_gross_pnl": 30.0,
                            "total_fees": 10.0,
                            "win_rate": 1.0,
                            "profit_factor": None,
                            "avg_net_pnl": 20.0,
                            "avg_gross_pnl": 30.0,
                            "max_drawdown": 0.0,
                            "warning": None,
                        },
                    },
                },
                "best_test_symbol": _SYMBOL_B,
                "worst_test_symbol": _SYMBOL_A,
                "net_positive_oos": [_SYMBOL_B],
                "excluded_worst_train_symbol": _SYMBOL_B,
                "excluded_worst_test": {
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
                },
            },
        }

    def test_json_created(self, tmp_path: Path):
        json_path, _ = save_outputs(self._analysis(), tmp_path)
        assert json_path.exists()

    def test_csv_created(self, tmp_path: Path):
        _, csv_path = save_outputs(self._analysis(), tmp_path)
        assert csv_path.exists()

    def test_json_is_valid(self, tmp_path: Path):
        json_path, _ = save_outputs(self._analysis(), tmp_path)
        with json_path.open() as fh:
            data = json.load(fh)
        assert isinstance(data, dict)

    def test_csv_has_symbol_rows(self, tmp_path: Path):
        _, csv_path = save_outputs(self._analysis(), tmp_path)
        df = pd.read_csv(csv_path)
        assert set(df["symbol"]) == {_SYMBOL_A, _SYMBOL_B}

    def test_output_dir_created_if_missing(self, tmp_path: Path):
        subdir = tmp_path / "nested" / "out"
        save_outputs(self._analysis(), subdir)
        assert subdir.exists()


# ---------------------------------------------------------------------------
# Tests: no Zerodha dependency
# ---------------------------------------------------------------------------


class TestNoZerodhaDependency:
    def test_module_no_kiteconnect(self):
        import inspect

        import analyze_first_hour_momentum as mod

        assert "kiteconnect" not in inspect.getsource(mod)

    def test_module_no_zerodha(self):
        import inspect

        import analyze_first_hour_momentum as mod

        assert "zerodha" not in inspect.getsource(mod).lower()

    def test_module_no_live_execution(self):
        import inspect

        import analyze_first_hour_momentum as mod

        src = inspect.getsource(mod)
        assert "live_execution" not in src
        assert "place_order" not in src
