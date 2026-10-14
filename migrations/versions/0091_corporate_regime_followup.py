"""Corporate Calibration-Qualified Regime Follow-Up v1.

Persists the immutable specification, matched-null-control provenance, and
result for the single ``SPLIT_BY_REGIME`` follow-up that survived Corporate
Research Calibration Depth v1's requalification
(``0090_corporate_calibration_depth``):
``cross_market_follow_up_622f5a0d3af61dcb7b69bd9b``, proposed against
parent hypothesis ``corporate_market_hypothesis_6d268d63f879f96f113e2737``.

Why this does not reuse ``0089``'s or ``0090``'s tables, even though
several shapes look similar: this is a genuinely new research shape (an
interaction between two regime groups, not a single-group trial or a
single-group matched-null control), so its own statistic -- the difference
between two group effects -- has no faithful representation in
``research_trial`` (one governed trial, one group), ``corporate_
calibration_null_control``/``corporate_calibration_null_outcome`` (matched
nulls for the *original*, unsplit hypothesis shape), or ``corporate_
calibration_follow_up_qualification`` (the qualification decision that
*preceded* this work, not its result). Migration ``0090``'s own follow-up
qualification and reassessment rows are referenced here by id, never
altered.

Only three narrowly-scoped, append-only, immutable tables are added:
``corporate_regime_followup_specification`` (section 1's frozen parent
semantics, regime-dimension selections, and calibration budget declared
before any result is computed), ``corporate_regime_followup_null_control``
(one row per matched-null replicate -- section 9's interaction-preserving-
regime-split statistic, not a single-group effect), and ``corporate_
regime_followup_result`` (the one genuine result: group evidence/effects,
interaction, temporal/concentration/outlier robustness, incremental
contribution, multiple-testing correction, parent-relationship behavior,
conclusion, evidence grade, and independent-confirmation requirement).
Every genuinely generic computation this module performs (panel-dependence
diagnostics, robustness checks, stability partitioning, BH-FDR correction,
matched-calibration percentile, evidence grading) reuses existing ``0079``/
``0082``/``0083``/``0089`` machinery unmodified and computed on demand --
never a separately persisted table for a fact that is exactly rebuildable
from these three tables' own retained payload.

Revision ID: 0091_corporate_regime_followup
Revises: 0090_corporate_calibration_depth
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0091_corporate_regime_followup"
down_revision: str | None = "0090_corporate_calibration_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "corporate_regime_followup_specification",
    "corporate_regime_followup_null_control",
    "corporate_regime_followup_result",
)
ALL_TABLES = IMMUTABLE_TABLES

_SPLIT_ROLES = "'PRIMARY','SECONDARY'"
_MECHANISMS = (
    "'SEEDED_TEMPORAL_DISPLACEMENT','SEEDED_ISSUER_DERANGEMENT',"
    "'SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION','SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION',"
    "'SEEDED_RANDOM_NON_EVENT_DATE_MATCHED','SEEDED_DISPLACED_FILING_EVENT_DATE',"
    "'SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT',"
    "'SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION',"
    "'SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION',"
    "'SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION'"
)
_CONCLUSIONS = (
    "'REGIME_EFFECT_SUPPORTED','PARTIAL_REGIME_EFFECT','REGIME_EFFECT_UNSTABLE',"
    "'REGIME_EFFECT_NULL_COMPATIBLE','INSUFFICIENT_EVIDENCE','INCREMENTAL_VALUE_ABSENT'"
)
_EVIDENCE_GRADES = "'UNRELIABLE','EXPLORATORY','SUGGESTIVE','VALIDATED','STRONG'"


def upgrade() -> None:
    op.create_table(
        "corporate_regime_followup_specification",
        sa.Column("specification_id", sa.String(160), primary_key=True),
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
        sa.Column(
            "parent_conclusion_id",
            sa.String(160),
            sa.ForeignKey("research_conclusion.conclusion_id"),
            nullable=False,
        ),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column("feature_id", sa.String(48), nullable=False),
        sa.Column("outcome_id", sa.String(48), nullable=False),
        sa.Column("horizon_sessions", sa.Integer(), nullable=False),
        sa.Column("post_hoc_status", sa.String(60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("follow_up_id", name="uq_crfs_follow_up"),
        sa.CheckConstraint("horizon_sessions > 0", name="ck_crfs_horizon"),
        sa.CheckConstraint(
            "post_hoc_status = 'POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP'", name="ck_crfs_post_hoc"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crfs_authority"),
    )

    op.create_table(
        "corporate_regime_followup_null_control",
        sa.Column("control_id", sa.String(160), primary_key=True),
        sa.Column(
            "specification_id",
            sa.String(160),
            sa.ForeignKey("corporate_regime_followup_specification.specification_id"),
            nullable=False,
        ),
        sa.Column("split_role", sa.String(16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("mechanism", sa.String(60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "specification_id", "split_role", "ordinal", name="uq_crfnc_spec_role_ordinal"
        ),
        sa.CheckConstraint(f"split_role IN ({_SPLIT_ROLES})", name="ck_crfnc_role"),
        sa.CheckConstraint(f"mechanism IN ({_MECHANISMS})", name="ck_crfnc_mechanism"),
        sa.CheckConstraint("seed >= 0", name="ck_crfnc_seed"),
        sa.CheckConstraint("ordinal >= 0", name="ck_crfnc_ordinal"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crfnc_authority"),
    )
    op.create_index(
        "ix_crfnc_specification", "corporate_regime_followup_null_control", ["specification_id"]
    )

    op.create_table(
        "corporate_regime_followup_result",
        sa.Column("result_id", sa.String(160), primary_key=True),
        sa.Column(
            "specification_id",
            sa.String(160),
            sa.ForeignKey("corporate_regime_followup_specification.specification_id"),
            nullable=False,
        ),
        sa.Column("conclusion", sa.String(40), nullable=False),
        sa.Column("evidence_grade", sa.String(20), nullable=False),
        sa.Column("independent_confirmation_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("specification_id", name="uq_crfr_specification"),
        sa.CheckConstraint(f"conclusion IN ({_CONCLUSIONS})", name="ck_crfr_conclusion"),
        sa.CheckConstraint(f"evidence_grade IN ({_EVIDENCE_GRADES})", name="ck_crfr_grade"),
        sa.CheckConstraint(
            "independent_confirmation_required = true", name="ck_crfr_confirmation_required"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_crfr_authority"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0091 with retained corporate regime follow-up evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("corporate_regime_followup_result")
    op.drop_table("corporate_regime_followup_null_control")
    op.drop_table("corporate_regime_followup_specification")
