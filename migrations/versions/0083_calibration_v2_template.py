"""Research Calibration v2: Template-Matched Nulls and Stability.

Strictly additive on top of ``0082``: no column, constraint, or row of any
``0082`` table is altered, and no ``0081``-or-earlier table is touched.
Every genuine v2 fact that a ``0082`` table can already represent faithfully
reuses that table unmodified through a *new* ``research_calibration_plan``
row (a distinct ``calibration_id``, version ``"V2"``) --
``research_calibration_plan``, ``research_calibration_run``,
``research_calibration_report``, ``research_calibration_breakdown``,
``research_calibration_method_comparison``, ``research_calibration_prior_
recalibration``, and ``research_calibration_promotion_evaluation`` all
gain new v2 rows without any schema change.

Six genuinely new fact types have no faithful representation in any
``0082`` table and get their own narrowly-scoped tables here:

* ``research_calibration_v2_null_control`` -- a template-matched null
  control (``domain.research_calibration_v2.NullControlSpecV2``) declares
  a ``research_design`` (section 2's taxonomy) and may carry real,
  untransformed secondary conditioning evidence alongside its nulled
  series; this shape does not fit ``0082``'s ``research_calibration_null_
  control`` (whose ``control_kind``/``design_family`` CHECK constraints
  and column set are closed over v1's kind vocabulary and never carry
  conditioning structure).
* ``research_calibration_v2_null_outcome`` -- v1's ``NullControlOutcome``
  dataclass is reused verbatim, but ``0082``'s ``research_calibration_
  null_outcome.spec_id`` foreign-keys to ``research_calibration_null_
  control`` specifically, so a v2 outcome needs its own FK target.
* ``research_calibration_v2_design_classification`` -- section 2's
  mandatory design declaration plus section 7's template-matched
  percentile result, for a null control (v1 *or* v2, by reference) or a
  genuine hypothesis; no ``0082`` table records a design taxonomy or a
  matched (as opposed to pooled) percentile at all.
* ``research_calibration_v2_stability_slice`` -- sections 10/11's
  time-period and volatility-regime false-discovery slices. Deliberately
  not stored as ``research_calibration_breakdown`` rows: a stability
  slice here is a cheaper, honestly-scoped raw/corrected significance
  count (no per-slice holdout/robustness re-run), and reusing
  ``CalibrationBreakdown``'s full-pipeline-shaped summary would overstate
  what was actually evaluated per slice.
* ``research_calibration_v2_walk_forward_null_summary`` -- section 12's
  fold-survival record for the bounded walk-forward-null subset; no
  ``0082`` table retains per-fold null survival.
* ``research_calibration_v2_follow_up_qualification`` -- section 17's new
  vocabulary (``CALIBRATION_QUALIFIED``/``CALIBRATION_INSUFFICIENT``/
  ``NO_LONGER_JUSTIFIED``/``REJECTED``) is genuinely different from
  ``0082``'s closed ``research_calibration_follow_up_priority.
  classification`` CHECK constraint (``JUSTIFIED``/``LOW_PRIORITY``/
  ``UNSUPPORTED_AFTER_CALIBRATION``/``REJECTED``) and therefore cannot be
  represented inside that column; the historical
  ``cross_market_follow_up_proposal`` row and ``0082``'s own v1
  ``research_calibration_follow_up_priority`` row are only ever
  *referenced* here by id, never modified.

Every table here is immutable and append-only (``apex.adapters.postgres.
immutable_trigger_ddl``) and ``authority``-checked, exactly like ``0082``.
Downgrade refuses whenever any v2 evidence is retained.

Revision ID: 0083_calibration_v2_template
Revises: 0082_research_signal_calibration
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0083_calibration_v2_template"
down_revision: str | None = "0082_research_signal_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "research_calibration_v2_null_control",
    "research_calibration_v2_null_outcome",
    "research_calibration_v2_design_classification",
    "research_calibration_v2_stability_slice",
    "research_calibration_v2_walk_forward_null_summary",
    "research_calibration_v2_follow_up_qualification",
)
ALL_TABLES = IMMUTABLE_TABLES

_RESEARCH_DESIGNS = (
    "'PREDICTIVE','CONDITIONAL','INTERACTION','CROSS_MARKET','PERSISTENCE','REGIME_COMPARISON'"
)
_EVIDENCE_GRADES = "'UNRELIABLE','EXPLORATORY','SUGGESTIVE','VALIDATED','STRONG'"


def upgrade() -> None:
    op.create_table(
        "research_calibration_v2_null_control",
        sa.Column("spec_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("research_design", sa.String(40), nullable=False),
        sa.Column("conditioning_shape", sa.String(40), nullable=False),
        sa.Column("matched_stratum", sa.String(200), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", "ordinal", name="uq_rcv2nc_calibration_ordinal"),
        sa.CheckConstraint(f"research_design IN ({_RESEARCH_DESIGNS})", name="ck_rcv2nc_design"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcv2nc_authority"),
    )
    op.create_index(
        "ix_rcv2nc_calibration", "research_calibration_v2_null_control", ["calibration_id"]
    )
    op.create_index(
        "ix_rcv2nc_calibration_design",
        "research_calibration_v2_null_control",
        ["calibration_id", "research_design"],
    )
    op.create_index(
        "ix_rcv2nc_stratum", "research_calibration_v2_null_control", ["matched_stratum"]
    )

    op.create_table(
        "research_calibration_v2_null_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "spec_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_v2_null_control.spec_id"),
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
        sa.UniqueConstraint("spec_id", name="uq_rcv2no_spec"),
        sa.CheckConstraint(
            "knowledge_status IN ('ACTIVE_RESEARCH','SUPPORTED_FOR_FURTHER_STUDY','WEAK',"
            "'REJECTED','RETIRED')",
            name="ck_rcv2no_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcv2no_authority"),
    )
    op.create_index("ix_rcv2no_run", "research_calibration_v2_null_outcome", ["calibration_run_id"])

    op.create_table(
        "research_calibration_v2_design_classification",
        sa.Column("classification_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("subject_id", sa.String(160), nullable=False),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("research_design", sa.String(40), nullable=False),
        sa.Column("matched_null_count", sa.Integer(), nullable=False),
        sa.Column("calibration_sufficient", sa.Boolean(), nullable=False),
        sa.Column("evidence_grade_v2", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", "subject_id", name="uq_rcv2dc_calibration_subject"),
        sa.CheckConstraint("subject_kind IN ('NULL_CONTROL','HYPOTHESIS')", name="ck_rcv2dc_kind"),
        sa.CheckConstraint(f"research_design IN ({_RESEARCH_DESIGNS})", name="ck_rcv2dc_design"),
        sa.CheckConstraint("matched_null_count >= 0", name="ck_rcv2dc_count"),
        sa.CheckConstraint(f"evidence_grade_v2 IN ({_EVIDENCE_GRADES})", name="ck_rcv2dc_grade"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcv2dc_authority"),
    )
    op.create_index(
        "ix_rcv2dc_calibration_design",
        "research_calibration_v2_design_classification",
        ["calibration_id", "research_design"],
    )

    op.create_table(
        "research_calibration_v2_stability_slice",
        sa.Column("slice_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("dimension_kind", sa.String(40), nullable=False),
        sa.Column("dimension_label", sa.String(80), nullable=False),
        sa.Column("control_count", sa.Integer(), nullable=False),
        sa.Column("sample_adequate", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "calibration_id",
            "dimension_kind",
            "dimension_label",
            name="uq_rcv2ss_calibration_dimension",
        ),
        sa.CheckConstraint(
            "dimension_kind IN ('TIME_PERIOD','VOLATILITY_REGIME')", name="ck_rcv2ss_dimension"
        ),
        sa.CheckConstraint("control_count >= 0", name="ck_rcv2ss_count"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcv2ss_authority"),
    )

    op.create_table(
        "research_calibration_v2_walk_forward_null_summary",
        sa.Column("summary_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "spec_id",
            sa.String(160),
            sa.ForeignKey("research_calibration_v2_null_control.spec_id"),
            nullable=False,
        ),
        sa.Column("fold_count", sa.Integer(), nullable=False),
        sa.Column("folds_beating_benchmark", sa.Integer(), nullable=False),
        sa.Column("final_evidence_grade", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("spec_id", name="uq_rcv2wf_spec"),
        sa.CheckConstraint("fold_count >= 0", name="ck_rcv2wf_fold_count"),
        sa.CheckConstraint(
            "folds_beating_benchmark >= 0 AND folds_beating_benchmark <= fold_count",
            name="ck_rcv2wf_folds_beating",
        ),
        sa.CheckConstraint(f"final_evidence_grade IN ({_EVIDENCE_GRADES})", name="ck_rcv2wf_grade"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcv2wf_authority"),
    )

    op.create_table(
        "research_calibration_v2_follow_up_qualification",
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
        sa.Column(
            "parent_hypothesis_id",
            sa.String(160),
            sa.ForeignKey("cross_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column("qualification", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "calibration_id", "follow_up_id", name="uq_rcv2fq_calibration_follow_up"
        ),
        sa.CheckConstraint(
            "qualification IN ('CALIBRATION_QUALIFIED','CALIBRATION_INSUFFICIENT',"
            "'NO_LONGER_JUSTIFIED','REJECTED')",
            name="ck_rcv2fq_qualification",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_rcv2fq_authority"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0083 with retained template-matched-calibration evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("research_calibration_v2_follow_up_qualification")
    op.drop_table("research_calibration_v2_walk_forward_null_summary")
    op.drop_table("research_calibration_v2_stability_slice")
    op.drop_table("research_calibration_v2_design_classification")
    op.drop_table("research_calibration_v2_null_outcome")
    op.drop_table("research_calibration_v2_null_control")
