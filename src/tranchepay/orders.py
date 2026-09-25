"""Order creation and response wrapping shared by the composer and the flow.

Everything that talks to ``client.order`` lives here so that the payload shape
(``payment_capture=1``, integer paise, optional receipt/notes) is defined once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .enums import PaymentMode
from .exceptions import PaymentComposeError
from .models import OrderResult
from .protocol import RazorpayClientProtocol

__all__ = ["create_razorpay_order", "order_result"]


def create_razorpay_order(
    client: RazorpayClientProtocol,
    amount_paise: int,
    *,
    currency: str,
    receipt: str | None = None,
    notes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Razorpay order with the tranchepay defaults.

    Orders are always created with ``payment_capture=1`` so the payment settles
    without a second API call, and ``amount`` is always an ``int`` number of
    paise.

    Args:
        client: Configured Razorpay client (used as-is, never mutated).
        amount_paise: Amount to charge, in paise.
        currency: ISO-4217 currency code.
        receipt: Optional receipt string, passed through unchanged.
        notes: Optional notes, passed through unchanged.

    Returns:
        The untouched Razorpay order response.
    """
    data: dict[str, Any] = {
        "amount": amount_paise,
        "currency": currency,
        "payment_capture": 1,
    }
    if receipt is not None:
        data["receipt"] = receipt
    if notes is not None:
        data["notes"] = dict(notes)
    return client.order.create(data)


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
    """Wrap a Razorpay order response into an :class:`~tranchepay.OrderResult`.

    Args:
        raw: Razorpay order response (or the fields stored for a session tranche).
        mode: Mode that produced the order.
        amount_paise: Amount sent to Razorpay, in paise.
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
        msg = f"Razorpay order response has no usable 'id' field: {dict(raw)!r}"
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
