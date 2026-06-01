# Selected First-Hour Extended Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone extended-validation script that runs two selected First-Hour Momentum configs over monthly, quarterly, train/test, and full-period windows with three slippage stress scenarios each, flagging insufficient evidence and stress failures.

**Architecture:** New script `scripts/validate_selected_first_hour.py` imports core helpers (`evaluate_task`, `filter_candles_by_rvol`, `filter_candles_by_date_range`, `load_all_candles`) from the existing validate script; adds `slippage_bps` param to `evaluate_task` (backward-compatible); defines two fixed configs and runs them across all windows × three slippage levels; outputs a flat result list to CSV/JSON.

**Tech Stack:** Python 3.12, pandas, Decimal, existing backtest engine (`evaluate_task`, `SlippageModel`), pytest, ruff.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/validate_first_hour_symbol_specific.py` | Modify | Add `slippage_bps` keyword arg to `evaluate_task` |
| `scripts/validate_selected_first_hour.py` | Create | Selected configs, window builders, orchestrator, print, save, main |
| `tests/unit/scripts/test_selected_first_hour.py` | Create | 22 unit tests covering all new functions |

---

## Task 1: Add `slippage_bps` parameter to `evaluate_task`

**Files:**
- Modify: `scripts/validate_first_hour_symbol_specific.py` (around line 352)
- Test: `tests/unit/scripts/test_selected_first_hour.py` (new file, first two tests)

- [ ] **Step 1: Write failing tests for slippage_bps**

Create `tests/unit/scripts/test_selected_first_hour.py`:

```python
"""Tests for validate_selected_first_hour.py and evaluate_task slippage_bps."""

from __future__ import annotations

import sys
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_first_hour_symbol_specific import evaluate_task  # noqa: E402


# ---------------------------------------------------------------------------
# Shared candle helpers
# ---------------------------------------------------------------------------


def _make_entry_exit_candles_df() -> pd.DataFrame:
    """15 first-window bars (09:15–09:29) trending up ~900 bps, then entry + stop.

    Window: 9:15–9:29, 15 bars of 1-min each.  close goes 100.0→107.0 (+700bps from
    first bar close to last bar close).  fw_return = (107-100)/100 * 10000 = 700bps >> 40.
    Narrow bars (high=c+0.1, low=c-0.1) keep opening_range small.
    Entry bar 9:30: close=107.5 → BUY triggered.
    Stop bar  9:31: low=106.0 < stop(107.5*(1-0.006)=106.855) → SELL at stop.
    """
    rows = []
    for i in range(15):
        c = 100.0 + i * 0.5
        rows.append({
            "timestamp": pd.Timestamp(f"2024-01-15 09:{15 + i:02d}:00"),
            "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1000,
        })
    rows.append({
        "timestamp": pd.Timestamp("2024-01-15 09:30:00"),
        "open": 107.0, "high": 108.5, "low": 106.5, "close": 107.5, "volume": 1000,
    })
    rows.append({
        "timestamp": pd.Timestamp("2024-01-15 09:31:00"),
        "open": 107.5, "high": 108.0, "low": 106.0, "close": 106.5, "volume": 1000,
    })
    return pd.DataFrame(rows)


def _fhm_params() -> dict:
    return {
        "momentum_window_minutes": 15,
        "min_first_window_return_bps": 40.0,
        "latest_entry_time": time(10, 30),
        "stop_loss_bps": 60.0,
        "target_bps": None,
        "allow_shorts": False,
        "max_trades_per_symbol_per_day": 1,
    }


# ---------------------------------------------------------------------------
# TestEvaluateTaskSlippageBps
# ---------------------------------------------------------------------------


class TestEvaluateTaskSlippageBps:
    def test_slippage_bps_param_accepted(self):
        """evaluate_task must accept slippage_bps keyword without error."""
        df = _make_entry_exit_candles_df()
        row = evaluate_task(
            "TEST", _fhm_params(), df, Decimal("100000"), 10, "minute",
            slippage_bps=Decimal("2"),
        )
        assert row.get("error") is None

    def test_higher_slippage_reduces_net_pnl(self):
        """net P&L with bps=10 must be lower (more negative) than bps=2 when trades exist."""
        df = _make_entry_exit_candles_df()
        row_low = evaluate_task(
            "TEST", _fhm_params(), df, Decimal("100000"), 10, "minute",
            slippage_bps=Decimal("2"),
        )
        row_high = evaluate_task(
            "TEST", _fhm_params(), df, Decimal("100000"), 10, "minute",
            slippage_bps=Decimal("10"),
        )
        assert row_low.get("error") is None
        assert row_high.get("error") is None
        if row_low.get("trade_count", 0) > 0:
            assert (row_high.get("total_pnl") or 0) < (row_low.get("total_pnl") or 0)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py::TestEvaluateTaskSlippageBps -v
