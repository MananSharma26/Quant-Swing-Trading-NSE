"""Tests for scripts/analyze_orb_report.py.

All tests use a small fake report fixture — no real files or Zerodha calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable.
# File lives at tests/unit/scripts/test_*.py → parents[3] = project root.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_orb_report import (  # noqa: E402
    _check_missing_fields,
    _trade_stats,
    analyze,
    compute_breakdowns,
    reconstruct_trades,
)

# ---------------------------------------------------------------------------
# Fake report fixture helpers
# ---------------------------------------------------------------------------

_TS_BUY = "2025-01-02T09:35:00+05:30"
_TS_SELL = "2025-01-02T10:15:00+05:30"
_TS_BUY2 = "2025-02-03T09:40:00+05:30"
_TS_SELL2 = "2025-02-03T11:00:00+05:30"


def _fill(fill_id: str, symbol: str, side: str, price: float, fees: float, ts: str) -> dict:
    return {
        "fill_id": fill_id,
        "symbol": symbol,
        "side": side,
        "quantity": 10,
        "price": str(price),
        "fees": str(fees),
        "timestamp": ts,
    }


def _make_fills_win() -> list[dict]:
    """One winning trade: buy 100, sell 110 → gross +100."""
    return [
        _fill("b1", "RELIANCE", "BUY", 100.0, 1.0, _TS_BUY),
        _fill("s1", "RELIANCE", "SELL", 110.0, 1.0, _TS_SELL),
    ]


def _make_fills_loss() -> list[dict]:
    """One losing trade: buy 100, sell 90 → gross -100."""
    return [
        _fill("b2", "RELIANCE", "BUY", 100.0, 1.0, _TS_BUY),
        _fill("s2", "RELIANCE", "SELL", 90.0, 1.0, _TS_SELL),
    ]


def _make_multi_symbol_fills() -> list[dict]:
    """Two symbols, two trades each month."""
    return [
        # RELIANCE Jan win
        _fill("b1", "RELIANCE", "BUY", 100.0, 1.0, _TS_BUY),
        _fill("s1", "RELIANCE", "SELL", 110.0, 1.0, _TS_SELL),
        # TCS Feb loss
        _fill("b2", "TCS", "BUY", 200.0, 2.0, _TS_BUY2),
        _fill("s2", "TCS", "SELL", 190.0, 2.0, _TS_SELL2),
    ]


def _make_fake_report(fills: list[dict] | None = None) -> dict:
    return {
        "strategy_id": "orb_v1",
        "symbols": ["RELIANCE"],
        "start_time": "2025-01-01T09:15:00+05:30",
        "end_time": "2025-01-31T15:30:00+05:30",
        "initial_cash": "500000",
        "final_equity": "500050",
        "metrics": {
            "total_pnl": "50",
            "total_fees": "2",
            "win_rate": "1.0",
            "profit_factor": "999",
            "max_drawdown": "0",
            "trade_count": 1,
            "winning_trades": 1,
            "losing_trades": 0,
            "average_win": "50",
            "average_loss": "0",
            "expectancy": "50",
            "realized_pnl": "50",
            "unrealized_pnl": "0",
            "total_return": "0.0001",
            "best_trade_pnl": "50",
            "worst_trade_pnl": "50",
            "sharpe_ratio": 1.0,
            "sortino_ratio": 1.0,
            "cagr": 0.1,
            "average_trade_pnl": "50",
        },
        "fills": fills if fills is not None else _make_fills_win(),
        "equity_curve": [],
        "parameters": {"interval": "minute", "opening_range_minutes": 15},
        "rejected_risk_decisions": [],
        "validation_result": None,
    }


# ---------------------------------------------------------------------------
# Tests: reconstruct_trades
# ---------------------------------------------------------------------------


class TestReconstructTrades:
    def test_win_trade_gross_pnl(self):
        fills = _make_fills_win()
        trades, _ = reconstruct_trades(fills)
        assert len(trades) == 1
        assert trades[0]["gross_pnl"] == pytest.approx((110.0 - 100.0) * 10)

    def test_loss_trade_gross_pnl(self):
        fills = _make_fills_loss()
        trades, _ = reconstruct_trades(fills)
        assert len(trades) == 1
        assert trades[0]["gross_pnl"] == pytest.approx((90.0 - 100.0) * 10)

    def test_net_pnl_deducts_fees(self):
        fills = _make_fills_win()
        trades, _ = reconstruct_trades(fills)
        # gross = 100, fees = 1+1 = 2, net = 98
        assert trades[0]["net_pnl"] == pytest.approx(98.0)

    def test_symbol_assigned_correctly(self):
        trades, _ = reconstruct_trades(_make_fills_win())
        assert trades[0]["symbol"] == "RELIANCE"

    def test_two_symbols_produce_two_trades(self):
        trades, _ = reconstruct_trades(_make_multi_symbol_fills())
        assert len(trades) == 2
        symbols = {t["symbol"] for t in trades}
        assert symbols == {"RELIANCE", "TCS"}

    def test_holding_minutes_calculated(self):
        trades, _ = reconstruct_trades(_make_fills_win())
        # _TS_BUY → _TS_SELL is 40 minutes
        assert trades[0]["holding_minutes"] == pytest.approx(40.0)

    def test_unmatched_buy_produces_warning(self):
        fills = [_fill("b1", "RELIANCE", "BUY", 100.0, 1.0, _TS_BUY)]
        _, warnings = reconstruct_trades(fills)
        assert any("BUY" in w for w in warnings)

    def test_empty_fills_returns_empty_trades(self):
        trades, warnings = reconstruct_trades([])
        assert trades == []
        assert warnings == []

    def test_fifo_ordering_for_multiple_buys(self):
        """Second SELL matches the first BUY (FIFO)."""
        fills = [
            _fill("b1", "RELIANCE", "BUY", 100.0, 0.0, "2025-01-02T09:30:00+05:30"),
            _fill("b2", "RELIANCE", "BUY", 120.0, 0.0, "2025-01-02T09:31:00+05:30"),
            _fill("s1", "RELIANCE", "SELL", 130.0, 0.0, "2025-01-02T10:00:00+05:30"),
        ]
        trades, _ = reconstruct_trades(fills)
        assert len(trades) == 1
        # First BUY at 100, SELL at 130 → gross = 30 * 10 = 300
        assert trades[0]["entry_price"] == 100.0
        assert trades[0]["gross_pnl"] == pytest.approx(300.0)

    def test_entry_and_exit_timestamps_set(self):
        trades, _ = reconstruct_trades(_make_fills_win())
        assert trades[0]["entry_ts"] is not None
        assert trades[0]["exit_ts"] is not None


# ---------------------------------------------------------------------------
# Tests: _trade_stats
# ---------------------------------------------------------------------------


class TestTradeStats:
    def _winning_trade(self, net: float = 100.0) -> dict:
        return {
            "gross_pnl": net + 2,
            "fees": 2.0,
            "net_pnl": net,
            "holding_minutes": 30.0,
        }

    def _losing_trade(self, net: float = -50.0) -> dict:
        return {
            "gross_pnl": net + 2,
            "fees": 2.0,
            "net_pnl": net,
            "holding_minutes": 20.0,
        }

    def test_empty_list_returns_zero_count(self):
        stats = _trade_stats([])
        assert stats["trade_count"] == 0

    def test_trade_count(self):
        trades = [self._winning_trade(), self._losing_trade()]
        assert _trade_stats(trades)["trade_count"] == 2

    def test_win_rate(self):
        trades = [self._winning_trade(), self._winning_trade(), self._losing_trade()]
        assert _trade_stats(trades)["win_rate"] == pytest.approx(2 / 3, rel=1e-4)

    def test_profit_factor(self):
        trades = [self._winning_trade(100.0), self._losing_trade(-50.0)]
        pf = _trade_stats(trades)["profit_factor"]
        assert pf == pytest.approx(2.0, rel=1e-4)

    def test_avg_win(self):
        trades = [self._winning_trade(100.0), self._winning_trade(200.0)]
        assert _trade_stats(trades)["avg_win"] == pytest.approx(150.0)

    def test_avg_loss(self):
        trades = [self._losing_trade(-50.0), self._losing_trade(-100.0)]
        assert _trade_stats(trades)["avg_loss"] == pytest.approx(-75.0)

    def test_avg_holding_minutes(self):
        trades = [self._winning_trade(), self._losing_trade()]
        assert _trade_stats(trades)["avg_holding_minutes"] == pytest.approx(25.0)

    def test_avg_holding_none_when_missing(self):
        trades = [
            {**self._winning_trade(), "holding_minutes": None},
        ]
        assert _trade_stats(trades)["avg_holding_minutes"] is None

    def test_net_pnl_sum(self):
        trades = [self._winning_trade(100.0), self._losing_trade(-30.0)]
        assert _trade_stats(trades)["net_pnl"] == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# Tests: compute_breakdowns
# ---------------------------------------------------------------------------


class TestComputeBreakdowns:
    def _trades(self) -> list[dict]:
        fills = _make_multi_symbol_fills()
        trades, _ = reconstruct_trades(fills)
        return trades

    def test_by_symbol_keys(self):
        bds = compute_breakdowns(self._trades())
        assert "RELIANCE" in bds["by_symbol"]
        assert "TCS" in bds["by_symbol"]

    def test_by_month_keys(self):
        bds = compute_breakdowns(self._trades())
        assert "2025-01" in bds["by_month"]
        assert "2025-02" in bds["by_month"]

    def test_by_day_of_week_keys(self):
        bds = compute_breakdowns(self._trades())
        days = set(bds["by_day_of_week"].keys())
        assert days <= set(
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        )

    def test_by_entry_hour_keys(self):
        bds = compute_breakdowns(self._trades())
        hours = bds["by_entry_hour"]
        # Entries are 09:35 and 09:40 → both in 09:00 bucket
        assert len(hours) >= 1

    def test_by_exit_reason_unknown_when_missing(self):
        bds = compute_breakdowns(self._trades())
        assert "unknown" in bds["by_exit_reason"]

    def test_overall_trade_count(self):
        trades = self._trades()
        bds = compute_breakdowns(trades)
        assert bds["overall"]["trade_count"] == len(trades)

    def test_overall_net_pnl_matches_sum(self):
        trades = self._trades()
        bds = compute_breakdowns(trades)
        expected = sum(t["net_pnl"] for t in trades)
        assert bds["overall"]["net_pnl"] == pytest.approx(expected, rel=1e-4)


# ---------------------------------------------------------------------------
# Tests: _check_missing_fields
# ---------------------------------------------------------------------------


class TestCheckMissingFields:
    def test_no_missing_fields_for_complete_report(self):
        report = _make_fake_report()
        missing = _check_missing_fields(report)
        # trades is always "missing" (we reconstruct from fills)
        for m in missing:
            assert "trades" in m  # only the expected trades notice

    def test_missing_fills_reported(self):
        report = _make_fake_report(fills=[])
        missing = _check_missing_fields(report)
        assert any("fills" in m for m in missing)

    def test_missing_metric_reported(self):
        report = _make_fake_report()
        del report["metrics"]["total_pnl"]
        missing = _check_missing_fields(report)
        assert any("total_pnl" in m for m in missing)


# ---------------------------------------------------------------------------
# Tests: analyze (integration, file-based)
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_analyze_returns_dict(self, tmp_path: Path):
        report = _make_fake_report()
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        assert isinstance(result, dict)

    def test_analyze_has_breakdowns(self, tmp_path: Path):
        report = _make_fake_report()
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        assert "breakdowns" in result

    def test_analyze_has_trades(self, tmp_path: Path):
        report = _make_fake_report()
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        assert "trades" in result
        assert len(result["trades"]) == 1

    def test_analyze_empty_fills_returns_error(self, tmp_path: Path):
        report = _make_fake_report(fills=[])
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        assert "error" in result

    def test_analyze_multi_symbol(self, tmp_path: Path):
        report = _make_fake_report(fills=_make_multi_symbol_fills())
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        sym_keys = set(result["breakdowns"]["by_symbol"].keys())
        assert sym_keys == {"RELIANCE", "TCS"}

    def test_reconstruct_trade_count_in_output(self, tmp_path: Path):
        report = _make_fake_report(fills=_make_multi_symbol_fills())
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        assert result["reconstructed_trade_count"] == 2

    def test_output_json_is_serialisable(self, tmp_path: Path):
        """Ensure analysis can be round-tripped through JSON."""
        report = _make_fake_report(fills=_make_multi_symbol_fills())
        p = tmp_path / "report.json"
        p.write_text(json.dumps(report))
        result = analyze(p)
        # Should not raise
        json.dumps(result, default=str)
