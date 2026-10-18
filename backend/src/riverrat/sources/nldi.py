"""USGS Network Linked Data Index (api.water.usgs.gov/nldi), DESIGN.md §4.5.

Links gauges to NHDPlus V2 reaches. An NHDPlus COMID is also the National Water Model's
reach (feature) ID. Watershed delineation will live here too (Phase 1, basins).
"""

from typing import Any

BASE_URL = "https://api.water.usgs.gov/nldi/linked-data"


def usgs_site_url(number: str) -> str:
    return f"{BASE_URL}/nwissite/USGS-{number}?f=json"


def parse_comid(body: dict[str, Any]) -> int | None:
    features = body.get("features") or [body]
    comid = (features[0].get("properties") or {}).get("comid") if features else None
    return int(comid) if comid else None


def basin_url(number: str) -> str:
    # splitCatchment=true would cut the last NHDPlus catchment at the gauge itself, but it
    # returned HTTP 502 on 2026-10-18. Unsplit basins stop at the catchment outlet, which
    # matches USGS drainage areas well except for gauges far up a long catchment.
    return f"{BASE_URL}/nwissite/USGS-{number}/basin?f=json&simplified=false&splitCatchment=false"


def parse_basin(body: dict[str, Any]) -> dict[str, Any] | None:
    """The watershed polygon as GeoJSON (Polygon or MultiPolygon), lon/lat."""
    features = body.get("features") or []
    geometry = features[0].get("geometry") if features else None
    if not geometry or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    return geometry
