"""Modes 1 and 2: EXACT and WITH_CHARGES."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fakes import FakeRazorpayClient
from tranchepay import (
    ChargesConfig,
    PaymentComposeError,
    PaymentComposer,
    PaymentGateway,
    PaymentMode,
    RazorpayAdapter,
    RoundingPolicy,
)


class TestExactMode:
    def test_charges_exactly_the_requested_amount(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        result = composer.create_order(150_000, mode=PaymentMode.EXACT)

        assert result.order_id == "order_0001"
        assert result.amount_paise == 150_000
        assert result.mode is PaymentMode.EXACT
        assert result.currency == "INR"
        assert result.net_paise is None
        assert result.fee_paise == 0
        assert fake_client.created_amounts() == [150_000]

    def test_exact_is_the_default_mode(self, composer: PaymentComposer) -> None:
        assert composer.create_order(500).mode is PaymentMode.EXACT

    def test_accepts_the_plain_string_mode(self, composer: PaymentComposer) -> None:
        """``PaymentMode`` is a str enum, so its raw value works at runtime too."""
        result = composer.create_order(500, mode="exact")  # type: ignore[arg-type]

        assert result.mode is PaymentMode.EXACT

    def test_order_is_auto_captured(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        composer.create_order(500)

        request = fake_client.created_orders[0]["request"]
        assert request["payment_capture"] == 1
        assert request["currency"] == "INR"

    def test_amount_reaches_the_api_as_an_int(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        composer.create_order(1_999)

        assert isinstance(fake_client.created_orders[0]["amount"], int)
        assert fake_client.created_orders[0]["amount"] == 1_999

    def test_no_session_is_created(self, composer: PaymentComposer) -> None:
        result = composer.create_order(500)

        assert result.session_id is None
        assert result.tranche_index is None

    def test_receipt_and_notes_are_forwarded(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        result = composer.create_order(500, receipt="rcpt_1", notes={"sku": "abc"})

        assert fake_client.created_orders[0]["receipt"] == "rcpt_1"
        assert fake_client.created_orders[0]["notes"] == {"sku": "abc"}
        assert result.receipt == "rcpt_1"

    def test_absent_receipt_and_notes_are_not_sent(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        composer.create_order(500)

        request = fake_client.created_orders[0]["request"]
        assert "notes" not in request
        assert "receipt" not in request

    def test_raw_response_is_preserved(self, composer: PaymentComposer) -> None:
        result = composer.create_order(500)

        assert result.raw["id"] == result.order_id
        assert result.raw["status"] == "created"

    def test_currency_can_be_overridden(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        result = composer.create_order(500, currency="USD")

        assert result.currency == "USD"
        assert fake_client.created_orders[0]["currency"] == "USD"

    def test_composer_currency_is_used_by_default(self, fake_gateway: RazorpayAdapter) -> None:
        composer = PaymentComposer(fake_gateway, currency="AED")

        assert composer.create_order(500).currency == "AED"

    @pytest.mark.parametrize("amount_paise", [0, -100])
    def test_non_positive_amounts_are_rejected(
        self, composer: PaymentComposer, amount_paise: int
    ) -> None:
        with pytest.raises(ValueError, match="amount_paise must be > 0"):
            composer.create_order(amount_paise)

    def test_float_amounts_are_rejected(self, composer: PaymentComposer) -> None:
        with pytest.raises(TypeError, match="must be an int number of paise"):
            composer.create_order(100.0)  # type: ignore[arg-type]

    def test_unknown_mode_is_rejected(self, composer: PaymentComposer) -> None:
        with pytest.raises(PaymentComposeError, match="unknown payment mode"):
            composer.create_order(500, mode="telepathy")  # type: ignore[arg-type]

    def test_response_without_an_id_is_rejected(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        fake_client.order.create = lambda data, **kwargs: {"status": "created"}  # type: ignore[method-assign]

        with pytest.raises(PaymentComposeError, match="no usable 'id'"):
            composer.create_order(500)


class TestWithChargesMode:
    def test_customer_is_charged_the_grossed_up_amount(
        self, composer: PaymentComposer, fake_client: FakeRazorpayClient
    ) -> None:
        result = composer.create_order(100_000, mode=PaymentMode.WITH_CHARGES)

        assert result.amount_paise == 102_417
        assert result.net_paise == 100_000
        assert result.fee_paise == 2_417
        assert fake_client.created_amounts() == [102_417]

    def test_gross_equals_net_when_the_fee_rate_is_zero(
        self, fake_gateway: RazorpayAdapter
    ) -> None:
        composer = PaymentComposer(fake_gateway, charges=ChargesConfig(fee_rate=Decimal(0)))

        result = composer.create_order(199_900, mode=PaymentMode.WITH_CHARGES)

        assert result.amount_paise == 199_900
        assert result.fee_paise == 0

    def test_configured_rounding_policy_is_applied(self, fake_gateway: RazorpayAdapter) -> None:
        half_up = PaymentComposer(
            fake_gateway, charges=ChargesConfig(fee_rate=Decimal("0.952"))
        ).create_order(3, mode=PaymentMode.WITH_CHARGES)
        round_down = PaymentComposer(
            fake_gateway,
            charges=ChargesConfig(fee_rate=Decimal("0.952"), rounding=RoundingPolicy.ROUND_DOWN),
        ).create_order(3, mode=PaymentMode.WITH_CHARGES)

        assert half_up.amount_paise == 63
        assert round_down.amount_paise == 62
        assert round_down.net_paise == 3

    def test_requires_a_charges_config(self, fake_gateway: RazorpayAdapter) -> None:
        composer = PaymentComposer(fake_gateway)

        with pytest.raises(PaymentComposeError, match="requires a ChargesConfig"):
            composer.create_order(100_000, mode=PaymentMode.WITH_CHARGES)

    def test_no_order_is_created_without_a_charges_config(
        self, fake_gateway: RazorpayAdapter, fake_client: FakeRazorpayClient
    ) -> None:
        with pytest.raises(PaymentComposeError):
            PaymentComposer(fake_gateway).create_order(100_000, mode=PaymentMode.WITH_CHARGES)

        assert fake_client.created_orders == []

    def test_mode_is_reported(self, composer: PaymentComposer) -> None:
        assert (
            composer.create_order(100, mode=PaymentMode.WITH_CHARGES).mode
            is PaymentMode.WITH_CHARGES
        )


class TestGatewayContract:
    def test_composer_exposes_the_gateway_untouched(self, fake_gateway: RazorpayAdapter) -> None:
        composer = PaymentComposer(fake_gateway)

        assert composer.gateway is fake_gateway

    @pytest.mark.parametrize("bad_gateway", [object(), None, "client"])
    def test_objects_that_are_not_gateways_are_rejected(self, bad_gateway: object) -> None:
        with pytest.raises(TypeError, match="gateway must be a PaymentGateway"):
            PaymentComposer(bad_gateway)  # type: ignore[arg-type]

    def test_a_gateway_class_is_not_an_instance(self) -> None:
        """Passing the class instead of an instance slips past mypy; the guard catches it."""
        with pytest.raises(TypeError, match="gateway must be a PaymentGateway"):
            PaymentComposer(FakeRazorpayClient)  # type: ignore[arg-type]

    def test_a_raw_client_is_no_longer_a_gateway(self, fake_client: FakeRazorpayClient) -> None:
        """The composer speaks the gateway protocol, never a provider SDK directly."""
        with pytest.raises(TypeError, match="gateway must be a PaymentGateway"):
            PaymentComposer(fake_client)  # type: ignore[arg-type]

    def test_the_official_client_satisfies_the_protocol_when_adapted(self) -> None:
        """tranchepay composes with the real client without touching the network."""
        razorpay = pytest.importorskip("razorpay")

        client = razorpay.Client(auth=("rzp_test_placeholder", "placeholder_secret"))
        adapter = RazorpayAdapter(client)

        assert isinstance(adapter, PaymentGateway)
        assert PaymentComposer(adapter).gateway is adapter
