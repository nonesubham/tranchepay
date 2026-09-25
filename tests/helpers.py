"""Test-side helper that pays tranches and verifies them.

Keeping the ids and the fake client together makes the split tests read like the
merchant flow they exercise: start a session, pay the current tranche, verify it,
repeat.
"""

from __future__ import annotations

from tests.fakes import FakeRazorpayClient
from tranchepay import OrderResult, PaymentComposer, PaymentMode, SplitSession

__all__ = ["SplitRun"]


class SplitRun:
    """Drives a split session against a :class:`FakeRazorpayClient`."""

    def __init__(self, composer: PaymentComposer, client: FakeRazorpayClient) -> None:
        self.composer = composer
        self.client = client
        self.session_id: str | None = None

    def start(self, amount_paise: int, **kwargs: object) -> OrderResult:
        """Create the first tranche order (or a single order below the ceiling)."""
        first = self.composer.create_order(amount_paise, mode=PaymentMode.SPLIT, **kwargs)  # type: ignore[arg-type]
        self.session_id = first.session_id
        return first

    def sign(
        self,
        order: OrderResult,
        payment_id: str,
        *,
        amount: int | None = None,
        status: str = "captured",
    ) -> str:
        """Register a payment for ``order`` and return a valid signature."""
        self.client.add_payment(
            payment_id,
            order.amount_paise if amount is None else amount,
            status=status,
            order_id=order.order_id,
        )
        return self.client.sign(order.order_id, payment_id)

    def verify(
        self,
        order: OrderResult,
        payment_id: str,
        *,
        amount: int | None = None,
        status: str = "captured",
        signature: str | None = None,
    ) -> OrderResult | None:
        """Verify ``order``'s payment and return the next order, if any."""
        assert self.session_id is not None
        if signature is None:
            signature = self.sign(order, payment_id, amount=amount, status=status)
        return self.composer.verify_and_advance(
            self.session_id, order.order_id, payment_id, signature
        )

    def session(self) -> SplitSession:
        """Return the persisted session."""
        assert self.session_id is not None
        session = self.composer.store.get(self.session_id)
        assert session is not None
        return session

    def expire(self, order: OrderResult) -> None:
        """Force an order into the ``expired`` state, as Razorpay eventually would."""
        self.client.set_order_status(order.order_id, "expired")
