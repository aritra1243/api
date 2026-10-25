"""
Fuel stop optimization service.

Strategy:
1. Projects fuel stations onto the 1D route polyline using fast spatial indexing (cKDTree).
2. Given vehicle parameters:
   - Maximum range: 500 miles
   - Fuel efficiency: 10 MPG (miles per gallon)
3. For routes <= 500 miles:
   - A single tank is sufficient to reach the destination.
   - Optionally highlights the single cheapest fuel stop along the corridor.
4. For routes > 500 miles:
   - Uses an optimal greedy lookahead algorithm:
     - Starting from current location (mile 0), the vehicle can travel up to 500 miles.
     - Within the safe refueling window (typically 300 to 480 miles ahead), it selects the
       station with the lowest retail fuel price per gallon.
     - Advances position to that station and repeats until destination is within 500 miles.
5. Computes total gallons consumed and total fuel expenditure.
"""
import logging
import math
from typing import Any

import numpy as np
from django.conf import settings
from scipy.spatial import cKDTree

from optimizer.services.routing import _haversine_miles

logger = logging.getLogger(__name__)

VEHICLE_RANGE = getattr(settings, "VEHICLE_RANGE_MILES", 500.0)  # 500 miles
VEHICLE_MPG = getattr(settings, "VEHICLE_MPG", 10.0)             # 10 miles per gallon
TANK_CAPACITY = VEHICLE_RANGE / VEHICLE_MPG                      # 50 gallons
MAX_CORRIDOR_DEVIATION_MILES = 25.0                              # Max distance off route (miles)


