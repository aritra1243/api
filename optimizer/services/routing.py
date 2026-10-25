"""
Routing and geocoding service supporting:
1. Geoapify (using provided API key, 3,000 requests/day free tier)
2. OSRM / OpenStreetMap (100% free fallback, zero key needed)
3. OpenRouteService (optional fallback)

Strategy:
- Geocodes start and finish locations (using Geoapify with Nominatim fallback)
- Calls routing engine ONCE to retrieve the full highway geometry and distance
- Computes cumulative distances locally
- Caches all results in memory for maximum speed
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

# ORS base URL (Optional)
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
    """Given a list of (lat, lon) points, calculate cumulative mileage at each point."""
    distances = [0.0]
    for i in range(1, len(coords)):
        d = _haversine_miles(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        distances.append(distances[-1] + d)
    return distances


def _geocode_geoapify(place: str, api_key: str) -> tuple[float, float] | None:
    """Geocode place using Geoapify API."""
    url = "https://api.geoapify.com/v1/geocode/search"
    params = {
        "text": place,
        "apiKey": api_key,
        "filter": "countrycode:us",
        "limit": 1,
    }
    session = _make_session(retries=2, backoff=0.5)
    try:
        resp = session.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            features = data.get("features", [])
            if features:
                coords = features[0]["geometry"]["coordinates"]
                # GeoJSON coordinates are [lon, lat]
                return float(coords[1]), float(coords[0])
    except Exception as e:
        logger.warning("Geoapify geocoding error for '%s': %s", place, e)
    return None


def _geocode_nominatim(place: str) -> tuple[float, float] | None:
    """Geocode place using Nominatim OpenStreetMap (no key)."""
    url = "https://nominatim.openstreetmap.org/search"
    cleaned = place.strip()
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
    session = _make_session(retries=2, backoff=1.0)
    try:
        resp = session.get(url, params=params, headers=headers, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        logger.warning("Nominatim geocoding error for '%s': %s", place, e)
    return None


def geocode(place: str) -> tuple[float, float]:
    """
    Geocode a place name or coordinate string to (latitude, longitude).
    1. Direct lat,lon parsing (e.g. '40.7128, -74.0060')
    2. Common US cities cache (0ms)
    3. Django cache (0ms)
    4. Geoapify (if key configured)
    5. Nominatim OpenStreetMap (free fallback)
    """
    cleaned = place.strip()

    # 1. Literal coordinates
    coord_match = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)$", cleaned)
    if coord_match:
        return float(coord_match.group(1)), float(coord_match.group(2))

    # 2. Common cities
    key_upper = cleaned.upper()
    if key_upper in COMMON_US_CITIES:
        return COMMON_US_CITIES[key_upper]

    # 3. Cache
    geo_cache_key = "geo_" + hashlib.md5(cleaned.lower().encode()).hexdigest()
    cached = cache.get(geo_cache_key)
    if cached:
        return cached

    # 4. Geoapify
    geoapify_key = getattr(settings, "GEOAPIFY_API_KEY", "").strip()
    result = None
    if geoapify_key:
        result = _geocode_geoapify(cleaned, geoapify_key)

    # 5. Nominatim fallback
    if not result:
        result = _geocode_nominatim(cleaned)

    if not result:
        raise ValueError(
            f"Could not locate '{place}' within the USA. "
            "Please provide a valid US city name, e.g. 'Chicago, IL' or 'Austin, TX'."
        )

    cache.set(geo_cache_key, result, timeout=86400)
    return result


def _get_route_geoapify(start_lat: float, start_lon: float, end_lat: float, end_lon: float) -> dict:
    """Fetch driving route from Geoapify Routing API."""
    api_key = getattr(settings, "GEOAPIFY_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEOAPIFY_API_KEY is not configured.")

    url = "https://api.geoapify.com/v1/routing"
    params = {
        "waypoints": f"{start_lat},{start_lon}|{end_lat},{end_lon}",
        "mode": "drive",
        "apiKey": api_key,
    }
    session = _make_session(retries=2, backoff=1.0)
    logger.info("Calling Geoapify Routing API: (%f, %f) -> (%f, %f)", start_lat, start_lon, end_lat, end_lon)
    resp = session.get(url, params=params, timeout=25)
    resp.raise_for_status()
    data = resp.json()

    feat = data["features"][0]
    props = feat["properties"]
    geom = feat["geometry"]

    distance_miles = props["distance"] * 0.000621371  # meters to miles
    duration_hours = props["time"] / 3600.0           # seconds to hours

    # Geoapify returns LineString or MultiLineString with [lon, lat] points
    if geom["type"] == "MultiLineString":
        raw_coords = [c for line in geom["coordinates"] for c in line]
    else:
        raw_coords = geom["coordinates"]

    # Convert to (lat, lon)
    coords = [(c[1], c[0]) for c in raw_coords]

    return {
        "coords": coords,
        "total_miles": distance_miles,
        "duration_hours": duration_hours,
    }


def _get_route_osrm(start_lon: float, start_lat: float, end_lon: float, end_lat: float) -> dict:
    """Fetch route from OSRM public demo server (100% free fallback)."""
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
                    coords = [(c[1], c[0]) for c in geojson_coords]
                    total_miles = route["distance"] * 0.000621371
                    duration_hours = route["duration"] / 3600.0
                    return {
                        "coords": coords,
                        "total_miles": total_miles,
                        "duration_hours": duration_hours,
                    }
        except Exception as e:
            logger.warning("OSRM server %s failed: %s", base_url, e)
            last_error = e

    raise RuntimeError(f"Unable to fetch driving route from OSRM servers: {last_error}")


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
    1. Geocodes start and finish locations (Geoapify / Nominatim)
    2. Checks in-memory cache for existing route
    3. Fetches driving route:
       - Primary: Geoapify API (using user's API key)
       - Fallback: OSRM open routing engine
    4. Computes cumulative polyline mileage
    5. Caches and returns normalized route dictionary
    """
    cache_key = "route_" + hashlib.md5(f"{start}|{finish}".lower().encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached:
        logger.info("Route cache HIT for %s -> %s", start, finish)
        return cached

    # Step 1: Geocode
    start_lat, start_lon = geocode(start)
    end_lat, end_lon = geocode(finish)

    # Step 2: Route using configured engine with automatic fallback
    engine = getattr(settings, "ROUTING_ENGINE", "geoapify").lower()
    geoapify_key = getattr(settings, "GEOAPIFY_API_KEY", "").strip()
    ors_key = getattr(settings, "ORS_API_KEY", "").strip()

    route_res = None

    if engine == "geoapify" and geoapify_key:
        try:
            route_res = _get_route_geoapify(start_lat, start_lon, end_lat, end_lon)
        except Exception as e:
            logger.warning("Geoapify routing failed (%s), falling back to OSRM", e)
            route_res = _get_route_osrm(start_lon, start_lat, end_lon, end_lat)
    elif engine == "ors" and ors_key:
        try:
            route_res = _get_route_ors(start_lon, start_lat, end_lon, end_lat)
        except Exception as e:
            logger.warning("ORS routing failed (%s), falling back to OSRM", e)
            route_res = _get_route_osrm(start_lon, start_lat, end_lon, end_lat)
    else:
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
