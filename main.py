"""
═════════════════════════════════════════════════════════════
  MAIN — Master Orchestration Loop for Gold 4HR Reversal
═════════════════════════════════════════════════════════════

State machine that monitors 4-hour candle completions on XAU/USD,
evaluates the 3-candle reversal pattern, and manages trade entries,
exits, and trailing stop-losses.

Loop structure:
  1. Wait for the next H4 candle to complete.
  2. Fetch the last 3 completed bars.
  3. Run pattern detection.
  4. If signal found and not in position → enter trade.
  5. If in position → monitor ticks for SL/TP/TSL exits.
  6. If opposite signal while in position → exit and reverse.

Gold trades 24/5 (Sunday 22:00 UTC to Friday 22:00 UTC).
No session windows or market-hour guards needed (unlike Indian markets).
"""

import os
import sys
import time
import random
import signal
import argparse
import subprocess
import threading
from datetime import datetime, timezone, timedelta

from config import (
    CONFIG, log, log_broker, now_ist, now_utc, rollover_logging, today_ist, IST,
    log_candle_csv, log_event, flush_all_logs,
)
from broker import MT5Broker
from pattern import detect_setup, format_pattern_eval_summary, evaluate_prospective_setup
from risk import calculate_sl_target, calculate_dollar_pnl
from orders import OrderManager
from state import load_state, save_state
from telegram_alerts import tg


# ─────────────────────────────────────────────
# INTERACTIVE TERMINAL COMMAND ENGINE
# ─────────────────────────────────────────────
_restart_event = threading.Event()
_status_event = threading.Event()
_shutdown_event = threading.Event()


def _sleep_interruptible(seconds: float) -> bool:
    """Sleep for up to `seconds` seconds, waking up immediately if a command is triggered."""
    end = time.time() + seconds
    while time.time() < end:
        if _restart_event.is_set() or _shutdown_event.is_set() or _status_event.is_set():
            return True
        time.sleep(min(0.25, max(0.01, end - time.time())))
    return False


def _log_heartbeat(broker: MT5Broker, state: dict) -> None:
    """Log full heartbeat banner with forming candle progress and strategy stats."""
    snap = broker.get_forming_candle_snapshot()
    if snap:
        pct = snap["progress_pct"]
        bar_fill = int(pct / 10)
        bar_str = "█" * bar_fill + "░" * (10 - bar_fill)
        ist_close_str = snap["ist_close"].strftime("%H:%M")
        utc_close_str = snap["utc_close"].strftime("%H:%M")
        pos_str = f"{state['current_position']['direction']}" if state.get("in_position") else "NONE"

        elapsed_str = f"{snap['elapsed_m']}m {snap['elapsed_s'] % 60:02d}s"
        left_str = f"{int(snap['seconds_left'] // 60)}m {int(snap['seconds_left'] % 60):02d}s"

        quote_status = f"Bid: ${snap['bid']:.2f} | Ask: ${snap['ask']:.2f} | Spread: ${snap['spread']:.2f}"
        if snap.get("tick_age_s", 0) > 60:
            quote_status += f" | ⚠️ Rollover Pause ({int(snap['tick_age_s']//60)}m quiet)"

        log.info(
            "[HEARTBEAT] ⏳ H4 Bar Progress: %.1f%% [%s] | Elapsed: %s/240m | Closes in %s (at %s IST / %s UTC)\n"
            "  • Live Gold LTP: $%.2f (%s)\n"
            "  • Forming Bar  : O=$%.2f H=$%.2f L=$%.2f C=$%.2f | %s %s (%+.2f pts / %+.2f%%)\n"
            "  • Strategy     : Position: %s | Trades Today: %d/%d | Realized P&L: $%.2f | Mode: %s",
            pct, bar_str, elapsed_str, left_str, ist_close_str, utc_close_str,
            snap["ltp"], quote_status,
            snap["open"], snap["high"], snap["low"], snap["close"],
            snap["color"], snap["direction"], snap["change_pts"], snap["change_pct"],
            pos_str, state.get("trades_today", 0), CONFIG["max_trades_per_day"],
            state.get("realized_pnl_today", 0.0), CONFIG["trading_mode"]
        )


