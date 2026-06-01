"""Backtest portfolio tracker.

Tracks cash, positions, realized/unrealized P&L, and equity over time.
Supports both long and short positions. A SELL on a flat/short position opens
or adds to a short. A BUY on a short position covers it.
Selling more than a current long position is rejected (close long first).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_engine.domain.enums import Exchange, ProductType, Side
from trading_engine.domain.models import PortfolioSnapshot, Position, TradeFill


class InsufficientPositionError(Exception):
    """Raised when a SELL quantity exceeds the current long position."""


@dataclass
class _PositionState:
    """Mutable internal position state. quantity < 0 means short."""

    symbol: str
    exchange: Exchange
    product: ProductType
    quantity: int = 0
    average_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    last_price: Decimal | None = None
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_position(self) -> Position:
        return Position(
            symbol=self.symbol,
            exchange=self.exchange,
            product=self.product,
            quantity=self.quantity,
            average_price=self.average_price,
            last_price=self.last_price,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            updated_at=self.updated_at,
        )


class BacktestPortfolio:
    """Tracks cash, positions, and P&L for a backtest run.

    Args:
        initial_cash:       Starting cash balance in INR.
        exchange:           Default exchange for position tracking.
        product:            Default product type for position tracking.
    """

    def __init__(
        self,
        initial_cash: Decimal,
        exchange: Exchange = Exchange.NSE,
        product: ProductType = ProductType.MIS,
    ) -> None:
        if initial_cash <= 0:
            raise ValueError(f"initial_cash must be positive, got {initial_cash}")
        self._cash: Decimal = initial_cash
        self._exchange = exchange
        self._product = product
        self._positions: dict[str, _PositionState] = {}
        self._fills: list[TradeFill] = []
        self._equity_curve: list[tuple[datetime, Decimal]] = []
        self._total_fees: Decimal = Decimal("0")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def fills(self) -> list[TradeFill]:
        return list(self._fills)

    @property
    def equity_curve(self) -> list[tuple[datetime, Decimal]]:
        return list(self._equity_curve)

    @property
    def total_fees(self) -> Decimal:
        return self._total_fees

    def total_equity(self, latest_prices: dict[str, Decimal] | None = None) -> Decimal:
        """Cash + market value of all open positions (long adds, short subtracts)."""
        prices = latest_prices or {}
        equity = self._cash
        for sym, pos in self._positions.items():
            if pos.quantity != 0:
                price = prices.get(sym, pos.average_price)
                equity += Decimal(str(pos.quantity)) * price
        return equity

    def get_position(self, symbol: str) -> _PositionState | None:
        return self._positions.get(symbol)

    def apply_fill(self, fill: TradeFill) -> None:
        """Update cash and position state from a trade fill.

        BUY on flat/long: opens or adds to long position.
        BUY on short: covers the short, books realized P&L.
        SELL on flat/short: opens or adds to short position.
        SELL on long: closes or reduces long, books realized P&L.

        Raises:
            InsufficientPositionError: If SELL quantity > current long position.
        """
        symbol = fill.symbol
        qty = Decimal(str(fill.quantity))
        gross_value = qty * fill.price

        pos = self._positions.setdefault(
            symbol,
            _PositionState(
                symbol=symbol,
                exchange=fill.exchange,
                product=self._product,
                updated_at=fill.timestamp,
            ),
        )

        if fill.side == Side.BUY:
            if pos.quantity < 0:
                # Covering a short position.
                cover_qty = Decimal(str(min(fill.quantity, -pos.quantity)))
                realized = cover_qty * pos.average_price - cover_qty * fill.price - fill.fees
                pos.realized_pnl += realized
                pos.quantity += fill.quantity
                self._cash -= gross_value + fill.fees
                if pos.quantity > 0:
                    # Went net long after cover — new long entered at fill price.
                    pos.average_price = fill.price
                elif pos.quantity == 0:
                    pos.average_price = Decimal("0")
            else:
                # Opening or adding to a long position.
                existing_value = Decimal(str(pos.quantity)) * pos.average_price
                self._cash -= gross_value + fill.fees
                pos.quantity += fill.quantity
                pos.average_price = (existing_value + gross_value) / Decimal(str(pos.quantity))
            pos.updated_at = fill.timestamp

        elif fill.side == Side.SELL:
            if pos.quantity > 0:
                # Reducing or closing a long position.
                if pos.quantity < fill.quantity:
                    raise InsufficientPositionError(
                        f"Cannot sell {fill.quantity} of {symbol}: only {pos.quantity} held long. "
                        "Close long before going short."
                    )
                cost_basis = qty * pos.average_price
                realized = gross_value - cost_basis - fill.fees
                pos.realized_pnl += realized
                pos.quantity -= fill.quantity
                if pos.quantity == 0:
                    pos.average_price = Decimal("0")
                self._cash += gross_value - fill.fees
            else:
                # Opening or adding to a short position (pos.quantity <= 0).
                existing_short = Decimal(str(-pos.quantity))
                new_short_total = existing_short + qty
                pos.average_price = (
                    (existing_short * pos.average_price + gross_value) / new_short_total
                )
                pos.quantity -= fill.quantity
                self._cash += gross_value - fill.fees
            pos.updated_at = fill.timestamp

        self._total_fees += fill.fees
        self._fills.append(fill)

    def mark_to_market(self, timestamp: datetime, latest_prices: dict[str, Decimal]) -> None:
        """Update unrealized P&L and record equity curve point."""
        for sym, pos in self._positions.items():
            if pos.quantity != 0 and sym in latest_prices:
                price = latest_prices[sym]
                pos.last_price = price
                # qty*(price - avg) works for both long (qty>0) and short (qty<0).
                pos.unrealized_pnl = (
                    Decimal(str(pos.quantity)) * price
                    - Decimal(str(pos.quantity)) * pos.average_price
                )
        equity = self.total_equity(latest_prices)
        self._equity_curve.append((timestamp, equity))

    def get_snapshot(self, timestamp: datetime) -> PortfolioSnapshot:
        """Return a PortfolioSnapshot of the current state."""
        positions = [p.to_position() for p in self._positions.values()]
        realized = sum((p.realized_pnl for p in self._positions.values()), Decimal("0"))
        unrealized = sum((p.unrealized_pnl for p in self._positions.values()), Decimal("0"))
        long_exposure = sum(
            (
                Decimal(str(p.quantity)) * (p.last_price or p.average_price)
                for p in self._positions.values()
                if p.quantity > 0
            ),
            Decimal("0"),
        )
        short_exposure = sum(
            (
                Decimal(str(-p.quantity)) * (p.last_price or p.average_price)
                for p in self._positions.values()
                if p.quantity < 0
            ),
            Decimal("0"),
        )
        return PortfolioSnapshot(
            timestamp=timestamp,
            cash=self._cash,
            positions=positions,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            gross_exposure=long_exposure + short_exposure,
            net_exposure=long_exposure - short_exposure,
        )
