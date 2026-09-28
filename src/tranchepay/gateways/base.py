"""Plumbing shared by gateways that speak a provider's HTTP API directly.

Paytm and PhonePe do not ship first-party Python SDKs, so their adapters build a
:class:`GatewayRequest` - method, URL, headers, JSON body - and hand it to
:meth:`HttpGatewayAdapter.execute`. Building the request is pure and fully
tested; sending it is the one step that has to be written against the provider's
documentation, so it raises ``NotImplementedError`` until a merchant fills it in
with the endpoints and signing rules for their account.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..exceptions import GatewayConfigurationError

if TYPE_CHECKING:  # pragma: no cover - import cycle free typing only
    import httpx

__all__ = ["GatewayRequest", "HttpGatewayAdapter", "require_credential", "rupees_from_paise"]


def rupees_from_paise(amount_paise: int) -> str:
    """Render integer paise as a fixed-point rupee string, e.g. ``"19.99"``.

    Several Indian PSPs take amounts as a decimal rupee string. This uses integer
    arithmetic so no float ever touches money.

    Args:
        amount_paise: Amount in paise.

    Returns:
        The amount as ``"<rupees>.<paise>"`` with two decimals.
    """
    rupees, paise = divmod(amount_paise, 100)
    return f"{rupees}.{paise:02d}"


def require_credential(credentials: Mapping[str, Any], key: str) -> str:
    """Return ``credentials[key]`` as a non-empty string, or raise.

    Args:
        credentials: Mapping supplied to :meth:`GatewayFactory.get_gateway`.
        key: Credential name, e.g. ``"merchant_id"``.

    Returns:
        The credential value, coerced to ``str``.

    Raises:
        GatewayConfigurationError: If the key is missing or empty.
    """
    value = credentials.get(key)
    if value is None or value == "":
        msg = (
            f"missing required credential {key!r}; "
            f"supplied keys: {', '.join(sorted(map(str, credentials))) or 'none'}"
        )
        raise GatewayConfigurationError(msg)
    return str(value)


@dataclass(frozen=True)
class GatewayRequest:
    """A provider API call that has been described but not yet sent.

    Attributes:
        method: HTTP method, upper-case.
        url: Absolute request URL.
        headers: Request headers, already signed where the provider requires it.
        json: JSON request body, if the call has one.
    """

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    json: Mapping[str, Any] | None = None


class HttpGatewayAdapter:
    """Base class for adapters built on a provider's raw HTTP API.

    Args:
        credentials: Provider credentials; the concrete adapter decides which
            keys it needs and validates them with :func:`require_credential`.
        client: Optional pre-configured ``httpx.Client``. Injecting one lets an
            application share connection pooling, proxies and timeouts; when it
            is omitted the adapter creates its own on first use.
        base_url: Overrides the provider's default host. Useful for sandbox
            environments, which every Indian PSP runs on a separate host.
    """

    provider: str = ""
    default_base_url: str = ""

    def __init__(
        self,
        credentials: Mapping[str, Any],
        *,
        client: httpx.Client | None = None,
        base_url: str | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._client = client
        self._owns_client = client is None
        self.base_url = base_url or self.default_base_url

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> GatewayRequest:
        """Describe a call: absolute URL, headers and body, without sending it."""
        return GatewayRequest(
            method=method.upper(),
            url=f"{self.base_url}{path}",
            headers=dict(headers or {}),
            json=dict(payload) if payload is not None else None,
        )

    def execute(self, request: GatewayRequest) -> dict[str, Any]:
        """Send ``request`` and return the decoded JSON body.

        Args:
            request: Call described by :meth:`request`.

        Returns:
            The provider's JSON response.

        Raises:
            NotImplementedError: Always, until this method is implemented for the
                provider. The endpoints, headers and checksum rules have to come
                from the provider's documentation and the merchant's account
                configuration, which tranchepay cannot guess.
        """
        msg = (
            f"{type(self).__name__} does not implement HTTP execution yet: "
            f"{request.method} {request.url} was built but not sent. Implement "
            f"{type(self).__name__}.execute() with {self.provider}'s endpoints and "
            "signing rules to go live."
        )
        raise NotImplementedError(msg)

    def close(self) -> None:
        """Close the HTTP client, if this adapter created it.

        An injected client belongs to the caller and is left open, so a shared
        connection pool is not torn down from under the application.
        """
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def http_client(self) -> httpx.Client:
        """Return the injected client, or lazily create one.

        Raises:
            GatewayConfigurationError: If ``httpx`` is not installed.
        """
        if self._client is not None:
            return self._client
        try:
            import httpx  # noqa: PLC0415 - optional dependency, imported on demand
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on install
            msg = f"the {self.provider} gateway needs httpx: pip install 'tranchepay[http]'"
            raise GatewayConfigurationError(msg) from exc
        self._client = httpx.Client(base_url=self.base_url, timeout=10.0)
        return self._client
