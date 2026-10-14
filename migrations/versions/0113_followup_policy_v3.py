"""Counterfactual Forward Outcome Temporal Integrity v1.

Additive only.  Makes ``FOLLOWUP_POLICY_V3`` -- a CRYPTO-only follow-up
policy version whose forward economic window starts at the first daily-close
boundary at or *after* its policy anchor, and whose every endpoint lookup is
bounded by a declared ``measurement_as_of`` -- insertable at all, and pins
its two structural invariants at the database rather than only in
application code.  See ``domain.counterfactual_followup.FOLLOWUP_POLICY_V3``
for the two already-deployed ``FOLLOWUP_POLICY_V2`` defects it corrects and
why correcting them earns a new version number instead of an in-place edit.

**No backfill.  No mutation of any already-retained row.  No destructive
rewrite.**  Unlike migration 0097 (which had to backfill ``baseline_kind``
onto every existing row and therefore had to temporarily disable the
append-only immutability trigger), nothing here touches retained data at
all: the one new column is nullable and stays ``NULL`` on every existing
row, permanently and correctly, so the trigger is never disabled and no
``UPDATE`` is ever issued.  Every already-retained ``FOLLOWUP_POLICY_V1``
and ``FOLLOWUP_POLICY_V2`` row -- and every research artifact already
descended from one -- is left exactly as it is, including its
``canonical_hash``, which today's shipped code still reproduces byte-for-byte
(``FOLLOWUP_POLICY_V3``'s six new hashed facts are appended as one block
gated on ``measurement_as_of``, which no pre-existing row has or can acquire;
see ``followup_observation_canonical_hash``).

Five operations, and deliberately only five:

1. ``decision_followup_observation.measurement_as_of`` (``TIMESTAMPTZ``,
   nullable).  The declared instant every V3 endpoint lookup was bounded by
   (``batch.completed_at <= measurement_as_of``).  It is a real column, not
   payload-only like its five sibling V3 facts, for exactly one reason: it
   is the one the database itself must constrain (operation 4).

2. ``ck_decision_followup_policy_version`` widens from the two-value list
   migration 0097 left it at to a three-value one.  Same idiom, same reason.

3. ``ck_decision_followup_v3_crypto_only`` -- new.  ``FOLLOWUP_POLICY_V3`` is
   CRYPTO-only as a matter of what it *means*: it is a daily-candle forward
   window contract, and neither a KALSHI live public read (no retained
   historical quote archive, no candle window) nor a SECURITIES structure
   terminal payoff (resolved from an options-chain snapshot by an entirely
   separate mechanism) has anything for it to measure.  Restated here as
   well as in ``domain.counterfactual_followup.followup_policy_version_
   supports_venue`` and in both non-CRYPTO builders' own guards -- the same
   defense-in-depth discipline ``ck_decision_followup_kalshi_baseline_kind``
   (migration 0097) already applies to ``FollowUpBaselineKind``.

4. ``ck_decision_followup_v3_measurement_as_of`` -- new, and deliberately a
   *biconditional* rather than a one-way "V3 requires it".  A V3 row must
   have one and every other row must not: the one-way form would have let a
   ``FOLLOWUP_POLICY_V1``/``V2`` row acquire a ``measurement_as_of`` it
   never measured under, which would both misrepresent that row and break
   the canonical-hash block invariant the application layer depends on.
   Every already-retained row satisfies it trivially (all are V1/V2, all are
   ``NULL``), so the constraint validates without touching any data.

5. ``ck_counterfactual_outcome_followup_policy_version`` widens the same way
   as operation 2.  ``counterfactual_outcome_aggregate`` partitions its
   cohorts on ``followup_policy_version`` and
   ``build_counterfactual_outcome_aggregates`` *discovers* its cohort triples
   from the retained observations themselves, so V3 cohorts appear
   automatically alongside -- never replacing -- the existing V2 ones, with
   no change to the outcome builder.  Without this widening those new
   cohorts would be computed and then rejected at insert.

**Deliberately NOT widened**: ``ck_securities_structure_outcome_followup_
policy_version`` (migration 0105).  SECURITIES stays on ``FOLLOWUP_POLICY_
V2`` -- its follow-up behavior has not changed, so it is not relabelled --
and V3 is CRYPTO-only, so no SECURITIES structure outcome can ever carry V3.
Widening it would assert a capability that does not and will not exist, and
would quietly weaken a constraint that is currently doing real work.

Downgrade is guarded, symmetric with migrations 0097 and 0103: a genuine
retained ``FOLLOWUP_POLICY_V3`` observation -- or a genuine V3 outcome
aggregate -- would immediately violate the narrower constraints this
downgrade restores, so it refuses rather than either silently deleting
retained evidence or leaving the database unable to re-validate its own
CHECK constraints.  A database holding only V1/V2 rows (or none) downgrades
cleanly and exactly reverses every operation above.

Historically self-contained per the migration-boundary rule
(``tests.security.test_architecture_boundaries.test_migrations_never_import_
mutable_domain_or_application_modules``, and migration 0095's "freeze the
exact literal values, never import live domain code" doctrine as hardened by
Migration 0095 Historical Reproducibility Hardening v1): this migration
imports only ``alembic``/``sqlalchemy`` and hardcodes every literal string
rather than importing ``domain.counterfactual_followup``.  A fresh
``0001 -> head`` replay must never depend on how live application code has
changed since this migration shipped.

Revision ID: 0113_followup_policy_v3
Revises: 0112_trial_evidence_coverage
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0113_followup_policy_v3"
down_revision: str | None = "0112_trial_evidence_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATION_TABLE = "decision_followup_observation"
_OUTCOME_TABLE = "counterfactual_outcome_aggregate"

_V3 = "FOLLOWUP_POLICY_V3"

_OBSERVATION_POLICY_CONSTRAINT = "ck_decision_followup_policy_version"
_OBSERVATION_POLICY_V1_AND_V2 = "policy_version IN ('FOLLOWUP_POLICY_V1', 'FOLLOWUP_POLICY_V2')"
_OBSERVATION_POLICY_V1_V2_AND_V3 = (
    "policy_version IN ('FOLLOWUP_POLICY_V1', 'FOLLOWUP_POLICY_V2', 'FOLLOWUP_POLICY_V3')"
)

_OUTCOME_POLICY_CONSTRAINT = "ck_counterfactual_outcome_followup_policy_version"
_OUTCOME_POLICY_V1_AND_V2 = "followup_policy_version IN ('FOLLOWUP_POLICY_V1','FOLLOWUP_POLICY_V2')"
_OUTCOME_POLICY_V1_V2_AND_V3 = (
    "followup_policy_version IN ('FOLLOWUP_POLICY_V1','FOLLOWUP_POLICY_V2','FOLLOWUP_POLICY_V3')"
)

_V3_CRYPTO_ONLY_CONSTRAINT = "ck_decision_followup_v3_crypto_only"
_V3_CRYPTO_ONLY_RULE = "policy_version <> 'FOLLOWUP_POLICY_V3' OR venue = 'CRYPTO'"

_V3_MEASUREMENT_CONSTRAINT = "ck_decision_followup_v3_measurement_as_of"
_V3_MEASUREMENT_RULE = "(policy_version = 'FOLLOWUP_POLICY_V3') = (measurement_as_of IS NOT NULL)"


def upgrade() -> None:
    op.add_column(
        _OBSERVATION_TABLE,
        sa.Column("measurement_as_of", sa.DateTime(timezone=True), nullable=True),
    )

    op.drop_constraint(_OBSERVATION_POLICY_CONSTRAINT, _OBSERVATION_TABLE, type_="check")
    op.create_check_constraint(
        _OBSERVATION_POLICY_CONSTRAINT, _OBSERVATION_TABLE, _OBSERVATION_POLICY_V1_V2_AND_V3
    )

    op.create_check_constraint(_V3_CRYPTO_ONLY_CONSTRAINT, _OBSERVATION_TABLE, _V3_CRYPTO_ONLY_RULE)
    op.create_check_constraint(_V3_MEASUREMENT_CONSTRAINT, _OBSERVATION_TABLE, _V3_MEASUREMENT_RULE)

    op.drop_constraint(_OUTCOME_POLICY_CONSTRAINT, _OUTCOME_TABLE, type_="check")
    op.create_check_constraint(
        _OUTCOME_POLICY_CONSTRAINT, _OUTCOME_TABLE, _OUTCOME_POLICY_V1_V2_AND_V3
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_OBSERVATION_TABLE} "  # nosec B608
            f"WHERE policy_version = '{_V3}') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0113 with retained FOLLOWUP_POLICY_V3 follow-up evidence'; "
            "END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_OUTCOME_TABLE} "  # nosec B608
            f"WHERE followup_policy_version = '{_V3}') "
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0113 with retained FOLLOWUP_POLICY_V3 outcome aggregates'; "
            "END IF; END $$"
        )
    )

    op.drop_constraint(_OUTCOME_POLICY_CONSTRAINT, _OUTCOME_TABLE, type_="check")
    op.create_check_constraint(
        _OUTCOME_POLICY_CONSTRAINT, _OUTCOME_TABLE, _OUTCOME_POLICY_V1_AND_V2
    )

    op.drop_constraint(_V3_MEASUREMENT_CONSTRAINT, _OBSERVATION_TABLE, type_="check")
    op.drop_constraint(_V3_CRYPTO_ONLY_CONSTRAINT, _OBSERVATION_TABLE, type_="check")

    op.drop_constraint(_OBSERVATION_POLICY_CONSTRAINT, _OBSERVATION_TABLE, type_="check")
    op.create_check_constraint(
        _OBSERVATION_POLICY_CONSTRAINT, _OBSERVATION_TABLE, _OBSERVATION_POLICY_V1_AND_V2
    )

    op.drop_column(_OBSERVATION_TABLE, "measurement_as_of")
