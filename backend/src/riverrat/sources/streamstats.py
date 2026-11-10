"""USGS StreamStats watershed delineation (streamstats.usgs.gov), DESIGN.md §5.1.

Delineates from a 10 m elevation grid, down to the exact point. NLDI's watersheds stop at
the NHDPlus catchment outlet, so they run large for a gauge far up a long catchment, and a
few are simply wrong. jobs/basins.py uses this to repair those. The point has to be on
StreamStats' stream grid; one just off it returns a sliver of hillside.
"""

from typing import Any

BASE_URL = "https://streamstats.usgs.gov/ss-delineate/v1/delineate/sshydro"

# USGS state FIPS codes (gauge.state_code) → StreamStats regions
REGIONS = {
    "01": "AL",
    "09": "CT",
    "10": "DE",
    "11": "DC",
    "12": "FL",
    "13": "GA",
    "18": "IN",
    "21": "KY",
    "24": "MD",
    "25": "MA",
    "34": "NJ",
    "36": "NY",
    "37": "NC",
    "39": "OH",
    "42": "PA",
    "45": "SC",
    "47": "TN",
    "50": "VT",
    "51": "VA",
    "54": "WV",
}


def delineate_url(region: str, lon: float, lat: float) -> str:
    return f"{BASE_URL}/{region}?lat={lat}&lon={lon}"


def parse_basin(body: Any) -> dict[str, Any] | None:
    """The watershed polygon as GeoJSON: the feature marked GlobalWshd, wherever it's nested."""
    if isinstance(body, dict):
        if body.get("type") == "Feature" and (body.get("properties") or {}).get("GlobalWshd") == 1:
            geometry = body.get("geometry")
            if geometry and geometry.get("type") in {"Polygon", "MultiPolygon"}:
                return geometry
            return None
        children = body.values()
    elif isinstance(body, list):
        children = body
    else:
        return None
    for child in children:
        found = parse_basin(child)
        if found:
            return found
    return None
