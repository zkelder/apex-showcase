"""Strategy Version Provenance v1 (Challenger Provenance v1).

Adds four immutable, append-only tables that bind the rule-strategy
lifecycle to exact strategy artifacts (see ``domain.
strategy_version_provenance`` and ``docs/STRATEGY_VERSION_PROVENANCE_V1.md``):

* ``strategy_version``: one row per exact artifact, keyed by ``(registry,
  strategy_id, version, definition_hash)``, with its exact parent artifact.
  ``artifact`` holds the canonical JSON bytes. A CHECK proves that their
  SHA-256 *is* ``definition_hash`` and that the identity columns agree with
  the artifact, so the database itself guarantees the stored artifact is
  the hashed artifact.
* ``strategy_evaluation_subject``: evaluation (recommendation) -> the exact
  artifact evaluated. Composite foreign keys onto the retained evaluation
  row pin the evaluation's own strategy id, version, and record hash.
* ``strategy_version_transition``: evaluated artifact -> promoted or
  admitted artifact, justified by that exact evaluation. A composite foreign
  key onto ``strategy_evaluation_subject`` makes it impossible to bind a
  transition to an evaluation of any other artifact, and another onto
  ``strategy_version``'s own parent columns makes the promoted artifact's
  recorded parent equal the evaluated artifact.
* ``strategy_paper_trial_provenance``: a non-legacy PAPER trial -> the exact
  transition that admitted the artifact it trades.

Additive only. No existing row or column is changed. Three UNIQUE constraints
are added to existing tables, each a superset of an existing primary key, so
they hold trivially for every retained row. They exist only as composite
foreign-key targets. One DEFERRABLE constraint trigger rejects, at commit, a
*non-legacy* ``internal_strategy_paper_trial`` insert that has no provenance
row in the same transaction. Legacy-adopted trials, including the one
production trial, are exempt: they predate provenance, and nothing is
backfilled or fabricated for them.

Self-contained per the migration-boundary rule: imports only ``alembic``,
``sqlalchemy``, and ``apex.adapters.postgres.immutable_trigger_ddl``.

Revision ID: 0115_strategy_provenance
Revises: 0114_outcome_specificity_v3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0115_strategy_provenance"
down_revision: str | None = "0114_outcome_specificity_v3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRATEGY_VERSION_PROVENANCE_TABLES = (
    "strategy_version",
    "strategy_evaluation_subject",
    "strategy_version_transition",
    "strategy_paper_trial_provenance",
)

_REGISTRIES = ("CATALOG_CODE", "EXTERNAL_OPERATOR")
_LIFECYCLE_STATES = ("RESEARCH_ONLY", "PAPER_ELIGIBLE", "ACTIVE", "RETIRED")
_TRANSITION_KINDS = (
    "CYCLE_GATE_ADMISSION",
    "INTERNAL_OPERATOR_PROMOTION",
    "EXTERNAL_OPERATOR_PROMOTION",
)

# (existing table, constraint name, columns) -- composite-FK targets only.
_EXISTING_TABLE_UNIQUES = (
    (
        "internal_strategy_evaluation",
        "uq_internal_strategy_evaluation_provenance_target",
        ("evaluation_id", "strategy_id", "strategy_version", "canonical_hash"),
    ),
    (
        "external_strategy_evaluation",
        "uq_external_strategy_evaluation_provenance_target",
        ("evaluation_id", "strategy_id", "strategy_version", "canonical_hash"),
    ),
    (
        "internal_strategy_paper_trial",
        "uq_internal_strategy_paper_trial_provenance_target",
        ("trial_id", "strategy_id", "strategy_version", "legacy_adopted", "canonical_hash"),
    ),
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ",".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    for table, name, columns in _EXISTING_TABLE_UNIQUES:
        op.create_unique_constraint(name, table, list(columns))

    op.create_table(
        "strategy_version",
        sa.Column("registry", sa.String(40), primary_key=True),
        sa.Column("strategy_id", sa.String(300), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("definition_hash", sa.String(64), primary_key=True),
        sa.Column("lifecycle_state", sa.String(20), nullable=False),
        sa.Column("parent_version", sa.Integer, nullable=True),
        sa.Column("parent_definition_hash", sa.String(64), nullable=True),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(_in("registry", _REGISTRIES), name="ck_strategy_version_registry"),
        sa.CheckConstraint(
            _in("lifecycle_state", _LIFECYCLE_STATES), name="ck_strategy_version_lifecycle"
        ),
        sa.CheckConstraint(
            "(version = 1 AND lifecycle_state = 'RESEARCH_ONLY' AND parent_version IS NULL "
            "AND parent_definition_hash IS NULL) OR (version > 1 "
            "AND lifecycle_state <> 'RESEARCH_ONLY' AND parent_version = version - 1 "
            "AND parent_definition_hash IS NOT NULL)",
            name="ck_strategy_version_lineage",
        ),
        sa.CheckConstraint(
            "encode(sha256(convert_to(artifact, 'UTF8')), 'hex') = definition_hash",
            name="ck_strategy_version_artifact_hash",
        ),
        sa.CheckConstraint(
            "(artifact::jsonb ->> 'strategy_id') = strategy_id "
            "AND (artifact::jsonb ->> 'version')::integer = version "
            "AND (artifact::jsonb ->> 'lifecycle_state') = lifecycle_state",
            name="ck_strategy_version_artifact_identity",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_strategy_version_authority"),
        sa.CheckConstraint(
            "length(definition_hash) = 64 AND length(canonical_hash) = 64",
            name="ck_strategy_version_hash",
        ),
        sa.ForeignKeyConstraint(
            ["registry", "strategy_id", "parent_version", "parent_definition_hash"],
            [
                "strategy_version.registry",
                "strategy_version.strategy_id",
                "strategy_version.version",
                "strategy_version.definition_hash",
            ],
            name="fk_strategy_version_parent",
        ),
        sa.UniqueConstraint(
            "registry",
            "strategy_id",
            "version",
            "definition_hash",
            "parent_version",
            "parent_definition_hash",
            name="uq_strategy_version_with_parent",
        ),
    )
    op.create_index("ix_strategy_version_strategy", "strategy_version", ["strategy_id"])

    op.create_table(
        "strategy_evaluation_subject",
        sa.Column("evaluation_source", sa.String(40), primary_key=True),
        sa.Column("evaluation_id", sa.String(300), primary_key=True),
        sa.Column("internal_evaluation_id", sa.String(300), nullable=True),
        sa.Column("external_evaluation_id", sa.String(300), nullable=True),
        sa.Column("evaluation_hash", sa.String(64), nullable=False),
        sa.Column("registry", sa.String(40), nullable=False),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "(evaluation_source = 'INTERNAL_STRATEGY_EVALUATION' "
            "AND registry = 'CATALOG_CODE' AND internal_evaluation_id = evaluation_id "
            "AND external_evaluation_id IS NULL) OR "
            "(evaluation_source = 'EXTERNAL_STRATEGY_EVALUATION' "
            "AND registry = 'EXTERNAL_OPERATOR' AND external_evaluation_id = evaluation_id "
            "AND internal_evaluation_id IS NULL)",
            name="ck_strategy_evaluation_subject_source",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_strategy_evaluation_subject_authority"),
        sa.ForeignKeyConstraint(
            ["internal_evaluation_id", "strategy_id", "version", "evaluation_hash"],
            [
                "internal_strategy_evaluation.evaluation_id",
                "internal_strategy_evaluation.strategy_id",
                "internal_strategy_evaluation.strategy_version",
                "internal_strategy_evaluation.canonical_hash",
            ],
            name="fk_strategy_evaluation_subject_internal",
        ),
        sa.ForeignKeyConstraint(
            ["external_evaluation_id", "strategy_id", "version", "evaluation_hash"],
            [
                "external_strategy_evaluation.evaluation_id",
                "external_strategy_evaluation.strategy_id",
                "external_strategy_evaluation.strategy_version",
                "external_strategy_evaluation.canonical_hash",
            ],
            name="fk_strategy_evaluation_subject_external",
        ),
        sa.ForeignKeyConstraint(
            ["registry", "strategy_id", "version", "definition_hash"],
            [
                "strategy_version.registry",
                "strategy_version.strategy_id",
                "strategy_version.version",
                "strategy_version.definition_hash",
            ],
            name="fk_strategy_evaluation_subject_version",
        ),
        sa.UniqueConstraint(
            "evaluation_source",
            "evaluation_id",
            "evaluation_hash",
            "registry",
            "strategy_id",
            "version",
            "definition_hash",
            name="uq_strategy_evaluation_subject_artifact",
        ),
    )
    op.create_index(
        "ix_strategy_evaluation_subject_strategy", "strategy_evaluation_subject", ["strategy_id"]
    )

    op.create_table(
        "strategy_version_transition",
        sa.Column("transition_id", sa.String(300), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("registry", sa.String(40), nullable=False),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("from_version", sa.Integer, nullable=False),
        sa.Column("from_definition_hash", sa.String(64), nullable=False),
        sa.Column("to_version", sa.Integer, nullable=False),
        sa.Column("to_definition_hash", sa.String(64), nullable=False),
        sa.Column("evaluation_source", sa.String(40), nullable=False),
        sa.Column("evaluation_id", sa.String(300), nullable=False),
        sa.Column("evaluation_hash", sa.String(64), nullable=False),
        sa.Column("gate_policy_version", sa.String(60), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            _in("kind", _TRANSITION_KINDS), name="ck_strategy_version_transition_kind"
        ),
        sa.CheckConstraint(
            "(kind IN ('CYCLE_GATE_ADMISSION', 'INTERNAL_OPERATOR_PROMOTION') "
            "AND registry = 'CATALOG_CODE') OR "
            "(kind = 'EXTERNAL_OPERATOR_PROMOTION' AND registry = 'EXTERNAL_OPERATOR')",
            name="ck_strategy_version_transition_registry",
        ),
        sa.CheckConstraint(
            "to_version = from_version + 1", name="ck_strategy_version_transition_step"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_strategy_version_transition_authority"),
        sa.ForeignKeyConstraint(
            ["registry", "strategy_id", "from_version", "from_definition_hash"],
            [
                "strategy_version.registry",
                "strategy_version.strategy_id",
                "strategy_version.version",
                "strategy_version.definition_hash",
            ],
            name="fk_strategy_version_transition_from",
        ),
        sa.ForeignKeyConstraint(
            [
                "registry",
                "strategy_id",
                "to_version",
                "to_definition_hash",
                "from_version",
                "from_definition_hash",
            ],
            [
                "strategy_version.registry",
                "strategy_version.strategy_id",
                "strategy_version.version",
                "strategy_version.definition_hash",
                "strategy_version.parent_version",
                "strategy_version.parent_definition_hash",
            ],
            name="fk_strategy_version_transition_to_with_parent",
        ),
        sa.ForeignKeyConstraint(
            [
                "evaluation_source",
                "evaluation_id",
                "evaluation_hash",
                "registry",
                "strategy_id",
                "from_version",
                "from_definition_hash",
            ],
            [
                "strategy_evaluation_subject.evaluation_source",
                "strategy_evaluation_subject.evaluation_id",
                "strategy_evaluation_subject.evaluation_hash",
                "strategy_evaluation_subject.registry",
                "strategy_evaluation_subject.strategy_id",
                "strategy_evaluation_subject.version",
                "strategy_evaluation_subject.definition_hash",
            ],
            name="fk_strategy_version_transition_evaluated_artifact",
        ),
        sa.UniqueConstraint(
            "transition_id",
            "registry",
            "strategy_id",
            "to_version",
            "to_definition_hash",
            name="uq_strategy_version_transition_target",
        ),
    )
    op.create_index(
        "ix_strategy_version_transition_strategy", "strategy_version_transition", ["strategy_id"]
    )

    op.create_table(
        "strategy_paper_trial_provenance",
        sa.Column("trial_id", sa.String(300), primary_key=True),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column("trial_legacy_adopted", sa.Boolean, nullable=False),
        sa.Column("trial_canonical_hash", sa.String(64), nullable=False),
        sa.Column("transition_id", sa.String(300), nullable=False),
        sa.Column("registry", sa.String(40), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "trial_legacy_adopted = false", name="ck_strategy_paper_trial_provenance_not_legacy"
        ),
        sa.CheckConstraint(
            "registry = 'CATALOG_CODE'", name="ck_strategy_paper_trial_provenance_registry"
        ),
        sa.CheckConstraint(
            "authority = 'NONE'", name="ck_strategy_paper_trial_provenance_authority"
        ),
        sa.ForeignKeyConstraint(
            [
                "trial_id",
                "strategy_id",
                "strategy_version",
                "trial_legacy_adopted",
                "trial_canonical_hash",
            ],
            [
                "internal_strategy_paper_trial.trial_id",
                "internal_strategy_paper_trial.strategy_id",
                "internal_strategy_paper_trial.strategy_version",
                "internal_strategy_paper_trial.legacy_adopted",
                "internal_strategy_paper_trial.canonical_hash",
            ],
            name="fk_strategy_paper_trial_provenance_trial",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id", "registry", "strategy_id", "strategy_version", "definition_hash"],
            [
                "strategy_version_transition.transition_id",
                "strategy_version_transition.registry",
                "strategy_version_transition.strategy_id",
                "strategy_version_transition.to_version",
                "strategy_version_transition.to_definition_hash",
            ],
            name="fk_strategy_paper_trial_provenance_transition",
        ),
    )

    for table in STRATEGY_VERSION_PROVENANCE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))

    op.execute(
        sa.text(
            """
CREATE FUNCTION apex_require_strategy_paper_trial_provenance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM strategy_paper_trial_provenance WHERE trial_id = NEW.trial_id
    ) THEN
        RAISE EXCEPTION 'STRATEGY_PAPER_TRIAL_PROVENANCE_REQUIRED: %', NEW.trial_id
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
"""
        )
    )
    op.execute(
        sa.text(
            """
CREATE CONSTRAINT TRIGGER apex_strategy_paper_trial_provenance_required
AFTER INSERT ON internal_strategy_paper_trial
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (NEW.legacy_adopted = false)
EXECUTE FUNCTION apex_require_strategy_paper_trial_provenance();
"""
        )
    )


def downgrade() -> None:
    for table in reversed(STRATEGY_VERSION_PROVENANCE_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0115 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_strategy_paper_trial_provenance_required "
            "ON internal_strategy_paper_trial"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_require_strategy_paper_trial_provenance()"))
    for table in reversed(STRATEGY_VERSION_PROVENANCE_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    for table in reversed(STRATEGY_VERSION_PROVENANCE_TABLES):
        op.drop_table(table)
    for table, name, _columns in reversed(_EXISTING_TABLE_UNIQUES):
        op.drop_constraint(name, table, type_="unique")
