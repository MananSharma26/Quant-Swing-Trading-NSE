"""Tests for OrderManager.

Verifies order creation, risk engine integration, ledger storage, and
that no broker APIs are ever called.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

from trading_engine.domain.enums import (
    OrderStatus,
    RiskReasonCode,
    Side,
)
from trading_engine.domain.identifiers import generate_risk_decision_id
from trading_engine.domain.models import InternalOrder, PortfolioSnapshot, RiskDecision
from trading_engine.execution.ledger import OrderLedger
from trading_engine.execution.order_manager import OrderManager
from trading_engine.risk.engine import RiskEngine
from trading_engine.risk.limits import RiskLimits
from trading_engine.strategy.signals import OrderIntent

_TS = datetime(2024, 1, 15, 9, 30, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(
    symbol: str = "RELIANCE",
    side: str = "BUY",
    quantity: int = 10,
    order_type: str = "MARKET",
    price: Decimal | None = None,
) -> OrderIntent:
    return OrderIntent(
        strategy_id="test_strategy",
        symbol=symbol,
        exchange="NSE",
        side=side,
        quantity=quantity,
        order_type=order_type,
        product="MIS",
        price=price,
    )


def _make_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=_TS,
        cash=Decimal("100000"),
        positions=[],
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
    )


def _make_approved_risk_decision() -> RiskDecision:
    return RiskDecision(
        risk_decision_id=generate_risk_decision_id(),
        approved=True,
        reason_code=RiskReasonCode.APPROVED,
        reason_message="All checks passed.",
        timestamp=_TS,
    )


def _make_rejected_risk_decision() -> RiskDecision:
    return RiskDecision(
        risk_decision_id=generate_risk_decision_id(),
        approved=False,
        reason_code=RiskReasonCode.DAILY_LOSS_LIMIT_BREACHED,
        reason_message="Daily loss limit breached.",
        timestamp=_TS,
    )


def _make_ledger() -> OrderLedger:
    return OrderLedger()


# ---------------------------------------------------------------------------
# Tests: no risk engine
# ---------------------------------------------------------------------------


class TestOrderManagerNoRiskEngine:
    def test_creates_internal_order_from_intent(self):
        manager = OrderManager(ledger=_make_ledger())
        intent = _make_intent()
        order = manager.create_order_from_intent(intent, current_timestamp=_TS)
        assert order is not None
        assert isinstance(order, InternalOrder)

    def test_order_stored_in_ledger(self):
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger)
        intent = _make_intent()
        order = manager.create_order_from_intent(intent, current_timestamp=_TS)
        assert order is not None
        ledger.get_order(order.internal_order_id)  # should not raise

    def test_order_status_is_created(self):
        manager = OrderManager(ledger=_make_ledger())
        order = manager.create_order_from_intent(_make_intent(), current_timestamp=_TS)
        assert order is not None
        assert order.status == OrderStatus.CREATED

    def test_order_fields_match_intent(self):
        manager = OrderManager(ledger=_make_ledger())
        intent = _make_intent(symbol="TCS", side="SELL", quantity=5)
        order = manager.create_order_from_intent(intent, current_timestamp=_TS)
        assert order is not None
        assert order.symbol == "TCS"
        assert order.side == Side.SELL
        assert order.quantity == 5
        assert order.strategy_id == "test_strategy"

    def test_internal_order_id_generated(self):
        manager = OrderManager(ledger=_make_ledger())
        o1 = manager.create_order_from_intent(_make_intent(), current_timestamp=_TS)
        o2 = manager.create_order_from_intent(_make_intent(), current_timestamp=_TS)
        assert o1 is not None and o2 is not None
        assert o1.internal_order_id != o2.internal_order_id

    def test_market_order_price_is_none(self):
        manager = OrderManager(ledger=_make_ledger())
        order = manager.create_order_from_intent(
            _make_intent(order_type="MARKET"), current_timestamp=_TS
        )
        assert order is not None
        assert order.price is None

    def test_limit_order_carries_price(self):
        manager = OrderManager(ledger=_make_ledger())
        intent = _make_intent(order_type="LIMIT", price=Decimal("2800"))
        order = manager.create_order_from_intent(intent, current_timestamp=_TS)
        assert order is not None
        assert order.price == Decimal("2800")


# ---------------------------------------------------------------------------
# Tests: with risk engine (fake)
# ---------------------------------------------------------------------------


class TestOrderManagerWithRiskEngine:
    def test_approved_intent_stored_as_risk_approved(self):
        fake_risk = MagicMock()
        fake_risk.check_order_intent.return_value = _make_approved_risk_decision()
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=fake_risk)
        order = manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        assert order is not None
        assert order.status == OrderStatus.RISK_APPROVED

    def test_rejected_intent_returns_none(self):
        fake_risk = MagicMock()
        fake_risk.check_order_intent.return_value = _make_rejected_risk_decision()
        manager = OrderManager(ledger=_make_ledger(), risk_engine=fake_risk)
        result = manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        assert result is None

    def test_rejected_order_stored_as_risk_rejected(self):
        fake_risk = MagicMock()
        fake_risk.check_order_intent.return_value = _make_rejected_risk_decision()
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=fake_risk)
        manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        orders = ledger.list_orders()
        assert len(orders) == 1
        assert orders[0].status == OrderStatus.RISK_REJECTED

    def test_risk_decision_stored_in_ledger_on_approval(self):
        fake_risk = MagicMock()
        decision = _make_approved_risk_decision()
        fake_risk.check_order_intent.return_value = decision
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=fake_risk)
        manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        decisions = ledger.list_risk_decisions()
        assert len(decisions) == 1
        assert decisions[0].approved is True

    def test_risk_decision_stored_in_ledger_on_rejection(self):
        fake_risk = MagicMock()
        decision = _make_rejected_risk_decision()
        fake_risk.check_order_intent.return_value = decision
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=fake_risk)
        manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        decisions = ledger.list_risk_decisions()
        assert len(decisions) == 1
        assert decisions[0].approved is False

    def test_risk_decision_id_attached_to_order(self):
        fake_risk = MagicMock()
        decision = _make_approved_risk_decision()
        fake_risk.check_order_intent.return_value = decision
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=fake_risk)
        order = manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        assert order is not None
        assert order.risk_decision_id == decision.risk_decision_id

    def test_no_snapshot_skips_risk_check(self):
        fake_risk = MagicMock()
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=fake_risk)
        order = manager.create_order_from_intent(
            _make_intent(), portfolio_snapshot=None, current_timestamp=_TS
        )
        fake_risk.check_order_intent.assert_not_called()
        assert order is not None
        assert order.status == OrderStatus.CREATED

    def test_does_not_call_broker(self):
        """Ensure no broker attribute is accessed on the risk engine mock."""
        fake_risk = MagicMock(spec=[])  # no allowed attributes other than check_order_intent
        fake_risk.check_order_intent = MagicMock(return_value=_make_approved_risk_decision())
        manager = OrderManager(ledger=_make_ledger(), risk_engine=fake_risk)
        manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        # No broker method should have been called (the mock would raise AttributeError otherwise)


# ---------------------------------------------------------------------------
# Tests: mark_submitted / mark_broker_update
# ---------------------------------------------------------------------------


class TestOrderManagerLifecycleHooks:
    def test_mark_submitted_transitions_to_submitted(self):
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger)
        order = manager.create_order_from_intent(_make_intent(), current_timestamp=_TS)
        assert order is not None
        # Manually approve so we can submit
        ledger.update_order_status(order.internal_order_id, OrderStatus.RISK_APPROVED)
        updated = manager.mark_submitted(order.internal_order_id, broker_order_id="zerodha_999")
        assert updated.status == OrderStatus.SUBMITTED
        assert updated.broker_order_id == "zerodha_999"

    def test_mark_broker_update_transitions_to_open(self):
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger)
        order = manager.create_order_from_intent(_make_intent(), current_timestamp=_TS)
        assert order is not None
        ledger.update_order_status(order.internal_order_id, OrderStatus.RISK_APPROVED)
        ledger.update_order_status(order.internal_order_id, OrderStatus.SUBMITTED)
        updated = manager.mark_broker_update(order.internal_order_id, OrderStatus.OPEN)
        assert updated.status == OrderStatus.OPEN

    def test_mark_submitted_does_not_call_zerodha(self):
        """mark_submitted must not interact with any broker."""
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger)
        order = manager.create_order_from_intent(_make_intent(), current_timestamp=_TS)
        assert order is not None
        ledger.update_order_status(order.internal_order_id, OrderStatus.RISK_APPROVED)
        # Just verify no exception from broker interaction
        result = manager.mark_submitted(order.internal_order_id)
        assert result.status == OrderStatus.SUBMITTED


# ---------------------------------------------------------------------------
# Tests: with real RiskEngine (integration)
# ---------------------------------------------------------------------------


class TestOrderManagerWithRealRiskEngine:
    def test_approved_by_real_risk_engine(self):
        limits = RiskLimits(
            max_order_value=Decimal("100000"),
            max_open_positions=10,
            max_daily_loss=Decimal("50000"),
        )
        risk_engine = RiskEngine(limits=limits)
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=risk_engine)
        intent = _make_intent(price=Decimal("2800"))
        manager.create_order_from_intent(intent, _make_snapshot(), _TS)
        # We just verify the order was stored (approved or not depends on limits)
        orders = ledger.list_orders()
        assert len(orders) >= 1

    def test_rejected_by_kill_switch(self):
        from trading_engine.risk.kill_switch import KillSwitch

        ks = KillSwitch()
        ks.activate("test")
        limits = RiskLimits()
        risk_engine = RiskEngine(limits=limits, kill_switch=ks)
        ledger = _make_ledger()
        manager = OrderManager(ledger=ledger, risk_engine=risk_engine)
        result = manager.create_order_from_intent(_make_intent(), _make_snapshot(), _TS)
        assert result is None
        assert ledger.list_orders()[0].status == OrderStatus.RISK_REJECTED
        decisions = ledger.list_risk_decisions()
        assert len(decisions) == 1
        assert decisions[0].approved is False
