"""Tests for live_execution.safety — LiveExecutionSafetyGuard."""

from __future__ import annotations

import pytest

from trading_engine.common.exceptions import SafetyError
from trading_engine.live_execution.approvals import LiveOrderApprovalGate
from trading_engine.live_execution.models import ApprovalDecision, ApprovalMode, ApprovalStatus
from trading_engine.live_execution.pilot_config import LivePilotConfig
from trading_engine.live_execution.safety import LiveExecutionSafetyGuard
from trading_engine.risk.kill_switch import KillSwitch
from trading_engine.strategy.signals import OrderIntent

from datetime import UTC, datetime


class _Settings:
    def __init__(self, live_trading_enabled: bool = False):
        self.live_trading_enabled = live_trading_enabled


def _make_intent(
    symbol: str = "RELIANCE",
    side: str = "BUY",
    quantity: int = 1,
    order_type: str = "MARKET",
    product: str = "MIS",
    exchange: str = "NSE",
) -> OrderIntent:
    return OrderIntent(
        strategy_id="test",
        symbol=symbol,
        exchange=exchange,
        side=side,
        quantity=quantity,
        order_type=order_type,
        product=product,
    )


def _make_approved_decision() -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="test-id-001",
        status=ApprovalStatus.APPROVED,
        decided_at=datetime.now(tz=UTC),
        decided_by="auto_paper",
    )


def _make_config(**kwargs: object) -> LivePilotConfig:
    defaults = dict(
        live_order_execution_enabled=True,
        live_order_pilot_enabled=True,
        max_order_quantity=5,
        allowed_symbols=["RELIANCE", "INFY"],
        allowed_exchange="NSE",
        allowed_product="MIS",
        allowed_order_types=["MARKET", "LIMIT"],
    )
    defaults.update(kwargs)
    return LivePilotConfig(**defaults)  # type: ignore[arg-type]


class _ApprovedRiskDecision:
    approved = True
    reason_code = "APPROVED"
    reason_message = "ok"


class _RejectedRiskDecision:
    approved = False
    reason_code = "ORDER_VALUE_LIMIT_BREACHED"
    reason_message = "value too large"


# ---------------------------------------------------------------------------
# assert_live_execution_allowed — prerequisite check
# ---------------------------------------------------------------------------


class TestLiveExecutionAllowed:
    def test_raises_when_live_trading_disabled(self):
        guard = LiveExecutionSafetyGuard(_Settings(live_trading_enabled=False))
        with pytest.raises(SafetyError, match="LIVE_TRADING_ENABLED"):
            guard.assert_live_execution_allowed()

    def test_passes_when_live_trading_enabled_and_no_kill_switch(self):
        guard = LiveExecutionSafetyGuard(_Settings(live_trading_enabled=True))
        guard.assert_live_execution_allowed()  # must not raise

    def test_raises_when_kill_switch_active(self):
        ks = KillSwitch()
        ks.activate("daily loss limit hit")
        guard = LiveExecutionSafetyGuard(_Settings(live_trading_enabled=True), kill_switch=ks)
        with pytest.raises(SafetyError, match="Kill switch"):
            guard.assert_live_execution_allowed()

    def test_passes_with_inactive_kill_switch(self):
        ks = KillSwitch()
        guard = LiveExecutionSafetyGuard(_Settings(live_trading_enabled=True), kill_switch=ks)
        guard.assert_live_execution_allowed()  # must not raise

    def test_raises_after_kill_switch_activated(self):
        ks = KillSwitch()
        guard = LiveExecutionSafetyGuard(_Settings(live_trading_enabled=True), kill_switch=ks)
        guard.assert_live_execution_allowed()  # fine initially
        ks.activate("manual stop")
        with pytest.raises(SafetyError):
            guard.assert_live_execution_allowed()

    def test_passes_after_kill_switch_deactivated(self):
        ks = KillSwitch()
        ks.activate("test")
        guard = LiveExecutionSafetyGuard(_Settings(live_trading_enabled=True), kill_switch=ks)
        ks.deactivate()
        guard.assert_live_execution_allowed()  # must not raise


