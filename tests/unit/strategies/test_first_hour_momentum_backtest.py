"""Integration tests: First-Hour Momentum strategy running end-to-end in BacktestEngine."""

from __future__ import annotations

from datetime import time
from decimal import Decimal

import pandas as pd

from trading_engine.backtest.cost_model import CostModel
from trading_engine.backtest.data_feed import HistoricalDataFeed
from trading_engine.backtest.engine import BacktestEngine
from trading_engine.backtest.portfolio import BacktestPortfolio
from trading_engine.backtest.report import BacktestReport
from trading_engine.backtest.simulated_broker import SimulatedBroker
from trading_engine.backtest.slippage_model import SlippageModel
from trading_engine.domain.enums import Side
from trading_engine.strategies.first_hour_momentum import (
    FirstHourMomentumConfig,
    FirstHourMomentumStrategy,
)

# ---------------------------------------------------------------------------
# Zero-cost, zero-slippage helpers
# ---------------------------------------------------------------------------

_ZERO_COST = CostModel(
    brokerage_per_order=Decimal("0"),
    brokerage_cap=Decimal("0"),
    stt_rate=Decimal("0"),
    exchange_txn_rate=Decimal("0"),
    sebi_rate=Decimal("0"),
    stamp_duty_rate=Decimal("0"),
    gst_rate=Decimal("0"),
)
_ZERO_SLIP = SlippageModel(bps=Decimal("0"))


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def _test_config(**kwargs) -> FirstHourMomentumConfig:
    """Config tuned for 5-bar window synthetic tests."""
    defaults: dict = dict(
        strategy_id="fhm_test",
        momentum_window_minutes=5,
        earliest_entry_time=time(9, 20),
        latest_entry_time=time(12, 0),
        square_off_time=time(15, 15),
        min_first_window_return_bps=100.0,
        min_opening_range_bps=0.0,
        max_opening_range_bps=10000.0,
        min_bars_before_signal=5,
        stop_loss_bps=100.0,
        target_bps=None,
        trailing_stop_bps=None,
        require_price_above_vwap_for_longs=False,
        allow_shorts=False,
        quantity=10,
    )
    defaults.update(kwargs)
    return FirstHourMomentumConfig(**defaults)


def _make_engine(
    candles: dict[str, pd.DataFrame],
    config: FirstHourMomentumConfig | None = None,
    initial_cash: Decimal = Decimal("500000"),
) -> BacktestEngine:
    cfg = config or _test_config()
    strategy = FirstHourMomentumStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    broker = SimulatedBroker(portfolio, _ZERO_COST, _ZERO_SLIP)
    feed = HistoricalDataFeed(candles)
    return BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=cfg.strategy_id,
        symbols=list(candles.keys()),
    )


# ---------------------------------------------------------------------------
# Synthetic data builders
# ---------------------------------------------------------------------------


def _row(ts: str, o: float, h: float, lo: float, c: float, v: int = 1000) -> dict:
    return {
        "timestamp": pd.Timestamp(ts),
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "volume": v,
    }


# 5 uptrend window bars: open=100→close=104 (return=400bps > 100 threshold)
_WINDOW_ROWS = [
    _row("2024-01-15 09:15:00", 100, 101, 99, 100),
    _row("2024-01-15 09:16:00", 101, 102, 100, 101),
    _row("2024-01-15 09:17:00", 102, 103, 101, 102),
    _row("2024-01-15 09:18:00", 103, 104, 102, 103),
    _row("2024-01-15 09:19:00", 104, 105, 103, 104),
]


def _no_entry_df() -> pd.DataFrame:
    """Window bars then bars that don't extend momentum → no entry."""
    rows = _WINDOW_ROWS + [
        # close=103 <= first_window_close=104 → no entry
        _row("2024-01-15 09:20:00", 104, 105, 103, 103),
        _row("2024-01-15 09:21:00", 103, 104, 102, 103),
        _row("2024-01-15 15:15:00", 103, 104, 102, 103),
    ]
    return pd.DataFrame(rows)


