# Stock Trading Engine

A personal Zerodha-connected Indian equity intraday trading engine.

**Asset class:** NSE cash equities only.
**Broker:** Zerodha Kite Connect.
**Style:** Intraday day trading (MIS product).
**Modes:** Backtest → Paper trading → Live trading.

---

## Safety warning

> **Live trading is disabled by default.**
>
> `LIVE_TRADING_ENABLED` defaults to `false`. The broker interface raises
> `LiveTradingDisabledError` on any order placement attempt. Live execution
> requires a complete order manager, risk engine, and reconciliation system
> (Milestone 9) before this flag can safely be set to `true`.

---

## Current milestone

**Milestone 16 — Live Order Execution Pilot** (complete)

Added a complete paper trading engine that simulates strategy execution against
pre-loaded bar data. No Zerodha WebSocket. No real order placement. No credentials.

**What was added:**

| Component | Location | Description |
|---|---|---|
| `PaperMarketFeed` | `paper/market_feed.py` | Yields bars from `list[Bar]` or DataFrame, globally time-sorted |
| `PaperPortfolio` | `paper/portfolio.py` | Subclass of `BacktestPortfolio`; tracks cash, positions, P&L |
| `PaperExecutionBroker` | `paper/broker.py` | Fills MARKET / LIMIT orders against synthetic bars |
| `PaperTradingReport` | `paper/report.py` | JSON-serialisable report (no metrics — forward-only mode) |
| `PaperTradingEngine` | `paper/engine.py` | Main loop: feed → strategy → risk → broker → report |
| `events.py` | `paper/events.py` | Frozen event dataclasses for paper mode |

**Difference between backtesting and paper mode:**

| | Backtest | Paper |
|---|---|---|
| Data source | `HistoricalDataFeed` (DataFrame) | `PaperMarketFeed` (Bar list or DataFrame) |
| Portfolio | `BacktestPortfolio` | `PaperPortfolio` (subclass) |
| Execution | `SimulatedBroker` | `PaperExecutionBroker` |
| Report | `BacktestReport` (with metrics) | `PaperTradingReport` (no metrics) |
| Intended use | Historical evaluation | Forward simulation |

**How paper mode stays safe:**

- No Zerodha SDK is imported anywhere in `src/trading_engine/paper/`.
- No real order placement — `PaperExecutionBroker` fills against synthetic bars only.
- No credentials required — runs fully offline.
- No Zerodha WebSocket — bar delivery is synchronous from pre-loaded data.
- Live order placement still blocked by `LiveTradingDisabledError` in `broker/paper.py`.

**Naming clarity:**

- `broker/paper.py` — `PaperBroker`: the Broker-interface stub (connection lifecycle only, no fills).
- `paper/broker.py` — `PaperExecutionBroker`: the execution simulator (fills orders).

```bash
# Run all tests (606 total, all pass)
python3 -m pytest -v

# Style checks
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts

# Optional: run ORB in paper mode using local Parquet data
# (exits cleanly if no local data exists)
python3 scripts/run_paper_orb.py
```

---

**Milestone 7 — Risk Engine v1** (complete)

Added a configurable pre-trade risk engine that sits between strategy `OrderIntent` output and `SimulatedBroker` execution. No Zerodha imports. Reusable across backtest, paper, and live modes.

**What the risk engine checks (first failing check wins):**

1. Kill switch active
2. Symbol not in `allowed_symbols`
3. Product type not in `allowed_product_types`
4. Order type not in `allowed_order_types`
5. Order value > `max_order_value`
6. Open position count ≥ `max_open_positions` (new symbol BUY only)
7. Daily loss > `max_daily_loss` (realized + unrealized)
8. Trades today ≥ `max_trades_per_day`
9. Orders this second ≥ `max_orders_per_second`
10. Approved

**Key components:**

| Class | Location | Description |
|---|---|---|
| `RiskLimits` | `risk/limits.py` | Dataclass with all threshold parameters |
| `KillSwitch` | `risk/kill_switch.py` | Emergency stop; blocks all orders when active |
| `RiskEngine` | `risk/engine.py` | Evaluates `OrderIntent` against `RiskLimits` |

**BacktestEngine integration:**

- `BacktestEngine` now accepts optional `risk_engine: RiskEngine | None = None`
- When provided, every `OrderIntent` passes through `risk_engine.check_order_intent()` before reaching `SimulatedBroker`
- Rejected orders are collected and included in `BacktestReport.rejected_risk_decisions`
- When `risk_engine=None`, all intents are approved (backward-compatible)

```bash
# Run all tests (540 total, all pass)
python3 -m pytest -v

# Style checks
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts
```

---

**Milestone 6 — Opening Range Breakout Strategy** (complete)

Added the first production strategy: Opening Range Breakout (ORB) in backtest-only mode.
No live trading, no Zerodha API calls, no real order placement.

**What ORB does:**

The strategy records the high and low of the first N minutes of the NSE session
(the "opening range"). Once the range closes, it enters LONG if price breaks above
the opening range high. Exits are triggered by stop-loss, profit target, or
square-off time.

