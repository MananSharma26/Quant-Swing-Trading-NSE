"""Long-Only Black Swan — Daily Paper Trading Digest.

Replays all historical bars to reconstruct full strategy state, then sends
ONE Telegram message per day with everything: open positions, today's signals,
realized P&L, and unrealized P&L.

No noise. No "scanning..." pings. One message with all the info.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
from datetime import datetime
from decimal import Decimal
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
CAPITAL_PER_LEG = 100_000
PAPER_TRADING_START = "2026-06-01"  # Only count P&L from this date onwards


def load_portfolio() -> list[dict]:
    path = REPORTS_DIR / "optimal_long_only_portfolio.json"
    if not path.exists():
        _log.error("optimal_long_only_portfolio.json not found.")
        return []
    with open(path) as f:
        return json.load(f)


def fetch(symbol: str) -> pd.DataFrame | None:
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


def replay_pair(sym_a: str, sym_b: str, params: dict) -> dict:
    """
    Replay all bars for a pair. Returns full state:
    - current position (if any) with entry date, price, qty, unrealized P&L
    - trades that closed TODAY
    - all realized P&L since we started tracking
    """
    window = params["window_size"]
    entry_z = params["entry_z_score"]
    exit_z = params.get("exit_z_score", 0.0)
    stop_z = params["stop_loss_z_score"]
    max_hold = params.get("max_hold_days", 30)

    df_a = fetch(sym_a)
    df_b = fetch(sym_b)
    if df_a is None or df_b is None:
        return {"error": f"Could not fetch data for {sym_a} or {sym_b}"}

    # Align on date
    df_a = df_a.set_index("timestamp")
    df_b = df_b.set_index("timestamp")
    common = df_a.index.intersection(df_b.index)
    df_a = df_a.loc[common].reset_index()
    df_b = df_b.loc[common].reset_index()

    if len(common) < window + 2:
        return {"error": "Insufficient data"}

    today_date = pd.Timestamp(df_a["timestamp"].iloc[-1]).date()

    ratio_history: list[float] = []
    position: str | None = None      # None, "LONG_A", "LONG_B"
    entry_price = 0.0
    entry_qty = 0
    entry_date: pd.Timestamp | None = None
    stop_price = 0.0
    last_z = 0.0  # track latest z-score for almost-signal detection

    realized_pnl = 0.0
    closed_trades: list[dict] = []   # all closed trades
    today_closed: list[dict] = []    # trades closed on today's bar

    for i in range(len(df_a)):
        pa = float(df_a["close"].iloc[i])
        pb = float(df_b["close"].iloc[i])
        ts = pd.Timestamp(df_a["timestamp"].iloc[i])

        if pb == 0:
            continue

        ratio = pa / pb
        ratio_history.append(ratio)
        if len(ratio_history) > window:
            ratio_history.pop(0)
        if len(ratio_history) < window:
            continue

        mean_r = statistics.mean(ratio_history)
        std_r = statistics.stdev(ratio_history)
        if std_r == 0:
            continue
        z = (ratio - mean_r) / std_r
        last_z = z
        is_today = (ts.date() == today_date)
        days_held = (ts - entry_date).days if entry_date else 0

        paper_start = pd.Timestamp(PAPER_TRADING_START).date()

        def close_position(close_sym: str, close_price: float, reason: str) -> None:
            nonlocal realized_pnl, position, entry_price, entry_qty, entry_date, stop_price
            pnl = (close_price - entry_price) * entry_qty
            exit_date = ts.date()
            # Only count P&L for trades entered on or after paper trading start
            is_live_trade = entry_date and entry_date.date() >= paper_start
            if is_live_trade:
                realized_pnl += pnl
            trade = {
                "pair": f"{sym_a}/{sym_b}",
                "side": position,
                "bought": close_sym if position == "LONG_A" else (sym_b if position == "LONG_B" else ""),
                "entry_price": round(entry_price, 2),
                "exit_price": round(close_price, 2),
                "qty": entry_qty,
                "pnl": round(pnl, 2),
                "reason": reason,
                "entry_date": str(entry_date.date()) if entry_date else "",
                "exit_date": str(exit_date),
                "live": is_live_trade,
            }
            if is_live_trade:
                closed_trades.append(trade)
            if is_today and is_live_trade:
                today_closed.append(trade)
            position = None
            entry_price = 0.0
            entry_qty = 0
            entry_date = None
            stop_price = 0.0

        # Exit logic
        if position == "LONG_A":
            if z <= -stop_z:
                close_position(sym_a, pa, "stop_loss")
            elif z >= -exit_z:
                close_position(sym_a, pa, "mean_reverted" if z >= 0 else "exit_z")
            elif days_held >= max_hold:
                close_position(sym_a, pa, "max_hold_days")

        elif position == "LONG_B":
            if z >= stop_z:
                close_position(sym_b, pb, "stop_loss")
            elif z <= exit_z:
                close_position(sym_b, pb, "mean_reverted" if z <= 0 else "exit_z")
            elif days_held >= max_hold:
                close_position(sym_b, pb, "max_hold_days")

        # Entry logic
        if position is None:
            if z <= -entry_z:
                # A is cheap relative to B → buy A
                qty = max(1, int(CAPITAL_PER_LEG / pa))
                position = "LONG_A"
                entry_price = pa
                entry_qty = qty
                entry_date = ts
                stop_price = pa  # not used for long — z-score stop instead
            elif z >= entry_z:
                # B is cheap relative to A → buy B
                qty = max(1, int(CAPITAL_PER_LEG / pb))
                position = "LONG_B"
                entry_price = pb
                entry_qty = qty
                entry_date = ts
                stop_price = pb

    # Current state
    last_pa = float(df_a["close"].iloc[-1])
    last_pb = float(df_b["close"].iloc[-1])

    if position == "LONG_A":
        current_price = last_pa
        bought_sym = sym_a
    elif position == "LONG_B":
        current_price = last_pb
        bought_sym = sym_b
    else:
        current_price = 0.0
        bought_sym = None

    unrealized_pnl = round((current_price - entry_price) * entry_qty, 2) if position else 0.0

    # Almost-signal: z-score approaching entry threshold (within 75%)
    almost_signal = None
    if not position and abs(last_z) >= entry_z * 0.75:
        side = sym_a if last_z <= -entry_z * 0.75 else sym_b
        almost_signal = {
            "reason": f"z-score {last_z:.2f} approaching entry at +/-{entry_z} (buy {side})"
        }

    return {
        "pair": f"{sym_a}/{sym_b}",
        "sym_a": sym_a,
        "sym_b": sym_b,
        "position": position,
        "bought_sym": bought_sym,
        "entry_price": round(entry_price, 2) if position else None,
        "entry_date": str(entry_date.date()) if entry_date else None,
        "entry_qty": entry_qty if position else 0,
        "current_price": round(current_price, 2) if position else None,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": round(realized_pnl, 2),
        "total_trades": len(closed_trades),
        "wins": sum(1 for t in closed_trades if t["pnl"] > 0),
        "closed_trades": closed_trades,
        "today_closed": today_closed,
        "today_date": str(today_date),
        "almost_signal": almost_signal,
        "error": None,
    }


def build_digest(results: list[dict]) -> str:
    today = datetime.now().strftime("%d %b %Y")
    lines = [f"🦢 Long-Only Black Swan — {today}"]
    lines.append("=" * 35)

    total_unrealized = 0.0
    total_realized = 0.0
    any_content = False

    for r in results:
        if r.get("error"):
            lines.append(f"\n⚠️ {r.get('pair','?')}: {r['error']}")
            continue

        pair = r["pair"]
        total_realized += r["realized_pnl"]

        # Today's closed trades (exits that fired today)
        for t in r["today_closed"]:
            any_content = True
            pnl = t["pnl"]
            emoji = "✅" if pnl >= 0 else "❌"
            sign = "+" if pnl >= 0 else ""
            lines.append(f"\n{emoji} CLOSED: {pair}")
            lines.append(f"  You bought  : {t['bought']} on {t['entry_date']}")
            lines.append(f"  Entry price : ₹{t['entry_price']:,}")
            lines.append(f"  Exit price  : ₹{t['exit_price']:,}")
            lines.append(f"  Qty         : {t['qty']} shares")
            lines.append(f"  Realised P&L: {sign}₹{pnl:,.0f}")
            lines.append(f"  Reason      : {t['reason']}")

        # Open position
        if r["position"]:
            any_content = True
            total_unrealized += r["unrealized_pnl"]
            upnl = r["unrealized_pnl"]
            sign = "+" if upnl >= 0 else ""
            pct = (upnl / (r["entry_price"] * r["entry_qty"])) * 100 if r["entry_price"] and r["entry_qty"] else 0
            days_held = (datetime.now().date() - datetime.strptime(r["entry_date"], "%Y-%m-%d").date()).days
            lines.append(f"\n📂 HOLDING: {pair}")
            lines.append(f"  Bought      : {r['bought_sym']} on {r['entry_date']}")
            lines.append(f"  Entry price : ₹{r['entry_price']:,}")
            lines.append(f"  Now at      : ₹{r['current_price']:,}")
            lines.append(f"  Qty         : {r['entry_qty']} shares")
            lines.append(f"  Unrealised  : {sign}₹{upnl:,.0f} ({pct:+.1f}%)")
            lines.append(f"  Days held   : {days_held}")

    # P&L summary
    lines.append("\n" + "─" * 35)
    r_sign = "+" if total_realized >= 0 else ""
    u_sign = "+" if total_unrealized >= 0 else ""
    total = total_realized + total_unrealized
    t_sign = "+" if total >= 0 else ""
    lines.append(f"Realised P&L  : {r_sign}₹{total_realized:,.0f}")
    lines.append(f"Unrealised P&L: {u_sign}₹{total_unrealized:,.0f}")
    lines.append(f"Total P&L     : {t_sign}₹{total:,.0f}")

    if not any_content:
        lines.insert(2, "\n💤 No open positions. No signals today.")

    return "\n".join(lines)


def run_paper_trader(bot_token: str, chat_id: str) -> None:
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    portfolio = load_portfolio()
    if not portfolio:
        notifier.send("⚠️ Black Swan: portfolio file not found.")
        return

    results = []
    for pair in portfolio:
        _log.info(f"Replaying {pair['symbol_a']}/{pair['symbol_b']}...")
        r = replay_pair(pair["symbol_a"], pair["symbol_b"], pair["optimal_params"])
        results.append(r)

    digest = build_digest(results)
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
