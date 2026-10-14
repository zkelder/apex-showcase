"""Kalshi Decision-Linked Baseline Quote Evidence v1.

Adds ``kalshi_baseline_quote``: one immutable, append-only row per genuinely
captured ``decision_id`` -- the decision-time ("baseline") quote evidence
``domain.counterfactual_outcome`` needs, and has never had, to reduce a
Kalshi ``CounterfactualDecision`` to an economic return. Closes the evidence
gap that module's own docstring names as its prerequisite: "retaining a
baseline-time Kalshi quote (an analogue of CRYPTO's
``reference_price_at_baseline``/``ORIGIN_EVIDENCE``)".

``quote_id`` is a pure function of ``(policy_version, decision_id)`` only
(``domain.kalshi_baseline_quote.kalshi_baseline_quote_id``) -- never of
``captured_at``/``run_id``, both retained purely as "when Apex derived this
row" provenance and excluded from the identity and from
``canonical_hash``, mirroring ``counterfactual_outcome_aggregate`` excluding
``generated_at`` and ``counterfactual_decision_classification`` excluding
``classified_at`` for the identical crash-resume-replay reason. At most one
baseline quote can ever exist per decision under one policy version.
``uq_kalshi_baseline_quote_decision`` restates that identity at the database
level: even a caller that bypassed ``kalshi_baseline_quote_id`` and somehow
computed a different ``quote_id`` string for the same ``decision_id`` could
not insert a second row for it.

``decision_id`` carries a real foreign key to
``counterfactual_decision.decision_id`` (that table's own immutable-parent
pattern, mirroring ``decision_followup_observation`` in migration 0096) --
unlike ``counterfactual_outcome_aggregate`` (migration 0101), which is a
cohort/rollup table one level removed from any single decision, this table
genuinely is one row per decision, so a real FK is both possible and correct
here.

This migration creates no new investment judgment and grants no new
authority: every row's granular per-leg evidence (``payload``'s ``legs``
array) is copied verbatim from already-retained, already-immutable
``binary_complement_evaluation``/``binary_complement_leg`` or
``threshold_bundle_evaluation``/``threshold_matched_slice`` rows a
``CounterfactualDecision`` already references by exact id and hash -- see
``domain.kalshi_baseline_quote`` for the full evidence-provenance contract
this supports, including exactly why ``ExhaustiveEventSetEvaluation``-sourced
decisions are excluded (no persisted evaluation row to source from without a
new computation).

TRUNCATE: follows the majority, established pattern for an append-only Apex
table with an incoming foreign key (migrations 0094/0096/0099/0101: a
row-level ``BEFORE UPDATE OR DELETE`` immutability trigger plus a downgrade
guard that refuses while any row is retained) -- not migration 0100's
narrower statement-level ``BEFORE TRUNCATE`` guard, which exists only to
replace a TRUNCATE protection a dropped foreign key used to provide for
free; this table's own incoming foreign key (on ``decision_id``) already
provides that same protection today, so no bespoke guard is needed.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application``. It creates the table only; there is no
data to backfill (a wholly new, forward-only, prospective-only evidence
stream a recurring operating-cycle task populates -- see
``domain.kalshi_baseline_quote``'s own module docstring for why capturing it
for already-existing historical decisions, using only their own already-
immutable, already-linked evaluation evidence, is not "backfill" in the
prohibited hindsight sense).

Revision ID: 0102_kalshi_baseline_quote
Revises: 0101_counterfactual_outcome
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0102_kalshi_baseline_quote"
down_revision: str | None = "0101_counterfactual_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KALSHI_BASELINE_QUOTE_TABLES = ("kalshi_baseline_quote",)

_SUPPORTED_SOURCE_RECORD_TYPES = ("BinaryComplementEvaluation", "ThresholdBundleEvaluation")


def upgrade() -> None:
    op.create_table(
        "kalshi_baseline_quote",
        sa.Column("quote_id", sa.String(300), primary_key=True),
        sa.Column(
            "decision_id",
            sa.String(300),
            sa.ForeignKey("counterfactual_decision.decision_id"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(60), nullable=False),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("subject_source_record_type", sa.String(80), nullable=False),
        sa.Column("decision_evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(300), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "policy_version = 'KALSHI_BASELINE_QUOTE_POLICY_V1'",
            name="ck_kalshi_baseline_quote_policy_version",
        ),
        sa.CheckConstraint("venue = 'KALSHI'", name="ck_kalshi_baseline_quote_venue"),
        sa.CheckConstraint(
            "subject_source_record_type IN ("
            + ",".join(f"'{value}'" for value in _SUPPORTED_SOURCE_RECORD_TYPES)
            + ")",
            name="ck_kalshi_baseline_quote_source_record_type",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_kalshi_baseline_quote_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_kalshi_baseline_quote_authority"),
        # Defensive, DB-level restatement of the domain identity formula
        # (``policy_version`` + ``decision_id`` -> ``quote_id``, and today
        # exactly one supported policy version exists): even a caller that
        # bypassed ``kalshi_baseline_quote_id`` and somehow computed a
        # different ``quote_id`` string for the same decision could not
        # insert a second row for it.
        sa.UniqueConstraint("decision_id", name="uq_kalshi_baseline_quote_decision"),
    )
    op.create_index(
        "ix_kalshi_baseline_quote_source_record_type",
        "kalshi_baseline_quote",
        ["subject_source_record_type"],
    )
    op.create_index(
        "ix_kalshi_baseline_quote_captured_at", "kalshi_baseline_quote", ["captured_at"]
    )
    op.create_index("ix_kalshi_baseline_quote_run_id", "kalshi_baseline_quote", ["run_id"])
    for table in KALSHI_BASELINE_QUOTE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(KALSHI_BASELINE_QUOTE_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0102 with retained kalshi-baseline-quote evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(KALSHI_BASELINE_QUOTE_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
