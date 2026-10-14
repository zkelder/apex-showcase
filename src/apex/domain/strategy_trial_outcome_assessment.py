"""Strategy PAPER Trial Outcome Assessment v1.

One immutable fact per closed strategy PAPER trial, binding its realized
outcome back to everything that justified it:

    strategy version artifact -> justifying evaluation -> PAPER admission
    (transition) -> trial -> closure -> attribution -> outcome assessment

See ``docs/STRATEGY_TRIAL_OUTCOME_ASSESSMENT_V1.md`` for the reconstruction
this was built from.

Design rules:

* **Evidence only.** Nothing here promotes, demotes, ranks, retires,
  authorizes, sizes, or trades. No status is read by any PAPER, eligibility,
  ranking, promotion, or execution path. ``authority`` is always ``NONE``.
* **No fabricated provenance.** A legacy-adopted trial is classified
  ``PREDATES_STRATEGY_PROVENANCE`` from the immutable flag on its own row.
  It never receives a version, transition, or evaluation, even though its
  economics are assessed in full.
* **No mutable current-strategy lookup.** The artifact, transition, and
  evaluation are the exact retained rows the trial's provenance names. The
  current catalog is never consulted.
* **No invented comparison.** The justifying evaluation's ``net_return`` is
  a backtest return over the whole evaluation window, not a 30-day trial
  expectation. It is stored verbatim with an explicit basis, and no
  expected-minus-realized delta is computed.
* **Independent economics.** Opening/closing economics are re-derived from
  the trial's own retained fills and reconciled against the ledger's
  realized P&L (the closure). The ledger stays authoritative. A disagreement
  beyond rounding is recorded as ``ECONOMICS_UNRECONCILED``, never averaged.
* **Point-in-time sample.** A trial's comparable population contains only
  trials of the exact same artifact/instrument/horizon that closed at or
  before it, so an assessment computed late is byte-identical to one
  computed on time, and a later generation never leaks into an earlier one.
* **Deterministic identity.** ``assessment_id`` is a pure function of
  ``(trial_id, policy_version)``. ``canonical_hash`` covers every evidence
  field and never ``assessed_at``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum

from apex.domain.crypto_paper import CryptoPaperFill, PaperSide
from apex.domain.performance_attribution import PaperEvidenceClass, SharedPerformanceAttribution
from apex.domain.shared_opportunity import STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES
from apex.domain.strategy_evaluation_history import InternalStrategyEvaluationRecord
from apex.domain.strategy_paper_trial import closed_position_attribution_source_hash
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialClosureRecord,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyPaperTrialProvenance,
    StrategyVersionRef,
    StrategyVersionRegistry,
    StrategyVersionTransition,
    TrialProvenanceClass,
    classify_trial_provenance,
)
from apex.domain.values import content_digest, deterministic_id

STRATEGY_TRIAL_OUTCOME_ASSESSMENT_WARNING = (
    "STRATEGY TRIAL OUTCOME ASSESSMENT -- IMMUTABLE OUTCOME EVIDENCE ONLY; "
    "NO PROMOTION, DEMOTION, RANKING, PAPER, OR LIVE AUTHORITY"
)

STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1 = "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1"

#: Comparable, provenance-bound, economically reconciled closed generations
#: required before any population statistic is retained. Reuses the
#: existing statistical-qualification floor rather than inventing a smaller
#: number. Consecutive trials are operationally separated, never claimed
#: statistically independent, so this is a conservative gate only.
STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE = STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES

#: Bounded writes per recurring dispatch. The remainder drains on later ticks.
STRATEGY_TRIAL_OUTCOME_MAX_ASSESSMENTS_PER_CYCLE = 25

#: ``domain.crypto_paper`` floors ``average_cost`` and ceils the removed cost
#: basis to 8 places, so the ledger's realized P&L differs from the pure
#: cash-flow P&L by at most one quote satoshi per base unit (floor) plus one
#: per SELL fill (ceil), plus rounding dust a prior generation may leave in
#: the shared position's cost. The tolerance allows twice the per-unit bound
#: to cover a similarly sized prior generation's dust. Anything larger is a
#: genuine disagreement and is never absorbed.
RECONCILIATION_QUANTUM = Decimal("0.00000001")

RETURN_QUANTUM = Decimal("0.00000001")

REALIZED_RETURN_BASIS = "LEDGER_REALIZED_PAPER_PNL_OVER_OPENING_COST"


class StrategyTrialOutcomeAssessmentError(ValueError):
    """An outcome-assessment input failed an identity or lineage invariant.
    Fails closed; nothing is guessed."""


class StrategyTrialOutcomeStatus(StrEnum):
    PREDATES_STRATEGY_PROVENANCE = "PREDATES_STRATEGY_PROVENANCE"
    PROVENANCE_MISSING = "PROVENANCE_MISSING"
    NO_ECONOMIC_OUTCOME = "NO_ECONOMIC_OUTCOME"
    ECONOMICS_UNRECONCILED = "ECONOMICS_UNRECONCILED"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    ASSESSED = "ASSESSED"


class TrialEconomicsState(StrEnum):
    RECONCILED = "RECONCILED"
    UNRECONCILED = "UNRECONCILED"
    NO_EXPOSURE = "NO_EXPOSURE"


class ExpectedReturnBasis(StrEnum):
    #: The justifying evaluation's compounded backtest net return over its
    #: whole evaluation window. Not a trial-horizon expectation.
    EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED = (
        "EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED"
    )
    #: No justifying evaluation was ever retained for this trial.
    NO_JUSTIFYING_EVALUATION_RETAINED = "NO_JUSTIFYING_EVALUATION_RETAINED"


# ---------------------------------------------------------------- economics


def is_close_attempt_order_id(*, trial_id: str, order_id: str) -> bool:
    """Exactly the order ids ``strategy_paper_trial_lifecycle.
    close_attempt_seed`` can produce for this trial: ``{trial}-close-order``
    (attempt 1) and ``{trial}-close-{N}-order`` for N >= 2. Never a prefix
    match, so another trial's orders can never be mistaken for these."""

    if order_id == f"{trial_id}-close-order":
        return True
    match = re.fullmatch(re.escape(trial_id) + r"-close-([1-9][0-9]*)-order", order_id)
    return match is not None and int(match.group(1)) >= 2


