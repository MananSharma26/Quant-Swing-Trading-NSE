"""Tests for BacktestPortfolio."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from trading_engine.backtest.portfolio import BacktestPortfolio, InsufficientPositionError
from trading_engine.domain.enums import Exchange, Side
from trading_engine.domain.models import TradeFill


def _make_fill(
    symbol: str = "RELIANCE",
    side: Side = Side.BUY,
    quantity: int = 10,
    price: Decimal = Decimal("500"),
    fees: Decimal = Decimal("20"),
    ts: datetime | None = None,
) -> TradeFill:
    return TradeFill(
        fill_id="fill_test",
        internal_order_id="ord_test",
        symbol=symbol,
        exchange=Exchange.NSE,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        timestamp=ts or datetime(2024, 1, 15, 9, 15),
    )


def _portfolio(cash: Decimal = Decimal("100000")) -> BacktestPortfolio:
    return BacktestPortfolio(initial_cash=cash)


class TestPortfolioBuy:
    def test_buy_reduces_cash(self) -> None:
        p = _portfolio()
        fill = _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("20"))
        p.apply_fill(fill)
        # 10 * 500 = 5000 + 20 fees = 5020 deducted
        assert p.cash == Decimal("100000") - Decimal("5020")

    def test_buy_creates_position(self) -> None:
        p = _portfolio()
        fill = _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"))
        p.apply_fill(fill)
        pos = p.get_position("RELIANCE")
        assert pos is not None
        assert pos.quantity == 10

    def test_buy_sets_average_price(self) -> None:
        p = _portfolio()
        fill = _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("0"))
        p.apply_fill(fill)
        pos = p.get_position("RELIANCE")
        assert pos.average_price == Decimal("500")

    def test_second_buy_updates_average_price(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(quantity=10, price=Decimal("500"), fees=Decimal("0")))
        p.apply_fill(_make_fill(quantity=10, price=Decimal("600"), fees=Decimal("0")))
        pos = p.get_position("RELIANCE")
        assert pos.quantity == 20
        assert pos.average_price == Decimal("550")

    def test_fees_tracked(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(fees=Decimal("25")))
        assert p.total_fees == Decimal("25")


class TestPortfolioSell:
    def test_sell_reduces_position(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(side=Side.BUY, quantity=10, fees=Decimal("0")))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=5, fees=Decimal("0")))
        pos = p.get_position("RELIANCE")
        assert pos.quantity == 5

    def test_sell_increases_cash(self) -> None:
        p = _portfolio()
        p.apply_fill(
            _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("0"))
        )
        cash_after_buy = p.cash
        p.apply_fill(
            _make_fill(side=Side.SELL, quantity=5, price=Decimal("550"), fees=Decimal("0"))
        )
        assert p.cash == cash_after_buy + Decimal("2750")  # 5 * 550

    def test_full_exit_zeroes_position(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(side=Side.BUY, quantity=10, fees=Decimal("0")))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, fees=Decimal("0")))
        pos = p.get_position("RELIANCE")
        assert pos.quantity == 0

    def test_sell_books_realized_pnl(self) -> None:
        p = _portfolio()
        p.apply_fill(
            _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("0"))
        )
        p.apply_fill(
            _make_fill(side=Side.SELL, quantity=10, price=Decimal("600"), fees=Decimal("0"))
        )
        pos = p.get_position("RELIANCE")
        # (600 - 500) * 10 = 1000 realized
        assert pos.realized_pnl == Decimal("1000")

    def test_sell_fees_reduce_realized_pnl(self) -> None:
        p = _portfolio()
        p.apply_fill(
            _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("0"))
        )
        p.apply_fill(
            _make_fill(side=Side.SELL, quantity=10, price=Decimal("600"), fees=Decimal("50"))
        )
        pos = p.get_position("RELIANCE")
        assert pos.realized_pnl == Decimal("950")  # 1000 - 50 fees

    def test_cannot_sell_more_than_held(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(side=Side.BUY, quantity=5, fees=Decimal("0")))
        with pytest.raises(InsufficientPositionError):
            p.apply_fill(_make_fill(side=Side.SELL, quantity=10, fees=Decimal("0")))

    def test_sell_with_no_position_opens_short(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(side=Side.SELL, quantity=1, price=Decimal("100"), fees=Decimal("0")))
        pos = p.get_position("RELIANCE")
        assert pos.quantity == -1


class TestShortSelling:
    def test_short_entry_increases_cash(self) -> None:
        p = _portfolio(Decimal("100000"))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, price=Decimal("100"), fees=Decimal("0")))
        assert p.cash == Decimal("101000")  # 100000 + 10*100

    def test_short_entry_sets_negative_quantity(self) -> None:
        p = _portfolio()
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, price=Decimal("100"), fees=Decimal("0")))
        assert p.get_position("RELIANCE").quantity == -10

    def test_short_cover_books_profit(self) -> None:
        p = _portfolio(Decimal("100000"))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, price=Decimal("100"), fees=Decimal("0")))
        p.apply_fill(_make_fill(side=Side.BUY, quantity=10, price=Decimal("90"), fees=Decimal("0")))
        pos = p.get_position("RELIANCE")
        assert pos.quantity == 0
        assert pos.realized_pnl == Decimal("100")  # (100-90)*10

    def test_short_cover_books_loss(self) -> None:
        p = _portfolio(Decimal("100000"))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, price=Decimal("100"), fees=Decimal("0")))
        p.apply_fill(_make_fill(side=Side.BUY, quantity=10, price=Decimal("110"), fees=Decimal("0")))
        pos = p.get_position("RELIANCE")
        assert pos.realized_pnl == Decimal("-100")  # (100-110)*10

    def test_short_cover_cash_settles_correctly(self) -> None:
        p = _portfolio(Decimal("100000"))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, price=Decimal("100"), fees=Decimal("0")))
        p.apply_fill(_make_fill(side=Side.BUY, quantity=10, price=Decimal("90"), fees=Decimal("0")))
        # Started 100000, received 1000 on short, paid 900 on cover → 100100
        assert p.cash == Decimal("100100")

    def test_total_equity_includes_short_liability(self) -> None:
        p = _portfolio(Decimal("100000"))
        p.apply_fill(_make_fill(side=Side.SELL, quantity=10, price=Decimal("100"), fees=Decimal("0")))
        # cash=101000, short 10 @ 100, current price=105 → equity=101000 + (-10)*105=100000-50=99950
        equity = p.total_equity({"RELIANCE": Decimal("105")})
        assert equity == Decimal("99950")


class TestPortfolioMarkToMarket:
    def test_mark_to_market_updates_unrealized_pnl(self) -> None:
        p = _portfolio()
        p.apply_fill(
            _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("0"))
        )
        p.mark_to_market(datetime(2024, 1, 15, 9, 16), {"RELIANCE": Decimal("550")})
        pos = p.get_position("RELIANCE")
        assert pos.unrealized_pnl == Decimal("500")  # (550 - 500) * 10

    def test_mark_to_market_records_equity_curve(self) -> None:
        p = _portfolio()
        p.mark_to_market(datetime(2024, 1, 15, 9, 15), {})
        p.mark_to_market(datetime(2024, 1, 15, 9, 16), {})
        assert len(p.equity_curve) == 2

    def test_total_equity_with_position(self) -> None:
        p = _portfolio(Decimal("100000"))
        p.apply_fill(
            _make_fill(side=Side.BUY, quantity=10, price=Decimal("500"), fees=Decimal("0"))
        )
        equity = p.total_equity({"RELIANCE": Decimal("600")})
        # cash = 100000 - 5000 = 95000; position = 10 * 600 = 6000; total = 101000
        assert equity == Decimal("101000")


class TestPortfolioInitValidation:
    def test_zero_cash_raises(self) -> None:
        with pytest.raises(ValueError):
            BacktestPortfolio(initial_cash=Decimal("0"))

    def test_negative_cash_raises(self) -> None:
        with pytest.raises(ValueError):
            BacktestPortfolio(initial_cash=Decimal("-1"))


class TestPortfolioSnapshot:
    def test_get_snapshot_returns_snapshot(self) -> None:
        from trading_engine.domain.models import PortfolioSnapshot

        p = _portfolio()
        snap = p.get_snapshot(datetime(2024, 1, 15, 9, 15))
        assert isinstance(snap, PortfolioSnapshot)

    def test_snapshot_cash_matches(self) -> None:
        p = _portfolio(Decimal("50000"))
        snap = p.get_snapshot(datetime(2024, 1, 15, 9, 15))
        assert snap.cash == Decimal("50000")
