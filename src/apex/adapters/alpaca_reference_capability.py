"""Honest, credential-gated Alpaca asset-master capability check.

This reuses the already-reviewed, GET-only ``AlpacaMarketDataHttpAdapter``
endpoint policy exactly as-is: no new endpoint, no order or account-mutation
capability, and no new credential path.  When ``APCA_API_KEY_ID``/
``APCA_API_SECRET_KEY`` are absent, this makes no network request at all and
reports that honestly rather than falling back to a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from apex.adapters.alpaca_market_data import (
    AlpacaMarketDataHttpAdapter,
    SecuritiesProviderHttpError,
)
from apex.adapters.security_reference import ParsedReferenceCapture, parse_alpaca_assets


@dataclass(frozen=True, slots=True)
class AlpacaCapabilityCheck:
    credentials_available: bool
    capture: ParsedReferenceCapture | None
    unavailable_reason: str | None
    checked_at: datetime


def check_alpaca_asset_capability(
    *,
    adapter: AlpacaMarketDataHttpAdapter | None = None,
    timeout_seconds: int = 10,
    maximum_response_bytes: int = 5_000_000,
) -> AlpacaCapabilityCheck:
    """Bounded, single-page, GET-only asset-identity capability check.

    Returns evidence, never raises for an ordinary provider-side outcome
    (absent credentials, rejected authentication, or a transport failure);
    those are reported in ``unavailable_reason``.
    """
    instance = adapter or AlpacaMarketDataHttpAdapter()
    if not instance.credentials_available:
        return AlpacaCapabilityCheck(False, None, "ALPACA_CREDENTIALS_ABSENT", datetime.now(tz=UTC))
    try:
        pages = instance.fetch_pages(
            endpoint_id="asset_master",
            query={"status": "active", "asset_class": "us_equity"},
            maximum_pages=1,
            maximum_response_bytes=maximum_response_bytes,
            timeout_seconds=timeout_seconds,
        )
    except SecuritiesProviderHttpError as error:
        return AlpacaCapabilityCheck(True, None, error.code, datetime.now(tz=UTC))
    page = pages[0]
    parsed = parse_alpaca_assets(
        payload=page.body, received_at=page.received_at, source_timestamp=page.received_at
    )
    return AlpacaCapabilityCheck(True, parsed, None, page.received_at)
