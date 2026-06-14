# Quantitative Swing Trading Engine: NSE India

A fully automated, stateless daily paper trading engine for Indian equities (NSE). No database. No persistent state. Every morning it replays 1-2 years of market history from scratch to reconstruct the portfolio, then sends a single Telegram digest with new entries, exits, open positions, and a watchlist of near-signals. GitHub Actions handles the rest.

---

## The Strategy Development Process

The deployed system is the end result of a methodical process that started with a much wider net.

### What was explored and discarded

The initial research phase tested strategies across multiple archetypes:

| Strategy type | Why it was discarded |
|---|---|
| Opening Range Breakout (ORB) | Strong in-sample, collapsed OOS on most symbols |
| First Hour Momentum | High win rate in training, no edge on unseen data |
| 52-Week High Breakout | Worked in bull runs, failed badly in sideways markets OOS |
| Gap Continuation | Too dependent on specific volatility regimes |
| Gap Fade | Negative expected value once realistic slippage was applied |
| VWAP Mean Reversion | Required intraday data; signal quality degraded on daily bars |
| Pairs Cointegration (short-side) | Cointegration broke down OOS on most tested pairs |
| RSI(2) Mean Reversion | Passed OOS on a handful of symbols but signal frequency too thin to draw statistically meaningful conclusions |

These strategies showed promise in-sample but failed when tested on unseen 2025 data. The rule was simple: if it does not hold up out-of-sample, it does not get deployed.

### The OOS validation methodology

Each strategy candidate was backtested on historical data up to end-2024 (train set), then evaluated on 2025 data it had never seen (test set). Parameters were optimised on train data only. Strategies that produced negative P&L or clearly degraded behaviour on the test set were eliminated before any combination work began.

This matters. It is easy to build a strategy that looks good on historical data; the harder discipline is respecting the train/test boundary and not cherry-picking results.

### Strategies that made the shortlist

Four strategies passed the OOS filter and moved forward to combination testing:

| Strategy | Mechanism | Universe | OOS pass rate |
|---|---|---|---|
| **MA Pullback** | Trend-following with RSI mean reversion. Buys when price is above the long-term MA but has pulled back below the short-term MA with RSI oversold. Exits on target, stop, or time. | 10 NSE large-caps | 5/10 symbols |
| **Supertrend** | ATR-based trend following. Enters on bullish band flip, exits on bearish reversal or time stop. | 10 NSE large-caps | 7/10 symbols |
| **BB Squeeze** | Volatility breakout. Waits for Bollinger Bands to compress below a tight threshold, then enters on the upside breakout. | 40 Nifty 50 symbols | 12/40 symbols |
| **Black Swan (Long-Only Pairs)** | Statistical arbitrage on correlated pairs. When the price ratio diverges beyond 2 standard deviations, buys the underperformer expecting mean reversion. Long-only to avoid margin. | BAJAJFINSV/BAJFINANCE, ICICIBANK/AXISBANK, AXISBANK/KOTAKBANK | 3/3 pairs |

Only the symbols that passed OOS validation are included in the deployed portfolios. The rest were dropped.

---

## Combination Testing

With four strategy candidates and their OOS-validated symbol sets, the next question was: which combination should be deployed together?

### Simulating combinations

Four combinations were simulated over 10 years on a ₹2L account using the Master Risk Engine's priority and capital allocation logic:

| Combination | CAGR | Sharpe | Max Drawdown | Negative years |
|---|---|---|---|---|
| BB + MA + Swan | 15.3% | 1.25 | 23.2% | 2/10 |
| ST + MA + Swan | 17.6% | 1.10 | 35.4% | 1/10 |
| BB + ST + Swan | 5.1% | 0.49 | 60.5% | 5/10 |
| **All 4 (deployed)** | **18.3%** | **1.07** | **35.0%** | **2/10** |

BB + ST + Swan was quickly ruled out: without MA Pullback the engine had long periods of capital starvation, and its 60.5% max drawdown disqualified it on risk grounds alone.

### Walk-forward validation

Picking the best-looking combination from a 10-year backtest introduces its own look-ahead bias. To test whether any selection criterion was genuinely predictive, a walk-forward analysis was run:

- **Method:** Expanding window. Train on 2016-(Y-1), select the best combination by Sharpe, Calmar, or total return. Measure actual return in year Y.
- **OOS windows:** 2021, 2022, 2023, 2024, 2025

No single criterion consistently identified which combination would outperform the following year. Selection performance was close to random across 5 OOS windows. Practical implication: run all four strategies consistently rather than adaptively reselecting.

---

## Parameter Optimisation

After fixing the strategy set, three engine parameters required calibration: the minimum position size floor, the capital allocation rule, and the priority ordering when multiple signals compete for capital.

