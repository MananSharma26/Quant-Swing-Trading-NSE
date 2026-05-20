"""Unit tests for GapFadeStrategy."""

from __future__ import annotations

import sys
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.strategies.gap_fade import GapFadeConfig, GapFadeStrategy  # noqa: E402


class TestGapFadeConfig:
    def test_default_config_is_valid(self):
        cfg = GapFadeConfig()
        assert cfg.strategy_id == "gap_fade_v1"
        assert cfg.min_gap_bps == 60.0
        assert cfg.max_gap_bps == 300.0
        assert cfg.fade_trigger_bps == 20.0
        assert cfg.stop_loss_bps == 80.0
        assert cfg.target_mode == "vwap"

    def test_quantity_must_be_positive(self):
        with pytest.raises(ValueError, match="quantity"):
            GapFadeConfig(quantity=0)

    def test_min_gap_bps_must_be_positive(self):
        with pytest.raises(ValueError, match="min_gap_bps"):
            GapFadeConfig(min_gap_bps=0.0)

    def test_max_gap_bps_must_exceed_min_gap_bps(self):
        with pytest.raises(ValueError, match="max_gap_bps"):
            GapFadeConfig(min_gap_bps=100.0, max_gap_bps=50.0)

    def test_fade_trigger_bps_must_be_positive(self):
        with pytest.raises(ValueError, match="fade_trigger_bps"):
            GapFadeConfig(fade_trigger_bps=0.0)

    def test_stop_loss_bps_must_be_positive(self):
        with pytest.raises(ValueError, match="stop_loss_bps"):
            GapFadeConfig(stop_loss_bps=0.0)

    def test_invalid_target_mode_raises(self):
        with pytest.raises(ValueError, match="target_mode"):
            GapFadeConfig(target_mode="invalid")

    def test_target_bps_must_be_positive_when_set(self):
        with pytest.raises(ValueError, match="target_bps"):
            GapFadeConfig(target_bps=0.0)

    def test_max_trades_must_be_at_least_one(self):
        with pytest.raises(ValueError, match="max_trades_per_symbol_per_day"):
            GapFadeConfig(max_trades_per_symbol_per_day=0)

    def test_latest_entry_time_before_square_off(self):
        with pytest.raises(ValueError, match="square_off_time"):
            GapFadeConfig(
                latest_entry_time=time(15, 15),
                square_off_time=time(15, 15),
            )

    def test_valid_target_modes_accepted(self):
        for mode in ("vwap", "prior_close", "half_gap"):
            cfg = GapFadeConfig(target_mode=mode)
            assert cfg.target_mode == mode

    def test_strategy_instantiates_with_default_config(self):
        strategy = GapFadeStrategy()
        assert strategy.strategy_id == "gap_fade_v1"

    def test_on_bar_returns_empty_list_when_no_prior_close(self):
        """First day: no prior close available -> no trades."""
        from trading_engine.strategy.base import StrategyContext
        from trading_engine.strategy.signals import Bar

        strategy = GapFadeStrategy(GapFadeConfig(require_vwap_confirmation=False))
        ctx = StrategyContext(strategy_id="test", mode="backtest", config={})
        bar = Bar(
            symbol="TEST",
            exchange="NSE",
            timestamp=datetime.fromisoformat("2024-01-15 09:15:00"),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1000,
            interval="minute",
        )
        intents = strategy.on_bar(bar, ctx)
        assert intents == []


# ---------------------------------------------------------------------------
# Shared helpers for strategy-level tests
# ---------------------------------------------------------------------------


def _ctx():
    from trading_engine.strategy.base import StrategyContext

    return StrategyContext(strategy_id="gf_test", mode="backtest", config={})


def _bar(
    ts: str,
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: int = 1000,
    symbol: str = "TEST",
):
    from trading_engine.strategy.signals import Bar

    return Bar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime.fromisoformat(ts),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
        interval="minute",
    )


def _permissive_cfg(**kwargs) -> GapFadeConfig:
    """Config with relaxed thresholds for controlled unit tests."""
    defaults = dict(
        min_gap_bps=50.0,
        max_gap_bps=500.0,
        fade_trigger_bps=10.0,
        stop_loss_bps=200.0,
        require_vwap_confirmation=False,
        target_mode="prior_close",
        allow_long_fades=True,
        allow_short_fades=True,
    )
    defaults.update(kwargs)
    return GapFadeConfig(**defaults)


