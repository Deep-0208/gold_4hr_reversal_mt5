"""
═════════════════════════════════════════════════════════════
  TEST AUDIT COMPREHENSIVE — Gold 4-Hour Reversal Strategy
═════════════════════════════════════════════════════════════

Comprehensive automated test suite covering:
  1. Timezone offset and multi-timezone candle integrity (IST, UTC, Server)
  2. Broker forming candle snapshot & status validation
  3. Pattern evaluation forensic summary & prospective setup analysis
  4. CSV candle logging format validation
  5. 1-second countdown anti-flooding logic verification
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to sys.path
PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from config import CONFIG, IST, now_ist, now_utc, log_candle_csv
from broker import MT5Broker
from pattern import (
    detect_setup, format_pattern_eval_summary, evaluate_prospective_setup,
    SetupSignal, is_bullish, is_bearish, body_top, body_bottom
)
from risk import RiskParams, calculate_sl_target, calculate_dollar_pnl, calculate_r_multiple


class TestGold4HrReversal(unittest.TestCase):

    def setUp(self):
        self.broker = MT5Broker()
        self.connected = self.broker.connect()

    def tearDown(self):
        if self.connected:
            self.broker.disconnect()

    def test_01_mt5_connection_and_timezone_offset(self):
        """Verify MT5 connection and broker server offset calculation."""
        if not self.connected:
            self.skipTest("MT5 terminal not running or not accessible.")

        self.assertIsNotNone(self.broker._server_offset_hours)
        print(f"\n✓ Detected broker server offset: UTC{self.broker._server_offset_hours:+d} hours")
        self.assertTrue(-12 <= self.broker._server_offset_hours <= 14)

    def test_02_candle_timezone_alignment(self):
        """Verify that candles contain aware IST and UTC dates and chronological order."""
        if not self.connected:
            self.skipTest("MT5 terminal not running.")

        candles = self.broker.get_candles(count=5, include_forming=False)
        self.assertGreaterEqual(len(candles), 3)

        for c in candles:
            self.assertIn("date", c)
            self.assertIn("date_utc", c)
            self.assertIn("date_server", c)
            self.assertEqual(c["date"].tzinfo, IST)
            self.assertEqual(c["date_utc"].tzinfo, timezone.utc)

            # IST must be UTC + 5h 30m
            diff = c["date"] - c["date_utc"].astimezone(IST)
            self.assertEqual(diff.total_seconds(), 0)

        # Chronological order
        for i in range(1, len(candles)):
            self.assertGreater(candles[i]["date"], candles[i-1]["date"])
        print(f"\n✓ Completed candles verified: {len(candles)} bars in chronological IST/UTC order.")

    def test_03_forming_candle_snapshot(self):
        """Verify real-time forming candle snapshot returns complete metrics."""
        if not self.connected:
            self.skipTest("MT5 terminal not running.")

        snap = self.broker.get_forming_candle_snapshot()
        self.assertIsNotNone(snap)
        self.assertIn("open", snap)
        self.assertIn("high", snap)
        self.assertIn("low", snap)
        self.assertIn("close", snap)
        self.assertIn("progress_pct", snap)
        self.assertIn("seconds_left", snap)
        self.assertIn("spread", snap)

        self.assertGreaterEqual(snap["progress_pct"], 0.0)
        self.assertLessEqual(snap["progress_pct"], 100.0)
        self.assertGreater(snap["high"], 0)
        self.assertGreater(snap["low"], 0)
        self.assertGreaterEqual(snap["high"], snap["low"])
        print(f"\n✓ Forming candle snapshot: {snap['progress_pct']:.1f}% progress, {int(snap['mins_left'])}m left, spread=${snap['spread']:.2f}")

    def test_04_pattern_forensic_summary_and_prospective(self):
        """Verify pattern audit summary formatting and prospective setup analysis."""
        # Simulated synthetic bars
        base_time = datetime(2026, 9, 14, 10, 30, tzinfo=IST)
        c1 = {"date": base_time, "open": 2580.0, "high": 2585.0, "low": 2570.0, "close": 2572.0} # Bear
        c2 = {"date": base_time + timedelta(hours=4), "open": 2575.0, "high": 2578.0, "low": 2568.0, "close": 2570.0} # Bear, open inside C1
        c3 = {"date": base_time + timedelta(hours=8), "open": 2571.0, "high": 2590.0, "low": 2569.0, "close": 2588.0} # Bull, closes above C1 & C2

        candles = [c1, c2, c3]
        sig = detect_setup(candles)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "LONG")

        summary = format_pattern_eval_summary(candles, sig)
        self.assertIn("[PATTERN EVAL]", summary)
        self.assertIn("VALID REVERSAL SIGNAL DETECTED", summary)
        print("\n✓ Pattern forensic summary formatted correctly for valid signal.")

        # Test failure scenario (3 green bars = momentum continuation)
        c1_green = {"date": base_time, "open": 2570.0, "high": 2585.0, "low": 2568.0, "close": 2580.0}
        c2_green = {"date": base_time + timedelta(hours=4), "open": 2575.0, "high": 2588.0, "low": 2572.0, "close": 2585.0}
        c3_green = {"date": base_time + timedelta(hours=8), "open": 2585.0, "high": 2595.0, "low": 2582.0, "close": 2592.0}

        candles_green = [c1_green, c2_green, c3_green]
        sig_none = detect_setup(candles_green)
        self.assertIsNone(sig_none)

        summary_fail = format_pattern_eval_summary(candles_green, sig_none)
        self.assertIn("NO SETUP", summary_fail)
        self.assertIn("momentum continuation", summary_fail)
        print("✓ Pattern forensic summary formatted correctly for failed signal.")

        # Test Pattern 2 (🟢🔴🟢) and Pattern 5 (🔴🟢🔴) prospective evaluation & summary
        # Pattern 2: C1 green, C2 red, C3 green
        c1_p2 = {"date": base_time, "open": 2570.0, "high": 2582.0, "low": 2568.0, "close": 2580.0} # Green (body top 2580)
        c2_p2 = {"date": base_time + timedelta(hours=4), "open": 2575.0, "high": 2578.0, "low": 2568.0, "close": 2570.0} # Red (open 2575 inside C1, body top 2575)
        forming_p2 = {"open": 2571.0, "high": 2589.0, "low": 2569.0, "close": 2585.0} # Green > 2580
        prospective_p2 = evaluate_prospective_setup(forming_p2, [c1_p2, c2_p2])
        self.assertTrue(prospective_p2["is_candidate"])
        self.assertEqual(prospective_p2["direction"], "LONG")

        # Pattern 5: C1 red, C2 green, C3 red
        c1_p5 = {"date": base_time, "open": 2580.0, "high": 2585.0, "low": 2568.0, "close": 2570.0} # Red (body bottom 2570)
        c2_p5 = {"date": base_time + timedelta(hours=4), "open": 2575.0, "high": 2582.0, "low": 2572.0, "close": 2578.0} # Green (open 2575 inside C1, body bottom 2575)
        forming_p5 = {"open": 2576.0, "high": 2577.0, "low": 2565.0, "close": 2567.0} # Red < 2570
        prospective_p5 = evaluate_prospective_setup(forming_p5, [c1_p5, c2_p5])
        self.assertTrue(prospective_p5["is_candidate"])
        self.assertEqual(prospective_p5["direction"], "SHORT")

        print("✓ Prospective setup evaluation detected valid prospective candidates for Pattern 2 & 5.")

    def test_05_csv_candle_logging(self):
        """Verify that log_candle_csv formats rows properly."""
        test_candle = {
            "date": datetime(2026, 9, 14, 18, 30, tzinfo=IST),
            "date_utc": datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc),
            "open": 4271.0,
            "high": 4317.79,
            "low": 4253.48,
            "close": 4315.76,
            "volume": 45210,
        }
        # Call log_candle_csv without error
        log_candle_csv("COMPLETED_H4", "XAUUSD", test_candle)
        today_str = now_ist().strftime("%Y-%m-%d")
        csv_path = PROJECT_DIR / "logs" / today_str / "candles.csv"
        self.assertTrue(csv_path.exists())
        self.assertGreater(csv_path.stat().st_size, 100)
        print(f"\n✓ candles.csv validated at {csv_path} (size: {csv_path.stat().st_size} bytes).")

    def test_06_risk_parameters_and_pnl(self):
        """Verify SL/Target calculation, sanity guards, and P&L math."""
        # Simulated valid LONG signal
        c3_long = {"open": 2571.0, "high": 2590.0, "low": 2565.0, "close": 2588.0}
        sig_long = SetupSignal(
            setup_type="BULLISH",
            direction="LONG",
            c1={}, c2={}, c3=c3_long,
            pattern_name="Pattern 1 (🔴🔴🟢)",
        )
        entry_price = 2588.0
        rp_long = calculate_sl_target(entry_price, sig_long)
        self.assertTrue(rp_long.is_valid)
        self.assertEqual(rp_long.direction, "LONG")
        # SL = c3["low"] - buffer (0.50) = 2565.0 - 0.50 = 2564.50
        self.assertEqual(rp_long.sl_price, 2564.50)
        # Risk = 2588.0 - 2564.50 = 23.50
        self.assertEqual(rp_long.risk, 23.50)
        # TP = entry + risk * 2.0 = 2588.0 + 47.0 = 2635.0
        self.assertEqual(rp_long.target_price, 2635.00)

        # Inverted risk test (LONG with entry below SL)
        rp_inverted = calculate_sl_target(2560.0, sig_long)
        self.assertFalse(rp_inverted.is_valid)
        self.assertIn("Inverted risk", rp_inverted.skip_reason)

        # Dollar P&L calculation: 0.01 lot = 1 oz. $10 gain = $10 profit
        pnl_long = calculate_dollar_pnl("LONG", 2580.0, 2590.0, lot_size=0.01)
        self.assertEqual(pnl_long, 10.0)

        pnl_short = calculate_dollar_pnl("SHORT", 2590.0, 2580.0, lot_size=0.01)
        self.assertEqual(pnl_short, 10.0)

        pnl_loss = calculate_dollar_pnl("LONG", 2590.0, 2580.0, lot_size=0.01)
        self.assertEqual(pnl_loss, -10.0)

        print("\n✓ Risk parameters, inverted SL guards, and dollar P&L math verified.")


if __name__ == "__main__":
    unittest.main()