def _project_stations_onto_route(
    stations: list[dict[str, Any]],
    route_coords: list[tuple[float, float]],
    cumulative_miles: list[float],
) -> list[dict[str, Any]]:
    """
    Project fuel stations onto the route polyline using cKDTree spatial indexing.
    Returns a sorted list of candidate stations along the route with their 'route_mile'.
    """
    if not stations or not route_coords:
        return []

    # 1. Bounding box pre-filter for the corridor
    lats = [c[0] for c in route_coords]
    lons = [c[1] for c in route_coords]
    buffer_deg = 0.45  # ~30 miles buffer

    min_lat, max_lat = min(lats) - buffer_deg, max(lats) + buffer_deg
    min_lon, max_lon = min(lons) - buffer_deg, max(lons) + buffer_deg

    candidate_stations = [
        s for s in stations
        if min_lat <= s["latitude"] <= max_lat and min_lon <= s["longitude"] <= max_lon
    ]

    if not candidate_stations:
        return []

    # 2. Downsample route points to ~1,500 points for sub-millisecond KDTree matching
    route_pts = np.array(route_coords)
    cum_miles = np.array(cumulative_miles)

    step = max(1, len(route_pts) // 1500)
    sampled_pts = route_pts[::step]
    sampled_miles = cum_miles[::step]

    # Build KDTree on sampled route
    tree = cKDTree(sampled_pts)
    station_pts = np.array([[s["latitude"], s["longitude"]] for s in candidate_stations])

    # Query within ~0.36 degrees (~25 miles)
    max_deg_dist = MAX_CORRIDOR_DEVIATION_MILES / 69.0
    dists, indices = tree.query(station_pts, distance_upper_bound=max_deg_dist)

    matched = []
    for i, dist in enumerate(dists):
        if np.isfinite(dist):
            s = candidate_stations[i].copy()
            idx = int(indices[i])
            route_mile = float(sampled_miles[idx])
            deviation_miles = float(dist * 69.0)

            # Avoid stations placed slightly past the destination
            total_route_miles = cumulative_miles[-1]
            if route_mile <= total_route_miles:
                s["route_mile"] = route_mile
                s["deviation_miles"] = deviation_miles
                matched.append(s)

    matched.sort(key=lambda s: s["route_mile"])
    return matched


def find_optimal_fuel_stops(route_data: dict[str, Any], stations: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Find optimal (cheapest) fuel stops along the route.

    Args:
        route_data: Output dictionary from routing.get_route()
        stations: List of fuel station dicts from DB

    Returns:
        {
            "fuel_stops": [...],
            "total_fuel_cost": float,
            "total_gallons": float,
        }
    """
    route_coords = route_data["coords"]
    cumulative_miles = route_data["cumulative_miles"]
    total_miles = route_data["total_miles"]

    # Total gallons consumed for the trip: total_miles / MPG
    total_trip_gallons = round(total_miles / VEHICLE_MPG, 2)

    # 1. Project stations onto the route corridor
    on_route = _project_stations_onto_route(stations, route_coords, cumulative_miles)

    if not on_route:
        # Fallback: estimate cost with national average if no station in DB
        avg_price = 3.50
        return {
            "fuel_stops": [],
            "total_fuel_cost": round(total_trip_gallons * avg_price, 2),
            "total_gallons": total_trip_gallons,
            "notes": "No stations found directly along corridor. Cost estimated at national average.",
        }

    # 2. Short trips (<= 500 miles):
    # Vehicle has 500-mile range.
    if total_miles <= VEHICLE_RANGE:
        # Highlight the single cheapest station along the route
        cheapest_station = min(on_route, key=lambda s: s["retail_price"])
        gallons_needed = total_trip_gallons
        cost = round(gallons_needed * cheapest_station["retail_price"], 2)

        fuel_stops = [{
            "station_id": cheapest_station["station_id"],
            "station_name": cheapest_station["name"],
            "address": cheapest_station.get("address", ""),
            "city": cheapest_station["city"],
            "state": cheapest_station["state"],
            "latitude": cheapest_station["latitude"],
            "longitude": cheapest_station["longitude"],
            "price_per_gallon": round(cheapest_station["retail_price"], 3),
            "distance_from_start_miles": round(cheapest_station["route_mile"], 1),
            "miles_to_this_stop": round(cheapest_station["route_mile"], 1),
            "gallons_purchased": gallons_needed,
            "cost_at_stop": cost,
            "notes": "Route is under 500 miles; this is the cheapest station along the route.",
        }]

        return {
            "fuel_stops": fuel_stops,
            "total_fuel_cost": cost,
            "total_gallons": total_trip_gallons,
        }

    # 3. Long trips (> 500 miles):
    # Need stops such that distance between consecutive stops <= 500 miles.
    fuel_stops = []
    current_mile = 0.0
    accumulated_cost = 0.0
    last_stop_mile = 0.0

    # Minimum fraction of tank used before refueling when possible (e.g. after 300 miles)
    preferred_min_interval = 280.0
    max_safe_interval = 480.0  # leave 20 miles buffer before empty

    while current_mile + VEHICLE_RANGE < total_miles:
        must_stop_by = min(current_mile + max_safe_interval, total_miles)
        preferred_start = current_mile + preferred_min_interval

        # Candidate window
        window = [
            s for s in on_route
            if preferred_start <= s["route_mile"] <= must_stop_by
        ]

        # If no stations in preferred window, expand to [current_mile + 50, must_stop_by]
        if not window:
            window = [
                s for s in on_route
                if current_mile + 50.0 <= s["route_mile"] <= must_stop_by
            ]

        # If still no station, take any reachable station ahead
        if not window:
            window = [
                s for s in on_route
                if current_mile < s["route_mile"] <= current_mile + VEHICLE_RANGE
            ]

        if not window:
            # Route gap: advance position and break
            logger.warning("No stations found in range from mile %.1f", current_mile)
            break

        # Pick the cheapest station in the window
        best_station = min(window, key=lambda s: s["retail_price"])

        miles_from_last = best_station["route_mile"] - last_stop_mile
        gallons_for_leg = round(miles_from_last / VEHICLE_MPG, 2)
        cost_at_stop = round(gallons_for_leg * best_station["retail_price"], 2)

        fuel_stops.append({
            "station_id": best_station["station_id"],
            "station_name": best_station["name"],
            "address": best_station.get("address", ""),
            "city": best_station["city"],
            "state": best_station["state"],
            "latitude": best_station["latitude"],
            "longitude": best_station["longitude"],
            "price_per_gallon": round(best_station["retail_price"], 3),
            "distance_from_start_miles": round(best_station["route_mile"], 1),
            "miles_to_this_stop": round(miles_from_last, 1),
            "gallons_purchased": gallons_for_leg,
            "cost_at_stop": cost_at_stop,
        })

        accumulated_cost += cost_at_stop
        current_mile = best_station["route_mile"]
        last_stop_mile = current_mile

    # Final leg: from last stop to destination
    final_leg_miles = total_miles - last_stop_mile
    final_gallons = round(final_leg_miles / VEHICLE_MPG, 2)

    # Use the price at the last refuel stop or the best route price
    if fuel_stops:
        final_price = fuel_stops[-1]["price_per_gallon"]
    else:
        final_price = min(on_route, key=lambda s: s["retail_price"])["retail_price"]

    final_cost = round(final_gallons * final_price, 2)
    accumulated_cost += final_cost

    return {
        "fuel_stops": fuel_stops,
        "total_fuel_cost": round(accumulated_cost, 2),
        "total_gallons": total_trip_gallons,
    }
