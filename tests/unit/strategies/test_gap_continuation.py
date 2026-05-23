"""Unit tests for GapContinuationStrategy."""

from __future__ import annotations

import sys
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.strategies.gap_continuation import (  # noqa: E402
    GapContinuationConfig,
    GapContinuationStrategy,
)


class TestGapContinuationConfig:
    def test_defaults_are_valid(self):
        cfg = GapContinuationConfig()
        assert cfg.strategy_id == "gap_cont_v1"
        assert cfg.min_gap_bps == 60.0
        assert cfg.max_gap_bps == 300.0
        assert cfg.continuation_trigger_bps == 20.0
        assert cfg.stop_loss_bps == 80.0
        assert cfg.target_bps is None
        assert cfg.allow_long_continuations is True
        assert cfg.allow_short_continuations is True

    def test_quantity_zero_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            GapContinuationConfig(quantity=0)

    def test_min_gap_zero_raises(self):
        with pytest.raises(ValueError, match="min_gap_bps"):
            GapContinuationConfig(min_gap_bps=0.0)

    def test_max_gap_not_greater_than_min_raises(self):
        with pytest.raises(ValueError, match="max_gap_bps"):
            GapContinuationConfig(min_gap_bps=100.0, max_gap_bps=100.0)

    def test_trigger_zero_raises(self):
        with pytest.raises(ValueError, match="continuation_trigger_bps"):
            GapContinuationConfig(continuation_trigger_bps=0.0)

    def test_stop_loss_zero_raises(self):
        with pytest.raises(ValueError, match="stop_loss_bps"):
            GapContinuationConfig(stop_loss_bps=0.0)

    def test_target_bps_zero_raises(self):
        with pytest.raises(ValueError, match="target_bps"):
            GapContinuationConfig(target_bps=0.0)

    def test_target_bps_positive_valid(self):
        cfg = GapContinuationConfig(target_bps=200.0)
        assert cfg.target_bps == 200.0

    def test_square_off_after_latest_entry(self):
        with pytest.raises(ValueError, match="square_off_time"):
            GapContinuationConfig(
                latest_entry_time=time(15, 15),
                square_off_time=time(10, 30),
            )

    def test_no_fills_on_first_day(self):
        """First day has no prior close — strategy must skip all entry signals."""
        cfg = GapContinuationConfig(
            min_gap_bps=50.0,
            max_gap_bps=500.0,
            continuation_trigger_bps=10.0,
            stop_loss_bps=200.0,
            allow_long_continuations=True,
            allow_short_continuations=True,
        )
        strategy = GapContinuationStrategy(config=cfg)

        from trading_engine.strategy.signals import Bar
        bar = Bar(
            symbol="TEST",
            exchange="NSE",
            timestamp=pd.Timestamp("2024-01-15 09:20:00"),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=5000,
        )
        intents = strategy.on_bar(bar, context=None)
        assert intents == []


def _make_bar(
    symbol: str = "TEST",
    timestamp: str = "2024-01-15 09:15:00",
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: int = 5000,
) -> "Bar":
    from trading_engine.strategy.signals import Bar
    return Bar(
        symbol=symbol,
        exchange="NSE",
        timestamp=pd.Timestamp(timestamp),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
    )


def _permissive_cfg(**kwargs) -> GapContinuationConfig:
    defaults = dict(
        min_gap_bps=50.0,
        max_gap_bps=1000.0,
        continuation_trigger_bps=10.0,
        stop_loss_bps=200.0,
        target_bps=None,
        allow_long_continuations=True,
        allow_short_continuations=True,
    )
    defaults.update(kwargs)
    return GapContinuationConfig(**defaults)


class TestPriorCloseTracking:
    def test_prior_close_is_none_on_first_day(self):
        strategy = GapContinuationStrategy(config=_permissive_cfg())
        bar = _make_bar(timestamp="2024-01-15 09:15:00", close=100.0)
        strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.prior_close is None

    def test_prior_close_set_after_day_rollover(self):
        strategy = GapContinuationStrategy(config=_permissive_cfg())
        # Day 1: close = 107
        bar1 = _make_bar(timestamp="2024-01-15 09:15:00", close=107.0)
        strategy.on_bar(bar1, context=None)
        # Day 2: opening bar triggers reset — prior_close should be 107
        bar2 = _make_bar(timestamp="2024-01-16 09:15:00", open_=100.0, close=100.0)
        strategy.on_bar(bar2, context=None)
        state = strategy._states["TEST"]
        assert state.prior_close == Decimal("107.0")

    def test_prior_close_updates_each_day(self):
        strategy = GapContinuationStrategy(config=_permissive_cfg())
        for close_val, ts in [
            (100.0, "2024-01-15 09:15:00"),
            (110.0, "2024-01-16 09:15:00"),
            (120.0, "2024-01-17 09:15:00"),
        ]:
            bar = _make_bar(timestamp=ts, close=close_val)
            strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.prior_close == Decimal("110.0")


