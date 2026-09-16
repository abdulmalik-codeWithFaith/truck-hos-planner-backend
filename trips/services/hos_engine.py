"""
Hours-of-Service (HOS) rules engine.

Implements the property-carrying-driver rules specified by the assessment:
- 11-hour driving limit per driving period (resets after a qualifying
  10-hour rest).
- 14-hour on-duty window: once a driver's workday starts, all driving must
  occur within 14 hours of that start, whether or not the driver is
  actively driving during that window.
- 30-minute break required after 8 cumulative hours of driving since the
  last qualifying break.
- 10 consecutive hours off duty required to reset the daily driving/window
  limits.
- 70-hour / 8-day cycle: total on-duty (driving + on-duty-not-driving)
  hours are capped by the driver's remaining cycle hours
  (70 - current_cycle_used).

Documented implementation assumptions (the assessment does not specify
these, so they are made explicit here rather than silently invented):
- Only full, consolidated 10-hour rests are modeled. The FMCSA "sleeper
  berth split" provision (e.g. splitting rest into an 8/2 split) is out of
  scope for this assessment.
- The 70-hour/8-day cycle is modeled as a simple running total that only
  depletes during this trip; the rolling recovery of hours that age out
  past 8 days (a full recalculation of the trailing 8-day window) is out
  of scope. If the cycle is exhausted mid-trip, the scheduler
  automatically schedules a 34-hour restart, which fully restores the
  70-hour cycle per the real FMCSA restart rule, so the trip always
  completes rather than failing outright.
- Off-duty and sleeper-berth time do not consume cycle hours, matching the
  real HOS rule that only on-duty time counts against the 70-hour cycle.
"""
RESTART_HOURS = 34.0
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

MAX_DRIVING_HOURS_PER_PERIOD = 11.0
MAX_WINDOW_HOURS = 14.0
BREAK_REQUIRED_AFTER_DRIVING_HOURS = 8.0
BREAK_DURATION_HOURS = 0.5
REQUIRED_REST_HOURS = 10.0
CYCLE_LIMIT_HOURS = 70.0
CYCLE_PERIOD_DAYS = 8

FLOAT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class DriverState:
    clock: datetime
    window_start: datetime | None  # None when the driver is off duty / workday not started
    driving_hours_today: float  # since the last qualifying 10-hour rest
    driving_hours_since_break: float  # since the last qualifying 30-min+ break
    cycle_hours_remaining: float  # 70 - current_cycle_used, depleted by on-duty time


def new_driver_state(start_time: datetime, current_cycle_used: float) -> DriverState:
    """
    Create the initial driver state at the start of trip planning.

    Raises:
        ValueError: if current_cycle_used is outside [0, 70].
    """
    if current_cycle_used < 0:
        raise ValueError("Current cycle used cannot be negative.")
    if current_cycle_used > CYCLE_LIMIT_HOURS:
        raise ValueError(
            f"Current cycle used cannot exceed the {CYCLE_LIMIT_HOURS:.0f}-hour/"
            f"{CYCLE_PERIOD_DAYS}-day limit."
        )

    return DriverState(
        clock=start_time,
        window_start=None,
        driving_hours_today=0.0,
        driving_hours_since_break=0.0,
        cycle_hours_remaining=CYCLE_LIMIT_HOURS - current_cycle_used,
    )


def _window_elapsed_hours(state: DriverState) -> float:
    if state.window_start is None:
        return 0.0
    return (state.clock - state.window_start).total_seconds() / 3600

def remaining_window_hours(state: DriverState) -> float:
    """Hours left in the 14-hour window right now (14.0 if no window is open yet)."""
    if state.window_start is None:
        return MAX_WINDOW_HOURS
    return max(0.0, MAX_WINDOW_HOURS - _window_elapsed_hours(state))
    
def begin_window_if_needed(state: DriverState) -> DriverState:
    """Start the 14-hour on-duty window if the driver isn't currently in one."""
    if state.window_start is not None:
        return state
    return replace(state, window_start=state.clock)


def needs_break(state: DriverState) -> bool:
    """True if the driver must take a 30-minute break before driving again."""
    return state.driving_hours_since_break >= BREAK_REQUIRED_AFTER_DRIVING_HOURS - FLOAT_TOLERANCE


def cycle_exhausted(state: DriverState) -> bool:
    return state.cycle_hours_remaining <= FLOAT_TOLERANCE


