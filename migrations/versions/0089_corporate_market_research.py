"""Cross-Market Research v3: Fundamental and Market Evidence Integration
with Forward Options Seam.

A durable, versioned corporate-evidence research agenda; its deterministically
generated, template-bound hypotheses (including negative controls); one
bounded mutable agenda-run cursor; each hypothesis's retained trial-execution
bundle; the append-only research-knowledge base; retained contradictions;
controlled follow-up proposals; and one agenda-level research-quality report.

This directly mirrors ``0081_cross_market_research``'s own table shape and
justification, re-scoped to corporate hypotheses: ``0081``'s
``cross_market_hypothesis.template_id``/``family`` CHECK constraints are
hard-bound to the price-only vocabulary (``PREDICTIVE_RELATIONSHIP_V1``,
``EQUITY_INTERNAL``, ...) and its agenda table requires a
``cross_market_dataset_id`` lineage FK with no corporate-dataset column --
neither can faithfully represent a corporate-evidence agenda without an
ALTER that would touch an already-retained, immutable table. The actual
governed trial lifecycle underneath every corporate hypothesis is *not*
duplicated here: every corporate hypothesis trial, governance policy,
robustness run, trial family, conclusion, and promotion evaluation is
persisted through the exact same, unmodified ``0079`` tables
(``research_governance_policy``, ``research_trial``, ``research_trial_family``,
``robustness_check_run``, ``research_conclusion``, ``research_promotion_
evaluation``) the price-only agenda already reuses -- this migration adds
only the corporate-agenda-specific wrapper records those generic tables
have no concept of (an agenda, a bounded hypothesis-generation budget, a
knowledge-base revision history, a contradiction, a follow-up proposal).

Incremental-contribution, corporate-contribution-accounting, and matched-
calibration-classification evidence (milestone sections 8, 10, 22) are
deliberately *not* new tables here: every one of those facts is a pure,
cheap, deterministic function of already-persisted ``research_trial``
diagnostics (market-only / corporate-only / combined trial effect sizes)
and is recomputed on demand by ``application.corporate_market_research``
at report time, exactly matching this codebase's existing convention of
never storing a report that is exactly rebuildable from already-retained
evidence.

``corporate_market_agenda_run`` is the one deliberately mutable table,
exactly mirroring ``cross_market_agenda_run``. Every other table here is
append-only and immutable, protected by
``apex.adapters.postgres.immutable_trigger_ddl``.

Revision ID: 0089_corporate_market_research
Revises: 0088_corporate_fundamentals
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0089_corporate_market_research"
down_revision: str | None = "0088_corporate_fundamentals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "corporate_market_research_agenda",
    "corporate_market_hypothesis",
    "corporate_market_hypothesis_execution",
    "corporate_market_knowledge_entry",
    "corporate_market_contradiction",
    "corporate_market_follow_up_proposal",
    "corporate_market_agenda_quality_report",
)
MUTABLE_TABLES = ("corporate_market_agenda_run",)
ALL_TABLES = IMMUTABLE_TABLES + MUTABLE_TABLES


def upgrade() -> None:
    op.create_table(
        "corporate_market_research_agenda",
        sa.Column("agenda_id", sa.String(160), primary_key=True),
        sa.Column("version", sa.String(80), nullable=False),
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
        sa.Column("cohort_policy_id", sa.String(160), nullable=False),
        sa.Column("maximum_genuine_hypothesis_count", sa.Integer(), nullable=False),
        sa.Column("maximum_hypothesis_count", sa.Integer(), nullable=False),
        sa.Column("maximum_total_trials", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "maximum_genuine_hypothesis_count >= 12 AND maximum_genuine_hypothesis_count <= 24",
            name="ck_cmra3_genuine_hypothesis_count",
        ),
        sa.CheckConstraint(
            "maximum_hypothesis_count >= maximum_genuine_hypothesis_count "
            "AND maximum_hypothesis_count <= 60",
            name="ck_cmra3_hypothesis_count",
        ),
        sa.CheckConstraint(
            "maximum_total_trials >= maximum_hypothesis_count AND maximum_total_trials <= 1000",
            name="ck_cmra3_total_trials",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmra3_authority"),
    )

    op.create_table(
        "corporate_market_hypothesis",
        sa.Column("hypothesis_id", sa.String(160), primary_key=True),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column("template_id", sa.String(80), nullable=False),
        sa.Column("family", sa.String(40), nullable=False),
        sa.Column("feature_id", sa.String(48), nullable=False),
        sa.Column("is_negative_control", sa.Boolean(), nullable=False),
        sa.Column("discovery_status", sa.String(40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("agenda_id", "ordinal", name="uq_cmh3_agenda_ordinal"),
        sa.CheckConstraint(
            "template_id IN ('CROSS_SECTIONAL_V1','CORPORATE_CHANGE_V1','FILING_EVENT_V1',"
            "'CONDITIONAL_RELATIONSHIP_V1','INTERACTION_V1')",
            name="ck_cmh3_template",
        ),
        sa.CheckConstraint(
            "family IN ('CROSS_SECTIONAL_LEVEL','CROSS_SECTIONAL_CHANGE','FILING_EVENT',"
            "'CONDITIONAL_MARKET_REGIME','INTERACTION_FUNDAMENTAL_MARKET','NEGATIVE_CONTROL')",
            name="ck_cmh3_family",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmh3_authority"),
    )
    op.create_index("ix_cmh3_agenda", "corporate_market_hypothesis", ["agenda_id"])
    op.create_index("ix_cmh3_family", "corporate_market_hypothesis", ["agenda_id", "family"])

    op.create_table(
        "corporate_market_agenda_run",
        sa.Column("agenda_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_research_agenda.agenda_id"),
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
        sa.UniqueConstraint("agenda_id", name="uq_cmar3_agenda"),
        sa.CheckConstraint("cursor_ordinal >= 0", name="ck_cmar3_cursor"),
        sa.CheckConstraint("trials_executed >= 0", name="ck_cmar3_trials"),
        sa.CheckConstraint(
            "status IN ('NOT_STARTED','IN_PROGRESS','COMPLETE','FAILED')", name="ck_cmar3_status"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmar3_authority"),
    )

    op.create_table(
        "corporate_market_hypothesis_execution",
        sa.Column("execution_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column(
            "governance_policy_id",
            sa.String(160),
            sa.ForeignKey("research_governance_policy.governance_policy_id"),
            nullable=False,
        ),
        sa.Column(
            "conclusion_id",
            sa.String(160),
            sa.ForeignKey("research_conclusion.conclusion_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("hypothesis_id", name="uq_cmhe3_hypothesis"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmhe3_authority"),
    )
    op.create_index("ix_cmhe3_agenda", "corporate_market_hypothesis_execution", ["agenda_id"])

    op.create_table(
        "corporate_market_knowledge_entry",
        sa.Column("entry_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column(
            "conclusion_id",
            sa.String(160),
            sa.ForeignKey("research_conclusion.conclusion_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "supersedes_entry_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_knowledge_entry.entry_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("hypothesis_id", "revision", name="uq_cmke3_hypothesis_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_cmke3_revision"),
        sa.CheckConstraint(
            "status IN ('ACTIVE_RESEARCH','SUPPORTED_FOR_FURTHER_STUDY','WEAK','REJECTED',"
            "'RETIRED')",
            name="ck_cmke3_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmke3_authority"),
    )
    op.create_index("ix_cmke3_hypothesis", "corporate_market_knowledge_entry", ["hypothesis_id"])
    op.create_index("ix_cmke3_status", "corporate_market_knowledge_entry", ["status"])

    op.create_table(
        "corporate_market_contradiction",
        sa.Column("contradiction_id", sa.String(160), primary_key=True),
        sa.Column(
            "first_entry_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_knowledge_entry.entry_id"),
            nullable=False,
        ),
        sa.Column(
            "second_entry_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_knowledge_entry.entry_id"),
            nullable=False,
        ),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("first_entry_id <> second_entry_id", name="ck_cmc3_distinct_entries"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmc3_authority"),
    )
    op.create_index("ix_cmc3_first_entry", "corporate_market_contradiction", ["first_entry_id"])
    op.create_index("ix_cmc3_second_entry", "corporate_market_contradiction", ["second_entry_id"])

    op.create_table(
        "corporate_market_follow_up_proposal",
        sa.Column("follow_up_id", sa.String(160), primary_key=True),
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
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("requires_new_agenda", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "kind IN ('SPLIT_BY_REGIME','ALTERNATE_HORIZON','ADJACENT_LOOKBACK',"
            "'ALTERNATE_MARKET_OUTCOME','INVESTIGATE_ROBUSTNESS_FAILURE')",
            name="ck_cmfup3_kind",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmfup3_authority"),
    )
    op.create_index(
        "ix_cmfup3_parent_hypothesis",
        "corporate_market_follow_up_proposal",
        ["parent_hypothesis_id"],
    )

    op.create_table(
        "corporate_market_agenda_quality_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("corporate_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column("hypotheses_proposed", sa.Integer(), nullable=False),
        sa.Column("trials_run", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("agenda_id", name="uq_cmaqr3_agenda"),
        sa.CheckConstraint("hypotheses_proposed >= 0", name="ck_cmaqr3_hypotheses"),
        sa.CheckConstraint("trials_run >= 0", name="ck_cmaqr3_trials"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmaqr3_authority"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0089 with retained corporate-market research evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("corporate_market_agenda_quality_report")
    op.drop_table("corporate_market_follow_up_proposal")
    op.drop_table("corporate_market_contradiction")
    op.drop_table("corporate_market_knowledge_entry")
    op.drop_table("corporate_market_hypothesis_execution")
    op.drop_table("corporate_market_agenda_run")
    op.drop_table("corporate_market_hypothesis")
    op.drop_table("corporate_market_research_agenda")
