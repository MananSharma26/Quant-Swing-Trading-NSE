"""Tests for OrderLedger.

Verifies in-memory storage, retrieval, status updates, and error handling.
No real Zerodha calls, no database.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from trading_engine.common.exceptions import OrderNotFoundError, OrderStateTransitionError
from trading_engine.domain.enums import (
    Exchange,
    OrderStatus,
    OrderType,
    ProductType,
    RiskReasonCode,
    Side,
)
from trading_engine.domain.identifiers import (
    generate_fill_id,
    generate_internal_order_id,
    generate_risk_decision_id,
)
from trading_engine.domain.models import InternalOrder, RiskDecision, TradeFill
from trading_engine.execution.ledger import OrderLedger

_TS = datetime(2024, 1, 15, 9, 30, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    symbol: str = "RELIANCE",
    status: OrderStatus = OrderStatus.CREATED,
) -> InternalOrder:
    return InternalOrder(
        internal_order_id=generate_internal_order_id(),
        strategy_id="test_strategy",
        symbol=symbol,
        exchange=Exchange.NSE,
        side=Side.BUY,
        quantity=10,
        order_type=OrderType.MARKET,
        product=ProductType.MIS,
        status=status,
        created_at=_TS,
        updated_at=_TS,
    )


def _make_fill(order: InternalOrder) -> TradeFill:
    return TradeFill(
        fill_id=generate_fill_id(),
        internal_order_id=order.internal_order_id,
        symbol=order.symbol,
        exchange=order.exchange,
        side=order.side,
        quantity=order.quantity,
        price=Decimal("2800.0"),
        timestamp=_TS,
    )


def _make_risk_decision(approved: bool = True) -> RiskDecision:
    return RiskDecision(
        risk_decision_id=generate_risk_decision_id(),
        approved=approved,
        reason_code=RiskReasonCode.APPROVED
        if approved
        else RiskReasonCode.DAILY_LOSS_LIMIT_BREACHED,
        reason_message="ok" if approved else "daily loss exceeded",
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# Tests: orders
# ---------------------------------------------------------------------------


class TestLedgerOrders:
    def test_add_and_get_order(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        retrieved = ledger.get_order(order.internal_order_id)
        assert retrieved.internal_order_id == order.internal_order_id

    def test_list_orders_empty(self):
        ledger = OrderLedger()
        assert ledger.list_orders() == []

    def test_list_orders_returns_all(self):
        ledger = OrderLedger()
        o1 = _make_order("RELIANCE")
        o2 = _make_order("TCS")
        ledger.add_order(o1)
        ledger.add_order(o2)
        orders = ledger.list_orders()
        assert len(orders) == 2

    def test_get_unknown_order_raises(self):
        ledger = OrderLedger()
        with pytest.raises(OrderNotFoundError):
            ledger.get_order("ord_nonexistent")

    def test_add_order_overwrites_same_id(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        updated = order.model_copy(update={"status": OrderStatus.RISK_APPROVED, "updated_at": _TS})
        ledger.add_order(updated)
        assert ledger.get_order(order.internal_order_id).status == OrderStatus.RISK_APPROVED


# ---------------------------------------------------------------------------
# Tests: update_order_status
# ---------------------------------------------------------------------------


class TestLedgerUpdateOrderStatus:
    def test_valid_transition_updates_status(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        updated = ledger.update_order_status(order.internal_order_id, OrderStatus.RISK_APPROVED)
        assert updated.status == OrderStatus.RISK_APPROVED
        assert ledger.get_order(order.internal_order_id).status == OrderStatus.RISK_APPROVED

    def test_invalid_transition_raises(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        with pytest.raises(OrderStateTransitionError):
            ledger.update_order_status(order.internal_order_id, OrderStatus.FILLED)

    def test_unknown_order_id_raises(self):
        ledger = OrderLedger()
        with pytest.raises(OrderNotFoundError):
            ledger.update_order_status("ord_ghost", OrderStatus.RISK_APPROVED)

    def test_broker_order_id_attached(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        ledger.update_order_status(
            order.internal_order_id,
            OrderStatus.RISK_APPROVED,
            broker_order_id="zerodha_123",
        )
        assert ledger.get_order(order.internal_order_id).broker_order_id == "zerodha_123"

    def test_updated_at_changes(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        ledger.update_order_status(order.internal_order_id, OrderStatus.RISK_APPROVED)
        # updated_at is set to datetime.now() so it may differ
        updated = ledger.get_order(order.internal_order_id)
        assert updated.updated_at >= order.updated_at

    def test_chained_transitions(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        ledger.update_order_status(order.internal_order_id, OrderStatus.RISK_APPROVED)
        ledger.update_order_status(order.internal_order_id, OrderStatus.SUBMITTED)
        ledger.update_order_status(order.internal_order_id, OrderStatus.OPEN)
        ledger.update_order_status(order.internal_order_id, OrderStatus.FILLED)
        assert ledger.get_order(order.internal_order_id).status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Tests: fills
# ---------------------------------------------------------------------------


class TestLedgerFills:
    def test_add_fill_and_list(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        fill = _make_fill(order)
        ledger.add_fill(fill)
        fills = ledger.list_fills()
        assert len(fills) == 1
        assert fills[0].fill_id == fill.fill_id

    def test_list_fills_empty(self):
        ledger = OrderLedger()
        assert ledger.list_fills() == []

    def test_multiple_fills(self):
        ledger = OrderLedger()
        order = _make_order()
        ledger.add_order(order)
        ledger.add_fill(_make_fill(order))
        ledger.add_fill(_make_fill(order))
        assert len(ledger.list_fills()) == 2


# ---------------------------------------------------------------------------
# Tests: risk decisions
# ---------------------------------------------------------------------------


class TestLedgerRiskDecisions:
    def test_add_and_list_risk_decision(self):
        ledger = OrderLedger()
        decision = _make_risk_decision(approved=True)
        ledger.add_risk_decision(decision)
        decisions = ledger.list_risk_decisions()
        assert len(decisions) == 1
        assert decisions[0].risk_decision_id == decision.risk_decision_id

    def test_rejected_risk_decision_stored(self):
        ledger = OrderLedger()
        decision = _make_risk_decision(approved=False)
        ledger.add_risk_decision(decision)
        assert ledger.list_risk_decisions()[0].approved is False

    def test_list_risk_decisions_empty(self):
        ledger = OrderLedger()
        assert ledger.list_risk_decisions() == []

    def test_multiple_risk_decisions(self):
        ledger = OrderLedger()
        ledger.add_risk_decision(_make_risk_decision(True))
        ledger.add_risk_decision(_make_risk_decision(False))
        assert len(ledger.list_risk_decisions()) == 2
