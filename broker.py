"""
═════════════════════════════════════════════════════════════
  BROKER — MetaTrader 5 Client Wrapper
═════════════════════════════════════════════════════════════

Clean, modular wrapper around the MT5 Python API.
Handles connection, candle fetching, tick data, order routing,
position management, and auto-reconnect logic.

On Windows: Uses the native MetaTrader5 C-API (direct IPC to terminal).
On Linux:   Uses MetaApi Cloud Gateway (pure Python, zero Wine needed).

The rest of the codebase is 100% identical across both platforms.
"""

import sys
import time
import platform
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass

from config import CONFIG, log, log_broker, IST, now_ist

# ─────────────────────────────────────────────
# PLATFORM DETECTION & IMPORT
# ─────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import MetaTrader5 as mt5
else:
    mt5 = None  # MetaApi will be used on Linux (deferred import)


@dataclass
class TickData:
    """Represents a live tick snapshot."""
    bid: float
    ask: float
    spread: float
    time: datetime


@dataclass
class PositionInfo:
    """Represents an open MT5 position."""
    ticket: int
    symbol: str
    direction: str       # "LONG" or "SHORT"
    volume: float        # lot size
    open_price: float
    sl: float
    tp: float
    profit: float        # current unrealized P&L
    magic: int
    comment: str


