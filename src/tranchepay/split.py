"""Pure split-tranche planning: no I/O, no client, no store, no clock.

The whole split policy is ``divmod(amount_paise, tranche_paise)``: ``n`` full
tranches plus an optional remainder tranche. Everything else in the split flow
(the session, the orders, the state machine) consumes a :class:`SplitPlan`; this
module stays testable without any doubles.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .models import DEFAULT_TRANCHE_PAISE, SplitSession, Tranche
from .money import require_paise

__all__ = ["SplitPlan", "build_session", "plan_tranches"]


class SplitPlan(BaseModel):
    """Pure output of :func:`plan_tranches`.

    Attributes:
        amount_paise: Total amount that was planned, in paise.
        tranche_paise: Ceiling applied to each tranche, in paise.
        amounts_paise: Tranche amounts in payment order. Every entry except the
            last is exactly ``tranche_paise``; the last is the remainder and is
            present only when the remainder is non-zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_paise: int
    tranche_paise: int
    amounts_paise: tuple[int, ...]

    @property
    def tranche_count(self) -> int:
        """Number of tranches in the plan."""
        return len(self.amounts_paise)

    @property
    def requires_session(self) -> bool:
        """Whether a split session should be created.

        ``False`` when the plan is a single tranche, i.e. ``amount_paise`` is at
        or below the tranche ceiling. The caller must then skip session creation
        and place one ordinary order for the full amount.
        """
        return len(self.amounts_paise) > 1

    @property
    def total_paise(self) -> int:
        """Sum of the planned tranche amounts; equals ``amount_paise``."""
        return sum(self.amounts_paise)


def plan_tranches(amount_paise: int, tranche_paise: int = DEFAULT_TRANCHE_PAISE) -> SplitPlan:
    """Split ``amount_paise`` into tranches of at most ``tranche_paise``.

    Args:
        amount_paise: Total amount to collect, in paise.
        tranche_paise: Maximum size of one tranche, in paise.

    Returns:
        A :class:`SplitPlan` whose ``amounts_paise`` are the full tranches
        followed by an optional remainder tranche (omitted when the remainder is
        zero). An ``amount_paise`` below ``tranche_paise`` yields a single
        tranche and ``plan.requires_session is False``.

    Raises:
        TypeError: If either argument is not an ``int``, so that a float can
            never leak into money arithmetic.
        ValueError: If either argument is zero or negative.
    """
    require_paise(amount_paise, "amount_paise")
    require_paise(tranche_paise, "tranche_paise")

    full_tranches, remainder = divmod(amount_paise, tranche_paise)
    amounts = [tranche_paise] * full_tranches
    if remainder:
        amounts.append(remainder)

    return SplitPlan(
        amount_paise=amount_paise,
        tranche_paise=tranche_paise,
        amounts_paise=tuple(amounts),
    )


def build_session(plan: SplitPlan, *, currency: str) -> SplitSession:
    """Build a pending session for ``plan``.

    Args:
        plan: Tranche plan to materialise.
        currency: ISO-4217 currency code for the session.

    Returns:
        A session whose tranches are all pending and carry no orders yet.
    """
    return SplitSession(
        amount_paise=plan.amount_paise,
        tranche_paise=plan.tranche_paise,
        currency=currency,
        tranches=[
            Tranche(index=index, amount_paise=amount)
            for index, amount in enumerate(plan.amounts_paise)
        ],
    )
