"""Counterfactual Outcome Evidence Specificity v3 -- policy constraint widening.

``domain.counterfactual_outcome`` gains ``COUNTERFACTUAL_OUTCOME_POLICY_V3``
(Counterfactual Outcome Evidence Specificity v3): a CRYPTO-only, additive
sibling of the frozen V1 and V2 policies that reports peer-relative,
canonically-attributed, independent-economic-window outcome evidence over
``FOLLOWUP_POLICY_V3`` observations.  Every V3 aggregate is a new,
additional row in the existing ``counterfactual_outcome_aggregate`` table
(its ``outcome_policy_version`` is already a cohort-partition dimension, so
a V3 cohort never shares a ``cohort_id`` with a V1/V2 one), and its extra
specificity facts live inside the existing schemaless ``payload`` column as
one nested block that reads back as ``NULL``/absent on every already-
retained V1/V2 row.

This migration therefore does exactly one thing, mirroring migration
0103's identical treatment when V2 was added beside V1: it widens the
single-column CHECK constraint ``ck_counterfactual_outcome_policy_version``
from the two-value ``IN (...)`` list 0103 left it at to a three-value one.
No column is added, no data is read, altered, or reinterpreted, no other
table is touched, and the table's own row-level append-only immutability
trigger (installed in 0101) is unaffected -- a ``BEFORE UPDATE OR DELETE``
trigger governs row mutation, not schema DDL.

Downgrade is guarded, symmetric with 0103 and 0113: a retained genuine
``COUNTERFACTUAL_OUTCOME_POLICY_V3`` aggregate would immediately violate
the narrower constraint this downgrade restores, so it refuses rather than
either silently deleting retained evidence or leaving the database unable
to re-validate its own CHECK constraint.  A database holding only V1/V2
aggregates (or none) downgrades cleanly to 0103's exact original constraint
text.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.test_migrations_never_import_
mutable_domain_or_application_modules`` and migration 0095's "freeze the
exact literal values, never import live domain code" doctrine): this
migration imports only ``alembic``/``sqlalchemy`` and hardcodes every
literal rather than importing ``domain.counterfactual_outcome``.

Revision ID: 0114_outcome_specificity_v3
Revises: 0113_followup_policy_v3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114_outcome_specificity_v3"
down_revision: str | None = "0113_followup_policy_v3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "ck_counterfactual_outcome_policy_version"
_TABLE_NAME = "counterfactual_outcome_aggregate"
_V1 = "COUNTERFACTUAL_OUTCOME_POLICY_V1"
_V2 = "COUNTERFACTUAL_OUTCOME_POLICY_V2"
_V3 = "COUNTERFACTUAL_OUTCOME_POLICY_V3"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        f"outcome_policy_version IN ('{_V1}', '{_V2}', '{_V3}')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE_NAME} "  # nosec B608
            f"WHERE outcome_policy_version = '{_V3}') THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade 0114 with retained COUNTERFACTUAL_OUTCOME_POLICY_V3 evidence'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(_CONSTRAINT_NAME, _TABLE_NAME, type_="check")
    op.create_check_constraint(
        _CONSTRAINT_NAME,
        _TABLE_NAME,
        f"outcome_policy_version IN ('{_V1}', '{_V2}')",
    )
