"""Live order execution pilot executor.

LiveOrderPilotExecutor orchestrates the full live order pipeline:
  1. Risk check (optional)
  2. Approval gate
  3. Safety guard
  4. Broker placement
  5. Audit logging

No orders are placed unless LIVE_ORDER_EXECUTION_ENABLED=true,
LIVE_ORDER_PILOT_ENABLED=true, and all constraints in LivePilotConfig pass.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from trading_engine.common.exceptions import SafetyError
from trading_engine.live_execution.audit import ApprovalAuditLogger
from trading_engine.live_execution.models import ApprovalStatus

if TYPE_CHECKING:
    from trading_engine.broker.zerodha.client import ZerodhaBroker
    from trading_engine.live_execution.approvals import LiveOrderApprovalGate
    from trading_engine.live_execution.pilot_config import LivePilotConfig
    from trading_engine.live_execution.safety import LiveExecutionSafetyGuard
    from trading_engine.strategy.signals import OrderIntent


class PilotOrderResult:
    """Result of a LiveOrderPilotExecutor.execute() call.

    Attributes:
        success:         True if the order was placed successfully.
        broker_order_id: Zerodha order ID, or None if placement failed.
        approval_status: The approval status at the time of execution.
        error:           Exception message if placement failed; None otherwise.
    """

    def __init__(
        self,
        success: bool,
        broker_order_id: str | None,
        approval_status: ApprovalStatus | None,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.broker_order_id = broker_order_id
        self.approval_status = approval_status
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "broker_order_id": self.broker_order_id,
            "approval_status": str(self.approval_status) if self.approval_status else None,
            "error": self.error,
        }


class LiveOrderPilotExecutor:
    """Orchestrates the live order pilot pipeline.

    Args:
        broker:        ZerodhaBroker instance (connected).
        pilot_config:  LivePilotConfig with execution constraints.
        approval_gate: LiveOrderApprovalGate in AUTO_PAPER or MANUAL_APPROVE mode.
        safety_guard:  LiveExecutionSafetyGuard instance.
        audit_logger:  Optional ApprovalAuditLogger for JSONL audit trails.
        risk_engine:   Optional risk engine; skipped if None.
        logger:        Optional logger override.
    """

    def __init__(
        self,
        broker: ZerodhaBroker,
        pilot_config: LivePilotConfig,
        approval_gate: LiveOrderApprovalGate,
        safety_guard: LiveExecutionSafetyGuard,
        audit_logger: ApprovalAuditLogger | None = None,
        risk_engine: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._broker = broker
        self._config = pilot_config
        self._gate = approval_gate
        self._safety = safety_guard
        self._audit = audit_logger
        self._risk = risk_engine
        self._log = logger or logging.getLogger(__name__)

    def execute(
        self,
        order_intent: OrderIntent,
        portfolio_snapshot: Any | None = None,
        estimated_price: Any | None = None,
    ) -> PilotOrderResult:
        """Execute a live pilot order through the full safety pipeline.

        Args:
            order_intent:       The order to place.
            portfolio_snapshot: Optional snapshot for risk engine check.
            estimated_price:    Optional indicative price for audit purposes.

        Returns:
            PilotOrderResult indicating success or failure.
        """
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC)

        # 1. Risk check
        risk_decision = None
        if self._risk is not None and portfolio_snapshot is not None:
            risk_decision = self._risk.check_order_intent(order_intent, portfolio_snapshot, ts)
            if not risk_decision.approved:
                self._log.warning(
                    "PilotExecutor: risk engine blocked order — %s", risk_decision.reason_code
                )
                return PilotOrderResult(
                    success=False,
                    broker_order_id=None,
                    approval_status=ApprovalStatus.AUTO_REJECTED,
                    error=f"Risk blocked: {risk_decision.reason_code} — {risk_decision.reason_message}",
                )

        # 2. Approval gate
        try:
            approval_decision = self._gate.require_approval(
                order_intent, estimated_price=estimated_price
            )
        except SafetyError as exc:
            self._log.error("PilotExecutor: approval gate SafetyError — %s", exc)
            return PilotOrderResult(
                success=False,
                broker_order_id=None,
                approval_status=ApprovalStatus.AUTO_REJECTED,
                error=str(exc),
            )
        except Exception as exc:
            # ManualApprovalRequired — approval is PENDING, not an error
            approval_id = getattr(exc, "approval_id", None)
            if approval_id is not None:
                self._log.info(
                    "PilotExecutor: manual approval required — approval_id=%r", approval_id
                )
                return PilotOrderResult(
                    success=False,
                    broker_order_id=None,
                    approval_status=ApprovalStatus.PENDING,
                    error=f"Manual approval required: approval_id={approval_id}",
                )
            raise

        if self._audit is not None:
            req = self._gate._requests.get(approval_decision.approval_id)
            if req is not None:
                self._audit.log_request(req)
            self._audit.log_decision(approval_decision)

        # 3. Safety guard + broker placement
        try:
            broker_order_id = self._broker.place_order(
                order_intent=order_intent,
                pilot_config=self._config,
                approval_decision=approval_decision,
                risk_decision=risk_decision,
                safety_guard=self._safety,
            )
        except SafetyError as exc:
            self._log.error("PilotExecutor: SafetyError during placement — %s", exc)
            return PilotOrderResult(
                success=False,
                broker_order_id=None,
                approval_status=approval_decision.status,
                error=str(exc),
            )
        except Exception as exc:
            self._log.error("PilotExecutor: unexpected error during placement — %s", exc)
            return PilotOrderResult(
                success=False,
                broker_order_id=None,
                approval_status=approval_decision.status,
                error=str(exc),
            )

        self._log.info(
            "PilotExecutor: order placed — broker_order_id=%r", broker_order_id
        )
        return PilotOrderResult(
            success=True,
            broker_order_id=broker_order_id,
            approval_status=ApprovalStatus.APPROVED,
        )
