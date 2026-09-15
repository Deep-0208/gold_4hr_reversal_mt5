"""
═════════════════════════════════════════════════════════════
  TELEGRAM ALERTS — Non-blocking, fail-safe notification module
═════════════════════════════════════════════════════════════

Sends real-time Telegram alerts for critical bot events.

Design principles:
  - NON-BLOCKING: every send runs in a background daemon thread.
  - FAIL-SAFE:    any error is logged silently; the bot NEVER crashes
                  due to a Telegram failure.

Setup (.env):
  TELEGRAM_BOT_TOKEN = 123456:ABC-your-token-here
  TELEGRAM_CHAT_ID   = -1001234567890   (group) or 123456789 (user)
"""

import os
import queue
import threading
import requests
import logging
from datetime import datetime, timezone, timedelta

# ── IST timezone helper ──────────────────────────────────────
_IST = timezone(timedelta(hours=5, minutes=30), "IST")

def _now_ist_str() -> str:
    """Return the current time in IST formatted as a string."""
    return datetime.now(_IST).strftime("%d %b %Y, %H:%M:%S IST")


# ── Module logger ─────────────────────────────────────────────
_log = logging.getLogger("Gold4HrReversal.telegram")


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


class TelegramAlerter:
    """
    Thread-safe, fail-safe Telegram message sender.

    All public methods fire-and-forget to a bounded queue processed by
    a dedicated background daemon worker thread.
    If BOT_TOKEN or CHAT_ID are not set, all calls are silent no-ops.
    """

    def __init__(self, token: str = "", chat_id: str = ""):
        self._token   = token.strip()
        self._chat_id = chat_id.strip()
        self._enabled = bool(self._token and self._chat_id)

        # Bounded queue + persistent worker thread
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._worker = threading.Thread(
            target=self._worker_loop,
            daemon=True,
            name="TelegramAlertWorker"
        )

        if self._enabled:
            self._worker.start()
            _log.info("[STARTUP] 📱 Telegram notifications active (Chat ID: %s)", self._chat_id)
        else:
            _log.info("[STARTUP] 📱 Telegram notifications DISABLED (no token/chat_id in .env)")

    def _worker_loop(self) -> None:
        """Background loop that processes the message queue."""
        while True:
            msg = self._queue.get()
            if msg is None:
                break
            self._send_sync(msg)

    def _send_sync(self, text: str) -> None:
        """Send a message synchronously (called from worker thread)."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"

        # Try MarkdownV2 first, fall back to plain text on failure
        for parse_mode in ("MarkdownV2", None):
            try:
                resp = requests.post(url, json={
                    "chat_id": self._chat_id,
                    "text": text if parse_mode == "MarkdownV2" else text.replace("\\", ""),
                    "parse_mode": parse_mode,
                }, timeout=10)

                if resp.status_code == 200:
                    return
                if resp.status_code == 400 and parse_mode == "MarkdownV2":
                    continue  # retry without markdown
                _log.warning("[TELEGRAM] ⚠️ HTTP %d: %s", resp.status_code, resp.text[:200])
                return
            except Exception as e:
                _log.warning("[TELEGRAM] ⚠️ Send failed: %s", e)
                return

    def _enqueue(self, text: str) -> None:
        """Add a message to the queue (non-blocking, drops if full)."""
        if not self._enabled:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            _log.warning("[TELEGRAM] ⚠️ Message queue full, dropping message.")

    # ─────────────────────────────────────────
    # PUBLIC ALERT METHODS
    # ─────────────────────────────────────────

    def bot_started(self, lot_size: float = 0.0) -> None:
        """Alert: bot startup notification with active configuration parameters."""
        ts = _escape_md(_now_ist_str())
        # Use explicitly passed lot_size parameter, or fall back to LOT_SIZE env / default (0.01)
        display_lot = lot_size if lot_size > 0 else float(os.getenv("LOT_SIZE", "0.01"))
        msg = (
            f"🤖 *Gold 4HR Reversal Bot Started*\n\n"
            f"⏰ {ts}\n"
            f"📊 Symbol: {_escape_md('XAU/USD')}\n"
            f"📈 Timeframe: H4\n"
            f"💰 Lot Size: {_escape_md(str(display_lot))}\n"
            f"🔄 Mode: {_escape_md(_get_mode())}"
        )
        self._enqueue(msg)

    def trade_entry(
        self,
        direction: str,
        entry_price: float,
        sl: float,
        tp: float,
        risk: float,
        lot_size: float,
    ) -> None:
        """Alert: trade entry notification with direction, prices, risk, and volume."""
        ts = _escape_md(_now_ist_str())
        emoji = "📈" if direction == "LONG" else "📉"
        msg = (
            f"{emoji} *GOLD {_escape_md(direction)} ENTRY*\n\n"
            f"⏰ {ts}\n"
            f"💵 Entry: ${_escape_md(f'{entry_price:.2f}')}\n"
            f"🛑 Stop Loss: ${_escape_md(f'{sl:.2f}')}\n"
            f"🎯 Target: ${_escape_md(f'{tp:.2f}')}\n"
            f"⚠️ Risk: ${_escape_md(f'{risk:.2f}')}\n"
            f"📦 Lots: {_escape_md(f'{lot_size:.2f}')}\n"
            f"🔄 Mode: {_escape_md(_get_mode())}"
        )
        self._enqueue(msg)

    def trade_exit(
        self,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        """Alert: trade exit notification with P&L, exit reason, and realized prices."""
        ts = _escape_md(_now_ist_str())
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} *GOLD {_escape_md(direction)} EXIT \\({_escape_md(reason)}\\)*\n\n"
            f"⏰ {ts}\n"
            f"💵 Entry: ${_escape_md(f'{entry_price:.2f}')}\n"
            f"💵 Exit: ${_escape_md(f'{exit_price:.2f}')}\n"
            f"💰 P&L: ${_escape_md(f'{pnl:+,.2f}')}\n"
            f"🔄 Mode: {_escape_md(_get_mode())}"
        )
        self._enqueue(msg)

    def tsl_update(
        self,
        direction: str,
        old_sl: float,
        new_sl: float,
        r_multiple: float,
    ) -> None:
        """Alert: Trailing Stop Loss adjustment notification with new SL and R-multiple."""
        ts = _escape_md(_now_ist_str())
        msg = (
            f"📈 *TSL UPDATE*\n\n"
            f"⏰ {ts}\n"
            f"📊 {_escape_md(direction)} | R\\-multiple: {_escape_md(f'{r_multiple:.2f}')}R\n"
            f"🛑 SL: ${_escape_md(f'{old_sl:.2f}')} → ${_escape_md(f'{new_sl:.2f}')}"
        )
        self._enqueue(msg)

    def bot_crashed(self, error: str) -> None:
        """Alert: emergency notification when the trading engine encounters a fatal crash."""
        ts = _escape_md(_now_ist_str())
        safe_err = _escape_md(str(error)[:500])
        msg = (
            f"🚨 *GOLD BOT CRASHED*\n\n"
            f"⏰ {ts}\n"
            f"❌ Error: {safe_err}"
        )
        self._enqueue(msg)

    def daily_summary(
        self,
        trades: int,
        pnl: float,
        balance: float,
    ) -> None:
        """Alert: end-of-day summary notification with daily trade count, P&L, and balance."""
        ts = _escape_md(_now_ist_str())
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"📊 *Daily Summary*\n\n"
            f"⏰ {ts}\n"
            f"📈 Trades: {trades}\n"
            f"{emoji} Day P&L: ${_escape_md(f'{pnl:+,.2f}')}\n"
            f"💰 Balance: ${_escape_md(f'{balance:,.2f}')}"
        )
        self._enqueue(msg)


# ─────────────────────────────────────────────
# MODULE-LEVEL HELPERS
# ─────────────────────────────────────────────
# These are defined at the bottom to avoid circular imports
# from config.py (which would trigger logging setup).

def _get_mode() -> str:
    """Get trading mode from environment or config."""
    return os.getenv("TRADING_MODE", "PAPER").upper()

# ── Singleton instance ─────────────────────────────────────────
tg = TelegramAlerter(
    token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
    chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
)
