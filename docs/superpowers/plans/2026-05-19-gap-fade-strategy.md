# Gap Fade Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a Gap Fade to VWAP / Prior Close mean-reversion strategy with backtest scripts and a parameter sweep, backtest-only with no live trading.

**Architecture:** A new `GapFadeStrategy` follows the exact same pattern as `FirstHourMomentumStrategy` — a `GapFadeConfig` dataclass, a `_SymbolState` dataclass with inter-day prior-close carry, and a `GapFadeStrategy(Strategy)` class. Two scripts follow the existing `run_first_hour_momentum_backtest.py` and `sweep_first_hour_momentum_params.py` patterns. Tests follow `tests/unit/strategies/test_first_hour_momentum.py` and `tests/unit/strategies/test_first_hour_momentum_backtest.py` patterns.

**Tech Stack:** Python 3.11+, pandas, Decimal arithmetic, pytest, ruff, existing `trading_engine.backtest.*` infrastructure.

---

## File Map

| File | Create/Modify | Purpose |
|------|--------------|---------|
| `src/trading_engine/strategies/gap_fade.py` | Create | `GapFadeConfig`, `_SymbolState`, `GapFadeStrategy` |
| `tests/unit/strategies/test_gap_fade.py` | Create | Unit tests for config validation + strategy logic |
| `tests/unit/strategies/test_gap_fade_backtest.py` | Create | BacktestEngine integration test |
| `scripts/run_gap_fade_backtest.py` | Create | CLI backtest runner |
| `scripts/sweep_gap_fade_params.py` | Create | Parameter sweep script |
| `tests/unit/scripts/test_gap_fade_scripts.py` | Create | Unit tests for script functions |
| `README.md` | Modify | Add Gap Fade section |

---

### Task 1: GapFadeConfig + _SymbolState + skeleton GapFadeStrategy

**Files:**
- Create: `src/trading_engine/strategies/gap_fade.py`
- Create: `tests/unit/strategies/test_gap_fade.py`

- [ ] **Step 1: Write the failing tests for GapFadeConfig**

```python
# tests/unit/strategies/test_gap_fade.py
"""Unit tests for GapFadeStrategy."""

from __future__ import annotations

import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.strategies.gap_fade import GapFadeConfig, GapFadeStrategy  # noqa: E402


class TestGapFadeConfig:
    def test_default_config_is_valid(self):
        cfg = GapFadeConfig()
        assert cfg.strategy_id == "gap_fade_v1"
        assert cfg.min_gap_bps == 60.0
        assert cfg.max_gap_bps == 300.0
        assert cfg.fade_trigger_bps == 20.0
        assert cfg.stop_loss_bps == 80.0
        assert cfg.target_mode == "vwap"

    def test_quantity_must_be_positive(self):
        with pytest.raises(ValueError, match="quantity"):
            GapFadeConfig(quantity=0)

    def test_min_gap_bps_must_be_positive(self):
        with pytest.raises(ValueError, match="min_gap_bps"):
            GapFadeConfig(min_gap_bps=0.0)

    def test_max_gap_bps_must_exceed_min_gap_bps(self):
        with pytest.raises(ValueError, match="max_gap_bps"):
            GapFadeConfig(min_gap_bps=100.0, max_gap_bps=50.0)

    def test_fade_trigger_bps_must_be_positive(self):
        with pytest.raises(ValueError, match="fade_trigger_bps"):
            GapFadeConfig(fade_trigger_bps=0.0)

    def test_stop_loss_bps_must_be_positive(self):
        with pytest.raises(ValueError, match="stop_loss_bps"):
            GapFadeConfig(stop_loss_bps=0.0)

    def test_invalid_target_mode_raises(self):
        with pytest.raises(ValueError, match="target_mode"):
            GapFadeConfig(target_mode="invalid")

    def test_target_bps_must_be_positive_when_set(self):
        with pytest.raises(ValueError, match="target_bps"):
            GapFadeConfig(target_bps=0.0)

    def test_max_trades_must_be_at_least_one(self):
        with pytest.raises(ValueError, match="max_trades_per_symbol_per_day"):
            GapFadeConfig(max_trades_per_symbol_per_day=0)

    def test_latest_entry_time_before_square_off(self):
        with pytest.raises(ValueError, match="square_off_time"):
            GapFadeConfig(
                latest_entry_time=time(15, 15),
                square_off_time=time(15, 15),
            )

    def test_valid_target_modes_accepted(self):
        for mode in ("vwap", "prior_close", "half_gap"):
            cfg = GapFadeConfig(target_mode=mode)
            assert cfg.target_mode == mode

    def test_strategy_instantiates_with_default_config(self):
        strategy = GapFadeStrategy()
        assert strategy.strategy_id == "gap_fade_v1"

    def test_on_bar_returns_empty_list_when_no_prior_close(self):
        """First day: no prior close available → no trades."""
        from trading_engine.strategy.base import StrategyContext
        from trading_engine.strategy.signals import Bar

        strategy = GapFadeStrategy(GapFadeConfig(require_vwap_confirmation=False))
        ctx = StrategyContext(strategy_id="test", mode="backtest", config={})
        bar = Bar(
            symbol="TEST",
            exchange="NSE",
            timestamp=datetime.fromisoformat("2024-01-15 09:15:00"),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1000,
            interval="minute",
        )
        intents = strategy.on_bar(bar, ctx)
        assert intents == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py::TestGapFadeConfig -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'trading_engine.strategies.gap_fade'`

- [ ] **Step 3: Write minimal implementation — GapFadeConfig + _SymbolState + skeleton GapFadeStrategy**

