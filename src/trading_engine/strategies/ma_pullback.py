"""Moving Average Pullback Strategy.

Buys fundamentally strong stocks in a macro uptrend (Price > 200 SMA)
when they pull back to their 50-day moving average and become oversold (RSI < 30).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent


@dataclass
class MAPullbackConfig:
    """Configuration for Moving Average Pullback Strategy."""
    strategy_id: str = "ma_pullback"
    symbol: str = "RELIANCE"
    capital_per_trade: int = 100000
    trend_ma_period: int = 200
    pullback_ma_period: int = 50
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    stop_loss_pct: float = 2.0
    target_pct: float = 6.0
    max_hold_days: int = 20

    def __post_init__(self) -> None:
        if self.capital_per_trade <= 0:
            raise ValueError("Capital must be positive.")
        if self.pullback_ma_period >= self.trend_ma_period:
            raise ValueError("pullback_ma_period must be less than trend_ma_period.")

@dataclass
class _SymbolState:
    """State tracked for the symbol."""
    close_history: list[float] = field(default_factory=list)
    avg_gain: float = 0.0
    avg_loss: float = 0.0
    position: str | None = None  # None or "LONG"
    entry_time: datetime | None = None
    entry_qty: int = 0
    entry_price: float = 0.0


class MAPullbackStrategy(Strategy):
    """Moving Average Pullback Swing Strategy."""

    def __init__(
        self,
        config: MAPullbackConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or MAPullbackConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        # Support running across multiple symbols (unlike long_only_swan which trades 1 pair)
        # We will key state by symbol so we can run a universe sweep.
        self._states: dict[str, _SymbolState] = {}

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        sym = bar.symbol
        
        if sym not in self._states:
            self._states[sym] = _SymbolState()
            
        state = self._states[sym]
        close_price = float(bar.close)

        # Update Wilder's RSI Smoothing
        if len(state.close_history) > 0:
            change = close_price - state.close_history[-1]
            gain = max(0.0, change)
            loss = max(0.0, -change)
            
            if len(state.close_history) < self._config.rsi_period:
                state.avg_gain += gain
                state.avg_loss += loss
            elif len(state.close_history) == self._config.rsi_period:
                state.avg_gain = (state.avg_gain + gain) / self._config.rsi_period
                state.avg_loss = (state.avg_loss + loss) / self._config.rsi_period
            else:
                state.avg_gain = (state.avg_gain * (self._config.rsi_period - 1) + gain) / self._config.rsi_period
                state.avg_loss = (state.avg_loss * (self._config.rsi_period - 1) + loss) / self._config.rsi_period

        state.close_history.append(close_price)
        if len(state.close_history) > self._config.trend_ma_period + 1:
            state.close_history.pop(0)

        # Wait until we have enough data for the 200 SMA
        if len(state.close_history) < self._config.trend_ma_period:
            return intents

        # Calculate SMAs
        trend_sma = sum(state.close_history[-self._config.trend_ma_period:]) / self._config.trend_ma_period
        pullback_sma = sum(state.close_history[-self._config.pullback_ma_period:]) / self._config.pullback_ma_period

        # Calculate RSI
        rsi = 50.0
        if state.avg_loss == 0:
            if state.avg_gain > 0:
                rsi = 100.0
        else:
            rs = state.avg_gain / state.avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        days_held = 0
        if state.entry_time:
            days_held = (bar.timestamp - state.entry_time).days

        if state.position == "LONG":
            # Exit Conditions
            stop_price = state.entry_price * (1.0 - self._config.stop_loss_pct / 100.0)
            target_price = state.entry_price * (1.0 + self._config.target_pct / 100.0)

            if close_price <= stop_price:
                intents.append(self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, "pullback_stop_loss"))
                self._clear_position(state)
            elif close_price >= target_price:
                intents.append(self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, "pullback_target"))
                self._clear_position(state)
            elif days_held >= self._config.max_hold_days:
                intents.append(self._create_intent(sym, "SELL", state.entry_qty, bar.exchange, "pullback_time_exit"))
                self._clear_position(state)

        elif state.position is None:
            # Entry Conditions
            is_uptrend = close_price > trend_sma
            is_pullback = close_price <= pullback_sma
            is_oversold = rsi <= self._config.rsi_oversold

            if is_uptrend and is_pullback and is_oversold:
                qty = max(1, int(self._config.capital_per_trade / close_price))
                intents.append(self._create_intent(sym, "BUY", qty, bar.exchange, "pullback_entry"))
                state.position = "LONG"
                state.entry_time = bar.timestamp
                state.entry_qty = qty
                state.entry_price = close_price

        return intents

    def _clear_position(self, state: _SymbolState) -> None:
        state.position = None
        state.entry_time = None
        state.entry_qty = 0
        state.entry_price = 0.0

    def _create_intent(self, symbol: str, side: str, quantity: int, exchange: str, reason: str) -> OrderIntent:
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
