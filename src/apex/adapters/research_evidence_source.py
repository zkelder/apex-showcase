"""One bounded, unauthenticated official Nasdaq directory capture client."""

from __future__ import annotations

import http.client
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.adapters.security_reference import (
    MAX_REFERENCE_BYTES,
    NASDAQ_LISTED_PATH,
    NASDAQ_OTHER_LISTED_PATH,
    NASDAQ_TRADER_HOST,
    SEC_DATA_HOST,
    SEC_TICKERS_PATH,
    ParsedReferenceCapture,
    parse_nasdaq_symbol_directory,
    parse_sec_company_tickers,
    validate_sec_user_agent,
)

MAX_NASDAQ_RECORDS = 20_000
MAX_NASDAQ_TIMEOUT_SECONDS = 30
MAX_NASDAQ_SOURCE_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_SEC_RECORDS = 20_000
MAX_SEC_TIMEOUT_SECONDS = 30
# otherlisted.txt is the same Nasdaq Trader symbol-directory family as
# nasdaqlisted.txt (same host, same pipe-delimited schema, same order of
# magnitude of listed securities) -- it shares that source's bounds rather
# than needing its own, larger ceiling the way the genuinely much bigger
# contract-level options directory does (see MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES).
NASDAQ_DIRECTORY_PATHS = (NASDAQ_LISTED_PATH, NASDAQ_OTHER_LISTED_PATH)


class NasdaqDirectoryClientError(RuntimeError):
    pass


class NasdaqDirectoryCapabilityUnavailable(NasdaqDirectoryClientError):
    pass


@dataclass(frozen=True, slots=True)
class NasdaqDirectoryHttpResponse:
    status: int
    body: bytes


