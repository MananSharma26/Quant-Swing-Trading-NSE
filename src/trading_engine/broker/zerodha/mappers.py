"""Zerodha response → internal model mappers.

Pure functions that convert raw Zerodha API dictionaries into the engine's
internal domain models.  These functions:
  - Must not call Zerodha or any external service.
  - Must not require credentials.
  - Are safe to call in unit tests with fake data.
  - Raise BrokerMappingError for unrecognisable critical fields.
  - Map unknown order statuses to OrderStatus.UNKNOWN rather than raising.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from trading_engine.common.exceptions import BrokerMappingError
from trading_engine.domain.enums import (
    Exchange,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
    TimeInForce,
)
from trading_engine.domain.identifiers import generate_fill_id, generate_internal_order_id
from trading_engine.domain.models import Instrument, InternalOrder, Position, TradeFill

# ---------------------------------------------------------------------------
# Zerodha status string → OrderStatus
# ---------------------------------------------------------------------------

_ZERODHA_STATUS_MAP: dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.FILLED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "AMO REQ RECEIVED": OrderStatus.SUBMITTED,
    "PENDING": OrderStatus.SUBMITTED,
    "VALIDATION PENDING": OrderStatus.SUBMITTED,
    "PUT ORDER REQ RECEIVED": OrderStatus.SUBMITTED,
    "MODIFY PENDING": OrderStatus.OPEN,
    "MODIFY VALIDATION PENDING": OrderStatus.OPEN,
    "AFTER MARKET ORDER REQ RECEIVED": OrderStatus.SUBMITTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}

# Zerodha validity → TimeInForce
_ZERODHA_VALIDITY_MAP: dict[str, TimeInForce] = {
    "DAY": TimeInForce.DAY,
    "IOC": TimeInForce.IOC,
    "TTL": TimeInForce.DAY,  # Treat TTL as DAY for our purposes
}

_ZERODHA_TIMESTAMP_FORMATS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]


def _parse_zerodha_timestamp(value: Any, field_name: str) -> datetime:
    """Parse a Zerodha timestamp string into a datetime."""
    if isinstance(value, datetime):
        return value
    if not value:
        raise BrokerMappingError(f"Missing required timestamp field: {field_name!r}")
    for fmt in _ZERODHA_TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    raise BrokerMappingError(
        f"Cannot parse timestamp {value!r} for field {field_name!r}. "
        f"Expected formats: {_ZERODHA_TIMESTAMP_FORMATS}"
    )


def _require(raw: dict[str, Any], key: str) -> Any:
    """Get a required field from a raw dict, raising BrokerMappingError if absent."""
    val = raw.get(key)
    if val is None or val == "":
        raise BrokerMappingError(
            f"Required field {key!r} is missing or empty in raw broker response."
        )
    return val


def _decimal_or_none(value: Any, zero_means_none: bool = True) -> Decimal | None:
    """Convert a numeric value to Decimal, treating 0.0 as None when requested."""
    if value is None:
        return None
    d = Decimal(str(value))
    if zero_means_none and d == Decimal("0"):
        return None
    return d


# ---------------------------------------------------------------------------
# Scalar mappers
# ---------------------------------------------------------------------------


def map_zerodha_order_status(status: str) -> OrderStatus:
    """Map a Zerodha status string to an internal OrderStatus.

    Unknown statuses map to OrderStatus.UNKNOWN rather than raising, so the
    engine can handle them via the reconciliation path.
    """
    return _ZERODHA_STATUS_MAP.get(status.strip().upper(), OrderStatus.UNKNOWN)


def map_zerodha_side(transaction_type: str) -> Side:
    """Map a Zerodha transaction_type string to Side."""
    t = transaction_type.strip().upper()
    if t == "BUY":
        return Side.BUY
    if t == "SELL":
        return Side.SELL
    raise BrokerMappingError(
        f"Unknown Zerodha transaction_type {transaction_type!r}. Expected 'BUY' or 'SELL'."
    )


def map_zerodha_order_type(order_type: str) -> OrderType:
    """Map a Zerodha order_type string to OrderType."""
    _map = {
        "MARKET": OrderType.MARKET,
        "LIMIT": OrderType.LIMIT,
        "SL": OrderType.SL,
        "SL-M": OrderType.SL_M,
    }
    result = _map.get(order_type.strip().upper())
    if result is None:
        raise BrokerMappingError(
            f"Unknown Zerodha order_type {order_type!r}. Expected one of {list(_map)}."
        )
    return result


def map_zerodha_product(product: str) -> ProductType:
    """Map a Zerodha product string to ProductType."""
    _map = {
        "MIS": ProductType.MIS,
        "CNC": ProductType.CNC,
        "NRML": ProductType.NRML,
    }
    result = _map.get(product.strip().upper())
    if result is None:
        raise BrokerMappingError(
            f"Unknown Zerodha product {product!r}. Expected one of {list(_map)}."
        )
    return result


def map_zerodha_exchange(exchange: str) -> Exchange:
    """Map a Zerodha exchange string to Exchange."""
    _map = {
        "NSE": Exchange.NSE,
        "BSE": Exchange.BSE,
    }
    result = _map.get(exchange.strip().upper())
    if result is None:
        raise BrokerMappingError(
            f"Unknown Zerodha exchange {exchange!r}. Expected one of {list(_map)}."
        )
    return result


# ---------------------------------------------------------------------------
# Composite mappers
# ---------------------------------------------------------------------------


def map_zerodha_order(raw: dict[str, Any]) -> InternalOrder:
    """Map a raw Zerodha order dict to an InternalOrder.

    A new internal_order_id is generated.  The Zerodha order_id is preserved
    as broker_order_id.  The full raw dict is stored in raw_broker_response.

    Args:
        raw: A dict as returned by KiteConnect.orders() or the orders endpoint.

    Returns:
        InternalOrder populated from the raw dict.

    Raises:
        BrokerMappingError: if a critical field is missing or unmappable.
    """
    try:
        symbol = _require(raw, "tradingsymbol")
        exchange = map_zerodha_exchange(_require(raw, "exchange"))
        side = map_zerodha_side(_require(raw, "transaction_type"))
        order_type = map_zerodha_order_type(_require(raw, "order_type"))
        product = map_zerodha_product(_require(raw, "product"))
        quantity = int(_require(raw, "quantity"))

        status_str = raw.get("status", "UNKNOWN") or "UNKNOWN"
        status = map_zerodha_order_status(status_str)

        broker_order_id: str | None = raw.get("order_id") or None

        created_at = _parse_zerodha_timestamp(raw.get("order_timestamp"), "order_timestamp")
        updated_at_raw = raw.get("exchange_update_timestamp") or raw.get("order_timestamp")
        updated_at = _parse_zerodha_timestamp(updated_at_raw, "updated_at")
        if updated_at < created_at:
            updated_at = created_at

        # Price: 0 means no limit price (MARKET orders)
        price = _decimal_or_none(raw.get("price", 0), zero_means_none=True)
        trigger_price = _decimal_or_none(raw.get("trigger_price", 0), zero_means_none=True)

        validity_str = (raw.get("validity") or "DAY").strip().upper()
        time_in_force = _ZERODHA_VALIDITY_MAP.get(validity_str, TimeInForce.DAY)

        strategy_id = raw.get("tag") or raw.get("strategy_id") or "external"

        return InternalOrder(
            internal_order_id=generate_internal_order_id(),
            broker_order_id=broker_order_id,
            strategy_id=strategy_id,
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=quantity,
            order_type=order_type,
            product=product,
            price=price,
            trigger_price=trigger_price,
            time_in_force=time_in_force,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            raw_broker_response=dict(raw),
        )
    except BrokerMappingError:
        raise
    except Exception as exc:
        raise BrokerMappingError(f"Failed to map Zerodha order to InternalOrder: {exc}") from exc


def map_zerodha_trade(raw: dict[str, Any]) -> TradeFill:
    """Map a raw Zerodha trade dict to a TradeFill.

    Because Zerodha trades reference a broker_order_id but not an internal
    order ID, the internal_order_id is derived as 'ord_<zerodha_order_id>'.
    Callers should resolve this to the actual internal_order_id via the ledger
    after mapping.

    Args:
        raw: A dict as returned by KiteConnect.trades() or the trades endpoint.

    Returns:
        TradeFill populated from the raw dict.

    Raises:
        BrokerMappingError: if a critical field is missing or unmappable.
    """
    try:
        _require(raw, "trade_id")  # validate presence; value used only via raw
        broker_order_id = _require(raw, "order_id")
        symbol = _require(raw, "tradingsymbol")
        exchange = map_zerodha_exchange(_require(raw, "exchange"))
        side = map_zerodha_side(_require(raw, "transaction_type"))
        quantity = int(_require(raw, "quantity"))
        price = Decimal(str(_require(raw, "average_price")))

        ts_raw = (
            raw.get("fill_timestamp") or raw.get("order_timestamp") or raw.get("exchange_timestamp")
        )
        timestamp = _parse_zerodha_timestamp(ts_raw, "fill_timestamp")

        return TradeFill(
            fill_id=generate_fill_id(),
            internal_order_id=f"ord_{broker_order_id}",
            broker_order_id=str(broker_order_id),
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=quantity,
            price=price,
            fees=Decimal("0"),
            timestamp=timestamp,
        )
    except BrokerMappingError:
        raise
    except Exception as exc:
        raise BrokerMappingError(
            f"Failed to map Zerodha trade {raw.get('trade_id')!r} to TradeFill: {exc}"
        ) from exc


def map_zerodha_position(raw: dict[str, Any], fetched_at: datetime | None = None) -> Position:
    """Map a raw Zerodha position dict to a Position.

    Zerodha does not expose an explicit updated_at on positions.  The
    fetched_at parameter is used instead; it defaults to datetime.now().

    Args:
        raw: A dict as returned by KiteConnect.positions()["net"] or ["day"].
        fetched_at: When the position data was fetched (defaults to now).

    Returns:
        Position populated from the raw dict.

    Raises:
        BrokerMappingError: if a critical field is missing or unmappable.
    """
    try:
        symbol = _require(raw, "tradingsymbol")
        exchange = map_zerodha_exchange(_require(raw, "exchange"))
        product = map_zerodha_product(_require(raw, "product"))
        quantity = int(raw.get("quantity", 0))
        average_price = Decimal(str(raw.get("average_price", 0) or 0))
        last_price_raw = raw.get("last_price")
        last_price = Decimal(str(last_price_raw)) if last_price_raw is not None else None
        realized_pnl = Decimal(str(raw.get("realised", 0) or 0))
        unrealized_pnl = Decimal(str(raw.get("unrealised", 0) or 0))
        updated_at = fetched_at or datetime.now()

        return Position(
            symbol=symbol,
            exchange=exchange,
            product=product,
            quantity=quantity,
            average_price=average_price,
            last_price=last_price,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            updated_at=updated_at,
        )
    except BrokerMappingError:
        raise
    except Exception as exc:
        raise BrokerMappingError(
            f"Failed to map Zerodha position for {raw.get('tradingsymbol')!r}: {exc}"
        ) from exc


def map_zerodha_instrument(raw: dict[str, Any]) -> Instrument:
    """Map a raw Zerodha instrument dict to an Instrument.

    Args:
        raw: A dict as returned by KiteConnect.instruments() or
             ZerodhaBroker.get_instruments().

    Returns:
        Instrument populated from the raw dict.

    Raises:
        BrokerMappingError: if a critical field is missing or unmappable.
    """
    try:
        symbol = _require(raw, "tradingsymbol")
        exchange = map_zerodha_exchange(_require(raw, "exchange"))
        instrument_token = raw.get("instrument_token")
        name = raw.get("name") or None
        tick_size_raw = raw.get("tick_size")
        tick_size = Decimal(str(tick_size_raw)) if tick_size_raw else None
        lot_size = int(raw.get("lot_size") or 1)

        return Instrument(
            symbol=symbol,
            exchange=exchange,
            instrument_token=int(instrument_token) if instrument_token is not None else None,
            name=name,
            tick_size=tick_size,
            lot_size=lot_size,
            is_active=True,
        )
    except BrokerMappingError:
        raise
    except Exception as exc:
        raise BrokerMappingError(
            f"Failed to map Zerodha instrument for {raw.get('tradingsymbol')!r}: {exc}"
        ) from exc