### Minimum chunk size

The `MIN_CHUNK` parameter sets the smallest acceptable position size. Below this threshold, no new entry is taken. Three alternatives were evaluated:

| MIN_CHUNK | CAGR | Sharpe | Max DD |
|---|---|---|---|
| ₹30,000 | 18.3% | 1.066 | 35.0% |
| ₹40,000 | 17.3% | 1.007 | 39.2% |
| ₹50,000 | 17.3% | 1.017 | 39.2% |
| ₹60,000 | 17.5% | 1.008 | 39.2% |

**₹30k wins on every metric.** The intuition: a lower floor means the engine wastes less free cash. With ₹97k available and a ₹50k floor, only one slot is taken; with a ₹30k floor, two ₹48.5k positions can be opened. Capital that would otherwise sit idle gets deployed.

### Dynamic vs fixed position sizing

Two allocation approaches were backtested:

- **Dynamic (current):** `chunk = free_cash / n_signals`. Free cash is split equally across all approved signals. If two signals fire with ₹200k free, each gets ₹100k. Position sizes grow naturally as the account compounds.
- **Fixed ₹50k:** Each approved signal always gets exactly ₹50k regardless of available capital.

| Approach | CAGR | Trades taken |
|---|---|---|
| Dynamic | 18.3% | 336 |
| Fixed ₹50k | 9.5% | 840 |

Dynamic sizing nearly doubles returns. Fixed sizing takes 2.5x more trades but earns half as much because each position is too small to compound meaningfully. Capital that sits idle above the fixed chunk earns nothing.

### Rebalancing (tested and rejected)

A rebalancing rule was also backtested: when a new signal fires with no free cash, sell half of eligible open positions (those with less than 10% unrealised gain) and equalise capital across all positions including the new entry.

| Approach | CAGR | Worse years |
|---|---|---|
| No rebalancing | 18.3% | 4/11 |
| With rebalancing | 10.5% | 7/11 |

Rebalancing nearly halves returns. Trimming a position mid-trade to fund a new entry means the sold portion misses the rest of the original trade's move. The natural exit (stop, target, or time) is the right exit. Capital recycles fast enough organically with max hold periods of 10-60 days.

### Priority ordering

When multiple strategies signal on the same day and capital is limited, a priority order determines which signals get funded. All 24 permutations of the four strategies were backtested across a train (2016-2020) and test (2021-2026) split.

The key finding: **priority ordering is largely noise.** Every top-10 in-sample configuration degraded when tested OOS (the in-sample rank-1 config dropped to rank-51 OOS). There is no permutation that robustly dominates across both periods.

Given this, the ordering was set on first principles rather than backtest optimisation:

```
MA Pullback  (priority 4, highest)  cleanest signal: requires 3 conditions simultaneously
Supertrend   (priority 3)           highest solo CAGR (26%) among the four strategies
BB Squeeze   (priority 2)           benefits from capital recycled by faster-exiting strategies
Black Swan   (priority 1, lowest)   diversifier; pairs trade, not a primary directional bet
```

### Deduplication rule

Two rules were added to prevent concentration in a single stock:

1. **Same-day dedup:** if two strategies signal the same symbol on the same day, only the higher-priority strategy's signal is taken. The other is rejected.
2. **Held-symbol filter:** if a symbol is already held by any strategy, new entries in that symbol are blocked regardless of which strategy generated the signal.

---

## The Master Risk Engine

### Capital structure

- **Total account:** ₹2,00,000
- **Minimum position size:** ₹30,000 (hard floor)
- **Leverage:** None. Cash only.
- **Priority:** MA Pullback > Supertrend > BB Squeeze > Black Swan

### Dynamic position sizing

When signals are approved, available free cash is divided equally across all concurrent approved signals. This means position sizes compound naturally as profits accumulate without any manual intervention.

### Position ledger

A persistent JSON ledger (`reports/master_position_ledger.json`) records the master-allocated quantity and entry price for every open position. This solves a subtle but important bug: each strategy internally computes its own quantity based on a fixed per-strategy capital assumption, which diverges from the master-allocated quantity when multiple signals share free cash. The ledger ensures exit P&L is always computed against what was actually deployed, not what the strategy assumed.

### Capital safety gate

If any strategy fails to fetch market data, **all new entries are blocked** for the day. The Telegram digest flags the error. This prevents accidental over-allocation from an incomplete view of locked capital.

### Stateless daily replay

There is no database storing trade state. Every run re-downloads 1-2 years of daily OHLCV data and replays the full trading logic from scratch to reconstruct the portfolio. The ledger is the only persistent state, and it is only used to correct quantities on exit. This makes the system trivially auditable: run it locally at any time and get the same answer.

---

## System Performance (canonical backtest, 10 years)