class TestPriorCloseTracking:
    """prior_close must carry last bar of day N as prior_close for day N+1."""

    def test_first_day_has_no_prior_close(self):
        """No entry on first day because prior_close is None."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        for i in range(5):
            intents = strategy.on_bar(_bar(f"2024-01-15 09:{15 + i:02d}:00", close=100.0), ctx)
            assert intents == [], f"bar {i} should produce no intent on day 1"

    def test_prior_close_set_after_day_one(self):
        """After day 1 closes at 104.0 (last bar), day 2 should see prior_close=104."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        for i in range(5):
            strategy.on_bar(_bar(f"2024-01-15 09:{15 + i:02d}:00", close=100.0 + i), ctx)
        state = strategy._states["TEST"]
        assert state.last_close == Decimal("104")
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=110.0, close=110.0), ctx)
        assert state.prior_close == Decimal("104")

    def test_gap_bps_computed_correctly_on_day2(self):
        """gap_bps = (opening / prior_close - 1) * 10000."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=110.0, close=110.0), ctx)
        state = strategy._states["TEST"]
        assert state.gap_bps is not None
        assert abs(float(state.gap_bps) - 1000.0) < 1.0


class TestGapDetection:
    def test_gap_below_min_not_qualified(self):
        """Gap of 30 bps < min_gap_bps=50 -> gap_qualified=False."""
        strategy = GapFadeStrategy(_permissive_cfg(min_gap_bps=50.0))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=100.3, close=100.3), ctx)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_above_max_not_qualified(self):
        """Gap of 600 bps > max_gap_bps=500 -> gap_qualified=False."""
        strategy = GapFadeStrategy(_permissive_cfg(max_gap_bps=500.0))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=106.0, close=106.0), ctx)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_up_sets_short_fade_direction(self):
        """Gap-up -> fade_direction == 'SHORT'."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=101.0, close=101.0), ctx)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.fade_direction == "SHORT"

    def test_gap_down_sets_long_fade_direction(self):
        """Gap-down -> fade_direction == 'LONG'."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=99.0, close=99.0), ctx)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.fade_direction == "LONG"

    def test_allow_long_fades_false_suppresses_gap_down(self):
        """allow_long_fades=False -> gap-down not qualified."""
        strategy = GapFadeStrategy(_permissive_cfg(allow_long_fades=False))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=99.0, close=99.0), ctx)
        assert strategy._states["TEST"].gap_qualified is False

    def test_min_opening_volume_filter(self):
        """Opening bar with volume < min_opening_volume -> not qualified."""
        strategy = GapFadeStrategy(_permissive_cfg(min_opening_volume=5000))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=101.0, close=101.0, volume=100), ctx)
        assert strategy._states["TEST"].gap_qualified is False


def _feed_two_days_opening(
    strategy,
    ctx,
    day1_close: float = 100.0,
    day2_open: float = 101.0,
) -> None:
    """Feed day-1 close bar, then day-2 opening bar."""
    strategy.on_bar(_bar("2024-01-15 09:15:00", close=day1_close), ctx)
    strategy.on_bar(_bar("2024-01-16 09:15:00", open_=day2_open, close=day2_open), ctx)


class TestEntryLogic:
    def test_long_entry_emitted_when_fade_trigger_met(self):
        """Gap-down + price rises fade_trigger_bps -> BUY intent returned."""
        # Day 1 close=100, Day 2 open=99 (100 bps gap-down -> LONG fade)
        # fade_trigger_bps=10 -> entry when close >= 99 * 1.001 = 99.099
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=10.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        # Entry bar: close=99.2 > trigger=99.099, vwap irrelevant (require=False)
        intents = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=99.0, high=99.5, low=98.9, close=99.2), ctx
        )
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gf_long_entry"

    def test_short_entry_emitted_when_fade_trigger_met(self):
        """Gap-up + price drops fade_trigger_bps -> SELL intent returned."""
        # Day 1 close=100, Day 2 open=101 (100 bps gap-up -> SHORT fade)
        # fade_trigger_bps=10 -> entry when close <= 101 * (1 - 0.001) = 100.899
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=10.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=101.0)
        # Entry bar: close=100.8 < trigger=100.899
        intents = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=101.0, high=101.2, low=100.7, close=100.8), ctx
        )
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gf_short_entry"

    def test_no_entry_before_entry_start_time(self):
        """Bars before entry_start_time=09:20 must not trigger entry."""
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=5.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        # 09:16 bar: trigger met but time < 09:20
        intents = strategy.on_bar(
            _bar("2024-01-16 09:16:00", open_=99.0, high=99.5, low=98.9, close=99.5), ctx
        )
        assert intents == []

    def test_no_entry_after_latest_entry_time(self):
        """Bars after latest_entry_time=10:30 must not trigger entry."""
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=5.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        intents = strategy.on_bar(
            _bar("2024-01-16 10:31:00", open_=99.0, high=99.5, low=98.9, close=99.5), ctx
        )
        assert intents == []

    def test_vwap_confirmation_blocks_long_entry_below_vwap(self):
        """Long fade with require_vwap_confirmation=True: close must be > VWAP."""
        # Strategy with VWAP confirmation on
        strategy = GapFadeStrategy(
            _permissive_cfg(fade_trigger_bps=1.0, require_vwap_confirmation=True)
        )
        ctx = _ctx()
        # Day 1 close=200, day 2 open=99 (huge gap down -> LONG fade qualified)
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=200.0, volume=10000), ctx)
        # Day 2 opening bar at 99, but supply a very high close to push VWAP up
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=99.0, close=200.0, volume=10000), ctx)
        # At 09:20: close=99.5 (above trigger 99*1.001=99.099) but VWAP is ~200
        intents = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=99.5, high=100.0, low=99.4, close=99.5), ctx
        )
        assert intents == []  # blocked by VWAP confirmation

    def test_max_trades_per_day_limit_respected(self):
        """After one trade is entered, second entry is blocked."""
        strategy = GapFadeStrategy(
            _permissive_cfg(fade_trigger_bps=10.0, max_trades_per_symbol_per_day=1)
        )
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        # First entry
        intents1 = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=99.0, high=99.5, low=98.9, close=99.2), ctx
        )
        assert len(intents1) == 1
        # Manually clear position to test trade counter
        strategy._states["TEST"].in_position = False
        strategy._states["TEST"].position_side = ""
        # Second entry attempt -- trade count already = 1
        intents2 = strategy.on_bar(
            _bar("2024-01-16 09:25:00", open_=99.0, high=99.5, low=98.9, close=99.2), ctx
        )
        assert intents2 == []