**Current assumptions (v1):**
- Long-only — downside breakdowns are ignored.
- MARKET order entry (fills at bar close in the backtester).
- Entry price assumed = bar.close (optimistic fill assumption; consistent with `SimulatedBroker`).
- Stop price = opening range low − optional stop buffer.
- Target = entry + `target_r_multiple` × risk-per-share.
- If stop and target are both touched in the same bar, stop-loss is assumed (conservative).
- One entry per symbol per day by default (`allow_reentry=False`).
- State resets automatically at the start of each new trading day.
- Multiple symbols maintain fully independent state.

**Key configuration (`ORBConfig`):**

| Parameter | Default | Description |
|---|---|---|
| `opening_range_minutes` | 15 | Minutes after 09:15 that define the OR |
| `quantity` | 1 | Shares per signal |
| `target_r_multiple` | 2.0 | Target as multiple of initial risk |
| `stop_buffer_bps` | 0 | Extra bps below OR low for stop |
| `entry_buffer_bps` | 0 | Extra bps above OR high for trigger |
| `square_off_time` | 15:15 | Time-based exit |
| `allow_reentry` | False | Re-enter after exit on same day |

**Intentionally not supported yet:**
- Short-side breakdowns (`long_only=True` is enforced; raises `NotImplementedError` if set to False)
- SL/SL-M order types (engine raises `UnsupportedOrderTypeError`)
- Fill confirmation callbacks (engine does not yet call `strategy.on_order_update`)
- Risk engine limits (placeholder `_risk_check()` always approves)

No Zerodha SDK is imported anywhere in the strategies package.

```bash
# Run all tests (482 total, all pass)
python3 -m pytest -v

# Style checks
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts

# Optional: run ORB backtest on local Parquet candle data
# (download data first with HistoricalDataDownloader)
python3 scripts/run_orb_backtest.py
```

---

**Milestone 5 — Event-Driven Backtesting Engine** (complete)

Added a complete offline backtesting framework. No live broker, no Zerodha calls,
no real order placement at any point in this milestone.

**How the backtester works:**

1. `HistoricalDataFeed` accepts one or more symbol DataFrames, merges them, and
   yields `(timestamp, symbol, Bar)` tuples in chronological order.
2. `BacktestEngine` iterates the feed, calls `strategy.on_bar()` per bar, and
   routes `OrderIntent` objects to `SimulatedBroker`.
3. `SimulatedBroker` applies slippage via `SlippageModel`, calculates fees via
   `CostModel`, and creates `TradeFill` objects, then updates `BacktestPortfolio`.
4. After each bar the portfolio is marked to market and equity is recorded.
5. At the end, `calculate_backtest_metrics()` computes summary statistics and
   `BacktestEngine.run()` returns a `BacktestReport` (JSON-serialisable).

**Supported in v1:**
- MARKET orders (fill at bar close ± slippage)
- LIMIT orders (BUY fills if bar.low ≤ limit; SELL fills if bar.high ≥ limit)
- Long-only positions
- Indian equity intraday fee model (brokerage, STT, exchange charge, SEBI, stamp, GST)
- Configurable slippage in basis points
- Per-run `BacktestReport` with equity curve, fills, and metrics (total return,
  max drawdown, win rate, profit factor, expectancy)
- JSON report serialisation via `report.save_json(path)`

**Intentionally not supported yet:**
- SL / SL-M orders (raise `UnsupportedOrderTypeError`)
- Short selling
- Risk engine limits (placeholder `_risk_check()` always approves)
- Multiple partial fills per bar
- Tick-level simulation

No Zerodha SDK is imported anywhere in the backtest package.

```bash
python3 -m pytest -v          # 417 tests, all pass
python3 -m ruff check src tests   # clean
python3 -m ruff format --check src tests  # clean
```

---

**Milestone 4 — Historical Data Pipeline** (complete)

Added a complete historical data acquisition pipeline:

- **`data/universe.py`** — `UniverseConfig` (Pydantic v2): validates symbol list
  (non-empty, no duplicates, no blank strings), defaults exchange to NSE, carries
  optional `filters` dict. `load_universe_config(path)` reads any YAML file that
  contains a `universe:` section. Default config has 10 liquid NSE large-caps.
- **`data/validation.py`** — `validate_ohlcv_dataframe(df, symbol, exchange, interval)`:
  returns a `DataValidationReport` with typed `DataValidationIssue` entries (severity
  `"error"` or `"warning"`). Checks: required columns, empty df, duplicate timestamps,
  positive OHLC prices, non-negative volume, correct high ≥ open/close/low, low ≤
  open/close/high, sorted timestamps, and intraday gap detection (warning, not error).
- **`data/historical.py`** — `HistoricalDataDownloader(broker, data_dir)`: downloads
  Zerodha candle dicts via injected broker, normalises `"date"` → `"timestamp"`,
  coerces numeric types, validates, and optionally saves as Parquet.
  Storage layout: `DATA_DIR/candles/{exchange}/{symbol}/{interval}.parquet`.
  `download_universe(instruments, universe, ...)` iterates the full symbol list.
