"""The split engine is a pure function: divmod plus edge cases."""

from __future__ import annotations

import math
import pathlib

import pytest

from tranchepay import (
    DEFAULT_TRANCHE_PAISE,
    SplitConfig,
    SplitPlan,
    estimate_tranche_count,
    plan_tranches,
)

SMALL = 1_000


def test_single_tranche_when_amount_is_below_the_ceiling() -> None:
    plan = plan_tranches(600, SMALL)

    assert plan.amounts_paise == (600,)
    assert plan.tranche_count == 1
    assert plan.requires_session is False, "caller must skip session creation and place one order"


def test_single_tranche_when_amount_equals_the_ceiling() -> None:
    plan = plan_tranches(SMALL, SMALL)

    assert plan.amounts_paise == (SMALL,)
    assert plan.requires_session is False


def test_exact_multiple_has_no_remainder_tranche() -> None:
    plan = plan_tranches(SMALL * 5, SMALL)

    assert plan.amounts_paise == (SMALL,) * 5
    assert plan.tranche_count == 5
    assert plan.requires_session is True


def test_remainder_becomes_a_shorter_final_tranche() -> None:
    plan = plan_tranches(SMALL * 2 + 500, SMALL)

    assert plan.amounts_paise == (SMALL, SMALL, 500)
    assert plan.tranche_count == 3


def test_two_full_tranches_and_a_one_paise_remainder() -> None:
    assert plan_tranches(SMALL * 2 + 1, SMALL).amounts_paise == (SMALL, SMALL, 1)


def test_every_tranche_except_the_last_is_exactly_the_ceiling() -> None:
    plan = plan_tranches(10_500, SMALL)

    assert len(plan.amounts_paise) == 11
    assert all(amount == SMALL for amount in plan.amounts_paise[:-1])
    assert plan.amounts_paise[-1] == 500


def test_tranches_always_sum_back_to_the_amount() -> None:
    for amount in (1, SMALL - 1, SMALL, SMALL + 1, 3 * SMALL, 7 * SMALL + 13):
        assert plan_tranches(amount, SMALL).total_paise == amount


def test_default_ceiling_is_used_when_omitted() -> None:
    plan = plan_tranches(500_000)

    assert plan.tranche_paise == DEFAULT_TRANCHE_PAISE
    assert plan.amounts_paise == (DEFAULT_TRANCHE_PAISE, DEFAULT_TRANCHE_PAISE, 100_200)


class TestEstimateTrancheCount:
    """The pre-session estimate: pure arithmetic, no client, no I/O."""

    def test_amount_below_the_ceiling_is_one_tranche(self) -> None:
        assert estimate_tranche_count(600, SMALL) == 1

    def test_amount_equal_to_the_ceiling_is_one_tranche(self) -> None:
        assert estimate_tranche_count(SMALL, SMALL) == 1

    @pytest.mark.parametrize(
        ("amount_paise", "expected"),
        [
            (SMALL * 2, 2),
            (SMALL * 5, 5),
            (SMALL * 2 + 500, 3),
            (SMALL * 2 + 1, 3),
        ],
    )
    def test_exact_multiples_and_remainders(self, amount_paise: int, expected: int) -> None:
        assert estimate_tranche_count(amount_paise, SMALL) == expected

    def test_default_ceiling_is_used_when_omitted(self) -> None:
        assert estimate_tranche_count(500_000) == 3
        assert estimate_tranche_count(DEFAULT_TRANCHE_PAISE * 2) == 2

    @pytest.mark.parametrize("amount_paise", [0, -1, -SMALL])
    def test_non_positive_amounts_raise(self, amount_paise: int) -> None:
        with pytest.raises(ValueError, match="amount_paise must be > 0"):
            estimate_tranche_count(amount_paise, SMALL)

    @pytest.mark.parametrize("tranche_paise", [0, -1])
    def test_non_positive_ceilings_raise(self, tranche_paise: int) -> None:
        with pytest.raises(ValueError, match="tranche_paise must be > 0"):
            estimate_tranche_count(SMALL, tranche_paise)

    @pytest.mark.parametrize("bad", [1_000.5, "1000"])
    def test_floats_and_strings_are_rejected_outright(self, bad: object) -> None:
        with pytest.raises(TypeError, match="must be an int number of paise"):
            estimate_tranche_count(bad, SMALL)  # type: ignore[arg-type]

    @pytest.mark.parametrize("amount_paise", [1, 999, SMALL, SMALL + 1, 10_500, 500_000])
    def test_agrees_with_the_engine_and_with_ceil(self, amount_paise: int) -> None:
        expected = plan_tranches(amount_paise, SMALL).tranche_count

        assert estimate_tranche_count(amount_paise, SMALL) == expected
        assert estimate_tranche_count(amount_paise, SMALL) == math.ceil(amount_paise / SMALL)


def test_plan_is_frozen_and_serializable() -> None:
    plan = plan_tranches(1_500, SMALL)
    assert SplitPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("amount_paise", [0, -1, -SMALL])
def test_zero_or_negative_amounts_raise(amount_paise: int) -> None:
    with pytest.raises(ValueError, match="amount_paise must be > 0"):
        plan_tranches(amount_paise, SMALL)


@pytest.mark.parametrize("tranche_paise", [0, -1])
def test_zero_or_negative_ceilings_raise(tranche_paise: int) -> None:
    with pytest.raises(ValueError, match="tranche_paise must be > 0"):
        plan_tranches(SMALL, tranche_paise)


@pytest.mark.parametrize("bad", [1_000.5, "1000"])
def test_floats_and_strings_are_rejected_outright(bad: object) -> None:
    with pytest.raises(TypeError, match="must be an int number of paise"):
        plan_tranches(bad, SMALL)  # type: ignore[arg-type]


def test_the_paise_threshold_is_hardcoded_exactly_once() -> None:
    """The ₹1,999 ceiling must live only in ``SplitConfig``'s default."""
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "tranchepay"
    hits = {
        path.name: stripped.count("1999")
        for path in sorted(src.glob("*.py"))
        if (stripped := path.read_text().replace("_", "")).count("1999")
    }

    assert hits == {"models.py": 1}
    assert SplitConfig.model_fields["tranche_paise"].default == 199_900
