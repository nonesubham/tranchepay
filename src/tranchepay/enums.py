"""Closed vocabularies shared by configuration, sessions, and responses."""

from __future__ import annotations

from enum import Enum

__all__ = ["PaymentMode", "RoundingPolicy", "SessionStatus", "TrancheStatus"]


class PaymentMode(str, Enum):
    """How the amount charged to the customer is derived."""

    EXACT = "exact"
    """Charge exactly ``amount_paise``; the merchant absorbs gateway charges."""

    WITH_CHARGES = "with_charges"
    """Gross the amount up so the merchant nets ``amount_paise`` after charges."""

    SPLIT = "split"
    """Split one large payment into sequential tranches of at most ``tranche_paise``."""


class RoundingPolicy(str, Enum):
    """Explicit rounding policy for gross-up arithmetic.

    Values match :mod:`decimal`'s rounding constants, so a policy can also be
    passed straight to ``Decimal.quantize``. The default is
    :attr:`ROUND_HALF_UP`. Because all amounts are non-negative,
    :attr:`ROUND_DOWN` equals :attr:`ROUND_FLOOR` and :attr:`ROUND_UP` equals
    :attr:`ROUND_CEILING`; both names are kept for readability.
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
    """Created; no tranche has been paid yet."""

    IN_PROGRESS = "in_progress"
    """At least one tranche paid; more tranches remain to be collected."""

    COMPLETE = "complete"
    """Every tranche paid. Terminal: the session must never change again."""

    ABORTED = "aborted"
    """Terminal: paid tranches were refunded (or no tranche was ever paid)."""
