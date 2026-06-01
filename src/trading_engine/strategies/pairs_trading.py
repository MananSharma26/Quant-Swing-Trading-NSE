"""Intraday Pairs Trading (Statistical Arbitrage) strategy.

This strategy trades the mean reversion of a spread between two highly
correlated assets (e.g., HDFCBANK and ICICIBANK).

1. It maintains a rolling window of the price ratio (Symbol A / Symbol B).
2. It calculates the Z-score of the current ratio relative to the window.
3. If the Z-score exceeds `entry_z_score` (e.g., +2.0), it shorts the spread
   (Sells A, Buys B). If it drops below `-entry_z_score`, it goes long the
   spread (Buys A, Sells B).
4. It exits when the Z-score reverts to `exit_z_score` (e.g., 0.0), or at
   the configured square-off time.
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
class PairsTradingConfig:
    """Configuration for PairsTradingStrategy."""

    strategy_id: str = "pairs_v1"
    symbol_a: str = "HDFCBANK"
    symbol_b: str = "ICICIBANK"
    quantity_a: int = 1
    quantity_b: int = 1
    window_size: int = 20
    entry_z_score: float = 2.0
    exit_z_score: float = 0.0
    stop_loss_z_score: float = 4.0
    square_off_time: time = field(default_factory=lambda: time(15, 15))

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
    """Mutable state tracked for the pair, per trading day."""

    current_date: date | None = None
    
    # Synchronization tracking
    last_bar_a: Bar | None = None
    last_bar_b: Bar | None = None
    
    # Rolling window for the ratio (Symbol A / Symbol B)
    ratio_history: list[float] = field(default_factory=list)
    
    # Position tracking
    position: str | None = None  # None, "LONG_SPREAD", or "SHORT_SPREAD"
    exited_today: bool = False

    def reset(self, new_date: date) -> None:
        """Reset state for a new trading day."""
        self.current_date = new_date
        self.last_bar_a = None
        self.last_bar_b = None
        self.ratio_history.clear()
        self.position = None
        self.exited_today = False


class PairsTradingStrategy(Strategy):
    """Pairs Trading Strategy."""

    def __init__(
        self,
        config: PairsTradingConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or PairsTradingConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._state = _PairState()

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        
        # We only process the configured symbols.
        if bar.symbol not in (self._config.symbol_a, self._config.symbol_b):
            return intents

        bar_date = _bar_date(bar)
        bar_time = _bar_time(bar)

        if self._state.current_date != bar_date:
            self._state.reset(bar_date)

        # Store the latest bar for synchronization
        if bar.symbol == self._config.symbol_a:
            self._state.last_bar_a = bar
        elif bar.symbol == self._config.symbol_b:
            self._state.last_bar_b = bar

        # If we don't have bars for both symbols yet, we can't calculate a spread.
        if self._state.last_bar_a is None or self._state.last_bar_b is None:
            return intents

        # Only process if both bars represent the same timestamp.
        if self._state.last_bar_a.timestamp != self._state.last_bar_b.timestamp:
            return intents

        # Both bars are aligned. Calculate the ratio.
        price_a = float(self._state.last_bar_a.close)
        price_b = float(self._state.last_bar_b.close)
        
        # Avoid division by zero (unlikely for stock prices, but good practice).
        if price_b == 0:
            return intents
            
        current_ratio = price_a / price_b
        self._state.ratio_history.append(current_ratio)

        # Maintain rolling window size.
        if len(self._state.ratio_history) > self._config.window_size:
            self._state.ratio_history.pop(0)

        # ── Exit checks ─────────────────────────────────────────────────
        if self._state.position is not None:
            if bar_time >= self._config.square_off_time:
                # Square off due to time
                intents.extend(self._close_position_intents(bar.exchange, "pairs_square_off"))
                self._state.position = None
                self._state.exited_today = True
                return intents

        # We need a full window to calculate z-score
        if len(self._state.ratio_history) < self._config.window_size:
            return intents

        mean_ratio = statistics.mean(self._state.ratio_history)
        stdev_ratio = statistics.stdev(self._state.ratio_history)
        
        if stdev_ratio == 0:
            return intents
            
        z_score = (current_ratio - mean_ratio) / stdev_ratio

        # ── Position Management ─────────────────────────────────────────
        if self._state.position == "LONG_SPREAD":
            # Check stop loss
            if z_score <= -self._config.stop_loss_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_stop_loss"))
                self._state.position = None
                self._state.exited_today = True
            # Exit long spread when z-score mean reverts upwards
            elif z_score >= -self._config.exit_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_exit_long"))
                self._state.position = None
                
        elif self._state.position == "SHORT_SPREAD":
            # Check stop loss
            if z_score >= self._config.stop_loss_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_stop_loss"))
                self._state.position = None
                self._state.exited_today = True
            # Exit short spread when z-score mean reverts downwards
            elif z_score <= self._config.exit_z_score:
                intents.extend(self._close_position_intents(bar.exchange, "pairs_exit_short"))
                self._state.position = None
                
        elif not self._state.exited_today:
            # Entry checks
            if z_score <= -self._config.entry_z_score:
                # Ratio is too low (A is cheap relative to B). Buy A, Sell B.
                intents.extend(
                    self._create_intents(
                        side_a="BUY", side_b="SELL", exchange=bar.exchange, reason="pairs_entry_long"
                    )
                )
                self._state.position = "LONG_SPREAD"
                
            elif z_score >= self._config.entry_z_score:
                # Ratio is too high (A is expensive relative to B). Sell A, Buy B.
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
            product="MIS",
            reason=reason,
        )
        intent_b = OrderIntent(
            strategy_id=self.strategy_id,
            symbol=self._config.symbol_b,
            exchange=exchange,
            side=side_b,
            quantity=self._config.quantity_b,
            order_type="MARKET",
            product="MIS",
            reason=reason,
        )
        return [intent_a, intent_b]

    def _close_position_intents(self, exchange: str, reason: str) -> list[OrderIntent]:
        if self._state.position == "LONG_SPREAD":
            # Long spread means we are long A, short B. So sell A, buy B.
            return self._create_intents(side_a="SELL", side_b="BUY", exchange=exchange, reason=reason)
        elif self._state.position == "SHORT_SPREAD":
            # Short spread means we are short A, long B. So buy A, sell B.
            return self._create_intents(side_a="BUY", side_b="SELL", exchange=exchange, reason=reason)
        return []


def _bar_time(bar: Bar) -> time:
    ts = bar.timestamp
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo
        ts = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ts.time()


def _bar_date(bar: Bar) -> date:
    ts = bar.timestamp
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo
        ts = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ts.date()
