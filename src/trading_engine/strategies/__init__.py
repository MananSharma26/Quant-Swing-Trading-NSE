"""Production trading strategies.

All strategies are backtest-only until the full risk engine and order manager
are implemented in later milestones. No strategy in this package may call
a live broker or place real orders.
"""

from trading_engine.strategies.pairs_trading import PairsTradingConfig, PairsTradingStrategy

__all__ = ["PairsTradingConfig", "PairsTradingStrategy"]
