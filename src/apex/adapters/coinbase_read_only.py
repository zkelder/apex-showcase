"""Secret-resolving, JWT-authenticated Coinbase GET-only adapter.

No instance of this adapter is constructed by the Operator API. A separately
approved operator workflow must inject exact reference mappings and approval.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import ssl
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from apex.application.coinbase_read_only import (
    CoinbaseCredentialMaterial,
    CoinbaseCredentialResolver,
    CoinbasePrivateResponse,
)
from apex.domain.coinbase_read_only import (
    COINBASE_PRIVATE_HOST,
    MAX_PRIVATE_RESPONSE_BYTES,
    CoinbaseEndpointClass,
    CoinbaseReadOnlyError,
    validate_coinbase_private_read_request,
)
from apex.domain.live_readiness import CredentialReference, SecretBackend


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _credential_payload(raw: str) -> CoinbaseCredentialMaterial:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID") from error
    if not isinstance(value, dict) or set(value) != {"key_name", "private_key"}:
        raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID")
    key_name = value["key_name"]
    private_key = value["private_key"]
    if not isinstance(key_name, str) or not isinstance(private_key, str):
        raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID")
    return CoinbaseCredentialMaterial(key_name=key_name, private_key=private_key)


class EnvironmentReferenceResolver:
    def __init__(
        self,
        *,
        reference_environment_names: Mapping[str, str],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._names = dict(reference_environment_names)
        self._environment = os.environ if environment is None else environment

    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial:
        name = self._names.get(reference.backend_reference_id)
        if name is None or name not in self._environment:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID")
        return _credential_payload(self._environment[name])


class LocalIgnoredFileReferenceResolver:
    def __init__(
        self,
        *,
        ignored_root: Path,
        reference_file_names: Mapping[str, str],
    ) -> None:
        self._root = ignored_root.resolve()
        self._names = dict(reference_file_names)

    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial:
        name = self._names.get(reference.backend_reference_id)
        if name is None or Path(name).name != name:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID")
        path = (self._root / name).resolve()
        if path.parent != self._root or not path.is_file() or path.stat().st_mode & 0o077:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID")
        return _credential_payload(path.read_text(encoding="utf-8"))


class OperatingSystemSecretReferenceResolver:
    def __init__(self, *, loader: Callable[[str], str]) -> None:
        self._loader = loader

    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial:
        try:
            return _credential_payload(self._loader(reference.backend_reference_id))
        except Exception as error:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID") from error


class _BackendResolver(Protocol):
    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial: ...


class CompositeCoinbaseCredentialResolver(CoinbaseCredentialResolver):
    def __init__(self, resolvers: Mapping[SecretBackend, _BackendResolver]) -> None:
        self._resolvers = dict(resolvers)

    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial:
        resolver = self._resolvers.get(reference.secret_backend)
        if resolver is None:
            raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_BACKEND_UNAVAILABLE")
        return resolver.resolve(reference)


def build_coinbase_rest_jwt(
    *, credential: CoinbaseCredentialMaterial, path: str, issued_at: datetime
) -> str:
    """Build Coinbase's short-lived ES256 request-bound bearer JWT in memory."""

    validate_coinbase_private_read_request(
        method="GET",
        host=COINBASE_PRIVATE_HOST,
        path=path,
        endpoint_class=(
            CoinbaseEndpointClass.PRODUCT_RESTRICTIONS_READ
            if path.startswith("/api/v3/brokerage/products/")
            else {
                "/api/v3/brokerage/accounts": CoinbaseEndpointClass.ACCOUNT_STATUS_READ,
                "/api/v3/brokerage/key_permissions": CoinbaseEndpointClass.PERMISSIONS_READ,
                "/api/v3/brokerage/transaction_summary": CoinbaseEndpointClass.FEES_READ,
            }.get(path, CoinbaseEndpointClass.ACCOUNT_STATUS_READ)
        ),
    )
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
        "uri": f"GET {COINBASE_PRIVATE_HOST}{path}",
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
        raise CoinbaseReadOnlyError("COINBASE_CREDENTIAL_REFERENCE_INVALID") from error
    signature_r, signature_s = decode_dss_signature(der_signature)
    signature = signature_r.to_bytes(32, "big") + signature_s.to_bytes(32, "big")
    return f"{encoded_header}.{encoded_payload}.{_b64(signature)}"