@dataclass(frozen=True, slots=True)
class TrialEconomics:
    state: TrialEconomicsState
    opening_fill_count: int
    opening_base_quantity: Decimal
    opening_quote_quantity: Decimal
    opening_fees: Decimal
    opening_cost: Decimal
    closing_fill_count: int
    closing_base_quantity: Decimal
    closing_quote_quantity: Decimal
    closing_fees: Decimal
    closing_net_proceeds: Decimal
    cash_flow_pnl: Decimal | None
    ledger_realized_pnl: Decimal | None
    rounding_residual: Decimal | None
    reconciliation_tolerance: Decimal
    realized_return: Decimal | None
    reasons: tuple[str, ...]


def derive_trial_economics(
    *,
    trial: StrategyPaperTrialRecord,
    closure: StrategyPaperTrialClosureRecord,
    fills: Sequence[CryptoPaperFill],
) -> TrialEconomics:
    """Pure. ``fills`` may be every fill on the trial's account; only the
    trial's own opening BUY and its own close-attempt SELLs are used."""

    if closure.trial_id != trial.trial_id:
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_CLOSURE_TRIAL_MISMATCH")
    opening = [item for item in fills if item.order_id == trial.opening_order_id]
    closing = [
        item
        for item in fills
        if is_close_attempt_order_id(trial_id=trial.trial_id, order_id=item.order_id)
    ]
    reasons: list[str] = []
    if any(item.side is not PaperSide.BUY for item in opening):
        reasons.append("OPENING_FILL_SIDE_NOT_BUY")
    if any(item.side is not PaperSide.SELL for item in closing):
        reasons.append("CLOSING_FILL_SIDE_NOT_SELL")
    if any(item.product_id != trial.instrument_id for item in (*opening, *closing)):
        reasons.append("FILL_INSTRUMENT_MISMATCH")

    zero = Decimal(0)
    opening_base = sum((item.base_quantity for item in opening), zero)
    opening_quote = sum((item.quote_quantity for item in opening), zero)
    opening_fees = sum((item.fee for item in opening), zero)
    closing_base = sum((item.base_quantity for item in closing), zero)
    closing_quote = sum((item.quote_quantity for item in closing), zero)
    closing_fees = sum((item.fee for item in closing), zero)
    opening_cost = opening_quote + opening_fees
    closing_net = closing_quote - closing_fees
    tolerance = (RECONCILIATION_QUANTUM * (2 * opening_base + len(closing) + 2)).quantize(
        RECONCILIATION_QUANTUM, rounding=ROUND_CEILING
    )

    def result(
        state: TrialEconomicsState,
        *,
        cash_flow: Decimal | None = None,
        residual: Decimal | None = None,
        realized_return: Decimal | None = None,
    ) -> TrialEconomics:
        return TrialEconomics(
            state=state,
            opening_fill_count=len(opening),
            opening_base_quantity=opening_base,
            opening_quote_quantity=opening_quote,
            opening_fees=opening_fees,
            opening_cost=opening_cost,
            closing_fill_count=len(closing),
            closing_base_quantity=closing_base,
            closing_quote_quantity=closing_quote,
            closing_fees=closing_fees,
            closing_net_proceeds=closing_net,
            cash_flow_pnl=cash_flow,
            ledger_realized_pnl=closure.realized_paper_pnl,
            rounding_residual=residual,
            reconciliation_tolerance=tolerance,
            realized_return=realized_return,
            reasons=tuple(sorted(set(reasons))),
        )

    if closure.terminal_reason is StrategyPaperTrialTerminalReason.OPENING_ORDER_EXPIRED_UNFILLED:
        if opening or closing:
            reasons.append("UNFILLED_EXPIRY_HAS_FILLS")
        if closure.realized_paper_pnl is not None:
            reasons.append("UNFILLED_EXPIRY_HAS_REALIZED_PNL")
        return result(
            TrialEconomicsState.UNRECONCILED if reasons else TrialEconomicsState.NO_EXPOSURE
        )

    if not opening:
        reasons.append("OPENING_FILLS_ABSENT")
    if not closing:
        reasons.append("CLOSING_FILLS_ABSENT")
    if not is_close_attempt_order_id(trial_id=trial.trial_id, order_id=closure.closing_order_id):
        reasons.append("CLOSURE_ORDER_NOT_A_CLOSE_ATTEMPT")
    elif not any(item.order_id == closure.closing_order_id for item in closing):
        reasons.append("CLOSURE_ORDER_HAS_NO_FILL")
    if any(item.event_time > closure.closed_at for item in (*opening, *closing)):
        reasons.append("FILL_AFTER_CLOSURE")
    if opening and closing and closing_base != opening_base:
        reasons.append("POSITION_NOT_FLATTENED_BY_TRIAL_FILLS")
    cash_flow = closing_net - opening_cost if opening and closing else None
    residual: Decimal | None = None
    if closure.realized_paper_pnl is None:
        reasons.append("LEDGER_REALIZED_PNL_ABSENT")
    elif cash_flow is not None:
        residual = cash_flow - closure.realized_paper_pnl
        if abs(residual) > tolerance:
            reasons.append("ROUNDING_RESIDUAL_EXCEEDS_TOLERANCE")
    if opening_cost <= 0:
        reasons.append("OPENING_COST_NOT_POSITIVE")
    if reasons or closure.realized_paper_pnl is None:
        return result(TrialEconomicsState.UNRECONCILED, cash_flow=cash_flow, residual=residual)
    realized_return = (closure.realized_paper_pnl / opening_cost).quantize(RETURN_QUANTUM)
    return result(
        TrialEconomicsState.RECONCILED,
        cash_flow=cash_flow,
        residual=residual,
        realized_return=realized_return,
    )


