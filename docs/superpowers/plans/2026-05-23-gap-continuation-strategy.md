# Gap Continuation Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a gap continuation strategy (invert of gap fade) that goes LONG on gap-ups and SHORT on gap-downs, validated against 1 year of NSE mid-cap minute data.

**Architecture:** `GapContinuationStrategy` mirrors `GapFadeStrategy` in structure (same `Strategy` base, same backtest infrastructure) but reverses the direction logic — gap-up → LONG, gap-down → SHORT, with price confirming in the gap direction before entry. No VWAP complexity; exits are stop-loss, fixed target (bps), or square-off at 15:15.

**Tech Stack:** Python 3.12, dataclasses, Decimal, pandas, pytest, existing `BacktestEngine` / `BacktestPortfolio` / `SimulatedBroker` / `HistoricalDataFeed`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/trading_engine/strategies/gap_continuation.py` | Create | `GapContinuationConfig`, `_SymbolState`, `GapContinuationStrategy` |
| `tests/unit/strategies/test_gap_continuation.py` | Create | Unit tests for config, prior-close, gap detection, entry, exit |
| `tests/unit/strategies/test_gap_continuation_backtest.py` | Create | End-to-end backtest integration tests |
| `scripts/run_gap_continuation_backtest.py` | Create | CLI runner: loads parquet, runs engine, prints summary |
| `scripts/sweep_gap_continuation_params.py` | Create | Grid sweep over continuation params, saves CSV + JSON |

---

## Codebase Context

All imports below are resolvable. The project root is one level above `src/` and `scripts/`.

**Existing patterns to follow exactly:**
- `src/trading_engine/strategies/gap_fade.py` — strategy structure (copy and invert direction logic)
- `scripts/run_gap_fade_backtest.py` — runner CLI pattern
- `scripts/sweep_gap_fade_params.py` — sweep pattern (build_grid, load_candles, run_single)

**Key imports:**
```python
from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent
from trading_engine.backtest.cost_model import CostModel
from trading_engine.backtest.data_feed import HistoricalDataFeed
from trading_engine.backtest.engine import BacktestEngine
from trading_engine.backtest.portfolio import BacktestPortfolio
from trading_engine.backtest.simulated_broker import SimulatedBroker
from trading_engine.backtest.slippage_model import SlippageModel
```

**BacktestEngine constructor signature:**
```python
BacktestEngine(
    strategy=strategy,
    data_feed=feed,
    portfolio=portfolio,
    simulated_broker=broker,
    initial_cash=initial_cash,
    strategy_id=config.strategy_id,
    symbols=list(candles.keys()),
    parameters={},
)
```

**HistoricalDataFeed constructor:**
```python
HistoricalDataFeed(candles, interval=interval)  # candles: dict[str, pd.DataFrame]
```

**Bar fields:** `bar.symbol`, `bar.exchange`, `bar.timestamp`, `bar.open`, `bar.high`, `bar.low`, `bar.close`, `bar.volume`

**OrderIntent constructor:**
```python
OrderIntent(
    strategy_id=self.strategy_id,
    symbol=bar.symbol,
    exchange=bar.exchange,
    side="BUY",           # or "SELL"
    quantity=cfg.quantity,
    order_type="MARKET",
    product=cfg.product,
    reason="gc_long_entry",
)
```

**BacktestReport fields used in scripts:** `report.fills`, `report.strategy_id`, `report.start_time`, `report.end_time`, `report.symbols`, `report.initial_cash`, `report.final_equity`, `report.metrics`

**BacktestMetrics fields:** `m.total_return`, `m.total_pnl`, `m.total_fees`, `m.max_drawdown`, `m.win_rate`, `m.winning_trades`, `m.losing_trades`

**Side enum:** `from trading_engine.domain.enums import Side` → `Side.BUY`, `Side.SELL`

---

## Task 1: GapContinuationConfig + Strategy Skeleton

**Files:**
- Create: `src/trading_engine/strategies/gap_continuation.py`
- Create: `tests/unit/strategies/test_gap_continuation.py`

- [ ] **Step 1: Write failing config tests**

```python
# tests/unit/strategies/test_gap_continuation.py
"""Unit tests for GapContinuationStrategy."""

from __future__ import annotations

import sys
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.strategies.gap_continuation import (  # noqa: E402
    GapContinuationConfig,
    GapContinuationStrategy,
)


class TestGapContinuationConfig:
    def test_defaults_are_valid(self):
        cfg = GapContinuationConfig()
        assert cfg.strategy_id == "gap_cont_v1"
        assert cfg.min_gap_bps == 60.0
        assert cfg.max_gap_bps == 300.0
        assert cfg.continuation_trigger_bps == 20.0
        assert cfg.stop_loss_bps == 80.0
        assert cfg.target_bps is None
        assert cfg.allow_long_continuations is True
        assert cfg.allow_short_continuations is True

    def test_quantity_zero_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            GapContinuationConfig(quantity=0)

    def test_min_gap_zero_raises(self):
        with pytest.raises(ValueError, match="min_gap_bps"):
            GapContinuationConfig(min_gap_bps=0.0)

    def test_max_gap_not_greater_than_min_raises(self):
        with pytest.raises(ValueError, match="max_gap_bps"):
            GapContinuationConfig(min_gap_bps=100.0, max_gap_bps=100.0)

    def test_trigger_zero_raises(self):
        with pytest.raises(ValueError, match="continuation_trigger_bps"):
            GapContinuationConfig(continuation_trigger_bps=0.0)

    def test_stop_loss_zero_raises(self):
        with pytest.raises(ValueError, match="stop_loss_bps"):
            GapContinuationConfig(stop_loss_bps=0.0)

    def test_target_bps_zero_raises(self):
        with pytest.raises(ValueError, match="target_bps"):
            GapContinuationConfig(target_bps=0.0)

    def test_target_bps_positive_valid(self):
        cfg = GapContinuationConfig(target_bps=200.0)
        assert cfg.target_bps == 200.0

    def test_square_off_after_latest_entry(self):
        with pytest.raises(ValueError, match="square_off_time"):
            GapContinuationConfig(
                latest_entry_time=time(15, 15),
                square_off_time=time(10, 30),
            )

    def test_no_fills_on_first_day(self):
        """First day has no prior close — strategy must skip all entry signals."""
        cfg = GapContinuationConfig(
            min_gap_bps=50.0,
            max_gap_bps=500.0,
            continuation_trigger_bps=10.0,
            stop_loss_bps=200.0,
            allow_long_continuations=True,
            allow_short_continuations=True,
        )
        strategy = GapContinuationStrategy(config=cfg)

        from trading_engine.strategy.signals import Bar
        bar = Bar(
            symbol="TEST",
            exchange="NSE",
            timestamp=pd.Timestamp("2024-01-15 09:20:00"),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=5000,
        )
        intents = strategy.on_bar(bar, context=None)
        assert intents == []