- **`storage/models.py`** — `HistoricalCandlesMetadata` ORM model: tracks per-symbol
  download runs, file path, candle count, validation status.
- **`configs/default.yaml`** — expanded universe to 10 symbols with filters section.

```bash
python3 -m pytest -v   # 297 tests, all pass
```

---

**Milestone 3 — Zerodha Read-Only Broker Adapter** (complete)

Added Zerodha broker integration and a safe paper broker:

- **`broker/zerodha/auth.py`** — `KiteAuthManager`: handles the Zerodha login URL
  → request_token → access_token flow via dependency-injected Kite client.
  Credentials held as `SecretStr`; raw values extracted only at SDK call boundaries.
  Never logs or returns secrets in repr.
- **`broker/zerodha/client.py`** — `ZerodhaBroker`: implements the abstract `Broker`
  interface. Wraps `kite_client.positions()`, `orders()`, `trades()`, `margins()`,
  `instruments()`, `historical_data()`. Requires `connect()` before data-fetching calls.
  `stream_ticks` raises `NotImplementedError` (Milestone 8). All order methods raise
  `LiveTradingDisabledError` (inherited from `Broker` base).
- **`broker/zerodha/mappers.py`** — placeholder for future Zerodha dict → internal model
  conversion.
- **`broker/paper.py`** — `PaperBroker`: safe simulated broker for paper trading.
  All read methods return empty/default values. Order methods raise `LiveTradingDisabledError`.

Why tests use fake clients: injecting a `FakeKiteClient` (defined in test files)
avoids any real Zerodha network calls. Tests run fully offline without credentials.

```bash
python3 -m pytest -v   # 212 tests, all pass
```

---

**Milestone 2 — Domain Models** (complete)

Added a broker-independent `trading_engine.domain` package containing:

- **`enums.py`** — `TradingMode`, `Exchange`, `Side`, `OrderType`, `ProductType`,
  `TimeInForce`, `OrderStatus`, `SignalType`, `RiskReasonCode` as `StrEnum`
  (members compare equal to their string values with no extra conversion needed)
- **`identifiers.py`** — `generate_internal_order_id()`, `generate_signal_id()`,
  `generate_risk_decision_id()`, `generate_fill_id()` — UUID4-based, prefixed
- **`models.py`** — Pydantic v2 models: `Money`, `Instrument`, `RiskDecision`,
  `InternalOrder`, `TradeFill`, `Position`, `PortfolioSnapshot`

Why broker-independent? The Zerodha SDK will be integrated in Milestone 3.
Domain models must be defined separately so the risk engine, backtester, and
paper engine can all use the same types without importing broker-specific code.

Live order placement is **still not implemented**. `Broker.place_order()` still
raises `LiveTradingDisabledError`. This will remain so until Milestone 9.

```bash
python3 -m pytest -v   # 151 tests, all pass
```

---

**Milestone 1 — Foundation** (complete)

- Pydantic Settings-based configuration with safe defaults
- Structured JSON logging with automatic secret redaction
- Core domain models: `Bar`, `Tick`, `OrderIntent`, `Signal`, `StrategyContext`
- Abstract `Strategy` base class
- Abstract `Broker` interface with live order placement blocked
- SQLAlchemy database scaffolding with health check
- Clock abstraction for backtest / live time control
- Custom exception hierarchy

**Milestone 3** — Zerodha read-only integration (next)

---

## Repository structure

```
src/trading_engine/
  common/         config, logging, exceptions, clock
  broker/         abstract Broker interface + zerodha/ stub
  strategy/       Strategy base class, Bar/Tick/OrderIntent models
  data/           (Milestone 4)
  backtest/       (Milestone 6)
  execution/      (Milestone 9)
  risk/           (Milestone 7)
  portfolio/      (Milestone 6+)
  storage/        SQLAlchemy base + health check

tests/
  unit/           test_config, test_logging, test_strategy_base
  integration/    (future)

configs/
  default.yaml        active config (gitignored if it has secrets)
  config.example.yaml reference config

docs/               full specification documents
```

---

## Local setup

### Prerequisites

- Python 3.11+
- Docker + Docker Compose (for PostgreSQL)
- Zerodha Kite Connect API credentials (not required for backtest mode)

### 1. Clone and install

```bash
git clone <repo-url>
cd <repo-dir>
make install
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values. At minimum for local development:

```env
APP_ENV=development
LOG_LEVEL=INFO
DATABASE_URL=postgresql+psycopg://trading:trading@localhost:5432/trading_engine

# Zerodha — leave blank until Milestone 3
ZERODHA_API_KEY=
ZERODHA_API_SECRET=
ZERODHA_ACCESS_TOKEN=

# Safety flags — do NOT change LIVE_TRADING_ENABLED until Milestone 9
LIVE_TRADING_ENABLED=false
PAPER_TRADING_ENABLED=true