```python
# src/trading_engine/strategies/gap_fade.py
"""Gap Fade to VWAP / Prior Close strategy (backtest-only, v1).

Intraday mean-reversion strategy for NSE cash equities:
  1. Detect opening gap (opening price vs previous day's closing price).
  2. If gap_bps is within [min_gap_bps, max_gap_bps], qualify the gap.
  3. After entry_start_time, enter a fade trade when price reverses
     fade_trigger_bps from the opening price (+ optional VWAP confirmation).
     Gap-up → fade SHORT. Gap-down → fade LONG.
  4. Exit on stop-loss, dynamic target (VWAP cross / prior close touch /
     half-gap touch), or square-off at 15:15.

No live order placement. No broker API calls. Backtest use only.

Prior close tracking
---------------------
The strategy carries the previous day's closing price across session
boundaries.  The first trading day in any dataset is always skipped because
no prior close is available.

Gap detection
-------------
  gap_bps = (opening_price / prior_close - 1) * 10000
  Gap-up  (gap_bps > 0) → fade SHORT if allow_short_fades=True.
  Gap-down (gap_bps < 0) → fade LONG  if allow_long_fades=True.

Entry trigger
--------------
  Gap-down long  fade: bar.close >= opening_price * (1 + fade_trigger_bps/10000)
  Gap-up  short fade: bar.close <= opening_price * (1 - fade_trigger_bps/10000)
  VWAP confirmation (require_vwap_confirmation=True):
    Long  fade: bar.close > session VWAP
    Short fade: bar.close < session VWAP

Target modes
-------------
  "vwap"        — exit when bar crosses session VWAP (re-evaluated each bar)
  "prior_close" — exit when bar reaches prior day's close
  "half_gap"    — exit at midpoint of (opening_price + prior_close)
  target_bps    — fixed-bps override; overrides target_mode when set

Exit reasons
-------------
  "gf_stop_loss"   — stop-loss hit
  "gf_target"      — dynamic or fixed target reached
  "gf_square_off"  — bar timestamp >= square_off_time
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
_TWO = Decimal("2")
_THREE = Decimal("3")

_VALID_TARGET_MODES = frozenset({"vwap", "prior_close", "half_gap"})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class GapFadeConfig:
    """Configuration for GapFadeStrategy.

    Args:
        strategy_id:                   Identifier for the strategy run.
        exchange:                      Exchange string, e.g. "NSE".
        product:                       Product type, e.g. "MIS" for intraday.
        quantity:                      Shares per signal. Must be positive.
        session_start:                 Market session open time (09:15 for NSE).
        entry_start_time:              No entries before this time. Default 09:20
                                       (one bar after session open).
        latest_entry_time:             No new entries at or after this time.
        square_off_time:               Force-close all positions at this time.
        min_gap_bps:                   Minimum absolute gap in bps to qualify.
        max_gap_bps:                   Maximum absolute gap in bps to qualify.
        fade_trigger_bps:              Price must reverse this many bps from
                                       opening_price before entry is triggered.
        require_vwap_confirmation:     If True, long fade requires close > VWAP;
                                       short fade requires close < VWAP.
        target_mode:                   "vwap", "prior_close", or "half_gap".
                                       Ignored if target_bps is set.
        stop_loss_bps:                 Stop-loss distance from entry price in bps.
        target_bps:                    If not None, fixed profit target in bps
                                       (overrides target_mode).
        max_trades_per_symbol_per_day: Maximum entries per symbol per calendar day.
        allow_long_fades:              If True, gap-down → fade LONG is allowed.
        allow_short_fades:             If True, gap-up → fade SHORT is allowed.
        min_opening_volume:            If not None, first bar's volume must be
                                       >= this value, else gap is skipped.
        min_gap_abs:                   If not None, |opening_price - prior_close|
                                       must be >= this value in price units.
    """

    strategy_id: str = "gap_fade_v1"
    exchange: str = "NSE"
    product: str = "MIS"
    quantity: int = 10
    session_start: time = field(default_factory=lambda: time(9, 15))
    entry_start_time: time = field(default_factory=lambda: time(9, 20))
    latest_entry_time: time = field(default_factory=lambda: time(10, 30))
    square_off_time: time = field(default_factory=lambda: time(15, 15))
    min_gap_bps: float = 60.0
    max_gap_bps: float = 300.0
    fade_trigger_bps: float = 20.0
    require_vwap_confirmation: bool = True
    target_mode: str = "vwap"
    stop_loss_bps: float = 80.0
    target_bps: float | None = None
    max_trades_per_symbol_per_day: int = 1
    allow_long_fades: bool = True
    allow_short_fades: bool = True
    min_opening_volume: int | None = None
    min_gap_abs: float | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.min_gap_bps <= 0:
            raise ValueError(f"min_gap_bps must be positive, got {self.min_gap_bps}")
        if self.max_gap_bps <= self.min_gap_bps:
            raise ValueError(
                f"max_gap_bps ({self.max_gap_bps}) must exceed "
                f"min_gap_bps ({self.min_gap_bps})"
            )
        if self.fade_trigger_bps <= 0:
            raise ValueError(f"fade_trigger_bps must be positive, got {self.fade_trigger_bps}")
        if self.stop_loss_bps <= 0:
            raise ValueError(f"stop_loss_bps must be positive, got {self.stop_loss_bps}")
        if self.target_mode not in _VALID_TARGET_MODES:
            raise ValueError(
                f"target_mode must be one of {sorted(_VALID_TARGET_MODES)}, "
                f"got {self.target_mode!r}"
            )
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
    """Mutable per-symbol state for GapFadeStrategy.

    ``prior_close`` and ``last_close`` survive across calls to reset() to
    implement the inter-day carry: at the start of each new day,
    ``last_close`` (the final close of the previous session) becomes
    ``prior_close`` for the new day.
    """

    current_date: date | None = None

    # Inter-day carry — NOT reset on new day
    prior_close: Decimal | None = None   # previous day's last close
    last_close: Decimal | None = None    # current day's running last close

    # Intraday gap analysis
    opening_bar_seen: bool = False
    opening_price: Decimal | None = None   # first bar's open at session_start
    opening_volume: int = 0
    gap_bps: Decimal | None = None
    gap_qualified: bool = False
    fade_direction: str = ""   # "LONG" (fade gap-down) or "SHORT" (fade gap-up)

    # Session VWAP accumulators
    cumulative_pv: Decimal = field(default_factory=lambda: Decimal("0"))
    cumulative_vol: int = 0
    vwap: Decimal | None = None

    # Position tracking
    in_position: bool = False
    position_side: str = ""   # "LONG" or "SHORT"
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    dynamic_target: Decimal | None = None  # None means use VWAP each bar

    # Daily counters
    bars_seen_today: int = 0
    trades_taken_today: int = 0

    def reset(self, new_date: date) -> None:
        """Reset intraday state for a new trading day; carry prior close."""
        self.prior_close = self.last_close   # inter-day carry
        self.last_close = None
        self.current_date = new_date
        self.opening_bar_seen = False
        self.opening_price = None
        self.opening_volume = 0
        self.gap_bps = None
        self.gap_qualified = False
        self.fade_direction = ""
        self.cumulative_pv = Decimal("0")
        self.cumulative_vol = 0
        self.vwap = None
        self.in_position = False
        self.position_side = ""
        self.entry_price = None
        self.stop_price = None
        self.dynamic_target = None
        self.bars_seen_today = 0
        self.trades_taken_today = 0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class GapFadeStrategy(Strategy):
    """Gap Fade to VWAP / Prior Close strategy (backtest v1).

    Construct with a GapFadeConfig to customise all parameters.

    Example::

        cfg = GapFadeConfig(min_gap_bps=80.0, target_mode="prior_close")
        strategy = GapFadeStrategy(config=cfg)
    """

    def __init__(
        self,
        config: GapFadeConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        cfg = config or GapFadeConfig()
        super().__init__(strategy_id=cfg.strategy_id)
        self._config = cfg
        self._logger = logger or logging.getLogger(__name__)
        self._states: dict[str, _SymbolState] = {}

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        """Process one bar; return zero or more OrderIntents."""
        state = self._get_state(bar.symbol)
        bar_date = _bar_date(bar)
        bar_time = _bar_time(bar)

        # Reset state at the start of each new trading day.
        if state.current_date != bar_date:
            state.reset(bar_date)

        # Always track last_close for inter-day carry.
        state.last_close = bar.close

        # Update session VWAP.
        self._update_vwap(bar, state)

        state.bars_seen_today += 1

        # Process the opening bar (first bar at or after session_start).
        if not state.opening_bar_seen and bar_time >= self._config.session_start:
            self._process_opening_bar(bar, state)

        intents: list[OrderIntent] = []

        # Exit checks before entry.
        if state.in_position:
            exit_intent = self._check_exit(bar, bar_time, state)
            if exit_intent is not None:
                intents.append(exit_intent)
                state.in_position = False
                state.position_side = ""
                return intents

        # Entry check.
        if self._can_enter(bar_time, state):
            entry_intent = self._check_entry(bar, state)
            if entry_intent is not None:
                intents.append(entry_intent)
                self._set_position_state(bar, state)

        return intents

    # ------------------------------------------------------------------
    # VWAP
    # ------------------------------------------------------------------

    def _update_vwap(self, bar: Bar, state: _SymbolState) -> None:
        if bar.volume > 0:
            tp = (bar.high + bar.low + bar.close) / _THREE
            state.cumulative_pv += tp * Decimal(bar.volume)
            state.cumulative_vol += bar.volume
        else:
            state.cumulative_pv += bar.close
            state.cumulative_vol += 1
        state.vwap = state.cumulative_pv / Decimal(state.cumulative_vol)

    # ------------------------------------------------------------------
    # Opening bar processing
    # ------------------------------------------------------------------

    def _process_opening_bar(self, bar: Bar, state: _SymbolState) -> None:
        """Record opening price and compute gap on the first bar of the session."""
        state.opening_bar_seen = True
        state.opening_price = bar.open
        state.opening_volume = bar.volume

        if state.prior_close is None or state.prior_close == _ZERO:
            return   # first day or invalid prior — no gap

        gap = (state.opening_price / state.prior_close - _ONE) * _TEN_THOUSAND
        state.gap_bps = gap

        cfg = self._config
        abs_gap = abs(gap)
        min_gap = Decimal(str(cfg.min_gap_bps))
        max_gap = Decimal(str(cfg.max_gap_bps))

        if abs_gap < min_gap or abs_gap > max_gap:
            return  # gap outside qualifying range

        if cfg.min_gap_abs is not None:
            abs_price_gap = abs(state.opening_price - state.prior_close)
            if abs_price_gap < Decimal(str(cfg.min_gap_abs)):
                return

        if cfg.min_opening_volume is not None and state.opening_volume < cfg.min_opening_volume:
            return  # volume filter

        if gap > _ZERO and cfg.allow_short_fades:
            state.gap_qualified = True
            state.fade_direction = "SHORT"
        elif gap < _ZERO and cfg.allow_long_fades:
            state.gap_qualified = True
            state.fade_direction = "LONG"

    # ------------------------------------------------------------------
    # Entry guards
    # ------------------------------------------------------------------

    def _can_enter(self, bar_time: time, state: _SymbolState) -> bool:
        cfg = self._config
        return (
            state.gap_qualified
            and not state.in_position
            and state.trades_taken_today < cfg.max_trades_per_symbol_per_day
            and bar_time >= cfg.entry_start_time
            and bar_time <= cfg.latest_entry_time
            and state.vwap is not None
            and state.opening_price is not None
        )

    def _check_entry(self, bar: Bar, state: _SymbolState) -> OrderIntent | None:
        cfg = self._config
        opening = state.opening_price
        assert opening is not None
        trigger_factor = Decimal(str(cfg.fade_trigger_bps)) / _TEN_THOUSAND

        if state.fade_direction == "LONG":
            trigger_price = opening * (_ONE + trigger_factor)
            if bar.close < trigger_price:
                return None
            if cfg.require_vwap_confirmation:
                if state.vwap is None or bar.close <= state.vwap:
                    return None
            return OrderIntent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side="BUY",
                quantity=cfg.quantity,
                order_type="MARKET",
                product=cfg.product,
                reason="gf_long_entry",
            )

        if state.fade_direction == "SHORT":
            trigger_price = opening * (_ONE - trigger_factor)
            if bar.close > trigger_price:
                return None
            if cfg.require_vwap_confirmation:
                if state.vwap is None or bar.close >= state.vwap:
                    return None
            return OrderIntent(
                strategy_id=self.strategy_id,
                symbol=bar.symbol,
                exchange=bar.exchange,
                side="SELL",
                quantity=cfg.quantity,
                order_type="MARKET",
                product=cfg.product,
                reason="gf_short_entry",
            )

        return None

    def _set_position_state(self, bar: Bar, state: _SymbolState) -> None:
        entry_price = bar.close
        cfg = self._config

        state.in_position = True
        state.position_side = state.fade_direction
        state.entry_price = entry_price
        state.trades_taken_today += 1

        sl_factor = Decimal(str(cfg.stop_loss_bps)) / _TEN_THOUSAND
        if state.position_side == "LONG":
            state.stop_price = entry_price * (_ONE - sl_factor)
        else:
            state.stop_price = entry_price * (_ONE + sl_factor)

        if cfg.target_bps is not None:
            tgt_factor = Decimal(str(cfg.target_bps)) / _TEN_THOUSAND
            if state.position_side == "LONG":
                state.dynamic_target = entry_price * (_ONE + tgt_factor)
            else:
                state.dynamic_target = entry_price * (_ONE - tgt_factor)
        elif cfg.target_mode == "prior_close":
            state.dynamic_target = state.prior_close
        elif cfg.target_mode == "half_gap":
            if state.prior_close is not None and state.opening_price is not None:
                state.dynamic_target = (state.opening_price + state.prior_close) / _TWO
            else:
                state.dynamic_target = None
        else:
            # "vwap" mode: dynamic_target=None signals _check_exit to use state.vwap
            state.dynamic_target = None

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _check_exit(self, bar: Bar, bar_time: time, state: _SymbolState) -> OrderIntent | None:
        assert state.stop_price is not None
        cfg = self._config

        if state.position_side == "LONG":
            stop_hit = bar.low <= state.stop_price
        else:
            stop_hit = bar.high >= state.stop_price

        target_hit = False
        if cfg.target_bps is not None and state.dynamic_target is not None:
            if state.position_side == "LONG":
                target_hit = bar.high >= state.dynamic_target
            else:
                target_hit = bar.low <= state.dynamic_target
        elif cfg.target_mode == "vwap" and state.vwap is not None:
            if state.position_side == "LONG":
                target_hit = bar.high >= state.vwap
            else:
                target_hit = bar.low <= state.vwap
        elif cfg.target_mode in ("prior_close", "half_gap") and state.dynamic_target is not None:
            if state.position_side == "LONG":
                target_hit = bar.high >= state.dynamic_target
            else:
                target_hit = bar.low <= state.dynamic_target

        square_off_hit = bar_time >= cfg.square_off_time

        if stop_hit:
            return self._exit_intent(bar, state, "gf_stop_loss")
        if target_hit:
            return self._exit_intent(bar, state, "gf_target")
        if square_off_hit:
            return self._exit_intent(bar, state, "gf_square_off")
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py::TestGapFadeConfig -v 2>&1 | tail -20
```

