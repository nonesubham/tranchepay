"""The split state machine: sequential tranches, resumable and idempotent.

Transitions are keyed on ``(session_id, tranche_index)``. Each one runs under the
session's process-global lock (see :mod:`tranchepay.session_locks`) and re-reads
the session inside it, so a double-submitted verification or two concurrent
callbacks advance a session at most once: the replay returns the same next order
instead of creating another.

The flow owns no state beyond its collaborators - the session lives in the store,
so a crashed process can pick the session up again with
:meth:`SplitFlow.resume`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .enums import PaymentMode, SessionStatus, TrancheStatus
from .exceptions import SessionStateError
from .models import DEFAULT_CURRENCY, OrderResult, SplitConfig, SplitSession
from .money import require_paise
from .orders import (
    create_razorpay_order,
    create_tranche_order,
    ensure_tranche_order,
    order_result,
)
from .protocol import RazorpayClientProtocol
from .session_locks import hold
from .split import build_session, plan_tranches
from .split_recovery import SplitRecovery
from .store import SessionStore, load_session
from .verification import require_captured_payment, verify_checkout_signature

__all__ = ["SplitFlow"]


class SplitFlow:
    """Drives one split session at a time through its tranches.

    Args:
        client: Configured Razorpay client (used as-is, never mutated).
        store: Where sessions are persisted between calls.
        config: Tranche ceiling; defaults to the ₹1,999 :class:`SplitConfig`.
        currency: Fallback ISO-4217 code for orders created by this flow.
    """

    def __init__(
        self,
        client: RazorpayClientProtocol,
        store: SessionStore,
        *,
        config: SplitConfig | None = None,
        currency: str = DEFAULT_CURRENCY,
    ) -> None:
        self._client = client
        self._store = store
        self._config = config if config is not None else SplitConfig()
        self._currency = currency
        self._recovery_helper: SplitRecovery | None = None

    def start(
        self,
        amount_paise: int,
        *,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
        currency: str | None = None,
    ) -> OrderResult:
        """Open a split session (if needed) and create the first tranche order.

        When ``amount_paise`` fits in a single tranche, no session is created at
        all: one ordinary order is returned for the whole amount, because there is
        nothing to sequence.

        Args:
            amount_paise: Total amount to collect across tranches, in paise.
            receipt: Optional receipt for the order created by this call.
            notes: Optional notes for the order created by this call.
            currency: Overrides the flow's currency for this session.

        Returns:
            The first tranche order, carrying ``session_id`` and
            ``tranche_index``. Later tranche orders carry neither receipt nor
            notes; pass them on the order created here.

        Raises:
            TypeError: If ``amount_paise`` is not an ``int``.
            ValueError: If ``amount_paise`` is not positive.
            PaymentComposeError: If Razorpay returns no usable order id.
        """
        require_paise(amount_paise, "amount_paise")
        order_currency = currency or self._currency
        plan = plan_tranches(amount_paise, self._config.tranche_paise)

        if not plan.requires_session:
            raw = create_razorpay_order(
                self._client, amount_paise, currency=order_currency, receipt=receipt, notes=notes
            )
            return order_result(
                raw,
                mode=PaymentMode.SPLIT,
                amount_paise=amount_paise,
                currency_default=order_currency,
                receipt=receipt,
            )

        session = build_session(plan, currency=order_currency)
        raw = create_razorpay_order(
            self._client,
            plan.amounts_paise[0],
            currency=order_currency,
            receipt=receipt,
            notes=notes,
        )
        first = order_result(
            raw,
            mode=PaymentMode.SPLIT,
            amount_paise=plan.amounts_paise[0],
            currency_default=order_currency,
            receipt=receipt,
            session_id=session.session_id,
            tranche_index=0,
        )
        session.tranches[0].order_id = first.order_id
        self._store.save(session)
        return first

    def verify_and_advance(
        self,
        session_id: str,
        order_id: str,
        payment_id: str,
        signature: str,
    ) -> OrderResult | None:
        """Verify a tranche payment and, if more remain, create the next order.

        Steps, in order: verify the checkout signature with
        ``client.utility.verify_payment_signature``; load the session; reject the
        call if the order does not belong to the session or the tranche is not the
        current pending one; fetch the payment and require ``status ==
        "captured"`` with ``amount`` equal to the tranche amount; mark the tranche
        paid; then either close the session or create the next order.

        Safe to call twice: a replay of an already-paid tranche verifies the
        signature again, skips the state change, and returns the current pending
        tranche's order (or ``None`` when the session is complete), reconstructed
        from the session with no extra API call and no new order.

        Args:
            session_id: Session returned by the first tranche order.
            order_id: Order the customer paid (``razorpay_order_id``).
            payment_id: Payment id from checkout (``razorpay_payment_id``).
            signature: Signature from checkout (``razorpay_signature``).

        Returns:
            The next tranche order for checkout, or ``None`` when this payment
            completed the session.

        Raises:
            VerificationError: If the signature is invalid or the payment is not
                captured.
            AmountMismatchError: If the captured amount is not the tranche amount.
            SessionNotFoundError: If the session id is unknown to the store.
            SessionStateError: If the order belongs to another session, the
                tranche is not the current pending one, or the session is closed.
        """
        verify_checkout_signature(self._client, order_id, payment_id, signature)

        with hold(session_id):
            session = load_session(self._store, session_id)
            tranche = session.tranche_for_order(order_id)
            if tranche is None:
                msg = f"order {order_id} does not belong to session {session_id}"
                raise SessionStateError(msg)

            if tranche.status is TrancheStatus.PAID:
                return self._pending_order(session)

            if session.is_terminal():
                msg = (
                    f"session {session_id} is {session.status.value}; "
                    f"tranche {tranche.index} cannot be advanced"
                )
                raise SessionStateError(msg)

            pending = session.next_pending_tranche()
            if pending is None or pending.index != tranche.index:
                current = pending.index if pending is not None else "none"
                msg = (
                    f"tranche {tranche.index} of session {session_id} is "
                    f"{tranche.status.value}; only tranche {current} can be advanced"
                )
                raise SessionStateError(msg)

            require_captured_payment(self._client, payment_id, tranche)
            tranche.status = TrancheStatus.PAID
            tranche.payment_id = payment_id
            session.status = (
                SessionStatus.COMPLETE if session.is_fully_paid() else SessionStatus.IN_PROGRESS
            )
            self._store.update(session.touched())
            if session.status is SessionStatus.COMPLETE:
                return None

            next_tranche = session.next_pending_tranche()
            if next_tranche is None:  # pragma: no cover - unreachable while not fully paid
                msg = f"session {session_id} has no pending tranche to collect"
                raise SessionStateError(msg)
            return create_tranche_order(self._client, self._store, session, next_tranche.index)

    def abort_and_refund(self, session_id: str) -> SplitSession:
        """Refund every paid tranche of a session and mark the session aborted.

        Idempotent: only tranches still in the ``PAID`` state are refunded, so no
        tranche is ever refunded twice.

        Args:
            session_id: Session to abort.

        Returns:
            The aborted session as persisted.

        Raises:
            SessionNotFoundError: If the session id is unknown to the store.
            SessionStateError: If the session is already ``COMPLETE``.
            PartialPaymentError: If a refund failed; call again to finish. See
                :class:`~tranchepay.split_recovery.SplitRecovery`.
        """
        return self._recovery().abort_and_refund(session_id)

    def resume(self, session_id: str) -> OrderResult:
        """Return a payable order for the session's pending tranche.

        Fetches the pending tranche's order and replaces it when it is no longer
        payable - for example after expiry - so an interrupted session can be
        resumed without re-creating it from scratch.

        Args:
            session_id: Session to resume.

        Returns:
            The order to send to checkout.

        Raises:
            SessionNotFoundError: If the session id is unknown to the store.
            SessionStateError: If the session is closed, has no pending tranche, or
                the pending order was already paid.
        """
        return self._recovery().resume(session_id)

    def _recovery(self) -> SplitRecovery:
        """Return the refund/resume helper, building it on first use."""
        if self._recovery_helper is None:
            self._recovery_helper = SplitRecovery(self._client, self._store)
        return self._recovery_helper

    def _pending_order(self, session: SplitSession) -> OrderResult | None:
        """Return the order for the current pending tranche, if there is one."""
        pending = session.next_pending_tranche()
        if pending is None:
            return None
        return ensure_tranche_order(self._client, self._store, session, pending.index)
