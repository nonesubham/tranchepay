"""The webhook helper delegates to the gateway and normalises the outcome."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from tests.fakes import FakeRazorpayClient, FakeSignatureError
from tranchepay import PaymentComposer, RazorpayAdapter, VerificationError, verify_webhook

BODY = '{"event":"payment.captured"}'
SECRET = "whsec_test"


def sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_valid_signature_returns_true(fake_gateway: RazorpayAdapter) -> None:
    assert verify_webhook(fake_gateway, BODY, sign(BODY, SECRET), SECRET) is True


def test_helper_works_with_the_gateway_a_composer_holds(composer: PaymentComposer) -> None:
    """The helper takes the gateway directly, so it works next to a composer."""
    assert verify_webhook(composer.gateway, BODY, sign(BODY, SECRET), SECRET) is True


def test_invalid_signature_raises_verification_error(fake_gateway: RazorpayAdapter) -> None:
    with pytest.raises(VerificationError, match="webhook signature verification failed"):
        verify_webhook(fake_gateway, BODY, "not-a-signature", SECRET)


def test_wrong_secret_raises_verification_error(fake_gateway: RazorpayAdapter) -> None:
    with pytest.raises(VerificationError):
        verify_webhook(fake_gateway, BODY, sign(BODY, "other_secret"), SECRET)


def test_foreign_exception_is_chained(fake_gateway: RazorpayAdapter) -> None:
    with pytest.raises(VerificationError) as excinfo:
        verify_webhook(fake_gateway, BODY, sign(BODY, "other_secret"), SECRET)

    assert isinstance(excinfo.value.__cause__, FakeSignatureError)


def test_a_falsey_result_is_treated_as_failure(
    fake_gateway: RazorpayAdapter, fake_client: FakeRazorpayClient
) -> None:
    """Some client versions return False instead of raising; both are failures."""
    fake_client.utility.verify_webhook_signature = lambda body, signature, secret: False  # type: ignore[method-assign]

    with pytest.raises(VerificationError, match="webhook signature verification failed"):
        verify_webhook(fake_gateway, BODY, "whatever", SECRET)
