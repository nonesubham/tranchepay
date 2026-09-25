"""The split flow: sequential tranches, independent verification, idempotency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.helpers import SplitRun
from tranchepay import (
    AmountMismatchError,
    InMemorySessionStore,
    OrderResult,
    PaymentMode,
    SessionNotFoundError,
    SessionStateError,
    SessionStatus,
    TrancheStatus,
    VerificationError,
)

CEILING = 1_000


class TestStartingASession:
    def test_first_tranche_order_is_created_with_a_session(self, run: SplitRun) -> None:
        first = run.start(2_500)

        assert first.amount_paise == CEILING
        assert first.tranche_index == 0
        assert first.session_id is not None
        assert first.mode is PaymentMode.SPLIT
        assert run.client.created_amounts() == [CEILING]

        session = run.session()
        assert [t.amount_paise for t in session.tranches] == [CEILING, CEILING, 500]
        assert session.status is SessionStatus.PENDING
        assert session.tranches[0].order_id == first.order_id
        assert session.tranches[1].order_id is None

    def test_amount_within_one_tranche_creates_no_session(
        self, run: SplitRun, split_store: InMemorySessionStore
    ) -> None:
        """Below the ceiling there is nothing to sequence: one ordinary order."""
        single = run.start(900)

        assert single.amount_paise == 900
        assert single.session_id is None
        assert single.tranche_index is None
        assert len(split_store) == 0
        assert run.client.created_amounts() == [900]

    def test_exact_multiple_has_no_remainder_tranche(self, run: SplitRun) -> None:
        run.start(2_000)

        assert [t.amount_paise for t in run.session().tranches] == [CEILING, CEILING]

    def test_receipt_and_notes_apply_to_the_first_order_only(self, run: SplitRun) -> None:
        first = run.start(2_000, receipt="rcpt_1", notes={"sku": "abc"})

        assert run.client.created_orders[0]["request"]["receipt"] == "rcpt_1"
        assert run.client.created_orders[0]["request"]["notes"] == {"sku": "abc"}

        run.verify(first, "pay_1")

        assert "receipt" not in run.client.created_orders[1]["request"]
        assert "notes" not in run.client.created_orders[1]["request"]

    def test_currency_is_kept_on_the_session(self, run: SplitRun) -> None:
        first = run.start(2_500, currency="AED")

        assert first.currency == "AED"
        assert run.session().currency == "AED"

    @pytest.mark.parametrize("amount_paise", [0, -1])
    def test_non_positive_amounts_are_rejected(self, run: SplitRun, amount_paise: int) -> None:
        with pytest.raises(ValueError, match="amount_paise must be > 0"):
            run.start(amount_paise)

    def test_float_amounts_are_rejected(self, run: SplitRun) -> None:
        with pytest.raises(TypeError, match="must be an int number of paise"):
            run.start(1_500.5)  # type: ignore[arg-type]

    def test_split_requires_a_payment_capture_flag(self, run: SplitRun) -> None:
        run.start(2_000)

        assert run.client.created_orders[0]["request"]["payment_capture"] == 1


class TestHappyPath:
    def test_three_tranches_are_verified_one_by_one(self, run: SplitRun) -> None:
        first = run.start(2_500)

        second = run.verify(first, "pay_1")
        assert second is not None
        assert second.amount_paise == CEILING
        assert second.tranche_index == 1

        assert run.session().status is SessionStatus.IN_PROGRESS
        assert run.session().tranches[0].status is TrancheStatus.PAID
        assert run.session().tranches[0].payment_id == "pay_1"

        third = run.verify(second, "pay_2")
        assert third is not None
        assert third.amount_paise == 500
        assert third.tranche_index == 2

        assert run.verify(third, "pay_3") is None

        final = run.session()
        assert final.status is SessionStatus.COMPLETE
        assert final.is_fully_paid()
        assert [t.payment_id for t in final.tranches] == ["pay_1", "pay_2", "pay_3"]
        assert final.total_paid_paise() == 2_500
        assert run.client.created_amounts() == [CEILING, CEILING, 500]

    def test_two_tranches_complete_after_the_second(self, run: SplitRun) -> None:
        first = run.start(1_500)
        second = run.verify(first, "pay_1")

        assert second is not None
        assert second.amount_paise == 500
        assert run.verify(second, "pay_2") is None
        assert run.session().status is SessionStatus.COMPLETE

    def test_each_tranche_is_verified_independently(self, run: SplitRun) -> None:
        first = run.start(2_000)

        with pytest.raises(AmountMismatchError):
            run.verify(first, "pay_1", amount=999)

        assert run.verify(first, "pay_2") is not None


class TestIdempotency:
    def test_replaying_a_paid_tranche_returns_the_same_next_order(self, run: SplitRun) -> None:
        first = run.start(2_000)

        second = run.verify(first, "pay_1")
        replay = run.verify(first, "pay_1")

        assert second is not None
        assert replay is not None
        assert replay.order_id == second.order_id
        assert run.client.created_amounts() == [CEILING, CEILING]

    def test_replaying_the_final_tranche_returns_none_twice(self, run: SplitRun) -> None:
        first = run.start(1_500)
        second = run.verify(first, "pay_1")
        assert second is not None

        assert run.verify(second, "pay_2") is None
        assert run.verify(second, "pay_2") is None
        assert run.client.created_amounts() == [CEILING, 500]

    def test_concurrent_replays_advance_the_session_exactly_once(self, run: SplitRun) -> None:
        first = run.start(2_000)
        signature = run.sign(first, "pay_1")
        workers = 8
        barrier = Barrier(workers)
        session_id = first.session_id or ""

        def verify() -> OrderResult | None:
            barrier.wait()
            return run.composer.verify_and_advance(session_id, first.order_id, "pay_1", signature)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(verify) for _ in range(workers)]
            results = [future.result() for future in futures]

        assert all(result is not None for result in results)
        assert len({result.order_id for result in results if result}) == 1
        assert run.client.created_amounts() == [CEILING, CEILING]

        session = run.session()
        assert [t.status for t in session.tranches] == [TrancheStatus.PAID, TrancheStatus.PENDING]
        assert session.status is SessionStatus.IN_PROGRESS


class TestRejections:
    def test_invalid_signature_is_rejected(self, run: SplitRun) -> None:
        first = run.start(2_000)
        run.client.add_payment("pay_1", CEILING, order_id=first.order_id)

        with pytest.raises(VerificationError, match="signature verification failed"):
            run.verify(first, "pay_1", signature="forged")

        assert run.session().tranches[0].status is TrancheStatus.PENDING
        assert len(run.client.created_orders) == 1

    def test_uncaptured_payment_is_rejected(self, run: SplitRun) -> None:
        first = run.start(2_000)

        with pytest.raises(VerificationError, match="expected 'captured'"):
            run.verify(first, "pay_1", status="authorized")

    def test_amount_mismatch_is_rejected(self, run: SplitRun) -> None:
        first = run.start(2_000)

        with pytest.raises(AmountMismatchError, match="expects 1000 paise"):
            run.verify(first, "pay_1", amount=999)

        assert run.session().tranches[0].status is TrancheStatus.PENDING

    def test_unknown_payment_is_rejected(self, run: SplitRun) -> None:
        first = run.start(2_000)
        signature = run.client.sign(first.order_id, "pay_missing")

        with pytest.raises(VerificationError, match="could not fetch payment"):
            run.verify(first, "pay_missing", signature=signature)

    def test_unknown_session_is_rejected(self, run: SplitRun) -> None:
        """A valid signature still cannot act on a session that does not exist."""
        signature = run.client.sign("order_0001", "pay_1")

        with pytest.raises(SessionNotFoundError, match="unknown split session"):
            run.composer.verify_and_advance("nope", "order_0001", "pay_1", signature)

    def test_order_from_another_session_is_rejected(self, run: SplitRun) -> None:
        run.start(2_000)
        other = run.start(2_000)
        signature = run.client.sign("order_0009", "pay_1")

        with pytest.raises(SessionStateError, match="does not belong to session"):
            run.composer.verify_and_advance(
                other.session_id or "", "order_0009", "pay_1", signature
            )

    def test_out_of_order_tranche_is_rejected(self, run: SplitRun) -> None:
        first = run.start(2_500)
        session = run.session()
        for index in (1, 2):
            order = run.client.order.create(
                {
                    "amount": session.tranches[index].amount_paise,
                    "currency": "INR",
                    "payment_capture": 1,
                }
            )
            session.tranches[index].order_id = order["id"]
        run.composer.store.update(session)

        third = session.tranches[2]
        assert third.order_id is not None
        run.client.add_payment("pay_3", third.amount_paise, order_id=third.order_id)
        signature = run.client.sign(third.order_id, "pay_3")

        with pytest.raises(SessionStateError, match="only tranche 0 can be advanced"):
            run.composer.verify_and_advance(
                first.session_id or "", third.order_id, "pay_3", signature
            )


def test_a_falsey_signature_result_is_rejected(run: SplitRun) -> None:
    """Some client versions return False instead of raising; both are failures."""
    first = run.start(2_000)
    run.client.add_payment("pay_1", CEILING, order_id=first.order_id)
    run.client.utility.verify_payment_signature = lambda parameters: False  # type: ignore[method-assign]

    with pytest.raises(VerificationError, match="signature verification failed"):
        run.verify(first, "pay_1", signature="anything")
