"""Gross-up arithmetic and the paise/Decimal guards."""

from __future__ import annotations

import decimal
from decimal import Decimal
from fractions import Fraction

import pytest

from tranchepay import RoundingPolicy, gross_up, require_paise, validate_fee_rate


class TestGrossUp:
    def test_realistic_gateway_rate(self) -> None:
        assert gross_up(100_000, Decimal("0.0236")) == 102_417

    def test_zero_fee_rate_is_a_pass_through(self) -> None:
        assert gross_up(123_456, Decimal(0)) == 123_456

    def test_integer_fee_rate_is_accepted_exactly(self) -> None:
        """An ``int`` rate is exact, so runtime tolerance is allowed (typing says Decimal)."""
        assert gross_up(100, 0, RoundingPolicy.ROUND_HALF_UP) == 100  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("net_paise", "fee_rate", "expected"),
        [
            # 3 / (1 - 0.952) = 62.5 exactly: an exact half-way case.
            (3, Decimal("0.952"), 63),
            (2, Decimal("0.968"), 63),
            (1, Decimal("0.6"), 3),
            (3, Decimal("0.6"), 8),
        ],
    )
    def test_half_up_boundaries(self, net_paise: int, fee_rate: Decimal, expected: int) -> None:
        assert gross_up(net_paise, fee_rate, RoundingPolicy.ROUND_HALF_UP) == expected

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            (RoundingPolicy.ROUND_HALF_UP, 63),
            (RoundingPolicy.ROUND_HALF_DOWN, 62),
            (RoundingPolicy.ROUND_HALF_EVEN, 62),  # 62.5 ties toward the even neighbour
            (RoundingPolicy.ROUND_UP, 63),
            (RoundingPolicy.ROUND_CEILING, 63),
            (RoundingPolicy.ROUND_DOWN, 62),
            (RoundingPolicy.ROUND_FLOOR, 62),
        ],
    )
    def test_each_policy_on_a_half_way_value(self, policy: RoundingPolicy, expected: int) -> None:
        assert gross_up(3, Decimal("0.952"), policy) == expected

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            (RoundingPolicy.ROUND_HALF_UP, 8),
            (RoundingPolicy.ROUND_HALF_DOWN, 7),
            (RoundingPolicy.ROUND_HALF_EVEN, 8),  # 7.5 ties toward the even neighbour
        ],
    )
    def test_half_even_rounds_to_the_even_neighbour(
        self, policy: RoundingPolicy, expected: int
    ) -> None:
        assert gross_up(3, Decimal("0.6"), policy) == expected

    def test_default_policy_is_half_up(self) -> None:
        assert gross_up(3, Decimal("0.952")) == gross_up(
            3, Decimal("0.952"), RoundingPolicy.ROUND_HALF_UP
        )

    @pytest.mark.parametrize("fee_rate", ["0.0236", "0.952", "0.6", "0.02"])
    @pytest.mark.parametrize("policy", [RoundingPolicy.ROUND_UP, RoundingPolicy.ROUND_CEILING])
    def test_rounding_up_never_under_recovers_the_fee(
        self, fee_rate: str, policy: RoundingPolicy
    ) -> None:
        rate = Decimal(fee_rate)
        for net_paise in (1, 999, 199_900, 1_000_000):
            gross = gross_up(net_paise, rate, policy)
            assert Fraction(gross) * (Fraction(1) - Fraction(rate)) >= net_paise

    @pytest.mark.parametrize("fee_rate", ["0.0236", "0.952", "0.6", "0.02"])
    def test_half_up_stays_within_half_a_paise(self, fee_rate: str) -> None:
        rate = Decimal(fee_rate)
        tolerance = (Fraction(1) - Fraction(rate)) / 2
        for net_paise in (1, 999, 199_900, 1_000_000):
            gross = gross_up(net_paise, rate, RoundingPolicy.ROUND_HALF_UP)
            recovered = Fraction(gross) * (Fraction(1) - Fraction(rate))
            assert abs(recovered - net_paise) <= tolerance

    def test_round_down_never_overcharges_the_customer(self) -> None:
        rate = Decimal("0.0236")
        for net_paise in (1, 7, 999, 199_901):
            gross = gross_up(net_paise, rate, RoundingPolicy.ROUND_DOWN)
            assert Fraction(gross) * (Fraction(1) - Fraction(rate)) <= net_paise

    def test_multi_digit_rate_stays_exact(self) -> None:
        """Cross-check the rational implementation against Decimal."""
        rate = Decimal("0.0236499999")
        with decimal.localcontext() as ctx:
            ctx.prec = 60
            quotient = Decimal(199_900) / (Decimal(1) - rate)
            expected = int(quotient.quantize(Decimal(1), rounding=decimal.ROUND_HALF_UP))

        assert gross_up(199_900, rate) == expected

    def test_unknown_policy_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="is not a valid RoundingPolicy"):
            gross_up(100, Decimal("0.02"), "ROUND_NEAREST")  # type: ignore[arg-type]

    @pytest.mark.parametrize("net_paise", [0, -1])
    def test_non_positive_net_is_rejected(self, net_paise: int) -> None:
        with pytest.raises(ValueError, match="net_paise must be > 0"):
            gross_up(net_paise, Decimal("0.02"))

    def test_float_net_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be an int number of paise"):
            gross_up(100.5, Decimal("0.02"))  # type: ignore[arg-type]


class TestValidateFeeRate:
    def test_decimal_is_returned_unchanged(self) -> None:
        rate = Decimal("0.0236")
        assert validate_fee_rate(rate) == rate

    def test_int_is_widened_exactly(self) -> None:
        assert validate_fee_rate(0) == Decimal(0)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0.02, "0.02", None, True, [0.02]])
    def test_inexact_or_nonsense_rates_are_rejected(self, bad: object) -> None:
        with pytest.raises(TypeError, match="binary floats are never used for money"):
            validate_fee_rate(bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad",
        [Decimal("-0.01"), Decimal(1), Decimal("1.000001"), Decimal("NaN"), Decimal("-Infinity")],
    )
    def test_out_of_range_and_non_finite_rates_are_rejected(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="fee_rate"):
            validate_fee_rate(bad)


class TestRequirePaise:
    def test_returns_the_amount(self) -> None:
        assert require_paise(1, "amount_paise") == 1

    @pytest.mark.parametrize("bad", [1.0, "1", None, True])
    def test_non_ints_are_rejected(self, bad: object) -> None:
        with pytest.raises(TypeError, match="amount_paise"):
            require_paise(bad, "amount_paise")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_amounts_are_rejected(self, bad: int) -> None:
        with pytest.raises(ValueError, match="amount_paise"):
            require_paise(bad, "amount_paise")
