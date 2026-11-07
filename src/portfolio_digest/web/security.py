"""Bounded, expiring server-side sessions and password verification.

Run one worker. Restarting the web service revokes all sessions. Login throttling is
per client address, with a process-wide ceiling against distributed attempts.
"""

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

COOKIE = "portfolio_session"
LIFETIME = 8 * 60 * 60
ANONYMOUS_LIFETIME = 10 * 60
IDLE_TIMEOUT = 30 * 60
ANONYMOUS_CAPACITY = 1024
AUTHENTICATED_CAPACITY = 1024
LOGIN_WINDOW = 300
CLIENT_FAILURE_LIMIT = 5
GLOBAL_FAILURE_LIMIT = 50


@dataclass
class BrowserSession:
    csrf: str
    expires: int
    authenticated: bool = False
    last_seen: int = 0
    message: str = ""


class Security:
    def __init__(self, password: str, *, secure_cookie: bool = False):
        if len(password) < 16:
            raise ValueError("DASHBOARD_PASSWORD must contain at least 16 characters.")
        self.cookie = "__Host-" + COOKIE if secure_cookie else COOKIE
        self.salt = secrets.token_bytes(16)
        self.expected = self.digest(password)
        self.sessions: dict[str, BrowserSession] = {}
        self.failures: list[tuple[int, str]] = []
        self.lock = threading.RLock()

    def flash(self, request: Request, message: str) -> None:
        with self.lock:
            request.state.browser.message = message[:240]

    def take_message(self, request: Request) -> str:
        with self.lock:
            message = request.state.browser.message
            request.state.browser.message = ""
            return message

    def digest(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode(), salt=self.salt, n=16384, r=8, p=1)

    def browser(self, request: Request, *, create: bool = False) -> tuple[str, BrowserSession]:
        now = int(time.monotonic())
        with self.lock:
            self.sessions = {
                k: v
                for k, v in self.sessions.items()
                if v.expires > now and (not v.authenticated or v.last_seen + IDLE_TIMEOUT > now)
            }
            ident = request.cookies.get(self.cookie, "")
            if ident not in self.sessions:
                if not create:
                    return "", BrowserSession("", 0)
                anonymous = [k for k, v in self.sessions.items() if not v.authenticated]
                if len(anonymous) >= ANONYMOUS_CAPACITY:
                    self.sessions.pop(anonymous[0])
                ident = secrets.token_urlsafe(32)
                self.sessions[ident] = BrowserSession(
                    secrets.token_urlsafe(32), now + ANONYMOUS_LIFETIME
                )
            self.sessions[ident].last_seen = now
            return ident, self.sessions[ident]

    def csrf(self, request: Request, token: str) -> None:
        if not request.state.session_id or not secrets.compare_digest(
            request.state.browser.csrf.encode(), token.encode()
        ):
            raise HTTPException(403, "The form has expired. Reload and try again.")

    def require_login(self, request: Request) -> None:
        if not request.state.browser.authenticated:
            raise HTTPException(401, "Sign in to view your portfolio.")

    def login(self, request: Request, password: str) -> None:
        now = int(time.monotonic())
        with self.lock:
            address = request.client.host if request.client else "unknown"
            self.failures = [(n, host) for n, host in self.failures if n > now - LOGIN_WINDOW]
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
            if sum(v.authenticated for v in self.sessions.values()) >= AUTHENTICATED_CAPACITY:
                raise HTTPException(503, "Too many signed-in sessions. Try again later.")
            # Rotate the opaque ID and CSRF token, preventing session fixation.
            self.sessions.pop(request.state.session_id, None)
            ident = secrets.token_urlsafe(32)
            browser = BrowserSession(secrets.token_urlsafe(32), now + LIFETIME, True, now)
            self.sessions[ident] = browser
            request.state.session_id, request.state.browser = ident, browser
