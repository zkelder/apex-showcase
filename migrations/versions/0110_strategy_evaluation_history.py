"""Strategy Evaluation History & Promotion Lineage v1.

Adds ``internal_strategy_evaluation`` and ``internal_strategy_promotion``:
the durable, replay-safe evidence substrate for Apex's OWN recurring
operating-cycle strategy catalog (``domain.strategy_benchmark.canonical_
strategy_benchmarks``, the fixed four-candidate set the live
``STRATEGY_PAPER_TRIAL_EXECUTION`` task evaluates every cycle). Closes the
gap External Strategy Evaluation v1 (migration ``0109``) intentionally left
open: that milestone gave durable persistence ONLY to operator-registered
external strategies (``external_strategy_definition``/``external_strategy_
evaluation``) -- the live recurring catalog's own ``StrategyDefinition``/
``StrategyEvaluationResult`` remained, and without this migration would
still remain, entirely ephemeral: recomputed fresh every dispatch and never
retained. See ``domain.strategy_evaluation_history`` for the full
rationale, including why this stays a physically separate table pair from
``external_strategy_*`` rather than reusing it (two genuinely different
governance/actor semantics an operator relies on staying distinct).

``internal_strategy_evaluation`` is one immutable row per genuinely
distinct ``domain.strategy_evaluation_history.InternalStrategyEvaluationRecord``
-- exactly which recurring-catalog strategy/version was evaluated, against
exactly which retained evidence window (``capture_id`` + the day-scoped
``as_of`` the recurring cycle itself holds constant all day), under exactly
which named/versioned evaluation policy, producing exactly which candidate
result and lifecycle-qualification outcome. Unlike ``external_strategy_
evaluation``, there is no FK to a sibling "definition" table: the recurring
catalog's definitions are never persisted anywhere (they are reconstructed
byte-identically from reviewed source code on every dispatch -- see
``domain.strategy_evaluation_history``'s own module docstring), so this
table carries its own ``source_kind``/``venue``/``rule_kind``/
``instrument_ids`` columns directly rather than joining through one.
``canonical_hash`` mirrors the deterministic-identity discipline every
recent Apex evidence table already uses (``kalshi_resolution_fact``,
``external_strategy_evaluation``, ...) -- what a repository's idempotent-
insert-or-conflict check compares against a replay at the identical
``evaluation_id``.

``internal_strategy_promotion`` is one immutable row per genuinely distinct
``domain.strategy_evaluation_history.InternalStrategyPromotionRecord`` --
an EXPLICIT, caller-invoked promotion-lineage event (never written by the
recurring cycle itself; see ``application.strategy_evaluation_history.
PromoteInternalStrategy``'s own module docstring). A foreign key to
``internal_strategy_evaluation(evaluation_id)`` ties every promotion to the
exact immutable evaluation that justified it -- an evaluation row can
never be deleted out from under a retained promotion that references it
(in practice neither table's rows are ever deleted at all: both carry the
same row-level ``BEFORE UPDATE OR DELETE`` immutability trigger every
recent Apex evidence table carries).

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application``. It creates both tables only; there is
no data to backfill (a wholly new, forward-only, prospective-only evidence
stream the recurring cycle populates going forward, plus whatever an
operator explicitly promotes via the new CLI action).

Revision ID: 0110_strategy_evaluation_history
Revises: 0109_external_strategy_research
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0110_strategy_evaluation_history"
down_revision: str | None = "0109_external_strategy_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRATEGY_EVALUATION_HISTORY_TABLES = ("internal_strategy_evaluation", "internal_strategy_promotion")

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
        "internal_strategy_evaluation",
        sa.Column("evaluation_id", sa.String(300), primary_key=True),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("rule_kind", sa.String(40), nullable=False),
        sa.Column("instrument_ids", sa.Text(), nullable=False),
        sa.Column("cycle_id", sa.String(300), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capture_id", sa.String(300), nullable=False),
        sa.Column("evaluation_policy_version", sa.String(60), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qualification", sa.String(50), nullable=False),
        sa.Column("lifecycle_state_before", sa.String(20), nullable=False),
        sa.Column("lifecycle_state_recommended_after", sa.String(20), nullable=False),
        sa.Column("sample_size", sa.Integer, nullable=False),
        sa.Column("net_return", sa.Numeric, nullable=True),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "source_kind IN (" + ",".join(f"'{value}'" for value in _SOURCE_KINDS) + ")",
            name="ck_internal_strategy_evaluation_source_kind",
        ),
        sa.CheckConstraint(
            "venue IN (" + ",".join(f"'{value}'" for value in _VENUES) + ")",
            name="ck_internal_strategy_evaluation_venue",
        ),
        sa.CheckConstraint(
            "rule_kind IN (" + ",".join(f"'{value}'" for value in _RULE_KINDS) + ")",
            name="ck_internal_strategy_evaluation_rule_kind",
        ),
        sa.CheckConstraint(
            "qualification IN (" + ",".join(f"'{value}'" for value in _QUALIFICATIONS) + ")",
            name="ck_internal_strategy_evaluation_qualification",
        ),
        sa.CheckConstraint(
            "lifecycle_state_before IN ("
            + ",".join(f"'{value}'" for value in _LIFECYCLE_STATES)
            + ")",
            name="ck_internal_strategy_evaluation_lifecycle_before",
        ),
        sa.CheckConstraint(
            "lifecycle_state_recommended_after IN ("
            + ",".join(f"'{value}'" for value in _LIFECYCLE_STATES)
            + ")",
            name="ck_internal_strategy_evaluation_lifecycle_after",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_internal_strategy_evaluation_hash"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_internal_strategy_evaluation_authority"),
        sa.CheckConstraint("strategy_version >= 1", name="ck_internal_strategy_evaluation_version"),
        sa.CheckConstraint("sample_size >= 0", name="ck_internal_strategy_evaluation_sample_size"),
    )
    op.create_index(
        "ix_internal_strategy_evaluation_strategy",
        "internal_strategy_evaluation",
        ["strategy_id", "strategy_version"],
    )
    op.create_index(
        "ix_internal_strategy_evaluation_evaluated_at",
        "internal_strategy_evaluation",
        ["evaluated_at"],
    )
    op.create_index(
        "ix_internal_strategy_evaluation_cycle", "internal_strategy_evaluation", ["cycle_id"]
    )

    op.create_table(
        "internal_strategy_promotion",
        sa.Column("promotion_id", sa.String(300), primary_key=True),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column("evaluation_id", sa.String(300), nullable=False),
        sa.Column("lifecycle_state_before", sa.String(20), nullable=False),
        sa.Column("lifecycle_state_after", sa.String(20), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promotion_policy_version", sa.String(60), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["internal_strategy_evaluation.evaluation_id"],
            name="fk_internal_strategy_promotion_evaluation",
        ),
        sa.CheckConstraint(
            "lifecycle_state_before IN ("
            + ",".join(f"'{value}'" for value in _LIFECYCLE_STATES)
            + ")",
            name="ck_internal_strategy_promotion_lifecycle_before",
        ),
        sa.CheckConstraint(
            "lifecycle_state_after IN ("
            + ",".join(f"'{value}'" for value in _LIFECYCLE_STATES)
            + ")",
            name="ck_internal_strategy_promotion_lifecycle_after",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_internal_strategy_promotion_hash"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_internal_strategy_promotion_authority"),
        sa.CheckConstraint("strategy_version >= 1", name="ck_internal_strategy_promotion_version"),
    )
    op.create_index(
        "ix_internal_strategy_promotion_strategy", "internal_strategy_promotion", ["strategy_id"]
    )
    op.create_index(
        "ix_internal_strategy_promotion_promoted_at",
        "internal_strategy_promotion",
        ["promoted_at"],
    )

    for table in STRATEGY_EVALUATION_HISTORY_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(STRATEGY_EVALUATION_HISTORY_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0110 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(STRATEGY_EVALUATION_HISTORY_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    op.drop_table("internal_strategy_promotion")
    op.drop_table("internal_strategy_evaluation")
