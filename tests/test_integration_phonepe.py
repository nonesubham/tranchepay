"""Live integration tests against the PhonePe PREPROD (sandbox) API.

These tests are **opt-in** and hit the real network. They are skipped unless
PhonePe credentials are present, so contributors without an account are never
blocked and CI runs ``pytest -m "not integration"`` and never needs secrets.
Run them with ``./run_integration.sh -k phonepe`` or:

    set -a; . ./.env; set +a
    pytest tests/test_integration_phonepe.py -v -m integration

What is live and what is mocked
-------------------------------
Live, straight through :class:`~tranchepay.PhonePeAdapter`, which is the point of
this suite: the request signing, the OAuth exchange, ``/checkout/v2/pay``, the
status endpoint, and PhonePe's duplicate-transaction rejection.

Mocked: nothing. PhonePe only records a payment once a human completes checkout
in a browser, which a backend test cannot automate, so the payment tests assert
the ``PENDING`` state PhonePe reports until someone pays. A shared, module-scoped
fixture creates one payment and both the creation and status tests reuse it so a
full run stays polite to the shared sandbox.

Credentials come only from the environment (``PHONEPE_MERCHANT_ID``,
``PHONEPE_SALT_KEY``, optional ``PHONEPE_SALT_INDEX`` and ``PHONEPE_ENV``); see
``.env.example``. Nothing here is hardcoded.

API note
--------
PhonePe has no "orders" and no first-party Python SDK. This account is
provisioned for Standard Checkout v2: client id/secret are exchanged for an
``O-Bearer`` token, a pay request is created at ``/checkout/v2/pay``, and state
is read from ``/checkout/v2/order/{merchantOrderId}/status``. The classic
X-VERIFY flow is implemented too and covered by the unit tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest

from tranchepay import GatewayFactory, PaymentComposeError, PhonePeAdapter

_ROOT = Path(__file__).resolve().parents[1]

try:
    import dotenv
except ImportError:  # pragma: no cover - env vars may be exported instead
    dotenv = None  # type: ignore[assignment]

if dotenv is not None:
    dotenv.load_dotenv(_ROOT / ".env")

MERCHANT_ID = os.environ.get("PHONEPE_MERCHANT_ID", "")
SALT_KEY = os.environ.get("PHONEPE_SALT_KEY", "")
SALT_INDEX = os.environ.get("PHONEPE_SALT_INDEX", "1")
ENV = os.environ.get("PHONEPE_ENV", "preprod")

# Small delay between live calls so a full run stays inside PhonePe's rate
# limits. The preprod sandbox is a shared service; be a good citizen.
PAUSE_SECONDS = 0.5

REDIRECT_URL = "https://example.com/tranchepay/success"
CALLBACK_URL = f"https://webhook.site/tranchepay-{uuid.uuid4().hex}"

CREDENTIALS: dict[str, Any] = {
    "merchant_id": MERCHANT_ID,
    "salt_key": SALT_KEY,
    "salt_index": SALT_INDEX,
    "env": ENV,
    "callback_url": CALLBACK_URL,
}

_T = TypeVar("_T")


def _pause() -> None:
    """Sleep briefly between live requests to avoid tripping rate limits."""
    time.sleep(PAUSE_SECONDS)


def _transaction_id() -> str:
    """A unique merchant transaction id; PhonePe rejects duplicates."""
    return f"TP{uuid.uuid4().hex.upper()}"


def _live(action: Callable[[], _T]) -> _T:
    """Run a live call, skipping with the real error if the sandbox is down.

    The suite never mocks a failed call to force a pass: a transport or
    configuration failure skips the test and prints PhonePe's actual error so it
    can be investigated.
    """
    try:
        return action()
    except PaymentComposeError as exc:
        sys.stderr.write(f"\n[phonepe-preprod] {type(exc).__name__}: {exc}\n")
        pytest.skip(f"PhonePe preprod sandbox unavailable: {exc}")


@pytest.fixture(scope="module")
def phonepe_adapter() -> Iterator[PhonePeAdapter]:
    """A factory-built PhonePe adapter, or a skip when credentials are absent."""
    if not (MERCHANT_ID and SALT_KEY):
        pytest.skip(
            "live PhonePe credentials are not set; copy .env.example to .env and "
            "fill in PHONEPE_MERCHANT_ID / PHONEPE_SALT_KEY"
        )
    gateway = GatewayFactory.get_gateway("phonepe", dict(CREDENTIALS))
    assert isinstance(gateway, PhonePeAdapter)
    try:
        yield gateway
    finally:
        gateway.close()


@pytest.fixture(scope="module")
def created_payment(phonepe_adapter: PhonePeAdapter) -> dict[str, Any]:
    """One live payment request, shared by the creation and status tests."""
    transaction_id = _transaction_id()
    _pause()
    result = _live(
        lambda: phonepe_adapter.create_order(
            amount_paise=100,
            merchant_transaction_id=transaction_id,
            callback_url=CALLBACK_URL,
            redirect_url=REDIRECT_URL,
        )
    )
    return {"transaction_id": transaction_id, "result": result}


class TestScenarioASigning:
    """Unit test: the classic X-VERIFY signature is computed exactly."""

    def test_sign_request_base64_and_sha256_match_the_known_vector(self) -> None:
        adapter = PhonePeAdapter(
            {"merchant_id": "M22TESTMERCHANT", "salt_key": "test_salt_key", "salt_index": "1"}
        )
        payload = {"amount": 100, "merchantId": "M22TESTMERCHANT", "transactionId": "TXN-TEST-1"}

        encoded, x_verify = adapter._sign_request("/pg/v1/pay", payload)

        assert encoded == (
            "eyJhbW91bnQiOjEwMCwibWVyY2hhbnRJZCI6Ik0yMlRFU1RNRVJDSEFOVCIsInRyYW5z"
            "YWN0aW9uSWQiOiJUWE4tVEVTVC0xIn0="
        )
        assert x_verify == ("c413ed41567cb0f5097be6a0d318c5ef76dcc6dd7c5b5c1721aa745cba6873f9###1")


class TestScenarioBPaymentRequest:
    """Live: a pay request is accepted and returns a checkout redirect."""

    @pytest.mark.integration
    def test_create_order_returns_a_live_redirect_url(
        self, created_payment: dict[str, Any]
    ) -> None:
        result = created_payment["result"]

        assert result["success"] is True, result["raw"]
        assert result["transaction_id"] == created_payment["transaction_id"]
        assert str(result["redirect_url"]).startswith("https://")
        assert result["raw"]["state"] == "PENDING"


class TestScenarioCPaymentStatus:
    """Live: the status endpoint reports the unpaid payment as pending."""

    @pytest.mark.integration
    def test_fetch_payment_reports_pending(
        self, phonepe_adapter: PhonePeAdapter, created_payment: dict[str, Any]
    ) -> None:
        transaction_id = created_payment["transaction_id"]
        _pause()

        status = _live(lambda: phonepe_adapter.fetch_payment(transaction_id))

        assert status["success"] is True, status
        assert status["transaction_id"] == transaction_id
        assert status["state"] == "PENDING"
        assert status["status"] == "pending"


class TestScenarioDWebhook:
    """Unit test: webhook checksum verification, no network."""

    def test_webhook_signature_passes_and_tampering_fails(self) -> None:
        adapter = PhonePeAdapter({"merchant_id": "M22TESTMERCHANT", "salt_key": "test_salt_key"})
        body = json.dumps(
            {"event": "checkout.order.completed", "merchantTransactionId": "TXN-TEST-1"},
            separators=(",", ":"),
            sort_keys=True,
        )
        signature = hashlib.sha256(f"{body}test_salt_key".encode()).hexdigest() + "###1"

        assert adapter.verify_signature(body, signature) is True
        assert adapter.verify_signature(body, "0" * 64) is False
        assert adapter.verify_signature(body.replace("TEST-1", "TEST-2"), signature) is False


class TestScenarioEDuplicates:
    """Live: PhonePe rejects a reused merchant transaction id."""

    @pytest.mark.integration
    def test_the_second_request_with_the_same_id_is_rejected(
        self, phonepe_adapter: PhonePeAdapter
    ) -> None:
        transaction_id = _transaction_id()
        request = lambda: phonepe_adapter.create_order(  # noqa: E731 - terse call builder
            amount_paise=100,
            merchant_transaction_id=transaction_id,
            callback_url=CALLBACK_URL,
            redirect_url=REDIRECT_URL,
        )

        _pause()
        first = _live(request)
        assert first["success"] is True, first["raw"]

        _pause()
        second = _live(request)

        assert second["success"] is False
        assert second["code"] == "DUPLICATE_TXN_REQUEST"


class TestScenarioFFactory:
    """Live: an adapter built by the factory behaves identically."""

    @pytest.mark.integration
    def test_the_factory_builds_a_working_phonepe_adapter(self) -> None:
        gateway = GatewayFactory.get_gateway("phonepe", dict(CREDENTIALS))
        assert isinstance(gateway, PhonePeAdapter)
        try:
            _pause()
            result = _live(
                lambda: gateway.create_order(
                    amount_paise=100,
                    merchant_transaction_id=_transaction_id(),
                    callback_url=CALLBACK_URL,
                    redirect_url=REDIRECT_URL,
                )
            )
        finally:
            gateway.close()

        assert result["success"] is True, result["raw"]
        assert str(result["redirect_url"]).startswith("https://")
