"""Order Manager — converts OrderIntents into InternalOrders.

Responsibilities:
  - Convert an OrderIntent into an InternalOrder (status: CREATED).
  - Run the risk engine (if provided) and store the RiskDecision.
  - Transition the order to RISK_APPROVED or RISK_REJECTED accordingly.
  - Store all orders and decisions in the OrderLedger.
  - Never submit orders to a broker.
  - Never call Zerodha APIs.

Not implemented in this milestone:
  - Broker submission (mark_submitted does not call the broker).
  - Order modification or cancellation.
  - WebSocket streaming.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from trading_engine.domain.enums import (
    Exchange,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    TimeInForce,
)
from trading_engine.domain.identifiers import generate_internal_order_id
from trading_engine.domain.models import InternalOrder, PortfolioSnapshot
from trading_engine.execution.ledger import OrderLedger
from trading_engine.execution.state_machine import OrderStateMachine
from trading_engine.strategy.signals import OrderIntent

_TIF_MAP = {"DAY": TimeInForce.DAY, "IOC": TimeInForce.IOC}


def _intent_to_internal_order(intent: OrderIntent, ts: datetime) -> InternalOrder:
    """Build an InternalOrder (status=CREATED) from an OrderIntent."""
    return InternalOrder(
        internal_order_id=generate_internal_order_id(),
        strategy_id=intent.strategy_id,
        symbol=intent.symbol,
        exchange=Exchange(intent.exchange),
        side=Side(intent.side),
        quantity=intent.quantity,
        order_type=OrderType(intent.order_type),
        product=ProductType(intent.product),
        price=intent.price,
        trigger_price=intent.trigger_price,
        time_in_force=_TIF_MAP.get(intent.validity, TimeInForce.DAY),
        status=OrderStatus.CREATED,
        created_at=ts,
        updated_at=ts,
    )


class OrderManager:
    """Convert OrderIntents to InternalOrders and apply pre-trade risk checks.

    Args:
        risk_engine:   Optional risk engine.  When provided, every intent is
                       checked and the decision stored in the ledger.
        ledger:        The in-memory order ledger.
        state_machine: Optional state machine (a default one is created if
                       not provided).
        logger:        Optional logger; defaults to module logger.
    """

    def __init__(
        self,
        ledger: OrderLedger,
        risk_engine: Any | None = None,
        state_machine: OrderStateMachine | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._ledger = ledger
        self._risk_engine = risk_engine
        self._state_machine = state_machine or OrderStateMachine()
        self._log = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Primary entry point
    # ------------------------------------------------------------------

    def create_order_from_intent(
        self,
        intent: OrderIntent,
        portfolio_snapshot: PortfolioSnapshot | None = None,
        current_timestamp: datetime | None = None,
    ) -> InternalOrder | None:
        """Convert an OrderIntent into a stored InternalOrder.

        Workflow:
          1. Build InternalOrder (CREATED).
          2. If risk_engine is set, run check and store the RiskDecision.
             a. Approved  → transition to RISK_APPROVED, store, return order.
             b. Rejected  → transition to RISK_REJECTED, store, return None.
          3. If no risk_engine → store as CREATED, return order.

        Args:
            intent:            The strategy's order intent.
            portfolio_snapshot: Current portfolio state for risk checks.
                                If None and risk_engine is set, a minimal
                                snapshot is not constructed — the risk engine
                                call is skipped gracefully.
            current_timestamp: Timestamp for the order.  Defaults to now.

        Returns:
            The stored InternalOrder if accepted (or no risk engine), else None.
        """
        ts = current_timestamp or datetime.now()
        order = _intent_to_internal_order(intent, ts)

        if self._risk_engine is None:
            self._ledger.add_order(order)
            self._log.debug(
                "OrderManager: no risk engine — stored order %s as CREATED",
                order.internal_order_id,
            )
            return order

        if portfolio_snapshot is None:
            self._log.warning(
                "OrderManager: risk engine set but no portfolio_snapshot provided. "
                "Storing order %s as CREATED without risk check.",
                order.internal_order_id,
            )
            self._ledger.add_order(order)
            return order

        # Run risk check.
        decision = self._risk_engine.check_order_intent(intent, portfolio_snapshot, ts)
        self._ledger.add_risk_decision(decision)

        # Store order first (CREATED), then transition.
        self._ledger.add_order(order)
        order_id = order.internal_order_id

        if decision.approved:
            updated = self._ledger.update_order_status(order_id, OrderStatus.RISK_APPROVED)
            updated = updated.model_copy(update={"risk_decision_id": decision.risk_decision_id})
            self._ledger.add_order(updated)  # overwrite with risk_decision_id attached
            self._log.debug(
                "OrderManager: order %s RISK_APPROVED (decision=%s)",
                order_id,
                decision.risk_decision_id,
            )
            return updated
        else:
            updated = self._ledger.update_order_status(order_id, OrderStatus.RISK_REJECTED)
            updated = updated.model_copy(update={"risk_decision_id": decision.risk_decision_id})
            self._ledger.add_order(updated)  # overwrite with risk_decision_id attached
            self._log.debug(
                "OrderManager: order %s RISK_REJECTED — %s",
                order_id,
                decision.reason_message,
            )
            return None

    # ------------------------------------------------------------------
    # Broker lifecycle hooks (do not call broker APIs)
    # ------------------------------------------------------------------

    def mark_submitted(
        self,
        internal_order_id: str,
        broker_order_id: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> InternalOrder:
        """Transition order to SUBMITTED and optionally attach broker metadata.

        This method only updates internal state.  It does NOT submit the order
        to any broker and does NOT call Zerodha APIs.

        Args:
            internal_order_id: The internal order to mark.
            broker_order_id:   Optional broker-side order ID received after
                               submission (populated by the caller after a real
                               broker call in future milestones).
            raw_response:      Optional raw broker response payload.

        Returns:
            The updated InternalOrder.
        """
        return self._ledger.update_order_status(
            internal_order_id,
            OrderStatus.SUBMITTED,
            broker_order_id=broker_order_id,
            raw_broker_response=raw_response,
        )

    def mark_broker_update(
        self,
        internal_order_id: str,
        broker_status: OrderStatus,
        raw_response: dict[str, Any] | None = None,
    ) -> InternalOrder:
        """Apply a broker-reported status to an order.

        This method only updates internal state.  It does NOT call Zerodha APIs.

        Args:
            internal_order_id: The internal order to update.
            broker_status:     The OrderStatus as mapped from the broker response
                               (use map_zerodha_order_status for Zerodha).
            raw_response:      Optional raw broker response payload.

        Returns:
            The updated InternalOrder.
        """
        return self._ledger.update_order_status(
            internal_order_id,
            broker_status,
            raw_broker_response=raw_response,
        )
