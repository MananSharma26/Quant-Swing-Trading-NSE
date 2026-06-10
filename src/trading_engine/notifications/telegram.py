"""Telegram notification helper for paper/live trading sessions.

Sends messages to a Telegram chat via the Bot API using plain HTTPS.
No Telegram SDK required — only the standard library + urllib.

Usage:
    notifier = TelegramNotifier(bot_token="...", chat_id="...")
    notifier.send("Hello from trading engine!")

If the token or chat_id is missing, all sends are silently no-ops so the
runner never crashes due to notification failures.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from trading_engine.domain.enums import Side
from trading_engine.domain.models import TradeFill

_log = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """Sends Telegram messages via the Bot HTTP API.

    Args:
        bot_token:  Telegram bot token from @BotFather.
        chat_id:    Your Telegram chat/user ID.
        timeout:    HTTP request timeout in seconds.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: int = 5,
    ) -> None:
        self._token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._timeout = timeout
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            _log.warning("TelegramNotifier: missing token or chat_id — notifications disabled.")

    # ------------------------------------------------------------------
    # Public helpers — called by the runner
    # ------------------------------------------------------------------

    def send(self, text: str, parse_mode: str | None = None) -> None:
        """Send a message. Silently swallows errors.

        Args:
            text:       Message text. Use HTML tags if parse_mode="HTML".
            parse_mode: "HTML", "MarkdownV2", or None for plain text.
        """
        if not self._enabled:
            return
        url = _TELEGRAM_API.format(token=self._token)
        body: dict = {"chat_id": self._chat_id, "text": text}
        if parse_mode:
            body["parse_mode"] = parse_mode
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout):
                pass
        except (urllib.error.URLError, OSError) as exc:
            _log.warning("Telegram send failed: %s", exc)

    def notify_fill(self, fill: TradeFill) -> None:
        """Format and send a fill notification."""
        side_label = "BUY" if fill.side == Side.BUY else "SELL"
        value = fill.quantity * fill.price
        msg = (
            f"{'📈' if fill.side == Side.BUY else '📉'} {side_label} {fill.symbol}\n"
            f"  Qty   : {fill.quantity}\n"
            f"  Price : ₹{fill.price:,.2f}\n"
            f"  Value : ₹{value:,.0f}\n"
            f"  Fees  : ₹{fill.fees:,.2f}\n"
            f"  Time  : {fill.timestamp.strftime('%H:%M:%S')}"
        )
        self.send(msg)

    def notify_session_start(self, strategy_id: str, symbols: list[str], cash: Decimal) -> None:
        """Send a session-start message."""
        self.send(
            f"🚀 Paper trading started\n"
            f"  Strategy : {strategy_id}\n"
            f"  Symbols  : {', '.join(symbols)}\n"
            f"  Capital  : ₹{cash:,.0f}"
        )

    def notify_session_end(
        self,
        strategy_id: str,
        total_fills: int,
        final_equity: Decimal,
        initial_cash: Decimal,
    ) -> None:
        """Send a session-end summary."""
        pnl = final_equity - initial_cash
        sign = "+" if pnl >= 0 else ""
        self.send(
            f"🏁 Session ended — {strategy_id}\n"
            f"  Fills    : {total_fills}\n"
            f"  Final    : ₹{final_equity:,.0f}\n"
            f"  Day P&L  : {sign}₹{pnl:,.0f}"
        )

    def notify_error(self, context: str, error: str) -> None:
        """Send an error alert."""
        self.send(f"⚠️ ERROR [{context}]\n{error}")
