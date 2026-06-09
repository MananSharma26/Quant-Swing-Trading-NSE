"""RSI(2) Mean Reversion Strategy.

Buys Nifty large-caps in a macro uptrend (Price > 200 SMA) when the
2-period RSI drops into oversold territory, then exits when RSI
recovers, a stop-loss is hit, or the max hold period expires.

Signal fires on bar close; fill is deferred to the NEXT bar (pending
flag approach) so there is no look-ahead bias.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent


@dataclass
class RSI2Config:
    """Configuration for the RSI(2) Mean Reversion Strategy."""

    strategy_id: str = "rsi2_mean_reversion"
    symbol: str = "RELIANCE"
    capital_per_trade: int = 100_000
    trend_ma_period: int = 200
    rsi_period: int = 2
    rsi_entry_threshold: float = 10.0   # BUY when RSI < this
    rsi_exit_threshold: float = 70.0    # SELL when RSI > this
    stop_loss_pct: float = 5.0          # % below entry price
    max_hold_days: int = 10

    def __post_init__(self) -> None:
        if self.capital_per_trade <= 0:
            raise ValueError("capital_per_trade must be positive.")
        if self.rsi_period < 1:
            raise ValueError("rsi_period must be >= 1.")
        if self.trend_ma_period <= self.rsi_period:
            raise ValueError("trend_ma_period must be greater than rsi_period.")


@dataclass
class _SymbolState:
    """Per-symbol state."""

    close_history: list[float] = field(default_factory=list)
    avg_gain: float = 0.0
    avg_loss: float = 0.0

    # Open position tracking
    position: str | None = None      # None or "LONG"
    entry_time: datetime | None = None
    entry_qty: int = 0
    entry_price: float = 0.0

    # Pending signal flags (signal fires on bar N, fill on bar N+1)
    pending_buy: bool = False
    pending_sell: bool = False
    pending_sell_reason: str = ""


class RSI2MeanReversionStrategy(Strategy):
    """RSI(2) Mean Reversion Swing Strategy."""

    def __init__(
        self,
        config: RSI2Config | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or RSI2Config()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _SymbolState] = {}

    # ------------------------------------------------------------------
    # Main bar handler
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        sym = bar.symbol

        if sym not in self._states:
            self._states[sym] = _SymbolState()

        state = self._states[sym]
        close_price = float(bar.close)

        # ---- Step 1: Execute any pending order from the PREVIOUS bar ----
        # The signal fired last bar; we fill on this bar (next-bar open
        # semantics — broker uses MARKET which fills at this bar's close,
        # one bar after the signal, eliminating same-bar look-ahead).
        if state.pending_sell:
            intents.append(
                self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, state.pending_sell_reason)
            )
            self._clear_position(state)
            state.pending_sell = False
            state.pending_sell_reason = ""

        elif state.pending_buy:
            qty = max(1, int(self._config.capital_per_trade / close_price))
            intents.append(self._create_intent(sym, "BUY", qty, bar.exchange, "rsi2_entry"))
            state.position = "LONG"
            state.entry_time = bar.timestamp
            state.entry_qty = qty
            state.entry_price = close_price
            state.pending_buy = False

        # ---- Step 2: Update Wilder's RSI (include current bar before dividing) ----
        if len(state.close_history) > 0:
            change = close_price - state.close_history[-1]
            gain = max(0.0, change)
            loss = max(0.0, -change)

            if len(state.close_history) < self._config.rsi_period:
                # Accumulate sum (seed phase, not yet dividing)
                state.avg_gain += gain
                state.avg_loss += loss
            elif len(state.close_history) == self._config.rsi_period:
                # Seed: include this bar then divide — matches ma_pullback.py exactly
                state.avg_gain = (state.avg_gain + gain) / self._config.rsi_period
                state.avg_loss = (state.avg_loss + loss) / self._config.rsi_period
            else:
                # Wilder's smoothing
                state.avg_gain = (
                    state.avg_gain * (self._config.rsi_period - 1) + gain
                ) / self._config.rsi_period
                state.avg_loss = (
                    state.avg_loss * (self._config.rsi_period - 1) + loss
                ) / self._config.rsi_period

        state.close_history.append(close_price)
        # Keep only as many bars as we need
        max_history = self._config.trend_ma_period + 1
        if len(state.close_history) > max_history:
            state.close_history.pop(0)

        # ---- Step 3: Wait for minimum 200 bars ----
        if len(state.close_history) < self._config.trend_ma_period:
            return intents

        # ---- Step 4: Compute indicators ----
        trend_sma = (
            sum(state.close_history[-self._config.trend_ma_period :])
            / self._config.trend_ma_period
        )

        rsi = 50.0
        if state.avg_loss == 0.0:
            if state.avg_gain > 0.0:
                rsi = 100.0
        else:
            rs = state.avg_gain / state.avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # ---- Step 5: Signal logic (sets pending flags for NEXT bar) ----
        if state.position == "LONG":
            days_held = 0
            if state.entry_time:
                days_held = (bar.timestamp - state.entry_time).days

            stop_price = state.entry_price * (1.0 - self._config.stop_loss_pct / 100.0)

            if close_price <= stop_price:
                state.pending_sell = True
                state.pending_sell_reason = "rsi2_stop_loss"
            elif rsi >= self._config.rsi_exit_threshold:
                state.pending_sell = True
                state.pending_sell_reason = "rsi2_rsi_exit"
            elif days_held >= self._config.max_hold_days:
                state.pending_sell = True
                state.pending_sell_reason = "rsi2_time_exit"

        elif state.position is None:
            is_uptrend = close_price > trend_sma
            is_oversold = rsi < self._config.rsi_entry_threshold

            if is_uptrend and is_oversold:
                state.pending_buy = True

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
