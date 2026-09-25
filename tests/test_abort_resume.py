"""Unwinding a split session (refunds) and repairing one (resume)."""

from __future__ import annotations

import pytest

from tests.fakes import FakeApiError
from tests.helpers import SplitRun
from tranchepay import (
    InMemorySessionStore,
    OrderResult,
    PartialPaymentError,
    SessionNotFoundError,
    SessionStateError,
    SessionStatus,
    TrancheStatus,
)


def paid_twice(run: SplitRun) -> tuple[OrderResult, OrderResult]:
    """Start a three-tranche session and pay the first two tranches."""
    first = run.start(2_500)
    second = run.verify(first, "pay_1")
    assert second is not None
    third = run.verify(second, "pay_2")
    assert third is not None
    return second, third


class TestAbortAndRefund:
    def test_refunds_every_paid_tranche_and_aborts(self, run: SplitRun) -> None:
        run.start(2_500)
        second, _ = paid_twice(run)

        aborted = run.composer.abort_and_refund(run.session_id or "")

        assert aborted.status is SessionStatus.ABORTED
        assert [t.status for t in aborted.tranches] == [
            TrancheStatus.REFUNDED,
            TrancheStatus.REFUNDED,
            TrancheStatus.PENDING,
        ]
        assert run.client.refund_count("pay_1") == 1
        assert run.client.refund_count("pay_2") == 1
        assert [refund["amount"] for refund in run.client.refunds] == [1_000, 1_000]
        assert second.tranche_index == 1

    def test_partial_payment_refunds_only_what_was_captured(self, run: SplitRun) -> None:
        first = run.start(2_500)
        run.verify(first, "pay_1")

        aborted = run.composer.abort_and_refund(run.session_id or "")

        assert [refund["amount"] for refund in run.client.refunds] == [1_000]
        assert aborted.tranches[0].status is TrancheStatus.REFUNDED
        assert aborted.tranches[1].status is TrancheStatus.PENDING
        assert aborted.status is SessionStatus.ABORTED

    def test_aborting_twice_refunds_once(self, run: SplitRun) -> None:
        run.start(1_500)
        first = run.start(2_500)

        run.verify(first, "pay_1")
        first_abort = run.composer.abort_and_refund(run.session_id or "")
        second_abort = run.composer.abort_and_refund(run.session_id or "")

        assert first_abort.status is SessionStatus.ABORTED
        assert second_abort.status is SessionStatus.ABORTED
        assert run.client.refund_count("pay_1") == 1

    def test_aborting_an_unpaid_session_issues_no_refunds(self, run: SplitRun) -> None:
        run.start(2_500)

        aborted = run.composer.abort_and_refund(run.session_id or "")

        assert aborted.status is SessionStatus.ABORTED
        assert run.client.refunds == []
        assert [t.status for t in aborted.tranches] == [TrancheStatus.PENDING] * 3

    def test_a_completed_session_is_not_abortable(self, run: SplitRun) -> None:
        first = run.start(1_500)
        second = run.verify(first, "pay_1")
        assert second is not None
        assert run.verify(second, "pay_2") is None

        with pytest.raises(SessionStateError, match="complete and cannot be aborted"):
            run.composer.abort_and_refund(run.session_id or "")

        assert run.client.refunds == []

    def test_unknown_session_is_rejected(self, run: SplitRun) -> None:
        with pytest.raises(SessionNotFoundError, match="unknown split session"):
            run.composer.abort_and_refund("nope")

    def test_a_failed_refund_is_reported_and_retryable(self, run: SplitRun) -> None:
        run.start(2_500)
        second, _ = paid_twice(run)
        run.client.refund_failures["pay_2"] = FakeApiError("gateway down")

        with pytest.raises(PartialPaymentError, match="refund failed for tranche 1"):
            run.composer.abort_and_refund(run.session_id or "")

        session = run.session()
        assert session.status is not SessionStatus.ABORTED
        assert session.tranches[0].status is TrancheStatus.REFUNDED
        assert session.tranches[1].status is TrancheStatus.PAID

        del run.client.refund_failures["pay_2"]
        aborted = run.composer.abort_and_refund(run.session_id or "")

        assert aborted.status is SessionStatus.ABORTED
        assert run.client.refund_count("pay_1") == 1, "already-refunded tranches are not repeated"
        assert run.client.refund_count("pay_2") == 1
        assert second.tranche_index == 1

    def test_a_failed_refund_is_chained(self, run: SplitRun) -> None:
        first = run.start(2_500)
        run.verify(first, "pay_1")
        run.client.refund_failures["pay_1"] = FakeApiError("gateway down")

        with pytest.raises(PartialPaymentError) as excinfo:
            run.composer.abort_and_refund(run.session_id or "")

        assert isinstance(excinfo.value.__cause__, FakeApiError)

    def test_verifying_after_abort_is_rejected(self, run: SplitRun) -> None:
        first = run.start(2_500)
        run.verify(first, "pay_1")
        run.composer.abort_and_refund(run.session_id or "")
        signature = run.sign(first, "pay_1")

        with pytest.raises(SessionStateError, match="aborted"):
            run.composer.verify_and_advance(
                run.session_id or "", first.order_id, "pay_1", signature
            )