```

Expected: FAIL with `TypeError: evaluate_task() got an unexpected keyword argument 'slippage_bps'`

- [ ] **Step 3: Add `slippage_bps` parameter to `evaluate_task`**

In `scripts/validate_first_hour_symbol_specific.py`, change the function signature and broker creation:

```python
def evaluate_task(
    symbol: str,
    params: dict,
    symbol_candles: pd.DataFrame,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
    slippage_bps: Decimal = Decimal("2"),
) -> dict:
```

And update the broker line (currently around line 399):
```python
broker = SimulatedBroker(portfolio, CostModel(), SlippageModel(bps=slippage_bps))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py::TestEvaluateTaskSlippageBps -v
```

Expected: 2 passed

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
python3 -m pytest tests/ -q 2>&1 | tail -5
```

Expected: same pass count as before (1687+), same 3 pre-existing failures only.

---

## Task 2: Create new script — constants, `run_config_on_slice`, window builders

**Files:**
- Create: `scripts/validate_selected_first_hour.py`
- Test: `tests/unit/scripts/test_selected_first_hour.py`

- [ ] **Step 1: Write failing tests for run_config_on_slice and window builders**

Append to `tests/unit/scripts/test_selected_first_hour.py`:

```python
from validate_selected_first_hour import (  # noqa: E402
    INSUFFICIENT_EVIDENCE_TRADES,
    SELECTED_CONFIGS,
    SLIPPAGE_SCENARIOS,
    STRESS_MATERIAL_THRESHOLD,
    build_month_windows,
    build_quarter_windows,
    check_insufficient_evidence,
    check_stress_rejection,
    run_config_on_slice,
    run_extended_validation,
)


def _make_flat_candles(n_days: int = 3) -> pd.DataFrame:
    """Flat candles that do not trigger any trades (not enough momentum)."""
    rows = []
    for day in range(n_days):
        d = date(2025, 1, 1 + day)
        for bar in range(30):
            ts = pd.Timestamp(f"{d} 09:{15 + bar:02d}:00")
            rows.append({
                "timestamp": ts, "open": 100.0, "high": 100.1,
                "low": 99.9, "close": 100.0, "volume": 1000,
            })
    return pd.DataFrame(rows)


def _make_multi_month_candles() -> pd.DataFrame:
    """Two months worth of flat bars — Jan and Feb 2025."""
    rows = []
    for month in [1, 2]:
        for day in [2, 3, 6, 7]:
            d = date(2025, month, day)
            for bar in range(5):
                ts = pd.Timestamp(f"{d} 09:{15 + bar:02d}:00")
                rows.append({
                    "timestamp": ts, "open": 100.0, "high": 100.1,
                    "low": 99.9, "close": 100.0, "volume": 1000,
                })
    return pd.DataFrame(rows)


class TestRunConfigOnSlice:
    def _cfg(self) -> dict:
        return SELECTED_CONFIGS[0]  # ICICIBANK RVOL=1.2

    def test_returns_required_keys(self):
        df = _make_flat_candles()
        cfg = self._cfg()
        row = run_config_on_slice(
            config_label=cfg["label"],
            symbol=cfg["symbol"],
            params=cfg["params"],
            min_rvol=None,
            candle_df=df,
            start_date=None,
            end_date=None,
            slippage_label="base",
            slippage_bps=Decimal("2"),
            window_label="full",
            window_type="full",
            initial_cash=Decimal("100000"),
            quantity=10,
            interval="minute",
        )
        for key in ("config_label", "symbol", "window_label", "window_type",
                    "slippage_label", "slippage_bps", "total_pnl", "trade_count",
                    "insufficient_evidence", "stress_rejected"):
            assert key in row, f"missing key: {key}"

    def test_empty_candles_returns_no_data_error(self):
        empty_df = pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        cfg = self._cfg()
        row = run_config_on_slice(
            config_label=cfg["label"], symbol=cfg["symbol"], params=cfg["params"],
            min_rvol=None, candle_df=empty_df, start_date=None, end_date=None,
            slippage_label="base", slippage_bps=Decimal("2"),
            window_label="full", window_type="full",
            initial_cash=Decimal("100000"), quantity=10, interval="minute",
        )
        assert row.get("error") == "no_data"

    def test_date_filter_applied(self):
        """Restricting to a window outside all candles yields no_data error."""
        df = _make_flat_candles(3)  # dates 2025-01-01 to 2025-01-03
        cfg = self._cfg()
        row = run_config_on_slice(
            config_label=cfg["label"], symbol=cfg["symbol"], params=cfg["params"],
            min_rvol=None, candle_df=df,
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
            slippage_label="base", slippage_bps=Decimal("2"),
            window_label="2026-01", window_type="month",
            initial_cash=Decimal("100000"), quantity=10, interval="minute",
        )
        assert row.get("error") == "no_data"

    def test_slippage_label_in_output(self):
        df = _make_flat_candles()
        cfg = self._cfg()
        row = run_config_on_slice(
            config_label=cfg["label"], symbol=cfg["symbol"], params=cfg["params"],
            min_rvol=None, candle_df=df, start_date=None, end_date=None,
            slippage_label="+2tick", slippage_bps=Decimal("4"),
            window_label="full", window_type="full",
            initial_cash=Decimal("100000"), quantity=10, interval="minute",
        )
        assert row.get("slippage_label") == "+2tick"
        assert row.get("slippage_bps") == 4


class TestBuildMonthWindows:
    def test_empty_candles_returns_empty(self):
        empty = {"SYM": pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])}
        assert build_month_windows(empty) == []

    def test_single_month_returns_one_window(self):
        df = _make_flat_candles(3)  # Jan 2025 only
        windows = build_month_windows({"SYM": df})
        assert len(windows) == 1
        label, start, end = windows[0]
        assert label == "2025-01"
        assert start == date(2025, 1, 1)
        assert end == date(2025, 1, 31)

    def test_two_months_returns_two_windows(self):
        df = _make_multi_month_candles()
        windows = build_month_windows({"SYM": df})
        assert len(windows) == 2
        assert windows[0][0] == "2025-01"
        assert windows[1][0] == "2025-02"


class TestBuildQuarterWindows:
    def test_empty_candles_returns_empty(self):
        empty = {"SYM": pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])}
        assert build_quarter_windows(empty) == []

    def test_q1_label_and_bounds(self):
        df = _make_flat_candles(3)  # Jan 2025 → Q1
        windows = build_quarter_windows({"SYM": df})
        assert len(windows) == 1
        label, start, end = windows[0]
        assert label == "Q1-2025"
        assert start == date(2025, 1, 1)
        assert end == date(2025, 3, 31)

    def test_two_months_in_same_quarter_gives_one_window(self):
        df = _make_multi_month_candles()  # Jan + Feb → both Q1-2025
        windows = build_quarter_windows({"SYM": df})
        assert len(windows) == 1
        assert windows[0][0] == "Q1-2025"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py -v 2>&1 | tail -20
```

