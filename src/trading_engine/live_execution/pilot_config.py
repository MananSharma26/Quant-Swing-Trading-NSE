"""Live order execution pilot configuration.

LivePilotConfig centralises all per-order constraints used by
LiveExecutionSafetyGuard.assert_pilot_order_allowed().  It is intentionally
a plain dataclass (not Settings) so it can be constructed in tests without
environment variables.

Usage::

    from trading_engine.common.config import load_settings
    from trading_engine.live_execution.pilot_config import LivePilotConfig

    settings = load_settings()
    config = LivePilotConfig.from_settings(settings)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LivePilotConfig:
    """Constraints applied to every live pilot order.

    Fields:
        live_order_execution_enabled: Master flag — False blocks all execution.
        live_order_pilot_enabled:     Pilot-specific flag — False blocks pilot orders.
        max_order_quantity:           Maximum quantity per order (inclusive).
        allowed_symbols:              Whitelist of tradingsymbols.  Empty = all blocked.
        allowed_exchange:             The only exchange allowed (e.g. "NSE").
        allowed_product:              The only product allowed (e.g. "MIS").
        allowed_order_types:          Allowed order type strings (e.g. ["MARKET", "LIMIT"]).
    """

    live_order_execution_enabled: bool = False
    live_order_pilot_enabled: bool = False
    max_order_quantity: int = 1
    allowed_symbols: list[str] = field(default_factory=list)
    allowed_exchange: str = "NSE"
    allowed_product: str = "MIS"
    allowed_order_types: list[str] = field(default_factory=lambda: ["MARKET", "LIMIT"])

    @classmethod
    def from_settings(cls, settings: Any) -> LivePilotConfig:
        """Build a LivePilotConfig from a Settings instance.

        Reads only the live_* attributes; unknown attributes are ignored.
        """
        return cls(
            live_order_execution_enabled=getattr(
                settings, "live_order_execution_enabled", False
            ),
            live_order_pilot_enabled=getattr(settings, "live_order_pilot_enabled", False),
            max_order_quantity=getattr(settings, "live_max_order_quantity", 1),
            allowed_symbols=list(getattr(settings, "live_allowed_symbols", [])),
            allowed_exchange=getattr(settings, "live_allowed_exchange", "NSE"),
            allowed_product=getattr(settings, "live_allowed_product", "MIS"),
            allowed_order_types=list(
                getattr(settings, "live_allowed_order_types", ["MARKET", "LIMIT"])
            ),
        )
