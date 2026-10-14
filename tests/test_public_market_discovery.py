from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.adapters.kalshi_market_fixtures import KalshiFixtureMarketDataClient
from apex.adapters.kalshi_public import (
    KALSHI_MARKETS_PATH,
    KALSHI_PUBLIC_HOST,
    KALSHI_PUBLIC_USER_AGENT,
    KalshiHttpResponse,
    KalshiPublicMarketDataClient,
    KalshiPublicMarketHttpError,
    KalshiPublicMarketNormalizer,
    KalshiPublicMarketRedirectError,
    KalshiPublicMarketResponseError,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    InMemoryPublicMarketDiscoveryStore,
    PublicMarketDiscoveryConflictError,
    PublicMarketIngestionState,
    PublicMarketPaginationError,
    PublicMarketScanRequest,
)
from apex.domain.public_market import (
    PublicMarketDataQualityState,
    PublicMarketReasonCode,
    evaluate_public_market_snapshot,
)

FIXTURE_TIME = datetime(2026, 7, 20, 12, 4, tzinfo=UTC)


class RecordingTransport:
    def __init__(self, response: KalshiHttpResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> KalshiHttpResponse:
        self.calls.append(kwargs)
        return self.response


def _service(*, client, store: InMemoryPublicMarketDiscoveryStore):
    return DiscoverPublicPredictionMarkets(
        client=client,
        normalizer=KalshiPublicMarketNormalizer(),
        store=store,
        clock=lambda: FIXTURE_TIME,
    )


def test_kalshi_adapter_is_fixed_get_only_and_sends_no_authentication_headers() -> None:
    transport = RecordingTransport(
        KalshiHttpResponse(
            status=200,
            content_type="application/json; charset=utf-8",
            body=b'{"cursor":null,"markets":[]}',
        )
    )
    client = KalshiPublicMarketDataClient(
        transport=transport,
        clock=lambda: FIXTURE_TIME,
    )

    page = client.fetch(
        status="open",
        page_size=5,
        cursor=None,
        timeout_seconds=10,
        exclude_multivariate=True,
    )

    assert page.document == {"cursor": None, "markets": []}
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["host"] == KALSHI_PUBLIC_HOST
    assert str(call["path"]).startswith(KALSHI_MARKETS_PATH + "?")
    assert "status=open" in str(call["path"])
    assert "mve_filter=exclude" in str(call["path"])
    assert call["total_timeout_seconds"] == 10
    headers = call["headers"]
    assert headers == {"Accept": "application/json", "User-Agent": KALSHI_PUBLIC_USER_AGENT}
    assert not {"authorization", "cookie", "x-kalshi-access-key"}.intersection(
        key.lower() for key in headers
    )


@pytest.mark.parametrize(
    ("response", "error"),
    (
        (KalshiHttpResponse(302, "application/json", b"{}"), KalshiPublicMarketRedirectError),
        (KalshiHttpResponse(503, "application/json", b"{}"), KalshiPublicMarketHttpError),
        (KalshiHttpResponse(200, "text/html", b"{}"), KalshiPublicMarketResponseError),
        (KalshiHttpResponse(200, "application/json", b"not-json"), KalshiPublicMarketResponseError),
    ),
)
def test_kalshi_adapter_rejects_redirect_http_and_malformed_response_safely(
    response,
    error,
) -> None:
    client = KalshiPublicMarketDataClient(
        transport=RecordingTransport(response),
        clock=lambda: FIXTURE_TIME,
    )
    with pytest.raises(error):
        client.fetch(
            status="open",
            page_size=1,
            cursor=None,
            timeout_seconds=10,
            exclude_multivariate=True,
        )


def test_kalshi_adapter_enforces_response_body_limit() -> None:
    client = KalshiPublicMarketDataClient(
        transport=RecordingTransport(KalshiHttpResponse(200, "application/json", b"{" + b"x" * 20)),
        clock=lambda: FIXTURE_TIME,
        maximum_response_bytes=10,
    )
    with pytest.raises(KalshiPublicMarketResponseError, match="byte limit"):
        client.fetch(
            status="open",
            page_size=1,
            cursor=None,
            timeout_seconds=10,
            exclude_multivariate=True,
        )


def test_normalization_preserves_decimal_rules_and_per_market_invalidity() -> None:
    page = KalshiFixtureMarketDataClient().fetch(
        status="open",
        page_size=25,
        cursor=None,
        timeout_seconds=10,
        exclude_multivariate=True,
    )
    snapshots = KalshiPublicMarketNormalizer().normalize(raw_response_id="raw-test", page=page)

    valid, incomplete, malformed = snapshots
    assert valid.yes_bid == Decimal("0.42")
    assert valid.yes_ask_size == Decimal("15.00")
    assert valid.volume == Decimal("123.45")
    assert valid.liquidity == Decimal(0)
    assert valid.rule_content_hash is not None
    assert incomplete.yes_ask is None
    assert incomplete.liquidity is None
    assert malformed.provider_market_ticker is None
    assert PublicMarketReasonCode.PROVIDER_DATA_INVALID in malformed.normalization_reason_codes

    valid_evaluation = evaluate_public_market_snapshot(
        valid,
        evaluated_at=page.received_at,
        evaluator_version="test-v1",
    )
    incomplete_evaluation = evaluate_public_market_snapshot(
        incomplete,
        evaluated_at=page.received_at,
        evaluator_version="test-v1",
    )
    malformed_evaluation = evaluate_public_market_snapshot(
        malformed,
        evaluated_at=page.received_at,
        evaluator_version="test-v1",
    )
    assert valid_evaluation.state is PublicMarketDataQualityState.ELIGIBLE
    assert valid_evaluation.yes_spread == Decimal("0.03")
    assert valid_evaluation.yes_midpoint == Decimal("0.435")
    assert incomplete_evaluation.state is PublicMarketDataQualityState.ELIGIBLE
    assert incomplete_evaluation.reason_codes == ()
    assert malformed_evaluation.state is PublicMarketDataQualityState.INVALID


def test_active_lifecycle_status_and_missing_optional_quotes_are_capture_eligible() -> None:
    page = KalshiFixtureMarketDataClient().fetch(
        status="open",
        page_size=25,
        cursor=None,
        timeout_seconds=10,
        exclude_multivariate=True,
    )
    market = dict(page.document["markets"][0])
    market["status"] = "active"
    for field in ("yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars"):
        market.pop(field)
    document = {"markets": [market], "cursor": None}
    active_page = CapturedPublicMarketPage(
        endpoint_identity=page.endpoint_identity,
        request_cursor=None,
        received_at=page.received_at,
        http_status=page.http_status,
        response_content_type=page.response_content_type,
        raw_payload=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
        response_cursor=None,
        document=document,
    )

    snapshot = KalshiPublicMarketNormalizer().normalize(
        raw_response_id="active-sparse", page=active_page
    )[0]
    evaluation = evaluate_public_market_snapshot(
        snapshot,
        evaluated_at=active_page.received_at,
        evaluator_version="test-v1",
    )

    assert snapshot.status == "open"
    assert snapshot.yes_bid is None and snapshot.no_ask is None
    assert evaluation.state is PublicMarketDataQualityState.ELIGIBLE


def test_fixture_scan_is_bounded_idempotent_and_preserves_raw_lineage() -> None:
    store = InMemoryPublicMarketDiscoveryStore()
    request = PublicMarketScanRequest(run_key="fixture-discovery", page_size=10, maximum_pages=3)
    service = _service(client=KalshiFixtureMarketDataClient(), store=store)

    first = service.discover(request)
    replay = service.discover(request)

    assert first.state is PublicMarketIngestionState.COMPLETED
    assert (first.page_count, first.market_count) == (2, 4)
    assert (first.eligible_count, first.ineligible_count, first.invalid_count) == (3, 0, 1)
    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert len(store.raw_responses) == 2
    assert len(store.snapshots) == 4
    assert len(store.evaluations) == 4
    assert all(
        snapshot.raw_response_id in store.raw_responses for snapshot in store.snapshots.values()
    )


def test_scan_identity_reuse_with_changed_configuration_fails_closed() -> None:
    store = InMemoryPublicMarketDiscoveryStore()
    service = _service(client=KalshiFixtureMarketDataClient(), store=store)
    service.discover(
        PublicMarketScanRequest(run_key="same-identity", page_size=10, maximum_pages=1)
    )

    with pytest.raises(PublicMarketDiscoveryConflictError, match="configuration"):
        service.discover(
            PublicMarketScanRequest(run_key="same-identity", page_size=11, maximum_pages=1)
        )


def test_repeated_cursor_fails_and_marks_the_scan_failed_after_retaining_raw_evidence() -> None:
    store = InMemoryPublicMarketDiscoveryStore()
    request = PublicMarketScanRequest(run_key="repeat-cursor", maximum_pages=3)
    service = _service(
        client=KalshiFixtureMarketDataClient(scenario="repeated_cursor"),
        store=store,
    )

    with pytest.raises(PublicMarketPaginationError, match="repeated or cycled"):
        service.discover(request)

    run = store.get_run(request.ingestion_run_id)
    assert run is not None and run.state is PublicMarketIngestionState.FAILED
    assert len(store.raw_responses) == 2


def test_maximum_page_bound_completes_without_loading_later_pages() -> None:
    store = InMemoryPublicMarketDiscoveryStore()
    summary = _service(client=KalshiFixtureMarketDataClient(), store=store).discover(
        PublicMarketScanRequest(run_key="single-page", maximum_pages=1)
    )

    assert summary.state is PublicMarketIngestionState.COMPLETED
    assert summary.page_count == 1
    assert summary.market_count == 3
    assert len(store.raw_responses) == 1


def test_three_page_discovery_is_bounded_before_selection_can_use_its_evidence() -> None:
    class ThreePageClient:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def fetch(
            self,
            *,
            status: str,
            page_size: int,
            cursor: str | None,
            timeout_seconds: int,
            exclude_multivariate: bool,
        ) -> CapturedPublicMarketPage:
            assert status == "open"
            assert page_size == 100
            assert timeout_seconds == 10
            assert exclude_multivariate
            self.calls.append(cursor)
            ordinal = len(self.calls)
            document = {
                "markets": [],
                "cursor": None if ordinal == 3 else f"three-page-{ordinal}",
            }
            return CapturedPublicMarketPage(
                endpoint_identity="KALSHI_GET_MARKETS",
                request_cursor=cursor,
                received_at=FIXTURE_TIME,
                http_status=200,
                response_content_type="application/json",
                raw_payload=json.dumps(document, sort_keys=True, separators=(",", ":")).encode(),
                response_cursor=document["cursor"],
                document=document,
            )

    client = ThreePageClient()
    summary = _service(client=client, store=InMemoryPublicMarketDiscoveryStore()).discover(
        PublicMarketScanRequest(run_key="three-page-bound", page_size=100, maximum_pages=3)
    )

    assert summary.state is PublicMarketIngestionState.COMPLETED
    assert summary.page_count == 3
    assert summary.market_count == 0
    assert client.calls == [None, "three-page-1", "three-page-2"]