# ---------------------------------------------------------------- lineage


@dataclass(frozen=True, slots=True)
class JustifyingEvaluationEvidence:
    """The exact retained evaluation a trial's admission transition names,
    reduced to the fields an outcome comparison could ever honestly use."""

    evaluation_source: StrategyEvaluationSource
    evaluation_id: str
    evaluation_hash: str
    evaluated_version: int
    as_of: datetime
    evaluation_kind: str
    window_start: datetime | None
    window_end: datetime
    sample_size: int
    net_return: Decimal | None
    average_trade_return: Decimal | None


@dataclass(frozen=True, slots=True)
class TrialVersionLineage:
    version_ref: StrategyVersionRef
    provenance_hash: str
    transition_id: str
    transition_hash: str
    transition_kind: str
    gate_policy_version: str
    evaluation: JustifyingEvaluationEvidence


def build_trial_version_lineage(
    *,
    trial: StrategyPaperTrialRecord,
    provenance: StrategyPaperTrialProvenance,
    transition: StrategyVersionTransition,
    evaluation: InternalStrategyEvaluationRecord,
) -> TrialVersionLineage:
    """Bind a BOUND trial to the exact artifact, transition, and evaluation
    its own provenance row names. Every link is checked by id *and* hash."""

    if classify_trial_provenance(trial=trial, provenance=provenance) is not (
        TrialProvenanceClass.BOUND
    ):
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_TRIAL_NOT_BOUND")
    if provenance.transition_id != transition.transition_id:
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_TRANSITION_MISMATCH")
    if transition.to_ref != provenance.version_ref:
        raise StrategyTrialOutcomeAssessmentError(
            "STRATEGY_TRIAL_OUTCOME_TRANSITION_ARTIFACT_MISMATCH"
        )
    if transition.evaluation_source is not StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION:
        raise StrategyTrialOutcomeAssessmentError(
            "STRATEGY_TRIAL_OUTCOME_EVALUATION_SOURCE_UNSUPPORTED"
        )
    if (
        evaluation.evaluation_id != transition.evaluation_id
        or evaluation.canonical_hash != transition.evaluation_hash
    ):
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_EVALUATION_MISMATCH")
    if (
        evaluation.strategy_id != transition.from_ref.strategy_id
        or evaluation.strategy_version != transition.from_ref.version
        or evaluation.candidate.strategy_id != evaluation.strategy_id
        or evaluation.candidate.strategy_version != evaluation.strategy_version
    ):
        raise StrategyTrialOutcomeAssessmentError(
            "STRATEGY_TRIAL_OUTCOME_EVALUATION_SUBJECT_MISMATCH"
        )
    candidate = evaluation.candidate
    return TrialVersionLineage(
        version_ref=provenance.version_ref,
        provenance_hash=provenance.canonical_hash,
        transition_id=transition.transition_id,
        transition_hash=transition.canonical_hash,
        transition_kind=transition.kind.value,
        gate_policy_version=transition.gate_policy_version,
        evaluation=JustifyingEvaluationEvidence(
            evaluation_source=transition.evaluation_source,
            evaluation_id=evaluation.evaluation_id,
            evaluation_hash=evaluation.canonical_hash,
            evaluated_version=evaluation.strategy_version,
            as_of=evaluation.as_of,
            evaluation_kind=candidate.evaluation_kind.value,
            window_start=candidate.window_start,
            window_end=candidate.window_end,
            sample_size=candidate.sample_size,
            net_return=candidate.net_return,
            average_trade_return=candidate.average_trade_return,
        ),
    )


