"""Domain proofs for Kalshi Decision-Linked Baseline Quote Evidence v1: pure
normalization of already-retained ``BinaryComplementEvaluation``/
``ThresholdBundleEvaluation`` evidence into ``KalshiBaselineQuoteEvidence``
rows, and the narrow ``reduce_kalshi_binary_complement_outcome`` economic
reduction. No database, no network -- mirrors the existing offline
``tests/test_counterfactual_ledger.py`` pattern.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_complement_fixtures import (
    KalshiFixtureBinaryComplementClient,
    fixture_market,
)
from apex.adapters.kalshi_public import KalshiPublicEventNormalizer, KalshiPublicMarketNormalizer
from apex.adapters.kalshi_sweep_fixtures import fixture_capture_candidates
from apex.adapters.kalshi_threshold_fixtures import KalshiThresholdFixtureClient
from apex.application.capture_public_orderbook_session import (
    CaptureBoundedPublicOrderbookSession,
    InMemoryPublicOrderbookCaptureStore,
)
from apex.application.discover_public_markets import (
    DiscoverPublicPredictionMarkets,
    InMemoryPublicMarketDiscoveryStore,
)
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    InMemoryPublicBinaryComplementStore,
    ScanPublicBinaryComplements,
)
from apex.application.scan_public_threshold_ladders import (
    InMemoryPublicThresholdLadderStore,
    PublicThresholdLadderScanRequest,
    ScanPublicThresholdLadders,
)
from apex.domain.counterfactual_followup import (
    FollowUpHorizonKind,
    build_kalshi_market_status_observation,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    EligibilityDecision,
    EligibilityState,
    build_decision_from_eligibility,
)
from apex.domain.kalshi_baseline_quote import (
    BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    KALSHI_BASELINE_QUOTE_POLICY_V1,
    KALSHI_BASELINE_QUOTE_POLICY_V2,
    THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
    KalshiBaselineLegRole,
    KalshiBaselineQuoteError,
    KalshiMarketQuoteLeg,
    KalshiOrderbookTopOfBook,
    KalshiQuoteKind,
    KalshiQuoteLegRole,
    SettlementValueBasis,
    ValueValidationError,
    build_kalshi_baseline_quote_from_binary_complement,
    build_kalshi_baseline_quote_from_threshold_bundle,
    build_kalshi_market_quote_baseline_from_binary_complement,
    build_kalshi_market_quote_baseline_from_threshold_bundle,
    kalshi_baseline_quote_id,
    reduce_kalshi_binary_complement_outcome,
)
from apex.domain.public_complement import ComplementState
from apex.domain.public_threshold import ThresholdEvaluationState
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fixture plumbing: real, valid evaluations + their matched fills, produced
# by the actual production evaluators (never hand-forged Decimal math).
# --------------------------------------------------------------------------


class _SlicesCapturingComplementStore(InMemoryPublicBinaryComplementStore):
    """The production report never exposes matched fill slices directly
    (only the aggregate evaluation); capture them as they are appended so
    tests can build a baseline quote from the exact same evidence the real
    evaluator produced."""

    def __init__(self, markets: object) -> None:
        super().__init__(markets)  # type: ignore[arg-type]
        self.captured_slices: dict[str, tuple] = {}

    def append_complement_evidence(self, **kwargs: object) -> None:
        by_evaluation: dict[str, list] = {}
        for item in kwargs["slices"]:  # type: ignore[index]
            by_evaluation.setdefault(item.evaluation_id, []).append(item)
        for evaluation_id, items in by_evaluation.items():
            self.captured_slices[evaluation_id] = tuple(items)
        super().append_complement_evidence(**kwargs)  # type: ignore[arg-type]


def _binary_complement_evaluation_and_slices(scenario: str = "positive"):
    market = fixture_market(scenario=scenario)
    store = _SlicesCapturingComplementStore((market,))
    report = ScanPublicBinaryComplements(
        client=KalshiFixtureBinaryComplementClient(scenario=scenario),
        store=store,
        clock=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    ).scan(
        BinaryComplementScanRequest(
            market_discovery_run_id=market.ingestion_run_id,
            market_limit=1,
            maximum_depth=5,
            maximum_bundle_quantity=Decimal("10"),
        )
    )
    evaluation = report.evaluations[0]
    return evaluation, store.captured_slices.get(evaluation.evaluation_id, ())


class _SlicesCapturingThresholdStore(InMemoryPublicThresholdLadderStore):
    def __init__(self) -> None:
        super().__init__()
        self.captured_slices: dict[str, tuple] = {}

    def append_round(self, scan: object, value: object) -> None:
        by_evaluation: dict[str, list] = {}
        for item in value.slices:  # type: ignore[attr-defined]
            by_evaluation.setdefault(item.evaluation_id, []).append(item)
        for evaluation_id, items in by_evaluation.items():
            self.captured_slices.setdefault(evaluation_id, tuple(items))
        super().append_round(scan, value)  # type: ignore[arg-type]


def _threshold_bundle_evaluation_slices_and_relationship():
    clock = VirtualCaptureClock(now=datetime(2026, 7, 22, 14, 0, tzinfo=UTC))
    client = KalshiThresholdFixtureClient(clock)
    discovery_store = InMemoryPublicMarketDiscoveryStore()
    normalizer = KalshiPublicMarketNormalizer()
    capture = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=normalizer,
    )
    store = _SlicesCapturingThresholdStore()
    report = ScanPublicThresholdLadders(
        discovery=DiscoverPublicPredictionMarkets(
            client=client, normalizer=normalizer, store=discovery_store, clock=clock.wall_clock
        ),
        capture=capture,
        client=client,
        candidate_selector=lambda run_id, limit: fixture_capture_candidates(
            discovery_store, run_id, limit
        ),
        store=store,
        normalizer=normalizer,
        event_normalizer=KalshiPublicEventNormalizer(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
    ).scan(
        PublicThresholdLadderScanRequest(
            scan_id="test-kalshi-baseline-quote-threshold-v1",
            event_count=5,
            maximum_pages=3,
            market_limit=100,
            maximum_enrichments=20,
            maximum_markets_per_event=8,
            maximum_relationships=100,
            rounds=3,
            interval_seconds=10,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            maximum_observation_skew_seconds=5,
            timeout_seconds=10,
        )
    )
    evaluation = next(
        item for item in report.evaluations if item.state is ThresholdEvaluationState.CANDIDATE
    )
    slices = store.captured_slices[evaluation.evaluation_id]
    relationship = next(
        item for item in report.relationships if item.relationship_id == evaluation.relationship_id
    )
    return evaluation, slices, relationship


def _projection(
    *,
    source_record_type: str,
    source_record_id: str,
    source_canonical_hash: str,
    instrument_id: str,
    product_or_market_id: str,
) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": f"shared-kalshi-{source_record_id}",
        "venue": SharedVenue.KALSHI,
        "source_record_type": source_record_type,
        "source_record_id": source_record_id,
        "source_canonical_hash": source_canonical_hash,
        "strategy_or_method_id": "public-binary-complement-v1",
        "instrument_id": instrument_id,
        "product_or_market_id": product_or_market_id,
        "direction": "BUY_YES_AND_NO_COMPLEMENT",
        "generated_at": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(hours=1),
        "evidence_class": "GENUINE_PUBLIC",
        "freshness_seconds": 300,
        "freshness_limit_seconds": 3600,
        "expected_gross_return": None,
        "expected_net_return": None,
        "comparable_value": "0.02",
        "comparable_value_identity": "CONSERVATIVE_NET_LOWER_BOUND_OVER_MODELED_COST",
        "uncertainty": None,
        "maximum_loss": "10",
        "modeled_fees": "0.35",
        "modeled_slippage": None,
        "costs_retained": True,
        "liquidity_state": "SUFFICIENT",
        "liquidity_limitations": (),
        "required_hypothetical_capital": "10",
        "maximum_sensible_research_allocation": "10",
        "source_maximum_allocation_fraction": None,
        "expected_holding_days": 3,
        "estimated_lockup_days": 3,
        "supporting_evidence_ids": ("ev-1",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _decision_for(projection: SharedOpportunityProjection) -> CounterfactualDecision:
    eligibility = EligibilityDecision(
        decision_id=f"{projection.projection_id}-eligibility-v1",
        projection_id=projection.projection_id,
        policy_id="SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1",
        evaluated_at=NOW,
        state=EligibilityState.ELIGIBLE,
        reasons=(),
        canonical_hash=content_digest(
            (
                f"{projection.projection_id}-eligibility-v1",
                projection.projection_id,
                "SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1",
                NOW,
                EligibilityState.ELIGIBLE,
                (),
                "NONE",
            )
        ),
    )
    return build_decision_from_eligibility(
        decision=eligibility, projection=projection, opportunity_set_id="set-1"
    )


def _binary_complement_baseline():
    evaluation, slices = _binary_complement_evaluation_and_slices()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    return decision, projection, evaluation, slices


def _threshold_bundle_baseline():
    evaluation, slices, relationship = _threshold_bundle_evaluation_slices_and_relationship()
    projection = _projection(
        source_record_type=THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=relationship.provider_event_ticker,
        product_or_market_id=(
            f"{relationship.broader_market_ticker}+{relationship.narrower_market_ticker}"
        ),
    )
    decision = _decision_for(projection)
    return decision, projection, evaluation, slices, relationship


# --------------------------------------------------------------------------
# Binary-complement baseline-quote construction
# --------------------------------------------------------------------------


def test_binary_complement_baseline_quote_retains_exact_fill_slice_legs() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )

    assert quote.decision_id == decision.decision_id
    assert quote.subject_source_record_type == BINARY_COMPLEMENT_SOURCE_RECORD_TYPE
    assert quote.subject_source_record_id == evaluation.evaluation_id
    assert quote.matched_quantity == evaluation.matched_quantity
    assert quote.total_modeled_acquisition_cost == evaluation.total_modeled_acquisition_cost
    assert quote.modeled_settlement_value == evaluation.gross_settlement_value
    assert quote.settlement_value_basis == SettlementValueBasis.EXACT_COMPLEMENT_VALUE.value
    assert quote.market_tickers == (evaluation.provider_market_ticker,)
    assert len(quote.legs) == len(slices)
    for leg, fill_slice in zip(
        quote.legs, sorted(slices, key=lambda item: item.ordinal), strict=True
    ):
        assert leg.leg_role is KalshiBaselineLegRole.SINGLE_MARKET_COMPLEMENT
        assert leg.purchased_side == fill_slice.purchased_side
        assert leg.acquisition_price == fill_slice.ask_price
        assert leg.acquisition_cost == fill_slice.acquisition_cost
        assert leg.quantity == fill_slice.quantity
        assert leg.market_ticker == evaluation.provider_market_ticker


def test_bid_ask_side_semantics_preserved_per_leg() -> None:
    """Each leg's ``purchased_side`` is the exact acquisition side the
    evaluator matched (YES ask derived from the NO bid, or vice versa) --
    never re-derived, never assumed."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    sides = {leg.purchased_side for leg in quote.legs}
    assert sides <= {"YES", "NO"}
    assert sides == {fill_slice.purchased_side for fill_slice in slices}


