"""Durable, versioned Investment Mandate and deterministic Opportunity evaluation.

This module owns no provider client, credential, network, execution, or
capital-mutation capability.  It expresses bounded user intent (an
``InvestmentMandate``) and evaluates one existing, already-retained
``SharedOpportunityProjection`` against that mandate, current normalized
portfolio truth (``domain.portfolio_state``), and the Opportunity's own
retained economics -- producing a structured, deterministic, replayable
result.  It never submits an order, creates a reservation, PAPER trial, or
LIVE authority, and every persisted fact this module produces carries
``authority = "NONE"``.

Missing or stale safety-relevant information always fails conservatively:
an unknown fact never becomes a silent ``ALLOWED``.  The evaluator is a pure
function of its inputs -- given the identical mandate, portfolio snapshot,
and Opportunity, it always returns the identical result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from apex.domain.portfolio_state import (
    KNOWN_MARKET_FAMILIES,
    MARKET_FAMILY_CRYPTO_SPOT,
    MARKET_FAMILY_KALSHI_EVENT_CONTRACT,
    MARKET_FAMILY_US_EQUITY_OPTION,
    PortfolioPosition,
    PortfolioSnapshot,
    PortfolioVenue,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import (
    DecimalInput,
    RiskFraction,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

INVESTMENT_MANDATE_V1 = "INVESTMENT_MANDATE_V1"
MANDATE_EVALUATION_V1 = "MANDATE_EVALUATION_V1"
MANDATE_WARNING = "MANDATE EVALUATION IS RESEARCH/PAPER GUIDANCE ONLY — NO ORDER OR LIVE AUTHORITY"
DEFAULT_MANDATE_LINEAGE_ID = "default"

# A portfolio snapshot older than this relative to the evaluation time is
# treated as safety-relevant-stale and fails the evaluation closed, rather
# than reasoning about capital limits against possibly-outdated truth.
PORTFOLIO_SNAPSHOT_FRESHNESS_LIMIT_SECONDS = 86_400

_KNOWN_VENUES = frozenset(item.value for item in PortfolioVenue)


class InvestmentMandateError(ValueError):
    """Raised when mandate or evaluation evidence is invalid, incomplete, or conflicting."""


class AutonomyRequirement(StrEnum):
    """Declares a mandate's autonomy preference; it grants no authority by itself.

    ``AUTONOMOUS_WITHIN_LIMITS`` mirrors how internal PAPER (Crypto/Kalshi)
    already operates today: no human click per eligible trial, but bounded
    by hard caps.  ``OPERATOR_APPROVAL_REQUIRED`` mirrors how External PAPER
    (Alpaca) already requires explicit operator confirmation before any
    broker submission.  Recording this preference here does not itself wire
    up, grant, or change any execution authority.
    """

    AUTONOMOUS_WITHIN_LIMITS = "AUTONOMOUS_WITHIN_LIMITS"
    OPERATOR_APPROVAL_REQUIRED = "OPERATOR_APPROVAL_REQUIRED"


class MandateDecisionState(StrEnum):
    ALLOWED = "ALLOWED"
    CONSTRAINED = "CONSTRAINED"
    REJECTED = "REJECTED"


def _id(value: str, reason: str, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise InvestmentMandateError(reason)
    return value.strip()


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvestmentMandateError(reason)
    return value.astimezone(UTC)


def _decimal(value: object, reason: str, *, nonnegative: bool = False) -> Decimal:
    try:
        return parse_decimal(cast(str | int | Decimal, value), allow_negative=not nonnegative)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise InvestmentMandateError(reason) from error


def _fraction(value: RiskFraction | DecimalInput, reason: str) -> RiskFraction:
    if isinstance(value, RiskFraction):
        return value
    try:
        return RiskFraction(value)
    except ValueValidationError as error:
        raise InvestmentMandateError(reason) from error


def _venue_for_shared_venue(venue: SharedVenue) -> PortfolioVenue:
    if venue is SharedVenue.CRYPTO:
        return PortfolioVenue.COINBASE
    if venue is SharedVenue.KALSHI:
        return PortfolioVenue.KALSHI
    if venue is SharedVenue.SECURITIES:
        # Securities Research -> Shared Opportunity Recurring Bridge v1.
        # Reuses PortfolioVenue.ALPACA -- already defined (and already the
        # real evidence-source label the Securities options-structure
        # producer itself carries: provider="ALPACA_OPTIONS_MARKET_DATA")
        # -- never a new venue member. Found and fixed before this bridge's
        # first real SECURITIES-eligible opportunity could ever reach the
        # recurring MANDATE_EVALUATION_REBUILD task: this function
        # previously raised for any non-CRYPTO/KALSHI venue with no
        # surrounding try/except at its one call site, which would have
        # crashed the whole cycle the instant one SECURITIES opportunity
        # became ELIGIBLE (confirmed reachable today against real retained
        # production evidence -- see the milestone's own representative
        # demonstration). Wiring this mapping does not grant any new
        # authority: the active production mandate's own
        # ``permitted_venues``/``permitted_market_families`` (verified
        # read-only against production: ``('COINBASE',)``/
        # ``('CRYPTO_SPOT',)``) already make every SECURITIES opportunity
        # evaluate to a genuine, honest ``REJECTED`` below -- exactly the
        # non-crashing outcome this fix exists to restore.
        return PortfolioVenue.ALPACA
    raise InvestmentMandateError("MANDATE_EVALUATION_UNSUPPORTED_SHARED_VENUE")


def _market_family_for_shared_venue(venue: SharedVenue) -> str:
    if venue is SharedVenue.CRYPTO:
        return MARKET_FAMILY_CRYPTO_SPOT
    if venue is SharedVenue.KALSHI:
        return MARKET_FAMILY_KALSHI_EVENT_CONTRACT
    if venue is SharedVenue.SECURITIES:
        # Already-defined, already-anticipated market family (see
        # ``portfolio_state.KNOWN_MARKET_FAMILIES``'s own module comment:
        # "these constants exist for callers to reference the known values
        # without retyping literals") -- the Securities bridge's only
        # producer today is defined-risk options structures, so
        # US_EQUITY_OPTION is the correct, honest family, not a proxy.
        return MARKET_FAMILY_US_EQUITY_OPTION
    raise InvestmentMandateError("MANDATE_EVALUATION_UNSUPPORTED_SHARED_VENUE")


@dataclass(frozen=True, slots=True)
class InvestmentMandate:
    mandate_id: str
    lineage_id: str
    version: int
    previous_mandate_id: str | None
    effective_at: datetime
    created_at: datetime
    total_capital_allocated: Decimal
    permitted_venues: tuple[str, ...]
    permitted_market_families: tuple[str, ...]
    disallowed_market_families: tuple[str, ...]
    maximum_aggregate_exposure_fraction: RiskFraction
    maximum_per_instrument_fraction: RiskFraction
    maximum_venue_concentration_fraction: RiskFraction
    maximum_market_family_concentration_fraction: RiskFraction
    leverage_permitted: bool
    short_exposure_permitted: bool
    options_permitted: bool
    options_defined_risk_only: bool
    minimum_liquidity_state: str
    maximum_capital_lockup_days: int | None
    autonomy_requirement: AutonomyRequirement
    notes: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        lineage_id: str = DEFAULT_MANDATE_LINEAGE_ID,
        version: int,
        previous_mandate_id: str | None,
        effective_at: datetime,
        created_at: datetime,
        total_capital_allocated: DecimalInput,
        permitted_venues: tuple[str, ...] = (),
        permitted_market_families: tuple[str, ...] = (),
        disallowed_market_families: tuple[str, ...] = (),
        maximum_aggregate_exposure_fraction: RiskFraction | DecimalInput,
        maximum_per_instrument_fraction: RiskFraction | DecimalInput,
        maximum_venue_concentration_fraction: RiskFraction | DecimalInput,
        maximum_market_family_concentration_fraction: RiskFraction | DecimalInput,
        leverage_permitted: bool = False,
        short_exposure_permitted: bool = False,
        options_permitted: bool = False,
        options_defined_risk_only: bool = True,
        minimum_liquidity_state: str = "SUFFICIENT",
        maximum_capital_lockup_days: int | None,
        autonomy_requirement: AutonomyRequirement = AutonomyRequirement.OPERATOR_APPROVAL_REQUIRED,
        notes: str = "",
    ) -> InvestmentMandate:
        if version <= 0:
            raise InvestmentMandateError("MANDATE_VERSION_INVALID")
        if version == 1 and previous_mandate_id is not None:
            raise InvestmentMandateError("MANDATE_INITIAL_VERSION_MUST_HAVE_NO_PREDECESSOR")
        if version > 1 and previous_mandate_id is None:
            raise InvestmentMandateError("MANDATE_REVISION_REQUIRES_PREDECESSOR")
        lineage = _id(lineage_id, "MANDATE_LINEAGE_ID_INVALID")
        effective = _time(effective_at, "MANDATE_EFFECTIVE_AT_INVALID")
        created = _time(created_at, "MANDATE_CREATED_AT_INVALID")
        if effective < created:
            # Never let an authored mandate retroactively claim it governed a
            # point in time before it was actually recorded.
            raise InvestmentMandateError("MANDATE_CANNOT_BE_BACKDATED_BEFORE_ITS_OWN_CREATION")
        capital = _decimal(
            total_capital_allocated, "MANDATE_TOTAL_CAPITAL_INVALID", nonnegative=True
        )
        venues = tuple(
            sorted({_id(item, "MANDATE_VENUE_INVALID", 40) for item in permitted_venues})
        )
        if any(item not in _KNOWN_VENUES for item in venues):
            raise InvestmentMandateError("MANDATE_VENUE_UNKNOWN")
        permitted_families = tuple(
            sorted(
                {
                    _id(item, "MANDATE_MARKET_FAMILY_INVALID", 60)
                    for item in permitted_market_families
                }
            )
        )
        disallowed_families = tuple(
            sorted(
                {
                    _id(item, "MANDATE_MARKET_FAMILY_INVALID", 60)
                    for item in disallowed_market_families
                }
            )
        )
        if any(
            item not in KNOWN_MARKET_FAMILIES
            for item in (*permitted_families, *disallowed_families)
        ):
            raise InvestmentMandateError("MANDATE_MARKET_FAMILY_UNKNOWN")
        if set(permitted_families) & set(disallowed_families):
            raise InvestmentMandateError("MANDATE_MARKET_FAMILY_PERMITTED_AND_DISALLOWED_CONFLICT")
        aggregate_fraction = _fraction(
            maximum_aggregate_exposure_fraction, "MANDATE_AGGREGATE_FRACTION_INVALID"
        )
        instrument_fraction = _fraction(
            maximum_per_instrument_fraction, "MANDATE_INSTRUMENT_FRACTION_INVALID"
        )
        venue_fraction = _fraction(
            maximum_venue_concentration_fraction, "MANDATE_VENUE_FRACTION_INVALID"
        )
        family_fraction = _fraction(
            maximum_market_family_concentration_fraction, "MANDATE_FAMILY_FRACTION_INVALID"
        )
        liquidity_state = _id(minimum_liquidity_state, "MANDATE_LIQUIDITY_STATE_INVALID", 40)
        if maximum_capital_lockup_days is not None and maximum_capital_lockup_days < 0:
            raise InvestmentMandateError("MANDATE_LOCKUP_DAYS_INVALID")
        if options_permitted is False and options_defined_risk_only is False:
            # A mandate that forbids options entirely need not also assert an
            # undefined-risk permission it can never exercise; keep the
            # combination unambiguous rather than silently ignoring it.
            raise InvestmentMandateError("MANDATE_UNDEFINED_RISK_OPTIONS_REQUIRE_OPTIONS_PERMITTED")
        notes_text = notes.strip()
        if len(notes_text) > 2_000:
            raise InvestmentMandateError("MANDATE_NOTES_TOO_LONG")
        values = (
            lineage,
            version,
            previous_mandate_id,
            effective,
            created,
            capital,
            venues,
            permitted_families,
            disallowed_families,
            aggregate_fraction,
            instrument_fraction,
            venue_fraction,
            family_fraction,
            leverage_permitted,
            short_exposure_permitted,
            options_permitted,
            options_defined_risk_only,
            liquidity_state,
            maximum_capital_lockup_days,
            autonomy_requirement,
            notes_text,
            "NONE",
        )
        mandate_id = deterministic_id("investment_mandate", (INVESTMENT_MANDATE_V1, *values))
        return cls(
            mandate_id,
            *values[:-1],
            content_digest((INVESTMENT_MANDATE_V1, mandate_id, *values)),
            values[-1],
        )

    @classmethod
    def default_v1(
        cls, *, effective_at: datetime, created_at: datetime | None = None
    ) -> InvestmentMandate:
        """The conservative, zero-capital, nothing-permitted starting mandate.

        Apex genuinely holds zero user-allocated capital today.  This default
        keeps every mandate evaluation ``REJECTED`` until an operator
        explicitly authors a mandate with real values -- missing mandate
        content must never read as permissive.
        """

        return cls.create(
            version=1,
            previous_mandate_id=None,
            effective_at=effective_at,
            created_at=effective_at if created_at is None else created_at,
            total_capital_allocated=Decimal(0),
            permitted_venues=(),
            permitted_market_families=(),
            disallowed_market_families=tuple(sorted(KNOWN_MARKET_FAMILIES)),
            maximum_aggregate_exposure_fraction=RiskFraction("0"),
            maximum_per_instrument_fraction=RiskFraction("0"),
            maximum_venue_concentration_fraction=RiskFraction("0"),
            maximum_market_family_concentration_fraction=RiskFraction("0"),
            leverage_permitted=False,
            short_exposure_permitted=False,
            options_permitted=False,
            options_defined_risk_only=True,
            minimum_liquidity_state="SUFFICIENT",
            maximum_capital_lockup_days=0,
            autonomy_requirement=AutonomyRequirement.OPERATOR_APPROVAL_REQUIRED,
            notes=(
                "Conservative default mandate: zero capital allocated, nothing "
                "permitted. Author a real mandate before any Opportunity can be "
                "ALLOWED or CONSTRAINED."
            ),
        )


@dataclass(frozen=True, slots=True)
class MandateConstraintCheck:
    name: str
    satisfied: bool
    binding: bool
    limit: str | None
    observed: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class MandateEvaluation:
    evaluation_id: str
    mandate_id: str
    mandate_lineage_id: str
    mandate_version: int
    mandate_hash: str
    portfolio_snapshot_id: str
    portfolio_snapshot_hash: str
    opportunity_id: str
    opportunity_hash: str
    venue: PortfolioVenue
    market_family: str
    evaluated_at: datetime
    state: MandateDecisionState
    reason_codes: tuple[str, ...]
    unknown_facts: tuple[str, ...]
    explanation: str
    constraints_evaluated: tuple[MandateConstraintCheck, ...]
    required_capital: Decimal | None
    permitted_allocation_amount: Decimal | None
    warnings: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def evaluate_mandate(
    *,
    mandate: InvestmentMandate,
    portfolio: PortfolioSnapshot,
    positions: tuple[PortfolioPosition, ...],
    opportunity: SharedOpportunityProjection,
    evaluated_at: datetime,
) -> MandateEvaluation:
    """Evaluate one Opportunity against one mandate version and one portfolio snapshot.

    ``positions`` must be exactly the positions referenced by
    ``portfolio.position_ids`` (the caller-side lineage completeness the
    rest of this codebase's "snapshot + components" pattern also requires,
    e.g. ``total_market_context.attach_context``); account-balance
    aggregates are read from the already-computed, conservative
    ``portfolio.total_known_*`` fields rather than re-derived here.

    Pure and deterministic: identical inputs always produce an identical
    result (including ``evaluation_id``/``canonical_hash``).  Never mutates
    its inputs, never performs I/O, never grants execution authority.
    """

    now = _time(evaluated_at, "MANDATE_EVALUATION_TIME_INVALID")
    if tuple(sorted(item.position_id for item in positions)) != tuple(
        sorted(portfolio.position_ids)
    ):
        raise InvestmentMandateError("MANDATE_EVALUATION_POSITION_LINEAGE_INCOMPLETE")

    checks: list[MandateConstraintCheck] = []
    reasons: list[str] = []
    unknown_facts: list[str] = []

    def record(
        name: str,
        satisfied: bool,
        *,
        limit: str | None = None,
        observed: str | None = None,
        detail: str = "",
        binding: bool | None = None,
    ) -> None:
        checks.append(
            MandateConstraintCheck(
                name,
                satisfied,
                (not satisfied) if binding is None else binding,
                limit,
                observed,
                detail,
            )
        )

    mandate_effective = mandate.effective_at <= now
    record(
        "MANDATE_EFFECTIVE",
        mandate_effective,
        limit=str(mandate.effective_at),
        observed=str(now),
        detail="the selected mandate version must already be in effect as of the evaluation time",
    )
    if not mandate_effective:
        reasons.append("MANDATE_NOT_YET_EFFECTIVE")

    portfolio_not_future = portfolio.as_of <= now
    record(
        "PORTFOLIO_SNAPSHOT_NOT_FROM_FUTURE",
        portfolio_not_future,
        limit=str(now),
        observed=str(portfolio.as_of),
    )
    if not portfolio_not_future:
        reasons.append("PORTFOLIO_SNAPSHOT_FROM_THE_FUTURE")

    snapshot_age_seconds = max(0, int((now - portfolio.as_of).total_seconds()))
    portfolio_fresh = snapshot_age_seconds <= PORTFOLIO_SNAPSHOT_FRESHNESS_LIMIT_SECONDS
    record(
        "PORTFOLIO_SNAPSHOT_FRESH",
        portfolio_fresh,
        limit=str(PORTFOLIO_SNAPSHOT_FRESHNESS_LIMIT_SECONDS),
        observed=str(snapshot_age_seconds),
        detail="stale portfolio truth must not be treated as current for capital limits",
    )
    if not portfolio_fresh:
        reasons.append("PORTFOLIO_SNAPSHOT_STALE")

    opportunity_not_future = opportunity.generated_at <= now
    record(
        "OPPORTUNITY_EVIDENCE_NOT_FROM_FUTURE",
        opportunity_not_future,
        limit=str(now),
        observed=str(opportunity.generated_at),
    )
    if not opportunity_not_future:
        reasons.append("OPPORTUNITY_EVIDENCE_FROM_THE_FUTURE")

    opportunity_not_expired = opportunity.valid_until is None or now < opportunity.valid_until
    record(
        "OPPORTUNITY_NOT_EXPIRED",
        opportunity_not_expired,
        limit=None if opportunity.valid_until is None else str(opportunity.valid_until),
        observed=str(now),
    )
    if not opportunity_not_expired:
        reasons.append("OPPORTUNITY_EXPIRED")

    venue = _venue_for_shared_venue(opportunity.venue)
    family = _market_family_for_shared_venue(opportunity.venue)

    venue_permitted = venue.value in mandate.permitted_venues
    record(
        "VENUE_PERMITTED",
        venue_permitted,
        limit=",".join(mandate.permitted_venues) or "(none permitted)",
        observed=venue.value,
    )
    if not venue_permitted:
        reasons.append("VENUE_NOT_PERMITTED_BY_MANDATE")

    family_disallowed = family in mandate.disallowed_market_families
    family_permitted = family in mandate.permitted_market_families and not family_disallowed
    record(
        "MARKET_FAMILY_PERMITTED",
        family_permitted,
        limit=",".join(mandate.permitted_market_families) or "(none permitted)",
        observed=family,
    )
    if family_disallowed:
        reasons.append("MARKET_FAMILY_EXPLICITLY_DISALLOWED_BY_MANDATE")
    elif not family_permitted:
        reasons.append("MARKET_FAMILY_NOT_PERMITTED_BY_MANDATE")

    options_ok = family != MARKET_FAMILY_US_EQUITY_OPTION or mandate.options_permitted
    record(
        "OPTIONS_PERMITTED_IF_APPLICABLE",
        options_ok,
        observed=family,
        detail="only binds when the Opportunity's market family is US_EQUITY_OPTION",
    )
    if not options_ok:
        reasons.append("OPTIONS_NOT_PERMITTED_BY_MANDATE")

    is_short = "SHORT" in opportunity.direction.upper()
    short_ok = (not is_short) or mandate.short_exposure_permitted
    record(
        "SHORT_EXPOSURE_PERMITTED_IF_APPLICABLE",
        short_ok,
        observed=opportunity.direction,
        detail="best-effort heuristic over the Opportunity's free-text direction field",
    )
    if not short_ok:
        reasons.append("SHORT_EXPOSURE_NOT_PERMITTED_BY_MANDATE")

    liquidity_ok = opportunity.liquidity_state == mandate.minimum_liquidity_state
    record(
        "LIQUIDITY_MEETS_MANDATE_MINIMUM",
        liquidity_ok,
        limit=mandate.minimum_liquidity_state,
        observed=opportunity.liquidity_state,
    )
    if not liquidity_ok:
        reasons.append("LIQUIDITY_BELOW_MANDATE_MINIMUM")

    lockup_ok = (
        mandate.maximum_capital_lockup_days is None
        or opportunity.estimated_lockup_days is None
        or opportunity.estimated_lockup_days <= mandate.maximum_capital_lockup_days
    )
    record(
        "CAPITAL_LOCKUP_WITHIN_MANDATE_MAXIMUM",
        lockup_ok,
        limit=None
        if mandate.maximum_capital_lockup_days is None
        else str(mandate.maximum_capital_lockup_days),
        observed=None
        if opportunity.estimated_lockup_days is None
        else str(opportunity.estimated_lockup_days),
    )
    if not lockup_ok:
        reasons.append("CAPITAL_LOCKUP_EXCEEDS_MANDATE_MAXIMUM")

    required_capital = opportunity.required_hypothetical_capital
    economics_complete = (
        required_capital is not None
        and required_capital > 0
        and opportunity.maximum_loss is not None
        and opportunity.costs_retained
    )
    record(
        "OPPORTUNITY_ECONOMICS_COMPLETE",
        economics_complete,
        detail="required capital, maximum loss, and retained cost evidence must all be present",
    )
    if not economics_complete:
        reasons.append("OPPORTUNITY_ECONOMICS_INCOMPLETE_FOR_MANDATE_EVALUATION")
        unknown_facts.append("OPPORTUNITY_REQUIRED_CAPITAL_OR_MAXIMUM_LOSS_OR_COSTS")

    eligibility_clean = not reasons
    permitted_allocation_amount: Decimal | None = None

    if eligibility_clean:
        instrument_positions = tuple(
            item for item in positions if item.instrument_id == opportunity.instrument_id
        )
        venue_positions = tuple(item for item in positions if item.venue is venue)
        family_positions = tuple(item for item in positions if item.market_family == family)

        def known_sum(group: tuple[PortfolioPosition, ...]) -> Decimal | None:
            if any(item.exposure_value is None for item in group):
                return None
            return sum((item.exposure_value or Decimal(0) for item in group), Decimal(0))

        instrument_existing = known_sum(instrument_positions)
        venue_existing = known_sum(venue_positions)
        family_existing = known_sum(family_positions)
        # Aggregate *exposure* means capital actually deployed into
        # positions (at risk), never idle cash -- using total portfolio
        # *value* (cash + positions) here would wrongly treat sitting cash
        # as if it were already at risk and starve every allocation.
        aggregate_existing = portfolio.total_known_position_value
        available_cash = portfolio.total_known_cash

        if instrument_existing is None:
            reasons.append("EXISTING_INSTRUMENT_EXPOSURE_UNKNOWN")
            unknown_facts.append("INSTRUMENT_EXISTING_EXPOSURE")
        if venue_existing is None:
            reasons.append("EXISTING_VENUE_EXPOSURE_UNKNOWN")
            unknown_facts.append("VENUE_EXISTING_EXPOSURE")
        if family_existing is None:
            reasons.append("EXISTING_MARKET_FAMILY_EXPOSURE_UNKNOWN")
            unknown_facts.append("MARKET_FAMILY_EXISTING_EXPOSURE")
        if aggregate_existing is None:
            reasons.append("AGGREGATE_POSITION_EXPOSURE_UNKNOWN")
            unknown_facts.append("AGGREGATE_POSITION_EXPOSURE")
        if available_cash is None:
            reasons.append("AVAILABLE_CASH_UNKNOWN")
            unknown_facts.append("AVAILABLE_CASH")

        if not reasons:
            assert required_capital is not None
            total_capital = mandate.total_capital_allocated
            caps = {
                "PER_INSTRUMENT_CONCENTRATION": max(
                    Decimal(0),
                    total_capital * mandate.maximum_per_instrument_fraction.value
                    - cast(Decimal, instrument_existing),
                ),
                "VENUE_CONCENTRATION": max(
                    Decimal(0),
                    total_capital * mandate.maximum_venue_concentration_fraction.value
                    - cast(Decimal, venue_existing),
                ),
                "MARKET_FAMILY_CONCENTRATION": max(
                    Decimal(0),
                    total_capital * mandate.maximum_market_family_concentration_fraction.value
                    - cast(Decimal, family_existing),
                ),
                "AGGREGATE_EXPOSURE": max(
                    Decimal(0),
                    total_capital * mandate.maximum_aggregate_exposure_fraction.value
                    - cast(Decimal, aggregate_existing),
                ),
                "AVAILABLE_CASH": max(Decimal(0), cast(Decimal, available_cash)),
            }
            permitted_allocation_amount = min(caps.values())
            for name, headroom in sorted(caps.items()):
                record(
                    name,
                    headroom >= required_capital,
                    limit=str(headroom),
                    observed=str(required_capital),
                    binding=(headroom == permitted_allocation_amount),
                    detail=f"remaining headroom under this limit is {headroom}",
                )
            if permitted_allocation_amount <= 0:
                reasons.append("MANDATE_HEADROOM_EXHAUSTED")
            elif permitted_allocation_amount < required_capital:
                reasons.append("MANDATE_CAPPED_BELOW_REQUIRED_CAPITAL")

    if not reasons:
        state = MandateDecisionState.ALLOWED
        permitted_allocation_amount = required_capital
    elif (
        eligibility_clean
        and permitted_allocation_amount is not None
        and permitted_allocation_amount > 0
        and set(reasons) == {"MANDATE_CAPPED_BELOW_REQUIRED_CAPITAL"}
    ):
        state = MandateDecisionState.CONSTRAINED
    else:
        state = MandateDecisionState.REJECTED

    reason_codes = tuple(sorted(set(reasons)))
    unknown = tuple(sorted(set(unknown_facts)))
    explanation = (
        f"{state.value}: mandate {mandate.lineage_id} v{mandate.version} evaluated opportunity "
        f"{opportunity.projection_id} ({venue.value}/{family}) at {now.isoformat()}. "
        + (
            f"permitted_allocation_amount={permitted_allocation_amount}. "
            if permitted_allocation_amount is not None
            else "no allocation amount computed. "
        )
        + (f"reasons=[{','.join(reason_codes)}]. " if reason_codes else "no binding constraint. ")
        + (f"unknown_facts=[{','.join(unknown)}]." if unknown else "all relevant facts known.")
    )
    evaluation_id = deterministic_id(
        "mandate_evaluation",
        (
            MANDATE_EVALUATION_V1,
            mandate.mandate_id,
            portfolio.snapshot_id,
            portfolio.canonical_hash,
            opportunity.projection_id,
            opportunity.canonical_hash,
            now,
        ),
    )
    values = (
        mandate.mandate_id,
        mandate.lineage_id,
        mandate.version,
        mandate.canonical_hash,
        portfolio.snapshot_id,
        portfolio.canonical_hash,
        opportunity.projection_id,
        opportunity.canonical_hash,
        venue,
        family,
        now,
        state,
        reason_codes,
        unknown,
        explanation,
        tuple(checks),
        required_capital,
        permitted_allocation_amount,
        (
            MANDATE_WARNING,
            "NOT_AN_ORDER",
            "NOT_A_RESERVATION",
            "NOT_A_PAPER_SUBMISSION",
            "REQUIRES_SEPARATE_EXECUTION_AUTHORITY",
        ),
        "NONE",
    )
    return MandateEvaluation(
        evaluation_id,
        *values[:-1],
        content_digest((MANDATE_EVALUATION_V1, evaluation_id, *values)),
        values[-1],
    )
