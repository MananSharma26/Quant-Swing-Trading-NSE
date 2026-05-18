"""Tests for ZerodhaBroker.

A FakeKiteClient is injected so no real Zerodha API calls are made.
All tests run without credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_engine.broker.zerodha.client import ZerodhaBroker
from trading_engine.common.exceptions import BrokerConnectionError, LiveTradingDisabledError
from trading_engine.domain.enums import Exchange

# ---------------------------------------------------------------------------
# Fake Kite client — simulates the pykiteconnect SDK surface we use
# ---------------------------------------------------------------------------


class FakeKiteClient:
    """Minimal fake KiteConnect client for unit testing."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._positions = {"net": [{"tradingsymbol": "RELIANCE", "quantity": 10}], "day": []}
        self._orders = [{"order_id": "ord_abc", "tradingsymbol": "INFY"}]
        self._trades = [{"trade_id": "trd_abc", "tradingsymbol": "INFY"}]
        self._margins = {"equity": {"available": {"cash": 50000.0}}, "commodity": {}}
        self._instruments = [
            {"tradingsymbol": "RELIANCE", "exchange": "NSE", "instrument_token": 738561},
        ]
        self._historical = [
            {
                "date": datetime(2024, 1, 15, 9, 15),
                "open": 2800,
                "high": 2820,
                "low": 2795,
                "close": 2810,
                "volume": 100000,
            },
        ]

    def set_access_token(self, token: str) -> None:
        self._access_token = token

    def positions(self) -> dict:
        return self._positions

    def orders(self) -> list:
        return self._orders

    def trades(self) -> list:
        return self._trades

    def margins(self) -> dict:
        return self._margins

    def instruments(self, exchange: str) -> list:
        return [i for i in self._instruments if i["exchange"] == exchange]

    def historical_data(
        self, instrument_token: int, from_date: datetime, to_date: datetime, interval: str
    ) -> list:
        return self._historical


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_kite() -> FakeKiteClient:
    return FakeKiteClient()


@pytest.fixture
def broker(fake_kite: FakeKiteClient) -> ZerodhaBroker:
    return ZerodhaBroker(kite_client=fake_kite)


@pytest.fixture
def connected_broker(broker: ZerodhaBroker) -> ZerodhaBroker:
    broker.connect()
    return broker


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestZerodhaBrokerConstruction:
    def test_instantiates_with_fake_client(self, fake_kite: FakeKiteClient) -> None:
        broker = ZerodhaBroker(kite_client=fake_kite)
        assert not broker.is_connected

    def test_none_client_raises(self) -> None:
        with pytest.raises(BrokerConnectionError, match="kite_client cannot be None"):
            ZerodhaBroker(kite_client=None)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestZerodhaBrokerConnection:
    def test_connect_marks_connected(self, broker: ZerodhaBroker) -> None:
        broker.connect()
        assert broker.is_connected is True

    def test_disconnect_marks_disconnected(self, connected_broker: ZerodhaBroker) -> None:
        connected_broker.disconnect()
        assert connected_broker.is_connected is False

    def test_connect_disconnect_cycle(self, broker: ZerodhaBroker) -> None:
        assert not broker.is_connected
        broker.connect()
        assert broker.is_connected
        broker.disconnect()
        assert not broker.is_connected
        broker.connect()
        assert broker.is_connected


# ---------------------------------------------------------------------------
# Data delegation — each method must delegate to the fake client
# ---------------------------------------------------------------------------


class TestZerodhaBrokerDelegation:
    def test_get_positions_delegates(
        self, connected_broker: ZerodhaBroker, fake_kite: FakeKiteClient
    ) -> None:
        result = connected_broker.get_positions()
        assert result == fake_kite.positions()

    def test_get_positions_returns_fake_data(self, connected_broker: ZerodhaBroker) -> None:
        result = connected_broker.get_positions()
        assert "net" in result
        assert result["net"][0]["tradingsymbol"] == "RELIANCE"

    def test_get_orders_delegates(
        self, connected_broker: ZerodhaBroker, fake_kite: FakeKiteClient
    ) -> None:
        result = connected_broker.get_orders()
        assert result == fake_kite.orders()

    def test_get_orders_returns_fake_data(self, connected_broker: ZerodhaBroker) -> None:
        result = connected_broker.get_orders()
        assert len(result) == 1
        assert result[0]["order_id"] == "ord_abc"

    def test_get_trades_delegates(
        self, connected_broker: ZerodhaBroker, fake_kite: FakeKiteClient
    ) -> None:
        result = connected_broker.get_trades()
        assert result == fake_kite.trades()

    def test_get_trades_returns_fake_data(self, connected_broker: ZerodhaBroker) -> None:
        result = connected_broker.get_trades()
        assert result[0]["trade_id"] == "trd_abc"

    def test_get_margins_delegates(
        self, connected_broker: ZerodhaBroker, fake_kite: FakeKiteClient
    ) -> None:
        result = connected_broker.get_margins()
        assert result == fake_kite.margins()

    def test_get_margins_returns_equity_key(self, connected_broker: ZerodhaBroker) -> None:
        result = connected_broker.get_margins()
        assert "equity" in result

    def test_get_instruments_delegates_with_exchange(
        self, broker: ZerodhaBroker, fake_kite: FakeKiteClient
    ) -> None:
        result = broker.get_instruments(exchange=Exchange.NSE)
        assert result == fake_kite.instruments("NSE")

    def test_get_instruments_filters_by_exchange(self, broker: ZerodhaBroker) -> None:
        nse_instruments = broker.get_instruments(exchange=Exchange.NSE)
        assert all(i["exchange"] == "NSE" for i in nse_instruments)

    def test_get_instruments_accepts_string_exchange(self, broker: ZerodhaBroker) -> None:
        result = broker.get_instruments(exchange="NSE")
        assert len(result) == 1

    def test_get_historical_data_delegates(self, broker: ZerodhaBroker) -> None:
        from_dt = datetime(2024, 1, 15, tzinfo=UTC)
        to_dt = datetime(2024, 1, 15, 15, 30, tzinfo=UTC)
        result = broker.get_historical_data(
            instrument_token=738561,
            from_date=from_dt,
            to_date=to_dt,
            interval="minute",
        )
        assert len(result) == 1
        assert result[0]["close"] == 2810


