"""External Strategy Evaluation v1.

Adds ``external_strategy_definition`` and ``external_strategy_evaluation``:
the durable, replay-safe evidence substrate for accepting a clearly
specified external/manual strategy definition, reproducing it
deterministically against retained Apex data, and retaining exactly what
was evaluated. Closes the gap ``domain.strategy_definition.
StrategyDefinition``/``domain.strategy_evaluation.StrategyEvaluationResult``
have carried since Apex Strategy-Source Contract & Genuine PAPER Activation
v1 introduced them: both types were, and remain, entirely ephemeral --
recomputed fresh every recurring-cycle tick from a fixed, hardcoded
four-item catalog (``domain.strategy_benchmark.canonical_strategy_
benchmarks``) and never persisted anywhere (confirmed by bounded
archaeology: zero references to either type anywhere under
``src/apex/adapters`` before this migration). This milestone's own
external-strategy registration/evaluation path needs durable, reproducible
history that survives a process restart, so this is the smallest new
persistence this milestone requires -- see ``domain.external_strategy`` and
``application.external_strategy_research`` for the full rationale; neither
existing table nor existing repository is touched or reused for a different
purpose.

``external_strategy_definition`` is one immutable row per genuinely
distinct, versioned ``StrategyDefinition`` -- ``(strategy_id, version)`` is
the natural composite primary key (mirrors ``StrategyDefinition`` itself:
``strategy_id`` is stable across ``next_version``/``promote_to_paper_
eligible`` re-versioning, so a strategy's full lineage is every row sharing
one ``strategy_id``). ``canonical_hash`` mirrors ``StrategyDefinition.
canonical_hash`` exactly (a content hash over source_kind/venue/instruments/
rule/parameters/evidence_requirements/provenance/lifecycle_state -- NOT
over ``created_at``, which is why that column is not part of any unique
constraint here) and is what a repository's idempotent-insert-or-conflict
check compares against a re-registration at the identical
``(strategy_id, version)`` -- the same "select existing hash, compare,
insert-or-conflict" idiom every recent Apex evidence table already uses
(``kalshi_resolution_fact``, ``kalshi_baseline_quote``, ...). The full
``StrategyDefinition.canonical_payload`` (already a canonicalizable, JSON-
safe dict -- source_kind/display_name/venue/instrument_ids/horizon_kind/
rule_kind/rule_parameters/evidence_requirements/provenance/lifecycle_state)
is retained verbatim as canonical JSON text, matching the established
``payload`` idiom (``kalshi_baseline_quote``, ``counterfactual_decision_
classification``) rather than flattening every nested field into its own
column.

``external_strategy_evaluation`` is one immutable row per genuinely
distinct ``domain.external_strategy.ExternalStrategyEvaluationRecord`` --
exactly which strategy definition/version was evaluated, against exactly
which retained evidence window (``capture_id`` + the effective ``as_of``
cutoff actually enforced), under exactly which named/versioned evaluation
policy, producing exactly which candidate result and canonical-benchmark
comparisons (Part 5, Reproducibility; Part 9, Benchmark Discipline). A
composite foreign key to ``external_strategy_definition(strategy_id,
version)`` ties every evaluation to the exact immutable definition version
it was computed against -- a definition row can never be deleted out from
under a retained evaluation that references it (ordinary ``ON DELETE`` rules
apply, though in practice neither table's rows are ever deleted at all: both
carry the same row-level ``BEFORE UPDATE OR DELETE`` immutability trigger
every recent Apex evidence table carries). ``qualification`` restates
``domain.external_strategy.ExternalStrategyQualification`` at the database
level -- never a promotion by itself; see that module's own docstring and
``application.external_strategy_research.PromoteExternalStrategy`` for the
one, separate, explicit action that can actually move a definition to
``PAPER_ELIGIBLE``, which persists through the FIRST table (a new,
``version + 1`` row), never this one.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application``. It creates both tables only; there is
no data to backfill (a wholly new, forward-only, prospective-only evidence
stream this milestone's CLI populates going forward).

Revision ID: 0109_external_strategy_research
Revises: 0108_kalshi_baseline_v2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0109_external_strategy_research"
down_revision: str | None = "0108_kalshi_baseline_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXTERNAL_STRATEGY_RESEARCH_TABLES = ("external_strategy_definition", "external_strategy_evaluation")

_SOURCE_KINDS = ("INTERNAL", "EXTERNAL")
_VENUES = ("CRYPTO", "KALSHI", "SECURITIES")
_RULE_KINDS = (
    "BUY_AND_HOLD",
    "MOVING_AVERAGE_TREND",
    "TIME_SERIES_MOMENTUM",
    "MEAN_REVERSION",
    "INTERNAL_METHOD_REFERENCE",
)
_LIFECYCLE_STATES = ("RESEARCH_ONLY", "PAPER_ELIGIBLE", "ACTIVE", "RETIRED")
_QUALIFICATIONS = (
    "RESEARCH_ONLY_INSUFFICIENT_SAMPLE",
    "RESEARCH_ONLY_NONPOSITIVE_NET_RETURN",
    "WOULD_QUALIFY_FOR_PAPER_ELIGIBLE",
)


def upgrade() -> None:
    op.create_table(
        "external_strategy_definition",
        sa.Column("strategy_id", sa.String(300), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("parent_strategy_id", sa.String(300), nullable=True),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("rule_kind", sa.String(40), nullable=False),
        sa.Column("lifecycle_state", sa.String(20), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "source_kind IN (" + ",".join(f"'{value}'" for value in _SOURCE_KINDS) + ")",
            name="ck_external_strategy_definition_source_kind",
        ),
        sa.CheckConstraint(
            "venue IN (" + ",".join(f"'{value}'" for value in _VENUES) + ")",
            name="ck_external_strategy_definition_venue",
        ),
        sa.CheckConstraint(
            "rule_kind IN (" + ",".join(f"'{value}'" for value in _RULE_KINDS) + ")",
            name="ck_external_strategy_definition_rule_kind",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN (" + ",".join(f"'{value}'" for value in _LIFECYCLE_STATES) + ")",
            name="ck_external_strategy_definition_lifecycle_state",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_external_strategy_definition_hash"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_external_strategy_definition_authority"),
        sa.CheckConstraint("version >= 1", name="ck_external_strategy_definition_version"),
    )
    op.create_index(
        "ix_external_strategy_definition_created_at", "external_strategy_definition", ["created_at"]
    )
    op.create_index(
        "ix_external_strategy_definition_lifecycle_state",
        "external_strategy_definition",
        ["lifecycle_state"],
    )

    op.create_table(
        "external_strategy_evaluation",
        sa.Column("evaluation_id", sa.String(300), primary_key=True),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_id", sa.String(300), nullable=False),
        sa.Column("evaluation_policy_version", sa.String(60), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qualification", sa.String(50), nullable=False),
        sa.Column("sample_size", sa.Integer, nullable=False),
        sa.Column("net_return", sa.Numeric, nullable=True),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["external_strategy_definition.strategy_id", "external_strategy_definition.version"],
            name="fk_external_strategy_evaluation_definition",
        ),
        sa.CheckConstraint(
            "qualification IN (" + ",".join(f"'{value}'" for value in _QUALIFICATIONS) + ")",
            name="ck_external_strategy_evaluation_qualification",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_external_strategy_evaluation_hash"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_external_strategy_evaluation_authority"),
        sa.CheckConstraint("sample_size >= 0", name="ck_external_strategy_evaluation_sample_size"),
    )
    op.create_index(
        "ix_external_strategy_evaluation_strategy",
        "external_strategy_evaluation",
        ["strategy_id", "strategy_version"],
    )
    op.create_index(
        "ix_external_strategy_evaluation_evaluated_at",
        "external_strategy_evaluation",
        ["evaluated_at"],
    )

    for table in EXTERNAL_STRATEGY_RESEARCH_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(EXTERNAL_STRATEGY_RESEARCH_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0109 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(EXTERNAL_STRATEGY_RESEARCH_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    op.drop_table("external_strategy_evaluation")
    op.drop_table("external_strategy_definition")