```

- [ ] **Step 2: Run test to verify it fails**

```
cd /path/to/project
python3 -m pytest tests/unit/strategies/test_gap_continuation.py -v
```
Expected: `ImportError` — `gap_continuation` module does not exist yet.

- [ ] **Step 3: Create the strategy file with config and skeleton**

```python
# src/trading_engine/strategies/gap_continuation.py
"""Gap Continuation strategy (backtest-only, v1).

Intraday momentum strategy for NSE cash equities:
  1. Detect opening gap (opening price vs previous day's closing price).
  2. If abs(gap_bps) is within [min_gap_bps, max_gap_bps], qualify the gap.
  3. After entry_start_time, enter in the GAP DIRECTION when price confirms
     continuation_trigger_bps from the opening price.
     Gap-up  -> enter LONG  (price continues up).
     Gap-down -> enter SHORT (price continues down).
  4. Exit on stop-loss, fixed target (target_bps), or square-off at 15:15.

No live order placement. No broker API calls. Backtest use only.

Prior close tracking
---------------------
The strategy carries the previous day's closing price across session
boundaries.  The first trading day in any dataset is always skipped because
no prior close is available.

Gap detection
-------------
  gap_bps = (opening_price / prior_close - 1) * 10000
  Gap-up   (gap_bps > 0) -> enter LONG  if allow_long_continuations=True.
  Gap-down (gap_bps < 0) -> enter SHORT if allow_short_continuations=True.

Entry trigger
--------------
  Gap-up  long:  bar.close >= opening_price * (1 + continuation_trigger_bps/10000)
  Gap-down short: bar.close <= opening_price * (1 - continuation_trigger_bps/10000)

Exit
-----
  stop_loss_bps: fixed stop from entry price.
  target_bps:    fixed profit target from entry price (None = square-off only).
  square_off_time: forced exit at this time.

Exit reasons
-------------
  "gc_stop_loss"   -- stop-loss hit
  "gc_target"      -- fixed target reached
  "gc_square_off"  -- bar timestamp >= square_off_time
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal

from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent

_TEN_THOUSAND = Decimal("10000")
_ZERO = Decimal("0")
_ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class GapContinuationConfig:
    """Configuration for GapContinuationStrategy."""

    strategy_id: str = "gap_cont_v1"
    exchange: str = "NSE"
    product: str = "MIS"
    quantity: int = 10
    session_start: time = field(default_factory=lambda: time(9, 15))
    entry_start_time: time = field(default_factory=lambda: time(9, 20))
    latest_entry_time: time = field(default_factory=lambda: time(10, 30))
    square_off_time: time = field(default_factory=lambda: time(15, 15))
    min_gap_bps: float = 60.0
    max_gap_bps: float = 300.0
    continuation_trigger_bps: float = 20.0
    stop_loss_bps: float = 80.0
    target_bps: float | None = None
    max_trades_per_symbol_per_day: int = 1
    allow_long_continuations: bool = True
    allow_short_continuations: bool = True
    min_opening_volume: int | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.min_gap_bps <= 0:
            raise ValueError(f"min_gap_bps must be positive, got {self.min_gap_bps}")
        if self.max_gap_bps <= self.min_gap_bps:
            raise ValueError(
                f"max_gap_bps ({self.max_gap_bps}) must exceed min_gap_bps ({self.min_gap_bps})"
            )
        if self.continuation_trigger_bps <= 0:
            raise ValueError(
                f"continuation_trigger_bps must be positive, got {self.continuation_trigger_bps}"
            )
        if self.stop_loss_bps <= 0:
            raise ValueError(f"stop_loss_bps must be positive, got {self.stop_loss_bps}")
        if self.target_bps is not None and self.target_bps <= 0:
            raise ValueError(f"target_bps must be positive when set, got {self.target_bps}")
        if self.max_trades_per_symbol_per_day < 1:
            raise ValueError(
                f"max_trades_per_symbol_per_day must be >= 1, "
                f"got {self.max_trades_per_symbol_per_day}"
            )
        if self.square_off_time <= self.latest_entry_time:
            raise ValueError(
                f"square_off_time ({self.square_off_time}) must be after "
                f"latest_entry_time ({self.latest_entry_time})"
            )


# ---------------------------------------------------------------------------
# Per-symbol daily state
# ---------------------------------------------------------------------------


@dataclass
class _SymbolState:
    """Mutable per-symbol state for GapContinuationStrategy."""

    current_date: date | None = None

    # Inter-day carry -- NOT reset on new day
    prior_close: Decimal | None = None
    last_close: Decimal | None = None

    # Intraday gap analysis
    opening_bar_seen: bool = False
    opening_price: Decimal | None = None
    opening_volume: int = 0
    gap_bps: Decimal | None = None
    gap_qualified: bool = False
    gap_direction: str = ""  # "LONG" or "SHORT"

    # Position tracking
    in_position: bool = False
    position_side: str = ""  # "LONG" or "SHORT"
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None

    # Daily counters
    bars_seen_today: int = 0
    trades_taken_today: int = 0

    def reset(self, new_date: date) -> None:
        """Reset intraday state for a new trading day; carry prior close."""
        self.prior_close = self.last_close  # inter-day carry
        self.last_close = None
        self.current_date = new_date
        self.opening_bar_seen = False
        self.opening_price = None
        self.opening_volume = 0
        self.gap_bps = None
        self.gap_qualified = False
        self.gap_direction = ""
        self.in_position = False
        self.position_side = ""
        self.entry_price = None
        self.stop_price = None
        self.target_price = None
        self.bars_seen_today = 0
        self.trades_taken_today = 0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class GapContinuationStrategy(Strategy):
    """Gap Continuation strategy (backtest v1)."""

    def __init__(
        self,
        config: GapContinuationConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or GapContinuationConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _SymbolState] = {}

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        """Process one bar; return zero or more OrderIntents."""
        state = self._get_state(bar.symbol)
        bar_date = _bar_date(bar)
        bar_time = _bar_time(bar)

        if state.current_date != bar_date:
            state.reset(bar_date)

        state.last_close = bar.close
        state.bars_seen_today += 1

        if not state.opening_bar_seen and bar_time >= self._config.session_start:
            self._process_opening_bar(bar, state)

        intents: list[OrderIntent] = []

        if state.in_position:
            exit_intent = self._check_exit(bar, bar_time, state)
            if exit_intent is not None:
                intents.append(exit_intent)
                state.in_position = False
                state.position_side = ""
                return intents

        if self._can_enter(bar_time, state):
            entry_intent = self._check_entry(bar, state)
            if entry_intent is not None:
                intents.append(entry_intent)
                self._set_position_state(bar, state)

        return intents

    def _process_opening_bar(self, bar: Bar, state: _SymbolState) -> None:
        state.opening_bar_seen = True
        state.opening_price = bar.open
        state.opening_volume = bar.volume

        if state.prior_close is None or state.prior_close == _ZERO:
            return

        gap = (state.opening_price / state.prior_close - _ONE) * _TEN_THOUSAND
        state.gap_bps = gap

        cfg = self._config
        abs_gap = abs(gap)
        min_gap = Decimal(str(cfg.min_gap_bps))
        max_gap = Decimal(str(cfg.max_gap_bps))

        if abs_gap < min_gap or abs_gap > max_gap:
            return

        if cfg.min_opening_volume is not None and state.opening_volume < cfg.min_opening_volume:
            return

        if gap > _ZERO and cfg.allow_long_continuations:
            state.gap_qualified = True
            state.gap_direction = "LONG"
        elif gap < _ZERO and cfg.allow_short_continuations:
            state.gap_qualified = True
            state.gap_direction = "SHORT"

    def _can_enter(self, bar_time: time, state: _SymbolState) -> bool:
        cfg = self._config
        return (
            state.gap_qualified
            and not state.in_position
            and state.trades_taken_today < cfg.max_trades_per_symbol_per_day
            and bar_time >= cfg.entry_start_time
            and bar_time <= cfg.latest_entry_time
            and state.opening_price is not None
        )

    def _check_entry(self, bar: Bar, state: _SymbolState) -> OrderIntent | None:
        cfg = self._config
        opening = state.opening_price
        assert opening is not None
        trigger_factor = Decimal(str(cfg.continuation_trigger_bps)) / _TEN_THOUSAND

        if state.gap_direction == "LONG":
            trigger_price = opening * (_ONE + trigger_factor)
            if bar.close < trigger_price:
                return None
            return OrderIntent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side="BUY",
                quantity=cfg.quantity,
                order_type="MARKET",
                product=cfg.product,
                reason="gc_long_entry",
            )

        if state.gap_direction == "SHORT":
            trigger_price = opening * (_ONE - trigger_factor)
            if bar.close > trigger_price:
                return None
            return OrderIntent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side="SELL",
                quantity=cfg.quantity,
                order_type="MARKET",
                product=cfg.product,
                reason="gc_short_entry",
            )

        return None

    def _set_position_state(self, bar: Bar, state: _SymbolState) -> None:
        entry_price = bar.close
        cfg = self._config
        sl_factor = Decimal(str(cfg.stop_loss_bps)) / _TEN_THOUSAND

        state.in_position = True
        state.position_side = state.gap_direction
        state.entry_price = entry_price
        state.trades_taken_today += 1

        if state.position_side == "LONG":
            state.stop_price = entry_price * (_ONE - sl_factor)
            if cfg.target_bps is not None:
                state.target_price = entry_price * (_ONE + Decimal(str(cfg.target_bps)) / _TEN_THOUSAND)
        else:
            state.stop_price = entry_price * (_ONE + sl_factor)
            if cfg.target_bps is not None:
                state.target_price = entry_price * (_ONE - Decimal(str(cfg.target_bps)) / _TEN_THOUSAND)

    def _check_exit(self, bar: Bar, bar_time: time, state: _SymbolState) -> OrderIntent | None:
        assert state.stop_price is not None

        if state.position_side == "LONG":
            stop_hit = bar.low <= state.stop_price
            target_hit = (
                state.target_price is not None and bar.high >= state.target_price
            )
        else:
            stop_hit = bar.high >= state.stop_price
            target_hit = (
                state.target_price is not None and bar.low <= state.target_price
            )

        square_off_hit = bar_time >= self._config.square_off_time

        if stop_hit:
            return self._exit_intent(bar, state, "gc_stop_loss")
        if target_hit:
            return self._exit_intent(bar, state, "gc_target")
        if square_off_hit:
            return self._exit_intent(bar, state, "gc_square_off")
        return None

    def _exit_intent(self, bar: Bar, state: _SymbolState, reason: str) -> OrderIntent:
        side = "SELL" if state.position_side == "LONG" else "BUY"
        return OrderIntent(
            strategy_id=self.strategy_id,
            symbol=bar.symbol,
            exchange=bar.exchange,
            side=side,
            quantity=self._config.quantity,
            order_type="MARKET",
            product=self._config.product,
            reason=reason,
        )

    def _get_state(self, symbol: str) -> _SymbolState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolState()
        return self._states[symbol]


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _bar_time(bar: Bar) -> time:
    ts = bar.timestamp
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo
        ts = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ts.time()


def _bar_date(bar: Bar) -> date:
    ts = bar.timestamp
    if ts.tzinfo is not None:
        from zoneinfo import ZoneInfo
        ts = ts.astimezone(ZoneInfo("Asia/Kolkata"))
    return ts.date()
```

- [ ] **Step 4: Run config tests to verify they pass**

```
python3 -m pytest tests/unit/strategies/test_gap_continuation.py::TestGapContinuationConfig -v
```
Expected: 10 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/trading_engine/strategies/gap_continuation.py tests/unit/strategies/test_gap_continuation.py
git commit -m "feat: add GapContinuationConfig and strategy skeleton"
```

---

## Task 2: Prior Close Tracking Tests

**Files:**
- Modify: `tests/unit/strategies/test_gap_continuation.py` (append)

- [ ] **Step 1: Write failing prior-close tests**

Append to `tests/unit/strategies/test_gap_continuation.py`:

```python
def _make_bar(
    symbol: str = "TEST",
    timestamp: str = "2024-01-15 09:15:00",
    open_: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: int = 5000,
) -> "Bar":
    from trading_engine.strategy.signals import Bar
    return Bar(
        symbol=symbol,
        exchange="NSE",
        timestamp=pd.Timestamp(timestamp),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
    )


def _permissive_cfg(**kwargs) -> GapContinuationConfig:
    defaults = dict(
        min_gap_bps=50.0,
        max_gap_bps=1000.0,
        continuation_trigger_bps=10.0,
        stop_loss_bps=200.0,
        target_bps=None,
        allow_long_continuations=True,
        allow_short_continuations=True,
    )
    defaults.update(kwargs)
    return GapContinuationConfig(**defaults)


class TestPriorCloseTracking:
    def test_prior_close_is_none_on_first_day(self):
        strategy = GapContinuationStrategy(config=_permissive_cfg())
        bar = _make_bar(timestamp="2024-01-15 09:15:00", close=100.0)
        strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.prior_close is None

    def test_prior_close_set_after_day_rollover(self):
        strategy = GapContinuationStrategy(config=_permissive_cfg())
        # Day 1: close = 107
        bar1 = _make_bar(timestamp="2024-01-15 09:15:00", close=107.0)
        strategy.on_bar(bar1, context=None)
        # Day 2: opening bar triggers reset — prior_close should be 107
        bar2 = _make_bar(timestamp="2024-01-16 09:15:00", open_=100.0, close=100.0)
        strategy.on_bar(bar2, context=None)
        state = strategy._states["TEST"]
        assert state.prior_close == Decimal("107.0")

    def test_prior_close_updates_each_day(self):
        strategy = GapContinuationStrategy(config=_permissive_cfg())
        for close_val, ts in [
            (100.0, "2024-01-15 09:15:00"),
            (110.0, "2024-01-16 09:15:00"),
            (120.0, "2024-01-17 09:15:00"),
        ]:
            bar = _make_bar(timestamp=ts, close=close_val)
            strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.prior_close == Decimal("110.0")
```

- [ ] **Step 2: Run to verify it passes** (code is already in place)

```
python3 -m pytest tests/unit/strategies/test_gap_continuation.py::TestPriorCloseTracking -v
```
Expected: 3 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/strategies/test_gap_continuation.py
git commit -m "test: add prior close tracking tests for GapContinuationStrategy"
```

---

## Task 3: Gap Detection and Entry Logic Tests

**Files:**
- Modify: `tests/unit/strategies/test_gap_continuation.py` (append)

- [ ] **Step 1: Write gap detection and entry tests**

Append to `tests/unit/strategies/test_gap_continuation.py`:

```python
class TestGapDetection:
    def _two_day_strategy(self, **cfg_kwargs):
        """Returns strategy with day-1 close=100 already fed in."""
        strategy = GapContinuationStrategy(config=_permissive_cfg(**cfg_kwargs))
        bar_day1 = _make_bar(timestamp="2024-01-15 09:15:00", close=100.0)
        strategy.on_bar(bar_day1, context=None)
        return strategy

    def test_gap_below_min_not_qualified(self):
        strategy = self._two_day_strategy(min_gap_bps=100.0)
        # gap = (103/100 - 1)*10000 = 300 bps... wait, let's do a small gap
        # open=100.5 → gap = 50 bps < min_gap_bps=100
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=100.5, close=100.5)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_above_max_not_qualified(self):
        strategy = self._two_day_strategy(max_gap_bps=200.0)
        # open=103 → gap = 300 bps > max_gap_bps=200
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=103.0, close=103.0)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_up_sets_long_direction(self):
        strategy = self._two_day_strategy()
        # prior_close=100, open=101 → gap=100 bps (within [50,1000])
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0)
        strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.gap_direction == "LONG"

    def test_gap_down_sets_short_direction(self):
        strategy = self._two_day_strategy()
        # prior_close=100, open=99 → gap=-100 bps
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0)
        strategy.on_bar(bar, context=None)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.gap_direction == "SHORT"

    def test_allow_long_false_blocks_gap_up(self):
        strategy = self._two_day_strategy(allow_long_continuations=False)
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False

    def test_allow_short_false_blocks_gap_down(self):
        strategy = self._two_day_strategy(allow_short_continuations=False)
        bar = _make_bar(timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0)
        strategy.on_bar(bar, context=None)
        assert strategy._states["TEST"].gap_qualified is False


class TestEntryLogic:
    def _setup_with_gap(self, gap_direction: str, **cfg_kwargs):
        """Returns strategy after day-1 close=100 and day-2 opening bar."""
        cfg = _permissive_cfg(**cfg_kwargs)
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        if gap_direction == "LONG":
            opening_bar = _make_bar(
                timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0
            )  # gap-up 100 bps
        else:
            opening_bar = _make_bar(
                timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0
            )  # gap-down 100 bps
        strategy.on_bar(opening_bar, context=None)
        return strategy

    def test_long_entry_when_close_above_trigger(self):
        # opening=101, trigger=10bps → trigger_price=101.101
        # bar.close=101.2 >= 101.101 → LONG entry
        strategy = self._setup_with_gap("LONG", continuation_trigger_bps=10.0)
        bar = _make_bar(timestamp="2024-01-16 09:20:00", open_=101.0, high=101.3, low=100.9, close=101.2)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gc_long_entry"

    def test_long_no_entry_below_trigger(self):
        # opening=101, trigger=10bps → trigger_price=101.101
        # bar.close=101.0 < 101.101 → no entry
        strategy = self._setup_with_gap("LONG", continuation_trigger_bps=10.0)
        bar = _make_bar(timestamp="2024-01-16 09:20:00", close=101.0)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_short_entry_when_close_below_trigger(self):
        # opening=99, trigger=10bps → trigger_price=98.901
        # bar.close=98.8 <= 98.901 → SHORT entry
        strategy = self._setup_with_gap("SHORT", continuation_trigger_bps=10.0)
        bar = _make_bar(timestamp="2024-01-16 09:20:00", open_=99.0, high=99.1, low=98.7, close=98.8)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gc_short_entry"

    def test_no_entry_before_entry_start_time(self):
        # entry_start_time=09:20, bar is at 09:19 → no entry
        cfg = _permissive_cfg(continuation_trigger_bps=10.0)
        cfg = GapContinuationConfig(
            min_gap_bps=50.0, max_gap_bps=1000.0, continuation_trigger_bps=10.0,
            stop_loss_bps=200.0, entry_start_time=time(9, 20),
        )
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0), context=None)
        # Bar at 09:19 with close above trigger
        bar = _make_bar(timestamp="2024-01-16 09:19:00", close=101.2)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_no_entry_after_latest_entry_time(self):
        cfg = GapContinuationConfig(
            min_gap_bps=50.0, max_gap_bps=1000.0, continuation_trigger_bps=10.0,
            stop_loss_bps=200.0, latest_entry_time=time(10, 30),
        )
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0), context=None)
        # Bar at 10:31
        bar = _make_bar(timestamp="2024-01-16 10:31:00", close=101.5)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_max_trades_per_day_respected(self):
        strategy = self._setup_with_gap("LONG", continuation_trigger_bps=10.0, max_trades_per_symbol_per_day=1)
        bar1 = _make_bar(timestamp="2024-01-16 09:20:00", close=101.2)
        strategy.on_bar(bar1, context=None)
        # Force exit so in_position=False
        state = strategy._states["TEST"]
        state.in_position = False
        # Second entry attempt
        bar2 = _make_bar(timestamp="2024-01-16 09:30:00", close=101.5)
        intents = strategy.on_bar(bar2, context=None)
        assert intents == []
```

- [ ] **Step 2: Run tests**

```
python3 -m pytest tests/unit/strategies/test_gap_continuation.py::TestGapDetection tests/unit/strategies/test_gap_continuation.py::TestEntryLogic -v
```
Expected: 12 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/strategies/test_gap_continuation.py
git commit -m "test: add gap detection and entry logic tests for GapContinuationStrategy"
```

---

## Task 4: Exit Logic Tests

**Files:**
- Modify: `tests/unit/strategies/test_gap_continuation.py` (append)

- [ ] **Step 1: Write exit logic tests**

Append to `tests/unit/strategies/test_gap_continuation.py`:

```python
class TestExitLogic:
    def _strategy_in_long_position(self, entry_price: float = 101.2, **cfg_kwargs) -> tuple:
        """Returns (strategy, state) with a LONG position open at entry_price."""
        cfg = _permissive_cfg(continuation_trigger_bps=10.0, **cfg_kwargs)
        strategy = GapContinuationStrategy(config=cfg)
        # Day 1
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        # Day 2 opening bar (gap-up)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:15:00", open_=101.0, close=101.0), context=None)
        # Entry bar
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:20:00", close=Decimal(str(entry_price))), context=None)
        state = strategy._states["TEST"]
        assert state.in_position is True
        assert state.position_side == "LONG"
        return strategy, state

    def _strategy_in_short_position(self, entry_price: float = 98.8, **cfg_kwargs) -> tuple:
        """Returns (strategy, state) with a SHORT position open at entry_price."""
        cfg = _permissive_cfg(continuation_trigger_bps=10.0, **cfg_kwargs)
        strategy = GapContinuationStrategy(config=cfg)
        strategy.on_bar(_make_bar(timestamp="2024-01-15 09:15:00", close=100.0), context=None)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:15:00", open_=99.0, close=99.0), context=None)
        strategy.on_bar(_make_bar(timestamp="2024-01-16 09:20:00", close=Decimal(str(entry_price))), context=None)
        state = strategy._states["TEST"]
        assert state.in_position is True
        assert state.position_side == "SHORT"
        return strategy, state

    def test_long_stop_loss_triggers(self):
        # entry=101.2, stop_loss=200bps → stop=101.2*(1-0.02)=99.176
        # bar.low=99.0 <= 99.176 → stop hit
        strategy, state = self._strategy_in_long_position(entry_price=101.2, stop_loss_bps=200.0)
        bar = _make_bar(timestamp="2024-01-16 09:25:00", open_=101.0, high=101.1, low=99.0, close=99.1)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gc_stop_loss"

    def test_short_stop_loss_triggers(self):
        # entry=98.8, stop_loss=200bps → stop=98.8*(1+0.02)=100.776
        # bar.high=101.0 >= 100.776 → stop hit
        strategy, state = self._strategy_in_short_position(entry_price=98.8, stop_loss_bps=200.0)
        bar = _make_bar(timestamp="2024-01-16 09:25:00", open_=99.0, high=101.0, low=98.5, close=100.0)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gc_stop_loss"

    def test_long_target_triggers(self):
        # entry=101.2, target_bps=100 → target=101.2*(1+0.01)=102.212
        # bar.high=102.5 >= 102.212 → target hit
        strategy, state = self._strategy_in_long_position(
            entry_price=101.2, stop_loss_bps=200.0, target_bps=100.0
        )
        bar = _make_bar(timestamp="2024-01-16 09:25:00", open_=101.5, high=102.5, low=101.0, close=102.0)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gc_target"

    def test_short_target_triggers(self):
        # entry=98.8, target_bps=100 → target=98.8*(1-0.01)=97.812
        # bar.low=97.5 <= 97.812 → target hit
        strategy, state = self._strategy_in_short_position(
            entry_price=98.8, stop_loss_bps=200.0, target_bps=100.0
        )
        bar = _make_bar(timestamp="2024-01-16 09:25:00", open_=98.5, high=98.9, low=97.5, close=97.8)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gc_target"

    def test_no_target_when_target_bps_none(self):
        # target_bps=None → no target exit, bar with high=200 still holds position
        strategy, state = self._strategy_in_long_position(
            entry_price=101.2, stop_loss_bps=10.0, target_bps=None
        )
        # Reset stop so it doesn't trigger
        state.stop_price = Decimal("0.01")
        bar = _make_bar(timestamp="2024-01-16 09:25:00", open_=101.5, high=200.0, low=101.0, close=150.0)
        intents = strategy.on_bar(bar, context=None)
        assert intents == []

    def test_square_off_at_15_15(self):
        strategy, state = self._strategy_in_long_position(entry_price=101.2, stop_loss_bps=200.0)
        bar = _make_bar(timestamp="2024-01-16 15:15:00", close=101.5)
        intents = strategy.on_bar(bar, context=None)
        assert len(intents) == 1
        assert intents[0].reason == "gc_square_off"

    def test_stop_takes_priority_over_target(self):
        # Both stop and target triggered on same bar → stop wins
        strategy, state = self._strategy_in_long_position(
            entry_price=101.2, stop_loss_bps=200.0, target_bps=100.0
        )
        # stop=99.176, target=102.212
        # bar: low=99.0 (stop), high=102.5 (target) — stop checked first
        bar = _make_bar(timestamp="2024-01-16 09:25:00", open_=100.0, high=102.5, low=99.0, close=100.0)
        intents = strategy.on_bar(bar, context=None)
        assert intents[0].reason == "gc_stop_loss"

    def test_position_cleared_after_exit(self):
        strategy, state = self._strategy_in_long_position(entry_price=101.2, stop_loss_bps=200.0)
        bar = _make_bar(timestamp="2024-01-16 15:15:00", close=101.5)
        strategy.on_bar(bar, context=None)
        assert state.in_position is False
        assert state.position_side == ""
```

- [ ] **Step 2: Run tests**

```
python3 -m pytest tests/unit/strategies/test_gap_continuation.py::TestExitLogic -v
```
Expected: 8 PASSED.

- [ ] **Step 3: Run full unit test file**

```
python3 -m pytest tests/unit/strategies/test_gap_continuation.py -v
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/strategies/test_gap_continuation.py
git commit -m "test: add exit logic tests for GapContinuationStrategy"
```

---

## Task 5: Integration Backtest Test

**Files:**
- Create: `tests/unit/strategies/test_gap_continuation_backtest.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/unit/strategies/test_gap_continuation_backtest.py
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
    """Two-day candle sequence that produces one gap-continuation SHORT trade.

    Day 1 (2024-01-15): single bar, close=100 (prior_close for day 2).
    Day 2 (2024-01-16):
      09:15 — opening bar: open=102 (~200 bps gap-up, within [50, 500]).
               gap_direction="LONG", gap_qualified=True.
      09:20 — entry bar: close=102.3 >= trigger=102*(1+0.001)=102.102 -> LONG entry.
      09:25–15:10 — bars that don't hit stop (stop=102.3*(1-0.02)=100.254)
                    or target (none by default → square-off).
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
    # 09:30–15:10: bars that don't hit stop (<100.254) or target (no target set)
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
```

- [ ] **Step 2: Run integration tests**

```
python3 -m pytest tests/unit/strategies/test_gap_continuation_backtest.py -v
```
Expected: 6 PASSED.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/strategies/test_gap_continuation_backtest.py
git commit -m "test: add BacktestEngine integration tests for GapContinuationStrategy"
```

