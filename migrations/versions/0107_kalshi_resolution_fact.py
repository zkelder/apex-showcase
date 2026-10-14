"""Kalshi Settlement & Resolution Evidence Maturation v1.

Adds ``kalshi_resolution_fact``: one immutable, append-only row per genuinely
observed Kalshi market resolution/settlement state -- the durable,
replay-safe evidence substrate ``domain.kalshi_resolution`` needed and did
not have. Closes the gap that module's own docstring documents in detail:
every Kalshi follow-up read already retained a raw ``settlement_result``
string with no gate on the market's own ``status``, risking a
provisional (``determined``/``disputed``/``amended``) result being trusted
as if it were the market's true, authoritative (``finalized``) outcome.

Identity is deliberately **content-derived**
(``domain.kalshi_resolution.kalshi_resolution_fact_id``), not a function of
an extrinsic key like ``decision_id`` -- see that module's own docstring for
the full rationale. This is why this table, unlike
``kalshi_baseline_quote``/``live_market_observation`` before it, carries no
defensive secondary ``UniqueConstraint`` restating its identity formula over
plain columns: every column that formula depends on
(``settlement_value_dollars``/``settlement_ts``/``close_time``) is
nullable, and PostgreSQL's own ``UNIQUE`` semantics treat ``NULL`` as
distinct from ``NULL`` -- a secondary constraint over those columns would
not actually be watertight, so this migration does not add one that only
looks like a guarantee. The primary key itself (a genuine content hash) is
the real guarantee.

No ``decision_id``/``counterfactual_decision`` foreign key: a resolution
fact is per-*market*, not per-decision -- many decisions (and many
follow-up observations, across horizons and origins) can legitimately
reference the identical Kalshi market, so this table is intentionally one
level of indirection away from any single decision, mirroring
``live_market_observation`` (migration 0106) rather than
``kalshi_baseline_quote`` (migration 0102) in this specific respect.

``ix_kalshi_resolution_fact_ticker_captured_at`` supports the one hot read
path this evidence exists to serve:
``application.kalshi_resolution.KalshiResolutionFactRepository.latest_fact``'s
point-in-time-bounded lookup (``WHERE market_ticker = ? AND captured_at <=
?  ORDER BY captured_at DESC LIMIT 1``) -- the same rigor established by the
Live Observation Point-in-Time Race Fix v1 (migration 0106's own
``ix_live_market_observation_latest``). ``ix_kalshi_resolution_fact_state``
supports the operator-observability census this milestone adds (how many
tracked markets remain unresolved, grouped by ``resolution_state``).

TRUNCATE: follows the majority, established pattern for an append-only Apex
evidence table with no incoming foreign key of its own (migrations
0094/0096/0099/0101/0106: a row-level ``BEFORE UPDATE OR DELETE``
immutability trigger plus a downgrade guard that refuses while any row is
retained).

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application``. It creates the table only; there is no
data to backfill (a wholly new, forward-only, prospective-only evidence
stream a recurring operating-cycle task populates).

Revision ID: 0107_kalshi_resolution_fact
Revises: 0106_live_market_observation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0107_kalshi_resolution_fact"
down_revision: str | None = "0106_live_market_observation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KALSHI_RESOLUTION_FACT_TABLES = ("kalshi_resolution_fact",)

_RESOLUTION_STATES = (
    "NOT_YET_CLOSED",
    "CLOSED_AWAITING_RESULT",
    "RESULT_DETERMINED",
    "DISPUTED",
    "AMENDED",
    "FINALIZED",
    "UNRECOGNIZED",
)

_EVIDENCE_CLASSES = (
    "FIXTURE",
    "GENUINE_PUBLIC",
    "HISTORICAL_PUBLIC",
    "SANDBOX",
    "PAPER",
    "LIVE",
)


def upgrade() -> None:
    op.create_table(
        "kalshi_resolution_fact",
        sa.Column("resolution_fact_id", sa.String(300), primary_key=True),
        sa.Column("market_ticker", sa.String(300), nullable=False),
        sa.Column("event_ticker", sa.String(300), nullable=True),
        sa.Column("provider_status", sa.String(30), nullable=False),
        sa.Column("resolution_state", sa.String(30), nullable=False),
        sa.Column("raw_result", sa.String(20), nullable=True),
        sa.Column("resolved_result", sa.String(20), nullable=True),
        sa.Column("settlement_value_dollars", sa.Numeric, nullable=True),
        sa.Column("settlement_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_endpoint_identity", sa.String(80), nullable=False),
        sa.Column("evidence_class", sa.String(20), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(300), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(
            "resolution_state IN (" + ",".join(f"'{value}'" for value in _RESOLUTION_STATES) + ")",
            name="ck_kalshi_resolution_fact_state",
        ),
        sa.CheckConstraint(
            "evidence_class IN (" + ",".join(f"'{value}'" for value in _EVIDENCE_CLASSES) + ")",
            name="ck_kalshi_resolution_fact_evidence_class",
        ),
        # Governing-constraint restatement at the database level, mirroring
        # domain.kalshi_resolution.KalshiMarketResolutionFact.__post_init__
        # exactly: nothing below FINALIZED may carry an authoritative
        # field, no matter what a future write path attempts.
        sa.CheckConstraint(
            "resolution_state = 'FINALIZED' OR "
            "(resolved_result IS NULL AND settlement_value_dollars IS NULL "
            "AND settlement_ts IS NULL)",
            name="ck_kalshi_resolution_fact_authoritative_only_when_finalized",
        ),
        sa.CheckConstraint(
            "resolved_result IS NULL OR resolved_result = raw_result",
            name="ck_kalshi_resolution_fact_resolved_echoes_raw",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_kalshi_resolution_fact_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_kalshi_resolution_fact_authority"),
    )
    op.create_index(
        "ix_kalshi_resolution_fact_ticker_captured_at",
        "kalshi_resolution_fact",
        ["market_ticker", "captured_at"],
    )
    op.create_index(
        "ix_kalshi_resolution_fact_state", "kalshi_resolution_fact", ["resolution_state"]
    )
    op.create_index("ix_kalshi_resolution_fact_run_id", "kalshi_resolution_fact", ["run_id"])
    for table in KALSHI_RESOLUTION_FACT_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(KALSHI_RESOLUTION_FACT_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0107 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(KALSHI_RESOLUTION_FACT_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    op.drop_table("kalshi_resolution_fact")
