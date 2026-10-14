"""Persist bounded, foreground public orderbook capture sessions.

Revision ID: 0012_orderbook_capture
Revises: 0011_orderbook_history
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_ORDERBOOK_CAPTURE_IMMUTABLE_TABLES,
    capture_metadata,
    immutable_trigger_ddl,
)

revision: str = "0012_orderbook_capture"
down_revision: str | None = "0011_orderbook_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CAPTURE_RAW_ENDPOINT_CHECK = (
    "endpoint_identity IN ('KALSHI_GET_MARKETS', 'KALSHI_GET_EVENTS', "
    "'KALSHI_GET_MARKET_ORDERBOOK', 'KALSHI_GET_SERIES', "
    "'KALSHI_GET_SERIES_FEE_CHANGES', 'KALSHI_GET_MARKET')"
)
PRIOR_RAW_ENDPOINT_CHECK = (
    "endpoint_identity IN ('KALSHI_GET_MARKETS', 'KALSHI_GET_EVENTS', "
    "'KALSHI_GET_MARKET_ORDERBOOK', 'KALSHI_GET_SERIES', "
    "'KALSHI_GET_SERIES_FEE_CHANGES')"
)
CAPTURE_CAPABILITY_CHECK = (
    "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY', "
    "'PUBLIC_REST_ORDERBOOK_CAPTURE')"
)
PRIOR_CAPABILITY_CHECK = (
    "capability IN ('PUBLIC_REST_MARKET_DISCOVERY', 'PUBLIC_REST_EVENT_DISCOVERY')"
)
PRIOR_ORDERBOOK_UNIQUE = "market_orderbook_snapshot_ingestion_run_id_market_snapshot__key"
CAPTURE_ORDERBOOK_UNIQUE = "uq_capture_orderbook_raw_observation"


CAPTURE_SESSION_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_orderbook_capture_session() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'orderbook capture session cannot be deleted';
    END IF;
    IF (to_jsonb(OLD) - ARRAY[
        'state', 'completed_at', 'attempted_observation_count',
        'successful_observation_count', 'failed_observation_count',
        'first_successful_observed_at', 'last_successful_observed_at',
        'history_analysis_id', 'canonical_result_hash', 'last_error_code'
    ]) <> (to_jsonb(NEW) - ARRAY[
        'state', 'completed_at', 'attempted_observation_count',
        'successful_observation_count', 'failed_observation_count',
        'first_successful_observed_at', 'last_successful_observed_at',
        'history_analysis_id', 'canonical_result_hash', 'last_error_code'
    ]) THEN
        RAISE EXCEPTION 'orderbook capture session configuration is immutable';
    END IF;
    IF OLD.state = 'CREATED' THEN
        IF NEW.state <> 'RUNNING' OR NEW.completed_at IS NOT NULL
           OR NEW.attempted_observation_count <> 0
           OR NEW.successful_observation_count <> 0
           OR NEW.failed_observation_count <> 0 THEN
            RAISE EXCEPTION 'orderbook capture session must begin running without observations';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'RUNNING' AND NEW.state = 'RUNNING' THEN
        IF NEW.completed_at IS NOT NULL OR NEW.history_analysis_id IS NOT NULL
           OR NEW.canonical_result_hash IS NOT NULL
           OR NEW.attempted_observation_count < OLD.attempted_observation_count
           OR NEW.successful_observation_count < OLD.successful_observation_count
           OR NEW.failed_observation_count < OLD.failed_observation_count THEN
            RAISE EXCEPTION 'orderbook capture running update is invalid';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'RUNNING' AND NEW.state = 'COMPLETED' THEN
        IF NEW.completed_at IS NULL OR NEW.history_analysis_id IS NULL
           OR NEW.canonical_result_hash IS NULL
           OR NEW.successful_observation_count < 3 THEN
            RAISE EXCEPTION 'completed orderbook capture requires durable history';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state = 'RUNNING' AND NEW.state IN ('FAILED', 'INTERRUPTED') THEN
        IF NEW.completed_at IS NULL OR NEW.history_analysis_id IS NOT NULL
           OR NEW.canonical_result_hash IS NOT NULL THEN
            RAISE EXCEPTION 'failed or interrupted orderbook capture cannot be complete';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'orderbook capture session transition is invalid';
END;
$$;

CREATE TRIGGER apex_guard_orderbook_capture_session_trigger
BEFORE UPDATE OR DELETE ON orderbook_capture_session
FOR EACH ROW EXECUTE FUNCTION apex_guard_orderbook_capture_session();
"""


def upgrade() -> None:
    """Add only public-evidence session and append-only observation records."""

    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        CAPTURE_CAPABILITY_CHECK,
    )
    op.drop_constraint("ck_raw_provider_response_endpoint", "raw_provider_response", type_="check")
    op.create_check_constraint(
        "ck_raw_provider_response_endpoint", "raw_provider_response", CAPTURE_RAW_ENDPOINT_CHECK
    )
    op.drop_constraint(PRIOR_ORDERBOOK_UNIQUE, "market_orderbook_snapshot", type_="unique")
    op.create_unique_constraint(
        CAPTURE_ORDERBOOK_UNIQUE,
        "market_orderbook_snapshot",
        ["ingestion_run_id", "raw_response_id", "maximum_depth"],
    )
    capture_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_ORDERBOOK_CAPTURE_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(CAPTURE_SESSION_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained capture evidence would be discarded."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM orderbook_capture_session) "
            "OR EXISTS (SELECT 1 FROM orderbook_capture_observation) "
            "OR EXISTS (SELECT 1 FROM raw_provider_response "
            "WHERE endpoint_identity = 'KALSHI_GET_MARKET') "
            "OR EXISTS (SELECT 1 FROM provider_ingestion_run "
            "WHERE capability = 'PUBLIC_REST_ORDERBOOK_CAPTURE') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0012 with retained bounded orderbook capture evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_orderbook_capture_session_trigger ON orderbook_capture_session"
        )
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_orderbook_capture_session()"))
    for table_name in PUBLIC_ORDERBOOK_CAPTURE_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    capture_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.drop_constraint(CAPTURE_ORDERBOOK_UNIQUE, "market_orderbook_snapshot", type_="unique")
    op.create_unique_constraint(
        PRIOR_ORDERBOOK_UNIQUE,
        "market_orderbook_snapshot",
        ["ingestion_run_id", "market_snapshot_id", "maximum_depth"],
    )
    op.drop_constraint("ck_raw_provider_response_endpoint", "raw_provider_response", type_="check")
    op.create_check_constraint(
        "ck_raw_provider_response_endpoint", "raw_provider_response", PRIOR_RAW_ENDPOINT_CHECK
    )
    op.drop_constraint(
        "ck_provider_ingestion_run_capability", "provider_ingestion_run", type_="check"
    )
    op.create_check_constraint(
        "ck_provider_ingestion_run_capability",
        "provider_ingestion_run",
        PRIOR_CAPABILITY_CHECK,
    )
