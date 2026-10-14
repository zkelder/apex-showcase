"""Permit the bounded twenty-asset crypto history shortlist.

Revision ID: 0056_crypto_universe_quality
Revises: 0055_crypto_multi_asset
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_crypto_universe_quality"
down_revision: str | None = "0055_crypto_multi_asset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_crypto_product_observation_checkpoint",
        "crypto_product_observation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_crypto_product_observation_checkpoint",
        "crypto_product_observation",
        "checkpoint_ordinal BETWEEN 1 AND 20",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS ("
            "SELECT 1 FROM crypto_product_observation WHERE checkpoint_ordinal > 10"
            ") THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade 0056 with crypto observation checkpoints above 10'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(
        "ck_crypto_product_observation_checkpoint",
        "crypto_product_observation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_crypto_product_observation_checkpoint",
        "crypto_product_observation",
        "checkpoint_ordinal BETWEEN 1 AND 10",
    )