def test_fee_and_rounding_reserve_are_copied_never_recomputed() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    assert quote.total_modeled_fees == (
        evaluation.modeled_taker_fees + evaluation.conservative_rounding_reserve
    )
    assert (
        quote.total_modeled_acquisition_cost
        == quote.gross_acquisition_cost + quote.total_modeled_fees
    )
    for leg, fill_slice in zip(
        quote.legs, sorted(slices, key=lambda item: item.ordinal), strict=True
    ):
        assert leg.modeled_fee == fill_slice.modeled_trade_fee
        assert leg.rounding_reserve == fill_slice.rounding_reserve


def test_required_leg_completeness_rejects_empty_fill_slices() -> None:
    decision, projection, evaluation, _ = _binary_complement_baseline()
    with pytest.raises(KalshiBaselineQuoteError, match="NO_FILL_SLICES"):
        build_kalshi_baseline_quote_from_binary_complement(
            decision=decision,
            projection=projection,
            evaluation=evaluation,
            fill_slices=(),
            run_id="run-1",
            captured_at=NOW,
        )


def test_missing_side_evaluation_never_produces_a_baseline_quote() -> None:
    """A NO_CANDIDATE/INELIGIBLE/INVALID evaluation (e.g. one side of the
    book missing) has no acquirable price -- the builder fails closed rather
    than inventing one, so no row is ever produced for it (Phase 2: absence
    of a row *is* the honest "unavailable" fact)."""

    evaluation, slices = _binary_complement_evaluation_and_slices(scenario="one_sided")
    assert evaluation.state is ComplementState.INELIGIBLE
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    with pytest.raises(KalshiBaselineQuoteError, match="NOT_CANDIDATE"):
        build_kalshi_baseline_quote_from_binary_complement(
            decision=decision,
            projection=projection,
            evaluation=evaluation,
            fill_slices=slices,
            run_id="run-1",
            captured_at=NOW,
        )