Expected: all 13 tests PASS.

- [ ] **Step 5: Run ruff**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m ruff check --fix src/trading_engine/strategies/gap_fade.py tests/unit/strategies/test_gap_fade.py
python3 -m ruff format src/trading_engine/strategies/gap_fade.py tests/unit/strategies/test_gap_fade.py
```

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add src/trading_engine/strategies/gap_fade.py tests/unit/strategies/test_gap_fade.py
git commit -m "Add GapFadeConfig, _SymbolState, and skeleton GapFadeStrategy"
```

---

### Task 2: Prior-close tracking and gap detection tests

**Files:**
- Modify: `tests/unit/strategies/test_gap_fade.py` (add `TestPriorCloseTracking` and `TestGapDetection`)
- No changes to `gap_fade.py` — implementation was already written in Task 1

- [ ] **Step 1: Write failing tests for prior-close tracking and gap detection**

Append to `tests/unit/strategies/test_gap_fade.py`:

```python
# ---------------------------------------------------------------------------
# Shared helpers for strategy-level tests
# ---------------------------------------------------------------------------


def _ctx():
    from trading_engine.strategy.base import StrategyContext
    return StrategyContext(strategy_id="gf_test", mode="backtest", config={})


def _bar(ts: str, open_: float = 100.0, high: float = 101.0, low: float = 99.0,
         close: float = 100.0, volume: int = 1000, symbol: str = "TEST") -> "Bar":
    from trading_engine.strategy.signals import Bar
    return Bar(
        symbol=symbol,
        exchange="NSE",
        timestamp=datetime.fromisoformat(ts),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
        interval="minute",
    )


def _permissive_cfg(**kwargs) -> GapFadeConfig:
    """Config with relaxed thresholds for controlled unit tests."""
    defaults = dict(
        min_gap_bps=50.0,
        max_gap_bps=500.0,
        fade_trigger_bps=10.0,
        stop_loss_bps=200.0,
        require_vwap_confirmation=False,
        target_mode="prior_close",
        allow_long_fades=True,
        allow_short_fades=True,
    )
    defaults.update(kwargs)
    return GapFadeConfig(**defaults)


class TestPriorCloseTracking:
    """prior_close must carry last bar of day N as prior_close for day N+1."""

    def test_first_day_has_no_prior_close(self):
        """No entry on first day because prior_close is None."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        # Day 1: flat bars, 09:15 session
        for i in range(5):
            intents = strategy.on_bar(
                _bar(f"2024-01-15 09:{15 + i:02d}:00", close=100.0), ctx
            )
            assert intents == [], f"bar {i} should produce no intent on day 1"

    def test_prior_close_set_after_day_one(self):
        """After day 1 closes at 105.0, day 2 should see prior_close=105."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        # Feed day 1
        for i in range(5):
            strategy.on_bar(_bar(f"2024-01-15 09:{15 + i:02d}:00", close=100.0 + i), ctx)
        # Check internal state via the strategy's _states dict
        state = strategy._states["TEST"]
        assert state.last_close == Decimal("104")
        # First bar of day 2 triggers reset → prior_close = 104
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=110.0, close=110.0), ctx)
        assert state.prior_close == Decimal("104")

    def test_gap_bps_computed_correctly_on_day2(self):
        """gap_bps = (opening / prior_close - 1) * 10000."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        # Day 1 closes at 100
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        # Day 2 opens at 110 (10% gap-up = 1000 bps)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=110.0, close=110.0), ctx)
        state = strategy._states["TEST"]
        assert state.gap_bps is not None
        assert abs(float(state.gap_bps) - 1000.0) < 1.0


class TestGapDetection:
    def test_gap_below_min_not_qualified(self):
        """Gap of 30 bps < min_gap_bps=50 → gap_qualified=False."""
        strategy = GapFadeStrategy(_permissive_cfg(min_gap_bps=50.0))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        # 30 bps gap-up: 100 * 1.003 = 100.3
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=100.3, close=100.3), ctx)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_above_max_not_qualified(self):
        """Gap of 600 bps > max_gap_bps=500 → gap_qualified=False."""
        strategy = GapFadeStrategy(_permissive_cfg(max_gap_bps=500.0))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        # 600 bps gap-up: 100 * 1.06 = 106
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=106.0, close=106.0), ctx)
        assert strategy._states["TEST"].gap_qualified is False

    def test_gap_up_sets_short_fade_direction(self):
        """Gap-up → fade_direction == 'SHORT'."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        # 100 bps gap-up
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=101.0, close=101.0), ctx)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.fade_direction == "SHORT"

    def test_gap_down_sets_long_fade_direction(self):
        """Gap-down → fade_direction == 'LONG'."""
        strategy = GapFadeStrategy(_permissive_cfg())
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        # 100 bps gap-down: 100 * 0.99 = 99
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=99.0, close=99.0), ctx)
        state = strategy._states["TEST"]
        assert state.gap_qualified is True
        assert state.fade_direction == "LONG"

    def test_allow_long_fades_false_suppresses_gap_down(self):
        """allow_long_fades=False → gap-down not qualified."""
        strategy = GapFadeStrategy(_permissive_cfg(allow_long_fades=False))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=99.0, close=99.0), ctx)
        assert strategy._states["TEST"].gap_qualified is False

    def test_min_opening_volume_filter(self):
        """Opening bar with volume < min_opening_volume → not qualified."""
        strategy = GapFadeStrategy(_permissive_cfg(min_opening_volume=5000))
        ctx = _ctx()
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=100.0), ctx)
        # Gap-up but volume=100 < 5000
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=101.0, close=101.0, volume=100), ctx)
        assert strategy._states["TEST"].gap_qualified is False
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py::TestPriorCloseTracking tests/unit/strategies/test_gap_fade.py::TestGapDetection -v 2>&1 | tail -20
```