class MT5Broker:
    """
    High-performance MT5 broker wrapper.

    Responsibilities:
      - Terminal connection & health checks
      - H4 candle fetching (with incomplete bar dropping)
      - Live tick/spread retrieval
      - Order execution (BUY/SELL market orders)
      - Position modification (SL/TP updates for TSL)
      - Position closing
      - Account info
    """

    def __init__(self):
        self._connected = False
        self._symbol = CONFIG["symbol"]
        self._magic = CONFIG["magic_number"]
        self._fetch_count = 0  # API call counter for observability
        self._server_offset_hours = 0  # Offset in hours between MT5 server and UTC

    # ─────────────────────────────────────────
    # CONNECTION
    # ─────────────────────────────────────────

    def connect(self) -> bool:
        """Initialize connection to the MT5 terminal."""
        if not IS_WINDOWS:
            log_broker.error("MT5 native connection requires Windows. Use MetaApi on Linux.")
            return False

        if not mt5.initialize():
            error = mt5.last_error()
            log_broker.error("[BROKER] ❌ Failed to connect to MT5 terminal. Error: %s", error)
            return False

        self._connected = True

        # Ensure symbol is visible in Market Watch
        if not mt5.symbol_select(self._symbol, True):
            log_broker.error("[BROKER] ❌ Symbol '%s' not found or cannot be selected.", self._symbol)
            self.disconnect()
            return False

        # Log account info
        info = mt5.account_info()
        if info:
            log.info(
                "[BROKER] ✅ Connected to MT5 | Account: %s (%s) | "
                "Balance: $%s | Leverage: 1:%d | Server: %s",
                info.login, info.name, f"{info.balance:,.2f}",
                info.leverage, info.server
            )

        # Detect broker server timezone offset relative to UTC (with weekend/stale-tick safeguard)
        self._detect_server_offset()
        return True

    def _detect_server_offset(self) -> int:
        """
        Detect broker server timezone offset relative to UTC.

        Safeguards:
          - If booted on a weekend or after market close, tick.time is stale (> 2h old).
            Computing (tick.time - now_utc) would produce a massive negative offset (e.g. -39h).
          - Valid broker server offsets are strictly between 0 and +4 hours (typically UTC+2 or UTC+3).
          - If tick is stale or out of bounds, default to UTC+3 (standard Exness/Forex server time).
        """
        if not IS_WINDOWS:
            self._server_offset_hours = 3
            return 3

        tick = mt5.symbol_info_tick(self._symbol)
        if tick and tick.time > 0:
            now_utc = datetime.now(timezone.utc)
            tick_dt = datetime.fromtimestamp(tick.time, timezone.utc)
            age_seconds = (now_utc - tick_dt).total_seconds()

            if age_seconds < 7200:  # Fresh tick within last 2 hours
                detected = round((tick_dt - now_utc).total_seconds() / 3600)
                if 0 <= detected <= 4:
                    self._server_offset_hours = detected
                    log_broker.info(
                        "[BROKER] 🕒 Broker server offset detected: UTC%+d hours | "
                        "Server Time: %s | Local Time: %s IST",
                        self._server_offset_hours,
                        tick_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        now_ist().strftime("%Y-%m-%d %H:%M:%S")
                    )
                    return self._server_offset_hours

            # Stale tick or out of bounds -> fallback to UTC+3
            log_broker.warning(
                "[BROKER] ⚠️ Stale tick detected (age=%.0fs / %.1fh). "
                "Defaulting server offset to UTC+3 (will refresh on first fresh tick).",
                age_seconds, age_seconds / 3600
            )

        self._server_offset_hours = 3  # Standard default UTC+3
        log_broker.info("[BROKER] 🕒 Server offset set to default: UTC%+d hours", self._server_offset_hours)
        return self._server_offset_hours

    def refresh_server_offset(self) -> None:
        """Refresh server offset if currently on fallback."""
        if hasattr(self, "_server_offset_hours") and self._server_offset_hours == 3:
            tick = mt5.symbol_info_tick(self._symbol) if IS_WINDOWS else None
            if tick and tick.time > 0:
                now_utc = datetime.now(timezone.utc)
                tick_dt = datetime.fromtimestamp(tick.time, timezone.utc)
                if (now_utc - tick_dt).total_seconds() < 300:  # Live tick within 5 minutes
                    detected = round((tick_dt - now_utc).total_seconds() / 3600)
                    if 0 <= detected <= 4 and detected != self._server_offset_hours:
                        self._server_offset_hours = detected
                        log_broker.info("[BROKER] 🕒 Server offset dynamically updated to UTC%+d hours", detected)

    def disconnect(self) -> None:
        """Cleanly shutdown MT5 connection."""
        if IS_WINDOWS and self._connected:
            mt5.shutdown()
            self._connected = False
            log_broker.info("[BROKER] 🔌 MT5 connection closed.")

    def ensure_connected(self) -> bool:
        """Check connection health and reconnect if needed."""
        if not self._connected:
            return self.connect()

        # Quick health check via terminal_info
        info = mt5.terminal_info()
        if info is None:
            err = mt5.last_error() if IS_WINDOWS else "Not Windows"
            log_broker.warning("[BROKER] ⚠️ MT5 connection lost (Error: %s). Attempting reconnect...", err)
            self._connected = False
            return self.connect()
        return True

    # ─────────────────────────────────────────
    # ACCOUNT INFO
    # ─────────────────────────────────────────

    def get_account_balance(self) -> float:
        """Return current account balance."""
        info = mt5.account_info()
        return info.balance if info else 0.0

    def get_account_equity(self) -> float:
        """Return current account equity."""
        info = mt5.account_info()
        return info.equity if info else 0.0

    # ─────────────────────────────────────────
    # CANDLE DATA
    # ─────────────────────────────────────────

    def get_candles(
        self,
        count: int = 10,
        include_forming: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch the last `count` H4 candles for Gold.

        By default, the currently forming (incomplete) bar is DROPPED
        to ensure only closed/completed bars are returned for pattern
        evaluation (per AGENTS.md §4.2).

        Args:
            count: number of bars to request (including the forming bar).
            include_forming: if True, include the currently forming bar.

        Returns:
            List of candle dicts with keys: date, open, high, low, close,
            tick_volume, spread, real_volume.
        """
        if not self.ensure_connected():
            log_broker.error("[BROKER] ❌ Cannot fetch candles — not connected.")
            return []

        # Request extra bars so we have enough after dropping the forming bar
        request_count = count + (0 if include_forming else 1)
        rates = mt5.copy_rates_from_pos(self._symbol, mt5.TIMEFRAME_H4, 0, request_count)
        self._fetch_count += 1

        if rates is None or len(rates) == 0:
            log_broker.error(
                "[BROKER] ❌ Failed to fetch H4 candles. Error: %s", mt5.last_error()
            )
            return []

        candles = []
        for r in rates:
            real_vol = int(r['real_volume']) if 'real_volume' in rates.dtype.names else 0
            server_dt = datetime.fromtimestamp(r['time'], tz=timezone.utc)
            utc_dt = datetime.fromtimestamp(r['time'] - self._server_offset_hours * 3600, tz=timezone.utc)
            ist_dt = utc_dt.astimezone(IST)

            candles.append({
                "date": ist_dt,
                "date_utc": utc_dt,
                "date_server": server_dt.strftime("%Y-%m-%d %H:%M Server"),
                "open": float(r['open']),
                "high": float(r['high']),
                "low": float(r['low']),
                "close": float(r['close']),
                "tick_volume": int(r['tick_volume']),
                "volume": real_vol if real_vol > 0 else int(r['tick_volume']),
                "spread": int(r['spread']),
            })

        # Drop the last bar (currently forming) unless explicitly requested
        if not include_forming and len(candles) > 0:
            candles = candles[:-1]

        log_broker.debug(
            "[BROKER] Fetched %d completed H4 candles (API call #%d). "
            "Latest: %s IST (%s UTC) O=$%.2f C=$%.2f",
            len(candles), self._fetch_count,
            candles[-1]["date"].strftime("%Y-%m-%d %H:%M") if candles else "N/A",
            candles[-1]["date_utc"].strftime("%H:%M") if candles else "N/A",
            candles[-1]["open"] if candles else 0,
            candles[-1]["close"] if candles else 0,
        )
        return candles

    def get_candle_status(self) -> Optional[Dict[str, Any]]:
        """
        Get current H4 candle status directly from MT5.

        Returns:
            Dict containing:
              - forming_open_time: int epoch of current forming bar start
              - last_closed_bar_time: int epoch of latest completed bar start
              - seconds_left: seconds remaining until current bar closes
              - current_server_time: broker server time epoch
              - expected_close_ist: aware datetime in IST for scheduled close
              - expected_close_utc: aware datetime in UTC for scheduled close
        """
        if not self.ensure_connected():
            return None

        rates = mt5.copy_rates_from_pos(self._symbol, mt5.TIMEFRAME_H4, 0, 2)
        if rates is None or len(rates) < 2:
            return None

        # Use continuous server time derived from live UTC clock + server offset
        now_utc_ts = datetime.now(timezone.utc).timestamp()
        current_server_time = int(now_utc_ts + (self._server_offset_hours * 3600))

        forming_open = int(rates[-1]['time'])
        last_closed = int(rates[-2]['time'])
        bar_duration = CONFIG.get("candle_tf_minutes", 240) * 60
        bar_close = forming_open + bar_duration
        seconds_left = max(0, bar_close - current_server_time)

        utc_close = datetime.fromtimestamp(bar_close - self._server_offset_hours * 3600, tz=timezone.utc)
        ist_close = utc_close.astimezone(IST)

        return {
            "forming_open_time": forming_open,
            "last_closed_bar_time": last_closed,
            "seconds_left": seconds_left,
            "current_server_time": current_server_time,
            "expected_close_ist": ist_close,
            "expected_close_utc": utc_close,
        }

    def get_forming_candle_snapshot(self) -> Optional[Dict[str, Any]]:
        """
        Return real-time snapshot of the currently forming H4 candle.
        Includes OHLC, progress %, elapsed minutes, remaining seconds,
        LTP, current spread, and whether it's bullish/bearish.
        """
        if not self.ensure_connected():
            return None

        rates = mt5.copy_rates_from_pos(self._symbol, mt5.TIMEFRAME_H4, 0, 1)
        if rates is None or len(rates) == 0:
            return None

        tick = mt5.symbol_info_tick(self._symbol)

        forming_open = int(rates[0]['time'])
        now_utc_ts = datetime.now(timezone.utc).timestamp()
        current_server_time = int(now_utc_ts + (self._server_offset_hours * 3600))

        bar_duration = CONFIG.get("candle_tf_minutes", 240) * 60
        elapsed_s = max(0, current_server_time - forming_open)
        seconds_left = max(0, bar_duration - elapsed_s)
        progress_pct = min(100.0, (elapsed_s / bar_duration) * 100)

        o = float(rates[0]['open'])
        h = float(rates[0]['high'])
        l = float(rates[0]['low'])
        c = float(rates[0]['close'])
        bid = float(tick.bid) if tick else c
        ask = float(tick.ask) if tick else c
        ltp = bid
        spread = round(float(ask - bid), 2) if tick else 0.0
        tick_age_s = max(0, int(current_server_time - int(tick.time))) if tick else 9999
        change_pts = c - o
        change_pct = (change_pts / o * 100) if o > 0 else 0.0

        utc_open = datetime.fromtimestamp(forming_open - self._server_offset_hours * 3600, tz=timezone.utc)
        ist_open = utc_open.astimezone(IST)
        utc_close = utc_open + timedelta(seconds=bar_duration)
        ist_close = utc_close.astimezone(IST)

        direction = "BULL" if c > o else ("BEAR" if c < o else "DOJI")
        color = "🟢" if c > o else ("🔴" if c < o else "⚪")

        return {
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "tick_age_s": tick_age_s,
            "change_pts": change_pts,
            "change_pct": change_pct,
            "direction": direction,
            "color": color,
            "elapsed_s": elapsed_s,
            "elapsed_m": elapsed_s // 60,
            "seconds_left": seconds_left,
            "mins_left": seconds_left / 60,
            "progress_pct": progress_pct,
            "ist_open": ist_open,
            "ist_close": ist_close,
            "utc_open": utc_open,
            "utc_close": utc_close,
        }

    # ─────────────────────────────────────────
    # LIVE TICK / SPREAD
    # ─────────────────────────────────────────

    def get_tick(self) -> Optional[TickData]:
        """Return the latest tick (bid, ask, spread) for Gold."""
        if not self.ensure_connected():
            return None

        tick = mt5.symbol_info_tick(self._symbol)
        if tick is None:
            log_broker.error("[BROKER] ❌ Failed to get tick for %s", self._symbol)
            return None

        symbol_info = mt5.symbol_info(self._symbol)
        point = symbol_info.point if symbol_info else 0.01

        spread_dollars = (tick.ask - tick.bid)
        return TickData(
            bid=tick.bid,
            ask=tick.ask,
            spread=round(spread_dollars, 2),
            time=datetime.fromtimestamp(tick.time, tz=timezone.utc),
        )

    def get_spread(self) -> float:
        """Return current spread in dollars."""
        tick = self.get_tick()
        return tick.spread if tick else 999.0

    # ─────────────────────────────────────────
    # ORDER EXECUTION
    # ─────────────────────────────────────────

    def send_order(
        self,
        direction: str,
        lot_size: float,
        sl: float,
        tp: float,
        comment: str = "",
    ) -> Optional[int]:
        """
        Send a market order (BUY or SELL) for Gold.

        Args:
            direction: "LONG" (buy) or "SHORT" (sell)
            lot_size:  position size in lots (e.g. 0.01)
            sl:        stop loss price
            tp:        take profit price
            comment:   order comment for audit trail

        Returns:
            Order ticket number on success, None on failure.
        """
        if not self.ensure_connected():
            return None

        tick = self.get_tick()
        if tick is None:
            log_broker.error("[BROKER] ❌ Cannot place order — no tick data available.")
            return None

        if direction == "LONG":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        else:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self._symbol,
            "volume": lot_size,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,  # max slippage in points
            "magic": self._magic,
            "comment": comment or f"Gold4HR_{direction}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        log_broker.info(
            "[BROKER] 📤 Sending %s order: %.2f lots @ $%.2f | SL=$%.2f | TP=$%.2f",
            direction, lot_size, price, sl, tp
        )

        result = mt5.order_send(request)

        if result is None:
            log_broker.error("[BROKER] ❌ Order send returned None. Error: %s", mt5.last_error())
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            log_broker.error(
                "[BROKER] ❌ Order REJECTED: retcode=%d, comment='%s'",
                result.retcode, result.comment
            )
            return None

        log_broker.info(
            "[BROKER] ✅ Order FILLED: ticket=%d | %s %.2f lots @ $%.2f | SL=$%.2f | TP=$%.2f",
            result.order, direction, lot_size, result.price, sl, tp
        )
        return result.order

    # ─────────────────────────────────────────
    # POSITION MODIFICATION (TSL)
    # ─────────────────────────────────────────

    def modify_position(
        self,
        ticket: int,
        new_sl: float,
        new_tp: float,
    ) -> bool:
        """
        Modify the SL and/or TP of an open position (used for TSL updates).

        Args:
            ticket: position ticket number
            new_sl: new stop loss price
            new_tp: new take profit price

        Returns:
            True if modification succeeded, False otherwise.
        """
        if not self.ensure_connected():
            return False

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": self._symbol,
            "sl": new_sl,
            "tp": new_tp,
            "magic": self._magic,
        }

        result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = result.retcode if result else "None"
            comment = result.comment if result else mt5.last_error()
            log_broker.error(
                "[BROKER] ❌ Position modify FAILED: ticket=%d, retcode=%s, comment='%s'",
                ticket, retcode, comment
            )
            return False

        log_broker.info(
            "[BROKER] ✅ Position MODIFIED: ticket=%d | New SL=$%.2f | New TP=$%.2f",
            ticket, new_sl, new_tp
        )
        return True

    # ─────────────────────────────────────────
    # POSITION CLOSING
    # ─────────────────────────────────────────

    def close_position(self, ticket: int) -> bool:
        """
        Close an open position at market price.

        Args:
            ticket: position ticket number

        Returns:
            True if the position was closed, False otherwise.
        """
        if not self.ensure_connected():
            return False

        # Find the position
        position = mt5.positions_get(ticket=ticket)
        if not position:
            log_broker.warning("[BROKER] ⚠️ Position ticket=%d not found (may already be closed).", ticket)
            return True  # Already closed is not an error

        pos = position[0]
        # Determine close direction (opposite of open)
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        deviation = int(CONFIG.get("close_deviation_points", 50))

        # Retry loop (up to 3 attempts with fresh ticks)
        for attempt in range(1, 4):
            tick = mt5.symbol_info_tick(self._symbol)
            if not tick:
                time.sleep(0.2)
                continue

            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self._symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": ticket,
                "price": price,
                "deviation": deviation,
                "magic": self._magic,
                "comment": "Gold4HR_Close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)

            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log_broker.info(
                    "[BROKER] ✅ Position CLOSED (attempt #%d): ticket=%d | Close price=$%.2f | P&L=$%.2f",
                    attempt, ticket, result.price, pos.profit
                )
                return True

            retcode = result.retcode if result else "None"
            comment = result.comment if result else mt5.last_error()
            log_broker.warning(
                "[BROKER] ⚠️ Close attempt #%d/3 failed: ticket=%d, retcode=%s, comment='%s'",
                attempt, ticket, retcode, comment
            )

            # Check if position was closed in the meantime (e.g. by broker SL/TP)
            remaining = mt5.positions_get(ticket=ticket)
            if not remaining:
                log_broker.info("[BROKER] ✅ Position ticket=%d confirmed closed externally during close attempt.", ticket)
                return True

            time.sleep(0.25)

        log_broker.error(
            "[BROKER] ❌ All 3 close attempts failed for ticket=%d. Position may require manual or server SL intervention.",
            ticket
        )
        return False

    # ─────────────────────────────────────────
    # POSITION QUERY
    # ─────────────────────────────────────────

    def get_open_positions(self) -> List[PositionInfo]:
        """Return all open positions for this strategy (filtered by magic number)."""
        if not self.ensure_connected():
            return []

        positions = mt5.positions_get(symbol=self._symbol)
        if positions is None:
            return []

        result = []
        for pos in positions:
            if pos.magic != self._magic:
                continue
            direction = "LONG" if pos.type == mt5.ORDER_TYPE_BUY else "SHORT"
            result.append(PositionInfo(
                ticket=pos.ticket,
                symbol=pos.symbol,
                direction=direction,
                volume=pos.volume,
                open_price=pos.price_open,
                sl=pos.sl,
                tp=pos.tp,
                profit=pos.profit,
                magic=pos.magic,
                comment=pos.comment,
            ))
        return result

    def get_position_by_ticket(self, ticket: int) -> Optional[PositionInfo]:
        """Get a specific open position by ticket number."""
        positions = self.get_open_positions()
        for pos in positions:
            if pos.ticket == ticket:
                return pos
        return None

    def get_deals_by_position(self, ticket: int) -> List[Any]:
        """Fetch historical deals associated with a position ticket."""
        if not self.ensure_connected():
            return []
        deals = mt5.history_deals_get(position=ticket)
        if deals is None:
            return []
        return list(deals)

    # ─────────────────────────────────────────
    # SYMBOL INFO
    # ─────────────────────────────────────────

    def get_symbol_info(self) -> Optional[Dict[str, Any]]:
        """Return key symbol properties for Gold."""
        if not self.ensure_connected():
            return None
        info = mt5.symbol_info(self._symbol)
        if info is None:
            return None
        return {
            "symbol": info.name,
            "bid": info.bid,
            "ask": info.ask,
            "spread": info.spread,
            "digits": info.digits,
            "point": info.point,
            "contract_size": info.trade_contract_size,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
        }
