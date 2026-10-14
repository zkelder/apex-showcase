"""One bounded, unauthenticated official Nasdaq options-directory capture client.

Mirrors ``apex.adapters.research_evidence_source``'s Nasdaq equity-directory
transport in spirit -- TLS-verified, one allowlisted host/path, no redirects
-- but this source is genuinely larger in scale (see
``apex.adapters.options_reference.MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES``), so
the response is read in bounded chunks with an incrementally updated hash
rather than materialized in one ``.read()`` call: a runaway or malformed
response is rejected as soon as it exceeds the bound, without first
buffering the whole oversized body.  A separate total-runtime deadline is
enforced across every chunk, distinct from the per-read socket timeout. This
is reference identity only (strike/expiration/type) — never a quote, Greek,
or implied-volatility observation, and never a provider write.
"""

from __future__ import annotations

import hashlib
import http.client
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.adapters.options_reference import (
    MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
    NASDAQ_OPTIONS_DIRECTORY_PATH,
    NASDAQ_OPTIONS_HOST,
    ParsedOptionReference,
    parse_nasdaq_options_directory,
)

MAX_OPTIONS_TIMEOUT_SECONDS = 30
# Bounds the *whole* capture (connect + every chunked read), separately from
# MAX_OPTIONS_TIMEOUT_SECONDS which bounds each individual socket operation.
# A slow trickle that never stalls on any single read, but never finishes,
# is still rejected once this wall-clock budget is exhausted.
MAX_OPTIONS_TOTAL_RUNTIME_SECONDS = 180
OPTIONS_STREAM_CHUNK_BYTES = 1_048_576  # 1 MiB


class NasdaqOptionsDirectoryClientError(RuntimeError):
    pass


class NasdaqOptionsDirectoryCapabilityUnavailable(NasdaqOptionsDirectoryClientError):
    pass


@dataclass(frozen=True, slots=True)
class NasdaqOptionsHttpResponse:
    status: int
    body: bytes
    sha256: str | None = None
    truncated: bool = False


class NasdaqOptionsDirectoryTransport(Protocol):
    def get(
        self,
        *,
        host: str,
        path: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
        maximum_total_runtime_seconds: int,
    ) -> NasdaqOptionsHttpResponse: ...


def read_bounded_stream(
    read: Callable[[int], bytes],
    *,
    maximum_response_bytes: int,
    chunk_bytes: int,
    declared_length: int | None,
    deadline: float,
) -> tuple[bytes, str, bool]:
    """Read a chunked byte source with an incremental hash and a hard bound.

    Never materializes more than one chunk beyond the already-accepted total
    at any point, so an oversized or runaway response is rejected as soon as
    it crosses ``maximum_response_bytes`` rather than after being fully
    buffered.  Returns ``(body, sha256_hex, truncated)``; ``truncated`` is
    true only when a declared length was known in advance and fewer bytes
    were actually delivered before the source signalled end-of-stream.
    """
    hasher = hashlib.sha256()
    buffer = bytearray()
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise NasdaqOptionsDirectoryCapabilityUnavailable(
                "NASDAQ_OPTIONS_CAPTURE_RUNTIME_EXCEEDED"
            )
        chunk = read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_response_bytes:
            raise NasdaqOptionsDirectoryClientError("NASDAQ_OPTIONS_RESPONSE_TOO_LARGE")
        hasher.update(chunk)
        buffer.extend(chunk)
    truncated = declared_length is not None and total != declared_length
    return bytes(buffer), hasher.hexdigest(), truncated


