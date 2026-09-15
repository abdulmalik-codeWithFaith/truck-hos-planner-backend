"""
Geocoding and routing service.

Uses two free, open services as specified by the assessment:
- Photon (https://photon.komoot.io) for geocoding location strings into
  coordinates. Photon is built on OpenStreetMap data (same free/open data
  source the assessment calls for) and was chosen over Nominatim's public
  server after testing showed Nominatim intermittently stalls/times out on
  scripted (non-browser) requests. This is a documented implementation
  choice, not a change to the underlying open-data requirement.
- OSRM (Open Source Routing Machine) public demo server for turn-by-turn
  road routes, distance, and duration between waypoints.

Both are free and require no API key. The public demo servers for both are
rate-limited and not intended for large-scale production traffic — a
documented implementation assumption acceptable for an assessment MVP.
"""

import time
from dataclasses import dataclass, field

import requests

PHOTON_URL = "https://photon.komoot.io/api/"
OSRM_URL_TEMPLATE = "http://router.project-osrm.org/route/v1/driving/{coords}"

REQUEST_HEADERS = {"User-Agent": "TruckHOS-TripPlanner/1.0 (assessment project)"}

METERS_PER_MILE = 1609.344

MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.5


class LocationNotFoundError(Exception):
    """Raised when a location string cannot be geocoded to coordinates."""


class RouteNotFoundError(Exception):
    """Raised when no drivable route exists between the given waypoints."""


class RoutingServiceError(Exception):
    """Raised when the geocoding or routing service fails/times out/errors."""


@dataclass
class GeocodedLocation:
    query: str
    lat: float
    lng: float
    display_name: str


@dataclass
class RouteLeg:
    distance_miles: float
    duration_hours: float


@dataclass
class Route:
    distance_miles: float
    duration_hours: float
    geometry: list  # list of [lng, lat] pairs, GeoJSON order
    legs: list = field(default_factory=list)


def _request_with_retry(method_fn, *args, **kwargs):
    """Call a requests method, retrying once on timeout/connection errors."""
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return method_fn(*args, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_exc


def _display_name_from_photon(properties: dict) -> str:
    parts = [
        properties.get("name"),
        properties.get("city"),
        properties.get("state"),
        properties.get("country"),
    ]
    return ", ".join(p for p in parts if p)


def geocode(location: str) -> GeocodedLocation:
    """
    Resolve a free-text location string (e.g. "Chicago, IL") to coordinates.

    Raises:
        LocationNotFoundError: if no match is found for the given text.
        RoutingServiceError: if the geocoding service is unreachable or errors.
    """
    if not location or not location.strip():
        raise LocationNotFoundError("Location cannot be empty.")

    try:
        response = _request_with_retry(
            requests.get,
            PHOTON_URL,
            params={"q": location.strip(), "limit": 1},
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RoutingServiceError(f"Geocoding service error for '{location}': {exc}") from exc

    data = response.json()
    features = data.get("features", [])
    if not features:
        raise LocationNotFoundError(
            f"We couldn't find '{location}'. Please enter a city, state, or valid address."
        )

    match = features[0]
    lng, lat = match["geometry"]["coordinates"]
    display_name = _display_name_from_photon(match.get("properties", {})) or location

    return GeocodedLocation(query=location, lat=lat, lng=lng, display_name=display_name)


def get_route(waypoints: list[GeocodedLocation]) -> Route:
    """
    Get a driving route through an ordered list of geocoded waypoints
    (e.g. [current, pickup, dropoff]).

    Raises:
        RouteNotFoundError: if OSRM cannot find a drivable route.
        RoutingServiceError: if the routing service is unreachable or errors.
    """
    if len(waypoints) < 2:
        raise RouteNotFoundError("At least two waypoints are required to calculate a route.")

    coords = ";".join(f"{wp.lng},{wp.lat}" for wp in waypoints)
    url = OSRM_URL_TEMPLATE.format(coords=coords)

    try:
        response = _request_with_retry(
            requests.get,
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RoutingServiceError(f"Routing service error: {exc}") from exc

    data = response.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteNotFoundError(
            "No drivable route could be found between the given locations."
        )

    route_data = data["routes"][0]
    legs = [
        RouteLeg(
            distance_miles=leg["distance"] / METERS_PER_MILE,
            duration_hours=leg["duration"] / 3600,
        )
        for leg in route_data.get("legs", [])
    ]

    return Route(
        distance_miles=route_data["distance"] / METERS_PER_MILE,
        duration_hours=route_data["duration"] / 3600,
        geometry=route_data["geometry"]["coordinates"],
        legs=legs,
    )


def geocode_trip_locations(
    current_location: str, pickup_location: str, dropoff_location: str
) -> list[GeocodedLocation]:
    """Convenience helper: geocode all three trip locations in order."""
    return [
        geocode(current_location),
        geocode(pickup_location),
        geocode(dropoff_location),
    ]