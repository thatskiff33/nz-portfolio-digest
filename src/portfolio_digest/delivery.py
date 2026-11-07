"""Recipient progress for one scheduled occurrence, independent of database rollback."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from hashlib import sha256


@dataclass
class RecipientProgress:
    delivered: set[str]
    save: Callable[[], None]
    scope: str = "report"
    message_id: str = ""

    def key(self, recipient: str) -> str:
        # State needs identities, never mailbox addresses or message contents.
        return sha256(f"{self.scope}\0{self.message_id}\0{recipient}".encode()).hexdigest()

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
        RecipientProgress(progress.delivered, progress.save, f"portfolio:{portfolio_id}")
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
