"""Tests for live_execution.pilot_config — LivePilotConfig."""

from __future__ import annotations

from trading_engine.live_execution.pilot_config import LivePilotConfig


class _FullSettings:
    live_order_execution_enabled = True
    live_order_pilot_enabled = True
    live_max_order_quantity = 3
    live_allowed_symbols = ["RELIANCE", "INFY"]
    live_allowed_exchange = "NSE"
    live_allowed_product = "MIS"
    live_allowed_order_types = ["MARKET", "LIMIT"]


class TestLivePilotConfigDefaults:
    def test_defaults_are_disabled(self):
        config = LivePilotConfig()
        assert config.live_order_execution_enabled is False
        assert config.live_order_pilot_enabled is False

    def test_default_max_quantity_is_one(self):
        config = LivePilotConfig()
        assert config.max_order_quantity == 1

    def test_default_allowed_symbols_is_empty(self):
        config = LivePilotConfig()
        assert config.allowed_symbols == []

    def test_default_exchange_is_nse(self):
        config = LivePilotConfig()
        assert config.allowed_exchange == "NSE"

    def test_default_product_is_mis(self):
        config = LivePilotConfig()
        assert config.allowed_product == "MIS"

    def test_default_order_types(self):
        config = LivePilotConfig()
        assert "MARKET" in config.allowed_order_types
        assert "LIMIT" in config.allowed_order_types


class TestFromSettings:
    def test_reads_all_fields_from_settings(self):
        config = LivePilotConfig.from_settings(_FullSettings())
        assert config.live_order_execution_enabled is True
        assert config.live_order_pilot_enabled is True
        assert config.max_order_quantity == 3
        assert config.allowed_symbols == ["RELIANCE", "INFY"]
        assert config.allowed_exchange == "NSE"
        assert config.allowed_product == "MIS"
        assert config.allowed_order_types == ["MARKET", "LIMIT"]

    def test_from_settings_with_empty_object_uses_defaults(self):
        config = LivePilotConfig.from_settings(object())
        assert config.live_order_execution_enabled is False
        assert config.live_order_pilot_enabled is False
        assert config.max_order_quantity == 1
        assert config.allowed_symbols == []

    def test_allowed_symbols_copied(self):
        s = _FullSettings()
        config = LivePilotConfig.from_settings(s)
        # Modifying original should not affect config
        s.live_allowed_symbols.append("TCS")
        assert "TCS" not in config.allowed_symbols

    def test_allowed_order_types_copied(self):
        s = _FullSettings()
        config = LivePilotConfig.from_settings(s)
        s.live_allowed_order_types.append("SL")
        assert "SL" not in config.allowed_order_types

    def test_partial_settings(self):
        class _Partial:
            live_order_execution_enabled = True

        config = LivePilotConfig.from_settings(_Partial())
        assert config.live_order_execution_enabled is True
        assert config.live_order_pilot_enabled is False  # falls back to default
