"""Live integration tests against the Razorpay TEST (sandbox) API.

These tests are **opt-in** and hit the real network. They are deselected by the
default ``pytest`` run (``-m "not integration"`` in ``addopts``) and are skipped
when credentials are missing, so contributors without a Razorpay account are
never blocked. Run them with ``./run_integration.sh`` or:

    set -a; . ./.env; set +a
    pytest tests/test_integration_razorpay.py -v -m integration

What is live and what is mocked
-------------------------------
Live, through the official SDK, which is the point of this suite:

* ``client.order.create`` / ``client.order.fetch`` - proves tranchepay's payloads
  are accepted by Razorpay and that what we report matches what was stored.
* ``client.utility.verify_webhook_signature`` - real HMAC verification.

Mocked, because Razorpay only creates a payment once a human completes checkout
in a browser, which a backend test cannot automate:

* ``client.payment.fetch`` - there is no real payment to fetch.
* ``client.utility.verify_payment_signature`` - where noted; one test instead
  delegates to the real SDK verifier to pin the parameter contract.

Nothing here moves money: ``rzp_test_`` keys only ever create sandbox objects.

API mapping
-----------
The scenarios in the task brief describe a slightly different surface than the
library ships, so they map onto the public API like this:

* ``create_order(amount_paise=..., mode=Mode.EXACT)`` ->
  ``composer.create_order(amount_paise, mode=PaymentMode.EXACT)``.
* ``create_order(..., mode=Mode.WITH_CHARGES, fee_rate=Decimal("0.02"))`` ->
  the fee rate is configuration, not a per-call argument, so the composer is
  built with ``ChargesConfig(fee_rate=Decimal("0.02"))``.
* ``start_split(amount_paise=..., config=SplitConfig(...))`` ->
  ``SplitConfig(tranche_paise=...)`` at construction plus
  ``composer.create_order(amount_paise, mode=PaymentMode.SPLIT)``, which opens the
  session and returns the first tranche's order.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tranchepay import (
    ChargesConfig,
    PaymentComposer,
    PaymentMode,
    SessionStatus,
    SplitConfig,
    TrancheStatus,
    VerificationError,
    gross_up,
    verify_webhook,
)

razorpay = pytest.importorskip("razorpay")

_ROOT = Path(__file__).resolve().parents[1]

try:
    import dotenv
except ImportError:  # pragma: no cover - optional: env vars may be exported instead
    dotenv = None  # type: ignore[assignment]

if dotenv is not None:
    dotenv.load_dotenv(_ROOT / ".env")

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# Small delay between live calls so a full run stays well inside Razorpay's rate
# limits. The sandbox is a shared service; be a good citizen.
PAUSE_SECONDS = 0.5

# Trailing fee imposed by the tranche ceiling shipped in SplitConfig (₹1,999).
TRANCHES = [199_900, 199_900, 100_200]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KEY_ID and KEY_SECRET),
        reason=(
            "live Razorpay sandbox credentials are not set; copy .env.example to .env "
            "and fill in TEST keys (see the README)"
        ),
    ),
]


def _pause() -> None:
    """Sleep briefly between live requests to avoid tripping rate limits."""
    time.sleep(PAUSE_SECONDS)


def _receipt() -> str:
    """A unique receipt so each sandbox order is traceable back to a test run."""
    return f"tranchepay-it-{uuid.uuid4().hex[:16]}"


def _payment_signature(order_id: str, payment_id: str) -> str:
    """The checkout signature Razorpay would compute with our key secret."""
    message = f"{order_id}|{payment_id}".encode()
    return hmac.new(KEY_SECRET.encode(), message, hashlib.sha256).hexdigest()


def _captured_payment(order_id: str, payment_id: str, amount_paise: int) -> dict[str, Any]:
    """A payment payload shaped exactly like ``client.payment.fetch`` would return."""
    return {
        "id": payment_id,
        "entity": "payment",
        "amount": amount_paise,
        "currency": "INR",
        "status": "captured",
        "order_id": order_id,
        "captured": True,
    }


@pytest.fixture(scope="session")
def rzp_client() -> Any:
    """A real ``razorpay.Client`` authenticated with the sandbox keys.

    Constructing the client performs no I/O; the first API call happens inside a
    test. The fixture refuses to run against anything but a ``rzp_test_`` key so
    this file can never be pointed at a live account by accident.
    """
    if not KEY_ID.startswith("rzp_test_"):
        pytest.fail(
            "refusing to run live integration tests with a non-test key id",
            pytrace=False,
        )
    return razorpay.Client(auth=(KEY_ID, KEY_SECRET))


@pytest.fixture
def composer(rzp_client: Any) -> PaymentComposer:
    """A composer whose fee rate is the 2% used by the WITH_CHARGES scenario."""
    return PaymentComposer(rzp_client, charges=ChargesConfig(fee_rate=Decimal("0.02")))


@pytest.fixture
def split_composer(rzp_client: Any) -> PaymentComposer:
    """A composer with the ₹1,999 tranche ceiling used by the split scenarios."""
    return PaymentComposer(rzp_client, split=SplitConfig(tranche_paise=199_900))


class TestScenarioAExact:
    """Mode 1: the merchant absorbs the charges."""

    def test_exact_order_is_created_and_fetched_live(
        self, rzp_client: Any, composer: PaymentComposer
    ) -> None:
        receipt = _receipt()

        result = composer.create_order(
            50_000, mode=PaymentMode.EXACT, receipt=receipt, notes={"suite": "tranchepay-it"}
        )

        assert result.order_id.startswith("order_")
        assert result.amount_paise == 50_000
        assert result.mode is PaymentMode.EXACT
        assert result.session_id is None

        _pause()
        fetched = rzp_client.order.fetch(result.order_id)

        assert fetched["id"] == result.order_id
        assert fetched["amount"] == 50_000, "the sandbox must hold exactly what we charged"
        assert fetched["currency"] == "INR"
        assert fetched["status"] == "created"
        assert fetched["receipt"] == receipt
        assert fetched["notes"] == {"suite": "tranchepay-it"}
        assert result.raw["amount"] == fetched["amount"]


class TestScenarioBWithCharges:
    """Mode 2: the customer covers the gateway fee."""

    def test_grossed_up_order_is_created_and_fetched_live(
        self, rzp_client: Any, composer: PaymentComposer
    ) -> None:
        fee_rate = Decimal("0.02")
        expected_gross = int(
            (Decimal(100_000) / (Decimal(1) - fee_rate)).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
        assert expected_gross == 102_041, "100000 / 0.98 = 102040.81..., rounded half-up"

        result = composer.create_order(100_000, mode=PaymentMode.WITH_CHARGES, receipt=_receipt())

        assert result.order_id.startswith("order_")
        assert result.amount_paise == expected_gross
        assert result.amount_paise == gross_up(100_000, fee_rate)
        assert result.net_paise == 100_000
        assert result.fee_paise == expected_gross - 100_000 == 2_041

        _pause()
        fetched = rzp_client.order.fetch(result.order_id)

        assert fetched["amount"] == expected_gross
        assert fetched["id"] == result.order_id
        assert result.raw["status"] == "created"


class TestScenarioCSplitStart:
    """Mode 3: one amount, collected as sequential tranches."""

    def test_split_session_and_first_tranche_order_are_created_live(
        self, rzp_client: Any, split_composer: PaymentComposer
    ) -> None:
        result = split_composer.create_order(500_000, mode=PaymentMode.SPLIT, receipt=_receipt())

        assert result.order_id.startswith("order_")
        assert result.amount_paise == 199_900
        assert result.tranche_index == 0
        assert result.session_id is not None
        assert result.mode is PaymentMode.SPLIT

        _pause()
        fetched = rzp_client.order.fetch(result.order_id)
        assert fetched["amount"] == 199_900
        assert fetched["status"] == "created"

        session = split_composer.store.get(result.session_id)
        assert session is not None
        assert session.status is SessionStatus.PENDING
        assert [tranche.amount_paise for tranche in session.tranches] == TRANCHES
        assert session.tranches[0].status is TrancheStatus.PENDING
        assert session.tranches[0].order_id == result.order_id
        assert session.tranches[1].order_id is None, (
            "tranche 2 is ordered only after tranche 1 pays"
        )
        assert session.total_paid_paise() == 0

    def test_an_amount_within_one_tranche_creates_no_session_live(
        self, rzp_client: Any, split_composer: PaymentComposer
    ) -> None:
        """Below the ceiling there is nothing to sequence: one ordinary order."""
        result = split_composer.create_order(1_500, mode=PaymentMode.SPLIT)

        assert result.session_id is None
        assert result.amount_paise == 1_500

        _pause()
        assert rzp_client.order.fetch(result.order_id)["amount"] == 1_500


class TestScenarioDWebhooks:
    """Webhook verification, using the SDK's real HMAC implementation."""

    def test_valid_signature_passes_and_tampering_is_rejected(self, rzp_client: Any) -> None:
        payload = json.dumps(
            {
                "entity": "event",
                "account_id": "acc_test",
                "event": "payment.captured",
                "contains": ["payment"],
                "payload": {
                    "payment": {
                        "entity": {
                            "id": "pay_TESTScenarioD",
                            "entity": "payment",
                            "amount": 50_000,
                            "currency": "INR",
                            "status": "captured",
                            "order_id": "order_TESTScenarioD",
                        }
                    }
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        # In production the webhook secret is configured separately in the
        # dashboard; here we verify the same HMAC path with our key secret.
        signature = hmac.new(KEY_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

        assert verify_webhook(rzp_client, payload, signature, KEY_SECRET) is True

        with pytest.raises(VerificationError):
            verify_webhook(rzp_client, payload, "0" * 64, KEY_SECRET)

        tampered = payload.replace('"amount":50000', '"amount":500000')
        assert tampered != payload
        with pytest.raises(VerificationError):
            verify_webhook(rzp_client, tampered, signature, KEY_SECRET)

    def test_the_sdk_rejection_is_chained_into_our_error(self, rzp_client: Any) -> None:
        body = '{"event":"payment.captured"}'
        signature = hmac.new(KEY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()

        with pytest.raises(VerificationError) as excinfo:
            verify_webhook(rzp_client, body, signature, "a-different-webhook-secret")

        assert isinstance(excinfo.value.__cause__, razorpay.errors.SignatureVerificationError)


class TestScenarioEPaymentVerification:
    """Payment state is mocked; every order created by the flow is live."""

    def test_mocked_payment_advances_split_and_creates_live_tranche_two_order(
        self, rzp_client: Any, split_composer: PaymentComposer
    ) -> None:
        first = split_composer.create_order(500_000, mode=PaymentMode.SPLIT)
        assert first.session_id is not None
        payment_id = f"pay_TEST{uuid.uuid4().hex[:12]}"

        with (
            patch.object(
                rzp_client.utility, "verify_payment_signature", return_value=True
            ) as verify,
            patch.object(
                rzp_client.payment,
                "fetch",
                return_value=_captured_payment(first.order_id, payment_id, 199_900),
            ) as fetch,
        ):
            second = split_composer.verify_and_advance(
                first.session_id, first.order_id, payment_id, "mocked-signature"
            )

            verify.assert_called_once_with(
                {
                    "razorpay_order_id": first.order_id,
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": "mocked-signature",
                }
            )
            fetch.assert_called_once_with(payment_id)

        assert second is not None
        assert second.order_id.startswith("order_")
        assert second.tranche_index == 1
        assert second.amount_paise == 199_900

        session = split_composer.store.get(first.session_id)
        assert session is not None
        assert session.tranches[0].status is TrancheStatus.PAID
        assert session.tranches[0].payment_id == payment_id
        assert session.status is SessionStatus.IN_PROGRESS
        assert session.total_paid_paise() == 199_900

        _pause()
        live = rzp_client.order.fetch(second.order_id)

        assert live["id"] == second.order_id
        assert live["amount"] == 199_900, "tranche 2's order really exists in the sandbox"
        assert live["status"] == "created"

    def test_split_session_drives_to_completion_with_live_orders(
        self, rzp_client: Any, split_composer: PaymentComposer
    ) -> None:
        first = split_composer.create_order(500_000, mode=PaymentMode.SPLIT)
        assert first.session_id is not None
        session_id = first.session_id
        orders = [first]
        current = first

        for index, amount_paise in enumerate(TRANCHES):
            payment_id = f"pay_TEST{index}{uuid.uuid4().hex[:10]}"
            with (
                patch.object(rzp_client.utility, "verify_payment_signature", return_value=True),
                patch.object(
                    rzp_client.payment,
                    "fetch",
                    return_value=_captured_payment(current.order_id, payment_id, amount_paise),
                ),
            ):
                nxt = split_composer.verify_and_advance(
                    session_id, current.order_id, payment_id, "mocked-signature"
                )

            if index < len(TRANCHES) - 1:
                assert nxt is not None, f"tranche {index} should unlock tranche {index + 1}"
                orders.append(nxt)
                current = nxt
            else:
                assert nxt is None, "the last tranche completes the session"

        session = split_composer.store.get(session_id)
        assert session is not None
        assert session.status is SessionStatus.COMPLETE
        assert session.is_fully_paid()
        assert [tranche.status for tranche in session.tranches] == [TrancheStatus.PAID] * 3

        _pause()
        for order, expected_amount in zip(orders, TRANCHES, strict=True):
            fetched = rzp_client.order.fetch(order.order_id)
            assert fetched["amount"] == expected_amount
        assert [order.order_id for order in orders] == [
            tranche.order_id for tranche in session.tranches
        ]

    def test_signature_parameters_satisfy_the_real_sdk_verifier(
        self, rzp_client: Any, split_composer: PaymentComposer
    ) -> None:
        """Pin the parameter contract by delegating the mocked call to the real SDK."""
        first = split_composer.create_order(500_000, mode=PaymentMode.SPLIT)
        assert first.session_id is not None
        payment_id = f"pay_TEST{uuid.uuid4().hex[:12]}"
        real_verify = rzp_client.utility.verify_payment_signature
        seen: list[dict[str, Any]] = []

        def spy(parameters: dict[str, Any]) -> bool:
            seen.append(dict(parameters))
            return bool(real_verify(parameters))

        with (
            patch.object(rzp_client.utility, "verify_payment_signature", side_effect=spy),
            patch.object(
                rzp_client.payment,
                "fetch",
                return_value=_captured_payment(first.order_id, payment_id, 199_900),
            ),
        ):
            second = split_composer.verify_and_advance(
                first.session_id,
                first.order_id,
                payment_id,
                _payment_signature(first.order_id, payment_id),
            )

        assert seen == [
            {
                "razorpay_order_id": first.order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": _payment_signature(first.order_id, payment_id),
            }
        ]
        assert second is not None, "the real SDK accepted the signature we sent"

        _pause()
        assert rzp_client.order.fetch(second.order_id)["amount"] == 199_900


class TestResumeWithLiveOrders:
    """``resume`` reads live order state; no mocking is needed at all."""

    def test_resume_returns_the_still_payable_pending_order(
        self, rzp_client: Any, split_composer: PaymentComposer
    ) -> None:
        first = split_composer.create_order(500_000, mode=PaymentMode.SPLIT)
        assert first.session_id is not None

        _pause()
        resumed = split_composer.resume(first.session_id)

        assert resumed.order_id == first.order_id, "a 'created' order is still payable"
        assert resumed.amount_paise == 199_900

        _pause()
        assert rzp_client.order.fetch(resumed.order_id)["status"] == "created"