@dataclass(frozen=True, slots=True)
class TrialAttributionLineage:
    opening_attribution_id: str
    opening_attribution_hash: str
    closing_attribution_id: str | None
    closing_attribution_hash: str | None


def expected_final_attribution_sources(
    *,
    trial: StrategyPaperTrialRecord,
    closure: StrategyPaperTrialClosureRecord,
    opening_order_hash: str,
    closing_order_hash: str | None,
) -> tuple[tuple[str, str], tuple[str, str] | None]:
    """The exact ``(source_id, source_hash)`` of the final attribution rows
    for a closed trial: the superseding ``POSITION_CLOSED`` rows of the
    opening BUY and the closing SELL after a horizon exit, or the opening
    BUY's own terminal row after an unfilled expiry (no closing order)."""

    if closure.terminal_reason is StrategyPaperTrialTerminalReason.OPENING_ORDER_EXPIRED_UNFILLED:
        return (trial.opening_order_id, opening_order_hash), None
    if closing_order_hash is None:
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_CLOSING_ORDER_ABSENT")
    return (
        (trial.opening_order_id, closed_position_attribution_source_hash(opening_order_hash)),
        (closure.closing_order_id, closed_position_attribution_source_hash(closing_order_hash)),
    )


def build_trial_attribution_lineage(
    *,
    trial: StrategyPaperTrialRecord,
    opening: SharedPerformanceAttribution,
    closing: SharedPerformanceAttribution | None,
    expected_opening_source: tuple[str, str],
    expected_closing_source: tuple[str, str] | None,
) -> TrialAttributionLineage:
    """Checks that each attribution row is exactly the expected final row
    for this trial -- same source identity, same strategy, genuine PAPER,
    and ``CLOSED`` -- before its id and hash become lineage."""

    def check(item: SharedPerformanceAttribution, source: tuple[str, str]) -> None:
        if (item.source_id, item.source_hash) != source:
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_ATTRIBUTION_SOURCE_MISMATCH"
            )
        if item.strategy_or_method_id != trial.strategy_id:
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_ATTRIBUTION_STRATEGY_MISMATCH"
            )
        if item.evidence_class is not PaperEvidenceClass.GENUINE_PAPER:
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_ATTRIBUTION_EVIDENCE_CLASS_INVALID"
            )
        if item.position_state != "CLOSED":
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_ATTRIBUTION_NOT_CLOSED"
            )

    check(opening, expected_opening_source)
    if (closing is None) != (expected_closing_source is None):
        raise StrategyTrialOutcomeAssessmentError(
            "STRATEGY_TRIAL_OUTCOME_CLOSING_ATTRIBUTION_SHAPE_INVALID"
        )
    if closing is not None and expected_closing_source is not None:
        check(closing, expected_closing_source)
    return TrialAttributionLineage(
        opening_attribution_id=opening.attribution_id,
        opening_attribution_hash=opening.canonical_hash,
        closing_attribution_id=None if closing is None else closing.attribution_id,
        closing_attribution_hash=None if closing is None else closing.canonical_hash,
    )