Expected: ImportError (module `validate_selected_first_hour` doesn't exist yet)

- [ ] **Step 3: Create `scripts/validate_selected_first_hour.py` with constants and core functions**

```python
"""Extended validation for selected First-Hour Momentum configs.

Runs two fixed configs (ICICIBANK RVOL=1.2, TCS no-RVOL) across monthly,
quarterly, train/test, and full-period windows with three slippage stress
scenarios.  No live trading.  No broker calls.  No .env.

Usage:
    python3 scripts/validate_selected_first_hour.py
    python3 scripts/validate_selected_first_hour.py --train-start 2025-01-01 --test-end 2026-01-31
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time as time_mod
from datetime import date, time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

from validate_first_hour_symbol_specific import (  # noqa: E402
    evaluate_task,
    filter_candles_by_date_range,
    filter_candles_by_rvol,
    load_all_candles,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DATA_DIR = ROOT / "data"
_DEFAULT_OUTPUT_DIR = ROOT / "reports"
_DEFAULT_INTERVAL = "minute"
_DEFAULT_INITIAL_CASH = Decimal("500000")
_DEFAULT_QUANTITY = 10

_BASE_PARAMS: dict = {
    "momentum_window_minutes": 15,
    "min_first_window_return_bps": 40.0,
    "latest_entry_time": time(10, 30),
    "stop_loss_bps": 60.0,
    "target_bps": None,
    "allow_shorts": False,
    "max_trades_per_symbol_per_day": 1,
}

SELECTED_CONFIGS: list[dict] = [
    {
        "label": "ICICIBANK_RVOL1.2",
        "symbol": "ICICIBANK",
        "params": _BASE_PARAMS,
        "min_first_window_rvol": 1.2,
    },
    {
        "label": "TCS_noRVOL",
        "symbol": "TCS",
        "params": _BASE_PARAMS,
        "min_first_window_rvol": None,
    },
]

# Slippage stress scenarios.  Each "tick" ≈ 1 extra bps for NSE large-caps
# at ~1000+ INR (conservative approximation; actual tick = 0.05 INR/share).
SLIPPAGE_SCENARIOS: list[tuple[str, Decimal]] = [
    ("base", Decimal("2")),
    ("+1tick", Decimal("3")),
    ("+2tick", Decimal("4")),
]

INSUFFICIENT_EVIDENCE_TRADES: int = 100   # fills (50 round-trips)
STRESS_MATERIAL_THRESHOLD: float = -500.0  # INR; any stress case below this = rejected


# ---------------------------------------------------------------------------
# Window builders
# ---------------------------------------------------------------------------


def build_month_windows(
    candles: dict[str, pd.DataFrame],
) -> list[tuple[str, date, date]]:
    """Return (label, start, end) for every calendar month present in data."""
    all_dates: set[date] = set()
    for df in candles.values():
        ts = df["timestamp"]
        if not pd.api.types.is_datetime64_any_dtype(ts):
            ts = pd.to_datetime(ts)
        all_dates.update(ts.dt.date.unique())

    if not all_dates:
        return []

    months: set[tuple[int, int]] = {(d.year, d.month) for d in all_dates}
    windows = []
    for year, month in sorted(months):
        last_day = calendar.monthrange(year, month)[1]
        start = date(year, month, 1)
        end = date(year, month, last_day)
        windows.append((f"{year}-{month:02d}", start, end))
    return windows


def build_quarter_windows(
    candles: dict[str, pd.DataFrame],
) -> list[tuple[str, date, date]]:
    """Return (label, start, end) for every calendar quarter present in data."""
    all_dates: set[date] = set()
    for df in candles.values():
        ts = df["timestamp"]
        if not pd.api.types.is_datetime64_any_dtype(ts):
            ts = pd.to_datetime(ts)
        all_dates.update(ts.dt.date.unique())

    if not all_dates:
        return []

    _q_start = {1: 1, 2: 4, 3: 7, 4: 10}
    _q_end = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}

    quarters: set[tuple[int, int]] = set()
    for d in all_dates:
        q = (d.month - 1) // 3 + 1
        quarters.add((d.year, q))

    windows = []
    for year, q in sorted(quarters):
        sm = _q_start[q]
        em, ed = _q_end[q]
        start = date(year, sm, 1)
        end = date(year, em, ed)
        windows.append((f"Q{q}-{year}", start, end))
    return windows


# ---------------------------------------------------------------------------
# Core slice runner
# ---------------------------------------------------------------------------


def run_config_on_slice(
    config_label: str,
    symbol: str,
    params: dict,
    min_rvol: float | None,
    candle_df: pd.DataFrame,
    start_date: date | None,
    end_date: date | None,
    slippage_label: str,
    slippage_bps: Decimal,
    window_label: str,
    window_type: str,
    initial_cash: Decimal,
    quantity: int,
    interval: str,
    rvol_lookback_days: int = 20,
) -> dict:
    """Run one config on one date slice with one slippage level. Returns result row."""
    sliced = filter_candles_by_date_range(candle_df, start_date, end_date)

    mwm = int(params.get("momentum_window_minutes", 15))
    if min_rvol is not None:
        sliced = filter_candles_by_rvol(sliced, min_rvol, mwm, rvol_lookback_days)

    _base_row = {
        "config_label": config_label,
        "symbol": symbol,
        "window_label": window_label,
        "window_type": window_type,
        "window_start": start_date,
        "window_end": end_date,
        "slippage_label": slippage_label,
        "slippage_bps": int(slippage_bps),
        "insufficient_evidence": False,
        "stress_rejected": False,
        "stress_reject_reason": None,
    }

    if sliced.empty:
        _base_row["error"] = "no_data"
        _base_row["total_pnl"] = None
        _base_row["gross_pnl"] = None
        _base_row["trade_count"] = None
        _base_row["win_rate"] = None
        _base_row["profit_factor"] = None
        _base_row["insufficient_evidence"] = True
        return _base_row

    metrics = evaluate_task(
        symbol, params, sliced, initial_cash, quantity, interval,
        slippage_bps=slippage_bps,
    )
    _base_row.update(metrics)
    return _base_row


# ---------------------------------------------------------------------------
# Rejection helpers
# ---------------------------------------------------------------------------


def check_insufficient_evidence(
    base_row: dict,
    min_trades: int = INSUFFICIENT_EVIDENCE_TRADES,
) -> bool:
    """Return True if the base scenario has fewer than min_trades fills."""
    tc = base_row.get("trade_count") or 0
    return tc < min_trades


def check_stress_rejection(
    stress_rows: list[dict],
    material_threshold: float = STRESS_MATERIAL_THRESHOLD,
) -> tuple[bool, str | None]:
    """Return (is_rejected, reason) if any stress case is materially negative."""
    for row in stress_rows:
        net = row.get("total_pnl")
        if net is not None and net < material_threshold:
            return True, f"{row['slippage_label']}_net={net:.0f}"
    return False, None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_extended_validation(
    configs: list[dict],
    candles: dict[str, pd.DataFrame],
    windows: list[tuple[str, str, date | None, date | None]],
    initial_cash: Decimal,
    quantity: int,
    interval: str,
    rvol_lookback_days: int = 20,
) -> list[dict]:
    """Run all configs × windows × slippage scenarios.

    windows: list of (window_label, window_type, start_date, end_date)
    Returns flat list of result dicts with evidence/stress flags stamped on each row.
    """
    results: list[dict] = []
    total_runs = len(configs) * len(windows) * len(SLIPPAGE_SCENARIOS)
    done = 0
    start_time = time_mod.time()

    print(
        f"\nRunning {len(configs)} configs × {len(windows)} windows "
        f"× {len(SLIPPAGE_SCENARIOS)} slippage = {total_runs} backtests (sequential)..."
    )

    for cfg in configs:
        sym = cfg["symbol"]
        if sym not in candles:
            print(f"  WARNING: {sym} not in candles — skipping config {cfg['label']}")
            continue
        candle_df = candles[sym]

        for window_label, window_type, start_date, end_date in windows:
            window_rows: list[dict] = []

            for slip_label, slip_bps in SLIPPAGE_SCENARIOS:
                row = run_config_on_slice(
                    config_label=cfg["label"],
                    symbol=sym,
                    params=cfg["params"],
                    min_rvol=cfg.get("min_first_window_rvol"),
                    candle_df=candle_df,
                    start_date=start_date,
                    end_date=end_date,
                    slippage_label=slip_label,
                    slippage_bps=slip_bps,
                    window_label=window_label,
                    window_type=window_type,
                    initial_cash=initial_cash,
                    quantity=quantity,
                    interval=interval,
                    rvol_lookback_days=rvol_lookback_days,
                )
                window_rows.append(row)
                done += 1
                _report_progress(done, total_runs, start_time)

            # Stamp evidence / stress flags on all rows in this window
            base_row = window_rows[0]
            insufficient = check_insufficient_evidence(base_row)
            if insufficient:
                stress_rejected, stress_reason = False, None
            else:
                stress_rejected, stress_reason = check_stress_rejection(window_rows[1:])

            for row in window_rows:
                row["insufficient_evidence"] = insufficient
                row["stress_rejected"] = stress_rejected
                row["stress_reject_reason"] = stress_reason
                results.append(row)

    print()
    return results


def _report_progress(done: int, total: int, start_time: float) -> None:
    elapsed = time_mod.time() - start_time
    avg = elapsed / done if done > 0 else 0
    rem = (total - done) * avg
    print(
        f"\r  Progress: {done}/{total} ({done / total:.1%}) | "
        f"Elapsed: {elapsed:.1f}s | Avg: {avg:.2f}s/t | ETA: {rem:.1f}s",
        end="",
        flush=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py::TestRunConfigOnSlice \
    tests/unit/scripts/test_selected_first_hour.py::TestBuildMonthWindows \
    tests/unit/scripts/test_selected_first_hour.py::TestBuildQuarterWindows -v
```

Expected: 10 passed

---

## Task 3: Rejection rules tests + `run_extended_validation` tests

**Files:**
- Test: `tests/unit/scripts/test_selected_first_hour.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/scripts/test_selected_first_hour.py`:

```python
class TestCheckInsufficientEvidence:
    def test_trades_below_threshold_is_insufficient(self):
        row = {"trade_count": 50}
        assert check_insufficient_evidence(row) is True

    def test_trades_at_threshold_is_not_insufficient(self):
        row = {"trade_count": 100}
        assert check_insufficient_evidence(row) is False

    def test_none_trade_count_treated_as_zero(self):
        row = {"trade_count": None}
        assert check_insufficient_evidence(row) is True


class TestCheckStressRejection:
    def test_no_stress_rows_returns_no_rejection(self):
        rejected, reason = check_stress_rejection([])
        assert rejected is False
        assert reason is None

    def test_stress_above_threshold_not_rejected(self):
        rows = [
            {"slippage_label": "+1tick", "total_pnl": -200.0},
            {"slippage_label": "+2tick", "total_pnl": -400.0},
        ]
        rejected, _ = check_stress_rejection(rows, material_threshold=-500.0)
        assert rejected is False

    def test_stress_below_threshold_is_rejected(self):
        rows = [
            {"slippage_label": "+1tick", "total_pnl": -300.0},
            {"slippage_label": "+2tick", "total_pnl": -600.0},
        ]
        rejected, reason = check_stress_rejection(rows, material_threshold=-500.0)
        assert rejected is True
        assert "+2tick" in reason

    def test_rejection_reason_includes_slippage_label_and_net(self):
        rows = [{"slippage_label": "+1tick", "total_pnl": -999.0}]
        _, reason = check_stress_rejection(rows, material_threshold=-500.0)
        assert reason is not None
        assert "+1tick" in reason
        assert "-999" in reason


class TestRunExtendedValidation:
    def _minimal_candles(self) -> dict[str, pd.DataFrame]:
        """Flat candles for both symbols — no trades triggered."""
        df = _make_flat_candles(3)
        return {"ICICIBANK": df.copy(), "TCS": df.copy()}

    def _single_window(self) -> list[tuple[str, str, date | None, date | None]]:
        return [("full", "full", None, None)]

    def test_result_count_equals_configs_times_windows_times_slippage(self):
        candles = self._minimal_candles()
        windows = self._single_window()
        results = run_extended_validation(
            SELECTED_CONFIGS, candles, windows,
            Decimal("100000"), 10, "minute",
        )
        expected = len(SELECTED_CONFIGS) * len(windows) * len(SLIPPAGE_SCENARIOS)
        assert len(results) == expected

    def test_each_row_has_slippage_label(self):
        candles = self._minimal_candles()
        windows = self._single_window()
        results = run_extended_validation(
            SELECTED_CONFIGS, candles, windows,
            Decimal("100000"), 10, "minute",
        )
        slip_labels = {r["slippage_label"] for r in results}
        assert slip_labels == {"base", "+1tick", "+2tick"}

    def test_no_trades_flags_insufficient_evidence(self):
        """Flat candles → no trades → trade_count=0 < 100 → insufficient_evidence=True."""
        candles = self._minimal_candles()
        windows = self._single_window()
        results = run_extended_validation(
            SELECTED_CONFIGS, candles, windows,
            Decimal("100000"), 10, "minute",
        )
        for row in results:
            assert row["insufficient_evidence"] is True

    def test_missing_symbol_skipped(self):
        """If a symbol is not in candles, its config produces no results."""
        # Only TCS available
        df = _make_flat_candles(3)
        candles = {"TCS": df}
        windows = self._single_window()
        results = run_extended_validation(
            SELECTED_CONFIGS, candles, windows,
            Decimal("100000"), 10, "minute",
        )
        # Only TCS_noRVOL config runs → 1 config × 1 window × 3 scenarios = 3 rows
        assert len(results) == 3
        assert all(r["config_label"] == "TCS_noRVOL" for r in results)


class TestNoLiveTradingImports:
    def test_no_zerodha_in_new_script(self):
        script_path = ROOT / "scripts" / "validate_selected_first_hour.py"
        source = script_path.read_text()
        assert "zerodha" not in source.lower()
        assert "kite" not in source.lower()
        assert "login" not in source.lower()

    def test_no_dotenv_in_new_script(self):
        script_path = ROOT / "scripts" / "validate_selected_first_hour.py"
        source = script_path.read_text()
        assert ".env" not in source
        assert "load_dotenv" not in source
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py::TestCheckInsufficientEvidence \
    tests/unit/scripts/test_selected_first_hour.py::TestCheckStressRejection \
    tests/unit/scripts/test_selected_first_hour.py::TestRunExtendedValidation \
    tests/unit/scripts/test_selected_first_hour.py::TestNoLiveTradingImports -v 2>&1 | tail -15
```

Expected: FAIL (functions not yet defined for some, or script doesn't have all symbols)

- [ ] **Step 3: The implementation is already complete from Task 2**

All functions (`check_insufficient_evidence`, `check_stress_rejection`, `run_extended_validation`) are already written. Re-run to confirm.

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py -v 2>&1 | tail -30
```

Expected: all 22 tests passed

---

## Task 4: Add print, save, and main() to new script

**Files:**
- Modify: `scripts/validate_selected_first_hour.py`

- [ ] **Step 1: Append print, save, and main() to the script**

Append to `scripts/validate_selected_first_hour.py`:

```python
# ---------------------------------------------------------------------------
# Print / save
# ---------------------------------------------------------------------------


def print_extended_validation_results(results: list[dict]) -> None:
    """Print results grouped by window_type then config_label."""
    if not results:
        print("No results to display.")
        return

    def _n(v, fmt: str = ".1f") -> str:
        if v is None:
            return "None"
        try:
            return format(float(v), fmt)
        except (TypeError, ValueError):
            return str(v)

    def _flag(row: dict) -> str:
        if row.get("error") == "no_data":
            return "NO_DATA"
        if row.get("insufficient_evidence"):
            return "INSUFF"
        if row.get("stress_rejected"):
            return f"STRESS_FAIL({row.get('stress_reject_reason', '')})"
        return "OK"

    window_types = ["month", "quarter", "split", "full"]
    for wtype in window_types:
        type_rows = [r for r in results if r.get("window_type") == wtype]
        if not type_rows:
            continue

        print(f"\n{'=' * 70}")
        print(f"=== {wtype.upper()} WINDOWS ===")
        print(f"{'=' * 70}")

        config_labels = sorted({r["config_label"] for r in type_rows})
        for cfg_label in config_labels:
            print(f"\n  {cfg_label}:")
            cfg_rows = [r for r in type_rows if r["config_label"] == cfg_label]
            window_labels = sorted({r["window_label"] for r in cfg_rows})
            for wlabel in window_labels:
                w_rows = [r for r in cfg_rows if r["window_label"] == wlabel]
                base = next((r for r in w_rows if r["slippage_label"] == "base"), None)
                flag = _flag(base) if base else "?"
                parts = []
                for row in sorted(w_rows, key=lambda r: r.get("slippage_bps", 0)):
                    net = _n(row.get("total_pnl"))
                    sl = row.get("slippage_label", "?")
                    parts.append(f"{sl}={net}")
                tc = (base or {}).get("trade_count")
                print(
                    f"    {wlabel:<12} tc={str(tc):<5} "
                    + "  ".join(parts)
                    + f"  [{flag}]"
                )

    # Summary verdict per config
    print(f"\n{'=' * 70}")
    print("=== VERDICT SUMMARY ===")
    config_labels = sorted({r["config_label"] for r in results})
    for cfg_label in config_labels:
        cfg_rows = [r for r in results if r["config_label"] == cfg_label]
        full_rows = [r for r in cfg_rows if r.get("window_type") == "full" and r.get("slippage_label") == "base"]
        if not full_rows:
            print(f"  {cfg_label}: no full-period base result")
            continue
        full = full_rows[0]
        net = full.get("total_pnl")
        tc = full.get("trade_count") or 0
        flag = _flag(full)
        print(
            f"  {cfg_label}: full_net={_n(net)}  trades={tc}  status={flag}"
        )
        # Count how many windows are OK vs problem
        base_rows = [r for r in cfg_rows if r.get("slippage_label") == "base"]
        ok = sum(1 for r in base_rows if _flag(r) == "OK")
        total_w = len(base_rows)
        print(f"    Windows OK: {ok}/{total_w}")


def save_results(
    results: list[dict],
    output_dir: Path,
    prefix: str = "selected_first_hour_validation",
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{prefix}.csv"
    json_path = output_dir / f"{prefix}.json"

    pd.DataFrame(results).to_csv(csv_path, index=False)
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)

    print(f"\nSaved {len(results)} rows to:")
    print(f"  {csv_path}")
    print(f"  {json_path}")
    return csv_path, json_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Extended validation for selected First-Hour Momentum configs."
    )
    parser.add_argument("--data-dir", default=str(_DEFAULT_DATA_DIR))
    parser.add_argument("--output-dir", default=str(_DEFAULT_OUTPUT_DIR))
    parser.add_argument("--rvol-lookback-days", type=int, default=20)
    parser.add_argument("--train-start", type=date.fromisoformat, default=date(2025, 1, 1))
    parser.add_argument("--train-end", type=date.fromisoformat, default=date(2025, 9, 30))
    parser.add_argument("--test-start", type=date.fromisoformat, default=date(2025, 10, 1))
    parser.add_argument("--test-end", type=date.fromisoformat, default=date(2026, 1, 31))
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)

    symbols = list({cfg["symbol"] for cfg in SELECTED_CONFIGS})
    print(f"Loading candles for: {symbols}")
    candles = load_all_candles(symbols, data_dir, _DEFAULT_INTERVAL)
    if not candles:
        print("No candle data found. Exiting.")
        sys.exit(1)

    # Build all windows
    all_windows: list[tuple[str, str, date | None, date | None]] = []

    for label, start, end in build_month_windows(candles):
        all_windows.append((label, "month", start, end))

    for label, start, end in build_quarter_windows(candles):
        all_windows.append((label, "quarter", start, end))

    all_windows.append(("train", "split", args.train_start, args.train_end))
    all_windows.append(("test", "split", args.test_start, args.test_end))
    all_windows.append(("full", "full", None, None))

    print(f"\nWindow summary: {len(all_windows)} windows")
    print(f"  Months:   {sum(1 for w in all_windows if w[1] == 'month')}")
    print(f"  Quarters: {sum(1 for w in all_windows if w[1] == 'quarter')}")
    print(f"  Split:    {sum(1 for w in all_windows if w[1] == 'split')}")
    print(f"  Full:     {sum(1 for w in all_windows if w[1] == 'full')}")

    results = run_extended_validation(
        SELECTED_CONFIGS,
        candles,
        all_windows,
        _DEFAULT_INITIAL_CASH,
        _DEFAULT_QUANTITY,
        _DEFAULT_INTERVAL,
        rvol_lookback_days=args.rvol_lookback_days,
    )

    print_extended_validation_results(results)
    save_results(results, output_dir)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run full test suite to confirm no regressions**

