"""Tests for scripts/live_order_pilot.py — live order pilot CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable before importing the script module.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import live_order_pilot as _script  # noqa: E402


class TestArgParsing:
    def test_required_args_parsed(self):
        args = _script._parse_args(
            [
                "--symbol", "RELIANCE",
                "--side", "BUY",
                "--quantity", "1",
                "--order-type", "MARKET",
                "--i-understand-this-places-real-orders",
            ]
        )
        assert args.symbol == "RELIANCE"
        assert args.side == "BUY"
        assert args.quantity == 1
        assert args.order_type == "MARKET"

    def test_confirmed_flag_false_by_default(self):
        args = _script._parse_args(
            ["--symbol", "RELIANCE", "--side", "BUY", "--quantity", "1", "--order-type", "MARKET"]
        )
        assert args.confirmed_flag is False

    def test_defaults(self):
        args = _script._parse_args(
            [
                "--symbol", "RELIANCE",
                "--side", "BUY",
                "--quantity", "1",
                "--order-type", "MARKET",
                "--i-understand-this-places-real-orders",
            ]
        )
        assert args.product == "MIS"
        assert args.strategy_id == "pilot"
        assert args.exchange == "NSE"

    def test_optional_price(self):
        args = _script._parse_args(
            [
                "--symbol", "RELIANCE",
                "--side", "BUY",
                "--quantity", "1",
                "--order-type", "LIMIT",
                "--price", "2345",
                "--i-understand-this-places-real-orders",
            ]
        )
        assert args.price == "2345"


class TestBuildIntent:
    def test_market_order_intent(self):
        args = _script._parse_args(
            ["--symbol", "RELIANCE", "--side", "BUY", "--quantity", "2", "--order-type", "MARKET"]
        )
        intent = _script._build_intent(args)
        assert intent.symbol == "RELIANCE"
        assert intent.quantity == 2

    def test_limit_order_requires_price(self):
        args = _script._parse_args(
            ["--symbol", "RELIANCE", "--side", "BUY", "--quantity", "1", "--order-type", "LIMIT"]
        )
        with pytest.raises(SystemExit):
            _script._build_intent(args)

    def test_invalid_quantity_exits(self):
        args = _script._parse_args(
            ["--symbol", "RELIANCE", "--side", "BUY", "--quantity", "-1", "--order-type", "MARKET"]
        )
        with pytest.raises(SystemExit):
            _script._build_intent(args)

    def test_zero_price_exits(self):
        args = _script._parse_args(
            [
                "--symbol", "RELIANCE",
                "--side", "BUY",
                "--quantity", "1",
                "--order-type", "LIMIT",
                "--price", "0",
            ]
        )
        with pytest.raises(SystemExit):
            _script._build_intent(args)


class TestMainSafetyChecks:
    def test_missing_flag_returns_2(self, capsys):
        rc = _script.main(
            ["--symbol", "RELIANCE", "--side", "BUY", "--quantity", "1", "--order-type", "MARKET"]
        )
        assert rc == 2

    def test_missing_flag_prints_error(self, capsys):
        _script.main(
            ["--symbol", "RELIANCE", "--side", "BUY", "--quantity", "1", "--order-type", "MARKET"]
        )
        captured = capsys.readouterr()
        obj = json.loads(captured.err)
        assert "i-understand-this-places-real-orders" in obj["error"].lower() or "safety" in obj["error"].lower() or "Missing" in obj["error"]

    def test_live_execution_disabled_returns_3(self, capsys, monkeypatch):
        # Ensure env vars are off
        monkeypatch.setenv("LIVE_ORDER_EXECUTION_ENABLED", "false")
        monkeypatch.setenv("LIVE_ORDER_PILOT_ENABLED", "false")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
        rc = _script.main(
            [
                "--symbol", "RELIANCE",
                "--side", "BUY",
                "--quantity", "1",
                "--order-type", "MARKET",
                "--i-understand-this-places-real-orders",
            ]
        )
        assert rc == 3

    def test_pilot_disabled_returns_3(self, capsys, monkeypatch):
        monkeypatch.setenv("LIVE_ORDER_EXECUTION_ENABLED", "true")
        monkeypatch.setenv("LIVE_ORDER_PILOT_ENABLED", "false")
        rc = _script.main(
            [
                "--symbol", "RELIANCE",
                "--side", "BUY",
                "--quantity", "1",
                "--order-type", "MARKET",
                "--i-understand-this-places-real-orders",
            ]
        )
        assert rc == 3
