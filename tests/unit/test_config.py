"""Tests for configuration management (Settings).

These tests verify that:
  - Safe defaults are applied even when no env file is present.
  - LIVE_TRADING_ENABLED defaults to False.
  - Secrets never appear in repr() or str().
  - Risk limits have sensible positive defaults.
"""

from __future__ import annotations

import pytest

from trading_engine.common.config import Settings


@pytest.fixture(autouse=True)
def clear_trading_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no leaked env vars affect defaults tests."""
    for var in (
        "LIVE_TRADING_ENABLED",
        "PAPER_TRADING_ENABLED",
        "LIVE_ORDER_EXECUTION_ENABLED",
        "LIVE_ORDER_PILOT_ENABLED",
        "LIVE_MAX_ORDER_QUANTITY",
        "LIVE_ALLOWED_SYMBOLS",
        "LIVE_ALLOWED_EXCHANGE",
        "LIVE_ALLOWED_PRODUCT",
        "LIVE_ALLOWED_ORDER_TYPES",
        "ZERODHA_API_KEY",
        "ZERODHA_API_SECRET",
        "ZERODHA_ACCESS_TOKEN",
        "MAX_DAILY_LOSS",
        "MAX_ORDER_VALUE",
        "MAX_TRADES_PER_DAY",
        "ORDER_RATE_LIMIT_PER_SECOND",
    ):
        monkeypatch.delenv(var, raising=False)


def make_settings(**kwargs: object) -> Settings:
    """Create Settings reading from a nonexistent .env file so only defaults apply."""
    return Settings(_env_file=".env.test_nonexistent", **kwargs)  # type: ignore[call-arg]


class TestSafeDefaults:
    def test_live_trading_disabled_by_default(self) -> None:
        settings = make_settings()
        assert settings.live_trading_enabled is False

    def test_paper_trading_enabled_by_default(self) -> None:
        settings = make_settings()
        assert settings.paper_trading_enabled is True

    def test_max_daily_loss_has_positive_default(self) -> None:
        settings = make_settings()
        assert settings.max_daily_loss > 0

    def test_max_order_value_has_positive_default(self) -> None:
        settings = make_settings()
        assert settings.max_order_value > 0

    def test_max_trades_per_day_has_positive_default(self) -> None:
        settings = make_settings()
        assert settings.max_trades_per_day > 0

    def test_order_rate_limit_has_positive_default(self) -> None:
        settings = make_settings()
        assert settings.order_rate_limit_per_second >= 1


class TestSecretsMasked:
    def test_api_key_not_in_repr(self) -> None:
        settings = make_settings(zerodha_api_key="MY_SECRET_KEY_12345")
        assert "MY_SECRET_KEY_12345" not in repr(settings)

    def test_api_secret_not_in_repr(self) -> None:
        settings = make_settings(zerodha_api_secret="SUPER_SECRET_VALUE")
        assert "SUPER_SECRET_VALUE" not in repr(settings)

    def test_access_token_not_in_repr(self) -> None:
        settings = make_settings(zerodha_access_token="MY_ACCESS_TOKEN_XYZ")
        assert "MY_ACCESS_TOKEN_XYZ" not in repr(settings)

    def test_api_key_not_in_str(self) -> None:
        settings = make_settings(zerodha_api_key="MY_SECRET_KEY_12345")
        assert "MY_SECRET_KEY_12345" not in str(settings)

    def test_pydantic_secret_str_masks_get_secret_value(self) -> None:
        settings = make_settings(zerodha_api_key="RAW_SECRET")
        # Pydantic SecretStr masks in repr but exposes via get_secret_value()
        assert settings.zerodha_api_key.get_secret_value() == "RAW_SECRET"
        # But it must NOT appear in the field's own repr
        assert "RAW_SECRET" not in repr(settings.zerodha_api_key)


class TestEnvOverride:
    def test_live_trading_can_be_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
        # Note: env vars take effect when Settings reads them; use a fresh instance.
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert settings.live_trading_enabled is True

    def test_paper_trading_can_be_disabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PAPER_TRADING_ENABLED", "false")
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert settings.paper_trading_enabled is False


class TestPilotDefaults:
    def test_live_order_execution_disabled_by_default(self) -> None:
        settings = make_settings()
        assert settings.live_order_execution_enabled is False

    def test_live_order_pilot_disabled_by_default(self) -> None:
        settings = make_settings()
        assert settings.live_order_pilot_enabled is False

    def test_max_order_quantity_default_is_one(self) -> None:
        settings = make_settings()
        assert settings.live_max_order_quantity == 1

    def test_allowed_symbols_default_is_empty(self) -> None:
        settings = make_settings()
        assert settings.live_allowed_symbols == []

    def test_allowed_exchange_default_is_nse(self) -> None:
        settings = make_settings()
        assert settings.live_allowed_exchange == "NSE"

    def test_allowed_product_default_is_mis(self) -> None:
        settings = make_settings()
        assert settings.live_allowed_product == "MIS"

    def test_allowed_order_types_includes_market_and_limit(self) -> None:
        settings = make_settings()
        assert "MARKET" in settings.live_allowed_order_types
        assert "LIMIT" in settings.live_allowed_order_types


class TestPilotEnvOverride:
    def test_execution_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVE_ORDER_EXECUTION_ENABLED", "true")
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert settings.live_order_execution_enabled is True

    def test_pilot_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVE_ORDER_PILOT_ENABLED", "true")
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert settings.live_order_pilot_enabled is True

    def test_allowed_symbols_parsed_from_json_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVE_ALLOWED_SYMBOLS", '["RELIANCE","INFY","TCS"]')
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert settings.live_allowed_symbols == ["RELIANCE", "INFY", "TCS"]

    def test_allowed_order_types_parsed_from_json_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVE_ALLOWED_ORDER_TYPES", '["MARKET","LIMIT"]')
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert "MARKET" in settings.live_allowed_order_types
        assert "LIMIT" in settings.live_allowed_order_types

    def test_max_order_quantity_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIVE_MAX_ORDER_QUANTITY", "3")
        settings = Settings(_env_file=".env.test_nonexistent")  # type: ignore[call-arg]
        assert settings.live_max_order_quantity == 3
