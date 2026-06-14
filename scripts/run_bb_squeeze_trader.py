"""BB Squeeze Swing Trader — Daily Digest.

Replays all bars to reconstruct state for each symbol, then sends ONE
Telegram message with: open positions + unrealized P&L, any trades that
closed today, any new entry signals, and total P&L summary.

No startup pings. No "scanning..." noise. One message with everything.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.notifications.telegram import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

REPORTS_DIR = ROOT / "reports"
BB_WINDOW = 20
BB_STD_MULT = 2.0
CAPITAL_PER_TRADE = 100_000
PAPER_TRADING_START = "2026-06-01"  # Only count P&L from this date onwards


def load_portfolio() -> list[dict]:
    path = REPORTS_DIR / "bb_squeeze_results.json"
    if not path.exists():
        _log.error("bb_squeeze_results.json not found.")
        return []
    with open(path) as f:
        data = json.load(f)
    return [r for r in data["results"] if r["pass_oos"]]


def fetch_data(symbol: str) -> pd.DataFrame | None:
    try:
        df = yf.download(f"{symbol}.NS", period="1y", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index.name = "timestamp"
        df = df.reset_index()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.sort_values("timestamp").reset_index(drop=True).ffill().dropna(subset=["close"])
    except Exception as exc:
        _log.warning(f"Failed to fetch {symbol}: {exc}")
        return None


def replay_symbol(df: pd.DataFrame, params: dict) -> dict:
    """Replay all bars. Returns full state including all closed trades and current position."""
    sq_thresh = params["squeeze_threshold"]
    sl_pct = params["stop_loss_pct"]
    max_hold = params["max_hold_days"]

    closes = df["close"].values.astype(float)
    opens = df["open"].values.astype(float)
    lows = df["low"].values.astype(float)
    dates = df["timestamp"].values
    n = len(df)

    today_date = pd.Timestamp(dates[-1]).date()

    in_position = False
    qty = 0
    entry_price = 0.0
    entry_date: pd.Timestamp | None = None
    stop_price = 0.0

    realized_pnl = 0.0
    closed_trades: list[dict] = []
    today_closed: list[dict] = []
    today_entry: dict | None = None

    paper_start = pd.Timestamp(PAPER_TRADING_START).date()

    def close_trade(exit_price: float, exit_date: pd.Timestamp, reason: str) -> None:
        nonlocal realized_pnl, in_position, qty, entry_price, entry_date, stop_price
        pnl = (exit_price - entry_price) * qty
        is_live_trade = entry_date and entry_date.date() >= paper_start
        if is_live_trade:
            realized_pnl += pnl
        trade = {
            "entry_date": str(entry_date.date()) if entry_date else "",
            "exit_date": str(exit_date.date()),
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "qty": qty,
            "pnl": round(pnl, 2),
            "reason": reason,
        }
        if is_live_trade:
            closed_trades.append(trade)
            if exit_date.date() == today_date:
                today_closed.append(trade)
        in_position = False
        qty = 0
        entry_price = 0.0
        entry_date = None
        stop_price = 0.0

    for i in range(BB_WINDOW, n):
        window_c = closes[i - BB_WINDOW + 1: i + 1]
        middle = float(np.mean(window_c))
        std = float(np.std(window_c, ddof=1))
        upper = middle + BB_STD_MULT * std
        close_i = float(closes[i])
        date_i = pd.Timestamp(dates[i])
        is_today = (date_i.date() == today_date)

        if in_position:
            days_held = (date_i - entry_date).days

            # Stop loss
            if float(lows[i]) <= stop_price:
                close_trade(stop_price, date_i, "stop_loss")
                continue

            # Exit conditions
            if close_i < middle:
                fill = float(opens[i + 1]) if i + 1 < n else close_i
                close_trade(fill, date_i, "price_below_midband")
                continue

            if days_held >= max_hold:
                fill = float(opens[i + 1]) if i + 1 < n else close_i
                close_trade(fill, date_i, f"max_{max_hold}_days_held")
                continue

        # Entry signal: previous bar in squeeze AND today breaks above upper band
        if not in_position and i >= BB_WINDOW:
            prev_w = closes[i - BB_WINDOW: i]
            prev_mid = float(np.mean(prev_w))
            prev_std = float(np.std(prev_w, ddof=1))
            prev_upper = prev_mid + BB_STD_MULT * prev_std
            prev_lower = prev_mid - BB_STD_MULT * prev_std
            prev_bw = (prev_upper - prev_lower) / prev_mid if prev_mid > 0 else 999.0
            prev_squeeze = prev_bw < sq_thresh

            if prev_squeeze and close_i > upper:
                fill_price = float(opens[i + 1]) if i + 1 < n else close_i
                fill_date = pd.Timestamp(dates[i + 1]) if i + 1 < n else date_i
                qty = max(1, int(CAPITAL_PER_TRADE / fill_price))
                entry_price = fill_price
                entry_date = fill_date
                stop_price = entry_price * (1 - sl_pct / 100.0)
                in_position = True
                if is_today:
                    today_entry = {
                        "entry_price": round(fill_price, 2),
                        "qty": qty,
                        "stop_loss": round(stop_price, 2),
                        "capital": round(qty * fill_price, 0),
                    }

    # Final state
    last_close = round(float(closes[-1]), 2)
    unrealized_pnl = round((last_close - entry_price) * qty, 2) if in_position else 0.0
    days_held = (pd.Timestamp(dates[-1]) - entry_date).days if in_position and entry_date else 0
    days_left = max(0, max_hold - days_held)

    wins = sum(1 for t in closed_trades if t["pnl"] > 0)
    losses = len(closed_trades) - wins
    win_rate = wins / len(closed_trades) if closed_trades else 0.0

    # Almost-signal detection: in a squeeze but haven't broken out yet
    almost_signal = None
    if not in_position and n > BB_WINDOW:
        _wc = closes[n - BB_WINDOW: n]
        _mid = float(np.mean(_wc))
        _std = float(np.std(_wc, ddof=1))
        _upper = _mid + BB_STD_MULT * _std
        _lower = _mid - BB_STD_MULT * _std
        _bw = (_upper - _lower) / _mid if _mid > 0 else 999.0
        if _bw < sq_thresh:
            pct_to_breakout = (_upper - closes[-1]) / closes[-1] * 100
            almost_signal = {
                "reason": f"BB squeeze active (bw={_bw:.3f} < {sq_thresh}), "
                          f"price {pct_to_breakout:.1f}% below breakout at ₹{_upper:.2f}"
            }

    return {
        "in_position": in_position,
        "entry_price": round(entry_price, 2) if in_position else None,
        "entry_date": str(entry_date.date()) if in_position and entry_date else None,
        "qty": qty if in_position else 0,
        "stop_price": round(stop_price, 2) if in_position else None,
        "last_close": last_close,
        "unrealized_pnl": unrealized_pnl,
        "days_held": days_held if in_position else 0,
        "days_left": days_left,
        "realized_pnl": round(realized_pnl, 2),
        "total_trades": len(closed_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 3),
        "closed_trades": closed_trades,
        "today_closed": today_closed,
        "today_entry": today_entry,
        "today_date": str(today_date),
        "almost_signal": almost_signal,
    }


def build_digest(symbol_states: list[tuple[str, dict]]) -> str:
    today = datetime.now().strftime("%d %b %Y")
    lines = [f"📊 BB Squeeze Swing — {today}"]
    lines.append("=" * 35)

    total_unrealized = 0.0
    total_realized = 0.0
    new_entries = []
    new_exits = []
    open_positions = []

    for sym, st in symbol_states:
        total_realized += st["realized_pnl"]
        if st["in_position"]:
            total_unrealized += st["unrealized_pnl"]
            open_positions.append((sym, st))
        for t in st["today_closed"]:
            new_exits.append((sym, t))
        if st["today_entry"]:
            new_entries.append((sym, st["today_entry"]))

    # New entries today
    if new_entries:
        lines.append("\n🟢 NEW ENTRIES TODAY")
        for sym, e in new_entries:
            lines.append(f"  {sym}: BUY {e['qty']} shares @ ₹{e['entry_price']:,}")
            lines.append(f"    Stop loss : ₹{e['stop_loss']:,}")
            lines.append(f"    Capital   : ₹{e['capital']:,.0f}")
            lines.append(f"    Action    : Place a BUY order for {sym} at market open tomorrow")

    # Exits today
    if new_exits:
        lines.append("\n🔴 EXITS TODAY")
        for sym, t in new_exits:
            pnl = t["pnl"]
            emoji = "✅" if pnl >= 0 else "❌"
            sign = "+" if pnl >= 0 else ""
            lines.append(f"  {emoji} {sym}: SELL {t['qty']} shares")
            lines.append(f"    Bought @ ₹{t['entry_price']:,} on {t['entry_date']}")
            lines.append(f"    Sold   @ ₹{t['exit_price']:,} today")
            lines.append(f"    P&L    : {sign}₹{pnl:,.0f}")
            lines.append(f"    Reason : {t['reason']}")
            lines.append(f"    Action : Place a SELL order for {sym} at market open tomorrow")

    # Open positions
    if open_positions:
        lines.append("\n📂 OPEN POSITIONS")
        for sym, st in open_positions:
            upnl = st["unrealized_pnl"]
            sign = "+" if upnl >= 0 else ""
            pct = (upnl / (st["entry_price"] * st["qty"])) * 100 if st["entry_price"] and st["qty"] else 0
            lines.append(f"  {sym}: {st['qty']} shares held since {st['entry_date']}")
            lines.append(f"    Entry     : ₹{st['entry_price']:,}")
            lines.append(f"    Now       : ₹{st['last_close']:,}")
            lines.append(f"    Stop loss : ₹{st['stop_price']:,}")
            lines.append(f"    Unrealised: {sign}₹{upnl:,.0f} ({pct:+.1f}%)")
            lines.append(f"    Days left : {st['days_left']} before time exit")

    if not new_entries and not new_exits and not open_positions:
        lines.append("\n💤 No positions. No signals today.")

    # P&L Summary
    lines.append("\n" + "─" * 35)
    lines.append("P&L SUMMARY (since tracking started)")
    total = total_realized + total_unrealized
    r_sign = "+" if total_realized >= 0 else ""
    u_sign = "+" if total_unrealized >= 0 else ""
    t_sign = "+" if total >= 0 else ""
    lines.append(f"  Realised  : {r_sign}₹{total_realized:,.0f}")
    lines.append(f"  Unrealised: {u_sign}₹{total_unrealized:,.0f}")
    lines.append(f"  Total     : {t_sign}₹{total:,.0f}")

    # Win rate across all closed trades
    all_trades = sum(st["total_trades"] for _, st in symbol_states)
    all_wins = sum(st["wins"] for _, st in symbol_states)
    if all_trades > 0:
        lines.append(f"  Win rate  : {all_wins}/{all_trades} ({all_wins/all_trades*100:.0f}%)")

    return "\n".join(lines)


def run_paper_trader(bot_token: str, chat_id: str) -> None:
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    portfolio = load_portfolio()
    if not portfolio:
        notifier.send("⚠️ BB Squeeze: portfolio file not found.")
        return

    symbol_states = []
    for entry in portfolio:
        sym = entry["symbol"]
        _log.info(f"Replaying {sym}...")
        df = fetch_data(sym)
        if df is None or len(df) < BB_WINDOW + 2:
            _log.warning(f"[{sym}] Insufficient data, skipping.")
            continue
        state = replay_symbol(df, entry["best_params"])
        symbol_states.append((sym, state))

    digest = build_digest(symbol_states)
    _log.info("Sending digest:\n" + digest)
    notifier.send(digest)


if __name__ == "__main__":
    import argparse
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""))
    args = parser.parse_args()
    run_paper_trader(args.bot_token, args.chat_id)
