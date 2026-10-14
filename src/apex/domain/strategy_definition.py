"""Typed strategy-source contract distinguishing INTERNAL and EXTERNAL origin.

This module is the smallest typed seam needed to answer, for any strategy
Apex might evaluate: who authored its rules (Apex's own research machinery,
or an explicit public/published/user-specified external convention), what
exact deterministic rule it runs, what venue/instruments/evidence it needs,
and its current research/PAPER-eligibility lifecycle state.

It is deliberately NOT a scripting engine. ``StrategyRuleKind`` is a closed,
reviewed enum; ``StrategyRuleParameters`` is a flat, fully deterministic
parameter bag (plain ``int``/``Decimal`` fields only). Adding a new rule
shape means adding a new reviewed enum member and a new pure evaluator
function elsewhere -- never accepting arbitrary code or formulas from a
definition's own data.

``StrategyProvenance`` deliberately has no field for a claimed external
performance figure. Apex never records, trusts, or compares against a
strategy's claimed track record -- only its own independently computed
evidence (see ``apex.domain.strategy_evaluation``) ever measures it.

Every record here carries authority ``NONE``. A ``StrategyDefinition`` is
evidence about what a strategy IS; it creates no Opportunity, Campaign,
Order, Position, PAPER trial, provider request, or LIVE authority by
itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import content_digest, deterministic_id

STRATEGY_DEFINITION_WARNING = "STRATEGY DEFINITION -- EVIDENCE ONLY, NO PAPER OR LIVE AUTHORITY"
STRATEGY_EXTERNAL_PROVENANCE_WARNING = (
    "EXTERNAL STRATEGY RULES ARE INDEPENDENTLY EVALUATED BY APEX; "
    "ANY CLAIMED EXTERNAL PERFORMANCE IS NEVER RECORDED OR TRUSTED"
)

# Matches STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES (shared_opportunity.py) /
# COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES (counterfactual_outcome.py) -- the
# one minimum-sample convention already used elsewhere in Apex for "is there
# enough evidence to say anything," reused here rather than inventing a new
# threshold.
MINIMUM_PROMOTION_SAMPLE_SIZE = 30


class StrategyDefinitionError(ValueError):
    """A strategy definition violated a typed contract or governance invariant."""


class StrategySourceKind(StrEnum):
    INTERNAL = "INTERNAL"
    EXTERNAL = "EXTERNAL"


class StrategyLifecycleState(StrEnum):
    RESEARCH_ONLY = "RESEARCH_ONLY"
    PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class StrategyRuleKind(StrEnum):
    BUY_AND_HOLD = "BUY_AND_HOLD"
    MOVING_AVERAGE_TREND = "MOVING_AVERAGE_TREND"
    TIME_SERIES_MOMENTUM = "TIME_SERIES_MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    INTERNAL_METHOD_REFERENCE = "INTERNAL_METHOD_REFERENCE"


class StrategyHorizonKind(StrEnum):
    DAILY_SIGNAL_INDEFINITE_HOLD = "DAILY_SIGNAL_INDEFINITE_HOLD"
    DAILY_SIGNAL_BOUNDED_HOLD = "DAILY_SIGNAL_BOUNDED_HOLD"


@dataclass(frozen=True, slots=True)
class StrategyRuleParameters:
    """Closed, deterministic parameter bag for one ``StrategyRuleKind``.

    Only the fields relevant to the owning definition's ``rule_kind`` may be
    set; every other field must stay ``None`` (enforced by
    ``_validate_rule``). Every field is a plain ``int``/``Decimal`` so a
    definition stays fully hashable and reproducible -- there is no formula
    or code field anywhere on this type.
    """

    fast_window_days: int | None = None
    slow_window_days: int | None = None
    momentum_lookback_days: int | None = None
    momentum_entry_threshold: Decimal | None = None
    reversion_lookback_days: int | None = None
    reversion_entry_threshold: Decimal | None = None
    maximum_holding_days: int | None = None
    referenced_internal_method_id: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyEvidenceRequirements:
    """What a strategy needs before it may act on an instrument."""

    minimum_history_days: int
    requires_contemporaneous_observation: bool
    maximum_observation_staleness_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class StrategyProvenance:
    """Reference/attribution metadata only -- see module docstring: no field
    here may ever hold a claimed external performance number."""

    reference_kind: str
    reference_description: str
    origin_note: str


_REFERENCE_KINDS = frozenset({"PUBLISHED_CONVENTION", "INTERNAL_RESEARCH_METHOD"})

_RULE_FIELD_NAMES = tuple(field.name for field in fields(StrategyRuleParameters))


def _require_only(params: StrategyRuleParameters, allowed: frozenset[str]) -> None:
    populated = {name for name in _RULE_FIELD_NAMES if getattr(params, name) is not None}
    extra = populated - allowed
    if extra:
        raise StrategyDefinitionError(
            f"unexpected rule parameters set for this rule_kind: {sorted(extra)}"
        )


def _validate_rule(
    kind: StrategyRuleKind,
    params: StrategyRuleParameters,
    source_kind: StrategySourceKind,
) -> None:
    if kind is StrategyRuleKind.INTERNAL_METHOD_REFERENCE:
        if source_kind is not StrategySourceKind.INTERNAL:
            raise StrategyDefinitionError("INTERNAL_METHOD_REFERENCE requires source_kind INTERNAL")
        if not params.referenced_internal_method_id:
            raise StrategyDefinitionError(
                "INTERNAL_METHOD_REFERENCE requires referenced_internal_method_id"
            )
        _require_only(params, frozenset({"referenced_internal_method_id"}))
        return

    if source_kind is not StrategySourceKind.EXTERNAL:
        raise StrategyDefinitionError(f"{kind} requires source_kind EXTERNAL")
    if params.referenced_internal_method_id is not None:
        raise StrategyDefinitionError(
            "referenced_internal_method_id is only valid for INTERNAL_METHOD_REFERENCE"
        )

    if kind is StrategyRuleKind.BUY_AND_HOLD:
        _require_only(params, frozenset())
    elif kind is StrategyRuleKind.MOVING_AVERAGE_TREND:
        if params.fast_window_days is None or params.slow_window_days is None:
            raise StrategyDefinitionError(
                "MOVING_AVERAGE_TREND requires fast_window_days and slow_window_days"
            )
        if params.fast_window_days < 2 or params.slow_window_days < 2:
            raise StrategyDefinitionError("moving-average windows must be at least 2 days")
        if params.fast_window_days >= params.slow_window_days:
            raise StrategyDefinitionError(
                "fast_window_days must be strictly less than slow_window_days"
            )
        _require_only(params, frozenset({"fast_window_days", "slow_window_days"}))
    elif kind is StrategyRuleKind.TIME_SERIES_MOMENTUM:
        if params.momentum_lookback_days is None or params.momentum_entry_threshold is None:
            raise StrategyDefinitionError(
                "TIME_SERIES_MOMENTUM requires momentum_lookback_days and momentum_entry_threshold"
            )
        if params.momentum_lookback_days < 2:
            raise StrategyDefinitionError("momentum_lookback_days must be at least 2 days")
        _require_only(
            params,
            frozenset(
                {"momentum_lookback_days", "momentum_entry_threshold", "maximum_holding_days"}
            ),
        )
    elif kind is StrategyRuleKind.MEAN_REVERSION:
        if params.reversion_lookback_days is None or params.reversion_entry_threshold is None:
            raise StrategyDefinitionError(
                "MEAN_REVERSION requires reversion_lookback_days and reversion_entry_threshold"
            )
        if params.reversion_lookback_days < 2:
            raise StrategyDefinitionError("reversion_lookback_days must be at least 2 days")
        if params.reversion_entry_threshold >= 0:
            raise StrategyDefinitionError(
                "reversion_entry_threshold must be negative (a downside distance from the mean)"
            )
        _require_only(
            params,
            frozenset(
                {"reversion_lookback_days", "reversion_entry_threshold", "maximum_holding_days"}
            ),
        )
    else:  # pragma: no cover - StrEnum is closed; defensive only
        raise StrategyDefinitionError(f"unsupported rule_kind: {kind}")


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_id: str
    version: int
    parent_strategy_id: str | None
    source_kind: StrategySourceKind
    display_name: str
    venue: SharedVenue
    instrument_ids: tuple[str, ...]
    horizon_kind: StrategyHorizonKind
    rule_kind: StrategyRuleKind
    rule_parameters: StrategyRuleParameters
    evidence_requirements: StrategyEvidenceRequirements
    provenance: StrategyProvenance
    lifecycle_state: StrategyLifecycleState
    canonical_payload: Mapping[str, object]
    canonical_hash: str
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        source_kind: StrategySourceKind,
        display_name: str,
        venue: SharedVenue,
        instrument_ids: tuple[str, ...],
        horizon_kind: StrategyHorizonKind,
        rule_kind: StrategyRuleKind,
        rule_parameters: StrategyRuleParameters,
        evidence_requirements: StrategyEvidenceRequirements,
        provenance: StrategyProvenance,
        created_at: datetime,
        version: int = 1,
        parent_strategy_id: str | None = None,
        lifecycle_state: StrategyLifecycleState = StrategyLifecycleState.RESEARCH_ONLY,
    ) -> StrategyDefinition:
        if created_at.tzinfo is None:
            raise StrategyDefinitionError("created_at must be timezone-aware")
        if not display_name.strip():
            raise StrategyDefinitionError("display_name is required")
        if venue is not SharedVenue.CRYPTO:
            # Live Market Observation v1 and the retained daily-candle evidence
            # this milestone reuses are CRYPTO-only today (see docs/OPPORTUNITY_
            # LIFECYCLE.md and the Phase A archaeology this module was built
            # from) -- narrowing here fails closed instead of silently
            # accepting a venue this milestone cannot honestly evaluate.
            raise StrategyDefinitionError("only SharedVenue.CRYPTO is supported by this milestone")
        if not instrument_ids or any(not item.strip() for item in instrument_ids):
            raise StrategyDefinitionError("instrument_ids must be non-empty")
        if len(set(instrument_ids)) != len(instrument_ids):
            raise StrategyDefinitionError("instrument_ids must not contain duplicates")
        if version < 1:
            raise StrategyDefinitionError("version must be >= 1")
        if (version == 1) != (parent_strategy_id is None):
            raise StrategyDefinitionError(
                "version 1 must have no parent; version > 1 requires a parent_strategy_id"
            )
        if (lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY) != (version == 1):
            # Bidirectional: RESEARCH_ONLY if-and-only-if version 1. The prior
            # one-way check (RESEARCH_ONLY only valid at version 1) left the
            # converse open -- a caller could construct a version-1 definition
            # already in PAPER_ELIGIBLE or ACTIVE directly, bypassing every
            # evidence gate below (Recurring Strategy PAPER Orchestration v1,
            # Phase H). Every non-RESEARCH_ONLY state must now be reached
            # through ``next_version`` (or, for ACTIVE, ``strategy_evaluation.
            # promote_to_active``) -- never fabricated at version 1.
            raise StrategyDefinitionError(
                "RESEARCH_ONLY is only valid as, and required for, an initial "
                "(version 1) state -- every other lifecycle_state must be reached "
                "through next_version"
            )
        if provenance.reference_kind not in _REFERENCE_KINDS:
            raise StrategyDefinitionError(
                f"provenance.reference_kind must be one of {sorted(_REFERENCE_KINDS)}"
            )
        if not provenance.reference_description.strip():
            raise StrategyDefinitionError("provenance.reference_description is required")
        expected_reference_kind = (
            "INTERNAL_RESEARCH_METHOD"
            if source_kind is StrategySourceKind.INTERNAL
            else "PUBLISHED_CONVENTION"
        )
        if provenance.reference_kind != expected_reference_kind:
            raise StrategyDefinitionError(
                f"source_kind {source_kind} requires provenance.reference_kind "
                f"{expected_reference_kind}"
            )
        if evidence_requirements.minimum_history_days < 1:
            raise StrategyDefinitionError("minimum_history_days must be positive")
        if (
            evidence_requirements.requires_contemporaneous_observation
            and evidence_requirements.maximum_observation_staleness_seconds is None
        ):
            raise StrategyDefinitionError(
                "requires_contemporaneous_observation needs a "
                "maximum_observation_staleness_seconds bound"
            )
        _validate_rule(rule_kind, rule_parameters, source_kind)

        # strategy_id is a pure function of what the strategy IS -- its
        # source, venue, instruments, and exact rule -- so re-versioning
        # (see next_version below) never changes it; only content that
        # changes the strategy's actual behavior would.
        identity_payload: dict[str, object] = {
            "source_kind": source_kind,
            "venue": venue,
            "instrument_ids": instrument_ids,
            "rule_kind": rule_kind,
            "rule_parameters": rule_parameters,
        }
        strategy_id = deterministic_id("strategy_definition", identity_payload)
        full_payload: dict[str, object] = {
            "strategy_id": strategy_id,
            "version": version,
            "parent_strategy_id": parent_strategy_id,
            "source_kind": source_kind,
            "display_name": display_name,
            "venue": venue,
            "instrument_ids": instrument_ids,
            "horizon_kind": horizon_kind,
            "rule_kind": rule_kind,
            "rule_parameters": rule_parameters,
            "evidence_requirements": evidence_requirements,
            "provenance": provenance,
            "lifecycle_state": lifecycle_state,
        }
        canonical_hash = content_digest(full_payload)
        return cls(
            strategy_id=strategy_id,
            version=version,
            parent_strategy_id=parent_strategy_id,
            source_kind=source_kind,
            display_name=display_name,
            venue=venue,
            instrument_ids=instrument_ids,
            horizon_kind=horizon_kind,
            rule_kind=rule_kind,
            rule_parameters=rule_parameters,
            evidence_requirements=evidence_requirements,
            provenance=provenance,
            lifecycle_state=lifecycle_state,
            canonical_payload=full_payload,
            canonical_hash=canonical_hash,
            created_at=created_at.astimezone(UTC),
        )


def next_version(
    definition: StrategyDefinition,
    *,
    lifecycle_state: StrategyLifecycleState,
    created_at: datetime,
) -> StrategyDefinition:
    """Return a new, immutable version of ``definition`` carrying an updated
    lifecycle state. Never mutates ``definition``. This is a pure, ungated
    version-bump primitive -- callers (see
    ``apex.domain.strategy_evaluation.promote_to_paper_eligible``) are
    responsible for requiring real evidence before calling it. Preserves
    ``strategy_id`` (same underlying strategy) while incrementing
    ``version`` and recording exact parent lineage.
    """

    if lifecycle_state is StrategyLifecycleState.RESEARCH_ONLY:
        raise StrategyDefinitionError("RESEARCH_ONLY is only valid as an initial (version 1) state")
    if lifecycle_state is StrategyLifecycleState.ACTIVE:
        # Strategy Lifecycle Guard (Recurring Strategy PAPER Orchestration v1,
        # Phase H): ``next_version`` is a bare, ungated version-bump primitive
        # (see its own docstring) -- it must never be the path that silently
        # grants ACTIVE. The one deliberate, evidence-gated PAPER_ELIGIBLE ->
        # ACTIVE transition is ``apex.domain.strategy_evaluation.
        # promote_to_active``, which does not call this function (it
        # constructs the next version directly, exactly mirroring this
        # function's own body, so this guard cannot be routed around).
        raise StrategyDefinitionError(
            "next_version never grants ACTIVE directly -- see "
            "apex.domain.strategy_evaluation.promote_to_active for the one "
            "explicit, evidence-gated PAPER_ELIGIBLE -> ACTIVE transition"
        )
    return StrategyDefinition.create(
        source_kind=definition.source_kind,
        display_name=definition.display_name,
        venue=definition.venue,
        instrument_ids=definition.instrument_ids,
        horizon_kind=definition.horizon_kind,
        rule_kind=definition.rule_kind,
        rule_parameters=definition.rule_parameters,
        evidence_requirements=definition.evidence_requirements,
        provenance=definition.provenance,
        created_at=created_at,
        version=definition.version + 1,
        parent_strategy_id=definition.strategy_id,
        lifecycle_state=lifecycle_state,
    )
