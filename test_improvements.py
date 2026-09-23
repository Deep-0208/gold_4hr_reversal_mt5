"""
Dedicated Test Suite for Improvements in Gold 4-Hour Reversal Strategy
"""
import unittest
from datetime import datetime, timezone
from config import CONFIG
from risk import calculate_sl_target, RiskParams
from orders import OrderManager


class MockBroker:
    def __init__(self):
        self.modify_calls = []
        self.close_calls = []

    def modify_position(self, ticket, new_sl, new_tp):
        self.modify_calls.append({"ticket": ticket, "new_sl": new_sl, "new_tp": new_tp})
        return True

    def close_position(self, ticket):
        self.close_calls.append(ticket)
        return True

    def get_deals_by_position(self, ticket):
        return []


class MockTick:
    def __init__(self, bid=4350.0, ask=4350.25):
        self.bid = bid
        self.ask = ask
        self.spread = round(ask - bid, 2)
        self.time = datetime.now(timezone.utc)


class TestImprovements(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker()
        self.order_mgr = OrderManager(broker=self.broker)

    def test_tsl_step_filter(self):
        """Verify TSL only ratchets and modifies MT5 if step >= tsl_min_step_dollars ($0.50)."""
        CONFIG["use_tsl"] = True
        CONFIG["tsl_activation_rr"] = 1.0
        CONFIG["tsl_floor_rr"] = 1.0
        CONFIG["tsl_trail_rr_fraction"] = 0.2
        CONFIG["tsl_min_step_dollars"] = 0.50

        # Create active SHORT position in trailing zone
        # Entry: 4340.00, SL: 4360.00 (Risk = 20.00, floor = 4320.00)
        # Peak at 4310.00 -> trail_sl = 4310 + 4 = 4314.00 (< floor 4320.00)
        state = {
            "in_position": True,
            "current_position": {
                "direction": "SHORT",
                "entry_price": 4340.00,
                "sl_price": 4314.00,
                "target_price": 4260.00,
                "risk": 20.00,
                "tsl_active": True,
                "lowest_price": 4310.00,
                "ticket": 123456,
            }
        }

        # Price drops slightly: lowest_price goes from 4310.00 to 4309.80 (delta = 0.20 < 0.50)
        # new_sl would be 4309.80 + 4.0 = 4313.80. Delta from 4314.00 is 0.20 (< 0.50)
        self.order_mgr.update_tsl(state, 4309.80)
        self.assertEqual(state["current_position"]["sl_price"], 4314.00, "SL should NOT update if move < $0.50")
        self.assertEqual(len(self.broker.modify_calls), 0, "MT5 should NOT be called if move < $0.50")

        # Now price drops further: lowest_price goes to 4308.00
        # new_sl = 4308.00 + 4.0 = 4312.00 (Delta from 4314.00 is 2.00 >= 0.50)
        self.order_mgr.update_tsl(state, 4308.00)
        self.assertEqual(state["current_position"]["sl_price"], 4312.00, "SL should update when move >= $0.50")
        self.assertEqual(len(self.broker.modify_calls), 1, "MT5 should be called once when move >= $0.50")

    def test_max_dollar_risk_cap(self):
        """Verify max dollar risk cap (disabled if 0, enforced if > 0)."""
        signal = {
            "direction": "LONG",
            "c3": {"low": 4300.0, "high": 4350.0},
        }
        entry = 4345.0  # SL = 4300.0 - 0.5 = 4299.50. Risk = 4345 - 4299.5 = 45.50

        # Test 1: Disabled by default (max_risk_dollars = 0)
        CONFIG["max_risk_dollars"] = 0
        params = calculate_sl_target(entry, signal)
        self.assertTrue(params.is_valid, "Setup should be valid when max_risk_dollars is disabled (0)")

        # Test 2: Enabled with $40 limit -> should reject risk of $45.50
        CONFIG["max_risk_dollars"] = 40.0
        params = calculate_sl_target(entry, signal)
        self.assertFalse(params.is_valid, "Setup should be rejected when risk ($45.50) > $40.00 limit")
        self.assertIn("exceeds max dollar risk limit", params.skip_reason)

        # Test 3: Setup with risk $20.50 with limit $40.00 -> should pass
        signal_small = {
            "direction": "LONG",
            "c3": {"low": 4320.0, "high": 4345.0},
        }
        # SL = 4320 - 0.5 = 4319.50. Risk = 4340 - 4319.50 = 20.50 < 40
        params_small = calculate_sl_target(4340.0, signal_small)
        self.assertTrue(params_small.is_valid, "Setup with $20.50 risk should pass $40.00 limit")

        # Reset to 0 per user preference
        CONFIG["max_risk_dollars"] = 0

    def test_check_exit_conditions_with_tick(self):
        """Verify check_exit_conditions accepts tick and executes clean SL exit."""
        state = {
            "in_position": True,
            "current_position": {
                "direction": "SHORT",
                "entry_price": 4340.00,
                "sl_price": 4360.00,
                "target_price": 4260.00,
                "risk": 20.00,
                "ticket": 123456,
            }
        }
        tick = MockTick(bid=4361.0, ask=4361.25)
        exited = self.order_mgr.check_exit_conditions(state, tick.ask, tick=tick)
        self.assertTrue(exited, "Position should exit when price >= SL")
        self.assertFalse(state["in_position"], "in_position should be False after exit")


if __name__ == "__main__":
    unittest.main()
