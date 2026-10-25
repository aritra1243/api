# Fuel Route Optimizer API

A high-performance Django REST API that plans fuel-efficient driving routes across the USA, identifying optimal, cost-effective fuel stops along the route.

## Key Highlights
- 🆓 **Free Routing API Support**: Integrates **Geoapify Routing & Geocoding API** (3,000 free requests/day) with automatic zero-downtime fallback to **OSRM (Open Source Routing Machine)**.
- ⛽ **Optimal Fuel Stops**: Constrained by a **500-mile vehicle range**, the algorithm identifies the most cost-effective truck stops to refuel along the route corridor.
- 💵 **Accurate Fuel Accounting**: Assumes **10 miles per gallon (MPG)**, returning exact gallons consumed and total money spent on fuel.
- 🗺️ **Interactive Route Maps**: Automatically generates interactive route maps with Folium showing start, destination, fuel stops with pricing popups, and the driving path.
- ⚡ **Ultra-Fast Performance**: Sub-2-second end-to-end response times using KDTree spatial indexing (`scipy.spatial.cKDTree`) across 6,600+ fuel stations.

---

## Architecture Overview

```
POST /api/route/
    │
    ├─► Input Validation (RouteRequestSerializer)
    │
    ├─► Routing Service (OSRM / OpenStreetMap Engine)
    │       ├─ Instant Geocode Cache + Nominatim OSM fallback
    │       └─ OSRM Directions (GeoJSON polyline, cumulative miles, duration)
    │
    ├─► Fuel Optimizer (scipy.spatial.cKDTree)
    │       ├─ Spatial corridor filter (±25 miles along route)
    │       ├─ Fast KDTree projection of 6,600+ stations onto 1D route
    │       ├─ Greedy lookahead: picks cheapest station before 500-mile limit
    │       └─ 10 MPG fuel cost & gallons calculation
    │
    ├─► Interactive Map Generator (Folium)
    │       └─ Lightweight HTML map saved to /maps/
    │
    └─► JSON Response
```

---

## Quickstart

### 1. Requirements & Dependencies
Ensure Python 3.10+ is installed.
```bash
pip install -r requirements.txt
```

### 2. Environment Configuration
Copy `.env.example` to `.env`. No external API keys are required:
```bash
cp .env.example .env
```
*(Optional: OpenRouteService is also supported if you set `ROUTING_ENGINE=ors` and provide `ORS_API_KEY`.)*

### 3. Load Fuel Station Data
The assessment fuel prices CSV is located at `data/fuel-prices-for-be-assessment.csv`.
Run the management command to load and geocode the truck stops:
```bash
python manage.py migrate
python manage.py load_fuel_data --clear
```
This imports **6,614 unique US fuel stations** across all 50 states into the SQLite database.

### 4. Run the Development Server
```bash
python manage.py runserver 8000
```

---

## API Endpoints

### 1. Health Check
```http
GET /api/route/health/
```
**Sample Response:**
```json
{
  "status": "ok",
  "fuel_stations_loaded": 6614,
  "vehicle": {
    "range_miles": 500,
    "mpg": 10,
    "tank_gallons": 50.0
  },
  "message": "Ready!"
}
```

### 2. Route & Fuel Optimization
```http
POST /api/route/
Content-Type: application/json

{
    "start": "New York, NY",
    "finish": "Chicago, IL"
}
```

**Sample Response:**
```json
{
  "route": {
    "start": "New York, NY",
    "finish": "Chicago, IL",
    "total_distance_miles": 790.5,
    "estimated_duration_hours": 14.8,
    "start_coordinates": {
      "lat": 40.7128,
      "lon": -74.006
    },
    "end_coordinates": {
      "lat": 41.8781,
      "lon": -87.6298
    }
  },
  "vehicle": {
    "max_range_miles": 500,
    "fuel_efficiency_mpg": 10,
    "tank_capacity_gallons": 50.0
  },
  "fuel_stops": [
    {
      "stop_number": 1,
      "station_id": "OPIS-72445",
      "station_name": "SHEETZ #639",
      "address": "I-80 Exit 223",
      "city": "YOUNGSTOWN",
      "state": "OH",
      "latitude": 41.030596,
      "longitude": -80.640454,
      "price_per_gallon": 3.059,
      "distance_from_start_miles": 390.3,
      "miles_to_this_stop": 390.3,
      "gallons_purchased": 39.03,
      "cost_at_stop": 119.39
    }
  ],
  "summary": {
    "total_fuel_stops": 1,
    "total_gallons_needed": 79.05,
    "total_fuel_cost_usd": 241.81,
    "average_price_per_gallon": 3.059
  },
  "map": {
    "interactive_map_url": "http://localhost:8000/maps/64aa0f75e14b.html",
    "note": "Open the map URL in a browser for an interactive route view."
  },
  "_meta": {
    "response_time_seconds": 1.579,
    "stations_in_db": 6614
  }
}
```

---

## Algorithm Details

1. **Spatial Projection (`cKDTree`)**:
   Candidate fuel stations along the route corridor (within 25 miles) are identified in milliseconds using a 2D bounding corridor and `scipy.spatial.cKDTree` nearest-neighbor projection.
2. **Range Constraints**:
   - The vehicle has a **500-mile maximum range** (50-gallon tank at 10 MPG).
   - The optimizer scans stations within the reachable lookahead window (between 280 and 480 miles ahead) and selects the cheapest station.
   - Distance between any two consecutive refueling stops never exceeds the 500-mile limit.
3. **Cost & Gallon Accounting**:
   - Gallons purchased at each stop cover the mileage driven on that segment (`segment_miles / 10`).
   - The final segment to the destination is accounted for using the last stop's price.
   - Total gallons equals `total_distance / 10`.

---

## Running the Test Suite

```bash
python manage.py test
```
Runs the unit and integration tests covering:
- Health check responses
- Input validation (missing locations, invalid payloads)
- 500-mile vehicle range constraint enforcement
- Exact 10 MPG fuel consumption and cost calculations
