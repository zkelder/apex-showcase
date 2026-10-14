"""Exact authenticated Coinbase preview client; never an order client.

No default application or API construction path instantiates this adapter.
An operator workflow must first retain an exact preview approval and inject the
credential material mapped to its opaque reference.  The sole HTTP method here
is the documented non-ordering preview operation.
"""

from __future__ import annotations

import base64
import http.client
import json
import secrets
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from apex.application.coinbase_read_only import CoinbaseCredentialMaterial
from apex.application.live_preflight import CoinbasePreviewTransport
from apex.domain.live_preflight import (
    COINBASE_PREVIEW_HOST,
    COINBASE_PREVIEW_PATH,
    LivePreflightError,
)

MAX_PREVIEW_RESPONSE_BYTES = 1_000_000


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def build_coinbase_preview_jwt(
    *, credential: CoinbaseCredentialMaterial, issued_at: datetime
) -> str:
    """Build a short-lived POST-preview-bound ES256 bearer JWT in memory."""

    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise LivePreflightError("COINBASE_PREVIEW_JWT_TIME_INVALID")
    now = int(issued_at.astimezone(UTC).timestamp())
    header = {
        "alg": "ES256",
        "kid": credential.key_name,
        "nonce": secrets.token_hex(16),
        "typ": "JWT",
    }
    payload = {
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "sub": credential.key_name,
        "uri": f"POST {COINBASE_PREVIEW_HOST}{COINBASE_PREVIEW_PATH}",
    }
    encoded_header = _b64(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_payload = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    try:
        private_key = serialization.load_pem_private_key(
            credential.private_key.encode(), password=None
        )
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ValueError
        der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    except Exception as error:
        raise LivePreflightError("COINBASE_PREVIEW_CREDENTIAL_INVALID") from error
    signature_r, signature_s = decode_dss_signature(der_signature)
    signature = signature_r.to_bytes(32, "big") + signature_s.to_bytes(32, "big")
    return f"{encoded_header}.{encoded_payload}.{_b64(signature)}"


class ApprovedCoinbasePreviewClient(CoinbasePreviewTransport):
    """One exact preview request per application invocation, with no order methods."""

    def __init__(
        self,
        *,
        credential_material: Mapping[str, CoinbaseCredentialMaterial],
        issued_at: datetime,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._credentials = dict(credential_material)
        self._issued_at = issued_at
        self._ssl_context = ssl.create_default_context() if ssl_context is None else ssl_context

    def preview(
        self,
        *,
        method: str,
        host: str,
        path: str,
        body: Mapping[str, object],
        credential_reference_id: str,
        timeout_seconds: int,
    ) -> tuple[int, Mapping[str, object]]:
        if (
            method != "POST"
            or host != COINBASE_PREVIEW_HOST
            or path != COINBASE_PREVIEW_PATH
            or not 1 <= timeout_seconds <= 30
        ):
            raise LivePreflightError("COINBASE_PREVIEW_REQUEST_NOT_ALLOWLISTED")
        if set(body) != {"product_id", "side", "order_configuration"}:
            raise LivePreflightError("COINBASE_PREVIEW_BODY_NOT_ALLOWLISTED")
        credential = self._credentials.get(credential_reference_id)
        if credential is None:
            raise LivePreflightError("COINBASE_PREVIEW_CREDENTIAL_REFERENCE_UNAVAILABLE")
        bearer = build_coinbase_preview_jwt(
            credential=credential,
            issued_at=self._issued_at,
        )
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        connection = http.client.HTTPSConnection(
            COINBASE_PREVIEW_HOST,
            timeout=timeout_seconds,
            context=self._ssl_context,
        )
        try:
            connection.request(
                "POST",
                COINBASE_PREVIEW_PATH,
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "User-Agent": "Apex/coinbase-provider-preview-v1",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_PREVIEW_RESPONSE_BYTES + 1)
            if len(raw) > MAX_PREVIEW_RESPONSE_BYTES:
                raise LivePreflightError("COINBASE_PREVIEW_RESPONSE_BYTES_EXCEEDED")
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as error:
                raise LivePreflightError("COINBASE_PREVIEW_RESPONSE_INVALID") from error
            if not isinstance(decoded, dict):
                raise LivePreflightError("COINBASE_PREVIEW_RESPONSE_INVALID")
            return response.status, decoded
        finally:
            connection.close()
