"""
Main API views for the Fuel Route Optimizer.

Endpoints:
    POST /api/route/          — Plan a route with optimal fuel stops
    GET  /api/route/health/   — Health check
"""
import hashlib
import logging
import time

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from optimizer.models import FuelStation
from optimizer.serializers import RouteRequestSerializer
from optimizer.services.fuel_optimizer import find_optimal_fuel_stops
from optimizer.services.map_generator import generate_route_map
from optimizer.services.routing import get_route

logger = logging.getLogger(__name__)


class HealthCheckView(APIView):
    """Simple health check endpoint."""

    def get(self, request):
        station_count = FuelStation.objects.count()
        return Response({
            "status": "ok",
            "fuel_stations_loaded": station_count,
            "vehicle": {
                "range_miles": settings.VEHICLE_RANGE_MILES,
                "mpg": settings.VEHICLE_MPG,
                "tank_gallons": settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG,
            },
            "message": (
                "Ready!" if station_count > 0
                else "WARNING: No fuel stations loaded. Run: python manage.py load_fuel_data"
            ),
        })


class RouteOptimizerView(APIView):
    """
    POST /api/route/

    Plans a driving route from start to finish (both within USA),
    identifies optimal (cheapest) fuel stops given a 500-mile vehicle range,
    and returns an interactive map + cost summary.
    """

    def get(self, request):
        """Allow testing via GET with query params (e.g. ?start=...&finish=...)."""
        return self._process(request, request.query_params)

    def post(self, request):
        """Process POST with JSON body or query parameters."""
        data = request.data if (request.data and "start" in request.data) else request.query_params
        return self._process(request, data)

    def _process(self, request, input_data):
        t_start = time.monotonic()

        # ── Validate input ──────────────────────────────────────────────────
        serializer = RouteRequestSerializer(data=input_data)
        if not serializer.is_valid():
            return Response(
                {
                    "error": "Invalid input",
                    "details": serializer.errors,
                    "tip": "In Postman, select the 'Body' tab -> 'raw' -> 'JSON' and provide: {\"start\": \"New York, NY\", \"finish\": \"Chicago, IL\"}",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        start = serializer.validated_data["start"]
        finish = serializer.validated_data["finish"]

        logger.info("Route request: '%s' → '%s'", start, finish)

        # ── Check fuel station data ─────────────────────────────────────────
        station_count = FuelStation.objects.count()
        if station_count == 0:
            return Response(
                {
                    "error": "No fuel station data found.",
                    "fix": "Run: python manage.py load_fuel_data",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            # ── Fetch route (1 ORS API call, cached) ───────────────────────
            route_data = get_route(start, finish)

            # ── Load all stations from DB ───────────────────────────────────
            # Load into memory as plain dicts for fast processing
            stations_qs = FuelStation.objects.values(
                "station_id", "name", "address", "city", "state",
                "latitude", "longitude", "retail_price"
            )
            stations = list(stations_qs)

            # ── Run optimizer ───────────────────────────────────────────────
            optimizer_result = find_optimal_fuel_stops(route_data, stations)

            # ── Generate map ────────────────────────────────────────────────
            map_id = hashlib.md5(f"{start}|{finish}".lower().encode()).hexdigest()[:12]
            map_url = generate_route_map(
                route_data,
                optimizer_result["fuel_stops"],
                map_id,
            )
            full_map_url = request.build_absolute_uri(map_url)

            # ── Build response ──────────────────────────────────────────────
            fuel_stops_with_numbers = [
                {"stop_number": i + 1, **stop}
                for i, stop in enumerate(optimizer_result["fuel_stops"])
            ]

            elapsed = round(time.monotonic() - t_start, 3)

            response_data = {
                "route": {
                    "start": start,
                    "finish": finish,
                    "total_distance_miles": round(route_data["total_miles"], 1),
                    "estimated_duration_hours": round(route_data["duration_hours"], 1),
                    "start_coordinates": {
                        "lat": route_data["start_coords"][0],
                        "lon": route_data["start_coords"][1],
                    },
                    "end_coordinates": {
                        "lat": route_data["end_coords"][0],
                        "lon": route_data["end_coords"][1],
                    },
                },
                "vehicle": {
                    "max_range_miles": settings.VEHICLE_RANGE_MILES,
                    "fuel_efficiency_mpg": settings.VEHICLE_MPG,
                    "tank_capacity_gallons": settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG,
                },
                "fuel_stops": fuel_stops_with_numbers,
                "summary": {
                    "total_fuel_stops": len(fuel_stops_with_numbers),
                    "total_gallons_needed": optimizer_result["total_gallons"],
                    "total_fuel_cost_usd": optimizer_result["total_fuel_cost"],
                    "average_price_per_gallon": round(
                        optimizer_result["total_fuel_cost"] / optimizer_result["total_gallons"], 3
                    ) if optimizer_result["total_gallons"] > 0 else 0,
                },
                "map": {
                    "interactive_map_url": full_map_url,
                    "note": "Open the map URL in a browser for an interactive route view.",
                },
                "_meta": {
                    "response_time_seconds": elapsed,
                    "stations_in_db": station_count,
                },
            }

            logger.info(
                "Request complete in %.3fs: %d stops, $%.2f total",
                elapsed,
                len(fuel_stops_with_numbers),
                optimizer_result["total_fuel_cost"],
            )

            return Response(response_data, status=status.HTTP_200_OK)

        except ValueError as e:
            logger.error("ValueError: %s", e)
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as e:
            logger.exception("Unexpected error processing route request")
            return Response(
                {"error": "An internal error occurred.", "detail": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
