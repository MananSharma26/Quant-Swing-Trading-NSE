"""End-to-end live order gating tests.

Verifies that the full chain — pilot_config + safety_guard + approval_gate +
broker.place_order() — blocks or passes as expected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from trading_engine.common.exceptions import SafetyError
from trading_engine.live_execution.approvals import LiveOrderApprovalGate
from trading_engine.live_execution.models import ApprovalMode, ApprovalStatus
from trading_engine.live_execution.pilot_config import LivePilotConfig
from trading_engine.live_execution.safety import LiveExecutionSafetyGuard
from trading_engine.strategy.signals import OrderIntent


class _FakeKite:
    """Minimal fake KiteConnect that records place_order calls."""

    def __init__(self, order_id: str = "ZRD123456") -> None:
        self._order_id = order_id
        self.calls: list[dict[str, Any]] = []

    def place_order(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"order_id": self._order_id}


def _make_intent(
    symbol: str = "RELIANCE",
    side: str = "BUY",
    quantity: int = 1,
    order_type: str = "MARKET",
    product: str = "MIS",
    exchange: str = "NSE",
) -> OrderIntent:
    return OrderIntent(
        strategy_id="gating_test",
        symbol=symbol,
        exchange=exchange,
        side=side,
        quantity=quantity,
        order_type=order_type,
        product=product,
    )


def _make_enabled_config(**overrides: Any) -> LivePilotConfig:
    defaults = dict(
        live_order_execution_enabled=True,
        live_order_pilot_enabled=True,
        max_order_quantity=5,
        allowed_symbols=["RELIANCE"],
        allowed_exchange="NSE",
        allowed_product="MIS",
        allowed_order_types=["MARKET", "LIMIT"],
    )
    defaults.update(overrides)
    return LivePilotConfig(**defaults)  # type: ignore[arg-type]


class TestBrokerPlaceOrderGating:
    """Tests for ZerodhaBroker.place_order() gating via safety guard."""

    def _broker(self, kite: _FakeKite | None = None) -> Any:
        from trading_engine.broker.zerodha.client import ZerodhaBroker

        b = ZerodhaBroker(kite_client=kite or _FakeKite())
        b.connect()
        return b

    def _guard(self) -> LiveExecutionSafetyGuard:
        class _S:
            live_trading_enabled = True

        return LiveExecutionSafetyGuard(_S())

    def _approval(self) -> Any:
        from trading_engine.live_execution.models import ApprovalDecision

        return ApprovalDecision(
            approval_id="gate-test-001",
            status=ApprovalStatus.APPROVED,
            decided_at=datetime.now(tz=UTC),
            decided_by="auto_paper",
        )

    def test_place_order_succeeds_with_all_flags_set(self):
        kite = _FakeKite()
        broker = self._broker(kite)
        broker_order_id = broker.place_order(
            order_intent=_make_intent(),
            pilot_config=_make_enabled_config(),
            approval_decision=self._approval(),
            risk_decision=None,
            safety_guard=self._guard(),
        )
        assert broker_order_id == "ZRD123456"
        assert len(kite.calls) == 1

    def test_place_order_passes_correct_params_to_kite(self):
        kite = _FakeKite()
        broker = self._broker(kite)
        broker.place_order(
            order_intent=_make_intent(symbol="RELIANCE", side="BUY", quantity=1),
            pilot_config=_make_enabled_config(),
            approval_decision=self._approval(),
            risk_decision=None,
            safety_guard=self._guard(),
        )
        call = kite.calls[0]
        assert call["tradingsymbol"] == "RELIANCE"
        assert call["transaction_type"] == "BUY"
        assert call["quantity"] == 1
        assert call["exchange"] == "NSE"

    def test_place_order_blocked_when_execution_disabled(self):
        broker = self._broker()
        config = _make_enabled_config(live_order_execution_enabled=False)
        with pytest.raises(SafetyError, match="LIVE_ORDER_EXECUTION_ENABLED"):
            broker.place_order(
                order_intent=_make_intent(),
                pilot_config=config,
                approval_decision=self._approval(),
                risk_decision=None,
                safety_guard=self._guard(),
            )

    def test_place_order_blocked_when_pilot_disabled(self):
        broker = self._broker()
        config = _make_enabled_config(live_order_pilot_enabled=False)
        with pytest.raises(SafetyError, match="LIVE_ORDER_PILOT_ENABLED"):
            broker.place_order(
                order_intent=_make_intent(),
                pilot_config=config,
                approval_decision=self._approval(),
                risk_decision=None,
                safety_guard=self._guard(),
            )

    def test_place_order_blocked_when_symbol_not_allowed(self):
        broker = self._broker()
        config = _make_enabled_config(allowed_symbols=["INFY"])
        with pytest.raises(SafetyError, match="not in the allowed symbols"):
            broker.place_order(
                order_intent=_make_intent(symbol="RELIANCE"),
                pilot_config=config,
                approval_decision=self._approval(),
                risk_decision=None,
                safety_guard=self._guard(),
            )

    def test_place_order_blocked_when_quantity_exceeds_max(self):
        broker = self._broker()
        config = _make_enabled_config(max_order_quantity=1)
        with pytest.raises(SafetyError, match="quantity"):
            broker.place_order(
                order_intent=_make_intent(quantity=2),
                pilot_config=config,
                approval_decision=self._approval(),
                risk_decision=None,
                safety_guard=self._guard(),
            )

    def test_place_order_requires_connection(self):
        from trading_engine.broker.zerodha.client import ZerodhaBroker
        from trading_engine.common.exceptions import BrokerConnectionError

        broker = ZerodhaBroker(kite_client=_FakeKite())
        # Not connected
        with pytest.raises(BrokerConnectionError):
            broker.place_order(
                order_intent=_make_intent(),
                pilot_config=_make_enabled_config(),
                approval_decision=self._approval(),
                risk_decision=None,
                safety_guard=self._guard(),
            )

    def test_sell_order_passes_correct_transaction_type(self):
        kite = _FakeKite()
        broker = self._broker(kite)
        broker.place_order(
            order_intent=_make_intent(symbol="RELIANCE", side="SELL", quantity=1),
            pilot_config=_make_enabled_config(),
            approval_decision=self._approval(),
            risk_decision=None,
            safety_guard=self._guard(),
        )
        assert kite.calls[0]["transaction_type"] == "SELL"