Expected: all 9 tests PASS (implementation was written in Task 1).

- [ ] **Step 3: Run ruff**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m ruff check --fix tests/unit/strategies/test_gap_fade.py
python3 -m ruff format tests/unit/strategies/test_gap_fade.py
```

- [ ] **Step 4: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add tests/unit/strategies/test_gap_fade.py
git commit -m "Add prior-close tracking and gap detection unit tests"
```

---

### Task 3: Entry logic tests (fade trigger + VWAP confirmation)

**Files:**
- Modify: `tests/unit/strategies/test_gap_fade.py` (add `TestEntryLogic`)

- [ ] **Step 1: Write entry logic tests**

Helper function to set up a two-day candle sequence. Append to `tests/unit/strategies/test_gap_fade.py`:

```python
def _feed_two_days_opening(
    strategy: GapFadeStrategy,
    ctx,
    day1_close: float = 100.0,
    day2_open: float = 101.0,  # default 100 bps gap-up
) -> None:
    """Feed day-1 close bar, then day-2 opening bar."""
    strategy.on_bar(_bar("2024-01-15 09:15:00", close=day1_close), ctx)
    strategy.on_bar(_bar("2024-01-16 09:15:00", open_=day2_open, close=day2_open), ctx)


class TestEntryLogic:
    def test_long_entry_emitted_when_fade_trigger_met(self):
        """Gap-down + price rises fade_trigger_bps → BUY intent returned."""
        # Day 1 close=100, Day 2 open=99 (100 bps gap-down → LONG fade)
        # fade_trigger_bps=10 → entry when close >= 99 * 1.001 = 99.099
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=10.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        # Entry bar: close=99.2 > trigger=99.099, vwap irrelevant (require=False)
        intents = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=99.0, high=99.5, low=98.9, close=99.2), ctx
        )
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gf_long_entry"

    def test_short_entry_emitted_when_fade_trigger_met(self):
        """Gap-up + price drops fade_trigger_bps → SELL intent returned."""
        # Day 1 close=100, Day 2 open=101 (100 bps gap-up → SHORT fade)
        # fade_trigger_bps=10 → entry when close <= 101 * (1 - 0.001) = 100.899
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=10.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=101.0)
        # Entry bar: close=100.8 < trigger=100.899
        intents = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=101.0, high=101.2, low=100.7, close=100.8), ctx
        )
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gf_short_entry"

    def test_no_entry_before_entry_start_time(self):
        """Bars at 09:15 (< entry_start_time=09:20) must not trigger entry."""
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=5.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        # 09:15 bar (same as opening bar already processed — next bar at 09:16 still < 09:20)
        intents = strategy.on_bar(
            _bar("2024-01-16 09:16:00", open_=99.0, high=99.5, low=98.9, close=99.5), ctx
        )
        assert intents == []

    def test_no_entry_after_latest_entry_time(self):
        """Bars after latest_entry_time=10:30 must not trigger entry."""
        strategy = GapFadeStrategy(_permissive_cfg(fade_trigger_bps=5.0))
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        intents = strategy.on_bar(
            _bar("2024-01-16 10:31:00", open_=99.0, high=99.5, low=98.9, close=99.5), ctx
        )
        assert intents == []

    def test_vwap_confirmation_blocks_long_entry_below_vwap(self):
        """Long fade with require_vwap_confirmation=True: close must be > VWAP."""
        # The VWAP will be around the opening bar price (99) after just one bar.
        # If close == 99.2 and VWAP is ~99, the condition close > VWAP should hold.
        # Test the blocking case: close <= VWAP (use a very high VWAP scenario).
        strategy = GapFadeStrategy(
            _permissive_cfg(fade_trigger_bps=1.0, require_vwap_confirmation=True)
        )
        ctx = _ctx()
        # Day 1 close=200, day 2 open=99 (huge gap down)
        strategy.on_bar(_bar("2024-01-15 09:15:00", close=200.0, volume=10000), ctx)
        # Day 2 opening bar at 99, but feed a bar at 200 first to push VWAP very high
        strategy.on_bar(_bar("2024-01-16 09:15:00", open_=99.0, close=200.0, volume=10000), ctx)
        # Now a bar at 09:20 with close=99.5 (triggers fade) but VWAP ~200 > 99.5
        intents = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=99.5, high=100.0, low=99.4, close=99.5), ctx
        )
        assert intents == []   # blocked by VWAP confirmation

    def test_max_trades_per_day_limit_respected(self):
        """After one trade is entered, second entry is blocked."""
        strategy = GapFadeStrategy(
            _permissive_cfg(fade_trigger_bps=10.0, max_trades_per_symbol_per_day=1)
        )
        ctx = _ctx()
        _feed_two_days_opening(strategy, ctx, day1_close=100.0, day2_open=99.0)
        # First entry
        intents1 = strategy.on_bar(
            _bar("2024-01-16 09:20:00", open_=99.0, high=99.5, low=98.9, close=99.2), ctx
        )
        assert len(intents1) == 1
        # Manually clear position to test trade counter (simulate exit)
        strategy._states["TEST"].in_position = False
        strategy._states["TEST"].position_side = ""
        # Second entry attempt — trade count already = 1
        intents2 = strategy.on_bar(
            _bar("2024-01-16 09:25:00", open_=99.0, high=99.5, low=98.9, close=99.2), ctx
        )
        assert intents2 == []
```