def _terminal_command_listener():
    """Background daemon thread listening for interactive terminal commands (e.g. 'restart')."""
    while not _shutdown_event.is_set():
        try:
            line = sys.stdin.readline()
            if not line:
                break
            cmd = line.strip().lower()
            if not cmd:
                continue

            if cmd in ("restart", "r"):
                log.info("[TERMINAL] 🔄 'restart' command received! Initiating strategy reboot...")
                _restart_event.set()
                break
            elif cmd in ("status", "s"):
                _status_event.set()
            elif cmd in ("quit", "exit", "q", "stop"):
                log.info("[TERMINAL] 🛑 '%s' command received! Initiating shutdown...", cmd)
                _shutdown_event.set()
                break
            elif cmd in ("help", "h", "?"):
                print("\n" + "═" * 60)
                print("  💡 INTERACTIVE TERMINAL COMMANDS:")
                print("   • restart (or r) : Cleanly reboot bot & reload code/config")
                print("   • status  (or s) : Print real-time heartbeat & forming bar")
                print("   • quit    (or q) : Gracefully exit and disconnect MT5")
                print("═" * 60 + "\n")
        except Exception:
            break


def _start_terminal_listener():
    """Start the background terminal command listener daemon thread."""
    t = threading.Thread(target=_terminal_command_listener, daemon=True)
    t.start()


def _restart_process(broker: MT5Broker):
    """Cleanly restart the current strategy process."""
    log.info("[RESTART] 🔄 Relaunching strategy process...")
    try:
        broker.disconnect()
    except Exception as e:
        log.warning("[RESTART] Warning during broker disconnect: %s", e)

    args = list(sys.argv)
    # If in LIVE mode, add --daemon so it seamlessly resumes without re-prompting
    if "--daemon" not in args and CONFIG["trading_mode"] == "LIVE":
        args.append("--daemon")

    subprocess.Popen([sys.executable] + args)
    sys.exit(0)


def _sleep_with_jitter(base_seconds: float, jitter: float = 1.5) -> None:
    """Sleep for base_seconds + random jitter to prevent API collisions (interruptible)."""
    _sleep_interruptible(base_seconds + random.uniform(0, jitter))


def _is_weekend() -> bool:
    """Check if markets are closed (Saturday/Sunday UTC).

    Gold market closes Friday ~22:00 UTC and reopens Sunday ~22:00 UTC.
    """
    now = now_utc()
    # Saturday (5) or Sunday (6)
    if now.weekday() == 5:
        return True
    if now.weekday() == 6 and now.hour < 22:
        return True
    # Friday after 22:00 UTC
    if now.weekday() == 4 and now.hour >= 22:
        return True
    return False


def _reconcile_orphaned_position(broker: MT5Broker, order_mgr: OrderManager, state: dict) -> None:
    """
    On startup, verify that a persisted open position actually still exists
    on the broker. If the broker closed it (via SL/TP hit, stop-out, or manual
    close while bot was offline), reconcile state immediately instead of monitoring
    a dead position.
    """
    if not state.get("in_position") or not state.get("current_position"):
        return

    pos = state["current_position"]
    ticket = pos.get("ticket")
    if not ticket:
        return

    # In PAPER mode, trust the local state (no broker to check against)
    if CONFIG["trading_mode"] != "LIVE":
        log.info(
            "[STARTUP] 📋 PAPER mode — trusting persisted position state "
            "(ticket=%s, direction=%s, entry=$%.2f).",
            ticket, pos["direction"], pos["entry_price"]
        )
        return

    log.info(
        "[STARTUP] 🔍 Verifying persisted position ticket=%s exists on MT5 broker...",
        ticket
    )

    live_pos = broker.get_position_by_ticket(ticket)
    if live_pos is not None:
        log.info(
            "[STARTUP] ✅ Position ticket=%s confirmed OPEN on broker. "
            "Current P&L: $%.2f. Resuming monitoring.",
            ticket, live_pos.profit
        )
        return

    # Position is GONE from broker — reconcile
    log.warning(
        "[STARTUP] ⚠️ Position ticket=%s NOT FOUND on broker! "
        "It was closed externally while bot was offline. Reconciling state...",
        ticket
    )
    reconciled = order_mgr.reconcile_external_exit(state, tg=tg)
    if reconciled:
        log.info("[STARTUP] ✅ Orphaned position reconciled successfully.")
    else:
        # Force-clear state if reconcile couldn't find deal history
        log.warning("[STARTUP] ⚠️ Could not find deal history. Force-clearing stale position state.")
        state["in_position"] = False
        state["current_position"] = None
        save_state(state)


