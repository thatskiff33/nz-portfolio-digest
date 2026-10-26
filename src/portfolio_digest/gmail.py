"""Read-only access to one Gmail label, for ASB Securities contract-note emails.

- The only scope ever requested is `gmail.readonly`, and a token granting anything more
  is refused. Nothing here can send, change or delete mail.
- Only messages carrying the configured label (GMAIL_LABEL) are read.
- The client secret and refresh token come from settings (`.env`). They are never printed
  or logged, and error messages carry only Google's short error code.

`authorise()` runs Google's desktop-app sign-in once to get a refresh token. It listens on
127.0.0.1 for Google's redirect, so run it on a computer with a web browser (or forward
the port over SSH).
"""

import base64
import binascii
import hashlib
import json
import os
import secrets
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from portfolio_digest.config import GmailSettings
from portfolio_digest.importers.asb import TRADE_SOURCE, NotAContractNote, parse_contract_note
from portfolio_digest.importers.base import FileProblem, ParseResult, RowError, RowProblem

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a password
API_URL = "https://gmail.googleapis.com/gmail/v1/users/me"

# (method, url, headers, body) -> parsed JSON. Swapped for a fake in tests.
Http = Callable[[str, str, dict[str, str], bytes | None], dict]


class GmailError(Exception):
    """Talking to Google failed. The message never includes a token or secret."""


