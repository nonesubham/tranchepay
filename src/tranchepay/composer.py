"""The composer: one configured client, three payment modes.

Composition only. The developer constructs ``razorpay.Client`` themselves and
hands it in; tranchepay calls ``client.order``, ``client.payment``, and
``client.utility`` and never subclasses, patches, or wraps the client itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .enums import PaymentMode
from .exceptions import PaymentComposeError
from .models import DEFAULT_CURRENCY, ChargesConfig, OrderResult, SplitConfig
from .money import gross_up, require_paise
from .protocol import RazorpayClientProtocol

__all__ = ["PaymentComposer"]


def _ensure_client(client: object) -> RazorpayClientProtocol:
    """Return ``client`` if it exposes the Razorpay resources tranchepay uses.

    Args:
        client: Candidate client, typed loosely so untyped callers are checked at
            runtime instead of trusted.

    Returns:
        The same object, narrowed to :class:`~tranchepay.protocol.RazorpayClientProtocol`.

    Raises:
        TypeError: If the object does not expose ``order``, ``payment``, and
            ``utility``.
    """
    if not isinstance(client, RazorpayClientProtocol):
        msg = (
            "client must be a configured razorpay.Client instance (or an object exposing "
            f"order/payment/utility resources), not {type(client).__name__}"
        )
        raise TypeError(msg)
    return client


class PaymentComposer:
    """Creates Razorpay orders for the three supported payment modes.

    Args:
        client: A configured ``razorpay.Client`` instance (or any object exposing
            the same ``order``/``payment``/``utility`` resources). It is used
            as-is and never mutated.
        charges: Required to use :attr:`PaymentMode.WITH_CHARGES`; carries the
            ``Decimal`` fee rate and the explicit rounding policy.
        split: Tranche ceiling for :attr:`PaymentMode.SPLIT`. Defaults to ₹1,999
            (see the COMPLIANCE section of the README).
        store: Session store for split sessions. Defaults to an in-process
            store, which is fine for tests and single-process apps but loses
            sessions on restart; pass a durable adapter for production.
        currency: ISO-4217 currency used for every order this composer creates.

    Raises:
        TypeError: If ``client`` does not expose the required resources.
    """

    def __init__(
        self,
        client: RazorpayClientProtocol,
        *,
        charges: ChargesConfig | None = None,
        split: SplitConfig | None = None,
        store: object | None = None,
        currency: str = DEFAULT_CURRENCY,
    ) -> None:
        self._client = _ensure_client(client)
        self._charges = charges
        self._split = split if split is not None else SplitConfig()
        self._currency = currency
        self._store = store

    @property
    def client(self) -> RazorpayClientProtocol:
        """The client this composer was configured with, unchanged."""
        return self._client

    def create_order(
        self,
        amount_paise: int,
        *,
        mode: PaymentMode = PaymentMode.EXACT,
        receipt: str | None = None,
        notes: Mapping[str, Any] | None = None,
        currency: str | None = None,
    ) -> OrderResult:
        """Create the Razorpay order for ``amount_paise`` under ``mode``.

        Orders are always created with ``payment_capture=1`` so the resulting
        payment settles without a second API call.

        Args:
            amount_paise: For ``EXACT`` the amount to charge; for
                ``WITH_CHARGES`` the amount the merchant wants to net; for
                ``SPLIT`` the total to collect across tranches.
            mode: Payment mode to apply.
            receipt: Optional receipt string, passed through to Razorpay.
            notes: Optional notes, passed through to Razorpay.
            currency: Overrides the composer's currency for this order.

        Returns:
            The created order, wrapped with the tranchepay context: for
            ``WITH_CHARGES`` the grossed-up ``amount_paise`` plus ``net_paise``
            and ``fee_paise``; for ``SPLIT`` the first tranche and the session id.

        Raises:
            TypeError: If ``amount_paise`` is not an ``int``.
            ValueError: If ``amount_paise`` is not positive.
            PaymentComposeError: As described below.
            PaymentComposeError: If the mode is unknown, if ``WITH_CHARGES`` is
                requested without a ``ChargesConfig``, or if the API response has
                no order id.
        """
        require_paise(amount_paise, "amount_paise")
        try:
            mode = PaymentMode(mode)
        except ValueError as exc:
            msg = f"unknown payment mode: {mode!r}"
            raise PaymentComposeError(msg) from exc
        order_currency = currency or self._currency

        if mode is PaymentMode.EXACT:
            raw = self._create_razorpay_order(amount_paise, order_currency, receipt, notes)
            return self._to_result(raw, mode=mode, amount_paise=amount_paise, receipt=receipt)

        if mode is PaymentMode.WITH_CHARGES:
            if self._charges is None:
                msg = (
                    "PaymentMode.WITH_CHARGES requires a ChargesConfig; pass "
                    "charges=ChargesConfig(fee_rate=Decimal('0.0236')) to PaymentComposer"
                )
                raise PaymentComposeError(msg)
            gross_paise = gross_up(amount_paise, self._charges.fee_rate, self._charges.rounding)
            raw = self._create_razorpay_order(gross_paise, order_currency, receipt, notes)
            return self._to_result(
                raw,
                mode=mode,
                amount_paise=gross_paise,
                receipt=receipt,
                net_paise=amount_paise,
                fee_paise=gross_paise - amount_paise,
            )

        msg = f"unsupported payment mode: {mode!r}"
        raise PaymentComposeError(msg)

    def _create_razorpay_order(
        self,
        amount_paise: int,
        currency: str,
        receipt: str | None,
        notes: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Call ``client.order.create`` with the standard tranchepay payload."""
        data: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "payment_capture": 1,
        }
        if receipt is not None:
            data["receipt"] = receipt
        if notes is not None:
            data["notes"] = dict(notes)
        return self._client.order.create(data)

    def _to_result(
        self,
        raw: Mapping[str, Any],
        *,
        mode: PaymentMode,
        amount_paise: int,
        receipt: str | None = None,
        session_id: str | None = None,
        tranche_index: int | None = None,
        net_paise: int | None = None,
        fee_paise: int = 0,
    ) -> OrderResult:
        """Wrap an API response into an :class:`OrderResult`."""
        order_id = raw.get("id")
        if not isinstance(order_id, str) or not order_id:
            msg = f"Razorpay order response has no usable 'id' field: {dict(raw)!r}"
            raise PaymentComposeError(msg)
        return OrderResult(
            order_id=order_id,
            amount_paise=amount_paise,
            currency=str(raw.get("currency") or self._currency),
            mode=mode,
            session_id=session_id,
            tranche_index=tranche_index,
            net_paise=net_paise,
            fee_paise=fee_paise,
            receipt=str(raw.get("receipt")) if raw.get("receipt") is not None else receipt,
            raw=dict(raw),
        )
