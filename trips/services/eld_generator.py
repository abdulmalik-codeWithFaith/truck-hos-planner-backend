"""
ELD (Electronic Logging Device) daily-log generator.

Consumes the schedule produced by scheduler.py and splits it into one
DailyLog per calendar date the trip touches, computing duty-status hour
totals, miles driven that day, and human-readable remarks.

Mirrors the exact midnight-clipping approach used by the frontend's
EldGrid component (clipEventsToDate in dutyStatus.ts): an event that spans
midnight (e.g. a 10-hour rest) is clipped to the portion that falls within
each calendar date, so the backend's totals and the frontend's visual
24-hour graph are always describing the same underlying event list and can
never disagree. This is the "single source of truth" requirement from the
assessment — the ELD is generated from the same schedule used for the trip
plan, not recomputed independently.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trips.services.scheduler import ScheduleEvent

FLOAT_TOLERANCE = 1e-6

STATUS_LABELS = {
    "OFF_DUTY": "Off duty",
    "SLEEPER_BERTH": "Sleeper berth",
    "DRIVING": "Driving",
    "ON_DUTY_NOT_DRIVING": "On duty",
}


@dataclass
class DailyLog:
    day: int
    date: str  # YYYY-MM-DD
    total_driving_hours: float = 0.0
    total_on_duty_hours: float = 0.0
    total_off_duty_hours: float = 0.0
    total_sleeper_hours: float = 0.0
    total_miles: float = 0.0
    remarks: list = field(default_factory=list)


def _clip_to_date(event: ScheduleEvent, date_start: datetime, date_end: datetime):
    """Return (clipped_start, clipped_end) or None if the event doesn't
    overlap [date_start, date_end)."""
    if event.end_time <= date_start or event.start_time >= date_end:
        return None
    clipped_start = max(event.start_time, date_start)
    clipped_end = min(event.end_time, date_end)
    return clipped_start, clipped_end


def _format_remark(event: ScheduleEvent, clipped_start: datetime) -> str:
    time_label = clipped_start.strftime("%H:%M")
    status_label = STATUS_LABELS.get(event.status, event.status)
    return f"{time_label} — {event.location} — {event.label} ({status_label})"


def generate_daily_logs(events: list[ScheduleEvent]) -> list[DailyLog]:
    """
    Build one DailyLog per calendar date the trip's events touch, in
    chronological order.
    """
    if not events:
        return []

    dates: list = []
    seen = set()
    for e in sorted(events, key=lambda ev: ev.start_time):
        d = e.start_time.date()
        if d not in seen:
            seen.add(d)
            dates.append(d)
        # If the event ends exactly at midnight, it doesn't actually extend
        # into the next calendar day — back off by a moment so we don't
        # spuriously create an empty log for a day the event never touches.
        effective_end = e.end_time - timedelta(microseconds=1) if e.end_time > e.start_time else e.end_time
        d_end = effective_end.date()
        if d_end not in seen:
            seen.add(d_end)
            dates.append(d_end)
    dates.sort()

    logs: list[DailyLog] = []

    for day_number, date in enumerate(dates, start=1):
        date_start = datetime.combine(date, datetime.min.time())
        date_end = date_start + timedelta(days=1)

        log = DailyLog(day=day_number, date=date.isoformat())

        for event in sorted(events, key=lambda ev: ev.start_time):
            clipped = _clip_to_date(event, date_start, date_end)
            if clipped is None:
                continue
            clipped_start, clipped_end = clipped
            clipped_hours = (clipped_end - clipped_start).total_seconds() / 3600
            if clipped_hours <= FLOAT_TOLERANCE:
                continue

            if event.status == "DRIVING":
                log.total_driving_hours += clipped_hours
                if event.distance_miles and event.duration_hours > FLOAT_TOLERANCE:
                    proportion = clipped_hours / event.duration_hours
                    log.total_miles += event.distance_miles * proportion
            elif event.status == "ON_DUTY_NOT_DRIVING":
                log.total_on_duty_hours += clipped_hours
            elif event.status == "SLEEPER_BERTH":
                log.total_sleeper_hours += clipped_hours
            elif event.status == "OFF_DUTY":
                log.total_off_duty_hours += clipped_hours

            log.remarks.append(_format_remark(event, clipped_start))

        log.total_driving_hours = round(log.total_driving_hours, 2)
        log.total_on_duty_hours = round(log.total_on_duty_hours, 2)
        log.total_off_duty_hours = round(log.total_off_duty_hours, 2)
        log.total_sleeper_hours = round(log.total_sleeper_hours, 2)
        log.total_miles = round(log.total_miles, 1)

        logs.append(log)

    return logs