```bash
python3 -m pytest tests/unit/scripts/test_selected_first_hour.py -v 2>&1 | tail -30
```

Expected: all 22 tests pass

- [ ] **Step 3: Run ruff on all modified/created files**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
ruff check --fix scripts/validate_first_hour_symbol_specific.py scripts/validate_selected_first_hour.py tests/unit/scripts/test_selected_first_hour.py
ruff format scripts/validate_first_hour_symbol_specific.py scripts/validate_selected_first_hour.py tests/unit/scripts/test_selected_first_hour.py
```

Expected: no errors; "N files reformatted" or "N files already formatted"

- [ ] **Step 4: Run ruff check one more time to confirm clean**

```bash
ruff check scripts/validate_first_hour_symbol_specific.py scripts/validate_selected_first_hour.py tests/unit/scripts/test_selected_first_hour.py
```

Expected: no output (all clean)

- [ ] **Step 5: Run full test suite**

```bash
python3 -m pytest tests/ -q 2>&1 | tail -5
```

Expected: 1709+ passed, same 3 pre-existing failures only.

- [ ] **Step 6: Commit**

```bash
cd "/mnt/c/Users/Manan Sharma/Desktop/Coding projects/Technical trading/GPT Build Pack"
git add scripts/validate_first_hour_symbol_specific.py \
        scripts/validate_selected_first_hour.py \
        tests/unit/scripts/test_selected_first_hour.py