---

## Task 6: Runner Script

**Files:**
- Create: `scripts/run_gap_continuation_backtest.py`

- [ ] **Step 1: Create the runner script**

```python
# scripts/run_gap_continuation_backtest.py
"""Run Gap Continuation backtest on locally stored Parquet candle data.

No broker API calls are made.  No live orders are placed.
Reads candle files from data/candles/NSE/{SYMBOL}/{interval}.parquet.

Usage:
    python3 scripts/run_gap_continuation_backtest.py
    python3 scripts/run_gap_continuation_backtest.py --symbols INDHOTEL MPHASIS
    python3 scripts/run_gap_continuation_backtest.py --target-bps 150
    python3 scripts/run_gap_continuation_backtest.py --long-only
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

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

_DEFAULT_SYMBOLS = [
    "INDHOTEL", "MPHASIS", "COFORGE", "LTTS", "BANDHANBNK",
    "IDFCFIRSTB", "TATACOMM", "ABFRL", "NYKAA",
]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT = ROOT / "reports" / "gap_continuation_report.json"


def _build_config(args: argparse.Namespace) -> GapContinuationConfig:
    return GapContinuationConfig(
        strategy_id="gap_cont_v1",
        quantity=args.quantity,
        min_gap_bps=args.min_gap_bps,
        max_gap_bps=args.max_gap_bps,
        continuation_trigger_bps=args.continuation_trigger_bps,
        stop_loss_bps=args.stop_loss_bps,
        target_bps=args.target_bps,
        allow_long_continuations=not args.short_only,
        allow_short_continuations=not args.long_only,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Gap Continuation backtest on local Parquet data."
    )
    parser.add_argument("--symbols", nargs="+", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument("--initial-cash", dest="initial_cash", type=float, default=float(_DEFAULT_INITIAL_CASH))
    parser.add_argument("--quantity", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument("--min-gap-bps", dest="min_gap_bps", type=float, default=60.0)
    parser.add_argument("--max-gap-bps", dest="max_gap_bps", type=float, default=300.0)
    parser.add_argument("--continuation-trigger-bps", dest="continuation_trigger_bps", type=float, default=20.0)
    parser.add_argument("--stop-loss-bps", dest="stop_loss_bps", type=float, default=80.0)
    parser.add_argument("--target-bps", dest="target_bps", type=float, default=None)
    parser.add_argument("--long-only", dest="long_only", action="store_true", default=False,
                        help="Only take gap-up LONG continuations")
    parser.add_argument("--short-only", dest="short_only", action="store_true", default=False,
                        help="Only take gap-down SHORT continuations")
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    initial_cash = Decimal(str(args.initial_cash))
    output_path = Path(args.output)
    interval = args.interval

    print("\nGap Continuation Backtest")
    print(f"  Requested symbols:          {args.symbols}")
    print(f"  Data dir:                   {data_dir}")
    print(f"  Interval:                   {interval}")
    print(f"  Initial cash:               {initial_cash}")
    print(f"  Quantity:                   {args.quantity}")
    print(f"  min_gap_bps:                {args.min_gap_bps}")
    print(f"  max_gap_bps:                {args.max_gap_bps}")
    print(f"  continuation_trigger_bps:   {args.continuation_trigger_bps}")
    print(f"  stop_loss_bps:              {args.stop_loss_bps}")
    print(f"  target_bps:                 {args.target_bps}")
    print(f"  long_only:                  {args.long_only}")
    print(f"  short_only:                 {args.short_only}")

    candles: dict[str, pd.DataFrame] = {}
    for symbol in args.symbols:
        path = data_dir / "candles" / "NSE" / symbol / f"{interval}.parquet"
        if not path.exists():
            print(f"  [skip] No data file for {symbol} at {path}")
            continue
        try:
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] Failed to read {symbol}: {exc}")

    if not candles:
        print("\nNo candle data found. Download historical data first, then re-run.\n")
        sys.exit(0)

    print(f"\nLoaded symbols: {list(candles.keys())}")

    config = _build_config(args)
    strategy = GapContinuationStrategy(config=config)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed(candles, interval=interval)

    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=config.strategy_id,
        symbols=list(candles.keys()),
        parameters={
            "interval": interval,
            "min_gap_bps": config.min_gap_bps,
            "max_gap_bps": config.max_gap_bps,
            "continuation_trigger_bps": config.continuation_trigger_bps,
            "stop_loss_bps": config.stop_loss_bps,
            "target_bps": str(config.target_bps),
        },
    )

    print(f"\nRunning backtest on {list(candles.keys())} ...")
    report = engine.run()

    m = report.metrics
    print(f"\n{'=' * 55}")
    print(f"Strategy : {report.strategy_id}")
    print(f"Period   : {report.start_time} -> {report.end_time}")
    print(f"Symbols  : {report.symbols}")
    print(f"Fills    : {len(report.fills)}")
    print(f"Equity   : {report.initial_cash} -> {report.final_equity}")
    print(f"Return   : {m.total_return:.4f}  ({m.total_pnl:+.2f} INR)")
    print(f"Max DD   : {m.max_drawdown:.4f}")
    print(f"Win rate : {m.win_rate:.4f}  ({m.winning_trades}W / {m.losing_trades}L)")
    print(f"Fees     : {m.total_fees:.2f}")
    print(f"{'=' * 55}\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    report.save_json(output_path)
    print(f"Report saved to {output_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the script against mid-cap data**

```
python3 scripts/run_gap_continuation_backtest.py
```
Expected: prints summary table with fills > 0. No crash.

- [ ] **Step 3: Also run long-only and short-only to see directional breakdown**

```
python3 scripts/run_gap_continuation_backtest.py --long-only
python3 scripts/run_gap_continuation_backtest.py --short-only
```

- [ ] **Step 4: Commit**

```bash
git add scripts/run_gap_continuation_backtest.py
git commit -m "feat: add run_gap_continuation_backtest.py runner script"
```

---

## Task 7: Parameter Sweep Script

**Files:**
- Create: `scripts/sweep_gap_continuation_params.py`
- Create: `tests/unit/scripts/test_gap_continuation_scripts.py`

- [ ] **Step 1: Write tests first**

```python
# tests/unit/scripts/test_gap_continuation_scripts.py
"""Tests for sweep_gap_continuation_params.py."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from sweep_gap_continuation_params import build_grid, load_candles, PARAM_GRID  # noqa: E402


class TestBuildGrid:
    def test_returns_list_of_dicts(self):
        combos = build_grid()
        assert isinstance(combos, list)
        assert all(isinstance(c, dict) for c in combos)

    def test_full_grid_count(self):
        # 4 * 3 * 3 * 3 = 108 combinations
        combos = build_grid()
        expected = 4 * 3 * 3 * 3
        assert len(combos) == expected

    def test_max_combinations_limits(self):
        combos = build_grid(max_combinations=10)
        assert len(combos) == 10

    def test_each_combo_has_required_keys(self):
        combos = build_grid(max_combinations=1)
        required = {"min_gap_bps", "max_gap_bps", "continuation_trigger_bps", "stop_loss_bps"}
        assert required.issubset(combos[0].keys())

    def test_custom_grid(self):
        grid = {"min_gap_bps": [50, 100], "stop_loss_bps": [80, 120]}
        combos = build_grid(grid=grid)
        assert len(combos) == 4


class TestLoadCandles:
    def test_missing_symbol_skipped(self, tmp_path):
        candles = load_candles(["NONEXISTENT"], tmp_path, "minute")
        assert "NONEXISTENT" not in candles

    def test_loads_existing_parquet(self, tmp_path):
        sym_dir = tmp_path / "candles" / "NSE" / "FAKE"
        sym_dir.mkdir(parents=True)
        df = pd.DataFrame({
            "timestamp": [pd.Timestamp("2024-01-15 09:15:00")],
            "open": [100.0], "high": [101.0], "low": [99.0],
            "close": [100.5], "volume": [1000],
        })
        df.to_parquet(sym_dir / "minute.parquet")
        candles = load_candles(["FAKE"], tmp_path, "minute")
        assert "FAKE" in candles
        assert len(candles["FAKE"]) == 1


class TestNoLiveTradingInScripts:
    def test_no_zerodha_in_sweep_script(self):
        source = (ROOT / "scripts" / "sweep_gap_continuation_params.py").read_text()
        assert "zerodha" not in source.lower()
        assert "kite" not in source.lower()

    def test_no_dotenv_in_sweep_script(self):
        source = (ROOT / "scripts" / "sweep_gap_continuation_params.py").read_text()
        assert "load_dotenv" not in source
        assert "import dotenv" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

```
python3 -m pytest tests/unit/scripts/test_gap_continuation_scripts.py -v
```
Expected: `ImportError` — sweep script doesn't exist yet.

- [ ] **Step 3: Create the sweep script**

```python
# scripts/sweep_gap_continuation_params.py
"""Gap Continuation parameter sweep.

