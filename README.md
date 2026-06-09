# Quantitative Swing Trading Engine — NSE India

A fully automated, stateless daily paper trading engine for Indian equities (NSE). No database. No persistent state. Every morning it replays 1–2 years of market history from scratch to reconstruct the portfolio, then sends a single Telegram digest with new entries, exits, open positions, and P&L. GitHub Actions handles the rest.

---

## The Strategy Development Process

The deployed system is the end result of a methodical process that started with a much wider net.

### What was explored and discarded

The initial research phase tested strategies across multiple archetypes:

| Strategy type | Verdict |
|---|---|
| Intraday momentum (ORB) | Discarded — did not survive OOS |
| Gap continuation | Discarded — did not survive OOS |
| Gap fade | Discarded — did not survive OOS |
| VWAP mean reversion | Discarded — did not survive OOS |

These strategies showed promise in-sample but failed when tested on unseen 2025 data. The rule was simple: if it doesn't hold up out-of-sample, it doesn't get deployed.

### The OOS validation methodology

Each strategy candidate was backtested on historical data up to end-2024 (train set), then evaluated on 2025 data it had never seen (test set). Parameters were optimised on train data only. Strategies that produced negative P&L or clearly degraded behaviour on the test set were eliminated before any combination work began.

This matters. It is easy to build a strategy that looks good on historical data — the harder discipline is respecting the train/test boundary and not cherry-picking results.

### Strategies that made the shortlist

Four strategies passed the OOS filter and moved forward to combination testing:

| Strategy | Mechanism | Universe | OOS pass rate |
|---|---|---|---|
| **MA Pullback** | Trend-following with RSI mean reversion. Buys when price is above the 100-day MA but has pulled back below the 20/50-day MA with RSI oversold. | 10 NSE large-caps | 5/10 symbols |
| **Supertrend** | ATR-based trend following. Enters on bullish band flip, exits on bearish reversal or time stop. | 10 NSE large-caps | 7/10 symbols |
| **BB Squeeze** | Volatility breakout. Waits for Bollinger Bands to compress below a tight threshold, then enters on the upside breakout. | 40 Nifty 50 symbols | 12/40 symbols |
| **Black Swan (Long-Only Pairs)** | Statistical arbitrage on correlated pairs. When the price ratio diverges beyond 2 standard deviations, buys the underperformer expecting mean reversion. Long-only to avoid margin. | BAJAJFINSV/BAJFINANCE, ICICIBANK/AXISBANK, AXISBANK/KOTAKBANK | 3/3 pairs |

Only the symbols that passed OOS validation are included in the deployed portfolios. The rest were dropped.

### What was tested but excluded from the final combination

**RSI(2) Mean Reversion** — a short-term mean reversion strategy using a 2-period RSI on stocks above their 200-day MA. It passed OOS on a handful of symbols (HDFCBANK, LT, BHARTIARTL, SBIN) but the signal frequency on the OOS window was too thin to draw statistically meaningful conclusions about combination behaviour. It was excluded from the final deployed combination.

---

## The Master Risk Engine

A collection of profitable strategies is not the same as a robust system. The Master Risk Engine is what ties everything together and prevents individual strategy signals from destroying each other's capital.

### Capital structure

- **Total account:** ₹2,00,000
- **Minimum position size:** ₹50,000 (hard floor — below this, no trade is taken)
- **Leverage:** None. Cash only.

### Priority-based allocation

When multiple strategies fire signals on the same day, capital is rationed in priority order:

```
MA Pullback  (priority 3 — highest)
BB Squeeze   (priority 2)
Supertrend   (priority 2)
Black Swan   (priority 1 — lowest)
```

The number of slots available is determined by `floor(free_cash / 50,000)`. The top-ranked signals fill those slots first. Lower-ranked signals are rejected with an explanation in the Telegram digest.

### Dynamic position sizing

Capital is not statically allocated per strategy. When signals are approved, the available free cash is divided equally across all concurrent approved signals. This means position sizes compound naturally as profits accumulate — a ₹2L account that has grown to ₹2.4L through realized gains will deploy larger chunks on the next signal, without any manual intervention.

### Capital safety gate

If any strategy fails to fetch market data for its symbols, the engine cannot accurately compute locked capital. In this scenario, **all new entries are blocked** for the day. The Telegram digest flags the error prominently. This prevents the engine from accidentally over-allocating due to a stale or incomplete picture of the portfolio.

### Stateless daily replay

