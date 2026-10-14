"""Persist immutable temporal public scan comparison evidence.

Revision ID: 0008_persisted_scan_comparison
Revises: 0007_public_event_consistency
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import (
    PUBLIC_COMPARISON_IMMUTABLE_TABLES,
    comparison_metadata,
    immutable_trigger_ddl,
)

revision: str = "0008_persisted_scan_comparison"
down_revision: str | None = "0007_public_event_consistency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add only immutable read-only temporal comparison evidence."""

    # 0006 treated a scan configuration as a run identity.  Temporal evidence
    # needs repeated observations under that same bounded configuration, so the
    # run key remains unique while the configuration hash becomes provenance.
    op.drop_constraint(
        "provider_ingestion_run_canonical_configuration_hash_key",
        "provider_ingestion_run",
        type_="unique",
    )
    comparison_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for table_name in PUBLIC_COMPARISON_IMMUTABLE_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table_name)))


def downgrade() -> None:
    """Never silently discard retained comparison evidence."""

    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM scan_comparison) "
            "OR EXISTS (SELECT 1 FROM market_snapshot_delta) "
            "OR EXISTS (SELECT 1 FROM event_consistency_transition) "
            "OR EXISTS (SELECT canonical_configuration_hash FROM provider_ingestion_run "
            "GROUP BY canonical_configuration_hash HAVING count(*) > 1) "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0008 with retained persisted scan comparison evidence "
            "or repeated scan configuration evidence'; "
            "END IF; END $$"
        )
    )
    for table_name in PUBLIC_COMPARISON_IMMUTABLE_TABLES:
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table_name}_trigger ON {table_name}"))
    comparison_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
    op.create_unique_constraint(
        "provider_ingestion_run_canonical_configuration_hash_key",
        "provider_ingestion_run",
        ["canonical_configuration_hash"],
    )