class NasdaqDirectoryTransport(Protocol):
    def get(
        self,
        *,
        host: str,
        path: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> NasdaqDirectoryHttpResponse: ...


class HttpsNasdaqDirectoryTransport:
    """TLS-verified, one-request transport with no redirect support."""

    def get(
        self,
        *,
        host: str,
        path: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> NasdaqDirectoryHttpResponse:
        if host != NASDAQ_TRADER_HOST or path not in NASDAQ_DIRECTORY_PATHS:
            raise NasdaqDirectoryClientError("NASDAQ_SOURCE_NOT_ALLOWLISTED")
        connection = http.client.HTTPSConnection(
            host,
            port=443,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "ApexResearchEvidence/1.0 (public read-only capture)",
                },
            )
            response = connection.getresponse()
            declared_length = response.getheader("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > maximum_response_bytes:
                        raise NasdaqDirectoryClientError("NASDAQ_RESPONSE_TOO_LARGE")
                except ValueError as error:
                    raise NasdaqDirectoryClientError("NASDAQ_CONTENT_LENGTH_INVALID") from error
            body = response.read(maximum_response_bytes + 1)
            if len(body) > maximum_response_bytes:
                raise NasdaqDirectoryClientError("NASDAQ_RESPONSE_TOO_LARGE")
            return NasdaqDirectoryHttpResponse(response.status, body)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise NasdaqDirectoryCapabilityUnavailable(
                f"NASDAQ_PUBLIC_SOURCE_UNAVAILABLE:{type(error).__name__}"
            ) from error
        finally:
            connection.close()


class NasdaqDirectoryClient:
    def __init__(self, transport: NasdaqDirectoryTransport | None = None) -> None:
        self._transport = transport or HttpsNasdaqDirectoryTransport()

    def capture(
        self,
        *,
        received_at: datetime,
        timeout_seconds: int,
        source_path: str = NASDAQ_LISTED_PATH,
        maximum_response_bytes: int = MAX_REFERENCE_BYTES,
        maximum_records: int = MAX_NASDAQ_RECORDS,
        maximum_source_age_seconds: int = MAX_NASDAQ_SOURCE_AGE_SECONDS,
    ) -> tuple[bytes, ParsedReferenceCapture]:
        if (
            received_at.tzinfo is None
            or source_path not in NASDAQ_DIRECTORY_PATHS
            or not 1 <= timeout_seconds <= MAX_NASDAQ_TIMEOUT_SECONDS
            or not 1 <= maximum_response_bytes <= MAX_REFERENCE_BYTES
            or not 1 <= maximum_records <= MAX_NASDAQ_RECORDS
            or not 1 <= maximum_source_age_seconds <= MAX_NASDAQ_SOURCE_AGE_SECONDS
        ):
            raise NasdaqDirectoryClientError("NASDAQ_CAPTURE_BOUNDS_INVALID")
        started = time.monotonic()
        response = self._transport.get(
            host=NASDAQ_TRADER_HOST,
            path=source_path,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        if time.monotonic() - started > timeout_seconds:
            raise NasdaqDirectoryCapabilityUnavailable("NASDAQ_CAPTURE_RUNTIME_EXCEEDED")
        if response.status != 200:
            raise NasdaqDirectoryCapabilityUnavailable(
                f"NASDAQ_PUBLIC_SOURCE_HTTP_{response.status}"
            )
        if not response.body or len(response.body) > maximum_response_bytes:
            raise NasdaqDirectoryClientError("NASDAQ_RESPONSE_SIZE_INVALID")
        parsed = parse_nasdaq_symbol_directory(
            payload=response.body,
            source_path=source_path,
            received_at=received_at,
        )
        if len(parsed.records) > maximum_records:
            raise NasdaqDirectoryClientError("NASDAQ_ACCEPTED_RECORD_LIMIT_EXCEEDED")
        age_seconds = (received_at - parsed.receipt.source_timestamp).total_seconds()
        if age_seconds < -300 or age_seconds > maximum_source_age_seconds:
            raise NasdaqDirectoryClientError("NASDAQ_SOURCE_STALE_OR_FUTURE")
        return response.body, parsed


class SecCompanyTickersClientError(RuntimeError):
    pass


class SecCompanyTickersCapabilityUnavailable(SecCompanyTickersClientError):
    pass


class SecCompanyTickersTransport(Protocol):
    def get(
        self,
        *,
        host: str,
        path: str,
        user_agent: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> NasdaqDirectoryHttpResponse: ...


class HttpsSecCompanyTickersTransport:
    """TLS-verified, one-request transport honoring SEC's identifying UA policy."""

    def get(
        self,
        *,
        host: str,
        path: str,
        user_agent: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> NasdaqDirectoryHttpResponse:
        if host != SEC_DATA_HOST or path != SEC_TICKERS_PATH:
            raise SecCompanyTickersClientError("SEC_SOURCE_NOT_ALLOWLISTED")
        connection = http.client.HTTPSConnection(
            host,
            port=443,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(
                "GET",
                path,
                headers={"Accept": "application/json", "User-Agent": user_agent},
            )
            response = connection.getresponse()
            declared_length = response.getheader("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > maximum_response_bytes:
                        raise SecCompanyTickersClientError("SEC_RESPONSE_TOO_LARGE")
                except ValueError as error:
                    raise SecCompanyTickersClientError("SEC_CONTENT_LENGTH_INVALID") from error
            body = response.read(maximum_response_bytes + 1)
            if len(body) > maximum_response_bytes:
                raise SecCompanyTickersClientError("SEC_RESPONSE_TOO_LARGE")
            return NasdaqDirectoryHttpResponse(response.status, body)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise SecCompanyTickersCapabilityUnavailable(
                f"SEC_PUBLIC_SOURCE_UNAVAILABLE:{type(error).__name__}"
            ) from error
        finally:
            connection.close()


class SecCompanyTickersClient:
    """One bounded, unauthenticated SEC ``company_tickers.json`` capture client.

    SEC EDGAR requires an identifying ``User-Agent`` for automated access but
    accepts no API key, cookie, or session; this remains a public GET.  The
    file carries no embedded freshness timestamp, so ``received_at`` is the
    honest evidence of currency (recorded as a limitation, never invented).
    """

    def __init__(self, transport: SecCompanyTickersTransport | None = None) -> None:
        self._transport = transport or HttpsSecCompanyTickersTransport()

    def capture(
        self,
        *,
        user_agent: str,
        received_at: datetime,
        timeout_seconds: int,
        maximum_response_bytes: int = MAX_REFERENCE_BYTES,
        maximum_records: int = MAX_SEC_RECORDS,
    ) -> tuple[bytes, ParsedReferenceCapture]:
        validated_agent = validate_sec_user_agent(user_agent)
        if (
            received_at.tzinfo is None
            or not 1 <= timeout_seconds <= MAX_SEC_TIMEOUT_SECONDS
            or not 1 <= maximum_response_bytes <= MAX_REFERENCE_BYTES
            or not 1 <= maximum_records <= MAX_SEC_RECORDS
        ):
            raise SecCompanyTickersClientError("SEC_CAPTURE_BOUNDS_INVALID")
        started = time.monotonic()
        response = self._transport.get(
            host=SEC_DATA_HOST,
            path=SEC_TICKERS_PATH,
            user_agent=validated_agent,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        if time.monotonic() - started > timeout_seconds:
            raise SecCompanyTickersCapabilityUnavailable("SEC_CAPTURE_RUNTIME_EXCEEDED")
        if response.status != 200:
            raise SecCompanyTickersCapabilityUnavailable(
                f"SEC_PUBLIC_SOURCE_HTTP_{response.status}"
            )
        if not response.body or len(response.body) > maximum_response_bytes:
            raise SecCompanyTickersClientError("SEC_RESPONSE_SIZE_INVALID")
        parsed = parse_sec_company_tickers(
            payload=response.body,
            received_at=received_at,
            source_timestamp=received_at,
        )
        if len(parsed.records) > maximum_records:
            raise SecCompanyTickersClientError("SEC_ACCEPTED_RECORD_LIMIT_EXCEEDED")
        return response.body, parsed
