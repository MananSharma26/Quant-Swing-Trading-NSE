"""MA Pullback Swing Trader — Daily Digest.

Replays all bars to reconstruct state for each symbol using the exact same
MA Pullback logic as ma_pullback.py (fixed Wilder's RSI seed). Sends ONE
Telegram message per day with: open positions + unrealized P&L, any trades
that closed today, any new entry signals, and total P&L summary.

Symbols and parameters are loaded from reports/optimal_ma_pullback_portfolio.json.
Only trades entered on or after PAPER_TRADING_START count toward P&L.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.notifications.telegram import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

REPORTS_DIR = ROOT / "reports"
CAPITAL_PER_TRADE = 100_000
PAPER_TRADING_START = "2026-06-01"


def load_portfolio() -> list[dict]:
    path = REPORTS_DIR / "optimal_ma_pullback_portfolio.json"
    if not path.exists():
        _log.error("optimal_ma_pullback_portfolio.json not found.")
        return []
    with open(path) as f:
        return json.load(f)


def fetch_data(symbol: str) -> pd.DataFrame | None:
    try:
        df = yf.download(f"{symbol}.NS", period="2y", interval="1d",
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
    """Replay all bars using MA Pullback logic. Returns full state."""
    trend_period = int(params["trend_ma_period"])
    pullback_period = int(params["pullback_ma_period"])
    rsi_period = int(params.get("rsi_period", 14))
    rsi_oversold = float(params["rsi_oversold"])
    stop_loss_pct = float(params["stop_loss_pct"])
    target_pct = float(params["target_pct"])
    max_hold = int(params["max_hold_days"])

    closes = df["close"].values.astype(float)
    opens = df["open"].values.astype(float)
    dates = df["timestamp"].values
    n = len(closes)

    today_date = pd.Timestamp(dates[-1]).date()
    paper_start = pd.Timestamp(PAPER_TRADING_START).date()

    # RSI state
    avg_gain = 0.0
    avg_loss = 0.0
    close_history: list[float] = []

    in_position = False
    qty = 0
    entry_price = 0.0
    entry_date: pd.Timestamp | None = None
    stop_price = 0.0
    target_price = 0.0

    realized_pnl = 0.0
    closed_trades: list[dict] = []
    today_closed: list[dict] = []
    today_entry: dict | None = None

    def close_trade(exit_price: float, exit_date: pd.Timestamp, reason: str) -> None:
        nonlocal realized_pnl, in_position, qty, entry_price, entry_date
        nonlocal stop_price, target_price
        pnl = (exit_price - entry_price) * qty
        is_live = entry_date and entry_date.date() >= paper_start
        if is_live:
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
        if is_live:
            closed_trades.append(trade)
            if exit_date.date() == today_date:
                today_closed.append(trade)
        in_position = False
        qty = 0
        entry_price = 0.0
        entry_date = None
        stop_price = 0.0
        target_price = 0.0

    for i in range(n):
        close_i = closes[i]
        date_i = pd.Timestamp(dates[i])

        # --- Update Wilder's RSI (fixed seed) ---
        if len(close_history) > 0:
            change = close_i - close_history[-1]
            gain = max(0.0, change)
            loss = max(0.0, -change)

            if len(close_history) < rsi_period:
                # Accumulate raw sums (not yet averaged)
                avg_gain += gain
                avg_loss += loss
            elif len(close_history) == rsi_period:
                # Seed: include current bar before dividing (fixed off-by-one)
                avg_gain = (avg_gain + gain) / rsi_period
                avg_loss = (avg_loss + loss) / rsi_period
            else:
                # Wilder's EMA smoothing
                avg_gain = (avg_gain * (rsi_period - 1) + gain) / rsi_period
                avg_loss = (avg_loss * (rsi_period - 1) + loss) / rsi_period

        close_history.append(close_i)
        if len(close_history) > trend_period + 1:
            close_history.pop(0)

        # Not enough data yet
        if len(close_history) < trend_period:
            continue

        # --- Compute indicators ---
        trend_sma = sum(close_history[-trend_period:]) / trend_period
        pullback_sma = sum(close_history[-pullback_period:]) / pullback_period

        rsi = 50.0
        if avg_loss == 0:
            rsi = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # --- Exit logic (if in position) ---
        if in_position:
            days_held = (date_i - entry_date).days

            if close_i <= stop_price:
                fill = float(opens[i + 1]) if i + 1 < n else close_i
                fill_date = pd.Timestamp(dates[i + 1]) if i + 1 < n else date_i
                close_trade(fill, fill_date, "stop_loss")
                continue

            if close_i >= target_price:
                fill = float(opens[i + 1]) if i + 1 < n else close_i
                fill_date = pd.Timestamp(dates[i + 1]) if i + 1 < n else date_i
                close_trade(fill, fill_date, "target_hit")
                continue

            if days_held >= max_hold:
                fill = float(opens[i + 1]) if i + 1 < n else close_i
                fill_date = pd.Timestamp(dates[i + 1]) if i + 1 < n else date_i
                close_trade(fill, fill_date, f"max_{max_hold}_days_held")
                continue

        # --- Entry logic ---
        if not in_position:
            is_uptrend = close_i > trend_sma
            is_pullback = close_i <= pullback_sma
            is_oversold = rsi <= rsi_oversold

            if is_uptrend and is_pullback and is_oversold:
                fill_price = float(opens[i + 1]) if i + 1 < n else close_i
                fill_date = pd.Timestamp(dates[i + 1]) if i + 1 < n else date_i
                qty = max(1, int(CAPITAL_PER_TRADE / fill_price))
                entry_price = fill_price
                entry_date = fill_date
                stop_price = entry_price * (1 - stop_loss_pct / 100.0)
                target_price = entry_price * (1 + target_pct / 100.0)
                in_position = True

                if date_i.date() == today_date:
                    today_entry = {
                        "entry_price": round(fill_price, 2),
                        "qty": qty,
                        "stop_loss": round(stop_price, 2),
                        "target": round(target_price, 2),
                        "capital": round(qty * fill_price, 0),
                    }

    # Final state
    last_close = round(float(closes[-1]), 2)
    unrealized_pnl = round((last_close - entry_price) * qty, 2) if in_position else 0.0
    days_held = (pd.Timestamp(dates[-1]) - entry_date).days if in_position and entry_date else 0
    days_left = max(0, max_hold - days_held)

    wins = sum(1 for t in closed_trades if t["pnl"] > 0)
    win_rate = wins / len(closed_trades) if closed_trades else 0.0

    return {
        "in_position": in_position,
        "entry_price": round(entry_price, 2) if in_position else None,
        "entry_date": str(entry_date.date()) if in_position and entry_date else None,
        "qty": qty if in_position else 0,
        "stop_price": round(stop_price, 2) if in_position else None,
        "target_price": round(target_price, 2) if in_position else None,
        "last_close": last_close,
        "unrealized_pnl": unrealized_pnl,
        "days_held": days_held if in_position else 0,
        "days_left": days_left,
        "realized_pnl": round(realized_pnl, 2),
        "total_trades": len(closed_trades),
        "wins": wins,
        "losses": len(closed_trades) - wins,
        "win_rate": round(win_rate, 3),
        "today_closed": today_closed,
        "today_entry": today_entry,
        "today_date": str(today_date),
    }


def build_digest(symbol_states: list[tuple[str, dict]]) -> str:
    today = datetime.now().strftime("%d %b %Y")
    lines = [f"📈 MA Pullback Swing — {today}"]
    lines.append("=" * 35)

    total_realized = 0.0
    total_unrealized = 0.0
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

    if new_entries:
        lines.append("\n🟢 NEW ENTRIES TODAY")
        for sym, e in new_entries:
            lines.append(f"  {sym}: BUY {e['qty']} shares @ ₹{e['entry_price']:,}")
            lines.append(f"    Stop loss : ₹{e['stop_loss']:,}")
            lines.append(f"    Target    : ₹{e['target']:,}")
            lines.append(f"    Capital   : ₹{e['capital']:,.0f}")
            lines.append(f"    Action    : Place a BUY order for {sym} at market open tomorrow")

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
            lines.append(f"    Target    : ₹{st['target_price']:,}")
            lines.append(f"    Unrealised: {sign}₹{upnl:,.0f} ({pct:+.1f}%)")
            lines.append(f"    Days left : {st['days_left']} before time exit")

    if not new_entries and not new_exits and not open_positions:
        lines.append("\n💤 No positions. No signals today.")

    lines.append("\n" + "─" * 35)
    lines.append("P&L SUMMARY (since tracking started)")
    total = total_realized + total_unrealized
    r_sign = "+" if total_realized >= 0 else ""
    u_sign = "+" if total_unrealized >= 0 else ""
    t_sign = "+" if total >= 0 else ""
    lines.append(f"  Realised  : {r_sign}₹{total_realized:,.0f}")
    lines.append(f"  Unrealised: {u_sign}₹{total_unrealized:,.0f}")
    lines.append(f"  Total     : {t_sign}₹{total:,.0f}")

    all_trades = sum(st["total_trades"] for _, st in symbol_states)
    all_wins = sum(st["wins"] for _, st in symbol_states)
    if all_trades > 0:
        lines.append(f"  Win rate  : {all_wins}/{all_trades} ({all_wins/all_trades*100:.0f}%)")

    return "\n".join(lines)


def run_paper_trader(bot_token: str, chat_id: str) -> None:
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    portfolio = load_portfolio()
    if not portfolio:
        notifier.send("⚠️ MA Pullback: portfolio file not found.")
        return

    symbol_states = []
    for entry in portfolio:
        sym = entry["symbol"]
        _log.info(f"Replaying {sym}...")
        df = fetch_data(sym)
        if df is None or len(df) < int(entry["optimal_params"]["trend_ma_period"]) + 2:
            _log.warning(f"[{sym}] Insufficient data, skipping.")
            continue
        state = replay_symbol(df, entry["optimal_params"])
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