class TestGapDetection:
    def _two_day_strategy(self, **cfg_kwargs):
        """Returns strategy with day-1 close=100 already fed in."""
        strategy = GapContinuationStrategy(config=_permissive_cfg(**cfg_kwargs))
        bar_day1 = _make_bar(timestamp="2024-01-15 09:15:00", close=100.0)
        strategy.on_bar(bar_day1, context=None)
        return strategy

    def test_gap_below_min_not_qualified(self):
        strategy = self._two_day_strategy(min_gap_bps=100.0)
        # prior_close=100, open=100.5 → gap=50 bps < min_gap_bps=100
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=100.5, close=100.5)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_above_max_not_qualified(self):
        strategy = self._two_day_strategy(max_gap_bps=200.0)
        # prior_close=100, open=103 → gap=300 bps > max_gap_bps=200
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=103.0, close=103.0)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_up_sets_long_direction(self):
        strategy = self._two_day_strategy()
        # prior_close=100, open=101 → gap=100 bps (within [50,1000])
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0)
        strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.gap_direction == "LONG"

    def test_gap_down_sets_short_direction(self):
        strategy = self._two_day_strategy()
        # prior_close=100, open=99 → gap=-100 bps
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0)
        strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.gap_direction == "SHORT"

    def test_allow_long_false_blocks_gap_up(self):
        strategy = self._two_day_strategy(allow_long_continuations=False)
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False

    def test_allow_short_false_blocks_gap_down(self):
        strategy = self._two_day_strategy(allow_short_continuations=False)
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False


class TestEntryLogic:
    def _setup_with_gap(self, gap_direction: str, **cfg_kwargs):
        """Returns strategy after day-1 close=100 and day-2 opening bar."""
        cfg = _permissive_cfg(**cfg_kwargs)
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        if gap_direction == "LONG":
            opening_bar = _make_bar(
                timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0
            )  # gap-up 100 bps
        else:
            opening_bar = _make_bar(
                timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0
            )  # gap-down 100 bps
        strategy.on_bar(opening_bar, context=None)
        return strategy

    def test_long_entry_when_close_above_trigger(self):
        # opening=101, trigger=10bps → trigger_price=101.101
        # bar.close=101.2 >= 101.101 → LONG entry
        strategy = self._setup_with_gap("LONG", continuation_trigger_bps=10.0)
        bar = _make_bar(timestamp="2024-01-16 09:20:00", open_=101.0, high=101.3, low=100.9, close=101.2)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gc_long_entry"

    def test_long_no_entry_below_trigger(self):
        # opening=101, trigger=10bps → trigger_price=101.101
        # bar.close=101.0 < 101.101 → no entry
        strategy = self._setup_with_gap("LONG", continuation_trigger_bps=10.0)
        bar = _make_bar(timestamp="2024-01-16 09:20:00", close=101.0)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_short_entry_when_close_below_trigger(self):
        # opening=99, trigger=10bps → trigger_price=98.901
        # bar.close=98.8 <= 98.901 → SHORT entry
        strategy = self._setup_with_gap("SHORT", continuation_trigger_bps=10.0)
        bar = _make_bar(timestamp="2024-01-16 09:20:00", open_=99.0, high=99.1, low=98.7, close=98.8)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gc_short_entry"

    def test_no_entry_before_entry_start_time(self):
        # entry_start_time=09:20, bar is at 09:19 → no entry
        cfg = GapContinuationConfig(
            min_gap_bps=50.0, max_gap_bps=1000.0, continuation_trigger_bps=10.0,
            stop_loss_bps=200.0, entry_start_time=time(9, 20),
        )
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0), context=None)
        # Bar at 09:19 with close above trigger
        bar = _make_bar(timestamp="2024-01-16 09:19:00", close=101.2)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_no_entry_after_latest_entry_time(self):
        cfg = GapContinuationConfig(
            min_gap_bps=50.0, max_gap_bps=1000.0, continuation_trigger_bps=10.0,
            stop_loss_bps=200.0, latest_entry_time=time(10, 30),
        )
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0), context=None)
        # Bar at 10:31
        bar = _make_bar(timestamp="2024-01-16 10:31:00", close=101.5)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_max_trades_per_day_respected(self):
        strategy = self._setup_with_gap("LONG", continuation_trigger_bps=10.0, max_trades_per_symbol_per_day=1)
        bar1 = _make_bar(timestamp="2024-01-16 09:20:00", close=101.2)
        strategy.on_bar(bar1, context=None)
        # Force exit so in_position=False
        state = strategy._states["TEST"]
        state.in_position = False
        # Second entry attempt
        bar2 = _make_bar(timestamp="2024-01-16 09:30:00", close=101.5)
        intents = strategy.on_bar(bar2, context=None)
        assert intents == []
