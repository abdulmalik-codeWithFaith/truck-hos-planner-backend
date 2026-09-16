from unittest.mock import patch

from rest_framework import status
from rest_framework.test import APITestCase

from trips.services.routing import (
    GeocodedLocation,
    LocationNotFoundError,
    Route,
    RouteLeg,
)

PLAN_URL = "/api/trips/plan/"


def _fake_geocode(location):
    return GeocodedLocation(query=location, lat=0.0, lng=0.0, display_name=location)


def _fake_route(waypoints):
    return Route(
        distance_miles=100.0,
        duration_hours=2.0,
        geometry=[[0.0, 0.0], [1.0, 1.0]],
        legs=[RouteLeg(distance_miles=50.0, duration_hours=1.0), RouteLeg(distance_miles=50.0, duration_hours=1.0)],
    )


class PlanTripViewTests(APITestCase):
    def _valid_payload(self, **overrides):
        payload = {
            "current_location": "Chicago, IL",
            "pickup_location": "Denver, CO",
            "dropoff_location": "Los Angeles, CA",
            "current_cycle_used": 30,
        }
        payload.update(overrides)
        return payload

    @patch("trips.views.routing.get_route", side_effect=_fake_route)
    @patch("trips.views.routing.geocode", side_effect=_fake_geocode)
    def test_successful_plan_returns_expected_shape(self, mock_geocode, mock_route):
        response = self.client.post(PLAN_URL, self._valid_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        for key in ("trip", "route", "stops", "schedule", "daily_logs"):
            self.assertIn(key, data)
        self.assertIn("distance_miles", data["trip"])
        self.assertGreater(len(data["schedule"]), 0)
        self.assertGreater(len(data["daily_logs"]), 0)

    def test_missing_fields_returns_400(self):
        response = self.client.post(PLAN_URL, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)

    def test_cycle_used_negative_returns_400(self):
        response = self.client.post(PLAN_URL, self._valid_payload(current_cycle_used=-5), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cycle_used_over_70_returns_400(self):
        response = self.client.post(PLAN_URL, self._valid_payload(current_cycle_used=85), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cycle_used_exactly_70_is_accepted_by_validation(self):
        with patch("trips.views.routing.geocode", side_effect=_fake_geocode), \
             patch("trips.views.routing.get_route", side_effect=_fake_route):
            response = self.client.post(PLAN_URL, self._valid_payload(current_cycle_used=70), format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_same_pickup_and_dropoff_returns_400(self):
        response = self.client.post(
            PLAN_URL,
            self._valid_payload(pickup_location="Denver, CO", dropoff_location="Denver, CO"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_location_returns_400(self):
        response = self.client.post(PLAN_URL, self._valid_payload(current_location=""), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("trips.views.routing.geocode", side_effect=LocationNotFoundError("not found"))
    def test_unresolvable_location_returns_422_with_friendly_message(self, mock_geocode):
        response = self.client.post(PLAN_URL, self._valid_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertIn("message", response.data)
        self.assertIn("current location", response.data["message"])