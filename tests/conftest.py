"""Shared pytest fixtures for the tranchepay test suite.

Nothing here touches the network: every test runs against in-process doubles.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal

import pytest

from tests.fakes import FakeRazorpayClient
from tranchepay import (
    ChargesConfig,
    InMemorySessionStore,
    PaymentComposer,
    SplitConfig,
    SplitSession,
    Tranche,
    TrancheStatus,
)

SessionFactory = Callable[..., SplitSession]

TEST_SECRET = "test_secret"


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


__all__ = ["TEST_SECRET", "SessionFactory", "Tranche", "TrancheStatus"]


@pytest.fixture
def fake_client() -> FakeRazorpayClient:
    """A fresh in-process Razorpay stand-in with no network access."""
    return FakeRazorpayClient(secret=TEST_SECRET)


@pytest.fixture
def composer(fake_client: FakeRazorpayClient) -> PaymentComposer:
    """A composer wired to the fake client and the default configs."""
    return PaymentComposer(fake_client, charges=ChargesConfig(fee_rate=Decimal("0.0236")))


@pytest.fixture
def split_store() -> InMemorySessionStore:
    """An in-process store that tests can inspect directly."""
    return InMemorySessionStore()


@pytest.fixture
def split_composer(
    fake_client: FakeRazorpayClient, split_store: InMemorySessionStore
) -> PaymentComposer:
    """A composer with a small tranche ceiling so tests stay tiny."""
    return PaymentComposer(
        fake_client,
        split=SplitConfig(tranche_paise=1_000),
        store=split_store,
        currency="INR",
    )
