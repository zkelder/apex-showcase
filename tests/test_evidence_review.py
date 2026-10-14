from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.application.autopilot import (
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyEvidenceState,
    StrategyFamily,
    StrategyImplementationState,
)
from apex.application.evidence_review import (
    BuildStrategyEvidenceSnapshot,
    BuildStrategyEvidenceSnapshotCommand,
    DecideStrategyEvidenceReview,
    DecideStrategyEvidenceReviewCommand,
    EvidenceCriterionState,
    EvidenceReviewSource,
    InMemoryEvidenceReviewStore,
    PredictionConfigurationLineage,
    RequestStrategyEvidenceReview,
    RequestStrategyEvidenceReviewCommand,
    StartStrategyEvidenceReview,
    StrategyConfigurationLineageState,
    StrategyEvidenceReviewState,
    StrategyEvidenceSnapshot,
    evaluate_evidence_criteria,
    initial_evidence_review_policies,
)
from apex.application.strategy_shadow import ShadowPrediction, ShadowPredictionState
from apex.domain.values import ContentHash, content_digest

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _descriptor() -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id="fixture-governed-evidence-v1",
        version="v1",
        display_name="Fixture governed evidence",
        family=StrategyFamily.MARKET_STRUCTURE,
        implementation_state=StrategyImplementationState.IMPLEMENTED,
        authority_state=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        last_evidence_decision="FIXTURE_ONLY",
        supported_instrument_class="BOUNDED_PREDICTION_MARKET",
        maximum_proposals_per_cycle=1,
        configuration_hash=_hash("fixture-governed-evidence"),
    )


def _prediction(prediction_id: str, *, configuration_version: str) -> ShadowPrediction:
    return ShadowPrediction(
        prediction_id=prediction_id,
        strategy_id=_descriptor().strategy_id,
        strategy_version="v1",
        campaign_id="fixture-campaign",
        cycle_id=None,
        invocation_id=None,
        proposal_id=None,
        provider="KALSHI",
        instrument_id=f"market-{prediction_id}",
        event_id=f"event-{prediction_id}",
        decision_at=NOW - timedelta(days=2),
        resolution_at=NOW + timedelta(days=1),
        point_probabilities=(("NO", Decimal("0.5")), ("YES", Decimal("0.5"))),
        conservative_probabilities=(("NO", Decimal("0.5")), ("YES", Decimal("0.5"))),
        market_probabilities=(("NO", Decimal("0.5")), ("YES", Decimal("0.5"))),
        market_evidence_id=f"evidence-{prediction_id}",
        expected_fee=Decimal(0),
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
        evidence_tier="RESEARCH",
        evidence_references=(f"evidence-{prediction_id}",),
        thesis_summary=None,
        correlation_buckets=(),
        operator_note=None,
        model_configuration_version=configuration_version,
        canonical_input_hash=_hash({"input": prediction_id}).value,
        canonical_prediction_hash=_hash({"prediction": prediction_id}).value,
        state=ShadowPredictionState.UNRESOLVED,
    )


def _snapshot(*, resolved: int, point_difference: Decimal | None) -> StrategyEvidenceSnapshot:
    return StrategyEvidenceSnapshot(
        snapshot_id=f"snapshot-{resolved}-{point_difference}",
        strategy_id=_descriptor().strategy_id,
        strategy_version="v1",
        configuration_hash=_descriptor().configuration_hash,
        policy_id="market_structure-paper-evidence-v1",
        policy_version="apex-evidence-review-policy-v1",
        cutoff_at=NOW,
        full_history_started_at=NOW - timedelta(days=30),
        recent_history_started_at=NOW - timedelta(days=7),
        total_predictions=resolved,
        unresolved_predictions=0,
        resolved_predictions=resolved,
        recent_resolved_predictions=min(resolved, 20),
        distinct_events=min(resolved, 30),
        distinct_markets=min(resolved, 30),
        probability_range_coverage=3,
        calibration_coverage=Decimal("0.60"),
        point_brier=Decimal("0.20") if point_difference is not None else None,
        conservative_brier=Decimal("0.20") if point_difference is not None else None,
        consensus_brier=Decimal("0.21") if point_difference is not None else None,
        point_log_loss=Decimal("0.20") if point_difference is not None else None,
        conservative_log_loss=Decimal("0.20") if point_difference is not None else None,
        consensus_log_loss=Decimal("0.21") if point_difference is not None else None,
        point_brier_difference=point_difference,
        conservative_brier_difference=point_difference,
        point_log_loss_difference=point_difference,
        conservative_log_loss_difference=point_difference,
        recent_conservative_brier=Decimal("0.20") if point_difference is not None else None,
        recent_conservative_log_loss=Decimal("0.20") if point_difference is not None else None,
        recent_brier_degradation=Decimal("0.01") if point_difference is not None else None,
        recent_log_loss_degradation=Decimal("0.01") if point_difference is not None else None,
        direction_accuracy=Decimal("0.60") if point_difference is not None else None,
        calibration_summary_identity=_hash("calibration"),
        conflict_frequency=Decimal(0),
        opportunity_frequency=Decimal(0),
        hypothetical_admission_frequency=Decimal(0),
        paper_cycles=0,
        paper_orders=0,
        paper_positions=0,
        integrity_warnings=(),
        configuration_lineage_complete=True,
        outcome_lineage_complete=True,
        source_set_hash=_hash("source"),
        canonical_input_hash=_hash("input"),
        canonical_snapshot_hash=_hash("snapshot"),
        created_at=NOW,
    )