class CoinbasePrivateReadOnlyClient:
    """One bounded GET per call, with no method accepting a body or mutation."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl.create_default_context() if ssl_context is None else ssl_context

    def get(
        self,
        *,
        credential: CoinbaseCredentialMaterial,
        endpoint_class: CoinbaseEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> CoinbasePrivateResponse:
        validate_coinbase_private_read_request(
            method="GET",
            host=COINBASE_PRIVATE_HOST,
            path=path,
            endpoint_class=endpoint_class,
        )
        if not 1 <= timeout_seconds <= 30:
            raise CoinbaseReadOnlyError("COINBASE_PRIVATE_TIMEOUT_INVALID")
        bearer = build_coinbase_rest_jwt(
            credential=credential,
            path=path,
            issued_at=requested_at,
        )
        connection = http.client.HTTPSConnection(
            COINBASE_PRIVATE_HOST,
            timeout=timeout_seconds,
            context=self._ssl_context,
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {bearer}",
                    "User-Agent": "Apex/coinbase-private-read-only-v1",
                },
            )
            response = connection.getresponse()
            payload = response.read(MAX_PRIVATE_RESPONSE_BYTES + 1)
            if len(payload) > MAX_PRIVATE_RESPONSE_BYTES:
                raise CoinbaseReadOnlyError("COINBASE_PRIVATE_RESPONSE_BYTES_EXCEEDED")
            return CoinbasePrivateResponse(
                status=response.status,
                payload=payload,
                completed_at=datetime.now(tz=UTC),
            )
        finally:
            connection.close()


class DeterministicCoinbasePrivateReadFixture:
    """Explicit fixture transport; it never has network or provider authority."""

    def __init__(
        self,
        *,
        responses: Mapping[str, tuple[int, bytes]],
        completed_at: datetime,
        failure: Exception | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._completed_at = completed_at
        self._failure = failure
        self.requests: list[tuple[str, str]] = []

    def get(
        self,
        *,
        credential: CoinbaseCredentialMaterial,
        endpoint_class: CoinbaseEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> CoinbasePrivateResponse:
        del credential, timeout_seconds, requested_at
        validate_coinbase_private_read_request(
            method="GET", host=COINBASE_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
        )
        self.requests.append(("GET", path))
        if self._failure is not None:
            raise self._failure
        status, payload = self._responses[path]
        return CoinbasePrivateResponse(
            status=status, payload=payload, completed_at=self._completed_at
        )


class StaticFixtureCredentialResolver:
    def __init__(self, material: CoinbaseCredentialMaterial) -> None:
        self._material = material

    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial:
        del reference
        return self._material


class UnavailableCoinbaseCredentialResolver:
    """Default API wiring: reference metadata may exist, but no secret is resolved."""

    def resolve(self, reference: CredentialReference) -> CoinbaseCredentialMaterial:
        del reference
        raise CoinbaseReadOnlyError("COINBASE_PRIVATE_ADAPTER_NOT_CONFIGURED")


class NetworkDisabledCoinbasePrivateTransport:
    """Default API wiring with no network implementation behind the route."""

    def get(
        self,
        *,
        credential: CoinbaseCredentialMaterial,
        endpoint_class: CoinbaseEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> CoinbasePrivateResponse:
        del credential, endpoint_class, path, timeout_seconds, requested_at
        raise CoinbaseReadOnlyError("COINBASE_PRIVATE_NETWORK_DISABLED")


def credential_reference_fingerprint(reference: CredentialReference) -> str:
    """Non-secret diagnostic identity for reference wiring only."""

    return hashlib.sha256(
        f"{reference.reference_id}:{reference.version}:{reference.secret_backend.value}".encode()
    ).hexdigest()
