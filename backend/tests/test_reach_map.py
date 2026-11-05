from sqlalchemy import func
from test_whatif_api import client, lake  # noqa: F401

from riverrat.db.models import Reach


def test_map_serves_each_reach_course_simplified(client, session):  # noqa: F811
    # a wiggle far smaller than a line's width, and a reach AW gives no course for
    wiggly = ", ".join(f"{-81 + i * 0.001} {38 + (i % 2) * 0.00001}" for i in range(50))
    session.add_all(
        [
            Reach(
                river="Gauley",
                section="Upper",
                states=["USA-WVR"],
                difficulty_raw="IV-V",
                geom=func.ST_GeomFromText(f"LINESTRING({wiggly})", 4326),
            ),
            Reach(river="Nowhere Creek", section="Lost", states=["USA-WVR"]),
        ]
    )
    session.commit()
    body = client.get("/api/map")
    assert body.status_code == 200
    assert "max-age" in body.headers["cache-control"]
    (reach,) = body.json()["reaches"]
    assert (reach["river"], reach["section"], reach["difficulty"]) == ("Gauley", "Upper", "IV-V")
    assert reach["line"][0] == [-81.0, 38.0]
    assert reach["line"][-1] == [-80.951, 38.0]  # rounded to about 10 m
    assert len(reach["line"]) < 10
