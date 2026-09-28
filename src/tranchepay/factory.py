"""The gateway registry: tranchepay's answer to JDBC's ``DriverManager``.

The rest of the library never names a provider. It asks the factory for a
:class:`~tranchepay.protocol.PaymentGateway` by name and drives the returned
adapter, exactly as JDBC code asks ``DriverManager`` for a ``Connection`` without
knowing which driver answers. New providers - including a merchant's own - are
added with :meth:`GatewayFactory.register_gateway`; nothing in the composer, the
split flow or the recovery code has to change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from .exceptions import UnsupportedGatewayError
from .gateways.paytm_adapter import PaytmAdapter
from .gateways.phonepe_adapter import PhonePeAdapter
from .gateways.razorpay_adapter import RazorpayAdapter
from .protocol import PaymentGateway

__all__ = ["GatewayFactory"]

GatewayBuilder = Callable[[Mapping[str, Any]], PaymentGateway]


class GatewayFactory:
    """Builds :class:`PaymentGateway` adapters from a provider name.

    Providers are matched case-insensitively. The three shipped names are
    ``"razorpay"``, ``"paytm"`` and ``"phonepe"``; see
    :meth:`available_gateways`.
    """

    _builders: ClassVar[dict[str, GatewayBuilder]] = {}

    @classmethod
    def register_gateway(cls, name: str, adapter: GatewayBuilder) -> None:
        """Register ``adapter`` as the builder for ``name``.

        This is the extension point behind :meth:`get_gateway`: a custom
        ``PaymentGateway`` is added by registering any callable that accepts the
        credentials mapping and returns the adapter. An adapter class whose
        ``__init__`` takes the credentials mapping can be registered directly.

        Args:
            name: Provider name to register, matched case-insensitively.
            adapter: Callable taking the credentials mapping and returning a
                :class:`~tranchepay.protocol.PaymentGateway`.

        Raises:
            ValueError: If ``name`` is empty.
        """
        normalised = name.strip().lower()
        if not normalised:
            msg = "gateway name must not be empty"
            raise ValueError(msg)
        cls._builders[normalised] = adapter

    @classmethod
    def get_gateway(cls, provider: str, credentials: Mapping[str, Any]) -> PaymentGateway:
        """Return the adapter registered for ``provider``.

        Args:
            provider: Provider name, e.g. ``"razorpay"``, matched
                case-insensitively.
            credentials: Provider credentials, passed to the adapter builder. The
                keys each adapter needs are documented on the adapter itself.

        Returns:
            A ready-to-use :class:`~tranchepay.protocol.PaymentGateway`. Building
            an adapter performs no I/O; the first API call happens when the
            composer or the split flow uses it.

        Raises:
            UnsupportedGatewayError: If no adapter is registered under
                ``provider``.
            GatewayConfigurationError: If the adapter's required credentials are
                missing, or its optional dependency is not installed.
        """
        try:
            builder = cls._builders[provider.strip().lower()]
        except KeyError as exc:
            known = ", ".join(cls.available_gateways()) or "none"
            msg = f"unsupported payment gateway {provider!r}; registered gateways: {known}"
            raise UnsupportedGatewayError(msg) from exc
        return builder(credentials)

    @classmethod
    def available_gateways(cls) -> tuple[str, ...]:
        """Return the registered provider names, sorted."""
        return tuple(sorted(cls._builders))


GatewayFactory.register_gateway("razorpay", RazorpayAdapter.from_credentials)
GatewayFactory.register_gateway("paytm", PaytmAdapter)
GatewayFactory.register_gateway("phonepe", PhonePeAdapter)
