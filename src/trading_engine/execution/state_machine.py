"""Order lifecycle state machine.

Enforces which OrderStatus transitions are valid.  Every status change in
the OMS must pass through OrderStateMachine.transition() so illegal moves
are caught immediately rather than silently corrupting state.
"""

from __future__ import annotations

from trading_engine.common.exceptions import OrderStateTransitionError
from trading_engine.domain.enums import OrderStatus

# ---------------------------------------------------------------------------
# Allowed transitions table
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.RISK_APPROVED, OrderStatus.RISK_REJECTED}),
    OrderStatus.RISK_APPROVED: frozenset({OrderStatus.SUBMITTED}),
    OrderStatus.RISK_REJECTED: frozenset({OrderStatus.FAILED}),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.CANCELLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.CANCEL_REQUESTED: frozenset(
        {
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.UNKNOWN: frozenset({OrderStatus.RECONCILED, OrderStatus.FAILED}),
    OrderStatus.RECONCILED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    # Terminal states — no further transitions allowed.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.FAILED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class OrderStateMachine:
    """Validate and execute order status transitions.

    Usage::

        sm = OrderStateMachine()
        next_status = sm.transition(OrderStatus.CREATED, OrderStatus.RISK_APPROVED)
    """

    def transition(self, current: OrderStatus, next_status: OrderStatus) -> OrderStatus:
        """Return *next_status* if the transition is valid, else raise.

        Args:
            current:     The order's current status.
            next_status: The status being requested.

        Returns:
            next_status (unchanged) when the transition is valid.

        Raises:
            OrderStateTransitionError: when the transition is not in the
                allowed set for *current*.
        """
        allowed = _TRANSITIONS.get(current, frozenset())
        if next_status not in allowed:
            raise OrderStateTransitionError(
                f"Invalid order status transition: {current!r} → {next_status!r}. "
                f"Allowed from {current!r}: {sorted(str(s) for s in allowed) or 'none (terminal state)'}."
            )
        return next_status

    def allowed_transitions(self, current: OrderStatus) -> frozenset[OrderStatus]:
        """Return the set of valid next statuses from *current*."""
        return _TRANSITIONS.get(current, frozenset())

    def is_terminal(self, status: OrderStatus) -> bool:
        """Return True if *status* has no further allowed transitions."""
        return not bool(_TRANSITIONS.get(status, frozenset()))
