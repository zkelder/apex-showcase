"""Persist immutable public Kalshi threshold-ladder research evidence.

Revision ID: 0013_threshold_ladder
Revises: 0012_orderbook_capture
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_THRESHOLD_LADDER_IMMUTABLE_TABLES,
    immutable_trigger_ddl,
    threshold_ladder_metadata,
)

revision: str = "0013_threshold_ladder"
down_revision: str | None = "0012_orderbook_capture"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


THRESHOLD_CAPABILITY_CHECK = (
    "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY', "
    "'PUBLIC_REST_ORDERBOOK_CAPTURE', 'PUBLIC_REST_THRESHOLD_LADDER_SCAN')"
)
PRIOR_CAPABILITY_CHECK = (
    "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY', "
    "'PUBLIC_REST_ORDERBOOK_CAPTURE')"
)


THRESHOLD_SCAN_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_threshold_ladder_scan() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'threshold ladder scan cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'selected_event_count', 'completed_at', 'state', 'relationship_count',
        'evaluation_count', 'candidate_count', 'persisting_candidate_count',
        'canonical_result_hash', 'last_error_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'selected_event_count', 'completed_at', 'state', 'relationship_count',
        'evaluation_count', 'candidate_count', 'persisting_candidate_count',
        'canonical_result_hash', 'last_error_code'
    ]) THEN
        RAISE EXCEPTION 'threshold ladder scan configuration is immutable';
    END IF;
    IF OLD.state <> 'RUNNING' THEN
        RAISE EXCEPTION 'completed threshold ladder scan is immutable';
    END IF;
    IF NEW.state = 'COMPLETED' THEN
        IF NEW.completed_at IS NULL OR NEW.canonical_result_hash IS NULL THEN
            RAISE EXCEPTION 'completed threshold ladder scan requires a result hash';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.state IN ('PARTIAL', 'FAILED', 'INTERRUPTED') THEN
        IF NEW.completed_at IS NULL OR NEW.canonical_result_hash IS NOT NULL THEN
            RAISE EXCEPTION 'incomplete threshold ladder scan cannot have a result hash';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'threshold ladder scan transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_threshold_ladder_scan_trigger
BEFORE UPDATE OR DELETE ON threshold_ladder_scan
FOR EACH ROW EXECUTE FUNCTION apex_guard_threshold_ladder_scan();
"""


def upgrade() -> None:
    """Add only immutable public threshold evidence and its guarded scan row."""

    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        THRESHOLD_CAPABILITY_CHECK,
    )
    threshold_ladder_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_THRESHOLD_LADDER_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(THRESHOLD_SCAN_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained threshold evidence would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM threshold_ladder_scan) "
            "OR EXISTS (SELECT 1 FROM threshold_market_relationship) "
            "OR EXISTS (SELECT 1 FROM threshold_bundle_evaluation) "
            "OR EXISTS (SELECT 1 FROM threshold_matched_slice) "
            "OR EXISTS (SELECT 1 FROM provider_ingestion_run "
            "WHERE capability = 'PUBLIC_REST_THRESHOLD_LADDER_SCAN') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0013 with retained threshold ladder evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_threshold_ladder_scan_trigger ON threshold_ladder_scan")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_threshold_ladder_scan()"))
    for table_name in PUBLIC_THRESHOLD_LADDER_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    threshold_ladder_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        PRIOR_CAPABILITY_CHECK,
    )
