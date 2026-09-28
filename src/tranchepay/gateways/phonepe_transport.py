"""HTTP transport for PhonePe: JSON calls and OAuth token management.

PhonePe's Standard Checkout v2 authenticates with an ``O-Bearer`` access token
minted from the client id and secret at ``/v1/oauth/token``. Tokens last an hour,
so they are cached on the adapter and refreshed a minute before expiry.

This module knows nothing about payments; it only sends calls and decodes bodies.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..exceptions import GatewayConfigurationError, PaymentComposeError
from .base import GatewayRequest, HttpGatewayAdapter

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

__all__ = ["PhonePeTransport"]

OAUTH_PATH = "/v1/oauth/token"
TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class PhonePeTransport(HttpGatewayAdapter):
    """Sends PhonePe HTTP calls and caches the OAuth access token."""

    provider = "phonepe"
    default_base_url = "https://api-preprod.phonepe.com/apis/pg-sandbox"

    def __init__(
        self,
        credentials: Mapping[str, Any],
        *,
        client: httpx.Client | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(credentials, client=client, base_url=base_url)
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform one HTTP call and decode its JSON body.

        Args:
            method: HTTP method.
            url: Absolute request URL.
            headers: Request headers.
            json: JSON body, for the calls that carry one.
            data: Form body, used by the OAuth token exchange.

        Returns:
            The decoded JSON object, or ``{}`` for a 204/empty response.

        Raises:
            PaymentComposeError: If the call fails at the transport level, or the
                response is not a JSON object.
        """
        client = self.http_client()
        try:
            response = client.request(method, url, headers=dict(headers), json=json, data=data)
        except Exception as exc:
            msg = f"PhonePe request {method} {url} failed: {exc}"
            raise PaymentComposeError(msg) from exc
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            msg = (
                f"PhonePe returned HTTP {response.status_code} with a non-JSON body: "
                f"{response.text[:200]!r}"
            )
            raise PaymentComposeError(msg) from exc
        if not isinstance(payload, dict):
            msg = f"PhonePe returned HTTP {response.status_code} with a non-object JSON body"
            raise PaymentComposeError(msg)
        return payload

    def execute(self, request: GatewayRequest) -> dict[str, Any]:
        """Send ``request`` through httpx and return the decoded JSON body."""
        return self._send(request.method, request.url, headers=request.headers, json=request.json)

    def _access_token(self) -> str:
        """Return a cached ``O-Bearer`` token, minting one when it has expired.

        Raises:
            GatewayConfigurationError: If PhonePe refuses the client credentials.
        """
        now = time.monotonic()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        raw = self._send(
            "POST",
            f"{self.base_url}{OAUTH_PATH}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": str(self._credentials.get("merchant_id") or ""),
                "client_secret": str(self._credentials.get("salt_key") or ""),
                "grant_type": "client_credentials",
                "client_version": str(self._credentials.get("client_version") or "1"),
            },
        )
        token = raw.get("access_token")
        if not isinstance(token, str) or not token:
            code = raw.get("code") or raw.get("message") or raw
            msg = f"PhonePe rejected the client credentials: {code}"
            raise GatewayConfigurationError(msg)
        self._token = token
        expires_in = raw.get("expires_in")
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else 900.0
        self._token_expires_at = now + max(ttl - TOKEN_REFRESH_MARGIN_SECONDS, 0.0)
        return token

    def oauth_headers(self) -> dict[str, str]:
        """Return the headers for an authenticated v2 call."""
        return {
            "Content-Type": "application/json",
            "Authorization": f"O-Bearer {self._access_token()}",
        }
