"""The reference adapter: tranchepay composed around the official Razorpay SDK.

This is the only module in tranchepay that knows the shape of ``razorpay.Client``
(``client.order``, ``client.payment``, ``client.utility``). Everything else talks
to :class:`~tranchepay.protocol.PaymentGateway`, so the composer, the split flow
and the recovery code are provider-agnostic.

The client is never subclassed, monkey-patched or wrapped: it is stored as-is and
only its public resources are called, which is what keeps the "composition only"
promise intact when a new gateway is added.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from ..exceptions import GatewayConfigurationError
from ..models import DEFAULT_CURRENCY
from .base import require_credential

__all__ = [
    "OrderResource",
    "PaymentResource",
    "RazorpayAdapter",
    "RazorpayClientProtocol",
    "UtilityResource",
]


class OrderResource(Protocol):
    """``razorpay.Client.order``."""

    def create(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Create an order and return the API response."""
        ...  # pragma: no cover - protocol declaration

    def fetch(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Fetch an order by id and return the API response."""
        ...  # pragma: no cover - protocol declaration


class PaymentResource(Protocol):
    """``razorpay.Client.payment``."""

    def fetch(self, payment_id: str, **kwargs: Any) -> dict[str, Any]:
        """Fetch a payment by id and return the API response."""
        ...  # pragma: no cover - protocol declaration

    def refund(self, payment_id: str, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Refund a payment; ``data`` carries at least ``amount`` in paise."""
        ...  # pragma: no cover - protocol declaration


class UtilityResource(Protocol):
    """``razorpay.Client.utility``."""

    def verify_payment_signature(self, parameters: dict[str, Any]) -> bool:
        """Verify a checkout signature; raise or return ``False`` when invalid."""
        ...  # pragma: no cover - protocol declaration

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        """Verify a webhook signature; raise or return ``False`` when invalid."""
        ...  # pragma: no cover - protocol declaration


@runtime_checkable
class RazorpayClientProtocol(Protocol):
    """A configured Razorpay client (official, or a structurally compatible stub).

    The resources are declared as read-only properties rather than attributes so
    concrete resource types are accepted covariantly: the official client exposes
    plain attributes and still satisfies this protocol.
    """

    @property
    def order(self) -> OrderResource:
        """Order resource used to create and fetch orders."""
        ...  # pragma: no cover - protocol declaration

    @property
    def payment(self) -> PaymentResource:
        """Payment resource used to fetch and refund payments."""
        ...  # pragma: no cover - protocol declaration

    @property
    def utility(self) -> UtilityResource:
        """Utility resource that verifies Razorpay signatures."""
        ...  # pragma: no cover - protocol declaration


class RazorpayAdapter:
    """Adapts a configured ``razorpay.Client`` to :class:`PaymentGateway`.

    Args:
        client: Configured Razorpay client (or any object exposing the same
            ``order``/``payment``/``utility`` resources). Stored as-is and never
            mutated.
    """

    provider = "razorpay"

    def __init__(self, client: RazorpayClientProtocol) -> None:
        self._client = client

    @property
    def client(self) -> RazorpayClientProtocol:
        """The wrapped Razorpay client, unchanged."""
        return self._client

    @classmethod
    def from_credentials(cls, credentials: Mapping[str, Any]) -> RazorpayAdapter:
        """Build an adapter from ``key_id``/``key_secret``, or a ready ``client``.

        Args:
            credentials: Either a pre-built ``client`` to wrap (useful when the
                application already owns one), or ``key_id`` and ``key_secret``
                to construct a new client with.

        Returns:
            A ready adapter. Constructing the SDK client performs no I/O.

        Raises:
            GatewayConfigurationError: If a credential is missing, or the
                ``razorpay`` package is not installed.
        """
        existing = credentials.get("client")
        if existing is not None:
            return cls(existing)
        key_id = require_credential(credentials, "key_id")
        key_secret = require_credential(credentials, "key_secret")
        try:
            import razorpay  # noqa: PLC0415 - optional dependency, imported on demand
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
            msg = "the razorpay gateway needs the razorpay package: pip install razorpay"
            raise GatewayConfigurationError(msg) from exc
        return cls(razorpay.Client(auth=(key_id, key_secret)))

    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str | None = None,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Razorpay order, always with ``payment_capture=1``."""
        data: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency or DEFAULT_CURRENCY,
            "payment_capture": 1,
        }
        if receipt is not None:
            data["receipt"] = receipt
        if notes is not None:
            data["notes"] = dict(notes)
        return self._client.order.create(data)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Fetch the order by id."""
        return self._client.order.fetch(order_id)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Fetch the payment by id."""
        return self._client.payment.fetch(payment_id)

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify a checkout signature with Razorpay's own utility resource."""
        return bool(
            self._client.utility.verify_payment_signature(
                {
                    "razorpay_order_id": order_id,
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature,
                }
            )
        )

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        """Verify a webhook signature with Razorpay's own utility resource."""
        return bool(self._client.utility.verify_webhook_signature(body, signature, secret))

    def refund_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        """Refund exactly ``amount_paise`` of ``payment_id``."""
        return self._client.payment.refund(payment_id, {"amount": amount_paise})
