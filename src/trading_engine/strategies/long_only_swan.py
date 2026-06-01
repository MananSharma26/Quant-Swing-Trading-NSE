"""Long-Only Black Swan (Relative Value) strategy.

Trades mean reversion of a spread between two highly cointegrated assets 
on a daily timeframe. However, to avoid shorting constraints in the cash market,
it only buys the underperformer in cash (CNC) and holds until the mean reverts.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent


@dataclass
class LongOnlySwanConfig:
    """Configuration for LongOnlySwanStrategy."""
    strategy_id: str = "long_only_swan"
    symbol_a: str = "HDFCBANK"
    symbol_b: str = "HDFCLIFE"
    # Note: Quantity is the number of shares to buy when that specific symbol crashes.
    # It should be sized independently based on the user's capital limit (e.g. 1 Lakh total cash).
    quantity_a: int = 100
    quantity_b: int = 90
    window_size: int = 120
    entry_z_score: float = 3.5
    exit_z_score: float = 0.0
    stop_loss_z_score: float = 5.0
    
    def __post_init__(self) -> None:
        if self.quantity_a <= 0 or self.quantity_b <= 0:
            raise ValueError("Quantities must be positive.")
        if self.window_size <= 1:
            raise ValueError("window_size must be > 1 to calculate standard deviation.")
        if self.entry_z_score <= self.exit_z_score:
            raise ValueError("entry_z_score must be strictly greater than exit_z_score.")
        if self.stop_loss_z_score <= self.entry_z_score:
            raise ValueError("stop_loss_z_score must be strictly greater than entry_z_score.")

@dataclass
class _PairState:
    """State tracked for the pair."""
    last_bar_a: Bar | None = None
    last_bar_b: Bar | None = None
    ratio_history: list[float] = field(default_factory=list)
    position: str | None = None  # None, "LONG_A", or "LONG_B"

class LongOnlySwanStrategy(Strategy):
    """Long-Only Black Swan Trading Strategy."""

    def __init__(
        self,
        config: LongOnlySwanConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or LongOnlySwanConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._state = _PairState()

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        
        if bar.symbol not in (self._config.symbol_a, self._config.symbol_b):
            return intents

        if bar.symbol == self._config.symbol_a:
            self._state.last_bar_a = bar
        elif bar.symbol == self._config.symbol_b:
            self._state.last_bar_b = bar

        if self._state.last_bar_a is None or self._state.last_bar_b is None:
            return intents

        if self._state.last_bar_a.timestamp != self._state.last_bar_b.timestamp:
            return intents

        price_a = float(self._state.last_bar_a.close)
        price_b = float(self._state.last_bar_b.close)
        
        if price_b == 0:
            return intents
            
        current_ratio = price_a / price_b
        self._state.ratio_history.append(current_ratio)

        if len(self._state.ratio_history) > self._config.window_size:
            self._state.ratio_history.pop(0)

        if len(self._state.ratio_history) < self._config.window_size:
            return intents

        mean_ratio = statistics.mean(self._state.ratio_history)
        stdev_ratio = statistics.stdev(self._state.ratio_history)
        
        if stdev_ratio == 0:
            return intents
            
        z_score = (current_ratio - mean_ratio) / stdev_ratio

        if self._state.position == "LONG_A":
            if z_score <= -self._config.stop_loss_z_score:
                intents.append(self._create_intent(self._config.symbol_a, "SELL", self._config.quantity_a, bar.exchange, "long_a_stop_loss"))
                self._state.position = None
            elif z_score >= -self._config.exit_z_score:
                intents.append(self._create_intent(self._config.symbol_a, "SELL", self._config.quantity_a, bar.exchange, "long_a_exit"))
                self._state.position = None
                
        elif self._state.position == "LONG_B":
            if z_score >= self._config.stop_loss_z_score:
                intents.append(self._create_intent(self._config.symbol_b, "SELL", self._config.quantity_b, bar.exchange, "long_b_stop_loss"))
                self._state.position = None
            elif z_score <= self._config.exit_z_score:
                intents.append(self._create_intent(self._config.symbol_b, "SELL", self._config.quantity_b, bar.exchange, "long_b_exit"))
                self._state.position = None
                
        elif self._state.position is None:
            if z_score <= -self._config.entry_z_score:
                # Symbol A is unusually cheap relative to B
                intents.append(self._create_intent(self._config.symbol_a, "BUY", self._config.quantity_a, bar.exchange, "long_a_entry"))
                self._state.position = "LONG_A"
                
            elif z_score >= self._config.entry_z_score:
                # Symbol B is unusually cheap relative to A
                intents.append(self._create_intent(self._config.symbol_b, "BUY", self._config.quantity_b, bar.exchange, "long_b_entry"))
                self._state.position = "LONG_B"

        return intents

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