def test_later_quote_cannot_substitute_for_baseline() -> None:
    """The builder is bound to the *exact* evaluation the decision already
    references; a structurally different (even if genuinely later/different)
    evaluation for the same decision is rejected rather than silently
    accepted as "close enough"."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    other_evaluation = dataclasses.replace(evaluation, evaluation_id="a-different-evaluation-id")
    with pytest.raises(KalshiBaselineQuoteError, match="EVALUATION_IDENTITY_MISMATCH"):
        build_kalshi_baseline_quote_from_binary_complement(
            decision=decision,
            projection=projection,
            evaluation=other_evaluation,
            fill_slices=slices,
            run_id="run-1",
            captured_at=NOW,
        )


def test_captured_at_is_never_treated_as_baseline_time() -> None:
    """``captured_at`` records when Apex derived the row; it must never
    equal or influence ``decision_evaluated_at``/``evaluation_observed_at``,
    and two captures at different wall-clock instants over the identical
    evidence must retain the identical baseline-time fields."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    first = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    much_later = NOW + timedelta(days=30)
    second = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-2",
        captured_at=much_later,
    )
    assert first.decision_evaluated_at == second.decision_evaluated_at == decision.evaluated_at
    assert first.evaluation_observed_at == second.evaluation_observed_at
    assert first.captured_at != second.captured_at
    assert second.decision_evaluated_at != second.captured_at


# --------------------------------------------------------------------------
# Canonical hash / identity determinism and idempotent-capture semantics
# --------------------------------------------------------------------------


def test_canonical_hash_is_deterministic_and_excludes_captured_at_and_run_id() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    first = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    second = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-9-crash-resume-replay",
        captured_at=NOW + timedelta(hours=5),
    )
    assert first.canonical_hash == second.canonical_hash
    assert first.quote_id == second.quote_id


def test_quote_id_is_pure_function_of_policy_version_and_decision_id() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    assert quote.quote_id == kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1, decision_id=decision.decision_id
    )


