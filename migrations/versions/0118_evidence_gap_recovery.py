"""Historical Evidence Gap Recovery v1: provenance and gap ledger.

Adds three immutable, append-only tables (see ``domain.evidence_recovery``):

* ``evidence_recovery_run`` -- one bounded recovery attempt; its path is always
  ``RECOVERY_BACKFILL`` and it can only be a recoverable family.
* ``evidence_coverage_gap`` -- append-only gap events (``OPEN``,
  ``RECOVERED``, ``PARTIALLY_RECOVERED``, ``IRRECOVERABLE``, ``CONFLICTED``).
  A live-only family may only ever be ``IRRECOVERABLE``.
* ``evidence_vintage`` -- sidecar saying how/when a retained fact was acquired.
  The database refuses a ``RECOVERY_BACKFILL`` vintage for any live-only family
  and for any vintage without a recovery run.

Additive only. No existing row or column changes, and no historical row is
rewritten to populate the sidecar (a fact without a vintage is legacy/unknown).

Revision ID: 0118_evidence_gap_recovery
Revises: 0117_trial_outcome_assessment
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0118_evidence_gap_recovery"
down_revision: str | None = "0117_trial_outcome_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN = "evidence_recovery_run"
GAP = "evidence_coverage_gap"
VINTAGE = "evidence_vintage"
TABLES = (RUN, GAP, VINTAGE)

_RECOVERABLE = "('CRYPTO_DAILY_CANDLE','SECURITIES_DAILY_BAR')"
_FAMILIES = (
    "('CRYPTO_DAILY_CANDLE','SECURITIES_DAILY_BAR','OPTIONS_CHAIN_SESSION',"
    "'KALSHI_REFRESH_DATE','LIVE_OBSERVATION_WINDOW','CRYPTO_ORDER_BOOK','PAPER_EXECUTION_FACT')"
)


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=f"ck_evidence_recovery_{name}")


def upgrade() -> None:
    op.create_table(
        RUN,
        sa.Column("run_id", sa.String(300), primary_key=True),
        sa.Column("evidence_family", sa.String(40), nullable=False),
        sa.Column("acquisition_path", sa.String(24), nullable=False),
        sa.Column("cycle_key", sa.String(300), nullable=False),
        sa.Column("scope_start", sa.String(40), nullable=False),
        sa.Column("scope_end", sa.String(40), nullable=False),
        sa.Column("gap_keys", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("request_ceiling", sa.Integer, nullable=False),
        sa.Column("provider_requests", sa.Integer, nullable=False),
        sa.Column("gaps_examined", sa.Integer, nullable=False),
        sa.Column("gaps_recovered", sa.Integer, nullable=False),
        sa.Column("gaps_partial", sa.Integer, nullable=False),
        sa.Column("gaps_irrecoverable", sa.Integer, nullable=False),
        sa.Column("gaps_conflicted", sa.Integer, nullable=False),
        sa.Column("reason_codes", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        _check(f"evidence_family IN {_RECOVERABLE}", "run_family_recoverable"),
        _check("acquisition_path = 'RECOVERY_BACKFILL'", "run_path"),
        _check(
            "status IN ('COMPLETED','PARTIAL','NOTHING_TO_DO','BLOCKED_CREDENTIALS',"
            "'BUDGET_EXHAUSTED','FAILED')",
            "run_status",
        ),
        _check("request_ceiling >= 0 AND provider_requests >= 0", "run_counts"),
        _check("provider_requests <= request_ceiling", "run_request_ceiling"),
        _check(
            "gaps_recovered >= 0 AND gaps_partial >= 0 AND gaps_irrecoverable >= 0 "
            "AND gaps_conflicted >= 0",
            "run_gap_counts",
        ),
        _check("length(canonical_hash) = 64", "run_hash"),
        _check("authority = 'NONE'", "run_authority"),
    )
    op.create_table(
        GAP,
        sa.Column("event_id", sa.String(300), primary_key=True),
        sa.Column("gap_key", sa.String(300), nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("evidence_family", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(300), nullable=False),
        sa.Column("interval_start", sa.String(40), nullable=False),
        sa.Column("interval_end", sa.String(40), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("reason_codes", sa.Text(), nullable=False),
        sa.Column("recovery_run_id", sa.String(300), sa.ForeignKey(f"{RUN}.run_id"), nullable=True),
        sa.Column("recovered_evidence_ids", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint("gap_key", "ordinal", name="uq_evidence_coverage_gap_key_ordinal"),
        _check(f"evidence_family IN {_FAMILIES}", "gap_family"),
        _check(
            "disposition IN ('OPEN','RECOVERED','PARTIALLY_RECOVERED',"
            "'IRRECOVERABLE','CONFLICTED')",
            "gap_disposition",
        ),
        _check("ordinal >= 1", "gap_ordinal"),
        _check("interval_end >= interval_start", "gap_interval"),
        # A gap's first event is detection: OPEN, IRRECOVERABLE for a live-only
        # family, or CONFLICTED for a provider revision found by a run.
        _check(
            "ordinal > 1 OR disposition IN ('OPEN','IRRECOVERABLE','CONFLICTED')",
            "gap_first_event",
        ),
        # Live-only evidence can never be recovered, partially or otherwise.
        _check(
            f"evidence_family IN {_RECOVERABLE} OR disposition = 'IRRECOVERABLE'",
            "gap_live_only_irrecoverable",
        ),
        _check(
            "disposition <> 'IRRECOVERABLE' OR reason_codes <> '[]'",
            "gap_irrecoverable_reason",
        ),
        # Any resolution after detection must name the recovery run that did it.
        _check(
            "disposition = 'OPEN' OR ordinal = 1 OR recovery_run_id IS NOT NULL",
            "gap_resolution_run",
        ),
        _check("length(canonical_hash) = 64", "gap_hash"),
        _check("authority = 'NONE'", "gap_authority"),
    )
    op.create_table(
        VINTAGE,
        sa.Column("vintage_id", sa.String(300), primary_key=True),
        sa.Column("evidence_family", sa.String(40), nullable=False),
        sa.Column("evidence_id", sa.String(300), nullable=False),
        sa.Column("market_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acquisition_path", sa.String(24), nullable=False),
        sa.Column("recovery_run_id", sa.String(300), sa.ForeignKey(f"{RUN}.run_id"), nullable=True),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.UniqueConstraint(
            "evidence_family",
            "evidence_id",
            "acquisition_path",
            name="uq_evidence_vintage_identity",
        ),
        _check(f"evidence_family IN {_FAMILIES}", "vintage_family"),
        _check(
            "acquisition_path IN ('SCHEDULED_LIVE','RECOVERY_BACKFILL','OPERATOR_IMPORT')",
            "vintage_path",
        ),
        # Recovered evidence: only recoverable families, always tied to a run.
        _check(
            f"acquisition_path <> 'RECOVERY_BACKFILL' OR "
            f"(evidence_family IN {_RECOVERABLE} AND recovery_run_id IS NOT NULL)",
            "vintage_recovered_shape",
        ),
        # A live-only family can only ever have contemporaneous provenance.
        _check(
            f"evidence_family IN {_RECOVERABLE} OR acquisition_path = 'SCHEDULED_LIVE'",
            "vintage_live_only_family",
        ),
        _check("acquired_at >= market_time", "vintage_acquired_after_market"),
        _check(
            "available_at IS NULL OR acquired_at >= available_at",
            "vintage_acquired_after_available",
        ),
        _check("length(canonical_hash) = 64", "vintage_hash"),
        _check("authority = 'NONE'", "vintage_authority"),
    )
    op.create_index(
        "ix_evidence_coverage_gap_family_start", GAP, ["evidence_family", "interval_start"]
    )
    op.create_index("ix_evidence_vintage_run", VINTAGE, ["recovery_run_id"])
    for table in TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM {table}) THEN
                        RAISE EXCEPTION 'cannot downgrade 0118 with retained {table} evidence';
                    END IF;
                END
                $$;
                """  # nosec B608
            )
        )
    for table in TABLES:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS apex_immutable_{table}_trigger ON {table}"))
    op.drop_index("ix_evidence_vintage_run", table_name=VINTAGE)
    op.drop_index("ix_evidence_coverage_gap_family_start", table_name=GAP)
    for table in reversed(TABLES):
        op.drop_table(table)
