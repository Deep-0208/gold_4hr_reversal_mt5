"""
═════════════════════════════════════════════════════════════
  CONFIGURATION — GOLD (XAU/USD) 4-Hour Reversal Strategy
                  on MetaTrader 5 (Positional)
═════════════════════════════════════════════════════════════

Single source of truth for ALL strategy parameters.
Nothing strategy-specific is hardcoded outside this module.

Gold trades 24/5 on a continuous market (Sunday 22:00 UTC to
Friday 22:00 UTC) using standard 4-hour (H4) candle bars.
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, date, time as dtime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

# Enable Windows ANSI VT100 processing for rich console logging
if os.name == "nt":
    try:
        os.system("")
    except Exception:
        pass

# ─────────────────────────────────────────────
# PATH SETUP
# ─────────────────────────────────────────────
STRATEGY_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = STRATEGY_DIR.parent
DOTENV_PATH = STRATEGY_DIR / ".env"
load_dotenv(DOTENV_PATH)

# Sub-directories for this strategy's own logs and state
LOG_DIR = STRATEGY_DIR / "logs"
STATE_DIR = STRATEGY_DIR / "state"
LOG_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# TIMEZONES
# ─────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30), "IST")
UTC = timezone.utc


def now_ist() -> datetime:
    """Current datetime in IST."""
    return datetime.now(IST)


def now_utc() -> datetime:
    """Current datetime in UTC."""
    return datetime.now(UTC)


def today_ist() -> date:
    """Current date in IST."""
    return now_ist().date()


# ─────────────────────────────────────────────
# STRATEGY CONFIGURATION
# ─────────────────────────────────────────────
CONFIG = {
    # ── Instrument ───────────────────────────────
    "symbol": "XAUUSD",
    "symbol_description": "Gold Spot / US Dollar",

    # ── Position sizing ──────────────────────────
    # Fixed lot size per trade.
    # 1.00 standard lot = 100 oz of gold ($100 per $1 move).
    # 0.01 micro lot = 1 oz of gold ($1 per $1 move).
    "lot_size": 0.01,

    # ── Candle / timeframe ───────────────────────
    # Standard H4 (4-hour) candles from MT5.
    # 6 candles per day: 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC.
    "candle_tf_label": "H4",
    "candle_tf_minutes": 240,

    # ── Risk management ──────────────────────────
    "max_trades_per_day": 5,
    "rr_ratio": 2.0,                 # base 2:1 Risk:Reward (used if TSL disabled)
    "use_tsl": True,                 # Enable Trailing Stop Loss
    "tsl_activation_rr": 1.0,        # R:R at which TSL activates (1.0R)
    "tsl_floor_rr": 1.0,             # Hard floor R:R locked upon activation (+1.0R guaranteed profit)
    "tsl_trail_rr_fraction": 0.2,    # Trailing distance = 0.20 × Risk
    "max_target_rr": 4.0,            # Hard exit R:R ceiling (4.0R)

    # ── SL placement ────────────────────────────
    # Buffer in dollars added beyond the 3-candle extreme for SL placement.
    # Protects against exact-low/high wicks triggering premature exits.
    "sl_buffer_dollars": 0.50,

    # ── Daily drawdown kill-switch ───────────────
    # 0 = disabled. Set to a positive $ value (e.g. 500) to halt
    # trading for the day once realized losses exceed this amount.
    "max_daily_loss": 0,

    # ── Spread guard ─────────────────────────────
    # Reject new entries if the current spread exceeds this threshold.
    # During daily rollover (~00:00 server time), spreads can spike.
    "max_spread_dollars": 1.20,

    # ── Opposite Signal ──────────────────────────
    # If True, an existing position will be exited when an opposite
    # 3-candle reversal setup is detected, and a new position opened.
    "exit_on_opposite_signal": False,

    # ── Pattern detection ────────────────────────
    # Tolerance band for "C2.open inside C1.body" check.
    # Expressed as a fraction of C1's full range (high - low)
    # with a minimum dollar floor.
    "c2_open_tolerance_pct": 0.05,       # 5% of C1 range
    "c2_open_min_tolerance_pts": 1.0,    # Minimum $1.00 floor for Gold H4 bars

    # ── Product type ─────────────────────────────
    # Positional: carry trades overnight and through weekends.
    "product": "POSITIONAL",

    # ── Trading mode ─────────────────────────────
    # "PAPER" = simulated (no real orders, tracks virtual P&L)
    # "LIVE"  = real orders via MT5
    "trading_mode": os.getenv("TRADING_MODE", "PAPER").upper(),

    # ── Virtual starting capital (paper mode) ────
    "starting_capital": 100000.0,

    # ── Monitoring loop intervals ────────────────
    "candle_poll_interval_s": 30,          # seconds between candle-close checks
    "position_monitor_interval_s": 5,      # seconds between tick checks when in position

    # ── MT5 Magic Number ─────────────────────────
    # Unique identifier to tag all orders from this strategy.
    # Prevents conflicts with manual trades or other EAs on the same account.
    "magic_number": 444001,

    # ── Logging ──────────────────────────────────
    "log_level": "INFO",                   # INFO for production
    "log_max_bytes": 10 * 1024 * 1024,     # 10 MB per log file
    "log_backup_count": 10,                # keep 10 rotated copies
}


# ═══════════════════════════════════════════════
# LOGGING SETUP — Multi-channel strategy logging
# ═══════════════════════════════════════════════
#
# Log files produced daily inside logs/YYYY-MM-DD/:
#   strategy.log    — Main strategy flow (INFO+)
#   debug.log       — Full debug trace (DEBUG+)
#   trades.log      — Entries, exits, P&L only
#   candles.csv     — Every candle in CSV for analysis
#
# Trade journal is maintained across days:
#   logs/journal.csv

class ISTFormatter(logging.Formatter):
    """
    Log formatter that explicitly converts all timestamps to IST (UTC+05:30)
    and formats multiline log messages with aligned continuation indentation.
    """

    def __init__(self, fmt: Optional[str] = None, datefmt: Optional[str] = None, include_module: bool = False):
        super().__init__(fmt, datefmt=datefmt)
        self.include_module = include_module

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + " IST"

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        if "\n" in s:
            lines = s.split("\n")
            first_line = lines[0]
            msg_first_line = record.getMessage().split("\n")[0]
            sep_idx = first_line.find(" | " + msg_first_line)
            if sep_idx > 0:
                indent = " " * sep_idx + " | "
            else:
                indent = " " * 31 + " | "
            return first_line + "\n" + "\n".join(indent + line if line.strip() else "" for line in lines[1:])
        return s


class ConsoleColorFormatter(ISTFormatter):
    """Log formatter with clean ANSI colors for interactive terminal output."""

    COLORS = {
        logging.DEBUG: "\033[90m",       # Dim Gray
        logging.INFO: "\033[36m",        # Cyan
        logging.WARNING: "\033[93m",     # Bright Yellow
        logging.ERROR: "\033[91m",       # Bright Red
        logging.CRITICAL: "\033[1;91m",  # Bold Red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        if sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            color = self.COLORS.get(record.levelno, "")
            if color:
                return f"{color}{formatted}{self.RESET}"
        return formatted


def _make_formatter(include_module: bool = False, use_color: bool = False) -> logging.Formatter:
    """Build a log formatter with explicit IST timestamp and optional terminal colors."""
    if include_module:
        fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    else:
        fmt = "%(asctime)s | %(levelname)-5s | %(message)s"

    if use_color:
        return ConsoleColorFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S IST", include_module=include_module)
    return ISTFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S IST", include_module=include_module)


class AutoFlushRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that immediately flushes records to disk for real-time observability."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


class AutoFlushFileHandler(logging.FileHandler):
    """FileHandler that immediately flushes records to disk for real-time observability."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def _add_file_handler(
    logger: logging.Logger,
    filename: str,
    directory: Path,
    level: int = logging.DEBUG,
    include_module: bool = False,
) -> None:
    """Add an auto-flushing rotating file handler to a logger."""
    fh = AutoFlushRotatingFileHandler(
        directory / filename,
        maxBytes=CONFIG["log_max_bytes"],
        backupCount=CONFIG["log_backup_count"],
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(_make_formatter(include_module))
    logger.addHandler(fh)


# Logger name constant — single source of truth
LOGGER_NAME = "Gold4HrReversal"
_logging_lock = threading.RLock()


def _remove_file_handlers(logger: logging.Logger) -> None:
    """Close and remove all FileHandler / RotatingFileHandler instances from a logger."""
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            logger.removeHandler(h)


def rollover_logging(new_date_str: str) -> None:
    """
    Dynamically roll over file handlers to a new daily directory (logs/YYYY-MM-DD).
    Used during 24/7 continuous runs when crossing midnight without process restart.

    Entire handler swap is protected by _logging_lock to prevent race conditions
    if a background thread attempts to log during rollover.
    """
    with _logging_lock:
        clean_date_str = Path(str(new_date_str)).name
        daily_log_dir = LOG_DIR / clean_date_str
        daily_log_dir.mkdir(exist_ok=True)

        root_logger = logging.getLogger(LOGGER_NAME)
        trade_logger = logging.getLogger(f"{LOGGER_NAME}.trades")
        candle_logger = logging.getLogger(f"{LOGGER_NAME}.candles")
        network_logger = logging.getLogger(f"{LOGGER_NAME}.network")
        broker_logger = logging.getLogger(f"{LOGGER_NAME}.broker")

        # 1. Remove old file handlers cleanly
        _remove_file_handlers(root_logger)
        _remove_file_handlers(trade_logger)
        _remove_file_handlers(candle_logger)
        _remove_file_handlers(network_logger)
        _remove_file_handlers(broker_logger)

        # 2. Main strategy log (INFO+) & Full debug log (DEBUG+)
        _add_file_handler(
            root_logger,
            "strategy.log",
            daily_log_dir,
            level=logging.INFO,
            include_module=True,
        )
        _add_file_handler(
            root_logger,
            "debug.log",
            daily_log_dir,
            level=logging.DEBUG,
            include_module=True,
        )

        # 3. Trade-only log (INFO, filtered)
        trade_fh = AutoFlushRotatingFileHandler(
            daily_log_dir / "trades.log",
            maxBytes=CONFIG["log_max_bytes"],
            backupCount=CONFIG["log_backup_count"],
            encoding="utf-8",
        )
        trade_fh.setLevel(logging.INFO)
        trade_fh.setFormatter(_make_formatter(include_module=False))
        trade_logger.addHandler(trade_fh)

        # 4. Candle CSV logger
        candle_fh = AutoFlushFileHandler(
            daily_log_dir / "candles.csv",
            mode="a",
            encoding="utf-8",
        )
        candle_fh.setLevel(logging.INFO)
        candle_fh.setFormatter(logging.Formatter("%(message)s"))
        candle_logger.addHandler(candle_fh)

        # Write CSV header if file is new/empty
        candle_csv = daily_log_dir / "candles.csv"
        if not candle_csv.exists() or candle_csv.stat().st_size == 0:
            candle_logger.info(
                "timestamp_ist,timestamp_utc,type,symbol,open,high,low,close,volume,"
                "is_bullish,is_bearish,body_size,upper_wick,lower_wick"
            )

        # 5. Dedicated Network & MT5 Broker API log (DEBUG+)
        # Receives all MT5 API calls, rates fetching, tick polling, and connection status
        network_fh = AutoFlushRotatingFileHandler(
            daily_log_dir / "network.log",
            maxBytes=CONFIG["log_max_bytes"],
            backupCount=CONFIG["log_backup_count"],
            encoding="utf-8",
        )
        network_fh.setLevel(logging.DEBUG)
        network_fh.setFormatter(_make_formatter(include_module=True))
        network_logger.addHandler(network_fh)
        broker_logger.addHandler(network_fh)

        root_logger.info("[STARTUP] 📂 Strategy logging initialized for trading date %s (Folder: %s)", clean_date_str, daily_log_dir)


def flush_all_logs() -> None:
    """Flush all active file handlers across the logging hierarchy to guarantee data persistence."""
    with _logging_lock:
        for name in (
            LOGGER_NAME,
            f"{LOGGER_NAME}.trades",
            f"{LOGGER_NAME}.candles",
            f"{LOGGER_NAME}.network",
            f"{LOGGER_NAME}.broker",
            f"{LOGGER_NAME}.pattern",
            f"{LOGGER_NAME}.risk",
            f"{LOGGER_NAME}.orders",
        ):
            l = logging.getLogger(name)
            for h in list(l.handlers):
                try:
                    h.flush()
                except Exception:
                    pass


def install_exception_hooks() -> None:
    """
    Install system-wide exception hooks for sys.excepthook and threading.excepthook
    so ANY unhandled crash in any thread is forensically recorded in strategy.log and debug.log.
    """
    root_log = logging.getLogger(LOGGER_NAME)

    def _sys_hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root_log.critical("[CRASH] 🚨 Uncaught system exception:", exc_info=(exc_type, exc_value, exc_tb))
        flush_all_logs()

    def _thread_hook(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        root_log.critical(
            f"[CRASH] 🚨 Uncaught exception in thread '{args.thread.name}':",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        flush_all_logs()

    sys.excepthook = _sys_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook


def log_event(event_type: str, data: Dict[str, Any]) -> None:
    """
    Log an atomic structured event to logs/YYYY-MM-DD/events.jsonl.
    Provides machine-readable forensic audit trail for analytics, dashboards, and automated monitoring.
    """
    clean_date_str = today_ist().isoformat()
    daily_log_dir = LOG_DIR / clean_date_str
    daily_log_dir.mkdir(exist_ok=True)
    jsonl_path = daily_log_dir / "events.jsonl"

    record = {
        "timestamp": now_ist().isoformat(),
        "event": event_type,
        "symbol": CONFIG.get("symbol", "XAUUSD"),
        **data,
    }
    with _logging_lock:
        try:
            with jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
                f.flush()
        except Exception as e:
            logging.getLogger(LOGGER_NAME).warning("[LOGGING] ⚠️ Failed to write to events.jsonl: %s", e)


def setup_logging() -> logging.Logger:
    """
    Configure the full logging hierarchy:

      Gold4HrReversal          — root strategy logger
      Gold4HrReversal.pattern  — pattern detection details
      Gold4HrReversal.risk     — risk/SL/target calculations
      Gold4HrReversal.orders   — order placement & exits
      Gold4HrReversal.broker   — MT5 broker API interactions
      Gold4HrReversal.trades   — trade-only filtered log
      Gold4HrReversal.candles  — raw candle CSV log
      Gold4HrReversal.network  — isolated network/broker API log
    """
    root_logger = logging.getLogger(LOGGER_NAME)
    if root_logger.handlers:
        return root_logger  # already configured

    # Root logger MUST be DEBUG so debug file handler receives everything.
    root_logger.setLevel(logging.DEBUG)

    today = today_ist().isoformat()

    # Console handler (respects CONFIG log_level and renders ANSI colors if interactive)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, CONFIG["log_level"], logging.INFO))
    ch.setFormatter(_make_formatter(include_module=False, use_color=True))
    root_logger.addHandler(ch)

    trade_logger = logging.getLogger(f"{LOGGER_NAME}.trades")
    trade_logger.propagate = False
    candle_logger = logging.getLogger(f"{LOGGER_NAME}.candles")
    candle_logger.propagate = False
    network_logger = logging.getLogger(f"{LOGGER_NAME}.network")
    network_logger.propagate = False

    rollover_logging(today)
    install_exception_hooks()
    return root_logger


# ── Module-level loggers ─────────────────────────
log = setup_logging()
log_pattern = logging.getLogger(f"{LOGGER_NAME}.pattern")
log_risk = logging.getLogger(f"{LOGGER_NAME}.risk")
log_orders = logging.getLogger(f"{LOGGER_NAME}.orders")
log_broker = logging.getLogger(f"{LOGGER_NAME}.broker")
log_trades = logging.getLogger(f"{LOGGER_NAME}.trades")
log_candles = logging.getLogger(f"{LOGGER_NAME}.candles")
log_network = logging.getLogger(f"{LOGGER_NAME}.network")


# ─────────────────────────────────────────────
# CANDLE LOGGING HELPER
# ─────────────────────────────────────────────
def log_candle_csv(
    candle_type: str,
    symbol: str,
    candle: dict,
) -> None:
    """
    Log a candle to the CSV file for post-session analysis.

    Args:
        candle_type: "COMPLETED_H4", "SPOT_SIGNAL" etc.
        symbol: instrument symbol (e.g. "XAUUSD")
        candle: dict with date, open, high, low, close, volume
    """
    if not candle:
        return
    o = float(candle.get("open") or 0.0)
    h = float(candle.get("high") or 0.0)
    l = float(candle.get("low") or 0.0)
    c = float(candle.get("close") or 0.0)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    is_bull = "1" if c > o else "0"
    is_bear = "1" if c < o else "0"
    vol = candle.get("volume", candle.get("tick_volume", 0))

    dt_ist = candle.get("date")
    if hasattr(dt_ist, "strftime"):
        ist_str = dt_ist.strftime("%Y-%m-%d %H:%M:%S")
    else:
        ist_str = str(dt_ist)

    dt_utc = candle.get("date_utc")
    if hasattr(dt_utc, "strftime"):
        utc_str = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
    else:
        utc_str = ist_str

    log_candles.info(
        f"{ist_str},{utc_str},{candle_type},{symbol},"
        f"{o:.2f},{h:.2f},{l:.2f},{c:.2f},{vol},"
        f"{is_bull},{is_bear},{body:.2f},{upper_wick:.2f},{lower_wick:.2f}"
    )
