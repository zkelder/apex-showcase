"""Rejected-Opportunity / Counterfactual Decision Ledger v1.

Persists one normalized, provider-neutral ``counterfactual_decision`` row per
already-retained decision Apex has made about whether to advance a candidate
Opportunity -- today sourced from two already-durable, already-trustworthy
origins: Shared Opportunity eligibility (``shared_opportunity_set``'s own
embedded eligibility evidence) and Investment Mandate evaluation
(``mandate_evaluation``).  This migration creates no new investment
judgment; it only indexes decisions that already exist elsewhere, so every
row's ``origin_decision_id``/``origin_canonical_hash`` points back at
already-immutable evidence rather than duplicating it.

``authority`` is fixed at ``'NONE'`` for every row: this table records
decisions, it never grants order, reservation, allocation, PAPER, or LIVE
authority.  Append-only/immutable via the existing trigger convention;
downgrade refuses to run while any retained row exists.

Revision ID: 0094_counterfactual_ledger
Revises: 0093_tmc_component_as_of
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0094_counterfactual_ledger"
down_revision: str | None = "0093_tmc_component_as_of"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTERFACTUAL_LEDGER_TABLES = ("counterfactual_decision",)


def upgrade() -> None:
    op.create_table(
        "counterfactual_decision",
        sa.Column("decision_id", sa.String(300), primary_key=True),
        sa.Column("origin", sa.String(40), nullable=False),
        sa.Column("origin_decision_id", sa.String(300), nullable=False),
        sa.Column(
            "subject_projection_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_projection.projection_id"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_set_id",
            sa.String(300),
            sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
            nullable=True,
        ),
        sa.Column(
            "mandate_id",
            sa.String(300),
            sa.ForeignKey("investment_mandate.mandate_id"),
            nullable=True,
        ),
        sa.Column(
            "portfolio_snapshot_id",
            sa.String(300),
            sa.ForeignKey("portfolio_snapshot.snapshot_id"),
            nullable=True,
        ),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disposition", sa.String(20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "origin IN ('SHARED_OPPORTUNITY_ELIGIBILITY','INVESTMENT_MANDATE_EVALUATION')",
            name="ck_counterfactual_decision_origin",
        ),
        sa.CheckConstraint("venue IN ('CRYPTO','KALSHI')", name="ck_counterfactual_decision_venue"),
        sa.CheckConstraint(
            "disposition IN ('ADVANCED','CONSTRAINED','NOT_ADVANCED')",
            name="ck_counterfactual_decision_disposition",
        ),
        sa.CheckConstraint(
            # A mandate-origin row always carries its mandate/portfolio
            # pointers; an eligibility-origin row always carries neither
            # (it has no mandate/portfolio dependency) -- this mirrors the
            # domain builders in ``domain.counterfactual_ledger`` exactly,
            # so a row that violates it could only come from bypassing them.
            "(origin = 'INVESTMENT_MANDATE_EVALUATION' AND mandate_id IS NOT NULL "
            "AND portfolio_snapshot_id IS NOT NULL) OR "
            "(origin = 'SHARED_OPPORTUNITY_ELIGIBILITY' AND mandate_id IS NULL "
            "AND portfolio_snapshot_id IS NULL)",
            name="ck_counterfactual_decision_origin_lineage",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_counterfactual_decision_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_counterfactual_decision_authority"),
    )
    op.create_index(
        "ix_counterfactual_decision_evaluated_at",
        "counterfactual_decision",
        ["evaluated_at"],
    )
    op.create_index(
        "ix_counterfactual_decision_subject",
        "counterfactual_decision",
        ["subject_projection_id"],
    )
    for table in COUNTERFACTUAL_LEDGER_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(COUNTERFACTUAL_LEDGER_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0094 with retained counterfactual-ledger evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(COUNTERFACTUAL_LEDGER_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