# Risk limits (INR)
MAX_DAILY_LOSS=1000
MAX_ORDER_VALUE=10000
MAX_TRADES_PER_DAY=20
ORDER_RATE_LIMIT_PER_SECOND=1
```

### 3. Start PostgreSQL

```bash
make up       # starts postgres (and redis) via docker compose
```

### 4. Copy config

```bash
cp configs/config.example.yaml configs/default.yaml
```

---

## Running tests

```bash
make test           # run all tests
make test-cov       # run tests with coverage report
```

All tests pass without a live Zerodha connection or a running database.

---

## Development commands

```bash
make install        # install package and dev dependencies
make test           # pytest
make test-cov       # pytest with coverage
make lint           # ruff check
make format         # ruff format
make typecheck      # mypy
make up             # start docker services
make down           # stop docker services
make run-dashboard  # placeholder (Milestone 10)
```

---

## Adding a strategy

Subclass `Strategy` and implement `on_bar()`:

```python
from trading_engine.strategy.base import Strategy, StrategyContext
from trading_engine.strategy.signals import Bar, OrderIntent
from decimal import Decimal

class MyStrategy(Strategy):
    def on_bar(self, bar: Bar, context: StrategyContext) -> list[OrderIntent]:
        # Analyse bar, return zero or more OrderIntents.
        # Do NOT call the broker here.
        return []
```

Strategies must never import Zerodha SDK modules. They emit `OrderIntent`
objects; the risk engine and order manager handle the rest.

---

## Milestone 9: Zerodha Historical Data Download

### What was added

- **`scripts/download_zerodha_historical.py`** — CLI to download candle data from Zerodha into Parquet files.
- **`scripts/zerodha_login_helper.py`** — Interactive helper to generate a daily Zerodha access token.
- **`src/trading_engine/data/zerodha_downloader.py`** — Reusable download orchestration (`DownloadConfig`, `DownloadResult`, `run_download`).
- **`src/trading_engine/broker/zerodha/login.py`** — Login helpers (`get_login_url`, `exchange_request_token`, `update_env_file`, `validate_credentials`).
- All Zerodha APIs used are **read-only**. No orders are placed, modified, or cancelled.
- The download script **refuses to run** if `LIVE_TRADING_ENABLED=true`.

### Step 1: Create Kite Connect credentials

1. Go to [https://developers.kite.trade](https://developers.kite.trade) and create an app.
2. Copy your **API Key** and **API Secret**.
3. Set the redirect URL to `https://127.0.0.1` (or any URL you can view in the browser).

### Step 2: Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```
ZERODHA_API_KEY=your_api_key
ZERODHA_API_SECRET=your_api_secret
# Leave ZERODHA_ACCESS_TOKEN blank — the login helper sets it.
ZERODHA_ACCESS_TOKEN=
```

### Step 3: Generate a daily access token

Zerodha access tokens expire at the end of each trading day. Run this each morning:

```bash
python3 scripts/zerodha_login_helper.py
```

Follow the prompts: open the URL, log in, copy the `request_token` from the redirect URL, and paste it. The script prints your new access token.

To update `.env` automatically:

```bash
python3 scripts/zerodha_login_helper.py --write-env
```

### Step 4: Download historical data

**Dry run (no API calls):**

```bash
python3 scripts/download_zerodha_historical.py \
  --config configs/default.yaml \
  --interval 5minute \
  --from-date 2026-01-01 \
  --to-date 2026-01-31 \
  --dry-run
```

**Download specific symbols:**

```bash
python3 scripts/download_zerodha_historical.py \
  --config configs/default.yaml \
  --interval 5minute \
  --from-date 2026-01-01 \
  --to-date 2026-01-31 \
  --symbols RELIANCE INFY TCS
```

**Download full universe:**

```bash
python3 scripts/download_zerodha_historical.py \
  --config configs/default.yaml \
  --interval 5minute \
  --from-date 2026-01-01 \
  --to-date 2026-01-31
```

### Where Parquet files are saved

```
data/
  candles/
    NSE/
      RELIANCE_5minute.parquet
      INFY_5minute.parquet
      TCS_5minute.parquet
