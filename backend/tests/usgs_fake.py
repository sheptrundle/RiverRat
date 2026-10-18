"""A fake USGS Water Data API for tests, serving made-up sites and readings."""

from datetime import datetime

import httpx2

from riverrat.sources import usgs


def site_feature(number: str, lon: float = -81.18, lat: float = 38.23, area=1316.0):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "id": usgs.site_id(number),
            "monitoring_location_number": number,
            "monitoring_location_name": f"TEST RIVER AT SITE {number}",
            "drainage_area": area,
            "state_code": "54",
            "hydrologic_unit_code": "050500050809",
        },
    }


def series_feature(
    number: str,
    code: str = "00060",
    stat: str | None = "00011",
    begin="1996-10-01T04:30:00.000001",
    end="2026-10-18T09:00:00.000001",
):
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "monitoring_location_id": usgs.site_id(number),
            "parameter_code": code,
            "statistic_id": stat,
            "begin": begin,
            "end": end,
        },
    }


def reading(number: str, ts: str, value, code="00060", status="Provisional", qualifier=None):
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "monitoring_location_id": usgs.site_id(number),
            "parameter_code": code,
            "time": ts,
            "value": value,
            "approval_status": status,
            "qualifier": qualifier,
        },
    }


def csv_body(rows: list[tuple[datetime, float | str]]) -> str:
    lines = ["x,y,time,value,approval_status,qualifier"]
    lines += [f",,{ts.isoformat(sep=' ')},{value},Approved," for ts, value in rows]
    return "\n".join(lines) + "\n"


class FakeUSGS:
    """Routes by collection name. `history` maps (site number, parameter code) to a
    function returning CSV rows for a [start, end) window."""

    def __init__(self, sites=(), series=(), readings=(), history=None, page_size=10_000):
        self.sites = list(sites)
        self.series = list(series)
        self.readings = list(readings)
        self.history = history or {}
        self.page_size = page_size
        self.requests: list[httpx2.URL] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request.url)
        collection = request.url.path.split("/")[-2]
        params = request.url.params
        if collection == "monitoring-locations":
            wanted = set(params["id"].split(","))
            return self._page(request, [f for f in self.sites if f["properties"]["id"] in wanted])
        if collection == "time-series-metadata":
            wanted = set(params["monitoring_location_id"].split(","))
            matches = [
                f for f in self.series if f["properties"]["monitoring_location_id"] in wanted
            ]
            return self._page(request, matches)
        if collection == "continuous" and "bbox" in params:
            return self._page(request, self.readings)
        if collection == "continuous":
            number = params["monitoring_location_id"].removeprefix("USGS-")
            start, end = (datetime.fromisoformat(t) for t in params["time"].split("/"))
            rows = self.history.get((number, params["parameter_code"]), lambda s, e: [])(start, end)
            return httpx2.Response(200, text=csv_body(rows[: int(params["limit"])]))
        return httpx2.Response(404)

    def _page(self, request: httpx2.Request, items: list) -> httpx2.Response:
        params = request.url.params
        offset = int(params.get("offset", 0))
        page = items[offset : offset + self.page_size]
        links = []
        if offset + self.page_size < len(items):
            nxt = request.url.copy_merge_params({"offset": offset + self.page_size})
            links.append({"rel": "next", "href": str(nxt)})
        body = {"type": "FeatureCollection", "features": page, "links": links}
        return httpx2.Response(200, json=body)

    def single_site_history_requests(self) -> int:
        return sum(
            1
            for url in self.requests
            if url.path.endswith("continuous/items") and "bbox" not in url.params
        )
