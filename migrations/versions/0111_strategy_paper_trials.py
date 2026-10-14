"""Repeatable Strategy PAPER Evidence v1.

Adds ``internal_strategy_paper_trial`` and
``internal_strategy_paper_trial_closure``: the durable, replay-safe
evidence substrate for BOUNDED, REPEATABLE strategy-driven PAPER trial
generations -- closing the gap left by the pre-existing
``recurring_strategy_paper_trial_seed`` (``domain.strategy_paper_trial``),
which is deliberately scoped to ``(strategy_id, strategy_version,
instrument_id)`` only and therefore permits "at most one lifetime trial"
per slot (see that function's own docstring). See ``domain.strategy_paper_
trial_lifecycle`` for the full rationale and ``docs/REPEATABLE_STRATEGY_
PAPER_EVIDENCE_V1.md`` for the milestone-level design writeup.

``internal_strategy_paper_trial`` is one immutable row per genuinely
distinct trial GENERATION for one ``(strategy_id, strategy_version,
instrument_id)`` slot -- written once, at open time, by ``application.
strategy_paper_trial_lifecycle.RecordStrategyPaperTrialOpened``. Deliberately
physically separate from ``internal_strategy_evaluation``/``internal_
strategy_promotion`` (migration ``0110``): those two tables answer "why is
this strategy in its current lifecycle state" (evaluation/qualification/
promotion lineage); this table pair answers a structurally different
question, "how many bounded PAPER exposure windows has this slot actually
run, and is one open right now" -- genuinely different identity (folds in
``generation``, which neither 0110 table has any concept of) and a
genuinely different write cadence (once per trial open/close, not once per
evaluation tick).

``internal_strategy_paper_trial_closure`` is one immutable row per
genuinely distinct trial CLOSURE event, written once by ``application.
strategy_paper_trial_lifecycle.RecordStrategyPaperTrialClosed`` when a
trial's bounded evidence horizon is reached and its PAPER exposure is
genuinely closed through the existing, unmodified ``domain.crypto_paper``
SELL/reconciliation machinery. The ``UNIQUE`` constraint on ``trial_id``
is the load-bearing DB-level guarantee behind "a terminal trial cannot be
closed twice" (Part 9 of this milestone's required tests) -- a second
closure attempt for the same ``trial_id`` cannot physically be inserted,
independent of any application-layer check.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application``. It creates both tables only; there is
no data to backfill (a wholly new, forward-only, prospective-only evidence
stream -- the pre-existing open BTC-USD PAPER position is ADOPTED as a
generation-1 trial by application-layer code the first time it observes
that position, from retained facts, never by a migration-time data
rewrite; see ``domain.strategy_paper_trial_lifecycle.adopt_legacy_trial``).

Revision ID: 0111_strategy_paper_trials
Revises: 0110_strategy_evaluation_history

Note on the revision id itself: ``alembic_version.version_num`` is
``VARCHAR(32)`` (Alembic's own default, unchanged elsewhere in this repo),
and the fuller ``0111_strategy_paper_trial_lifecycle`` id this migration
was originally drafted under is 35 characters -- 3 over that limit. Shortened
to ``0111_strategy_paper_trials`` (27 characters) rather than widening the
column, matching this repository's existing convention that every revision
id fit the column as-is (verified directly against the real ``apex_test``
database during this milestone's own persistence-test pass -- a real
``StringDataRightTruncation`` error, not a hypothetical one).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0111_strategy_paper_trials"
down_revision: str | None = "0110_strategy_evaluation_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STRATEGY_PAPER_TRIAL_LIFECYCLE_TABLES = (
    "internal_strategy_paper_trial",
    "internal_strategy_paper_trial_closure",
)

_TERMINAL_REASONS = ("TRIAL_HORIZON_EXIT", "OPENING_ORDER_EXPIRED_UNFILLED")


def upgrade() -> None:
    op.create_table(
        "internal_strategy_paper_trial",
        sa.Column("trial_id", sa.String(300), primary_key=True),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column("instrument_id", sa.String(60), nullable=False),
        sa.Column("account_id", sa.String(300), nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opening_authorization_id", sa.String(300), nullable=False),
        sa.Column("opening_order_id", sa.String(300), nullable=False),
        sa.Column("opening_realized_pnl_baseline", sa.Numeric, nullable=False),
        sa.Column("horizon_days", sa.Integer, nullable=False),
        sa.Column("horizon_target_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cooldown_days", sa.Integer, nullable=False),
        sa.Column("policy_version", sa.String(60), nullable=False),
        sa.Column("legacy_adopted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_internal_strategy_paper_trial_authority"),
        sa.CheckConstraint(
            "strategy_version >= 1", name="ck_internal_strategy_paper_trial_version"
        ),
        sa.CheckConstraint("generation >= 1", name="ck_internal_strategy_paper_trial_generation"),
        sa.CheckConstraint("horizon_days >= 1", name="ck_internal_strategy_paper_trial_horizon"),
        sa.CheckConstraint("cooldown_days >= 0", name="ck_internal_strategy_paper_trial_cooldown"),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_internal_strategy_paper_trial_hash"
        ),
        sa.UniqueConstraint(
            "strategy_id",
            "strategy_version",
            "instrument_id",
            "generation",
            name="uq_internal_strategy_paper_trial_slot_generation",
        ),
    )
    op.create_index(
        "ix_internal_strategy_paper_trial_slot",
        "internal_strategy_paper_trial",
        ["strategy_id", "strategy_version", "instrument_id"],
    )
    op.create_index(
        "ix_internal_strategy_paper_trial_opened_at",
        "internal_strategy_paper_trial",
        ["opened_at"],
    )

    op.create_table(
        "internal_strategy_paper_trial_closure",
        sa.Column("closure_id", sa.String(300), primary_key=True),
        sa.Column("trial_id", sa.String(300), nullable=False, unique=True),
        sa.Column("strategy_id", sa.String(300), nullable=False),
        sa.Column("instrument_id", sa.String(60), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_reason", sa.String(40), nullable=False),
        sa.Column("closing_authorization_id", sa.String(300), nullable=False),
        sa.Column("closing_order_id", sa.String(300), nullable=False),
        sa.Column("realized_paper_pnl", sa.Numeric, nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.ForeignKeyConstraint(
            ["trial_id"],
            ["internal_strategy_paper_trial.trial_id"],
            name="fk_internal_strategy_paper_trial_closure_trial",
        ),
        sa.CheckConstraint(
            "terminal_reason IN (" + ",".join(f"'{value}'" for value in _TERMINAL_REASONS) + ")",
            name="ck_internal_strategy_paper_trial_closure_reason",
        ),
        sa.CheckConstraint(
            "authority = 'NONE'", name="ck_internal_strategy_paper_trial_closure_authority"
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_internal_strategy_paper_trial_closure_hash"
        ),
    )
    op.create_index(
        "ix_internal_strategy_paper_trial_closure_closed_at",
        "internal_strategy_paper_trial_closure",
        ["closed_at"],
    )

    for table in STRATEGY_PAPER_TRIAL_LIFECYCLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(STRATEGY_PAPER_TRIAL_LIFECYCLE_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0111 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(STRATEGY_PAPER_TRIAL_LIFECYCLE_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    op.drop_table("internal_strategy_paper_trial_closure")
    op.drop_table("internal_strategy_paper_trial")