- [ ] **Step 2: Run tests**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py::TestEntryLogic -v 2>&1 | tail -20
```

Expected: all 6 tests PASS.

- [ ] **Step 3: Run ruff**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m ruff check --fix tests/unit/strategies/test_gap_fade.py
python3 -m ruff format tests/unit/strategies/test_gap_fade.py
```

- [ ] **Step 4: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add tests/unit/strategies/test_gap_fade.py
git commit -m "Add entry logic unit tests for GapFadeStrategy"
```

---

### Task 4: Exit logic tests (stop-loss, dynamic targets, square-off)

**Files:**
- Modify: `tests/unit/strategies/test_gap_fade.py` (add `TestExitLogic`)

- [ ] **Step 1: Write exit logic tests**

Append to `tests/unit/strategies/test_gap_fade.py`:

```python
def _enter_position(strategy: GapFadeStrategy, ctx, side: str = "LONG") -> None:
    """Force-enter a position by manipulating state directly for exit tests."""
    from decimal import Decimal
    state = strategy._states.setdefault("TEST", __import__(
        "trading_engine.strategies.gap_fade", fromlist=["_SymbolState"]
    )._SymbolState())
    state.in_position = True
    state.position_side = side
    state.entry_price = Decimal("100")
    state.stop_price = Decimal("98") if side == "LONG" else Decimal("102")
    state.vwap = Decimal("101") if side == "LONG" else Decimal("99")
    state.current_date = __import__("datetime").date(2024, 1, 16)
    state.trades_taken_today = 1


class TestExitLogic:
    def _strategy_with_position(self, side: str = "LONG", **cfg_kwargs) -> GapFadeStrategy:
        s = GapFadeStrategy(_permissive_cfg(**cfg_kwargs))
        s._states["TEST"] = s._get_state("TEST")
        from datetime import date
        state = s._states["TEST"]
        state.in_position = True
        state.position_side = side
        state.entry_price = Decimal("100")
        state.stop_price = Decimal("98") if side == "LONG" else Decimal("102")
        state.current_date = date(2024, 1, 16)
        state.trades_taken_today = 1
        state.gap_qualified = True
        state.opening_bar_seen = True
        state.prior_close = Decimal("98")   # for prior_close and half_gap targets
        state.opening_price = Decimal("100")
        # half_gap_target = (100 + 98) / 2 = 99 — LONG enters at 100, target BELOW entry…
        # Use opening_price=95, prior_close=100 so half_gap=97.5 above entry at 95.
        # Easier: just override dynamic_target directly in each test.
        state.vwap = Decimal("101")
        return s

    def test_stop_loss_exits_long(self):
        """bar.low <= stop_price → SELL with reason gf_stop_loss."""
        s = self._strategy_with_position("LONG")
        ctx = _ctx()
        # stop=98, bar.low=97 → stop hit
        intents = s.on_bar(
            _bar("2024-01-16 09:25:00", open_=100.0, high=100.5, low=97.0, close=97.5), ctx
        )
        assert len(intents) == 1
        assert intents[0].side == "SELL"
        assert intents[0].reason == "gf_stop_loss"

    def test_stop_loss_exits_short(self):
        """bar.high >= stop_price → BUY with reason gf_stop_loss."""
        s = self._strategy_with_position("SHORT")
        s._states["TEST"].stop_price = Decimal("102")
        ctx = _ctx()
        intents = s.on_bar(
            _bar("2024-01-16 09:25:00", open_=100.0, high=103.0, low=99.5, close=100.0), ctx
        )
        assert len(intents) == 1
        assert intents[0].side == "BUY"
        assert intents[0].reason == "gf_stop_loss"

    def test_vwap_target_exits_long(self):
        """target_mode=vwap: bar.high >= vwap → exit with gf_target."""
        s = self._strategy_with_position("LONG", target_mode="vwap")
        s._states["TEST"].vwap = Decimal("101")
        s._states["TEST"].dynamic_target = None   # vwap mode uses state.vwap
        ctx = _ctx()
        intents = s.on_bar(
            _bar("2024-01-16 09:25:00", open_=100.0, high=101.5, low=99.9, close=101.0), ctx
        )
        assert len(intents) == 1
        assert intents[0].reason == "gf_target"

    def test_prior_close_target_exits_long(self):
        """target_mode=prior_close: bar.high >= dynamic_target → exit."""
        s = self._strategy_with_position("LONG", target_mode="prior_close")
        s._states["TEST"].dynamic_target = Decimal("102")
        ctx = _ctx()
        intents = s.on_bar(
            _bar("2024-01-16 09:25:00", open_=100.0, high=102.5, low=99.9, close=102.0), ctx
        )
        assert len(intents) == 1
        assert intents[0].reason == "gf_target"

    def test_half_gap_target_exits_short(self):
        """target_mode=half_gap: bar.low <= dynamic_target → exit."""
        s = self._strategy_with_position("SHORT", target_mode="half_gap")
        s._states["TEST"].dynamic_target = Decimal("99")  # half-gap
        ctx = _ctx()
        intents = s.on_bar(
            _bar("2024-01-16 09:25:00", open_=100.0, high=100.2, low=98.5, close=99.0), ctx
        )
        assert len(intents) == 1
        assert intents[0].reason == "gf_target"

    def test_square_off_exits_at_15_15(self):
        """Bar at 15:15 triggers square-off regardless of P&L."""
        s = self._strategy_with_position("LONG")
        s._states["TEST"].vwap = Decimal("105")  # VWAP above range — won't trigger target
        ctx = _ctx()
        intents = s.on_bar(
            _bar("2024-01-16 15:15:00", open_=100.0, high=100.1, low=99.9, close=100.0), ctx
        )
        assert len(intents) == 1
        assert intents[0].reason == "gf_square_off"

    def test_stop_loss_takes_priority_over_target(self):
        """When both stop and target would be hit on same bar, stop-loss wins."""
        s = self._strategy_with_position("LONG", target_mode="prior_close")
        state = s._states["TEST"]
        state.stop_price = Decimal("98")    # bar.low=97 → stop hit
        state.dynamic_target = Decimal("102")  # bar.high=103 → target hit too
        ctx = _ctx()
        intents = s.on_bar(
            _bar("2024-01-16 09:30:00", open_=100.0, high=103.0, low=97.0, close=100.0), ctx
        )
        assert len(intents) == 1
        assert intents[0].reason == "gf_stop_loss"

    def test_position_cleared_after_exit(self):
        """After an exit intent, in_position must be False."""
        s = self._strategy_with_position("LONG")
        ctx = _ctx()
        s.on_bar(_bar("2024-01-16 09:25:00", open_=100.0, high=100.1, low=97.0, close=97.5), ctx)
        assert s._states["TEST"].in_position is False
```

- [ ] **Step 2: Run tests**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py::TestExitLogic -v 2>&1 | tail -25
```

Expected: all 8 tests PASS.

- [ ] **Step 3: Run full test file**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py -v 2>&1 | tail -10
```

Expected: all tests PASS.

- [ ] **Step 4: Run ruff**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m ruff check --fix tests/unit/strategies/test_gap_fade.py
python3 -m ruff format tests/unit/strategies/test_gap_fade.py
```

- [ ] **Step 5: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add tests/unit/strategies/test_gap_fade.py
git commit -m "Add exit logic unit tests for GapFadeStrategy"
```

---

### Task 5: BacktestEngine integration test + run_gap_fade_backtest.py

**Files:**
- Create: `tests/unit/strategies/test_gap_fade_backtest.py`
- Create: `scripts/run_gap_fade_backtest.py`

- [ ] **Step 1: Write integration test (failing)**

```python
# tests/unit/strategies/test_gap_fade_backtest.py
"""Integration tests: GapFadeStrategy running end-to-end in BacktestEngine."""