def test_a_different_decision_produces_a_different_quote_id() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    other_decision = dataclasses.replace(decision, decision_id="a-different-decision-id")
    first = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    second = build_kalshi_baseline_quote_from_binary_complement(
        decision=other_decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    assert first.quote_id != second.quote_id


# --------------------------------------------------------------------------
# Threshold-bundle (multi-leg) baseline-quote construction
# --------------------------------------------------------------------------


def test_threshold_bundle_legs_are_deterministically_ordered_broader_then_narrower() -> None:
    decision, projection, evaluation, slices, relationship = _threshold_bundle_baseline()
    quote = build_kalshi_baseline_quote_from_threshold_bundle(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        matched_slices=slices,
        broader_market_ticker=relationship.broader_market_ticker,
        narrower_market_ticker=relationship.narrower_market_ticker,
        run_id="run-1",
        captured_at=NOW,
    )

    assert quote.subject_source_record_type == THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE
    assert quote.market_tickers == (
        relationship.broader_market_ticker,
        relationship.narrower_market_ticker,
    )
    assert quote.settlement_value_basis == SettlementValueBasis.CONSERVATIVE_FLOOR_VALUE.value
    assert quote.modeled_settlement_value == evaluation.gross_lower_bound_value
    assert len(quote.legs) == 2 * len(slices)
    # Deterministic ordinal ordering: for each matched slice (in ordinal
    # order), the broader-YES leg always immediately precedes its paired
    # narrower-NO leg -- never interleaved, never reordered.
    ordered_slices = sorted(slices, key=lambda item: item.ordinal)
    for index, matched_slice in enumerate(ordered_slices):
        broader_leg = quote.legs[2 * index]
        narrower_leg = quote.legs[2 * index + 1]
        assert broader_leg.ordinal == 2 * index
        assert narrower_leg.ordinal == 2 * index + 1
        assert broader_leg.leg_role is KalshiBaselineLegRole.THRESHOLD_BROADER_YES
        assert narrower_leg.leg_role is KalshiBaselineLegRole.THRESHOLD_NARROWER_NO
        assert broader_leg.purchased_side == "YES"
        assert narrower_leg.purchased_side == "NO"
        assert broader_leg.market_ticker == relationship.broader_market_ticker
        assert narrower_leg.market_ticker == relationship.narrower_market_ticker
        assert broader_leg.acquisition_price == matched_slice.broader_yes_ask
        assert narrower_leg.acquisition_price == matched_slice.narrower_no_ask


def test_threshold_bundle_missing_leg_orderbook_observation_fails_closed() -> None:
    decision, projection, evaluation, slices, relationship = _threshold_bundle_baseline()
    broken_evaluation = dataclasses.replace(evaluation, broader_observed_at=None)
    with pytest.raises(KalshiBaselineQuoteError, match="ORDERBOOK_OBSERVATION_MISSING"):
        build_kalshi_baseline_quote_from_threshold_bundle(
            decision=decision,
            projection=projection,
            evaluation=broken_evaluation,
            matched_slices=slices,
            broader_market_ticker=relationship.broader_market_ticker,
            narrower_market_ticker=relationship.narrower_market_ticker,
            run_id="run-1",
            captured_at=NOW,
        )


def test_threshold_bundle_and_binary_complement_quotes_use_different_settlement_bases() -> None:
    """Method isolation at the evidence-contract level: a floor value is
    never presented as an exact value or vice versa."""

    binary_decision, binary_projection, binary_evaluation, binary_slices = (
        _binary_complement_baseline()
    )
    binary_quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=binary_decision,
        projection=binary_projection,
        evaluation=binary_evaluation,
        fill_slices=binary_slices,
        run_id="run-1",
        captured_at=NOW,
    )
    (
        threshold_decision,
        threshold_projection,
        threshold_evaluation,
        threshold_slices,
        relationship,
    ) = _threshold_bundle_baseline()
    threshold_quote = build_kalshi_baseline_quote_from_threshold_bundle(
        decision=threshold_decision,
        projection=threshold_projection,
        evaluation=threshold_evaluation,
        matched_slices=threshold_slices,
        broader_market_ticker=relationship.broader_market_ticker,
        narrower_market_ticker=relationship.narrower_market_ticker,
        run_id="run-1",
        captured_at=NOW,
    )
    assert binary_quote.settlement_value_basis == SettlementValueBasis.EXACT_COMPLEMENT_VALUE.value
    assert (
        threshold_quote.settlement_value_basis
        == SettlementValueBasis.CONSERVATIVE_FLOOR_VALUE.value
    )


# --------------------------------------------------------------------------
# Phase 5: reduce_kalshi_binary_complement_outcome
# --------------------------------------------------------------------------


def _observation_for(
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    *,
    settlement_result: str | None,
    market_status: str = "finalized",
) -> object:
    return build_kalshi_market_status_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=3,
        target_time=NOW + timedelta(days=3),
        subject_instrument_id=projection.instrument_id,
        subject_product_or_market_id=projection.product_or_market_id,
        market_status=market_status,
        yes_bid=None,
        yes_ask=None,
        settlement_result=settlement_result,
        settlement_source="KALSHI_PUBLIC_RESULT_FIELD" if settlement_result else None,
        source_endpoint_identity="kalshi-public-event-followup-v1",
        observed_at=NOW + timedelta(days=3, minutes=1),
    )