Benchmark: Nifty 50 buy-and-hold CAGR ~12.5% over the same period.

| Metric | Value |
|---|---|
| CAGR | 18.3% |
| Sharpe ratio | 1.066 |
| Max drawdown | 35.0% |
| Calmar ratio | 0.523 |
| Negative years | 2 of 11 |
| Best year | +49.7% (2018) |
| Worst year | -9.9% (2026 YTD) |

The Sharpe of 1.07 compares favourably to published NSE momentum strategy backtests (0.48-1.01) and top systematic fund benchmarks (Man AHL: 0.86). The CAGR of 18.3% sits at the top of the published range for Indian multi-strategy systematic backtests (14-18%).

Note: first 5 years (2016-2020) CAGR was ~28% due to an unusually strong trending environment. Last 5 years (2021-2026) CAGR is ~8%, consistent with a more choppy post-rate-hike regime that is harder for trend and momentum strategies.

---

## How It Runs

**GitHub Actions** triggers the workflow Monday-Friday at 3:15 PM IST (after NSE market close):

```
.github/workflows/daily_trader.yml
  → python scripts/run_master_trader.py
```

The script:
1. Fetches fresh OHLCV data for all symbols across all four strategies
2. Replays each strategy's logic to reconstruct current positions
3. Checks for exits triggered today (stop loss, target, time stop, signal reversal)
4. Evaluates new entry signals, applies dedup and held-symbol filters
5. Applies the Master Risk Engine's capital allocation logic
6. Scans for near-signals (almost signals) across all strategies for a watchlist
7. Sends one Telegram message: fetch errors, approved/rejected entries, exits with P&L, open positions with stop/target, watchlist, and since-inception P&L summary

There are no emails, no dashboards, no other outputs. One message per trading day.

---

## Setup

### Prerequisites

```bash
pip install pandas yfinance python-dotenv pydantic numpy statsmodels
pip install -e .
```

### Environment variables

```
TELEGRAM_BOT_TOKEN   # from BotFather
TELEGRAM_CHAT_ID     # your chat or channel ID
```

For GitHub Actions, add these as repository secrets.

### Running manually

```bash
# Run the daily paper trader
python scripts/run_master_trader.py

# Regenerate strategy portfolios (runs OOS sweepers)
python archive/sweep_ma_pullback.py
python archive/sweep_supertrend.py
python archive/sweep_bb_squeeze.py
python archive/sweep_black_swan.py

# Backtest research scripts
python archive/simulate_combinations.py       # 10-year combination comparison
python archive/walk_forward_analysis.py       # walk-forward + Sharpe/Calmar
python archive/sensitivity_analysis.py        # MIN_CHUNK + priority sweep (96 combos)
python archive/sensitivity_train_test.py      # same sweep with train/test split
python archive/backtest_rebalancing.py        # rebalancing vs no rebalancing
python archive/backtest_fixed_vs_dynamic.py   # fixed vs dynamic sizing
python archive/backtest_gold_hedge.py         # GOLDBEES regime hedge test
```

---

## Repository Structure

```
scripts/
  run_master_trader.py          # deployed daily engine (all 4 strategies)
  run_ma_pullback_trader.py     # MA Pullback standalone
  run_bb_squeeze_trader.py      # BB Squeeze standalone
  run_supertrend_trader.py      # Supertrend standalone
  run_paper_trader.py           # Black Swan standalone

src/trading_engine/
  strategy_priority.py          # single source of truth for priority ordering
  notifications/telegram.py     # Telegram notifier with HTML parse_mode

reports/
  optimal_ma_pullback_portfolio.json   # 5 OOS-validated symbols
  optimal_supertrend_portfolio.json    # 7 OOS-validated symbols
  bb_squeeze_results.json              # 12/40 symbols passed OOS
  optimal_long_only_portfolio.json     # 3 OOS-validated pairs
  master_position_ledger.json          # runtime: master-allocated qty per position

archive/
  simulate_combinations.py          # 10-year combination backtest
  walk_forward_analysis.py          # walk-forward + risk metrics
  sensitivity_analysis.py           # 96-combo parameter sweep (full 10y)
  sensitivity_train_test.py         # 96-combo sweep with train/test split
  backtest_rebalancing.py           # rebalancing backtest (rejected)
  backtest_fixed_vs_dynamic.py      # fixed vs dynamic sizing (dynamic wins)
  backtest_gold_hedge.py            # GOLDBEES regime hedge (rejected)

.github/workflows/
  daily_trader.yml              # cron trigger at 3:15 PM IST, Mon-Fri
```

---

## What This Is Not

This is a paper trading engine. It tracks signals and sends digests, but does not place live orders. There is no broker integration in the deployed path. The system is designed for signal generation, portfolio tracking, and strategy research on Indian equities.
