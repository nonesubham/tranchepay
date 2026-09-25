"""Exception hierarchy raised by :mod:`tranchepay`.

Every error derives from :class:`PaymentComposeError`, so callers can wrap a
whole checkout handler in a single ``except PaymentComposeError`` if they do not
care about the specific failure mode.

:class:`AmountMismatchError` is the one nesting: it derives from
:class:`VerificationError` because a payment whose amount does not match the
tranche is, from the session's point of view, simply a payment that failed
verification. Code that treats "this payment did not verify" as one branch can
catch :class:`VerificationError` and get both cases.
"""

from __future__ import annotations

__all__ = [
    "AmountMismatchError",
    "PartialPaymentError",
    "PaymentComposeError",
    "SessionNotFoundError",
    "SessionStateError",
    "VerificationError",
]


class PaymentComposeError(Exception):
    """Base class for every exception raised by tranchepay."""


class VerificationError(PaymentComposeError):
    """A signature check or a payment-state check did not pass.

    Raised when ``client.utility.verify_payment_signature`` /
    ``verify_webhook_signature`` rejects a payload, or when a fetched payment is
    not in the ``captured`` state and therefore cannot settle a tranche.
    """


class AmountMismatchError(VerificationError):
    """A captured payment did not match the amount expected for its tranche.

    Raised by ``verify_and_advance`` when the amount reported by
    ``client.payment.fetch`` differs from the ``amount_paise`` recorded for the
    tranche, which would leave the session under- or over-funded.
    """


class SessionNotFoundError(PaymentComposeError):
    """The session id is unknown to the configured ``SessionStore``."""


class SessionStateError(PaymentComposeError):
    """The requested operation is not legal for the current session state.

    Examples: advancing an aborted session, verifying an order that does not
    belong to the session, or resuming a completed session.
    """


class PartialPaymentError(PaymentComposeError):
    """A split session could not complete or unwind cleanly.

    Raised when a session is left in a partially paid or partially refunded
    state, for example because a refund call failed midway through
    ``abort_and_refund``. Already-applied side effects are persisted, so a retry
    of the same call only processes the remaining tranches.
    """
