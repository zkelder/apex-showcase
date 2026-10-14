from __future__ import annotations

from collections import Counter
from decimal import Decimal
from hashlib import sha256

import pytest

from apex.adapters.kalshi_public import KalshiHttpResponse, KalshiPublicOrderbookDataClient
from apex.adapters.kalshi_threshold_fixtures import threshold_ladder_fixture
from apex.application.scan_public_threshold_ladders import PublicThresholdLadderScanRequest
from apex.domain.public_threshold import (
    ThresholdDirection,
    ThresholdEvaluationReasonCode,
    ThresholdEvaluationState,
    ThresholdRelationshipReasonCode,
    ThresholdRelationshipState,
    threshold_market_evidence,
)
from apex.domain.values import ContentHash


class _ThresholdTransport:
    def get(self, **_: object) -> KalshiHttpResponse:
        return KalshiHttpResponse(
            200,
            "application/json",
            (
                b'{"market":{"ticker":"NUMERIC-STRIKE","event_ticker":"EXACT-EVENT",'
                b'"market_type":"binary","strike_type":"greater",'
                b'"floor_strike":70.99,"rules_primary":"Exact structured rule"}}'
            ),
        )


def test_fixture_exercises_only_bounded_public_evidence_paths_and_research_outcomes() -> None:
    report = threshold_ladder_fixture()

    assert report.scan.state.value == "COMPLETED"
    assert report.scan.selected_event_count == 5
    assert report.scan.relationship_count == 8
    assert report.scan.evaluation_count == 15
    assert report.scan.candidate_count == 3
    assert report.scan.persisting_candidate_count == 1
    assert [value.label.value for value in report.ranking] == [
        "PERSISTING_CANDIDATE",
        "FEE_ELIMINATED",
        "ROUNDING_ELIMINATED",
        "INSUFFICIENT_BOOK",
        "SKEW_INELIGIBLE",
    ]
    states = Counter(value.state for value in report.evaluations)
    assert states == {
        ThresholdEvaluationState.CANDIDATE: 3,
        ThresholdEvaluationState.NO_CANDIDATE: 6,
        ThresholdEvaluationState.INELIGIBLE: 6,
    }
    reasons = Counter(reason for value in report.evaluations for reason in value.reason_codes)
    assert reasons == {
        ThresholdEvaluationReasonCode.FEES_ELIMINATE_LOWER_BOUND: 3,
        ThresholdEvaluationReasonCode.ROUNDING_ELIMINATES_LOWER_BOUND: 3,
        ThresholdEvaluationReasonCode.ORDERBOOK_MISSING_SIDE: 3,
        ThresholdEvaluationReasonCode.OBSERVATION_SKEW_EXCEEDED: 3,
    }
    candidate = report.ranking[0]
    assert candidate.candidate_round_count == 3
    assert candidate.maximum_matched_quantity is not None
    assert candidate.maximum_conservative_net_lower_bound is not None
    assert candidate.maximum_conservative_net_lower_bound > 0


def test_fixture_proves_structured_greater_and_less_ordering_and_rejects_other_forms() -> None:
    report = threshold_ladder_fixture()
    relationships = {
        (value.broader_market_ticker, value.narrower_market_ticker): value
        for value in report.relationships
    }
    assert relationships[("THRESH-GT-10", "THRESH-GT-20")].direction is ThresholdDirection.GREATER
    assert relationships[("THRESH-LT-20", "THRESH-LT-10")].direction is ThresholdDirection.LESS
    assert relationships[("THRESH-RANGE-A", "THRESH-RANGE-B")].state is (
        ThresholdRelationshipState.UNSUPPORTED
    )
    assert relationships[("THRESH-RULE-A", "THRESH-RULE-B")].state is (
        ThresholdRelationshipState.INVALID
    )
    assert relationships[("THRESH-SOURCE-A", "THRESH-SOURCE-B")].state is (
        ThresholdRelationshipState.INVALID
    )
    assert all("THRESH-ONE" not in pair for pair in relationships)


