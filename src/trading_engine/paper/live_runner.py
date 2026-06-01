"""Paper live runner — wires live market data feed into paper trading.

PaperLiveRunner connects a ZerodhaLiveMarketFeed (or any feed that emits
LiveTick objects) to:
  - CandleBuilder  — aggregates ticks into Bars
  - Strategy       — produces OrderIntents from Bars
  - RiskEngine     — approves or rejects each intent
  - PaperExecutionBroker — simulates fills
  - PaperPortfolio — tracks cash, positions, equity
  - DashboardSessionWriter — writes periodic status snapshots

No real orders are placed. No Zerodha order APIs are called.
No live_trading_enabled flag is set or required here (the caller checks it).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_engine.dashboard.session_writer import DashboardSessionWriter
from trading_engine.live_data.candle_builder import CandleBuilder
from trading_engine.live_data.models import LiveTick
from trading_engine.notifications.telegram import TelegramNotifier
from trading_engine.paper.broker import PaperExecutionBroker
from trading_engine.paper.portfolio import PaperPortfolio
from trading_engine.risk.engine import RiskEngine
from trading_engine.strategy.base import Strategy, StrategyContext


@dataclass
class PaperLiveRunnerConfig:
    """Configuration for PaperLiveRunner.

    Args:
        strategy_id:       Identifier used in StrategyContext and dashboard.
        symbols:           List of symbols to trade.
        interval_seconds:  Candle interval in seconds (default 60).
        initial_cash:      Starting portfolio cash (default 100,000).
        dashboard_path:    Path to write dashboard JSON. None = no dashboard.
    """

    strategy_id: str = "orb_v1"
    symbols: list[str] = field(default_factory=list)
    interval_seconds: int = 60
    initial_cash: Decimal = field(default_factory=lambda: Decimal("100000"))
    dashboard_path: str | Path | None = "data/dashboard/session_status.json"


class PaperLiveRunner:
    """Runs a paper trading session fed by live ticks.

    The runner is intentionally decoupled from any specific market data
    source: it exposes an ``on_tick`` method that accepts LiveTick objects.
    Tests can feed synthetic ticks directly without any WebSocket.

    Args:
        config:            PaperLiveRunnerConfig with runtime parameters.
        candle_builder:    CandleBuilder that aggregates ticks into Bars.
        strategy:          Strategy instance to call on_bar on.
        execution_broker:  PaperExecutionBroker for simulated fills.
        portfolio:         PaperPortfolio tracking cash and positions.
        risk_engine:       Optional RiskEngine. None = all intents approved.
        dashboard_writer:  Optional DashboardSessionWriter for status snapshots.
        logger:            Optional logger override.
    """

    def __init__(
        self,
        config: PaperLiveRunnerConfig,
        candle_builder: CandleBuilder,
        strategy: Strategy,
        execution_broker: PaperExecutionBroker,
        portfolio: PaperPortfolio,
        risk_engine: RiskEngine | None = None,
        dashboard_writer: DashboardSessionWriter | None = None,
        notifier: TelegramNotifier | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._candle_builder = candle_builder
        self._strategy = strategy
        self._broker = execution_broker
        self._portfolio = portfolio
        self._risk_engine = risk_engine
        self._dashboard_writer = dashboard_writer
        self._notifier = notifier
        self._logger = logger or logging.getLogger(__name__)

        self._context = StrategyContext(
            strategy_id=config.strategy_id,
            mode="paper",
            config={},
        )
        self._fills: list[Any] = []
        self._rejected: list[Any] = []
        self._latest_prices: dict[str, Decimal] = {}
        self._started = False
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialise the strategy. Call once before feeding ticks."""
        if not self._started:
            self._strategy.on_start(self._context)
            self._started = True
            self._logger.info(
                "PaperLiveRunner started — strategy=%s symbols=%s",
                self._config.strategy_id,
                self._config.symbols,
            )
            if self._notifier:
                self._notifier.notify_session_start(
                    self._config.strategy_id,
                    self._config.symbols,
                    self._config.initial_cash,
                )

    def stop(self) -> None:
        """Signal the runner to stop and flush incomplete candles."""
        self._stop_event.set()
        self._flush_open_candles()
        self._strategy.on_stop(self._context)
        self._logger.info(
            "PaperLiveRunner stopped — fills=%d",
            len(self._fills),
        )
        if self._notifier:
            final_equity = self._portfolio.total_equity(self._latest_prices)
            self._notifier.notify_session_end(
                self._config.strategy_id,
                len(self._fills),
                final_equity,
                self._config.initial_cash,
            )

    def on_tick(self, tick: LiveTick) -> None:
        """Process a single live tick.

        1. Feed into CandleBuilder.
        2. If a Bar is completed, pass it to the strategy.
        3. Risk-check resulting OrderIntents.
        4. Simulate fills via PaperExecutionBroker.
        5. Update portfolio and write dashboard snapshot.
        """
        if self._stop_event.is_set():
            return

        bar = self._candle_builder.add_tick(tick)
        if bar is not None:
            self._process_bar(bar)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_bar(self, bar: Any) -> None:
        symbol = bar.symbol
        self._latest_prices[symbol] = bar.close
        ts = bar.timestamp

        order_intents = self._strategy.on_bar(bar, self._context)

        for intent in order_intents:
            decision = self._run_risk_check(intent, ts)
            if decision is not None and not decision.approved:
                self._rejected.append(decision)
                self._logger.warning(
                    "Risk rejected %s %s: %s",
                    intent.side,
                    intent.symbol,
                    decision.reason_code,
                )
                continue

            try:
                fill = self._broker.execute_order_intent(intent, bar)
            except Exception as exc:
                self._logger.warning(
                    "Order execution error for %s %s: %s",
                    intent.side,
                    intent.symbol,
                    exc,
                )
                fill = None

            if fill is not None:
                self._fills.append(fill)
                if self._notifier:
                    self._notifier.notify_fill(fill)

        self._portfolio.mark_to_market(ts, self._latest_prices)
        self._write_dashboard()

    def _flush_open_candles(self) -> None:
        for bar in self._candle_builder.flush():
            self._process_bar(bar)

    def _run_risk_check(self, intent: Any, ts: datetime) -> Any | None:
        if self._risk_engine is None:
            return None
        snapshot = self._portfolio.get_snapshot(ts)
        return self._risk_engine.check_order_intent(intent, snapshot, ts)

    def _write_dashboard(self) -> None:
        if self._dashboard_writer is None:
            return
        try:
            final_equity = self._portfolio.total_equity(self._latest_prices)
            status: dict[str, Any] = {
                "strategy_id": self._config.strategy_id,
                "mode": "paper_live",
                "fills": len(self._fills),
                "rejected": len(self._rejected),
                "final_equity": str(final_equity),
                "symbols": self._config.symbols,
                "updated_at": datetime.now(tz=UTC).isoformat(),
            }
            self._dashboard_writer.write_status(status, source="paper_live")
        except Exception:
            self._logger.exception("Failed to write dashboard status.")
