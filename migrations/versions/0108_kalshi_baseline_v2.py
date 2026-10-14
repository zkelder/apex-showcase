"""Kalshi Baseline Quote Capture & Economic Maturation v1.

Root cause closed by this migration (bounded archaeology, not assumption --
see ``domain.kalshi_baseline_quote``'s own "Kalshi Baseline Quote Capture &
Economic Maturation v1" module comment block for the full derivation):
``kalshi_baseline_quote`` stayed at 0 retained rows in production not
because capture was ever unwired, but because migration 0102's own
``KalshiBaselineQuoteEvidence`` conflated two genuinely different concepts
under one name -- "the matched-trade execution economics of a genuine
complement/threshold match" (what it actually models, requiring a
``CANDIDATE``-state evaluation that essentially never occurs on an
efficiently market-made Kalshi book) and "the plain market quote observed
at decision time" (what a *baseline* is actually supposed to mean, and
what ``domain.counterfactual_outcome``'s own docstring names as the
explicit prerequisite this table was always meant to close). This migration
does not replace or narrow anything 0102 shipped -- it additively widens the
same table to also retain the second, more broadly applicable shape.

Schema change, minimal and additive:

* New column ``quote_kind`` (``'MATCHED_TRADE_EXECUTION'`` |
  ``'MARKET_QUOTE_OBSERVATION'``), ``NOT NULL``, with a server default of
  ``'MATCHED_TRADE_EXECUTION'`` -- the correct, non-guessed backfill value
  for any pre-existing row, since every row 0102 could ever have produced
  is definitionally that kind. Mirrors this exact same table's own
  ``authority`` column, whose ``server_default='NONE'`` from 0102 was kept
  permanently rather than dropped after backfill -- the identical
  established idiom, not a new one.
* ``ck_kalshi_baseline_quote_policy_version`` widened from a single-value
  equality check to a two-value ``IN (...)`` list, admitting the new
  ``KALSHI_BASELINE_QUOTE_POLICY_V2`` alongside the frozen, untouched
  ``..._V1`` -- the exact idiom migration 0103 already established for
  ``counterfactual_outcome_aggregate.outcome_policy_version``.
* New ``ck_kalshi_baseline_quote_quote_kind`` restates the two valid
  ``quote_kind`` values at the database level.
* New ``ck_kalshi_baseline_quote_kind_matches_policy_version`` is a genuine
  additional correctness guarantee this migration introduces (defense in
  depth, not required by any prior schema): a ``MATCHED_TRADE_EXECUTION``
  row can only ever carry policy version V1, and a
  ``MARKET_QUOTE_OBSERVATION`` row only ever V2 -- the two dimensions can
  never legitimately disagree, so the database itself now refuses a row
  where they would.
* New index on ``quote_kind`` -- the operator-observability census
  (``PostgresKalshiBaselineQuoteRepository.census()``) now groups by it on
  every read.

No existing row's ``quote_id``/``decision_id``/``payload``/``canonical_hash``
is touched. The table's row-level immutability trigger (installed by 0102,
a ``BEFORE UPDATE OR DELETE`` guard against row *mutation*) is unaffected by
and does not block this migration's schema-level ``ALTER TABLE`` statements
-- DDL is not a row UPDATE/DELETE, and production retains zero rows in this
table at the time this migration is authored regardless.

Downgrade is guarded exactly like 0103's own precedent: narrowing back to
V1-only would be unsafe (structurally impossible to have honestly produced)
if any genuine ``MARKET_QUOTE_OBSERVATION``/``KALSHI_BASELINE_QUOTE_POLICY_V2``
row has already been retained. If none exists, downgrade is a pure, safe
schema no-op reverting to 0102's exact original constraints and column set.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic`` and ``sqlalchemy`` -- like 0103, it needs
no ``apex.adapters.postgres.immutable_trigger_ddl`` since it creates no new
table and the existing trigger is untouched.

Revision ID: 0108_kalshi_baseline_v2
Revises: 0107_kalshi_resolution_fact
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0108_kalshi_baseline_v2"
down_revision: str | None = "0107_kalshi_resolution_fact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kalshi_baseline_quote"
_POLICY_CONSTRAINT = "ck_kalshi_baseline_quote_policy_version"
_KIND_CONSTRAINT = "ck_kalshi_baseline_quote_quote_kind"
_KIND_MATCHES_POLICY_CONSTRAINT = "ck_kalshi_baseline_quote_kind_matches_policy_version"
_V1 = "KALSHI_BASELINE_QUOTE_POLICY_V1"
_V2 = "KALSHI_BASELINE_QUOTE_POLICY_V2"
_MATCHED_TRADE = "MATCHED_TRADE_EXECUTION"
_MARKET_QUOTE = "MARKET_QUOTE_OBSERVATION"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("quote_kind", sa.String(30), nullable=False, server_default=_MATCHED_TRADE),
    )
    op.create_index("ix_kalshi_baseline_quote_quote_kind", _TABLE, ["quote_kind"])
    op.create_check_constraint(
        _KIND_CONSTRAINT, _TABLE, f"quote_kind IN ('{_MATCHED_TRADE}', '{_MARKET_QUOTE}')"
    )
    op.drop_constraint(_POLICY_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_POLICY_CONSTRAINT, _TABLE, f"policy_version IN ('{_V1}', '{_V2}')")
    op.create_check_constraint(
        _KIND_MATCHES_POLICY_CONSTRAINT,
        _TABLE,
        f"(quote_kind = '{_MATCHED_TRADE}' AND policy_version = '{_V1}') OR "
        f"(quote_kind = '{_MARKET_QUOTE}' AND policy_version = '{_V2}')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE} "  # nosec B608
            f"WHERE quote_kind <> '{_MATCHED_TRADE}' OR policy_version <> '{_V1}') THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade 0108 with retained MARKET_QUOTE_OBSERVATION evidence'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(_KIND_MATCHES_POLICY_CONSTRAINT, _TABLE, type_="check")
    op.drop_constraint(_POLICY_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_POLICY_CONSTRAINT, _TABLE, f"policy_version = '{_V1}'")
    op.drop_constraint(_KIND_CONSTRAINT, _TABLE, type_="check")
    op.drop_index("ix_kalshi_baseline_quote_quote_kind", table_name=_TABLE)
    op.drop_column(_TABLE, "quote_kind")