def hours_available_to_drive(state: DriverState) -> float:
    """
    The maximum contiguous hours the driver may drive right now, considering
    every applicable limit. Returns 0 if a break or rest is required first,
    or if the cycle is exhausted.
    """
    if state.window_start is None:
        return 0.0
    if needs_break(state):
        return 0.0
    if cycle_exhausted(state):
        return 0.0

    remaining_window = MAX_WINDOW_HOURS - _window_elapsed_hours(state)
    remaining_daily_driving = MAX_DRIVING_HOURS_PER_PERIOD - state.driving_hours_today
    remaining_until_break = BREAK_REQUIRED_AFTER_DRIVING_HOURS - state.driving_hours_since_break
    remaining_cycle = state.cycle_hours_remaining

    return max(0.0, min(remaining_window, remaining_daily_driving, remaining_until_break, remaining_cycle))


def advance_driving(state: DriverState, hours: float) -> DriverState:
    """
    Advance the clock by `hours` of driving time.

    Raises:
        ValueError: if `hours` exceeds what hours_available_to_drive() permits.
            This is a defensive guard — the scheduler is responsible for
            never requesting more than is currently legal.
    """
    if hours <= 0:
        raise ValueError("Driving hours must be positive.")
    available = hours_available_to_drive(state)
    if hours > available + FLOAT_TOLERANCE:
        raise ValueError(
            f"Requested {hours:.2f}h of driving exceeds the {available:.2f}h "
            "currently available under HOS rules."
        )

    return replace(
        state,
        clock=state.clock + timedelta(hours=hours),
        driving_hours_today=state.driving_hours_today + hours,
        driving_hours_since_break=state.driving_hours_since_break + hours,
        cycle_hours_remaining=state.cycle_hours_remaining - hours,
    )


def advance_on_duty_not_driving(state: DriverState, hours: float) -> DriverState:
    """
    Advance the clock by `hours` of on-duty-not-driving time (pickup,
    dropoff, fueling, inspections). Consumes cycle hours and window time,
    but does not count toward the driving limits.

    Per the current FMCSA rule, the 30-minute break requirement may be
    satisfied by ANY non-driving period of at least 30 minutes — not only a
    dedicated break — so any on-duty-not-driving period of >= 30 minutes
    also resets the since-last-break counter here.
    """
    if hours <= 0:
        raise ValueError("On-duty hours must be positive.")

    state = begin_window_if_needed(state)
    since_break = (
        0.0 if hours >= BREAK_DURATION_HOURS - FLOAT_TOLERANCE else state.driving_hours_since_break
    )
    return replace(
        state,
        clock=state.clock + timedelta(hours=hours),
        cycle_hours_remaining=max(0.0, state.cycle_hours_remaining - hours),
        driving_hours_since_break=since_break,
    )


def advance_break(state: DriverState, hours: float = BREAK_DURATION_HOURS) -> DriverState:
    """
    Advance the clock by a qualifying break (default 30 minutes). Resets
    the since-last-break driving counter. Still consumes window time and
    cycle hours, since a short break is on-duty-not-driving, not off-duty.
    """
    state = advance_on_duty_not_driving(state, hours)
    return replace(state, driving_hours_since_break=0.0)



def advance_rest(state: DriverState, hours: float) -> DriverState:
    """
    Advance the clock by an off-duty/sleeper-berth rest period.

    - >= 34 hours qualifies as a full 34-hour restart: resets the daily
      driving limit, the break counter, the 14-hour window, AND restores
      the full 70-hour/8-day cycle, matching the real FMCSA restart rule.
    - >= 10 hours (but < 34) qualifies as a normal daily reset: resets the
      daily driving limit, break counter, and window, but does NOT restore
      cycle hours (only a full 34-hour restart does that).
    - Shorter off-duty periods advance the clock but reset nothing (per
      the documented assumption that sleeper-berth splits are out of scope).

    Off-duty/sleeper time never consumes cycle hours regardless of length.
    """
    if hours <= 0:
        raise ValueError("Rest hours must be positive.")

    new_clock = state.clock + timedelta(hours=hours)

    if hours >= RESTART_HOURS - FLOAT_TOLERANCE:
        return DriverState(
            clock=new_clock,
            window_start=None,
            driving_hours_today=0.0,
            driving_hours_since_break=0.0,
            cycle_hours_remaining=CYCLE_LIMIT_HOURS,
        )

    if hours >= REQUIRED_REST_HOURS - FLOAT_TOLERANCE:
        return DriverState(
            clock=new_clock,
            window_start=None,
            driving_hours_today=0.0,
            driving_hours_since_break=0.0,
            cycle_hours_remaining=state.cycle_hours_remaining,
        )

    return replace(state, clock=new_clock)