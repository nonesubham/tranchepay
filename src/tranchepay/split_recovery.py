"""Unwinding and repairing a split session: refunds and resume.

These are the operations a merchant reaches for when a split payment does not go
to plan: the customer stopped paying (``resume``), or the merchant wants to give
back what was already collected (``abort_and_refund``). Both are idempotent and
both persist their progress before returning, so a failure can always be retried
without repeating a side effect.
"""

from __future__ import annotations

from .enums import SessionStatus, TrancheStatus
from .exceptions import PartialPaymentError, SessionStateError
from .models import OrderResult, SplitSession, Tranche
from .orders import create_tranche_order, ensure_tranche_order, order_is_payable
from .protocol import RazorpayClientProtocol
from .session_locks import hold
from .store import SessionStore, load_session

__all__ = ["SplitRecovery"]


class SplitRecovery:
    """Refunds and resumption for split sessions.

    Args:
        client: Configured Razorpay client (used as-is, never mutated).
        store: Store holding the sessions.
    """

    def __init__(self, client: RazorpayClientProtocol, store: SessionStore) -> None:
        self._client = client
        self._store = store

    def abort_and_refund(self, session_id: str) -> SplitSession:
        """Refund every paid tranche of a session and mark the session aborted.

        Each refund is ``client.payment.refund(payment_id, {"amount": tranche})``,
        i.e. exactly the amount captured for that tranche. Only tranches in the
        ``PAID`` state are refunded, and each is persisted as ``REFUNDED`` before
        the next one is touched, so a tranche is never refunded twice - including
        when this method is called again after a failure.

        Tranches that were never paid are left ``PENDING``; ``ABORTED`` is
        terminal, so they can no longer be collected.

        Args:
            session_id: Session to abort.

        Returns:
            The aborted session as persisted.

        Raises:
            SessionNotFoundError: If the session id is unknown to the store.
            SessionStateError: If the session is already ``COMPLETE``. A completed
                session is immutable; refund those payments directly through the
                client if that is the intent.
            PartialPaymentError: If a refund failed. Refunds already made are
                persisted, so calling this method again resumes from the first
                tranche that still needs refunding.
        """
        with hold(session_id):
            session = load_session(self._store, session_id)
            if session.status is SessionStatus.ABORTED:
                return session
            if session.status is SessionStatus.COMPLETE:
                msg = (
                    f"session {session_id} is complete and cannot be aborted; "
                    "refund those payments directly through the client"
                )
                raise SessionStateError(msg)

            for tranche in session.paid_tranches():
                self._refund(session, tranche)

            session.status = SessionStatus.ABORTED
            self._store.update(session.touched())
            return session

    def resume(self, session_id: str) -> OrderResult:
        """Return a payable order for the session's current pending tranche.

        Razorpay orders expire (``status == "expired"``), which would otherwise
        strand a session mid-way. ``resume`` fetches the pending tranche's order
        and, when it is no longer payable, creates a replacement for the same
        amount and records it on the session.

        Args:
            session_id: Session to resume.

        Returns:
            The order to send to checkout: the existing one when it is still
            payable, otherwise a freshly created replacement.

        Raises:
            SessionNotFoundError: If the session id is unknown to the store.
            SessionStateError: If the session is closed, has no pending tranche, or
                the pending order was already paid (verify that payment instead of
                paying again).
            PaymentComposeError: If Razorpay returns no usable order id.
        """
        with hold(session_id):
            session = load_session(self._store, session_id)
            if session.is_terminal():
                msg = f"session {session_id} is {session.status.value}; nothing can be resumed"
                raise SessionStateError(msg)

            pending = session.next_pending_tranche()
            if pending is None:
                msg = f"session {session_id} has no pending tranche to resume"
                raise SessionStateError(msg)

            if pending.order_id is not None and order_is_payable(self._client, pending.order_id):
                return ensure_tranche_order(self._client, self._store, session, pending.index)
            return create_tranche_order(self._client, self._store, session, pending.index)

    def _refund(self, session: SplitSession, tranche: Tranche) -> None:
        """Refund one paid tranche and persist it as refunded.

        Args:
            session: Session being aborted.
            tranche: Paid tranche to refund.

        Raises:
            PartialPaymentError: If the refund call failed, or the tranche has no
                payment id to refund.
        """
        if tranche.payment_id is None:  # pragma: no cover - paid tranches carry one
            msg = (
                f"tranche {tranche.index} of session {session.session_id} is paid "
                "but has no payment id to refund"
            )
            raise PartialPaymentError(msg)
        try:
            self._client.payment.refund(tranche.payment_id, {"amount": tranche.amount_paise})
        except Exception as exc:
            self._store.update(session.touched())
            msg = (
                f"refund failed for tranche {tranche.index} of session "
                f"{session.session_id}; already-refunded tranches are persisted, so "
                "call abort_and_refund again to finish"
            )
            raise PartialPaymentError(msg) from exc
        tranche.status = TrancheStatus.REFUNDED
        self._store.update(session.touched())
