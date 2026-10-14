"""Cross-Market Feature, Regime, and Controlled Hypothesis Research v1:
a durable, versioned research agenda; its deterministically generated,
template-bound hypotheses (including negative controls); one bounded
mutable agenda-run cursor; each hypothesis's retained trial-execution
bundle (governance policy, trial ids, robustness run, stability report,
conclusion, promotion evaluation); the append-only research-knowledge
base (never overwritten -- every conclusion revision is its own row);
retained contradictions between knowledge-base entries; controlled
follow-up proposals; and one agenda-level research-quality report.

Every fact here is genuinely new and cannot be represented faithfully in
any existing table: ``domain.research_methodology``'s tables retain one
trial/conclusion/promotion at a time with no concept of an agenda, a
bounded hypothesis-generation budget, a knowledge-base revision history,
a contradiction, or a follow-up proposal. No existing table from ``0080``
or earlier is altered.

``cross_market_agenda_run`` is the one deliberately mutable table (a
plain, ungoverned-by-trigger table, exactly like ``0079``'s
``walk_forward_run`` in spirit): its cursor advances across bounded
"agenda-run start/resume" invocations. Every other table here is
append-only and immutable, protected by ``apex.adapters.postgres.
immutable_trigger_ddl``.

Revision ID: 0081_cross_market_research
Revises: 0080_genuine_multi_asset_history
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0081_cross_market_research"
down_revision: str | None = "0080_genuine_multi_asset_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "cross_market_research_agenda",
    "cross_market_hypothesis",
    "cross_market_hypothesis_execution",
    "cross_market_knowledge_entry",
    "cross_market_contradiction",
    "cross_market_follow_up_proposal",
    "cross_market_agenda_quality_report",
)
MUTABLE_TABLES = ("cross_market_agenda_run",)
ALL_TABLES = IMMUTABLE_TABLES + MUTABLE_TABLES


def upgrade() -> None:
    op.create_table(
        "cross_market_research_agenda",
        sa.Column("agenda_id", sa.String(160), primary_key=True),
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
        sa.Column("cohort_policy_id", sa.String(160), nullable=False),
        sa.Column("maximum_hypothesis_count", sa.Integer(), nullable=False),
        sa.Column("maximum_total_trials", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "maximum_hypothesis_count >= 1 AND maximum_hypothesis_count <= 30",
            name="ck_cmra_hypothesis_count",
        ),
        sa.CheckConstraint(
            "maximum_total_trials >= maximum_hypothesis_count AND maximum_total_trials <= 2000",
            name="ck_cmra_total_trials",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmra_authority"),
    )

    op.create_table(
        "cross_market_hypothesis",
        sa.Column("hypothesis_id", sa.String(160), primary_key=True),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("cross_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column("template_id", sa.String(80), nullable=False),
        sa.Column("family", sa.String(40), nullable=False),
        sa.Column("is_negative_control", sa.Boolean(), nullable=False),
        sa.Column("discovery_status", sa.String(40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("agenda_id", "ordinal", name="uq_cmh_agenda_ordinal"),
        sa.CheckConstraint(
            "template_id IN ('PREDICTIVE_RELATIONSHIP_V1','CONDITIONAL_RELATIONSHIP_V1',"
            "'CROSS_MARKET_RELATIONSHIP_V1','INTERACTION_V1','PERSISTENCE_V1')",
            name="ck_cmh_template",
        ),
        sa.CheckConstraint(
            "family IN ('EQUITY_INTERNAL','EQUITY_BTC','FIXED_INCOME_CONTEXT',"
            "'COMMODITY_CONTEXT','NEGATIVE_CONTROL')",
            name="ck_cmh_family",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmh_authority"),
    )
    op.create_index("ix_cmh_agenda", "cross_market_hypothesis", ["agenda_id"])
    op.create_index("ix_cmh_family", "cross_market_hypothesis", ["agenda_id", "family"])

    op.create_table(
        "cross_market_agenda_run",
        sa.Column("agenda_run_id", sa.String(160), primary_key=True),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("cross_market_research_agenda.agenda_id"),
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
        sa.UniqueConstraint("agenda_id", name="uq_cmar_agenda"),
        sa.CheckConstraint("cursor_ordinal >= 0", name="ck_cmar_cursor"),
        sa.CheckConstraint("trials_executed >= 0", name="ck_cmar_trials"),
        sa.CheckConstraint(
            "status IN ('NOT_STARTED','IN_PROGRESS','COMPLETE','FAILED')", name="ck_cmar_status"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmar_authority"),
    )

    op.create_table(
        "cross_market_hypothesis_execution",
        sa.Column("execution_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_id",
            sa.String(160),
            sa.ForeignKey("cross_market_hypothesis.hypothesis_id"),
            nullable=False,
        ),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("cross_market_research_agenda.agenda_id"),
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
        sa.UniqueConstraint("hypothesis_id", name="uq_cmhe_hypothesis"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmhe_authority"),
    )
    op.create_index("ix_cmhe_agenda", "cross_market_hypothesis_execution", ["agenda_id"])

    op.create_table(
        "cross_market_knowledge_entry",
        sa.Column("entry_id", sa.String(160), primary_key=True),
        sa.Column(
            "hypothesis_id",
            sa.String(160),
            sa.ForeignKey("cross_market_hypothesis.hypothesis_id"),
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
            sa.ForeignKey("cross_market_knowledge_entry.entry_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("hypothesis_id", "revision", name="uq_cmke_hypothesis_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_cmke_revision"),
        sa.CheckConstraint(
            "status IN ('ACTIVE_RESEARCH','SUPPORTED_FOR_FURTHER_STUDY','WEAK','REJECTED',"
            "'RETIRED')",
            name="ck_cmke_status",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmke_authority"),
    )
    op.create_index("ix_cmke_hypothesis", "cross_market_knowledge_entry", ["hypothesis_id"])
    op.create_index("ix_cmke_status", "cross_market_knowledge_entry", ["status"])

    op.create_table(
        "cross_market_contradiction",
        sa.Column("contradiction_id", sa.String(160), primary_key=True),
        sa.Column(
            "first_entry_id",
            sa.String(160),
            sa.ForeignKey("cross_market_knowledge_entry.entry_id"),
            nullable=False,
        ),
        sa.Column(
            "second_entry_id",
            sa.String(160),
            sa.ForeignKey("cross_market_knowledge_entry.entry_id"),
            nullable=False,
        ),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("first_entry_id <> second_entry_id", name="ck_cmc_distinct_entries"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmc_authority"),
    )
    op.create_index("ix_cmc_first_entry", "cross_market_contradiction", ["first_entry_id"])
    op.create_index("ix_cmc_second_entry", "cross_market_contradiction", ["second_entry_id"])

    op.create_table(
        "cross_market_follow_up_proposal",
        sa.Column("follow_up_id", sa.String(160), primary_key=True),
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
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("requires_new_agenda", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "kind IN ('SPLIT_BY_REGIME','ALTERNATE_HORIZON','ADJACENT_LOOKBACK',"
            "'ALTERNATE_MARKET_OUTCOME','INVESTIGATE_ROBUSTNESS_FAILURE')",
            name="ck_cmfup_kind",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmfup_authority"),
    )
    op.create_index(
        "ix_cmfup_parent_hypothesis", "cross_market_follow_up_proposal", ["parent_hypothesis_id"]
    )

    op.create_table(
        "cross_market_agenda_quality_report",
        sa.Column("report_id", sa.String(160), primary_key=True),
        sa.Column(
            "agenda_id",
            sa.String(160),
            sa.ForeignKey("cross_market_research_agenda.agenda_id"),
            nullable=False,
        ),
        sa.Column("hypotheses_proposed", sa.Integer(), nullable=False),
        sa.Column("trials_run", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("agenda_id", name="uq_cmaqr_agenda"),
        sa.CheckConstraint("hypotheses_proposed >= 0", name="ck_cmaqr_hypotheses"),
        sa.CheckConstraint("trials_run >= 0", name="ck_cmaqr_trials"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_cmaqr_authority"),
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0081 with retained controlled cross-market research evidence'; "
            "END IF; END $$"
        )
    )
    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("cross_market_agenda_quality_report")
    op.drop_table("cross_market_follow_up_proposal")
    op.drop_table("cross_market_contradiction")
    op.drop_table("cross_market_knowledge_entry")
    op.drop_table("cross_market_hypothesis_execution")
    op.drop_table("cross_market_agenda_run")
    op.drop_table("cross_market_hypothesis")
    op.drop_table("cross_market_research_agenda")
