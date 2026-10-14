from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from apex.adapters.kalshi_consistency_fixtures import KalshiFixturePublicConsistencyClient
from apex.adapters.kalshi_public import (
    KALSHI_EVENTS_PATH,
    KALSHI_PUBLIC_HOST,
    KalshiHttpResponse,
    KalshiPublicEventDataClient,
    KalshiPublicEventNormalizer,
    KalshiPublicMarketNormalizer,
    KalshiPublicMarketResponseError,
)
from apex.application.discover_public_events import (
    DiscoverPublicEventConsistency,
    InMemoryPublicEventConsistencyStore,
    PublicEventDiscoveryConflictError,
    PublicEventIngestionState,
    PublicEventPaginationError,
    PublicEventScanRequest,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    InMemoryPublicMarketDiscoveryStore,
    PublicMarketScanRequest,
)
from apex.domain.public_event import (
    ConsistencyEvaluationState,
    EventConsistencyReasonCode,
    EventEvidenceState,
    EventRelationshipState,
)


class RecordingTransport:
    def __init__(self, response: KalshiHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> KalshiHttpResponse:
        self.calls.append(kwargs)
        return self.response


def _scan_market_then_events(*, repeated_events: bool = False):
    client = KalshiFixturePublicConsistencyClient(repeated_events=repeated_events)
    market_store = InMemoryPublicMarketDiscoveryStore()

    def fixed_time() -> datetime:
        return datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    DiscoverPublicPredictionMarkets(
        client=client,
        normalizer=KalshiPublicMarketNormalizer(),
        store=market_store,
        clock=fixed_time,
    ).discover(PublicMarketScanRequest(run_key="event-market-fixture", maximum_pages=3))
    event_store = InMemoryPublicEventConsistencyStore(market_store)
    return (
        DiscoverPublicEventConsistency(
            client=client,
            normalizer=KalshiPublicEventNormalizer(),
            store=event_store,
            clock=fixed_time,
        ),
        event_store,
    )


def test_event_client_uses_only_the_documented_allowlisted_unauthenticated_get() -> None:
    body = json.dumps({"events": [], "cursor": None}).encode()
    transport = RecordingTransport(KalshiHttpResponse(200, "application/json", body))
    client = KalshiPublicEventDataClient(
        transport=transport,
        clock=lambda: datetime(2026, 7, 20, 12, tzinfo=UTC),
    )

    page = client.fetch_events(
        status="open", page_size=5, cursor=None, timeout_seconds=10, with_nested_markets=True
    )

    assert page.endpoint_identity == "KALSHI_GET_EVENTS"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["host"] == KALSHI_PUBLIC_HOST
    assert str(call["path"]).startswith(f"{KALSHI_EVENTS_PATH}?")
    assert "status=open" in str(call["path"])
    assert "with_nested_markets=true" in str(call["path"])
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert set(headers) == {"Accept", "User-Agent"}
    assert not any("auth" in key.lower() or "key" in key.lower() for key in headers)


def test_event_client_rejects_malformed_json_without_exposing_a_payload() -> None:
    client = KalshiPublicEventDataClient(
        transport=RecordingTransport(KalshiHttpResponse(200, "application/json", b"{"))
    )

    with pytest.raises(KalshiPublicMarketResponseError, match="malformed"):
        client.fetch_events(
            status="open", page_size=5, cursor=None, timeout_seconds=10, with_nested_markets=True
        )


def test_fixture_flow_keeps_unknown_exhaustiveness_ineligible_and_ranks_only_proven_candidate() -> (
    None
):
    service, store = _scan_market_then_events()
    request = PublicEventScanRequest(run_key="event-consistency-fixture", maximum_pages=3)

    first = service.discover(request)
    replay = service.discover(request)

    assert first.state is PublicEventIngestionState.COMPLETED
    assert (first.page_count, first.event_count) == (2, 9)
    assert (
        first.consistent_count,
        first.candidate_count,
        first.ineligible_count,
        first.invalid_count,
    ) == (
        1,
        1,
        3,
        4,
    )
    assert first.ranked_event_tickers == ("EVT-CANDIDATE",)
    assert replay.idempotent_replay
    values = {
        event.provider_event_ticker: evaluation
        for event, evaluation in store.evaluations_for_event_run(request.ingestion_run_id)
    }
    assert values["EVT-UNKNOWN"].state is ConsistencyEvaluationState.INELIGIBLE
    assert EventConsistencyReasonCode.EXHAUSTIVENESS_UNKNOWN in values["EVT-UNKNOWN"].reason_codes
    assert values["EVT-UNKNOWN"].yes_ask_sum is not None
    assert values["EVT-CANDIDATE"].state is ConsistencyEvaluationState.CANDIDATE
    assert values["EVT-CANDIDATE"].exhaustiveness_evidence is EventEvidenceState.VERIFIED
    assert values["EVT-INCOMPLETE"].state is ConsistencyEvaluationState.INELIGIBLE
    assert EventConsistencyReasonCode.QUOTE_INCOMPLETE in values["EVT-INCOMPLETE"].reason_codes
    assert values["EVT-MALFORMED"].state is ConsistencyEvaluationState.INVALID
    assert len(store.raw_responses) == 2
    assert len(store.snapshots) == 9
    assert all(
        snapshot.raw_response_id in store.raw_responses for snapshot in store.snapshots.values()
    )


def test_fixture_relationships_are_exact_and_mismatches_remain_explicit() -> None:
    service, store = _scan_market_then_events()
    service.discover(PublicEventScanRequest(run_key="relationship-fixture", maximum_pages=3))
    relationships = {value.provider_event_ticker: value for value in store.relationships.values()}

    assert relationships["EVT-CANDIDATE"].relationship_state is EventRelationshipState.EXACT
    assert relationships["EVT-SOURCE"].relationship_state is (
        EventRelationshipState.SETTLEMENT_SOURCE_MISMATCH
    )
    assert relationships["EVT-RULE"].relationship_state is EventRelationshipState.RULE_MISMATCH
    assert relationships["EVT-CLOSE"].relationship_state is (
        EventRelationshipState.CLOSE_TIME_MISMATCH
    )


def test_repeated_event_cursor_marks_a_partial_run_failed_after_retaining_raw_evidence() -> None:
    service, store = _scan_market_then_events(repeated_events=True)
    request = PublicEventScanRequest(run_key="event-repeated-cursor", maximum_pages=3)

    with pytest.raises(PublicEventPaginationError, match="repeated or cycled"):
        service.discover(request)

    run = store.get_event_run(request.ingestion_run_id)
    assert run is not None and run.state is PublicEventIngestionState.FAILED
    assert len(store.raw_responses) == 2


def test_event_ingestion_identity_conflict_fails_closed() -> None:
    service, _ = _scan_market_then_events()
    service.discover(PublicEventScanRequest(run_key="same-event-identity", event_limit=8))

    with pytest.raises(PublicEventDiscoveryConflictError, match="configuration"):
        service.discover(PublicEventScanRequest(run_key="same-event-identity", event_limit=9))
