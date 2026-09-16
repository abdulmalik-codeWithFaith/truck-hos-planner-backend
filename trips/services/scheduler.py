"""
Trip scheduler.

Combines the route (from routing.py) with the HOS engine (hos_engine.py) to
build a single chronological list of schedule events: driving, required
30-minute breaks, fuel stops, the 1-hour pickup, the 1-hour dropoff, and
required 10-hour rests (or a 34-hour restart if the cycle is exhausted) —
respecting the 11-hour/14-hour/70-hour limits at every step. This is the
single source of truth the ELD generator and the API response both
consume; nothing downstream recomputes HOS logic.

Documented implementation assumptions (not specified by the assessment):
- Average driving speed per route leg is derived from OSRM's own
  distance/duration estimate for that leg (miles / hours), rather than an
  invented flat speed, since OSRM already accounts for road type and speed
  limits along the actual route.
- Fuel stop duration is 30 minutes (FUEL_STOP_DURATION_HOURS), a
  configurable constant, since the assessment does not specify one.
- Intermediate stop coordinates (fuel stops, rest stops that occur mid-leg)
  are linearly interpolated between the leg's origin and destination
  waypoints and labeled generically by mile marker, rather than
  reverse-geocoded to a named place — reverse geocoding every interpolated
  stop was out of scope for the assessment's time budget. The map's route
  line itself still uses the real OSRM road geometry; only stop *pin*
  placement is interpolated.
- The trip is assumed to start at the moment the plan is generated, since
  the assessment does not specify a planned start time.
- The 14-hour on-duty window applies to ALL on-duty activity, not just
  driving: if a fixed-duration stop (break, fuel, pickup, dropoff) would
  push the driver past the 14-hour window, a rest is inserted first rather
  than letting the window silently overflow.
- If the 70-hour/8-day cycle is exhausted mid-trip, the scheduler
  automatically inserts a 34-hour restart (OFF_DUTY), which fully restores
  the 70-hour cycle per the real FMCSA restart rule, so the trip always
  reaches its dropoff rather than failing outright.
"""

from dataclasses import dataclass, field
from datetime import datetime

from trips.services import hos_engine as hos
from trips.services.routing import GeocodedLocation, RouteLeg

FUEL_INTERVAL_MILES = 1000.0
FUEL_MILE_TOLERANCE = 0.5  # miles; avoids spurious near-zero driving fragments right at a fuel threshold
FUEL_STOP_DURATION_HOURS = 0.5
PICKUP_DURATION_HOURS = 1.0
DROPOFF_DURATION_HOURS = 1.0

FLOAT_TOLERANCE = 1e-6
MAX_LOOP_ITERATIONS = 2000  # safety guard against any runaway loop


class CycleExhaustedError(Exception):
    """Raised when the driver's remaining 70-hour/8-day cycle is used up
    before the trip can be completed. Kept as a safety net; the scheduler
    now handles cycle exhaustion via an automatic 34-hour restart instead
    of raising this, so in practice it should not be reached."""


@dataclass
class ScheduleEvent:
    day: int
    start_time: datetime
    end_time: datetime
    duration_hours: float
    status: str  # OFF_DUTY | SLEEPER_BERTH | DRIVING | ON_DUTY_NOT_DRIVING
    label: str
    location: str
    distance_miles: float | None = None  # populated for DRIVING events only


@dataclass
class TripStop:
    type: str  # current | pickup | dropoff | fuel | rest
    label: str
    lat: float
    lng: float
    mile_marker: float | None = None


@dataclass
class TripPlan:
    events: list[ScheduleEvent] = field(default_factory=list)
    stops: list[TripStop] = field(default_factory=list)
    distance_miles: float = 0.0
    driving_hours: float = 0.0
    cycle_hours_remaining: float = 0.0


def _interpolate(start: GeocodedLocation, end: GeocodedLocation, fraction: float) -> tuple[float, float]:
    fraction = max(0.0, min(1.0, fraction))
    lat = start.lat + (end.lat - start.lat) * fraction
    lng = start.lng + (end.lng - start.lng) * fraction
    return lat, lng