def test_structured_evidence_never_uses_title_or_ticker_syntax() -> None:
    rule = "Exact structured rule"
    market = {
        "ticker": "TITLE-AND-TICKER-ARE-IGNORED",
        "event_ticker": "EXACT-EVENT",
        "series_ticker": "EXACT-SERIES",
        "market_type": "binary",
        "strike_type": "greater",
        "floor_strike": "10",
        "functional_strike": "temperature",
        "response_price_units": "usd_cent",
        "price_level_structure": "binary",
        "rules_primary": rule,
        "title": "The relationship is entirely in this title",
        "subtitle": "and this subtitle",
    }
    event = {
        "event_ticker": "EXACT-EVENT",
        "series_ticker": "EXACT-SERIES",
        "settlement_sources": [{"name": "official", "url": "https://source.invalid"}],
        "markets": [market],
    }
    evidence = threshold_market_evidence(
        market_snapshot_id="snapshot-1",
        provider_market_ticker="TITLE-AND-TICKER-ARE-IGNORED",
        market_is_active=True,
        rule_content_hash=ContentHash(sha256(rule.encode()).hexdigest()),
        market_document=market,
        event_document=event,
    )
    title_changed = dict(market, title="A completely different title", subtitle="different")
    changed_evidence = threshold_market_evidence(
        market_snapshot_id="snapshot-1",
        provider_market_ticker="TITLE-AND-TICKER-ARE-IGNORED",
        market_is_active=True,
        rule_content_hash=ContentHash(sha256(rule.encode()).hexdigest()),
        market_document=title_changed,
        event_document={**event, "markets": [title_changed]},
    )

    assert evidence.is_supported
    assert changed_evidence.is_supported
    assert evidence.canonical_hash == changed_evidence.canonical_hash


def test_exact_event_series_identity_supports_omitted_market_series_but_rejects_conflict() -> None:
    rule = "Exact structured rule"
    market = {
        "ticker": "OMITTED-MARKET-SERIES",
        "event_ticker": "EXACT-EVENT",
        "market_type": "binary",
        "strike_type": "greater",
        "floor_strike": "10",
        "functional_strike": "temperature",
        "response_price_units": "usd_cent",
        "price_level_structure": "binary",
        "rules_primary": rule,
    }
    event = {
        "event_ticker": "EXACT-EVENT",
        "series_ticker": "EXACT-SERIES",
        "settlement_sources": [{"name": "official", "url": "https://source.invalid"}],
        "markets": [market],
    }

    evidence = threshold_market_evidence(
        market_snapshot_id="snapshot-omitted-series",
        provider_market_ticker="OMITTED-MARKET-SERIES",
        market_is_active=True,
        rule_content_hash=ContentHash(sha256(rule.encode()).hexdigest()),
        market_document=market,
        event_document=event,
    )
    conflicting = threshold_market_evidence(
        market_snapshot_id="snapshot-conflicting-series",
        provider_market_ticker="OMITTED-MARKET-SERIES",
        market_is_active=True,
        rule_content_hash=ContentHash(sha256(rule.encode()).hexdigest()),
        market_document={**market, "series_ticker": "CONFLICTING-SERIES"},
        event_document=event,
    )

    assert evidence.is_supported
    assert evidence.series_ticker == "EXACT-SERIES"
    assert not conflicting.is_supported
    assert ThresholdRelationshipReasonCode.SERIES_IDENTITY_MISMATCH in conflicting.reason_codes


