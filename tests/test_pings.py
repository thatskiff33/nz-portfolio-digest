"""Monitoring pings refuse redirects and insecure public URLs, and never log the URL."""

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from portfolio_digest import pings, report_service
from portfolio_digest.akahu import AkahuError, push_success
from portfolio_digest.scheduling import ScheduleConfig

SECRET = "invented-push-token"  # noqa: S105 (fictitious)


@pytest.fixture
def kuma():
    """A loopback server that answers /ok with Kuma's reply and anything else with a 302."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/ok"):
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "/ok/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", hits
    server.shutdown()
    server.server_close()


def test_ping_succeeds_over_http_to_loopback(kuma):
    base, hits = kuma
    assert pings.ping(f"{base}/ok/{SECRET}") == b'{"ok":true}'
    assert hits == [f"/ok/{SECRET}"]


def test_redirect_is_a_failure_and_is_not_followed(kuma, caplog):
    base, hits = kuma
    with pytest.raises(pings.PingError) as caught:
        pings.ping(f"{base}/moved/{SECRET}")
    assert hits == [f"/moved/{SECRET}"]
    assert SECRET not in str(caught.value)


def test_akahu_success_push_treats_a_redirect_as_failure(kuma):
    base, hits = kuma
    with pytest.raises(AkahuError) as caught:
        push_success(SecretStr(f"{base}/moved/{SECRET}"))
    assert len(hits) == 1
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://kuma.example.invalid/api/push/{SECRET}",
        f"http://203.0.113.5/api/push/{SECRET}",
        f"http://100.128.0.1/api/push/{SECRET}",
        f"ftp://127.0.0.1/api/push/{SECRET}",
        f"https://kuma.example.invalid/api/push/{SECRET}#fragment",
        "https:///no-host",
    ],
)
def test_insecure_or_malformed_urls_are_refused_without_a_request(monkeypatch, url):
    opener = Mock()
    monkeypatch.setattr(pings, "_opener", opener)
    with pytest.raises(pings.PingError) as caught:
        pings.ping(url)
    opener.open.assert_not_called()
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://kuma.example.invalid/api/push/x",
        "http://localhost:3001/api/push/x",
        "http://127.0.0.1/api/push/x",
        "http://[::1]/api/push/x",
        "http://100.64.0.1/api/push/x",
        "http://100.127.255.254/api/push/x",
    ],
)
def test_https_or_private_addresses_are_allowed(url):
    pings.check_url(url)


def test_ping_uses_a_ten_second_timeout(monkeypatch):
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=Mock(status=200, read=lambda: b""))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(pings, "_opener", opener)
    pings.ping("https://kuma.example.invalid/api/push/x")
    assert opener.open.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("path", ["moved", "ok"])
def test_report_ping_never_logs_the_url(kuma, caplog, path):
    base, _ = kuma
    caplog.set_level(logging.DEBUG)
    config = ScheduleConfig(schedules=[], kuma_push_url=SecretStr(f"{base}/{path}/{SECRET}"))
    report_service.ping_kuma(config)
    assert SECRET not in caplog.text
    assert ("monitoring push failed" in caplog.text) == (path == "moved")


def test_report_ping_refuses_http_to_a_public_host(monkeypatch, caplog):
    opener = Mock()
    monkeypatch.setattr(pings, "_opener", opener)
    url = f"http://kuma.example.invalid/api/push/{SECRET}"
    report_service.ping_kuma(ScheduleConfig(schedules=[], kuma_push_url=SecretStr(url)))
    opener.open.assert_not_called()
    assert "monitoring push failed" in caplog.text
    assert SECRET not in caplog.text
