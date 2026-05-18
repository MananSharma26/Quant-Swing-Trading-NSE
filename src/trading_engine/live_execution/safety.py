"""Live execution safety guard.

LiveExecutionSafetyGuard is the final gatekeeper before any real order
placement path.

assert_live_execution_allowed() checks LIVE_TRADING_ENABLED and kill switch.

assert_pilot_order_allowed() performs the full per-order check for the
live order execution pilot: it validates both global flags, kill switch,
risk approval, manual approval, and all order constraints (symbol, exchange,
product, order_type, quantity).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from trading_engine.common.exceptions import SafetyError

if TYPE_CHECKING:
    from trading_engine.live_execution.models import ApprovalDecision
    from trading_engine.live_execution.pilot_config import LivePilotConfig
    from trading_engine.risk.kill_switch import KillSwitch
    from trading_engine.strategy.signals import OrderIntent


class LiveExecutionSafetyGuard:
    """Hard safety checks for the live execution path.

    Args:
        settings:    Settings object with live trading attributes.
        kill_switch: Optional KillSwitch instance.
        logger:      Optional logger override.
    """

    def __init__(
        self,
        settings: Any,
        kill_switch: KillSwitch | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._kill_switch = kill_switch
        self._log = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Prerequisite checker
    # ------------------------------------------------------------------

    def assert_live_execution_allowed(self) -> None:
        """Check that prerequisites for live execution are satisfied.

        Raises:
            SafetyError: if LIVE_TRADING_ENABLED is False.
            SafetyError: if the kill switch is active.
        """
        live_enabled = getattr(self._settings, "live_trading_enabled", False)
        if not live_enabled:
            raise SafetyError(
                "LIVE_TRADING_ENABLED is False. "
                "Set LIVE_TRADING_ENABLED=true in your environment to enable live trading. "
                "Ensure you understand the risks before doing so."
            )

        if self._kill_switch is not None and self._kill_switch.is_active():
            raise SafetyError(
                f"Kill switch is active — reason: {self._kill_switch.reason!r}. "
                "Deactivate the kill switch before attempting live execution."
            )

        self._log.info("LiveExecutionSafetyGuard: prerequisites satisfied.")

    # ------------------------------------------------------------------
    # Pilot order gate
    # ------------------------------------------------------------------

    def assert_pilot_order_allowed(
        self,
        order_intent: OrderIntent,
        config: LivePilotConfig,
        approval_decision: ApprovalDecision,
        risk_decision: Any | None,
    ) -> None:
        """Validate that a live pilot order is fully authorised.

        Checks (in order):
          1. LIVE_ORDER_EXECUTION_ENABLED flag
          2. LIVE_ORDER_PILOT_ENABLED flag
          3. Kill switch not active
          4. Risk approved (if a risk_decision is provided)
          5. Approval status is APPROVED
          6. Symbol in allowed_symbols
          7. Exchange matches allowed_exchange
          8. Product matches allowed_product
          9. OrderType in allowed_order_types
         10. Quantity <= max_order_quantity

        Args:
            order_intent:       The order to be placed.
            config:             LivePilotConfig with all pilot constraints.
            approval_decision:  Result from LiveOrderApprovalGate.require_approval().
            risk_decision:      Result from RiskEngine.check_order_intent(), or None.

        Raises:
            SafetyError: if any check fails.
        """
        # 1. Master execution flag
        if not config.live_order_execution_enabled:
            raise SafetyError(
                "LIVE_ORDER_EXECUTION_ENABLED is False. "
                "Set it to true to enable live order placement."
            )

        # 2. Pilot-specific flag
        if not config.live_order_pilot_enabled:
            raise SafetyError(
                "LIVE_ORDER_PILOT_ENABLED is False. "
                "Set it to true to enable the live order pilot."
            )

        # 3. Kill switch
        if self._kill_switch is not None and self._kill_switch.is_active():
            raise SafetyError(
                f"Kill switch is active — reason: {self._kill_switch.reason!r}. "
                "Deactivate the kill switch before placing live orders."
            )

        # 4. Risk decision
        if risk_decision is not None and not risk_decision.approved:
            raise SafetyError(
                f"Risk engine blocked this order: {risk_decision.reason_code} — "
                f"{risk_decision.reason_message}"
            )

        # 5. Approval status
        from trading_engine.live_execution.models import ApprovalStatus

        if approval_decision.status != ApprovalStatus.APPROVED:
            raise SafetyError(
                f"Order approval status is {approval_decision.status!r}, not APPROVED. "
                "The order must be explicitly approved before placement."
            )

        # 6. Symbol whitelist
        symbol = str(order_intent.symbol).upper()
        allowed_symbols = [s.upper() for s in config.allowed_symbols]
        if not allowed_symbols:
            raise SafetyError(
                "LIVE_ALLOWED_SYMBOLS is empty — no symbols are permitted. "
                "Add at least one symbol to the whitelist."
            )
        if symbol not in allowed_symbols:
            raise SafetyError(
                f"Symbol {symbol!r} is not in the allowed symbols list: {allowed_symbols}."
            )

        # 7. Exchange
        intent_exchange = str(order_intent.exchange).upper()
        allowed_exchange = config.allowed_exchange.upper()
        if intent_exchange != allowed_exchange:
            raise SafetyError(
                f"Exchange {intent_exchange!r} is not the allowed exchange {allowed_exchange!r}."
            )

        # 8. Product
        intent_product = str(order_intent.product).upper()
        allowed_product = config.allowed_product.upper()
        if intent_product != allowed_product:
            raise SafetyError(
                f"Product {intent_product!r} is not the allowed product {allowed_product!r}."
            )

        # 9. Order type
        intent_order_type = str(order_intent.order_type).upper()
        allowed_order_types = [ot.upper() for ot in config.allowed_order_types]
        if intent_order_type not in allowed_order_types:
            raise SafetyError(
                f"Order type {intent_order_type!r} is not in allowed order types: "
                f"{allowed_order_types}."
            )

        # 10. Quantity
        if order_intent.quantity > config.max_order_quantity:
            raise SafetyError(
                f"Order quantity {order_intent.quantity} exceeds the pilot maximum "
                f"{config.max_order_quantity}."
            )

        self._log.info(
            "LiveExecutionSafetyGuard: pilot order allowed — %s %s %s qty=%s",
            order_intent.side,
            order_intent.quantity,
            order_intent.symbol,
            order_intent.quantity,
        )
