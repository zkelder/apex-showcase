"""Live Market Observation Foundation v1.

Adds the durable, provider-agnostic live/intraday market-observation
substrate: ``live_market_observation_round`` (one row per bounded capture
round -- one worker tick over a bounded instrument list) and
``live_market_observation`` (one row per ``(round_id, instrument_id,
observation_type)``, e.g. one top-of-book QUOTE reading). Mirrors migration
0057 (``crypto_market_pulse``)'s refresh/checkpoint table shape and migration
0102 (``kalshi_baseline_quote``)'s narrow-columns + ``payload`` +
``canonical_hash`` + ``authority`` persistence idiom.

This is genuinely new substrate, not a rename or extension of an existing
table: every existing capture path in this codebase (CRYPTO market pulse,
CRYPTO universe, SECURITIES options evidence) is scheduled *snapshot*
capture (once/day, once/7-days, once/trading-session respectively); none is
intraday. See ``domain.live_market_observation``'s own module docstring for
the full architectural rationale, and ``application.live_market_observation``
for the capture orchestration and decision-time staleness-gate services that
use these two tables.

Grants no capital or execution authority: every row's ``authority`` is
CHECK-constrained to ``'NONE'``, restating ``domain.live_market_observation``'s
own invariant at the database level, exactly like every other evidence table
in this codebase.

``uq_live_market_observation_round_identity`` (``plan_id``, ``window_start``)
and ``uq_live_market_observation_identity`` (``round_id``, ``instrument_id``,
``observation_type``) are defensive, database-level restatements of the two
domain identity formulas (``live_observation_round_id`` and
``LiveMarketObservation.create``'s own ``observation_id`` formula) -- even a
caller that bypassed those functions and somehow computed a different id
string for the same logical round/observation could not insert a duplicate.

``ix_live_market_observation_latest`` supports the Phase D decision-time
hook's hot read path (``ObtainContemporaneousMarketObservation``: latest
retained observation for one ``(venue, instrument_id, observation_type)``,
ordered by ``captured_at``).

TRUNCATE: follows the same majority append-only-table pattern as migrations
0057/0102 (a row-level ``BEFORE UPDATE OR DELETE`` immutability trigger plus
a downgrade guard that refuses while any row is retained).

Historically self-contained per the migration-boundary rule (``tests.
security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic``, ``sqlalchemy``, and ``apex.adapters.
postgres.immutable_trigger_ddl`` -- never ``apex.domain``/``apex.
application``. It creates the two new tables only; there is no data to
backfill.

Revision ID: 0106_live_market_observation
Revises: 0105_securities_structure_perf
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0106_live_market_observation"
down_revision: str | None = "0105_securities_structure_perf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE_MARKET_OBSERVATION_TABLES = (
    "live_market_observation_round",
    "live_market_observation",
)

_VENUES = "venue IN ('CRYPTO','KALSHI','SECURITIES')"


def upgrade() -> None:
    op.create_table(
        "live_market_observation_round",
        sa.Column("round_id", sa.String(300), primary_key=True),
        sa.Column("plan_id", sa.String(300), nullable=False),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(_VENUES, name="ck_live_market_observation_round_venue"),
        sa.CheckConstraint(
            "status IN ('COMPLETED','PARTIAL','FAILED')",
            name="ck_live_market_observation_round_status",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= requested_at",
            name="ck_live_market_observation_round_completed_after_requested",
        ),
        sa.CheckConstraint(
            "length(canonical_hash) = 64", name="ck_live_market_observation_round_hash"
        ),
        sa.CheckConstraint("authority = 'NONE'", name="ck_live_market_observation_round_authority"),
        sa.UniqueConstraint(
            "plan_id", "window_start", name="uq_live_market_observation_round_identity"
        ),
    )
    op.create_index(
        "ix_live_market_observation_round_plan_id",
        "live_market_observation_round",
        ["plan_id"],
    )

    op.create_table(
        "live_market_observation",
        sa.Column("observation_id", sa.String(300), primary_key=True),
        sa.Column(
            "round_id",
            sa.String(300),
            sa.ForeignKey("live_market_observation_round.round_id"),
            nullable=False,
        ),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("instrument_id", sa.String(300), nullable=False),
        sa.Column("observation_type", sa.String(20), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_class", sa.String(20), nullable=False),
        sa.Column("quality", sa.String(10), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint(_VENUES, name="ck_live_market_observation_venue"),
        sa.CheckConstraint(
            "observation_type IN ('QUOTE','TRADE','BAR')",
            name="ck_live_market_observation_type",
        ),
        sa.CheckConstraint(
            "evidence_class IN "
            "('FIXTURE','GENUINE_PUBLIC','HISTORICAL_PUBLIC','SANDBOX','PAPER','LIVE')",
            name="ck_live_market_observation_evidence_class",
        ),
        sa.CheckConstraint(
            "quality IN ('PASSED','FAILED')", name="ck_live_market_observation_quality"
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_live_market_observation_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_live_market_observation_authority"),
        sa.UniqueConstraint(
            "round_id",
            "instrument_id",
            "observation_type",
            name="uq_live_market_observation_identity",
        ),
    )
    op.create_index("ix_live_market_observation_round_id", "live_market_observation", ["round_id"])
    op.create_index(
        "ix_live_market_observation_latest",
        "live_market_observation",
        ["venue", "instrument_id", "observation_type", "captured_at"],
    )

    for table in LIVE_MARKET_OBSERVATION_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(LIVE_MARKET_OBSERVATION_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                f"'cannot downgrade 0106 with retained {table} evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(LIVE_MARKET_OBSERVATION_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
    op.drop_table("live_market_observation")
    op.drop_table("live_market_observation_round")
