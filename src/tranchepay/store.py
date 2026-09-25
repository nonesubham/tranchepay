"""Split-session persistence: one small protocol plus an in-process store.

The protocol is deliberately three methods wide. A Redis or SQLAlchemy adapter
is a drop-in replacement, and because :class:`~tranchepay.SplitSession` is a
pydantic model, persisting it is ``session.model_dump_json()`` and reading it
back is ``SplitSession.model_validate_json(...)``.

Adapter contract:

* ``save`` upserts a session; ``get`` returns ``None`` for an unknown id and
  never raises; ``update`` persists the whole session.
* ``get`` should return a copy (or a freshly loaded object), so that mutating the
  result has no effect until it is written back with ``update``. The shipped
  :class:`InMemorySessionStore` does exactly that.
* Make ``update`` atomic if several processes share the store (``WATCH``/``MULTI``
  or a Lua script in Redis, ``SELECT ... FOR UPDATE`` in SQL). tranchepay's
  in-process locking cannot protect cross-process races.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from .exceptions import SessionNotFoundError
from .models import SplitSession

__all__ = ["InMemorySessionStore", "SessionStore", "load_session"]


@runtime_checkable
class SessionStore(Protocol):
    """Where split sessions live between API calls."""

    def save(self, session: SplitSession) -> None:
        """Persist ``session``, replacing any existing record with its id."""
        ...  # pragma: no cover - protocol declaration

    def get(self, session_id: str) -> SplitSession | None:
        """Return the stored session, or ``None`` if the id is unknown."""
        ...  # pragma: no cover - protocol declaration

    def update(self, session: SplitSession) -> None:
        """Persist changes to an existing session."""
        ...  # pragma: no cover - protocol declaration


class InMemorySessionStore:
    """A thread-safe store in this process's memory.

    Suitable for tests, CLIs, and single-process apps. Sessions are lost when
    the process restarts, and are not shared between workers, so production
    deployments with more than one worker should supply a durable adapter.

    Sessions are copied on the way in and on the way out, so the store behaves
    like a remote store: a mutation only takes effect once ``update`` is called.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SplitSession] = {}
        self._lock = threading.RLock()

    def save(self, session: SplitSession) -> None:
        """Persist ``session``, replacing any existing record with its id."""
        with self._lock:
            self._sessions[session.session_id] = session.model_copy(deep=True)

    def get(self, session_id: str) -> SplitSession | None:
        """Return a copy of the stored session, or ``None`` if unknown."""
        with self._lock:
            session = self._sessions.get(session_id)
        return session.model_copy(deep=True) if session is not None else None

    def update(self, session: SplitSession) -> None:
        """Persist ``session``; an unknown id is inserted rather than rejected."""
        with self._lock:
            self._sessions[session.session_id] = session.model_copy(deep=True)

    def __len__(self) -> int:
        """Number of stored sessions."""
        with self._lock:
            return len(self._sessions)


def load_session(store: SessionStore, session_id: str) -> SplitSession:
    """Return the stored session, or raise :class:`SessionNotFoundError`.

    Args:
        store: Store to read from.
        session_id: Session to load.

    Returns:
        The stored session.

    Raises:
        SessionNotFoundError: If the id is unknown to the store.
    """
    session = store.get(session_id)
    if session is None:
        msg = f"unknown split session: {session_id}"
        raise SessionNotFoundError(msg)
    return session