def _google_reason(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read() or b"{}")
    except ValueError:
        return exc.reason or "no detail"
    error = payload.get("error")
    if isinstance(error, dict):  # Gmail API: {"error": {"status": "NOT_FOUND", ...}}
        return str(error.get("status") or error.get("code") or "no detail")
    return str(error or "no detail")  # OAuth: {"error": "invalid_grant", ...}


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict:
    if not url.startswith("https://"):
        raise GmailError("Refusing to talk to Google over an unencrypted connection.")
    request = Request(url, data=body, headers=headers, method=method)  # noqa: S310 - https only
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - https only
            return json.load(response)
    except HTTPError as exc:
        raise GmailError(
            f"Google refused the request ({exc.code} {_google_reason(exc)})."
        ) from None
    except (URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise GmailError(f"Cannot reach Google ({reason}).") from None
    except ValueError:
        raise GmailError("Google returned a response the app could not read.") from None


def _check_scope(token: dict) -> None:
    granted = set(str(token.get("scope", SCOPE)).split())
    if granted != {SCOPE}:
        raise GmailError(
            "The Gmail token grants more than read-only access. Run "
            "`portfolio-digest gmail authorise` again and use the new token."
        )


class GmailClient:
    def __init__(self, settings: GmailSettings, http: Http = urllib_http) -> None:
        settings.require("client_id", "client_secret", "refresh_token")
        self._settings = settings
        self._http = http
        self._access_token: str | None = None

    def _token(self) -> str:
        if self._access_token is None:
            s = self._settings
            form = {
                "client_id": s.client_id,
                "client_secret": s.client_secret.get_secret_value(),
                "refresh_token": s.refresh_token.get_secret_value(),
                "grant_type": "refresh_token",
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            token = self._http("POST", TOKEN_URL, headers, urlencode(form).encode())
            _check_scope(token)
            self._access_token = token.get("access_token")
            if not self._access_token:
                raise GmailError("Google did not return an access token. Sign in again.")
        return self._access_token

    def _get(self, path: str, **params: str) -> dict:
        url = f"{API_URL}/{path}"
        if params:
            url += "?" + urlencode(params)
        return self._http("GET", url, {"Authorization": f"Bearer {self._token()}"}, None)

    def label_id(self, name: str) -> str:
        for label in self._get("labels").get("labels", []):
            if label.get("name", "").casefold() == name.casefold():
                label_id = label.get("id")
                if not label_id:
                    raise GmailError("Gmail returned the configured label without an ID.")
                return label_id
        raise GmailError(f"There is no Gmail label called {name!r}. Check GMAIL_LABEL.")

    def message_ids(self, label_id: str) -> Iterator[str]:
        params = {"labelIds": label_id, "maxResults": "500"}
        while True:
            page = self._get("messages", **params)
            for message in page.get("messages", []):
                message_id = message.get("id")
                if not message_id:
                    raise GmailError("Gmail returned a message without an ID.")
                yield message_id
            if not page.get("nextPageToken"):
                return
            params["pageToken"] = page["nextPageToken"]

    def raw_message(self, message_id: str) -> bytes:
        raw = self._get(f"messages/{message_id}", format="raw").get("raw")
        if not raw:
            raise GmailError(f"Gmail did not return the contents of message {message_id}.")
        try:
            return base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            raise GmailError(f"Gmail returned damaged contents for message {message_id}.") from None


class AsbGmailImporter:
    """The ongoing ASB feed: every contract-note email under the configured label.

    Emails already imported are found again each time and skipped as duplicates, by their
    contract note number. Other emails under the label are skipped with a notice.
    """

    name = "asb-gmail"
    trade_source = TRADE_SOURCE
    summary = "ASB Securities contract-note emails from the configured Gmail label"

    def __init__(self, client: GmailClient, label: str) -> None:
        self.client = client
        self.label = label

    def fetch(self) -> ParseResult:
        result = ParseResult()
        label_id = self.client.label_id(self.label)
        for message_id in self.client.message_ids(label_id):
            where = f"message {message_id}"
            try:
                raw = self.client.raw_message(message_id)
                result.trades.append(parse_contract_note(raw, where))
            except NotAContractNote:
                result.skipped.append(RowError(where, "not an ASB contract note"))
            except (FileProblem, RowProblem) as exc:
                result.skipped.append(RowError(where, str(exc)))
            except Exception:
                result.skipped.append(
                    RowError(
                        where, "could not read this message; check it and correct it explicitly"
                    )
                )
        return result


# One-off sign-in --------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def auth_url(client_id: str, redirect_uri: str, state: str, code_verifier: str) -> str:
    challenge = _b64url(hashlib.sha256(code_verifier.encode()).digest())
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _wait_for_code(server: HTTPServer, state: str) -> str:
    """The authorisation code from Google's redirect to our loopback address."""
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the name http.server looks for
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state") == [state]:
                received.update({k: v[0] for k, v in query.items()})
                text = "Done. You can close this tab and go back to the terminal."
            else:
                text = "This page was not expected. Go back to the terminal."
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # the request line contains the authorisation code

    server.RequestHandlerClass = Handler
    while not received:
        server.handle_request()
        if not received and server.timed_out:
            raise GmailError("Gave up waiting for Google's sign-in page. Try again.")
    if "code" not in received:
        raise GmailError(f"Google sign-in did not finish ({received.get('error', 'no code')}).")
    return received["code"]


class _Server(HTTPServer):
    timed_out = False

    def handle_timeout(self) -> None:
        self.timed_out = True


def save_refresh_token(path: Path, token: str, *, force: bool = False) -> None:
    """Create a private token file, refusing symlinks even when replacement is requested."""
    try:
        if force:
            if path.is_symlink():
                raise GmailError("Refusing to replace a symlinked token file.")
            path.unlink(missing_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"GMAIL_REFRESH_TOKEN={token}\n")
    except FileExistsError:
        raise GmailError(
            "The token output file already exists. Choose another path or use --force."
        ) from None
    except OSError:
        raise GmailError("Cannot save the token file. Check its path and permissions.") from None


def authorise(
    settings: GmailSettings,
    output: Path,
    show: Callable[[str], None] = print,
    http: Http = urllib_http,
    port: int = 0,
    timeout: float = 300,
    *,
    force: bool = False,
) -> None:
    """Signs in to Google once and saves a read-only refresh token to `output`."""
    settings.require("client_id", "client_secret")
    server = _Server(("127.0.0.1", port), BaseHTTPRequestHandler)
    server.timeout = timeout
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        show(
            "Open this link in a web browser, sign in to the Gmail account that receives "
            "your ASB contract notes, and allow read-only access:\n\n"
            + auth_url(settings.client_id, redirect_uri, state, verifier)
            + "\n"
        )
        code = _wait_for_code(server, state)
    finally:
        server.server_close()
    form = {
        "code": code,
        "client_id": settings.client_id,
        "client_secret": settings.client_secret.get_secret_value(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token = http("POST", TOKEN_URL, headers, urlencode(form).encode())
    _check_scope(token)
    if not token.get("refresh_token"):
        raise GmailError("Google did not return a refresh token. Try again.")
    save_refresh_token(output, token["refresh_token"], force=force)
