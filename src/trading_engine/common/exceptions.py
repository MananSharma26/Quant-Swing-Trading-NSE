"""Custom exceptions for the trading engine."""

from __future__ import annotations


class TradingEngineError(Exception):
    """Base exception for all trading engine errors."""


class LiveTradingDisabledError(TradingEngineError):
    """Raised when live order placement is attempted but live trading is disabled."""


class SafetyError(TradingEngineError):
    """Raised when a safety constraint is violated."""


class ConfigurationError(TradingEngineError):
    """Raised when configuration is invalid or missing required values."""


class BrokerConnectionError(TradingEngineError):
    """Raised when broker connection fails or becomes unavailable."""


class RiskLimitError(TradingEngineError):
    """Raised when a risk limit check is violated."""


class MarketDataError(TradingEngineError):
    """Raised when market data is stale, missing, or invalid."""


class OrderValidationError(TradingEngineError):
    """Raised when an order intent fails validation."""


class OrderStateTransitionError(TradingEngineError):
    """Raised when an illegal order status transition is attempted."""


class OrderNotFoundError(TradingEngineError):
    """Raised when an order lookup fails because the ID is not in the ledger."""


class BrokerMappingError(TradingEngineError):
    """Raised when a raw broker response cannot be mapped to an internal model."""
