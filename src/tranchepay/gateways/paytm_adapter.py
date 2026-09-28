"""Paytm adapter: structural, with the HTTP call left to the merchant.

Paytm has no first-party Python SDK, so this adapter is written directly against
its REST API. Request building - URL, headers, JSON body, and the paise-to-rupee
conversion - is complete and tested; :meth:`execute
<tranchepay.gateways.base.HttpGatewayAdapter.execute>` raises
``NotImplementedError`` because sending the call also requires the merchant's
checksum signing rules and the host their account is provisioned on.

The URLs below are placeholders. Replace them (via the ``base_url`` argument or
by implementing ``execute``) with the hosts from Paytm's documentation for your
merchant account before going live.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..models import DEFAULT_CURRENCY
from .base import GatewayRequest, HttpGatewayAdapter, require_credential, rupees_from_paise

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

__all__ = ["PaytmAdapter"]


class PaytmAdapter(HttpGatewayAdapter):
    """Adapts Paytm's REST API to :class:`~tranchepay.protocol.PaymentGateway`.

    Args:
        credentials: Must contain ``merchant_id`` and ``merchant_key``. Optional
            keys: ``website`` (defaults to ``"DEFAULT"``) and ``callback_url``.
        client: Optional pre-configured ``httpx.Client``.
        base_url: Overrides the placeholder host.

    Raises:
        GatewayConfigurationError: If a required credential is missing.
    """

    provider = "paytm"
    default_base_url = "https://api.paytm.com"  # placeholder - confirm before use

    def __init__(
        self,
        credentials: Mapping[str, Any],
        *,
        client: httpx.Client | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(credentials, client=client, base_url=base_url)
        self.merchant_id = require_credential(credentials, "merchant_id")
        self.merchant_key = require_credential(credentials, "merchant_key")
        self.website = str(credentials.get("website") or "DEFAULT")
        self.callback_url = credentials.get("callback_url")

    def _headers(self) -> dict[str, str]:
        """Headers common to every call.

        Paytm also expects a ``signature`` header carrying the body checksum
        (generated with the merchant key). It is deliberately not fabricated
        here: add it in ``execute`` using the algorithm from Paytm's docs.
        """
        return {"Content-Type": "application/json"}

    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str | None = None,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the ``initiateTransaction`` call and send it.

        Raises:
            NotImplementedError: Until :meth:`execute` is implemented.
        """
        payload: dict[str, Any] = {
            "requestType": "Payment",
            "mid": self.merchant_id,
            "websiteName": self.website,
            "orderId": receipt or f"tranchepay-{amount_paise}",
            "txnAmount": {
                "value": rupees_from_paise(amount_paise),
                "currency": currency or DEFAULT_CURRENCY,
            },
        }
        if self.callback_url is not None:
            payload["callbackUrl"] = self.callback_url
        if notes is not None:
            payload["notes"] = dict(notes)
        request = self.request(
            "POST",
            f"/theia/api/v1/initiateTransaction?mid={self.merchant_id}",
            headers=self._headers(),
            payload=payload,
        )
        return self.execute(request)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Build the order-status call and send it."""
        request = self.order_status_request(order_id)
        return self.execute(request)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Build a status call for ``payment_id``.

        Paytm reports payment state per order rather than per standalone payment
        id, so ``payment_id`` is looked up as an order id.
        """
        request = self.order_status_request(payment_id)
        return self.execute(request)

    def order_status_request(self, order_id: str) -> GatewayRequest:
        """Describe the order-status call without sending it."""
        return self.request(
            "POST",
            "/v3/order/status",
            headers=self._headers(),
            payload={"mid": self.merchant_id, "orderId": order_id},
        )

    def refund_request(self, payment_id: str, amount_paise: int) -> GatewayRequest:
        """Describe the refund call without sending it."""
        return self.request(
            "POST",
            "/v2/refund/apply",
            headers=self._headers(),
            payload={
                "mid": self.merchant_id,
                "txnId": payment_id,
                "refundAmount": {
                    "value": rupees_from_paise(amount_paise),
                    "currency": DEFAULT_CURRENCY,
                },
            },
        )

    def refund_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        """Build the refund call and send it."""
        return self.execute(self.refund_request(payment_id, amount_paise))

    def verify_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Not implemented: Paytm verifies with a body checksum, not a signature.

        Raises:
            NotImplementedError: Always. Implement the checksum with
                :attr:`merchant_key` per Paytm's documentation.
        """
        msg = (
            "PaytmAdapter.verify_signature is not implemented: Paytm uses a "
            "response checksum (SHA-256 over the body with merchant_key) rather "
            "than a checkout signature. Implement it with Paytm's algorithm."
        )
        raise NotImplementedError(msg)

    def verify_webhook_signature(self, body: str, signature: str, secret: str) -> bool:
        """Not implemented: see :meth:`verify_signature`."""
        msg = (
            "PaytmAdapter.verify_webhook_signature is not implemented: verify "
            "Paytm's checksum header with the merchant key before trusting a "
            "webhook payload."
        )
        raise NotImplementedError(msg)
