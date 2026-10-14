"""Corporate Fundamental, Filing, and Event Intelligence v1.

Seven new, narrowly-scoped, immutable/append-only tables retaining point-in-
time official SEC filing/fact evidence and its bounded derived research
artifacts (``domain.corporate_fundamentals``):

* ``corporate_evidence_cohort_plan`` -- one row per registered bounded
  capture plan (cohort members resolved to CIK, capture bounds).
* ``corporate_filing_event`` -- one row per retained SEC filing (10-K,
  10-K/A, 10-Q, 10-Q/A, 8-K, 8-K/A only; every other observed form is
  counted, never dropped, in the data-quality report).
* ``corporate_fundamental_fact`` -- one row per retained structured XBRL
  fact. Identity is content-addressed over every field including
  ``accession_number``, so a later restatement of the identical concept/
  period is a genuinely new, separately retained row -- this table is
  append-only and nothing is ever overwritten in place; point-in-time
  resolution (``resolve_fact_as_of``) is a pure read-time query over it.
* ``corporate_evidence_capture_checkpoint`` -- one row per bounded capture
  run, resumable via its own retained ``cumulative_processed_ciks``.
* ``corporate_data_quality_report`` -- one row per quality assessment.
* ``corporate_history_dataset_snapshot`` -- one immutable dataset manifest
  binding a plan's checkpoints/filings/facts/quality report together --
  never a parallel backtest datastore, purely a content-addressed manifest.
* ``replay_corporate_evidence_attachment`` -- the additive Historical
  Replay integration seam: one row per (replay run, frame, security) this
  milestone's snapshot/feature reconstruction was attached to. No column
  of any existing replay table is read or written by this migration.

Every table is immutable (``apex.adapters.postgres.immutable_trigger_ddl``)
and ``authority``-checked. Downgrade refuses whenever any evidence is
retained. No Order, Position, reservation, PAPER, or LIVE-authority column
exists anywhere in this migration; nothing here submits a trade, creates a
PAPER order, or creates a Shared Opportunity. Options/Coinbase/Kalshi
schema is entirely unmodified by this revision.

Revision ID: 0088_corporate_fundamentals
Revises: 0087_options_forward_evidence

Note: alembic_version.version_num is VARCHAR(32); as with 0086's own
truncated revision id ("0086_defined_risk_options_eco"), the id below is
shortened to fit while the filename stays fully descriptive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0088_corporate_fundamentals"
down_revision: str | None = "0087_options_forward_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "corporate_evidence_cohort_plan",
    "corporate_filing_event",
    "corporate_fundamental_fact",
    "corporate_evidence_capture_checkpoint",
    "corporate_data_quality_report",
    "corporate_history_dataset_snapshot",
    "replay_corporate_evidence_attachment",
)
ALL_TABLES = IMMUTABLE_TABLES


def upgrade() -> None:
    op.create_table(
        "corporate_evidence_cohort_plan",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column("cohort_policy_id", sa.String(160), nullable=False),
        sa.Column("cohort_policy_hash", sa.String(64), nullable=False),
        sa.Column("bounds_id", sa.String(160), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("member_count >= 0", name="ck_cecp_member_count_non_negative"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cecp_authority"),
    )

    op.create_table(
        "corporate_filing_event",
        sa.Column("filing_event_id", sa.String(160), primary_key=True),
        sa.Column("stable_security_id", sa.String(160), nullable=False),
        sa.Column("cik", sa.String(10), nullable=False),
        sa.Column("accession_number", sa.String(20), nullable=False, unique=True),
        sa.Column("form", sa.String(10), nullable=False),
        sa.Column("amendment_state", sa.String(20), nullable=False),
        sa.Column("report_period", sa.String(10), nullable=True),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("primary_document", sa.String(300), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("file_number", sa.String(40), nullable=True),
        sa.Column("is_xbrl", sa.Boolean(), nullable=False),
        sa.Column("is_inline_xbrl", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "accepted_at IS NULL OR accepted_at >= filed_at - INTERVAL '1 day'",
            name="ck_cfe_accepted_not_before_filed",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cfe_authority"),
    )
    op.create_index(
        "ix_cfe_security_accepted", "corporate_filing_event", ["stable_security_id", "accepted_at"]
    )
    op.create_index(
        "ix_cfe_security_form", "corporate_filing_event", ["stable_security_id", "form"]
    )
    op.create_index("ix_cfe_cik", "corporate_filing_event", ["cik"])

    op.create_table(
        "corporate_fundamental_fact",
        sa.Column("fact_id", sa.String(160), primary_key=True),
        sa.Column("stable_security_id", sa.String(160), nullable=False),
        sa.Column("cik", sa.String(10), nullable=False),
        sa.Column("concept", sa.String(40), nullable=False),
        sa.Column("source_taxonomy", sa.String(20), nullable=False),
        sa.Column("source_tag", sa.String(160), nullable=False),
        sa.Column("unit", sa.String(20), nullable=False),
        sa.Column("period_type", sa.String(10), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_period", sa.String(4), nullable=False),
        sa.Column("period_start", sa.String(10), nullable=True),
        sa.Column("period_end", sa.String(10), nullable=False),
        # NUMERIC(38, 8), matching the codebase's established wide-USD-amount
        # precedent (e.g. 0063's cross_venue_allocation.amount): a mega-cap's
        # revenue/assets can carry up to 18 significant digits per
        # domain.values.parse_decimal's own MAX_PRECISION, which a tighter
        # NUMERIC(18, 8) (only 10 integer digits) would reject outright for
        # any real value at or above ten billion.
        sa.Column("value", sa.Numeric(38, 8), nullable=False),
        sa.Column("accession_number", sa.String(20), nullable=False),
        sa.Column("form", sa.String(10), nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frame", sa.String(40), nullable=True),
        sa.Column("is_ytd_derived", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "accepted_at IS NULL OR accepted_at >= filed_at - INTERVAL '1 day'",
            name="ck_cff_accepted_not_before_filed",
        ),
        sa.CheckConstraint("fiscal_year >= 1993", name="ck_cff_fiscal_year_bounded"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cff_authority"),
    )
    op.create_index(
        "ix_cff_security_concept_period",
        "corporate_fundamental_fact",
        ["stable_security_id", "concept", "period_end"],
    )
    op.create_index(
        "ix_cff_security_accepted",
        "corporate_fundamental_fact",
        ["stable_security_id", "accepted_at"],
    )
    op.create_index("ix_cff_accession", "corporate_fundamental_fact", ["accession_number"])

    op.create_table(
        "corporate_evidence_capture_checkpoint",
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("corporate_evidence_cohort_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column("issuers_attempted_count", sa.Integer(), nullable=False),
        sa.Column("issuers_captured_count", sa.Integer(), nullable=False),
        sa.Column("total_requests_this_run", sa.Integer(), nullable=False),
        sa.Column("total_bytes_this_run", sa.BigInteger(), nullable=False),
        sa.Column("runtime_seconds", sa.Numeric(12, 3), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "issuers_captured_count >= 0 AND issuers_captured_count <= issuers_attempted_count",
            name="ck_cecc_captured_bounded",
        ),
        sa.CheckConstraint("total_requests_this_run >= 0", name="ck_cecc_requests_non_negative"),
        sa.CheckConstraint("total_bytes_this_run >= 0", name="ck_cecc_bytes_non_negative"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cecc_authority"),
    )
    op.create_index(
        "ix_cecc_plan_created", "corporate_evidence_capture_checkpoint", ["plan_id", "created_at"]
    )

    op.create_table(
        "corporate_data_quality_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("corporate_evidence_cohort_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("issuers_requested", sa.Integer(), nullable=False),
        sa.Column("issuers_captured", sa.Integer(), nullable=False),
        sa.Column("point_in_time_completeness", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "issuers_captured >= 0 AND issuers_captured <= issuers_requested",
            name="ck_cdqr_captured_bounded",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cdqr_authority"),
    )
    op.create_index(
        "ix_cdqr_plan_created", "corporate_data_quality_report", ["plan_id", "created_at"]
    )

    op.create_table(
        "corporate_history_dataset_snapshot",
        sa.Column("dataset_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("corporate_evidence_cohort_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("filing_count", sa.Integer(), nullable=False),
        sa.Column(
            "quality_report_id",
            sa.String(160),
            sa.ForeignKey("corporate_data_quality_report.report_id"),
            nullable=False,
        ),
        sa.Column("completeness", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("fact_count >= 0", name="ck_chds_fact_count_non_negative"),
        sa.CheckConstraint("filing_count >= 0", name="ck_chds_filing_count_non_negative"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_chds_authority"),
    )
    op.create_index(
        "ix_chds_plan_created", "corporate_history_dataset_snapshot", ["plan_id", "created_at"]
    )

    op.create_table(
        "replay_corporate_evidence_attachment",
        sa.Column("attachment_id", sa.String(160), primary_key=True),
        sa.Column("replay_run_id", sa.String(160), nullable=False),
        sa.Column("frame_id", sa.String(160), nullable=False),
        sa.Column("stable_security_id", sa.String(160), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_quality", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcea_authority"),
    )
    op.create_index(
        "ix_rcea_run_frame_security",
        "replay_corporate_evidence_attachment",
        ["replay_run_id", "frame_id", "stable_security_id"],
        unique=True,
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0088 with retained corporate fundamental evidence'; "
            "END IF; END $$"
        )
    )
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("replay_corporate_evidence_attachment")
    op.drop_table("corporate_history_dataset_snapshot")
    op.drop_table("corporate_data_quality_report")
    op.drop_table("corporate_evidence_capture_checkpoint")
    op.drop_table("corporate_fundamental_fact")
    op.drop_table("corporate_filing_event")
    op.drop_table("corporate_evidence_cohort_plan")
