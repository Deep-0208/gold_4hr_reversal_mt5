"""
═════════════════════════════════════════════════════════════
  ORDERS — Trade Execution & Trailing Stop-Loss Manager
═════════════════════════════════════════════════════════════

Manages the full lifecycle of Gold trades:
  1. Entry execution (PAPER or LIVE via MT5 broker)
  2. Real-time TSL monitoring and SL modification
  3. Exit execution (SL hit, TP hit, TSL trail, opposite signal)
  4. Position flipping on opposite signal

Thread-safe: all order operations are protected by trade_lock.
"""

import threading
from typing import Optional, Dict, Any
from datetime import datetime

from config import CONFIG, log, log_orders, log_trades, now_ist
from broker import MT5Broker, TickData
from risk import RiskParams, calculate_dollar_pnl
from state import save_state, append_trade_journal
from telegram_alerts import tg


# ─────────────────────────────────────────────
# THREAD SAFETY
# ─────────────────────────────────────────────
trade_lock = threading.Lock()


class OrderManager:
    """
    Manages order execution for both PAPER and LIVE modes.

    In PAPER mode: simulates fills at current bid/ask, tracks virtual P&L.
    In LIVE mode:  sends real market orders to MT5 with broker-side SL/TP.
    """

    def __init__(self, broker: MT5Broker):
        self._broker = broker
        self._mode = CONFIG["trading_mode"]  # "PAPER" or "LIVE"
        self._lot_size = CONFIG["lot_size"]

    # ─────────────────────────────────────────
    # ENTRY
    # ─────────────────────────────────────────

    def enter_trade(
        self,
        state: Dict[str, Any],
        risk_params: RiskParams,
    ) -> bool:
        """
        Execute a trade entry (LONG or SHORT).

        Args:
            state:       mutable strategy state dict
            risk_params: calculated SL/Target/Risk from risk.py

        Returns:
            True if entry was successful, False if rejected.
        """
        with trade_lock:
            direction = risk_params.direction
            entry_price = risk_params.entry_price
            sl = risk_params.sl_price
            tp = risk_params.target_price

            if state.get("in_position"):
                log_orders.warning("[ORDERS] ⚠️ Already in position — cannot enter new trade.")
                return False

            # Guard: max trades per day
            if state.get("trades_today", 0) >= CONFIG["max_trades_per_day"]:
                log_orders.warning(
                    "[ORDERS] ⚠️ Max trades per day reached (%d). No new entries.",
                    CONFIG["max_trades_per_day"]
                )
                return False

            # Guard: daily drawdown kill-switch
            max_loss = CONFIG.get("max_daily_loss", 0)
            if max_loss > 0 and abs(state.get("realized_pnl_today", 0)) >= max_loss:
                log_orders.warning(
                    "[ORDERS] ⚠️ Daily drawdown kill-switch active. "
                    "Realized loss today: $%.2f >= limit $%.2f. No new entries.",
                    abs(state.get("realized_pnl_today", 0)), max_loss
                )
                return False

            # Guard: spread check
            spread = self._broker.get_spread()
            max_spread = CONFIG.get("max_spread_dollars", 1.20)
            if spread > max_spread:
                log_orders.warning(
                    "[ORDERS] ⚠️ Spread too wide: $%.2f > max $%.2f. Skipping entry.",
                    spread, max_spread
                )
                return False

            ticket = None
            fill_price = entry_price

            if self._mode == "LIVE":
                ticket = self._broker.send_order(
                    direction=direction,
                    lot_size=self._lot_size,
                    sl=sl,
                    tp=tp,
                    comment=f"Gold4HR_{direction}",
                )
                if ticket is None:
                    log_orders.error("[ORDERS] ❌ LIVE order REJECTED by broker.")
                    return False

                # Get actual fill price from the broker position
                pos_info = self._broker.get_position_by_ticket(ticket)
                if pos_info:
                    fill_price = pos_info.open_price
            else:
                # PAPER mode: simulate fill at current bid/ask
                tick = self._broker.get_tick()
                if tick:
                    fill_price = tick.ask if direction == "LONG" else tick.bid
                ticket = int(now_ist().timestamp())  # pseudo-ticket for paper mode

            # Update state
            now = now_ist()
            state["in_position"] = True
            state["trades_today"] = state.get("trades_today", 0) + 1
            state["current_position"] = {
                "direction": direction,
                "ticket": ticket,
                "lot_size": self._lot_size,
                "entry_price": fill_price,
                "sl_price": sl,
                "target_price": tp,
                "risk": risk_params.risk,
                "entry_time": now.isoformat(),
                "entry_date": now.date().isoformat(),
                "tsl_active": False,
                "highest_price": fill_price,
                "lowest_price": fill_price,
                "last_heartbeat": now.isoformat(),
            }
            save_state(state)

            log.info(
                f"[ORDER EXEC] ✅ {self._mode} {direction} ENTRY | "
                f"Ticket: {ticket} | Fill: ${fill_price:.2f} | "
                f"SL: ${sl:.2f} | TP: ${tp:.2f} | Risk: ${risk_params.risk:.2f} | "
                f"Lots: {self._lot_size}"
            )
            log_trades.info(
                f"[ENTRY] {direction} | Price: ${fill_price:.2f} | "
                f"SL: ${sl:.2f} | TP: ${tp:.2f} | "
                f"Risk: ${risk_params.risk:.2f} | Mode: {self._mode}"
            )
            return True

    # ─────────────────────────────────────────
    # EXIT
    # ─────────────────────────────────────────

    def exit_trade(
        self,
        state: Dict[str, Any],
        exit_price: float,
        reason: str,
    ) -> bool:
        """
        Exit the current position.

        Args:
            state:      mutable strategy state dict
            exit_price: current Gold price at exit
            reason:     exit reason (SL_HIT, TP_HIT, TSL_HIT, OPPOSITE_SIGNAL, etc.)

        Returns:
            True if exit was successful, False otherwise.
        """
        with trade_lock:
            if not state.get("in_position") or not state.get("current_position"):
                log_orders.warning("[ORDERS] ⚠️ No active position to exit.")
                return False

            pos = state["current_position"]
            direction = pos["direction"]
            entry_price = pos["entry_price"]
            ticket = pos.get("ticket")

            # Close in broker (LIVE mode)
            broker_pnl = None
            if self._mode == "LIVE" and ticket:
                success = self._broker.close_position(ticket)
                if not success:
                    log_orders.error("[ORDERS] ❌ Failed to close position ticket=%s", ticket)
                    return False

                # Use broker's actual realized P&L from closed deals (includes spread, slippage, swap)
                try:
                    deals = self._broker.get_deals_by_position(ticket)
                    if deals:
                        broker_pnl = sum(d.profit for d in deals)
                        log_orders.info(
                            "[ORDERS] 📊 Broker actual P&L for ticket=%d: $%.2f (from %d deals)",
                            ticket, broker_pnl, len(deals)
                        )
                except Exception as deal_err:
                    log_orders.warning(
                        "[ORDERS] ⚠️ Could not fetch deal history for ticket=%d: %s. Using formula P&L.",
                        ticket, deal_err
                    )

            # Calculate P&L: prefer broker actual, fall back to formula
            formula_pnl = calculate_dollar_pnl(direction, entry_price, exit_price, self._lot_size)
            pnl = broker_pnl if broker_pnl is not None else formula_pnl
            pnl_pct = (pnl / (pos.get("risk", 1.0) * self._lot_size * 100)) * 100 if pos.get("risk") else 0

            if broker_pnl is not None and abs(broker_pnl - formula_pnl) > 0.50:
                log_orders.info(
                    "[ORDERS] 📊 P&L delta: broker=$%.2f vs formula=$%.2f (diff=$%.2f from spread/slippage)",
                    broker_pnl, formula_pnl, broker_pnl - formula_pnl
                )

            # Update state
            state["in_position"] = False
            state["realized_pnl_today"] = state.get("realized_pnl_today", 0) + pnl
            state["total_realized_pnl"] = state.get("total_realized_pnl", 0) + pnl
            state["cash"] = state.get("cash", CONFIG["starting_capital"]) + pnl

            # Journal entry
            now = now_ist()
            append_trade_journal({
                "date": now.date().isoformat(),
                "symbol": CONFIG["symbol"],
                "direction": direction,
                "lot_size": self._lot_size,
                "ticket": ticket,
                "entry_time": pos.get("entry_time", ""),
                "exit_time": now.isoformat(),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "sl_price": pos.get("sl_price"),
                "target_price": pos.get("target_price"),
                "risk": pos.get("risk"),
                "gross_pnl": pnl,
                "net_pnl": pnl,
                "pnl_percent": round(pnl_pct, 2),
                "exit_reason": reason,
                "mode": self._mode,
            })

            state["current_position"] = None
            save_state(state)

            emoji = "🟢" if pnl >= 0 else "🔴"
            log.info(
                f"[ORDER EXEC] {emoji} {self._mode} {direction} EXIT ({reason}) | "
                f"Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f} | "
                f"P&L: ${pnl:+,.2f} | Day P&L: ${state['realized_pnl_today']:+,.2f}"
            )
            log_trades.info(
                f"[EXIT] {direction} ({reason}) | Entry: ${entry_price:.2f} | "
                f"Exit: ${exit_price:.2f} | P&L: ${pnl:+,.2f} | Mode: {self._mode}"
            )

            # Dispatch Telegram exit notification for all exits
            try:
                tg.trade_exit(
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl=pnl,
                    reason=reason,
                )
            except Exception as _tg_err:
                log.warning("[ORDERS] ⚠️ Telegram exit alert failed: %s", _tg_err)

            return True

    # ─────────────────────────────────────────
    # TRAILING STOP-LOSS (TSL) UPDATE
    # ─────────────────────────────────────────

    def update_tsl(
        self,
        state: Dict[str, Any],
        current_price: float,
    ) -> None:
        """
        Evaluate and update the Trailing Stop-Loss for the current position.

        TSL Logic:
          1. Calculate current R-multiple: (current_price - entry) / risk
          2. If R-multiple >= tsl_activation_rr (e.g. 1.0R), activate TSL
          3. Once active, trail SL at: peak_price - (risk * tsl_trail_rr_fraction)
          4. Floor: SL never drops below tsl_floor_rr * risk above entry (locked profit)
          5. If R-multiple >= max_target_rr (e.g. 4.0R), exit immediately

        Args:
            state:         mutable strategy state dict
            current_price: latest Gold price (bid for LONG, ask for SHORT)
        """
        if not state.get("in_position") or not state.get("current_position"):
            return

        if not CONFIG.get("use_tsl", False):
            return

        pos = state["current_position"]
        direction = pos["direction"]
        entry = pos["entry_price"]
        risk = pos.get("risk", 0)
        if risk <= 0:
            return

        # Calculate current R-multiple
        if direction == "LONG":
            r_multiple = (current_price - entry) / risk
        else:
            r_multiple = (entry - current_price) / risk

        # Check hard ceiling (max target)
        max_rr = CONFIG.get("max_target_rr", 1.5)
        if r_multiple >= max_rr:
            log.info(
                f"[TSL] 🎯 MAX TARGET HIT at {r_multiple:.2f}R (ceiling={max_rr}R). "
                f"Exiting at ${current_price:.2f}."
            )
            self.exit_trade(state, current_price, f"MAX_TARGET_{max_rr}R")
            return

        # Check if TSL should activate
        activation_rr = CONFIG.get("tsl_activation_rr", 1.2)
        floor_rr = CONFIG.get("tsl_floor_rr", 1.2)
        trail_fraction = CONFIG.get("tsl_trail_rr_fraction", 0.4)

        if r_multiple >= activation_rr and not pos.get("tsl_active"):
            pos["tsl_active"] = True
            log.info(
                f"[TSL] 🔄 TSL ACTIVATED at {r_multiple:.2f}R (threshold={activation_rr}R). "
                f"Floor locked at {floor_rr}R."
            )

        if not pos.get("tsl_active"):
            return

        # Update peak tracking
        if direction == "LONG":
            if current_price > pos.get("highest_price", entry):
                pos["highest_price"] = current_price
            peak = pos["highest_price"]
            trail_distance = risk * trail_fraction
            trail_sl = round(peak - trail_distance, 2)
            floor_reached = (peak - entry) >= risk * floor_rr
            floor_sl = round(entry + (risk * floor_rr), 2) if floor_reached else trail_sl
            new_sl = max(trail_sl, floor_sl)  # never below floor once reached
        else:
            if current_price < pos.get("lowest_price", entry):
                pos["lowest_price"] = current_price
            peak = pos["lowest_price"]
            trail_distance = risk * trail_fraction
            trail_sl = round(peak + trail_distance, 2)
            floor_reached = (entry - peak) >= risk * floor_rr
            floor_sl = round(entry - (risk * floor_rr), 2) if floor_reached else trail_sl
            new_sl = min(trail_sl, floor_sl)  # never above floor for SHORT once reached

        current_sl = pos.get("sl_price", 0)

        # Only move SL in the favorable direction
        should_update = False
        if direction == "LONG" and new_sl > current_sl:
            should_update = True
        elif direction == "SHORT" and new_sl < current_sl:
            should_update = True

        if should_update:
            old_sl = current_sl
            pos["sl_price"] = new_sl

            # Update broker-side SL in LIVE mode
            if self._mode == "LIVE" and pos.get("ticket"):
                self._broker.modify_position(
                    ticket=pos["ticket"],
                    new_sl=new_sl,
                    new_tp=pos.get("target_price", 0),
                )

            save_state(state)
            log.info(
                f"[TSL] 📈 SL TRAILED: ${old_sl:.2f} → ${new_sl:.2f} | "
                f"Peak=${peak:.2f} | R-multiple={r_multiple:.2f}R | "
                f"Trail distance=${trail_distance:.2f}"
            )

    # ─────────────────────────────────────────
    # EXTERNAL / MANUAL EXIT RECONCILIATION
    # ─────────────────────────────────────────

    def reconcile_external_exit(
        self,
        state: Dict[str, Any],
        tg: Optional[Any] = None,
    ) -> bool:
        """
        Check if the active position was closed externally (via MT5 Mobile App,
        Desktop terminal, or broker stop-out) and reconcile state immediately.

        Args:
            state: mutable strategy state dict
            tg:    optional TelegramAlerter instance to dispatch notifications

        Returns:
            True if an external exit was detected and state reconciled, False otherwise.
        """
        with trade_lock:
            if not state.get("in_position") or not state.get("current_position"):
                return False

            if self._mode != "LIVE":
                return False

            pos = state["current_position"]
            ticket = pos.get("ticket")
            if not ticket:
                return False

            # Check if position still exists on MT5 server
            live_pos = self._broker.get_position_by_ticket(ticket)
            if live_pos is not None:
                return False  # Position is still open

            # Position is GONE from MT5! Query deal history for exact exit details
            deals = self._broker.get_deals_by_position(ticket)
            exit_price = pos["entry_price"]
            profit = 0.0
            closing_deal = None

            if deals:
                # The closing deal is the last deal associated with the position
                closing_deal = deals[-1]
                exit_price = closing_deal.price
                profit = sum(d.profit for d in deals)

            # Determine intelligent exit reason (Broker SL, Broker TP, or Manual/External exit)
            exit_reason = "MANUAL_APP_EXIT"
            deal_reason = getattr(closing_deal, "reason", None) if closing_deal else None
            deal_comment = str(getattr(closing_deal, "comment", "")).lower() if closing_deal else ""

            sl_price = pos.get("sl_price")
            tp_price = pos.get("target_price")

            # MT5 deal reasons: 3 = DEAL_REASON_SL, 4 = DEAL_REASON_TP, 5 = DEAL_REASON_SO
            if deal_reason == 3 or "[sl]" in deal_comment or "sl" in deal_comment:
                exit_reason = "BROKER_SL_HIT"
            elif deal_reason == 4 or "[tp]" in deal_comment or "tp" in deal_comment:
                exit_reason = "BROKER_TP_HIT"
            elif sl_price is not None and (
                (pos["direction"] == "SHORT" and exit_price >= (sl_price - 1.00)) or
                (pos["direction"] == "LONG" and exit_price <= (sl_price + 1.00))
            ):
                exit_reason = "BROKER_SL_HIT"
            elif tp_price is not None and (
                (pos["direction"] == "SHORT" and exit_price <= (tp_price + 1.00)) or
                (pos["direction"] == "LONG" and exit_price >= (tp_price - 1.00))
            ):
                exit_reason = "BROKER_TP_HIT"

            # Reconcile strategy state
            state["in_position"] = False
            state["realized_pnl_today"] = state.get("realized_pnl_today", 0) + profit
            state["total_realized_pnl"] = state.get("total_realized_pnl", 0) + profit
            state["cash"] = state.get("cash", CONFIG["starting_capital"]) + profit

            # Journal entry
            now = now_ist()
            append_trade_journal({
                "date": now.date().isoformat(),
                "symbol": CONFIG["symbol"],
                "direction": pos["direction"],
                "lot_size": pos["lot_size"],
                "ticket": ticket,
                "entry_time": pos.get("entry_time", ""),
                "exit_time": now.isoformat(),
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "sl_price": pos.get("sl_price"),
                "target_price": pos.get("target_price"),
                "risk": pos.get("risk"),
                "gross_pnl": profit,
                "net_pnl": profit,
                "pnl_percent": round((profit / (pos.get("risk", 1.0) * pos["lot_size"] * 100)) * 100, 2) if pos.get("risk") else 0,
                "exit_reason": exit_reason,
                "mode": self._mode,
            })

            state["current_position"] = None
            save_state(state)

            emoji = "🟢" if profit >= 0 else "🔴"
            log.info(
                f"[SYNC] 🔔 External Exit Detected: Ticket {ticket} closed on MT5 ({exit_reason}) | "
                f"Entry: ${pos['entry_price']:.2f} → Exit: ${exit_price:.2f} | "
                f"P&L: {emoji} ${profit:+,.2f} | Day P&L: ${state['realized_pnl_today']:+,.2f}. "
                f"State synchronized! Resuming candle monitoring."
            )
            log_trades.info(
                f"[EXTERNAL EXIT] {pos['direction']} ({exit_reason}) | "
                f"Entry: ${pos['entry_price']:.2f} | Exit: ${exit_price:.2f} | P&L: ${profit:+,.2f}"
            )

            # Dispatch Telegram Notification
            if tg:
                try:
                    tg.trade_exit(
                        direction=pos["direction"],
                        entry_price=pos["entry_price"],
                        exit_price=exit_price,
                        pnl=profit,
                        reason=exit_reason,
                    )
                except Exception as e:
                    log.warning("[SYNC] ⚠️ Failed to send Telegram alert for external exit: %s", e)

            return True

    # ─────────────────────────────────────────
    # EXIT CONDITION CHECK (called from main loop)
    # ─────────────────────────────────────────

    def check_exit_conditions(
        self,
        state: Dict[str, Any],
        current_price: float,
    ) -> bool:
        """
        Check if the current position should be exited based on
        SL hit, TP hit, or TSL conditions.

        Args:
            state:         mutable strategy state dict
            current_price: latest Gold price

        Returns:
            True if position was exited, False if still open.
        """
        if not state.get("in_position") or not state.get("current_position"):
            return False

        pos = state["current_position"]
        direction = pos["direction"]
        sl = pos.get("sl_price", 0)
        tp = pos.get("target_price", 0)

        # Check SL hit (or trailed SL hit)
        if direction == "LONG" and current_price <= sl:
            exit_reason = "TSL_HIT" if pos.get("tsl_active") else "SL_HIT"
            log.info(f"[EXIT] 🛑 {exit_reason}: Price ${current_price:.2f} <= SL ${sl:.2f}")
            return self.exit_trade(state, current_price, exit_reason)

        if direction == "SHORT" and current_price >= sl:
            exit_reason = "TSL_HIT" if pos.get("tsl_active") else "SL_HIT"
            log.info(f"[EXIT] 🛑 {exit_reason}: Price ${current_price:.2f} >= SL ${sl:.2f}")
            return self.exit_trade(state, current_price, exit_reason)

        # In TSL mode: evaluate TSL activation, trailing, and max target ceiling
        if CONFIG.get("use_tsl", False):
            self.update_tsl(state, current_price)
            # update_tsl exits immediately if max target ceiling is reached
            return not state.get("in_position", False)

        # In Fixed Target mode (only if TSL is disabled)
        if direction == "LONG" and current_price >= tp:
            log.info(f"[EXIT] 🎯 TARGET HIT: Price ${current_price:.2f} >= TP ${tp:.2f}")
            return self.exit_trade(state, current_price, "TP_HIT")

        if direction == "SHORT" and current_price <= tp:
            log.info(f"[EXIT] 🎯 TARGET HIT: Price ${current_price:.2f} <= TP ${tp:.2f}")
            return self.exit_trade(state, current_price, "TP_HIT")

        return False
