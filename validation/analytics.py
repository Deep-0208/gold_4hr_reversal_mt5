"""
═════════════════════════════════════════════════════════════
  ANALYTICS — Central Analytics Coordinator
═════════════════════════════════════════════════════════════

Fail-safe orchestrator that delegates to individual trackers.
Every tracker call is wrapped in try/except to ensure
analytics never crash the trading engine.
"""

import logging
from typing import Dict, Any, Optional

from config import LOGGER_NAME

_log = logging.getLogger(f"{LOGGER_NAME}.analytics")


class AnalyticsEngine:
    """
    Central coordinator for the validation subsystem.

    Wraps all tracker calls in error isolation so that
    analytics failures never impact the live trading loop.
    """

    def __init__(self):
        self._enabled = True
        _log.info("[ANALYTICS] ✅ Analytics engine initialized.")

    def _safe_call(self, func_name: str, func, *args, **kwargs):
        """Wrap a tracker call in fail-safe error isolation."""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            _log.warning("[ANALYTICS] ⚠️ %s failed (non-fatal): %s", func_name, e)
            return None

    def log_candle_evaluation(self, candle: Dict[str, Any], signal: Optional[Any] = None) -> None:
        """Log a candle evaluation (every completed H4 bar)."""
        self._safe_call(
            "log_candle_evaluation",
            self._do_log_candle,
            candle, signal
        )

    def _do_log_candle(self, candle: Dict[str, Any], signal: Optional[Any]) -> None:
        signal_str = f"SIGNAL={signal.direction}" if signal else "NO_SIGNAL"
        _log.debug(
            "[ANALYTICS] Candle evaluated: %s | O=$%.2f C=$%.2f | %s",
            candle.get("date"), candle.get("open", 0), candle.get("close", 0),
            signal_str
        )

    def log_trade_entry(self, trade_data: Dict[str, Any]) -> None:
        """Log a trade entry event."""
        self._safe_call("log_trade_entry", self._do_log_entry, trade_data)

    def _do_log_entry(self, data: Dict[str, Any]) -> None:
        _log.info(
            "[ANALYTICS] Trade ENTRY: %s @ $%.2f | SL=$%.2f | TP=$%.2f",
            data.get("direction"), data.get("entry_price", 0),
            data.get("sl_price", 0), data.get("target_price", 0)
        )

    def log_trade_exit(self, trade_data: Dict[str, Any]) -> None:
        """Log a trade exit event."""
        self._safe_call("log_trade_exit", self._do_log_exit, trade_data)

    def _do_log_exit(self, data: Dict[str, Any]) -> None:
        _log.info(
            "[ANALYTICS] Trade EXIT (%s): %s | P&L=$%.2f",
            data.get("exit_reason"), data.get("direction"),
            data.get("pnl", 0)
        )

    def log_decision(self, decision: str, reason: str, context: Optional[Dict] = None) -> None:
        """Log an algorithmic state transition or decision."""
        self._safe_call("log_decision", self._do_log_decision, decision, reason, context)

    def _do_log_decision(self, decision: str, reason: str, context: Optional[Dict]) -> None:
        _log.debug("[ANALYTICS] Decision: %s | Reason: %s | Context: %s", decision, reason, context)
