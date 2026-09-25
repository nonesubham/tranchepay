"""Signature and payment checks, delegated to the client and normalised.

Razorpay owns signature verification; tranchepay never re-implements the HMAC.
These helpers call the client's own verifier and translate whatever it does
(raise, or return falsey) into this library's exception hierarchy, chaining the
original exception so nothing is lost.
"""

from __future__ import annotations

from .exceptions import AmountMismatchError, VerificationError
from .models import Tranche
from .protocol import RazorpayClientProtocol

__all__ = ["CAPTURED", "require_captured_payment", "verify_checkout_signature"]

CAPTURED = "captured"


def verify_checkout_signature(
    client: RazorpayClientProtocol,
    order_id: str,
    payment_id: str,
    signature: str,
) -> None:
    """Verify a checkout signature with the client's own utility resource.

    Args:
        client: Configured Razorpay client.
        order_id: ``razorpay_order_id`` from checkout.
        payment_id: ``razorpay_payment_id`` from checkout.
        signature: ``razorpay_signature`` from checkout.

    Raises:
        VerificationError: If the client rejects the signature or raises while
            verifying it; the original exception is chained as ``__cause__``.
    """
    failure = f"signature verification failed for order {order_id} / payment {payment_id}"
    parameters = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    }
    try:
        verified = client.utility.verify_payment_signature(parameters)
    except Exception as exc:
        raise VerificationError(failure) from exc
    if not verified:
        raise VerificationError(failure)


def require_captured_payment(
    client: RazorpayClientProtocol,
    payment_id: str,
    tranche: Tranche,
) -> None:
    """Assert that ``payment_id`` is captured for exactly the tranche amount.

    Args:
        client: Configured Razorpay client.
        payment_id: Payment to inspect.
        tranche: Tranche the payment is expected to settle.

    Raises:
        VerificationError: If the payment cannot be fetched, or is not captured.
        AmountMismatchError: If the captured amount is not the tranche amount.
    """
    try:
        payment = client.payment.fetch(payment_id)
    except Exception as exc:
        msg = f"could not fetch payment {payment_id} for tranche {tranche.index}"
        raise VerificationError(msg) from exc

    status = payment.get("status")
    if status != CAPTURED:
        msg = (
            f"payment {payment_id} for tranche {tranche.index} is {status!r}, expected {CAPTURED!r}"
        )
        raise VerificationError(msg)

    amount = payment.get("amount")
    if amount != tranche.amount_paise:
        msg = (
            f"payment {payment_id} captured {amount!r} paise but tranche "
            f"{tranche.index} expects {tranche.amount_paise} paise"
        )
        raise AmountMismatchError(msg)
