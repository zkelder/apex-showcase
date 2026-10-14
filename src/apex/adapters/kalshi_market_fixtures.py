"""Strict synthetic Kalshi-shaped pages for offline discovery verification."""

from __future__ import annotations

import json
from datetime import UTC
from hashlib import sha256
from pathlib import Path
from typing import cast

from apex.application.discover_public_markets import CapturedPublicMarketPage
from apex.domain.public_market import parse_public_market_timestamp

_FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "kalshi-public-market-discovery"
_MANIFEST_NAME = "manifest.json"


class KalshiPublicFixtureError(ValueError):
    """A frozen public-market fixture was changed or did not match its small schema."""


class KalshiFixtureMarketDataClient:
    """A deterministic, no-network replacement for the public GET /markets client."""

    def __init__(self, *, root: Path | None = None, scenario: str = "standard") -> None:
        if scenario not in {"standard", "repeated_cursor"}:
            raise ValueError("public market fixture scenario is not registered")
        self._root = root or _FIXTURE_ROOT
        self._scenario = scenario

    def _manifest(self) -> dict[str, object]:
        path = self._root / _MANIFEST_NAME
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise KalshiPublicFixtureError(
                "public market fixture manifest is unavailable"
            ) from error
        if not isinstance(value, dict) or not isinstance(value.get("pages"), dict):
            raise KalshiPublicFixtureError("public market fixture manifest is malformed")
        return cast(dict[str, object], value)

    def _page(self, key: str, *, request_cursor: str | None) -> CapturedPublicMarketPage:
        pages = cast(dict[str, object], self._manifest()["pages"])
        entry = pages.get(key)
        if not isinstance(entry, dict):
            raise KalshiPublicFixtureError("public market fixture page is not registered")
        filename = entry.get("filename")
        expected_hash = entry.get("sha256")
        received_at = entry.get("received_at")
        if (
            not isinstance(filename, str)
            or not isinstance(expected_hash, str)
            or not isinstance(received_at, str)
        ):
            raise KalshiPublicFixtureError("public market fixture page metadata is malformed")
        try:
            raw_payload = (self._root / filename).read_bytes()
        except OSError as error:
            raise KalshiPublicFixtureError("public market fixture page is unavailable") from error
        if sha256(raw_payload).hexdigest() != expected_hash:
            raise KalshiPublicFixtureError(
                "public market fixture page hash does not match manifest"
            )
        try:
            document = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KalshiPublicFixtureError("public market fixture JSON is malformed") from error
        if not isinstance(document, dict) or not isinstance(document.get("markets"), list):
            raise KalshiPublicFixtureError("public market fixture response shape is malformed")
        response_cursor = document.get("cursor")
        if response_cursor is not None and not isinstance(response_cursor, str):
            raise KalshiPublicFixtureError("public market fixture cursor is malformed")
        return CapturedPublicMarketPage(
            endpoint_identity="KALSHI_GET_MARKETS",
            request_cursor=request_cursor,
            received_at=parse_public_market_timestamp(received_at).astimezone(UTC),
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw_payload,
            response_cursor=response_cursor,
            document=cast(dict[str, object], document),
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
        if status != "open" or not exclude_multivariate:
            raise ValueError("fixture discovery supports open non-multivariate markets only")
        if not 1 <= page_size <= 1000 or not 1 <= timeout_seconds <= 60:
            raise ValueError("fixture discovery request bounds are invalid")
        if self._scenario == "repeated_cursor":
            if cursor not in (None, "fixture-repeat"):
                raise KalshiPublicFixtureError("fixture pagination cursor is not registered")
            return self._page("repeated_cursor", request_cursor=cursor)
        if cursor is None:
            return self._page("first", request_cursor=None)
        if cursor == "fixture-next":
            return self._page("terminal", request_cursor=cursor)
        raise KalshiPublicFixtureError("fixture pagination cursor is not registered")
