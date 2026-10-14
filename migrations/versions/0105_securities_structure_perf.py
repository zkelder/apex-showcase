"""Securities Structure Forward Performance v1.

Adds ``securities_structure_outcome_aggregate``: one immutable, append-only
row per genuinely computed ``(cohort_id, run_id)``, where ``cohort_id`` is a
pure function of a fully-specified partition key -- ``(outcome_policy_
version, strategy_or_method_id, structure_type, horizon_kind, baseline_kind,
followup_policy_version, evidence_quality_policy_version)`` -- and ``run_id``
is the caller-supplied, recurring-cycle-scoped versioning axis across time.
Mirrors migration 0101 (``counterfactual_outcome_aggregate``) exactly in
structure and every established idiom; a distinct table because the two
aggregate shapes answer structurally different questions over the retained
evidence (see ``domain.securities_structure_outcome``'s own module
docstring for the full "why a separate mechanism" reasoning, discovered by
direct inspection of the live retained SECURITIES corpus during this
milestone: 270/270 retained SECURITIES eligibility decisions are
``ADVANCED``, so they classify to zero reason families under either
``REASON_CLASSIFIER_V1``/``..._V2`` and can never enter a ``counterfactual_
outcome_aggregate`` cohort -- this table is what makes that same, already-
genuine, already-computed per-decision evidence
(``DecisionFollowUpObservation.subsequent_return``) comparable at all).

This migration creates no new investment judgment and grants no new
authority; it only adds a place to durably record aggregate evidence over
already-retained, already-immutable ``counterfactual_decision`` /
``decision_followup_observation`` / ``followup_evidence_quality_assessment``
rows -- never a verdict on any one retained decision.

Each row is a point-in-time rollup, not a mutable "current cohort state":
re-running the aggregation for the identical cycle (identical ``run_id``)
reproduces the identical ``aggregate_id``/``canonical_hash`` (idempotent
replay); a later cycle over more accumulated evidence adds a new, sibling
row under a new ``run_id`` rather than mutating an earlier one.

``uq_securities_structure_outcome_aggregate_identity`` restates the domain
identity formula (``cohort_id`` + ``run_id`` -> ``aggregate_id``) at the
database level. Defensive CHECK constraints restate ``domain.securities_
structure_outcome``'s own internal count invariants (mature <= total
considered; eligible <= mature) so a row that violates them could only come
from bypassing ``build_securities_structure_outcome_aggregates`` entirely.
``qualification_state`` admits only ``QUALIFIED``/``INSUFFICIENT_SAMPLE`` --
unlike ``counterfactual_outcome_aggregate``, there is no venue-economic-
reduction-unavailable state here, because every SECURITIES structure
follow-up observation already carries a directly usable return (see the
domain module docstring). ``baseline_kind`` admits only ``DECISION_TIME``:
``domain.counterfactual_followup._baseline_kinds_for`` returns exactly
``(DECISION_TIME,)`` for SECURITIES under every known followup policy
version -- a genuine structural fact confirmed by direct inspection, not a
current-data coincidence -- so ``ORIGIN_EVIDENCE`` (CRYPTO-only) is
correctly never admitted.

TRUNCATE: follows the same majority append-only-table pattern as migration
0101 (a row-level ``BEFORE UPDATE OR DELETE`` immutability trigger plus a
downgrade guard that refuses while any row is retained).

Additively widens two pre-existing, unrelated CHECK constraints so
``ResearchVenue.SECURITIES`` (``domain.research_operations``, added
alongside this migration) is a legal value wherever a governed research
fact/proposal already records its venue -- ``research_agenda_item_venue_
check`` and ``governed_research_proposal_venue_check`` (migration 0065,
originally created without an explicit ``name=`` and thus carrying
Postgres's own auto-generated names, confirmed by direct inspection of the
live database rather than assumed). Mirrors migration 0104's own
``_WIDENED_CONSTRAINTS`` idiom for widening a pre-existing venue CHECK
exactly. No other column on either table is touched, and no row on either
table is modified.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application``. It creates the new table and widens
the two constraints only; there is no data to backfill.

Revision ID: 0105_securities_structure_perf
Revises: 0104_securities_bridge_v1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0105_securities_structure_perf"
down_revision: str | None = "0104_securities_bridge_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECURITIES_STRUCTURE_OUTCOME_TABLES = ("securities_structure_outcome_aggregate",)

_STRUCTURE_TYPES = (
    "CALL_DEBIT_VERTICAL",
    "PUT_DEBIT_VERTICAL",
    "CALL_CREDIT_VERTICAL",
    "PUT_CREDIT_VERTICAL",
)

_VENUE_NARROW = "venue IN ('CRYPTO','KALSHI','CROSS_VENUE')"
_VENUE_WIDE = "venue IN ('CRYPTO','KALSHI','CROSS_VENUE','SECURITIES')"

_WIDENED_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("research_agenda_item", "research_agenda_item_venue_check"),
    ("governed_research_proposal", "governed_research_proposal_venue_check"),
)


def upgrade() -> None:
    op.create_table(
        "securities_structure_outcome_aggregate",
        sa.Column("aggregate_id", sa.String(300), primary_key=True),
        sa.Column("cohort_id", sa.String(300), nullable=False),
        sa.Column("outcome_policy_version", sa.String(60), nullable=False),
        sa.Column("run_id", sa.String(300), nullable=False),
        sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
        sa.Column("structure_type", sa.String(40), nullable=False),
        sa.Column("horizon_kind", sa.String(20), nullable=False),
        sa.Column("baseline_kind", sa.String(20), nullable=False),
        sa.Column("followup_policy_version", sa.String(40), nullable=False),
        sa.Column("evidence_quality_policy_version", sa.String(60), nullable=False),
        sa.Column("qualification_state", sa.String(40), nullable=False),
        sa.Column("total_decisions_considered", sa.Integer(), nullable=False),
        sa.Column("mature_observation_count", sa.Integer(), nullable=False),
        sa.Column("eligible_observation_count", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "outcome_policy_version = 'SECURITIES_STRUCTURE_OUTCOME_POLICY_V1'",
            name="ck_securities_structure_outcome_policy_version",
        ),
        sa.CheckConstraint(
            "structure_type IN (" + ",".join(f"'{value}'" for value in _STRUCTURE_TYPES) + ")",
            name="ck_securities_structure_outcome_structure_type",
        ),
        sa.CheckConstraint(
            "horizon_kind IN ('SHORT_HORIZON','STRATEGY_HORIZON')",
            name="ck_securities_structure_outcome_horizon_kind",
        ),
        sa.CheckConstraint(
            "baseline_kind IN ('DECISION_TIME')",
            name="ck_securities_structure_outcome_baseline_kind",
        ),
        sa.CheckConstraint(
            "followup_policy_version IN ('FOLLOWUP_POLICY_V1','FOLLOWUP_POLICY_V2')",
            name="ck_securities_structure_outcome_followup_policy_version",
        ),
        sa.CheckConstraint(
            "evidence_quality_policy_version = 'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1'",
            name="ck_securities_structure_outcome_evidence_quality_policy_version",
        ),
        sa.CheckConstraint(
            "qualification_state IN ('QUALIFIED','INSUFFICIENT_SAMPLE')",
            name="ck_securities_structure_outcome_qualification_state",
        ),
        sa.CheckConstraint(
            "total_decisions_considered >= 0", name="ck_securities_structure_outcome_total_nonneg"
        ),
        sa.CheckConstraint(
            "mature_observation_count >= 0", name="ck_securities_structure_outcome_mature_nonneg"
        ),
        sa.CheckConstraint(
            "eligible_observation_count >= 0",
            name="ck_securities_structure_outcome_eligible_nonneg",
        ),
        sa.CheckConstraint(
            "mature_observation_count <= total_decisions_considered",
            name="ck_securities_structure_outcome_mature_le_total",
        ),
        sa.CheckConstraint(
            "eligible_observation_count <= mature_observation_count",
            name="ck_securities_structure_outcome_eligible_le_mature",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_securities_structure_outcome_hash"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_securities_structure_outcome_authority"),
        # Defensive, DB-level restatement of the domain identity formula
        # (``cohort_id`` + ``run_id`` -> ``aggregate_id``): even a caller
        # that bypassed ``securities_structure_outcome_aggregate_id`` and
        # somehow computed a different ``aggregate_id`` string for the same
        # pair could not insert a second row for it.
        sa.UniqueConstraint(
            "cohort_id", "run_id", name="uq_securities_structure_outcome_aggregate_identity"
        ),
    )
    op.create_index(
        "ix_securities_structure_outcome_cohort_id",
        "securities_structure_outcome_aggregate",
        ["cohort_id"],
    )
    op.create_index(
        "ix_securities_structure_outcome_qualification_state",
        "securities_structure_outcome_aggregate",
        ["qualification_state"],
    )
    op.create_index(
        "ix_securities_structure_outcome_generated_at",
        "securities_structure_outcome_aggregate",
        ["generated_at"],
    )
    op.create_index(
        "ix_securities_structure_outcome_method_structure",
        "securities_structure_outcome_aggregate",
        ["strategy_or_method_id", "structure_type"],
    )
    for table in SECURITIES_STRUCTURE_OUTCOME_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))

    for table, name in _WIDENED_CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _VENUE_WIDE)


def downgrade() -> None:
    for table, _name in _WIDENED_CONSTRAINTS:
        op.execute(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {table} WHERE venue = 'SECURITIES') "  # nosec B608
            "THEN RAISE EXCEPTION "
            f"'cannot downgrade 0105 with retained SECURITIES {table} evidence'; "
            "END IF; END $$"
        )
    for table, name in reversed(_WIDENED_CONSTRAINTS):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, _VENUE_NARROW)

    for table in reversed(SECURITIES_STRUCTURE_OUTCOME_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0105 with retained securities-structure-outcome evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(SECURITIES_STRUCTURE_OUTCOME_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
