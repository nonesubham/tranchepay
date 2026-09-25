"""Process-global, per-session locks that serialise split transitions.

The split state machine reads a session, decides, and writes it back. Two
callbacks for the same session must not interleave that sequence, or a tranche
could be advanced twice. :func:`hold` provides the mutual exclusion inside one
process; a store that supports atomic compare-and-swap is what protects several
processes (see :mod:`tranchepay.store`).

The registry holds locks, never session data, and entries are dropped once no
thread holds them, so a long-running process does not accumulate state.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from weakref import WeakValueDictionary

__all__ = ["hold"]

# A module-level registry (rather than one per composer) so that two composers
# sharing a store in the same process still serialise against each other.
_guard = threading.Lock()
_locks: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()


@contextmanager
def hold(session_id: str) -> Iterator[None]:
    """Serialise transitions for ``session_id`` within this process.

    Args:
        session_id: Session whose transitions must not interleave.

    Yields:
        ``None``; the caller performs its read-modify-write inside the block.
    """
    with _guard:
        lock = _locks.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _locks[session_id] = lock
    with lock:
        yield