def test_reduce_binary_complement_positive_economic_outcome() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    observation = _observation_for(decision, projection, settlement_result="YES")
    result = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=observation)

    expected = (
        (quote.modeled_settlement_value - quote.total_modeled_acquisition_cost)
        / quote.total_modeled_acquisition_cost
    ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
    assert result is not None
    assert result > 0
    assert result == expected
    # Sanity check against the same real numbers the evaluator itself
    # verified in ``tests/test_public_binary_complements.py``'s "positive"
    # scenario: matched_quantity=10 -> settlement=10.00, cost=9.76.
    assert quote.modeled_settlement_value == Decimal("10")
    assert quote.total_modeled_acquisition_cost == Decimal("9.76")
    assert result == (Decimal("0.24") / Decimal("9.76")).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
    )


def test_reduce_binary_complement_is_side_independent_for_a_clean_settlement() -> None:
    """A genuine complement holds both YES and NO of the same market, so
    the realized payout -- and therefore the return -- does not depend on
    which side the market actually resolved to."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    yes_result = reduce_kalshi_binary_complement_outcome(
        baseline=quote, observation=_observation_for(decision, projection, settlement_result="YES")
    )
    no_result = reduce_kalshi_binary_complement_outcome(
        baseline=quote, observation=_observation_for(decision, projection, settlement_result="NO")
    )
    assert yes_result == no_result


def test_reduce_binary_complement_negative_economic_outcome_via_synthetic_baseline() -> None:
    """A genuinely CANDIDATE-sourced binary-complement quote structurally
    guarantees a positive settlement-minus-cost gap (that gate is what
    ``ComplementState.CANDIDATE`` *means* economically) -- so a negative
    outcome is exercised here against a baseline directly constructed (not
    produced by the builder) with the settlement value deliberately set
    below the modeled cost, proving the reduction function's arithmetic
    itself distinguishes a genuine loss from "unavailable" rather than
    ever clamping a loss to zero or None."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    losing_quote = dataclasses.replace(
        quote, modeled_settlement_value=quote.total_modeled_acquisition_cost - Decimal("2")
    )
    observation = _observation_for(decision, projection, settlement_result="YES")
    result = reduce_kalshi_binary_complement_outcome(baseline=losing_quote, observation=observation)

    assert result is not None
    assert result < 0


def test_reduce_binary_complement_zero_breakeven_outcome_via_synthetic_baseline() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    breakeven_quote = dataclasses.replace(
        quote, modeled_settlement_value=quote.total_modeled_acquisition_cost
    )
    observation = _observation_for(decision, projection, settlement_result="NO")
    result = reduce_kalshi_binary_complement_outcome(
        baseline=breakeven_quote, observation=observation
    )

    assert result == Decimal("0")


