"""Monitoring pings (Uptime Kuma push URLs). The URL carries a secret and is never logged."""

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

TIMEOUT_SECONDS = 10
# Tailscale addresses. Plain HTTP stays inside the encrypted tailnet.
_TAILNET = ip_network("100.64.0.0/10")


class PingError(Exception):
    """A safe message that never includes the URL or upstream content."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or secret URLs to a redirect destination.
        return None


_opener = build_opener(NoRedirect())


def _private_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or (address.version == 4 and address in _TAILNET)


def check_url(url: str) -> None:
    """Require HTTPS, except to a loopback or Tailscale address."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise PingError("The monitoring push URL is invalid.") from None
    if not host or parts.fragment:
        raise PingError("The monitoring push URL is invalid.")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and _private_host(host):
        return
    raise PingError("The monitoring push URL must use HTTPS unless it is a private address.")


def ping(url: str) -> bytes:
    """GET a push URL without following redirects. Returns the body of a 200 response."""
    check_url(url)
    try:
        # urllib does not log request URLs, unlike httpx.
        with _opener.open(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise PingError("The monitoring push was not accepted.")
            return response.read()
    except PingError:
        raise
    except Exception:
        # Exceptions from urllib can quote the URL; drop them.
        raise PingError("The monitoring push failed.") from None
