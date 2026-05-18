"""Zerodha Kite Connect broker adapter — read-only.

Wraps the KiteConnect SDK behind the abstract Broker interface.
All order placement methods remain blocked.

Design decisions:
  - The KiteConnect client is injected via __init__; never created here.
  - This class does NOT store credentials directly. The caller is responsible
    for initialising the Kite client with a valid access_token before calling
    connect().
  - Live order placement raises LiveTradingDisabledError (inherited from Broker).
  - WebSocket streaming raises NotImplementedError; it will be implemented
    in a later milestone when live market data is required.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from trading_engine.broker.base import Broker
from trading_engine.common.exceptions import BrokerConnectionError
from trading_engine.domain.enums import Exchange


class ZerodhaBroker(Broker):
    """Zerodha Kite Connect broker adapter (read-only).

    Accepts a KiteConnect-compatible client via dependency injection so that
    tests can substitute a fake client without making real API calls.

    Args:
        kite_client: A KiteConnect instance (or compatible fake) with
                     positions(), orders(), trades(), margins(),
                     instruments(), historical_data(), and set_access_token().
        settings:    Optional Settings object (not used directly here;
                     use KiteAuthManager for credential management).
        logger:      Optional logger; defaults to module logger.

    Example:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=settings.zerodha_api_key.get_secret_value())
        kite.set_access_token(settings.zerodha_access_token.get_secret_value())
        broker = ZerodhaBroker(kite_client=kite, settings=settings)
        broker.connect()
    """

    def __init__(
        self,
        kite_client: Any,
        settings: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if kite_client is None:
            raise BrokerConnectionError("kite_client cannot be None. Pass a KiteConnect instance.")
        self._kite = kite_client
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._connected: bool = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Mark the broker as connected.

        In practice this means the Kite client has been configured with an
        access_token (via KiteAuthManager.generate_session or manually via
        kite.set_access_token). Call this before any data-fetching methods.
        """
        self._connected = True
        self._logger.info("ZerodhaBroker: connected.")

    def disconnect(self) -> None:
        """Mark the broker as disconnected."""
        self._connected = False
        self._logger.info("ZerodhaBroker: disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Internal guard
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self._connected:
            raise BrokerConnectionError(
                "ZerodhaBroker is not connected. Call connect() before fetching data."
            )

    # ------------------------------------------------------------------
    # Read-only account data
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict[str, Any]]:
        """Return raw positions dict from Zerodha.

        Zerodha returns {"net": [...], "day": [...]}.  The raw dict is passed
        through here; normalisation to internal Position models is done by
        mappers (future milestone).
        """
        self._require_connected()
        return self._kite.positions()  # type: ignore[no-any-return]

    def get_orders(self) -> list[dict[str, Any]]:
        """Return today's orders from Zerodha."""
        self._require_connected()
        return self._kite.orders()  # type: ignore[no-any-return]

    def get_trades(self) -> list[dict[str, Any]]:
        """Return today's executed trades from Zerodha."""
        self._require_connected()
        return self._kite.trades()  # type: ignore[no-any-return]

    def get_margins(self) -> dict[str, Any]:
        """Return margin and fund information from Zerodha."""
        self._require_connected()
        return self._kite.margins()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Instrument and historical data (no connection guard — no session required)
    # ------------------------------------------------------------------

    def get_instruments(self, exchange: Exchange | str = Exchange.NSE) -> list[dict[str, Any]]:
        """Return the full instrument list for an exchange.

        This downloads a large CSV-backed list from Zerodha. Cache the result;
        do not call it on every bar.

        Args:
            exchange: Exchange enum or string, e.g. Exchange.NSE or "NSE".
        """
        return self._kite.instruments(str(exchange))  # type: ignore[no-any-return]

    def get_historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Return historical OHLCV candles for an instrument.

        Args:
            instrument_token: Zerodha integer token for the instrument.
            from_date:        Start of the date range (datetime).
            to_date:          End of the date range (datetime).
            interval:         Candle interval, e.g. "minute", "5minute", "day".

        Returns:
            List of dicts with keys: date, open, high, low, close, volume.
        """
        return self._kite.historical_data(  # type: ignore[no-any-return]
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
        )

    # ------------------------------------------------------------------
    # Streaming — not yet implemented
    # ------------------------------------------------------------------

    def stream_ticks(self, symbols: list[str], callback: Any) -> None:
        """Live WebSocket tick streaming — not implemented in Milestone 3.

        Will be implemented in a later milestone when live market data is
        required (Milestone 8: Paper trading engine).

        Raises:
            NotImplementedError: always, until streaming is implemented.
        """
        raise NotImplementedError(
            "ZerodhaBroker.stream_ticks is not implemented in Milestone 3. "
            "WebSocket streaming will be added in a later milestone."
        )

    # ------------------------------------------------------------------
    # Order placement — gated through LiveExecutionSafetyGuard
    # ------------------------------------------------------------------

    def place_order(  # type: ignore[override]
        self,
        order_intent: Any,
        pilot_config: Any,
        approval_decision: Any,
        risk_decision: Any | None = None,
        safety_guard: Any | None = None,
    ) -> str:
        """Place a real order via Zerodha Kite Connect.

        This method requires ALL of the following to be in place:
          - A LiveExecutionSafetyGuard that passes assert_pilot_order_allowed().
          - An ApprovalDecision with status APPROVED.
          - A LivePilotConfig with live_order_execution_enabled=True and
            live_order_pilot_enabled=True.

        Args:
            order_intent:       The OrderIntent describing the order.
            pilot_config:       LivePilotConfig with execution constraints.
            approval_decision:  ApprovalDecision from LiveOrderApprovalGate.
            risk_decision:      Optional risk engine decision.
            safety_guard:       LiveExecutionSafetyGuard instance.  If None,
                                a default guard (no kill switch, using self._settings)
                                is used.

        Returns:
            The Zerodha broker_order_id string.

        Raises:
            SafetyError: if any safety check fails.
            BrokerConnectionError: if not connected.
        """
        from trading_engine.live_execution.safety import LiveExecutionSafetyGuard

        self._require_connected()

        guard = safety_guard or LiveExecutionSafetyGuard(
            settings=self._settings,
            logger=self._logger,
        )
        guard.assert_pilot_order_allowed(
            order_intent=order_intent,
            config=pilot_config,
            approval_decision=approval_decision,
            risk_decision=risk_decision,
        )

        # Map OrderIntent fields to Zerodha Kite params.
        variety = "regular"
        transaction_type = str(order_intent.side).upper()
        order_type = str(order_intent.order_type).upper()
        # Zerodha uses "SL-M" not "SL_M"
        if order_type == "SL_M":
            order_type = "SL-M"

        kite_params: dict[str, Any] = {
            "variety": variety,
            "exchange": str(order_intent.exchange).upper(),
            "tradingsymbol": str(order_intent.symbol).upper(),
            "transaction_type": transaction_type,
            "quantity": int(order_intent.quantity),
            "order_type": order_type,
            "product": str(order_intent.product).upper(),
        }

        price = getattr(order_intent, "price", None)
        if price is not None:
            kite_params["price"] = float(price)

        trigger_price = getattr(order_intent, "trigger_price", None)
        if trigger_price is not None:
            kite_params["trigger_price"] = float(trigger_price)

        # Tag with strategy_id for reconciliation (max 20 chars in Kite).
        strategy_id = str(getattr(order_intent, "strategy_id", "pilot"))
        kite_params["tag"] = strategy_id[:20]

        self._logger.info(
            "ZerodhaBroker.place_order: placing %s %s %s qty=%s",
            transaction_type,
            order_intent.quantity,
            order_intent.symbol,
            order_intent.quantity,
        )

        response = self._kite.place_order(**kite_params)

        # Kite returns {"order_id": "..."} or just the order_id string.
        if isinstance(response, dict):
            broker_order_id = str(response.get("order_id", response))
        else:
            broker_order_id = str(response)

        self._logger.info(
            "ZerodhaBroker.place_order: order placed — broker_order_id=%r", broker_order_id
        )
        return broker_order_id

    # modify_order, cancel_order still raise LiveTradingDisabledError (inherited from Broker).
