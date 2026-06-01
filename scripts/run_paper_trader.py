"""Stateless Daily Paper Trader with Telegram Integration.

This script loads the optimal Long-Only portfolio, fetches the latest 
historical data up to the current day, and feeds it into the strategy. 
If a trade entry or exit is triggered on the final (current) day, it 
sends an alert directly to your Telegram.

Usage:
  python scripts/run_paper_trader.py --bot-token YOUR_TOKEN --chat-id YOUR_CHAT_ID
"""

import argparse
import json
import logging
import os
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from trading_engine.notifications.telegram import TelegramNotifier
from trading_engine.strategies.long_only_swan import LongOnlySwanConfig, LongOnlySwanStrategy
from trading_engine.strategy.signals import Bar

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_log = logging.getLogger(__name__)

DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"

def load_portfolio() -> list[dict]:
    path = REPORTS_DIR / "optimal_long_only_portfolio.json"
    if not path.exists():
        _log.error("Optimal portfolio not found. Run sweeper first.")
        return []
    with open(path, "r") as f:
        return json.load(f)

def fetch_yfinance_data(symbol: str) -> pd.DataFrame | None:
    # Append .NS to the symbol for National Stock Exchange of India
    yf_ticker = f"{symbol}.NS"
    try:
        # Fetch last 200 trading days
        df = yf.download(yf_ticker, period="1y", interval="1d", progress=False)
        if df.empty:
            return None
        
        # yfinance returns multi-index columns if downloading multiple, but single ticker is simple.
        # We need to map 'Open', 'High', 'Low', 'Close', 'Volume' to lowercase
        # Sometimes yf returns columns as ('Close', 'BAJAJFINSV.NS'), so we flatten it
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        df = df.rename(columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume"
        })
        # yf timestamp is timezone aware or naive depending on the pull. Make sure we can iterate over it.
        # Index is already 'Date' or 'Datetime'
        df.index.name = "timestamp"
        # Forward fill any missing values just in case
        df = df.ffill().dropna()
        return df
    except Exception as e:
        _log.error(f"Failed to fetch yfinance data for {symbol}: {e}")
        return None

def run_paper_trader(bot_token: str, chat_id: str):
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    
    if bot_token and chat_id:
        notifier.send("🤖 Long-Only Black Swan Paper Trader Online! Scanning portfolio for daily signals...")
        _log.info("Telegram notifier connected and started.")
    else:
        _log.warning("No Telegram credentials provided. Running in dry-run mode.")

    portfolio = load_portfolio()
    if not portfolio:
        return

    _log.info(f"Scanning {len(portfolio)} pairs for trade signals...")
    
    signals_found = 0

    for pair in portfolio:
        sym_a = pair["symbol_a"]
        sym_b = pair["symbol_b"]
        qty_a = pair["qty_a"]
        qty_b = pair["qty_b"]
        params = pair["optimal_params"]
        
        df_a = fetch_yfinance_data(sym_a)
        df_b = fetch_yfinance_data(sym_b)
        
        if df_a is None or df_b is None:
            _log.warning(f"Could not fetch data for {sym_a} or {sym_b}. Skipping.")
            continue
            
        # Align data
        common_idx = df_a.index.intersection(df_b.index)
        df_a = df_a.loc[common_idx]
        df_b = df_b.loc[common_idx]
        
        if len(common_idx) < params["window_size"]:
            continue
            
        cfg = LongOnlySwanConfig(
            strategy_id=f"paper_{sym_a}_{sym_b}",
            symbol_a=sym_a,
            symbol_b=sym_b,
            quantity_a=qty_a,
            quantity_b=qty_b,
            window_size=params["window_size"],
            entry_z_score=params["entry_z_score"],
            stop_loss_z_score=params["stop_loss_z_score"],
        )
        strategy = LongOnlySwanStrategy(config=cfg)
        
        # We manually feed all bars to reconstruct state
        last_intents = []
        
        for ts in common_idx:
            row_a = df_a.loc[ts]
            row_b = df_b.loc[ts]
            
            bar_a = Bar(
                symbol=sym_a, exchange="NSE", timestamp=ts,
                open=Decimal(row_a["open"]), high=Decimal(row_a["high"]),
                low=Decimal(row_a["low"]), close=Decimal(row_a["close"]),
                volume=int(row_a["volume"])
            )
            bar_b = Bar(
                symbol=sym_b, exchange="NSE", timestamp=ts,
                open=Decimal(row_b["open"]), high=Decimal(row_b["high"]),
                low=Decimal(row_b["low"]), close=Decimal(row_b["close"]),
                volume=int(row_b["volume"])
            )
            
            # Feed to strategy
            # Strategy checks timestamp matching internally, so we feed one, then the other
            strategy.on_bar(bar_a, None)
            intents = strategy.on_bar(bar_b, None)
            
            # Keep track of the intents generated on the very last bar
            if ts == common_idx[-1]:
                last_intents = intents
                
        # Check if the final day generated a signal
        if last_intents:
            signals_found += len(last_intents)
            for intent in last_intents:
                msg = (
                    f"🚨 **TRADE ALERT: {sym_a} / {sym_b}** 🚨\n\n"
                    f"Action: {intent.side}\n"
                    f"Symbol: {intent.symbol}\n"
                    f"Quantity: {intent.quantity} shares\n"
                    f"Reason: {intent.reason}"
                )
                _log.info(f"SIGNAL TRIGGERED: {msg}")
                notifier.send(msg)

    if signals_found == 0:
        msg = "✅ Scan complete. No new trade signals triggered today."
        _log.info(msg)
        notifier.send(msg)
    else:
        _log.info(f"Scan complete. {signals_found} signals triggered.")

if __name__ == "__main__":
    load_dotenv()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""), help="Telegram Bot Token")
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID", ""), help="Telegram Chat ID")
    args = parser.parse_args()
    
    run_paper_trader(args.bot_token, args.chat_id)
