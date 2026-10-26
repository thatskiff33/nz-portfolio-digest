"""Bounded, expiring server-side sessions and password verification.

Run one worker. Restarting the web service revokes all sessions. Login throttling is
process-wide so changing client addresses or cookies cannot bypass it.
"""

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

COOKIE = "portfolio_session"
LIFETIME = 8 * 60 * 60


@dataclass
class BrowserSession:
    csrf: str
    expires: int
    authenticated: bool = False


class Security:
    def __init__(self, password: str):
        if len(password) < 16:
            raise ValueError("DASHBOARD_PASSWORD must contain at least 16 characters.")
        self.salt = secrets.token_bytes(16)
        self.expected = self.digest(password)
        self.sessions: dict[str, BrowserSession] = {}
        self.failures: list[int] = []
        self.lock = threading.RLock()

    def digest(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode(), salt=self.salt, n=16384, r=8, p=1)

    def browser(self, request: Request) -> tuple[str, BrowserSession]:
        now = int(time.monotonic())
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
            ident = request.cookies.get(COOKIE, "")
            if ident not in self.sessions:
                if len(self.sessions) >= 1024:
                    raise HTTPException(503, "Too many browser sessions. Try again later.")
                ident = secrets.token_urlsafe(32)
                self.sessions[ident] = BrowserSession(secrets.token_urlsafe(32), now + LIFETIME)
            return ident, self.sessions[ident]

    def csrf(self, request: Request, token: str) -> None:
        if not secrets.compare_digest(request.state.browser.csrf.encode(), token.encode()):
            raise HTTPException(403, "The form has expired. Reload and try again.")

    def require_login(self, request: Request) -> None:
        if not request.state.browser.authenticated:
            raise HTTPException(401, "Sign in to make changes.")

    def login(self, request: Request, password: str) -> None:
        now = int(time.monotonic())
        with self.lock:
            self.failures = [n for n in self.failures if n > now - 300]
            if len(self.failures) >= 5:
                raise HTTPException(429, "Too many failed sign-ins. Wait five minutes.")
            if len(password) > 1024 or not secrets.compare_digest(
                self.digest(password), self.expected
            ):
                self.failures.append(now)
                raise HTTPException(401, "Password not recognised.")
            # Rotate the opaque ID and CSRF token, preventing session fixation.
            self.sessions.pop(request.state.session_id, None)
            ident = secrets.token_urlsafe(32)
            browser = BrowserSession(secrets.token_urlsafe(32), now + LIFETIME, True)
            self.sessions[ident] = browser
            request.state.session_id, request.state.browser = ident, browser