class TestNoSettingsAttribute:
    def test_missing_attribute_treated_as_disabled(self):
        guard = LiveExecutionSafetyGuard(object())  # plain object, no live_trading_enabled
        with pytest.raises(SafetyError):
            guard.assert_live_execution_allowed()


# ---------------------------------------------------------------------------
# assert_pilot_order_allowed — full per-order gate
# ---------------------------------------------------------------------------


class TestPilotOrderAllowed:
    def _guard(self, kill_switch: KillSwitch | None = None) -> LiveExecutionSafetyGuard:
        return LiveExecutionSafetyGuard(_Settings(live_trading_enabled=True), kill_switch=kill_switch)

    def test_passes_when_all_conditions_met(self):
        guard = self._guard()
        guard.assert_pilot_order_allowed(
            order_intent=_make_intent(),
            config=_make_config(),
            approval_decision=_make_approved_decision(),
            risk_decision=_ApprovedRiskDecision(),
        )  # must not raise

    def test_passes_with_no_risk_decision(self):
        guard = self._guard()
        guard.assert_pilot_order_allowed(
            order_intent=_make_intent(),
            config=_make_config(),
            approval_decision=_make_approved_decision(),
            risk_decision=None,
        )  # must not raise

    def test_raises_when_execution_disabled(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="LIVE_ORDER_EXECUTION_ENABLED"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(),
                config=_make_config(live_order_execution_enabled=False),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_pilot_disabled(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="LIVE_ORDER_PILOT_ENABLED"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(),
                config=_make_config(live_order_pilot_enabled=False),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_kill_switch_active(self):
        ks = KillSwitch()
        ks.activate("risk limit")
        guard = self._guard(kill_switch=ks)
        with pytest.raises(SafetyError, match="Kill switch"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(),
                config=_make_config(),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_risk_rejected(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="Risk engine blocked"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(),
                config=_make_config(),
                approval_decision=_make_approved_decision(),
                risk_decision=_RejectedRiskDecision(),
            )

    def test_raises_when_approval_not_approved(self):
        guard = self._guard()
        pending = ApprovalDecision(
            approval_id="test-pending",
            status=ApprovalStatus.PENDING,
            decided_at=datetime.now(tz=UTC),
        )
        with pytest.raises(SafetyError, match="approval status"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(),
                config=_make_config(),
                approval_decision=pending,
                risk_decision=None,
            )

    def test_raises_when_symbol_not_in_whitelist(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="not in the allowed symbols"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(symbol="TCS"),
                config=_make_config(allowed_symbols=["RELIANCE", "INFY"]),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_allowed_symbols_empty(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="LIVE_ALLOWED_SYMBOLS is empty"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(),
                config=_make_config(allowed_symbols=[]),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_exchange_mismatch(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="Exchange"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(exchange="BSE"),
                config=_make_config(allowed_exchange="NSE"),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_product_mismatch(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="Product"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(product="CNC"),
                config=_make_config(allowed_product="MIS"),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_order_type_not_allowed(self):
        guard = self._guard()
        # MARKET intent against a config that only allows LIMIT
        with pytest.raises(SafetyError, match="Order type"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(order_type="MARKET"),
                config=_make_config(allowed_order_types=["LIMIT"]),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_raises_when_quantity_exceeds_max(self):
        guard = self._guard()
        with pytest.raises(SafetyError, match="quantity"):
            guard.assert_pilot_order_allowed(
                order_intent=_make_intent(quantity=10),
                config=_make_config(max_order_quantity=5),
                approval_decision=_make_approved_decision(),
                risk_decision=None,
            )

    def test_passes_at_max_quantity(self):
        guard = self._guard()
        guard.assert_pilot_order_allowed(
            order_intent=_make_intent(quantity=5),
            config=_make_config(max_order_quantity=5),
            approval_decision=_make_approved_decision(),
            risk_decision=None,
        )  # must not raise

    def test_symbol_check_is_case_insensitive(self):
        guard = self._guard()
        guard.assert_pilot_order_allowed(
            order_intent=_make_intent(symbol="reliance"),
            config=_make_config(allowed_symbols=["RELIANCE"]),
            approval_decision=_make_approved_decision(),
            risk_decision=None,
        )  # must not raise
