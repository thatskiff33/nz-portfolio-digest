"""Bounded, expiring server-side sessions and password verification.

Run one worker. Restarting the web service revokes all sessions. Login throttling is
per client address, with a process-wide ceiling against distributed attempts. Behind
Tailscale Serve or Docker's published port, that address is shared, so the per-address
limit applies to everyone arriving that way. Forwarded headers are not consulted.
"""

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

COOKIE = "portfolio_session"
HOST_COOKIE = "__Host-portfolio_session"
ANONYMOUS_LIFETIME = 10 * 60
IDLE_TIMEOUT = 30 * 60
ABSOLUTE_LIFETIME = 8 * 60 * 60
ANONYMOUS_CAP = 1024
AUTHENTICATED_CAP = 1024
LOGIN_WINDOW = 300
CLIENT_FAILURE_LIMIT = 5
GLOBAL_FAILURE_LIMIT = 50


def cookie_name(secure: bool) -> str:
    """Secure cookies use the __Host- prefix: Path=/, no Domain, Secure."""
    return HOST_COOKIE if secure else COOKIE


@dataclass
class BrowserSession:
    csrf: str
    created: int
    activity: int
    authenticated: bool = False

    def deadline(self) -> int:
        if not self.authenticated:
            return self.created + ANONYMOUS_LIFETIME
        return min(self.activity + IDLE_TIMEOUT, self.created + ABSOLUTE_LIFETIME)

    def remaining(self, now: int) -> int:
        return max(0, self.deadline() - now)


class Security:
    def __init__(self, password: str, *, secure_cookie: bool = False):
        if len(password) < 16:
            raise ValueError("DASHBOARD_PASSWORD must contain at least 16 characters.")
        self.salt = secrets.token_bytes(16)
        self.expected = self.digest(password)
        self.cookie_name = cookie_name(secure_cookie)
        self.sessions: dict[str, BrowserSession] = {}
        self.failures: list[tuple[int, str]] = []
        self.lock = threading.RLock()

    def digest(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode(), salt=self.salt, n=16384, r=8, p=1)

    def _now(self) -> int:
        return int(time.monotonic())

    def _prune(self, now: int) -> None:
        self.sessions = {
            key: session for key, session in self.sessions.items() if session.deadline() > now
        }

    def _evict_oldest_anonymous(self) -> None:
        anonymous = [key for key, session in self.sessions.items() if not session.authenticated]
        if len(anonymous) >= ANONYMOUS_CAP:
            # Insertion order is creation order. Never drop a signed-in session.
            del self.sessions[anonymous[0]]

    def cookie_max_age(self, session: BrowserSession) -> int:
        return session.remaining(self._now())

    def browser(self, request: Request, *, create: bool = False) -> tuple[str, BrowserSession]:
        now = self._now()
        with self.lock:
            self._prune(now)
            ident = request.cookies.get(self.cookie_name, "")
            session = self.sessions.get(ident)
            if session is None:
                if not create:
                    return "", BrowserSession("", 0, 0, False)
                self._evict_oldest_anonymous()
                ident = secrets.token_urlsafe(32)
                session = BrowserSession(secrets.token_urlsafe(32), now, now, False)
                self.sessions[ident] = session
                return ident, session
            if session.authenticated:
                # Expiry was applied above, before this refresh, so the absolute
                # deadline (created + eight hours) does not move.
                session.activity = now
            return ident, session

    def csrf(self, request: Request, token: str) -> None:
        if not request.state.session_id or not secrets.compare_digest(
            request.state.browser.csrf.encode(), token.encode()
        ):
            raise HTTPException(403, "The form has expired. Reload and try again.")

    def require_login(self, request: Request) -> None:
        if not request.state.browser.authenticated:
            raise HTTPException(401, "Sign in to continue.")

    def login(self, request: Request, password: str) -> None:
        now = self._now()
        with self.lock:
            self._prune(now)
            address = request.client.host if request.client else "unknown"
            self.failures = [
                (stamp, host) for stamp, host in self.failures if stamp > now - LOGIN_WINDOW
            ]
            if (
                sum(host == address for _, host in self.failures) >= CLIENT_FAILURE_LIMIT
                or len(self.failures) >= GLOBAL_FAILURE_LIMIT
            ):
                raise HTTPException(429, "Too many failed sign-ins. Wait five minutes.")
            if len(password) > 1024 or not secrets.compare_digest(
                self.digest(password), self.expected
            ):
                self.failures.append((now, address))
                raise HTTPException(401, "Password not recognised.")
            current = self.sessions.get(request.state.session_id)
            signed_in = sum(session.authenticated for session in self.sessions.values())
            if signed_in >= AUTHENTICATED_CAP and not (current and current.authenticated):
                raise HTTPException(503, "Too many signed-in sessions. Try again later.")
            # Rotate the opaque ID and CSRF token, preventing session fixation.
            self.sessions.pop(request.state.session_id, None)
            ident = secrets.token_urlsafe(32)
            browser = BrowserSession(secrets.token_urlsafe(32), now, now, True)
            self.sessions[ident] = browser
            request.state.session_id, request.state.browser = ident, browser
