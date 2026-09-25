"""In-process doubles for the Razorpay client.

Nothing here touches the network and nothing here calls a real Razorpay API.
``FakeRazorpayClient`` implements the same call shapes as the official client
for the resources tranchepay uses, including real HMAC-SHA256 signature
verification so signature handling is exercised rather than stubbed out.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from typing import Any

__all__ = ["FakeApiError", "FakeRazorpayClient", "FakeSignatureError"]


class FakeApiError(Exception):
    """Mirrors ``razorpay.errors.BadRequestError`` for unknown ids."""


class FakeSignatureError(Exception):
    """Mirrors ``razorpay.errors.SignatureVerificationError``."""


class FakeRazorpayClient:
    """A thread-safe stand-in for a configured ``razorpay.Client``.

    Args:
        secret: Key secret used for HMAC signature verification.
    """

    def __init__(self, secret: str = "test_secret") -> None:
        self.secret = secret
        self.created_orders: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.captures: list[dict[str, Any]] = []
        self.refund_failures: dict[str, Exception] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._payments: dict[str, dict[str, Any]] = {}
        self._order_seq = 0
        self._refund_seq = 0
        self._lock = threading.Lock()
        self.order = FakeOrderResource(self)
        self.payment = FakePaymentResource(self)
        self.utility = FakeUtilityResource(self)

    # ---------------------------------------------------------------- helpers
    def expected_signature(self, order_id: str, payment_id: str) -> str:
        """Return the checkout signature Razorpay would produce."""
        message = f"{order_id}|{payment_id}".encode()
        return hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()

    def sign(self, order_id: str, payment_id: str) -> str:
        """Return a *valid* signature for ``order_id``/``payment_id``."""
        return self.expected_signature(order_id, payment_id)

    def add_payment(
        self,
        payment_id: str,
        amount: int,
        *,
        status: str = "captured",
        order_id: str | None = None,
    ) -> dict[str, Any]:
        """Register a payment as if the customer had just paid."""
        payment = {
            "id": payment_id,
            "amount": amount,
            "status": status,
            "order_id": order_id,
            "currency": "INR",
        }
        with self._lock:
            self._payments[payment_id] = payment
        return dict(payment)

    def set_order_status(self, order_id: str, status: str) -> None:
        """Force an order's status, e.g. ``expired`` for resume tests."""
        with self._lock:
            self._orders[order_id]["status"] = status

    def order_status(self, order_id: str) -> str:
        """Return the status of a stored order."""
        with self._lock:
            return str(self._orders[order_id]["status"])

    def created_amounts(self) -> list[int]:
        """Amounts of every order created, in creation order."""
        return [int(order["amount"]) for order in self.created_orders]

    def refund_count(self, payment_id: str) -> int:
        """How many refunds were issued for ``payment_id``."""
        return sum(1 for refund in self.refunds if refund["payment_id"] == payment_id)


class FakeOrderResource:
    """``client.order``."""

    def __init__(self, client: FakeRazorpayClient) -> None:
        self._client = client

    def create(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        with self._client._lock:
            self._client._order_seq += 1
            order = {
                "id": f"order_{self._client._order_seq:04d}",
                "amount": data["amount"],
                "currency": data.get("currency", "INR"),
                "receipt": data.get("receipt"),
                "notes": data.get("notes"),
                "payment_capture": data.get("payment_capture"),
                "status": "created",
                "request": dict(data),
            }
            self._client._orders[order["id"]] = order
            self._client.created_orders.append(dict(order))
        return dict(order)

    def fetch(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._client._lock:
            order = self._client._orders.get(order_id)
        if order is None:
            msg = f"order {order_id} not found"
            raise FakeApiError(msg)
        return dict(order)


class FakePaymentResource:
    """``client.payment``."""

    def __init__(self, client: FakeRazorpayClient) -> None:
        self._client = client

    def fetch(self, payment_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._client._lock:
            payment = self._client._payments.get(payment_id)
        if payment is None:
            msg = f"payment {payment_id} not found"
            raise FakeApiError(msg)
        return dict(payment)

    def capture(self, payment_id: str, amount: int, **kwargs: Any) -> dict[str, Any]:
        with self._client._lock:
            payment = self._client._payments[payment_id]
            payment["amount"] = amount
            payment["status"] = "captured"
            self._client.captures.append({"payment_id": payment_id, "amount": amount})
        return dict(payment)

    def refund(
        self, payment_id: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        failure = self._client.refund_failures.get(payment_id)
        if failure is not None:
            raise failure
        with self._client._lock:
            self._client._refund_seq += 1
            refund = {
                "id": f"rfnd_{self._client._refund_seq:04d}",
                "payment_id": payment_id,
                "amount": (data or {}).get("amount"),
                "status": "processed",
            }
            self._client.refunds.append(dict(refund))
        return dict(refund)


class FakeUtilityResource:
    """``client.utility``."""

    def __init__(self, client: FakeRazorpayClient) -> None:
        self._client = client

    def verify_payment_signature(self, parameters: dict[str, Any]) -> bool:
        order_id = str(parameters["razorpay_order_id"])
        payment_id = str(parameters["razorpay_payment_id"])
        signature = str(parameters["razorpay_signature"])
        expected = self._client.expected_signature(order_id, payment_id)
        if not hmac.compare_digest(expected, signature):
            msg = "Razorpay Signature Verification Failed"
            raise FakeSignatureError(msg)
        return True

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            msg = "Razorpay Signature Verification Failed"
            raise FakeSignatureError(msg)
        return True
