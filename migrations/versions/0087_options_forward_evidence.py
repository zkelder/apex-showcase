"""Fresh Options Evidence Operations and Structure Research Baseline v1.

One new, narrowly-scoped, immutable table retaining the daily longitudinal
options-forward-evidence checkpoint
(``domain.options_research_operations.OptionsForwardEvidenceCheckpoint``):
one row per genuine capture-evidence outcome for one options-research-plan
session, built strictly on top of already-retained evidence in
``options_chain_capture_plan``/``options_chain_snapshot`` (``0085``) and
``option_structure_candidate`` (``0086``) -- never a second quote-chain or
structure-economics pipeline. This table is deliberately the *only* new
persisted fact this milestone needs: due-state, chain-coverage-quality,
distinct-session-history, and cross-sectional-baseline are all pure,
reproducible computations over already-retained evidence
(``domain.options_research_operations``), matching this codebase's existing
convention of not persisting a computed report that can be rebuilt exactly
from retained evidence (mirroring ``StratifiedChainSelection``/
``CohortQuoteQualityReport`` from the two prior options milestones).

``options_forward_evidence_checkpoint`` binds: the exact capture-window
policy and session window used, provider/feed, requested-versus-captured
underlying counts, structures-evaluated/retained counts, and a full
canonical JSON payload carrying every other retained field (fresh/stale
snapshot counts, quote-quality and chain-coverage-quality distributions,
fee/OPRA/open-interest/historical-pricing limitation state, and IV-history/
forward-history readiness counts) -- restorable exactly via
``canonical_rehydrate.rehydrate``, the identical pattern
``options_quote_intelligence_postgres``/``options_structures_postgres``
already use. Identity is content-addressed over the session's contributing
evidence (retained snapshot/structure-snapshot ids and counts), so
recomputing a checkpoint from unchanged evidence -- an exact same-session
replay -- resolves to the identical retained row rather than a duplicate;
newly captured evidence within the same still-open session produces a new,
more complete checkpoint, both retained (append-only), with the most
recently created one being the session's "latest checkpoint".

The table is immutable (``apex.adapters.postgres.immutable_trigger_ddl``)
and ``authority``-checked. Downgrade refuses whenever any evidence is
retained. No order, position, reservation, PAPER, or LIVE-authority column
exists anywhere in this migration; nothing here submits an options order,
creates a PAPER order, or creates a Shared Opportunity.

Revision ID: 0087_options_forward_evidence
Revises: 0086_defined_risk_options_eco
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0087_options_forward_evidence"
down_revision: str | None = "0086_defined_risk_options_eco"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = ("options_forward_evidence_checkpoint",)
ALL_TABLES = IMMUTABLE_TABLES


def upgrade() -> None:
    op.create_table(
        "options_forward_evidence_checkpoint",
        sa.Column("checkpoint_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("options_chain_capture_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(10), nullable=False),
        sa.Column("capture_window_policy_id", sa.String(160), nullable=False),
        sa.Column("window_open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_close_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("feed", sa.String(16), nullable=False),
        sa.Column("underlyings_requested_count", sa.Integer(), nullable=False),
        sa.Column("underlyings_captured_count", sa.Integer(), nullable=False),
        sa.Column("structures_evaluated", sa.Integer(), nullable=False),
        sa.Column("structures_retained", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("window_close_at >= window_open_at", name="ck_ofec_window_order"),
        sa.CheckConstraint(
            "underlyings_requested_count >= 0", name="ck_ofec_requested_non_negative"
        ),
        sa.CheckConstraint(
            "underlyings_captured_count >= 0 "
            "AND underlyings_captured_count <= underlyings_requested_count",
            name="ck_ofec_captured_bounded",
        ),
        sa.CheckConstraint("structures_evaluated >= 0", name="ck_ofec_evaluated_non_negative"),
        sa.CheckConstraint(
            "structures_retained >= 0 AND structures_retained <= structures_evaluated",
            name="ck_ofec_retained_bounded",
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_ofec_authority"),
    )
    op.create_index(
        "ix_ofec_plan_session_created",
        "options_forward_evidence_checkpoint",
        ["plan_id", "session_id", "created_at"],
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0087 with retained options forward-evidence checkpoints'; "
            "END IF; END $$"
        )
    )
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("options_forward_evidence_checkpoint")
