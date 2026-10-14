"""Secret-resolving, RSA-PSS-authenticated Kalshi GET-only adapter.

No instance of this adapter is constructed by the Operator API. A separately
approved operator workflow must inject exact reference mappings and approval.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import ssl
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from apex.application.kalshi_read_only import (
    KalshiCredentialMaterial,
    KalshiCredentialResolver,
    KalshiPrivateResponse,
)
from apex.domain.kalshi_read_only import (
    KALSHI_PRIVATE_HOST,
    MAX_PRIVATE_RESPONSE_BYTES,
    KalshiEndpointClass,
    KalshiReadOnlyError,
    validate_kalshi_private_read_request,
)
from apex.domain.live_readiness import CredentialReference, SecretBackend


def _credential_payload(raw: str) -> KalshiCredentialMaterial:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID") from error
    if not isinstance(value, dict) or set(value) != {"key_id", "private_key"}:
        raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID")
    key_id = value["key_id"]
    private_key = value["private_key"]
    if not isinstance(key_id, str) or not isinstance(private_key, str):
        raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID")
    return KalshiCredentialMaterial(key_id=key_id, private_key=private_key)


class EnvironmentReferenceResolver:
    def __init__(
        self,
        *,
        reference_environment_names: Mapping[str, str],
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._names = dict(reference_environment_names)
        self._environment = os.environ if environment is None else environment

    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial:
        name = self._names.get(reference.backend_reference_id)
        if name is None or name not in self._environment:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID")
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

    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial:
        name = self._names.get(reference.backend_reference_id)
        if name is None or Path(name).name != name:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID")
        path = (self._root / name).resolve()
        if path.parent != self._root or not path.is_file() or path.stat().st_mode & 0o077:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID")
        return _credential_payload(path.read_text(encoding="utf-8"))


class OperatingSystemSecretReferenceResolver:
    def __init__(self, *, loader: Callable[[str], str]) -> None:
        self._loader = loader

    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial:
        try:
            return _credential_payload(self._loader(reference.backend_reference_id))
        except Exception as error:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID") from error


class _BackendResolver(Protocol):
    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial: ...


class CompositeKalshiCredentialResolver(KalshiCredentialResolver):
    def __init__(self, resolvers: Mapping[SecretBackend, _BackendResolver]) -> None:
        self._resolvers = dict(resolvers)

    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial:
        resolver = self._resolvers.get(reference.secret_backend)
        if resolver is None:
            raise KalshiReadOnlyError("KALSHI_CREDENTIAL_BACKEND_UNAVAILABLE")
        return resolver.resolve(reference)


def build_kalshi_request_signature(
    *, credential: KalshiCredentialMaterial, path: str, issued_at: datetime
) -> tuple[str, str]:
    """Build Kalshi's request-bound millisecond timestamp and RSA-PSS signature."""

    path_without_query = path.split("?", 1)[0]
    validate_kalshi_private_read_request(
        method="GET",
        host=KALSHI_PRIVATE_HOST,
        path=path,
        endpoint_class={
            "/trade-api/v2/communications/id": KalshiEndpointClass.ACCOUNT_IDENTITY_READ,
            "/trade-api/v2/portfolio/balance?subaccount=0": KalshiEndpointClass.BALANCE_READ,
            "/trade-api/v2/api_keys": KalshiEndpointClass.PERMISSIONS_READ,
            "/trade-api/v2/account/limits": KalshiEndpointClass.ACCOUNT_TIER_READ,
        }.get(path, KalshiEndpointClass.ACCOUNT_IDENTITY_READ),
    )
    timestamp = str(int(issued_at.astimezone(UTC).timestamp() * 1000))
    signing_input = f"{timestamp}GET{path_without_query}".encode()
    try:
        private_key = serialization.load_pem_private_key(
            credential.private_key.encode(), password=None
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError
        signature = private_key.sign(
            signing_input,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    except Exception as error:
        raise KalshiReadOnlyError("KALSHI_CREDENTIAL_REFERENCE_INVALID") from error
    return timestamp, base64.b64encode(signature).decode("ascii")


class KalshiPrivateReadOnlyClient:
    """One bounded GET per call, with no method accepting a body or mutation."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl.create_default_context() if ssl_context is None else ssl_context

    def get(
        self,
        *,
        credential: KalshiCredentialMaterial,
        endpoint_class: KalshiEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> KalshiPrivateResponse:
        validate_kalshi_private_read_request(
            method="GET",
            host=KALSHI_PRIVATE_HOST,
            path=path,
            endpoint_class=endpoint_class,
        )
        if not 1 <= timeout_seconds <= 30:
            raise KalshiReadOnlyError("KALSHI_PRIVATE_TIMEOUT_INVALID")
        timestamp, signature = build_kalshi_request_signature(
            credential=credential,
            path=path,
            issued_at=requested_at,
        )
        connection = http.client.HTTPSConnection(
            KALSHI_PRIVATE_HOST,
            timeout=timeout_seconds,
            context=self._ssl_context,
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "application/json",
                    "KALSHI-ACCESS-KEY": credential.key_id,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                    "User-Agent": "Apex/kalshi-private-read-only-v1",
                },
            )
            response = connection.getresponse()
            payload = response.read(MAX_PRIVATE_RESPONSE_BYTES + 1)
            if len(payload) > MAX_PRIVATE_RESPONSE_BYTES:
                raise KalshiReadOnlyError("KALSHI_PRIVATE_RESPONSE_BYTES_EXCEEDED")
            return KalshiPrivateResponse(
                status=response.status,
                payload=payload,
                completed_at=datetime.now(tz=UTC),
            )
        finally:
            connection.close()


class DeterministicKalshiPrivateReadFixture:
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
        credential: KalshiCredentialMaterial,
        endpoint_class: KalshiEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> KalshiPrivateResponse:
        del credential, timeout_seconds, requested_at
        validate_kalshi_private_read_request(
            method="GET", host=KALSHI_PRIVATE_HOST, path=path, endpoint_class=endpoint_class
        )
        self.requests.append(("GET", path))
        if self._failure is not None:
            raise self._failure
        status, payload = self._responses[path]
        return KalshiPrivateResponse(
            status=status, payload=payload, completed_at=self._completed_at
        )


class StaticFixtureCredentialResolver:
    def __init__(self, material: KalshiCredentialMaterial) -> None:
        self._material = material

    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial:
        del reference
        return self._material


class UnavailableKalshiCredentialResolver:
    """Default API wiring: reference metadata may exist, but no secret is resolved."""

    def resolve(self, reference: CredentialReference) -> KalshiCredentialMaterial:
        del reference
        raise KalshiReadOnlyError("KALSHI_PRIVATE_ADAPTER_NOT_CONFIGURED")


class NetworkDisabledKalshiPrivateTransport:
    """Default API wiring with no network implementation behind the route."""

    def get(
        self,
        *,
        credential: KalshiCredentialMaterial,
        endpoint_class: KalshiEndpointClass,
        path: str,
        timeout_seconds: int,
        requested_at: datetime,
    ) -> KalshiPrivateResponse:
        del credential, endpoint_class, path, timeout_seconds, requested_at
        raise KalshiReadOnlyError("KALSHI_PRIVATE_NETWORK_DISABLED")


def credential_reference_fingerprint(reference: CredentialReference) -> str:
    """Non-secret diagnostic identity for reference wiring only."""

    return hashlib.sha256(
        f"{reference.reference_id}:{reference.version}:{reference.secret_backend.value}".encode()
    ).hexdigest()
