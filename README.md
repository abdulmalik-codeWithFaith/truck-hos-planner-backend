## `backend/README.md`

```markdown
# TruckHOS — Backend

Django + Django REST Framework backend for TruckHOS, a truck trip planner
and Hours-of-Service (HOS) / ELD log generator. Built as part of a Full
Stack Developer hiring assessment.

**Live API:** https://truck-hos-planner-backend.onrender.com
**Frontend repo:** https://github.com/abdulmalik-codeWithFaith/TruckHOS

## What it does

Given a driver's current location, pickup location, dropoff location, and
current 70-hour/8-day cycle usage, this API:

1. Geocodes all three locations (Photon — OpenStreetMap data).
2. Calculates a real driving route between them (OSRM).
3. Builds a chronological trip schedule that respects FMCSA
   property-carrying-driver Hours-of-Service rules.
4. Inserts fuel stops (~every 1,000 miles), a 1-hour pickup, and a 1-hour
   dropoff into that schedule.
5. Splits the schedule into one ELD daily log per calendar day, with duty
   status totals, mileage, and remarks.

Accuracy of the HOS/ELD calculation is the primary grading criterion for
this assessment, ahead of UI polish.

## Tech stack

- Python + Django + Django REST Framework
- Photon (OpenStreetMap-based) for geocoding
- OSRM (Open Source Routing Machine) for route calculation
- SQLite (default Django database — the trip calculation is stateless;
  no complex data layer needed)
- gunicorn + whitenoise for production serving

No authentication, payments, or account system — out of scope per the
assessment brief.

## Architecture

```
trips/
├── models.py             Optional Trip record (calculation is stateless)
├── serializers.py         Request validation
├── views.py                POST /api/trips/plan/
├── urls.py
├── services/
│   ├── routing.py          Geocoding (Photon) + routing (OSRM)
│   ├── hos_engine.py       Pure, immutable HOS rules state machine
│   ├── scheduler.py         Combines route + HOS engine + stops into
│   │                        one chronological event list
│   └── eld_generator.py     Splits the schedule into per-day ELD logs
└── tests/                   38 automated tests
```

The scheduler is the single source of truth: it produces one event list.
The ELD generator only splits and aggregates that same list by calendar
date — it never recalculates HOS logic independently.

## HOS rules implemented

| Rule | Limit |
|---|---|
| Max driving per period | 11 hours |
| On-duty window | 14 hours from start of shift (applies to all on-duty activity, not just driving) |
| Required break | 30 minutes after 8 cumulative hours of driving |
| Required rest | 10 consecutive hours off duty |
| Cycle limit | 70 hours / 8 days |
| Cycle restart | 34 consecutive hours off duty fully restores the 70-hour cycle |

Implemented in `trips/services/hos_engine.py` as a pure, immutable state
machine (`DriverState` + functions that advance it), independently
testable from routing and scheduling.

## Known implementation assumptions

The assessment intentionally leaves some details unspecified. Rather than
inventing silent behavior, these are the explicit choices made and why:

- **Trip start time**: assumed to be the moment the plan is generated,
  since the assessment doesn't specify a planned departure time.
- **Average driving speed**: derived per route leg from OSRM's own
  distance/duration estimate, rather than an invented flat speed.
- **Fuel stop duration**: 30 minutes, a documented constant
  (`FUEL_STOP_DURATION_HOURS` in `scheduler.py`) — not specified by the
  assessment.
- **Fuel stop placement**: triggered at the next natural stopping point at
  or shortly after each 1,000-mile threshold.
- **Intermediate stop coordinates**: fuel and mid-route rest stops are
  linearly interpolated between surrounding waypoints and labeled
  generically by mile marker, rather than reverse-geocoded to a named
  place. The map's route *line* still uses real OSRM road geometry; only
  stop *pin* placement is interpolated.
- **14-hour window scope**: applies to all on-duty activity, not just
  driving — a fixed-duration stop (break, fuel, pickup, dropoff) that
  would push the driver past the window triggers a rest first rather than
  silently overflowing it.
- **Sleeper-berth splits**: only full, consolidated 10-hour rests and
  34-hour restarts are modeled. The FMCSA sleeper-berth split provision
  (e.g. an 8/2 split) is out of scope.
- **70-hour/8-day cycle recovery**: modeled as a running total, fully
  restored only by a 34-hour restart. The rolling recovery of hours aging
  out past 8 days is out of scope.
- **Geocoding provider**: Photon (Komoot) rather than Nominatim's public
  server, after testing showed Nominatim intermittently stalls on
  scripted requests. Both are free, OpenStreetMap-based services.

## Local setup

```bash
python -m venv venv
source venv/Scripts/activate      # Windows Git Bash
# or: source venv/bin/activate    # macOS/Linux

pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Runs at `http://localhost:8000`.

## Environment variables

For local development, defaults in `config/settings.py` work out of the
box. For production, set:

```
SECRET_KEY=<a real generated secret>
DEBUG=False
ALLOWED_HOSTS=.onrender.com
CORS_EXTRA_ORIGINS=https://your-frontend-url.vercel.app
```

Generate a secret key with:
```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

## API

### `POST /api/trips/plan/`

**Request**
```json
{
  "current_location": "Chicago, IL",
  "pickup_location": "Denver, CO",
  "dropoff_location": "Los Angeles, CA",
  "current_cycle_used": 30
}
```

**Response**
```json
{
  "trip": {
    "distance_miles": 2022.0,
    "driving_hours": 35.61,
    "estimated_days": 4,
    "cycle_hours_remaining": 0.39
  },
  "route": { "coordinates": [[lng, lat], ...] },
  "stops": [{ "type": "fuel", "label": "...", "lat": 0, "lng": 0, "mile_marker": 1000 }],
  "schedule": [{ "day": 1, "start_time": "...", "end_time": "...", "status": "DRIVING", "label": "Driving", "location": "..." }],
  "daily_logs": [{ "day": 1, "date": "2026-09-14", "total_driving_hours": 11.0, "remarks": ["..."] }]
}
```

**Error responses**
- `400` — validation error (missing/invalid fields, cycle out of range, same pickup/dropoff)
- `422` — location not found, or no drivable route exists
- `503` — upstream geocoding/routing service temporarily unavailable

Each error response includes a `message` field intended for direct
display to the end user.

## Testing

```bash
python manage.py test trips
```

38 tests, all passing. No real network calls are made during the test
run — the scheduler, HOS engine, and ELD generator are tested with
synthetic data, and the API view tests mock the geocoding/routing calls —
so the suite is fast and deterministic.

Covers: short and long trips, cycle usage at 0/30/65/70 hours, the 34-hour
restart trigger, the 8-hour break trigger, the 11-hour driving limit, the
14-hour window (including fixed-duration stops), the 10-hour rest, exact
1-hour pickup/dropoff durations, fuel stop intervals, multi-day ELD log
generation (including events that span midnight), and invalid
location/cycle input handling.

## Deployment

Deployed to Render as a Python web service.

- **Build command**: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
- **Start command**: `gunicorn config.wsgi`
```