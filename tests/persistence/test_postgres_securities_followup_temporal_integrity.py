"""Securities Follow-Up Temporal Integrity v1 -- real-Postgres proofs.

Drives the genuine bridge chain (retained structure candidate -> Shared
Opportunity projection -> counterfactual decision -> terminal options-chain
snapshot -> follow-up observation) through the production repositories, then
assesses it under the frozen V1 and the new SECURITIES-only V2 evidence-quality
policies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.operating_cycle_tasks import _BoundedUnassessedFollowUpEvidenceSource
from apex.adapters.securities_structure_outcome_postgres import (
    PostgresSecuritiesStructureOutcomeRepository,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.application.counterfactual_followup import (
    AssessFollowUpEvidenceQuality,
    SyncFollowUpObservations,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    DecisionFollowUpObservation,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    SecuritiesTerminalTemporalStatus,
)
from apex.domain.counterfactual_ledger import build_decision_from_eligibility
from apex.domain.securities_structure_outcome import build_securities_structure_outcome_aggregates
from apex.domain.shared_opportunity import SharedVenue, build_opportunity_set
from tests.persistence.test_postgres_securities_shared_opportunity_bridge import (
    NOW,
    _NeverCalledKalshiSource,
    _persist_full_session,
)

ROOT = Path(__file__).resolve().parents[2]
# ``NOW`` is Thu 2026-08-20 20:00Z (an XNYS close). A SHORT_HORIZON target is
# therefore Fri 2026-08-21 20:00Z, inside Friday's own [20:00, 20:15] window.
FRIDAY_ON_TIME = NOW + timedelta(days=1, seconds=30)
# The Friday window missed; the next capture is Mon 2026-08-24 (one session late).
MONDAY_LATE = NOW + timedelta(days=4, seconds=30)


def _config(connection: sa.Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _seed_short_observation(
    engine: Engine, *, terminal_captured_at: datetime, set_id: str
) -> DecisionFollowUpObservation:
    _persist_full_session(engine, captured_at=NOW)
    source = RetainedSharedOpportunitySource(engine)
    (projection, *_rest) = source.securities_projections(as_of=NOW)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=set_id, created_at=NOW, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"{set_id}-key",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    decision = build_decision_from_eligibility(
        decision=decisions[0], projection=projection, opportunity_set_id=set_id
    )
    PostgresCounterfactualLedgerRepository(engine).save_decision(decision=decision)
    _persist_full_session(engine, captured_at=terminal_captured_at)

    repository = PostgresCounterfactualFollowUpRepository(engine)
    SyncFollowUpObservations(
        repository.ledger,
        repository.subjects,
        repository.crypto_prices,
        kalshi_source=_NeverCalledKalshiSource(),
        repository=repository,
        securities_prices=repository.securities_prices,
    ).execute(as_of=terminal_captured_at + timedelta(hours=1), policy_version="FOLLOWUP_POLICY_V2")
    short = [
        item
        for item in repository.observations()
        if item.venue is SharedVenue.SECURITIES
        and item.horizon_kind is FollowUpHorizonKind.SHORT_HORIZON
    ]
    assert len(short) == 1
    return short[0]


def _raw_assessment_rows(engine: Engine, version: str) -> list[tuple[str, str, str]]:
    with engine.connect() as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                sa.text(
                    "SELECT assessment_id, payload, canonical_hash "
                    "FROM followup_evidence_quality_assessment "
                    "WHERE assessment_policy_version = :version ORDER BY assessment_id"
                ),
                {"version": version},
            )
        ]


@pytest.mark.postgres
def test_on_time_terminal_snapshot_is_v2_eligible(postgres_engine: Engine) -> None:
    observation = _seed_short_observation(
        postgres_engine, terminal_captured_at=FRIDAY_ON_TIME, set_id="on-time"
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    assessment = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    assert assessment is not None
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    timing = assessment.securities_terminal_timing
    assert timing is not None
    assert timing.eligible_session_date == "2026-08-21"
    assert timing.terminal_captured_at == FRIDAY_ON_TIME


@pytest.mark.postgres
def test_late_terminal_snapshot_is_rejected_and_v1_row_stays_byte_identical(
    postgres_engine: Engine,
) -> None:
    observation = _seed_short_observation(
        postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="late"
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)

    # Before: the production state -- V1 stamps it ELIGIBLE.
    AssessFollowUpEvidenceQuality(repository, repository).execute(assessed_at=MONDAY_LATE)
    v1_before = _raw_assessment_rows(postgres_engine, FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1)
    v1 = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert v1 is not None and v1.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert v1.securities_terminal_timing is None

    # After: V2 rejects it, explains why, and changes no V1 row.
    result = AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE + timedelta(days=1),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    assert result.ineligible_count >= 1
    v2 = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    assert v2 is not None
    assert v2.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert v2.evidence_defect_type is FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID
    timing = v2.securities_terminal_timing
    assert timing is not None
    assert timing.status is SecuritiesTerminalTemporalStatus.AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW
    assert timing.eligible_session_date == "2026-08-21"
    assert timing.terminal_captured_at == MONDAY_LATE
    assert timing.terminal_snapshot_id == observation.source_lineage[-1]
    assert _raw_assessment_rows(postgres_engine, FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1) == v1_before
    # The observation itself is untouched.
    assert repository.observation(observation.observation_id) == observation

    # Replay: an identical re-run writes nothing and conflicts with nothing.
    replay = AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE + timedelta(days=30),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    assert replay.created_count == 0
    assert replay.idempotent_replay_count == result.considered_observations


@pytest.mark.postgres
def test_retained_v2_row_is_immutable(postgres_engine: Engine) -> None:
    _seed_short_observation(postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="immutable")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    with pytest.raises(sa.exc.DBAPIError), postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE followup_evidence_quality_assessment SET verdict = 'ELIGIBLE', "
                "evidence_defect_type = 'NONE' WHERE assessment_policy_version = :version"
            ),
            {"version": FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2},
        )


@pytest.mark.postgres
def test_v2_backlog_is_securities_only_and_drains(postgres_engine: Engine) -> None:
    _seed_short_observation(postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="backlog")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    lane = _BoundedUnassessedFollowUpEvidenceSource(
        repository,
        100,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
        venue=SharedVenue.SECURITIES,
    )
    pending = lane.observations()
    assert pending and all(item.venue is SharedVenue.SECURITIES for item in pending)
    AssessFollowUpEvidenceQuality(lane, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    assert lane.observations() == ()


@pytest.mark.postgres
def test_v1_row_can_never_carry_a_v2_only_defect(postgres_engine: Engine) -> None:
    observation = _seed_short_observation(
        postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="constraint"
    )
    with pytest.raises(sa.exc.IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO followup_evidence_quality_assessment (assessment_id, observation_id, "
                "assessment_policy_version, verdict, evidence_defect_type, assessed_at, payload, "
                "canonical_hash, authority) VALUES ('forged', :observation_id, :version, "
                "'INELIGIBLE_EVIDENCE', 'TARGET_EVIDENCE_TEMPORALLY_INVALID', now(), '{}', "
                ":hash, 'NONE')"
            ),
            {
                "observation_id": observation.observation_id,
                "version": FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
                "hash": "f" * 64,
            },
        )


@pytest.mark.postgres
def test_v2_aggregate_persists_excluding_the_late_row(postgres_engine: Engine) -> None:
    observation = _seed_short_observation(
        postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="aggregate"
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    service = AssessFollowUpEvidenceQuality(repository, repository)
    service.execute(assessed_at=MONDAY_LATE)
    service.execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    decision = repository.ledger.decision(observation.decision_id)
    assert decision is not None
    projections = repository.subjects.projections_by_id({decision.subject_projection_id})
    projection = projections[decision.subject_projection_id]
    outcome_repository = PostgresSecuritiesStructureOutcomeRepository(postgres_engine)
    for version, expected_eligible in (
        (FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1, 1),
        (FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2, 0),
    ):
        aggregates = build_securities_structure_outcome_aggregates(
            decisions=(decision,),
            observations=repository.observations(),
            assessments=repository.assessments(),
            strategy_or_method_id_by_projection={
                projection.projection_id: projection.strategy_or_method_id
            },
            structure_type_by_projection={projection.projection_id: projection.direction},
            run_id="cycle-temporal-integrity",
            generated_at=MONDAY_LATE,
            evidence_quality_policy_version=version,
        )
        short = next(
            item for item in aggregates if item.horizon_kind is FollowUpHorizonKind.SHORT_HORIZON
        )
        assert short.eligible_observation_count == expected_eligible
        assert outcome_repository.save_aggregate(aggregate=short) is False
        assert outcome_repository.aggregate(short.aggregate_id) == short


@pytest.mark.postgres
def test_0116_downgrade_refuses_with_retained_v2_evidence(postgres_engine: Engine) -> None:
    _seed_short_observation(postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="guard")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="cannot downgrade 0116"):
            command.downgrade(_config(connection), "0115_strategy_provenance")
        connection.rollback()


@pytest.mark.postgres
def test_0116_downgrade_and_upgrade_round_trip_when_empty(postgres_engine: Engine) -> None:
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0115_strategy_provenance")
        connection.commit()
    with postgres_engine.connect() as connection:
        clause = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_followup_evidence_quality_policy_version'"
            )
        )
        assert clause is not None and "POLICY_V2" not in clause
        command.upgrade(_config(connection), "head")
        connection.commit()
    with postgres_engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        clause = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_securities_structure_outcome_evidence_quality_policy_version'"
            )
        )
    assert revision == "0118_evidence_gap_recovery"
    assert clause is not None and "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2" in clause


@pytest.mark.postgres
def test_no_authority_bearing_state_is_created(postgres_engine: Engine) -> None:
    _seed_short_observation(postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="authority")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    with postgres_engine.connect() as connection:
        authorities = set(
            connection.scalars(
                sa.text("SELECT DISTINCT authority FROM followup_evidence_quality_assessment")
            )
        )
    assert authorities == {"NONE"}
    assert all(item.authority == "NONE" for item in repository.assessments())


@pytest.mark.postgres
def test_recurring_cycle_writes_v2_for_securities_beside_unchanged_v1(
    postgres_engine: Engine,
) -> None:
    from tests.persistence.test_postgres_counterfactual_followup_recurring import (
        _due_task,
        _executor,
        _FakeRecurringKalshiSource,
        _register_plan,
    )

    observation = _seed_short_observation(
        postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="recurring"
    )
    plan = _register_plan(postgres_engine, plan_id="securities-temporal-integrity-plan-v1")
    executed_at = MONDAY_LATE + timedelta(hours=2)
    kalshi = _FakeRecurringKalshiSource()
    result = _executor(postgres_engine, kalshi_source=kalshi).execute(
        task=_due_task("cycle-securities-temporal-1"),
        cycle_id="cycle-securities-temporal-1",
        plan=plan,
        as_of=executed_at,
        executed_at=executed_at,
    )
    assert result.status.value != "FAILED"
    assert not any("SWEEP_FAILED" in warning for warning in result.warnings)
    assert kalshi.call_count == 0

    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    v1 = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    v2 = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    assert v1 is not None and v1.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert v2 is not None and v2.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert any(
        warning.startswith("FOLLOWUP_EVIDENCE_QUALITY_V2_INELIGIBLE:")
        for warning in result.warnings
    )
    # V2 rows exist only for SECURITIES observations.
    v2_observation_ids = {
        item.observation_id
        for item in repository.assessments(
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
        )
    }
    venues = {
        item.venue
        for item in repository.observations()
        if item.observation_id in v2_observation_ids
    }
    assert venues == {SharedVenue.SECURITIES}

    # A second cycle replays: nothing new written, no conflict.
    replay = _executor(postgres_engine, kalshi_source=kalshi).execute(
        task=_due_task("cycle-securities-temporal-2"),
        cycle_id="cycle-securities-temporal-2",
        plan=plan,
        as_of=executed_at + timedelta(hours=1),
        executed_at=executed_at + timedelta(hours=1),
    )
    assert "FOLLOWUP_EVIDENCE_QUALITY_V2_ASSESSED:0" in replay.warnings
