"""JSON over HTTP with retries, for free data sources that sometimes fall over."""

import json
import logging
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Yahoo turns away requests that do not look like they come from a browser.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) portfolio-digest"
TIMEOUT = httpx.Timeout(30.0)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER = 60.0


class SourceError(Exception):
    """A source could not supply the data. The message is safe to show and log."""


class NotFound(SourceError):
    """The source does not know the symbol or currency. Retrying will not help."""


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)


class JsonFetcher:
    """GETs JSON, retrying network errors, rate limits and server errors with backoff.

    Numbers are parsed straight to Decimal, so a float never touches a price or rate.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 4,
        backoff: float = 2.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.attempts = attempts
        self.backoff = backoff
        self.max_response_bytes = max_response_bytes
        self.sleep = sleep

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> Any:
        for attempt in range(1, self.attempts + 1):
            wait = self.backoff * 2 ** (attempt - 1)
            try:
                with self.client.stream("GET", url, params=params) as response:
                    if response.status_code == 404:
                        raise NotFound(f"{_host(url)} has no data for this request (HTTP 404).")
                    if response.is_redirect:
                        raise SourceError(
                            f"{_host(url)} redirected the request; refusing to follow."
                        )
                    if response.status_code not in _RETRY_STATUSES:
                        if response.is_error:
                            raise SourceError(
                                f"{_host(url)} refused the request ({_status(response)})."
                            )
                        return _decode(response, url, self.max_response_bytes)
                    problem = f"{_status(response)} from {_host(url)}"
                    wait = max(wait, _retry_after(response))
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__} from {_host(url)}"
            if attempt == self.attempts:
                raise SourceError(f"{problem}, gave up after {self.attempts} attempts.")
            log.warning("%s; trying again in %.0f seconds.", problem, wait)
            self.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover


def _decode(response: httpx.Response, url: str, max_bytes: int) -> Any:
    chunks: list[bytes] = []
    size = 0
    try:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise SourceError(f"{_host(url)} exceeded the response-size limit.")
            chunks.append(chunk)
        return json.loads(b"".join(chunks), parse_float=Decimal)
    except (ValueError, RecursionError):
        raise SourceError(f"{_host(url)} sent something that is not JSON.") from None


def _host(url: str) -> str:
    return httpx.URL(url).host


def _status(response: httpx.Response) -> str:
    return f"HTTP {response.status_code}"


def _retry_after(response: httpx.Response) -> float:
    try:
        return min(float(response.headers.get("Retry-After", 0)), _MAX_RETRY_AFTER)
    except ValueError:
        return 0.0
