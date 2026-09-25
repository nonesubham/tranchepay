"""Pydantic v2 models: configuration, split sessions, and API responses.

All monetary fields are integer **paise**. Fee rates are :class:`~decimal.Decimal`
and are never converted to binary floating point, because ``0.1 + 0.2 != 0.3``
is not an acceptable property for a payment library.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "DEFAULT_CURRENCY",
    "DEFAULT_TRANCHE_PAISE",
    "ChargesConfig",
    "OrderResult",
    "PaymentMode",
    "RoundingPolicy",
    "SessionStatus",
    "SplitConfig",
    "SplitSession",
    "Tranche",
    "TrancheStatus",
]

DEFAULT_CURRENCY = "INR"


class PaymentMode(str, Enum):
    """How the amount charged to the customer is derived."""

    EXACT = "exact"
    """Charge exactly ``amount_paise``; the merchant absorbs gateway charges."""

    WITH_CHARGES = "with_charges"
    """Gross the amount up so the merchant nets ``amount_paise`` after charges."""

    SPLIT = "split"
    """Split one large payment into sequential tranches."""


class RoundingPolicy(str, Enum):
    """Explicit rounding policy applied to gross-up arithmetic.

    The values match :mod:`decimal`'s rounding constants, so a policy can be
    passed straight to ``Decimal.quantize``. The default is
    :attr:`ROUND_HALF_UP`, which is the most common merchant expectation and
    equal to :attr:`ROUND_HALF_CEILING` for non-negative amounts.
    """

    ROUND_HALF_UP = "ROUND_HALF_UP"
    ROUND_HALF_DOWN = "ROUND_HALF_DOWN"
    ROUND_HALF_EVEN = "ROUND_HALF_EVEN"
    ROUND_UP = "ROUND_UP"
    ROUND_DOWN = "ROUND_DOWN"
    ROUND_CEILING = "ROUND_CEILING"
    ROUND_FLOOR = "ROUND_FLOOR"


class TrancheStatus(str, Enum):
    """Lifecycle of a single tranche of a split payment."""

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class SessionStatus(str, Enum):
    """Lifecycle of a split session."""

    PENDING = "pending"
    """Created, no tranche paid yet."""

    IN_PROGRESS = "in_progress"
    """At least one tranche paid, more tranches to collect."""

    COMPLETE = "complete"
    """Every tranche paid; the session is closed and must not change again."""

    ABORTED = "aborted"
    """Terminal: paid tranches were refunded (or no tranche was ever paid)."""


class ChargesConfig(BaseModel):
    """Configuration for :attr:`PaymentMode.WITH_CHARGES`.

    Attributes:
        fee_rate: Gateway rate as a fraction of the *gross* amount, e.g.
            ``Decimal("0.0236")`` for 2.36%. Must satisfy ``0 <= fee_rate < 1``.
        rounding: Policy applied to ``net / (1 - fee_rate)``. Explicit by
            design; nothing in this library rounds implicitly.
        currency: ISO-4217 currency code sent to Razorpay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fee_rate: Decimal
    rounding: RoundingPolicy = RoundingPolicy.ROUND_HALF_UP
    currency: str = DEFAULT_CURRENCY

    @field_validator("fee_rate")
    @classmethod
    def _validate_fee_rate(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            msg = f"fee_rate must be finite, got {value!r}"
            raise ValueError(msg)
        if value < 0:
            msg = f"fee_rate must be >= 0, got {value!r}"
            raise ValueError(msg)
        if value >= 1:
            msg = f"fee_rate must be < 1, got {value!r}"
            raise ValueError(msg)
        return value


class SplitConfig(BaseModel):
    """Configuration for :attr:`PaymentMode.SPLIT`.

    Attributes:
        tranche_paise: Maximum size of a single tranche in paise. The default
            exists because NPCI's MDR rule applies to merchant UPI payments
            above ₹2,000; see the COMPLIANCE section of the README. This is the
            only place in the codebase where the ₹1,999 threshold is written.
        currency: ISO-4217 currency code sent to Razorpay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tranche_paise: int = 199_900
    currency: str = DEFAULT_CURRENCY

    @field_validator("tranche_paise")
    @classmethod
    def _validate_tranche_paise(cls, value: int) -> int:
        if value <= 0:
            msg = f"tranche_paise must be > 0, got {value!r}"
            raise ValueError(msg)
        return value


DEFAULT_TRANCHE_PAISE: int = SplitConfig.model_fields["tranche_paise"].default
"""Default tranche ceiling, derived from :class:`SplitConfig` (₹1,999)."""


class Tranche(BaseModel):
    """One installment of a split payment.

    Attributes:
        index: Zero-based position within the session.
        amount_paise: Amount to charge for this tranche, in paise.
        order_id: Razorpay order created for this tranche, if any.
        payment_id: Razorpay payment that settled this tranche, if any.
        status: Lifecycle state of the tranche.
    """

    index: int = Field(ge=0)
    amount_paise: int = Field(gt=0)
    order_id: str | None = None
    payment_id: str | None = None
    status: TrancheStatus = TrancheStatus.PENDING


class SplitSession(BaseModel):
    """A resumable, serializable split-payment session.

    The model is the unit of persistence for :class:`~tranchepay.SessionStore`
    and round-trips through ``model_dump_json`` / ``model_validate_json`` so a
    developer can persist it in their own database.
    """

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    amount_paise: int = Field(gt=0)
    tranche_paise: int = Field(gt=0)
    currency: str = DEFAULT_CURRENCY
    tranches: list[Tranche]
    status: SessionStatus = SessionStatus.PENDING
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_tranches(self) -> SplitSession:
        indices = [tranche.index for tranche in self.tranches]
        if indices != list(range(len(self.tranches))):
            msg = f"tranche indices must be 0..{len(self.tranches) - 1}, got {indices}"
            raise ValueError(msg)
        if not self.tranches:
            msg = "a split session needs at least one tranche"
            raise ValueError(msg)
        if sum(tranche.amount_paise for tranche in self.tranches) != self.amount_paise:
            msg = "tranche amounts must add up to amount_paise"
            raise ValueError(msg)
        return self

    def touched(self) -> SplitSession:
        """Return a copy stamped with the current UTC time in ``updated_at``."""
        return self.model_copy(update={"updated_at": datetime.now(timezone.utc)})

    def tranche_for_order(self, order_id: str) -> Tranche | None:
        """Return the tranche that was created as ``order_id``, if any."""
        return next((t for t in self.tranches if t.order_id == order_id), None)

    def next_pending_tranche(self) -> Tranche | None:
        """Return the first tranche that still needs to be collected."""
        return next((t for t in self.tranches if t.status is TrancheStatus.PENDING), None)

    def paid_tranches(self) -> list[Tranche]:
        """Return every tranche currently marked paid (and not refunded)."""
        return [t for t in self.tranches if t.status is TrancheStatus.PAID]

    def total_paid_paise(self) -> int:
        """Return the sum of all tranches currently captured."""
        return sum(t.amount_paise for t in self.paid_tranches())

    def is_terminal(self) -> bool:
        """Whether the session can no longer change state."""
        return self.status in {SessionStatus.COMPLETE, SessionStatus.ABORTED}


class OrderResult(BaseModel):
    """A created Razorpay order plus the tranchepay context around it.

    This is what callers hand to Razorpay Checkout: pass ``raw`` (the untouched
    API response) or feed ``order_id`` into your own front end.

    Attributes:
        order_id: Razorpay order id.
        amount_paise: Amount *sent to Razorpay*, i.e. the grossed-up amount for
            :attr:`PaymentMode.WITH_CHARGES` and the tranche amount for
            :attr:`PaymentMode.SPLIT`.
        currency: ISO-4217 currency code.
        mode: Mode that produced the order.
        session_id: Split session id, when the order belongs to one.
        tranche_index: Zero-based tranche position within the session.
        net_paise: Amount the merchant keeps (``WITH_CHARGES`` only).
        fee_paise: Gateway fee charged to the customer (``WITH_CHARGES`` only).
        receipt: Receipt string echoed back by Razorpay, if one was sent.
        raw: Untouched Razorpay API response.
    """

    model_config = ConfigDict(frozen=True)

    order_id: str
    amount_paise: int
    currency: str
    mode: PaymentMode
    session_id: str | None = None
    tranche_index: int | None = None
    net_paise: int | None = None
    fee_paise: int = 0
    receipt: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)
