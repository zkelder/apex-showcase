"""Narrow, GET-only Alpaca market-data evidence adapter.

The adapter deliberately does not import any Apex financial repository.  It
constructs requests only from the reviewed endpoint policy below, never accepts
an arbitrary URL, never follows redirects, and retains only sanitized request
metadata plus raw response bytes in the managed local evidence root.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlencode

from apex.application.real_securities_data import SecuritiesProviderPage
from apex.domain.real_securities_data import RealSecuritiesDataError
from apex.domain.values import canonical_json_bytes, canonical_timestamp, content_digest

ALPACA_ENDPOINT_POLICY_ID = "ALPACA_MARKET_DATA_V2_IEX_PAPER_ASSET_AND_V1BETA1_OPTIONS_GET_ONLY_V3"
ALPACA_ENDPOINT_POLICY_VERSION = "2026-08-08"
ALPACA_MARKET_DATA_HOST = "data.alpaca.markets"
# Alpaca paper credentials authorize the current asset identity list on the
# paper trading host.  This is deliberately the sole non-market-data host in
# the reviewed policy, and the endpoint remains a GET-only current snapshot.
ALPACA_ASSET_MASTER_HOST = "paper-api.alpaca.markets"


@dataclass(frozen=True, slots=True)
class AlpacaEndpoint:
    identity: str
    host: str
    path: str
    query_names: tuple[str, ...]
    paginated: bool
    # Named ``{placeholder}`` segments in ``path`` that must be supplied via
    # ``fetch_pages(path_params=...)``. Empty for every endpoint that takes
    # its identity solely from the query string.
    path_params: tuple[str, ...] = ()


ALPACA_ENDPOINTS = MappingProxyType(
    {
        "historical_daily_bars": AlpacaEndpoint(
            "historical_daily_bars",
            ALPACA_MARKET_DATA_HOST,
            "/v2/stocks/bars",
            (
                "symbols",
                "timeframe",
                "start",
                "end",
                "feed",
                "adjustment",
                "limit",
                "sort",
                "page_token",
            ),
            True,
        ),
        "latest_snapshots": AlpacaEndpoint(
            "latest_snapshots",
            ALPACA_MARKET_DATA_HOST,
            "/v2/stocks/snapshots",
            ("symbols", "feed", "currency"),
            False,
        ),
        "corporate_actions": AlpacaEndpoint(
            "corporate_actions",
            ALPACA_MARKET_DATA_HOST,
            "/v1/corporate-actions",
            ("symbols", "types", "start", "end", "limit", "sort", "page_token", "region"),
            True,
        ),
        # Alpaca documents this as the master asset list.  It is an identity
        # read only; no account, portfolio, order, or position path is present.
        "asset_master": AlpacaEndpoint(
            "asset_master",
            ALPACA_ASSET_MASTER_HOST,
            "/v2/assets",
            ("status", "asset_class", "exchange"),
            False,
        ),
        # Genuine current option-chain quote/greeks/IV read for one
        # underlying at a time -- GET-only, research evidence, no order or
        # account path. See docs.alpaca.markets/us/reference/optionchain.
        "option_chain_snapshots": AlpacaEndpoint(
            "option_chain_snapshots",
            ALPACA_MARKET_DATA_HOST,
            "/v1beta1/options/snapshots/{underlying_symbol}",
            (
                "feed",
                "limit",
                "updated_since",
                "page_token",
                "type",
                "strike_price_gte",
                "strike_price_lte",
                "expiration_date",
                "expiration_date_gte",
                "expiration_date_lte",
                "root_symbol",
            ),
            True,
            ("underlying_symbol",),
        ),
    }
)
ALPACA_ENDPOINT_POLICY_HASH = content_digest(
    {
        "identity": ALPACA_ENDPOINT_POLICY_ID,
        "version": ALPACA_ENDPOINT_POLICY_VERSION,
        "method": "GET",
        "endpoints": tuple(
            {
                "identity": item.identity,
                "host": item.host,
                "path": item.path,
                "query_names": item.query_names,
                "paginated": item.paginated,
                "path_params": item.path_params,
            }
            for item in ALPACA_ENDPOINTS.values()
        ),
    }
)


class SecuritiesProviderHttpError(RuntimeError):
    """A sanitized bounded provider transport error."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class SecuritiesProviderHttpTransport(Protocol):
    def request(
        self,
        *,
        host: str,
        target: str,
        headers: Mapping[str, str],
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> ProviderHttpResponse: ...


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    received_at: datetime


class HttpsProviderTransport:
    """TLS-verified one-shot HTTPS transport with no redirect machinery."""

    def request(
        self,
        *,
        host: str,
        target: str,
        headers: Mapping[str, str],
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> ProviderHttpResponse:
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            host,
            timeout=timeout_seconds,
            context=context,
        )
        try:
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            declared = response.getheader("Content-Length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > maximum_response_bytes
            ):
                raise SecuritiesProviderHttpError("PROVIDER_RESPONSE_BYTES_EXCEEDED")
            body = response.read(maximum_response_bytes + 1)
            if len(body) > maximum_response_bytes:
                raise SecuritiesProviderHttpError("PROVIDER_RESPONSE_BYTES_EXCEEDED")
            return ProviderHttpResponse(
                status=response.status,
                headers={key: value for key, value in response.getheaders()},
                body=body,
                received_at=datetime.now(tz=UTC),
            )
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise SecuritiesProviderHttpError("PROVIDER_NETWORK_TIMEOUT", retryable=True) from error
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class ProviderRawPage:
    endpoint_id: str
    query: Mapping[str, str]
    status: int
    headers: Mapping[str, str]
    body: bytes
    received_at: datetime
    page_ordinal: int
    next_page_token_hash: str | None
    provider_request_id: str | None
    retry_delays_seconds: tuple[str, ...]

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass(frozen=True, slots=True)
class RawArtifactSet:
    relative_path: str
    manifest_sha256: str
    byte_count: int
    page_count: int


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def sanitized_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Retain debugging-safe response metadata only; never request credentials."""

    allowed = {
        "content-type",
        "content-length",
        "date",
        "retry-after",
        "x-request-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in allowed and key.lower() not in {"authorization", "cookie", "set-cookie"}
    }


class LocalAlpacaRawArtifactStore:
    """Immutable, traversal-safe provider page retention under the managed root."""

    def __init__(self, root: Path = Path(".local/securities/providers/alpaca")) -> None:
        self._root = root.resolve()

    def retain(
        self, *, operation_id: str, pages: Sequence[SecuritiesProviderPage], adapter_version: str
    ) -> RawArtifactSet:
        if not operation_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in operation_id
        ):
            raise RealSecuritiesDataError("provider operation identity is unsafe")
        if not pages:
            raise RealSecuritiesDataError("provider artifact retention requires at least one page")
        self._ensure_root()
        destination = self._root / operation_id
        if destination.exists() or destination.is_symlink():
            raise RealSecuritiesDataError("provider raw evidence is immutable and already retained")
        destination.mkdir(mode=0o700)
        manifests: list[dict[str, object]] = []
        try:
            for page in pages:
                ordinal = f"{page.page_ordinal:04d}"
                raw_name = f"{ordinal}.json"
                raw_path = destination / raw_name
                self._atomic_new_file(raw_path, page.body)
                manifest = {
                    "operation_id": operation_id,
                    "endpoint_identity": page.endpoint_id,
                    "query": dict(sorted(page.query.items())),
                    "request_timestamp": canonical_timestamp(page.received_at),
                    "response_timestamp": canonical_timestamp(page.received_at),
                    "status": page.status,
                    "response_headers": sanitized_headers(page.headers),
                    "raw_relative_path": raw_name,
                    "raw_sha256": page.raw_sha256,
                    "byte_size": len(page.body),
                    "page_ordinal": page.page_ordinal,
                    "next_page_token_hash": page.next_page_token_hash,
                    "provider_request_id": page.provider_request_id,
                    "adapter_version": adapter_version,
                    "retry_delays_seconds": page.retry_delays_seconds,
                    "local_research_only": True,
                }
                self._atomic_new_file(
                    destination / f"{ordinal}.manifest.json",
                    canonical_json_bytes(manifest),
                )
                manifests.append(manifest)
            aggregate = {
                "operation_id": operation_id,
                "adapter_version": adapter_version,
                "page_count": len(manifests),
                "pages": manifests,
                "local_research_only": True,
            }
            aggregate_bytes = canonical_json_bytes(aggregate)
            self._atomic_new_file(destination / "manifest.json", aggregate_bytes)
            relative = destination.relative_to(Path.cwd().resolve()).as_posix()
            return RawArtifactSet(
                relative_path=relative,
                manifest_sha256=hashlib.sha256(aggregate_bytes).hexdigest(),
                byte_count=sum(len(page.body) for page in pages),
                page_count=len(pages),
            )
        except Exception:
            # Evidence directories are never partially reused.  Do not remove a
            # retained page: a failed operation is diagnosed by its distinct path.
            raise

    def _ensure_root(self) -> None:
        candidate = Path.cwd().resolve()
        for part in self._root.relative_to(candidate).parts:
            candidate = candidate / part
            if candidate.exists() and candidate.is_symlink():
                raise RealSecuritiesDataError("provider artifact root may not traverse a symlink")
            candidate.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def _atomic_new_file(path: Path, payload: bytes) -> None:
        if path.parent.is_symlink() or path.exists():
            raise RealSecuritiesDataError("provider raw artifact path is not new and safe")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if path.exists():
                raise RealSecuritiesDataError("provider raw artifact would overwrite evidence")
            os.replace(temporary, path)
        except Exception:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise


class AlpacaMarketDataHttpAdapter:
    """Versioned Alpaca policy adapter; credentials live only in this process."""

    adapter_version = "ALPACA_MARKET_DATA_V2_IEX_AND_PAPER_ASSET_GET_ONLY_V2"

    def __init__(
        self,
        *,
        transport: SecuritiesProviderHttpTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        # These values are deliberately read only from the process environment.
        # They are not accepted by CLI/API constructors, persisted, or exposed.
        self._key_id = os.environ.get("APCA_API_KEY_ID")
        self._secret_key = os.environ.get("APCA_API_SECRET_KEY")
        self._transport = transport or HttpsProviderTransport()
        self._sleeper = sleeper

    @property
    def credentials_available(self) -> bool:
        return bool(self._key_id and self._secret_key)

    @property
    def credential_source(self) -> str:
        return "ENVIRONMENT"

    def fetch_pages(
        self,
        *,
        endpoint_id: str,
        query: Mapping[str, str],
        maximum_pages: int,
        maximum_response_bytes: int,
        timeout_seconds: int,
        path_params: Mapping[str, str] = MappingProxyType({}),
    ) -> tuple[ProviderRawPage, ...]:
        if endpoint_id not in ALPACA_ENDPOINTS:
            raise SecuritiesProviderHttpError("PROVIDER_ENDPOINT_REJECTED")
        endpoint = ALPACA_ENDPOINTS[endpoint_id]
        if not self.credentials_available:
            raise SecuritiesProviderHttpError("ALPACA_CREDENTIALS_REQUIRED")
        if not (
            1 <= maximum_pages <= 100 and maximum_response_bytes >= 1024 and timeout_seconds >= 1
        ):
            raise SecuritiesProviderHttpError("PROVIDER_POLICY_BOUNDS_INVALID")
        resolved_path = self._resolved_path(endpoint, path_params)
        current_query = self._validated_query(endpoint, query)
        seen_tokens: set[str] = set()
        pages: list[ProviderRawPage] = []
        for ordinal in range(1, maximum_pages + 1):
            page = self._request_page(
                endpoint=endpoint,
                path=resolved_path,
                query=current_query,
                maximum_response_bytes=maximum_response_bytes,
                timeout_seconds=timeout_seconds,
                ordinal=ordinal,
            )
            pages.append(page)
            token = self._next_page_token(page) if endpoint.paginated else None
            if token is None:
                return tuple(pages)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if token_hash in seen_tokens:
                raise SecuritiesProviderHttpError("PROVIDER_PAGE_TOKEN_CYCLE")
            seen_tokens.add(token_hash)
            current_query = {**current_query, "page_token": token}
        raise SecuritiesProviderHttpError("PROVIDER_PAGE_LIMIT_REACHED")

    def fetch_one_page(
        self,
        *,
        endpoint_id: str,
        query: Mapping[str, str],
        maximum_response_bytes: int,
        timeout_seconds: int,
        path_params: Mapping[str, str] = MappingProxyType({}),
    ) -> ProviderRawPage:
        """Fetch exactly one page and return it -- unlike ``fetch_pages``,
        never raises merely because the response's own ``next_page_token``
        indicates more data exists beyond this page.

        Reuses the identical credential/endpoint/query/path-param
        validation and single-request transport ``fetch_pages`` uses; the
        only difference is the caller-facing contract. ``fetch_pages``
        walks pagination to completion or fails, because several existing
        callers (equity daily bars, corporate actions) genuinely need "all
        requested data or an explicit failure" -- silently handing back a
        truncated multi-page dataset there would be a correctness hazard.
        A bounded per-underlying options capture has the opposite,
        equally legitimate need: it already manages its own multi-request
        continuation and per-underlying contract/byte/request budget
        across its own outer loop (see
        ``adapters.alpaca_options_quote_source.AlpacaOptionsChainCaptureClient.
        capture_underlying``), and one full page routinely already
        satisfies its bounded contract cap even though a real, liquid
        underlying's genuine *total* chain is commonly much larger than
        one page. Discarding that one valid, already-fetched page just
        because the provider's unrelated total happens to exceed it would
        turn an ordinary, well-within-policy underlying into a false
        ``BLOCKED_PROVIDER_ERROR`` with zero contracts retained. The
        caller parses the returned page's own next-page token from its
        body (identically to how ``fetch_pages`` does) to decide whether
        to continue; this method adds no endpoint, host, or query name
        beyond what ``ALPACA_ENDPOINTS``/``ALPACA_ENDPOINT_POLICY_HASH``
        already declare.
        """

        if endpoint_id not in ALPACA_ENDPOINTS:
            raise SecuritiesProviderHttpError("PROVIDER_ENDPOINT_REJECTED")
        endpoint = ALPACA_ENDPOINTS[endpoint_id]
        if not self.credentials_available:
            raise SecuritiesProviderHttpError("ALPACA_CREDENTIALS_REQUIRED")
        if not (maximum_response_bytes >= 1024 and timeout_seconds >= 1):
            raise SecuritiesProviderHttpError("PROVIDER_POLICY_BOUNDS_INVALID")
        resolved_path = self._resolved_path(endpoint, path_params)
        current_query = self._validated_query(endpoint, query)
        return self._request_page(
            endpoint=endpoint,
            path=resolved_path,
            query=current_query,
            maximum_response_bytes=maximum_response_bytes,
            timeout_seconds=timeout_seconds,
            ordinal=1,
        )

    def _request_page(
        self,
        *,
        endpoint: AlpacaEndpoint,
        path: str,
        query: Mapping[str, str],
        maximum_response_bytes: int,
        timeout_seconds: int,
        ordinal: int,
    ) -> ProviderRawPage:
        target = path + "?" + urlencode(sorted(query.items()))
        headers = {
            "Accept": "application/json",
            "APCA-API-KEY-ID": self._key_id or "",
            "APCA-API-SECRET-KEY": self._secret_key or "",
        }
        delays: list[str] = []
        for attempt in range(3):
            try:
                response = self._transport.request(
                    host=endpoint.host,
                    target=target,
                    headers=headers,
                    timeout_seconds=timeout_seconds,
                    maximum_response_bytes=maximum_response_bytes,
                )
            except SecuritiesProviderHttpError as error:
                if not error.retryable or attempt == 2:
                    raise
                delay = self._retry_delay({}, attempt, endpoint.identity, query)
                delays.append(_delay_text(delay))
                self._sleeper(delay)
                continue
            if response.status in {401, 403}:
                # Preserve the endpoint's sanitized status for the capability
                # service.  A separate endpoint may still prove that the same
                # credentials are accepted for market-data reads.
                raise SecuritiesProviderHttpError(
                    f"ALPACA_AUTHENTICATION_REJECTED_HTTP_{response.status}"
                )
            if response.status in {301, 302, 303, 307, 308}:
                raise SecuritiesProviderHttpError("PROVIDER_REDIRECT_REJECTED")
            if 200 <= response.status < 300:
                try:
                    json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise SecuritiesProviderHttpError("PROVIDER_JSON_INVALID") from error
                token = self._extract_token(response.body) if endpoint.paginated else None
                return ProviderRawPage(
                    endpoint_id=endpoint.identity,
                    query=dict(sorted(query.items())),
                    status=response.status,
                    headers=sanitized_headers(response.headers),
                    body=response.body,
                    received_at=response.received_at,
                    page_ordinal=ordinal,
                    next_page_token_hash=(
                        hashlib.sha256(token.encode("utf-8")).hexdigest() if token else None
                    ),
                    provider_request_id=_header_value(response.headers, "x-request-id"),
                    retry_delays_seconds=tuple(delays),
                )
            if response.status not in {429, 500, 502, 503, 504} or attempt == 2:
                raise SecuritiesProviderHttpError(f"PROVIDER_HTTP_{response.status}")
            delay = self._retry_delay(response.headers, attempt, endpoint.identity, query)
            delays.append(_delay_text(delay))
            self._sleeper(delay)
        raise SecuritiesProviderHttpError("PROVIDER_RETRY_EXHAUSTED")

    @staticmethod
    def _validated_query(endpoint: AlpacaEndpoint, query: Mapping[str, str]) -> dict[str, str]:
        if set(query) - set(endpoint.query_names):
            raise SecuritiesProviderHttpError("PROVIDER_QUERY_REJECTED")
        normalized = {key: str(value) for key, value in query.items() if str(value) != ""}
        symbols = normalized.get("symbols")
        if symbols is not None:
            values = tuple(item.strip().upper() for item in symbols.split(",") if item.strip())
            if (
                not values
                or len(values) > 100
                or any(not item.replace(".", "").replace("-", "").isalnum() for item in values)
            ):
                raise SecuritiesProviderHttpError("PROVIDER_SYMBOLS_REJECTED")
            normalized["symbols"] = ",".join(values)
        if endpoint.identity == "historical_daily_bars":
            if normalized.get("timeframe") != "1Day" or normalized.get("feed") != "iex":
                raise SecuritiesProviderHttpError("PROVIDER_DAILY_IEX_POLICY_REQUIRED")
            if normalized.get("adjustment", "raw") != "raw":
                raise SecuritiesProviderHttpError("PROVIDER_RAW_ADJUSTMENT_REQUIRED")
            normalized.setdefault("adjustment", "raw")
            normalized.setdefault("sort", "asc")
            normalized.setdefault("limit", "10000")
        if endpoint.identity == "latest_snapshots" and normalized.get("feed") != "iex":
            raise SecuritiesProviderHttpError("PROVIDER_IEX_POLICY_REQUIRED")
        if endpoint.identity == "corporate_actions":
            normalized.setdefault("region", "us")
            normalized.setdefault("sort", "asc")
            normalized.setdefault("limit", "1000")
            if normalized["region"] != "us":
                raise SecuritiesProviderHttpError("PROVIDER_US_ACTION_POLICY_REQUIRED")
        if endpoint.identity == "option_chain_snapshots":
            feed = normalized.get("feed", "indicative")
            if feed not in {"indicative", "opra"}:
                raise SecuritiesProviderHttpError("PROVIDER_OPTIONS_FEED_POLICY_REQUIRED")
            normalized["feed"] = feed
            limit = normalized.get("limit", "100")
            if not limit.isdigit() or not 1 <= int(limit) <= 1000:
                raise SecuritiesProviderHttpError("PROVIDER_OPTIONS_LIMIT_OUT_OF_RANGE")
            normalized["limit"] = limit
        return normalized

    @staticmethod
    def _resolved_path(endpoint: AlpacaEndpoint, path_params: Mapping[str, str]) -> str:
        if set(path_params) != set(endpoint.path_params):
            raise SecuritiesProviderHttpError("PROVIDER_PATH_PARAMS_REJECTED")
        resolved = endpoint.path
        for name, value in path_params.items():
            if not value or not value.replace(".", "").replace("-", "").isalnum():
                raise SecuritiesProviderHttpError("PROVIDER_PATH_PARAM_VALUE_REJECTED")
            resolved = resolved.replace(f"{{{name}}}", value.upper())
        if "{" in resolved or "}" in resolved:
            raise SecuritiesProviderHttpError("PROVIDER_PATH_PARAM_UNRESOLVED")
        return resolved

    @staticmethod
    def _extract_token(body: bytes) -> str | None:
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise SecuritiesProviderHttpError("PROVIDER_RESPONSE_OBJECT_REQUIRED")
        token = value.get("next_page_token")
        if token is None:
            return None
        if not isinstance(token, str) or not token:
            raise SecuritiesProviderHttpError("PROVIDER_PAGE_TOKEN_INVALID")
        return token

    def _next_page_token(self, page: ProviderRawPage) -> str | None:
        return self._extract_token(page.body)

    @staticmethod
    def _retry_delay(
        headers: Mapping[str, str], attempt: int, endpoint_id: str, query: Mapping[str, str]
    ) -> float:
        retry_after = _header_value(headers, "retry-after")
        if retry_after is not None:
            try:
                parsed = float(retry_after)
            except ValueError:
                parsed = -1.0
            if 0.0 <= parsed <= 30.0:
                return parsed
        digest = hashlib.sha256(
            canonical_json_bytes(
                {"endpoint": endpoint_id, "query": dict(query), "attempt": attempt}
            )
        ).digest()
        jitter = int.from_bytes(digest[:2], "big") / 65535 / 10
        return min(30.0, float(2**attempt) + jitter)


def _delay_text(value: float) -> str:
    """Canonical retained retry evidence; sleeping still receives a float."""

    return format(value, ".6f")
