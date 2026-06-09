"""Supertrend Trend Following Strategy.

Trades long-only based on the Supertrend indicator. Enters on a bullish trend flip
(close crosses above the upper band) and exits on a bearish flip (close crosses
below the lower band), a time-based exit, or a stop-loss.

Indicator math (no external TA libraries):
  - True Range  = max(high - low, abs(high - prev_close), abs(low - prev_close))
  - ATR         = Wilder's EMA of TR over atr_period bars
                  (seed = simple average of first atr_period TRs)
  - basic_upper = (high + low) / 2 + multiplier * ATR
  - basic_lower = (high + low) / 2 - multiplier * ATR
  - Final bands ratchet/tighten as the trend continues (never widen mid-trend).
  - Direction: close > final_upper -> BULLISH (trend=1)
               close < final_lower -> BEARISH (trend=-1)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent


@dataclass
class SupertrendConfig:
    """Configuration for the Supertrend Trend Following Strategy."""

    strategy_id: str = "supertrend"
    symbol: str = "RELIANCE"
    capital_per_trade: int = 100_000
    atr_period: int = 14
    multiplier: float = 3.0
    stop_loss_pct: float = 8.0
    max_hold_days: int = 60

    def __post_init__(self) -> None:
        if self.capital_per_trade <= 0:
            raise ValueError("capital_per_trade must be positive.")
        if self.atr_period < 2:
            raise ValueError("atr_period must be >= 2.")
        if self.multiplier <= 0:
            raise ValueError("multiplier must be positive.")


@dataclass
class _SymbolState:
    """Per-symbol running state for the Supertrend indicator and position tracking."""

    # Raw price history needed for TR computation
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)

    # ATR state
    tr_buffer: list[float] = field(default_factory=list)  # accumulates TRs before seed
    atr: float = 0.0
    atr_seeded: bool = False

    # Supertrend band state (previous bar's final bands)
    prev_final_upper: float = float("inf")
    prev_final_lower: float = 0.0
    prev_close: float = 0.0

    # Trend state: 1 = BULLISH, -1 = BEARISH, 0 = undefined
    trend: int = 0

    # Position tracking
    position: str | None = None   # None or "LONG"
    entry_time: datetime | None = None
    entry_qty: int = 0
    entry_price: float = 0.0

    # Track whether bands have been initialised for at least one bar
    bands_ready: bool = False


class SupertrendStrategy(Strategy):
    """Supertrend Trend Following Swing Strategy (long-only)."""

    def __init__(
        self,
        config: SupertrendConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or SupertrendConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _SymbolState] = {}

    # ------------------------------------------------------------------
    # Core event handler
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        sym = bar.symbol

        if sym not in self._states:
            self._states[sym] = _SymbolState()

        state = self._states[sym]

        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)

        # ---- Step 1: Compute True Range --------------------------------
        if len(state.closes) == 0:
            # Very first bar — no previous close, TR = high - low
            tr = high - low
        else:
            prev_close = state.closes[-1]
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )

        # ---- Step 2: Update ATR (Wilder's EMA) -------------------------
        n = self._config.atr_period
        if not state.atr_seeded:
            state.tr_buffer.append(tr)
            if len(state.tr_buffer) == n:
                # Seed: simple average of first n TRs
                state.atr = sum(state.tr_buffer) / n
                state.atr_seeded = True
        else:
            # Wilder's smoothing: atr = (atr * (n-1) + tr) / n
            state.atr = (state.atr * (n - 1) + tr) / n

        # Update history AFTER using prev_close for TR
        state.closes.append(close)
        state.highs.append(high)
        state.lows.append(low)

        # We need the ATR seeded AND at least one prior bar to have bands
        if not state.atr_seeded or len(state.closes) < 2:
            return intents

        # ---- Step 3: Compute basic bands for this bar ------------------
        hl2 = (high + low) / 2.0
        basic_upper = hl2 + self._config.multiplier * state.atr
        basic_lower = hl2 - self._config.multiplier * state.atr

        # ---- Step 4: Final bands — ratchet logic -----------------------
        # The previous bar's close is the second-to-last entry in state.closes
        prev_close = state.closes[-2]

        if not state.bands_ready:
            # First time we have ATR: initialise bands without adjustment
            final_upper = basic_upper
            final_lower = basic_lower
            state.bands_ready = True
        else:
            # Upper band: only tighten (move lower) when prev close is below it;
            # otherwise hold the previous tighter value.
            if basic_upper < state.prev_final_upper or prev_close > state.prev_final_upper:
                final_upper = basic_upper
            else:
                final_upper = state.prev_final_upper

            # Lower band: only tighten (move higher) when prev close is above it;
            # otherwise hold the previous tighter value.
            if basic_lower > state.prev_final_lower or prev_close < state.prev_final_lower:
                final_lower = basic_lower
            else:
                final_lower = state.prev_final_lower

        # ---- Step 5: Determine trend direction -------------------------
        prev_trend = state.trend

        if close > final_upper:
            new_trend = 1   # BULLISH
        elif close < final_lower:
            new_trend = -1  # BEARISH
        else:
            new_trend = prev_trend  # no change; continue existing trend

        # ---- Step 6: Trade signals -------------------------------------
        days_held = 0
        if state.entry_time is not None:
            days_held = (bar.timestamp - state.entry_time).days

        if state.position == "LONG":
            stop_price = state.entry_price * (1.0 - self._config.stop_loss_pct / 100.0)

            # Bearish flip → exit
            if new_trend == -1 and prev_trend == 1:
                intents.append(
                    self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, "supertrend_bearish_flip")
                )
                self._clear_position(state)

            # Stop loss
            elif close <= stop_price:
                intents.append(
                    self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, "supertrend_stop_loss")
                )
                self._clear_position(state)

            # Max hold exceeded
            elif days_held >= self._config.max_hold_days:
                intents.append(
                    self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, "supertrend_time_exit")
                )
                self._clear_position(state)

        elif state.position is None:
            # Bullish flip → enter
            if new_trend == 1 and prev_trend != 1:
                qty = max(1, int(self._config.capital_per_trade / close))
                intents.append(
                    self._create_intent(sym, "BUY", qty, bar.exchange, "supertrend_bullish_flip")
                )
                state.position = "LONG"
                state.entry_time = bar.timestamp
                state.entry_qty = qty
                state.entry_price = close

        # ---- Step 7: Persist band state for next bar -------------------
        state.prev_final_upper = final_upper
        state.prev_final_lower = final_lower
        state.prev_close = close
        state.trend = new_trend

        return intents

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_position(self, state: _SymbolState) -> None:
        state.position = None
        state.entry_time = None
        state.entry_qty = 0
        state.entry_price = 0.0

    def _create_intent(
        self, symbol: str, side: str, quantity: int, exchange: str, reason: str
    ) -> OrderIntent:
        return OrderIntent(
            strategy_id=self.strategy_id,
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=quantity,
            order_type="MARKET",
            product="CNC",
            reason=reason,
        )
