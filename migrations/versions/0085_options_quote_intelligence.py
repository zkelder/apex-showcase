"""Genuine Options Quote-Chain and Volatility Intelligence v1.

Six new, narrowly-scoped, immutable tables for genuine *current* option
quote-chain evidence -- a fact family no existing table can represent
faithfully, since ``options_intelligence_contract`` (migration ``0075``)
is provider-neutral contract *reference* identity only (strike/expiration/
type), never a quote, Greek, or implied-volatility observation:

* ``options_quote_capability`` -- the genuine, credential-gated Alpaca
  options quote-chain capability check
  (``domain.options_quote_intelligence.OptionsQuoteProviderCapabilitySnapshot``),
  reusing ``domain.options_intelligence.OptionProviderCapabilities``
  unmodified inside its payload. Capability is never inferred from
  authentication: absent credentials means zero requests and every
  capability dimension explicitly ``UNKNOWN``.
* ``options_chain_capture_plan`` -- the declared, bounded chain-selection
  policy (10-25 underlyings drawn from the existing genuine multi-asset
  research cohort, plus fixed per-underlying request/byte/runtime bounds),
  declared before any quote result exists.
* ``options_chain_capture_run`` -- one row per bounded capture invocation,
  whether it completes, partially completes, or is honestly blocked
  (credentials absent or a provider error) -- the forward-accumulation
  seam a later recurring operator command reuses unmodified.
* ``options_quote_observation`` -- one immutable row per genuine current
  per-contract quote observation, bound to a freshly-derived
  ``options_intelligence.OptionContract`` identity (never a new contract
  identity model). Retained independently of any one snapshot so
  longitudinal options-history accumulates one capture at a time.
* ``options_chain_snapshot`` -- the immutable binding snapshot: descriptive
  profiles, Apex IV/Greeks estimates, volatility-surface primitives, skew,
  term structure, expected-move estimators, chain liquidity profile,
  implied-versus-realized volatility, and arbitrage-obvious anomaly flags,
  all nested in one content-addressed payload. Identity
  (``snapshot_id``) is deliberately narrower than full content: only the
  underlying, the declared capture time, and the observed-evidence hash
  determine it, so replaying identical quote evidence always resolves to
  the identical row and its history-dependent IV-rank is never silently
  rewritten by a later capture that sees more accumulated history.
* ``options_volatility_research_feature`` -- the flat cross-market
  research-seam projection (one row per snapshot): ATM IV, skew, term
  slope, expected move, implied-versus-realized premium, and liquidity
  state. Feature evidence only; no hypothesis is generated here.

Every table is immutable (``apex.adapters.postgres.immutable_trigger_ddl``)
and ``authority``-checked. Downgrade refuses whenever any evidence is
retained. No order, position, reservation, PAPER, or LIVE-authority column
exists anywhere in this migration.

Revision ID: 0085_options_quote_intelligence
Revises: 0084_research_replication
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0085_options_quote_intelligence"
down_revision: str | None = "0084_research_replication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_TABLES = (
    "options_quote_capability",
    "options_chain_capture_plan",
    "options_chain_capture_run",
    "options_quote_observation",
    "options_chain_snapshot",
    "options_volatility_research_feature",
)
ALL_TABLES = IMMUTABLE_TABLES

_CAPTURE_RUN_STATUSES = (
    "'COMPLETE','PARTIAL','BLOCKED_CREDENTIALS_ABSENT','BLOCKED_PROVIDER_ERROR','EMPTY'"
)
_QUOTE_STATUSES = (
    "'OBSERVED','ONE_SIDED','ZERO_LIQUIDITY','STALE','CROSSED','INVALID','UNAVAILABLE'"
)
_DATA_QUALITY_STATUSES = "'COMPLETE','PARTIAL','EMPTY','BLOCKED'"


def upgrade() -> None:
    op.create_table(
        "options_quote_capability",
        sa.Column("canonical_hash", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credentials_available", sa.Boolean(), nullable=False),
        sa.Column("requests_made", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("requests_made >= 0", name="ck_oqcap_requests_non_negative"),
        sa.CheckConstraint(
            "NOT credentials_available OR requests_made >= 0", name="ck_oqcap_requests_valid"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_oqcap_authority"),
    )
    op.create_index(
        "ix_oqcap_provider_checked", "options_quote_capability", ["provider", "checked_at"]
    )

    op.create_table(
        "options_chain_capture_plan",
        sa.Column("plan_id", sa.String(160), primary_key=True),
        sa.Column("cohort_policy_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("feed", sa.String(16), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("feed IN ('indicative','opra')", name="ck_oqplan_feed"),
        sa.CheckConstraint(
            "member_count >= 10 AND member_count <= 25", name="ck_oqplan_member_count"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_oqplan_authority"),
    )

    op.create_table(
        "options_chain_capture_run",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("options_chain_capture_plan.plan_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(f"status IN ({_CAPTURE_RUN_STATUSES})", name="ck_oqrun_status"),
        sa.CheckConstraint(
            "completed_at >= requested_at", name="ck_oqrun_completed_after_requested"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_oqrun_authority"),
    )
    op.create_index("ix_oqrun_plan", "options_chain_capture_run", ["plan_id", "requested_at"])

    op.create_table(
        "options_quote_observation",
        sa.Column("observation_id", sa.String(160), primary_key=True),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("canonical_contract_id", sa.String(160), nullable=False),
        sa.Column("expiration", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("quote_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(f"status IN ({_QUOTE_STATUSES})", name="ck_oqobs_status"),
        sa.CheckConstraint(
            "ingestion_timestamp >= quote_timestamp", name="ck_oqobs_timestamp_ordering"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_oqobs_authority"),
    )
    op.create_index(
        "ix_oqobs_underlying", "options_quote_observation", ["underlying_canonical_security_id"]
    )
    op.create_index(
        "ix_oqobs_contract_quote_time",
        "options_quote_observation",
        ["canonical_contract_id", "quote_timestamp"],
    )

    op.create_table(
        "options_chain_snapshot",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(160),
            sa.ForeignKey("options_chain_capture_plan.plan_id"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(160),
            sa.ForeignKey("options_chain_capture_run.run_id"),
            nullable=False,
        ),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("underlying_symbol", sa.String(16), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_quality_status", sa.String(20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            f"data_quality_status IN ({_DATA_QUALITY_STATUSES})", name="ck_oqsnap_quality"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_oqsnap_authority"),
    )
    op.create_index(
        "ix_oqsnap_underlying_captured",
        "options_chain_snapshot",
        ["underlying_canonical_security_id", "captured_at"],
    )

    op.create_table(
        "options_volatility_research_feature",
        sa.Column("feature_id", sa.String(160), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("options_chain_snapshot.snapshot_id"),
            nullable=False,
        ),
        sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
        sa.Column("as_of", sa.String(10), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("snapshot_id", name="uq_oqfeature_snapshot"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_oqfeature_authority"),
    )
    op.create_index(
        "ix_oqfeature_underlying_as_of",
        "options_volatility_research_feature",
        ["underlying_canonical_security_id", "as_of"],
    )

    for table in IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    checks = " OR ".join(f'EXISTS (SELECT 1 FROM "{table}")' for table in ALL_TABLES)
    op.execute(
        sa.text(
            f"DO $$ BEGIN IF {checks} THEN RAISE EXCEPTION "
            "'cannot downgrade 0085 with retained options quote intelligence evidence'; "
            "END IF; END $$"
        )
    )
    for table in reversed(IMMUTABLE_TABLES):
        op.execute(sa.text(f'DROP TRIGGER apex_immutable_{table}_trigger ON "{table}"'))
    op.drop_table("options_volatility_research_feature")
    op.drop_table("options_chain_snapshot")
    op.drop_table("options_quote_observation")
    op.drop_table("options_chain_capture_run")
    op.drop_table("options_chain_capture_plan")
    op.drop_table("options_quote_capability")
