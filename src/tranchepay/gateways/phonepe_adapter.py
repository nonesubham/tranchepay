"""PhonePe adapter: Standard Checkout v2 (OAuth) and classic PG (X-VERIFY).

PhonePe ships no first-party Python SDK, so this adapter talks to its REST API
through :mod:`~tranchepay.gateways.phonepe_transport`. Both integration styles
are implemented:

* ``auth_mode="oauth"`` (default) - Standard Checkout v2: the client id and
  secret are exchanged for an ``O-Bearer`` token, payments start at
  ``/checkout/v2/pay``, and state is read from
  ``/checkout/v2/order/{merchant_order_id}/status``. PhonePe test accounts use
  this flow.
* ``auth_mode="x-verify"`` - classic PG: the base64 body is signed with
  ``SHA256(base64 + endpoint + salt_key) + "###" + salt_index`` for ``/pg/v1/pay``.

Both modes return the same normalised envelope. Refunds are out of scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from ..exceptions import GatewayConfigurationError
from ..models import DEFAULT_CURRENCY
from .base import require_credential
from .phonepe_auth import (
    api_merchant_id,
    encode_payload,
    resolve_base_url,
    verify_checksum,
    x_verify_token,
)
from .phonepe_normalise import nested, order_envelope, pending_status, status_envelope
from .phonepe_transport import PhonePeTransport

__all__ = ["PhonePeAdapter"]

PAY_PATH = "/pg/v1/pay"
PAY_V2_PATH = "/checkout/v2/pay"
STATUS_PATH = "/pg/v1/status"
ORDER_V2_PATH = "/checkout/v2/order"
PAY_PAGE = "PAY_PAGE"


def _transaction_id(value: str | UUID | None, receipt: str | None) -> str:
    """Return the caller's id, the receipt, or a fresh unique id."""
    if value is not None:
        return str(value)
    if receipt:
        return str(receipt)
    return f"TP{uuid4().hex.upper()}"


