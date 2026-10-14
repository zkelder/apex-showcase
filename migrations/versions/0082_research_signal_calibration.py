"""Research Signal Calibration and Negative-Control Hardening v1: a
versioned, bounded null/negative-control calibration plan against the
same retained cross-market dataset/replay lineage as the genuine
25-hypothesis agenda; its deterministically generated null-control
battery (50-150, declared before results); one bounded mutable
calibration-run cursor; each control's retained trial-execution outcome;
family/horizon/time-period/volatility-regime empirical false-discovery
breakdowns; a statistical-diagnostic-method comparison; one retained
calibration report; a non-destructive, per-hypothesis recalibration of the
genuine agenda's already-retained conclusions; a follow-up-proposal
priority classification; and an additive, calibrated production-promotion
gate layered on top of the existing, unmodified promotion evaluation.

Every fact here is genuinely new and cannot be represented faithfully in
any existing table: no existing table retains a null-control battery, an
empirical false-discovery rate, an evidence grade, or a derived
recalibration of an already-retained conclusion. No table from ``0081`` or
earlier is altered.

``research_calibration_run`` is the one deliberately mutable table (a
plain, ungoverned-by-trigger table, exactly like ``0081``'s
``cross_market_agenda_run`` in spirit): its cursor advances across bounded
"calibration-run start/resume" invocations. Every other table here is
append-only and immutable, protected by
``apex.adapters.postgres.immutable_trigger_ddl``.

Revision ID: 0082_research_signal_calibration
Revises: 0081_cross_market_research
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0082_research_signal_calibration"
down_revision: str | None = "0081_cross_market_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "research_calibration_plan",
    "research_calibration_null_control",
    "research_calibration_null_outcome",
    "research_calibration_breakdown",
    "research_calibration_method_comparison",
    "research_calibration_report",
    "research_calibration_prior_recalibration",
    "research_calibration_follow_up_priority",
    "research_calibration_promotion_evaluation",
)
MUTABLE_TABLES = ("research_calibration_run",)
ALL_TABLES = IMMUTABLE_TABLES + MUTABLE_TABLES


def upgrade() -> None:
    op.create_table(
        "research_calibration_plan",
        sa.Column("calibration_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column(
            "dataset_snapshot_id",
            sa.String(160),
            sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column(
            "replay_run_id",
            sa.String(160),
            sa.ForeignKey("historical_replay_run.replay_run_id"),
            nullable=False,
        ),
        sa.Column(
            "cross_market_dataset_id",
            sa.String(160),
            sa.ForeignKey("cross_market_dataset.cross_market_dataset_id"),
            nullable=False,
        ),
        sa.Column(
            "source_agenda_id",
            sa.String(160),
            sa.ForeignKey("cross_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column("negative_control_budget", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "negative_control_budget >= 50 AND negative_control_budget <= 150",
            name="ck_rcp_budget",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcp_authority"),
    )

    op.create_table(
        "research_calibration_null_control",
        sa.Column("spec_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("family", sa.String(40), nullable=False),
        sa.Column("nature", sa.String(40), nullable=False),
        sa.Column("design_family", sa.String(40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", "ordinal", name="uq_rcnc_calibration_ordinal"),
        sa.CheckConstraint(
            "family IN ('TEMPORAL_DISPLACEMENT','ECONOMIC_NEGATIVE_CONTROL',"
            "'DETERMINISTIC_PERMUTATION','SEEDED_SYNTHETIC_NULL','PERSISTENCE_RANDOM_BASELINE')",
            name="ck_rcnc_family",
        ),
        sa.CheckConstraint(
            "nature IN ('MATHEMATICAL_SYNTHETIC_NULL','TEMPORAL_NULL','ECONOMIC_NEGATIVE_CONTROL')",
            name="ck_rcnc_nature",
        ),
        sa.CheckConstraint(
            "design_family IN ('EQUITY_INTERNAL','EQUITY_BTC','FIXED_INCOME_CONTEXT',"
            "'COMMODITY_CONTEXT','INTERACTION_CONDITIONAL','PURE_MATHEMATICAL_NULL')",
            name="ck_rcnc_design_family",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcnc_authority"),
    )
    op.create_index("ix_rcnc_calibration", "research_calibration_null_control", ["calibration_id"])
    op.create_index(
        "ix_rcnc_calibration_family",
        "research_calibration_null_control",
        ["calibration_id", "family"],
    )

    op.create_table(
        "research_calibration_run",
        sa.Column("calibration_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("cursor_ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("trials_executed", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", name="uq_rcrun_calibration"),
        sa.CheckConstraint("cursor_ordinal >= 0", name="ck_rcrun_cursor"),
        sa.CheckConstraint("trials_executed >= 0", name="ck_rcrun_trials"),
        sa.CheckConstraint(
            "status IN ('NOT_STARTED','IN_PROGRESS','COMPLETE','FAILED')", name="ck_rcrun_status"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcrun_authority"),
    )

    op.create_table(
        "research_calibration_null_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "spec_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_null_control.spec_id"),
            nullable=False,
        ),
        sa.Column(
            "calibration_run_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_run.calibration_run_id"),
            nullable=False,
        ),
        sa.Column(
            "conclusion_id",
            sa.String(160),
            sa.ForeignKey("research_conclusion.conclusion_id"),
            nullable=True,
        ),
        sa.Column("knowledge_status", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("spec_id", name="uq_rcno_spec"),
        sa.CheckConstraint(
            "knowledge_status IN ('ACTIVE_RESEARCH','SUPPORTED_FOR_FURTHER_STUDY','WEAK',"
            "'REJECTED','RETIRED')",
            name="ck_rcno_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcno_authority"),
    )
    op.create_index("ix_rcno_run", "research_calibration_null_outcome", ["calibration_run_id"])

    op.create_table(
        "research_calibration_breakdown",
        sa.Column("breakdown_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("dimension_kind", sa.String(40), nullable=False),
        sa.Column("dimension_label", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcbd_authority"),
    )
    op.create_index(
        "ix_rcbd_calibration_dimension",
        "research_calibration_breakdown",
        ["calibration_id", "dimension_kind"],
    )

    op.create_table(
        "research_calibration_method_comparison",
        sa.Column("comparison_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("method_id", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcmc_authority"),
    )
    op.create_index(
        "ix_rcmc_calibration", "research_calibration_method_comparison", ["calibration_id"]
    )

    op.create_table(
        "research_calibration_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "calibration_run_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_run.calibration_run_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", name="uq_rcr_calibration"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcr_authority"),
    )

    op.create_table(
        "research_calibration_prior_recalibration",
        sa.Column("recalibration_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "hypothesis_id",
            sa.String(160),
            sa.ForeignKey("cross_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column("calibrated_evidence_grade", sa.String(20), nullable=False),
        sa.Column("follow_up_priority", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "calibration_id", "hypothesis_id", name="uq_rcpr_calibration_hypothesis"
        ),
        sa.CheckConstraint(
            "calibrated_evidence_grade IN ('UNRELIABLE','EXPLORATORY','SUGGESTIVE','VALIDATED',"
            "'STRONG')",
            name="ck_rcpr_grade",
        ),
        sa.CheckConstraint(
            "follow_up_priority IN ('JUSTIFIED','LOW_PRIORITY','UNSUPPORTED_AFTER_CALIBRATION',"
            "'REJECTED')",
            name="ck_rcpr_priority",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcpr_authority"),
    )
    op.create_index(
        "ix_rcpr_calibration", "research_calibration_prior_recalibration", ["calibration_id"]
    )

    op.create_table(
        "research_calibration_follow_up_priority",
        sa.Column("assessment_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "follow_up_id",
            sa.String(160),
            sa.ForeignKey("cross_market_follow_up_proposal.follow_up_id"),
            nullable=False,
        ),
        sa.Column("classification", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "calibration_id", "follow_up_id", name="uq_rcfup_calibration_follow_up"
        ),
        sa.CheckConstraint(
            "classification IN ('JUSTIFIED','LOW_PRIORITY','UNSUPPORTED_AFTER_CALIBRATION',"
            "'REJECTED')",
            name="ck_rcfup_classification",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcfup_authority"),
    )

    op.create_table(
        "research_calibration_promotion_evaluation",
        sa.Column("calibrated_promotion_evaluation_id", sa.String(160), primary_key=True),
        sa.Column(
            "promotion_evaluation_id",
            sa.String(160),
            sa.ForeignKey("research_promotion_evaluation.promotion_evaluation_id"),
            nullable=False,
        ),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcpe_authority"),
    )
    op.create_index(
        "ix_rcpe_calibration", "research_calibration_promotion_evaluation", ["calibration_id"]
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0082 with retained research-signal-calibration evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("research_calibration_promotion_evaluation")
    op.drop_table("research_calibration_follow_up_priority")
    op.drop_table("research_calibration_prior_recalibration")
    op.drop_table("research_calibration_report")
    op.drop_table("research_calibration_method_comparison")
    op.drop_table("research_calibration_breakdown")
    op.drop_table("research_calibration_null_outcome")
    op.drop_table("research_calibration_run")
    op.drop_table("research_calibration_null_control")
    op.drop_table("research_calibration_plan")
