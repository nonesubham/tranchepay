"""Turning PhonePe response bodies into the envelope tranchepay consumes.

PhonePe's flows answer with different shapes - the v2 checkout API returns
``{"orderId", "state", "redirectUrl"}`` directly, the classic API wraps everything
in ``{"success", "code", "data"}`` - so both are normalised here, in pure
functions, before the adapter sees them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import DEFAULT_CURRENCY

__all__ = ["nested", "order_envelope", "pending_status", "status_envelope"]


def nested(source: Any, *keys: str) -> Any:
    """Walk ``keys`` into nested mappings, returning ``None`` when absent."""
    current: Any = source
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def order_envelope(
    raw: Mapping[str, Any],
    transaction_id: str,
    amount_paise: int,
    currency: str | None,
    redirect_url: Any,
) -> dict[str, Any]:
    """Normalise a pay response from either PhonePe flow.

    Args:
        raw: Untouched PhonePe response.
        transaction_id: Merchant order id sent with the request.
        amount_paise: Amount requested, in paise.
        currency: Currency requested, if any.
        redirect_url: Checkout URL, pulled from whichever field the flow uses.

    Returns:
        An envelope carrying ``transaction_id``, ``redirect_url``, the normalised
        ``status``/``success``/``code``, and ``raw`` under its own key.
    """
    envelope: dict[str, Any] = {
        "id": transaction_id,
        "transaction_id": transaction_id,
        "amount": amount_paise,
        "currency": currency or DEFAULT_CURRENCY,
        "redirect_url": str(redirect_url) if redirect_url else None,
        "raw": raw,
    }
    if raw.get("success") is False:
        return {
            **envelope,
            "order_id": None,
            "status": "failed",
            "success": False,
            "code": str(raw.get("code") or "PAYMENT_FAILED"),
            "message": str(raw.get("message") or "PhonePe rejected the payment request"),
        }
    state = str(raw.get("state") or "PENDING")
    order_id = raw.get("orderId") or nested(raw, "data", "merchantTransactionId")
    return {
        **envelope,
        "order_id": str(order_id) if order_id else None,
        "status": state.lower(),
        "success": True,
        "code": str(raw.get("code") or "PAYMENT_INITIATED"),
        "message": str(raw.get("message") or ""),
    }


def pending_status(transaction_id: str) -> dict[str, Any]:
    """The envelope for an order PhonePe answered with 204 (nothing settled yet)."""
    return {
        "success": True,
        "code": "PAYMENT_PENDING",
        "message": "no payment recorded yet",
        "state": "PENDING",
        "status": "pending",
        "amount": None,
        "transaction_id": transaction_id,
        "data": {},
    }


def status_envelope(raw: Mapping[str, Any], transaction_id: str) -> dict[str, Any]:
    """Normalise a status response from either PhonePe flow."""
    state = str(raw.get("state") or nested(raw, "data", "state") or "PENDING")
    amount = raw.get("amount")
    if amount is None:
        amount = nested(raw, "data", "amount")
    return {
        "success": bool(raw.get("success", True)),
        "code": str(raw.get("code") or f"PAYMENT_{state}"),
        "message": str(raw.get("message") or ""),
        "state": state,
        "status": state.lower(),
        "amount": amount,
        "transaction_id": transaction_id,
        "data": raw,
    }
