"""
═════════════════════════════════════════════════════════════
  RISK MANAGEMENT — Gold 4-Hour Reversal Strategy (MT5)
═════════════════════════════════════════════════════════════

Core responsibilities:
  1. Calculate initial Stop-Loss and Take-Profit based on the 3-candle
     reversal formation extremes (C3 high/low) with a configurable buffer.
  2. Perform pre-trade risk sanity checks (e.g. inverted SL from gap down/up,
     excessive risk distance exceeding threshold).
  3. Calculate dollar P&L and R-multiples for Gold (XAU/USD) positions.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any

from config import CONFIG, log, log_risk


# ─────────────────────────────────────────────
# RISK PARAMETERS DATA STRUCTURE
# ─────────────────────────────────────────────

@dataclass
class RiskParams:
    """Calculated stop loss, profit target, and risk metrics for a trade."""
    direction: str               # "LONG" or "SHORT"
    entry_price: float           # Anticipated or actual entry fill price
    sl_price: float              # Stop-loss price
    target_price: float          # Take-profit target price
    risk: float                  # Dollar risk distance per oz (abs(entry - sl))
    is_valid: bool = True        # False if trade fails sanity/gap checks
    skip_reason: str = ""        # Descriptive reason if is_valid is False
    rr_ratio: float = 2.0        # Risk-to-reward ratio used
    max_target_price: float = 0.0 # Maximum ceiling target for TSL (e.g. 4.0R)


# ─────────────────────────────────────────────
# SL / TARGET CALCULATION
# ─────────────────────────────────────────────

def calculate_sl_target(
    entry_price: float,
    signal: Any,
) -> RiskParams:
    """
    Calculate Stop-Loss and Take-Profit target from the 3-candle setup.

    Rules:
      - LONG:
          SL     = C3.low - buffer_dollars
          Risk   = entry_price - SL
          Target = entry_price + (Risk * rr_ratio)
      - SHORT:
          SL     = C3.high + buffer_dollars
          Risk   = SL - entry_price
          Target = entry_price - (Risk * rr_ratio)

    Sanity Guards:
      - Inverted SL: For LONG, reject if SL >= entry. For SHORT, reject if SL <= entry.
      - Max Risk %: Reject if risk exceeds max_risk_pct of entry price (default 3%).
    """
    buffer = float(CONFIG.get("sl_buffer_dollars", 0.50))
    rr = float(CONFIG.get("rr_ratio", 2.0))
    max_target_rr = float(CONFIG.get("max_target_rr", 4.0))
    max_risk_pct = float(CONFIG.get("max_risk_pct", 0.03))  # 3% threshold

    # Extract direction and C3 bar
    direction = getattr(signal, "direction", None) or signal.get("direction", "")
    direction = str(direction).upper()

    c3 = getattr(signal, "c3", None) or (signal.get("c3") if isinstance(signal, dict) else None)
    if not c3 or "low" not in c3 or "high" not in c3:
        log_risk.error("[RISK] ❌ Signal missing valid C3 candle: %s", signal)
        return RiskParams(
            direction=direction,
            entry_price=entry_price,
            sl_price=0.0,
            target_price=0.0,
            risk=0.0,
            is_valid=False,
            skip_reason="Signal missing C3 candle data",
        )

    if direction == "LONG":
        formation_low = float(c3["low"])
        sl_price = round(formation_low - buffer, 2)
        risk = round(entry_price - sl_price, 2)
        tp_price = round(entry_price + (risk * rr), 2)
        max_target_price = round(entry_price + (risk * max_target_rr), 2)

        # Sanity Guard 1: Inverted SL (gap or fast move past SL)
        if sl_price >= entry_price or risk <= 0:
            skip_msg = f"Inverted risk: SL (${sl_price:.2f}) >= entry (${entry_price:.2f})"
            log_risk.warning("[RISK] ⚠️ %s", skip_msg)
            return RiskParams(
                direction=direction,
                entry_price=entry_price,
                sl_price=sl_price,
                target_price=tp_price,
                risk=risk,
                is_valid=False,
                skip_reason=skip_msg,
                rr_ratio=rr,
                max_target_price=max_target_price,
            )

        # Sanity Guard 2: Excessive risk %
        if risk > entry_price * max_risk_pct:
            skip_msg = (
                f"Risk (${risk:.2f}) exceeds {max_risk_pct*100:.1f}% limit of "
                f"entry (${entry_price * max_risk_pct:.2f})"
            )
            log_risk.warning("[RISK] ⚠️ %s", skip_msg)
            return RiskParams(
                direction=direction,
                entry_price=entry_price,
                sl_price=sl_price,
                target_price=tp_price,
                risk=risk,
                is_valid=False,
                skip_reason=skip_msg,
                rr_ratio=rr,
                max_target_price=max_target_price,
            )

    elif direction == "SHORT":
        formation_high = float(c3["high"])
        sl_price = round(formation_high + buffer, 2)
        risk = round(sl_price - entry_price, 2)
        tp_price = round(entry_price - (risk * rr), 2)
        max_target_price = round(entry_price - (risk * max_target_rr), 2)

        # Sanity Guard 1: Inverted SL (gap or fast move past SL)
        if sl_price <= entry_price or risk <= 0:
            skip_msg = f"Inverted risk: SL (${sl_price:.2f}) <= entry (${entry_price:.2f})"
            log_risk.warning("[RISK] ⚠️ %s", skip_msg)
            return RiskParams(
                direction=direction,
                entry_price=entry_price,
                sl_price=sl_price,
                target_price=tp_price,
                risk=risk,
                is_valid=False,
                skip_reason=skip_msg,
                rr_ratio=rr,
                max_target_price=max_target_price,
            )

        # Sanity Guard 2: Excessive risk %
        if risk > entry_price * max_risk_pct:
            skip_msg = (
                f"Risk (${risk:.2f}) exceeds {max_risk_pct*100:.1f}% limit of "
                f"entry (${entry_price * max_risk_pct:.2f})"
            )
            log_risk.warning("[RISK] ⚠️ %s", skip_msg)
            return RiskParams(
                direction=direction,
                entry_price=entry_price,
                sl_price=sl_price,
                target_price=tp_price,
                risk=risk,
                is_valid=False,
                skip_reason=skip_msg,
                rr_ratio=rr,
                max_target_price=max_target_price,
            )

    else:
        log_risk.error("[RISK] ❌ Unknown direction: %s", direction)
        return RiskParams(
            direction=direction,
            entry_price=entry_price,
            sl_price=0.0,
            target_price=0.0,
            risk=0.0,
            is_valid=False,
            skip_reason=f"Unknown trade direction: '{direction}'",
        )

    log_risk.info(
        "[RISK] ✅ Setup validated: %s | Entry: $%.2f | SL: $%.2f | TP: $%.2f | "
        "Risk: $%.2f (%.2f%%) | R:R: 1:%.1f | MaxTP: $%.2f",
        direction, entry_price, sl_price, tp_price, risk, (risk / entry_price) * 100, rr, max_target_price
    )

    return RiskParams(
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        target_price=tp_price,
        risk=risk,
        is_valid=True,
        skip_reason="",
        rr_ratio=rr,
        max_target_price=max_target_price,
    )


# ─────────────────────────────────────────────
# DOLLAR P&L AND R-MULTIPLE CALCULATIONS
# ─────────────────────────────────────────────

def calculate_dollar_pnl(
    direction: str,
    entry_price: float,
    exit_price: float,
    lot_size: float = 0.01,
    contract_size: float = 100.0,
) -> float:
    """
    Calculate gross dollar P&L for a Gold (XAU/USD) trade.

    Standard Contract: 1.00 lot = 100 oz of gold ($100 per $1 price move).
    Micro Contract:    0.01 lot = 1 oz of gold ($1 per $1 price move).

    Formula:
      LONG:  (exit_price - entry_price) * lot_size * contract_size
      SHORT: (entry_price - exit_price) * lot_size * contract_size
    """
    d = direction.upper()
    if d == "LONG":
        pnl = (exit_price - entry_price) * lot_size * contract_size
    elif d == "SHORT":
        pnl = (entry_price - exit_price) * lot_size * contract_size
    else:
        pnl = 0.0
    return round(pnl, 2)


def calculate_r_multiple(
    direction: str,
    entry_price: float,
    current_price: float,
    risk: float,
) -> float:
    """
    Calculate the current R-multiple for an active position.

    Formula:
      LONG:  (current_price - entry_price) / risk
      SHORT: (entry_price - current_price) / risk
    """
    if risk <= 0:
        return 0.0
    d = direction.upper()
    if d == "LONG":
        return round((current_price - entry_price) / risk, 2)
    elif d == "SHORT":
        return round((entry_price - current_price) / risk, 2)
    return 0.0
