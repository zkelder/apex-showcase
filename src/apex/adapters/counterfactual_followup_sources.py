"""Genuine, bounded, read-only Kalshi public-market source for Decision
Follow-Up Observation v1.

Reuses ``adapters.kalshi_public.KalshiPublicOrderbookDataClient`` byte-for-
byte for the actual network transport -- the one existing client in Apex
that already exposes single-item ``fetch_event``/``fetch_market`` (the bulk
``KalshiPublicEventDataClient``/``KalshiPublicMarketDataClient`` only expose
paginated *discovery* over open markets/events, never a single already-known
ticker regardless of its status) -- the same allowlisted-host, GET-only,
bounded-response-size, unauthenticated transport every other genuine Kalshi
public read in Apex already uses.  This module adds no new HTTP capability
of its own, only a narrow, local parse of the one additional raw field
(``market["result"]``) neither ``KalshiPublicMarketNormalizer`` nor
``KalshiPublicEventNormalizer`` currently extracts, because neither existing
normalizer has ever needed a settlement result before (both are discovery
paths over *open* markets/events only).  Extending either shared normalizer
for this one milestone's narrow need would have widened a well-tested,
already-relied-upon contract used by unrelated discovery flows
(``application.discover_public_markets``, ``application.
compare_public_scans``, ...); reading the one extra field directly from the
already-captured raw JSON here keeps that contract untouched.

Every ``CounterfactualDecision`` subject's ``instrument_id`` is a Kalshi
*event* ticker for two of the three integrated Kalshi origins
(``ExhaustiveEventSetEvaluation``, ``ThresholdBundleEvaluation`` --
confirmed structurally in ``adapters.shared_opportunity_sources``); for the
third (``BinaryComplementEvaluation``) it is "event ticker if one was
retained, else the market ticker" -- genuinely ambiguous from retained
fields alone.  Rather than special-case by origin, ``read_market_status``
always tries the event endpoint first (the common, unambiguous case) and
falls back to the single-market endpoint on any failure -- which also
transparently resolves the ambiguous case, and costs at most two bounded
GETs per genuine attempt.

Kalshi Settlement & Resolution Evidence Maturation v1: ``_read_single_market``
additionally reads five more already-present raw fields (``ticker``,
``event_ticker``, ``settlement_value_dollars``, ``settlement_ts``,
``close_time``) onto ``KalshiMarketRead`` -- no new request, no new endpoint,
same genuine single-market/single-nested-market response this module already
captures.  This module still never gates or interprets ``result`` itself
(that authoritative-vs-provisional judgment is
``application.counterfactual_followup.SyncFollowUpObservations``'s job, via
``domain.kalshi_resolution``); it stays a narrow, honest parse of whatever
the provider actually sent, exactly like ``_settlement_result`` always was.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from apex.adapters.kalshi_public import KalshiPublicOrderbookDataClient
from apex.application.counterfactual_followup import FollowUpSourceUnavailable, KalshiMarketRead
from apex.application.discover_public_markets import PublicMarketDataError
from apex.domain.public_market import parse_public_market_timestamp
from apex.domain.values import ValueValidationError

EVENT_ENDPOINT_IDENTITY = "kalshi-public-event-followup-v1"
MARKET_ENDPOINT_IDENTITY = "kalshi-public-market-followup-v1"
SETTLEMENT_RESULT_FIELD_SOURCE = "KALSHI_PUBLIC_RESULT_FIELD"
MULTI_LEG_PRICE_LIMITATION = "MULTI_LEG_EVENT_PRICE_NOT_ATTRIBUTABLE_TO_ONE_MARKET"


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp_or_none(value: object) -> datetime | None:
    """Permissive: a malformed or absent optional timestamp field never
    fails the whole read -- it is simply not retained on this read, exactly
    like every other optional raw field here (Part 13: missing or
    inconsistent provider fields are tolerated individually)."""

    if not isinstance(value, str):
        return None
    try:
        return parse_public_market_timestamp(value)
    except ValueValidationError:
        return None


def _settlement_result(market: Mapping[str, object]) -> str | None:
    raw = market.get("result")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().upper()
    return normalized or None


def _status(value: Mapping[str, object]) -> str:
    raw = value.get("status")
    return str(raw) if raw is not None else "UNKNOWN"


def _aggregate_status(markets: list[Mapping[str, object]]) -> str:
    """The genuine Kalshi event payload carries no event-level ``status``
    field at all (confirmed against a real response: an event object's own
    keys never include one, only each nested market's do) -- so a multi-leg
    event's own "status" is honestly derived from its legs' actual statuses,
    never read from a field that does not exist."""

    statuses = {_status(market) for market in markets}
    if len(statuses) == 1:
        return next(iter(statuses))
    return "MIXED:" + ",".join(sorted(statuses))


class KalshiFollowUpPublicMarketSource:
    """Implements ``application.counterfactual_followup.KalshiMarketSource``."""

    def __init__(self, *, client: KalshiPublicOrderbookDataClient | None = None) -> None:
        self._client = client or KalshiPublicOrderbookDataClient()

    def _read_single_market(
        self,
        market: Mapping[str, object],
        *,
        endpoint_identity: str,
        request_count: int,
        limitations: tuple[str, ...] = (),
    ) -> KalshiMarketRead:
        settlement_result = _settlement_result(market)
        return KalshiMarketRead(
            market_status=_status(market),
            yes_bid=_decimal_or_none(market.get("yes_bid_dollars")),
            yes_ask=_decimal_or_none(market.get("yes_ask_dollars")),
            settlement_result=settlement_result,
            settlement_source=SETTLEMENT_RESULT_FIELD_SOURCE if settlement_result else None,
            source_endpoint_identity=endpoint_identity,
            request_count=request_count,
            source_limitations=limitations,
            market_ticker=_string_or_none(market.get("ticker")),
            event_ticker=_string_or_none(market.get("event_ticker")),
            settlement_value_dollars=_decimal_or_none(market.get("settlement_value_dollars")),
            settlement_ts=_timestamp_or_none(market.get("settlement_ts")),
            close_time=_timestamp_or_none(market.get("close_time")),
        )

    def _read_event(
        self, event: Mapping[str, object], markets: list[object]
    ) -> KalshiMarketRead | None:
        del event  # no genuine event-level fields are read; see _aggregate_status
        typed_markets: list[Mapping[str, object]] = [m for m in markets if isinstance(m, Mapping)]
        if not typed_markets:
            return None
        if len(typed_markets) == 1:
            return self._read_single_market(
                typed_markets[0], endpoint_identity=EVENT_ENDPOINT_IDENTITY, request_count=1
            )
        settled_legs = tuple(
            f"{market.get('ticker')}={result}"
            for market in typed_markets
            for result in (_settlement_result(market),)
            if result is not None
        )
        return KalshiMarketRead(
            market_status=_aggregate_status(typed_markets),
            yes_bid=None,
            yes_ask=None,
            settlement_result=",".join(settled_legs) or None,
            settlement_source=SETTLEMENT_RESULT_FIELD_SOURCE if settled_legs else None,
            source_endpoint_identity=EVENT_ENDPOINT_IDENTITY,
            request_count=1,
            source_limitations=(MULTI_LEG_PRICE_LIMITATION,),
        )

    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        del (
            subject_source_record_type
        )  # the try-event-then-market fallback covers every origin uniformly
        del (
            product_or_market_id
        )  # composite/synthetic for two of three origins; instrument_id is the real ticker
        event_error: str | None = None
        try:
            response = self._client.fetch_event(
                event_ticker=instrument_id, timeout_seconds=timeout_seconds
            )
        except PublicMarketDataError as error:
            event_error = str(error)
        else:
            event = response.document.get("event")
            if isinstance(event, Mapping):
                markets = event.get("markets")
                read = self._read_event(event, markets if isinstance(markets, list) else [])
                if read is not None:
                    return read
        # Reaching here always means exactly one prior request (the event
        # lookup) was made, whether it failed outright or simply returned no
        # usable market -- this fallback is always the second and last.
        try:
            response = self._client.fetch_market(
                market_ticker=instrument_id, timeout_seconds=timeout_seconds
            )
        except PublicMarketDataError as error:
            raise FollowUpSourceUnavailable(
                f"KALSHI_PUBLIC_READ_FAILED ticker={instrument_id!r} "
                f"event_error={event_error!r} market_error={error!r}",
                request_count=2,
            ) from error
        market = response.document.get("market")
        if not isinstance(market, Mapping):
            raise FollowUpSourceUnavailable(
                "KALSHI_PUBLIC_MARKET_RESPONSE_SHAPE_INVALID", request_count=2
            )
        return self._read_single_market(
            market, endpoint_identity=MARKET_ENDPOINT_IDENTITY, request_count=2
        )
