"""The gateway factory and the provider-agnostic composer contract.

Nothing here touches the network: the Razorpay path is checked against the
in-process client, and the HTTP adapters are exercised only far enough to prove
their request building, because sending a Paytm or PhonePe call needs an account
tranchepay cannot fabricate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, cast
from unittest.mock import Mock

import pytest

from tests.fakes import FakeHttpClient, FakeHttpResponse, FakeRazorpayClient
from tranchepay import (
    ChargesConfig,
    GatewayConfigurationError,
    GatewayFactory,
    OrderResult,
    PaymentComposer,
    PaymentGateway,
    PaymentMode,
    PaytmAdapter,
    PhonePeAdapter,
    RazorpayAdapter,
    UnsupportedGatewayError,
)


class StubGateway:
    """A minimal, fully in-memory :class:`PaymentGateway` for structural tests."""

    def __init__(self) -> None:
        self.created: list[int] = []
        self.refunds: list[tuple[str, int]] = []

    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str | None = None,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.created.append(amount_paise)
        return {
            "id": f"order_stub_{len(self.created)}",
            "amount": amount_paise,
            "currency": currency or "INR",
            "status": "created",
        }

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "status": "created"}

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return {"id": payment_id, "status": "captured", "amount": 0}

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        return True

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        return True

    def refund_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        self.refunds.append((payment_id, amount_paise))
        return {"id": "rfnd_stub"}


AdapterFactory = Callable[[Mapping[str, Any]], PaymentGateway]

RAZORPAY_CREDENTIALS = {"key_id": "rzp_test_placeholder", "key_secret": "placeholder"}
PAYTM_CREDENTIALS = {"merchant_id": "mid", "merchant_key": "mkey"}
PHONEPE_CREDENTIALS = {"merchant_id": "mid", "salt_key": "salt"}


class TestGetGateway:
    def test_razorpay_credentials_build_a_razorpay_adapter(self) -> None:
        gateway = GatewayFactory.get_gateway("razorpay", RAZORPAY_CREDENTIALS)

        assert isinstance(gateway, RazorpayAdapter)

    def test_an_existing_client_can_be_reused(self, fake_client: FakeRazorpayClient) -> None:
        gateway = GatewayFactory.get_gateway("razorpay", {"client": fake_client})

        assert isinstance(gateway, RazorpayAdapter)
        assert gateway.client is fake_client

    def test_paytm_returns_a_paytm_adapter(self) -> None:
        assert isinstance(GatewayFactory.get_gateway("paytm", PAYTM_CREDENTIALS), PaytmAdapter)

    def test_phonepe_returns_a_phonepe_adapter(self) -> None:
        gateway = GatewayFactory.get_gateway("phonepe", PHONEPE_CREDENTIALS)

        assert isinstance(gateway, PhonePeAdapter)

    @pytest.mark.parametrize("provider", ["  RAZORPAY  ", "Razorpay"])
    def test_provider_names_are_case_insensitive(self, provider: str) -> None:
        assert isinstance(
            GatewayFactory.get_gateway(provider, RAZORPAY_CREDENTIALS), RazorpayAdapter
        )

    def test_an_unknown_provider_is_rejected(self) -> None:
        with pytest.raises(UnsupportedGatewayError, match="unsupported payment gateway"):
            GatewayFactory.get_gateway("stripe", RAZORPAY_CREDENTIALS)

    def test_the_error_lists_the_registered_providers(self) -> None:
        with pytest.raises(UnsupportedGatewayError, match="razorpay"):
            GatewayFactory.get_gateway("unknown", {})

    def test_available_gateways_lists_the_shipped_adapters(self) -> None:
        assert {"razorpay", "paytm", "phonepe"} <= set(GatewayFactory.available_gateways())

    def test_missing_credentials_are_reported_by_the_adapter(self) -> None:
        with pytest.raises(GatewayConfigurationError, match="key_secret"):
            GatewayFactory.get_gateway("razorpay", {"key_id": "rzp_test_placeholder"})


class TestRegisterGateway:
    def test_a_custom_gateway_can_be_registered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = StubGateway()
        monkeypatch.setattr(GatewayFactory, "_builders", dict(GatewayFactory._builders))

        GatewayFactory.register_gateway("custom", lambda credentials: adapter)

        assert GatewayFactory.get_gateway("custom", {}) is adapter
        assert "custom" in GatewayFactory.available_gateways()

    def test_an_adapter_class_can_be_registered_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(GatewayFactory, "_builders", dict(GatewayFactory._builders))
        GatewayFactory.register_gateway("paytm-sandbox", PaytmAdapter)

        gateway = GatewayFactory.get_gateway("paytm-sandbox", PAYTM_CREDENTIALS)

        assert isinstance(gateway, PaytmAdapter)

    def test_an_empty_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            GatewayFactory.register_gateway("   ", lambda credentials: StubGateway())


class TestGatewayProtocol:
    def test_a_custom_class_satisfies_the_protocol(self) -> None:
        assert isinstance(StubGateway(), PaymentGateway)

    def test_a_raw_client_does_not_satisfy_the_protocol(
        self, fake_client: FakeRazorpayClient
    ) -> None:
        assert not isinstance(fake_client, PaymentGateway)

    def test_the_razorpay_adapter_wraps_without_changing_the_client(
        self, fake_client: FakeRazorpayClient
    ) -> None:
        adapter = RazorpayAdapter(fake_client)

        assert isinstance(adapter, PaymentGateway)
        assert adapter.client is fake_client


def _drive(adapter_type: type[PaymentGateway]) -> tuple[OrderResult, OrderResult]:
    """Run the same two modes through a mock of ``adapter_type``."""
    gateway = Mock(spec=adapter_type)
    gateway.create_order.return_value = {
        "id": "order_same",
        "amount": 1_000,
        "currency": "INR",
        "status": "created",
    }
    composer = PaymentComposer(
        cast(PaymentGateway, gateway), charges=ChargesConfig(fee_rate=Decimal("0.02"))
    )
    return (
        composer.create_order(1_000, mode=PaymentMode.EXACT),
        composer.create_order(1_000, mode=PaymentMode.WITH_CHARGES),
    )


def test_the_composer_behaves_identically_for_mocked_paytm_and_razorpay() -> None:
    """Swapping the adapter underneath the composer changes nothing it returns."""
    assert _drive(PaytmAdapter) == _drive(RazorpayAdapter)


class TestHttpAdapters:
    def test_paytm_is_a_gateway_and_builds_a_live_shaped_request(self) -> None:
        adapter = PaytmAdapter(PAYTM_CREDENTIALS)

        assert isinstance(adapter, PaymentGateway)
        with pytest.raises(NotImplementedError, match="initiateTransaction"):
            adapter.create_order(1_500, currency="INR", notes={"sku": "abc"})

    def test_paytm_refund_request_uses_rupee_strings(self) -> None:
        adapter = PaytmAdapter(PAYTM_CREDENTIALS)

        request = adapter.refund_request("pay_1", 1_500)

        assert request.url == "https://api.paytm.com/v2/refund/apply"
        assert request.json is not None
        assert request.json["refundAmount"] == {"value": "15.00", "currency": "INR"}

    def test_phonepe_create_order_targets_the_v2_pay_endpoint(self) -> None:
        http = FakeHttpClient(
            [
                FakeHttpResponse(payload={"access_token": "tok", "expires_in": 3600}),
                FakeHttpResponse(
                    payload={
                        "orderId": "OMO1",
                        "state": "PENDING",
                        "redirectUrl": "https://mercury.test/checkout",
                    }
                ),
            ]
        )
        adapter = PhonePeAdapter(PHONEPE_CREDENTIALS, client=http)

        assert isinstance(adapter, PaymentGateway)
        result = adapter.create_order(1_500, currency="INR")

        assert result["success"] is True
        assert result["redirect_url"] == "https://mercury.test/checkout"
        assert result["transaction_id"]
        request = http.requests[1]
        assert request["method"] == "POST"
        assert request["url"] == ("https://api-preprod.phonepe.com/apis/pg-sandbox/checkout/v2/pay")
        assert request["json"]["amount"] == 1_500
        assert request["json"]["merchantOrderId"] == result["transaction_id"]

    def test_phonepe_status_request_targets_the_merchant_order(self) -> None:
        http = FakeHttpClient(
            [
                FakeHttpResponse(payload={"access_token": "tok", "expires_in": 3600}),
                FakeHttpResponse(payload={"orderId": "OMO1", "state": "PENDING", "amount": 1_500}),
            ]
        )
        adapter = PhonePeAdapter(PHONEPE_CREDENTIALS, client=http)

        result = adapter.fetch_payment("txn_1")

        request = http.requests[-1]
        assert request["method"] == "GET"
        assert request["url"] == (
            "https://api-preprod.phonepe.com/apis/pg-sandbox/checkout/v2/order/txn_1/status"
        )
        assert request["headers"]["Authorization"] == "O-Bearer tok"
        assert result["state"] == "PENDING"

    def test_phonepe_webhook_checksums_are_verified(self) -> None:
        adapter = PhonePeAdapter(PHONEPE_CREDENTIALS)
        payload = '{"event":"checkout.order.completed"}'
        signature = hashlib.sha256(f"{payload}salt".encode()).hexdigest() + "###1"

        assert adapter.verify_signature(payload, signature) is True
        assert adapter.verify_signature(payload, "0" * 64) is False

    @pytest.mark.parametrize(
        ("adapter_factory", "credentials"),
        [(PaytmAdapter, PAYTM_CREDENTIALS), (PhonePeAdapter, PHONEPE_CREDENTIALS)],
    )
    def test_signature_verification_is_explicitly_unimplemented(
        self, adapter_factory: AdapterFactory, credentials: dict[str, str]
    ) -> None:
        adapter = adapter_factory(credentials)

        with pytest.raises(NotImplementedError):
            adapter.verify_signature("order_1", "pay_1", "signature")

    @pytest.mark.parametrize(
        ("adapter_factory", "credentials"),
        [(PaytmAdapter, PAYTM_CREDENTIALS), (PhonePeAdapter, PHONEPE_CREDENTIALS)],
    )
    def test_missing_credentials_are_rejected(
        self, adapter_factory: AdapterFactory, credentials: dict[str, str]
    ) -> None:
        with pytest.raises(GatewayConfigurationError, match="merchant_id"):
            adapter_factory({})