class TestResume:
    def test_an_expired_order_is_replaced(self, run: SplitRun) -> None:
        first = run.start(2_500)
        second = run.verify(first, "pay_1")
        assert second is not None
        run.expire(second)

        fresh = run.composer.resume(run.session_id or "")

        assert fresh.order_id != second.order_id
        assert fresh.amount_paise == second.amount_paise
        assert fresh.tranche_index == 1
        assert run.session().tranches[1].order_id == fresh.order_id
        assert run.client.created_amounts() == [1_000, 1_000, 1_000]

    def test_a_still_payable_order_is_returned_untouched(self, run: SplitRun) -> None:
        first = run.start(2_500)
        second = run.verify(first, "pay_1")
        assert second is not None
        created = len(run.client.created_orders)

        resumed = run.composer.resume(run.session_id or "")

        assert resumed.order_id == second.order_id
        assert len(run.client.created_orders) == created

    def test_an_order_with_no_order_id_is_created(
        self, run: SplitRun, split_store: InMemorySessionStore
    ) -> None:
        run.start(2_500)
        session = run.session()
        session.tranches[0].order_id = None
        split_store.update(session)

        resumed = run.composer.resume(run.session_id or "")

        assert resumed.tranche_index == 0
        assert resumed.amount_paise == 1_000
        assert run.session().tranches[0].order_id == resumed.order_id

    def test_a_paid_order_is_never_replaced(self, run: SplitRun) -> None:
        """Re-creating an order the customer already paid would risk a double charge."""
        first = run.start(2_500)
        run.client.set_order_status(first.order_id, "paid")

        with pytest.raises(SessionStateError, match="is already paid"):
            run.composer.resume(run.session_id or "")

        assert len(run.client.created_orders) == 1

    def test_an_unfetchable_order_is_reported(
        self, run: SplitRun, split_store: InMemorySessionStore
    ) -> None:
        run.start(2_500)
        session = run.session()
        session.tranches[0].order_id = "order_missing"
        split_store.update(session)

        with pytest.raises(SessionStateError, match="could not fetch order"):
            run.composer.resume(run.session_id or "")

    def test_resume_is_refused_on_a_completed_session(self, run: SplitRun) -> None:
        first = run.start(1_500)
        second = run.verify(first, "pay_1")
        assert second is not None
        assert run.verify(second, "pay_2") is None

        with pytest.raises(SessionStateError, match="complete"):
            run.composer.resume(run.session_id or "")

    def test_resume_is_refused_on_an_aborted_session(self, run: SplitRun) -> None:
        run.start(2_500)
        run.composer.abort_and_refund(run.session_id or "")

        with pytest.raises(SessionStateError, match="aborted"):
            run.composer.resume(run.session_id or "")

    def test_resume_rejects_an_unknown_session(self, run: SplitRun) -> None:
        with pytest.raises(SessionNotFoundError, match="unknown split session"):
            run.composer.resume("nope")

    def test_a_resumed_session_can_be_paid_to_completion(self, run: SplitRun) -> None:
        first = run.start(2_500)
        second = run.verify(first, "pay_1")
        assert second is not None
        run.expire(second)

        fresh = run.composer.resume(run.session_id or "")
        third = run.verify(fresh, "pay_2")

        assert third is not None
        assert third.amount_paise == 500
        assert run.verify(third, "pay_3") is None
        assert run.session().status is SessionStatus.COMPLETE
        assert run.session().total_paid_paise() == 2_500


def test_a_paid_tranche_without_a_payment_id_cannot_be_refunded(run: SplitRun) -> None:
    """Refunding needs a payment id; a corrupted session must not abort silently."""
    first = run.start(2_500)
    run.verify(first, "pay_1")
    session = run.session()
    session.tranches[0].payment_id = None
    run.composer.store.update(session)

    with pytest.raises(PartialPaymentError, match="has no payment id to refund"):
        run.composer.abort_and_refund(run.session_id or "")

    assert run.client.refunds == []


def test_resume_needs_a_pending_tranche(run: SplitRun) -> None:
    first = run.start(2_500)
    session = run.session()
    assert first.session_id is not None
    for tranche in session.tranches:
        tranche.status = TrancheStatus.FAILED
    run.composer.store.update(session)

    with pytest.raises(SessionStateError, match="no pending tranche to resume"):
        run.composer.resume(first.session_id)
