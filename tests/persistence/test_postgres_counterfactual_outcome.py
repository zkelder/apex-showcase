"""PostgreSQL persistence proofs for Counterfactual Outcome Feedback v1:
migration 0101, ``PostgresCounterfactualOutcomeRepository``, and
``RefreshCounterfactualOutcomeAggregates`` against a real, freshly migrated
``apex_test`` database."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.counterfactual_outcome_sources import RetainedCounterfactualOutcomeSource
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_outcome import RefreshCounterfactualOutcomeAggregates
from apex.domain.counterfactual_classification import (
    CURRENT_CLASSIFIER_VERSION,
    CounterfactualDecisionClassification,
    ReasonFamily,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeError,
    CounterfactualOutcomeQualificationState,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 19, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _projection(
    identity: str, *, strategy_or_method_id: str = "TEST_METHOD_V1"
) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"co-proj-{identity}",
        venue=SharedVenue.CRYPTO,
        source_record_type="FixtureOpportunity",
        source_record_id=f"co-source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id=strategy_or_method_id,
        instrument_id="BTC",
        product_or_market_id="BTC-USD",
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=60),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value="0.10",
        comparable_value_identity=None,
        uncertainty="0.10",
        maximum_loss="50",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.5",
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=(f"co-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


def _seed_projection(engine: Engine, *, identity: str, strategy_or_method_id: str) -> str:
    projection = _projection(identity, strategy_or_method_id=strategy_or_method_id)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"co-set-{identity}", created_at=NOW, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"co-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    return projection.projection_id


def _decision(*, decision_id: str, projection_id: str) -> CounterfactualDecision:
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        origin_decision_id=f"{decision_id}-origin",
        origin_canonical_hash="a" * 64,
        origin_state="REJECTED",
        subject_projection_id=projection_id,
        subject_source_record_type="CryptoResearchOpportunity",
        subject_source_record_id=f"{decision_id}-record",
        venue=SharedVenue.CRYPTO,
        opportunity_set_id=None,
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=NOW,
        disposition=DecisionDisposition.NOT_ADVANCED,
        raw_reason_codes=("SOURCE:ECONOMICS_TEST",),
        explanation="test decision",
        canonical_hash="b" * 64,
        authority="NONE",
    )


def _classification(*, decision_id: str) -> CounterfactualDecisionClassification:
    return CounterfactualDecisionClassification(
        classification_id=f"{decision_id}-classification",
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        classifier_version=CURRENT_CLASSIFIER_VERSION,
        classifier_policy_hash="c" * 64,
        reason_families=(ReasonFamily.ECONOMICS,),
        classified_at=NOW,
        canonical_hash="d" * 64,
        authority="NONE",
    )


def _observation(*, decision_id: str, subsequent_return: Decimal) -> DecisionFollowUpObservation:
    observation_id = f"{decision_id}-obs"
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        venue=SharedVenue.CRYPTO,
        disposition=DecisionDisposition.NOT_ADVANCED,
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        decision_evaluated_at=NOW,
        baseline_time=NOW,
        target_time=NOW,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id="BTC-USD",
        subject_product_or_market_id="BTC-USD",
        provenance=EvidenceClass.GENUINE_PUBLIC,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version="CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1",
        observed_at=NOW,
        source_lineage=("batch-1",),
        data_limitations=(),
        reference_price_at_baseline=Decimal("100"),
        reference_price_at_target=Decimal("105"),
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=f"{observation_id}-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


def _assessment(*, observation: DecisionFollowUpObservation) -> FollowUpEvidenceQualityAssessment:
    return FollowUpEvidenceQualityAssessment(
        assessment_id=f"{observation.observation_id}-assessment",
        observation_id=observation.observation_id,
        observation_canonical_hash=observation.canonical_hash,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        baseline_evidence_class=observation.baseline_evidence_class,
        target_evidence_class=observation.target_evidence_class,
        verdict=FollowUpEvidenceQualityVerdict.ELIGIBLE,
        reason_code="TEST_REASON",
        evidence_defect_type=FollowUpEvidenceDefectType.NONE,
        assessed_at=NOW,
        canonical_hash=f"{observation.observation_id}-assessment-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


def _aggregate(
    *, aggregate_id: str, cohort_id: str, run_id: str, decision_id: str = "decision-1"
) -> CounterfactualOutcomeAggregate:
    return CounterfactualOutcomeAggregate(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        run_id=run_id,
        venue=SharedVenue.CRYPTO,
        strategy_or_method_id="TEST_METHOD_V1",
        reason_family=ReasonFamily.ECONOMICS,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        classifier_version=CURRENT_CLASSIFIER_VERSION,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        generated_at=NOW,
        total_observations_considered=1,
        mature_observation_count=1,
        eligible_observation_count=1,
        evidence_ineligible_observation_count=0,
        unassessed_observation_count=0,
        unavailable_observation_count=0,
        positive_outcome_count=1,
        negative_outcome_count=0,
        zero_outcome_count=0,
        mean_subsequent_return=None,
        median_subsequent_return=None,
        positive_rate=None,
        qualification_state=CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE,
        contributing_decision_ids=(decision_id,),
        explanation="test aggregate",
        canonical_hash="e" * 64,
        authority="NONE",
    )


# ---------------------------------------------------------------------------
# Migration shape and downgrade guard
# ---------------------------------------------------------------------------


def test_migration_creates_the_table_and_it_is_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 0


def test_downgrade_refuses_with_retained_evidence(postgres_engine: Engine) -> None:
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    aggregate = _aggregate(aggregate_id="agg-1", cohort_id="cohort-1", run_id="run-1")
    assert repository.save_aggregate(aggregate=aggregate) is False
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0118_evidence_gap_recovery"

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0101 with retained counterfactual-outcome evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        # From head (now 0103), this genuinely steps 0103 -> 0102 -> 0101
        # cleanly (this fixture's aggregate is COUNTERFACTUAL_OUTCOME_
        # POLICY_V1 and creates no kalshi_baseline_quote row, so neither
        # newer guard fires) before genuinely blocking at 0101 -> 0100 --
        # the guard this test exists to prove. The stopping point is
        # therefore 0101, not head, regardless of how many migrations sit
        # above 0101 today.
        command.downgrade(_alembic_config(connection), "0100_crypto_opportunity_lineage")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0101_counterfactual_outcome"
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    # Restore head for any later test sharing this session-scoped fixture.
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
        connection.commit()


def test_downgrade_and_reupgrade_round_trip_when_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 0
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0100_crypto_opportunity_lineage")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0100_crypto_opportunity_lineage"
    with postgres_engine.connect() as connection:
        exists = connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'counterfactual_outcome_aggregate')"
            )
        )
    assert exists is False

    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0101_counterfactual_outcome")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0101_counterfactual_outcome"
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 0


# ---------------------------------------------------------------------------
# Immutability / TRUNCATE
# ---------------------------------------------------------------------------


def test_retained_aggregate_cannot_be_updated_or_deleted(postgres_engine: Engine) -> None:
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    aggregate = _aggregate(aggregate_id="agg-immutable", cohort_id="cohort-1", run_id="run-1")
    repository.save_aggregate(aggregate=aggregate)

    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE counterfactual_outcome_aggregate SET qualification_state = 'QUALIFIED' "
                "WHERE aggregate_id = :id"
            ),
            {"id": "agg-immutable"},
        )

    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text("DELETE FROM counterfactual_outcome_aggregate WHERE aggregate_id = :id"),
            {"id": "agg-immutable"},
        )
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1


def test_truncate_is_not_specially_guarded_matching_the_majority_append_only_pattern(
    postgres_engine: Engine,
) -> None:
    """Documents and proves the deliberate design choice explained in
    migration 0101's own docstring: this table follows the *majority*
    append-only pattern (row-level immutable trigger + downgrade guard),
    not migration 0100's narrower statement-level TRUNCATE guard (which
    exists only because dropping a foreign key regressed two specific
    producer tables). A raw ``TRUNCATE`` from an ordinary connection
    succeeds here -- exactly like ``counterfactual_decision``,
    ``decision_followup_observation``, and every other table in this
    majority group."""

    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    repository.save_aggregate(
        aggregate=_aggregate(aggregate_id="agg-truncate", cohort_id="cohort-1", run_id="run-1")
    )
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1
    with postgres_engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE TABLE counterfactual_outcome_aggregate"))
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 0


# ---------------------------------------------------------------------------
# Repository: idempotency, identity conflict, reads
# ---------------------------------------------------------------------------


def test_save_aggregate_idempotent_replay_and_identity_conflict(postgres_engine: Engine) -> None:
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    aggregate = _aggregate(aggregate_id="agg-replay", cohort_id="cohort-1", run_id="run-1")
    assert repository.save_aggregate(aggregate=aggregate) is False  # newly written
    assert repository.save_aggregate(aggregate=aggregate) is True  # idempotent replay
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    conflicting = replace(
        _aggregate(
            aggregate_id="agg-replay",
            cohort_id="cohort-1",
            run_id="run-1",
            decision_id="decision-DIFFERENT",
        ),
        canonical_hash="f" * 64,  # deliberately different from the retained "e" * 64
    )
    with pytest.raises(
        CounterfactualOutcomeError, match="COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT"
    ):
        repository.save_aggregate(aggregate=conflicting)


def test_earliest_qualified_per_cohort_picks_the_first_by_generated_at(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    base = _aggregate(aggregate_id="agg-day1", cohort_id="cohort-q", run_id="run-day1")
    qualified_early = replace(
        base,
        qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        generated_at=NOW,
    )
    qualified_later = replace(
        qualified_early,
        aggregate_id="agg-day2",
        run_id="run-day2",
        generated_at=NOW + timedelta(days=1),
        canonical_hash="f" * 64,
    )
    insufficient = _aggregate(
        aggregate_id="agg-other-cohort", cohort_id="cohort-i", run_id="run-day1"
    )
    repository.save_aggregate(aggregate=qualified_early)
    repository.save_aggregate(aggregate=qualified_later)
    repository.save_aggregate(aggregate=insufficient)

    result = repository.earliest_qualified_per_cohort()
    assert len(result) == 1
    assert result[0].aggregate_id == "agg-day1"


def test_census_reports_counts_by_state_and_venue(postgres_engine: Engine) -> None:
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    repository.save_aggregate(
        aggregate=_aggregate(aggregate_id="agg-1", cohort_id="cohort-1", run_id="run-1")
    )
    repository.save_aggregate(
        aggregate=_aggregate(aggregate_id="agg-2", cohort_id="cohort-2", run_id="run-1")
    )
    census = repository.census()
    assert census["total_aggregate_rows"] == 2
    assert census["distinct_cohort_count"] == 2
    assert census["by_qualification_state"]["INSUFFICIENT_SAMPLE"] == 2
    assert census["by_venue"]["CRYPTO"] == 2


# ---------------------------------------------------------------------------
# Application service against real repositories
# ---------------------------------------------------------------------------


def test_refresh_service_end_to_end_against_real_repositories(postgres_engine: Engine) -> None:
    projection_id = _seed_projection(
        postgres_engine, identity="refresh-1", strategy_or_method_id="REAL_METHOD_V1"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = _decision(decision_id="refresh-decision-1", projection_id=projection_id)
    ledger.save_decision(decision=decision)
    ledger.save_classification(classification=_classification(decision_id=decision.decision_id))

    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observation = _observation(decision_id=decision.decision_id, subsequent_return=Decimal("0.05"))
    followup.save_observation(observation=observation)
    followup.save_assessment(assessment=_assessment(observation=observation))

    source = RetainedCounterfactualOutcomeSource(postgres_engine)
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    service = RefreshCounterfactualOutcomeAggregates(source, repository)

    first = service.execute(run_id="test-run-1", generated_at=NOW)
    assert first.considered_count == 1
    assert first.newly_written_count == 1
    assert first.idempotent_replay_count == 0
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    stored = repository.aggregate(first.aggregate_ids[0])
    assert stored is not None
    assert stored.strategy_or_method_id == "REAL_METHOD_V1"
    assert stored.contributing_decision_ids == (decision.decision_id,)
    assert stored.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE

    # Replay: identical run_id -> idempotent, no duplicate row.
    second = service.execute(run_id="test-run-1", generated_at=NOW + timedelta(minutes=5))
    assert second.newly_written_count == 0
    assert second.idempotent_replay_count == 1
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    # A new day's run_id adds a fresh, append-only sibling row.
    third = service.execute(run_id="test-run-2", generated_at=NOW + timedelta(days=1))
    assert third.newly_written_count == 1
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 2


def test_refresh_service_excludes_evidence_ineligible_observations(postgres_engine: Engine) -> None:
    projection_id = _seed_projection(
        postgres_engine, identity="refresh-ineligible", strategy_or_method_id="REAL_METHOD_V1"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = _decision(decision_id="refresh-decision-ineligible", projection_id=projection_id)
    ledger.save_decision(decision=decision)
    ledger.save_classification(classification=_classification(decision_id=decision.decision_id))

    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observation = _observation(decision_id=decision.decision_id, subsequent_return=Decimal("0.05"))
    followup.save_observation(observation=observation)
    ineligible_assessment = FollowUpEvidenceQualityAssessment(
        assessment_id=f"{observation.observation_id}-assessment",
        observation_id=observation.observation_id,
        observation_canonical_hash=observation.canonical_hash,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        baseline_evidence_class=observation.baseline_evidence_class,
        target_evidence_class=observation.target_evidence_class,
        verdict=FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE,
        reason_code="TEST_INELIGIBLE",
        evidence_defect_type=FollowUpEvidenceDefectType.TARGET_EVIDENCE_INELIGIBLE,
        assessed_at=NOW,
        canonical_hash=f"{observation.observation_id}-assessment-hash".ljust(64, "0")[:64],
        authority="NONE",
    )
    followup.save_assessment(assessment=ineligible_assessment)

    source = RetainedCounterfactualOutcomeSource(postgres_engine)
    repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    service = RefreshCounterfactualOutcomeAggregates(source, repository)
    result = service.execute(run_id="test-run-ineligible", generated_at=NOW)
    stored = repository.aggregate(result.aggregate_ids[0])
    assert stored is not None
    assert stored.eligible_observation_count == 0
    assert stored.evidence_ineligible_observation_count == 1
    assert stored.contributing_decision_ids == ()