from __future__ import annotations

import sys
from datetime import date
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
from trading_engine.strategies.gap_fade import GapFadeConfig, GapFadeStrategy  # noqa: E402

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
    config: GapFadeConfig | None = None,
    initial_cash: Decimal = Decimal("500000"),
) -> BacktestEngine:
    cfg = config or GapFadeConfig()
    strategy = GapFadeStrategy(config=cfg)
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


def _make_gap_fade_candles() -> pd.DataFrame:
    """Two-day candle sequence that produces one gap-fade trade.

    Day 1 (2024-01-15): session closes at 100.0 (prior_close for day 2).
    Day 2 (2024-01-16):
      09:15 — opening bar: open=107 (700 bps gap-up, qualifies [50, 1000]).
      09:20 — entry bar: close=106.8 < trigger=107*(1-0.001)=106.893 → SHORT entry.
               VWAP irrelevant (require_vwap_confirmation=False).
      09:25 — target bar: low=106.0; prior_close target=100 → not yet.
               high=107.5 → no. close=106.5.
      09:30 — target bar: low=101; high=107. prior_close=100 → low=101 > 100, not hit.
      15:15 — square-off bar.
    """
    rows = []
    # Day 1: single bar, close=100
    rows.append({
        "timestamp": pd.Timestamp("2024-01-15 09:15:00"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 5000,
    })
    # Day 2 opening bar: gap-up to 107
    rows.append({
        "timestamp": pd.Timestamp("2024-01-16 09:15:00"),
        "open": 107.0, "high": 107.5, "low": 106.5, "close": 107.0, "volume": 5000,
    })
    # 09:20 — SHORT entry: close=106.5 < 107*(1-0.001)=106.893
    rows.append({
        "timestamp": pd.Timestamp("2024-01-16 09:20:00"),
        "open": 107.0, "high": 107.2, "low": 106.4, "close": 106.5, "volume": 3000,
    })
    # 09:25–15:10: bars that don't hit stop (stop=107*(1+0.02)=109.14) or target
    for h in range(9, 15):
        for m in [30, 45]:
            ts = f"2024-01-16 {h:02d}:{m:02d}:00"
            rows.append({
                "timestamp": pd.Timestamp(ts),
                "open": 106.0, "high": 106.5, "low": 105.5, "close": 106.0, "volume": 2000,
            })
    # 15:15 — square-off
    rows.append({
        "timestamp": pd.Timestamp("2024-01-16 15:15:00"),
        "open": 106.0, "high": 106.2, "low": 105.8, "close": 106.0, "volume": 1000,
    })
    return pd.DataFrame(rows)


class TestGapFadeBacktest:
    def _cfg(self) -> GapFadeConfig:
        return GapFadeConfig(
            strategy_id="gf_test",
            min_gap_bps=50.0,
            max_gap_bps=1000.0,
            fade_trigger_bps=10.0,
            stop_loss_bps=200.0,
            target_mode="prior_close",
            require_vwap_confirmation=False,
            allow_short_fades=True,
            allow_long_fades=True,
        )

    def test_engine_runs_without_error(self):
        df = _make_gap_fade_candles()
        engine = _make_engine({"TEST": df}, config=self._cfg())
        report = engine.run()
        assert report is not None

    def test_at_least_one_fill_produced(self):
        """The fixture produces a short fade entry + square-off exit."""
        df = _make_gap_fade_candles()
        engine = _make_engine({"TEST": df}, config=self._cfg())
        report = engine.run()
        assert len(report.fills) >= 2, "expected entry + exit fills"

    def test_fills_are_entry_sell_and_exit_buy(self):
        """First fill is SELL (short entry), second is BUY (exit)."""
        from trading_engine.domain.enums import Side
        df = _make_gap_fade_candles()
        engine = _make_engine({"TEST": df}, config=self._cfg())
        report = engine.run()
        fills = report.fills
        assert fills[0].side == Side.SELL
        assert fills[1].side == Side.BUY

    def test_first_day_produces_no_fills(self):
        """On first day there is no prior close, so no trade can happen."""
        single_day = _make_gap_fade_candles().iloc[:1]  # only day-1 bar
        engine = _make_engine({"TEST": single_day}, config=self._cfg())
        report = engine.run()
        assert len(report.fills) == 0

    def test_report_has_metrics(self):
        df = _make_gap_fade_candles()
        engine = _make_engine({"TEST": df}, config=self._cfg())
        report = engine.run()
        assert hasattr(report, "metrics")
        assert report.metrics is not None

    def test_no_zerodha_or_dotenv_in_strategy(self):
        source = (ROOT / "src" / "trading_engine" / "strategies" / "gap_fade.py").read_text()
        assert "zerodha" not in source.lower()
        assert "kite" not in source.lower()
        assert "load_dotenv" not in source
```

- [ ] **Step 2: Run integration test to verify it fails**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade_backtest.py -v 2>&1 | head -20
```

Expected: `ImportError` or test failures because the strategy was already written — tests may pass. If so, proceed to the run script.

- [ ] **Step 3: Run full integration suite**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade_backtest.py -v 2>&1 | tail -15
```

Expected: all 6 tests PASS.

- [ ] **Step 4: Write run_gap_fade_backtest.py**

```python
# scripts/run_gap_fade_backtest.py
"""Run Gap Fade to VWAP / Prior Close backtest on locally stored Parquet candle data.

No broker API calls are made.  No live orders are placed.
Reads candle files from data/candles/NSE/{SYMBOL}/{interval}.parquet.

Usage:
    python3 scripts/run_gap_fade_backtest.py
    python3 scripts/run_gap_fade_backtest.py --symbols RELIANCE TCS
    python3 scripts/run_gap_fade_backtest.py --target-mode prior_close
    python3 scripts/run_gap_fade_backtest.py --long-only
    python3 scripts/run_gap_fade_backtest.py --output report.json
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402 — after sys.path patch

from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.data_feed import HistoricalDataFeed  # noqa: E402
from trading_engine.backtest.engine import BacktestEngine  # noqa: E402
from trading_engine.backtest.portfolio import BacktestPortfolio  # noqa: E402
from trading_engine.backtest.simulated_broker import SimulatedBroker  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.strategies.gap_fade import GapFadeConfig, GapFadeStrategy  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = [
    "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK",
    "HDFC", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "SBIN",
]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT = ROOT / "reports" / "gap_fade_report.json"


def _build_config(args: argparse.Namespace) -> GapFadeConfig:
    return GapFadeConfig(
        strategy_id="gap_fade_v1",
        quantity=args.quantity,
        min_gap_bps=args.min_gap_bps,
        max_gap_bps=args.max_gap_bps,
        fade_trigger_bps=args.fade_trigger_bps,
        stop_loss_bps=args.stop_loss_bps,
        target_mode=args.target_mode,
        require_vwap_confirmation=not args.no_vwap_confirmation,
        allow_long_fades=not args.short_only,
        allow_short_fades=not args.long_only,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Gap Fade to VWAP / Prior Close backtest on local Parquet data."
    )
    parser.add_argument("--symbols", nargs="+", default=_DEFAULT_SYMBOLS)
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument("--initial-cash", dest="initial_cash", type=float,
                        default=float(_DEFAULT_INITIAL_CASH))
    parser.add_argument("--quantity", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument("--min-gap-bps", dest="min_gap_bps", type=float, default=60.0)
    parser.add_argument("--max-gap-bps", dest="max_gap_bps", type=float, default=300.0)
    parser.add_argument("--fade-trigger-bps", dest="fade_trigger_bps", type=float, default=20.0)
    parser.add_argument("--stop-loss-bps", dest="stop_loss_bps", type=float, default=80.0)
    parser.add_argument("--target-mode", dest="target_mode", default="vwap",
                        choices=["vwap", "prior_close", "half_gap"])
    parser.add_argument("--no-vwap-confirmation", dest="no_vwap_confirmation",
                        action="store_true", default=False)
    parser.add_argument("--long-only", dest="long_only", action="store_true", default=False,
                        help="Only fade gap-downs (long trades)")
    parser.add_argument("--short-only", dest="short_only", action="store_true", default=False,
                        help="Only fade gap-ups (short trades)")
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    data_dir = Path(args.data_dir)
    initial_cash = Decimal(str(args.initial_cash))
    output_path = Path(args.output)
    interval = args.interval

    print("\nGap Fade to VWAP / Prior Close Backtest")
    print(f"  Requested symbols:    {args.symbols}")
    print(f"  Data dir:             {data_dir}")
    print(f"  Interval:             {interval}")
    print(f"  Initial cash:         {initial_cash}")
    print(f"  Quantity:             {args.quantity}")
    print(f"  min_gap_bps:          {args.min_gap_bps}")
    print(f"  max_gap_bps:          {args.max_gap_bps}")
    print(f"  fade_trigger_bps:     {args.fade_trigger_bps}")
    print(f"  stop_loss_bps:        {args.stop_loss_bps}")
    print(f"  target_mode:          {args.target_mode}")
    print(f"  VWAP confirmation:    {not args.no_vwap_confirmation}")
    print(f"  long_only:            {args.long_only}")
    print(f"  short_only:           {args.short_only}")

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
    strategy = GapFadeStrategy(config=config)
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
            "fade_trigger_bps": config.fade_trigger_bps,
            "stop_loss_bps": config.stop_loss_bps,
            "target_mode": config.target_mode,
        },
    )

    print(f"\nRunning backtest on {list(candles.keys())} ...")
    report = engine.run()

    m = report.metrics
    print(f"\n{'=' * 55}")
    print(f"Strategy : {report.strategy_id}")
    print(f"Period   : {report.start_time} → {report.end_time}")
    print(f"Symbols  : {report.symbols}")
    print(f"Fills    : {len(report.fills)}")
    print(f"Equity   : {report.initial_cash} → {report.final_equity}")
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

- [ ] **Step 5: Verify run script is importable**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -c "import scripts.run_gap_fade_backtest" 2>&1 || python3 scripts/run_gap_fade_backtest.py --help 2>&1 | head -5
```

Expected: help text printed without errors.

- [ ] **Step 6: Run ruff on all new files**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m ruff check --fix tests/unit/strategies/test_gap_fade_backtest.py scripts/run_gap_fade_backtest.py
python3 -m ruff format tests/unit/strategies/test_gap_fade_backtest.py scripts/run_gap_fade_backtest.py
```

- [ ] **Step 7: Run all gap fade tests**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py tests/unit/strategies/test_gap_fade_backtest.py -v 2>&1 | tail -15
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add tests/unit/strategies/test_gap_fade_backtest.py scripts/run_gap_fade_backtest.py
git commit -m "Add BacktestEngine integration test and run_gap_fade_backtest.py script"
```

---

### Task 6: Sweep script + script unit tests

**Files:**
- Create: `scripts/sweep_gap_fade_params.py`
- Create: `tests/unit/scripts/test_gap_fade_scripts.py`

- [ ] **Step 1: Write failing tests for sweep script functions**

```python
# tests/unit/scripts/test_gap_fade_scripts.py
"""Unit tests for sweep_gap_fade_params.py CLI functions."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from sweep_gap_fade_params import (  # noqa: E402
    PARAM_GRID,
    build_grid,
    load_candles,
)


