"""Webhook verification, delegated to the client's own utility resource."""

from __future__ import annotations

from .exceptions import VerificationError
from .protocol import RazorpayClientProtocol

__all__ = ["verify_webhook"]


def verify_webhook(
    client: RazorpayClientProtocol,
    body: str,
    signature: str,
    secret: str,
) -> bool:
    """Verify a Razorpay webhook payload against ``secret``.

    The signature itself is checked by ``client.utility.verify_webhook_signature``
    (i.e. by Razorpay's own HMAC implementation); this helper only normalises the
    outcome into this library's exception hierarchy.

    Args:
        client: The configured Razorpay client to delegate verification to.
        body: Raw request body, exactly as received, as ``str`` or ``bytes``.
        signature: Value of the ``X-Razorpay-Signature`` header.
        secret: Webhook secret configured in the Razorpay dashboard. This is the
            webhook secret, which is *not* the same as the API key secret.

    Returns:
        ``True`` when the signature is valid.

    Raises:
        VerificationError: If the signature is invalid, or the client raised
            while verifying. The original exception is chained as ``__cause__``.
    """
    try:
        result = client.utility.verify_webhook_signature(body, signature, secret)
    except Exception as exc:
        msg = "webhook signature verification failed"
        raise VerificationError(msg) from exc
    if not result:
        msg = "webhook signature verification failed"
        raise VerificationError(msg)
    return True