```

The root directory is controlled by `DATA_DIR` in `.env` (default: `./data`).

---

## Milestone 10: Broker Mapping and OMS Skeleton

### What was added

- **`src/trading_engine/broker/zerodha/mappers.py`** — Pure mapping functions that convert raw Zerodha API dicts into internal domain models: `map_zerodha_order`, `map_zerodha_trade`, `map_zerodha_position`, `map_zerodha_instrument`, and scalar mappers for status, side, order type, product, and exchange.
- **`src/trading_engine/execution/state_machine.py`** — `OrderStateMachine`: validates every order status transition against a defined table. Invalid transitions raise `OrderStateTransitionError`.
- **`src/trading_engine/execution/ledger.py`** — `OrderLedger`: in-memory store for `InternalOrder`, `TradeFill`, and `RiskDecision` objects. All status updates go through the state machine.
- **`src/trading_engine/execution/order_manager.py`** — `OrderManager`: converts `OrderIntent` objects into `InternalOrder` objects, runs the risk engine (if configured), and stores everything in the ledger. Never calls a broker API.
- **`src/trading_engine/common/exceptions.py`** — Added `OrderStateTransitionError`, `OrderNotFoundError`, `BrokerMappingError`.

### Why broker response normalisation matters

Zerodha returns raw dicts with string status values like `"COMPLETE"`, `"TRIGGER PENDING"`, and product codes like `"MIS"`. These must be mapped to the engine's `OrderStatus`, `ProductType`, etc. before any internal logic can use them. The mapper layer makes this conversion explicit, testable without credentials, and isolated from the rest of the codebase.

### What the order state machine does

`OrderStateMachine.transition(current, next)` either returns the target status (valid transition) or raises `OrderStateTransitionError` (invalid). This prevents order state from being corrupted by out-of-sequence broker callbacks or logic bugs. Terminal states (`FILLED`, `CANCELLED`, `FAILED`, `REJECTED`) have no allowed outgoing transitions.

### What the in-memory ledger does

`OrderLedger` is the single source of truth for order state during a session. It stores orders, fills, and risk decisions. `update_order_status` enforces the state machine on every write. Lookups by `internal_order_id` raise `OrderNotFoundError` for unknown IDs. No database or file persistence is used in this milestone.

### Confirmation

- Real order placement is **not implemented**. `place_order()`, `modify_order()`, and `cancel_order()` remain blocked on `ZerodhaBroker`.
- `LIVE_TRADING_ENABLED` remains `false`.
- `mark_submitted()` and `mark_broker_update()` on `OrderManager` update **internal state only** — they do not call Zerodha APIs.

### Commands to verify

```bash
python3 -m pytest -v                                     # 784 tests, all pass
python3 -m ruff check src tests scripts                  # no errors
python3 -m ruff format --check src tests scripts         # no reformats needed
```

---

## Milestone 11: Read-Only Broker Reconciliation

### What was added

- **`src/trading_engine/reconciliation/models.py`** — `ReconciliationDiscrepancy` and `ReconciliationReport` dataclasses with safe `to_dict()` serialisation.
- **`src/trading_engine/reconciliation/service.py`** — `ReconciliationService`: compares broker-reported order state against the in-memory `OrderLedger` and updates ledger statuses where valid transitions exist.
- **`src/trading_engine/reconciliation/status.py`** — `StatusPage`: lightweight JSON-serialisable dict aggregating session metrics (orders by status, fills, positions, last reconciliation result).

### What reconciliation checks

| Check | Severity | Action |
|-------|----------|--------|
| Ledger order (with broker ID) not returned by broker | HIGH | Record `MISSING_IN_BROKER` discrepancy |
| Broker order not found in ledger | MEDIUM | Record `UNKNOWN_BROKER_ORDER` discrepancy |
| Status mismatch — valid state machine transition | LOW | Update ledger, record `STATUS_MISMATCH_UPDATED` |
| Status mismatch — invalid state machine transition | HIGH | Record `INVALID_TRANSITION`, ledger unchanged |
| Broker response cannot be parsed | MEDIUM | Record `MAPPING_ERROR` discrepancy |

`ReconciliationReport.success` is `True` when no HIGH-severity discrepancies exist.

### What the status page reports

```python
from trading_engine.reconciliation.status import StatusPage

page = StatusPage(ledger=ledger, last_reconciliation_report=report)
print(page.to_dict())
# {
#   "orders_by_status": {"RISK_APPROVED": 2, "FILLED": 1},
#   "total_orders": 3,
#   "total_fills": 1,
#   "total_risk_decisions": 3,
#   "open_positions_count": 2,
#   "last_reconciliation_timestamp": "2024-01-15T09:30:00",
#   "last_reconciliation_success": True,
#   "discrepancy_count": 0
# }
```

### Why this matters before live execution

Reconciliation ensures that what the engine believes about its orders matches what the broker actually holds. Without this, stale ledger state could lead to duplicate orders, missed fills, or incorrect risk calculations. The reconciliation service must be clean and reporting zero HIGH-severity discrepancies before live order placement is enabled.

### Confirmation

- Broker state is **never modified** by reconciliation. `place_order`, `modify_order`, and `cancel_order` are never called.
- `ZerodhaBroker` read methods (`get_orders`, `get_trades`, `get_positions`) are unchanged — backward-compatible.
- `LIVE_TRADING_ENABLED` remains `false`.
- Tests use `FakeBroker` with hardcoded raw dicts — no real Zerodha API calls.

### Commands to verify

```bash
python3 -m pytest -v                     # 834 tests, all pass
python3 -m ruff check src tests scripts  # no errors
python3 -m ruff format --check src tests scripts  # no reformats needed
```

---

## Milestone 12: Dashboard v1

### What was added

- **`src/trading_engine/dashboard/models.py`** — `DashboardSession`: JSON-serialisable envelope wrapping a `StatusPage` snapshot with metadata (`generated_at`, `source`, `version`).
- **`src/trading_engine/dashboard/session_writer.py`** — `DashboardSessionWriter`: atomic JSON writer/reader. Writes to a temp file then renames to prevent partial reads.
- **`scripts/dashboard.py`** — Streamlit dashboard that reads the session JSON and renders orders, fills, reconciliation status, and a raw JSON debug view.
- **`scripts/write_demo_dashboard_status.py`** — Generates demo session JSON without credentials or live data.
- **`Makefile`** — Updated `run-dashboard` and added `write-demo-dashboard` targets.

### How the dashboard reads session JSON

The engine (backtest, paper, or future live engine) calls `DashboardSessionWriter.write_status(status_page.to_dict())` to update the session file. The Streamlit process reads that file on each render cycle. There is no shared memory or IPC — just a JSON file.

The default session file path is `data/dashboard/session_status.json`. Override with:

```bash
DASHBOARD_SESSION_PATH=data/my/path.json streamlit run scripts/dashboard.py
```

### How to write demo dashboard status

```bash
python3 scripts/write_demo_dashboard_status.py
# or:
make write-demo-dashboard
```

This generates a realistic fake session at `data/dashboard/session_status.json`.

### How to run the dashboard

```bash
streamlit run scripts/dashboard.py
# or:
make run-dashboard
```

Then open the URL shown in the terminal (typically `http://localhost:8501`).

