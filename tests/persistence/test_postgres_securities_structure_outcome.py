"""PostgreSQL persistence proofs for Securities Structure Forward Performance
v1: migration 0105, ``PostgresSecuritiesStructureOutcomeRepository``, and
``RefreshSecuritiesStructureOutcomeAggregates`` against a real, freshly
migrated ``apex_test`` database. Mirrors ``test_postgres_counterfactual_
outcome.py`` exactly in structure and coverage."""

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
from apex.adapters.counterfactual_outcome_sources import RetainedCounterfactualOutcomeSource
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.securities_structure_outcome import (
    RefreshSecuritiesStructureOutcomeAggregates,
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
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities_structure_outcome import (
    SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
    SecuritiesStructureOutcomeAggregate,
    SecuritiesStructureOutcomeError,
    SecuritiesStructureOutcomeQualificationState,
)
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 24, tzinfo=UTC)
_METHOD_ID = "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"
_STRUCTURE_TYPE = "CALL_CREDIT_VERTICAL"


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _projection(
    identity: str, *, strategy_or_method_id: str = _METHOD_ID
) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"sso-proj-{identity}",
        venue=SharedVenue.SECURITIES,
        source_record_type="OptionStructureCandidate",
        source_record_id=f"sso-source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id=strategy_or_method_id,
        instrument_id="XLK",
        product_or_market_id=f"structure-{identity}",
        direction=_STRUCTURE_TYPE,
        generated_at=NOW,
        valid_until=NOW + timedelta(days=60),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=345600,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value=None,
        comparable_value_identity=None,
        uncertainty=None,
        maximum_loss="50",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.5",
        expected_holding_days=7,
        estimated_lockup_days=7,
        supporting_evidence_ids=(f"sso-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


def _seed_projection(
    engine: Engine, *, identity: str, strategy_or_method_id: str = _METHOD_ID
) -> str:
    projection = _projection(identity, strategy_or_method_id=strategy_or_method_id)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"sso-set-{identity}", created_at=NOW, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"sso-set-{identity}-bundle",
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
        origin_state="ELIGIBLE",
        subject_projection_id=projection_id,
        subject_source_record_type="OptionStructureCandidate",
        subject_source_record_id=f"{decision_id}-record",
        venue=SharedVenue.SECURITIES,
        opportunity_set_id=None,
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=NOW,
        disposition=DecisionDisposition.ADVANCED,
        raw_reason_codes=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
        explanation="test decision",
        canonical_hash="b" * 64,
        authority="NONE",
    )


def _observation(*, decision_id: str, subsequent_return: Decimal) -> DecisionFollowUpObservation:
    observation_id = f"{decision_id}-obs"
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        venue=SharedVenue.SECURITIES,
        disposition=DecisionDisposition.ADVANCED,
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        decision_evaluated_at=NOW,
        baseline_time=NOW,
        target_time=NOW,
        observation_type=FollowUpObservationType.SECURITIES_STRUCTURE_TERMINAL_PAYOFF,
        subject_instrument_id="XLK",
        subject_product_or_market_id="structure-candidate-1",
        provenance=EvidenceClass.GENUINE_PUBLIC,
        baseline_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version=None,
        observed_at=NOW,
        source_lineage=("batch-1",),
        data_limitations=(
            "SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT",
        ),
        reference_price_at_baseline=Decimal("187.5"),
        reference_price_at_target=Decimal("185"),
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
        reason_code="SECURITIES_OBSERVATION_ALWAYS_GENUINE_PUBLIC_ALPACA_CAPTURE",
        evidence_defect_type=FollowUpEvidenceDefectType.NONE,
        assessed_at=NOW,
        canonical_hash=f"{observation.observation_id}-assessment-hash".ljust(64, "0")[:64],
        authority="NONE",
    )


def _aggregate(
    *, aggregate_id: str, cohort_id: str, run_id: str, decision_id: str = "decision-1"
) -> SecuritiesStructureOutcomeAggregate:
    return SecuritiesStructureOutcomeAggregate(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
        run_id=run_id,
        strategy_or_method_id=_METHOD_ID,
        structure_type=_STRUCTURE_TYPE,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        generated_at=NOW,
        total_decisions_considered=1,
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
        qualification_state=SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE,
        contributing_decision_ids=(decision_id,),
        explanation="test aggregate",
        canonical_hash="e" * 64,
        authority="NONE",
    )