There is no database storing trade state. Every time the engine runs, it re-downloads 1–2 years of daily OHLCV data and replays the full trading logic from scratch to reconstruct the current portfolio state. This makes the system trivially auditable — you can run it locally at any time and get the same answer. It also eliminates an entire class of bugs around state corruption or migration.

---

## Combination Testing and Final Selection

With four strategy candidates and their OOS-validated symbol sets, the next question was: which combination of strategies should actually be deployed together?

### What was tested

Four combinations were simulated over 10 years on a ₹2L account using the Master Risk Engine's priority and capital allocation logic:

| Combination | CAGR | Sharpe | Max Drawdown | Negative years (of 10) |
|---|---|---|---|---|
| BB + MA + Swan | 16.1% | 1.25 | 23.2% | 2/10 |
| ST + MA + Swan | 19.3% | 1.10 | 35.4% | 1/10 |
| BB + ST + Swan | 5.7% | 0.49 | 60.5% | 5/10 |
| **All 4 (deployed)** | **20.1%** | **1.10** | **35.6%** | **2/10** |

BB + ST + Swan was quickly ruled out — the absence of MA Pullback created long periods of capital starvation where high-priority signals were rarely present, leaving capital idle and dragging returns. Its 60.5% max drawdown also disqualified it on risk grounds.

### Walk-forward validation

Picking the best-looking combination from a 10-year backtest introduces its own look-ahead bias. To test whether any selection criterion was genuinely predictive, a walk-forward analysis was run:

- **Method:** Expanding window. Train on 2016–(Y−1), select the best combination by Sharpe, Calmar, or total return. Then measure that combination's actual return in year Y.
- **OOS windows:** 2021, 2022, 2023, 2024, 2025 (5 out-of-sample years)

The result was that no single selection criterion — Sharpe, Calmar, or total return — consistently identified the combination that would perform best in the following year. Selection performance was close to random across the 5 OOS years.

This is an important finding. It means the apparently "smart" approach of periodically reselecting your strategy combination based on past performance does not add value over the test horizon. The practical implication: just run all four strategies consistently. The walk-forward results showed that running All 4 produced comparable or better risk-adjusted returns than any adaptive selection scheme.

**Deployed configuration: All 4 strategies — CAGR 20.1%, Sharpe 1.10, max drawdown 35.6%, 2 negative years in 10.**

---

## How It Runs

The engine is fully hands-free after initial setup.

**GitHub Actions** triggers the workflow Monday–Friday at 3:15 PM IST (after NSE market close):

```
.github/workflows/daily_trader.yml
  → python scripts/run_master_trader.py
```

The script:
1. Fetches fresh OHLCV data for all symbols across all four strategies
2. Replays each strategy's logic to reconstruct current positions
3. Checks for new exits triggered today (stop loss, target, time stop, or signal reversal)
4. Evaluates new entry signals
5. Applies the Master Risk Engine's capital allocation logic
6. Sends one Telegram message containing: fetch errors (if any), approved/rejected new entries, exits with P&L, all open positions, and a since-inception P&L summary

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
TELEGRAM_BOT_TOKEN   — from BotFather
TELEGRAM_CHAT_ID     — your chat or channel ID
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

# Rerun combination analysis
python archive/simulate_combinations.py

# Rerun walk-forward + Sharpe/Calmar analysis
python archive/walk_forward_analysis.py
```

---

## Repository Structure

```
scripts/
  run_master_trader.py          — deployed daily engine
  run_ma_pullback_trader.py     — MA Pullback standalone
  run_bb_squeeze_trader.py      — BB Squeeze standalone
  run_paper_trader.py           — Black Swan standalone

reports/
  optimal_ma_pullback_portfolio.json   — 5 OOS-validated symbols
  optimal_supertrend_portfolio.json    — 7 OOS-validated symbols
  bb_squeeze_results.json              — 12/40 symbols passed OOS
  optimal_long_only_portfolio.json     — 3 OOS-validated pairs

archive/
  simulate_combinations.py     — 10-year combination backtest
  walk_forward_analysis.py     — walk-forward + risk metrics

.github/workflows/
  daily_trader.yml              — cron trigger at 3:15 PM IST, Mon–Fri
```

---

## What This Is Not

This is a paper trading engine — it tracks signals and sends digests, but does not place live orders. There is no broker integration in the deployed path. The system is designed for signal generation, portfolio tracking, and strategy research on Indian equities.
