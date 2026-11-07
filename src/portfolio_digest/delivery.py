"""Recipient progress for one scheduled occurrence, independent of database rollback."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from hashlib import sha256
from hmac import digest
from secrets import token_bytes


def keyed_checkpoint(secret: bytes, checkpoint: str) -> str:
    """Key an existing digest so legacy progress can be migrated without addresses."""
    return digest(secret, checkpoint.encode(), "sha256").hex()


@dataclass
class RecipientProgress:
    delivered: set[str]
    save: Callable[[], None]
    scope: str = "report"
    message_id: str = ""
    secret: bytes = field(default_factory=lambda: token_bytes(32), repr=False)

    def key(self, recipient: str) -> str:
        checkpoint = sha256(f"{self.scope}\0{self.message_id}\0{recipient}".encode()).hexdigest()
        return keyed_checkpoint(self.secret, checkpoint)

    def complete(self, recipient: str) -> None:
        self.delivered.add(self.key(recipient))
        self.save()


_progress: ContextVar[RecipientProgress | None] = ContextVar("recipient_progress", default=None)


def recipient_progress() -> RecipientProgress | None:
    return _progress.get()


@contextmanager
def track_recipients(progress: RecipientProgress | None) -> Iterator[None]:
    token = _progress.set(progress)
    try:
        yield
    finally:
        _progress.reset(token)


@contextmanager
def portfolio_delivery(portfolio_id: int) -> Iterator[None]:
    progress = recipient_progress()
    scoped = (
        replace(progress, scope=f"portfolio:{portfolio_id}", message_id="")
        if progress is not None
        else None
    )
    with track_recipients(scoped):
        yield


@contextmanager
def message_delivery(message_id: str) -> Iterator[None]:
    """Bind recipient progress to a stable identity for this message."""
    progress = recipient_progress()
    scoped = replace(progress, message_id=message_id) if progress is not None else None
    with track_recipients(scoped):
        yield
