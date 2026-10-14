"""Persist immutable exact-event multi-observation history evidence.

Revision ID: 0009_event_observation_history
Revises: 0008_persisted_scan_comparison
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_HISTORY_IMMUTABLE_TABLES,
    history_metadata,
    immutable_trigger_ddl,
)

revision: str = "0009_event_observation_history"
down_revision: str | None = "0008_persisted_scan_comparison"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


HISTORY_LINEAGE_GUARD_DDL = r"""
CREATE OR REPLACE FUNCTION apex_guard_event_history_analysis() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM provider_ingestion_run
         WHERE ingestion_run_id = NEW.first_ingestion_run_id
           AND provider = NEW.provider
           AND capability = 'PUBLIC_REST_MARKET_DISCOVERY'
           AND state = 'COMPLETED'
    ) OR NOT EXISTS (
        SELECT 1 FROM provider_ingestion_run
         WHERE ingestion_run_id = NEW.last_ingestion_run_id
           AND provider = NEW.provider
           AND capability = 'PUBLIC_REST_MARKET_DISCOVERY'
           AND state = 'COMPLETED'
    ) THEN
        RAISE EXCEPTION 'event history analysis requires completed exact-provider market runs';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION apex_guard_event_history_observation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    analysis_provider text;
    analysis_ticker text;
BEGIN
    SELECT provider, provider_event_ticker
      INTO analysis_provider, analysis_ticker
      FROM event_history_analysis
     WHERE analysis_id = NEW.analysis_id;
    IF analysis_provider IS NULL THEN
        RAISE EXCEPTION 'event history observation requires retained analysis';
    END IF;
    IF NEW.state = 'ABSENT' THEN
        IF NEW.event_snapshot_id IS NOT NULL OR NEW.consistency_evaluation_id IS NOT NULL
           OR NEW.evaluation_state IS NOT NULL OR NEW.rule_evidence_hash IS NOT NULL
           OR NEW.settlement_source_hash IS NOT NULL
           OR NEW.relationship_evidence_hash IS NOT NULL THEN
            RAISE EXCEPTION 'absent event history observation cannot carry event evidence';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.event_snapshot_id IS NULL OR NEW.consistency_evaluation_id IS NULL
       OR NEW.evaluation_state IS NULL OR NEW.rule_evidence_hash IS NULL
       OR NEW.settlement_source_hash IS NULL OR NEW.relationship_evidence_hash IS NULL THEN
        RAISE EXCEPTION 'present event history observation requires exact event evidence';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM normalized_event_snapshot AS event_snapshot
          JOIN consistency_evaluation AS evaluation
            ON evaluation.event_snapshot_id = event_snapshot.event_snapshot_id
          JOIN event_market_relationship AS relationship
            ON relationship.event_snapshot_id = event_snapshot.event_snapshot_id
          JOIN normalized_market_snapshot AS market_snapshot
            ON market_snapshot.snapshot_id = relationship.market_snapshot_id
         WHERE event_snapshot.event_snapshot_id = NEW.event_snapshot_id
           AND evaluation.evaluation_id = NEW.consistency_evaluation_id
           AND event_snapshot.provider = analysis_provider
           AND event_snapshot.provider_event_ticker = analysis_ticker
           AND market_snapshot.ingestion_run_id = NEW.ingestion_run_id
    ) THEN
        RAISE EXCEPTION 'event history observation requires exact event and market-run lineage';
    END IF;
    IF NEW.comparison_transition_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
          FROM event_consistency_transition AS transition
          JOIN scan_comparison AS comparison
            ON comparison.comparison_id = transition.comparison_id
         WHERE transition.transition_id = NEW.comparison_transition_id
           AND transition.provider = analysis_provider
           AND transition.provider_event_ticker = analysis_ticker
           AND comparison.current_ingestion_run_id = NEW.ingestion_run_id
    ) THEN
        RAISE EXCEPTION 'event history transition must match exact provider event and current run';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER apex_guard_event_history_analysis_trigger
BEFORE INSERT ON event_history_analysis
FOR EACH ROW EXECUTE FUNCTION apex_guard_event_history_analysis();

CREATE TRIGGER apex_guard_event_history_observation_trigger
BEFORE INSERT ON event_history_observation
FOR EACH ROW EXECUTE FUNCTION apex_guard_event_history_observation();
"""


def upgrade() -> None:
    """Add only immutable public-evidence history projections."""

    history_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_HISTORY_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))
    op.execute(sa.text(HISTORY_LINEAGE_GUARD_DDL))


def downgrade() -> None:
    """Refuse before DDL whenever retained exact-event history would be lost."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM event_history_analysis) "
            "OR EXISTS (SELECT 1 FROM event_history_observation) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0009 with retained event observation history evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DROP TRIGGER apex_guard_event_history_observation_trigger ON event_history_observation"
        )
    )
    op.execute(
        sa.text("DROP TRIGGER apex_guard_event_history_analysis_trigger ON event_history_analysis")
    )
    op.execute(sa.text("DROP FUNCTION apex_guard_event_history_observation()"))
    op.execute(sa.text("DROP FUNCTION apex_guard_event_history_analysis()"))
    for table_name in PUBLIC_HISTORY_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    history_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