def test_each_evidence_gate_is_visible_and_unknown_blocks_paper_approval() -> None:
    policy = initial_evidence_review_policies(NOW)[0]
    snapshot = _snapshot(resolved=10, point_difference=None)

    criteria = evaluate_evidence_criteria(snapshot, policy)
    states = {item.criterion_code: item.state for item in criteria}

    assert states["RESOLVED_PREDICTIONS"] is EvidenceCriterionState.FAIL
    assert states["POINT_BRIER_VS_CONSENSUS"] is EvidenceCriterionState.UNKNOWN
    assert states["RECENT_CONSERVATIVE_BRIER_DEGRADATION"] is EvidenceCriterionState.UNKNOWN


def test_snapshot_scopes_to_one_configuration_and_excludes_mismatched_lineage() -> None:
    policy = initial_evidence_review_policies(NOW)[0]
    selected_hash = _hash("selected-configuration")
    source = EvidenceReviewSource(
        descriptor=_descriptor(),
        predictions=(
            _prediction("selected", configuration_version="selected-v1"),
            _prediction("mismatched", configuration_version="other-v1"),
        ),
        evaluations=(),
        configuration_lineage=(
            PredictionConfigurationLineage(
                prediction_id="selected",
                configuration_hash=selected_hash,
                state=StrategyConfigurationLineageState.PLAN_CONFIGURATION_MATCH,
                reason_codes=("PLAN_CONFIGURATION_MATCH",),
            ),
            PredictionConfigurationLineage(
                prediction_id="mismatched",
                configuration_hash=_hash("other-configuration"),
                state=StrategyConfigurationLineageState.PLAN_CONFIGURATION_MISMATCH,
                reason_codes=("PLAN_CONFIGURATION_MISMATCH",),
            ),
        ),
    )
    store = InMemoryEvidenceReviewStore(policies=(policy,), sources=(source,))

    snapshot, _criteria = BuildStrategyEvidenceSnapshot(store).build(
        BuildStrategyEvidenceSnapshotCommand(
            strategy_id=_descriptor().strategy_id,
            strategy_version="v1",
            configuration_hash=selected_hash,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            cutoff_at=NOW,
            full_history_started_at=NOW - timedelta(days=30),
            recent_history_started_at=NOW - timedelta(days=7),
            idempotency_key="selected-configuration-snapshot",
        )
    )

    assert snapshot.total_predictions == 1
    assert snapshot.configuration_lineage_complete is True
    assert snapshot.configuration_exclusion_counts == (("PLAN_CONFIGURATION_MISMATCH", 1),)


def test_failed_hard_gate_transitions_a_review_to_blocked_without_trial() -> None:
    policy = initial_evidence_review_policies(NOW)[0]
    snapshot = _snapshot(resolved=10, point_difference=Decimal("-0.01"))
    criteria = evaluate_evidence_criteria(snapshot, policy)
    store = InMemoryEvidenceReviewStore(
        policies=(policy,),
        sources=(EvidenceReviewSource(descriptor=_descriptor(), predictions=(), evaluations=()),),
    )
    store._snapshots[snapshot.snapshot_id] = snapshot
    store._criteria[snapshot.snapshot_id] = criteria
    request = RequestStrategyEvidenceReview(store).request(
        RequestStrategyEvidenceReviewCommand(
            strategy_id=snapshot.strategy_id,
            strategy_version=snapshot.strategy_version,
            snapshot_id=snapshot.snapshot_id,
            campaign_id="fixture-campaign",
            expires_at=NOW + timedelta(hours=1),
            operator_reason="inspect frozen fixture evidence",
            operator_note="",
            idempotency_key="review-request-1",
        ),
        now=NOW,
    )
    StartStrategyEvidenceReview(store).start(
        review_id=request.review_id,
        operator_reason="begin fixture review",
        operator_note="",
        idempotency_key="review-start-1",
        now=NOW,
    )

    review, trial = DecideStrategyEvidenceReview(store).decide(
        DecideStrategyEvidenceReviewCommand(
            review_id=request.review_id,
            decision=StrategyEvidenceReviewState.APPROVED,
            operator_reason="attempt cannot override failed gate",
            operator_note="",
            idempotency_key="review-decision-1",
            confirmation_text="incorrect-confirmation",
        ),
        now=NOW,
    )

    assert review.state is StrategyEvidenceReviewState.BLOCKED
    assert trial is None
