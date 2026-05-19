"""First-Hour Momentum to Close strategy (backtest-only, v1).

Intraday long-only (or long+short) strategy for NSE cash equities:
  1. Observe the first ``momentum_window_minutes`` bars to establish direction.
  2. Enter LONG if the first-window return is strongly positive, the opening
     range is within configured bounds, and price continues above the window
     close after the window ends.
  3. Optionally require price to be above session VWAP at entry.
  4. Exit on stop-loss, profit target, trailing stop, or square-off time.

No live order placement.  No broker API calls.  Backtest use only.

Entry price assumption
----------------------
Strategy emits a BUY / SELL OrderIntent on the bar where conditions are met.
SimulatedBroker fills at that bar's *close* price (optimistic assumption,
consistent with the backtest engine).  Stop and target are set relative to
bar.close.

Trailing stop
-------------
Updated each bar while in position.  For LONG the trailing stop rises when
bar.high exceeds the highest price seen since entry; it is never lowered.
For SHORT the trailing stop falls when bar.low sets a new low; it is never
raised.

First-window metrics
--------------------
  first_window_return_bps = (first_window_close / first_window_open - 1) * 10000
  opening_range_bps       = (first_window_high  / first_window_low  - 1) * 10000

RVOL / ATR filters
-------------------
``min_first_window_rvol`` and ``min_first_window_atr_multiple`` are accepted
in the config but are not enforced in v1: no historical baseline data is
available inside the strategy.  If set to a non-None value, a warning is
logged once per symbol per day and the filter is skipped.

Exit reasons
------------
  "fhm_stop_loss"     — bar low (long) / high (short) breaches initial stop
  "fhm_trailing_stop" — trailing stop is hit
  "fhm_target"        — bar high (long) / low (short) reaches profit target
  "fhm_square_off"    — bar timestamp >= square_off_time
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent

_TEN_THOUSAND = Decimal("10000")
_ZERO = Decimal("0")
_THREE = Decimal("3")
_ONE = Decimal("1")

_VALID_POSITIONS = frozenset({"LONG", "SHORT", ""})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class FirstHourMomentumConfig:
    """Configuration for FirstHourMomentumStrategy.

    Args:
        strategy_id:                   Identifier for the strategy run.
        exchange:                      Exchange string, e.g. "NSE".
        product:                       Product type, e.g. "MIS" for intraday.
        quantity:                      Shares per signal.  Must be positive.
        session_start:                 Market session open time (09:15 for NSE).
        momentum_window_minutes:       Number of bars that form the first window.
        earliest_entry_time:           No entries before this time.  Should be
                                       at or after session_start + window.
        latest_entry_time:             No new entries at or after this time.
        square_off_time:               Force-close all positions at this time.
        min_first_window_return_bps:   First-window return must be >= this for
                                       LONG (or <= negative of this for SHORT).
        min_opening_range_bps:         First-window (high-low) range must be >= this.
        max_opening_range_bps:         First-window range must be <= this.
        require_price_above_vwap_for_longs:  If True, bar.close must exceed VWAP.
        require_price_below_vwap_for_shorts: If True, bar.close must be below VWAP.
        allow_shorts:                  If True, short entries are also considered.
        stop_loss_bps:                 Initial stop this many bps from entry price.
        trailing_stop_bps:             If not None, activate a trailing stop this
                                       many bps from the best price seen since entry.
        target_bps:                    If not None, profit target this many bps above
                                       (long) or below (short) entry.
        max_trades_per_symbol_per_day: Maximum entries per symbol per calendar day.
        min_bars_before_signal:        Minimum bars seen today before any entry.
                                       Must be >= momentum_window_minutes.
        min_first_window_rvol:         Reserved for future use.  If not None, a
                                       warning is logged and the filter is skipped.
        min_first_window_atr_multiple: Reserved for future use.  Same behaviour.
    """

    strategy_id: str = "first_hour_momentum_v1"
    exchange: str = "NSE"
    product: str = "MIS"
    quantity: int = 10
    session_start: time = field(default_factory=lambda: time(9, 15))
    momentum_window_minutes: int = 30
    earliest_entry_time: time = field(default_factory=lambda: time(9, 45))
    latest_entry_time: time = field(default_factory=lambda: time(12, 0))
    square_off_time: time = field(default_factory=lambda: time(15, 15))
    min_first_window_return_bps: float = 60.0
    min_opening_range_bps: float = 30.0
    max_opening_range_bps: float = 250.0
    require_price_above_vwap_for_longs: bool = True
    require_price_below_vwap_for_shorts: bool = True
    allow_shorts: bool = False
    stop_loss_bps: float = 80.0
    trailing_stop_bps: float | None = None
    target_bps: float | None = None
    max_trades_per_symbol_per_day: int = 1
    min_bars_before_signal: int = 30
    min_first_window_rvol: float | None = None
    min_first_window_atr_multiple: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.momentum_window_minutes <= 0:
            raise ValueError(
                f"momentum_window_minutes must be positive, got {self.momentum_window_minutes}"
            )
        if self.min_first_window_return_bps <= 0:
            raise ValueError(
                f"min_first_window_return_bps must be positive, "
                f"got {self.min_first_window_return_bps}"
            )
        if self.stop_loss_bps <= 0:
            raise ValueError(f"stop_loss_bps must be positive, got {self.stop_loss_bps}")
        if self.latest_entry_time < self.earliest_entry_time:
            raise ValueError(
                f"latest_entry_time ({self.latest_entry_time}) must be >= "
                f"earliest_entry_time ({self.earliest_entry_time})"
            )
        if self.square_off_time <= self.latest_entry_time:
            raise ValueError(
                f"square_off_time ({self.square_off_time}) must be after "
                f"latest_entry_time ({self.latest_entry_time})"
            )
        if self.max_trades_per_symbol_per_day < 1:
            raise ValueError(
                f"max_trades_per_symbol_per_day must be >= 1, "
                f"got {self.max_trades_per_symbol_per_day}"
            )
        if self.min_bars_before_signal < self.momentum_window_minutes:
            raise ValueError(
                f"min_bars_before_signal ({self.min_bars_before_signal}) must be >= "
                f"momentum_window_minutes ({self.momentum_window_minutes})"
            )
        if self.trailing_stop_bps is not None and self.trailing_stop_bps <= 0:
            raise ValueError(
                f"trailing_stop_bps must be positive when set, got {self.trailing_stop_bps}"
            )
        if self.target_bps is not None and self.target_bps <= 0:
            raise ValueError(f"target_bps must be positive when set, got {self.target_bps}")


# ---------------------------------------------------------------------------
# Per-symbol daily state
# ---------------------------------------------------------------------------


@dataclass
class _SymbolState:
    """Mutable per-symbol, per-day state for FirstHourMomentumStrategy."""

    current_date: date | None = None

    # Session VWAP accumulators
    cumulative_pv: Decimal = field(default_factory=Decimal)
    cumulative_vol: int = 0
    vwap: Decimal | None = None

    # First window tracking
    bars_in_window: int = 0
    first_window_open: Decimal | None = None
    first_window_high: Decimal | None = None
    first_window_low: Decimal | None = None
    first_window_close: Decimal | None = None
    first_window_volume: int = 0
    first_window_complete: bool = False
    first_window_return_bps: Decimal | None = None
    opening_range_bps: Decimal | None = None

    # Session bar count
    bars_seen_today: int = 0

    # Position tracking
    in_position: bool = False
    position_side: str = ""  # "LONG" or "SHORT"
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    trailing_stop_price: Decimal | None = None
    highest_since_entry: Decimal | None = None
    lowest_since_entry: Decimal | None = None

    # Daily trade tracking
    entered_today: bool = False
    trades_taken_today: int = 0

    # Warning flags (to avoid repeated log messages per day)
    rvol_warning_logged: bool = False
    atr_warning_logged: bool = False

    def __post_init__(self) -> None:
        self.cumulative_pv = Decimal("0")

    def reset(self, new_date: date) -> None:
        """Reset all intraday state for a new trading day."""
        self.current_date = new_date
        self.cumulative_pv = Decimal("0")
        self.cumulative_vol = 0
        self.vwap = None
        self.bars_in_window = 0
        self.first_window_open = None
        self.first_window_high = None
        self.first_window_low = None
        self.first_window_close = None
        self.first_window_volume = 0
        self.first_window_complete = False
        self.first_window_return_bps = None
        self.opening_range_bps = None
        self.bars_seen_today = 0
        self.in_position = False
        self.position_side = ""
        self.entry_price = None
        self.stop_price = None
        self.target_price = None
        self.trailing_stop_price = None
        self.highest_since_entry = None
        self.lowest_since_entry = None
        self.entered_today = False
        self.trades_taken_today = 0
        self.rvol_warning_logged = False
        self.atr_warning_logged = False


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class FirstHourMomentumStrategy(Strategy):
    """First-Hour Momentum to Close strategy (long-only or long+short, backtest v1).

    Construct with a FirstHourMomentumConfig to customise all parameters.

    Example::

        cfg = FirstHourMomentumConfig(quantity=10, stop_loss_bps=80.0)
        strategy = FirstHourMomentumStrategy(config=cfg)

    The strategy resets per-symbol state automatically on the first bar of
    each new trading date, making it correct for multi-day backtests.
    """

    def __init__(
        self,
        config: FirstHourMomentumConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or FirstHourMomentumConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _SymbolState] = {}

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        """Process one bar; return zero or more OrderIntents."""
        state = self._get_state(bar.symbol)
        bar_date = _bar_date(bar)
        bar_time = _bar_time(bar)

        # Reset state at the start of each new trading day.
        if state.current_date != bar_date:
            state.reset(bar_date)

        # Update session VWAP.
        self._update_vwap(bar, state)

        # Track first window OHLCV.
        self._update_window(bar, bar_time, state)

        state.bars_seen_today += 1

        intents: list[OrderIntent] = []

        # ── Exit checks (before entry) ─────────────────────────────────
        if state.in_position:
            # Update trailing stop first so the new bar's price is reflected.
            self._update_trailing_stop(bar, state)
            exit_intent = self._check_exit(bar, bar_time, state)
            if exit_intent is not None:
                intents.append(exit_intent)
                state.in_position = False
                state.position_side = ""
                return intents

        # ── Entry check ───────────────────────────────────────────────
        if self._can_enter(bar_time, state):
            self._maybe_warn_filters(bar.symbol, state)
            entry_intent = self._check_entry(bar, state)
            if entry_intent is not None:
                intents.append(entry_intent)
                self._set_position_state(bar, entry_intent.side, state)

        return intents

    # ------------------------------------------------------------------
    # VWAP
    # ------------------------------------------------------------------

    def _update_vwap(self, bar: Bar, state: _SymbolState) -> None:
        """Update cumulative session VWAP.

        Zero-volume bars use bar.close with weight 1 to avoid divide-by-zero.
        """
        if bar.volume > 0:
            tp = (bar.high + bar.low + bar.close) / _THREE
            state.cumulative_pv += tp * Decimal(bar.volume)
            state.cumulative_vol += bar.volume
        else:
            state.cumulative_pv += bar.close
            state.cumulative_vol += 1
        state.vwap = state.cumulative_pv / Decimal(state.cumulative_vol)

    # ------------------------------------------------------------------
    # First window
    # ------------------------------------------------------------------

    def _update_window(self, bar: Bar, bar_time: time, state: _SymbolState) -> None:
        """Accumulate OHLCV for the first momentum window.

        Bars before session_start are skipped.  Once momentum_window_minutes
        bars have been seen the window is marked complete and return/range bps
        are computed.
        """
        if state.first_window_complete:
            return
        if bar_time < self._config.session_start:
            return

        if state.bars_in_window == 0:
            state.first_window_open = bar.open

        if state.first_window_high is None or bar.high > state.first_window_high:
            state.first_window_high = bar.high
        if state.first_window_low is None or bar.low < state.first_window_low:
            state.first_window_low = bar.low

        state.first_window_close = bar.close
        state.first_window_volume += bar.volume
        state.bars_in_window += 1

        if state.bars_in_window >= self._config.momentum_window_minutes:
            state.first_window_complete = True
            fw_open = state.first_window_open
            fw_close = state.first_window_close
            fw_high = state.first_window_high
            fw_low = state.first_window_low

            if fw_open is not None and fw_open > _ZERO and fw_close is not None:
                state.first_window_return_bps = (fw_close / fw_open - _ONE) * _TEN_THOUSAND

            if fw_high is not None and fw_low is not None and fw_low > _ZERO:
                state.opening_range_bps = (fw_high / fw_low - _ONE) * _TEN_THOUSAND

    # ------------------------------------------------------------------
    # Entry guards
    # ------------------------------------------------------------------

    def _can_enter(self, bar_time: time, state: _SymbolState) -> bool:
        """Return True if time/count guards allow entry consideration."""
        cfg = self._config
        return (
            state.first_window_complete
            and not state.in_position
            and state.trades_taken_today < cfg.max_trades_per_symbol_per_day
            and bar_time >= cfg.earliest_entry_time
            and bar_time <= cfg.latest_entry_time
            and state.bars_seen_today >= cfg.min_bars_before_signal
            and state.vwap is not None
        )

    def _check_entry(self, bar: Bar, state: _SymbolState) -> OrderIntent | None:
        """Return a BUY (or SELL) OrderIntent if all entry conditions are met."""
        fwr = state.first_window_return_bps
        orb = state.opening_range_bps
        fw_close = state.first_window_close

        if fwr is None or orb is None or fw_close is None:
            return None

        cfg = self._config
        threshold = Decimal(str(cfg.min_first_window_return_bps))
        orb_min = Decimal(str(cfg.min_opening_range_bps))
        orb_max = Decimal(str(cfg.max_opening_range_bps))

        # Range filter applies to both directions.
        if orb < orb_min or orb > orb_max:
            return None

        # ── LONG ──────────────────────────────────────────────────────
        if fwr >= threshold:
            if bar.close <= fw_close:
                return None  # price not extending momentum
            if cfg.require_price_above_vwap_for_longs:
                if state.vwap is None or bar.close <= state.vwap:
                    return None
            return OrderIntent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side="BUY",
                quantity=cfg.quantity,
                order_type="MARKET",
                product=cfg.product,
                reason="fhm_long_entry",
            )

        # ── SHORT ─────────────────────────────────────────────────────
        if cfg.allow_shorts and fwr <= -threshold:
            if bar.close >= fw_close:
                return None  # price not extending downward momentum
            if cfg.require_price_below_vwap_for_shorts:
                if state.vwap is None or bar.close >= state.vwap:
                    return None
            return OrderIntent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side="SELL",
                quantity=cfg.quantity,
                order_type="MARKET",
                product=cfg.product,
                reason="fhm_short_entry",
            )

        return None

    def _set_position_state(self, bar: Bar, side: str, state: _SymbolState) -> None:
        """Update state after an entry intent is emitted."""
        entry_price = bar.close
        cfg = self._config

        state.in_position = True
        state.position_side = "LONG" if side == "BUY" else "SHORT"
        state.entry_price = entry_price
        state.entered_today = True
        state.trades_taken_today += 1

        sl_factor = Decimal(str(cfg.stop_loss_bps)) / _TEN_THOUSAND
        if state.position_side == "LONG":
            state.stop_price = entry_price * (_ONE - sl_factor)
        else:
            state.stop_price = entry_price * (_ONE + sl_factor)

        if cfg.target_bps is not None:
            tgt_factor = Decimal(str(cfg.target_bps)) / _TEN_THOUSAND
            if state.position_side == "LONG":
                state.target_price = entry_price * (_ONE + tgt_factor)
            else:
                state.target_price = entry_price * (_ONE - tgt_factor)
        else:
            state.target_price = None

        if cfg.trailing_stop_bps is not None:
            trail_factor = Decimal(str(cfg.trailing_stop_bps)) / _TEN_THOUSAND
            if state.position_side == "LONG":
                state.highest_since_entry = entry_price
                state.trailing_stop_price = entry_price * (_ONE - trail_factor)
            else:
                state.lowest_since_entry = entry_price
                state.trailing_stop_price = entry_price * (_ONE + trail_factor)
        else:
            state.trailing_stop_price = None
            state.highest_since_entry = None
            state.lowest_since_entry = None

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _update_trailing_stop(self, bar: Bar, state: _SymbolState) -> None:
        """Raise (long) or lower (short) trailing stop based on new bar prices."""
        if self._config.trailing_stop_bps is None or state.trailing_stop_price is None:
            return

        trail_factor = Decimal(str(self._config.trailing_stop_bps)) / _TEN_THOUSAND

        if state.position_side == "LONG":
            if state.highest_since_entry is None or bar.high > state.highest_since_entry:
                state.highest_since_entry = bar.high
            new_trail = state.highest_since_entry * (_ONE - trail_factor)
            if new_trail > state.trailing_stop_price:
                state.trailing_stop_price = new_trail
        else:
            if state.lowest_since_entry is None or bar.low < state.lowest_since_entry:
                state.lowest_since_entry = bar.low
            new_trail = state.lowest_since_entry * (_ONE + trail_factor)
            if new_trail < state.trailing_stop_price:
                state.trailing_stop_price = new_trail

    def _check_exit(self, bar: Bar, bar_time: time, state: _SymbolState) -> OrderIntent | None:
        """Return a SELL/BUY exit OrderIntent if any exit condition is met.

        Priority (per spec): stop-loss → target → trailing stop → square-off.
        """
        assert state.stop_price is not None

        if state.position_side == "LONG":
            stop_hit = bar.low <= state.stop_price
            target_hit = state.target_price is not None and bar.high >= state.target_price
            trailing_hit = (
                state.trailing_stop_price is not None and bar.low <= state.trailing_stop_price
            )
        else:  # SHORT
            stop_hit = bar.high >= state.stop_price
            target_hit = state.target_price is not None and bar.low <= state.target_price
            trailing_hit = (
                state.trailing_stop_price is not None and bar.high >= state.trailing_stop_price
            )

        square_off_hit = bar_time >= self._config.square_off_time

        if stop_hit:
            return self._exit_intent(bar, state, "fhm_stop_loss")
        if target_hit:
            return self._exit_intent(bar, state, "fhm_target")
        if trailing_hit:
            return self._exit_intent(bar, state, "fhm_trailing_stop")
        if square_off_hit:
            return self._exit_intent(bar, state, "fhm_square_off")
        return None

    def _exit_intent(self, bar: Bar, state: _SymbolState, reason: str) -> OrderIntent:
        side = "SELL" if state.position_side == "LONG" else "BUY"
        return OrderIntent(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            exchange=bar.exchange,
            side=side,
            quantity=self._config.quantity,
            order_type="MARKET",
            product=self._config.product,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_state(self, symbol: str) -> _SymbolState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolState()
        return self._states[symbol]

    def _maybe_warn_filters(self, symbol: str, state: _SymbolState) -> None:
        """Log one-time warnings for unimplemented RVOL/ATR filters."""
        cfg = self._config
        if cfg.min_first_window_rvol is not None and not state.rvol_warning_logged:
            self._logger.warning(
                "%s: min_first_window_rvol=%s is configured but not enforced in v1 "
                "(no historical volume baseline available).",
                symbol,
                cfg.min_first_window_rvol,
            )
            state.rvol_warning_logged = True
        if cfg.min_first_window_atr_multiple is not None and not state.atr_warning_logged:
            self._logger.warning(
                "%s: min_first_window_atr_multiple=%s is configured but not enforced "
                "in v1 (no ATR baseline available).",
                symbol,
                cfg.min_first_window_atr_multiple,
            )
            state.atr_warning_logged = True


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _bar_time(bar: Bar) -> time:
    """Return the bar's time, converting to IST if timezone-aware."""
    ts = bar.timestamp
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo

        ts = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ts.time()


def _bar_date(bar: Bar) -> date:
    """Return the bar's date, converting to IST if timezone-aware."""
    ts = bar.timestamp
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo

        ts = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ts.date()