# ---------------------------------------------------------------------------
# Migration shape and downgrade guard
# ---------------------------------------------------------------------------


def test_migration_creates_the_table_and_it_is_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 0


def test_downgrade_refuses_with_retained_evidence(postgres_engine: Engine) -> None:
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    aggregate = _aggregate(aggregate_id="agg-1", cohort_id="cohort-1", run_id="run-1")
    assert repository.save_aggregate(aggregate=aggregate) is False
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 1

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0118_evidence_gap_recovery"

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0105 with retained securities-structure-outcome evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0104_securities_bridge_v1")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # The downgrade sequence ran 0106->0105 (succeeds) then attempted
    # 0105->0104, which is exactly migration 0105's own downgrade() body --
    # that step is what raises above, so the retained state lands one step
    # short of the requested target, at 0105, not at head.
    assert current_revision == "0105_securities_structure_perf"
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 1


def test_downgrade_and_reupgrade_round_trip_when_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 0
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0104_securities_bridge_v1")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0104_securities_bridge_v1"
    with postgres_engine.connect() as connection:
        exists = connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'securities_structure_outcome_aggregate')"
            )
        )
    assert exists is False
    # The two widened venue CHECK constraints must also have reverted to
    # their pre-0105 narrow form.
    with postgres_engine.connect() as connection:
        definition = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'research_agenda_item_venue_check'"
            )
        )
    assert definition is not None
    assert "SECURITIES" not in definition

    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0118_evidence_gap_recovery"
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 0
    with postgres_engine.connect() as connection:
        definition = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'research_agenda_item_venue_check'"
            )
        )
    assert definition is not None
    assert "SECURITIES" in definition


# ---------------------------------------------------------------------------
# CHECK constraints genuinely enforced (not merely declared)
# ---------------------------------------------------------------------------


_RAW_INSERT = sa.text(
    "INSERT INTO securities_structure_outcome_aggregate "
    "(aggregate_id, cohort_id, outcome_policy_version, run_id, strategy_or_method_id, "
    "structure_type, horizon_kind, baseline_kind, followup_policy_version, "
    "evidence_quality_policy_version, qualification_state, total_decisions_considered, "
    "mature_observation_count, eligible_observation_count, generated_at, payload, "
    "canonical_hash, authority) "
    "VALUES (:aggregate_id, :cohort_id, :outcome_policy_version, :run_id, :method_id, "
    ":structure_type, :horizon_kind, :baseline_kind, :followup_policy_version, "
    ":evidence_quality_policy_version, :qualification_state, 0, 0, 0, :now, '{}', :hash, 'NONE')"
)


def _raw_insert_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "aggregate_id": "bad-row",
        "cohort_id": "cohort-bad",
        "outcome_policy_version": SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
        "run_id": "run-bad",
        "method_id": _METHOD_ID,
        "structure_type": _STRUCTURE_TYPE,
        "horizon_kind": FollowUpHorizonKind.SHORT_HORIZON.value,
        "baseline_kind": FollowUpBaselineKind.DECISION_TIME.value,
        "followup_policy_version": FOLLOWUP_POLICY_V2,
        "evidence_quality_policy_version": FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        "qualification_state": (
            SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE.value
        ),
        "now": NOW,
        "hash": "0" * 64,
    }
    params.update(overrides)
    return params


