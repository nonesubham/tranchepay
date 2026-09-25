"""Structural typing for the Razorpay client tranchepay composes around.

tranchepay never subclasses ``razorpay.Client``. It only calls public resources
on an instance the developer already configured, so the dependency is expressed
as a :class:`typing.Protocol`: the official client satisfies it structurally, and
so does any other object exposing ``order``, ``payment``, and ``utility`` with the
same call shapes (which is what makes the test double in this repo possible).

Only the subset tranchepay actually uses is declared. Every method here is
called by this library and is covered by the test double; nothing else is
assumed about the client.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "OrderResource",
    "PaymentResource",
    "RazorpayClientProtocol",
    "UtilityResource",
]


class OrderResource(Protocol):
    """``client.order``."""

    def create(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Create an order and return the API response."""
        ...  # pragma: no cover - protocol declaration

    def fetch(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Fetch an order by id and return the API response."""
        ...  # pragma: no cover - protocol declaration


class PaymentResource(Protocol):
    """``client.payment``."""

    def fetch(self, payment_id: str, **kwargs: Any) -> dict[str, Any]:
        """Fetch a payment by id and return the API response."""
        ...  # pragma: no cover - protocol declaration

    def capture(self, payment_id: str, amount: int, **kwargs: Any) -> dict[str, Any]:
        """Capture a payment for ``amount`` paise."""
        ...  # pragma: no cover - protocol declaration

    def refund(self, payment_id: str, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Refund a payment; ``data`` carries at least ``amount`` in paise."""
        ...  # pragma: no cover - protocol declaration


class UtilityResource(Protocol):
    """``client.utility``."""

    def verify_payment_signature(self, parameters: dict[str, Any]) -> bool:
        """Verify a checkout signature; raise or return ``False`` when invalid."""
        ...  # pragma: no cover - protocol declaration

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        """Verify a webhook signature; raise or return ``False`` when invalid."""
        ...  # pragma: no cover - protocol declaration


@runtime_checkable
class RazorpayClientProtocol(Protocol):
    """A configured Razorpay client (official or compatible).

    The resources are declared as read-only properties rather than attributes so
    that concrete resource types are accepted covariantly: the official client
    exposes plain attributes, and ``isinstance`` works against both.
    """

    @property
    def order(self) -> OrderResource:
        """Order resource used to create and fetch orders."""
        ...  # pragma: no cover - protocol declaration

    @property
    def payment(self) -> PaymentResource:
        """Payment resource used to fetch, capture, and refund payments."""
        ...  # pragma: no cover - protocol declaration

    @property
    def utility(self) -> UtilityResource:
        """Utility resource that verifies Razorpay signatures."""
        ...  # pragma: no cover - protocol declaration
