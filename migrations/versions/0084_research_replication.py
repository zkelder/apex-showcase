"""Calibration-Qualified Interaction Replication v1.

Six new, narrowly-scoped, immutable, append-only tables for facts no
``0082``/``0083`` table can represent faithfully:

* ``research_replication_specification`` -- the frozen parent-hypothesis
  lineage plus fixed primary configuration
  (``domain.research_replication.ReplicationSpecification``). Exactly one
  row per replicated follow-up id; references the historical
  ``cross_market_follow_up_proposal``, ``cross_market_hypothesis``,
  ``research_conclusion``, ``cross_market_research_agenda``, both v1 and
  v2 ``research_calibration_plan`` rows, and the v2
  ``research_calibration_v2_design_classification`` row it froze, but
  never modifies any of them.
* ``research_replication_timing`` -- the two predeclared cross-market
  timing-alignment checks (primary same-day, lagged-equity robustness).
* ``research_replication_partition`` -- full-sample, TIME_PERIOD, and
  VOLATILITY_STATE breakdowns (one shape; a ``partition_kind`` column
  discriminates).
* ``research_replication_effect_profile`` -- the combined effect-size,
  distributional, and outlier-robustness profile (one row per
  replication; these are computed together over the identical sample).
* ``research_replication_confound_assessment`` -- the regime-confound
  plausibility check across six already-retained regime dimensions.
* ``research_replication_conclusion`` -- the retained replication
  conclusion, evidence grade, forward-confirmation requirement, and
  follow-up qualification, distinct from (and never a rewrite of) the
  original ``ResearchConclusion``.

Every table is immutable (``apex.adapters.postgres.immutable_trigger_ddl``)
and ``authority``-checked, exactly like ``0082``/``0083``. Downgrade
refuses whenever any replication evidence is retained.

Revision ID: 0084_research_replication
Revises: 0083_calibration_v2_template
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0084_research_replication"
down_revision: str | None = "0083_calibration_v2_template"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "research_replication_specification",
    "research_replication_timing",
    "research_replication_partition",
    "research_replication_effect_profile",
    "research_replication_confound_assessment",
    "research_replication_conclusion",
)
ALL_TABLES = IMMUTABLE_TABLES

_RESEARCH_DESIGNS = (
    "'PREDICTIVE','CONDITIONAL','INTERACTION','CROSS_MARKET','PERSISTENCE','REGIME_COMPARISON'"
)
_CONDITIONING_SHAPES = (
    "'UNCONDITIONAL','NUMERIC_SPLIT','LABEL_SPLIT','NUMERIC_INTERACTION',"
    "'REGIME_LABEL_COMPARISON','STATE_TRANSITION'"
)
_EVIDENCE_GRADES = "'UNRELIABLE','EXPLORATORY','SUGGESTIVE','VALIDATED','STRONG'"
_CONCLUSION_STATES = (
    "'REPLICATED','PARTIALLY_REPLICATED','UNSTABLE','NULL_COMPATIBLE',"
    "'INSUFFICIENT_EVIDENCE','TIMING_INVALID','CONFOUNDING_NOT_RESOLVED'"
)
_QUALIFICATIONS = (
    "'REPLICATION_SUPPORTED','FORWARD_CONFIRMATION_REQUIRED','RESEARCH_ONLY','REJECTED',"
    "'INSUFFICIENT'"
)
_COST_RELEVANCE = (
    "'BELOW_COST_SCALE','COMPARABLE_TO_COST_SCALE','ABOVE_COST_SCALE','COST_EVIDENCE_UNAVAILABLE'"
)


def upgrade() -> None:
    op.create_table(
        "research_replication_specification",
        sa.Column("replication_id", sa.String(160), primary_key=True),
        sa.Column(
            "follow_up_id",
            sa.String(160),
            sa.ForeignKey("cross_market_follow_up_proposal.follow_up_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_hypothesis_id",
            sa.String(160),
            sa.ForeignKey("cross_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_conclusion_id",
            sa.String(160),
            sa.ForeignKey("research_conclusion.conclusion_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_agenda_id",
            sa.String(160),
            sa.ForeignKey("cross_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column(
            "v1_calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "v1_priority_assessment_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_follow_up_priority.assessment_id"),
            nullable=False,
        ),
        sa.Column("v2_calibration_id", sa.String(160), nullable=False),
        sa.Column(
            "v2_classification_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_v2_design_classification.classification_id"),
            nullable=False,
        ),
        sa.Column("research_design", sa.String(40), nullable=False),
        sa.Column("conditioning_shape", sa.String(40), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
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
        sa.Column("is_in_sample_replication", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("follow_up_id", name="uq_rrspec_follow_up"),
        sa.CheckConstraint(f"research_design IN ({_RESEARCH_DESIGNS})", name="ck_rrspec_design"),
        sa.CheckConstraint(
            f"conditioning_shape IN ({_CONDITIONING_SHAPES})", name="ck_rrspec_shape"
        ),
        sa.CheckConstraint("horizon_sessions >= 1", name="ck_rrspec_horizon"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rrspec_authority"),
    )
    op.create_index(
        "ix_rrspec_hypothesis", "research_replication_specification", ["parent_hypothesis_id"]
    )

    op.create_table(
        "research_replication_timing",
        sa.Column("alignment_id", sa.String(160), primary_key=True),
        sa.Column(
            "replication_id",
            sa.String(160),
            sa.ForeignKey("research_replication_specification.replication_id"),
            nullable=False,
        ),
        sa.Column("convention", sa.String(40), nullable=False),
        sa.Column("ordering_valid", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "replication_id", "convention", name="uq_rrtiming_replication_convention"
        ),
        sa.CheckConstraint(
            "convention IN ('PRIMARY_SAME_DAY_V1','LAGGED_EQUITY_ONE_SESSION_V1')",
            name="ck_rrtiming_convention",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rrtiming_authority"),
    )

    op.create_table(
        "research_replication_partition",
        sa.Column("partition_id", sa.String(160), primary_key=True),
        sa.Column(
            "replication_id",
            sa.String(160),
            sa.ForeignKey("research_replication_specification.replication_id"),
            nullable=False,
        ),
        sa.Column("partition_kind", sa.String(20), nullable=False),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("matched_percentile", sa.Numeric(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "replication_id", "partition_kind", "label", name="uq_rrpart_replication_kind_label"
        ),
        sa.CheckConstraint(
            "partition_kind IN ('FULL_SAMPLE','TIME_PERIOD','VOLATILITY_STATE')",
            name="ck_rrpart_kind",
        ),
        sa.CheckConstraint("sample_count >= 0", name="ck_rrpart_sample_count"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rrpart_authority"),
    )
    op.create_index(
        "ix_rrpart_replication_kind",
        "research_replication_partition",
        ["replication_id", "partition_kind"],
    )

    op.create_table(
        "research_replication_effect_profile",
        sa.Column("profile_id", sa.String(160), primary_key=True),
        sa.Column(
            "replication_id",
            sa.String(160),
            sa.ForeignKey("research_replication_specification.replication_id"),
            nullable=False,
        ),
        sa.Column("meets_practical_floor", sa.Boolean(), nullable=False),
        sa.Column("outlier_robust", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("replication_id", name="uq_rreffect_replication"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rreffect_authority"),
    )

    op.create_table(
        "research_replication_confound_assessment",
        sa.Column("assessment_id", sa.String(160), primary_key=True),
        sa.Column(
            "replication_id",
            sa.String(160),
            sa.ForeignKey("research_replication_specification.replication_id"),
            nullable=False,
        ),
        sa.Column("overall_confound_resolved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("replication_id", name="uq_rrconfound_replication"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rrconfound_authority"),
    )

    op.create_table(
        "research_replication_conclusion",
        sa.Column("conclusion_id", sa.String(160), primary_key=True),
        sa.Column(
            "replication_id",
            sa.String(160),
            sa.ForeignKey("research_replication_specification.replication_id"),
            nullable=False,
        ),
        sa.Column(
            "follow_up_id",
            sa.String(160),
            sa.ForeignKey("cross_market_follow_up_proposal.follow_up_id"),
            nullable=False,
        ),
        sa.Column("conclusion_state", sa.String(40), nullable=False),
        sa.Column("evidence_grade", sa.String(20), nullable=False),
        sa.Column("evidence_grade_upgraded", sa.Boolean(), nullable=False),
        sa.Column("transaction_cost_relevance", sa.String(40), nullable=False),
        sa.Column("forward_confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("qualification", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("replication_id", name="uq_rrconclusion_replication"),
        sa.CheckConstraint(
            f"conclusion_state IN ({_CONCLUSION_STATES})", name="ck_rrconclusion_state"
        ),
        sa.CheckConstraint(f"evidence_grade IN ({_EVIDENCE_GRADES})", name="ck_rrconclusion_grade"),
        sa.CheckConstraint(
            f"transaction_cost_relevance IN ({_COST_RELEVANCE})", name="ck_rrconclusion_cost"
        ),
        sa.CheckConstraint(
            f"qualification IN ({_QUALIFICATIONS})", name="ck_rrconclusion_qualification"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rrconclusion_authority"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0084 with retained research-replication evidence'; "
            "END IF; END $$"
        )
    )
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("research_replication_conclusion")
    op.drop_table("research_replication_confound_assessment")
    op.drop_table("research_replication_effect_profile")
    op.drop_table("research_replication_partition")
    op.drop_table("research_replication_timing")
    op.drop_table("research_replication_specification")