class HttpsNasdaqOptionsDirectoryTransport:
    """TLS-verified, one-request transport with no redirect support."""

    def get(
        self,
        *,
        host: str,
        path: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
        maximum_total_runtime_seconds: int,
    ) -> NasdaqOptionsHttpResponse:
        if host != NASDAQ_OPTIONS_HOST or path != NASDAQ_OPTIONS_DIRECTORY_PATH:
            raise NasdaqOptionsDirectoryClientError("NASDAQ_OPTIONS_SOURCE_NOT_ALLOWLISTED")
        connection = http.client.HTTPSConnection(
            host, port=443, timeout=timeout_seconds, context=ssl.create_default_context()
        )
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "Accept": "text/plain",
                    "User-Agent": "ApexOptionsReference/1.0 (public read-only capture)",
                },
            )
            response = connection.getresponse()
            declared_length: int | None = None
            declared_header = response.getheader("Content-Length")
            if declared_header is not None:
                try:
                    declared_length = int(declared_header)
                except ValueError as error:
                    raise NasdaqOptionsDirectoryClientError(
                        "NASDAQ_OPTIONS_CONTENT_LENGTH_INVALID"
                    ) from error
                if declared_length > maximum_response_bytes:
                    raise NasdaqOptionsDirectoryClientError("NASDAQ_OPTIONS_RESPONSE_TOO_LARGE")
            body, digest, truncated = read_bounded_stream(
                response.read,
                maximum_response_bytes=maximum_response_bytes,
                chunk_bytes=OPTIONS_STREAM_CHUNK_BYTES,
                declared_length=declared_length,
                deadline=time.monotonic() + maximum_total_runtime_seconds,
            )
            if truncated:
                raise NasdaqOptionsDirectoryClientError("NASDAQ_OPTIONS_RESPONSE_TRUNCATED")
            return NasdaqOptionsHttpResponse(response.status, body, digest, truncated)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise NasdaqOptionsDirectoryCapabilityUnavailable(
                f"NASDAQ_OPTIONS_SOURCE_UNAVAILABLE:{type(error).__name__}"
            ) from error
        finally:
            connection.close()


class NasdaqOptionsDirectoryClient:
    def __init__(self, transport: NasdaqOptionsDirectoryTransport | None = None) -> None:
        self._transport = transport or HttpsNasdaqOptionsDirectoryTransport()

    def capture(
        self,
        *,
        received_at: datetime,
        underlying_ids: dict[str, str],
        timeout_seconds: int,
        maximum_response_bytes: int = MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
        maximum_total_runtime_seconds: int = MAX_OPTIONS_TOTAL_RUNTIME_SECONDS,
    ) -> tuple[bytes, ParsedOptionReference]:
        if (
            received_at.tzinfo is None
            or not 1 <= timeout_seconds <= MAX_OPTIONS_TIMEOUT_SECONDS
            or not 1 <= maximum_response_bytes <= MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES
            or not 1 <= maximum_total_runtime_seconds <= MAX_OPTIONS_TOTAL_RUNTIME_SECONDS
        ):
            raise NasdaqOptionsDirectoryClientError("NASDAQ_OPTIONS_CAPTURE_BOUNDS_INVALID")
        started = time.monotonic()
        response = self._transport.get(
            host=NASDAQ_OPTIONS_HOST,
            path=NASDAQ_OPTIONS_DIRECTORY_PATH,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
            maximum_total_runtime_seconds=maximum_total_runtime_seconds,
        )
        if time.monotonic() - started > maximum_total_runtime_seconds:
            raise NasdaqOptionsDirectoryCapabilityUnavailable(
                "NASDAQ_OPTIONS_CAPTURE_RUNTIME_EXCEEDED"
            )
        if response.status != 200:
            raise NasdaqOptionsDirectoryCapabilityUnavailable(
                f"NASDAQ_OPTIONS_SOURCE_HTTP_{response.status}"
            )
        if not response.body or len(response.body) > maximum_response_bytes:
            raise NasdaqOptionsDirectoryClientError("NASDAQ_OPTIONS_RESPONSE_SIZE_INVALID")
        parsed = parse_nasdaq_options_directory(
            payload=response.body, received_at=received_at, underlying_ids=underlying_ids
        )
        return response.body, parsed