def test_structure_type_check_constraint_rejects_unknown_value(postgres_engine: Engine) -> None:
    with (
        pytest.raises(Exception, match="ck_securities_structure_outcome_structure_type"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(_RAW_INSERT, _raw_insert_params(structure_type="IRON_CONDOR"))


def test_baseline_kind_check_constraint_rejects_origin_evidence(postgres_engine: Engine) -> None:
    """Confirms the deliberately narrow CHECK -- ``ORIGIN_EVIDENCE`` is a
    real, legal ``FollowUpBaselineKind`` member (CRYPTO uses it), but is
    genuinely never legal on this SECURITIES-only table."""

    with (
        pytest.raises(Exception, match="ck_securities_structure_outcome_baseline_kind"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            _RAW_INSERT,
            _raw_insert_params(baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE.value),
        )


def test_qualification_state_check_constraint_rejects_venue_unavailable(
    postgres_engine: Engine,
) -> None:
    """``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` is a real, legal
    ``CounterfactualOutcomeQualificationState`` member on the sibling table
    -- genuinely never legal here (see the domain module docstring for
    why)."""

    with (
        pytest.raises(Exception, match="ck_securities_structure_outcome_qualification_state"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            _RAW_INSERT,
            _raw_insert_params(qualification_state="VENUE_ECONOMIC_REDUCTION_UNAVAILABLE"),
        )


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


def test_retained_aggregate_cannot_be_updated_or_deleted(postgres_engine: Engine) -> None:
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    aggregate = _aggregate(aggregate_id="agg-immutable", cohort_id="cohort-1", run_id="run-1")
    repository.save_aggregate(aggregate=aggregate)

    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE securities_structure_outcome_aggregate "
                "SET qualification_state = 'QUALIFIED' WHERE aggregate_id = :id"
            ),
            {"id": "agg-immutable"},
        )

    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text("DELETE FROM securities_structure_outcome_aggregate WHERE aggregate_id = :id"),
            {"id": "agg-immutable"},
        )
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 1


# ---------------------------------------------------------------------------
# Repository: idempotency, identity conflict, reads
# ---------------------------------------------------------------------------


def test_save_aggregate_idempotent_replay_and_identity_conflict(postgres_engine: Engine) -> None:
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    aggregate = _aggregate(aggregate_id="agg-replay", cohort_id="cohort-1", run_id="run-1")
    assert repository.save_aggregate(aggregate=aggregate) is False  # newly written
    assert repository.save_aggregate(aggregate=aggregate) is True  # idempotent replay
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 1

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
        SecuritiesStructureOutcomeError, match="SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT"
    ):
        repository.save_aggregate(aggregate=conflicting)


