import gzip
import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from riverrat.api import cache
from riverrat.api.main import app


class Thing(BaseModel):
    n: int


@pytest.fixture
def counted():
    """A cached response whose version the test sets, counting its builds."""
    state = {"version": 1, "builds": 0}

    def build(_session):
        state["builds"] += 1
        return Thing(n=state["builds"])

    cached = cache.register("test-thing", lambda _session: state["version"], build)
    yield cached, state
    cache._registry.pop("test-thing")


def _request(accept_encoding: str = "") -> Request:
    headers = [(b"accept-encoding", accept_encoding.encode())] if accept_encoding else []
    return Request({"type": "http", "headers": headers})


def test_a_response_is_built_once_until_its_data_changes(counted):
    cached, state = counted
    first = cache.respond(cached, _request(), None)
    again = cache.respond(cached, _request(), None)
    assert json.loads(first.body) == json.loads(again.body) == {"n": 1}
    assert state["builds"] == 1

    state["version"] = 2
    assert json.loads(cache.respond(cached, _request(), None).body) == {"n": 2}
    assert state["builds"] == 2


def test_gzip_is_sent_to_clients_that_take_it(counted):
    cached, _ = counted
    response = cache.respond(cached, _request("gzip, deflate"), None)
    assert response.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(response.body)) == {"n": 1}
    assert "content-encoding" not in cache.respond(cached, _request(), None).headers


def test_a_failed_build_is_retried_on_the_next_request(counted):
    cached, state = counted
    broken = cache.Cached(cached.key, cached.version, lambda _s: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        cache.respond(broken, _request(), None)
    assert json.loads(cache.respond(cached, _request(), None).body) == {"n": 1}
    assert state["builds"] == 1


def test_the_cached_endpoints_keep_their_schemas():
    """The web's types are generated from the OpenAPI schema (web/src/api.ts)."""
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    for path, model in [
        ("/api/outlook", "Outlook"),
        ("/api/map", "ReachMap"),
        ("/api/track-record", "TrackRecord"),
    ]:
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith(f"/{model}")
    reaches = paths["/api/reaches"]["get"]["responses"]["200"]["content"]["application/json"]
    assert reaches["schema"]["items"]["$ref"].endswith("/ReachSummary")
