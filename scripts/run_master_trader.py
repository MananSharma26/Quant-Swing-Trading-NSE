"""Master Portfolio Risk Engine — Unified Daily Digest.

Aggregates all 4 strategies (Long-Only Black Swan, BB Squeeze, MA Pullback,
Supertrend). Calculates total open capital, remaining free cash, and dynamically
sizes new signals based on a 2 Lakh total account limit and 50k floor.
Sends ONE unified Telegram message.
"""

import argparse
import json
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

# Import logic from the 4 standalone scripts
import run_paper_trader as pt_swan
import run_bb_squeeze_trader as pt_bb
import run_ma_pullback_trader as pt_ma
import run_supertrend_trader as pt_st

from trading_engine.strategy_priority import strategy_score

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

TOTAL_ACCOUNT_CAPITAL = 2_00_000
MIN_CHUNK_SIZE = 40_000
LEDGER_PATH = ROOT / "reports" / "master_position_ledger.json"


def load_ledger() -> dict:
    """Load persisted master position ledger. Keys: 'Strategy/Symbol'."""
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_ledger(ledger: dict) -> None:
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def run_master_trader(bot_token: str, chat_id: str):
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    # Load master position ledger (persists actual qty allocated by master engine)
    ledger = load_ledger()

    fetch_errors = []     # (strategy_name, label, error_msg)

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

    _log.info("Running Supertrend...")
    st_portfolio = pt_st.load_portfolio()
    st_states = []
    for entry in st_portfolio:
        sym = entry["symbol"]
        df = pt_st.fetch_data(sym)
        if df is None or len(df) < int(entry["optimal_params"]["atr_period"]) + 2:
            fetch_errors.append(("Supertrend", sym, "Failed to fetch data"))
            continue
        state = pt_st.replay_symbol(df, entry["optimal_params"])
        st_states.append((sym, state))

    # Aggregate Open Positions & Capital Locked
    total_locked = 0.0
    total_realized = 0.0
    total_unrealized = 0.0

    open_positions = []
    new_exits = []
    raw_new_entries = []  # (strategy_name, symbol, entry_dict)
    almost_signals = []   # (strategy_name, symbol_or_pair, reason_str)

    # 1. Process Swan
    for r in swan_results:
        if r.get("error"):
            fetch_errors.append(("Black Swan", r.get("pair", "?"), r["error"]))
            continue
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
        elif r.get("almost_signal"):
            almost_signals.append(("Black Swan", r["pair"], r["almost_signal"]["reason"]))

    # 2. Process BB Squeeze
    for sym, st in bb_states:
        total_realized += st["realized_pnl"]
        for t in st["today_closed"]:
            new_exits.append(("BB Squeeze", sym, t))
        if st["in_position"] and not st.get("today_entry"):
            # includes positions filled today at open from yesterday's signal
            locked = st["entry_price"] * st["qty"]
            total_locked += locked
            total_unrealized += st["unrealized_pnl"]
            open_positions.append(("BB Squeeze", sym, st))
        if st["today_entry"]:
            raw_new_entries.append(("BB Squeeze", sym, st["today_entry"]))
        elif not st["in_position"] and st.get("almost_signal"):
            almost_signals.append(("BB Squeeze", sym, st["almost_signal"]["reason"]))

    # 3. Process MA Pullback
    for sym, st in ma_states:
        total_realized += st["realized_pnl"]
        for t in st["today_closed"]:
            new_exits.append(("MA Pullback", sym, t))
        if st["in_position"] and not st.get("today_entry"):
            locked = st["entry_price"] * st["qty"]
            total_locked += locked
            total_unrealized += st["unrealized_pnl"]
            open_positions.append(("MA Pullback", sym, st))
        if st["today_entry"]:
            raw_new_entries.append(("MA Pullback", sym, st["today_entry"]))
        elif not st["in_position"] and st.get("almost_signal"):
            almost_signals.append(("MA Pullback", sym, st["almost_signal"]["reason"]))

    # 4. Process Supertrend
    for sym, st in st_states:
        total_realized += st["realized_pnl"]
        for t in st["today_closed"]:
            new_exits.append(("Supertrend", sym, t))
        if st["in_position"] and not st.get("today_entry"):
            locked = st["entry_price"] * st["qty"]
            total_locked += locked
            total_unrealized += st["unrealized_pnl"]
            open_positions.append(("Supertrend", sym, st))
        if st["today_entry"]:
            raw_new_entries.append(("Supertrend", sym, st["today_entry"]))
        elif not st["in_position"] and st.get("almost_signal"):
            almost_signals.append(("Supertrend", sym, st["almost_signal"]["reason"]))

    # Correct exit qty/pnl using master ledger, then remove closed positions from ledger
    corrected_exits = []
    for strat, sym, t in new_exits:
        key = f"{strat}/{sym}"
        if key in ledger:
            master_qty = ledger[key]["qty"]
            entry_p = ledger[key]["entry_price"]
            t = dict(t)  # don't mutate strategy's object
            t["qty"] = master_qty
            t["pnl"] = round((t["exit_price"] - entry_p) * master_qty, 2)
            del ledger[key]
        corrected_exits.append((strat, sym, t))
    new_exits = corrected_exits

    # Correct open position qty/unrealized using ledger, recompute locked capital
    total_locked = 0.0
    total_unrealized = 0.0
    corrected_open = []
    for strat, sym, st in open_positions:
        key = f"{strat}/{sym}"
        if key in ledger:
            master_qty = ledger[key]["qty"]
            entry_p = ledger[key]["entry_price"]
            last_c = st.get("last_close") or st.get("current_price") or entry_p
            st = dict(st)
            st["qty"] = master_qty
            st["entry_qty"] = master_qty
            st["entry_price"] = entry_p
            st["unrealized_pnl"] = round((last_c - entry_p) * master_qty, 2)
            total_locked += entry_p * master_qty
            total_unrealized += st["unrealized_pnl"]
        else:
            total_locked += (st.get("entry_price") or 0) * (st.get("qty") or st.get("entry_qty") or 0)
            total_unrealized += st.get("unrealized_pnl", 0)
        corrected_open.append((strat, sym, st))
    open_positions = corrected_open

    # Dynamic Capital Allocation
    # If any strategy had a fetch error, capital accounting is untrustworthy.
    # Block all new entries to protect capital until the error clears.
    capital_safe = len(fetch_errors) == 0

    free_cash = TOTAL_ACCOUNT_CAPITAL - total_locked
    if free_cash < 0:
        free_cash = 0

    approved_entries = []
    rejected_entries = []

    if raw_new_entries:
        if not capital_safe:
            for strat, sym, e in raw_new_entries:
                rejected_entries.append((strat, sym, "Blocked — data fetch error (see warnings)"))
        elif free_cash < MIN_CHUNK_SIZE:
            for strat, sym, e in raw_new_entries:
                rejected_entries.append((strat, sym, "Insufficient Free Cash"))
        else:
            raw_new_entries.sort(key=lambda s: strategy_score(s[0]), reverse=True)

            # Rule 1: deduplicate by symbol — keep highest-priority strategy only
            seen_syms: set[str] = set()
            deduped = []
            for entry in raw_new_entries:
                sym = entry[1]
                if sym not in seen_syms:
                    seen_syms.add(sym)
                    deduped.append(entry)
                else:
                    rejected_entries.append((entry[0], entry[1], "Duplicate symbol — already taken by higher-priority strategy"))
            raw_new_entries = deduped

            # Rule 2: skip symbols already held in any open position
            held_syms: set[str] = {sym for _, sym, _ in open_positions}
            filtered = []
            for entry in raw_new_entries:
                sym = entry[1]
                if sym not in held_syms:
                    filtered.append(entry)
                else:
                    holder = next((s for s, ps, _ in open_positions if ps == sym), "another strategy")
                    rejected_entries.append((entry[0], entry[1], f"Already held via {holder}"))
            raw_new_entries = filtered

            if not raw_new_entries:
                pass
            else:
                max_slots = int(free_cash // MIN_CHUNK_SIZE)
                selected = raw_new_entries[:max_slots]
                rejected = raw_new_entries[max_slots:]

                chunk_size = free_cash / len(selected)

                for strat, sym, e in selected:
                    new_qty = max(1, int(chunk_size / e["entry_price"]))
                    e["qty"] = new_qty
                    e["capital"] = new_qty * e["entry_price"]
                    approved_entries.append((strat, sym, e))
                    # Persist to ledger so tomorrow's exits use correct qty
                    ledger[f"{strat}/{sym}"] = {
                        "qty": new_qty,
                        "entry_price": e["entry_price"],
                    }

                for strat, sym, e in rejected:
                    rejected_entries.append((strat, sym, "Ranked out (Max slots reached)"))

    # ── Build Unified Digest (HTML) ──────────────────────────────────
    today = datetime.now().strftime("%d %b %Y")
    used_pct = int((total_locked / TOTAL_ACCOUNT_CAPITAL) * 100)
    bar_filled = used_pct // 10
    capital_bar = "█" * bar_filled + "░" * (10 - bar_filled)

    lines = []

    # Header
    lines.append(f"<b>👑 MASTER ENGINE</b>  ·  {today}")
    lines.append("─" * 32)

    # Capital row
    lines.append(
        f"💰 <b>₹{free_cash:,.0f}</b> free  "
        f"🔒 ₹{total_locked:,.0f} locked\n"
        f"<code>[{capital_bar}] {used_pct}% deployed</code>"
    )

    # Fetch errors
    if fetch_errors:
        lines.append("\n⚠️ <b>DATA ERRORS — entries blocked</b>")
        for strat, label, msg in fetch_errors:
            lines.append(f"  [{strat}] {label}: {msg}")

    # Approved entries
    if approved_entries:
        lines.append("\n🟢 <b>NEW ENTRIES</b>")
        for strat, sym, e in approved_entries:
            lines.append(f"  <b>{sym}</b>  <i>{strat}</i>")
            lines.append(f"  ├ BUY {e['qty']} shares @ ₹{e['entry_price']:,}")
            lines.append(f"  ├ Allocated  ₹{e['capital']:,.0f}")
            if "stop_loss" in e:
                lines.append(f"  └ Stop loss  ₹{e['stop_loss']:,}")

    # Rejected entries
    if rejected_entries:
        lines.append("\n🟡 <b>REJECTED</b>")
        for strat, sym, reason in rejected_entries:
            lines.append(f"  {sym} [{strat}] — {reason}")

    # Exits
    if new_exits:
        lines.append("\n🔴 <b>EXITS TODAY</b>")
        for strat, sym, t in new_exits:
            pnl = t["pnl"]
            emoji = "✅" if pnl >= 0 else "❌"
            sign = "+" if pnl >= 0 else ""
            entry_d = t.get("entry_date", "?")
            lines.append(f"  {emoji} <b>{sym}</b>  <i>{strat}</i>")
            lines.append(f"  ├ Entered {entry_d}  ·  {t['qty']} shares")
            lines.append(f"  ├ ₹{t['entry_price']:,} → ₹{t['exit_price']:,}  ({t['reason']})")
            lines.append(f"  └ P&L  <b>{sign}₹{pnl:,.0f}</b>")

    # Open positions
    if open_positions:
        lines.append("\n📂 <b>OPEN POSITIONS</b>")
        for strat, sym, st in open_positions:
            if strat == "Black Swan":
                upnl = st["unrealized_pnl"]
                sign = "+" if upnl >= 0 else ""
                entry_p = st["entry_price"]
                pct = (upnl / (entry_p * st["entry_qty"])) * 100 if entry_p and st["entry_qty"] else 0
                lines.append(f"  <b>{sym}</b>  <i>{strat}</i>")
                lines.append(f"  ├ {st['entry_qty']} shares @ ₹{entry_p:,}  since {st['entry_date']}")
                lines.append(f"  └ Unrealised  <b>{sign}₹{upnl:,.0f}</b> ({pct:+.1f}%)")
            else:
                upnl = st["unrealized_pnl"]
                sign = "+" if upnl >= 0 else ""
                entry_p = st["entry_price"]
                pct = (upnl / (entry_p * st["qty"])) * 100 if entry_p and st["qty"] else 0
                lines.append(f"  <b>{sym}</b>  <i>{strat}</i>")
                lines.append(f"  ├ {st['qty']} shares @ ₹{entry_p:,}  since {st['entry_date']}")
                if st.get("stop_price"):
                    target_str = f"  Target ₹{st['target_price']:,}" if st.get("target_price") else ""
                    lines.append(f"  ├ Stop ₹{st['stop_price']:,}{target_str}")
                lines.append(f"  ├ Unrealised  <b>{sign}₹{upnl:,.0f}</b> ({pct:+.1f}%)")
                lines.append(f"  └ Day {st.get('days_held', 0)} of {st.get('days_held', 0) + st.get('days_left', 0)}")

    # Almost signals
    if almost_signals:
        lines.append("\n👀 <b>WATCH LIST</b>")
        for strat, label, reason in almost_signals:
            safe_reason = reason.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"  <b>{label}</b>  <i>{strat}</i>")
            lines.append(f"  └ {safe_reason}")

    if not fetch_errors and not approved_entries and not new_exits and not open_positions:
        lines.append("\n💤 No positions. No signals today.")

    # P&L summary
    total = total_realized + total_unrealized
    r_sign = "+" if total_realized >= 0 else ""
    u_sign = "+" if total_unrealized >= 0 else ""
    t_sign = "+" if total >= 0 else ""
    lines.append("\n────────────────────────────────")
    lines.append("<b>P&amp;L  (since inception)</b>")
    lines.append(
        f"<code>"
        f"Realised    {r_sign}{total_realized:>12,.0f}\n"
        f"Unrealised  {u_sign}{total_unrealized:>12,.0f}\n"
        f"------------------------------\n"
        f"Total       {t_sign}{total:>12,.0f}"
        f"</code>"
    )

    # Persist updated ledger (entries added, exits removed)
    save_ledger(ledger)

    digest = "\n".join(lines)
    _log.info("Sending Master Digest:\n" + digest)
    notifier.send(digest, parse_mode="HTML")


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""))
    args = parser.parse_args()
    run_master_trader(args.bot_token, args.chat_id)
