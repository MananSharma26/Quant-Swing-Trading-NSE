"""In-memory order ledger.

Tracks InternalOrder objects, TradeFill objects, and RiskDecision objects
for the lifetime of a trading session.  All storage is in-memory; no
database persistence is implemented in this milestone.

Status updates are validated through OrderStateMachine so that illegal
transitions are caught immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from trading_engine.common.exceptions import OrderNotFoundError
from trading_engine.domain.enums import OrderStatus
from trading_engine.domain.models import InternalOrder, RiskDecision, TradeFill
from trading_engine.execution.state_machine import OrderStateMachine

logger = logging.getLogger(__name__)


class OrderLedger:
    """In-memory store for orders, fills, and risk decisions.

    Designed for single-session use.  Thread safety is not guaranteed in v1.

    Args:
        state_machine: Optional ``OrderStateMachine`` to validate status
                       transitions.  A default instance is created if not
                       provided.
    """

    def __init__(self, state_machine: OrderStateMachine | None = None) -> None:
        self._state_machine = state_machine or OrderStateMachine()
        self._orders: dict[str, InternalOrder] = {}
        self._fills: list[TradeFill] = []
        self._risk_decisions: list[RiskDecision] = []

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def add_order(self, order: InternalOrder) -> None:
        """Store an order.  Overwrites any existing order with the same ID."""
        self._orders[order.internal_order_id] = order
        logger.debug("Ledger: added order %s (status=%s)", order.internal_order_id, order.status)

    def get_order(self, internal_order_id: str) -> InternalOrder:
        """Return the order for *internal_order_id*.

        Raises:
            OrderNotFoundError: if the ID is not in the ledger.
        """
        try:
            return self._orders[internal_order_id]
        except KeyError as exc:
            raise OrderNotFoundError(
                f"No order found with internal_order_id={internal_order_id!r}."
            ) from exc

    def list_orders(self) -> list[InternalOrder]:
        """Return all orders (insertion order preserved)."""
        return list(self._orders.values())

    def update_order_status(
        self,
        internal_order_id: str,
        new_status: OrderStatus,
        *,
        broker_order_id: str | None = None,
        raw_broker_response: dict[str, Any] | None = None,
    ) -> InternalOrder:
        """Transition an order to *new_status* and return the updated order.

        The transition is validated by the state machine before any mutation.
        A new ``InternalOrder`` copy is created (the old one is discarded).

        Args:
            internal_order_id:   The order to update.
            new_status:          Target status (must be a valid transition).
            broker_order_id:     Optional broker-side order ID to attach.
            raw_broker_response: Optional raw broker payload to store.

        Returns:
            The updated InternalOrder.

        Raises:
            OrderNotFoundError:       if the order is not in the ledger.
            OrderStateTransitionError: if the transition is invalid.
        """
        order = self.get_order(internal_order_id)
        self._state_machine.transition(order.status, new_status)  # raises if invalid

        updated = order.model_copy(
            update={
                "status": new_status,
                "updated_at": datetime.now(),
                "broker_order_id": broker_order_id
                if broker_order_id is not None
                else order.broker_order_id,
                "raw_broker_response": raw_broker_response
                if raw_broker_response is not None
                else order.raw_broker_response,
            }
        )
        self._orders[internal_order_id] = updated
        logger.debug(
            "Ledger: order %s transitioned %s → %s",
            internal_order_id,
            order.status,
            new_status,
        )
        return updated

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    def add_fill(self, fill: TradeFill) -> None:
        """Append a trade fill to the ledger."""
        self._fills.append(fill)
        logger.debug("Ledger: added fill %s for order %s", fill.fill_id, fill.internal_order_id)

    def list_fills(self) -> list[TradeFill]:
        """Return all fills in insertion order."""
        return list(self._fills)

    # ------------------------------------------------------------------
    # Risk decisions
    # ------------------------------------------------------------------

    def add_risk_decision(self, decision: RiskDecision) -> None:
        """Append a risk decision to the ledger."""
        self._risk_decisions.append(decision)
        logger.debug(
            "Ledger: added risk decision %s (approved=%s)",
            decision.risk_decision_id,
            decision.approved,
        )

    def list_risk_decisions(self) -> list[RiskDecision]:
        """Return all risk decisions in insertion order."""
        return list(self._risk_decisions)