def _entry_then_square_off_df() -> pd.DataFrame:
    """Window, entry at 09:20 (close=105 > fw_close=104), square-off at 15:15."""
    rows = _WINDOW_ROWS + [
        _row("2024-01-15 09:20:00", 104, 106, 104, 105),  # entry bar; close>fw_close
        _row("2024-01-15 09:21:00", 105, 106, 104.5, 105),  # hold; stop=103.95 not hit
        _row("2024-01-15 10:00:00", 105, 106, 104.5, 105),  # hold
        _row("2024-01-15 15:15:00", 105, 106, 104.5, 105),  # square-off
    ]
    return pd.DataFrame(rows)


def _entry_then_stop_df() -> pd.DataFrame:
    """Window, entry at 09:20 (close=105), stop hit at 09:21.

    Entry=105; stop = 105*(1-100/10000) = 103.95.
    Next bar low=103 <= 103.95 → stop.
    """
    rows = _WINDOW_ROWS + [
        _row("2024-01-15 09:20:00", 104, 106, 104, 105),  # entry
        _row("2024-01-15 09:21:00", 105, 106, 103, 104),  # stop hit (low=103 < 103.95)
        _row("2024-01-15 15:15:00", 104, 105, 103, 104),  # already exited
    ]
    return pd.DataFrame(rows)


def _entry_then_target_df() -> pd.DataFrame:
    """Window, entry at 09:20 (close=105), target=107.1 hit at 09:21.

    Entry=105; target = 105*(1+200/10000) = 107.1.
    Next bar high=108 >= 107.1 → target.
    """
    rows = _WINDOW_ROWS + [
        _row("2024-01-15 09:20:00", 104, 106, 104, 105),  # entry
        _row("2024-01-15 09:21:00", 105, 108, 105, 107),  # target hit (high=108)
        _row("2024-01-15 15:15:00", 107, 108, 106, 107),  # already exited
    ]
    return pd.DataFrame(rows)


