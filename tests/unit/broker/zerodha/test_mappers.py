"""Tests for Zerodha response mappers.

All tests use fake raw dicts — no real Zerodha API calls.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from trading_engine.broker.zerodha.mappers import (
    map_zerodha_exchange,
    map_zerodha_instrument,
    map_zerodha_order,
    map_zerodha_order_status,
    map_zerodha_order_type,
    map_zerodha_position,
    map_zerodha_product,
    map_zerodha_side,
    map_zerodha_trade,
)
from trading_engine.common.exceptions import BrokerMappingError
from trading_engine.domain.enums import (
    Exchange,
    OrderStatus,
    OrderType,
    ProductType,
    Side,
)

# ---------------------------------------------------------------------------
# Fake raw data
# ---------------------------------------------------------------------------

_FAKE_ORDER = {
    "order_id": "150516000121212",
    "exchange": "NSE",
    "tradingsymbol": "RELIANCE",
    "transaction_type": "BUY",
    "order_type": "LIMIT",
    "product": "MIS",
    "quantity": 10,
    "price": 2800.0,
    "trigger_price": 0.0,
    "validity": "DAY",
    "status": "OPEN",
    "order_timestamp": "2024-01-15 09:15:00",
    "exchange_update_timestamp": "2024-01-15 09:15:01",
    "tag": "orb_strategy",
}

_FAKE_MARKET_ORDER = {
    "order_id": "150516000999999",
    "exchange": "NSE",
    "tradingsymbol": "TCS",
    "transaction_type": "SELL",
    "order_type": "MARKET",
    "product": "CNC",
    "quantity": 5,
    "price": 0.0,
    "trigger_price": 0.0,
    "validity": "IOC",
    "status": "COMPLETE",
    "order_timestamp": "2024-01-15 10:00:00",
    "exchange_update_timestamp": "2024-01-15 10:00:01",
}

_FAKE_TRADE = {
    "trade_id": "17990797",
    "order_id": "150516000121212",
    "exchange": "NSE",
    "tradingsymbol": "RELIANCE",
    "transaction_type": "BUY",
    "quantity": 10,
    "average_price": 2795.5,
    "fill_timestamp": "2024-01-15 09:20:00",
    "product": "MIS",
}

_FAKE_POSITION = {
    "tradingsymbol": "INFY",
    "exchange": "NSE",
    "instrument_token": 408065,
    "product": "MIS",
    "quantity": 200,
    "average_price": 1176.15,
    "last_price": 1178.0,
    "realised": 0.0,
    "unrealised": 369.99,
}

_FAKE_INSTRUMENT = {
    "instrument_token": 738561,
    "tradingsymbol": "RELIANCE",
    "name": "RELIANCE INDUSTRIES",
    "exchange": "NSE",
    "tick_size": 0.05,
    "lot_size": 1,
    "instrument_type": "EQ",
}


# ---------------------------------------------------------------------------
# Tests: map_zerodha_order_status
# ---------------------------------------------------------------------------


class TestMapZerodhaOrderStatus:
    def test_complete_maps_to_filled(self):
        assert map_zerodha_order_status("COMPLETE") == OrderStatus.FILLED

    def test_open_maps_to_open(self):
        assert map_zerodha_order_status("OPEN") == OrderStatus.OPEN

    def test_trigger_pending_maps_to_open(self):
        assert map_zerodha_order_status("TRIGGER PENDING") == OrderStatus.OPEN

    def test_cancelled_maps_to_cancelled(self):
        assert map_zerodha_order_status("CANCELLED") == OrderStatus.CANCELLED

    def test_rejected_maps_to_rejected(self):
        assert map_zerodha_order_status("REJECTED") == OrderStatus.REJECTED

    def test_pending_maps_to_submitted(self):
        assert map_zerodha_order_status("PENDING") == OrderStatus.SUBMITTED

    def test_unknown_status_maps_to_unknown(self):
        assert map_zerodha_order_status("SOME_FUTURE_STATUS") == OrderStatus.UNKNOWN

    def test_empty_status_maps_to_unknown(self):
        assert map_zerodha_order_status("") == OrderStatus.UNKNOWN

    def test_case_insensitive(self):
        assert map_zerodha_order_status("complete") == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# Tests: map_zerodha_side
# ---------------------------------------------------------------------------


class TestMapZerodhaSide:
    def test_buy(self):
        assert map_zerodha_side("BUY") == Side.BUY

    def test_sell(self):
        assert map_zerodha_side("SELL") == Side.SELL

    def test_lowercase(self):
        assert map_zerodha_side("buy") == Side.BUY

    def test_unknown_raises(self):
        with pytest.raises(BrokerMappingError):
            map_zerodha_side("HOLD")


# ---------------------------------------------------------------------------
# Tests: map_zerodha_order_type
# ---------------------------------------------------------------------------


class TestMapZerodhaOrderType:
    def test_market(self):
        assert map_zerodha_order_type("MARKET") == OrderType.MARKET

    def test_limit(self):
        assert map_zerodha_order_type("LIMIT") == OrderType.LIMIT

    def test_sl(self):
        assert map_zerodha_order_type("SL") == OrderType.SL

    def test_sl_m(self):
        assert map_zerodha_order_type("SL-M") == OrderType.SL_M

    def test_unknown_raises(self):
        with pytest.raises(BrokerMappingError):
            map_zerodha_order_type("STOP")


# ---------------------------------------------------------------------------
# Tests: map_zerodha_product
# ---------------------------------------------------------------------------


class TestMapZerodhaProduct:
    def test_mis(self):
        assert map_zerodha_product("MIS") == ProductType.MIS

    def test_cnc(self):
        assert map_zerodha_product("CNC") == ProductType.CNC

    def test_nrml(self):
        assert map_zerodha_product("NRML") == ProductType.NRML

    def test_unknown_raises(self):
        with pytest.raises(BrokerMappingError):
            map_zerodha_product("BO")


# ---------------------------------------------------------------------------
# Tests: map_zerodha_exchange
# ---------------------------------------------------------------------------


class TestMapZerodhaExchange:
    def test_nse(self):
        assert map_zerodha_exchange("NSE") == Exchange.NSE

    def test_bse(self):
        assert map_zerodha_exchange("BSE") == Exchange.BSE

    def test_unknown_raises(self):
        with pytest.raises(BrokerMappingError):
            map_zerodha_exchange("MCX")


# ---------------------------------------------------------------------------
# Tests: map_zerodha_order
# ---------------------------------------------------------------------------


class TestMapZerodhaOrder:
    def test_valid_limit_order(self):
        order = map_zerodha_order(_FAKE_ORDER)
        assert order.symbol == "RELIANCE"
        assert order.exchange == Exchange.NSE
        assert order.side == Side.BUY
        assert order.order_type == OrderType.LIMIT
        assert order.product == ProductType.MIS
        assert order.quantity == 10
        assert order.price == Decimal("2800.0")
        assert order.trigger_price is None  # 0.0 → None
        assert order.status == OrderStatus.OPEN
        assert order.broker_order_id == "150516000121212"
        assert order.strategy_id == "orb_strategy"
        assert order.raw_broker_response == _FAKE_ORDER

    def test_valid_market_order(self):
        order = map_zerodha_order(_FAKE_MARKET_ORDER)
        assert order.order_type == OrderType.MARKET
        assert order.price is None  # 0.0 → None for MARKET
        assert order.status == OrderStatus.FILLED
        assert order.side == Side.SELL
        assert order.product == ProductType.CNC

    def test_internal_order_id_generated(self):
        o1 = map_zerodha_order(_FAKE_ORDER)
        o2 = map_zerodha_order(_FAKE_ORDER)
        assert o1.internal_order_id != o2.internal_order_id
        assert o1.internal_order_id.startswith("ord_")

    def test_missing_tradingsymbol_raises(self):
        raw = dict(_FAKE_ORDER)
        del raw["tradingsymbol"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_order(raw)

    def test_missing_exchange_raises(self):
        raw = dict(_FAKE_ORDER)
        del raw["exchange"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_order(raw)

    def test_unknown_exchange_raises(self):
        raw = dict(_FAKE_ORDER, exchange="MCX")
        with pytest.raises(BrokerMappingError):
            map_zerodha_order(raw)

    def test_timestamp_parsed_correctly(self):
        order = map_zerodha_order(_FAKE_ORDER)
        assert order.created_at == datetime(2024, 1, 15, 9, 15, 0)

    def test_default_strategy_id_when_no_tag(self):
        raw = dict(_FAKE_ORDER)
        raw.pop("tag", None)
        order = map_zerodha_order(raw)
        assert order.strategy_id == "external"


# ---------------------------------------------------------------------------
# Tests: map_zerodha_trade
# ---------------------------------------------------------------------------


class TestMapZerodhaTrade:
    def test_valid_trade(self):
        fill = map_zerodha_trade(_FAKE_TRADE)
        assert fill.symbol == "RELIANCE"
        assert fill.exchange == Exchange.NSE
        assert fill.side == Side.BUY
        assert fill.quantity == 10
        assert fill.price == Decimal("2795.5")
        assert fill.broker_order_id == "150516000121212"
        assert fill.fill_id.startswith("fill_")

    def test_internal_order_id_derived_from_broker_order_id(self):
        fill = map_zerodha_trade(_FAKE_TRADE)
        assert fill.internal_order_id == "ord_150516000121212"

    def test_missing_trade_id_raises(self):
        raw = dict(_FAKE_TRADE)
        del raw["trade_id"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_trade(raw)

    def test_missing_order_id_raises(self):
        raw = dict(_FAKE_TRADE)
        del raw["order_id"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_trade(raw)

    def test_missing_tradingsymbol_raises(self):
        raw = dict(_FAKE_TRADE)
        del raw["tradingsymbol"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_trade(raw)

    def test_fill_id_unique_per_call(self):
        f1 = map_zerodha_trade(_FAKE_TRADE)
        f2 = map_zerodha_trade(_FAKE_TRADE)
        assert f1.fill_id != f2.fill_id

    def test_timestamp_from_fill_timestamp(self):
        fill = map_zerodha_trade(_FAKE_TRADE)
        assert fill.timestamp == datetime(2024, 1, 15, 9, 20, 0)

    def test_fallback_to_order_timestamp(self):
        raw = dict(_FAKE_TRADE)
        del raw["fill_timestamp"]
        raw["order_timestamp"] = "2024-01-15 10:00:00"
        fill = map_zerodha_trade(raw)
        assert fill.timestamp == datetime(2024, 1, 15, 10, 0, 0)

    def test_fees_default_zero(self):
        fill = map_zerodha_trade(_FAKE_TRADE)
        assert fill.fees == Decimal("0")


# ---------------------------------------------------------------------------
# Tests: map_zerodha_position
# ---------------------------------------------------------------------------


class TestMapZerodhaPosition:
    def test_valid_position(self):
        ts = datetime(2024, 1, 15, 10, 0, 0)
        pos = map_zerodha_position(_FAKE_POSITION, fetched_at=ts)
        assert pos.symbol == "INFY"
        assert pos.exchange == Exchange.NSE
        assert pos.product == ProductType.MIS
        assert pos.quantity == 200
        assert pos.average_price == Decimal("1176.15")
        assert pos.last_price == Decimal("1178.0")
        assert pos.realized_pnl == Decimal("0.0")
        assert pos.unrealized_pnl == Decimal("369.99")
        assert pos.updated_at == ts

    def test_missing_tradingsymbol_raises(self):
        raw = dict(_FAKE_POSITION)
        del raw["tradingsymbol"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_position(raw)

    def test_fetched_at_defaults_to_now(self):
        pos = map_zerodha_position(_FAKE_POSITION)
        assert pos.updated_at is not None

    def test_unknown_product_raises(self):
        raw = dict(_FAKE_POSITION, product="BO")
        with pytest.raises(BrokerMappingError):
            map_zerodha_position(raw)


# ---------------------------------------------------------------------------
# Tests: map_zerodha_instrument
# ---------------------------------------------------------------------------


class TestMapZerodhaInstrument:
    def test_valid_instrument(self):
        inst = map_zerodha_instrument(_FAKE_INSTRUMENT)
        assert inst.symbol == "RELIANCE"
        assert inst.exchange == Exchange.NSE
        assert inst.instrument_token == 738561
        assert inst.name == "RELIANCE INDUSTRIES"
        assert inst.tick_size == Decimal("0.05")
        assert inst.lot_size == 1
        assert inst.is_active is True

    def test_missing_tradingsymbol_raises(self):
        raw = dict(_FAKE_INSTRUMENT)
        del raw["tradingsymbol"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_instrument(raw)

    def test_missing_exchange_raises(self):
        raw = dict(_FAKE_INSTRUMENT)
        del raw["exchange"]
        with pytest.raises(BrokerMappingError):
            map_zerodha_instrument(raw)

    def test_no_instrument_token_allowed(self):
        raw = dict(_FAKE_INSTRUMENT)
        del raw["instrument_token"]
        inst = map_zerodha_instrument(raw)
        assert inst.instrument_token is None

    def test_no_tick_size_allowed(self):
        raw = dict(_FAKE_INSTRUMENT)
        raw.pop("tick_size", None)
        inst = map_zerodha_instrument(raw)
        assert inst.tick_size is None

    def test_bse_exchange(self):
        raw = dict(_FAKE_INSTRUMENT, exchange="BSE")
        inst = map_zerodha_instrument(raw)
        assert inst.exchange == Exchange.BSE
