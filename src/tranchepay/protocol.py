"""The gateway abstraction tranchepay composes around.

:class:`PaymentGateway` is the only surface the rest of the library talks to:
create an order, look an order or payment up, verify a signature, issue a
refund. Razorpay, Paytm and PhonePe each ship an adapter implementing it, which
is why the composer, the split flow and the recovery code contain no
provider-specific calls at all.

Adapters return their provider's response payload unchanged; tranchepay reads
only the fields it needs, so forwarding the API response is both simpler and
more future-proof than translating it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = ["PaymentGateway"]


@runtime_checkable
class PaymentGateway(Protocol):
    """What tranchepay needs from a payment provider.

    A gateway is satisfied structurally: implement these methods and the
    composer will drive it. The shipped adapters are
    :class:`~tranchepay.gateways.RazorpayAdapter`,
    :class:`~tranchepay.gateways.PaytmAdapter` and
    :class:`~tranchepay.gateways.PhonePeAdapter`; a custom one can be registered
    with :meth:`tranchepay.GatewayFactory.register_gateway`.

    The protocol is intentionally narrow. Everything is expressed in integer
    paise, and no method may mutate the arguments it is given.
    """

    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str | None = None,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an order for ``amount_paise`` and return the provider response.

        The response must carry a usable ``id``; ``amount``, ``currency``,
        ``receipt`` and ``status`` are read when present. ``currency`` falls back
        to the provider's default when ``None``.
        """
        ...  # pragma: no cover - protocol declaration

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Return the provider's order payload, including its ``status``.

        Raises whatever the provider raises when the order is unknown; callers
        translate that into :class:`~tranchepay.SessionStateError`.
        """
        ...  # pragma: no cover - protocol declaration

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Return the provider's payment payload, including ``status``/``amount``."""
        ...  # pragma: no cover - protocol declaration

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verify a checkout signature.

        Returns ``True`` when valid and may either return ``False`` or raise when
        it is not, mirroring the provider SDKs; tranchepay normalises both into
        :class:`~tranchepay.VerificationError`.
        """
        ...  # pragma: no cover - protocol declaration

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        """Verify a webhook payload against ``secret``, same contract as above."""
        ...  # pragma: no cover - protocol declaration

    def refund_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        """Refund exactly ``amount_paise`` of ``payment_id`` and return the response."""
        ...  # pragma: no cover - protocol declaration
