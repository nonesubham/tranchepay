"""Exact paise arithmetic and the gross-up rule.

Two rules hold everywhere in this module and in the rest of the library:

* amounts are ``int`` **paise** — a float is rejected rather than rounded, so a
  binary floating point value can never reach an order;
* ``fee_rate`` is a :class:`~decimal.Decimal`, and the gross-up is evaluated as
  an exact rational before a single, explicitly configured rounding step.

The gross-up is ``gross = round(net / (1 - fee_rate))``. Because the *total* is
rounded, the merchant can net slightly more or less than requested, which is why
the policy is configuration rather than a hardcoded choice:

* ``ROUND_UP``/``ROUND_CEILING`` — the customer covers the fee in full; the
  merchant never under-recovers (the merchant may net up to one paisa extra).
* ``ROUND_HALF_UP`` — the closest paisa; the merchant may under-recover by less
  than half a paisa on a given order. This is the default.
* ``ROUND_DOWN``/``ROUND_FLOOR`` — the customer is never overcharged, and the
  merchant absorbs up to one paisa per order.

Whichever policy is chosen, tranchepay reports the exact gross it charged and
the fee it implies (``OrderResult.fee_paise``) instead of hiding the remainder.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from .enums import RoundingPolicy

__all__ = ["gross_up", "require_paise", "validate_fee_rate"]

_HALF_UP = RoundingPolicy.ROUND_HALF_UP
_ROUND_DOWN_NAMES = (RoundingPolicy.ROUND_DOWN.value, RoundingPolicy.ROUND_FLOOR.value)
_ROUND_UP_NAMES = (RoundingPolicy.ROUND_UP.value, RoundingPolicy.ROUND_CEILING.value)


def require_paise(value: int, name: str) -> int:
    """Return ``value`` if it is a genuine positive ``int`` number of paise.

    Args:
        value: Candidate amount.
        name: Parameter name used in the error message.

    Returns:
        The validated amount.

    Raises:
        TypeError: If ``value`` is not an ``int`` (``bool`` included), which
            keeps floats out of money arithmetic.
        ValueError: If ``value`` is zero or negative.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an int number of paise, got {type(value).__name__}"
        raise TypeError(msg)
    if value <= 0:
        msg = f"{name} must be > 0, got {value}"
        raise ValueError(msg)
    return value


def _as_exact_decimal(fee_rate: object) -> Decimal:
    """Narrow ``fee_rate`` to an exact :class:`~decimal.Decimal`.

    Args:
        fee_rate: Candidate rate, typed loosely so that untyped callers are
            checked at runtime rather than trusted.

    Returns:
        The rate as a :class:`~decimal.Decimal` with no precision loss.

    Raises:
        TypeError: If the value is a float, string, bool, or None.
    """
    if isinstance(fee_rate, bool) or not isinstance(fee_rate, (Decimal, int)):
        msg = (
            f"fee_rate must be a Decimal (or an exact int), got {type(fee_rate).__name__}; "
            "binary floats are never used for money in tranchepay"
        )
        raise TypeError(msg)
    return Decimal(fee_rate)


def validate_fee_rate(fee_rate: Decimal) -> Decimal:
    """Return ``fee_rate`` as a :class:`~decimal.Decimal`, or reject it.

    Args:
        fee_rate: Gateway fee as a fraction of the gross amount, e.g.
            ``Decimal("0.0236")`` for 2.36%.

    Returns:
        The fee rate as an exact :class:`~decimal.Decimal`.

    Raises:
        TypeError: If ``fee_rate`` is a float, string, bool, or None. Exactness
            matters more than convenience here, so floats are refused instead of
            being silently converted.
        ValueError: If the rate is not finite or is outside ``[0, 1)``.
    """
    value = _as_exact_decimal(fee_rate)
    if not value.is_finite():
        msg = f"fee_rate must be finite, got {value}"
        raise ValueError(msg)
    if value < 0:
        msg = f"fee_rate must be >= 0, got {value}"
        raise ValueError(msg)
    if value >= 1:
        msg = f"fee_rate must be < 1, got {value}"
        raise ValueError(msg)
    return value


def _round_fraction(value: Fraction, policy: RoundingPolicy) -> int:
    """Round a non-negative :class:`~fractions.Fraction` to an ``int`` exactly.

    Args:
        value: Exact value to round. Must be non-negative, which is guaranteed
            for money and lets floor division double as truncation toward zero.
        policy: Explicit rounding policy.

    Returns:
        The rounded integer.

    Raises:
        ValueError: If ``policy`` is not a known :class:`RoundingPolicy`.
    """
    quotient, remainder = divmod(value.numerator, value.denominator)
    if remainder == 0:
        return quotient
    # Distance from the exact half-way point: negative below, zero on, positive above.
    half = 2 * remainder - value.denominator
    name = RoundingPolicy(policy).value

    if name in _ROUND_DOWN_NAMES:
        bump = 0
    elif name in _ROUND_UP_NAMES:
        bump = 1
    elif name == RoundingPolicy.ROUND_HALF_UP.value:
        bump = int(half >= 0)
    elif name == RoundingPolicy.ROUND_HALF_DOWN.value:
        bump = int(half > 0)
    elif name == RoundingPolicy.ROUND_HALF_EVEN.value:
        bump = int(half > 0) if half != 0 else quotient % 2
    else:  # pragma: no cover - RoundingPolicy(policy) already rejected unknown names
        msg = f"unsupported rounding policy: {policy!r}"
        raise ValueError(msg)

    return quotient + bump


def gross_up(
    net_paise: int,
    fee_rate: Decimal,
    rounding: RoundingPolicy = _HALF_UP,
) -> int:
    """Return the gross amount to charge so ``net_paise`` survives the fee.

    Computes ``round(net_paise / (1 - fee_rate))`` under ``rounding``, evaluated
    as an exact rational so that half-way cases round the way the policy says
    rather than the way binary division happens to land.

    Args:
        net_paise: Amount the merchant wants to keep, in paise.
        fee_rate: Gateway fee as a fraction of the gross amount.
        rounding: Explicit rounding policy, applied once to the final total.

    Returns:
        The gross amount to send to Razorpay, in paise.

    Raises:
        TypeError: If ``net_paise`` is not an ``int`` or ``fee_rate`` is a float.
        ValueError: If ``net_paise`` is not positive, or ``fee_rate`` is outside
            ``[0, 1)``.

    Example:
        >>> from decimal import Decimal
        >>> gross_up(100_000, Decimal("0.0236"))
        102417
    """
    require_paise(net_paise, "net_paise")
    rate = validate_fee_rate(fee_rate)
    if rate == 0:
        return net_paise

    keep_fraction = Fraction(1) - Fraction(rate)
    return _round_fraction(Fraction(net_paise) / keep_fraction, rounding)