Enable **Auto-refresh** in the sidebar to poll for updates every 30 seconds.

### Confirmation

- The dashboard is **read-only**. It reads a JSON file and renders it. No broker calls, no orders, no mutations.
- `LIVE_TRADING_ENABLED` remains `false`.
- No credentials are required to run the dashboard.
- The generated `data/dashboard/session_status.json` file is listed in `.gitignore` (add it if not present) and must not be committed.

### Commands to verify

```bash
python3 -m pytest -v                                    # 864 tests, all pass
python3 -m ruff check src tests scripts                 # no errors
python3 -m ruff format --check src tests scripts        # no reformats needed
python3 scripts/write_demo_dashboard_status.py          # writes demo JSON
streamlit run scripts/dashboard.py                      # opens dashboard
```

---

## Milestone 13: Strategy Analytics and Validation Gates

Extended the backtest engine with richer performance analytics and a configurable validation gate system.

### Analytics additions (`trading_engine/backtest/metrics.py`)

`BacktestMetrics` now includes:

| Field | Description |
|---|---|
| `average_trade_pnl` | Mean P&L per completed trade |
| `best_trade_pnl` | Single best trade P&L |
| `worst_trade_pnl` | Single worst trade P&L |
| `sharpe_ratio` | Annualised Sharpe ratio (bar-to-bar returns × √252); `None` if flat equity or < 2 bars |
| `sortino_ratio` | Annualised Sortino ratio (downside deviation only); `None` if no losing periods |
| `cagr` | Compound Annual Growth Rate; `None` if period < 1 day or missing timestamps |

### Validation gates (`trading_engine/validation/`)

```
src/trading_engine/validation/
├── __init__.py
├── models.py       # ValidationGateConfig, ValidationGateFailure, ValidationResult
└── validator.py    # StrategyValidator
```

`ValidationGateConfig` accepts thresholds for 7 gates:

```python
ValidationGateConfig(
    min_trades=30,
    min_sharpe=0.5,
    max_drawdown_pct=0.20,
    min_win_rate=0.40,
    min_profit_factor=1.2,
    min_expectancy=None,
    max_total_fees_pct_of_pnl=0.30,
)
```

Gates with `None` values are skipped. A gate fails if the corresponding metric is also `None` (missing data counts as failure).

### BacktestEngine integration

Pass an optional `StrategyValidator` when constructing `BacktestEngine`:

```python
validator = StrategyValidator(ValidationGateConfig(min_trades=30, min_sharpe=0.5))
engine = BacktestEngine(..., strategy_validator=validator)
report = engine.run()

if report.validation_result.passed:
    print("Strategy passed all gates")
else:
    for failure in report.validation_result.failed_gates:
        print(f"  FAIL {failure.gate_name}: {failure.message}")
```

`BacktestReport.to_dict()` includes `"validation_result"` (or `null` when no validator was provided).

### Tests

| File | Coverage |
|---|---|
| `tests/unit/backtest/test_metrics.py` | Sharpe, Sortino, CAGR, per-trade stats |
| `tests/unit/validation/test_models.py` | Config validation, serialisation |
| `tests/unit/validation/test_validator.py` | Each gate: pass/fail/missing-metric |
| `tests/unit/backtest/test_engine_validation.py` | BacktestEngine + StrategyValidator integration |

---

## Milestone 14: Zerodha Live Market Data for Paper Trading

Adds real-time Zerodha WebSocket tick ingestion, candle building, and a live
paper trading runner. **No real orders are placed.** All fills are simulated.

### What was added

```
src/trading_engine/live_data/
├── __init__.py
├── models.py          # LiveTick dataclass with validation
├── candle_builder.py  # CandleBuilder: ticks → OHLCV Bar objects
└── zerodha_feed.py    # ZerodhaLiveMarketFeed: KiteTicker wrapper

src/trading_engine/paper/live_runner.py   # PaperLiveRunner + PaperLiveRunnerConfig

scripts/run_paper_live_zerodha.py         # CLI runner with safety checks
```

### How it works

1. `ZerodhaLiveMarketFeed` connects to the Zerodha KiteTicker WebSocket and
   converts raw tick dicts into `LiveTick` objects via `_raw_tick_to_live_tick`.
