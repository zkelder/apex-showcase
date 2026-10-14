"""Counterfactual Decision Follow-Up Observation v1.

Adds ``decision_followup_observation``: one immutable, append-only row per
genuinely observed ``(decision_id, policy_version, horizon_kind)``, each
referencing an already-retained ``counterfactual_decision`` row by
``decision_id`` (foreign key) + ``decision_canonical_hash`` (embedded in the
row's own ``payload``, for tamper-evidence -- the same "reference by id and
hash" idiom migration 0094/0095 already established).  This migration
creates no new investment judgment and grants no new authority; it only adds
a place to durably record objective future facts about decisions Apex has
already made, never a verdict on whether those decisions were good or bad
(see ``domain.counterfactual_followup`` for the full semantic contract).

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``,
hardened by Migration 0095 Historical Reproducibility Hardening v1): this
migration imports only ``alembic``, ``sqlalchemy``, and
``apex.adapters.postgres.immutable_trigger_ddl`` -- never
``apex.domain``/``apex.application`` -- so a fresh ``0001 -> head`` replay
never depends on how live application code has changed since this migration
shipped.  It creates the table only; it does not backfill any data (there is
none to backfill -- this is a wholly new, forward-only evidence stream, not
a correction to already-retained rows the way 0095 was).

Revision ID: 0096_counterfactual_followup
Revises: 0095_reason_classification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apex.adapters.postgres import immutable_trigger_ddl

revision: str = "0096_counterfactual_followup"
down_revision: str | None = "0095_reason_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECISION_FOLLOWUP_TABLES = ("decision_followup_observation",)


def upgrade() -> None:
    op.create_table(
        "decision_followup_observation",
        sa.Column("observation_id", sa.String(300), primary_key=True),
        sa.Column(
            "decision_id",
            sa.String(300),
            sa.ForeignKey("counterfactual_decision.decision_id"),
            nullable=False,
        ),
        sa.Column("venue", sa.String(20), nullable=False),
        sa.Column("disposition", sa.String(20), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("horizon_kind", sa.String(20), nullable=False),
        sa.Column("observation_type", sa.String(60), nullable=False),
        sa.Column("target_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
        sa.CheckConstraint("venue IN ('CRYPTO','KALSHI')", name="ck_decision_followup_venue"),
        sa.CheckConstraint(
            "disposition IN ('ADVANCED','CONSTRAINED','NOT_ADVANCED')",
            name="ck_decision_followup_disposition",
        ),
        sa.CheckConstraint(
            "policy_version = 'FOLLOWUP_POLICY_V1'", name="ck_decision_followup_policy_version"
        ),
        sa.CheckConstraint(
            "horizon_kind IN ('SHORT_HORIZON','STRATEGY_HORIZON')",
            name="ck_decision_followup_horizon_kind",
        ),
        sa.CheckConstraint(
            "observation_type IN "
            "('CRYPTO_REFERENCE_PRICE_MOVEMENT','KALSHI_MARKET_STATUS_AND_PRICE')",
            name="ck_decision_followup_observation_type",
        ),
        # A CRYPTO observation's type/venue must agree, and likewise KALSHI --
        # mirrors ``domain.counterfactual_followup``'s builders, which only
        # ever pair CRYPTO with CRYPTO_REFERENCE_PRICE_MOVEMENT and KALSHI
        # with KALSHI_MARKET_STATUS_AND_PRICE.  A row that violates this
        # could only come from bypassing those builders.
        sa.CheckConstraint(
            "(venue = 'CRYPTO' AND observation_type = 'CRYPTO_REFERENCE_PRICE_MOVEMENT') OR "
            "(venue = 'KALSHI' AND observation_type = 'KALSHI_MARKET_STATUS_AND_PRICE')",
            name="ck_decision_followup_venue_type_agreement",
        ),
        sa.CheckConstraint("length(canonical_hash) = 64", name="ck_decision_followup_hash"),
        sa.CheckConstraint("authority = 'NONE'", name="ck_decision_followup_authority"),
        # Defensive, DB-level restatement of the domain identity formula
        # (``decision_id`` + ``policy_version`` + ``horizon_kind`` ->
        # ``observation_id``): even a caller that bypassed
        # ``followup_observation_id`` and somehow computed a different
        # ``observation_id`` string for the same triple could not insert a
        # second row for it.
        sa.UniqueConstraint(
            "decision_id",
            "policy_version",
            "horizon_kind",
            name="uq_decision_followup_identity",
        ),
    )
    op.create_index(
        "ix_decision_followup_target_time", "decision_followup_observation", ["target_time"]
    )
    op.create_index(
        "ix_decision_followup_decision_id", "decision_followup_observation", ["decision_id"]
    )
    for table in DECISION_FOLLOWUP_TABLES:
        op.execute(sa.text(immutable_trigger_ddl(table)))


def downgrade() -> None:
    for table in reversed(DECISION_FOLLOWUP_TABLES):
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF EXISTS (SELECT 1 FROM {table}) THEN "  # nosec B608
                "RAISE EXCEPTION "
                "'cannot downgrade 0096 with retained follow-up observation evidence'; "
                "END IF; END $$"
            )
        )
    for table in reversed(DECISION_FOLLOWUP_TABLES):
        op.execute(sa.text(f"DROP TRIGGER apex_immutable_{table}_trigger ON {table}"))
        op.drop_table(table)
