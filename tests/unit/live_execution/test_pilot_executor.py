"""Tests for live_execution.pilot_executor — LiveOrderPilotExecutor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from trading_engine.common.exceptions import SafetyError
from trading_engine.live_execution.approvals import LiveOrderApprovalGate
from trading_engine.live_execution.models import ApprovalMode, ApprovalStatus
from trading_engine.live_execution.pilot_config import LivePilotConfig
from trading_engine.live_execution.pilot_executor import LiveOrderPilotExecutor, PilotOrderResult
from trading_engine.live_execution.safety import LiveExecutionSafetyGuard
from trading_engine.strategy.signals import OrderIntent


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeKite:
    def __init__(self, order_id: str = "ZRD999") -> None:
        self._order_id = order_id
        self.calls: list[dict[str, Any]] = []

    def place_order(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"order_id": self._order_id}


class _FailingKite:
    def place_order(self, **kwargs: Any) -> None:
        raise RuntimeError("Kite connection timeout")


class _FakeRiskDecision:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.reason_code = "APPROVED" if approved else "BLOCKED"
        self.reason_message = "ok" if approved else "blocked"


class _ApprovingRisk:
    def check_order_intent(self, intent: Any, snapshot: Any, ts: Any) -> _FakeRiskDecision:
        return _FakeRiskDecision(approved=True)


class _RejectingRisk:
    def check_order_intent(self, intent: Any, snapshot: Any, ts: Any) -> _FakeRiskDecision:
        return _FakeRiskDecision(approved=False)


_INTENT = OrderIntent(
    strategy_id="test",
    symbol="RELIANCE",
    exchange="NSE",
    side="BUY",
    quantity=1,
    order_type="MARKET",
    product="MIS",
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


def _make_broker(kite: Any = None) -> Any:
    from trading_engine.broker.zerodha.client import ZerodhaBroker

    b = ZerodhaBroker(kite_client=kite or _FakeKite())
    b.connect()
    return b


def _make_guard() -> LiveExecutionSafetyGuard:
    class _S:
        live_trading_enabled = True

    return LiveExecutionSafetyGuard(_S())


def _make_executor(kite: Any = None, risk: Any = None) -> LiveOrderPilotExecutor:
    return LiveOrderPilotExecutor(
        broker=_make_broker(kite),
        pilot_config=_make_enabled_config(),
        approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER),
        safety_guard=_make_guard(),
        risk_engine=risk,
    )


# ---------------------------------------------------------------------------
# PilotOrderResult
# ---------------------------------------------------------------------------


class TestPilotOrderResult:
    def test_to_dict_contains_expected_keys(self):
        result = PilotOrderResult(
            success=True,
            broker_order_id="ZRD123",
            approval_status=ApprovalStatus.APPROVED,
        )
        d = result.to_dict()
        assert "success" in d
        assert "broker_order_id" in d
        assert "approval_status" in d
        assert "error" in d

    def test_success_result(self):
        result = PilotOrderResult(
            success=True,
            broker_order_id="ZRD123",
            approval_status=ApprovalStatus.APPROVED,
        )
        assert result.success is True
        assert result.broker_order_id == "ZRD123"
        assert result.error is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPilotExecutorHappyPath:
    def test_execute_returns_success(self):
        executor = _make_executor(kite=_FakeKite("ZRD999"))
        result = executor.execute(_INTENT)
        assert result.success is True

    def test_broker_order_id_returned(self):
        executor = _make_executor(kite=_FakeKite("ZRD999"))
        result = executor.execute(_INTENT)
        assert result.broker_order_id == "ZRD999"

    def test_approval_status_approved(self):
        executor = _make_executor()
        result = executor.execute(_INTENT)
        assert result.approval_status == ApprovalStatus.APPROVED

    def test_kite_called_once(self):
        kite = _FakeKite()
        executor = _make_executor(kite=kite)
        executor.execute(_INTENT)
        assert len(kite.calls) == 1

    def test_no_error_in_result(self):
        executor = _make_executor()
        result = executor.execute(_INTENT)
        assert result.error is None


# ---------------------------------------------------------------------------
# Risk rejection
# ---------------------------------------------------------------------------


class TestPilotExecutorRiskRejection:
    def _executor_with_rejecting_risk(self) -> LiveOrderPilotExecutor:
        return LiveOrderPilotExecutor(
            broker=_make_broker(),
            pilot_config=_make_enabled_config(),
            approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER),
            safety_guard=_make_guard(),
            risk_engine=_RejectingRisk(),
        )

    def test_risk_rejection_returns_failure(self):
        executor = self._executor_with_rejecting_risk()

        class _FakeSnapshot:
            pass

        result = executor.execute(_INTENT, portfolio_snapshot=_FakeSnapshot())
        assert result.success is False

    def test_risk_rejection_status_is_auto_rejected(self):
        executor = self._executor_with_rejecting_risk()

        class _FakeSnapshot:
            pass

        result = executor.execute(_INTENT, portfolio_snapshot=_FakeSnapshot())
        assert result.approval_status == ApprovalStatus.AUTO_REJECTED

    def test_risk_rejection_no_kite_call(self):
        kite = _FakeKite()
        executor = LiveOrderPilotExecutor(
            broker=_make_broker(kite),
            pilot_config=_make_enabled_config(),
            approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER),
            safety_guard=_make_guard(),
            risk_engine=_RejectingRisk(),
        )

        class _FakeSnapshot:
            pass

        executor.execute(_INTENT, portfolio_snapshot=_FakeSnapshot())
        assert len(kite.calls) == 0

    def test_no_snapshot_skips_risk_check(self):
        executor = LiveOrderPilotExecutor(
            broker=_make_broker(_FakeKite("ZRD-skip")),
            pilot_config=_make_enabled_config(),
            approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER),
            safety_guard=_make_guard(),
            risk_engine=_RejectingRisk(),
        )
        result = executor.execute(_INTENT, portfolio_snapshot=None)
        # No snapshot → risk skipped → order proceeds
        assert result.success is True


# ---------------------------------------------------------------------------
# Broker failure
# ---------------------------------------------------------------------------


class TestPilotExecutorBrokerFailure:
    def test_kite_error_returns_failure(self):
        from trading_engine.broker.zerodha.client import ZerodhaBroker

        broker = ZerodhaBroker(kite_client=_FailingKite())
        broker.connect()
        executor = LiveOrderPilotExecutor(
            broker=broker,
            pilot_config=_make_enabled_config(),
            approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER),
            safety_guard=_make_guard(),
        )
        result = executor.execute(_INTENT)
        assert result.success is False
        assert result.error is not None

    def test_safety_error_returns_failure(self):
        executor = LiveOrderPilotExecutor(
            broker=_make_broker(),
            pilot_config=_make_enabled_config(live_order_pilot_enabled=False),
            approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER),
            safety_guard=_make_guard(),
        )
        result = executor.execute(_INTENT)
        assert result.success is False
        assert "LIVE_ORDER_PILOT_ENABLED" in (result.error or "")


# ---------------------------------------------------------------------------
# Manual approval gate
# ---------------------------------------------------------------------------


class TestPilotExecutorManualApproval:
    def test_manual_gate_returns_pending(self):
        executor = LiveOrderPilotExecutor(
            broker=_make_broker(),
            pilot_config=_make_enabled_config(),
            approval_gate=LiveOrderApprovalGate(mode=ApprovalMode.MANUAL_APPROVE),
            safety_guard=_make_guard(),
        )
        result = executor.execute(_INTENT)
        assert result.success is False
        assert result.approval_status == ApprovalStatus.PENDING
