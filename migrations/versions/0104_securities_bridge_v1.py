"""Securities Research -> Shared Opportunity Recurring Bridge v1.

Widens exactly seven already-established ``venue``/``observation_type``-shaped
CHECK constraints, across four already-existing tables, from the closed
two-member set (``'CRYPTO'``, ``'KALSHI'``) to additionally permit
``'SECURITIES'`` -- so a genuine defined-risk options structure candidate
(``domain.options_structures.OptionStructureCandidate``, already retained,
already live, already trading-session-scheduled) can flow through the
existing, unmodified Shared Opportunity / counterfactual-ledger / follow-up /
outcome-cohort machinery exactly the way CRYPTO and KALSHI already do, without
weakening a single existing rule for either of them.

This migration creates no new table and no new investment judgment. Every
touched table already carries ``authority = 'NONE'`` end to end; nothing here
changes that. It purely widens allow-lists so a genuinely new, honestly
provenanced value can be inserted at all -- mirrors migration 0097's own
"widen a frozen allow-list, never edit a row" idiom exactly (drop the named
CHECK, recreate it wider, guard the downgrade against retained evidence that
would violate the narrower shape).

Also adds one index, ``ix_osi_candidate_underlying_evaluated`` on
``option_structure_candidate(underlying_canonical_security_id, evaluated_at)``
-- the exact shape the new recurring-cycle producer query
(``latest_structures_by_underlying``) groups/joins on, mirroring the sibling
``options_structure_research_snapshot`` table's own identically-shaped index
from migration 0085.

The seven constraints, and why each is required (found by tracing, not
guessed): a SECURITIES ``SharedOpportunityProjection`` is retained (1); every
retained projection's eligibility decision is unconditionally synced into the
counterfactual ledger regardless of venue, by existing origin-agnostic code
(2); a SECURITIES decision with a DUE follow-up candidate produces a
``decision_followup_observation`` row of a new, honestly-labeled observation
type (3, 4, 5); ``FollowUpBaselineKind.DECISION_TIME`` is the only baseline
kind SECURITIES follow-up ever uses, and migration 0097's own
``ck_decision_followup_kalshi_baseline_kind`` is -- despite its name -- a
closed allow-list of every venue/baseline_kind pairing that predates this
milestone, so a SECURITIES row would otherwise violate it unconditionally (6);
and once at least one SECURITIES follow-up observation exists, the existing,
unmodified cohort-discovery code in ``domain.counterfactual_outcome``
(``_cohort_venue_is_in_scope_for_policy_version`` returns ``True``
unconditionally under policy V1) attempts to persist an honest
``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` aggregate row for it -- exactly the
same structural non-answer KALSHI has always carried under V1 -- which would
otherwise violate the outcome table's own venue CHECK the first time it
happens (7). Confirmed empirically before writing this migration: the domain
dispatch functions behind (1)-(7) (``_decision_is_economically_reducible``,
``_cohort_venue_is_in_scope_for_policy_version``, ``_baseline_kinds_for``,
``assess_followup_evidence_quality``) are already membership-tested rather
than exhaustively enumerated, so none of them raise for an unrecognized venue
-- only the database's own CHECK constraints would have (this migration
removes the one remaining place that would).

Historically self-contained per the migration-boundary rule (mirrors
migrations 0095/0096/0097's own established discipline): imports only
``alembic``/``sqlalchemy`` and hardcodes every literal string rather than
importing live domain constants, so a fresh ``0001 -> head`` replay never
depends on how ``domain.shared_opportunity.SharedVenue`` or
``domain.counterfactual_followup.FollowUpObservationType`` have changed since
this migration shipped.

Revision ID: 0104_securities_bridge_v1
Revises: 0103_counterfactual_outcome_v2

(Named ``0104_securities_bridge_v1``, not the fuller ``0104_securities_
shared_opportunity_bridge`` the milestone's own name would suggest --
``alembic_version.version_num`` is ``VARCHAR(32)`` and the fuller name
does not fit; every other migration in this repository fits its own
revision id inside that same budget, so this shortens rather than widens
another column.)
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0104_securities_bridge_v1"
down_revision: str | None = "0103_counterfactual_outcome_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARED_PROJECTION_TABLE = "shared_opportunity_projection"
_SHARED_PROJECTION_VENUE_CONSTRAINT = "ck_shared_projection_venue"
_SHARED_PROJECTION_VENUE_NARROW = "venue IN ('CRYPTO','KALSHI')"
_SHARED_PROJECTION_VENUE_WIDE = "venue IN ('CRYPTO','KALSHI','SECURITIES')"

_COUNTERFACTUAL_DECISION_TABLE = "counterfactual_decision"
_COUNTERFACTUAL_DECISION_VENUE_CONSTRAINT = "ck_counterfactual_decision_venue"
_COUNTERFACTUAL_DECISION_VENUE_NARROW = "venue IN ('CRYPTO','KALSHI')"
_COUNTERFACTUAL_DECISION_VENUE_WIDE = "venue IN ('CRYPTO','KALSHI','SECURITIES')"

_FOLLOWUP_TABLE = "decision_followup_observation"
_FOLLOWUP_VENUE_CONSTRAINT = "ck_decision_followup_venue"
_FOLLOWUP_VENUE_NARROW = "venue IN ('CRYPTO','KALSHI')"
_FOLLOWUP_VENUE_WIDE = "venue IN ('CRYPTO','KALSHI','SECURITIES')"

_FOLLOWUP_TYPE_CONSTRAINT = "ck_decision_followup_observation_type"
_FOLLOWUP_TYPE_NARROW = (
    "observation_type IN ('CRYPTO_REFERENCE_PRICE_MOVEMENT','KALSHI_MARKET_STATUS_AND_PRICE')"
)
_FOLLOWUP_TYPE_WIDE = (
    "observation_type IN "
    "('CRYPTO_REFERENCE_PRICE_MOVEMENT','KALSHI_MARKET_STATUS_AND_PRICE',"
    "'SECURITIES_STRUCTURE_TERMINAL_PAYOFF')"
)

_FOLLOWUP_AGREEMENT_CONSTRAINT = "ck_decision_followup_venue_type_agreement"
_FOLLOWUP_AGREEMENT_NARROW = (
    "(venue = 'CRYPTO' AND observation_type = 'CRYPTO_REFERENCE_PRICE_MOVEMENT') OR "
    "(venue = 'KALSHI' AND observation_type = 'KALSHI_MARKET_STATUS_AND_PRICE')"
)
_FOLLOWUP_AGREEMENT_WIDE = (
    "(venue = 'CRYPTO' AND observation_type = 'CRYPTO_REFERENCE_PRICE_MOVEMENT') OR "
    "(venue = 'KALSHI' AND observation_type = 'KALSHI_MARKET_STATUS_AND_PRICE') OR "
    "(venue = 'SECURITIES' AND observation_type = 'SECURITIES_STRUCTURE_TERMINAL_PAYOFF')"
)

_FOLLOWUP_BASELINE_KIND_CONSTRAINT = "ck_decision_followup_kalshi_baseline_kind"
_FOLLOWUP_BASELINE_KIND_NARROW = (
    "(venue = 'KALSHI' AND baseline_kind = 'DECISION_TIME') OR venue = 'CRYPTO'"
)
_FOLLOWUP_BASELINE_KIND_WIDE = (
    "(venue = 'KALSHI' AND baseline_kind = 'DECISION_TIME') OR venue = 'CRYPTO' OR "
    "(venue = 'SECURITIES' AND baseline_kind = 'DECISION_TIME')"
)

_OUTCOME_TABLE = "counterfactual_outcome_aggregate"
_OUTCOME_VENUE_CONSTRAINT = "ck_counterfactual_outcome_venue"
_OUTCOME_VENUE_NARROW = "venue IN ('CRYPTO','KALSHI')"
_OUTCOME_VENUE_WIDE = "venue IN ('CRYPTO','KALSHI','SECURITIES')"

_WIDENED_CONSTRAINTS: tuple[tuple[str, str, str, str], ...] = (
    (
        _SHARED_PROJECTION_TABLE,
        _SHARED_PROJECTION_VENUE_CONSTRAINT,
        _SHARED_PROJECTION_VENUE_NARROW,
        _SHARED_PROJECTION_VENUE_WIDE,
    ),
    (
        _COUNTERFACTUAL_DECISION_TABLE,
        _COUNTERFACTUAL_DECISION_VENUE_CONSTRAINT,
        _COUNTERFACTUAL_DECISION_VENUE_NARROW,
        _COUNTERFACTUAL_DECISION_VENUE_WIDE,
    ),
    (_FOLLOWUP_TABLE, _FOLLOWUP_VENUE_CONSTRAINT, _FOLLOWUP_VENUE_NARROW, _FOLLOWUP_VENUE_WIDE),
    (_FOLLOWUP_TABLE, _FOLLOWUP_TYPE_CONSTRAINT, _FOLLOWUP_TYPE_NARROW, _FOLLOWUP_TYPE_WIDE),
    (
        _FOLLOWUP_TABLE,
        _FOLLOWUP_AGREEMENT_CONSTRAINT,
        _FOLLOWUP_AGREEMENT_NARROW,
        _FOLLOWUP_AGREEMENT_WIDE,
    ),
    (
        _FOLLOWUP_TABLE,
        _FOLLOWUP_BASELINE_KIND_CONSTRAINT,
        _FOLLOWUP_BASELINE_KIND_NARROW,
        _FOLLOWUP_BASELINE_KIND_WIDE,
    ),
    (_OUTCOME_TABLE, _OUTCOME_VENUE_CONSTRAINT, _OUTCOME_VENUE_NARROW, _OUTCOME_VENUE_WIDE),
)


_STRUCTURE_CANDIDATE_INDEX = "ix_osi_candidate_underlying_evaluated"


def upgrade() -> None:
    for table, name, _narrow, wide in _WIDENED_CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, wide)
    # ``latest_structures_by_underlying`` (Securities Research -> Shared
    # Opportunity Recurring Bridge v1's own producer query, invoked every
    # recurring-cycle tick) groups/joins on exactly ``(underlying_canonical_
    # security_id, evaluated_at)`` -- the sibling ``options_structure_
    # research_snapshot`` table already carries an index in this identical
    # shape (``ix_osi_snapshot_underlying_evaluated``, migration 0085) for
    # the identical reason; ``option_structure_candidate`` itself only had
    # ``ix_osi_candidate_underlying_expiration`` (underlying + expiration),
    # which does not serve this query. Without this, the query forces a
    # sequential scan of a table that only grows every trading session.
    op.create_index(
        _STRUCTURE_CANDIDATE_INDEX,
        "option_structure_candidate",
        ["underlying_canonical_security_id", "evaluated_at"],
    )


def downgrade() -> None:
    op.drop_index(_STRUCTURE_CANDIDATE_INDEX, table_name="option_structure_candidate")
    # Refuse rather than either silently deleting retained SECURITIES
    # evidence or leaving the database unable to re-validate its own CHECK
    # constraints -- mirrors migration 0096/0097's own downgrade guard
    # exactly. A database holding no SECURITIES-venue rows (and no
    # SECURITIES_STRUCTURE_TERMINAL_PAYOFF observation_type rows) downgrades
    # cleanly.
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM {_SHARED_PROJECTION_TABLE} WHERE venue = 'SECURITIES') "  # nosec B608
        "THEN RAISE EXCEPTION "
        "'cannot downgrade 0104 with retained SECURITIES shared opportunity evidence'; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM {_COUNTERFACTUAL_DECISION_TABLE} WHERE venue = 'SECURITIES') "  # nosec B608
        "THEN RAISE EXCEPTION "
        "'cannot downgrade 0104 with retained SECURITIES counterfactual decision evidence'; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM {_FOLLOWUP_TABLE} WHERE venue = 'SECURITIES') "  # nosec B608
        "THEN RAISE EXCEPTION "
        "'cannot downgrade 0104 with retained SECURITIES follow-up observation evidence'; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM {_OUTCOME_TABLE} WHERE venue = 'SECURITIES') "  # nosec B608
        "THEN RAISE EXCEPTION "
        "'cannot downgrade 0104 with retained SECURITIES counterfactual outcome evidence'; "
        "END IF; END $$"
    )
    for table, name, narrow, _wide in reversed(_WIDENED_CONSTRAINTS):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, narrow)