2. Each `LiveTick` is passed to `CandleBuilder`, which groups ticks by symbol
   and interval bucket (default 60 s).  When a new bucket starts, the previous
   candle is emitted as a `Bar`.
3. `PaperLiveRunner.on_tick()` receives each tick, forwards completed `Bar`
   objects to the strategy, runs risk checks, and simulates fills via
   `PaperExecutionBroker`.
4. Dashboard status is written after each bar via `DashboardSessionWriter`.

### How this differs from live trading

| Paper Live (this milestone) | Live Trading (not implemented) |
|---|---|
| Receives real market data | Receives real market data |
| Simulates fills in memory | Places real orders via Zerodha |
| No `place_order` calls | Calls `place_order` on broker |
| `LIVE_TRADING_ENABLED=false` | `LIVE_TRADING_ENABLED=true` |
| Safe to run at any time | Requires explicit safety approval |

### Required credentials

```bash
ZERODHA_API_KEY=your_api_key
ZERODHA_ACCESS_TOKEN=your_daily_token   # generate with zerodha_login_helper.py
LIVE_TRADING_ENABLED=false              # must be false
```

### Safety flag

The script will not start without:
```
--i-understand-this-uses-live-market-data
```

It also hard-refuses if `LIVE_TRADING_ENABLED=true`.

### Example command

```bash
python3 scripts/run_paper_live_zerodha.py \
  --i-understand-this-uses-live-market-data \
  --symbols RELIANCE INFY \
  --interval-seconds 60 \
  --strategy orb \
  --dashboard-path data/dashboard/session_status.json
```

Press Ctrl+C to stop. Open candles are flushed through the strategy on shutdown.

### Real order placement is still not implemented

`ZerodhaBroker.place_order()` still raises `LiveTradingDisabledError`.
`modify_order` and `cancel_order` remain unimplemented. This milestone adds
data ingestion only.

### Tests

| File | Coverage |
|---|---|
| `tests/unit/live_data/test_models.py` | LiveTick validation |
| `tests/unit/live_data/test_candle_builder.py` | OHLCV logic, bucket alignment, flush, reset |
| `tests/unit/live_data/test_zerodha_feed.py` | Fake ticker connect/disconnect, tick conversion, callback |
| `tests/unit/paper/test_live_runner.py` | Runner lifecycle, fills, stop-event, script safety checks |

---

## Milestone 15: Live Execution Safety Layer and Manual Approval Mode

Creates the safety and approval infrastructure that all future live order
execution must pass through. **Real order placement is still not implemented.**
`LIVE_TRADING_ENABLED` remains `false`.

### What was added

```
src/trading_engine/live_execution/
├── __init__.py
├── models.py      # ApprovalMode, ApprovalStatus, ApprovalRequest, ApprovalDecision
├── approvals.py   # LiveOrderApprovalGate
├── dry_run.py     # DryRunExecutor, DryRunOrderPreview
├── audit.py       # ApprovalAuditLogger (JSONL)
└── safety.py      # LiveExecutionSafetyGuard

src/trading_engine/common/exceptions.py  — added ManualApprovalRequired

scripts/live_order_dry_run.py  — CLI dry-run preview tool
```

### Approval modes

| Mode | Behaviour |
|---|---|
| `AUTO_PAPER` | Instantly approves; suitable for paper trading only. No real orders. |
| `MANUAL_APPROVE` | Creates a `PENDING` request; raises `ManualApprovalRequired`. Operator must call `approve()` or `reject()`. |
| `AUTO_LIVE` | **Raises `SafetyError`** — not implemented in this milestone. |

### Why AUTO_LIVE is intentionally blocked

`AUTO_LIVE` exists as an enum value to document the intended future design,
but any attempt to use it calls `LiveExecutionSafetyGuard.assert_order_placement_blocked_for_now()`
which always raises `SafetyError`.  Real order execution requires a dedicated
future milestone to wire up `ZerodhaBroker.place_order()`, which is still
unimplemented.

### Dry-run previews

`DryRunExecutor.preview()` shows what would happen to an `OrderIntent`:
- Runs the risk engine (if provided).
- Queries the approval gate.
- Returns a `DryRunOrderPreview` with `to_dict()` for JSON output.
- Never touches a broker. Never calls Zerodha.

### Audit logging

`ApprovalAuditLogger` writes JSON-lines records to a file:
- One line per `log_request()`, `log_decision()`, `log_dry_run()` call.
- Parent directories created automatically.
- No secrets logged. No database required.

### Safety guard

`LiveExecutionSafetyGuard` provides two methods:
- `assert_order_placement_blocked_for_now()` — **always raises `SafetyError`** in this milestone.
- `assert_live_execution_allowed()` — checks `LIVE_TRADING_ENABLED` and kill switch status.

### Example — CLI dry-run preview

```bash
python3 scripts/live_order_dry_run.py \
  --symbol RELIANCE \
  --side BUY \
  --quantity 1 \
  --order-type MARKET \
  --strategy-id manual_dry_run
```

