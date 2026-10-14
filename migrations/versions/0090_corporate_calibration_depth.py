"""Corporate Research Calibration Depth v1.

A durable, versioned, bounded calibration plan measuring how the five
corporate research designs Cross-Market Research v3
(``0089_corporate_market_research``) used behave against a structurally
matched, deliberately deepened null-control battery; its resumable
calibration-run cursor; each generated control's retained provenance and
trial-execution outcome; a non-destructive reassessment of the 20 genuine
v3 hypotheses and 17 v3 negative controls; a requalification of the 13
retained v3 follow-up proposals; and one retained calibration-quality
report.

Why this does not reuse ``0082_research_signal_calibration``/
``0083_calibration_v2_template``'s tables, even though several shapes look
similar: ``research_calibration_plan.source_agenda_id`` foreign-keys to
``cross_market_research_agenda`` (not ``corporate_market_research_agenda``)
and its ``dataset_snapshot_id``/``cross_market_dataset_id`` columns to
price-only dataset lineage with no corporate-dataset column at all;
``research_calibration_v2_null_control``'s and ``research_calibration_v2_
design_classification``'s ``research_design`` CHECK constraints are closed
over v1/v2's original six-member taxonomy and do not admit
``CROSS_SECTIONAL``/``CHANGE_BASED``/``FILING_EVENT`` at the database layer
even though ``domain.research_calibration_v2.ResearchDesign`` already
carries them (added additively for v3 without a matching migration, since
v3 never persists a ``DesignClassification`` row); ``research_calibration_
prior_recalibration``/``research_calibration_v2_follow_up_qualification``
foreign-key ``hypothesis_id``/``parent_hypothesis_id`` to ``cross_market_
hypothesis``, not ``corporate_market_hypothesis``. This is exactly the same
"generic governed-trial tables reused unmodified, agenda-shaped wrapper
tables re-declared" split ``0089`` itself already documents and justifies
for the identical reason -- none of it is altered here.

Every governed trial, robustness run, conclusion, and promotion evaluation
this migration's evidence produces is persisted through the exact same,
unmodified ``0079`` tables (``research_governance_policy``, ``research_
trial``, ``robustness_check_run``, ``research_conclusion``, ``research_
promotion_evaluation``) every prior research generation already reuses;
this migration adds only the calibration-depth-specific wrapper records
those generic tables have no concept of. A design-classification-shaped
percentile comparison, a temporal/volatility stability slice, and an
incremental-contribution-calibration verdict are deliberately *not* new
tables here: each is a pure, cheap, deterministic function of already-
retained trial evidence plus this migration's own null-control provenance
and outcome tables, recomputed on demand by
``application.corporate_calibration_depth`` at report time -- the same
"never store a report that is exactly rebuildable from already-retained
evidence" convention ``0089`` itself states explicitly.

``corporate_calibration_run`` is the one deliberately mutable table,
exactly mirroring ``corporate_market_agenda_run``/``research_calibration_
run``. Every other table here is append-only and immutable, protected by
``apex.adapters.postgres.immutable_trigger_ddl``.

Revision ID: 0090_corporate_calibration_depth
Revises: 0089_corporate_market_research
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0090_corporate_calibration_depth"
down_revision: str | None = "0089_corporate_market_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "corporate_calibration_plan",
    "corporate_calibration_null_control",
    "corporate_calibration_null_outcome",
    "corporate_calibration_reassessment",
    "corporate_calibration_follow_up_qualification",
    "corporate_calibration_report",
)
MUTABLE_TABLES = ("corporate_calibration_run",)
ALL_TABLES = IMMUTABLE_TABLES + MUTABLE_TABLES

_RESEARCH_DESIGNS = "'CROSS_SECTIONAL','CHANGE_BASED','FILING_EVENT','CONDITIONAL','INTERACTION'"
_CONDITIONING_SHAPES = "'UNCONDITIONAL','NUMERIC_SPLIT','LABEL_SPLIT','NUMERIC_INTERACTION'"
_MECHANISMS = (
    "'SEEDED_TEMPORAL_DISPLACEMENT','SEEDED_ISSUER_DERANGEMENT',"
    "'SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION','SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION',"
    "'SEEDED_RANDOM_NON_EVENT_DATE_MATCHED','SEEDED_DISPLACED_FILING_EVENT_DATE',"
    "'SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT',"
    "'SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION',"
    "'SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION',"
    "'SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION'"
)
_EVIDENCE_GRADES = "'UNRELIABLE','EXPLORATORY','SUGGESTIVE','VALIDATED','STRONG'"


def upgrade() -> None:
    op.create_table(
        "corporate_calibration_plan",
        sa.Column("calibration_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column(
            "source_agenda_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column(
            "corporate_dataset_id",
            sa.String(160),
            sa.ForeignKey("corporate_history_dataset_snapshot.dataset_id"),
            nullable=False,
        ),
        sa.Column(
            "equity_snapshot_id",
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
        sa.Column("controls_per_stratum", sa.Integer(), nullable=False),
        sa.Column("total_new_control_budget", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "controls_per_stratum >= 20 AND controls_per_stratum <= 40",
            name="ck_ccp_controls_per_stratum",
        ),
        sa.CheckConstraint(
            "total_new_control_budget >= controls_per_stratum AND total_new_control_budget <= 500",
            name="ck_ccp_total_budget",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ccp_authority"),
    )

    op.create_table(
        "corporate_calibration_run",
        sa.Column("calibration_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_plan.calibration_id"),
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
        sa.UniqueConstraint("calibration_id", name="uq_ccrun_calibration"),
        sa.CheckConstraint("cursor_ordinal >= 0", name="ck_ccrun_cursor"),
        sa.CheckConstraint("trials_executed >= 0", name="ck_ccrun_trials"),
        sa.CheckConstraint(
            "status IN ('NOT_STARTED','IN_PROGRESS','COMPLETE','FAILED')", name="ck_ccrun_status"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ccrun_authority"),
    )

    op.create_table(
        "corporate_calibration_null_control",
        sa.Column("spec_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column("matched_stratum", sa.String(200), nullable=False),
        sa.Column("research_design", sa.String(40), nullable=False),
        sa.Column("conditioning_shape", sa.String(40), nullable=False),
        sa.Column("mechanism", sa.String(60), nullable=False),
        sa.Column("feature_id", sa.String(48), nullable=False),
        sa.Column("outcome_id", sa.String(48), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", "ordinal", name="uq_ccnc_calibration_ordinal"),
        sa.CheckConstraint(f"research_design IN ({_RESEARCH_DESIGNS})", name="ck_ccnc_design"),
        sa.CheckConstraint(f"conditioning_shape IN ({_CONDITIONING_SHAPES})", name="ck_ccnc_shape"),
        sa.CheckConstraint(f"mechanism IN ({_MECHANISMS})", name="ck_ccnc_mechanism"),
        sa.CheckConstraint("seed >= 0", name="ck_ccnc_seed"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ccnc_authority"),
    )
    op.create_index("ix_ccnc_calibration", "corporate_calibration_null_control", ["calibration_id"])
    op.create_index("ix_ccnc_stratum", "corporate_calibration_null_control", ["matched_stratum"])

    op.create_table(
        "corporate_calibration_null_outcome",
        sa.Column("outcome_id", sa.String(160), primary_key=True),
        sa.Column(
            "spec_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_null_control.spec_id"),
            nullable=False,
        ),
        sa.Column(
            "calibration_run_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_run.calibration_run_id"),
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
        sa.UniqueConstraint("spec_id", name="uq_cco_spec"),
        sa.CheckConstraint(
            "knowledge_status IN ('ACTIVE_RESEARCH','SUPPORTED_FOR_FURTHER_STUDY','WEAK',"
            "'REJECTED','RETIRED')",
            name="ck_cco_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cco_authority"),
    )
    op.create_index("ix_cco_run", "corporate_calibration_null_outcome", ["calibration_run_id"])

    op.create_table(
        "corporate_calibration_reassessment",
        sa.Column("reassessment_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("new_evidence_grade", sa.String(20), nullable=False),
        sa.Column("assessment_changed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", "subject_id", name="uq_ccra_calibration_subject"),
        sa.CheckConstraint(
            "subject_kind IN ('HYPOTHESIS','NEGATIVE_CONTROL')", name="ck_ccra_kind"
        ),
        sa.CheckConstraint(f"new_evidence_grade IN ({_EVIDENCE_GRADES})", name="ck_ccra_grade"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ccra_authority"),
    )
    op.create_index("ix_ccra_calibration", "corporate_calibration_reassessment", ["calibration_id"])

    op.create_table(
        "corporate_calibration_follow_up_qualification",
        sa.Column("assessment_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "follow_up_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_follow_up_proposal.follow_up_id"),
            nullable=False,
        ),
        sa.Column(
            "parent_hypothesis_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column("qualification", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", "follow_up_id", name="uq_ccfq_calibration_follow_up"),
        sa.CheckConstraint(
            "qualification IN ('CALIBRATION_QUALIFIED','CALIBRATION_INSUFFICIENT',"
            "'NO_LONGER_JUSTIFIED','REJECTED')",
            name="ck_ccfq_qualification",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ccfq_authority"),
    )

    op.create_table(
        "corporate_calibration_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "calibration_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_plan.calibration_id"),
            nullable=False,
        ),
        sa.Column(
            "calibration_run_id",
            sa.String(160),
            sa.ForeignKey("corporate_calibration_run.calibration_run_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("calibration_id", name="uq_ccr_calibration"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ccr_authority"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0090 with retained corporate calibration-depth evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("corporate_calibration_report")
    op.drop_table("corporate_calibration_follow_up_qualification")
    op.drop_table("corporate_calibration_reassessment")
    op.drop_table("corporate_calibration_null_outcome")
    op.drop_table("corporate_calibration_null_control")
    op.drop_table("corporate_calibration_run")
    op.drop_table("corporate_calibration_plan")
