"""
Test suite for the Fuel Route Optimizer API.
Covers:
- Health check endpoint
- Validation error handling
- Fuel optimization algorithm constraints (500-mile max range, 10 MPG)
- Integration endpoint tests
"""
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from optimizer.models import FuelStation
from optimizer.services.fuel_optimizer import find_optimal_fuel_stops


class HealthCheckTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("optimizer:health-check")

    def test_health_check_empty_db(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("status", response.data)
        self.assertEqual(response.data["status"], "ok")
        self.assertEqual(response.data["fuel_stations_loaded"], 0)

    def test_health_check_with_stations(self):
        FuelStation.objects.create(
            station_id="TEST-1",
            name="Test Station",
            city="New York",
            state="NY",
            latitude=40.7128,
            longitude=-74.0060,
            retail_price=3.199,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["fuel_stations_loaded"], 1)
        self.assertEqual(response.data["vehicle"]["range_miles"], 500)
        self.assertEqual(response.data["vehicle"]["mpg"], 10)


class FuelOptimizerAlgorithmTests(TestCase):
    def setUp(self):
        # Create synthetic fuel stations along a 1,200 mile corridor
        # Points along lat=40.0, from lon=-75.0 to -95.0
        self.stations = [
            {
                "station_id": "ST-1",
                "name": "Expensive Stop",
                "city": "Town A",
                "state": "PA",
                "latitude": 40.0,
                "longitude": -78.0,
                "retail_price": 4.10,
            },
            {
                "station_id": "ST-2",
                "name": "Cheap Stop",
                "city": "Town B",
                "state": "OH",
                "latitude": 40.0,
                "longitude": -82.0,
                "retail_price": 2.85,
            },
            {
                "station_id": "ST-3",
                "name": "Mid Stop",
                "city": "Town C",
                "state": "IN",
                "latitude": 40.0,
                "longitude": -86.0,
                "retail_price": 3.20,
            },
            {
                "station_id": "ST-4",
                "name": "Bargain Stop",
                "city": "Town D",
                "state": "IL",
                "latitude": 40.0,
                "longitude": -90.0,
                "retail_price": 2.75,
            },
        ]

    def test_short_route_under_500_miles(self):
        # Synthetic 300-mile route
        route_data = {
            "coords": [(40.0, -75.0 + i * 0.01) for i in range(100)],
            "cumulative_miles": [i * 3.0 for i in range(100)],
            "total_miles": 300.0,
            "duration_hours": 5.0,
            "start_coords": (40.0, -75.0),
            "end_coords": (40.0, -74.0),
        }
        res = find_optimal_fuel_stops(route_data, self.stations)
        self.assertIn("total_fuel_cost", res)
        self.assertIn("total_gallons", res)
        # 300 miles / 10 mpg = 30 gallons
        self.assertEqual(res["total_gallons"], 30.0)

    def test_long_route_enforces_500_mile_range(self):
        # Synthetic 1,000-mile route
        num_pts = 100
        total_miles = 1000.0
        route_data = {
            "coords": [(40.0, -75.0 - (i / num_pts) * 20.0) for i in range(num_pts + 1)],
            "cumulative_miles": [(i / num_pts) * total_miles for i in range(num_pts + 1)],
            "total_miles": total_miles,
            "duration_hours": 16.0,
            "start_coords": (40.0, -75.0),
            "end_coords": (40.0, -95.0),
        }
        res = find_optimal_fuel_stops(route_data, self.stations)
        self.assertGreater(len(res["fuel_stops"]), 0)

        # Check that distance between any consecutive stops is <= 500 miles
        prev_mile = 0.0
        for stop in res["fuel_stops"]:
            gap = stop["distance_from_start_miles"] - prev_mile
            self.assertLessEqual(gap, 500.0, f"Stop {stop['station_name']} exceeds 500-mile gap: {gap} mi")
            prev_mile = stop["distance_from_start_miles"]

        final_gap = total_miles - prev_mile
        self.assertLessEqual(final_gap, 500.0, f"Final leg exceeds 500-mile gap: {final_gap} mi")


class RouteOptimizerAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("optimizer:route-optimizer")

        # Seed test station
        FuelStation.objects.create(
            station_id="SEED-1",
            name="Ohio Pilot",
            city="YOUNGSTOWN",
            state="OH",
            latitude=41.03,
            longitude=-80.64,
            retail_price=3.05,
        )

    def test_missing_input_fields_returns_400(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_missing_finish_returns_400(self):
        response = self.client.post(self.url, {"start": "New York, NY"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("optimizer.views.get_route")
    def test_successful_route_request(self, mock_get_route):
        mock_get_route.return_value = {
            "coords": [(40.71, -74.00), (41.03, -80.64), (41.87, -87.62)],
            "cumulative_miles": [0.0, 390.0, 790.0],
            "total_miles": 790.0,
            "duration_hours": 14.5,
            "start_coords": (40.71, -74.00),
            "end_coords": (41.87, -87.62),
        }

        response = self.client.post(
            self.url,
            {"start": "New York, NY", "finish": "Chicago, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("route", response.data)
        self.assertIn("vehicle", response.data)
        self.assertIn("fuel_stops", response.data)
        self.assertIn("summary", response.data)
        self.assertIn("map", response.data)
        self.assertEqual(response.data["route"]["total_distance_miles"], 790.0)
        self.assertEqual(response.data["summary"]["total_gallons_needed"], 79.0)
