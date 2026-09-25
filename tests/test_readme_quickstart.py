"""End-to-end run of the README quickstart, so the docs cannot silently rot.

The snippet here mirrors ``README.md``: the same construction, the same three
modes, and the same callback handling - with the fake client in place of the
network.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any

import pytest

from tests.fakes import FakeRazorpayClient
from tranchepay import (
    ChargesConfig,
    PaymentComposer,
    PaymentMode,
    SessionStatus,
    SplitConfig,
    verify_webhook,
)
from tranchepay.protocol import RazorpayClientProtocol


def test_readme_quickstart_end_to_end(fake_client: FakeRazorpayClient) -> None:
    composer = PaymentComposer(
        fake_client,
        charges=ChargesConfig(fee_rate=Decimal("0.0236")),  # 2.36% - your rate
        split=SplitConfig(),  # tranche ceiling, default 199_900 paise
    )

    exact = composer.create_order(50_000, mode=PaymentMode.EXACT)
    assert exact.amount_paise == 50_000
    assert exact.raw["id"] == exact.order_id

    charged = composer.create_order(50_000, mode=PaymentMode.WITH_CHARGES)
    assert (charged.amount_paise, charged.net_paise, charged.fee_paise) == (51_209, 50_000, 1_209)

    first = composer.create_order(450_000, mode=PaymentMode.SPLIT)
    session_id = first.session_id
    assert session_id is not None
    assert (first.tranche_index, first.amount_paise) == (0, 199_900)

    payload: dict[str, Any] = {"razorpay_payment_id": "pay_readme"}
    fake_client.add_payment("pay_readme", first.amount_paise, order_id=first.order_id)

    next_order = composer.verify_and_advance(
        session_id=session_id,
        order_id=first.order_id,
        payment_id=payload["razorpay_payment_id"],
        signature=fake_client.sign(first.order_id, "pay_readme"),
    )
    assert next_order is not None
    assert next_order.amount_paise == 199_900

    session = composer.store.get(session_id)
    assert session is not None
    assert session.status is SessionStatus.IN_PROGRESS
    assert session.total_paid_paise() == 199_900
    assert session.model_dump_json()

    assert composer.resume(session_id).order_id == next_order.order_id

    aborted = composer.abort_and_refund(session_id)
    assert aborted.status is SessionStatus.ABORTED
    assert [refund["amount"] for refund in fake_client.refunds] == [199_900]

    body, secret = '{"event":"payment.captured"}', "whsec_test"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    assert verify_webhook(fake_client, body, signature, secret) is True


def test_manual_capture_satisfies_the_captured_check(fake_client: FakeRazorpayClient) -> None:
    """Merchants on manual capture can capture first; verification then passes."""
    composer = PaymentComposer(fake_client, split=SplitConfig(tranche_paise=1_000))
    first = composer.create_order(2_000, mode=PaymentMode.SPLIT)
    assert first.session_id is not None

    fake_client.add_payment("pay_1", 1_000, status="authorized", order_id=first.order_id)
    fake_client.payment.capture("pay_1", 1_000)

    nxt = composer.verify_and_advance(
        first.session_id, first.order_id, "pay_1", fake_client.sign(first.order_id, "pay_1")
    )

    assert nxt is not None
    assert fake_client.captures == [{"payment_id": "pay_1", "amount": 1_000}]


def test_protocol_declares_the_resources_tranchepay_composes(
    fake_client: FakeRazorpayClient,
) -> None:
    assert isinstance(fake_client, RazorpayClientProtocol)
    for resource in ("order", "payment", "utility"):
        assert hasattr(fake_client, resource)
    with pytest.raises(TypeError):
        PaymentComposer(object())  # type: ignore[arg-type]
