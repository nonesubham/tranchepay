"""The exception hierarchy is part of the public API; pin it down."""

from __future__ import annotations

import pytest

from tranchepay import exceptions


@pytest.mark.parametrize(
    "name",
    [
        "VerificationError",
        "AmountMismatchError",
        "SessionNotFoundError",
        "SessionStateError",
        "PartialPaymentError",
    ],
)
def test_every_error_is_a_payment_compose_error(name: str) -> None:
    assert issubclass(getattr(exceptions, name), exceptions.PaymentComposeError)


def test_payment_compose_error_is_an_exception() -> None:
    assert issubclass(exceptions.PaymentComposeError, Exception)


def test_amount_mismatch_is_a_verification_error() -> None:
    assert issubclass(exceptions.AmountMismatchError, exceptions.VerificationError)


def test_errors_carry_a_message() -> None:
    error = exceptions.AmountMismatchError("expected 1000 paise, got 900")
    assert "expected 1000 paise, got 900" in str(error)


def test_all_is_exported() -> None:
    assert "PaymentComposeError" in exceptions.__all__
    assert set(exceptions.__all__) == {
        "PaymentComposeError",
        "VerificationError",
        "AmountMismatchError",
        "SessionNotFoundError",
        "SessionStateError",
        "PartialPaymentError",
    }


def test_catching_the_base_class_covers_every_error() -> None:
    message = "no session here"

    with pytest.raises(exceptions.PaymentComposeError, match=message):
        raise exceptions.SessionNotFoundError(message)