def test_earliest_qualified_per_cohort_picks_the_first_by_generated_at(
    postgres_engine: Engine,
) -> None:
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    base = _aggregate(aggregate_id="agg-day1", cohort_id="cohort-q", run_id="run-day1")
    qualified_early = replace(
        base,
        qualification_state=SecuritiesStructureOutcomeQualificationState.QUALIFIED,
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


def test_census_reports_counts_by_state_and_structure_type(postgres_engine: Engine) -> None:
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    repository.save_aggregate(
        aggregate=_aggregate(aggregate_id="agg-1", cohort_id="cohort-1", run_id="run-1")
    )
    repository.save_aggregate(
        aggregate=_aggregate(aggregate_id="agg-2", cohort_id="cohort-2", run_id="run-1")
    )
    census = repository.census()
    assert census["total_aggregates"] == 2
    assert census["by_qualification_state"]["INSUFFICIENT_SAMPLE"] == 2
    assert census["by_structure_type"][_STRUCTURE_TYPE] == 2


# ---------------------------------------------------------------------------
# Application service against real repositories
# ---------------------------------------------------------------------------


def test_refresh_service_end_to_end_against_real_repositories(postgres_engine: Engine) -> None:
    projection_id = _seed_projection(postgres_engine, identity="refresh-1")
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = _decision(decision_id="refresh-decision-1", projection_id=projection_id)
    ledger.save_decision(decision=decision)

    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observation = _observation(decision_id=decision.decision_id, subsequent_return=Decimal("0.05"))
    followup.save_observation(observation=observation)
    followup.save_assessment(assessment=_assessment(observation=observation))

    source = RetainedCounterfactualOutcomeSource(postgres_engine)
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    service = RefreshSecuritiesStructureOutcomeAggregates(source, repository)

    first = service.execute(
        run_id="test-run-1",
        generated_at=NOW,
        # These fixtures carry V1 assessments; V2 is the canonical default.
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert first.considered_count == 1
    assert first.newly_written_count == 1
    assert first.idempotent_replay_count == 0
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 1

    stored = repository.aggregate(first.aggregate_ids[0])
    assert stored is not None
    assert stored.strategy_or_method_id == _METHOD_ID
    assert stored.structure_type == _STRUCTURE_TYPE
    assert stored.contributing_decision_ids == (decision.decision_id,)
    assert (
        stored.qualification_state
        is SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )

    # Replay: identical run_id -> idempotent, no duplicate row.
    second = service.execute(
        run_id="test-run-1",
        generated_at=NOW + timedelta(minutes=5),
        # These fixtures carry V1 assessments; V2 is the canonical default.
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert second.newly_written_count == 0
    assert second.idempotent_replay_count == 1
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 1

    # A new day's run_id adds a fresh, append-only sibling row.
    third = service.execute(
        run_id="test-run-2",
        generated_at=NOW + timedelta(days=1),
        # These fixtures carry V1 assessments; V2 is the canonical default.
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert third.newly_written_count == 1
    assert _row_count(postgres_engine, "securities_structure_outcome_aggregate") == 2


def test_refresh_service_excludes_rejected_decision_through_real_read_path(
    postgres_engine: Engine,
) -> None:
    """Real, retained-database proof that a REJECTED eligibility decision
    for the same underlying structure never contributes, alongside one
    genuine ADVANCED/SHARED_OPPORTUNITY_ELIGIBILITY decision that does --
    exercising the actual Postgres read path
    (``RetainedCounterfactualOutcomeSource``), not just the pure domain
    filter (see ``test_mandate_origin_decision_never_contributes`` and
    ``test_rejected_securities_decision_never_contributes`` in ``tests/
    test_securities_structure_outcome.py`` for that in-memory proof)."""

    projection_id = _seed_projection(postgres_engine, identity="refresh-excl")
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)

    real = _decision(decision_id="excl-real", projection_id=projection_id)
    ledger.save_decision(decision=real)

    rejected = replace(
        real,
        decision_id="excl-rejected",
        disposition=DecisionDisposition.NOT_ADVANCED,
        origin_state="REJECTED",
        raw_reason_codes=("SOURCE:LIQUIDITY_INSUFFICIENT",),
        canonical_hash="c" * 64,
    )
    ledger.save_decision(decision=rejected)
    # A genuine INVESTMENT_MANDATE_EVALUATION-origin row additionally
    # requires ``mandate_id``/``portfolio_snapshot_id`` to satisfy real
    # foreign keys into ``investment_mandate``/``portfolio_snapshot``
    # (``ck_counterfactual_decision_origin_lineage`` plus two FKs) --
    # exactly the kind of incidental construction complexity this
    # persistence proof does not need to take on: the origin/disposition
    # exclusion filter itself is a pure domain-layer decision, already
    # proven directly and exhaustively by ``test_mandate_origin_decision_
    # never_contributes`` in ``tests/test_securities_structure_outcome.py``
    # with zero DB dependency. This test's own job is narrower and
    # different -- proving the *read path* through a real, retained
    # Postgres decision correctly excludes a REJECTED one -- which the
    # ``rejected`` row above already exercises.

    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observation = _observation(decision_id=real.decision_id, subsequent_return=Decimal("0.05"))
    followup.save_observation(observation=observation)
    followup.save_assessment(assessment=_assessment(observation=observation))

    source = RetainedCounterfactualOutcomeSource(postgres_engine)
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    service = RefreshSecuritiesStructureOutcomeAggregates(source, repository)
    result = service.execute(
        run_id="test-run-excl",
        generated_at=NOW,
        # These fixtures carry V1 assessments; V2 is the canonical default.
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )

    assert len(result.aggregate_ids) == 1
    stored = repository.aggregate(result.aggregate_ids[0])
    assert stored is not None
    assert stored.total_decisions_considered == 1
    assert stored.contributing_decision_ids == (real.decision_id,)


def test_refresh_service_excludes_evidence_ineligible_observations(postgres_engine: Engine) -> None:
    projection_id = _seed_projection(postgres_engine, identity="refresh-ineligible")
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = _decision(decision_id="refresh-decision-ineligible", projection_id=projection_id)
    ledger.save_decision(decision=decision)

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
    repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    service = RefreshSecuritiesStructureOutcomeAggregates(source, repository)
    result = service.execute(
        run_id="test-run-ineligible",
        generated_at=NOW,
        # These fixtures carry V1 assessments; V2 is the canonical default.
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    stored = repository.aggregate(result.aggregate_ids[0])
    assert stored is not None
    assert stored.eligible_observation_count == 0
    assert stored.evidence_ineligible_observation_count == 1
    assert stored.contributing_decision_ids == ()