# ---------------------------------------------------------------------------
# Connection guard — methods requiring connection must fail if disconnected
# ---------------------------------------------------------------------------


class TestConnectionGuard:
    def test_get_positions_fails_if_disconnected(self, broker: ZerodhaBroker) -> None:
        with pytest.raises(BrokerConnectionError, match="not connected"):
            broker.get_positions()

    def test_get_orders_fails_if_disconnected(self, broker: ZerodhaBroker) -> None:
        with pytest.raises(BrokerConnectionError):
            broker.get_orders()

    def test_get_trades_fails_if_disconnected(self, broker: ZerodhaBroker) -> None:
        with pytest.raises(BrokerConnectionError):
            broker.get_trades()

    def test_get_margins_fails_if_disconnected(self, broker: ZerodhaBroker) -> None:
        with pytest.raises(BrokerConnectionError):
            broker.get_margins()

    def test_get_instruments_does_not_require_connection(self, broker: ZerodhaBroker) -> None:
        # Instrument list download doesn't require a trading session.
        result = broker.get_instruments()
        assert isinstance(result, list)

    def test_get_historical_data_does_not_require_connection(self, broker: ZerodhaBroker) -> None:
        result = broker.get_historical_data(
            instrument_token=738561,
            from_date=datetime(2024, 1, 15, tzinfo=UTC),
            to_date=datetime(2024, 1, 15, 15, 30, tzinfo=UTC),
            interval="minute",
        )
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Streaming — not yet implemented
# ---------------------------------------------------------------------------


class TestStreamTicks:
    def test_stream_ticks_raises_not_implemented(self, connected_broker: ZerodhaBroker) -> None:
        with pytest.raises(NotImplementedError, match="Milestone 3"):
            connected_broker.stream_ticks(["NSE:RELIANCE"], callback=lambda t: None)


# ---------------------------------------------------------------------------
# Safety gate — order placement gated through safety guard
# ---------------------------------------------------------------------------


class TestOrderPlacementBlocked:
    def test_place_order_blocked_when_pilot_disabled(self, connected_broker: ZerodhaBroker) -> None:
        from trading_engine.common.exceptions import SafetyError
        from trading_engine.live_execution.models import ApprovalDecision, ApprovalStatus
        from trading_engine.live_execution.pilot_config import LivePilotConfig
        from trading_engine.live_execution.safety import LiveExecutionSafetyGuard
        from trading_engine.strategy.signals import OrderIntent
        from datetime import UTC, datetime

        config = LivePilotConfig()  # all flags False by default
        approval = ApprovalDecision(
            approval_id="x",
            status=ApprovalStatus.APPROVED,
            decided_at=datetime.now(tz=UTC),
        )
        intent = OrderIntent(
            strategy_id="t",
            symbol="RELIANCE",
            exchange="NSE",
            side="BUY",
            quantity=1,
            order_type="MARKET",
            product="MIS",
        )
        guard = LiveExecutionSafetyGuard(object())

        with pytest.raises(SafetyError, match="LIVE_ORDER_EXECUTION_ENABLED"):
            connected_broker.place_order(
                order_intent=intent,
                pilot_config=config,
                approval_decision=approval,
                safety_guard=guard,
            )

    def test_modify_order_raises(self, connected_broker: ZerodhaBroker) -> None:
        with pytest.raises(LiveTradingDisabledError):
            connected_broker.modify_order(order_id="ord_abc", price=2850)

    def test_cancel_order_raises(self, connected_broker: ZerodhaBroker) -> None:
        with pytest.raises(LiveTradingDisabledError):
            connected_broker.cancel_order(order_id="ord_abc")

    def test_place_order_requires_connection(self, broker: ZerodhaBroker) -> None:
        from trading_engine.common.exceptions import BrokerConnectionError
        from trading_engine.live_execution.models import ApprovalDecision, ApprovalStatus
        from trading_engine.live_execution.pilot_config import LivePilotConfig
        from trading_engine.live_execution.safety import LiveExecutionSafetyGuard
        from trading_engine.strategy.signals import OrderIntent
        from datetime import UTC, datetime

        config = LivePilotConfig(
            live_order_execution_enabled=True,
            live_order_pilot_enabled=True,
            allowed_symbols=["RELIANCE"],
        )
        approval = ApprovalDecision(
            approval_id="x",
            status=ApprovalStatus.APPROVED,
            decided_at=datetime.now(tz=UTC),
        )
        intent = OrderIntent(
            strategy_id="t",
            symbol="RELIANCE",
            exchange="NSE",
            side="BUY",
            quantity=1,
            order_type="MARKET",
            product="MIS",
        )
        guard = LiveExecutionSafetyGuard(object())

        with pytest.raises(BrokerConnectionError):
            broker.place_order(
                order_intent=intent,
                pilot_config=config,
                approval_decision=approval,
                safety_guard=guard,
            )
