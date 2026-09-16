from datetime import datetime

from django.test import SimpleTestCase

from trips.services.routing import GeocodedLocation, RouteLeg
from trips.services.scheduler import (
    DROPOFF_DURATION_HOURS,
    FUEL_INTERVAL_MILES,
    PICKUP_DURATION_HOURS,
    build_schedule,
)


def _loc(name, lat=0.0, lng=0.0):
    return GeocodedLocation(query=name, lat=lat, lng=lng, display_name=name)


def _leg(distance_miles, speed_mph=50.0):
    return RouteLeg(distance_miles=distance_miles, duration_hours=distance_miles / speed_mph)


class SchedulerTests(SimpleTestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, 6, 0)
        self.waypoints = [_loc("Current"), _loc("Pickup"), _loc("Dropoff")]

    def test_short_trip_needs_no_break_no_rest_no_fuel(self):
        legs = [_leg(50), _leg(50)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=10, start_time=self.start)

        statuses = [e.status for e in plan.events]
        self.assertNotIn("SLEEPER_BERTH", statuses)
        self.assertNotIn("OFF_DUTY", statuses)
        self.assertFalse(any("break" in e.label.lower() for e in plan.events))
        self.assertFalse(any("fuel" in e.label.lower() for e in plan.events))
        self.assertAlmostEqual(plan.distance_miles, 100.0)

    def test_pickup_and_dropoff_are_exactly_one_hour(self):
        legs = [_leg(50), _leg(50)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=10, start_time=self.start)

        pickup_events = [e for e in plan.events if e.label == "Pickup"]
        dropoff_events = [e for e in plan.events if e.label == "Dropoff"]
        self.assertEqual(len(pickup_events), 1)
        self.assertEqual(len(dropoff_events), 1)
        self.assertAlmostEqual(pickup_events[0].duration_hours, PICKUP_DURATION_HOURS)
        self.assertAlmostEqual(dropoff_events[0].duration_hours, DROPOFF_DURATION_HOURS)
        self.assertEqual(pickup_events[0].status, "ON_DUTY_NOT_DRIVING")
        self.assertEqual(dropoff_events[0].status, "ON_DUTY_NOT_DRIVING")

    def test_break_required_after_8_hours_of_driving(self):
        legs = [_leg(500), _leg(50)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)

        driving_before_first_break = 0.0
        for e in plan.events:
            if e.status == "DRIVING":
                driving_before_first_break += e.duration_hours
            elif "break" in e.label.lower():
                break
        self.assertLessEqual(driving_before_first_break, 8.0 + 1e-6)
        self.assertTrue(any("break" in e.label.lower() for e in plan.events))

    def test_11_hour_driving_limit_never_exceeded_between_rests(self):
        legs = [_leg(1200), _leg(1200)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)

        running_total = 0.0
        for e in plan.events:
            if e.status == "DRIVING":
                running_total += e.duration_hours
                self.assertLessEqual(running_total, 11.0 + 1e-6)
            elif e.status == "SLEEPER_BERTH" or "restart" in e.label.lower():
                running_total = 0.0

    def test_14_hour_window_never_exceeded_in_a_single_work_period(self):
        legs = [_leg(1200), _leg(1200)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)

        window_start = None
        for e in plan.events:
            if window_start is None:
                window_start = e.start_time
            if e.status in ("SLEEPER_BERTH", "OFF_DUTY"):
                elapsed_before_rest = (e.start_time - window_start).total_seconds() / 3600
                self.assertLessEqual(elapsed_before_rest, 14.0 + 1e-6)
                window_start = None
            else:
                elapsed = (e.end_time - window_start).total_seconds() / 3600
                self.assertLessEqual(elapsed, 14.0 + 1e-6)

    def test_fuel_stops_occur_at_roughly_1000_mile_intervals(self):
        legs = [_leg(1200), _leg(1200)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)

        fuel_events = [e for e in plan.events if "fuel" in e.label.lower()]
        expected_stops = int(2400 // FUEL_INTERVAL_MILES)
        self.assertEqual(len(fuel_events), expected_stops)

    def test_10_hour_rest_appears_when_daily_limit_reached(self):
        legs = [_leg(1200), _leg(1200)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)
        self.assertTrue(any(e.status == "SLEEPER_BERTH" for e in plan.events))

    def test_multi_day_trip_spans_multiple_trip_days(self):
        legs = [_leg(1200), _leg(1200)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)
        day_numbers = {e.day for e in plan.events}
        self.assertGreater(len(day_numbers), 1)

    def test_cycle_65_hours_used_completes_short_trip_within_remaining_5_hours(self):
        legs = [_leg(50), _leg(50)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=65, start_time=self.start)
        self.assertAlmostEqual(plan.cycle_hours_remaining, 1.0, places=2)
        self.assertNotIn("OFF_DUTY", [e.status for e in plan.events])

    def test_cycle_exactly_70_triggers_immediate_34_hour_restart(self):
        legs = [_leg(50), _leg(50)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=70, start_time=self.start)

        first_event = plan.events[0]
        self.assertEqual(first_event.status, "OFF_DUTY")
        self.assertAlmostEqual(first_event.duration_hours, 34.0)
        self.assertIn("restart", first_event.label.lower())

    def test_distance_and_driving_hours_match_input_legs(self):
        legs = [_leg(300, speed_mph=60), _leg(400, speed_mph=60)]
        plan = build_schedule(self.waypoints, legs, current_cycle_used=0, start_time=self.start)
        self.assertAlmostEqual(plan.distance_miles, 700.0, places=1)
        self.assertAlmostEqual(plan.driving_hours, 700 / 60, places=1)