def test_reduce_binary_complement_settlement_unavailable_returns_none_not_zero() -> None:
    """Not-yet-settled must be distinguished from breakeven -- ``None`` is
    never conflated with a computed ``Decimal(0)``."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    unsettled = _observation_for(decision, projection, settlement_result=None, market_status="open")
    result = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=unsettled)
    assert result is None


def test_reduce_binary_complement_void_settlement_returns_none() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    voided = _observation_for(decision, projection, settlement_result="VOID")
    result = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=voided)
    assert result is None


def test_reduce_multi_leg_comma_joined_settlement_returns_none() -> None:
    """A multi-leg follow-up read's comma-joined ``settlement_result``
    string (e.g. ``"TICKER-A=YES,TICKER-B=NO"``) is never parsed as a clean
    single-market result."""

    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    multi_leg = _observation_for(decision, projection, settlement_result="TICKER-A=YES,TICKER-B=NO")
    result = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=multi_leg)
    assert result is None


def test_reduce_method_isolation_threshold_bundle_baseline_stays_unavailable() -> None:
    """Phase 5 scopes the reduction to ``BinaryComplementEvaluation`` only;
    a threshold-ladder baseline (even with a clean settlement observation)
    never produces a return -- it fails closed, honestly, rather than
    reusing binary-complement arithmetic for a structurally different
    dual-market economic instrument."""

    decision, projection, evaluation, slices, relationship = _threshold_bundle_baseline()
    quote = build_kalshi_baseline_quote_from_threshold_bundle(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        matched_slices=slices,
        broader_market_ticker=relationship.broader_market_ticker,
        narrower_market_ticker=relationship.narrower_market_ticker,
        run_id="run-1",
        captured_at=NOW,
    )
    observation = _observation_for(decision, projection, settlement_result="YES")
    result = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=observation)
    assert result is None


def test_reduce_rejects_mismatched_decision_and_observation() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    other_decision, other_projection, _, _ = _binary_complement_baseline()
    other_decision = dataclasses.replace(other_decision, decision_id="a-totally-different-decision")
    mismatched_observation = _observation_for(
        other_decision, other_projection, settlement_result="YES"
    )
    with pytest.raises(KalshiBaselineQuoteError, match="DECISION_MISMATCH"):
        reduce_kalshi_binary_complement_outcome(baseline=quote, observation=mismatched_observation)


def test_reduce_is_deterministic() -> None:
    decision, projection, evaluation, slices = _binary_complement_baseline()
    quote = build_kalshi_baseline_quote_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        fill_slices=slices,
        run_id="run-1",
        captured_at=NOW,
    )
    observation = _observation_for(decision, projection, settlement_result="YES")
    first = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=observation)
    second = reduce_kalshi_binary_complement_outcome(baseline=quote, observation=observation)
    assert first == second


# --------------------------------------------------------------------------
# Kalshi Baseline Quote Capture & Economic Maturation v1: plain market-quote
# observation (V2) construction, distinct from the matched-trade path above.
# --------------------------------------------------------------------------


def _non_candidate_binary_complement():
    """A genuine, real (production-scanner-produced) two-sided evaluation
    that never matched a complement trade -- the exact everyday case
    (essentially every retained Kalshi evaluation, per the retained-corpus
    audit) V1 above always, correctly, produced zero rows for."""

    evaluation, slices = _binary_complement_evaluation_and_slices(scenario="non_candidate")
    assert evaluation.state is ComplementState.NO_CANDIDATE
    assert evaluation.matched_quantity == 0
    return evaluation, slices


def _top_of_book_for(evaluation, *, yes_bid: str, no_bid: str) -> KalshiOrderbookTopOfBook:
    return KalshiOrderbookTopOfBook(
        orderbook_snapshot_id=evaluation.orderbook_snapshot_id,
        market_ticker=evaluation.provider_market_ticker,
        observed_at=evaluation.evaluated_at,
        data_quality_state="VALID",
        yes_bid=Decimal(yes_bid),
        no_bid=Decimal(no_bid),
    )


def _threshold_bundle_non_candidate_evaluation_and_relationship():
    """Mirrors ``_threshold_bundle_evaluation_slices_and_relationship``
    exactly, except it selects a genuine ``NO_CANDIDATE``/``INELIGIBLE``
    evaluation from the identical real scan report instead of the one
    ``CANDIDATE`` result -- both states coexist in one report, confirmed by
    direct inspection of ``report.evaluations``."""

    clock = VirtualCaptureClock(now=datetime(2026, 7, 22, 14, 0, tzinfo=UTC))
    client = KalshiThresholdFixtureClient(clock)
    discovery_store = InMemoryPublicMarketDiscoveryStore()
    normalizer = KalshiPublicMarketNormalizer()
    capture = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=normalizer,
    )
    store = InMemoryPublicThresholdLadderStore()
    report = ScanPublicThresholdLadders(
        discovery=DiscoverPublicPredictionMarkets(
            client=client, normalizer=normalizer, store=discovery_store, clock=clock.wall_clock
        ),
        capture=capture,
        client=client,
        candidate_selector=lambda run_id, limit: fixture_capture_candidates(
            discovery_store, run_id, limit
        ),
        store=store,
        normalizer=normalizer,
        event_normalizer=KalshiPublicEventNormalizer(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
    ).scan(
        PublicThresholdLadderScanRequest(
            scan_id="test-kalshi-market-quote-baseline-threshold-v1",
            event_count=5,
            maximum_pages=3,
            market_limit=100,
            maximum_enrichments=20,
            maximum_markets_per_event=8,
            maximum_relationships=100,
            rounds=3,
            interval_seconds=10,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            maximum_observation_skew_seconds=5,
            timeout_seconds=10,
        )
    )
    evaluation = next(
        item for item in report.evaluations if item.state is not ThresholdEvaluationState.CANDIDATE
    )
    relationship = next(
        item for item in report.relationships if item.relationship_id == evaluation.relationship_id
    )
    return evaluation, relationship


def test_market_quote_baseline_captures_two_sided_non_candidate_binary_complement() -> None:
    """The everyday case: a real, two-sided, ``NO_CANDIDATE`` evaluation
    still yields a genuine, honest plain-quote baseline -- exactly the
    evidence V1 above structurally can never retain for it."""

    evaluation, _slices = _non_candidate_binary_complement()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    snapshot = _top_of_book_for(evaluation, yes_bid="0.50", no_bid="0.50")

    quote = build_kalshi_market_quote_baseline_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        snapshot=snapshot,
        run_id="run-1",
        captured_at=NOW,
    )

    assert quote.policy_version == KALSHI_BASELINE_QUOTE_POLICY_V2
    assert quote.quote_kind == KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value
    assert quote.decision_id == decision.decision_id
    assert len(quote.legs) == 1
    leg = quote.legs[0]
    assert leg.leg_role is KalshiQuoteLegRole.SINGLE_MARKET
    assert leg.yes_bid == Decimal("0.50")
    assert leg.no_bid == Decimal("0.50")
    assert leg.yes_ask == Decimal("0.50")
    assert leg.no_ask == Decimal("0.50")
    assert leg.implied_yes_probability == Decimal("0.50000000")
    # Never the V1 identity for the same decision -- the two policy
    # versions are deliberately distinct hash-namespace inputs.
    assert quote.quote_id != kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V1, decision_id=decision.decision_id
    )
    assert quote.quote_id == kalshi_baseline_quote_id(
        policy_version=KALSHI_BASELINE_QUOTE_POLICY_V2, decision_id=decision.decision_id
    )


def test_market_quote_baseline_rejects_a_genuine_candidate_evaluation() -> None:
    """Mutual exclusivity, restated defensively: this builder must never be
    invoked for an evaluation V1's own matched-trade path already owns."""

    decision, projection, evaluation, _slices = _binary_complement_baseline()
    assert evaluation.state is ComplementState.CANDIDATE
    snapshot = _top_of_book_for(evaluation, yes_bid="0.40", no_bid="0.40")
    with pytest.raises(KalshiBaselineQuoteError, match="EVALUATION_WAS_CANDIDATE"):
        build_kalshi_market_quote_baseline_from_binary_complement(
            decision=decision,
            projection=projection,
            evaluation=evaluation,
            snapshot=snapshot,
            run_id="run-1",
            captured_at=NOW,
        )


