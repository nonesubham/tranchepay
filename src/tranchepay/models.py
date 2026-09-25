"""Pydantic v2 models: configuration, split sessions, and API responses.

Monetary fields are integer **paise**. Fee rates are :class:`~decimal.Decimal`
validated by :mod:`tranchepay.money`, never binary floating point.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .enums import PaymentMode, RoundingPolicy, SessionStatus, TrancheStatus
from .money import validate_fee_rate

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


class ChargesConfig(BaseModel):
    """Configuration for :attr:`PaymentMode.WITH_CHARGES`.

    Attributes:
        fee_rate: Gateway fee as a fraction of the *gross* amount, e.g.
            ``Decimal("0.0236")`` for 2.36%. Must satisfy ``0 <= fee_rate < 1``.
        rounding: Policy applied to ``net / (1 - fee_rate)``. Explicit by design;
            nothing in this library rounds implicitly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fee_rate: Decimal
    rounding: RoundingPolicy = RoundingPolicy.ROUND_HALF_UP

    @field_validator("fee_rate")
    @classmethod
    def _validate_fee_rate(cls, value: Decimal) -> Decimal:
        return validate_fee_rate(value)


class SplitConfig(BaseModel):
    """Configuration for :attr:`PaymentMode.SPLIT`.

    Attributes:
        tranche_paise: Maximum size of a single tranche in paise. The default
            exists because NPCI's MDR rule applies to merchant UPI payments above
            ₹2,000; see the COMPLIANCE section of the README. This default is the
            only place in the codebase where that threshold is written down.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tranche_paise: int = 199_900

    @field_validator("tranche_paise")
    @classmethod
    def _validate_tranche_paise(cls, value: int) -> int:
        if value <= 0:
            msg = f"tranche_paise must be > 0, got {value!r}"
            raise ValueError(msg)
        return value


DEFAULT_TRANCHE_PAISE: int = SplitConfig.model_fields["tranche_paise"].default
"""Default tranche ceiling in paise (₹1,999), derived from :class:`SplitConfig`."""


class Tranche(BaseModel):
    """One installment of a split payment.

    Attributes:
        index: Zero-based position within the session.
        amount_paise: Amount to charge for this tranche, in paise.
        order_id: Razorpay order created for this tranche, if any.
        payment_id: Razorpay payment that settled this tranche, if any.
        status: Lifecycle state of the tranche.
    """

    model_config = ConfigDict(validate_assignment=True)

    index: int = Field(ge=0)
    amount_paise: int = Field(gt=0)
    order_id: str | None = None
    payment_id: str | None = None
    status: TrancheStatus = TrancheStatus.PENDING


class SplitSession(BaseModel):
    """A resumable, serializable split-payment session.

    This model is the unit of persistence for ``SessionStore``. It round-trips
    through ``model_dump_json`` / ``model_validate_json`` so a developer can
    persist it in their own database instead of using a shipped store. Assigning
    to a field revalidates it, so ``tranche.status = "paid"`` is coerced rather
    than silently stored as a string.
    """

    model_config = ConfigDict(validate_assignment=True)

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
        if not self.tranches:
            msg = "a split session needs at least one tranche"
            raise ValueError(msg)
        indices = [tranche.index for tranche in self.tranches]
        if indices != list(range(len(self.tranches))):
            msg = f"tranche indices must be 0..{len(self.tranches) - 1}, got {indices}"
            raise ValueError(msg)
        if sum(tranche.amount_paise for tranche in self.tranches) != self.amount_paise:
            msg = "tranche amounts must add up to amount_paise"
            raise ValueError(msg)
        return self

    def touched(self) -> SplitSession:
        """Return a copy of this session stamped with the current UTC time."""
        return self.model_copy(update={"updated_at": datetime.now(timezone.utc)})

    def tranche_for_order(self, order_id: str) -> Tranche | None:
        """Return the tranche created as ``order_id``, if any."""
        return next((t for t in self.tranches if t.order_id == order_id), None)

    def next_pending_tranche(self) -> Tranche | None:
        """Return the first tranche that still needs to be collected."""
        return next((t for t in self.tranches if t.status is TrancheStatus.PENDING), None)

    def paid_tranches(self) -> list[Tranche]:
        """Return every tranche currently captured and not yet refunded."""
        return [t for t in self.tranches if t.status is TrancheStatus.PAID]

    def is_fully_paid(self) -> bool:
        """Whether every tranche has been captured."""
        return all(tranche.status is TrancheStatus.PAID for tranche in self.tranches)

    def total_paid_paise(self) -> int:
        """Return the sum of all tranches currently captured."""
        return sum(t.amount_paise for t in self.paid_tranches())

    def is_terminal(self) -> bool:
        """Whether the session can no longer change state."""
        return self.status in {SessionStatus.COMPLETE, SessionStatus.ABORTED}


class OrderResult(BaseModel):
    """A created Razorpay order plus the tranchepay context around it.

    Hand ``raw`` to Razorpay Checkout, or read ``order_id`` if your front end
    builds its own payload.

    Attributes:
        order_id: Razorpay order id.
        amount_paise: Amount *sent to Razorpay*: the grossed-up amount for
            ``WITH_CHARGES``, the tranche amount for ``SPLIT``.
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