class _DayIndexer:
    """Assigns sequential trip-day numbers (1, 2, 3...) the first time each
    calendar date is seen, so events and later daily_logs agree on numbering."""

    def __init__(self):
        self._dates: dict = {}

    def day_for(self, dt: datetime) -> int:
        date_key = dt.date()
        if date_key not in self._dates:
            self._dates[date_key] = len(self._dates) + 1
        return self._dates[date_key]


def build_schedule(
    waypoints: list[GeocodedLocation],
    legs: list[RouteLeg],
    current_cycle_used: float,
    start_time: datetime,
) -> TripPlan:
    """
    Build the full chronological schedule for a trip: current -> pickup -> dropoff.

    Raises:
        ValueError: if current_cycle_used is invalid, or waypoints/legs don't
            line up (defensive — should already be validated by the caller).
    """
    if len(waypoints) != 3:
        raise ValueError("Expected exactly 3 waypoints: current, pickup, dropoff.")
    if len(legs) != 2:
        raise ValueError("Expected exactly 2 route legs: current->pickup, pickup->dropoff.")

    current_wp, pickup_wp, dropoff_wp = waypoints
    leg_to_pickup, leg_to_dropoff = legs

    state = hos.new_driver_state(start_time, current_cycle_used)
    day_indexer = _DayIndexer()

    events: list[ScheduleEvent] = []
    stops: list[TripStop] = [TripStop(type="current", label=current_wp.display_name, lat=current_wp.lat, lng=current_wp.lng)]

    total_miles_driven = 0.0
    next_fuel_threshold = FUEL_INTERVAL_MILES
    total_driving_hours = 0.0

    def insert_rest(reason_location: str) -> None:
        """Insert whatever rest the current state requires: a 34-hour
        restart if the cycle is exhausted, otherwise a 10-hour rest."""
        nonlocal state
        seg_start = state.clock
        if hos.cycle_exhausted(state):
            rest_hours = hos.RESTART_HOURS
            rest_status = "OFF_DUTY"
            rest_label = "34-hour restart (resets 70-hour/8-day cycle)"
        else:
            rest_hours = hos.REQUIRED_REST_HOURS
            rest_status = "SLEEPER_BERTH"
            rest_label = "10-hour rest"
        state = hos.advance_rest(state, rest_hours)
        events.append(ScheduleEvent(
            day=day_indexer.day_for(seg_start),
            start_time=seg_start,
            end_time=state.clock,
            duration_hours=rest_hours,
            status=rest_status,
            label=rest_label,
            location=reason_location,
        ))
        state = hos.begin_window_if_needed(state)

    def ensure_window_capacity(duration_needed: float, reason_location: str) -> None:
        """If there isn't enough room left in the 14-hour window for a
        fixed-duration on-duty action (break/fuel/pickup/dropoff), rest
        first. The 14-hour window applies to all on-duty time, not just
        driving."""
        nonlocal state
        state = hos.begin_window_if_needed(state)
        if hos.remaining_window_hours(state) + FLOAT_TOLERANCE < duration_needed:
            insert_rest(reason_location)

    leg_definitions = [
        (leg_to_pickup, current_wp, pickup_wp, "pickup"),
        (leg_to_dropoff, pickup_wp, dropoff_wp, "dropoff"),
    ]

    iterations = 0

    for leg, leg_start_wp, leg_end_wp, leg_end_kind in leg_definitions:
        remaining_leg_miles = leg.distance_miles
        avg_speed_mph = leg.distance_miles / leg.duration_hours if leg.duration_hours > 0 else 0.0

        while remaining_leg_miles > FLOAT_TOLERANCE:
            iterations += 1
            if iterations > MAX_LOOP_ITERATIONS:
                raise RuntimeError("Scheduler exceeded maximum iterations — possible logic error.")

            state = hos.begin_window_if_needed(state)

            if hos.cycle_exhausted(state):
                insert_rest(f"Rest stop (~{round(total_miles_driven)} mi)")
                continue

            if hos.needs_break(state):
                ensure_window_capacity(hos.BREAK_DURATION_HOURS, f"Rest area (~{round(total_miles_driven)} mi)")
                seg_start = state.clock
                state = hos.advance_break(state)
                events.append(ScheduleEvent(
                    day=day_indexer.day_for(seg_start),
                    start_time=seg_start,
                    end_time=state.clock,
                    duration_hours=hos.BREAK_DURATION_HOURS,
                    status="ON_DUTY_NOT_DRIVING",
                    label="30-minute break (required after 8h driving)",
                    location=f"Rest area (~{round(total_miles_driven)} mi)",
                ))
                continue

            available_hours = hos.hours_available_to_drive(state)
            if available_hours <= FLOAT_TOLERANCE:
                insert_rest(f"Rest stop (~{round(total_miles_driven)} mi)")
                continue

            remaining_leg_hours = remaining_leg_miles / avg_speed_mph if avg_speed_mph > 0 else 0
            miles_until_fuel = next_fuel_threshold - total_miles_driven
            if miles_until_fuel <= FUEL_MILE_TOLERANCE:
                hours_until_fuel = 0.0
            elif avg_speed_mph > 0:
                hours_until_fuel = miles_until_fuel / avg_speed_mph
            else:
                hours_until_fuel = float("inf")

            chunk_hours = min(available_hours, remaining_leg_hours, hours_until_fuel)

            if chunk_hours <= FLOAT_TOLERANCE:
                ensure_window_capacity(FUEL_STOP_DURATION_HOURS, f"Fuel stop (~{round(total_miles_driven)} mi)")
                seg_start = state.clock
                fraction = 1 - (remaining_leg_miles / leg.distance_miles) if leg.distance_miles else 0
                lat, lng = _interpolate(leg_start_wp, leg_end_wp, fraction)
                state = hos.advance_on_duty_not_driving(state, FUEL_STOP_DURATION_HOURS)
                events.append(ScheduleEvent(
                    day=day_indexer.day_for(seg_start),
                    start_time=seg_start,
                    end_time=state.clock,
                    duration_hours=FUEL_STOP_DURATION_HOURS,
                    status="ON_DUTY_NOT_DRIVING",
                    label=f"Fuel stop (~{round(next_fuel_threshold)} mi interval)",
                    location=f"Fuel stop (~{round(total_miles_driven)} mi)",
                ))
                stops.append(TripStop(
                    type="fuel",
                    label=f"Fuel stop (~{round(total_miles_driven)} mi)",
                    lat=lat, lng=lng,
                    mile_marker=round(total_miles_driven),
                ))
                next_fuel_threshold += FUEL_INTERVAL_MILES
                continue

            seg_start = state.clock
            state = hos.advance_driving(state, chunk_hours)
            miles_this_chunk = chunk_hours * avg_speed_mph
            total_miles_driven += miles_this_chunk
            total_driving_hours += chunk_hours
            remaining_leg_miles -= miles_this_chunk

            events.append(ScheduleEvent(
                day=day_indexer.day_for(seg_start),
                start_time=seg_start,
                end_time=state.clock,
                duration_hours=chunk_hours,
                status="DRIVING",
                label="Driving",
                location=f"En route to {leg_end_wp.display_name}",
                distance_miles=miles_this_chunk,
            ))

        duration = PICKUP_DURATION_HOURS if leg_end_kind == "pickup" else DROPOFF_DURATION_HOURS
        ensure_window_capacity(duration, leg_end_wp.display_name)
        seg_start = state.clock
        state = hos.advance_on_duty_not_driving(state, duration)
        events.append(ScheduleEvent(
            day=day_indexer.day_for(seg_start),
            start_time=seg_start,
            end_time=state.clock,
            duration_hours=duration,
            status="ON_DUTY_NOT_DRIVING",
            label="Pickup" if leg_end_kind == "pickup" else "Dropoff",
            location=leg_end_wp.display_name,
        ))
        stops.append(TripStop(
            type=leg_end_kind,
            label=f"{'Pickup' if leg_end_kind == 'pickup' else 'Dropoff'} — {leg_end_wp.display_name}",
            lat=leg_end_wp.lat, lng=leg_end_wp.lng,
        ))

    return TripPlan(
        events=events,
        stops=stops,
        distance_miles=total_miles_driven,
        driving_hours=total_driving_hours,
        cycle_hours_remaining=state.cycle_hours_remaining,
    )