class TestBuildGrid:
    def test_default_grid_has_correct_total(self):
        """Default grid: 4*3*3*3*3 = 324 combos."""
        combos = build_grid()
        assert len(combos) == 324

    def test_max_combinations_limits_output(self):
        combos = build_grid(max_combinations=50)
        assert len(combos) == 50

    def test_each_combo_has_all_param_keys(self):
        keys = set(PARAM_GRID.keys())
        for combo in build_grid(max_combinations=5):
            assert set(combo.keys()) == keys

    def test_custom_grid_overrides_default(self):
        custom = {"min_gap_bps": [60, 80], "stop_loss_bps": [80]}
        combos = build_grid(grid=custom)
        assert len(combos) == 2

    def test_max_combinations_none_returns_all(self):
        combos = build_grid(max_combinations=None)
        assert len(combos) == 324


class TestLoadCandles:
    def test_missing_symbol_skipped(self, tmp_path):
        candles = load_candles(["NONEXISTENT"], tmp_path, "minute")
        assert candles == {}

    def test_parquet_loaded_correctly(self, tmp_path):
        symbol = "TESTX"
        data_path = tmp_path / "candles" / "NSE" / symbol
        data_path.mkdir(parents=True)
        df = pd.DataFrame({
            "timestamp": pd.date_range("2024-01-15 09:15", periods=5, freq="1min"),
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.0] * 5,
            "volume": [1000] * 5,
        })
        df.to_parquet(data_path / "minute.parquet")
        candles = load_candles([symbol], tmp_path, "minute")
        assert symbol in candles
        assert len(candles[symbol]) == 5


class TestNoLiveTradingInScripts:
    def test_no_zerodha_in_sweep_script(self):
        source = (ROOT / "scripts" / "sweep_gap_fade_params.py").read_text()
        assert "zerodha" not in source.lower()
        assert "kite" not in source.lower()

    def test_no_dotenv_in_run_script(self):
        source = (ROOT / "scripts" / "run_gap_fade_backtest.py").read_text()
        assert "load_dotenv" not in source
        assert "import dotenv" not in source
```

- [ ] **Step 2: Run tests to see them fail**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/scripts/test_gap_fade_scripts.py -v 2>&1 | head -15
```

Expected: `ModuleNotFoundError: No module named 'sweep_gap_fade_params'`

- [ ] **Step 3: Write sweep_gap_fade_params.py**

```python
# scripts/sweep_gap_fade_params.py
"""Gap Fade parameter sweep.

Runs BacktestEngine over a grid of GapFadeConfig parameters using locally
stored Parquet candle data.  Results are saved to CSV and JSON.

No live trading.  No broker API calls.  No credentials required.

Usage:
    python3 scripts/sweep_gap_fade_params.py
    python3 scripts/sweep_gap_fade_params.py --fast
    python3 scripts/sweep_gap_fade_params.py --max-combinations 50
    python3 scripts/sweep_gap_fade_params.py --output-dir /tmp/results

WARNING: all results are IN-SAMPLE only.  Do not use to size or place live trades.
"""

from __future__ import annotations

import argparse
import json
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
from trading_engine.strategies.gap_fade import GapFadeConfig, GapFadeStrategy  # noqa: E402

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_SYMBOLS = [
    "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK",
    "HDFC", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "SBIN",
]
_FAST_SYMBOLS = ["RELIANCE", "TCS", "ICICIBANK"]
_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10
_DEFAULT_OUTPUT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Parameter grid
# ---------------------------------------------------------------------------

PARAM_GRID: dict[str, list] = {
    "min_gap_bps": [40, 60, 80, 120],
    "max_gap_bps": [200, 300, 500],
    "fade_trigger_bps": [10, 20, 40],
    "stop_loss_bps": [60, 80, 120],
    "target_mode": ["vwap", "prior_close", "half_gap"],
}
# 4 * 3 * 3 * 3 * 3 = 324 total combinations.

# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


_ZERO = Decimal("0")


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
        cfg = GapFadeConfig(
            strategy_id=f"gf_sweep_{run_index}",
            quantity=quantity,
            min_gap_bps=float(params["min_gap_bps"]),
            max_gap_bps=float(params["max_gap_bps"]),
            fade_trigger_bps=float(params["fade_trigger_bps"]),
            stop_loss_bps=float(params["stop_loss_bps"]),
            target_mode=str(params["target_mode"]),
        )
    except ValueError as exc:
        return {**params, "error": str(exc), "total_pnl": None, "trade_count": None}

    strategy = GapFadeStrategy(config=cfg)
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


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gap Fade parameter sweep.")
    parser.add_argument("--symbols", nargs="+", default=None,
                        help="Symbols (default: 10 symbols, or 3 in --fast mode)")
    parser.add_argument("--data-dir", dest="data_dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--interval", default=_DEFAULT_INTERVAL)
    parser.add_argument("--initial-cash", dest="initial_cash", type=float,
                        default=float(_DEFAULT_INITIAL_CASH))
    parser.add_argument("--quantity", type=int, default=_DEFAULT_QUANTITY)
    parser.add_argument("--fast", action="store_true", default=False,
                        help="Fast mode: 3 symbols, up to 50 combinations")
    parser.add_argument("--max-combinations", dest="max_combinations", type=int, default=None)
    parser.add_argument("--output-dir", dest="output_dir", default=str(_DEFAULT_OUTPUT_DIR))
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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
        max_combinations = max_combinations or 50

    symbols = symbols or _DEFAULT_SYMBOLS

    print("\nGap Fade Parameter Sweep")
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

    csv_path = output_dir / "gap_fade_sweep_results.csv"
    json_path = output_dir / "gap_fade_sweep_results.json"
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
    print(top10[["min_gap_bps", "max_gap_bps", "fade_trigger_bps",
                  "stop_loss_bps", "target_mode", "total_pnl",
                  "win_rate", "trade_count"]].to_string(index=False))
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the script tests**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/scripts/test_gap_fade_scripts.py -v 2>&1 | tail -20
```

