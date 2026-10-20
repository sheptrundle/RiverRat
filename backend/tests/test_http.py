import httpx2
import pytest

from riverrat.http import PoliteClient


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(handler, fake: FakeTime, min_interval: float = 5.0) -> PoliteClient:
    return PoliteClient(
        "test-agent",
        min_interval,
        transport=httpx2.MockTransport(handler),
        sleep=fake.sleep,
        clock=fake.clock,
    )


def test_waits_between_requests_and_sends_user_agent():
    agents = []

    def handler(request):
        agents.append(request.headers["User-Agent"])
        return httpx2.Response(200, text="ok")

    fake = FakeTime()
    with make_client(handler, fake) as client:
        client.get("https://example.org/a")
        fake.now += 1.5  # time spent processing between requests counts toward the wait
        client.get("https://example.org/b")

    assert fake.sleeps == [3.5]
    assert agents == ["test-agent", "test-agent"]


def test_retries_transient_errors_honoring_retry_after():
    responses = iter(
        [httpx2.Response(503, headers={"Retry-After": "30"}), httpx2.Response(200, text="ok")]
    )
    fake = FakeTime()
    with make_client(lambda request: next(responses), fake) as client:
        assert client.get("https://example.org").text == "ok"
    assert fake.sleeps == [30.0]


def test_does_not_retry_client_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(404)

    with make_client(handler, FakeTime()) as client, pytest.raises(httpx2.HTTPStatusError):
        client.get("https://example.org/missing")
    assert len(calls) == 1


def test_gives_up_after_max_retries():
    fake = FakeTime()
    with make_client(lambda request: httpx2.Response(500), fake) as client:
        with pytest.raises(httpx2.HTTPStatusError):
            client.get("https://example.org")
    assert fake.sleeps == [5.0, 10.0, 20.0, 40.0]


def test_waits_out_network_outages():
    fake = FakeTime()
    calls = []

    def handler(request):
        calls.append(fake.now)
        if fake.now < 600:  # ten minutes without a network
            raise httpx2.ConnectError("Name or service not known")
        return httpx2.Response(200, text="ok")

    with make_client(handler, fake) as client:
        assert client.get("https://example.org").text == "ok"
    assert fake.sleeps[:4] == [5.0, 10.0, 20.0, 40.0]
    assert set(fake.sleeps[4:]) == {60.0}  # then checks once a minute


def test_gives_up_on_long_outages():
    fake = FakeTime()

    def handler(request):
        raise httpx2.ConnectError("Name or service not known")

    with make_client(handler, fake) as client, pytest.raises(httpx2.ConnectError):
        client.get("https://example.org")
    assert 30 * 60 <= fake.now < 32 * 60


def test_gives_up_on_repeated_read_errors():
    fake = FakeTime()

    def handler(request):
        raise httpx2.ReadError("Connection reset by peer")

    with make_client(handler, fake) as client, pytest.raises(httpx2.ReadError):
        client.get("https://example.org")
    assert fake.sleeps == [5.0, 10.0, 20.0, 40.0]


def test_sends_extra_headers():
    seen = []

    def handler(request):
        seen.append(request.headers.get("Range"))
        return httpx2.Response(206, content=b"ab")

    with make_client(handler, FakeTime()) as client:
        assert client.get("https://example.org", headers={"Range": "bytes=0-1"}).content == b"ab"
    assert seen == ["bytes=0-1"]
