"""
═════════════════════════════════════════════════════════════
  PATTERN — 3-Candle Reversal Detection (Gold H4)
═════════════════════════════════════════════════════════════

Pure-function pattern detection on standard 4-hour candles.
No side effects, no state — just takes candles in, returns a
signal (or None) out.

V3.0 — Color-Aware 6-Pattern Reversal Detection
─────────────────────────────────────────────────
Only reversals are valid: C1 and C3 must be DIFFERENT colors.
This eliminates same-color momentum patterns (e.g. 🟢🟢🟢, 🔴🔴🔴).

C2.open (not C2.close) must be inside C1's body, with a configurable
tolerance band (±TOLERANCE_PCT × C1.range) to handle real-world
near-miss opens.

LONG (Bullish) — C3 green, must close above body_top of BOTH C1 and C2:
  Pattern 1: 🔴🔴🟢  c3.close > c1.open     AND c3.close > c2.open
  Pattern 2: 🟢🔴🟢  c3.close > c1.close    AND c3.close > c2.open
  Pattern 3: 🔴🟢🟢  c3.close > c1.open     AND c3.close > c2.close

SHORT (Bearish) — C3 red, must close below body_bottom of BOTH C1 and C2:
  Pattern 4: 🟢🟢🔴  c3.close < c1.open     AND c3.close < c2.open
  Pattern 5: 🔴🟢🔴  c3.close < c1.close    AND c3.close < c2.open
  Pattern 6: 🟢🔴🔴  c3.close < c1.open     AND c3.close < c2.close

The C1/C2 field used in the C3 comparison is always the candle's body_top
(for LONG) or body_bottom (for SHORT). body_top resolves to Open for red
candles and Close for green candles; body_bottom is the inverse.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from config import CONFIG, log, log_pattern, log_candle_csv, now_ist


@dataclass
class SetupSignal:
    """Represents a validated 3-candle reversal signal."""
    setup_type: str        # "BULLISH" or "BEARISH"
    direction: str         # "LONG" or "SHORT"
    c1: Dict[str, Any]
    c2: Dict[str, Any]
    c3: Dict[str, Any]
    pattern_name: str = "" # e.g. "Pattern 1 (🔴🔴🟢)"


# ─────────────────────────────────────────────
# CANDLE HELPERS
# ─────────────────────────────────────────────

def is_bullish(candle: Dict[str, Any]) -> bool:
    """A candle is bullish when Close > Open."""
    return candle["close"] > candle["open"]


def is_bearish(candle: Dict[str, Any]) -> bool:
    """A candle is bearish when Close < Open."""
    return candle["close"] < candle["open"]


def body_top(candle: Dict[str, Any]) -> float:
    """Return the top of the candle body = max(Open, Close).

    Green candle → Close, Red candle → Open.
    """
    return max(candle["open"], candle["close"])


def body_bottom(candle: Dict[str, Any]) -> float:
    """Return the bottom of the candle body = min(Open, Close).

    Green candle → Open, Red candle → Close.
    """
    return min(candle["open"], candle["close"])


def _candle_color(c: Dict[str, Any]) -> str:
    """Return a human-readable color label for logging."""
    if is_bullish(c):
        return "🟢"
    elif is_bearish(c):
        return "🔴"
    return "⚪"


def _candle_summary(label: str, c: Dict[str, Any]) -> str:
    """One-line candle summary for log messages."""
    direction = "BULL" if is_bullish(c) else ("BEAR" if is_bearish(c) else "DOJI")
    body = abs(c["close"] - c["open"])
    full_range = c["high"] - c["low"]
    body_pct = (body / full_range * 100) if full_range > 0 else 0
    return (
        f"{label}: O=${c['open']:.2f} H=${c['high']:.2f} "
        f"L=${c['low']:.2f} C=${c['close']:.2f} "
        f"[{direction} | body=${body:.2f} ({body_pct:.0f}%) | range=${full_range:.2f}]"
    )


# ─────────────────────────────────────────────
# C2 BODY OVERLAPS C1 BODY — U-Shape Tolerance Check
# ─────────────────────────────────────────────

def _c2_body_overlaps_c1_body(c1: Dict[str, Any], c2: Dict[str, Any]) -> bool:
    """
    Check whether C2's body connects with C1's body (U-Shape validation).

    Valid if EITHER C2.open OR C2.close falls inside C1's body
    (with a configurable tolerance band clamped by a noise floor).

    This captures true U-shape absorption reversals where C2 gaps
    below/above C1 but closes back inside C1's body range.

    The check is:
      (body_bottom - tolerance) <= c2.open  <= (body_top + tolerance)  OR
      (body_bottom - tolerance) <= c2.close <= (body_top + tolerance)
    where tolerance = max(TOLERANCE_PCT × C1.range, MIN_TOLERANCE_PTS).
    """
    c1_bt = body_top(c1)
    c1_bb = body_bottom(c1)
    c1_range = c1["high"] - c1["low"]
    tolerance_pct = CONFIG.get("c2_open_tolerance_pct", 0.05)
    min_tolerance_pts = CONFIG.get("c2_open_min_tolerance_pts", 1.0)
    tolerance = max(c1_range * tolerance_pct, min_tolerance_pts)

    lower = c1_bb - tolerance
    upper = c1_bt + tolerance

    c2_open = c2["open"]
    c2_close = c2["close"]
    open_inside = lower <= c2_open <= upper
    close_inside = lower <= c2_close <= upper
    is_valid = open_inside or close_inside

    matched_on = "open" if open_inside else ("close" if close_inside else "neither")
    log_pattern.debug(
        "  C2 body overlap: c2.open=$%.2f c2.close=$%.2f | C1 body=[$%.2f - $%.2f] | "
        "tolerance=$%.2f (max(%.1f%% of range $%.2f, $%.1f floor)) | bounds=[$%.2f - $%.2f] | "
        "%s (matched on %s)",
        c2_open, c2_close, c1_bb, c1_bt,
        tolerance, tolerance_pct * 100, c1_range, min_tolerance_pts,
        lower, upper,
        "✓ INSIDE" if is_valid else "✗ OUTSIDE",
        matched_on,
    )
    return is_valid


# ─────────────────────────────────────────────
# BULLISH REVERSAL → Open LONG position
# ─────────────────────────────────────────────

def detect_bullish_setup(
    candles: List[Dict[str, Any]],
    log_csv: bool = False,
) -> Optional[SetupSignal]:
    """
    Bullish Reversal — Open LONG on Gold.

    V3.0 Color-Aware Detection (3 valid LONG patterns):
      Pattern 1: 🔴🔴🟢  c3.close > c1.open  AND c3.close > c2.open
      Pattern 2: 🟢🔴🟢  c3.close > c1.close AND c3.close > c2.open
      Pattern 3: 🔴🟢🟢  c3.close > c1.open  AND c3.close > c2.close

    In all cases:
      - C1 must be a DIFFERENT color from C3 (reversal, not continuation)
      - C2.open must be inside C1's body (with tolerance band)
      - C3 must be Green (Close > Open)
      - C3.close must be strictly above body_top of BOTH C1 and C2

    Args:
        candles: list of completed H4 candles, oldest→newest.
                 Must have at least 3 entries.

    Returns:
        SetupSignal if all conditions are met, else None.
    """
    if len(candles) < 3:
        log_pattern.debug("BULLISH check: insufficient candles (%d < 3)", len(candles))
        return None

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    # Log all three candles for analysis
    log_pattern.debug("── BULLISH REVERSAL CHECK ──")
    log_pattern.debug("  %s", _candle_summary("C1", c1))
    log_pattern.debug("  %s", _candle_summary("C2", c2))
    log_pattern.debug("  %s", _candle_summary("C3", c3))

    # ① C3 must be green (bullish)
    if not is_bullish(c3):
        log_pattern.debug(
            "  ✗ BULLISH FAIL: C3 is not green (Close=$%.2f <= Open=$%.2f)",
            c3["close"], c3["open"]
        )
        return None
    log_pattern.debug("  ✓ C3 is green")

    # ② C2.open must be inside C1's body (with tolerance band)
    if not _c2_body_overlaps_c1_body(c1, c2):
        log_pattern.debug("  ✗ BULLISH FAIL: C2 body does not overlap C1 body (with tolerance)")
        return None
    log_pattern.debug("  ✓ C2 body overlaps C1 body")

    # ③ Pattern matching — determine color combo and exclude all-same-color momentum
    c1_bt = body_top(c1)
    c2_bt = body_top(c2)

    c1_red = is_bearish(c1)
    c2_red = is_bearish(c2)
    c1_green = is_bullish(c1)
    c2_green = is_bullish(c2)

    # Exclude 🟢🟢🟢 (all-green momentum, not a reversal)
    if c1_green and c2_green:
        log_pattern.debug(
            "  ✗ BULLISH FAIL: 🟢🟢🟢 all-green momentum — not a reversal, excluded"
        )
        return None

    # Identify which pattern matched
    if c1_red and c2_red:
        pattern_label = "Pattern 1 (🔴🔴🟢)"
    elif c1_green and c2_red:
        pattern_label = "Pattern 2 (🟢🔴🟢)"
    elif c1_red and c2_green:
        pattern_label = "Pattern 3 (🔴🟢🟢)"
    else:
        log_pattern.debug("  ✗ BULLISH FAIL: C1/C2 color combo doesn't match any pattern")
        return None

    log_pattern.debug("  ✓ Color combo matches %s", pattern_label)

    # ④ C3.close must be strictly above body_top of BOTH C1 and C2
    if c3["close"] <= c1_bt:
        log_pattern.debug(
            "  ✗ BULLISH FAIL: C3.close ($%.2f) <= body_top(C1) ($%.2f) — need $%.2f more",
            c3["close"], c1_bt, c1_bt - c3["close"] + 0.05
        )
        return None
    log_pattern.debug("  ✓ C3.close ($%.2f) > body_top(C1) ($%.2f)", c3["close"], c1_bt)

    if c3["close"] <= c2_bt:
        log_pattern.debug(
            "  ✗ BULLISH FAIL: C3.close ($%.2f) <= body_top(C2) ($%.2f) — need $%.2f more",
            c3["close"], c2_bt, c2_bt - c3["close"] + 0.05
        )
        return None
    log_pattern.debug("  ✓ C3.close ($%.2f) > body_top(C2) ($%.2f)", c3["close"], c2_bt)

    # ✅ All conditions met
    combo = f"{_candle_color(c1)}{_candle_color(c2)}{_candle_color(c3)}"
    log.info(
        f"[SIGNAL] 🎯 BULLISH REVERSAL DETECTED! Sequence: {pattern_label} | {combo} completed. "
        f"C3 closed above both C1 and C2 body highs. Preparing to open LONG."
    )
    log.info(
        f"[CANDLE EVAL]    C1: O=${c1['open']:.2f}, H=${c1['high']:.2f}, "
        f"L=${c1['low']:.2f}, C=${c1['close']:.2f} "
        f"[{_candle_color(c1)} Body Top=${c1_bt:.2f}]"
    )
    log.info(
        f"[CANDLE EVAL]    C2: O=${c2['open']:.2f}, H=${c2['high']:.2f}, "
        f"L=${c2['low']:.2f}, C=${c2['close']:.2f} "
        f"[{_candle_color(c2)} Body Top=${c2_bt:.2f}]"
    )
    log.info(
        f"[CANDLE EVAL]    C3: O=${c3['open']:.2f}, H=${c3['high']:.2f}, "
        f"L=${c3['low']:.2f}, C=${c3['close']:.2f} "
        f"[🟢 C>BodyTop(C1) (${c1_bt:.2f}) & C>BodyTop(C2) (${c2_bt:.2f}) ✓]"
    )

    # Signal audit trail
    log.info(
        "[SIGNAL] 📋 SIGNAL AUDIT | signal_timestamp=%s | pattern=%s | "
        "c1_date=%s (c1_bt=$%.2f) | c2_date=%s (c2_bt=$%.2f) | c3_date=%s | "
        "data_source=H4_CONTINUOUS",
        now_ist(), pattern_label,
        c1.get('date'), c1_bt, c2.get('date'), c2_bt, c3.get('date')
    )

    # Log candles to CSV for backtesting
    if log_csv:
        symbol = CONFIG["symbol"]
        log_candle_csv("SPOT_SIGNAL", symbol, c1)
        log_candle_csv("SPOT_SIGNAL", symbol, c2)
        log_candle_csv("SPOT_SIGNAL", symbol, c3)

    return SetupSignal(
        setup_type="BULLISH",
        direction="LONG",
        c1=c1,
        c2=c2,
        c3=c3,
        pattern_name=pattern_label,
    )


# ─────────────────────────────────────────────
# BEARISH REVERSAL → Open SHORT position
# ─────────────────────────────────────────────

def detect_bearish_setup(
    candles: List[Dict[str, Any]],
    log_csv: bool = False,
) -> Optional[SetupSignal]:
    """
    Bearish Reversal — Open SHORT on Gold.

    V3.0 Color-Aware Detection (3 valid SHORT patterns):
      Pattern 4: 🟢🟢🔴  c3.close < c1.open  AND c3.close < c2.open
      Pattern 5: 🔴🟢🔴  c3.close < c1.close AND c3.close < c2.open
      Pattern 6: 🟢🔴🔴  c3.close < c1.open  AND c3.close < c2.close

    In all cases:
      - At least one of C1/C2 must differ in color from C3 (reversal, not momentum)
      - C2.open must be inside C1's body (with tolerance band)
      - C3 must be Red (Close < Open)
      - C3.close must be strictly below body_bottom of BOTH C1 and C2

    Args:
        candles: list of completed H4 candles, oldest→newest.
                 Must have at least 3 entries.

    Returns:
        SetupSignal if all conditions are met, else None.
    """
    if len(candles) < 3:
        log_pattern.debug("BEARISH check: insufficient candles (%d < 3)", len(candles))
        return None

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    # Log all three candles for analysis
    log_pattern.debug("── BEARISH REVERSAL CHECK ──")
    log_pattern.debug("  %s", _candle_summary("C1", c1))
    log_pattern.debug("  %s", _candle_summary("C2", c2))
    log_pattern.debug("  %s", _candle_summary("C3", c3))

    # ① C3 must be red (bearish)
    if not is_bearish(c3):
        log_pattern.debug(
            "  ✗ BEARISH FAIL: C3 is not red (Close=$%.2f >= Open=$%.2f)",
            c3["close"], c3["open"]
        )
        return None
    log_pattern.debug("  ✓ C3 is red")

    # ② C2.open must be inside C1's body (with tolerance band)
    if not _c2_body_overlaps_c1_body(c1, c2):
        log_pattern.debug("  ✗ BEARISH FAIL: C2 body does not overlap C1 body (with tolerance)")
        return None
    log_pattern.debug("  ✓ C2 body overlaps C1 body")

    # ③ Pattern matching — determine color combo and exclude all-same-color momentum
    c1_red = is_bearish(c1)
    c2_red = is_bearish(c2)
    c1_green = is_bullish(c1)
    c2_green = is_bullish(c2)

    # Exclude 🔴🔴🔴 (all-red momentum, not a reversal)
    if c1_red and c2_red:
        log_pattern.debug(
            "  ✗ BEARISH FAIL: 🔴🔴🔴 all-red momentum — not a reversal, excluded"
        )
        return None

    # Identify which pattern matched
    if c1_green and c2_green:
        pattern_label = "Pattern 4 (🟢🟢🔴)"
    elif c1_red and c2_green:
        pattern_label = "Pattern 5 (🔴🟢🔴)"
    elif c1_green and c2_red:
        pattern_label = "Pattern 6 (🟢🔴🔴)"
    else:
        log_pattern.debug("  ✗ BEARISH FAIL: C1/C2 color combo doesn't match any pattern")
        return None

    log_pattern.debug("  ✓ Color combo matches %s", pattern_label)

    # ④ C3.close must be strictly below body_bottom of BOTH C1 and C2
    c1_bb = body_bottom(c1)
    c2_bb = body_bottom(c2)

    if c3["close"] >= c1_bb:
        log_pattern.debug(
            "  ✗ BEARISH FAIL: C3.close ($%.2f) >= body_bottom(C1) ($%.2f) — need $%.2f more drop",
            c3["close"], c1_bb, c3["close"] - c1_bb + 0.05
        )
        return None
    log_pattern.debug("  ✓ C3.close ($%.2f) < body_bottom(C1) ($%.2f)", c3["close"], c1_bb)

    if c3["close"] >= c2_bb:
        log_pattern.debug(
            "  ✗ BEARISH FAIL: C3.close ($%.2f) >= body_bottom(C2) ($%.2f) — need $%.2f more drop",
            c3["close"], c2_bb, c3["close"] - c2_bb + 0.05
        )
        return None
    log_pattern.debug("  ✓ C3.close ($%.2f) < body_bottom(C2) ($%.2f)", c3["close"], c2_bb)

    # ✅ All conditions met
    combo = f"{_candle_color(c1)}{_candle_color(c2)}{_candle_color(c3)}"
    log.info(
        f"[SIGNAL] 🎯 BEARISH REVERSAL DETECTED! Sequence: {pattern_label} | {combo} completed. "
        f"C3 closed below both C1 and C2 body lows. Preparing to open SHORT."
    )
    log.info(
        f"[CANDLE EVAL]    C1: O=${c1['open']:.2f}, H=${c1['high']:.2f}, "
        f"L=${c1['low']:.2f}, C=${c1['close']:.2f} "
        f"[{_candle_color(c1)} Body Bottom=${c1_bb:.2f}]"
    )
    log.info(
        f"[CANDLE EVAL]    C2: O=${c2['open']:.2f}, H=${c2['high']:.2f}, "
        f"L=${c2['low']:.2f}, C=${c2['close']:.2f} "
        f"[{_candle_color(c2)} Body Bottom=${c2_bb:.2f}]"
    )
    log.info(
        f"[CANDLE EVAL]    C3: O=${c3['open']:.2f}, H=${c3['high']:.2f}, "
        f"L=${c3['low']:.2f}, C=${c3['close']:.2f} "
        f"[🔴 C<BodyBottom(C1) (${c1_bb:.2f}) & C<BodyBottom(C2) (${c2_bb:.2f}) ✓]"
    )

    # Signal audit trail
    log.info(
        "[SIGNAL] 📋 SIGNAL AUDIT | signal_timestamp=%s | pattern=%s | "
        "c1_date=%s (c1_bb=$%.2f) | c2_date=%s (c2_bb=$%.2f) | c3_date=%s | "
        "data_source=H4_CONTINUOUS",
        now_ist(), pattern_label,
        c1.get('date'), c1_bb, c2.get('date'), c2_bb, c3.get('date')
    )

    # Log candles to CSV for backtesting
    if log_csv:
        symbol = CONFIG["symbol"]
        log_candle_csv("SPOT_SIGNAL", symbol, c1)
        log_candle_csv("SPOT_SIGNAL", symbol, c2)
        log_candle_csv("SPOT_SIGNAL", symbol, c3)

    return SetupSignal(
        setup_type="BEARISH",
        direction="SHORT",
        c1=c1,
        c2=c2,
        c3=c3,
        pattern_name=pattern_label,
    )


# ─────────────────────────────────────────────
# CONVENIENCE: scan for either setup
# ─────────────────────────────────────────────

def detect_setup(
    candles: List[Dict[str, Any]],
    log_csv: bool = False,
) -> Optional[SetupSignal]:
    """
    Scan the most recent 3 completed H4 candles for either a bullish
    or bearish reversal.  Returns whichever fires first (bearish
    checked first for no particular reason), or None.
    """
    signal = detect_bearish_setup(candles, log_csv=log_csv)
    if signal is not None:
        return signal
    return detect_bullish_setup(candles, log_csv=log_csv)


# ─────────────────────────────────────────────
# FORENSIC AUDIT & PROSPECTIVE SUMMARY HELPERS
# ─────────────────────────────────────────────

def format_pattern_eval_summary(
    candles: List[Dict[str, Any]],
    signal: Optional[SetupSignal],
) -> str:
    """
    Format a clean forensic checklist summary of the 3 evaluated candles
    and the pattern checks, suitable for logging at INFO level.
    """
    if len(candles) < 3:
        return "[PATTERN EVAL] Insufficient candles for evaluation (<3)."

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    c1_c = _candle_color(c1)
    c2_c = _candle_color(c2)
    c3_c = _candle_color(c3)
    c1_bb, c1_bt = body_bottom(c1), body_top(c1)
    c2_bb, c2_bt = body_bottom(c2), body_top(c2)

    c1_date = c1.get("date")
    c1_time_str = c1_date.strftime("%m-%d %H:%M") if hasattr(c1_date, "strftime") else str(c1_date)
    c2_date = c2.get("date")
    c2_time_str = c2_date.strftime("%m-%d %H:%M") if hasattr(c2_date, "strftime") else str(c2_date)
    c3_date = c3.get("date")
    c3_time_str = c3_date.strftime("%m-%d %H:%M") if hasattr(c3_date, "strftime") else str(c3_date)

    lines = [
        "════════════════════════════════════════════════════════════════════════════════",
        f"[PATTERN EVAL] 🔍 3-Candle Reversal Audit | Sequence: {c1_c}{c2_c}{c3_c}",
        f"  • C1 ({c1_time_str} IST): O=${c1['open']:.2f} H=${c1['high']:.2f} L=${c1['low']:.2f} C=${c1['close']:.2f} [{c1_c} Body: ${c1_bb:.2f}-${c1_bt:.2f}]",
        f"  • C2 ({c2_time_str} IST): O=${c2['open']:.2f} H=${c2['high']:.2f} L=${c2['low']:.2f} C=${c2['close']:.2f} [{c2_c} Body: ${c2_bb:.2f}-${c2_bt:.2f}]",
        f"  • C3 ({c3_time_str} IST): O=${c3['open']:.2f} H=${c3['high']:.2f} L=${c3['low']:.2f} C=${c3['close']:.2f} [{c3_c}]",
    ]

    if signal:
        lines.append(f"  🎯 RESULT: VALID REVERSAL SIGNAL DETECTED! -> {signal.pattern_name} ({signal.direction})")
    else:
        # Determine failure reason
        reasons = []
        if is_bullish(c1) and is_bullish(c2) and is_bullish(c3):
            reasons.append("🟢🟢🟢 All-green momentum continuation (not a reversal)")
        elif is_bearish(c1) and is_bearish(c2) and is_bearish(c3):
            reasons.append("🔴🔴🔴 All-red momentum continuation (not a reversal)")
        else:
            # Check C2 inside C1 body tolerance
            c1_range = c1["high"] - c1["low"]
            tol = max(c1_range * CONFIG.get("c2_open_tolerance_pct", 0.05), CONFIG.get("c2_open_min_tolerance_pts", 1.0))
            c2_open_inside = (c1_bb - tol) <= c2["open"] <= (c1_bt + tol)
            c2_close_inside = (c1_bb - tol) <= c2["close"] <= (c1_bt + tol)
            if not (c2_open_inside or c2_close_inside):
                reasons.append(f"C2 body (open=${c2['open']:.2f}, close=${c2['close']:.2f}) outside C1 body bounds [${c1_bb-tol:.2f} - ${c1_bt+tol:.2f}]")
            else:
                if is_bullish(c3):
                    if is_bullish(c1) and is_bullish(c2):
                        reasons.append("🟢🟢🟢 All-green momentum continuation")
                    else:
                        if c3["close"] <= c1_bt:
                            reasons.append(f"C3.close (${c3['close']:.2f}) <= C1 body top (${c1_bt:.2f})")
                        if c3["close"] <= c2_bt:
                            reasons.append(f"C3.close (${c3['close']:.2f}) <= C2 body top (${c2_bt:.2f})")
                elif is_bearish(c3):
                    if is_bearish(c1) and is_bearish(c2):
                        reasons.append("🔴🔴🔴 All-red momentum continuation")
                    else:
                        if c3["close"] >= c1_bb:
                            reasons.append(f"C3.close (${c3['close']:.2f}) >= C1 body bottom (${c1_bb:.2f})")
                        if c3["close"] >= c2_bb:
                            reasons.append(f"C3.close (${c3['close']:.2f}) >= C2 body bottom (${c2_bb:.2f})")
                else:
                    reasons.append("C3 is Doji (Open == Close)")

        reason_str = "; ".join(reasons) if reasons else "Pattern criteria not satisfied"
        lines.append(f"  ❌ RESULT: NO SETUP -> Reason: {reason_str}")

    lines.append("════════════════════════════════════════════════════════════════════════════════")
    return "\n".join(lines)


def evaluate_prospective_setup(
    forming_candle: Dict[str, Any],
    completed_candles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Evaluate whether the currently forming candle (as prospective C3)
    would trigger a 3-candle reversal if it closes at its current price.

    Args:
        forming_candle: snapshot dict of forming bar (must have open, high, low, close)
        completed_candles: list of completed bars (last 2 will be C1 and C2)

    Returns:
        Dict with keys:
          - is_candidate: bool
          - direction: "LONG", "SHORT", or ""
          - pattern_name: str
          - message: str describing the prospective status
    """
    if len(completed_candles) < 2 or not forming_candle:
        return {"is_candidate": False, "direction": "", "pattern_name": "", "message": "Insufficient bars"}

    c1, c2, c3 = completed_candles[-2], completed_candles[-1], forming_candle
    c1_bb, c1_bt = body_bottom(c1), body_top(c1)
    c2_bb, c2_bt = body_bottom(c2), body_top(c2)

    c1_red = is_bearish(c1)
    c1_green = is_bullish(c1)
    c2_red = is_bearish(c2)
    c2_green = is_bullish(c2)
    c3_green = is_bullish(c3)
    c3_red = is_bearish(c3)

    c1_range = c1["high"] - c1["low"]
    tol = max(c1_range * CONFIG.get("c2_open_tolerance_pct", 0.05), CONFIG.get("c2_open_min_tolerance_pts", 1.0))
    c2_open_inside = (c1_bb - tol) <= c2["open"] <= (c1_bt + tol)
    c2_close_inside = (c1_bb - tol) <= c2["close"] <= (c1_bt + tol)
    c2_inside = c2_open_inside or c2_close_inside

    if not c2_inside:
        return {
            "is_candidate": False,
            "direction": "",
            "pattern_name": "",
            "message": f"Normal continuation (C2 body outside C1 tolerance band)"
        }

    # Prospective Bullish Check (Patterns 1, 2, 3 — excludes 🟢🟢🟢)
    if c3_green and not (c1_green and c2_green):
        needed = max(c1_bt, c2_bt)
        if c3["close"] > needed:
            return {
                "is_candidate": True,
                "direction": "LONG",
                "pattern_name": "Prospective Bullish Reversal",
                "message": f"🎯 PROSPECTIVE BULLISH REVERSAL! C3 is currently above body tops (${c3['close']:.2f} > ${needed:.2f})."
            }
        else:
            diff = needed - c3["close"]
            return {
                "is_candidate": False,
                "direction": "LONG",
                "pattern_name": "Near Bullish Setup",
                "message": f"Bullish structure forming, but C3 needs +${diff:.2f} more gain to close above ${needed:.2f}."
            }

    # Prospective Bearish Check (Patterns 4, 5, 6 — excludes 🔴🔴🔴)
    if c3_red and not (c1_red and c2_red):
        needed = min(c1_bb, c2_bb)
        if c3["close"] < needed:
            return {
                "is_candidate": True,
                "direction": "SHORT",
                "pattern_name": "Prospective Bearish Reversal",
                "message": f"🎯 PROSPECTIVE BEARISH REVERSAL! C3 is currently below body bottoms (${c3['close']:.2f} < ${needed:.2f})."
            }
        else:
            diff = c3["close"] - needed
            return {
                "is_candidate": False,
                "direction": "SHORT",
                "pattern_name": "Near Bearish Setup",
                "message": f"Bearish structure forming, but C3 needs -${diff:.2f} more drop to close below ${needed:.2f}."
            }

    return {
        "is_candidate": False,
        "direction": "",
        "pattern_name": "",
        "message": "Market consolidation/continuation (no reversal pattern prospective)."
    }