class PhonePeAdapter(PhonePeTransport):
    """Adapts PhonePe's REST API to :class:`~tranchepay.protocol.PaymentGateway`.

    Args:
        credentials: Needs ``merchant_id`` (client id) and ``salt_key`` (client
            secret), plus optional ``salt_index``, ``env`` (default
            ``"preprod"``), ``auth_mode`` (default ``"oauth"``),
            ``callback_url``, ``merchant_user_id``, ``api_merchant_id`` and
            ``client_version``.
        client: Optional ``httpx.Client``; ``base_url`` overrides ``env``.

    Raises:
        GatewayConfigurationError: If a required credential is missing, or the
            environment or auth mode is unknown.
    """

    def __init__(
        self,
        credentials: Mapping[str, Any],
        *,
        client: Any = None,
        base_url: str | None = None,
    ) -> None:
        env = str(credentials.get("env") or "preprod")
        super().__init__(credentials, client=client, base_url=base_url or resolve_base_url(env))
        self.env = env.strip().lower()
        self.merchant_id = require_credential(credentials, "merchant_id")
        self.salt_key = require_credential(credentials, "salt_key")
        self.salt_index = str(credentials.get("salt_index") or "1")
        self.auth_mode = str(credentials.get("auth_mode") or "oauth").strip().lower()
        if self.auth_mode not in {"oauth", "x-verify"}:
            msg = f"unknown PhonePe auth_mode {self.auth_mode!r}; expected 'oauth' or 'x-verify'"
            raise GatewayConfigurationError(msg)
        self.merchant_user_id = str(credentials.get("merchant_user_id") or self.merchant_id)
        callback = credentials.get("callback_url")
        self.callback_url: str | None = str(callback) if callback else None
        self.api_merchant_id = str(
            credentials.get("api_merchant_id") or api_merchant_id(self.merchant_id)
        )

    def _sign_request(self, endpoint: str, payload: Mapping[str, Any]) -> tuple[str, str]:
        """Sign ``payload`` for the classic X-VERIFY flow.

        Args:
            endpoint: Endpoint path, e.g. ``"/pg/v1/pay"``.
            payload: JSON body to encode and sign.

        Returns:
            ``(base64_payload, x_verify_header)``.
        """
        encoded = encode_payload(payload)
        return encoded, x_verify_token(encoded, endpoint, self.salt_key, self.salt_index)

    def _x_verify_headers(self, x_verify: str) -> dict[str, str]:
        """Headers for a classic X-VERIFY call."""
        return {
            "Content-Type": "application/json",
            "X-VERIFY": x_verify,
            "X-MERCHANT-ID": self.api_merchant_id,
        }

    def _apply_optionals(
        self,
        body: dict[str, Any],
        *,
        currency: str | None,
        notes: Mapping[str, Any] | None,
        callback_url: str | None,
        redirect_url: str | None,
    ) -> None:
        """Add the optional fields both pay flows accept, when they are set."""
        callback = callback_url or self.callback_url
        if callback:
            body["callbackUrl"] = callback
        if redirect_url:
            body["redirectUrl"] = redirect_url
        if currency and currency != DEFAULT_CURRENCY:
            body["currency"] = currency
        if notes:
            body["metaInfo"] = dict(notes)

    def _pay_body(
        self, transaction_id: str, amount_paise: int, merchant_user_id: str | None, extra: Any
    ) -> dict[str, Any]:
        """Build the shared pay body, adding the id field each flow requires."""
        body: dict[str, Any] = {
            **extra,
            "merchantTransactionId": transaction_id,
            "merchantUserId": merchant_user_id or self.merchant_user_id,
            "amount": amount_paise,
            "redirectMode": "REDIRECT",
            "paymentInstrument": {"type": PAY_PAGE},
        }
        if self.auth_mode == "x-verify":
            body["merchantId"] = self.api_merchant_id
        else:
            body["merchantOrderId"] = transaction_id
        return body

    def create_order(
        self,
        amount_paise: int,
        *,
        currency: str | None = None,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
        merchant_transaction_id: str | UUID | None = None,
        callback_url: str | None = None,
        redirect_url: str | None = None,
        merchant_user_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Create a PhonePe payment request and return the checkout redirect.

        ``merchant_transaction_id`` (or ``receipt``) becomes PhonePe's
        ``merchantOrderId`` and must be unique per attempt; a UUID-derived id is
        generated when neither is given. ``extra`` is merged into the request body
        as an escape hatch, but the named fields always win.

        Args:
            amount_paise: Amount to charge, in paise.
            currency: ISO-4217 code; only non-INR values are forwarded.
            receipt: Used as the transaction id when no id is passed.
            notes: Forwarded as ``metaInfo``.
            merchant_transaction_id: Unique id for this payment attempt.
            callback_url: Webhook URL PhonePe should call with the result.
            redirect_url: Where PhonePe sends the customer after checkout.
            merchant_user_id: PhonePe ``merchantUserId``; defaults to config.
            **extra: Extra body fields merged into the request.

        Returns:
            An envelope with ``transaction_id``, ``redirect_url`` and ``raw``.
        """
        transaction_id = _transaction_id(merchant_transaction_id, receipt)
        body = self._pay_body(transaction_id, amount_paise, merchant_user_id, extra)
        self._apply_optionals(
            body,
            currency=currency,
            notes=notes,
            callback_url=callback_url,
            redirect_url=redirect_url,
        )
        if self.auth_mode == "x-verify":
            encoded, x_verify = self._sign_request(PAY_PATH, body)
            request = self.request(
                "POST",
                PAY_PATH,
                headers=self._x_verify_headers(x_verify),
                payload={"request": encoded},
            )
            raw = self.execute(request)
            redirect = nested(raw, "data", "instrumentResponse", "redirectInfo", "url")
        else:
            request = self.request("POST", PAY_V2_PATH, headers=self.oauth_headers(), payload=body)
            raw = self.execute(request)
            redirect = raw.get("redirectUrl")
        return order_envelope(raw, transaction_id, amount_paise, currency, redirect)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        """Return the normalised status for ``order_id``."""
        return self._status(order_id)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Return the status for ``payment_id``, a PhonePe merchant order id."""
        return self._status(payment_id)

    def _status(self, transaction_id: str) -> dict[str, Any]:
        """Read payment state from PhonePe's status endpoint."""
        if self.auth_mode == "x-verify":
            path = f"{STATUS_PATH}/{self.api_merchant_id}/{transaction_id}"
            x_verify = x_verify_token("", path, self.salt_key, self.salt_index)
            request = self.request("GET", path, headers=self._x_verify_headers(x_verify))
        else:
            path = f"{ORDER_V2_PATH}/{transaction_id}/status"
            request = self.request("GET", path, headers=self.oauth_headers())
        raw = self.execute(request)
        return pending_status(transaction_id) if not raw else status_envelope(raw, transaction_id)

    def verify_signature(
        self, order_id: str, payment_id: str, signature: str | None = None
    ) -> bool:
        """Verify a PhonePe ``X-VERIFY`` checksum.

        PhonePe signs *payloads*, not order/payment pairs, so the two-argument
        form ``verify_signature(payload, checksum)`` is the meaningful one:
        ``payload`` is the raw callback body (or the base64 ``response`` string
        from the redirect) and ``checksum`` is its ``X-VERIFY`` value.

        Returns:
            ``True`` for the two-argument form when the checksum matches.

        Raises:
            NotImplementedError: For the three-argument
                :class:`~tranchepay.PaymentGateway` form, which has no PhonePe
                meaning. Use :meth:`verify_webhook_signature` and
                :meth:`fetch_payment` instead.
        """
        if signature is None:
            return verify_checksum(order_id, payment_id, self.salt_key)
        msg = (
            "PhonePeAdapter.verify_signature does not verify order/payment pairs: "
            "PhonePe signs payloads (X-VERIFY), not checkout signatures. Call "
            "verify_webhook_signature(body, signature) with the raw payload, and "
            "fetch_payment(transaction_id) to confirm the payment state."
        )
        raise NotImplementedError(msg)

    def verify_webhook_signature(
        self, body: str, signature: str, secret: str | None = None
    ) -> bool:
        """Verify a PhonePe callback/webhook ``X-VERIFY`` checksum.

        The checksum is ``SHA256(payload + salt_key) + "###" + salt_index``; the
        suffix is optional here, so a bare hex digest also verifies.

        Args:
            body: The exact payload PhonePe signed, not a re-serialized copy.
            signature: Value of the ``X-VERIFY`` header.
            secret: Overrides the configured salt key.

        Returns:
            ``True`` when the checksum matches.
        """
        return verify_checksum(body, signature, secret or self.salt_key)

    def refund_payment(self, payment_id: str, amount_paise: int) -> dict[str, Any]:
        """Not supported: PhonePe refunds use a separate API and are out of scope.

        Raises:
            NotImplementedError: Always. PhonePe refunds go through its Refund
                API, with its own signing and state machine, which this adapter
                does not model. Refund from PhonePe's dashboard instead.
        """
        msg = (
            "PhonePeAdapter.refund_payment is not implemented: PhonePe refunds use "
            "the separate Refund API (not the checkout pay/status flow) and are out "
            "of scope for this adapter. Refund through the PhonePe dashboard or "
            "PhonePe's own tooling, then abort the session in tranchepay."
        )
        raise NotImplementedError(msg)
