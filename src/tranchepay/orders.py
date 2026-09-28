"""Order creation and per-tranche bookkeeping shared by the composer and the flow.

Everything that creates or inspects an order lives here, so the rule that a
tranche order is placed before the session that points at it is written is
defined once, on top of the provider-agnostic
:class:`~tranchepay.protocol.PaymentGateway`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .enums import PaymentMode
from .exceptions import PaymentComposeError, SessionStateError
from .models import OrderResult, SplitSession
from .protocol import PaymentGateway
from .store import SessionStore

__all__ = [
    "create_order",
    "create_tranche_order",
    "ensure_tranche_order",
    "order_is_payable",
    "order_result",
    "tranche_order_result",
]

PAYABLE_ORDER_STATUSES = frozenset({"created", "attempted"})


def create_order(
    gateway: PaymentGateway,
    amount_paise: int,
    *,
    currency: str,
    receipt: str | None = None,
    notes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an order through ``gateway`` using the tranchepay defaults.

    ``amount`` is always an ``int`` number of paise, and the adapter is
    responsible for translating that into whatever the provider expects (for
    example Razorpay's ``payment_capture=1``).

    Args:
        gateway: Provider adapter (used as-is, never mutated).
        amount_paise: Amount to charge, in paise.
        currency: ISO-4217 currency code.
        receipt: Optional receipt string, passed through unchanged.
        notes: Optional notes, passed through unchanged.

    Returns:
        The untouched provider order response.
    """
    return gateway.create_order(amount_paise, currency=currency, receipt=receipt, notes=notes)


def order_result(
    raw: Mapping[str, Any],
    *,
    mode: PaymentMode,
    amount_paise: int,
    currency_default: str,
    receipt: str | None = None,
    session_id: str | None = None,
    tranche_index: int | None = None,
    net_paise: int | None = None,
    fee_paise: int = 0,
) -> OrderResult:
    """Wrap a provider order response into an :class:`~tranchepay.OrderResult`.

    Args:
        raw: Provider order response (or the fields stored for a session tranche).
        mode: Mode that produced the order.
        amount_paise: Amount sent to the provider, in paise.
        currency_default: Currency to fall back to when the response omits it.
        receipt: Receipt to fall back to when the response omits it.
        session_id: Split session id, when the order belongs to one.
        tranche_index: Zero-based tranche position within the session.
        net_paise: Amount the merchant keeps (``WITH_CHARGES`` only).
        fee_paise: Gateway fee charged to the customer (``WITH_CHARGES`` only).

    Returns:
        The wrapped order.

    Raises:
        PaymentComposeError: If the response carries no usable order id.
    """
    order_id = raw.get("id")
    if not isinstance(order_id, str) or not order_id:
        msg = f"order response has no usable 'id' field: {dict(raw)!r}"
        raise PaymentComposeError(msg)
    raw_receipt = raw.get("receipt")
    return OrderResult(
        order_id=order_id,
        amount_paise=amount_paise,
        currency=str(raw.get("currency") or currency_default),
        mode=mode,
        session_id=session_id,
        tranche_index=tranche_index,
        net_paise=net_paise,
        fee_paise=fee_paise,
        receipt=str(raw_receipt) if raw_receipt is not None else receipt,
        raw=dict(raw),
    )


def tranche_order_result(session: SplitSession, index: int) -> OrderResult:
    """Wrap the order already recorded on a tranche, without calling the API.

    Args:
        session: Session holding the tranche.
        index: Zero-based tranche position.

    Returns:
        The order for that tranche.

    Raises:
        PaymentComposeError: If the tranche has no order yet.
    """
    tranche = session.tranches[index]
    if tranche.order_id is None:
        msg = f"tranche {index} of session {session.session_id} has no order yet"
        raise PaymentComposeError(msg)
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


def create_tranche_order(
    gateway: PaymentGateway,
    store: SessionStore,
    session: SplitSession,
    index: int,
) -> OrderResult:
    """Create the order for a tranche and record it on the session.

    The order is placed before the session is written, so a failure leaves the
    session's pending tranche without an order - which :func:`ensure_tranche_order`
    retries - rather than pointing at an order that does not exist.

    Args:
        gateway: Provider adapter.
        store: Store holding the session.
        session: Session to update.
        index: Zero-based tranche position.

    Returns:
        The newly created order.
    """
    tranche = session.tranches[index]
    raw = create_order(gateway, tranche.amount_paise, currency=session.currency)
    result = order_result(
        raw,
        mode=PaymentMode.SPLIT,
        amount_paise=tranche.amount_paise,
        currency_default=session.currency,
        session_id=session.session_id,
        tranche_index=index,
    )
    tranche.order_id = result.order_id
    store.update(session.touched())
    return result


def ensure_tranche_order(
    gateway: PaymentGateway,
    store: SessionStore,
    session: SplitSession,
    index: int,
) -> OrderResult:
    """Return the tranche's order, creating it if the session has none yet."""
    if session.tranches[index].order_id is None:
        return create_tranche_order(gateway, store, session, index)
    return tranche_order_result(session, index)


def order_is_payable(gateway: PaymentGateway, order_id: str) -> bool:
    """Whether checkout can still settle ``order_id``.

    Args:
        gateway: Provider adapter.
        order_id: Order to inspect.

    Returns:
        ``True`` when the order's status is ``created`` or ``attempted``.

    Raises:
        SessionStateError: If the order cannot be fetched, or has already been
            paid - paying a replacement would risk a double charge, so the caller
            is told to verify the existing payment instead.
    """
    try:
        order = gateway.fetch_order(order_id)
    except Exception as exc:
        msg = f"could not fetch order {order_id} to check whether it is still payable"
        raise SessionStateError(msg) from exc
    status = order.get("status")
    if status == "paid":
        msg = (
            f"order {order_id} is already paid; verify that payment with "
            "verify_and_advance instead of creating a replacement order"
        )
        raise SessionStateError(msg)
    return status in PAYABLE_ORDER_STATUSES