Output (JSON, no Zerodha calls, no credentials required):
```json
{
  "symbol": "RELIANCE",
  "side": "BUY",
  "quantity": 1,
  "order_type": "MARKET",
  "approval_status": "approved",
  "message": "DRY RUN: BUY 1 RELIANCE @ MARKET — risk: PASS, approval gate: APPROVED. No order placed."
}
```

### Confirmation

- Real order placement is **not** implemented.
- `ZerodhaBroker.place_order()` still raises `LiveTradingDisabledError`.
- `modify_order` and `cancel_order` remain unimplemented.
- `LIVE_TRADING_ENABLED` defaults to `false`.

### Tests

| File | Coverage |
|---|---|
| `tests/unit/live_execution/test_models.py` | ApprovalRequest/Decision validation, serialisation |
| `tests/unit/live_execution/test_approvals.py` | All three modes, approve/reject/check_decision |
| `tests/unit/live_execution/test_dry_run.py` | Risk integration, approval gate, JSON output |
| `tests/unit/live_execution/test_audit.py` | JSONL writes, multi-record, nested dirs |
| `tests/unit/live_execution/test_safety.py` | Order placement always blocked, prerequisites check |
| `tests/unit/scripts/test_live_order_dry_run.py` | CLI parsing, validation, JSON output, no Zerodha |

---

## Milestone 16: Live Order Execution Pilot

### What was added

| Component | Location | Description |
|---|---|---|
| New `Settings` fields | `common/config.py` | `live_order_execution_enabled`, `live_order_pilot_enabled`, `live_max_order_quantity`, `live_allowed_symbols`, `live_allowed_exchange`, `live_allowed_product`, `live_allowed_order_types` |
| `LivePilotConfig` | `live_execution/pilot_config.py` | Dataclass wrapping all pilot constraints; `from_settings()` classmethod |
| `assert_pilot_order_allowed()` | `live_execution/safety.py` | Full 10-check gate: both flags, kill switch, risk, approval, symbol/exchange/product/type/qty |
| `ZerodhaBroker.place_order()` | `broker/zerodha/client.py` | Real Kite placement gated through `LiveExecutionSafetyGuard` |
| `LiveOrderPilotExecutor` | `live_execution/pilot_executor.py` | Orchestrates risk → approval → safety → broker → audit |
| `PilotOrderResult` | `live_execution/pilot_executor.py` | Result dataclass with `to_dict()` |
| `OrderVerificationService` | `live_execution/order_verification.py` | Polls broker to confirm placed order appears in order list |
| `scripts/live_order_pilot.py` | `scripts/` | CLI: requires `--i-understand-this-places-real-orders` + interactive phrase confirmation |

### Safety model

Three flags must ALL be true to place any real order:

```
LIVE_TRADING_ENABLED=true
LIVE_ORDER_EXECUTION_ENABLED=true
LIVE_ORDER_PILOT_ENABLED=true
```

Additionally, per-order checks (via `assert_pilot_order_allowed`):
- Kill switch not active
- Risk engine approved (if provided)
- Approval gate returned APPROVED
- Symbol in `LIVE_ALLOWED_SYMBOLS`
- Exchange matches `LIVE_ALLOWED_EXCHANGE`
- Product matches `LIVE_ALLOWED_PRODUCT`
- Order type in `LIVE_ALLOWED_ORDER_TYPES`
- Quantity ≤ `LIVE_MAX_ORDER_QUANTITY`

All defaults are safe (`false`, `1`, `[]`). Empty `LIVE_ALLOWED_SYMBOLS` blocks all orders.

### Tests

| File | Coverage |
|---|---|
| `tests/unit/live_execution/test_pilot_config.py` | LivePilotConfig defaults, from_settings(), partial settings |
| `tests/unit/live_execution/test_safety.py` | All 10 assert_pilot_order_allowed() checks, kill switch, case-insensitivity |
| `tests/unit/live_execution/test_live_order_gating.py` | ZerodhaBroker.place_order() end-to-end gating |
| `tests/unit/live_execution/test_pilot_executor.py` | Happy path, risk rejection, broker failure, manual approval |
| `tests/unit/live_execution/test_order_verification.py` | Found, not found, retry, broker error |
| `tests/unit/scripts/test_live_order_pilot.py` | CLI parsing, safety flag, pilot flag checks |

---

## Documentation

| File | Contents |
|------|----------|
| `docs/00_personal_trading_engine_spec.md` | Full product spec |
| `docs/01_technical_architecture.md` | Architecture overview |
| `docs/02_risk_management_spec.md` | Risk engine design |
| `docs/03_backtesting_methodology.md` | Backtesting approach |
| `docs/04_implementation_plan.md` | All 12 milestones |
| `docs/05_claude_prompt_pack.md` | Prompts for each milestone |
| `docs/07_acceptance_checklists.md` | Go-live criteria |

---

## Operating principle

```
Strategy idea
  -> backtest
  -> validation and out-of-sample review
  -> paper trading
  -> tiny-size live trading
  -> gradual scale-up only after review
```

Strategies emit `OrderIntent` objects. The risk engine and order manager
decide whether an order can be placed. Strategies never touch the broker.

---

*This is not financial advice. The purpose of this project is to build a
safer, testable, auditable trading software system.*
