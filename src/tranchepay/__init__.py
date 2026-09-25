"""tranchepay — payment composition around a Razorpay client you already own.

tranchepay never subclasses, monkey-patches, or forks ``razorpay.Client``: you
construct and configure the official client yourself and hand it to
:class:`~tranchepay.PaymentComposer`. Everything tranchepay does is expressed
through that client's public resources (``order``, ``payment``, ``utility``).

Three modes are supported:

* ``PaymentMode.EXACT`` — charge exactly ``amount_paise``.
* ``PaymentMode.WITH_CHARGES`` — gross up so the merchant nets ``amount_paise``.
* ``PaymentMode.SPLIT`` — collect one large amount as sequential tranches of at
  most ``SplitConfig.tranche_paise``.
"""

from __future__ import annotations

from .exceptions import (
    AmountMismatchError,
    PartialPaymentError,
    PaymentComposeError,
    SessionNotFoundError,
    SessionStateError,
    VerificationError,
)
from .models import (
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
from .split import SplitPlan, plan_tranches

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_TRANCHE_PAISE",
    "AmountMismatchError",
    "ChargesConfig",
    "OrderResult",
    "PartialPaymentError",
    "PaymentComposeError",
    "PaymentMode",
    "RoundingPolicy",
    "SessionNotFoundError",
    "SessionStateError",
    "SessionStatus",
    "SplitConfig",
    "SplitPlan",
    "SplitSession",
    "Tranche",
    "TrancheStatus",
    "VerificationError",
    "__version__",
    "plan_tranches",
]