def test_market_quote_baseline_fails_closed_on_one_sided_snapshot() -> None:
    """Section 4's hard rule, restated as a test: a genuinely one-sided book
    is never silently treated as a valid two-sided baseline."""

    evaluation, _slices = _binary_complement_evaluation_and_slices(scenario="one_sided")
    assert evaluation.state is ComplementState.INELIGIBLE
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    one_sided_snapshot = KalshiOrderbookTopOfBook(
        orderbook_snapshot_id=evaluation.orderbook_snapshot_id,
        market_ticker=evaluation.provider_market_ticker,
        observed_at=evaluation.evaluated_at,
        data_quality_state="VALID",
        yes_bid=Decimal("0.53"),
        no_bid=None,
    )
    assert one_sided_snapshot.is_two_sided is False
    with pytest.raises(KalshiBaselineQuoteError, match="ONE_SIDED_ORDERBOOK"):
        build_kalshi_market_quote_baseline_from_binary_complement(
            decision=decision,
            projection=projection,
            evaluation=evaluation,
            snapshot=one_sided_snapshot,
            run_id="run-1",
            captured_at=NOW,
        )


def test_market_quote_baseline_rejects_snapshot_identity_mismatch() -> None:
    evaluation, _slices = _non_candidate_binary_complement()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    wrong_snapshot = dataclasses.replace(
        _top_of_book_for(evaluation, yes_bid="0.50", no_bid="0.50"),
        orderbook_snapshot_id="a-totally-different-snapshot-id",
    )
    with pytest.raises(KalshiBaselineQuoteError, match="SNAPSHOT_IDENTITY_MISMATCH"):
        build_kalshi_market_quote_baseline_from_binary_complement(
            decision=decision,
            projection=projection,
            evaluation=evaluation,
            snapshot=wrong_snapshot,
            run_id="run-1",
            captured_at=NOW,
        )


def test_market_quote_leg_rejects_non_reciprocal_ask() -> None:
    with pytest.raises(ValueValidationError, match="reciprocal"):
        _market_quote_leg_directly(yes_bid="0.50", no_bid="0.50", yes_ask="0.60")


def test_market_quote_leg_rejects_wrong_implied_probability() -> None:
    with pytest.raises(ValueValidationError, match="midpoint"):
        _market_quote_leg_directly(
            yes_bid="0.50", no_bid="0.50", implied_yes_probability="0.90000000"
        )


def _market_quote_leg_directly(
    *,
    yes_bid: str,
    no_bid: str,
    yes_ask: str | None = None,
    implied_yes_probability: str | None = None,
):
    yes_bid_d = Decimal(yes_bid)
    no_bid_d = Decimal(no_bid)
    return KalshiMarketQuoteLeg(
        ordinal=0,
        leg_role=KalshiQuoteLegRole.SINGLE_MARKET,
        market_ticker="COMPLEMENT-MARKET",
        orderbook_snapshot_id="snapshot-1",
        quote_observed_at=NOW,
        yes_bid=yes_bid_d,
        no_bid=no_bid_d,
        yes_ask=Decimal(yes_ask) if yes_ask is not None else (Decimal("1") - no_bid_d),
        no_ask=Decimal("1") - yes_bid_d,
        implied_yes_probability=(
            Decimal(implied_yes_probability)
            if implied_yes_probability is not None
            else ((yes_bid_d + (Decimal("1") - no_bid_d)) / 2)
        ),
    )


def test_market_quote_baseline_rejects_a_leg_observed_after_its_own_capture() -> None:
    """Temporal correctness (hard requirement), corrected by Kalshi Baseline
    Temporal Semantics & Sweep Isolation v1: no leg's own quote timestamp
    may fall after ``captured_at`` -- the sweep's own real "now" at the
    moment this row is derived, never ``decision.evaluated_at`` (see
    ``test_market_quote_baseline_accepts_a_leg_observed_after_the_coarse_
    decision_evaluated_at`` immediately below for exactly why that former
    comparator was wrong, not merely strict). Causality makes this bound
    unconditionally true for any genuine row, so only a claimed observation
    from strictly after the capture instant itself can trip it."""

    evaluation, _slices = _non_candidate_binary_complement()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    snapshot = _top_of_book_for(evaluation, yes_bid="0.50", no_bid="0.50")
    quote = build_kalshi_market_quote_baseline_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        snapshot=snapshot,
        run_id="run-1",
        captured_at=NOW,
    )
    future_leg = dataclasses.replace(quote.legs[0], quote_observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueValidationError, match="observed after its own capture"):
        dataclasses.replace(quote, legs=(future_leg,))


