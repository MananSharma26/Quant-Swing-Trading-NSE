"""Black Swan Cointegration (Swing Pairs) strategy.

Trades mean reversion of a spread between two highly cointegrated assets 
on a daily timeframe. Positions are held for multiple days (CNC).
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
class BlackSwanPairsConfig:
    """Configuration for BlackSwanPairsStrategy."""
    strategy_id: str = "black_swan_pairs"
    symbol_a: str = "HDFCBANK"
    symbol_b: str = "HDFCLIFE"
    quantity_a: int = 100
    quantity_b: int = 90
    window_size: int = 120  # 120 days (approx 6 months)
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
    position: str | None = None  # None, "LONG_SPREAD", or "SHORT_SPREAD"

class BlackSwanPairsStrategy(Strategy):
    """Black Swan Pairs Trading Strategy."""

    def __init__(
        self,
        config: BlackSwanPairsConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or BlackSwanPairsConfig()
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

        if self._state.position == "LONG_SPREAD":
            if z_score <= -self._config.stop_loss_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_stop_loss"))
                self._state.position = None
            elif z_score >= -self._config.exit_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_exit_long"))
                self._state.position = None
                
        elif self._state.position == "SHORT_SPREAD":
            if z_score >= self._config.stop_loss_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_stop_loss"))
                self._state.position = None
            elif z_score <= self._config.exit_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_exit_short"))
                self._state.position = None
                
        elif self._state.position is None:
            if z_score <= -self._config.entry_z_score:
                intents.extend(
                    self._create_intents(
                        side_a="BUY", side_b="SELL", exchange=bar.exchange, reason="pairs_entry_long"
                    )
                )
                self._state.position = "LONG_SPREAD"
                
            elif z_score >= self._config.entry_z_score:
                intents.extend(
                    self._create_intents(
                        side_a="SELL", side_b="BUY", exchange=bar.exchange, reason="pairs_entry_short"
                    )
                )
                self._state.position = "SHORT_SPREAD"

        return intents

    def _create_intents(self, side_a: str, side_b: str, exchange: str, reason: str) -> list[OrderIntent]:
        intent_a = OrderIntent(
            strategy_id=self.strategy_id,
            symbol=self._config.symbol_a,
            exchange=exchange,
            side=side_a,
            quantity=self._config.quantity_a,
            order_type="MARKET",
            product="CNC",
            reason=reason,
        )
        intent_b = OrderIntent(
            strategy_id=self.strategy_id,
            symbol=self._config.symbol_b,
            exchange=exchange,
            side=side_b,
            quantity=self._config.quantity_b,
            order_type="MARKET",
            product="CNC",
            reason=reason,
        )
        return [intent_a, intent_b]

    def _close_position_intents(self, exchange: str, reason: str) -> list[OrderIntent]:
        if self._state.position == "LONG_SPREAD":
            return self._create_intents(side_a="SELL", side_b="BUY", exchange=exchange, reason=reason)
        elif self._state.position == "SHORT_SPREAD":
            return self._create_intents(side_a="BUY", side_b="SELL", exchange=exchange, reason=reason)
        return []