# ---------------------------------------------------------------- facts and sample


@dataclass(frozen=True, slots=True)
class ClosedTrialFacts:
    """Everything retained about one closed trial that an assessment, or a
    later trial's population, may use. ``lineage`` is present exactly when
    ``provenance_class`` is ``BOUND``."""

    trial: StrategyPaperTrialRecord
    closure: StrategyPaperTrialClosureRecord
    provenance_class: TrialProvenanceClass
    lineage: TrialVersionLineage | None
    economics: TrialEconomics

    def __post_init__(self) -> None:
        if self.closure.trial_id != self.trial.trial_id:
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_CLOSURE_TRIAL_MISMATCH"
            )
        if (self.provenance_class is TrialProvenanceClass.BOUND) != (self.lineage is not None):
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_LINEAGE_SHAPE_INVALID"
            )
        if self.trial.legacy_adopted != (
            self.provenance_class is TrialProvenanceClass.PREDATES_STRATEGY_PROVENANCE
        ):
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_LEGACY_CLASSIFICATION_INVALID"
            )


def comparable_population_key(facts: ClosedTrialFacts) -> tuple[object, ...] | None:
    """The exact artifact on the exact instrument and PAPER account under the
    exact trial horizon and lifecycle policy. ``None`` for any trial without
    bound provenance: it has no artifact to be comparable on."""

    if facts.lineage is None:
        return None
    return (
        facts.lineage.version_ref.as_tuple(),
        facts.trial.account_id,
        facts.trial.instrument_id,
        facts.trial.horizon_days,
        facts.trial.policy_version,
    )


def is_sample_member(facts: ClosedTrialFacts) -> bool:
    return (
        facts.provenance_class is TrialProvenanceClass.BOUND
        and facts.closure.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT
        and facts.economics.state is TrialEconomicsState.RECONCILED
        and facts.economics.realized_return is not None
    )


def _order_key(facts: ClosedTrialFacts) -> tuple[datetime, str]:
    return (facts.closure.closed_at, facts.trial.trial_id)


@dataclass(frozen=True, slots=True)
class SamplePopulation:
    population_id: str
    members: tuple[tuple[str, str, Decimal], ...]
    members_hash: str
    size: int
    minimum_size: int
    mean_realized_return: Decimal | None
    positive_count: int | None
    total_realized_pnl: Decimal | None