Expected: all 7 tests PASS.

- [ ] **Step 5: Verify sweep script CLI**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 scripts/sweep_gap_fade_params.py --help 2>&1 | head -5
```

Expected: help text printed without errors.

- [ ] **Step 6: Run ruff**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m ruff check --fix scripts/sweep_gap_fade_params.py tests/unit/scripts/test_gap_fade_scripts.py
python3 -m ruff format scripts/sweep_gap_fade_params.py tests/unit/scripts/test_gap_fade_scripts.py
```

- [ ] **Step 7: Run all gap fade tests together**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/strategies/test_gap_fade.py tests/unit/strategies/test_gap_fade_backtest.py tests/unit/scripts/test_gap_fade_scripts.py -v 2>&1 | tail -15
```

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add scripts/sweep_gap_fade_params.py tests/unit/scripts/test_gap_fade_scripts.py
git commit -m "Add sweep_gap_fade_params.py and script unit tests"
```

---

### Task 7: README Gap Fade section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read README to find the right insertion point**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
grep -n "First.Hour\|FHM\|Momentum\|## Str\|## Back\|## Run\|## Script" README.md | head -20
```

- [ ] **Step 2: Add Gap Fade section after the First-Hour Momentum section**

Find the last heading under the First-Hour Momentum strategy section and insert the following after it. The exact insertion point depends on the README structure (check in Step 1). Add the following block:

```markdown
## Gap Fade to VWAP / Prior Close Strategy

### Why a new strategy?

Extended validation of First-Hour Momentum (FHM) on ICICIBANK and TCS showed:
- **Insufficient evidence**: only 32 trades over the full period for the best ICICIBANK config — far below the 100-trade threshold needed for statistical confidence.
- **Slippage sensitivity**: the ICICIBANK base config produced +4.6 INR net at 2 bps slippage, turning negative at +1 tick (+3 bps) — unusable in practice.
- **Conclusion**: FHM requires stronger market structure (higher RVOL, more trend days) than the available data exhibits.

**Gap Fade** takes the opposite approach: mean reversion. When a stock opens with a significant gap (60–300 bps) from the previous close, it tends to partially retrace. The strategy fades the gap — entering long on gap-downs or short on gap-ups — and exits when price reaches the session VWAP, the prior close, or the half-gap level.

### Strategy overview

1. **Prior close tracking**: the previous day's last bar close is carried as `prior_close`.
2. **Gap detection**: `gap_bps = (opening_price / prior_close − 1) × 10000`. First day always skipped.
3. **Qualifying gap**: `min_gap_bps (60) ≤ |gap_bps| ≤ max_gap_bps (300)`.
4. **Fade trigger**: price must reverse `fade_trigger_bps (20)` from opening_price before entry.
5. **Optional VWAP confirmation**: long fades require `close > VWAP`; short fades require `close < VWAP`.
6. **Exits**: stop-loss (80 bps), dynamic target (VWAP cross / prior close touch / half-gap), or square-off at 15:15.

### Running the backtest

```bash
# Default 10 symbols, vwap target mode
python3 scripts/run_gap_fade_backtest.py

# Custom configuration
python3 scripts/run_gap_fade_backtest.py \
    --symbols RELIANCE TCS INFY \
    --min-gap-bps 80 \
    --max-gap-bps 400 \
    --target-mode prior_close \
    --stop-loss-bps 100 \
    --output reports/my_gap_fade.json
```

### Running the parameter sweep

```bash
# Full sweep (324 combinations × 10 symbols — can take several minutes)
python3 scripts/sweep_gap_fade_params.py

# Fast mode: 3 symbols × 50 combinations for quick exploration
python3 scripts/sweep_gap_fade_params.py --fast --max-combinations 50

# Custom output directory
python3 scripts/sweep_gap_fade_params.py --output-dir /tmp/sweep_results
```

Sweep results are saved to `reports/gap_fade_sweep_results.csv` and `.json`. The top-10 combinations by total P&L are printed to stdout.

### Sweep grid

| Parameter | Values |
|-----------|--------|
| `min_gap_bps` | 40, 60, 80, 120 |
| `max_gap_bps` | 200, 300, 500 |
| `fade_trigger_bps` | 10, 20, 40 |
| `stop_loss_bps` | 60, 80, 120 |
| `target_mode` | vwap, prior_close, half_gap |
```

- [ ] **Step 3: Verify README renders correctly**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -c "
text = open('README.md').read()
assert 'Gap Fade' in text
assert 'prior_close' in text
assert 'sweep_gap_fade_params' in text
print('README check OK')
"
```

- [ ] **Step 4: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add README.md
git commit -m "Add gap fade strategy and scripts

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Self-Review

### Spec coverage

| Spec requirement | Covered in |
|---|---|
| `GapFadeConfig` with all listed params | Task 1 |
| `GapFadeStrategy` with `on_bar` | Task 1 |
| Prior close tracking (inter-day carry) | Task 1 + tested in Task 2 |
| Gap detection with min/max/abs/volume filters | Task 1 + tested in Task 2 |
| Fade trigger (long + short) | Task 1 + tested in Task 3 |
| VWAP confirmation | Task 1 + tested in Task 3 |
| Stop-loss exit | Task 1 + tested in Task 4 |
| Dynamic targets: vwap/prior_close/half_gap | Task 1 + tested in Task 4 |
| Fixed `target_bps` override | Task 1 |
| Square-off at 15:15 | Task 1 + tested in Task 4 |
| `run_gap_fade_backtest.py` with all CLI args | Task 5 |
| `sweep_gap_fade_params.py` with grid | Task 6 |
| `--fast` flag (3 symbols, 50 combos) | Task 6 |
| Output to CSV + JSON | Task 6 |
| Top-10 table printed | Task 6 |
| Unit tests for strategy | Tasks 1–4 |
| Integration test with BacktestEngine | Task 5 |
| Script unit tests | Task 6 |
| README Gap Fade section | Task 7 |
| No zerodha/kite/dotenv in new files | Tested in Task 5 + 6 |

### Type consistency check

- `GapFadeConfig` → `GapFadeStrategy` constructor accepts it ✓
- `_SymbolState.fade_direction` is `str` ("LONG"/"SHORT") — matches `position_side` assignment in `_set_position_state` ✓
- `_check_entry` returns `OrderIntent | None` — matches `on_bar` signature ✓
- `build_grid(grid=..., max_combinations=...)` — same signature used in tests and `main()` ✓
- `load_candles(symbols, data_dir, interval)` — same signature used in tests and `main()` ✓
