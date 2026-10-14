"""Cross-milestone proofs for the post-close integration of Strategy Version
Provenance v1 (``0115_strategy_provenance``) and Securities Follow-Up Temporal
Integrity v1 (``0116_securities_followup_lag``).

Each milestone already proves its own behavior. These tests prove only what
stacking them adds: a linear chain, exact-revision compatibility at every
step, a 0116 that leaves every provenance object untouched, correct guard
ordering on downgrade, and a SECURITIES V2 assessment that never reaches
strategy provenance or strategy PAPER trial state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.schema_compatibility_postgres import require_compatible_schema
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    STRATEGY_VERSION_PROVENANCE_TABLES,
)
from apex.application.counterfactual_followup import AssessFollowUpEvidenceQuality
from apex.application.strategy_paper_trial_lifecycle import RecordStrategyPaperTrialOpened
from apex.application.strategy_version_provenance import RecordProvenancedTrialOpened
from apex.domain.counterfactual_followup import FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
from apex.domain.schema_compatibility import (
    REQUIRED_SCHEMA_REVISION,
    SchemaCompatibilityError,
    SchemaCompatibilityStatus,
)
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks
from tests.persistence.test_postgres_securities_followup_temporal_integrity import (
    MONDAY_LATE,
    _seed_short_observation,
)
from tests.persistence.test_postgres_strategy_version_provenance import NOW, _chain, _trial

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
R0114 = "0114_outcome_specificity_v3"
R0115 = "0115_strategy_provenance"
R0116 = "0116_securities_followup_lag"
R0117 = "0117_trial_outcome_assessment"
R0118 = "0118_evidence_gap_recovery"
TRIAL_TABLE = "internal_strategy_paper_trial"
STRATEGY_STATE_TABLES = (*STRATEGY_VERSION_PROVENANCE_TABLES, TRIAL_TABLE)


def _config(connection: sa.Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _migrate(engine: Engine, target: str, *, down: bool) -> None:
    engine.dispose()
    with engine.connect() as connection:
        (command.downgrade if down else command.upgrade)(_config(connection), target)
        connection.commit()


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _provenance_schema(engine: Engine) -> dict[str, tuple[str, ...]]:
    """Every schema object a strategy provenance guarantee depends on."""

    tables = STRATEGY_STATE_TABLES
    with engine.connect() as connection:
        columns = connection.execute(
            sa.text(
                "SELECT table_name, column_name, data_type, is_nullable, "
                "coalesce(column_default, '') FROM information_schema.columns "
                "WHERE table_name = ANY(:t) ORDER BY table_name, ordinal_position"
            ),
            {"t": list(tables)},
        ).all()
        constraints = connection.execute(
            sa.text(
                "SELECT c.conrelid::regclass::text, c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c WHERE c.conrelid::regclass::text = ANY(:t) "
                "ORDER BY 1, 2"
            ),
            {"t": list(tables)},
        ).all()
        indexes = connection.execute(
            sa.text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE tablename = ANY(:t) ORDER BY 1, 2"
            ),
            {"t": list(tables)},
        ).all()
        triggers = connection.execute(
            sa.text(
                "SELECT t.tgrelid::regclass::text, t.tgname, pg_get_triggerdef(t.oid), "
                "p.prosrc FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                "WHERE NOT t.tgisinternal AND t.tgrelid::regclass::text = ANY(:t) "
                "ORDER BY 1, 2"
            ),
            {"t": list(tables)},
        ).all()
    return {
        "columns": tuple(repr(tuple(row)) for row in columns),
        "constraints": tuple(repr(tuple(row)) for row in constraints),
        "indexes": tuple(repr(tuple(row)) for row in indexes),
        "triggers": tuple(repr(tuple(row)) for row in triggers),
    }


def _strategy_state(engine: Engine) -> dict[str, tuple[str, ...]]:
    with engine.connect() as connection:
        return {
            table: tuple(
                str(row[0])
                for row in connection.execute(
                    sa.text(f"SELECT canonical_hash FROM {table} ORDER BY canonical_hash")  # nosec B608
                )
            )
            for table in STRATEGY_STATE_TABLES
        }


def _seed_bound_and_legacy_trials(engine: Engine) -> None:
    provenance, _recorded, promoted, admission = _chain(engine)
    RecordProvenancedTrialOpened(provenance).execute(
        trial=_trial(promoted), transition_id=admission.transition.transition_id
    )
    # A distinct strategy, so the legacy trial has its own trial identity.
    legacy_definition = canonical_strategy_benchmarks(created_at=NOW)[1]
    legacy = _trial(legacy_definition, legacy_adopted=True)
    RecordStrategyPaperTrialOpened(PostgresStrategyPaperTrialRepository(engine)).execute(
        record=legacy
    )


def test_migration_chain_is_linear_with_one_head() -> None:
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    # Strategy PAPER Trial Outcome Assessment v1 stacks 0117; Evidence Gap
    # Recovery v1 stacks 0118 on top of it.
    assert script.get_heads() == [R0118]
    assert REQUIRED_SCHEMA_REVISION == R0118
    assert script.get_revision(R0117).down_revision == R0116
    assert script.get_revision(R0118).down_revision == R0117
    assert script.get_revision(R0116).down_revision == R0115
    assert script.get_revision(R0115).down_revision == R0114


def test_combined_code_is_compatible_only_at_head(postgres_engine: Engine) -> None:
    _migrate(postgres_engine, R0114, down=True)
    for revision in (R0114, R0115, R0116):
        if revision != R0114:
            _migrate(postgres_engine, revision, down=False)
        assert _revision(postgres_engine) == revision
        with pytest.raises(SchemaCompatibilityError) as refused:
            require_compatible_schema(postgres_engine, worker_name="integration-proof")
        assert (
            refused.value.report.status
            is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
        )
    _migrate(postgres_engine, R0117, down=False)
    with pytest.raises(SchemaCompatibilityError) as behind:
        require_compatible_schema(postgres_engine, worker_name="integration-proof")
    assert (
        behind.value.report.status is SchemaCompatibilityStatus.SCHEMA_REVISION_BEHIND_APPLICATION
    )
    _migrate(postgres_engine, R0118, down=False)
    report = require_compatible_schema(postgres_engine, worker_name="integration-proof")
    assert report.status is SchemaCompatibilityStatus.COMPATIBLE
    assert report.observed_revision == R0118


def test_0116_leaves_every_provenance_object_and_row_unchanged(postgres_engine: Engine) -> None:
    _seed_bound_and_legacy_trials(postgres_engine)
    schema_at_head = _provenance_schema(postgres_engine)
    # Strategy PAPER Trial Outcome Assessment v1 (0117) sits above 0116 and
    # adds FK-target UNIQUE constraints to two provenance tables, so this
    # proof about 0116 is anchored at 0116 itself.
    _migrate(postgres_engine, R0116, down=True)
    schema_at_0116 = _provenance_schema(postgres_engine)
    state_at_0116 = _strategy_state(postgres_engine)
    assert all(state_at_0116[table] for table in STRATEGY_STATE_TABLES)

    _migrate(postgres_engine, R0115, down=True)
    assert _revision(postgres_engine) == R0115
    assert _provenance_schema(postgres_engine) == schema_at_0116
    assert _strategy_state(postgres_engine) == state_at_0116

    _migrate(postgres_engine, R0116, down=False)
    assert _provenance_schema(postgres_engine) == schema_at_0116
    assert _strategy_state(postgres_engine) == state_at_0116

    # 0117 changes no provenance row and adds exactly its two FK-target
    # UNIQUE constraints (plus their backing indexes) to provenance tables.
    _migrate(postgres_engine, R0117, down=False)
    assert _strategy_state(postgres_engine) == state_at_0116
    assert _provenance_schema(postgres_engine) == schema_at_head
    added = {
        key: tuple(item for item in schema_at_head[key] if item not in schema_at_0116[key])
        for key in schema_at_head
    }
    assert all(not item for key, item in added.items() if key not in {"constraints", "indexes"})
    for key in ("constraints", "indexes"):
        assert len(added[key]) == 2
        assert all(
            "uq_strategy_paper_trial_provenance_outcome_target" in item
            or "uq_strategy_version_transition_outcome_target" in item
            for item in added[key]
        )
    assert not any(
        item not in schema_at_head[key] for key in schema_at_0116 for item in schema_at_0116[key]
    )


def test_0116_guard_fires_first_and_leaves_provenance_intact(postgres_engine: Engine) -> None:
    _seed_bound_and_legacy_trials(postgres_engine)
    _seed_short_observation(postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="stack")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    state = _strategy_state(postgres_engine)
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="cannot downgrade 0116"):
            command.downgrade(_config(connection), R0114)
        connection.rollback()
    assert _revision(postgres_engine) == R0116
    assert _strategy_state(postgres_engine) == state


def test_downgrade_with_only_provenance_retained_rests_at_0115(postgres_engine: Engine) -> None:
    _seed_bound_and_legacy_trials(postgres_engine)
    state = _strategy_state(postgres_engine)
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="cannot downgrade 0115"):
            command.downgrade(_config(connection), R0114)
        connection.rollback()
    # Each migration step commits independently: 0116's empty-evidence
    # downgrade completes, then 0115's provenance guard refuses.
    assert _revision(postgres_engine) == R0115
    assert _strategy_state(postgres_engine) == state
    _migrate(postgres_engine, R0116, down=False)
    assert _strategy_state(postgres_engine) == state


def test_securities_v2_assessment_never_touches_strategy_state(postgres_engine: Engine) -> None:
    _seed_bound_and_legacy_trials(postgres_engine)
    _seed_short_observation(postgres_engine, terminal_captured_at=MONDAY_LATE, set_id="isolated")
    before = _strategy_state(postgres_engine)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=MONDAY_LATE, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    with postgres_engine.connect() as connection:
        v2_rows = connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM followup_evidence_quality_assessment "
                "WHERE assessment_policy_version = :v"
            ),
            {"v": FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2},
        )
    assert v2_rows and v2_rows > 0
    assert _strategy_state(postgres_engine) == before
