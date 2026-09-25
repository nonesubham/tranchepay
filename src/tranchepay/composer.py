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
from .models import (
    DEFAULT_CURRENCY,
    ChargesConfig,
    OrderResult,
    SplitConfig,
)
from .money import gross_up, require_paise
from .orders import create_razorpay_order, order_result
from .protocol import RazorpayClientProtocol
from .split_flow import SplitFlow
from .store import InMemorySessionStore, SessionStore

__all__ = ["PaymentComposer"]


def _ensure_client(client: object) -> RazorpayClientProtocol:
    """Return ``client`` if it exposes the Razorpay resources tranchepay uses.

    Args:
        client: Candidate client, typed loosely so untyped callers are checked at
            runtime rather than trusted.

    Returns:
        The same object, narrowed to
        :class:`~tranchepay.protocol.RazorpayClientProtocol`.

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


def _ensure_store(store: object) -> SessionStore:
    """Return ``store`` if it implements the :class:`SessionStore` protocol.

    Args:
        store: Candidate store, typed loosely so untyped callers are checked at
            runtime rather than trusted.

    Returns:
        The same object, narrowed to :class:`~tranchepay.store.SessionStore`.

    Raises:
        TypeError: If the object does not implement ``save``/``get``/``update``.
    """
    if not isinstance(store, SessionStore):
        msg = (
            "store must implement the SessionStore protocol (save/get/update), "
            f"not {type(store).__name__}"
        )
        raise TypeError(msg)
    return store


class PaymentComposer:
    """Creates Razorpay orders for the three supported payment modes.

    Args:
        client: A configured ``razorpay.Client`` instance (or any object exposing
            the same ``order``/``payment``/``utility`` resources). Used as-is and
            never mutated.
        charges: Required to use :attr:`PaymentMode.WITH_CHARGES`; carries the
            ``Decimal`` fee rate and the explicit rounding policy.
        split: Tranche ceiling for :attr:`PaymentMode.SPLIT`. Defaults to ₹1,999
            (see the COMPLIANCE section of the README).
        store: Session store for split sessions. Defaults to an in-process store,
            which is fine for tests and single-process apps but loses sessions on
            restart and shares nothing between workers.
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
        store: SessionStore | None = None,
        currency: str = DEFAULT_CURRENCY,
    ) -> None:
        self._client = _ensure_client(client)
        self._charges = charges
        self._split = split if split is not None else SplitConfig()
        self._currency = currency
        self._store = _ensure_store(store) if store is not None else InMemorySessionStore()
        self._flow: SplitFlow | None = None

    @property
    def client(self) -> RazorpayClientProtocol:
        """The client this composer was configured with, unchanged."""
        return self._client

    @property
    def store(self) -> SessionStore:
        """The session store in use, including the default in-process store."""
        return self._store

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
            The created order wrapped with the tranchepay context: the grossed-up
            ``amount_paise`` plus ``net_paise``/``fee_paise`` for
            ``WITH_CHARGES``, or the first tranche and a ``session_id`` for
            ``SPLIT``.

        Raises:
            TypeError: If ``amount_paise`` is not an ``int``.
            ValueError: If ``amount_paise`` is not positive.
            PaymentComposeError: If the mode is unknown, if ``WITH_CHARGES`` is
                requested without a ``ChargesConfig``, or if the API response has
                no order id.
        """
        require_paise(amount_paise, "amount_paise")
        try:
            resolved = PaymentMode(mode)
        except ValueError as exc:
            msg = f"unknown payment mode: {mode!r}"
            raise PaymentComposeError(msg) from exc
        # Dispatch on the value rather than the member so the fallback below stays
        # reachable if the enum ever grows a mode that this method does not handle.
        mode_value = resolved.value
        order_currency = currency or self._currency

        if mode_value == PaymentMode.EXACT.value:
            raw = create_razorpay_order(
                self._client, amount_paise, currency=order_currency, receipt=receipt, notes=notes
            )
            return order_result(
                raw,
                mode=resolved,
                amount_paise=amount_paise,
                currency_default=order_currency,
                receipt=receipt,
            )

        if mode_value == PaymentMode.WITH_CHARGES.value:
            if self._charges is None:
                msg = (
                    "PaymentMode.WITH_CHARGES requires a ChargesConfig; pass "
                    "charges=ChargesConfig(fee_rate=Decimal('0.0236')) to PaymentComposer"
                )
                raise PaymentComposeError(msg)
            gross_paise = gross_up(amount_paise, self._charges.fee_rate, self._charges.rounding)
            raw = create_razorpay_order(
                self._client, gross_paise, currency=order_currency, receipt=receipt, notes=notes
            )
            return order_result(
                raw,
                mode=resolved,
                amount_paise=gross_paise,
                currency_default=order_currency,
                receipt=receipt,
                net_paise=amount_paise,
                fee_paise=gross_paise - amount_paise,
            )

        if mode_value == PaymentMode.SPLIT.value:
            return self._split_flow().start(
                amount_paise, receipt=receipt, notes=notes, currency=currency
            )

        msg = f"unsupported payment mode: {mode!r}"  # pragma: no cover - defensive
        raise PaymentComposeError(msg)

    def verify_and_advance(
        self,
        session_id: str,
        order_id: str,
        payment_id: str,
        signature: str,
    ) -> OrderResult | None:
        """Verify one tranche payment and create the next order, if any.

        Idempotent and safe against retries and concurrent callbacks: a replay of
        an already-verified tranche advances nothing and returns the current
        pending order.

        Args:
            session_id: Session id returned with the first tranche order.
            order_id: Order the customer paid (``razorpay_order_id``).
            payment_id: Payment id from checkout (``razorpay_payment_id``).
            signature: Signature from checkout (``razorpay_signature``).

        Returns:
            The next tranche order for checkout, or ``None`` when the payment
            completed the session.

        Raises:
            VerificationError: If the signature is invalid or the payment is not
                captured.
            AmountMismatchError: If the captured amount is not the tranche amount.
            SessionNotFoundError: If the session id is unknown to the store.
            SessionStateError: If the order is not the current pending tranche's
                order, or the session is already closed.
        """
        return self._split_flow().verify_and_advance(session_id, order_id, payment_id, signature)

    def _split_flow(self) -> SplitFlow:
        """Return the split flow for this composer, building it on first use."""
        if self._flow is None:
            self._flow = SplitFlow(
                self._client, self._store, config=self._split, currency=self._currency
            )
        return self._flow
