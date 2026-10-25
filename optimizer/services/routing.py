"""
Routing service supporting free OpenStreetMap/OSRM routing (no API key needed)
and optional OpenRouteService (ORS) when configured.

Primary Engine:
- OSRM (Open Source Routing Machine) public demo server
  URL: https://router.project-osrm.org
  - 100% free, NO API key required.
  - Returns complete driving geometry, distance, and duration.
- Backup Engine: OpenStreetMap routed-car
  URL: https://routing.openstreetmap.de/routed-car

Geocoding:
- Built-in instant coordinates for top US metropolitan areas.
- Free Nominatim OpenStreetMap geocoding for all other US cities.
- Caching layer (LocMemCache) to avoid duplicate external requests.
"""
import hashlib
import logging
import math
import re
import time

import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# OSRM public endpoints (Free, NO API key required)
OSRM_SERVERS = [
    "https://router.project-osrm.org",
    "https://routing.openstreetmap.de/routed-car",
]

# ORS base URL (Optional, requires API key)
ORS_BASE = "https://api.openrouteservice.org"

# Instant coordinate cache for popular US cities (avoids geocode latency)
COMMON_US_CITIES = {
    "NEW YORK, NY": (40.7128, -74.0060),
    "NEW YORK": (40.7128, -74.0060),
    "NYC": (40.7128, -74.0060),
    "LOS ANGELES, CA": (34.0522, -118.2437),
    "LOS ANGELES": (34.0522, -118.2437),
    "LA": (34.0522, -118.2437),
    "CHICAGO, IL": (41.8781, -87.6298),
    "CHICAGO": (41.8781, -87.6298),
    "HOUSTON, TX": (29.7604, -95.3698),
    "HOUSTON": (29.7604, -95.3698),
    "PHOENIX, AZ": (33.4484, -112.0740),
    "PHOENIX": (33.4484, -112.0740),
    "PHILADELPHIA, PA": (39.9526, -75.1652),
    "PHILADELPHIA": (39.9526, -75.1652),
    "SAN ANTONIO, TX": (29.4241, -98.4936),
    "SAN ANTONIO": (29.4241, -98.4936),
    "SAN DIEGO, CA": (32.7157, -117.1611),
    "SAN DIEGO": (32.7157, -117.1611),
    "DALLAS, TX": (32.7767, -96.7970),
    "DALLAS": (32.7767, -96.7970),
    "AUSTIN, TX": (30.2672, -97.7431),
    "AUSTIN": (30.2672, -97.7431),
    "SAN JOSE, CA": (37.3382, -121.8863),
    "SAN FRANCISCO, CA": (37.7749, -122.4194),
    "SAN FRANCISCO": (37.7749, -122.4194),
    "SEATTLE, WA": (47.6062, -122.3321),
    "SEATTLE": (47.6062, -122.3321),
    "DENVER, CO": (39.7392, -104.9903),
    "DENVER": (39.7392, -104.9903),
    "BOSTON, MA": (42.3601, -71.0589),
    "BOSTON": (42.3601, -71.0589),
    "MIAMI, FL": (25.7617, -80.1918),
    "MIAMI": (25.7617, -80.1918),
    "ATLANTA, GA": (33.7490, -84.3880),
    "ATLANTA": (33.7490, -84.3880),
    "DETROIT, MI": (42.3314, -83.0458),
    "MINNEAPOLIS, MN": (44.9778, -93.2650),
    "LAS VEGAS, NV": (36.1699, -115.1398),
    "LAS VEGAS": (36.1699, -115.1398),
    "PORTLAND, OR": (45.5152, -122.6784),
    "ST. LOUIS, MO": (38.6270, -90.1994),
    "SAINT LOUIS, MO": (38.6270, -90.1994),
    "NASHVILLE, TN": (36.1627, -86.7816),
    "MEMPHIS, TN": (35.1495, -90.0490),
    "NEW ORLEANS, LA": (29.9511, -90.0715),
    "KANSAS CITY, MO": (39.0997, -94.5786),
    "CHARLOTTE, NC": (35.2271, -80.8431),
    "INDIANAPOLIS, IN": (39.7684, -86.1581),
    "COLUMBUS, OH": (39.9612, -82.9988),
    "CLEVELAND, OH": (41.4993, -81.6944),
    "PITTSBURGH, PA": (40.4406, -79.9959),
    "BALTIMORE, MD": (39.2904, -76.6122),
    "WASHINGTON, DC": (38.9072, -77.0369),
    "SALT LAKE CITY, UT": (40.7608, -111.8910),
    "ALBUQUERQUE, NM": (35.0844, -106.6504),
    "OKLAHOMA CITY, OK": (35.4676, -97.5164),
    "TAMPA, FL": (27.9506, -82.4572),
    "ORLANDO, FL": (28.5383, -81.3792),
    "RICHMOND, VA": (37.5407, -77.4360),
    "RALEIGH, NC": (35.7796, -78.6382),
}


