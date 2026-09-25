"""tranchepay — payment composition around a Razorpay client you already own.

tranchepay never subclasses, monkey-patches, or forks ``razorpay.Client``: you
build and configure the official client yourself and hand it to
:class:`~tranchepay.PaymentComposer`. Everything tranchepay does is expressed
through that client's public resources (``order``, ``payment``, ``utility``).

Three modes are supported:

* ``PaymentMode.EXACT`` — charge exactly ``amount_paise``.
* ``PaymentMode.WITH_CHARGES`` — gross up so the merchant nets ``amount_paise``.
* ``PaymentMode.SPLIT`` — collect one large amount as sequential tranches of at
  most ``SplitConfig.tranche_paise``.

All amounts are integer paise and fee rates are :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from .composer import PaymentComposer
from .enums import PaymentMode, RoundingPolicy, SessionStatus, TrancheStatus
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
    SplitConfig,
    SplitSession,
    Tranche,
)
from .money import gross_up, require_paise, validate_fee_rate
from .split import SplitPlan, plan_tranches
from .webhooks import verify_webhook

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_TRANCHE_PAISE",
    "AmountMismatchError",
    "ChargesConfig",
    "OrderResult",
    "PartialPaymentError",
    "PaymentComposeError",
    "PaymentComposer",
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
    "gross_up",
    "plan_tranches",
    "require_paise",
    "validate_fee_rate",
    "verify_webhook",
]