def select_sample_population(
    *, subject: ClosedTrialFacts, candidates: Sequence[ClosedTrialFacts]
) -> SamplePopulation | None:
    """Pure and point-in-time. ``None`` for a subject with no artifact.
    Members are the reconciled horizon-exit trials of the subject's exact
    key that closed at or before the subject; statistics are withheld below
    the minimum. ``candidates`` may include the subject and trials closed
    later -- later ones are excluded by construction."""

    key = comparable_population_key(subject)
    if key is None:
        return None
    cutoff = _order_key(subject)
    by_trial: dict[str, ClosedTrialFacts] = {}
    for item in (*candidates, subject):
        existing = by_trial.get(item.trial.trial_id)
        if existing is not None and existing != item:
            raise StrategyTrialOutcomeAssessmentError(
                "STRATEGY_TRIAL_OUTCOME_POPULATION_FACTS_CONFLICT"
            )
        by_trial[item.trial.trial_id] = item
    members = sorted(
        (
            item
            for item in by_trial.values()
            if comparable_population_key(item) == key
            and is_sample_member(item)
            and _order_key(item) <= cutoff
        ),
        key=_order_key,
    )
    rows = tuple(
        (
            item.trial.trial_id,
            item.closure.canonical_hash,
            item.economics.realized_return,
        )
        for item in members
        if item.economics.realized_return is not None
    )
    size = len(rows)
    qualified = size >= STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE
    returns = [row[2] for row in rows]
    pnls = [
        item.economics.ledger_realized_pnl
        for item in members
        if item.economics.ledger_realized_pnl is not None
    ]
    return SamplePopulation(
        population_id=deterministic_id(
            "strategy_trial_outcome_population", (STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1, key)
        ),
        members=rows,
        members_hash=content_digest(rows),
        size=size,
        minimum_size=STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE,
        mean_realized_return=(
            (sum(returns, Decimal(0)) / size).quantize(RETURN_QUANTUM) if qualified else None
        ),
        positive_count=sum(1 for value in returns if value > 0) if qualified else None,
        total_realized_pnl=sum(pnls, Decimal(0)) if qualified else None,
    )


# ---------------------------------------------------------------- assessment


@dataclass(frozen=True, slots=True)
class StrategyTrialOutcomeAssessment:
    assessment_id: str
    policy_version: str
    status: StrategyTrialOutcomeStatus
    reason_codes: tuple[str, ...]
    # trial
    trial_id: str
    trial_hash: str
    strategy_id: str
    strategy_version: int
    instrument_id: str
    account_id: str
    generation: int
    trial_legacy_adopted: bool
    trial_policy_version: str
    horizon_days: int
    opened_at: datetime
    # closure
    closure_id: str
    closure_hash: str
    terminal_reason: StrategyPaperTrialTerminalReason
    closed_at: datetime
    # provenance (BOUND only)
    provenance_class: TrialProvenanceClass
    version_registry: StrategyVersionRegistry | None
    definition_hash: str | None
    provenance_hash: str | None
    transition_id: str | None
    transition_hash: str | None
    transition_kind: str | None
    gate_policy_version: str | None
    # justifying evaluation (BOUND only)
    evaluation_source: StrategyEvaluationSource | None
    evaluation_id: str | None
    evaluation_hash: str | None
    evaluated_version: int | None
    evaluation_as_of: datetime | None
    evaluation_kind: str | None
    evaluation_window_start: datetime | None
    evaluation_window_end: datetime | None
    evaluation_sample_size: int | None
    evaluation_net_return: Decimal | None
    evaluation_average_trade_return: Decimal | None
    expected_return_basis: ExpectedReturnBasis
    # attribution
    opening_attribution_id: str
    opening_attribution_hash: str
    closing_attribution_id: str | None
    closing_attribution_hash: str | None
    # economics
    economics_state: TrialEconomicsState
    opening_fill_count: int
    opening_base_quantity: Decimal
    opening_quote_quantity: Decimal
    opening_fees: Decimal
    opening_cost: Decimal
    closing_fill_count: int
    closing_base_quantity: Decimal
    closing_quote_quantity: Decimal
    closing_fees: Decimal
    closing_net_proceeds: Decimal
    cash_flow_pnl: Decimal | None
    ledger_realized_pnl: Decimal | None
    rounding_residual: Decimal | None
    reconciliation_tolerance: Decimal
    realized_return: Decimal | None
    realized_return_basis: str
    # sample
    population_id: str | None
    population_members_hash: str | None
    population_size: int
    minimum_sample_size: int
    population_mean_realized_return: Decimal | None
    population_positive_count: int | None
    population_total_realized_pnl: Decimal | None
    canonical_hash: str
    authority: str = "NONE"


