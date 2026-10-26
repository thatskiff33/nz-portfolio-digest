"""Read-only Personal App access. Response bodies and credentials are never logged."""

import json
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import SecretStr


class AkahuError(Exception):
    """A safe message that contains no upstream content."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials to a redirect destination.
        return None


def exact_units(value: Any) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise AkahuError("Akahu returned invalid units.")
    try:
        units = Decimal(value)
    except InvalidOperation:
        raise AkahuError("Akahu returned invalid units.") from None
    if (
        not units.is_finite()
        or units.copy_abs() >= Decimal("1e16")
        or units.as_tuple().exponent < -24
    ):
        raise AkahuError("Akahu returned invalid units.")
    return units


def _reject_constant(value: str) -> None:
    raise ValueError("Non-standard JSON number")


class AkahuClient:
    def __init__(
        self,
        app_token: SecretStr,
        user_token: SecretStr,
        *,
        open_request: Callable | None = None,
        sleep: Callable = time.sleep,
    ):
        self._app_token = app_token
        self._user_token = user_token
        self._open = open_request or build_opener(NoRedirect()).open
        self._sleep = sleep

    def _get(self, path: str, cursor: str | None) -> dict:
        url = "https://api.akahu.io/v1" + path
        if cursor is not None:
            url += "?" + urlencode({"cursor": cursor})
        request = Request(  # noqa: S310 - fixed HTTPS Akahu origin
            url,
            headers={
                "X-Akahu-Id": self._app_token.get_secret_value(),
                "Authorization": "Bearer " + self._user_token.get_secret_value(),
            },
        )
        for attempt in range(5):
            try:
                with self._open(request, timeout=30) as response:
                    payload = json.loads(
                        response.read(), parse_float=Decimal, parse_constant=_reject_constant
                    )
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    raise AkahuError("Akahu returned an unsuccessful response.")
                return payload
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()
                if status != 429 or attempt == 4:
                    raise AkahuError(f"Akahu request failed (HTTP {status}).") from None
                delay = (
                    min(60, max(2**attempt, int(retry_after)))
                    if (retry_after.isascii() and retry_after.isdecimal() and len(retry_after) < 8)
                    else 2**attempt
                )
                self._sleep(delay)
            except (URLError, OSError, ValueError, HTTPException):
                raise AkahuError("Akahu request failed or returned invalid JSON.") from None
        raise AkahuError("Akahu rate limit exhausted.")

    def _list(self, path: str) -> list[dict]:
        items = []
        cursor = None
        seen = set()
        while True:
            payload = self._get(path, cursor)
            page = payload.get("items")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise AkahuError("Akahu returned an invalid list.")
            items.extend(page)
            cursors = payload.get("cursor", {})
            if not isinstance(cursors, dict):
                raise AkahuError("Akahu returned invalid pagination.")
            cursor = cursors.get("next")
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise AkahuError("Akahu returned invalid pagination.")
            seen.add(cursor)

    def accounts(self) -> list[dict]:
        return self._list("/accounts")

    def holdings(self, account_id: str) -> list[dict]:
        return self._list("/accounts/" + quote(account_id, safe="") + "/holdings")


def push_success(url: SecretStr) -> None:
    """The configured Kuma push URL includes its secret; never echo it."""
    raw = url.get_secret_value()
    try:
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise AkahuError("UPTIME_KUMA_PUSH_URL must be an HTTP(S) URL.")
        if parts.fragment:
            raise AkahuError("UPTIME_KUMA_PUSH_URL must not contain a fragment.")
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "status"
        ]
        query.append(("status", "up"))
        push_url = urlunsplit(parts._replace(query=urlencode(query)))
        with build_opener(NoRedirect()).open(push_url, timeout=15) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise AkahuError("Kuma success push was not accepted.")
    except (OSError, ValueError, HTTPException):
        raise AkahuError("Kuma success push failed.") from None