def _make_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    """Create a requests.Session with automatic retry on transient errors."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in miles between two lat/lon points."""
    r = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _polyline_cumulative_distances(coords: list[tuple[float, float]]) -> list[float]:
    """
    Given a list of (lat, lon) points, calculate cumulative mileage at each point.
    """
    distances = [0.0]
    for i in range(1, len(coords)):
        d = _haversine_miles(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        distances.append(distances[-1] + d)
    return distances


def geocode(place: str) -> tuple[float, float]:
    """
    Geocode a place name or coordinate string to (latitude, longitude).
    1. Direct lat,lon parsing (e.g. '40.7128, -74.0060')
    2. Common US cities cache (0ms)
    3. Django cache (0ms)
    4. Nominatim OpenStreetMap API (free, no key needed)
    """
    cleaned = place.strip()

    # 1. Check if user passed literal coordinates: "lat, lon"
    coord_match = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)$", cleaned)
    if coord_match:
        lat, lon = float(coord_match.group(1)), float(coord_match.group(2))
        return lat, lon

    key_upper = cleaned.upper()
    if key_upper in COMMON_US_CITIES:
        return COMMON_US_CITIES[key_upper]

    # Check cache
    geo_cache_key = "geo_" + hashlib.md5(cleaned.lower().encode()).hexdigest()
    cached = cache.get(geo_cache_key)
    if cached:
        return cached

    # Query Nominatim
    url = "https://nominatim.openstreetmap.org/search"
    query = cleaned if "USA" in cleaned.upper() or "UNITED STATES" in cleaned.upper() else f"{cleaned}, USA"
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {
        "User-Agent": "FuelRouteOptimizer/2.0 (DjangoAssessment)",
        "Accept": "application/json",
    }

    session = _make_session(retries=3, backoff=1.0)
    try:
        resp = session.get(url, params=params, headers=headers, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Nominatim request error for '%s': %s", cleaned, e)
        # Try without ', USA' suffix
        try:
            resp = session.get(url, params={"q": cleaned, "format": "json", "limit": 1}, headers=headers, timeout=12)
            data = resp.json()
        except Exception:
            data = []

    if not data:
        raise ValueError(
            f"Could not locate '{place}' within the USA. "
            "Please provide a valid US city name, e.g. 'Chicago, IL' or 'Austin, TX'."
        )

    lat = float(data[0]["lat"])
    lon = float(data[0]["lon"])
    result = (lat, lon)
    cache.set(geo_cache_key, result, timeout=86400)
    return result


def _get_route_osrm(start_lon: float, start_lat: float, end_lon: float, end_lat: float) -> dict:
    """
    Fetch route from OSRM public demo server (100% free, NO API key required).
    Tries primary server then fallback server.
    """
    session = _make_session(retries=3, backoff=1.0)
    last_error = None

    for base_url in OSRM_SERVERS:
        endpoint = f"{base_url}/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
        params = {
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
        }
        try:
            logger.info("Calling OSRM at %s: (%f, %f) -> (%f, %f)", base_url, start_lat, start_lon, end_lat, end_lon)
            resp = session.get(endpoint, params=params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == "Ok" and data.get("routes"):
                    route = data["routes"][0]
                    geojson_coords = route["geometry"]["coordinates"]
                    # GeoJSON is [lon, lat] -> convert to (lat, lon)
                    coords = [(c[1], c[0]) for c in geojson_coords]
                    total_miles = route["distance"] * 0.000621371  # meters to miles
                    duration_hours = route["duration"] / 3600.0    # seconds to hours
                    return {
                        "coords": coords,
                        "total_miles": total_miles,
                        "duration_hours": duration_hours,
                    }
                else:
                    logger.warning("OSRM returned non-OK status: %s", data.get("code"))
            else:
                logger.warning("OSRM server %s returned status %d", base_url, resp.status_code)
        except Exception as e:
            logger.warning("OSRM server %s failed: %s", base_url, e)
            last_error = e

    raise RuntimeError(
        f"Unable to fetch driving route from OSRM servers. "
        f"Please check network connection. Details: {last_error}"
    )


def _get_route_ors(start_lon: float, start_lat: float, end_lon: float, end_lat: float) -> dict:
    """Optional ORS route fetcher when ORS_API_KEY is configured."""
    api_key = getattr(settings, "ORS_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ORS_API_KEY is not configured.")

    url = f"{ORS_BASE}/v2/directions/driving-car"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json, application/geo+json",
    }
    body = {
        "coordinates": [[start_lon, start_lat], [end_lon, end_lat]],
        "format": "geojson",
        "geometry": True,
        "units": "mi",
        "instructions": False,
    }

    session = _make_session(retries=2, backoff=1.5)
    resp = session.post(url, json=body, headers=headers, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    feature = data["features"][0]
    summary = feature["properties"]["summary"]
    coords = [(c[1], c[0]) for c in feature["geometry"]["coordinates"]]
    return {
        "coords": coords,
        "total_miles": summary["distance"],
        "duration_hours": summary["duration"] / 3600.0,
    }


def get_route(start: str, finish: str) -> dict:
    """
    Main entry point for routing:
    1. Geocode start and finish locations (free, zero API key)
    2. Check cache for route
    3. Query free OSRM (or ORS if configured and requested)
    4. Compute cumulative polyline mileage
    5. Cache and return normalized route object

    Returns:
        {
            "coords": [(lat, lon), ...],
            "cumulative_miles": [0.0, 1.2, ...],
            "total_miles": float,
            "duration_hours": float,
            "start_coords": (lat, lon),
            "end_coords": (lat, lon),
        }
    """
    cache_key = "route_" + hashlib.md5(f"{start}|{finish}".lower().encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        logger.info("Route cache HIT for %s -> %s", start, finish)
        return cached

    # Step 1: Geocode
    start_lat, start_lon = geocode(start)
    end_lat, end_lon = geocode(finish)

    # Step 2: Route
    engine = getattr(settings, "ROUTING_ENGINE", "osrm").lower()
    ors_key = getattr(settings, "ORS_API_KEY", "").strip()

    if engine == "ors" and ors_key:
        try:
            route_res = _get_route_ors(start_lon, start_lat, end_lon, end_lat)
        except Exception as e:
            logger.warning("ORS routing failed (%s), falling back to free OSRM", e)
            route_res = _get_route_osrm(start_lon, start_lat, end_lon, end_lat)
    else:
        # Default to free OSRM
        route_res = _get_route_osrm(start_lon, start_lat, end_lon, end_lat)

    coords = route_res["coords"]
    total_miles = route_res["total_miles"]
    duration_hours = route_res["duration_hours"]

    # Step 3: Compute cumulative distances along polyline
    cumulative_miles = _polyline_cumulative_distances(coords)

    result = {
        "coords": coords,
        "cumulative_miles": cumulative_miles,
        "total_miles": total_miles,
        "duration_hours": duration_hours,
        "start_coords": (start_lat, start_lon),
        "end_coords": (end_lat, end_lon),
    }

    cache.set(cache_key, result, timeout=86400)
    return result