_HASH_EXCLUDED = frozenset({"canonical_hash", "authority"})


def strategy_trial_outcome_assessment_id(*, trial_id: str, policy_version: str) -> str:
    return deterministic_id("strategy_trial_outcome_assessment", (trial_id, policy_version))


def _content_hash(assessment: StrategyTrialOutcomeAssessment) -> str:
    return content_digest(
        {
            name: getattr(assessment, name)
            for name in StrategyTrialOutcomeAssessment.__dataclass_fields__
            if name not in _HASH_EXCLUDED
        }
    )


def verify_strategy_trial_outcome_assessment(
    assessment: StrategyTrialOutcomeAssessment,
) -> StrategyTrialOutcomeAssessment:
    """Re-derive identity and hash from content; used on every read."""

    if assessment.assessment_id != strategy_trial_outcome_assessment_id(
        trial_id=assessment.trial_id, policy_version=assessment.policy_version
    ):
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_ASSESSMENT_ID_MISMATCH")
    if assessment.authority != "NONE":
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_AUTHORITY_INVALID")
    if assessment.canonical_hash != _content_hash(assessment):
        raise StrategyTrialOutcomeAssessmentError("STRATEGY_TRIAL_OUTCOME_ASSESSMENT_HASH_MISMATCH")
    return assessment


def _status(
    subject: ClosedTrialFacts, population: SamplePopulation | None
) -> StrategyTrialOutcomeStatus:
    if subject.provenance_class is TrialProvenanceClass.PREDATES_STRATEGY_PROVENANCE:
        return StrategyTrialOutcomeStatus.PREDATES_STRATEGY_PROVENANCE
    if subject.provenance_class is TrialProvenanceClass.PROVENANCE_MISSING:
        return StrategyTrialOutcomeStatus.PROVENANCE_MISSING
    if subject.economics.state is TrialEconomicsState.NO_EXPOSURE:
        return StrategyTrialOutcomeStatus.NO_ECONOMIC_OUTCOME
    if subject.economics.state is TrialEconomicsState.UNRECONCILED:
        return StrategyTrialOutcomeStatus.ECONOMICS_UNRECONCILED
    if population is None or population.size < STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE:
        return StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE
    return StrategyTrialOutcomeStatus.ASSESSED


