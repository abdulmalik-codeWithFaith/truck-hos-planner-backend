import logging
from datetime import datetime

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .serializers import TripRequestSerializer
from .services import routing
from .services.eld_generator import generate_daily_logs
from .services.scheduler import CycleExhaustedError, build_schedule

logger = logging.getLogger(__name__)

LOCATION_FIELD_LABELS = {
    "current_location": "current location",
    "pickup_location": "pickup location",
    "dropoff_location": "dropoff location",
}


def _serialize_event(event) -> dict:
    return {
        "day": event.day,
        "start_time": event.start_time.isoformat(),
        "end_time": event.end_time.isoformat(),
        "duration_hours": round(event.duration_hours, 2),
        "status": event.status,
        "label": event.label,
        "location": event.location,
        "distance_miles": round(event.distance_miles, 1) if event.distance_miles else None,
    }


def _serialize_stop(stop) -> dict:
    return {
        "type": stop.type,
        "label": stop.label,
        "lat": stop.lat,
        "lng": stop.lng,
        "mile_marker": stop.mile_marker,
    }


def _serialize_daily_log(log) -> dict:
    return {
        "day": log.day,
        "date": log.date,
        "total_driving_hours": log.total_driving_hours,
        "total_on_duty_hours": log.total_on_duty_hours,
        "total_off_duty_hours": log.total_off_duty_hours,
        "total_sleeper_hours": log.total_sleeper_hours,
        "total_miles": log.total_miles,
        "remarks": log.remarks,
    }


@api_view(["POST"])
def plan_trip(request):
    serializer = TripRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {
                "message": "Please check the trip details and try again.",
                "errors": serializer.errors,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    data = serializer.validated_data

    geocoded = {}
    for field_name in ("current_location", "pickup_location", "dropoff_location"):
        try:
            geocoded[field_name] = routing.geocode(data[field_name])
        except routing.LocationNotFoundError:
            friendly = LOCATION_FIELD_LABELS[field_name]
            return Response(
                {
                    "message": (
                        f'We couldn\'t find the {friendly} "{data[field_name]}". '
                        "Please enter a city, state, or valid address."
                    ),
                    "field": field_name,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except routing.RoutingServiceError as exc:
            logger.warning("Geocoding service error: %s", exc)
            return Response(
                {"message": "The map service is temporarily unavailable. Please try again in a moment."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    waypoints = [geocoded["current_location"], geocoded["pickup_location"], geocoded["dropoff_location"]]

    try:
        route = routing.get_route(waypoints)
    except routing.RouteNotFoundError:
        return Response(
            {"message": "We couldn't find a drivable route between these locations."},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except routing.RoutingServiceError as exc:
        logger.warning("Routing service error: %s", exc)
        return Response(
            {"message": "The routing service is temporarily unavailable. Please try again in a moment."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        plan = build_schedule(
            waypoints=waypoints,
            legs=route.legs,
            current_cycle_used=data["current_cycle_used"],
            start_time=datetime.now().replace(second=0, microsecond=0),
        )
    except CycleExhaustedError as exc:
        return Response({"message": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    except ValueError as exc:
        return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    daily_logs = generate_daily_logs(plan.events)

    response_data = {
        "trip": {
            "distance_miles": round(plan.distance_miles, 1),
            "driving_hours": round(plan.driving_hours, 2),
            "estimated_days": len(daily_logs),
            "cycle_hours_remaining": round(plan.cycle_hours_remaining, 2),
        },
        "route": {"coordinates": route.geometry},
        "stops": [_serialize_stop(s) for s in plan.stops],
        "schedule": [_serialize_event(e) for e in plan.events],
        "daily_logs": [_serialize_daily_log(log) for log in daily_logs],
    }

    return Response(response_data, status=status.HTTP_200_OK)