"""Counterfactual Outcome Policy V2 Constraint Widening.

Migration 0101 added ``ck_counterfactual_outcome_policy_version`` as a
single-value equality check (``outcome_policy_version =
'COUNTERFACTUAL_OUTCOME_POLICY_V1'``) because only V1 existed at the time it
was authored -- unlike this same table's ``classifier_version``/
``followup_policy_version`` columns, whose own constraints were already
written as two-value ``IN (...)`` lists in 0101, because
``REASON_CLASSIFIER_V2``/``FOLLOWUP_POLICY_V2`` already existed by then.
``outcome_policy_version`` itself did not yet have a second value to
anticipate.

Kalshi Decision-Linked Baseline Quote Evidence v1 adds
``COUNTERFACTUAL_OUTCOME_POLICY_V2`` (``domain.counterfactual_outcome``) as
an additive sibling to the frozen V1 -- so this migration widens exactly
that one column's CHECK constraint to a two-value ``IN (...)`` list,
matching the identical idiom its sibling columns already use. It changes no
data, adds no column, and touches no other table. ``counterfactual_outcome_
aggregate``'s own row-level immutability trigger (installed in 0101) is
unaffected: a ``BEFORE UPDATE OR DELETE`` trigger governs row mutation, not
schema DDL, so widening a CHECK constraint on the table is unrelated to
(and does not weaken) that guarantee -- no already-retained row's content
is read, altered, or reinterpreted by this migration.

Downgrade is guarded: narrowing the constraint back to V1-only would be
unsafe if any genuine V2 row has already been retained (the narrower
constraint could never have accepted it, so downgrading with such a row
present would leave the database in a state ``0101``'s own schema could not
have produced honestly). If no V2 row exists, downgrade is a pure, safe
schema no-op reverting to 0101's exact original constraint text.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.
test_migrations_never_import_mutable_domain_or_application_modules``): this
migration imports only ``alembic`` and ``sqlalchemy`` -- it does not even
need ``apex.adapters.postgres.immutable_trigger_ddl``, since it creates no
new table.

Revision ID: 0103_counterfactual_outcome_v2
Revises: 0102_kalshi_baseline_quote
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103_counterfactual_outcome_v2"
down_revision: str | None = "0102_kalshi_baseline_quote"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_counterfactual_outcome_policy_version"
_TABLE_NAME = "counterfactual_outcome_aggregate"
_V1 = "COUNTERFACTUAL_OUTCOME_POLICY_V1"
_V2 = "COUNTERFACTUAL_OUTCOME_POLICY_V2"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        f"outcome_policy_version IN ('{_V1}', '{_V2}')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE_NAME} "  # nosec B608
            f"WHERE outcome_policy_version <> '{_V1}') THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade 0103 with retained COUNTERFACTUAL_OUTCOME_POLICY_V2 evidence'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        f"outcome_policy_version = '{_V1}'",
    )
