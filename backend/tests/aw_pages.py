"""Synthetic American Whitewater pages for tests.

These mimic how AW's Next.js pages embed data (`self.__next_f.push([1, "..."])` chunks,
"$..." back-references, a description blob we must ignore) using made-up content only.
Never commit real AW pages: the repo is public.
"""

import json
from typing import Any

LINE = {"type": "LineString", "coordinates": [[-80.88, 38.21], [-80.90, 38.22], [-80.93, 38.23]]}


def flight_html(payload: str, chunks: int = 3) -> str:
    size = len(payload) // chunks + 1
    parts = [payload[i : i + size] for i in range(0, len(payload), size)]
    scripts = "".join(
        # Alternate ASCII-escaped and raw UTF-8 chunks; AW pages contain both.
        f"<script>self.__next_f.push([1,{json.dumps(part, ensure_ascii=i % 2 == 0)}])</script>"
        for i, part in enumerate(parts)
    )
    return f"<!DOCTYPE html><html><head><title>AW</title></head><body>{scripts}</body></html>"


def stub(aw_id: int, river="Testriver", section="Upper Gorge", difficulty="IVtoV", states=None):
    return {
        "id": str(aw_id),
        "section": section,
        "altname": "",
        "river": river,
        "difficulty": difficulty,
        "states": states or ["USA-WVR"],
    }


def correlation(
    source="USGS",
    gauge_id="03190000",
    name="TESTRIVER NEAR TESTVILLE, WV",
    primary=True,
    metric: str | None = "cfs",
    low: float | None = 400,
    high: float | None = 5000,
    status: str | None = "low-runnable",
) -> dict[str, Any]:
    details = None
    if metric is not None:
        details = {"data": {"metric": metric, "beginLowRunnable": low, "endHighRunnable": high}}
    return {
        "isPrimary": primary,
        "gaugeInfo": {"id": gauge_id, "name": name, "source": source},
        "correlationDetails": details,
        "latestReading": {"value": 1234, "dateTime": "2026-10-18T17:45:00.000Z"},
        "status": {"status": status} if status else None,
        "adjustedDifficulty": None,
    }


def index_page(stubs: list[dict[str, Any]]) -> str:
    payload = "\n".join(
        [
            '0:["$","html",null,{"lang":"en","children":"$L1"}]',
            '3:["$","div",null,{"className":"max-w-7xl","children":[["$","$L12",null,'
            f'{{"reachStubs":{json.dumps(stubs)}}}]]}}]',
        ]
    )
    return flight_html(payload)


def reach_page(
    aw_id: int,
    *,
    correlations: list[dict[str, Any]] | None = None,
    geometry: dict[str, Any] | None = LINE,
    next_release: str | None = None,
    **stub_fields: Any,
) -> str:
    rid = str(aw_id)
    reach_stub = stub(aw_id, **stub_fields)
    sidebar = {
        "difficulty": reach_stub["difficulty"],
        "length": 12,
        "averageGradient": 36,
        "permitUrl": "",
        "permitInfo": "",
        "correlations": correlations if correlations is not None else [correlation()],
        "reachId": rid,
        "reachInfoUpdated": "2025-05-26T17:40:09.000Z",
        "nextRelease": {"date": next_release, "link": "https://example.org"}
        if next_release
        else None,
    }
    lines = [
        '0:["$","html",null,{"lang":"en","children":"$L1"}]',
        f'5:["$","$L12",null,{{"mobileMenuExtras":["$","$L14",null,'
        f'{{"reachId":"{rid}","stub":{json.dumps(reach_stub)}}}]}}]',
        # Next.js back-references repeat keys with "$..." strings; the parser must skip them.
        '7:["$","$L15",null,{"stub":"$5:props:mobileMenuExtras:props:stub"}]',
    ]
    if geometry is not None:
        lines += [
            f'8:["$","$L17",null,{{"reachId":"{rid}","geometry":{json.dumps(geometry)}}}]',
            f'9:["$","$L18",null,{{"reachId":"{rid}","geometry":'
            '{"type":"LineString","coordinates":"$8:props:geometry:coordinates"}}]',
        ]
    lines += [
        f'6:[["$","$L19",null,{json.dumps(sidebar)}]]',
        # Free text we must never extract. Braces and quotes test the decoder's robustness.
        '1c:T99,**Description:** Made-up "text" with {braces} and a café; not stored.',
    ]
    return flight_html("\n".join(lines))


def not_found_page() -> str:
    return flight_html('0:["$","html",null,{"children":[["$","title",null,{"children":"404"}]]}]')
