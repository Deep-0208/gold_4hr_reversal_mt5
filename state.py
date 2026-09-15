"""
═════════════════════════════════════════════════════════════
  STATE — Persistence & Trade Journal
═════════════════════════════════════════════════════════════

Persists strategy state to a single state_active.json file.
Positions carry across day boundaries (overnight, weekend).
Daily counters (trades_today, realized_pnl_today) reset when
the date changes.

Every trade is appended to a CSV journal for post-session analysis.
"""

import json
import csv
import os
import time
import tempfile
import threading
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

from config import STATE_DIR, LOG_DIR, CONFIG, today_ist, log


# ─────────────────────────────────────────────
# THREAD MUTEX
# ─────────────────────────────────────────────
_state_lock = threading.RLock()


def _coerce_numeric_state(state: Dict[str, Any]) -> None:
    """Ensure numeric values are native Python float/int types to prevent JSON type drift."""
    for key in ("cash", "realized_pnl_today", "total_realized_pnl"):
        if key in state and state[key] is not None:
            try:
                state[key] = float(state[key])
            except (ValueError, TypeError):
                pass
    if "trades_today" in state and state["trades_today"] is not None:
        try:
            state["trades_today"] = int(state["trades_today"])
        except (ValueError, TypeError):
            pass

    pos = state.get("current_position")
    if isinstance(pos, dict):
        for key in ("entry_price", "sl_price", "target_price", "risk",
                     "highest_price", "lowest_price", "lot_size"):
            if key in pos and pos[key] is not None:
                try:
                    pos[key] = float(pos[key])
                except (ValueError, TypeError):
                    pass
        if "ticket" in pos and pos["ticket"] is not None:
            try:
                pos["ticket"] = int(pos["ticket"])
            except (ValueError, TypeError):
                pass


# ─────────────────────────────────────────────
# TYPED STATE DEFINITIONS
# ─────────────────────────────────────────────

class PositionState(TypedDict, total=False):
    """Schema for a live position within the strategy state."""
    direction: str             # "LONG" / "SHORT"
    ticket: int                # MT5 position ticket
    lot_size: float
    entry_price: float         # Gold price at entry
    sl_price: float
    target_price: float
    risk: float                # absolute dollar risk distance
    entry_time: str            # ISO timestamp
    entry_date: str            # ISO date
    tsl_active: bool
    highest_price: float       # highest price seen since entry (for LONG TSL)
    lowest_price: float        # lowest price seen since entry (for SHORT TSL)
    last_heartbeat: str        # ISO timestamp, updated every 60s while in-position


class StrategyState(TypedDict, total=False):
    """Top-level strategy state persisted to disk."""
    trades_today: int
    in_position: bool
    current_position: Optional[PositionState]
    date: str
    cash: float
    realized_pnl_today: float
    total_realized_pnl: float


# ─────────────────────────────────────────────
# STATE PERSISTENCE
# ─────────────────────────────────────────────

def _state_file_path():
    """Path to the single active state file."""
    return STATE_DIR / "state_active.json"


def fresh_state() -> StrategyState:
    """Return a blank state dict for a new day."""
    return StrategyState(
        trades_today=0,
        in_position=False,
        current_position=None,
        date=today_ist().isoformat(),
        cash=CONFIG["starting_capital"],
        realized_pnl_today=0.0,
        total_realized_pnl=0.0,
    )


