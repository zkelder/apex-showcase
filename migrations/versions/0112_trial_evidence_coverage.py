"""Trial Evidence Coverage Disclosure v1.

Adds six additive, nullable ``coverage_*`` columns to the existing
``internal_strategy_paper_trial_closure`` table (migration ``0111``) so a
Strategy PAPER trial's closure record can honestly disclose how much
genuine, qualifying ``live_market_observation`` evidence was actually
retained across the trial's own real elapsed window
``[opened_at, closed_at]`` -- see ``domain.strategy_paper_trial_evidence_
coverage`` for what each column means and how it is computed.

Deliberately excludes a persisted ``material_gap``/materiality boolean:
whether a gap is "material" is an interpretive threshold judgment, not a
retained fact, so this migration never writes one -- see ``domain.strategy_
paper_trial_evidence_coverage``'s own "Materiality is derived, never
persisted" section. ``coverage_largest_gap_seconds`` (an objective,
mechanically precise measure) is the authoritative persisted value; any
materiality label is derived from it at read-model/display time only, via
``is_material_coverage_gap``, and can be freely reinterpreted later without
a migration or backfill.

PURELY DESCRIPTIVE. This migration touches no other table, adds no new
CHECK constraint that could reject an already-valid row, and changes
nothing about how a trial opens, closes, or is priced -- see that domain
module's own docstring for the full list of things this milestone is
explicitly forbidden from influencing.

``internal_strategy_paper_trial`` (the sibling table) is untouched: the one
existing production row does not need mutation, and this migration performs
no ``UPDATE``/backfill of any kind -- ``internal_strategy_paper_trial_
closure`` is retained-0-rows in production as of this migration (confirmed
via the 2026-09-21 recovery-acceptance audit immediately preceding this
milestone), so there is nothing to backfill even in principle. Every new
column stays nullable forever (never promoted to ``NOT NULL`` the way
migration ``0097``'s ``baseline_kind`` was, since that migration had
existing rows to reconcile and this one has none) -- application code
always populates all six together (see ``domain.strategy_paper_trial_
lifecycle.build_strategy_paper_trial_closure_record``), so nullability here
is purely a conservative, zero-backfill-required migration shape, not a
claim that partial coverage data is ever a legitimate row shape.

Historically self-contained per the migration-boundary rule (mirrors every
prior migration's own doctrine): imports only ``alembic``/``sqlalchemy``,
never ``apex.domain``/``apex.application``.

Revision ID: 0112_trial_evidence_coverage
Revises: 0111_strategy_paper_trials
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0112_trial_evidence_coverage"
down_revision: str | None = "0111_strategy_paper_trials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "internal_strategy_paper_trial_closure"

_NEW_COLUMNS = (
    ("coverage_window_duration_seconds", sa.Integer),
    ("coverage_nominal_days", sa.Integer),
    ("coverage_covered_days", sa.Integer),
    ("coverage_qualifying_observation_count", sa.Integer),
    ("coverage_largest_gap_seconds", sa.Integer),
    ("coverage_policy_version", sa.String(60)),
)

_NONNEGATIVE_CHECKS = (
    (
        # Shortened from the column name's own "_window_duration" (which,
        # with the "ck_internal_strategy_paper_trial_closure_coverage_"
        # prefix, exceeds PostgreSQL's 63-byte identifier limit --
        # confirmed the hard way against a real apex_test database, not
        # just computed on paper; mirrors migration 0111's own revision-id
        # length lesson).
        "ck_internal_strategy_paper_trial_closure_coverage_window_secs",
        "coverage_window_duration_seconds IS NULL OR coverage_window_duration_seconds >= 0",
    ),
    (
        "ck_internal_strategy_paper_trial_closure_coverage_nominal_days",
        "coverage_nominal_days IS NULL OR coverage_nominal_days >= 0",
    ),
    (
        "ck_internal_strategy_paper_trial_closure_coverage_covered_days",
        "coverage_covered_days IS NULL OR coverage_covered_days >= 0",
    ),
    (
        "ck_internal_strategy_paper_trial_closure_coverage_obs_count",
        "coverage_qualifying_observation_count IS NULL "
        "OR coverage_qualifying_observation_count >= 0",
    ),
    (
        "ck_internal_strategy_paper_trial_closure_coverage_gap",
        "coverage_largest_gap_seconds IS NULL OR coverage_largest_gap_seconds >= 0",
    ),
    (
        # covered_days can never exceed nominal_days -- see domain.strategy_
        # paper_trial_evidence_coverage's own module docstring for why both
        # are counted on the identical distinct-UTC-calendar-date basis.
        "ck_internal_strategy_paper_trial_closure_coverage_days_bound",
        "coverage_covered_days IS NULL OR coverage_nominal_days IS NULL "
        "OR coverage_covered_days <= coverage_nominal_days",
    ),
)


def upgrade() -> None:
    for name, column_type in _NEW_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, column_type, nullable=True))
    for name, condition in _NONNEGATIVE_CHECKS:
        op.create_check_constraint(name, _TABLE, condition)


def downgrade() -> None:
    # Refuse rather than silently discard genuine retained coverage
    # evidence -- mirrors every other Apex migration's downgrade-guard
    # doctrine (e.g. 0097, 0100). A database with zero populated coverage
    # rows (the case for every environment this migration has been applied
    # to as of this writing) downgrades cleanly.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {_TABLE} WHERE coverage_policy_version IS NOT NULL) "  # nosec B608
            "THEN RAISE EXCEPTION "
            "'cannot downgrade 0112 with retained trial evidence coverage data'; "
            "END IF; END $$"
        )
    )
    for name, _condition in _NONNEGATIVE_CHECKS:
        op.drop_constraint(name, _TABLE, type_="check")
    for name, _column_type in _NEW_COLUMNS:
        op.drop_column(_TABLE, name)
