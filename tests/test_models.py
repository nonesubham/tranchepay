"""Config, session, and response models."""

from __future__ import annotations

import decimal
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.conftest import SessionFactory
from tranchepay import (
    DEFAULT_CURRENCY,
    DEFAULT_TRANCHE_PAISE,
    ChargesConfig,
    OrderResult,
    PaymentMode,
    RoundingPolicy,
    SessionStatus,
    SplitConfig,
    SplitSession,
    Tranche,
    TrancheStatus,
)


class TestChargesConfig:
    def test_defaults(self) -> None:
        config = ChargesConfig(fee_rate=Decimal("0.0236"))
        assert config.fee_rate == Decimal("0.0236")
        assert isinstance(config.fee_rate, Decimal)
        assert config.rounding is RoundingPolicy.ROUND_HALF_UP
        assert config.currency == DEFAULT_CURRENCY

    def test_rounding_policy_is_explicit_and_matches_decimal_constants(self) -> None:
        assert RoundingPolicy.ROUND_HALF_UP.value == decimal.ROUND_HALF_UP
        assert RoundingPolicy.ROUND_HALF_EVEN.value == decimal.ROUND_HALF_EVEN

    def test_zero_fee_rate_is_allowed(self) -> None:
        assert ChargesConfig(fee_rate=Decimal(0)).fee_rate == 0

    @pytest.mark.parametrize("fee_rate", [Decimal("-0.01"), Decimal(1), Decimal("1.5")])
    def test_out_of_range_fee_rates_are_rejected(self, fee_rate: Decimal) -> None:
        with pytest.raises(ValidationError):
            ChargesConfig(fee_rate=fee_rate)

    @pytest.mark.parametrize("fee_rate", [Decimal("NaN"), Decimal("Infinity")])
    def test_non_finite_fee_rates_are_rejected(self, fee_rate: Decimal) -> None:
        with pytest.raises(ValidationError):
            ChargesConfig(fee_rate=fee_rate)

    def test_config_is_frozen(self) -> None:
        config = ChargesConfig(fee_rate=Decimal("0.02"))
        with pytest.raises(ValidationError):
            config.fee_rate = Decimal("0.03")  # type: ignore[misc]

    def test_unknown_keys_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChargesConfig(fee_rate=Decimal("0.02"), fee=Decimal("0.02"))  # type: ignore[call-arg]


class TestSplitConfig:
    def test_default_threshold_is_the_npci_ceiling(self) -> None:
        assert SplitConfig().tranche_paise == 199_900
        assert DEFAULT_TRANCHE_PAISE == 199_900
        assert SplitConfig.model_fields["tranche_paise"].default == DEFAULT_TRANCHE_PAISE

    @pytest.mark.parametrize("tranche_paise", [0, -1])
    def test_non_positive_thresholds_are_rejected(self, tranche_paise: int) -> None:
        with pytest.raises(ValidationError):
            SplitConfig(tranche_paise=tranche_paise)


class TestTranche:
    def test_defaults(self) -> None:
        tranche = Tranche(index=1, amount_paise=500)
        assert tranche.status is TrancheStatus.PENDING
        assert tranche.order_id is None
        assert tranche.payment_id is None

    def test_amount_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Tranche(index=0, amount_paise=0)

    def test_index_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            Tranche(index=-1, amount_paise=1)


class TestSplitSession:
    def test_helpers(self, make_session: SessionFactory) -> None:
        session = make_session((1_000, 1_000, 500))
        session.tranches[0].status = TrancheStatus.PAID
        session.tranches[0].payment_id = "pay_1"
        session.tranches[0].order_id = "order_1"

        assert session.tranche_for_order("order_1") is session.tranches[0]
        assert session.tranche_for_order("order_missing") is None
        assert session.next_pending_tranche() is session.tranches[1]
        assert [t.index for t in session.paid_tranches()] == [0]
        assert session.total_paid_paise() == 1_000
        assert session.is_terminal() is False

    @pytest.mark.parametrize("status", [SessionStatus.COMPLETE, SessionStatus.ABORTED])
    def test_terminal_statuses(self, make_session: SessionFactory, status: SessionStatus) -> None:
        assert make_session(status=status).is_terminal() is True

    def test_generated_session_id_is_a_uuid4(self) -> None:
        session = SplitSession(
            amount_paise=100,
            tranche_paise=100,
            tranches=[Tranche(index=0, amount_paise=100)],
        )
        parsed = uuid.UUID(session.session_id)
        assert parsed.version == 4

    def test_timestamps_are_timezone_aware(self, make_session: SessionFactory) -> None:
        session = make_session()
        assert session.created_at.tzinfo is not None
        assert session.updated_at.tzinfo is not None

    def test_touched_advances_updated_at(self, make_session: SessionFactory) -> None:
        session = make_session()
        assert session.touched().updated_at >= session.updated_at

    def test_tranche_indices_must_be_sequential(self) -> None:
        with pytest.raises(ValidationError):
            SplitSession(
                amount_paise=30,
                tranche_paise=10,
                tranches=[Tranche(index=1, amount_paise=10), Tranche(index=2, amount_paise=20)],
            )

    def test_tranches_must_sum_to_the_session_amount(self) -> None:
        with pytest.raises(ValidationError):
            SplitSession(
                amount_paise=99,
                tranche_paise=10,
                tranches=[Tranche(index=0, amount_paise=10)],
            )

    def test_session_needs_at_least_one_tranche(self) -> None:
        with pytest.raises(ValidationError):
            SplitSession(amount_paise=99, tranche_paise=10, tranches=[])

    def test_json_round_trip(self, make_session: SessionFactory) -> None:
        session = make_session((1_000, 500))
        session.tranches[0].status = TrancheStatus.PAID
        session.tranches[0].payment_id = "pay_1"

        restored = SplitSession.model_validate_json(session.model_dump_json())
        assert restored == session
        assert restored.tranches[0].payment_id == "pay_1"

    def test_dict_round_trip(self, make_session: SessionFactory) -> None:
        session = make_session()
        assert SplitSession.model_validate(session.model_dump()) == session


class TestOrderResult:
    def test_fields(self) -> None:
        result = OrderResult(
            order_id="order_1",
            amount_paise=199_900,
            currency="INR",
            mode=PaymentMode.SPLIT,
            session_id="session-1",
            tranche_index=0,
            raw={"id": "order_1", "status": "created"},
        )
        assert result.fee_paise == 0
        assert result.net_paise is None
        assert result.raw["status"] == "created"

    def test_is_frozen(self) -> None:
        result = OrderResult(
            order_id="order_1", amount_paise=1, currency="INR", mode=PaymentMode.EXACT
        )
        with pytest.raises(ValidationError):
            result.order_id = "order_2"  # type: ignore[misc]