def load_state() -> StrategyState:
    """
    Load persisted state from disk, or return fresh state.

    Uses a single state_active.json file. When the date changes,
    daily counters (trades_today, realized_pnl_today) reset but
    position state (in_position, current_position, cash,
    total_realized_pnl) is preserved across days.

    Thread-safe under _state_lock.
    """
    with _state_lock:
        state_file = _state_file_path()
        _cleanup_old_state_files()

        state = None
        if state_file.exists():
            for read_attempt in range(3):
                try:
                    content = state_file.read_text(encoding="utf-8")
                    state = json.loads(content)
                    break
                except PermissionError:
                    time.sleep(0.05)
                except Exception as e:
                    if read_attempt == 2:
                        log.critical(
                            f"[CRASH GUARD] 🛑 Critical State Corrupted: Existing state file {state_file} cannot be read ({e}). "
                            f"Halting trading engine immediately to protect capital from unmanaged positions!"
                        )
                        raise RuntimeError(
                            f"Existing state file {state_file} is corrupt or unreadable: {e}"
                        )
                    time.sleep(0.05)

        if state is not None:
            # Merge with fresh defaults to fill any missing keys
            merged = fresh_state()
            merged.update(state)
            state = merged

            # Coerce numerics to prevent JSON type drift
            _coerce_numeric_state(state)

            # Check if date has changed → reset daily counters
            if state.get("date") != today_ist().isoformat():
                log.info(
                    f"[STATE RECOVERY] 📂 Trading Date Rollover Detected: Previous date was {state.get('date')}, "
                    f"new date is {today_ist().isoformat()}. Resetting daily counters (trades_today=0, realized_pnl_today=$0.00). "
                    f"Active position and account balance preserved."
                )
                state["trades_today"] = 0
                state["realized_pnl_today"] = 0.0
                state["date"] = today_ist().isoformat()
                try:
                    save_state(state)
                except Exception as se:
                    log.error(f"[STATE RECOVERY] ❌ Failed to persist state on date rollover: {se}")
            else:
                pos_info = (
                    f"Active position: {state['current_position']['direction']} ticket #{state['current_position'].get('ticket', 'N/A')}"
                    if state.get("in_position") and state.get("current_position")
                    else "No active position"
                )
                log.info(
                    f"[STATE RECOVERY] 📂 Existing State Restored: "
                    f"Trades today: {state['trades_today']} | Status: {pos_info} | "
                    f"Cash balance: ${state.get('cash', 0):,.2f} | Total realized P&L: ${state.get('total_realized_pnl', 0):+,.2f}"
                )
            return state

        log.info(
            f"[STATE RECOVERY] 📂 Fresh State Initialized: No prior state file found. "
            f"Starting capital: ${CONFIG['starting_capital']:,.2f}."
        )
        return fresh_state()


def _cleanup_old_state_files() -> None:
    """Sweep orphaned temp files older than 10 minutes."""
    now_ts = time.time()
    for f in STATE_DIR.glob("state_*.tmp"):
        try:
            if (now_ts - f.stat().st_mtime) > 600:
                f.unlink()
                log.debug("[CLEANUP] 🗑️ Cleaned up orphaned temporary state file: %s", f.name)
        except OSError:
            pass


def save_state(state: StrategyState) -> None:
    """
    Persist state to disk via atomic write (write to temp → rename).
    Prevents corruption if the process crashes mid-write.
    Thread-safe under _state_lock.
    """
    with _state_lock:
        _coerce_numeric_state(state)
        state_file = _state_file_path()
        data = json.dumps(state, indent=2, default=str)

        # Write to a temp file in the same directory, then atomically rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(STATE_DIR), suffix=".tmp", prefix="state_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            # Retry loop for Windows sharing violation
            for attempt in range(3):
                try:
                    os.replace(tmp_path, str(state_file))
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ─────────────────────────────────────────────
# TRADE JOURNAL (CSV)
# ─────────────────────────────────────────────

JOURNAL_FIELDS = [
    "date",
    "symbol",
    "direction",          # LONG / SHORT
    "lot_size",
    "ticket",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "sl_price",
    "target_price",
    "risk",
    "gross_pnl",
    "net_pnl",
    "pnl_percent",
    "exit_reason",
    "mode",               # PAPER / LIVE
]


def append_trade_journal(row: Dict[str, Any]) -> None:
    """Append one trade record to the main CSV journal. Thread-safe under _state_lock."""
    with _state_lock:
        journal_file = LOG_DIR / "journal.csv"
        new_file = not journal_file.exists() or journal_file.stat().st_size == 0

        # Guard against duplicate journal entry for the same ticket
        ticket = row.get("ticket")
        if not new_file and ticket:
            try:
                with journal_file.open("r", encoding="utf-8") as rf:
                    reader = csv.DictReader(rf)
                    if any(str(r.get("ticket")) == str(ticket) for r in reader):
                        log.warning(
                            f"[ORDER EXEC] ⚠️ Ticket {ticket} is already recorded in {journal_file.name}. "
                            "Skipping duplicate journal append."
                        )
                        return
            except Exception as e:
                log.warning(f"[ORDER EXEC] ⚠️ Could not verify journal duplicates: {e}")

        with journal_file.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=JOURNAL_FIELDS,
                extrasaction="ignore",
            )
            if new_file:
                writer.writeheader()
            writer.writerow(row)

        log.info(f"[ORDER EXEC] 📝 Trade record successfully saved to persistent journal: {journal_file.name}")
