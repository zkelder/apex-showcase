"""Counterfactual Outcome Feedback v1.

Adds ``counterfactual_outcome_aggregate``: one immutable, append-only row
per genuinely computed ``(cohort_id, run_id)``, where ``cohort_id`` is a
pure function of a fully-specified partition key -- ``(outcome_policy_
version, venue, strategy_or_method_id, reason_family, horizon_kind,
baseline_kind, followup_policy_version, classifier_version, evidence_
quality_policy_version)`` -- and ``run_id`` is the caller-supplied,
recurring-cycle-scoped versioning axis across time (mirrors
``StrategyHealthAssessment.health_id``'s own ``f"{run_id}-..."`` idiom, not
a new pattern). This migration creates no new investment judgment and
grants no new authority; it only adds a place to durably record aggregate
evidence over already-retained, already-immutable ``counterfactual_
decision`` / ``counterfactual_decision_classification`` / ``decision_
followup_observation`` / ``followup_evidence_quality_assessment`` rows --
never a verdict on any one retained decision (see
``domain.counterfactual_outcome`` for the full semantic contract this
supports).

Each row is a point-in-time rollup, not a mutable "current cohort state":
re-running the aggregation for the identical cycle (identical ``run_id``)
reproduces the identical ``aggregate_id``/``canonical_hash`` (idempotent
replay, enforced the same read-compare-insert way every other Apex
identity-conflict-checked table already is); a later cycle over more
accumulated evidence adds a new, sibling row under a new ``run_id`` rather
than mutating an earlier one -- "a later accumulation of evidence must not
mutate an earlier historical interpretation in place."

``uq_counterfactual_outcome_aggregate_identity`` restates the domain
identity formula (``cohort_id`` + ``run_id`` -> ``aggregate_id``) at the
database level: even a caller that bypassed
``counterfactual_outcome_aggregate_id`` and somehow computed a different
``aggregate_id`` string for the same pair could not insert a second row for
it. Three additional defensive CHECK constraints restate ``domain.
counterfactual_outcome``'s own internal count invariants (mature <= total
considered; eligible <= mature) so a row that violates them could only
come from bypassing ``build_counterfactual_outcome_aggregates`` entirely.

TRUNCATE: this table follows the majority, established pattern for an
append-only Apex table (migrations 0094/0096/0099: a row-level ``BEFORE
UPDATE OR DELETE`` immutability trigger plus a downgrade guard that refuses
while any row is retained) rather than migration 0100's narrower ``BEFORE
TRUNCATE`` statement-level guard. That guard exists specifically because
dropping a single-table foreign key on ``crypto_discovery_run``/
``crypto_stateful_current_signal`` silently removed a TRUNCATE protection
those two producer tables used to get for free from Postgres itself; this
table never had an incoming foreign key providing that side effect; adding
a bespoke TRUNCATE guard here without an equivalent regression to close
would be inconsistent with the majority of this schema's own append-only
tables, not "established production/test doctrine".

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``,
hardened by Migration 0095 Historical Reproducibility Hardening v1): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application`` -- so a fresh ``0001 -> head`` replay
never depends on how live application code has changed since this
migration shipped. It creates the table only; there is no data to backfill
(a wholly new, forward-only evidence stream computed by an explicitly
triggered recurring task or CLI command, never something a migration
silently does on behalf of an operator).

Revision ID: 0101_counterfactual_outcome
Revises: 0100_crypto_opportunity_lineage
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0101_counterfactual_outcome"
down_revision: str | None = "0100_crypto_opportunity_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTERFACTUAL_OUTCOME_TABLES = ("counterfactual_outcome_aggregate",)

_REASON_FAMILIES = (
    "EVIDENCE_INCOMPLETE",
    "STALE",
    "LIQUIDITY",
    "ECONOMICS",
    "RESEARCH_CONFIDENCE",
    "MANDATE",
    "RISK",
    "CAPACITY",
    "COMPETITIVE_SELECTION",
    "PROVIDER_UNAVAILABLE",
    "OPERATIONAL_FAILURE",
    "OTHER_EXPLICIT",
)


def upgrade() -> None:
    op.create_table(
        "counterfactual_outcome_aggregate",
        sa.Column("aggregate_id", sa.String(300), primary_key=True),
        sa.Column("cohort_id", sa.String(300), nullable=False),
        sa.Column("outcome_policy_version", sa.String(60), nullable=False),
        sa.Column("run_id", sa.String(300), nullable=False),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
        sa.Column("reason_family", sa.String(40), nullable=False),
        sa.Column("horizon_kind", sa.String(20), nullable=False),
        sa.Column("baseline_kind", sa.String(20), nullable=False),
        sa.Column("followup_policy_version", sa.String(40), nullable=False),
        sa.Column("classifier_version", sa.String(40), nullable=False),
        sa.Column("evidence_quality_policy_version", sa.String(60), nullable=False),
        sa.Column("qualification_state", sa.String(40), nullable=False),
        sa.Column("total_observations_considered", sa.Integer(), nullable=False),
        sa.Column("mature_observation_count", sa.Integer(), nullable=False),
        sa.Column("eligible_observation_count", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "outcome_policy_version = 'COUNTERFACTUAL_OUTCOME_POLICY_V1'",
            name="ck_counterfactual_outcome_policy_version",
        ),
        sa.CheckConstraint("venue IN ('CRYPTO','KALSHI')", name="ck_counterfactual_outcome_venue"),
        sa.CheckConstraint(
            "reason_family IN (" + ",".join(f"'{value}'" for value in _REASON_FAMILIES) + ")",
            name="ck_counterfactual_outcome_reason_family",
        ),
        sa.CheckConstraint(
            "horizon_kind IN ('SHORT_HORIZON','STRATEGY_HORIZON')",
            name="ck_counterfactual_outcome_horizon_kind",
        ),
        sa.CheckConstraint(
            "baseline_kind IN ('ORIGIN_EVIDENCE','DECISION_TIME')",
            name="ck_counterfactual_outcome_baseline_kind",
        ),
        sa.CheckConstraint(
            "followup_policy_version IN ('FOLLOWUP_POLICY_V1','FOLLOWUP_POLICY_V2')",
            name="ck_counterfactual_outcome_followup_policy_version",
        ),
        sa.CheckConstraint(
            "classifier_version IN ('REASON_CLASSIFIER_V1','REASON_CLASSIFIER_V2')",
            name="ck_counterfactual_outcome_classifier_version",
        ),
        sa.CheckConstraint(
            "evidence_quality_policy_version = 'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1'",
            name="ck_counterfactual_outcome_evidence_quality_policy_version",
        ),
        sa.CheckConstraint(
            "qualification_state IN "
            "('QUALIFIED','INSUFFICIENT_SAMPLE','VENUE_ECONOMIC_REDUCTION_UNAVAILABLE')",
            name="ck_counterfactual_outcome_qualification_state",
        ),
        sa.CheckConstraint(
            "total_observations_considered >= 0", name="ck_counterfactual_outcome_total_nonneg"
        ),
        sa.CheckConstraint(
            "mature_observation_count >= 0", name="ck_counterfactual_outcome_mature_nonneg"
        ),
        sa.CheckConstraint(
            "eligible_observation_count >= 0", name="ck_counterfactual_outcome_eligible_nonneg"
        ),
        sa.CheckConstraint(
            "mature_observation_count <= total_observations_considered",
            name="ck_counterfactual_outcome_mature_le_total",
        ),
        sa.CheckConstraint(
            "eligible_observation_count <= mature_observation_count",
            name="ck_counterfactual_outcome_eligible_le_mature",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_counterfactual_outcome_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_counterfactual_outcome_authority"),
        # Defensive, DB-level restatement of the domain identity formula
        # (``cohort_id`` + ``run_id`` -> ``aggregate_id``): even a caller
        # that bypassed ``counterfactual_outcome_aggregate_id`` and somehow
        # computed a different ``aggregate_id`` string for the same pair
        # could not insert a second row for it.
        sa.UniqueConstraint(
            "cohort_id", "run_id", name="uq_counterfactual_outcome_aggregate_identity"
        ),
    )
    op.create_index(
        "ix_counterfactual_outcome_cohort_id",
        "counterfactual_outcome_aggregate",
        ["cohort_id"],
    )
    op.create_index(
        "ix_counterfactual_outcome_qualification_state",
        "counterfactual_outcome_aggregate",
        ["qualification_state"],
    )
    op.create_index(
        "ix_counterfactual_outcome_generated_at",
        "counterfactual_outcome_aggregate",
        ["generated_at"],
    )
    op.create_index(
        "ix_counterfactual_outcome_venue_method",
        "counterfactual_outcome_aggregate",
        ["venue", "strategy_or_method_id"],
    )
    for table in COUNTERFACTUAL_OUTCOME_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(COUNTERFACTUAL_OUTCOME_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0101 with retained counterfactual-outcome evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(COUNTERFACTUAL_OUTCOME_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
