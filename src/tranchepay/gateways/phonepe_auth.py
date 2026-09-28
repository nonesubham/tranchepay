"""PhonePe signing, checksums and environment resolution.

PhonePe runs two integration styles, and both are kept honest here:

* **Standard Checkout v2 (OAuth).** A client id and secret are exchanged for a
  short-lived ``O-Bearer`` access token; orders are created with a plain JSON
  body. Current PhonePe test accounts use this flow.
* **Classic PG (X-VERIFY).** The JSON body is base64-encoded and signed with
  ``SHA256(base64_payload + endpoint + salt_key) + "###" + salt_index``, and that
  value travels in the ``X-VERIFY`` header. The same checksum, without the
  endpoint, authenticates webhook and callback payloads.

Everything in this module is pure - no network, no stored state - so it can be
unit-tested without a merchant account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from ..exceptions import GatewayConfigurationError

__all__ = [
    "PHONEPE_BASE_URLS",
    "api_merchant_id",
    "encode_payload",
    "resolve_base_url",
    "verify_checksum",
    "x_verify_token",
]

PHONEPE_BASE_URLS: dict[str, str] = {
    "preprod": "https://api-preprod.phonepe.com/apis/pg-sandbox",
    "sandbox": "https://api-preprod.phonepe.com/apis/pg-sandbox",
    "test": "https://api-preprod.phonepe.com/apis/pg-sandbox",
    "production": "https://api.phonepe.com/apis/hermes",
    "prod": "https://api.phonepe.com/apis/hermes",
    "live": "https://api.phonepe.com/apis/hermes",
}


def resolve_base_url(env: str, override: str | None = None) -> str:
    """Return the PhonePe host for ``env``, or ``override`` when given.

    Args:
        env: Environment name, matched case-insensitively: ``preprod``/``sandbox``/
            ``test`` for the sandbox, ``production``/``prod``/``live`` for live.
        override: Explicit base URL that wins over the named environment.

    Returns:
        The base URL, without a trailing slash.

    Raises:
        GatewayConfigurationError: If ``env`` names no known environment.
    """
    if override:
        return override.rstrip("/")
    try:
        return PHONEPE_BASE_URLS[env.strip().lower()]
    except KeyError as exc:
        known = ", ".join(sorted(PHONEPE_BASE_URLS))
        msg = f"unknown PhonePe environment {env!r}; expected one of: {known}"
        raise GatewayConfigurationError(msg) from exc


def api_merchant_id(merchant_id: str) -> str:
    """Return the API ``merchantId`` for a PhonePe client id.

    PhonePe issues client ids as ``<merchantId>_<issued-at>``; the API expects the
    short form. Ids without a suffix are returned unchanged.
    """
    return merchant_id.split("_", 1)[0] or merchant_id


def encode_payload(payload: Mapping[str, Any]) -> str:
    """Base64-encode ``payload`` exactly as PhonePe's classic flow expects.

    The JSON is canonical (compact separators, sorted keys) so the string that is
    signed is byte-for-byte the string that is sent.
    """
    canonical = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)
    return base64.b64encode(canonical.encode()).decode()


def x_verify_token(payload: str, endpoint: str, salt_key: str, salt_index: str) -> str:
    """Build the classic ``X-VERIFY`` value for a signed request.

    Args:
        payload: The base64 payload (for a request) or raw body (for a callback).
        endpoint: Endpoint path included in the digest, e.g. ``/pg/v1/pay``. Pass
            an empty string to checksum a webhook payload.
        salt_key: Merchant salt key / client secret.
        salt_index: Salt index advertised by PhonePe, usually ``"1"``.

    Returns:
        ``"<sha256-hex>###<salt_index>"``.
    """
    digest = hashlib.sha256(f"{payload}{endpoint}{salt_key}".encode()).hexdigest()
    return f"{digest}###{salt_index}"


def verify_checksum(payload: str, signature: str, salt_key: str) -> bool:
    """Whether ``signature`` is the PhonePe checksum of ``payload``.

    ``signature`` may be the full ``X-VERIFY`` value (``<digest>###<index>``) or a
    bare hex digest; the comparison is constant-time.

    Args:
        payload: The exact payload PhonePe signed - the raw callback body or the
            base64 ``response`` string, not a re-serialized copy.
        signature: Value of the ``X-VERIFY`` header.
        salt_key: Merchant salt key / client secret.

    Returns:
        ``True`` when the digest matches.
    """
    provided = signature.split("###", 1)[0].strip()
    if not provided:
        return False
    expected = hashlib.sha256(f"{payload}{salt_key}".encode()).hexdigest()
    return hmac.compare_digest(expected, provided)
