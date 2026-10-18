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