def run_strategy():
    """Main strategy execution loop."""
    parser = argparse.ArgumentParser(description="Gold 4HR Reversal Strategy")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode (no interactive prompts)")
    args = parser.parse_args()

    # ── Initialize broker ──────────────────────────
    broker = MT5Broker()
    if not broker.connect():
        log.critical("[STARTUP] ❌ Cannot connect to MT5. Exiting.")
        sys.exit(1)

    # ── Initialize order manager ───────────────────
    order_mgr = OrderManager(broker)

    # ── Load state ─────────────────────────────────
    state = load_state()

    # ── Safety prompt in LIVE mode ─────────────────
    if CONFIG["trading_mode"] == "LIVE":
        if not args.daemon and sys.stdin.isatty():
            confirm = input("⚠️  LIVE trading mode. Type 'LIVE' to confirm: ")
            if confirm.strip().upper() != "LIVE":
                log.info("[STARTUP] User cancelled LIVE mode. Exiting.")
                broker.disconnect()
                sys.exit(0)
        log.warning("[STARTUP] 🔴 LIVE TRADING MODE ACTIVE — Real orders will be placed!")
    else:
        log.info("[STARTUP] 📝 PAPER trading mode — No real orders will be placed.")

    # ── Verify persisted position still exists on broker ──
    _reconcile_orphaned_position(broker, order_mgr, state)

    # ── Startup notifications ──────────────────────
    tg.bot_started(lot_size=CONFIG["lot_size"])
    log.info(
        "[STARTUP] ✅ Gold 4HR Reversal Bot started | Symbol: %s | "
        "Lot: %.2f | Mode: %s | TSL: %s",
        CONFIG["symbol"], CONFIG["lot_size"], CONFIG["trading_mode"],
        "ON" if CONFIG["use_tsl"] else "OFF"
    )

    # ── Seed last_evaluated_candle_time from broker ──
    # Prevents stale candle evaluation if bot starts mid-bar.
    last_evaluated_candle_time = None
    status = broker.get_candle_status()
    if status:
        last_evaluated_candle_time = status["last_closed_bar_time"]
        seconds_left = status["seconds_left"]
        bar_duration = CONFIG.get("candle_tf_minutes", 240) * 60
        # If we're more than 5 minutes into the current bar, skip the prior bar
        if seconds_left < (bar_duration - 300):
            log.info(
                "[STARTUP] 🕐 Bot started mid-candle (%d seconds until next H4 close). "
                "Seeding candle tracker to avoid stale entry. Waiting for NEXT bar.",
                seconds_left
            )
        else:
            # We're within 5 minutes of the new bar — treat as fresh start
            last_evaluated_candle_time = None
            log.info(
                "[STARTUP] 🕐 Bot started near candle boundary (%d seconds left). "
                "Will evaluate the latest completed bar.",
                seconds_left
            )

    # ── Interactive Terminal Listener ──────────────
    if sys.stdin and not sys.stdin.closed:
        _start_terminal_listener()
        if sys.stdin.isatty():
            log.info(
                "[TERMINAL] ⌨️ Interactive console active. "
                "Type 'restart' (or 'r') to reboot, 'status' (or 's') for snapshot, 'quit' to exit."
            )

    _current_date = today_ist().isoformat()
    last_heartbeat_time = 0.0
    last_in_position_hb_time = 0.0
    alert_15m_sent = False
    alert_5m_sent = False

    # ── Main loop ──────────────────────────────────
    try:
        while True:
            # ── Check interactive terminal events ──
            if _restart_event.is_set():
                _restart_process(broker)

            if _shutdown_event.is_set():
                log.info("[SHUTDOWN] 🛑 Shutdown requested from terminal.")
                break

            if _status_event.is_set():
                _status_event.clear()
                _log_heartbeat(broker, state)

            # ── Day rollover — reset daily counters ──
            new_date = today_ist().isoformat()
            if new_date != _current_date:
                # Send daily summary before resetting counters
                try:
                    tg.daily_summary(
                        trades=state.get("trades_today", 0),
                        pnl=state.get("realized_pnl_today", 0.0),
                        balance=state.get("cash", CONFIG["starting_capital"]),
                    )
                except Exception as _tg_err:
                    log.warning("[DAY ROLLOVER] ⚠️ Failed to send daily summary: %s", _tg_err)

                _current_date = new_date
                rollover_logging(new_date)
                state["trades_today"] = 0
                state["realized_pnl_today"] = 0.0
                state["date"] = new_date
                save_state(state)
                alert_15m_sent = False
                alert_5m_sent = False
                log.info(
                    "[DAY ROLLOVER] 📅 New trading date: %s | "
                    "Daily counters reset (trades_today=0, realized_pnl_today=$0.00).",
                    new_date
                )
                log_event("DAY_ROLLOVER", {
                    "new_date": new_date,
                    "trades_today": 0,
                    "realized_pnl_today": 0.0,
                })

            # ── Non-blocking weekend guard ──
            if _is_weekend():
                log.debug("[WEEKEND] 💤 Market closed. Sleeping 60s...")
                time.sleep(60)
                continue

            # ── Position monitoring (when in trade) ──
            if state.get("in_position") and state.get("current_position"):
                # 1. First: check if position was closed externally in MT5 (App/Terminal/SL-TP)
                if order_mgr.reconcile_external_exit(state, tg=tg):
                    last_in_position_hb_time = 0.0
                    log.info("[POSITION] External exit reconciled. Resuming candle monitoring.")
                    continue

                tick = broker.get_tick()
                if tick:
                    broker.refresh_server_offset()
                    pos = state["current_position"]
                    direction = pos["direction"]
                    # Use bid for LONG exit check, ask for SHORT exit check
                    check_price = tick.bid if direction == "LONG" else tick.ask

                    exited = order_mgr.check_exit_conditions(state, check_price, tick=tick)
                    if exited:
                        last_in_position_hb_time = 0.0
                        log.info("[POSITION] Position exited. Resuming candle monitoring.")

                    # Heartbeat update (every 60s)
                    now = now_ist()
                    last_hb = pos.get("last_heartbeat", "")
                    if last_hb:
                        try:
                            last_dt = datetime.fromisoformat(last_hb)
                            if (now - last_dt).total_seconds() >= 60:
                                pos["last_heartbeat"] = now.isoformat()
                                save_state(state)
                        except (ValueError, TypeError):
                            pos["last_heartbeat"] = now.isoformat()
                            save_state(state)

                    # Periodic in-trade heartbeat log (every 5 minutes) for server observability
                    now_ts = time.time()
                    hb_interval = CONFIG.get("in_position_heartbeat_interval_s", 300)
                    if (now_ts - last_in_position_hb_time) >= hb_interval:
                        last_in_position_hb_time = now_ts
                        entry = pos.get("entry_price", 0.0)
                        diff = (check_price - entry) if direction == "LONG" else (entry - check_price)
                        est_profit = diff * pos.get("lot_size", CONFIG.get("lot_size", 0.01)) * 100.0
                        emoji = "🟢" if est_profit >= 0 else "🔴"
                        log.info(
                            "[IN POSITION] ⏳ Monitoring %s ticket #%s | Current Tick: $%.2f (Bid: $%.2f | Ask: $%.2f | Spread: $%.2f) | "
                            "Entry: $%.2f | SL: $%.2f | TP: $%.2f | Est. P&L: %s $%.2f",
                            direction, pos.get("ticket", "N/A"), check_price, tick.bid, tick.ask, tick.spread,
                            entry, pos.get("sl_price", 0.0), pos.get("target_price", 0.0),
                            emoji, est_profit
                        )

                poll_interval = CONFIG.get("position_monitor_interval_s", 0.5)
                poll_jitter = CONFIG.get("position_monitor_jitter_s", 0.1)
                _sleep_with_jitter(poll_interval, jitter=poll_jitter)
                continue

            # ── Check H4 candle status from MT5 broker ──
            status = broker.get_candle_status()
            if not status:
                log.warning("[DATA] ⚠️ Unable to fetch candle status from broker. Retrying...")
                _sleep_with_jitter(5)
                continue

            last_closed_bar_time = status["last_closed_bar_time"]
            seconds_left = status["seconds_left"]

            # ── If a new candle has completed that we haven't evaluated yet ──
            if last_evaluated_candle_time is None or last_closed_bar_time != last_evaluated_candle_time:
                # Small delay if the bar literally just crossed over to let MT5 finalize rates
                if seconds_left > (CONFIG.get("candle_tf_minutes", 240) * 60 - 5):
                    time.sleep(1.5)

                candles = broker.get_candles(count=5, include_forming=False)
                if len(candles) < 3:
                    log.warning("[DATA] ⚠️ Not enough completed candles (%d < 3).", len(candles))
                    _sleep_with_jitter(5)
                    continue

                last_evaluated_candle_time = last_closed_bar_time
                alert_15m_sent = False
                alert_5m_sent = False

                latest_candle = candles[-1]
                o = latest_candle["open"]
                h = latest_candle["high"]
                l = latest_candle["low"]
                c = latest_candle["close"]
                pts = c - o
                pct = (pts / o * 100) if o > 0 else 0.0
                range_pts = h - l
                body_pts = abs(c - o)
                upper_wick = h - max(o, c)
                lower_wick = min(o, c) - l
                vol = latest_candle.get("volume", 0)
                color = "🟢 BULLISH" if c > o else ("🔴 BEARISH" if c < o else "⚪ DOJI")

                dt_ist_str = latest_candle["date"].strftime("%Y-%m-%d %H:%M")
                close_ist_str = (latest_candle["date"] + timedelta(minutes=CONFIG.get("candle_tf_minutes", 240))).strftime("%H:%M")
                dt_utc_str = latest_candle["date_utc"].strftime("%H:%M")
                close_utc_str = (latest_candle["date_utc"] + timedelta(minutes=CONFIG.get("candle_tf_minutes", 240))).strftime("%H:%M")

                log.info("═" * 80)
                log.info(
                    "[CANDLE CLOSED] 📊 H4 Bar Completed | Period: %s to %s IST (%s to %s UTC)",
                    dt_ist_str, close_ist_str, dt_utc_str, close_utc_str
                )
                log.info(
                    "  • OHLC   : Open=$%.2f | High=$%.2f | Low=$%.2f | Close=$%.2f",
                    o, h, l, c
                )
                log.info(
                    "  • Details: %s (%+.2f pts / %+.2f%%) | Range: $%.2f | Body: $%.2f | Wicks: Upper $%.2f, Lower $%.2f | Vol: %s",
                    color, pts, pct, range_pts, body_pts, upper_wick, lower_wick, f"{vol:,}"
                )
                log.info("═" * 80)

                # ── Guaranteed write of every completed H4 candle to candles.csv ──
                log_candle_csv("COMPLETED_H4", CONFIG["symbol"], latest_candle)
                log_event("CANDLE_CLOSED", {
                    "period_start": dt_ist_str,
                    "period_end": close_ist_str,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": vol,
                    "color": color,
                })

                # ── Pattern detection ──────────────────
                signal = detect_setup(candles, log_csv=True)

                # ── Forensic Pattern Audit Checklist ──
                eval_summary = format_pattern_eval_summary(candles, signal)
                log.info(eval_summary)

                if signal is not None:
                    log_event("SIGNAL_DETECTED", {
                        "pattern": signal.pattern_name,
                        "direction": signal.direction,
                        "c3_close": c,
                    })
                    # ── Opposite signal check ──────────────
                    if state.get("in_position") and state.get("current_position"):
                        current_dir = state["current_position"]["direction"]
                        if signal.direction != current_dir and CONFIG.get("exit_on_opposite_signal"):
                            tick = broker.get_tick()
                            if tick:
                                exit_price = tick.bid if current_dir == "LONG" else tick.ask
                                log.info(
                                    "[SIGNAL] 🔄 Opposite signal detected (%s → %s). Exiting current position.",
                                    current_dir, signal.direction
                                )
                                order_mgr.exit_trade(state, exit_price, "OPPOSITE_SIGNAL")
                            else:
                                log.warning("[SIGNAL] ⚠️ Cannot exit for opposite signal — no tick data.")
                                continue
                        else:
                            log.info("[SIGNAL] Signal matches current direction. No action needed.")
                            continue

                    # ── Tier 4: Precision 1-Second Entry Window (0s to +60s) ──
                    if not state.get("in_position"):
                        log.info(
                            "[ENTRY WINDOW] ⚡ Reversal Signal confirmed (%s %s)! "
                            "Entering precision 1-second execution window for earliest entry...",
                            signal.direction, signal.pattern_name
                        )
                        entry_window_start = time.time()
                        entry_success = False
                        max_spread = CONFIG.get("max_spread_dollars", 1.20)

                        while (time.time() - entry_window_start) <= 60:
                            tick = broker.get_tick()
                            if not tick:
                                time.sleep(1)
                                continue

                            # Spread spike guard (especially for 00:00 server rollover at 02:30 IST)
                            if tick.spread > max_spread:
                                log.warning(
                                    "[ENTRY TIMING] ⏳ Spread widened at boundary: $%.2f > max $%.2f. "
                                    "Waiting for spread normalization (checking every 1s)...",
                                    tick.spread, max_spread
                                )
                                time.sleep(1)
                                continue

                            entry_price = tick.ask if signal.direction == "LONG" else tick.bid
                            risk_params = calculate_sl_target(entry_price, signal)

                            if not risk_params.is_valid:
                                log.warning("[ENTRY] ⚠️ Trade SKIPPED: %s", risk_params.skip_reason)
                                break

                            success = order_mgr.enter_trade(state, risk_params)
                            if success:
                                latency = time.time() - entry_window_start
                                log.info(
                                    "[ENTRY PERFORMANCE] 🚀 Trade executed in %.2fs from candle close! "
                                    "Fill: $%.2f | Spread: $%.2f | Latency: %.2fs",
                                    latency, entry_price, tick.spread, latency
                                )
                                tg.trade_entry(
                                    direction=signal.direction,
                                    entry_price=risk_params.entry_price,
                                    sl=risk_params.sl_price,
                                    tp=risk_params.target_price,
                                    risk=risk_params.risk,
                                    lot_size=CONFIG["lot_size"],
                                )
                                entry_success = True
                                break

                            time.sleep(1)

                        if not entry_success and not state.get("in_position"):
                            log.warning("[ENTRY WINDOW] ⚠️ 60-second entry window expired without order execution.")

            # ── Multi-Tier Adaptive Polling & Anti-Flooding Sleep Management ──

            # Tier 3: Critical Pre-Close 1-Second Countdown (-60s to 0s)
            if 0 < seconds_left <= 60:
                snap = broker.get_forming_candle_snapshot()
                if snap:
                    # In console terminal: dynamic in-place update using carriage return
                    if sys.stdout.isatty():
                        sys.stdout.write(
                            f"\r[1s WATCH] T-{int(seconds_left):02d}s to H4 close | "
                            f"Bid: ${snap['bid']:.2f} | Ask: ${snap['ask']:.2f} | Spread: ${snap['spread']:.2f} | "
                            f"Bar: {snap['color']} {snap['direction']}   "
                        )
                        sys.stdout.flush()

                    # In strategy.log: log milestones only to prevent log flooding
                    if int(seconds_left) in (60, 30, 15, 10, 5, 3, 2, 1):
                        log.info(
                            "[COUNTDOWN] ⏳ T-%ds to H4 close | Bid: $%.2f | Ask: $%.2f | Spread: $%.2f",
                            int(seconds_left), snap["bid"], snap["ask"], snap["spread"]
                        )
                _sleep_interruptible(1)

            # Tier 2: Preparation Phase (15 minutes down to 1 minute left, 900s to 60s)
            elif 60 < seconds_left <= 900:
                # 15-minute alert
                if seconds_left <= 900 and not alert_15m_sent:
                    alert_15m_sent = True
                    log.info("[PRE-CLOSE] ⏳ 15 minutes remaining until H4 candle completion. Monitoring forming bar structure.")

                # 5-minute pre-close watch & prospective setup evaluation
                if seconds_left <= 300 and not alert_5m_sent:
                    alert_5m_sent = True
                    snap = broker.get_forming_candle_snapshot()
                    candles = broker.get_candles(count=5, include_forming=False)
                    if snap and len(candles) >= 2:
                        prospective = evaluate_prospective_setup(snap, candles)
                        log.info(
                            "[PRE-CLOSE WATCH] ⏳ 5 minutes to H4 close! Prospective status:\n  • %s",
                            prospective["message"]
                        )

                # Sleep 10 seconds between checks (interruptible by console commands)
                _sleep_interruptible(10)

            # Tier 1: Quiet Cruising (> 15 minutes left)
            elif seconds_left > 900:
                # Periodic milestone Heartbeat every 15 minutes (or on initial boot)
                now_ts = time.time()
                if (now_ts - last_heartbeat_time) >= 900:
                    last_heartbeat_time = now_ts
                    _log_heartbeat(broker, state)

                # Sleep 30 seconds quietly without log spam (interruptible by console commands)
                _sleep_interruptible(30)

            else:
                # 0 seconds remaining: wait for MT5 candle boundary crossover
                _sleep_interruptible(1)

    except KeyboardInterrupt:
        log.info("[SHUTDOWN] 🛑 Keyboard interrupt received. Shutting down gracefully.")
    except Exception as e:
        log.critical(f"[CRASH] 🚨 Unhandled exception: {e}", exc_info=True)
        tg.bot_crashed(str(e))

        # Emergency: try to close any open position
        if state.get("in_position") and state.get("current_position"):
            log.warning("[CRASH GUARD] ⚠️ Attempting emergency position close...")
            try:
                tick = broker.get_tick()
                if tick:
                    direction = state["current_position"]["direction"]
                    exit_price = tick.bid if direction == "LONG" else tick.ask
                    order_mgr.exit_trade(state, exit_price, "CRASH_EMERGENCY")
            except Exception as crash_e:
                log.critical("[CRASH GUARD] ❌ Emergency exit FAILED: %s", crash_e)
        raise
    finally:
        broker.disconnect()
        flush_all_logs()
        log.info("[SHUTDOWN] ✅ Bot cleanly shut down.")


if __name__ == "__main__":
    run_strategy()
