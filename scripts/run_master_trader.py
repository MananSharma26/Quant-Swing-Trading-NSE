"""Master Portfolio Risk Engine — Unified Daily Digest.

Aggregates all 3 strategies (Long-Only Black Swan, BB Squeeze, MA Pullback).
Calculates total open capital, remaining free cash, and dynamically
sizes new signals based on a 2 Lakh total account limit and 50k floor.
Sends ONE unified Telegram message.
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from trading_engine.notifications.telegram import TelegramNotifier

# Import logic from the 3 standalone scripts
import run_paper_trader as pt_swan
import run_bb_squeeze_trader as pt_bb
import run_ma_pullback_trader as pt_ma

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

TOTAL_ACCOUNT_CAPITAL = 2_00_000
MIN_CHUNK_SIZE = 50_000


def run_master_trader(bot_token: str, chat_id: str):
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    _log.info("Running Long-Only Black Swan...")
    swan_portfolio = pt_swan.load_portfolio()
    swan_results = []
    for pair in swan_portfolio:
        r = pt_swan.replay_pair(pair["symbol_a"], pair["symbol_b"], pair["optimal_params"])
        swan_results.append(r)

    _log.info("Running BB Squeeze...")
    bb_portfolio = pt_bb.load_portfolio()
    bb_states = []
    for entry in bb_portfolio:
        sym = entry["symbol"]
        df = pt_bb.fetch_data(sym)
        if df is not None and len(df) >= pt_bb.BB_WINDOW + 2:
            state = pt_bb.replay_symbol(df, entry["best_params"])
            bb_states.append((sym, state))

    _log.info("Running MA Pullback...")
    ma_portfolio = pt_ma.load_portfolio()
    ma_states = []
    for entry in ma_portfolio:
        sym = entry["symbol"]
        df = pt_ma.fetch_data(sym)
        if df is not None and len(df) >= int(entry["optimal_params"]["trend_ma_period"]) + 2:
            state = pt_ma.replay_symbol(df, entry["optimal_params"])
            ma_states.append((sym, state))

    # Aggregate Open Positions & Capital Locked
    total_locked = 0.0
    total_realized = 0.0
    total_unrealized = 0.0
    
    open_positions = []
    new_exits = []
    raw_new_entries = []  # (strategy_name, symbol, entry_dict)

    # 1. Process Swan
    for r in swan_results:
        if r.get("error"): continue
        total_realized += r["realized_pnl"]
        for t in r["today_closed"]:
            new_exits.append(("Black Swan", r["pair"], t))
        if r["position"]:
            is_new_today = (r["entry_date"] == r["today_date"])
            if not is_new_today:
                locked = r["entry_price"] * r["entry_qty"]
                total_locked += locked
                total_unrealized += r["unrealized_pnl"]
                open_positions.append(("Black Swan", r["pair"], r))
            else:
                raw_new_entries.append(("Black Swan", r["bought_sym"], {
                    "entry_price": r["entry_price"],
                    "qty": r["entry_qty"],
                }))

    # 2. Process BB Squeeze
    for sym, st in bb_states:
        total_realized += st["realized_pnl"]
        for t in st["today_closed"]:
            new_exits.append(("BB Squeeze", sym, t))
        if st["in_position"] and st["entry_date"] != st["today_date"]: # Don't count today's entries as locked yet
            locked = st["entry_price"] * st["qty"]
            total_locked += locked
            total_unrealized += st["unrealized_pnl"]
            open_positions.append(("BB Squeeze", sym, st))
        if st["today_entry"]:
            raw_new_entries.append(("BB Squeeze", sym, st["today_entry"]))

    # 3. Process MA Pullback
    for sym, st in ma_states:
        total_realized += st["realized_pnl"]
        for t in st["today_closed"]:
            new_exits.append(("MA Pullback", sym, t))
        if st["in_position"] and st["entry_date"] != st["today_date"]:
            locked = st["entry_price"] * st["qty"]
            total_locked += locked
            total_unrealized += st["unrealized_pnl"]
            open_positions.append(("MA Pullback", sym, st))
        if st["today_entry"]:
            raw_new_entries.append(("MA Pullback", sym, st["today_entry"]))

    # Dynamic Capital Allocation
    free_cash = TOTAL_ACCOUNT_CAPITAL - total_locked
    if free_cash < 0:
        free_cash = 0

    approved_entries = []
    rejected_entries = []

    if raw_new_entries:
        if free_cash < MIN_CHUNK_SIZE:
            # Reject all, no cash
            for strat, sym, e in raw_new_entries:
                rejected_entries.append((strat, sym, "Insufficient Free Cash"))
        else:
            # Calculate max slots
            max_slots = int(free_cash // MIN_CHUNK_SIZE)
            
            # Rank signals (for now, simply by strategy precedence: MA > BB > Swan)
            def strat_score(s):
                if s[0] == "MA Pullback": return 3
                if s[0] == "BB Squeeze": return 2
                return 1
                
            raw_new_entries.sort(key=strat_score, reverse=True)
            
            selected = raw_new_entries[:max_slots]
            rejected = raw_new_entries[max_slots:]
            
            chunk_size = free_cash / len(selected)
            
            for strat, sym, e in selected:
                new_qty = max(1, int(chunk_size / e["entry_price"]))
                e["qty"] = new_qty
                e["capital"] = new_qty * e["entry_price"]
                approved_entries.append((strat, sym, e))
                
            for strat, sym, e in rejected:
                rejected_entries.append((strat, sym, "Ranked out (Max slots reached)"))

    # Build Unified Digest
    today = datetime.now().strftime("%d %b %Y")
    lines = [f"👑 MASTER RISK ENGINE — {today}"]
    lines.append("=" * 35)
    
    # Portfolio Status
    lines.append(f"💰 Account Limit: ₹{TOTAL_ACCOUNT_CAPITAL:,.0f}")
    lines.append(f"🔒 Locked Cash  : ₹{total_locked:,.0f}")
    lines.append(f"💸 Free Cash    : ₹{free_cash:,.0f}")
    lines.append("-" * 35)

    if approved_entries:
        lines.append("\n🟢 APPROVED ENTRIES (Dynamically Sized)")
        for strat, sym, e in approved_entries:
            lines.append(f"  [{strat}] {sym}")
            lines.append(f"    BUY {e['qty']} shares @ ₹{e['entry_price']:,}")
            lines.append(f"    Allocated: ₹{e['capital']:,.0f}")
            if "stop_loss" in e:
                lines.append(f"    Stop loss: ₹{e['stop_loss']:,.2f}")
    
    if rejected_entries:
        lines.append("\n🟡 REJECTED ENTRIES (Capital Protection)")
        for strat, sym, reason in rejected_entries:
            lines.append(f"  [{strat}] {sym} - {reason}")

    if new_exits:
        lines.append("\n🔴 EXITS TODAY")
        for strat, sym, t in new_exits:
            pnl = t["pnl"]
            emoji = "✅" if pnl >= 0 else "❌"
            sign = "+" if pnl >= 0 else ""
            lines.append(f"  {emoji} [{strat}] {sym}: SELL {t['qty']} shares")
            lines.append(f"    P&L: {sign}₹{pnl:,.0f}")

    if open_positions:
        lines.append("\n📂 OPEN POSITIONS")
        for strat, sym, st in open_positions:
            if strat == "Black Swan":
                upnl = st["unrealized_pnl"]
                sign = "+" if upnl >= 0 else ""
                lines.append(f"  [{strat}] {sym}: {st['entry_qty']} shares @ ₹{st['entry_price']:,} -> Unrealised: {sign}₹{upnl:,.0f}")
            else:
                upnl = st["unrealized_pnl"]
                sign = "+" if upnl >= 0 else ""
                lines.append(f"  [{strat}] {sym}: {st['qty']} shares @ ₹{st['entry_price']:,} -> Unrealised: {sign}₹{upnl:,.0f}")

    if not approved_entries and not new_exits and not open_positions:
        lines.append("\n💤 No open positions. No signals today.")

    lines.append("\n" + "─" * 35)
    lines.append("GLOBAL P&L SUMMARY (Since Inception)")
    total = total_realized + total_unrealized
    r_sign = "+" if total_realized >= 0 else ""
    u_sign = "+" if total_unrealized >= 0 else ""
    t_sign = "+" if total >= 0 else ""
    lines.append(f"  Realised  : {r_sign}₹{total_realized:,.0f}")
    lines.append(f"  Unrealised: {u_sign}₹{total_unrealized:,.0f}")
    lines.append(f"  Total     : {t_sign}₹{total:,.0f}")

    digest = "\n".join(lines)
    _log.info("Sending Master Digest:\n" + digest)
    notifier.send(digest)


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""))
    args = parser.parse_args()
    run_master_trader(args.bot_token, args.chat_id)
