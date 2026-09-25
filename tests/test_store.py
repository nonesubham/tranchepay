"""The store protocol and the shipped in-process implementation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tests.conftest import SessionFactory
from tests.fakes import FakeRazorpayClient
from tranchepay import (
    InMemorySessionStore,
    PaymentComposer,
    SessionStore,
    SplitConfig,
    SplitSession,
    TrancheStatus,
)


class TestInMemorySessionStore:
    def test_save_and_get(self, make_session: SessionFactory) -> None:
        store = InMemorySessionStore()
        session = make_session()

        store.save(session)

        assert store.get(session.session_id) == session
        assert len(store) == 1

    def test_unknown_id_returns_none(self) -> None:
        assert InMemorySessionStore().get("nope") is None

    def test_get_returns_a_copy(self, make_session: SessionFactory) -> None:
        store = InMemorySessionStore()
        session = make_session()
        store.save(session)

        loaded = store.get(session.session_id)
        assert loaded is not None
        loaded.status = "complete"  # type: ignore[assignment]

        stored = store.get(session.session_id)
        assert stored is not None
        assert stored.status != "complete"

    def test_save_copies_on_the_way_in(self, make_session: SessionFactory) -> None:
        store = InMemorySessionStore()
        session = make_session()
        store.save(session)

        session.status = "aborted"  # type: ignore[assignment]

        stored = store.get(session.session_id)
        assert stored is not None
        assert stored.status != "aborted"

    def test_update_persists_changes(self, make_session: SessionFactory) -> None:
        store = InMemorySessionStore()
        session = make_session()
        store.save(session)

        loaded = store.get(session.session_id)
        assert loaded is not None
        loaded.tranches[0].status = TrancheStatus.PAID
        store.update(loaded)

        stored = store.get(session.session_id)
        assert stored is not None
        assert stored.tranches[0].status.value == "paid"

    def test_update_upserts_an_unknown_session(self, make_session: SessionFactory) -> None:
        store = InMemorySessionStore()
        session = make_session(session_id="brand-new")

        store.update(session)

        assert store.get("brand-new") == session

    def test_sessions_are_independent(self, make_session: SessionFactory) -> None:
        store = InMemorySessionStore()
        first = make_session(session_id="one")
        second = make_session(session_id="two", amounts=(500,))

        store.save(first)
        store.save(second)

        assert len(store) == 2
        assert store.get("one") == first
        assert store.get("two") == second

    def test_assignment_is_validated_and_coerced(self, make_session: SessionFactory) -> None:
        session = make_session()

        session.tranches[0].status = "paid"  # type: ignore[assignment]
        assert session.tranches[0].status is TrancheStatus.PAID

        with pytest.raises(ValidationError):
            session.tranches[0].status = "not-a-status"  # type: ignore[assignment]

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(InMemorySessionStore(), SessionStore)

    def test_round_trips_through_json(self, make_session: SessionFactory) -> None:
        """Adapters can persist the model with json and reload it verbatim."""
        session = make_session()
        restored = SplitSession.model_validate_json(session.model_dump_json())

        assert restored == session


class DictBackedStore:
    """A drop-in adapter written without touching tranchepay's internals."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def save(self, session: SplitSession) -> None:
        self.rows[session.session_id] = session.model_dump_json()

    def get(self, session_id: str) -> SplitSession | None:
        payload = self.rows.get(session_id)
        return SplitSession.model_validate_json(payload) if payload else None

    def update(self, session: SplitSession) -> None:
        self.save(session)


class TestCustomAdapters:
    def test_a_json_dict_adapter_is_a_drop_in(self) -> None:
        store = DictBackedStore()

        assert isinstance(store, SessionStore)

        composer = PaymentComposer(
            FakeRazorpayClient(), split=SplitConfig(tranche_paise=1_000), store=store
        )
        first = composer.create_order(2_500, mode="split")  # type: ignore[arg-type]
        assert first.session_id is not None

        assert store.get(first.session_id) is not None

    def test_the_composer_exposes_the_store_it_uses(self) -> None:
        store = DictBackedStore()
        composer = PaymentComposer(FakeRazorpayClient(), store=store)

        assert composer.store is store

    @pytest.mark.parametrize("bad_store", [object(), "store", 3])
    def test_objects_that_are_not_stores_are_rejected(self, bad_store: Any) -> None:
        with pytest.raises(TypeError, match="store must implement"):
            PaymentComposer(FakeRazorpayClient(), store=bad_store)

    def test_none_means_use_the_default_store(self) -> None:
        composer = PaymentComposer(FakeRazorpayClient(), store=None)

        assert isinstance(composer.store, InMemorySessionStore)
