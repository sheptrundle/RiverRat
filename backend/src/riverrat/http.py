"""A throttled, retrying HTTP client shared by every external data source."""

import logging
import time

import httpx2

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# While the network is down (DNS fails, connections refused), keep trying this long,
# checking at most this often, before giving up: long backfills shouldn't die when the
# laptop's Wi-Fi drops or it sleeps for a while.
OUTAGE_PATIENCE_S = 30 * 60
OUTAGE_POLL_S = 60.0


class PoliteClient:
    """Read-only (GET/HEAD) client that waits `min_interval` seconds between requests and retries
    transient failures with exponential backoff (honoring Retry-After). Failures to connect
    at all are retried for up to `outage_patience` seconds."""

    def __init__(
        self,
        user_agent: str,
        min_interval: float,
        headers: dict[str, str] | None = None,
        max_retries: int = 4,
        outage_patience: float = OUTAGE_PATIENCE_S,
        timeout: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self._client = httpx2.Client(
            headers={"User-Agent": user_agent, **(headers or {})},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._outage_patience = outage_patience
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    def get(self, url: str) -> httpx2.Response:
        return self._request("GET", url)

    def head(self, url: str) -> httpx2.Response:
        return self._request("HEAD", url)

    def _request(self, method: str, url: str) -> httpx2.Response:
        attempt = 0
        outage_since: float | None = None
        while True:
            self._wait_turn()
            try:
                response = self._client.request(method, url)
            except httpx2.ConnectError as exc:
                # Nothing reached the server, so waiting for the network costs it nothing.
                now = self._clock()
                outage_since = now if outage_since is None else outage_since
                if now - outage_since >= self._outage_patience:
                    raise
                delay = min(self._backoff(attempt), OUTAGE_POLL_S)
                attempt = min(attempt + 1, self._max_retries)
                log.warning("%s %s failed (%s); retrying in %.0fs", method, url, exc, delay)
            except httpx2.TransportError as exc:
                if attempt == self._max_retries:
                    raise
                delay = self._backoff(attempt)
                attempt += 1
                log.warning("%s %s failed (%s); retrying in %.0fs", method, url, exc, delay)
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == self._max_retries:
                    response.raise_for_status()
                    return response
                delay = _retry_after(response) or self._backoff(attempt)
                attempt += 1
                log.warning(
                    "%s %s -> %s; retrying in %.0fs", method, url, response.status_code, delay
                )
            self._sleep(delay)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _wait_turn(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            remaining = self._min_interval - (now - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._clock()

    def _backoff(self, attempt: int) -> float:
        return max(self._min_interval, 2.0) * 2**attempt


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else None