def test_market_quote_baseline_accepts_a_leg_observed_after_the_coarse_decision_evaluated_at() -> (
    None
):
    """The core regression this milestone fixes, reproducing the real
    production shape byte-for-byte: ``decision.evaluated_at`` is a coarse,
    scheduler-assigned day-boundary/rebuild ``as_of`` (every decision built
    by one ``SHARED_OPPORTUNITY_REBUILD`` firing shares the identical
    value, and ``apex.cli``'s ``operating-cycle-worker`` sets it to the
    current UTC day's exact midnight) -- never the real instant the
    referenced evaluation's own orderbook response was received. A leg
    genuinely observed a couple of minutes after that coarse floor, but
    still at or before the sweep's own real ``captured_at``, is the
    ordinary case (confirmed: 184 of 500 genuinely eligible production
    decisions hit exactly this shape), and must be accepted -- the previous
    comparison against ``decision.evaluated_at`` rejected it outright."""

    evaluation, _slices = _non_candidate_binary_complement()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    coarse_day_boundary = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    real_capture_instant = coarse_day_boundary + timedelta(minutes=1, seconds=42)
    swept_at = coarse_day_boundary + timedelta(minutes=5)
    decision = dataclasses.replace(decision, evaluated_at=coarse_day_boundary)
    snapshot = _top_of_book_for(evaluation, yes_bid="0.50", no_bid="0.50")

    quote = build_kalshi_market_quote_baseline_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        snapshot=snapshot,
        run_id="run-1",
        captured_at=swept_at,
    )
    legitimate_leg = dataclasses.replace(quote.legs[0], quote_observed_at=real_capture_instant)
    # Never raises: this is the accepted case, not a defensive
    # ``pytest.raises`` block.
    accepted = dataclasses.replace(quote, legs=(legitimate_leg,))
    assert accepted.legs[0].quote_observed_at == real_capture_instant
    assert accepted.legs[0].quote_observed_at > accepted.decision_evaluated_at
    assert accepted.legs[0].quote_observed_at <= accepted.captured_at


def test_threshold_bundle_market_quote_baseline_captures_both_legs_when_non_candidate() -> None:
    evaluation, relationship = _threshold_bundle_non_candidate_evaluation_and_relationship()
    assert evaluation.state is not ThresholdEvaluationState.CANDIDATE
    projection = _projection(
        source_record_type=THRESHOLD_BUNDLE_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=relationship.provider_event_ticker,
        product_or_market_id=(
            f"{relationship.broader_market_ticker}+{relationship.narrower_market_ticker}"
        ),
    )
    decision = _decision_for(projection)
    broader_snapshot = KalshiOrderbookTopOfBook(
        orderbook_snapshot_id=evaluation.broader_orderbook_snapshot_id,
        market_ticker=relationship.broader_market_ticker,
        observed_at=evaluation.broader_observed_at or evaluation.evaluated_at,
        data_quality_state="VALID",
        yes_bid=Decimal("0.40"),
        no_bid=Decimal("0.55"),
    )
    narrower_snapshot = KalshiOrderbookTopOfBook(
        orderbook_snapshot_id=evaluation.narrower_orderbook_snapshot_id,
        market_ticker=relationship.narrower_market_ticker,
        observed_at=evaluation.narrower_observed_at or evaluation.evaluated_at,
        data_quality_state="VALID",
        yes_bid=Decimal("0.30"),
        no_bid=Decimal("0.65"),
    )

    quote = build_kalshi_market_quote_baseline_from_threshold_bundle(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        broader_snapshot=broader_snapshot,
        narrower_snapshot=narrower_snapshot,
        run_id="run-1",
        captured_at=NOW,
    )
    assert len(quote.legs) == 2
    assert quote.legs[0].leg_role is KalshiQuoteLegRole.THRESHOLD_BROADER
    assert quote.legs[1].leg_role is KalshiQuoteLegRole.THRESHOLD_NARROWER
    assert quote.legs[0].market_ticker == relationship.broader_market_ticker
    assert quote.legs[1].market_ticker == relationship.narrower_market_ticker


def test_market_quote_baseline_canonical_hash_excludes_captured_at_and_run_id() -> None:
    evaluation, _slices = _non_candidate_binary_complement()
    projection = _projection(
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=evaluation.evaluation_id,
        source_canonical_hash=str(evaluation.canonical_input_hash),
        instrument_id=evaluation.provider_market_ticker,
        product_or_market_id=evaluation.provider_market_ticker,
    )
    decision = _decision_for(projection)
    snapshot = _top_of_book_for(evaluation, yes_bid="0.50", no_bid="0.50")
    first = build_kalshi_market_quote_baseline_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        snapshot=snapshot,
        run_id="run-1",
        captured_at=NOW,
    )
    second = build_kalshi_market_quote_baseline_from_binary_complement(
        decision=decision,
        projection=projection,
        evaluation=evaluation,
        snapshot=snapshot,
        run_id="a-completely-different-run",
        captured_at=NOW + timedelta(days=100),
    )
    assert first.canonical_hash == second.canonical_hash
    assert first.quote_id == second.quote_id
