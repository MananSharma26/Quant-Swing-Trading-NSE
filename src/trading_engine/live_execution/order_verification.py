"""Post-placement order verification service.

OrderVerificationService polls the broker for a placed order and verifies
that the expected order_id appears in the broker's order list.  This is a
lightweight sanity check, not a full reconciliation.

Usage::

    svc = OrderVerificationService(broker=zerodha_broker)
    result = svc.verify(broker_order_id="123456789", retries=3, delay_seconds=2)
    if not result.found:
        raise RuntimeError(f"Order {broker_order_id} not found after placement!")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_engine.broker.zerodha.client import ZerodhaBroker


@dataclass
class VerificationResult:
    """Result of an order verification attempt.

    Attributes:
        broker_order_id: The order ID that was checked.
        found:           True if the order was found in the broker's order list.
        raw_order:       The raw order dict from the broker, if found; None otherwise.
        attempts:        Number of polling attempts made.
        error:           Exception message if a broker call failed; None otherwise.
    """

    broker_order_id: str
    found: bool
    raw_order: dict[str, Any] | None
    attempts: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker_order_id": self.broker_order_id,
            "found": self.found,
            "attempts": self.attempts,
            "error": self.error,
            "raw_order": self.raw_order,
        }


class OrderVerificationService:
    """Polls ZerodhaBroker.get_orders() to confirm a placed order exists.

    Args:
        broker: A connected ZerodhaBroker instance.
        logger: Optional logger override.
    """

    def __init__(
        self,
        broker: ZerodhaBroker,
        logger: logging.Logger | None = None,
    ) -> None:
        self._broker = broker
        self._log = logger or logging.getLogger(__name__)

    def verify(
        self,
        broker_order_id: str,
        retries: int = 3,
        delay_seconds: float = 1.0,
    ) -> VerificationResult:
        """Poll the broker until the order appears or retries are exhausted.

        Args:
            broker_order_id: The Zerodha order ID to look for.
            retries:         Maximum number of polling attempts (default: 3).
            delay_seconds:   Seconds to wait between retries (default: 1.0).

        Returns:
            VerificationResult with found=True if the order was located.
        """
        for attempt in range(1, retries + 1):
            try:
                orders = self._broker.get_orders()
                for order in orders:
                    oid = str(order.get("order_id", ""))
                    if oid == str(broker_order_id):
                        self._log.info(
                            "OrderVerification: found order %r on attempt %d",
                            broker_order_id,
                            attempt,
                        )
                        return VerificationResult(
                            broker_order_id=broker_order_id,
                            found=True,
                            raw_order=dict(order),
                            attempts=attempt,
                        )
            except Exception as exc:
                self._log.warning(
                    "OrderVerification: broker call failed on attempt %d — %s", attempt, exc
                )
                if attempt == retries:
                    return VerificationResult(
                        broker_order_id=broker_order_id,
                        found=False,
                        raw_order=None,
                        attempts=attempt,
                        error=str(exc),
                    )

            if attempt < retries:
                time.sleep(delay_seconds)

        self._log.warning(
            "OrderVerification: order %r not found after %d attempts",
            broker_order_id,
            retries,
        )
        return VerificationResult(
            broker_order_id=broker_order_id,
            found=False,
            raw_order=None,
            attempts=retries,
        )
