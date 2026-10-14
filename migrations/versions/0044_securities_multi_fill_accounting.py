"""Allow immutable multiple Securities PAPER fill increments per Order.

Revision ID: 0044_securities_multi_fill
Revises: 0043_external_paper_binding
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_securities_multi_fill"
down_revision: str | None = "0043_external_paper_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("securities_fill_evidence_order_id_key", "securities_fill_evidence")
    for column in (
        "increment_ordinal integer NOT NULL DEFAULT 1",
        "cumulative_quantity numeric(28,12) NOT NULL DEFAULT 0",
        "remaining_quantity numeric(28,12) NOT NULL DEFAULT 0",
        "execution_source varchar(48) NOT NULL DEFAULT 'INTERNAL_SIMULATION'",
        "source_evidence_id varchar(160)",
        "economic_identity_hash varchar(64)",
    ):
        op.execute(
            sa.text(f"ALTER TABLE securities_fill_evidence ADD COLUMN IF NOT EXISTS {column}")
        )
    op.execute(
        "UPDATE securities_fill_evidence fill SET cumulative_quantity = fill.quantity, "
        "remaining_quantity = orders.requested_quantity - fill.quantity, "
        "economic_identity_hash = fill.canonical_hash, source_evidence_id = fill.fill_id "
        "FROM securities_paper_order orders WHERE orders.order_id = fill.order_id"
    )
    op.alter_column("securities_fill_evidence", "source_evidence_id", nullable=False)
    op.alter_column("securities_fill_evidence", "economic_identity_hash", nullable=False)
    op.create_unique_constraint(
        "uq_securities_fill_increment_order_ordinal",
        "securities_fill_evidence",
        ["order_id", "increment_ordinal"],
    )
    op.create_unique_constraint(
        "uq_securities_fill_economic_identity",
        "securities_fill_evidence",
        ["economic_identity_hash"],
    )
    op.create_unique_constraint(
        "uq_securities_fill_source_evidence",
        "securities_fill_evidence",
        ["execution_source", "source_evidence_id"],
    )
    op.create_check_constraint(
        "ck_securities_fill_increment_values",
        "securities_fill_evidence",
        "increment_ordinal > 0 AND quantity > 0 AND cumulative_quantity >= quantity "
        "AND remaining_quantity >= 0",
    )
    op.create_check_constraint(
        "ck_securities_fill_execution_source",
        "securities_fill_evidence",
        "execution_source IN ('INTERNAL_SIMULATION','EXTERNAL_PAPER_VALIDATED_FILL')",
    )
    op.create_index(
        "ix_securities_fill_order_increment",
        "securities_fill_evidence",
        ["order_id", "increment_ordinal"],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM securities_fill_evidence "
            "GROUP BY order_id HAVING count(*) > 1) THEN RAISE EXCEPTION "
            "'cannot downgrade 0044 with retained multi-fill Securities evidence'; END IF; END $$"
        )
    )
    op.drop_index("ix_securities_fill_order_increment", table_name="securities_fill_evidence")
    op.drop_constraint("ck_securities_fill_execution_source", "securities_fill_evidence")
    op.drop_constraint("ck_securities_fill_increment_values", "securities_fill_evidence")
    op.drop_constraint("uq_securities_fill_source_evidence", "securities_fill_evidence")
    op.drop_constraint("uq_securities_fill_economic_identity", "securities_fill_evidence")
    op.drop_constraint("uq_securities_fill_increment_order_ordinal", "securities_fill_evidence")
    op.drop_column("securities_fill_evidence", "economic_identity_hash")
    op.drop_column("securities_fill_evidence", "source_evidence_id")
    op.drop_column("securities_fill_evidence", "execution_source")
    op.drop_column("securities_fill_evidence", "remaining_quantity")
    op.drop_column("securities_fill_evidence", "cumulative_quantity")
    op.drop_column("securities_fill_evidence", "increment_ordinal")
    op.create_unique_constraint(
        "securities_fill_evidence_order_id_key", "securities_fill_evidence", ["order_id"]
    )
