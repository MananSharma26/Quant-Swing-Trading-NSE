"""Tests for OrderStateMachine.

Covers valid transitions, invalid transitions, terminal states, and helpers.
"""

from __future__ import annotations

import pytest

from trading_engine.common.exceptions import OrderStateTransitionError
from trading_engine.domain.enums import OrderStatus
from trading_engine.execution.state_machine import OrderStateMachine


@pytest.fixture()
def sm() -> OrderStateMachine:
    return OrderStateMachine()


# ---------------------------------------------------------------------------
# Tests: valid transitions
# ---------------------------------------------------------------------------


class TestValidTransitions:
    def test_created_to_risk_approved(self, sm):
        assert (
            sm.transition(OrderStatus.CREATED, OrderStatus.RISK_APPROVED)
            == OrderStatus.RISK_APPROVED
        )

    def test_created_to_risk_rejected(self, sm):
        assert (
            sm.transition(OrderStatus.CREATED, OrderStatus.RISK_REJECTED)
            == OrderStatus.RISK_REJECTED
        )

    def test_risk_approved_to_submitted(self, sm):
        assert (
            sm.transition(OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTED) == OrderStatus.SUBMITTED
        )

    def test_risk_rejected_to_failed(self, sm):
        assert sm.transition(OrderStatus.RISK_REJECTED, OrderStatus.FAILED) == OrderStatus.FAILED

    def test_submitted_to_open(self, sm):
        assert sm.transition(OrderStatus.SUBMITTED, OrderStatus.OPEN) == OrderStatus.OPEN

    def test_submitted_to_filled(self, sm):
        assert sm.transition(OrderStatus.SUBMITTED, OrderStatus.FILLED) == OrderStatus.FILLED

    def test_submitted_to_rejected(self, sm):
        assert sm.transition(OrderStatus.SUBMITTED, OrderStatus.REJECTED) == OrderStatus.REJECTED

    def test_submitted_to_failed(self, sm):
        assert sm.transition(OrderStatus.SUBMITTED, OrderStatus.FAILED) == OrderStatus.FAILED

    def test_submitted_to_unknown(self, sm):
        assert sm.transition(OrderStatus.SUBMITTED, OrderStatus.UNKNOWN) == OrderStatus.UNKNOWN

    def test_open_to_partially_filled(self, sm):
        assert (
            sm.transition(OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)
            == OrderStatus.PARTIALLY_FILLED
        )

    def test_open_to_filled(self, sm):
        assert sm.transition(OrderStatus.OPEN, OrderStatus.FILLED) == OrderStatus.FILLED

    def test_open_to_cancel_requested(self, sm):
        assert (
            sm.transition(OrderStatus.OPEN, OrderStatus.CANCEL_REQUESTED)
            == OrderStatus.CANCEL_REQUESTED
        )

    def test_open_to_cancelled(self, sm):
        assert sm.transition(OrderStatus.OPEN, OrderStatus.CANCELLED) == OrderStatus.CANCELLED

    def test_open_to_rejected(self, sm):
        assert sm.transition(OrderStatus.OPEN, OrderStatus.REJECTED) == OrderStatus.REJECTED

    def test_open_to_unknown(self, sm):
        assert sm.transition(OrderStatus.OPEN, OrderStatus.UNKNOWN) == OrderStatus.UNKNOWN

    def test_partially_filled_to_filled(self, sm):
        assert sm.transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED) == OrderStatus.FILLED

    def test_partially_filled_to_cancel_requested(self, sm):
        assert (
            sm.transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCEL_REQUESTED)
            == OrderStatus.CANCEL_REQUESTED
        )

    def test_partially_filled_to_cancelled(self, sm):
        assert (
            sm.transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELLED)
            == OrderStatus.CANCELLED
        )

    def test_partially_filled_to_unknown(self, sm):
        assert (
            sm.transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN) == OrderStatus.UNKNOWN
        )

    def test_cancel_requested_to_cancelled(self, sm):
        assert (
            sm.transition(OrderStatus.CANCEL_REQUESTED, OrderStatus.CANCELLED)
            == OrderStatus.CANCELLED
        )

    def test_cancel_requested_to_filled(self, sm):
        assert sm.transition(OrderStatus.CANCEL_REQUESTED, OrderStatus.FILLED) == OrderStatus.FILLED

    def test_cancel_requested_to_unknown(self, sm):
        assert (
            sm.transition(OrderStatus.CANCEL_REQUESTED, OrderStatus.UNKNOWN) == OrderStatus.UNKNOWN
        )

    def test_unknown_to_reconciled(self, sm):
        assert sm.transition(OrderStatus.UNKNOWN, OrderStatus.RECONCILED) == OrderStatus.RECONCILED

    def test_unknown_to_failed(self, sm):
        assert sm.transition(OrderStatus.UNKNOWN, OrderStatus.FAILED) == OrderStatus.FAILED

    def test_reconciled_to_open(self, sm):
        assert sm.transition(OrderStatus.RECONCILED, OrderStatus.OPEN) == OrderStatus.OPEN

    def test_reconciled_to_filled(self, sm):
        assert sm.transition(OrderStatus.RECONCILED, OrderStatus.FILLED) == OrderStatus.FILLED

    def test_reconciled_to_cancelled(self, sm):
        assert sm.transition(OrderStatus.RECONCILED, OrderStatus.CANCELLED) == OrderStatus.CANCELLED

    def test_reconciled_to_rejected(self, sm):
        assert sm.transition(OrderStatus.RECONCILED, OrderStatus.REJECTED) == OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# Tests: invalid transitions
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    def test_created_to_open_raises(self, sm):
        with pytest.raises(OrderStateTransitionError):
            sm.transition(OrderStatus.CREATED, OrderStatus.OPEN)

    def test_created_to_filled_raises(self, sm):
        with pytest.raises(OrderStateTransitionError):
            sm.transition(OrderStatus.CREATED, OrderStatus.FILLED)

    def test_risk_approved_to_open_raises(self, sm):
        with pytest.raises(OrderStateTransitionError):
            sm.transition(OrderStatus.RISK_APPROVED, OrderStatus.OPEN)

    def test_filled_to_anything_raises(self, sm):
        for status in OrderStatus:
            if status != OrderStatus.FILLED:
                with pytest.raises(OrderStateTransitionError):
                    sm.transition(OrderStatus.FILLED, status)

    def test_cancelled_to_anything_raises(self, sm):
        with pytest.raises(OrderStateTransitionError):
            sm.transition(OrderStatus.CANCELLED, OrderStatus.OPEN)

    def test_failed_to_anything_raises(self, sm):
        with pytest.raises(OrderStateTransitionError):
            sm.transition(OrderStatus.FAILED, OrderStatus.SUBMITTED)

    def test_rejected_to_anything_raises(self, sm):
        with pytest.raises(OrderStateTransitionError):
            sm.transition(OrderStatus.REJECTED, OrderStatus.OPEN)

    def test_error_message_mentions_statuses(self, sm):
        with pytest.raises(OrderStateTransitionError, match="CREATED"):
            sm.transition(OrderStatus.CREATED, OrderStatus.FILLED)


# ---------------------------------------------------------------------------
# Tests: helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_terminal_states(self, sm):
        terminal = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
            OrderStatus.REJECTED,
        }
        for status in terminal:
            assert sm.is_terminal(status) is True

    def test_non_terminal_states(self, sm):
        non_terminal = {
            OrderStatus.CREATED,
            OrderStatus.RISK_APPROVED,
            OrderStatus.SUBMITTED,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        }
        for status in non_terminal:
            assert sm.is_terminal(status) is False

    def test_allowed_transitions_created(self, sm):
        allowed = sm.allowed_transitions(OrderStatus.CREATED)
        assert OrderStatus.RISK_APPROVED in allowed
        assert OrderStatus.RISK_REJECTED in allowed
        assert OrderStatus.OPEN not in allowed

    def test_allowed_transitions_terminal_empty(self, sm):
        assert sm.allowed_transitions(OrderStatus.FILLED) == frozenset()
