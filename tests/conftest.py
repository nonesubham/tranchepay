"""Shared pytest fixtures for the tranchepay test suite.

Nothing here touches the network: every test runs against in-process doubles.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from tranchepay import SplitSession, Tranche, TrancheStatus

SessionFactory = Callable[..., SplitSession]


@pytest.fixture
def make_session() -> SessionFactory:
    """Return a factory that builds a pending split session from tranche amounts."""

    def factory(
        amounts: Sequence[int] = (1_000, 1_000, 500),
        *,
        status: str = "pending",
        session_id: str = "session-1",
    ) -> SplitSession:
        tranches = [Tranche(index=i, amount_paise=amount) for i, amount in enumerate(amounts)]
        return SplitSession(
            session_id=session_id,
            amount_paise=sum(amounts),
            tranche_paise=max(amounts),
            tranches=tranches,
            status=status,
        )

    return factory


__all__ = ["SessionFactory", "Tranche", "TrancheStatus"]
