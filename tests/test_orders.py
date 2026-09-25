"""The order helpers shared by the composer and the split flow."""

from __future__ import annotations

import pytest

from tests.fakes import FakeRazorpayClient
from tranchepay import InMemorySessionStore, PaymentComposeError, PaymentMode, SplitSession, Tranche
from tranchepay.exceptions import SessionStateError
from tranchepay.orders import (
    create_razorpay_order,
    create_tranche_order,
    ensure_tranche_order,
    order_is_payable,
    order_result,
    tranche_order_result,
)


def session_with_order(order_id: str | None = "order_0001") -> SplitSession:
    return SplitSession(
        session_id="session-1",
        amount_paise=1_500,
        tranche_paise=1_000,
        tranches=[
            Tranche(index=0, amount_paise=1_000, order_id=order_id),
            Tranche(index=1, amount_paise=500),
        ],
    )


class TestCreateRazorpayOrder:
    def test_payload_defaults(self, fake_client: FakeRazorpayClient) -> None:
        create_razorpay_order(fake_client, 1_000, currency="INR")

        request = fake_client.created_orders[0]["request"]
        assert request == {"amount": 1_000, "currency": "INR", "payment_capture": 1}

    def test_receipt_and_notes_are_included_when_given(
        self, fake_client: FakeRazorpayClient
    ) -> None:
        create_razorpay_order(fake_client, 1_000, currency="INR", receipt="rcpt", notes={"a": 1})

        request = fake_client.created_orders[0]["request"]
        assert request["receipt"] == "rcpt"
        assert request["notes"] == {"a": 1}

    def test_response_is_returned_untouched(self, fake_client: FakeRazorpayClient) -> None:
        raw = create_razorpay_order(fake_client, 1_000, currency="INR")

        assert raw["id"] == "order_0001"
        assert raw["status"] == "created"


class TestOrderResult:
    def test_wraps_the_response(self, fake_client: FakeRazorpayClient) -> None:
        raw = create_razorpay_order(fake_client, 1_000, currency="INR", receipt="rcpt")

        result = order_result(
            raw,
            mode=PaymentMode.EXACT,
            amount_paise=1_000,
            currency_default="USD",
            receipt="fallback",
        )

        assert result.order_id == "order_0001"
        assert result.currency == "INR", "the response currency wins over the fallback"
        assert result.receipt == "rcpt"
        assert result.raw == raw

    def test_falls_back_to_the_given_currency_and_receipt(self) -> None:
        result = order_result(
            {"id": "order_1"},
            mode=PaymentMode.EXACT,
            amount_paise=100,
            currency_default="AED",
            receipt="rcpt",
        )

        assert result.currency == "AED"
        assert result.receipt == "rcpt"

    @pytest.mark.parametrize("raw", [{}, {"id": ""}, {"id": 42}])
    def test_a_response_without_an_id_is_rejected(self, raw: dict[str, object]) -> None:
        with pytest.raises(PaymentComposeError, match="no usable 'id'"):
            order_result(raw, mode=PaymentMode.EXACT, amount_paise=100, currency_default="INR")


class TestTrancheOrders:
    def test_existing_order_is_wrapped_without_an_api_call(
        self, fake_client: FakeRazorpayClient
    ) -> None:
        result = tranche_order_result(session_with_order(), 0)

        assert result.order_id == "order_0001"
        assert result.amount_paise == 1_000
        assert result.session_id == "session-1"
        assert result.tranche_index == 0
        assert fake_client.created_orders == []

    def test_a_tranche_without_an_order_is_rejected(self) -> None:
        with pytest.raises(PaymentComposeError, match="has no order yet"):
            tranche_order_result(session_with_order(order_id=None), 0)

    def test_ensure_creates_the_order_when_missing(self, fake_client: FakeRazorpayClient) -> None:
        store = InMemorySessionStore()
        session = session_with_order(order_id=None)
        store.save(session)

        result = ensure_tranche_order(fake_client, store, session, 0)

        assert result.order_id == "order_0001"
        stored = store.get(session.session_id)
        assert stored is not None
        assert stored.tranches[0].order_id == "order_0001"

    def test_ensure_reuses_the_recorded_order(self, fake_client: FakeRazorpayClient) -> None:
        store = InMemorySessionStore()
        session = session_with_order()
        store.save(session)
        fake_client.order.create({"amount": 1_000, "currency": "INR"})
        existing = fake_client.created_orders[0]["id"]

        result = ensure_tranche_order(fake_client, store, session, 1)

        assert result.amount_paise == 500
        assert len(fake_client.created_orders) == 2, "the missing order was created"
        assert existing == "order_0001"

    def test_create_tranche_order_records_the_new_order(
        self, fake_client: FakeRazorpayClient
    ) -> None:
        store = InMemorySessionStore()
        session = session_with_order()
        store.save(session)

        result = create_tranche_order(fake_client, store, session, 1)

        assert result.amount_paise == 500
        assert result.tranche_index == 1
        stored = store.get(session.session_id)
        assert stored is not None
        assert stored.tranches[1].order_id == result.order_id


class TestOrderIsPayable:
    @pytest.mark.parametrize("status", ["created", "attempted"])
    def test_payable_states(self, fake_client: FakeRazorpayClient, status: str) -> None:
        fake_client.order.create({"amount": 100, "currency": "INR"})
        fake_client.set_order_status("order_0001", status)

        assert order_is_payable(fake_client, "order_0001") is True

    @pytest.mark.parametrize("status", ["expired", "cancelled", "failed"])
    def test_unpayable_states(self, fake_client: FakeRazorpayClient, status: str) -> None:
        fake_client.order.create({"amount": 100, "currency": "INR"})
        fake_client.set_order_status("order_0001", status)

        assert order_is_payable(fake_client, "order_0001") is False

    def test_a_paid_order_is_an_error_not_a_replacement(
        self, fake_client: FakeRazorpayClient
    ) -> None:
        fake_client.order.create({"amount": 100, "currency": "INR"})
        fake_client.set_order_status("order_0001", "paid")

        with pytest.raises(SessionStateError, match="is already paid"):
            order_is_payable(fake_client, "order_0001")

    def test_an_unknown_order_is_reported(self, fake_client: FakeRazorpayClient) -> None:
        """An order the API cannot return is reported, never silently replaced."""
        with pytest.raises(SessionStateError, match="could not fetch order"):
            order_is_payable(fake_client, "order_missing")
