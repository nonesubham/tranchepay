"""The webhook helper delegates to the client and normalises the outcome."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from tests.fakes import FakeRazorpayClient, FakeSignatureError
from tranchepay import PaymentComposer, VerificationError, verify_webhook

BODY = '{"event":"payment.captured"}'
SECRET = "whsec_test"


def sign(body: str, secret: str) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_valid_signature_returns_true(fake_client: FakeRazorpayClient) -> None:
    assert verify_webhook(fake_client, BODY, sign(BODY, SECRET), SECRET) is True


def test_helper_works_with_the_clients_a_composer_holds(composer: PaymentComposer) -> None:
    """The helper takes the client directly, so it works next to a composer."""
    assert verify_webhook(composer.client, BODY, sign(BODY, SECRET), SECRET) is True


def test_invalid_signature_raises_verification_error(fake_client: FakeRazorpayClient) -> None:
    with pytest.raises(VerificationError, match="webhook signature verification failed"):
        verify_webhook(fake_client, BODY, "not-a-signature", SECRET)


def test_wrong_secret_raises_verification_error(fake_client: FakeRazorpayClient) -> None:
    with pytest.raises(VerificationError):
        verify_webhook(fake_client, BODY, sign(BODY, "other_secret"), SECRET)


def test_foreign_exception_is_chained(fake_client: FakeRazorpayClient) -> None:
    with pytest.raises(VerificationError) as excinfo:
        verify_webhook(fake_client, BODY, sign(BODY, "other_secret"), SECRET)

    assert isinstance(excinfo.value.__cause__, FakeSignatureError)


def test_a_falsey_result_is_treated_as_failure(fake_client: FakeRazorpayClient) -> None:
    """Some client versions return False instead of raising; both are failures."""
    fake_client.utility.verify_webhook_signature = lambda body, signature, secret: False  # type: ignore[method-assign]

    with pytest.raises(VerificationError, match="webhook signature verification failed"):
        verify_webhook(fake_client, BODY, "whatever", SECRET)
