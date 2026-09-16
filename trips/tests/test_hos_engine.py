from datetime import datetime

from django.test import SimpleTestCase

from trips.services import hos_engine as hos


class HosEngineTests(SimpleTestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, 6, 0)

    def test_new_driver_state_computes_remaining_cycle(self):
        state = hos.new_driver_state(self.start, current_cycle_used=30)
        self.assertAlmostEqual(state.cycle_hours_remaining, 40.0)

    def test_new_driver_state_rejects_negative_cycle(self):
        with self.assertRaises(ValueError):
            hos.new_driver_state(self.start, current_cycle_used=-5)

    def test_new_driver_state_rejects_cycle_over_70(self):
        with self.assertRaises(ValueError):
            hos.new_driver_state(self.start, current_cycle_used=71)

    def test_new_driver_state_allows_cycle_exactly_70(self):
        state = hos.new_driver_state(self.start, current_cycle_used=70)
        self.assertAlmostEqual(state.cycle_hours_remaining, 0.0)

    def test_break_required_after_8_hours_driving(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_driving(state, 8.0)
        self.assertTrue(hos.needs_break(state))
        self.assertEqual(hos.hours_available_to_drive(state), 0.0)

    def test_break_resets_since_break_counter(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_driving(state, 8.0)
        state = hos.advance_break(state)
        self.assertFalse(hos.needs_break(state))
        self.assertAlmostEqual(hos.hours_available_to_drive(state), 3.0)

    def test_11_hour_driving_limit_per_period(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_driving(state, 8.0)
        state = hos.advance_break(state)
        state = hos.advance_driving(state, 3.0)
        self.assertAlmostEqual(state.driving_hours_today, 11.0)
        self.assertEqual(hos.hours_available_to_drive(state), 0.0)

    def test_advance_driving_rejects_exceeding_available_hours(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.begin_window_if_needed(state)
        with self.assertRaises(ValueError):
            hos.advance_driving(state, 9.0)

    def test_14_hour_window_limits_available_driving(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_on_duty_not_driving(state, 13.0)
        self.assertAlmostEqual(hos.hours_available_to_drive(state), 1.0)

    def test_10_hour_rest_resets_daily_limits_but_not_cycle(self):
        state = hos.new_driver_state(self.start, current_cycle_used=30)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_driving(state, 8.0)
        state = hos.advance_break(state)
        state = hos.advance_driving(state, 3.0)
        cycle_before_rest = state.cycle_hours_remaining
        state = hos.advance_rest(state, 10.0)
        state = hos.begin_window_if_needed(state)
        self.assertAlmostEqual(state.driving_hours_today, 0.0)
        self.assertAlmostEqual(state.driving_hours_since_break, 0.0)
        self.assertEqual(hos.hours_available_to_drive(state), 8.0)
        self.assertAlmostEqual(state.cycle_hours_remaining, cycle_before_rest)

    def test_34_hour_restart_fully_restores_cycle(self):
        state = hos.new_driver_state(self.start, current_cycle_used=65)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_driving(state, 5.0)
        self.assertTrue(hos.cycle_exhausted(state))
        state = hos.advance_rest(state, hos.RESTART_HOURS)
        self.assertAlmostEqual(state.cycle_hours_remaining, hos.CYCLE_LIMIT_HOURS)

    def test_cycle_depletes_with_on_duty_not_driving_time(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.advance_on_duty_not_driving(state, 1.0)
        self.assertAlmostEqual(state.cycle_hours_remaining, 69.0)

    def test_off_duty_rest_shorter_than_10h_does_not_reset_daily_limits(self):
        state = hos.new_driver_state(self.start, current_cycle_used=0)
        state = hos.begin_window_if_needed(state)
        state = hos.advance_driving(state, 5.0)
        state = hos.advance_rest(state, 3.0)
        self.assertAlmostEqual(state.driving_hours_today, 5.0)