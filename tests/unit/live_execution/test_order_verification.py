"""Tests for live_execution.order_verification — OrderVerificationService."""

from __future__ import annotations

from typing import Any

from trading_engine.live_execution.order_verification import OrderVerificationService, VerificationResult


class _FakeBroker:
    """Fake ZerodhaBroker that returns a fixed orders list."""

    def __init__(self, orders: list[dict[str, Any]]) -> None:
        self._orders = orders
        self.call_count = 0

    def get_orders(self) -> list[dict[str, Any]]:
        self.call_count += 1
        return list(self._orders)


class _FailingBroker:
    """Fake broker whose get_orders() always raises."""

    def get_orders(self) -> list[dict[str, Any]]:
        raise ConnectionError("broker unreachable")


class _EventuallySucceedingBroker:
    """Returns empty list for the first N calls, then returns the order."""

    def __init__(self, succeed_on: int, order_id: str) -> None:
        self._succeed_on = succeed_on
        self._order_id = order_id
        self.call_count = 0

    def get_orders(self) -> list[dict[str, Any]]:
        self.call_count += 1
        if self.call_count >= self._succeed_on:
            return [{"order_id": self._order_id, "status": "OPEN"}]
        return []


class TestVerificationResultToDict:
    def test_to_dict_contains_required_keys(self):
        result = VerificationResult(
            broker_order_id="123",
            found=True,
            raw_order={"order_id": "123"},
            attempts=1,
        )
        d = result.to_dict()
        for key in ("broker_order_id", "found", "attempts", "error", "raw_order"):
            assert key in d

    def test_found_result(self):
        result = VerificationResult(
            broker_order_id="123",
            found=True,
            raw_order={"order_id": "123"},
            attempts=1,
        )
        assert result.found is True
        assert result.error is None


class TestOrderVerificationFound:
    def test_finds_order_in_first_attempt(self):
        broker = _FakeBroker(orders=[{"order_id": "ZRD123", "status": "OPEN"}])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("ZRD123", retries=3, delay_seconds=0)
        assert result.found is True
        assert result.broker_order_id == "ZRD123"

    def test_raw_order_populated_when_found(self):
        broker = _FakeBroker(orders=[{"order_id": "ZRD123", "status": "COMPLETE"}])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("ZRD123", retries=1, delay_seconds=0)
        assert result.raw_order is not None
        assert result.raw_order["order_id"] == "ZRD123"

    def test_attempts_is_one_when_found_immediately(self):
        broker = _FakeBroker(orders=[{"order_id": "ZRD123"}])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("ZRD123", retries=5, delay_seconds=0)
        assert result.attempts == 1


class TestOrderVerificationNotFound:
    def test_not_found_after_retries(self):
        broker = _FakeBroker(orders=[{"order_id": "OTHER"}])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("ZRD999", retries=2, delay_seconds=0)
        assert result.found is False

    def test_attempts_equals_retries_when_not_found(self):
        broker = _FakeBroker(orders=[])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("MISSING", retries=3, delay_seconds=0)
        assert result.attempts == 3

    def test_no_error_when_not_found_cleanly(self):
        broker = _FakeBroker(orders=[])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("MISSING", retries=1, delay_seconds=0)
        assert result.error is None


class TestOrderVerificationRetry:
    def test_eventually_finds_order(self):
        broker = _EventuallySucceedingBroker(succeed_on=3, order_id="ZRD555")
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("ZRD555", retries=5, delay_seconds=0)
        assert result.found is True
        assert result.attempts == 3

    def test_broker_called_multiple_times(self):
        broker = _FakeBroker(orders=[])
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        svc.verify("X", retries=3, delay_seconds=0)
        assert broker.call_count == 3


class TestOrderVerificationBrokerError:
    def test_error_captured_when_broker_fails(self):
        broker = _FailingBroker()
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("X", retries=2, delay_seconds=0)
        assert result.found is False
        assert result.error is not None
        assert "unreachable" in result.error

    def test_found_is_false_when_broker_always_fails(self):
        broker = _FailingBroker()
        svc = OrderVerificationService(broker=broker)  # type: ignore[arg-type]
        result = svc.verify("X", retries=1, delay_seconds=0)
        assert result.found is False