Runs BacktestEngine over a grid of GapContinuationConfig parameters using
locally stored Parquet candle data. Results are saved to CSV and JSON.

No live trading. No broker API calls. No credentials required.

Usage:
    python3 scripts/sweep_gap_continuation_params.py
    python3 scripts/sweep_gap_continuation_params.py --fast
    python3 scripts/sweep_gap_continuation_params.py --max-combinations 50
    python3 scripts/sweep_gap_continuation_params.py --symbols INDHOTEL MPHASIS

WARNING: all results are IN-SAMPLE only.
"""

from __future__ import annotations

import argparse
import math
import sys
from decimal import Decimal
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

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

_DEFAULT_SYMBOLS = [
    "INDHOTEL", "MPHASIS", "COFORGE", "LTTS", "BANDHANBNK",
    "IDFCFIRSTB", "TATACOMM", "ABFRL", "NYKAA",
]
_FAST_SYMBOLS = ["INDHOTEL", "MPHASIS", "COFORGE"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Parameter grid — 4 * 3 * 3 * 3 = 108 total combinations.
# ---------------------------------------------------------------------------

PARAM_GRID: dict[str, list] = {
    "min_gap_bps": [40, 60, 80, 120],
    "max_gap_bps": [200, 300, 500],
    "continuation_trigger_bps": [10, 20, 40],
    "stop_loss_bps": [60, 80, 120],
}


def build_grid(
    grid: dict[str, list] | None = None,
    max_combinations: int | None = None,
) -> list[dict]:
    """Return list of parameter dicts for the Cartesian product of the grid."""
    g = grid if grid is not None else PARAM_GRID
    keys = list(g.keys())
    combos = [dict(zip(keys, combo, strict=True)) for combo in product(*g.values())]
    if max_combinations is not None and max_combinations < len(combos):
        combos = combos[:max_combinations]
    return combos


def load_candles(
    symbols: list[str],
    data_dir: Path,
    interval: str,
) -> dict[str, pd.DataFrame]:
    """Load Parquet candle files; skip missing or unreadable symbols."""
    candles: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = data_dir / "candles" / "NSE" / symbol / f"{interval}.parquet"
        if not path.exists():
            print(f"  [skip] No data file for {symbol} at {path}")
            continue
        try:
            df = pd.read_parquet(path)
            candles[symbol] = df
            print(f"  Loaded {symbol}: {len(df)} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] Failed to read {symbol}: {exc}")
    return candles


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def run_single(
    candles: dict[str, pd.DataFrame],
    params: dict,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
    run_index: int = 0,
) -> dict:
    """Run one backtest with the given params; return a result row dict."""
    try:
        cfg = GapContinuationConfig(
            strategy_id=f"gc_sweep_{run_index}",
            quantity=quantity,
            min_gap_bps=float(params["min_gap_bps"]),
            max_gap_bps=float(params["max_gap_bps"]),
            continuation_trigger_bps=float(params["continuation_trigger_bps"]),
            stop_loss_bps=float(params["stop_loss_bps"]),
        )
    except ValueError as exc:
        return {**params, "error": str(exc), "total_pnl": None, "trade_count": None}

    strategy = GapContinuationStrategy(config=cfg)
    portfolio = BacktestPortfolio(initial_cash=initial_cash)
    cost_model = CostModel()
    slippage_model = SlippageModel(bps=Decimal("2"))
    broker = SimulatedBroker(portfolio, cost_model, slippage_model)
    feed = HistoricalDataFeed(candles, interval=interval)
    engine = BacktestEngine(
        strategy=strategy,
        data_feed=feed,
        portfolio=portfolio,
        simulated_broker=broker,
        initial_cash=initial_cash,
        strategy_id=cfg.strategy_id,
        symbols=list(candles.keys()),
        parameters={k: str(v) for k, v in params.items()},
    )

    report = engine.run()
    m = report.metrics
    trade_count = m.winning_trades + m.losing_trades

    return {
        **params,
        "error": None,
        "total_return": _safe_float(m.total_return),
        "total_pnl": _safe_float(m.total_pnl),
        "total_fees": _safe_float(m.total_fees),
        "max_drawdown": _safe_float(m.max_drawdown),
        "win_rate": _safe_float(m.win_rate),
        "trade_count": trade_count,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gap Continuation parameter sweep.")
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument("--initial-cash", dest="initial_cash", type=float, default=float(_DEFAULT_INITIAL_CASH))
    parser.add_argument("--quantity", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument("--fast", action="store_true", default=False,
                        help="Fast mode: 3 symbols, up to 30 combinations")
    parser.add_argument("--max-combinations", dest="max_combinations", type=int, default=None)
    parser.add_argument("--output-dir", dest="output_dir", default=str(_DEFAULT_OUTPUT_DIR))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    initial_cash = Decimal(str(args.initial_cash))
    interval = args.interval

    symbols = args.symbols
    max_combinations = args.max_combinations
    if args.fast:
        symbols = symbols or _FAST_SYMBOLS
        max_combinations = max_combinations or 30

    symbols = symbols or _DEFAULT_SYMBOLS

    print("\nGap Continuation Parameter Sweep")
    print(f"  Symbols:          {symbols}")
    print(f"  Data dir:         {data_dir}")
    print(f"  Fast mode:        {args.fast}")
    print(f"  Max combinations: {max_combinations}")

    candles = load_candles(symbols, data_dir, interval)
    if not candles:
        print("\nNo candle data found. Download historical data first, then re-run.\n")
        sys.exit(0)

    combos = build_grid(max_combinations=max_combinations)
    print(f"\nRunning {len(combos)} combinations on {list(candles.keys())} ...")

    results = []
    for i, params in enumerate(combos):
        row = run_single(candles, params, initial_cash, args.quantity, interval, run_index=i)
        results.append(row)
        if (i + 1) % 10 == 0 or (i + 1) == len(combos):
            print(f"  {i + 1}/{len(combos)} done")

    df_results = pd.DataFrame(results)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "gap_continuation_sweep_results.csv"
    json_path = output_dir / "gap_continuation_sweep_results.json"
    df_results.to_csv(csv_path, index=False)
    df_results.to_json(json_path, orient="records", indent=2)

    print(f"\nSaved: {csv_path}")
    print(f"Saved: {json_path}")

    valid = df_results[df_results["error"].isna() & df_results["total_pnl"].notna()]
    if valid.empty:
        print("\nNo valid results to rank.")
        return

    top10 = valid.nlargest(10, "total_pnl")
    print(f"\n{'=' * 60}")
    print("Top 10 combos by total_pnl:")
    print(
        top10[[
            "min_gap_bps", "max_gap_bps", "continuation_trigger_bps",
            "stop_loss_bps", "total_pnl", "win_rate", "trade_count",
        ]].to_string(index=False)
    )
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all script tests**

```
python3 -m pytest tests/unit/scripts/test_gap_continuation_scripts.py -v
```
Expected: 9 PASSED.

- [ ] **Step 5: Run full test suite to confirm nothing broken**

```
python3 -m pytest --tb=short -q
```
Expected: previous pass count + ~50 new tests, ≤3 pre-existing failures.

- [ ] **Step 6: Commit**

```bash
git add scripts/sweep_gap_continuation_params.py tests/unit/scripts/test_gap_continuation_scripts.py
git commit -m "feat: add sweep_gap_continuation_params.py and script tests"
```

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Gap-up → LONG, gap-down → SHORT (Task 1 strategy logic)
- ✅ Continuation trigger (not fade trigger) — price moves further in gap direction (Tasks 1, 3)
- ✅ Prior close carry across days (Task 2)
- ✅ Stop-loss, fixed target (target_bps), square-off exits (Task 4)
- ✅ Short selling works (BacktestPortfolio was fixed in this session before this plan)
- ✅ Integration test with BacktestEngine (Task 5)
- ✅ Runner script with CLI flags (Task 6)
- ✅ Parameter sweep with CSV/JSON output and top-10 ranking (Task 7)

**Placeholder scan:** None found — all steps have concrete code.

**Type consistency:**
- `continuation_trigger_bps` used consistently across config, strategy, tests, runner, sweep ✅
- `gap_direction` (not `fade_direction`) used throughout ✅
- `gc_long_entry`, `gc_short_entry`, `gc_stop_loss`, `gc_target`, `gc_square_off` reasons consistent ✅
- `target_price` (not `dynamic_target`) used in `_SymbolState` and `_check_exit` ✅
