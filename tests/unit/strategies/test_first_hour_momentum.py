"""Unit tests for FirstHourMomentumStrategy."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from trading_engine.strategies.first_hour_momentum import (
    FirstHourMomentumConfig,
    FirstHourMomentumStrategy,
)
from trading_engine.strategy.base import StrategyContext
from trading_engine.strategy.signals import Bar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXCHANGE = "NSE"
_SYMBOL = "RELIANCE"


def _ctx() -> StrategyContext:
    return StrategyContext(strategy_id="fhm_test", mode="backtest", config={})


def _bar(
    ts: str,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: int = 1000,
    symbol: str = _SYMBOL,
) -> Bar:
    return Bar(
        symbol=symbol,
        exchange=_EXCHANGE,
        timestamp=datetime.fromisoformat(ts),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
        interval="minute",
    )


def _cfg(**kwargs) -> FirstHourMomentumConfig:
    """Permissive test config with 5-bar window so tests are small."""
    defaults: dict = dict(
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
        require_price_below_vwap_for_shorts=False,
        allow_shorts=False,
        quantity=10,
    )
    defaults.update(kwargs)
    return FirstHourMomentumConfig(**defaults)


def _strategy(**cfg_kwargs) -> FirstHourMomentumStrategy:
    return FirstHourMomentumStrategy(config=_cfg(**cfg_kwargs))


# 5 uptrend window bars (09:15–09:19): open=100 → close=104; return~400bps
_WINDOW_TIMES = [
    "2024-01-15 09:15:00",
    "2024-01-15 09:16:00",
    "2024-01-15 09:17:00",
    "2024-01-15 09:18:00",
    "2024-01-15 09:19:00",
]
_WINDOW_CLOSES = [100, 101, 102, 103, 104]


def _window_bar(i: int) -> Bar:
    c = _WINDOW_CLOSES[i]
    return _bar(_WINDOW_TIMES[i], open_=c, high=c + 1, low=c - 1, close=c)


def _feed_window(strategy: FirstHourMomentumStrategy, ctx: StrategyContext) -> None:
    """Feed 5 uptrend window bars (no entry expected during window)."""
    for i in range(5):
        strategy.on_bar(_window_bar(i), ctx)


# After 5 window bars: first_window_open=100, first_window_close=104
# return_bps = (104/100 - 1)*10000 = 400bps


# ---------------------------------------------------------------------------
# Tests: config validation
# ---------------------------------------------------------------------------


class TestFirstHourMomentumConfig:
    def test_default_config_is_valid(self):
        cfg = FirstHourMomentumConfig()
        assert cfg.quantity == 10
        assert cfg.strategy_id == "first_hour_momentum_v1"

    def test_invalid_quantity_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            FirstHourMomentumConfig(quantity=0)

    def test_negative_quantity_raises(self):
        with pytest.raises(ValueError):
            FirstHourMomentumConfig(quantity=-5)

    def test_invalid_momentum_window_raises(self):
        with pytest.raises(ValueError, match="momentum_window_minutes"):
            FirstHourMomentumConfig(momentum_window_minutes=0)

    def test_invalid_min_return_raises(self):
        with pytest.raises(ValueError, match="min_first_window_return_bps"):
            FirstHourMomentumConfig(min_first_window_return_bps=0.0)

    def test_invalid_stop_loss_raises(self):
        with pytest.raises(ValueError, match="stop_loss_bps"):
            FirstHourMomentumConfig(stop_loss_bps=0.0)

    def test_latest_before_earliest_raises(self):
        with pytest.raises(ValueError, match="latest_entry_time"):
            FirstHourMomentumConfig(
                earliest_entry_time=time(11, 0),
                latest_entry_time=time(10, 0),
                square_off_time=time(15, 15),
            )

    def test_square_off_before_latest_entry_raises(self):
        with pytest.raises(ValueError, match="square_off_time"):
            FirstHourMomentumConfig(
                latest_entry_time=time(14, 30),
                square_off_time=time(13, 0),
            )

    def test_max_trades_zero_raises(self):
        with pytest.raises(ValueError, match="max_trades_per_symbol_per_day"):
            FirstHourMomentumConfig(max_trades_per_symbol_per_day=0)

    def test_min_bars_less_than_window_raises(self):
        with pytest.raises(ValueError, match="min_bars_before_signal"):
            FirstHourMomentumConfig(
                momentum_window_minutes=10,
                min_bars_before_signal=5,
            )

    def test_trailing_stop_zero_raises(self):
        with pytest.raises(ValueError, match="trailing_stop_bps"):
            _cfg(trailing_stop_bps=0.0)

    def test_target_zero_raises(self):
        with pytest.raises(ValueError, match="target_bps"):
            _cfg(target_bps=0.0)

    def test_target_none_is_valid(self):
        cfg = _cfg(target_bps=None)
        assert cfg.target_bps is None

    def test_trailing_stop_none_is_valid(self):
        cfg = _cfg(trailing_stop_bps=None)
        assert cfg.trailing_stop_bps is None


# ---------------------------------------------------------------------------
# Tests: first window tracking
# ---------------------------------------------------------------------------


class TestFirstWindowTracking:
    def test_window_not_complete_before_window_bars(self):
        s = _strategy()
        ctx = _ctx()
        for i in range(4):
            s.on_bar(_window_bar(i), ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_complete is False

    def test_window_complete_after_window_bars(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_complete is True

    def test_window_open_is_first_bar_open(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_open == pytest.approx(Decimal("100"), rel=Decimal("0.001"))

    def test_window_close_is_last_bar_close(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_close == pytest.approx(Decimal("104"), rel=Decimal("0.001"))

    def test_window_high_is_max_across_bars(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        # bar highs: 101, 102, 103, 104, 105 → max=105
        assert state.first_window_high == pytest.approx(Decimal("105"), rel=Decimal("0.001"))

    def test_window_low_is_min_across_bars(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        # bar lows: 99, 100, 101, 102, 103 → min=99
        assert state.first_window_low == pytest.approx(Decimal("99"), rel=Decimal("0.001"))

    def test_window_return_bps_computed_correctly(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        # return_bps = (104/100 - 1)*10000 = 400
        assert state.first_window_return_bps == pytest.approx(Decimal("400"), rel=Decimal("0.01"))

    def test_opening_range_bps_computed_correctly(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        # range_bps = (105/99 - 1)*10000 ≈ 606
        assert state.opening_range_bps is not None
        assert float(state.opening_range_bps) == pytest.approx((105 / 99 - 1) * 10000, rel=1e-3)

    def test_window_resets_on_new_day(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_complete is True
        # New day
        s.on_bar(_bar("2024-01-16 09:15:00", close=100), ctx)
        assert state.first_window_complete is False
        assert state.bars_in_window == 1

    def test_bars_before_session_start_not_counted_in_window(self):
        s = _strategy(momentum_window_minutes=3)
        ctx = _ctx()
        # Bar before session start (09:10)
        s.on_bar(_bar("2024-01-15 09:10:00", close=100), ctx)
        state = s._states[_SYMBOL]
        assert state.bars_in_window == 0

    def test_window_volume_accumulates(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_volume == 5 * 1000


# ---------------------------------------------------------------------------
# Tests: VWAP
# ---------------------------------------------------------------------------


class TestVWAPCalculation:
    def test_vwap_computed_after_first_bar(self):
        s = _strategy()
        ctx = _ctx()
        s.on_bar(_bar("2024-01-15 09:15:00", high=101, low=99, close=100, volume=1000), ctx)
        state = s._states[_SYMBOL]
        assert state.vwap is not None
        assert float(state.vwap) == pytest.approx(100.0, rel=1e-3)

    def test_vwap_zero_volume_does_not_crash(self):
        s = _strategy()
        ctx = _ctx()
        s.on_bar(_bar("2024-01-15 09:15:00", close=100, volume=0), ctx)
        state = s._states[_SYMBOL]
        assert state.vwap is not None
        assert float(state.vwap) == pytest.approx(100.0, rel=1e-3)

    def test_vwap_resets_on_new_day(self):
        s = _strategy()
        ctx = _ctx()
        s.on_bar(_bar("2024-01-15 09:15:00", close=200, volume=5000), ctx)
        state = s._states[_SYMBOL]
        vwap_day1 = state.vwap
        s.on_bar(_bar("2024-01-16 09:15:00", close=100, volume=1000), ctx)
        assert state.vwap != vwap_day1
        assert state.cumulative_vol == 1000  # only current day


# ---------------------------------------------------------------------------
# Tests: no-trade guard conditions
# ---------------------------------------------------------------------------


class TestNoTradeConditions:
    def test_no_entry_before_window_complete(self):
        s = _strategy()
        ctx = _ctx()
        # Only 4 bars: window not complete
        for i in range(4):
            intents = s.on_bar(_window_bar(i), ctx)
            assert intents == []

    def test_no_entry_before_earliest_entry_time(self):
        """Window bar 5 is at 09:19, earliest_entry_time=09:20 → no entry."""
        s = _strategy(earliest_entry_time=time(9, 20))
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.first_window_complete is True
        # The 5th window bar IS at 09:19 < 09:20 → no entry on that bar
        assert not state.in_position

    def test_no_entry_after_latest_entry_time(self):
        s = _strategy(latest_entry_time=time(12, 0))
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 12:01:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_when_return_below_threshold(self):
        """Window return below min_first_window_return_bps → no entry."""
        s = _strategy(min_first_window_return_bps=500.0)  # needs 500bps; window only 400
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_when_close_not_above_window_close(self):
        """close <= first_window_close → no LONG entry."""
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        # first_window_close = 104; entry bar close=104 (equal, not above)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=105, low=103, close=104), ctx)
        assert intents == []

    def test_no_entry_when_opening_range_too_small(self):
        s = _strategy(min_opening_range_bps=1000.0)  # need >1000bps range; we have ~606
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_when_opening_range_too_large(self):
        s = _strategy(max_opening_range_bps=100.0)  # max 100bps; our range ~606bps
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_when_vwap_filter_fails(self):
        """require_price_above_vwap_for_longs=True: close must exceed VWAP at entry bar.

        Feed window bars with high=200, low=190 so typical price ≈ 164 >> close.
        VWAP after window ≈ 164; entry bar close=105 < VWAP → filter blocks entry.
        """
        s = _strategy(require_price_above_vwap_for_longs=True)
        ctx = _ctx()
        # Window bars: closes=[100..104], high=200, low=190 → typical≈164 → VWAP≈164
        for i in range(5):
            c = 100 + i
            s.on_bar(_bar(_WINDOW_TIMES[i], open_=c, high=200, low=190, close=c), ctx)
        # VWAP≈164 after 5 bars; entry bar close=105 (>fw_close=104) but <VWAP → blocked.
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_when_max_trades_reached(self):
        s = _strategy(max_trades_per_symbol_per_day=1)
        ctx = _ctx()
        _feed_window(s, ctx)
        # Manually set trades_taken_today to the limit
        state = s._states[_SYMBOL]
        state.trades_taken_today = 1
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_before_min_bars(self):
        """bars_seen_today < min_bars_before_signal → no entry."""
        # min_bars=10 but window=5; after 5 window bars, bars_seen_today=5 < 10
        s = _strategy(min_bars_before_signal=10)
        ctx = _ctx()
        _feed_window(s, ctx)
        state = s._states[_SYMBOL]
        assert state.bars_seen_today == 5  # exactly the window
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents == []

    def test_no_entry_when_downtrend_and_shorts_disabled(self):
        """Negative first-window return with allow_shorts=False → no entry."""
        s = _strategy(allow_shorts=False)
        ctx = _ctx()
        # Feed downtrend window: open=104, close=100 → return=-400bps
        down_times = [
            "2024-01-15 09:15:00",
            "2024-01-15 09:16:00",
            "2024-01-15 09:17:00",
            "2024-01-15 09:18:00",
            "2024-01-15 09:19:00",
        ]
        for i, ts in enumerate(down_times):
            c = 104 - i
            s.on_bar(_bar(ts, open_=c, high=c + 1, low=c - 1, close=c), ctx)
        # Entry bar with close below window close
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=101, low=99, close=100), ctx)
        assert intents == []


# ---------------------------------------------------------------------------
# Tests: long entry
# ---------------------------------------------------------------------------


class TestLongEntry:
    def test_long_entry_emits_buy_intent(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        # first_window_close=104; entry bar close=105 > 104 ✓
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].symbol == _SYMBOL

    def test_long_entry_is_market_order(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents[0].order_type == "MARKET"

    def test_long_entry_quantity(self):
        s = _strategy(quantity=7)
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents[0].quantity == 7

    def test_long_entry_sets_in_position(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        state = s._states[_SYMBOL]
        assert state.in_position is True
        assert state.position_side == "LONG"

    def test_long_entry_sets_stop_price(self):
        s = _strategy(stop_loss_bps=100.0)
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        state = s._states[_SYMBOL]
        # entry=105, stop = 105*(1 - 100/10000) = 105*0.99 = 103.95
        expected_stop = Decimal("105") * (1 - Decimal("100") / Decimal("10000"))
        assert state.stop_price == pytest.approx(expected_stop, rel=Decimal("0.001"))

    def test_long_entry_sets_target_when_configured(self):
        s = _strategy(target_bps=200.0)
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        state = s._states[_SYMBOL]
        # entry=105, target = 105*(1 + 200/10000) = 105*1.02 = 107.1
        expected_target = Decimal("105") * (1 + Decimal("200") / Decimal("10000"))
        assert state.target_price == pytest.approx(expected_target, rel=Decimal("0.001"))

    def test_long_entry_no_target_when_not_configured(self):
        s = _strategy(target_bps=None)
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        state = s._states[_SYMBOL]
        assert state.target_price is None

    def test_long_entry_reason(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert intents[0].reason == "fhm_long_entry"

    def test_entered_today_set_after_entry(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert s._states[_SYMBOL].entered_today is True

    def test_trades_taken_incremented(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        assert s._states[_SYMBOL].trades_taken_today == 1


# ---------------------------------------------------------------------------
# Tests: exit logic
# ---------------------------------------------------------------------------


class TestExitLogic:
    def _enter(self, strategy: FirstHourMomentumStrategy, ctx: StrategyContext) -> None:
        """Feed window bars then trigger LONG entry at close=105."""
        _feed_window(strategy, ctx)
        strategy.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        state = strategy._states[_SYMBOL]
        assert state.in_position is True

    def test_stop_loss_emits_sell(self):
        s = _strategy(stop_loss_bps=100.0)
        ctx = _ctx()
        self._enter(s, ctx)
        # stop = 105 * 0.99 = 103.95; bar low=103 < 103.95
        intents = s.on_bar(_bar("2024-01-15 09:21:00", high=106, low=103, close=104), ctx)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "fhm_stop_loss"

    def test_stop_loss_clears_position(self):
        s = _strategy(stop_loss_bps=100.0)
        ctx = _ctx()
        self._enter(s, ctx)
        s.on_bar(_bar("2024-01-15 09:21:00", high=106, low=103, close=104), ctx)
        assert s._states[_SYMBOL].in_position is False

    def test_target_emits_sell(self):
        s = _strategy(target_bps=200.0)
        ctx = _ctx()
        self._enter(s, ctx)
        # target = 105 * 1.02 = 107.1; bar high=108 >= 107.1
        intents = s.on_bar(_bar("2024-01-15 09:21:00", high=108, low=105, close=107), ctx)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "fhm_target"

    def test_target_clears_position(self):
        s = _strategy(target_bps=200.0)
        ctx = _ctx()
        self._enter(s, ctx)
        s.on_bar(_bar("2024-01-15 09:21:00", high=108, low=105, close=107), ctx)
        assert s._states[_SYMBOL].in_position is False

    def test_square_off_emits_sell(self):
        s = _strategy(square_off_time=time(15, 15))
        ctx = _ctx()
        self._enter(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 15:15:00", high=106, low=104, close=105), ctx)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "fhm_square_off"

    def test_square_off_clears_position(self):
        s = _strategy()
        ctx = _ctx()
        self._enter(s, ctx)
        s.on_bar(_bar("2024-01-15 15:15:00", high=106, low=104, close=105), ctx)
        assert s._states[_SYMBOL].in_position is False

    def test_stop_priority_over_target(self):
        """Both stop and target hit in same bar → stop wins."""
        s = _strategy(stop_loss_bps=100.0, target_bps=200.0)
        ctx = _ctx()
        self._enter(s, ctx)
        # stop=103.95, target=107.1; bar: low=103 < stop, high=108 > target
        intents = s.on_bar(_bar("2024-01-15 09:21:00", high=108, low=103, close=105), ctx)
        assert intents[0].reason == "fhm_stop_loss"

    def test_stop_priority_over_square_off(self):
        """Stop hit at square-off time → stop reason (per spec priority)."""
        s = _strategy(stop_loss_bps=100.0, square_off_time=time(15, 15))
        ctx = _ctx()
        self._enter(s, ctx)
        # stop=103.95; bar at 15:15 with low=103
        intents = s.on_bar(_bar("2024-01-15 15:15:00", high=104, low=103, close=103), ctx)
        assert intents[0].reason == "fhm_stop_loss"

    def test_no_exit_between_stop_and_target(self):
        """Price between stop and target → no exit."""
        s = _strategy(stop_loss_bps=100.0, target_bps=200.0)
        ctx = _ctx()
        self._enter(s, ctx)
        # stop=103.95, target=107.1; bar low=105, high=106 → neither hit
        intents = s.on_bar(_bar("2024-01-15 09:21:00", high=106, low=105, close=105.5), ctx)
        assert intents == []

    def test_trailing_stop_emits_sell(self):
        """Trailing stop is hit when price retraces after new high."""
        s = _strategy(trailing_stop_bps=100.0)
        ctx = _ctx()
        self._enter(s, ctx)
        # Entry at 105; initial trailing = 105*0.99 = 103.95.
        # New high bar: high=110 → trailing = 110*0.99 = 108.9.
        # low=109 > 108.9 → no exit on this bar (stop not hit either).
        s.on_bar(_bar("2024-01-15 09:21:00", high=110, low=109, close=109), ctx)
        # Retrace bar: low=108 < trailing_stop=108.9 → trailing hit
        intents = s.on_bar(_bar("2024-01-15 09:22:00", high=109, low=108, close=108.5), ctx)
        assert len(intents) == 1
        assert intents[0].reason == "fhm_trailing_stop"

    def test_trailing_stop_rises_with_price(self):
        """Trailing stop price increases as bar high rises (LONG)."""
        s = _strategy(trailing_stop_bps=100.0)
        ctx = _ctx()
        self._enter(s, ctx)
        state = s._states[_SYMBOL]
        initial_trail = state.trailing_stop_price
        # High rises to 110 → trailing should rise
        s.on_bar(_bar("2024-01-15 09:21:00", high=110, low=107, close=109), ctx)
        assert state.trailing_stop_price > initial_trail

    def test_trailing_stop_never_falls_for_long(self):
        """Trailing stop (LONG) never moves below previous value."""
        s = _strategy(trailing_stop_bps=100.0)
        ctx = _ctx()
        self._enter(s, ctx)
        # High rises to 110
        s.on_bar(_bar("2024-01-15 09:21:00", high=110, low=108, close=109), ctx)
        state = s._states[_SYMBOL]
        trail_after_high = state.trailing_stop_price
        # Price drops back but doesn't hit stop
        s.on_bar(_bar("2024-01-15 09:22:00", high=109, low=108, close=108.5), ctx)
        assert state.trailing_stop_price >= trail_after_high


# ---------------------------------------------------------------------------
# Tests: short support
# ---------------------------------------------------------------------------


class TestShortSupport:
    def _feed_downtrend_window(
        self, strategy: FirstHourMomentumStrategy, ctx: StrategyContext
    ) -> None:
        """Feed 5 downtrend window bars: open=104, close=100 → -400bps."""
        times = [
            "2024-01-15 09:15:00",
            "2024-01-15 09:16:00",
            "2024-01-15 09:17:00",
            "2024-01-15 09:18:00",
            "2024-01-15 09:19:00",
        ]
        for i, ts in enumerate(times):
            c = 104 - i
            strategy.on_bar(_bar(ts, open_=c, high=c + 1, low=c - 1, close=c), ctx)

    def test_short_entry_emits_sell_when_allow_shorts_true(self):
        s = _strategy(allow_shorts=True, require_price_below_vwap_for_shorts=False)
        ctx = _ctx()
        self._feed_downtrend_window(s, ctx)
        # first_window_close=100; entry bar close=99 < 100 ✓
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=101, low=98, close=99), ctx)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "fhm_short_entry"

    def test_no_short_when_allow_shorts_false(self):
        s = _strategy(allow_shorts=False)
        ctx = _ctx()
        self._feed_downtrend_window(s, ctx)
        intents = s.on_bar(_bar("2024-01-15 09:20:00", high=101, low=98, close=99), ctx)
        assert intents == []

    def test_short_entry_sets_stop_above_entry(self):
        s = _strategy(
            allow_shorts=True,
            require_price_below_vwap_for_shorts=False,
            stop_loss_bps=100.0,
        )
        ctx = _ctx()
        self._feed_downtrend_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=101, low=98, close=99), ctx)
        state = s._states[_SYMBOL]
        # short stop = entry*(1 + 100/10000) = 99*1.01 = 99.99
        expected_stop = Decimal("99") * (1 + Decimal("100") / Decimal("10000"))
        assert state.stop_price == pytest.approx(expected_stop, rel=Decimal("0.001"))
        assert state.position_side == "SHORT"

    def test_short_stop_exits_on_high(self):
        """SHORT exits when bar.high >= stop_price."""
        s = _strategy(
            allow_shorts=True,
            require_price_below_vwap_for_shorts=False,
            stop_loss_bps=100.0,
        )
        ctx = _ctx()
        self._feed_downtrend_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=101, low=98, close=99), ctx)
        # stop = 99*1.01 = 99.99; next bar high=100.5 > 99.99 → stop
        intents = s.on_bar(_bar("2024-01-15 09:21:00", high=100.5, low=97, close=98), ctx)
        assert len(intents) == 1
        assert intents[0].side == "BUY"  # cover short
        assert intents[0].reason == "fhm_stop_loss"


# ---------------------------------------------------------------------------
# Tests: daily reset and multi-symbol independence
# ---------------------------------------------------------------------------


class TestDailyReset:
    def test_state_resets_on_new_day(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        state = s._states[_SYMBOL]
        assert state.in_position is True
        # New day resets state
        s.on_bar(_bar("2024-01-16 09:15:00", close=100), ctx)
        assert state.in_position is False
        assert state.first_window_complete is False
        assert state.trades_taken_today == 0

    def test_entered_today_resets_on_new_day(self):
        s = _strategy()
        ctx = _ctx()
        state = s._get_state(_SYMBOL)
        state.current_date = date(2024, 1, 15)
        state.entered_today = True
        s.on_bar(_bar("2024-01-16 09:15:00"), ctx)
        assert state.entered_today is False

    def test_multi_symbol_states_independent(self):
        s = _strategy()
        ctx = _ctx()
        _feed_window(s, ctx)
        s.on_bar(_bar("2024-01-15 09:20:00", high=106, low=104, close=105), ctx)
        # INFY should have clean state
        infy_state = s._get_state("INFY")
        assert infy_state.in_position is False
        assert infy_state.vwap is None