git commit -m "$(cat <<'EOF'
Add selected first-hour extended validation

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
git push
```

Expected: `main` updated on remote.

---

## Self-Review

### 1. Spec coverage

| Requirement | Covered by |
|---|---|
| Run selected configs only, no broad parameter grid | `SELECTED_CONFIGS` fixed list; no grid sweep |
| ICICIBANK RVOL=1.2 config | `SELECTED_CONFIGS[0]` |
| TCS no-RVOL config | `SELECTED_CONFIGS[1]` |
| Support custom date windows | `--train-start/end`, `--test-start/end` CLI args |
| Report by month | `build_month_windows` + `window_type="month"` |
| Report by quarter | `build_quarter_windows` + `window_type="quarter"` |
| Report by train/test | hardcoded "train"/"test" windows in `main` |
| Report by full year if data exists | `window_type="full"` (full period, not strictly annual — acceptable given data may not span exact years) |
| Slippage stress: base, +1tick, +2tick | `SLIPPAGE_SCENARIOS` with bps 2/3/4 |
| Reject if any stress case materially negative | `check_stress_rejection` + `STRESS_MATERIAL_THRESHOLD=-500` |
| Mark insufficient evidence if trades < 100 | `check_insufficient_evidence` + `INSUFFICIENT_EVIDENCE_TRADES=100` |
| Output reports/selected_first_hour_validation.csv/json | `save_results` with prefix |
| No live trading / No Zerodha / No .env | confirmed in tests + no such imports |
| Tests + ruff | 22 tests + ruff steps |
| Commit "Add selected first-hour extended validation" | Step 6 of Task 4 |

### 2. Placeholder scan
None found.

### 3. Type consistency
- `check_stress_rejection` returns `tuple[bool, str | None]` — used consistently in `run_extended_validation`
- `build_month_windows` / `build_quarter_windows` return `list[tuple[str, date, date]]` — consumed correctly in `main` with 4-tuple expansion
- `evaluate_task` gains `slippage_bps: Decimal = Decimal("2")` — all existing callers unaffected (default unchanged)
