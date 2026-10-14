from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
from apex.adapters.performance_attribution_acceptance import _source
from apex.adapters.performance_attribution_postgres import (
    PostgresPerformanceAttributionRepository,
)
from apex.application.performance_attribution import (
    MaterializePerformanceAttribution,
    MaterializeStrategyHealth,
)
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskStatus,
    OperatingTaskType,
)
from apex.domain.performance_attribution import (
    PaperEvidenceClass,
    PerformanceAttributionError,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 8, 1, 16, tzinfo=UTC)


class _Source:
    def __init__(self) -> None:
        self.values = tuple(
            replace(
                _source(f"postgres-source-{index}"), evidence_class=PaperEvidenceClass.GENUINE_PAPER
            )
            for index in range(1, 4)
        )

    def snapshots(self, evidence_class: PaperEvidenceClass):
        return self.values if evidence_class is PaperEvidenceClass.GENUINE_PAPER else ()

    def snapshot(self, source_id: str, evidence_class: PaperEvidenceClass):
        return next(
            (item for item in self.snapshots(evidence_class) if item.source_id == source_id), None
        )


def test_attribution_health_and_summaries_are_immutable_idempotent_and_nonfinancial(
    postgres_engine: sa.Engine,
) -> None:
    financial_tables = (
        '"order"',
        "position",
        "risk_reservation",
        "execution_event",
        "ledger_journal",
        "ledger_posting",
        "external_paper_instruction",
        "external_paper_validated_fill",
    )
    with postgres_engine.connect() as connection:
        before = {
            table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
            for table in financial_tables
        }
    repository = PostgresPerformanceAttributionRepository(postgres_engine)
    source = _Source()
    service = MaterializePerformanceAttribution(repository, source)
    first = service.execute(
        run_id="postgres-performance",
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        materialized_at=NOW,
        idempotency_key="postgres-performance-key",
    )
    replay = service.execute(
        run_id="postgres-performance-replay-identity-is-ignored-for-exact-source",
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        materialized_at=NOW,
        idempotency_key="postgres-performance-replay-key",
    )
    assert len(first.attributions) == len(replay.replayed_attribution_ids) == 3
    assert first.provider_requests == first.broker_requests == first.financial_mutations == 0
    assert all(item.authority == item.live_authority == "NONE" for item in first.attributions)
    health = MaterializeStrategyHealth(repository).execute(
        run_id="postgres-performance",
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        as_of=NOW,
        idempotency_key="postgres-performance-health-key",
    )
    health_replay = MaterializeStrategyHealth(repository).execute(
        run_id="postgres-performance",
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        as_of=NOW,
        idempotency_key="postgres-performance-health-key",
    )
    assert len(health.assessments) == 1
    assert len(health.method_summaries) == 1
    assert len(health.venue_summaries) == 3
    assert len(health_replay.replayed_ids) == 6
    assert (
        repository.portfolio_summary(PaperEvidenceClass.GENUINE_PAPER) == health.portfolio_summary
    )
    with pytest.raises(sa.exc.DBAPIError), postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE shared_performance_attribution SET authority = 'PAPER_ONLY' "
                "WHERE attribution_id = :identity"
            ),
            {"identity": first.attributions[0].attribution_id},
        )
    with postgres_engine.connect() as connection:
        after = {
            table: int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
            for table in financial_tables
        }
    assert before == after


def test_operating_cycle_delegates_attribution_without_provider_io(
    postgres_engine: sa.Engine,
) -> None:
    operating = PostgresOperatingCycleRepository(postgres_engine)
    state_source = PostgresOperatingStateSource(postgres_engine, operating)
    executor = ExistingCapabilityOperatingTaskExecutor(postgres_engine, state_source)
    plan = OperatingCyclePlan.create(
        plan_id="performance-postgres-operating-plan",
        crypto_pulse_plan_id="performance-postgres-pulse-plan",
        actor="fixture-operator",
        created_at=NOW,
    )
    task = DueTask.create(
        task_type=OperatingTaskType.PERFORMANCE_ATTRIBUTION_REBUILD,
        disposition=DueDisposition.DUE,
        reasons=("RETAINED_PAPER_EVIDENCE_CHANGED",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id="performance-postgres-cycle",
        network_behavior="RETAINED_PAPER_EVIDENCE_ONLY_ZERO_PROVIDER_REQUESTS",
    )
    first = executor.execute(
        task=task,
        cycle_id="performance-postgres-cycle",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    replay = executor.execute(
        task=task,
        cycle_id="performance-postgres-cycle",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert first.status is OperatingTaskStatus.COMPLETED
    assert first.network_requests == 0
    assert "ATTRIBUTION_USED_RETAINED_EVIDENCE_ONLY" in first.warnings
    assert replay.status is OperatingTaskStatus.REUSED
    assert replay.changed is False
    assert replay.network_requests == 0


def test_attribution_conflicts_and_downgrade_refuses_retained_rows(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresPerformanceAttributionRepository(postgres_engine)
    source = _Source()
    result = MaterializePerformanceAttribution(repository, source).execute(
        run_id="postgres-conflict",
        evidence_class=PaperEvidenceClass.GENUINE_PAPER,
        materialized_at=NOW,
        idempotency_key="postgres-conflict-key",
        source_ids=(source.values[0].source_id,),
    )
    with pytest.raises(PerformanceAttributionError, match="IDENTITY_CONFLICT"):
        repository.save_attribution(
            idempotency_key="different-key",
            attribution=result.attributions[0],
            components=result.components,
        )
    with postgres_engine.connect() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0064"):
            command.downgrade(config, "0063_cross_venue_allocation")
        connection.rollback()
