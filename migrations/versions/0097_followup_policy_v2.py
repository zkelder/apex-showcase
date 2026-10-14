"""Counterfactual Crypto Follow-Up Point-in-Time Semantics v2.

Widens ``decision_followup_observation``'s own ``ck_decision_followup_
policy_version`` CHECK constraint from a literal equality
(``policy_version = 'FOLLOWUP_POLICY_V1'``) to a small, explicit allow-list
(``policy_version IN ('FOLLOWUP_POLICY_V1', 'FOLLOWUP_POLICY_V2')``) so a
genuine ``FOLLOWUP_POLICY_V2`` row can be inserted at all, and adds one new
``baseline_kind`` column so a single ``(decision, policy_version,
horizon_kind)`` can retain up to two structurally distinct measurements --
see ``domain.counterfactual_followup.FollowUpBaselineKind`` for the full
semantic contract this supports.

This is a pre-deployment correction to migration 0097 itself, not a new
migration 0098: ``FOLLOWUP_POLICY_V2`` had never been synced against the
live database when a follow-up audit of this exact milestone found that
leaving the *target horizon* always anchored to ``decision.evaluated_at`` --
while the corrected *baseline* had just been moved to the subject's own,
potentially much earlier, ``projection.generated_at`` -- reintroduced a
different, still-real defect: it silently blended pre-decision staleness
drift into what was presented as a post-decision subsequent return. Because
zero ``FOLLOWUP_POLICY_V2`` rows (and zero non-``FOLLOWUP_POLICY_V1`` rows of
any kind) were ever retained under this migration's original, narrower
shape, correcting it here changes no retained evidence and reproduces every
already-applied case byte-for-byte where one exists (there is none, since
0097 has only ever been validated against disposable ``apex_test`` chains --
see this migration's own git history for the original, narrower version this
replaces). Repository versioning doctrine reserves a new migration number
for correcting an *already-deployed* schema; this is not that.

``baseline_kind`` (``VARCHAR(20)``, ``CHECK (baseline_kind IN
('ORIGIN_EVIDENCE', 'DECISION_TIME'))``) records which instant a row's own
baseline price and target horizon are anchored to.  Every row that will ever
exist under the *original* (0096-only) schema -- i.e. every row already
retained on any database this migration upgrades -- is, structurally,
``DECISION_TIME`` (``FOLLOWUP_POLICY_V1`` never had this axis at all, and
every such row was always, bugs in *which* price it selected
notwithstanding, trying to answer "what happened after the decision"), so
the backfill for already-retained rows is unambiguous and lossless: it adds
one new fact to each row, and does not touch, encode, recompute, or rewrite
any existing column or the row's own immutable ``payload``/``canonical_hash``
(recomputing ``canonical_hash`` under today's shipped code reproduces every
already-retained hash byte-for-byte -- ``baseline_kind``/``baseline_time``
only ever widen the hash payload for ``ORIGIN_EVIDENCE``, which no
pre-existing row is or can retroactively become). A defensive CHECK
(``ck_decision_followup_kalshi_baseline_kind``) additionally restates, at
the database level, that a KALSHI row can never be anything but
``DECISION_TIME`` (Apex retains no historical Kalshi quote archive to
construct an ``ORIGIN_EVIDENCE`` reading from, and this module does not
introduce new provider history merely to manufacture one) -- mirroring
``ck_decision_followup_venue_type_agreement``'s own existing discipline.

``uq_decision_followup_identity`` widens from (``decision_id``,
``policy_version``, ``horizon_kind``) to (``decision_id``, ``policy_version``,
``horizon_kind``, ``baseline_kind``) -- required so a genuine
``ORIGIN_EVIDENCE`` row and a genuine ``DECISION_TIME`` row for the same
``(decision, policy_version, horizon_kind)`` can coexist as two distinct
retained facts rather than colliding on the same identity, while still
rejecting any genuine duplicate within one fully-specified quadruple.  Every
already-retained row keeps satisfying it trivially (all four columns are
already populated and immutable per row; widening a UNIQUE constraint can
only ever make it less restrictive for the *new* column's distinct values,
never invalidate an existing row).

Historically self-contained per the migration-boundary rule (mirrors
migration 0096's own docstring, and 0095's "freeze the exact literal
values, never import live domain code" doctrine, hardened by Migration 0095
Historical Reproducibility Hardening v1): this migration imports only
``alembic``/``sqlalchemy`` and hardcodes every literal string rather than
importing ``domain.counterfactual_followup`` constants -- a fresh
``0001 -> head`` replay must never depend on how live application code has
changed since this migration shipped, and a future policy-version or
baseline-kind string change in ``domain.counterfactual_followup`` must never
silently change what this migration, run today or replayed years from now,
actually did.

Revision ID: 0097_followup_policy_v2
Revises: 0096_counterfactual_followup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0097_followup_policy_v2"
down_revision: str | None = "0096_counterfactual_followup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "decision_followup_observation"
_POLICY_CONSTRAINT = "ck_decision_followup_policy_version"
_POLICY_V1_ONLY = "policy_version = 'FOLLOWUP_POLICY_V1'"
_POLICY_V1_AND_V2 = "policy_version IN ('FOLLOWUP_POLICY_V1', 'FOLLOWUP_POLICY_V2')"
_BASELINE_KIND_CONSTRAINT = "ck_decision_followup_baseline_kind"
_BASELINE_KIND_ALLOWED = "baseline_kind IN ('ORIGIN_EVIDENCE', 'DECISION_TIME')"
_KALSHI_BASELINE_KIND_CONSTRAINT = "ck_decision_followup_kalshi_baseline_kind"
_KALSHI_BASELINE_KIND_RULE = (
    "(venue = 'KALSHI' AND baseline_kind = 'DECISION_TIME') OR venue = 'CRYPTO'"
)
_IDENTITY_CONSTRAINT = "uq_decision_followup_identity"
_IDENTITY_COLUMNS_OLD = ("decision_id", "policy_version", "horizon_kind")
_IDENTITY_COLUMNS_NEW = ("decision_id", "policy_version", "horizon_kind", "baseline_kind")
_IMMUTABLE_TRIGGER = f"apex_immutable_{_TABLE}_trigger"


def upgrade() -> None:
    op.drop_constraint(_POLICY_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_POLICY_CONSTRAINT, _TABLE, _POLICY_V1_AND_V2)

    op.add_column(_TABLE, sa.Column("baseline_kind", sa.String(20), nullable=True))
    bind = op.get_bind()
    # Every already-retained row is protected by the same append-only
    # immutability trigger every retained Apex row carries -- a deliberate,
    # narrow, migration-only exception to add one new backfilled fact to a
    # column that did not exist when those rows were written, not a
    # precedent for application code to ever update a retained row (mirrors
    # migration 0093's own ``context_as_of`` backfill exactly). Re-enabled
    # unconditionally before this function returns.
    bind.execute(sa.text(f"ALTER TABLE {_TABLE} DISABLE TRIGGER {_IMMUTABLE_TRIGGER}"))
    try:
        bind.execute(
            sa.text(
                f"UPDATE {_TABLE} SET baseline_kind = 'DECISION_TIME' WHERE baseline_kind IS NULL"
            )
        )
    finally:
        bind.execute(sa.text(f"ALTER TABLE {_TABLE} ENABLE TRIGGER {_IMMUTABLE_TRIGGER}"))
    op.alter_column(_TABLE, "baseline_kind", nullable=False)
    op.create_check_constraint(_BASELINE_KIND_CONSTRAINT, _TABLE, _BASELINE_KIND_ALLOWED)
    op.create_check_constraint(_KALSHI_BASELINE_KIND_CONSTRAINT, _TABLE, _KALSHI_BASELINE_KIND_RULE)

    op.drop_constraint(_IDENTITY_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(_IDENTITY_CONSTRAINT, _TABLE, list(_IDENTITY_COLUMNS_NEW))


def downgrade() -> None:
    # Symmetric with migration 0096's own downgrade guard, scoped to exactly
    # what this migration widened: a genuine retained FOLLOWUP_POLICY_V2 (or
    # any other non-V1) row, or a genuine retained ORIGIN_EVIDENCE row, would
    # immediately violate the narrower constraints this downgrade is about
    # to restore, so refuse rather than either silently deleting retained
    # evidence or leaving the database in a state that cannot re-validate
    # its own CHECK constraints. A database holding only FOLLOWUP_POLICY_V1
    # / DECISION_TIME rows (or none at all) downgrades cleanly.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE} WHERE policy_version <> 'FOLLOWUP_POLICY_V1') "  # nosec B608
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0097 with retained non-V1 follow-up policy evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE} WHERE baseline_kind <> 'DECISION_TIME') "  # nosec B608
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0097 with retained ORIGIN_EVIDENCE follow-up evidence'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(_IDENTITY_CONSTRAINT, _TABLE, type_="unique")
    op.create_unique_constraint(_IDENTITY_CONSTRAINT, _TABLE, list(_IDENTITY_COLUMNS_OLD))

    op.drop_constraint(_KALSHI_BASELINE_KIND_CONSTRAINT, _TABLE, type_="check")
    op.drop_constraint(_BASELINE_KIND_CONSTRAINT, _TABLE, type_="check")
    op.drop_column(_TABLE, "baseline_kind")

    op.drop_constraint(_POLICY_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_POLICY_CONSTRAINT, _TABLE, _POLICY_V1_ONLY)