def build_strategy_trial_outcome_assessment(
    *,
    subject: ClosedTrialFacts,
    candidates: Sequence[ClosedTrialFacts],
    attribution: TrialAttributionLineage,
) -> StrategyTrialOutcomeAssessment:
    """Pure constructor. ``candidates`` are every retained closed trial's
    facts (any order, any closing time): the population is selected
    point-in-time from them."""

    trial, closure, economics, lineage = (
        subject.trial,
        subject.closure,
        subject.economics,
        subject.lineage,
    )
    population = select_sample_population(subject=subject, candidates=candidates)
    status = _status(subject, population)
    is_sample = status in {
        StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE,
        StrategyTrialOutcomeStatus.ASSESSED,
    }
    evaluation = None if lineage is None else lineage.evaluation
    reasons = set(economics.reasons)
    if lineage is None:
        reasons.add("NO_STRATEGY_VERSION_BINDING")
    else:
        reasons.add("EXPECTED_RETURN_NOT_HORIZON_MATCHED_NO_DELTA_COMPUTED")
    if status is StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE:
        reasons.add("POPULATION_BELOW_MINIMUM_SAMPLE")
    reasons.add("NO_STRATEGY_LEVEL_VERDICT_V1")
    draft = StrategyTrialOutcomeAssessment(
        assessment_id=strategy_trial_outcome_assessment_id(
            trial_id=trial.trial_id, policy_version=STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1
        ),
        policy_version=STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1,
        status=status,
        reason_codes=tuple(sorted(reasons)),
        trial_id=trial.trial_id,
        trial_hash=trial.canonical_hash,
        strategy_id=trial.strategy_id,
        strategy_version=trial.strategy_version,
        instrument_id=trial.instrument_id,
        account_id=trial.account_id,
        generation=trial.generation,
        trial_legacy_adopted=trial.legacy_adopted,
        trial_policy_version=trial.policy_version,
        horizon_days=trial.horizon_days,
        opened_at=trial.opened_at,
        closure_id=closure.closure_id,
        closure_hash=closure.canonical_hash,
        terminal_reason=closure.terminal_reason,
        closed_at=closure.closed_at,
        provenance_class=subject.provenance_class,
        version_registry=None if lineage is None else lineage.version_ref.registry,
        definition_hash=None if lineage is None else lineage.version_ref.definition_hash,
        provenance_hash=None if lineage is None else lineage.provenance_hash,
        transition_id=None if lineage is None else lineage.transition_id,
        transition_hash=None if lineage is None else lineage.transition_hash,
        transition_kind=None if lineage is None else lineage.transition_kind,
        gate_policy_version=None if lineage is None else lineage.gate_policy_version,
        evaluation_source=None if evaluation is None else evaluation.evaluation_source,
        evaluation_id=None if evaluation is None else evaluation.evaluation_id,
        evaluation_hash=None if evaluation is None else evaluation.evaluation_hash,
        evaluated_version=None if evaluation is None else evaluation.evaluated_version,
        evaluation_as_of=None if evaluation is None else evaluation.as_of,
        evaluation_kind=None if evaluation is None else evaluation.evaluation_kind,
        evaluation_window_start=None if evaluation is None else evaluation.window_start,
        evaluation_window_end=None if evaluation is None else evaluation.window_end,
        evaluation_sample_size=None if evaluation is None else evaluation.sample_size,
        evaluation_net_return=None if evaluation is None else evaluation.net_return,
        evaluation_average_trade_return=(
            None if evaluation is None else evaluation.average_trade_return
        ),
        expected_return_basis=(
            ExpectedReturnBasis.NO_JUSTIFYING_EVALUATION_RETAINED
            if evaluation is None
            else ExpectedReturnBasis.EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED
        ),
        opening_attribution_id=attribution.opening_attribution_id,
        opening_attribution_hash=attribution.opening_attribution_hash,
        closing_attribution_id=attribution.closing_attribution_id,
        closing_attribution_hash=attribution.closing_attribution_hash,
        economics_state=economics.state,
        opening_fill_count=economics.opening_fill_count,
        opening_base_quantity=economics.opening_base_quantity,
        opening_quote_quantity=economics.opening_quote_quantity,
        opening_fees=economics.opening_fees,
        opening_cost=economics.opening_cost,
        closing_fill_count=economics.closing_fill_count,
        closing_base_quantity=economics.closing_base_quantity,
        closing_quote_quantity=economics.closing_quote_quantity,
        closing_fees=economics.closing_fees,
        closing_net_proceeds=economics.closing_net_proceeds,
        cash_flow_pnl=economics.cash_flow_pnl,
        ledger_realized_pnl=economics.ledger_realized_pnl,
        rounding_residual=economics.rounding_residual,
        reconciliation_tolerance=economics.reconciliation_tolerance,
        realized_return=economics.realized_return,
        realized_return_basis=REALIZED_RETURN_BASIS,
        population_id=None if population is None else population.population_id,
        population_members_hash=None if population is None else population.members_hash,
        population_size=0 if population is None else population.size,
        minimum_sample_size=STRATEGY_TRIAL_OUTCOME_MINIMUM_SAMPLE_SIZE,
        # Peer statistics are retained only on a row that is itself a sample
        # member (Fable review #2): an unreconciled or no-exposure trial
        # records its population size, never its peers' statistics.
        population_mean_realized_return=(
            population.mean_realized_return if population is not None and is_sample else None
        ),
        population_positive_count=(
            population.positive_count if population is not None and is_sample else None
        ),
        population_total_realized_pnl=(
            population.total_realized_pnl if population is not None and is_sample else None
        ),
        canonical_hash="",
    )
    return _with_hash(draft)


def _with_hash(draft: StrategyTrialOutcomeAssessment) -> StrategyTrialOutcomeAssessment:
    return replace(draft, canonical_hash=_content_hash(draft))
