"""Integration tests: GapContinuationStrategy running end-to-end in BacktestEngine."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.data_feed import HistoricalDataFeed  # noqa: E402
from trading_engine.backtest.engine import BacktestEngine  # noqa: E402
from trading_engine.backtest.portfolio import BacktestPortfolio  # noqa: E402
from trading_engine.backtest.simulated_broker import SimulatedBroker  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.strategies.gap_continuation import (  # noqa: E402
    GapContinuationConfig,
    GapContinuationStrategy,
)

_ZERO_COST = CostModel(
    brokerage_per_order=Decimal("0"),
    brokerage_cap=Decimal("0"),
    stt_rate=Decimal("0"),
    exchange_txn_rate=Decimal("0"),
    sebi_rate=Decimal("0"),
    stamp_duty_rate=Decimal("0"),
    gst_rate=Decimal("0"),
)
_ZERO_SLIP = SlippageModel(bps=Decimal("0"))


def _make_engine(
    candles: dict[str, pd.DataFrame],
    config: GapContinuationConfig | None = None,
    initial_cash: Decimal = Decimal("500000"),
) -> BacktestEngine:
    cfg = config or GapContinuationConfig()
    strategy = GapContinuationStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    broker = SimulatedBroker(portfolio, _ZERO_COST, _ZERO_SLIP)
    feed = HistoricalDataFeed(candles)
    return BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=cfg.strategy_id,
        symbols=list(candles.keys()),
        parameters={},
    )


def _make_gap_continuation_candles() -> pd.DataFrame:
    """Two-day candle sequence that produces one gap-continuation LONG trade.

    Day 1 (2024-01-15): single bar, close=100 (prior_close for day 2).
    Day 2 (2024-01-16):
      09:15 — opening bar: open=102 (~200 bps gap-up, within [50, 500]).
               gap_direction="LONG", gap_qualified=True.
      09:20 — entry bar: close=102.3 >= trigger=102*(1+0.001)=102.102 -> LONG entry.
      09:25-15:10 — bars that don't hit stop (stop=102.3*(1-0.02)=100.254)
                    or target (none by default -> square-off).
      15:15 — square-off bar.
    """
    rows = []
    # Day 1
    rows.append({
        "timestamp": pd.Timestamp("2024-01-15 09:15:00"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 5000,
    })
    # Day 2 opening bar: gap-up to 102
    rows.append({
        "timestamp": pd.Timestamp("2024-01-16 09:15:00"),
        "open": 102.0, "high": 102.5, "low": 101.5, "close": 102.0, "volume": 5000,
    })
    # 09:20 — LONG entry: close=102.3 >= trigger=102.102
    rows.append({
        "timestamp": pd.Timestamp("2024-01-16 09:20:00"),
        "open": 102.0, "high": 102.4, "low": 101.8, "close": 102.3, "volume": 3000,
    })
    # 09:30-15:10: bars that don't hit stop (<100.254) or target (no target set)
    for h in range(9, 15):
        for m in [30, 45]:
            rows.append({
                "timestamp": pd.Timestamp(f"2024-01-16 {h:02d}:{m:02d}:00"),
                "open": 102.5, "high": 103.0, "low": 102.0, "close": 102.5, "volume": 2000,
            })
    # 15:15 — square-off
    rows.append({
        "timestamp": pd.Timestamp("2024-01-16 15:15:00"),
        "open": 102.5, "high": 102.8, "low": 102.2, "close": 102.5, "volume": 1000,
    })
    return pd.DataFrame(rows)


def _test_cfg() -> GapContinuationConfig:
    return GapContinuationConfig(
        strategy_id="gc_test",
        min_gap_bps=50.0,
        max_gap_bps=500.0,
        continuation_trigger_bps=10.0,
        stop_loss_bps=200.0,
        target_bps=None,
        allow_long_continuations=True,
        allow_short_continuations=True,
    )


class TestGapContinuationBacktest:
    def test_engine_runs_without_error(self):
        df = _make_gap_continuation_candles()
        engine = _make_engine({"TEST": df}, config=_test_cfg())
        report = engine.run()
        assert report is not None

    def test_at_least_two_fills_produced(self):
        """Fixture produces a long continuation entry + square-off exit."""
        df = _make_gap_continuation_candles()
        engine = _make_engine({"TEST": df}, config=_test_cfg())
        report = engine.run()
        assert len(report.fills) >= 2, "expected entry + exit fills"

    def test_fills_are_buy_entry_then_sell_exit(self):
        from trading_engine.domain.enums import Side
        df = _make_gap_continuation_candles()
        engine = _make_engine({"TEST": df}, config=_test_cfg())
        report = engine.run()
        fills = report.fills
        assert fills[0].side == Side.BUY
        assert fills[1].side == Side.SELL

    def test_first_day_produces_no_fills(self):
        single_day = _make_gap_continuation_candles().iloc[:1]
        engine = _make_engine({"TEST": single_day}, config=_test_cfg())
        report = engine.run()
        assert len(report.fills) == 0

    def test_report_has_metrics(self):
        df = _make_gap_continuation_candles()
        engine = _make_engine({"TEST": df}, config=_test_cfg())
        report = engine.run()
        assert hasattr(report, "metrics")
        assert report.metrics is not None

    def test_no_zerodha_or_dotenv_in_strategy(self):
        source = (ROOT / "src" / "trading_engine" / "strategies" / "gap_continuation.py").read_text()
        assert "zerodha" not in source.lower()
        assert "kite" not in source.lower()
        assert "load_dotenv" not in source
