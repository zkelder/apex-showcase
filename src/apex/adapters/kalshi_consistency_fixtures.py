"""Strict synthetic Kalshi-shaped market and event pages for offline consistency checks."""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from hashlib import sha256
from pathlib import Path
from typing import cast

from apex.application.discover_public_events import CapturedPublicEventPage
from apex.application.discover_public_markets import CapturedPublicMarketPage
from apex.domain.public_event import EventEvidenceState
from apex.domain.public_market import parse_public_market_timestamp

_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "kalshi-public-event-consistency"


class KalshiPublicConsistencyFixtureError(ValueError):
    """A frozen consistency fixture changed or failed its compact schema."""


class KalshiFixturePublicConsistencyClient:
    """A deterministic no-network client serving both official-shaped public endpoints."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        repeated_events: bool = False,
        received_at_offset: timedelta = timedelta(0),
    ) -> None:
        self._root = root or _FIXTURE_ROOT
        self._repeated_events = repeated_events
        self._received_at_offset = received_at_offset

    def _manifest(self) -> dict[str, object]:
        try:
            value = json.loads((self._root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture manifest is unavailable"
            ) from error
        if not isinstance(value, dict):
            raise KalshiPublicConsistencyFixtureError("consistency fixture manifest is malformed")
        return cast(dict[str, object], value)

    def _document(self, section: str, key: str) -> tuple[bytes, dict[str, object], str, str]:
        section_entries = self._manifest().get(section)
        if not isinstance(section_entries, dict):
            raise KalshiPublicConsistencyFixtureError("consistency fixture section is malformed")
        entry = section_entries.get(key)
        if not isinstance(entry, dict):
            raise KalshiPublicConsistencyFixtureError("consistency fixture page is not registered")
        filename, expected_hash, received_at = (
            entry.get("filename"),
            entry.get("sha256"),
            entry.get("received_at"),
        )
        if not all(isinstance(value, str) for value in (filename, expected_hash, received_at)):
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture page metadata is malformed"
            )
        try:
            raw_payload = (self._root / cast(str, filename)).read_bytes()
        except OSError as error:
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture page is unavailable"
            ) from error
        if sha256(raw_payload).hexdigest() != expected_hash:
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture page hash does not match"
            )
        try:
            document = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture JSON is malformed"
            ) from error
        if not isinstance(document, dict):
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture response shape is malformed"
            )
        return (
            raw_payload,
            cast(dict[str, object], document),
            cast(str, received_at),
            cast(str, expected_hash),
        )

    def fetch(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        exclude_multivariate: bool,
    ) -> CapturedPublicMarketPage:
        if status != "open" or not exclude_multivariate or not 1 <= page_size <= 1000:
            raise ValueError("consistency market fixture request is invalid")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("consistency market fixture timeout is invalid")
        key = "first" if cursor is None else "terminal" if cursor == "fixture-market-next" else None
        if key is None:
            raise KalshiPublicConsistencyFixtureError("consistency market cursor is not registered")
        raw, document, received_at, _ = self._document("markets", key)
        response_cursor = document.get("cursor")
        if response_cursor is not None and not isinstance(response_cursor, str):
            raise KalshiPublicConsistencyFixtureError("consistency market cursor is malformed")
        return CapturedPublicMarketPage(
            endpoint_identity="KALSHI_GET_MARKETS",
            request_cursor=cursor,
            received_at=(
                parse_public_market_timestamp(received_at).astimezone(UTC)
                + self._received_at_offset
            ),
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw,
            response_cursor=response_cursor,
            document=document,
        )

    def fetch_events(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        with_nested_markets: bool,
    ) -> CapturedPublicEventPage:
        if status != "open" or not with_nested_markets or not 1 <= page_size <= 200:
            raise ValueError("consistency event fixture request is invalid")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("consistency event fixture timeout is invalid")
        if self._repeated_events:
            key = "repeated" if cursor in (None, "fixture-event-repeat") else None
        else:
            key = (
                "first"
                if cursor is None
                else "terminal"
                if cursor == "fixture-event-next"
                else None
            )
        if key is None:
            raise KalshiPublicConsistencyFixtureError("consistency event cursor is not registered")
        raw, document, received_at, _ = self._document("events", key)
        response_cursor = document.get("cursor")
        if response_cursor is not None and not isinstance(response_cursor, str):
            raise KalshiPublicConsistencyFixtureError("consistency event cursor is malformed")
        manifest = self._manifest()
        raw_exhaustiveness = manifest.get("fixture_exhaustiveness", {})
        raw_hashes = manifest.get("fixture_nested_settlement_hashes", {})
        if not isinstance(raw_exhaustiveness, dict) or not isinstance(raw_hashes, dict):
            raise KalshiPublicConsistencyFixtureError("consistency fixture evidence is malformed")
        try:
            exhaustiveness = {
                str(ticker): EventEvidenceState(str(value))
                for ticker, value in raw_exhaustiveness.items()
            }
        except ValueError as error:
            raise KalshiPublicConsistencyFixtureError(
                "consistency fixture evidence state is malformed"
            ) from error
        nested_hashes: dict[str, dict[str, str]] = {}
        for event_ticker, values in raw_hashes.items():
            if not isinstance(values, dict):
                raise KalshiPublicConsistencyFixtureError(
                    "nested evidence hash mapping is malformed"
                )
            nested_hashes[str(event_ticker)] = {
                str(market_ticker): str(value) for market_ticker, value in values.items()
            }
        return CapturedPublicEventPage(
            endpoint_identity="KALSHI_GET_EVENTS",
            request_cursor=cursor,
            received_at=(
                parse_public_market_timestamp(received_at).astimezone(UTC)
                + self._received_at_offset
            ),
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw,
            response_cursor=response_cursor,
            document=document,
            fixture_exhaustiveness=exhaustiveness,
            fixture_nested_settlement_hashes=nested_hashes,
        )
