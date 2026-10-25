"""
Map generation service using Folium.

Generates an interactive HTML map showing:
- Route polyline (blue)
- Start marker (green)
- End marker (red flag)
- Fuel stop markers (orange gas pump icons)
- Popup info for each stop
"""
import hashlib
import os
import logging

import folium
from django.conf import settings

logger = logging.getLogger(__name__)


def generate_route_map(route_data: dict, fuel_stops: list, map_id: str) -> str:
    """
    Generate an interactive Folium map and save it as HTML.

    Args:
        route_data: Output from routing.get_route()
        fuel_stops: List of fuel stop dicts from optimizer
        map_id: Unique identifier for the map file

    Returns:
        Relative URL path to the map file (e.g., '/maps/abc123.html')
    """
    coords = route_data["coords"]
    start_coords = route_data["start_coords"]
    end_coords = route_data["end_coords"]
    total_miles = route_data["total_miles"]

    # Center map on midpoint of route
    mid_idx = len(coords) // 2
    center = coords[mid_idx]

    geoapify_key = getattr(settings, "GEOAPIFY_API_KEY", "").strip()
    if geoapify_key:
        tiles_url = f"https://maps.geoapify.com/v1/tile/osm-bright/{{z}}/{{x}}/{{y}}.png?apiKey={geoapify_key}"
        attribution = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, &copy; <a href="https://www.geoapify.com/">Geoapify</a>'
    else:
        tiles_url = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
        attribution = "Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS, Intermap, iPC, NRCAN, Esri Japan, METI, Esri China (Hong Kong), Esri (Thailand), TomTom"

    m = folium.Map(
        location=center,
        zoom_start=5,
        tiles=tiles_url,
        attr=attribution,
    )

    # ── Route polyline (downsample if over 2000 points for fast rendering) ─
    if len(coords) > 2000:
        step = max(1, len(coords) // 1500)
        display_coords = coords[::step]
        if display_coords[-1] != coords[-1]:
            display_coords.append(coords[-1])
    else:
        display_coords = coords

    folium.PolyLine(
        locations=display_coords,
        color="#2563EB",
        weight=5,
        opacity=0.85,
        tooltip=f"Route: {total_miles:.1f} miles (~{route_data.get('duration_hours', 0):.1f} hours)",
    ).add_to(m)

    # ── Start marker ────────────────────────────────────────────────────────
    folium.Marker(
        location=start_coords,
        popup=folium.Popup("<b>🚗 Start</b>", max_width=150),
        tooltip="Start",
        icon=folium.Icon(color="green", icon="play", prefix="fa"),
    ).add_to(m)

    # ── End marker ──────────────────────────────────────────────────────────
    folium.Marker(
        location=end_coords,
        popup=folium.Popup("<b>🏁 Destination</b>", max_width=150),
        tooltip="Destination",
        icon=folium.Icon(color="red", icon="flag", prefix="fa"),
    ).add_to(m)

    # ── Fuel stop markers ───────────────────────────────────────────────────
    for i, stop in enumerate(fuel_stops, start=1):
        popup_html = f"""
        <div style="font-family: Arial, sans-serif; min-width: 200px;">
            <h4 style="margin:0 0 6px; color:#1e40af;">⛽ Stop #{i}</h4>
            <b>{stop['station_name']}</b><br>
            {stop['city']}, {stop['state']}<br>
            <hr style="margin:4px 0">
            <table style="width:100%; font-size:13px;">
                <tr><td>Price:</td><td><b>${stop['price_per_gallon']:.3f}/gal</b></td></tr>
                <tr><td>From start:</td><td>{stop['distance_from_start_miles']:.0f} mi</td></tr>
                <tr><td>Gallons:</td><td>{stop['gallons_purchased']:.1f} gal</td></tr>
                <tr><td>Cost here:</td><td><b>${stop['cost_at_stop']:.2f}</b></td></tr>
            </table>
        </div>
        """
        folium.Marker(
            location=(stop["latitude"], stop["longitude"]),
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"Stop #{i}: {stop['station_name']} — ${stop['price_per_gallon']:.3f}/gal",
            icon=folium.Icon(color="orange", icon="tint", prefix="fa"),
        ).add_to(m)

    # ── Ensure maps directory exists ─────────────────────────────────────────
    os.makedirs(settings.MAPS_DIR, exist_ok=True)
    filename = f"{map_id}.html"
    filepath = os.path.join(settings.MAPS_DIR, filename)
    m.save(filepath)

    logger.info("Map saved to %s", filepath)
    return f"/maps/{filename}"
