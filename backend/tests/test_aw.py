from datetime import UTC, date, datetime

import pytest
from aw_pages import LINE, correlation, index_page, not_found_page, reach_page, stub

from riverrat.sources import aw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IVtoV", ("IV", "V", None)),
        ("IIItoIVstandoutV", ("III", "IV", "V")),
        ("IIIplusstandoutVplus", ("III+", None, "V+")),
        ("IItoIIIminus", ("II", "III-", None)),
        ("I", ("I", None, None)),
        ("VI", ("VI", None, None)),
    ],
)
def test_parse_difficulty(raw, expected):
    parsed = aw.parse_difficulty(raw)
    assert (parsed.low, parsed.high, parsed.standout) == expected


@pytest.mark.parametrize("raw", [None, ""])
def test_parse_difficulty_missing(raw):
    assert aw.parse_difficulty(raw) is None


def test_parse_difficulty_rejects_unknown_format():
    with pytest.raises(aw.AWParseError):
        aw.parse_difficulty("Class 4")


def test_chunks_are_joined_and_unescaped():
    html = reach_page(5, river='Café "Creek" {Upper}')
    assert aw.parse_reach_page(html, 5).stub.river == 'Café "Creek" {Upper}'


def test_flight_payload_requires_next_data():
    with pytest.raises(aw.AWParseError):
        aw.flight_payload("<html><body>maintenance</body></html>")


def test_parse_index():
    stubs = aw.parse_index(
        index_page([stub(1), stub(2, river="Second", states=["USA-VRG", "USA-NCR"])])
    )
    assert [(s.id, s.river, s.states) for s in stubs] == [
        (1, "Testriver", ["USA-WVR"]),
        (2, "Second", ["USA-VRG", "USA-NCR"]),
    ]


def test_parse_index_without_stubs():
    with pytest.raises(aw.AWParseError):
        aw.parse_index(not_found_page())


def test_parse_reach_page_extracts_facts():
    html = reach_page(
        2378,
        river="Gauley",
        difficulty="IVtoV",
        next_release="2027-09-16",
        correlations=[
            correlation(source="CALCULATED", gauge_id="36510909", low=400, high=5000),
            correlation(gauge_id="03189600", primary=False, metric="levelFT", low=1.8, high=6),
        ],
    )
    detail = aw.parse_reach_page(html, 2378)

    assert detail.stub.id == 2378
    assert detail.stub.river == "Gauley"
    assert detail.length_mi == 12
    assert detail.gradient_fpm == 36
    assert detail.geometry == LINE
    assert detail.next_release == date(2027, 9, 16)
    assert detail.aw_updated_at == datetime(2025, 5, 26, 17, 40, 9, tzinfo=UTC)
    first, second = detail.gauges
    assert (first.source, first.source_id, first.is_primary) == ("CALCULATED", "36510909", True)
    assert first.range == aw.FlowRange(metric="cfs", min=400, max=5000)
    assert first.aw_status == "low-runnable"
    assert second.range == aw.FlowRange(metric="ft", min=1.8, max=6)
    assert not second.is_primary


def test_reach_page_has_no_free_text_fields():
    detail = aw.parse_reach_page(reach_page(7), 7)
    assert "Description" not in detail.model_dump_json()


def test_aw_open_ended_range_sentinels_become_none():
    html = reach_page(9, correlations=[correlation(low=0, high=100000)])
    (link,) = aw.parse_reach_page(html, 9).gauges
    assert (link.range.min, link.range.max) == (None, None)


def test_gauge_without_range():
    html = reach_page(9, correlations=[correlation(metric=None, status=None)])
    (link,) = aw.parse_reach_page(html, 9).gauges
    assert link.range is None
    assert link.aw_status is None


def test_reach_without_gauges_or_geometry():
    detail = aw.parse_reach_page(reach_page(9, correlations=[], geometry=None), 9)
    assert detail.gauges == []
    assert detail.geometry is None


def test_unexpected_geometry_is_a_parse_error():
    html = reach_page(9, geometry={"type": "Point", "coordinates": [-80.0, 38.0]})
    with pytest.raises(aw.AWParseError):
        aw.parse_reach_page(html, 9)


@pytest.mark.parametrize("html", [not_found_page(), reach_page(2379)])
def test_reach_not_found(html):
    with pytest.raises(aw.ReachNotFound):
        aw.parse_reach_page(html, 2378)