def _weak_window_df() -> pd.DataFrame:
    """Flat window (return near 0) → no entry signal."""
    rows = [
        _row("2024-01-15 09:15:00", 100, 101, 99, 100),
        _row("2024-01-15 09:16:00", 100, 101, 99, 100),
        _row("2024-01-15 09:17:00", 100, 101, 99, 100),
        _row("2024-01-15 09:18:00", 100, 101, 99, 100),
        _row("2024-01-15 09:19:00", 100, 101, 99, 100),
        # close=101 > fw_close=100, but return_bps≈0 < threshold=100
        _row("2024-01-15 09:20:00", 100, 102, 100, 101),
        _row("2024-01-15 15:15:00", 100, 101, 99, 100),
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Tests: engine runs
# ---------------------------------------------------------------------------


class TestFHMEngineRun:
    def test_engine_runs_and_returns_report(self):
        engine = _make_engine({"RELIANCE": _no_entry_df()})
        report = engine.run()
        assert isinstance(report, BacktestReport)

    def test_report_has_correct_strategy_id(self):
        cfg = _test_config(strategy_id="fhm_check")
        engine = _make_engine({"RELIANCE": _no_entry_df()}, config=cfg)
        report = engine.run()
        assert report.strategy_id == "fhm_check"

    def test_equity_curve_is_populated(self):
        engine = _make_engine({"RELIANCE": _no_entry_df()})
        report = engine.run()
        assert len(report.equity_curve) > 0

    def test_start_and_end_time_set(self):
        engine = _make_engine({"RELIANCE": _no_entry_df()})
        report = engine.run()
        assert report.start_time is not None
        assert report.end_time is not None


# ---------------------------------------------------------------------------
# Tests: no-entry scenario
# ---------------------------------------------------------------------------


class TestFHMNoEntry:
    def test_weak_window_produces_no_fills(self):
        engine = _make_engine({"RELIANCE": _weak_window_df()})
        report = engine.run()
        assert report.fills == []

    def test_no_entry_when_close_not_above_fw_close(self):
        engine = _make_engine({"RELIANCE": _no_entry_df()})
        report = engine.run()
        assert report.fills == []

    def test_no_entry_equity_equals_initial(self):
        engine = _make_engine({"RELIANCE": _no_entry_df()}, initial_cash=Decimal("300000"))
        report = engine.run()
        assert report.final_equity == Decimal("300000")


# ---------------------------------------------------------------------------
# Tests: entry + square-off
# ---------------------------------------------------------------------------


class TestFHMSquareOff:
    def test_entry_produces_buy_fill(self):
        engine = _make_engine({"RELIANCE": _entry_then_square_off_df()})
        report = engine.run()
        buys = [f for f in report.fills if f.side == Side.BUY]
        assert len(buys) >= 1

    def test_square_off_produces_sell_fill(self):
        engine = _make_engine({"RELIANCE": _entry_then_square_off_df()})
        report = engine.run()
        sells = [f for f in report.fills if f.side == Side.SELL]
        assert len(sells) >= 1

    def test_entry_plus_square_off_gives_two_fills(self):
        engine = _make_engine({"RELIANCE": _entry_then_square_off_df()})
        report = engine.run()
        assert len(report.fills) == 2

    def test_buy_fill_symbol(self):
        engine = _make_engine({"RELIANCE": _entry_then_square_off_df()})
        report = engine.run()
        buys = [f for f in report.fills if f.side == Side.BUY]
        assert buys[0].symbol == "RELIANCE"


# ---------------------------------------------------------------------------
# Tests: stop loss
# ---------------------------------------------------------------------------


class TestFHMStopLoss:
    def test_stop_loss_gives_two_fills(self):
        engine = _make_engine({"RELIANCE": _entry_then_stop_df()})
        report = engine.run()
        assert len(report.fills) == 2

    def test_stop_loss_fill_sides(self):
        engine = _make_engine({"RELIANCE": _entry_then_stop_df()})
        report = engine.run()
        assert report.fills[0].side == Side.BUY
        assert report.fills[1].side == Side.SELL


# ---------------------------------------------------------------------------
# Tests: profit target
# ---------------------------------------------------------------------------


class TestFHMTarget:
    def test_target_gives_two_fills(self):
        engine = _make_engine(
            {"RELIANCE": _entry_then_target_df()},
            config=_test_config(target_bps=200.0),
        )
        report = engine.run()
        assert len(report.fills) == 2

    def test_target_fill_sides(self):
        engine = _make_engine(
            {"RELIANCE": _entry_then_target_df()},
            config=_test_config(target_bps=200.0),
        )
        report = engine.run()
        assert report.fills[0].side == Side.BUY
        assert report.fills[1].side == Side.SELL

    def test_target_trade_count(self):
        engine = _make_engine(
            {"RELIANCE": _entry_then_target_df()},
            config=_test_config(target_bps=200.0),
        )
        report = engine.run()
        assert report.metrics.trade_count == 2


# ---------------------------------------------------------------------------
# Tests: no live trading dependency
# ---------------------------------------------------------------------------


class TestNoLiveDependency:
    def test_module_has_no_kiteconnect_import(self):
        import inspect

        import trading_engine.strategies.first_hour_momentum as mod

        src = inspect.getsource(mod)
        assert "kiteconnect" not in src

    def test_module_has_no_zerodha_import(self):
        import inspect

        import trading_engine.strategies.first_hour_momentum as mod

        src = inspect.getsource(mod)
        assert "zerodha" not in src.lower()

    def test_module_has_no_live_execution_import(self):
        import inspect

        import trading_engine.strategies.first_hour_momentum as mod

        src = inspect.getsource(mod)
        assert "live_execution" not in src
        assert "place_order" not in src
