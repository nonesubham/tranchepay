"""The split state machine: sequential tranches, resumable and idempotent.

State transitions are keyed on ``(session_id, tranche_index)``. Each transition
is guarded by a per-session lock and re-reads the session inside that lock, so a
double-submitted verification or two concurrent callbacks advance a session at
most once: the replay returns the same next order instead of creating another.

The lock is per :class:`SplitFlow` (i.e. per composer) and therefore protects a
single process. Cross-process safety is the store adapter's job; see
:mod:`tranchepay.store`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from weakref import WeakValueDictionary

from .enums import PaymentMode, SessionStatus, TrancheStatus
from .exceptions import (
    AmountMismatchError,
    SessionNotFoundError,
    SessionStateError,
    VerificationError,
)
from .models import DEFAULT_CURRENCY, OrderResult, SplitConfig, SplitSession, Tranche
from .money import require_paise
from .orders import create_razorpay_order, order_result
from .protocol import RazorpayClientProtocol
from .split import plan_tranches
from .store import SessionStore

__all__ = ["SplitFlow"]

CAPTURED = "captured"
PAYABLE_ORDER_STATUSES = frozenset({"created", "attempted"})


class _SessionLocks:
    """Per-session re-entrant locks, held only while a transition runs.

    Locks live in a weak-value map: while one thread holds a session's lock it
    also holds a strong reference, so concurrent callers are guaranteed to see
    the same lock object, and idle locks are collected afterwards.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()

    @contextmanager
    def hold(self, session_id: str) -> Iterator[None]:
        """Serialise transitions for ``session_id`` within this process."""
        with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
        with lock:
            yield


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
        self._locks = _SessionLocks()

    # ------------------------------------------------------------------ public
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
            ``tranche_index``. Subsequent tranche orders carry neither receipt
            nor notes; pass them on the order created here.

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

        session = SplitSession(
            amount_paise=amount_paise,
            tranche_paise=plan.tranche_paise,
            currency=order_currency,
            tranches=[
                Tranche(index=index, amount_paise=amount)
                for index, amount in enumerate(plan.amounts_paise)
            ],
        )
        raw = create_razorpay_order(
            self._client,
            plan.amounts_paise[0],
            currency=order_currency,
            receipt=receipt,
            notes=notes,
        )
        result = order_result(
            raw,
            mode=PaymentMode.SPLIT,
            amount_paise=plan.amounts_paise[0],
            currency_default=order_currency,
            receipt=receipt,
            session_id=session.session_id,
            tranche_index=0,
        )
        session.tranches[0].order_id = result.order_id
        self._store.save(session)
        return result

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
        tranche's order (or ``None`` when the session is complete) - reconstructed
        from the session, with no extra API call and no new order.

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
                tranche is not the current pending tranche, or the session is
                already closed.
        """
        self._verify_signature(order_id, payment_id, signature)

        with self._locks.hold(session_id):
            session = self._load(session_id)
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
                msg = (
                    f"tranche {tranche.index} of session {session_id} is "
                    f"{tranche.status.value}; only tranche "
                    f"{pending.index if pending else 'none'} can be advanced"
                )
                raise SessionStateError(msg)

            self._require_captured(payment_id, tranche)
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
            return self._create_order_for(session, next_tranche.index)

    # ----------------------------------------------------------------- internal
    def _load(self, session_id: str) -> SplitSession:
        """Return the stored session or raise :class:`SessionNotFoundError`."""
        session = self._store.get(session_id)
        if session is None:
            msg = f"unknown split session: {session_id}"
            raise SessionNotFoundError(msg)
        return session

    def _pending_order(self, session: SplitSession) -> OrderResult | None:
        """Return the order for the current pending tranche, if there is one."""
        pending = session.next_pending_tranche()
        if pending is None:
            return None
        return self._order_for(session, pending.index)

    def _order_for(self, session: SplitSession, index: int) -> OrderResult:
        """Return the order for ``index``, creating the Razorpay order if needed."""
        tranche = session.tranches[index]
        if tranche.order_id is None:
            return self._create_order_for(session, index)
        raw = {
            "id": tranche.order_id,
            "amount": tranche.amount_paise,
            "currency": session.currency,
            "payment_capture": 1,
            "status": "created",
        }
        return order_result(
            raw,
            mode=PaymentMode.SPLIT,
            amount_paise=tranche.amount_paise,
            currency_default=session.currency,
            session_id=session.session_id,
            tranche_index=index,
        )

    def _create_order_for(self, session: SplitSession, index: int) -> OrderResult:
        """Create the Razorpay order for ``index`` and persist it on the session."""
        tranche = session.tranches[index]
        raw = create_razorpay_order(self._client, tranche.amount_paise, currency=session.currency)
        result = order_result(
            raw,
            mode=PaymentMode.SPLIT,
            amount_paise=tranche.amount_paise,
            currency_default=session.currency,
            session_id=session.session_id,
            tranche_index=index,
        )
        tranche.order_id = result.order_id
        self._store.update(session.touched())
        return result

    def _verify_signature(self, order_id: str, payment_id: str, signature: str) -> None:
        """Delegate signature verification to Razorpay's own implementation."""
        parameters = {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        }
        try:
            verified = self._client.utility.verify_payment_signature(parameters)
        except Exception as exc:
            msg = f"signature verification failed for order {order_id} / payment {payment_id}"
            raise VerificationError(msg) from exc
        if not verified:
            msg = f"signature verification failed for order {order_id} / payment {payment_id}"
            raise VerificationError(msg)

    def _require_captured(self, payment_id: str, tranche: Tranche) -> None:
        """Assert the payment is captured for exactly the tranche amount.

        Raises:
            VerificationError: If the payment cannot be fetched or is not
                captured.
            AmountMismatchError: If the captured amount is not the tranche amount.
        """
        try:
            payment = self._client.payment.fetch(payment_id)
        except Exception as exc:
            msg = f"could not fetch payment {payment_id} for tranche {tranche.index}"
            raise VerificationError(msg) from exc
        status = payment.get("status")
        if status != CAPTURED:
            msg = (
                f"payment {payment_id} for tranche {tranche.index} is {status!r}, "
                f"expected {CAPTURED!r}"
            )
            raise VerificationError(msg)
        amount = payment.get("amount")
        if amount != tranche.amount_paise:
            msg = (
                f"payment {payment_id} captured {amount!r} paise but tranche "
                f"{tranche.index} expects {tranche.amount_paise} paise"
            )
            raise AmountMismatchError(msg)
