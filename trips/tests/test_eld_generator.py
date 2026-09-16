from datetime import datetime

from django.test import SimpleTestCase

from trips.services.eld_generator import generate_daily_logs
from trips.services.scheduler import ScheduleEvent


def _event(day, start, end, status, label="Event", location="Somewhere", distance_miles=None):
    return ScheduleEvent(
        day=day,
        start_time=start,
        end_time=end,
        duration_hours=(end - start).total_seconds() / 3600,
        status=status,
        label=label,
        location=location,
        distance_miles=distance_miles,
    )


class EldGeneratorTests(SimpleTestCase):
    def test_single_full_day_sums_to_24_hours(self):
        events = [
            _event(1, datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 8, 0), "DRIVING", distance_miles=400),
            _event(1, datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 9, 0), "ON_DUTY_NOT_DRIVING"),
            _event(1, datetime(2026, 1, 1, 9, 0), datetime(2026, 1, 2, 0, 0), "OFF_DUTY"),
        ]
        logs = generate_daily_logs(events)
        self.assertEqual(len(logs), 1)
        log = logs[0]
        total = (
            log.total_driving_hours
            + log.total_on_duty_hours
            + log.total_off_duty_hours
            + log.total_sleeper_hours
        )
        self.assertAlmostEqual(total, 24.0, places=2)

    def test_event_spanning_midnight_is_split_across_two_days(self):
        events = [
            _event(1, datetime(2026, 1, 1, 9, 0), datetime(2026, 1, 1, 19, 0), "DRIVING", distance_miles=500),
            _event(1, datetime(2026, 1, 1, 19, 0), datetime(2026, 1, 2, 5, 0), "SLEEPER_BERTH"),
        ]
        logs = generate_daily_logs(events)
        self.assertEqual(len(logs), 2)

        day1, day2 = logs[0], logs[1]
        self.assertEqual(day1.date, "2026-01-01")
        self.assertEqual(day2.date, "2026-01-02")

        self.assertAlmostEqual(day1.total_driving_hours, 10.0, places=2)
        self.assertAlmostEqual(day1.total_sleeper_hours, 5.0, places=2)

        self.assertAlmostEqual(day2.total_sleeper_hours, 5.0, places=2)
        self.assertAlmostEqual(day2.total_driving_hours, 0.0, places=2)

    def test_miles_are_allocated_proportionally_when_a_driving_event_spans_midnight(self):
        events = [
            _event(
                1,
                datetime(2026, 1, 1, 21, 0),
                datetime(2026, 1, 2, 7, 0),
                "DRIVING",
                distance_miles=100.0,
            ),
        ]
        logs = generate_daily_logs(events)
        self.assertEqual(len(logs), 2)
        day1, day2 = logs[0], logs[1]
        self.assertAlmostEqual(day1.total_miles, 30.0, places=1)
        self.assertAlmostEqual(day2.total_miles, 70.0, places=1)
        self.assertAlmostEqual(day1.total_miles + day2.total_miles, 100.0, places=1)

    def test_remarks_are_generated_from_events(self):
        events = [
            _event(
                1,
                datetime(2026, 1, 1, 6, 0),
                datetime(2026, 1, 1, 7, 0),
                "ON_DUTY_NOT_DRIVING",
                label="Pickup",
                location="Denver, CO",
            ),
        ]
        logs = generate_daily_logs(events)
        self.assertEqual(len(logs), 1)
        self.assertTrue(any("Pickup" in r and "Denver, CO" in r for r in logs[0].remarks))

    def test_empty_schedule_produces_no_logs(self):
        self.assertEqual(generate_daily_logs([]), [])

    def test_days_are_numbered_sequentially_in_chronological_order(self):
        events = [
            _event(1, datetime(2026, 1, 1, 6, 0), datetime(2026, 1, 1, 10, 0), "DRIVING", distance_miles=200),
            _event(2, datetime(2026, 1, 2, 6, 0), datetime(2026, 1, 2, 10, 0), "DRIVING", distance_miles=200),
            _event(3, datetime(2026, 1, 3, 6, 0), datetime(2026, 1, 3, 10, 0), "DRIVING", distance_miles=200),
        ]
        logs = generate_daily_logs(events)
        self.assertEqual([log.day for log in logs], [1, 2, 3])
        self.assertEqual([log.date for log in logs], ["2026-01-01", "2026-01-02", "2026-01-03"])