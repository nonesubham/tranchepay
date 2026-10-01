"""tranchepay — payment composition around a gateway you already own.

tranchepay never subclasses, monkey-patches, or forks a provider SDK: the
developer builds and configures the client themselves (or lets
:meth:`GatewayFactory.get_gateway` do it) and hands the resulting
:class:`PaymentGateway` adapter to :class:`~tranchepay.PaymentComposer`.
Razorpay, Paytm and PhonePe each ship an adapter, and a custom one can be
registered with :meth:`GatewayFactory.register_gateway`.

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
    GatewayConfigurationError,
    PartialPaymentError,
    PaymentComposeError,
    SessionNotFoundError,
    SessionStateError,
    UnsupportedGatewayError,
    VerificationError,
)
from .factory import GatewayFactory
from .gateways import PaytmAdapter, PhonePeAdapter, RazorpayAdapter
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
from .protocol import PaymentGateway
from .split import SplitPlan, estimate_tranche_count, plan_tranches
from .store import InMemorySessionStore, SessionStore
from .webhooks import verify_webhook

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_TRANCHE_PAISE",
    "AmountMismatchError",
    "ChargesConfig",
    "GatewayConfigurationError",
    "GatewayFactory",
    "InMemorySessionStore",
    "OrderResult",
    "PartialPaymentError",
    "PaymentComposeError",
    "PaymentComposer",
    "PaymentGateway",
    "PaymentMode",
    "PaytmAdapter",
    "PhonePeAdapter",
    "RazorpayAdapter",
    "RoundingPolicy",
    "SessionNotFoundError",
    "SessionStateError",
    "SessionStatus",
    "SessionStore",
    "SplitConfig",
    "SplitPlan",
    "SplitSession",
    "Tranche",
    "TrancheStatus",
    "UnsupportedGatewayError",
    "VerificationError",
    "__version__",
    "estimate_tranche_count",
    "gross_up",
    "plan_tranches",
    "require_paise",
    "validate_fee_rate",
    "verify_webhook",
]
