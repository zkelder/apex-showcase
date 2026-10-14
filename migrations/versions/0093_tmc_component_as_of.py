"""Scope Total Market context component identity to its own as-of date.

Recurring Operating Health & Unattended Evidence Closure v1: root-caused a
genuine ``TOTAL_MARKET_CONTEXT_REFRESH`` failure reproduced against the real
dev database. ``ContextComponent.freshness_seconds`` (and therefore its
``canonical_hash``) is *inherently* as-of-relative -- ``domain.total_market_
context.build_context_snapshot`` already enforces
``freshness_seconds == (snapshot.created_at - available_at)`` as an
invariant -- but ``total_market_context_component`` carried only a
``UNIQUE(component_type, source_observation_id)`` constraint with no as-of
scope at all. Once a component was ever retained for one exact source bar,
re-observing that identical bar on any later day (the source batch not yet
refreshed, or a historical-replay snapshot revisited) always violated this
constraint -- a genuine persistence/query defect, not a batch-readiness
problem. ``adapters.total_market_context_sources.RetainedTotalMarketContext
Source`` already had its ``component_id`` fixed to include the as-of date
(mirroring the sibling ``adapters.historical_total_market_context_sources.
HistoricalTotalMarketContextSource``, which already did this correctly);
this migration makes the schema's own constraint agree, by adding an
explicit ``context_as_of`` date column and widening the unique constraint
to include it.

Existing retained rows are backfilled from their own already-persisted
``payload`` (``available_at`` + ``freshness_seconds`` reconstructs the
original as-of instant exactly, per the invariant above) rather than
guessed -- zero rows are dropped or reinterpreted.

Revision ID: 0093_tmc_component_as_of
Revises: 0092_portfolio_mandate
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision: str = "0093_tmc_component_as_of"
down_revision: str | None = "0092_portfolio_mandate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CONSTRAINT = "uq_total_market_component_source"
_NEW_CONSTRAINT = "uq_total_market_component_source_as_of"


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        item["name"] for item in sa.inspect(bind).get_columns("total_market_context_component")
    }
    if "context_as_of" not in columns:
        op.add_column(
            "total_market_context_component",
            sa.Column("context_as_of", sa.Date(), nullable=True),
        )
    rows = bind.execute(
        sa.text(
            'SELECT component_id, payload FROM "total_market_context_component" '
            "WHERE context_as_of IS NULL"
        )
    ).fetchall()
    if rows:
        # The existing rows this backfills are protected by the same
        # append-only immutability trigger every retained Apex row carries
        # (``apex_reject_mutation``) -- a deliberate, narrow, migration-only
        # exception to correct a genuine schema defect, not a precedent for
        # application code to ever update a retained row. Re-enabled
        # unconditionally before this function returns, including on the
        # backfill-value path below.
        bind.execute(
            sa.text(
                "ALTER TABLE total_market_context_component "
                "DISABLE TRIGGER apex_immutable_total_market_context_component_trigger"
            )
        )
        try:
            for component_id, payload in rows:
                value = json.loads(payload)
                parsed_available_at = datetime.fromisoformat(
                    str(value["available_at"]).replace("Z", "+00:00")
                )
                as_of_instant = parsed_available_at + timedelta(
                    seconds=int(value["freshness_seconds"])
                )
                bind.execute(
                    sa.text(
                        'UPDATE "total_market_context_component" SET context_as_of = '
                        ":context_as_of WHERE component_id = :component_id"
                    ),
                    {"context_as_of": as_of_instant.date(), "component_id": component_id},
                )
        finally:
            bind.execute(
                sa.text(
                    "ALTER TABLE total_market_context_component "
                    "ENABLE TRIGGER apex_immutable_total_market_context_component_trigger"
                )
            )
    op.alter_column("total_market_context_component", "context_as_of", nullable=False)
    constraints = {
        item["name"]
        for item in sa.inspect(bind).get_unique_constraints("total_market_context_component")
    }
    if _OLD_CONSTRAINT in constraints:
        op.drop_constraint(_OLD_CONSTRAINT, "total_market_context_component", type_="unique")
    if _NEW_CONSTRAINT not in constraints:
        op.create_unique_constraint(
            _NEW_CONSTRAINT,
            "total_market_context_component",
            ["component_type", "source_observation_id", "context_as_of"],
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM total_market_context_component) "
            "THEN RAISE EXCEPTION 'cannot downgrade 0093 with retained context component "
            "evidence'; END IF; END $$"
        )
    )
    op.drop_constraint(_NEW_CONSTRAINT, "total_market_context_component", type_="unique")
    op.create_unique_constraint(
        _OLD_CONSTRAINT,
        "total_market_context_component",
        ["component_type", "source_observation_id"],
    )
    op.drop_column("total_market_context_component", "context_as_of")