def test_public_numeric_threshold_strike_is_retained_as_decimal() -> None:
    response = KalshiPublicOrderbookDataClient(transport=_ThresholdTransport()).fetch_market(
        market_ticker="NUMERIC-STRIKE", timeout_seconds=10
    )
    market = response.document["market"]
    assert isinstance(market, dict)
    assert market["floor_strike"] == Decimal("70.99")

    evidence = threshold_market_evidence(
        market_snapshot_id="snapshot-numeric-strike",
        provider_market_ticker="NUMERIC-STRIKE",
        market_is_active=True,
        rule_content_hash=ContentHash(sha256(b"Exact structured rule").hexdigest()),
        market_document=market,
        event_document={
            "event_ticker": "EXACT-EVENT",
            "series_ticker": "EXACT-SERIES",
            "settlement_sources": [{"name": "official", "url": "https://source.invalid"}],
            "markets": [market],
        },
    )

    assert evidence.is_supported
    assert evidence.strike == Decimal("70.99")


def test_missing_structured_strike_is_explicitly_unsupported() -> None:
    rule = "Exact rule"
    market = {
        "ticker": "UNSUPPORTED",
        "event_ticker": "EVENT",
        "series_ticker": "SERIES",
        "market_type": "binary",
        "strike_type": "range",
        "rules_primary": rule,
    }
    evidence = threshold_market_evidence(
        market_snapshot_id="snapshot-unsupported",
        provider_market_ticker="UNSUPPORTED",
        market_is_active=True,
        rule_content_hash=ContentHash(sha256(rule.encode()).hexdigest()),
        market_document=market,
        event_document={
            "event_ticker": "EVENT",
            "series_ticker": "SERIES",
            "settlement_sources": [{"name": "official", "url": "https://source.invalid"}],
            "markets": [market],
        },
    )

    assert not evidence.is_supported
    assert ThresholdRelationshipReasonCode.STRIKE_TYPE_UNSUPPORTED in evidence.reason_codes


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"event_count": 0}, "event count"),
        ({"maximum_pages": 4}, "maximum pages"),
        ({"market_limit": 49}, "market limit"),
        ({"maximum_markets_per_event": 13}, "markets per event"),
        ({"maximum_relationships": 101}, "relationships"),
        ({"rounds": 1}, "rounds"),
        ({"interval_seconds": 4}, "interval"),
        ({"maximum_depth": 21}, "depth"),
        ({"maximum_bundle_quantity": "101"}, "bundle quantity"),
        ({"maximum_observation_skew_seconds": 16}, "skew"),
        ({"timeout_seconds": 31}, "timeout"),
    ],
)
def test_request_rejects_values_outside_its_bounded_public_contract(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "scan_id": "bounded-threshold-request",
        "event_count": 1,
        "maximum_pages": 1,
        "market_limit": 50,
        "maximum_enrichments": 1,
        "maximum_markets_per_event": 2,
        "maximum_relationships": 100,
        "rounds": 2,
        "interval_seconds": 5,
        "maximum_depth": 1,
        "maximum_bundle_quantity": "1",
        "maximum_observation_skew_seconds": 1,
        "timeout_seconds": 1,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        PublicThresholdLadderScanRequest(**values)  # type: ignore[arg-type]


def test_request_configuration_hash_is_stable_for_identical_public_bounds() -> None:
    request = PublicThresholdLadderScanRequest(scan_id="stable-threshold-request")
    replay = PublicThresholdLadderScanRequest(scan_id="stable-threshold-request")

    assert request.canonical_configuration_hash == replay.canonical_configuration_hash
    assert request.evidence_ingestion_run_id == replay.evidence_ingestion_run_id


def test_relationship_evaluation_cap_is_configuration_bound() -> None:
    default = PublicThresholdLadderScanRequest(scan_id="relationship-cap")
    narrow = PublicThresholdLadderScanRequest(
        scan_id="relationship-cap",
        maximum_relationships=1,
    )

    assert default.maximum_relationships == 100
    assert narrow.maximum_relationships == 1
    assert default.canonical_configuration_hash != narrow.canonical_configuration_hash

    report = threshold_ladder_fixture(maximum_relationships=1)
    evaluated_relationships = {item.relationship_id for item in report.evaluations}
    assert len(evaluated_relationships) == 1
    assert report.scan.evaluation_count == 3
