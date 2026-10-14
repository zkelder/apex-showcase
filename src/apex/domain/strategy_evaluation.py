"""Unifies INTERNAL and EXTERNAL strategy evidence into one comparable shape.

Three adapters populate the same ``StrategyEvaluationResult`` from three
genuinely different evidence sources, deliberately without re-deriving any
of them -- this module never re-simulates or re-aggregates evidence another
module already owns (see the Phase A archaeology this was built from):

- ``evaluate_external_strategy`` adapts a fresh
  ``strategy_benchmark.StrategyBacktestResult`` -- every field is
  genuinely computed; nothing is unavailable.
- ``evaluate_internal_strategy_from_health`` adapts an existing
  ``performance_attribution.StrategyHealthAssessment`` -- Apex's own real
  PAPER-trial-level evidence for an internal method, when any exists.
  Several EXTERNAL-only fields (gross return distinct from net, win/loss
  rate, average/median trade return, turnover) are not computed by that
  module and are honestly reported as unavailable (see
  ``unavailable_fields``) rather than invented.
- ``evaluate_internal_strategy_from_counterfactual_outcome`` adapts an
  existing ``counterfactual_outcome.CounterfactualOutcomeAggregate`` --
  Apex's real retained decision-acceptance evidence for an internal
  method, available even where zero PAPER trials have ever executed. This
  is decision-level counterfactual evidence (would the subsequent price
  move have been favorable), not realized trade P&L -- labeled as such via
  ``evaluation_kind`` and ``unavailable_fields``, never conflated with a
  genuine trade outcome.

``promote_to_paper_eligible`` is one of two deliberate, evidence-gated
lifecycle transitions this module implements: it requires a real
``StrategyEvaluationResult`` with adequate sample size and a strictly
positive net return before calling
``strategy_definition.next_version``. Callers: the operator-invoked
``PromoteExternalStrategy``/``PromoteInternalStrategy``, and the recurring
cycle's per-day in-memory PAPER admission gate
(``adapters.operating_cycle_tasks._execute_strategy_paper_trial``). That gate
never durably changes lifecycle state. Since Strategy Version Provenance v1,
every call that trades or promotes retains an immutable
``strategy_version_transition`` binding the exact evaluated and resulting
artifacts (``domain.strategy_version_provenance``).

``promote_to_active`` (Recurring Strategy PAPER Orchestration v1, Phase H
Strategy Lifecycle Guard) is the second: the one explicit, evidence-gated
PAPER_ELIGIBLE -> ACTIVE transition, mirroring ``promote_to_paper_eligible``'s
exact shape and threshold. ``strategy_definition.next_version`` itself now
refuses ``ACTIVE`` directly, so this is the only path there. ACTIVE still
grants no LIVE authority whatsoever -- ``lifecycle_state`` is not read by
``domain.crypto_paper``/``domain.kalshi_operating``/``application.
strategy_paper_trial`` today, and this module adds no new caller that reads
it either.

``compare_strategy_evaluations`` is a pure, descriptive delta comparison
between any two results (e.g. an internal strategy vs an external
benchmark -- comparing across source kinds is the primary intended use,
not an edge case) -- generalizes the existing ``RetainedPriorComparison``
delta-comparison pattern (``crypto_challenger.py``) across source kinds.
Descriptive only: it never promotes, authorizes, or mutates anything,
matching ``StrategyHealthAssessment``'s existing "descriptive operator
evidence only" doctrine. Its Comparability Guard (Phase I) returns
``ComparisonVerdict.NON_COMPARABLE`` -- never a silently-ranked delta --
when the two sides' covered windows provably do not overlap. It
deliberately does NOT gate on ``evaluation_kind`` matching: ``net_return``
is already the honest common unit across all three adapters (a real
percentage for ``EXTERNAL_BACKTEST``/``INTERNAL_PAPER_ATTRIBUTION``, or
``None`` for the decision-level ``INTERNAL_COUNTERFACTUAL_COHORT``, which
never claims a return figure) -- a missing ``net_return`` on either side
already and honestly falls through to
``ComparisonVerdict.INCONCLUSIVE_INSUFFICIENT_EVIDENCE`` below, so a
separate identity gate would only ever block the cross-kind comparisons
this function exists to make.

Authority ``NONE`` throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.counterfactual_outcome import CounterfactualOutcomeAggregate
from apex.domain.crypto_research import RESEARCH_METRIC_QUANTUM
from apex.domain.performance_attribution import StrategyHealthAssessment
from apex.domain.strategy_benchmark import StrategyBacktestResult
from apex.domain.strategy_definition import (
    MINIMUM_PROMOTION_SAMPLE_SIZE,
    StrategyDefinition,
    StrategyLifecycleState,
    StrategySourceKind,
    next_version,
)
from apex.domain.values import content_digest

STRATEGY_EVALUATION_WARNING = "STRATEGY EVALUATION -- COMPARISON EVIDENCE ONLY, NOT LIVE AUTHORITY"


class StrategyEvaluationError(ValueError):
    """A strategy evaluation, promotion, or comparison violated an evidence invariant."""


class StrategyEvaluationKind(StrEnum):
    EXTERNAL_BACKTEST = "EXTERNAL_BACKTEST"
    INTERNAL_PAPER_ATTRIBUTION = "INTERNAL_PAPER_ATTRIBUTION"
    INTERNAL_COUNTERFACTUAL_COHORT = "INTERNAL_COUNTERFACTUAL_COHORT"


class ComparisonVerdict(StrEnum):
    CANDIDATE_OUTPERFORMS_BENCHMARK = "CANDIDATE_OUTPERFORMS_BENCHMARK"
    BENCHMARK_OUTPERFORMS_CANDIDATE = "BENCHMARK_OUTPERFORMS_CANDIDATE"
    INCONCLUSIVE_INSUFFICIENT_EVIDENCE = "INCONCLUSIVE_INSUFFICIENT_EVIDENCE"
    # Comparability Guard (Recurring Strategy PAPER Orchestration v1, Phase
    # I): a deterministic period/evidence-basis mismatch makes any return
    # delta meaningless, not merely "inconclusive" -- see
    # ``compare_strategy_evaluations``'s own comparability gate below.
    NON_COMPARABLE = "NON_COMPARABLE"


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(RESEARCH_METRIC_QUANTUM)


@dataclass(frozen=True, slots=True)
class StrategyEvaluationResult:
    strategy_id: str
    strategy_version: int
    source_kind: StrategySourceKind
    evaluation_kind: StrategyEvaluationKind
    benchmark_identity: str
    window_start: datetime | None
    window_end: datetime
    candidate_count: int
    trade_count: int
    abstained_count: int
    sample_size: int
    win_count: int | None
    loss_count: int | None
    win_rate: Decimal | None
    average_trade_return: Decimal | None
    median_trade_return: Decimal | None
    gross_return: Decimal | None
    net_return: Decimal | None
    maximum_drawdown: Decimal | None
    turnover: Decimal | None
    total_fees: Decimal | None
    total_slippage: Decimal | None
    total_spread: Decimal | None
    fee_bps: Decimal | None
    spread_bps: Decimal | None
    slippage_bps: Decimal | None
    evidence_class: str
    evidence_provenance: str
    unavailable_fields: tuple[str, ...]
    canonical_hash: str


def evaluate_external_strategy(
    definition: StrategyDefinition, backtest: StrategyBacktestResult
) -> StrategyEvaluationResult:
    if definition.source_kind is not StrategySourceKind.EXTERNAL:
        raise StrategyEvaluationError("evaluate_external_strategy requires an EXTERNAL definition")
    mismatched_id = definition.strategy_id != backtest.strategy_id
    mismatched_version = definition.version != backtest.strategy_version
    if mismatched_id or mismatched_version:
        raise StrategyEvaluationError(
            "backtest strategy_id/strategy_version does not match the given definition"
        )
    payload = (
        definition.strategy_id,
        definition.version,
        "EXTERNAL_BACKTEST",
        backtest.canonical_hash,
    )
    return StrategyEvaluationResult(
        strategy_id=definition.strategy_id,
        strategy_version=definition.version,
        source_kind=StrategySourceKind.EXTERNAL,
        evaluation_kind=StrategyEvaluationKind.EXTERNAL_BACKTEST,
        benchmark_identity=definition.rule_kind.value,
        window_start=backtest.window_start,
        window_end=backtest.window_end,
        candidate_count=backtest.candidate_count,
        trade_count=backtest.trade_count,
        abstained_count=backtest.abstained_day_count,
        sample_size=backtest.sample_size,
        win_count=backtest.win_count,
        loss_count=backtest.loss_count,
        win_rate=backtest.win_rate,
        average_trade_return=backtest.average_trade_return,
        median_trade_return=backtest.median_trade_return,
        gross_return=backtest.gross_return,
        net_return=backtest.net_return,
        maximum_drawdown=backtest.maximum_drawdown,
        turnover=backtest.turnover,
        total_fees=backtest.total_fees,
        total_slippage=backtest.total_slippage,
        total_spread=backtest.total_spread,
        fee_bps=backtest.fee_bps,
        spread_bps=backtest.spread_bps,
        slippage_bps=backtest.slippage_bps,
        evidence_class=backtest.evidence_class.value,
        evidence_provenance=backtest.evidence_provenance,
        unavailable_fields=(),
        canonical_hash=content_digest(payload),
    )


_HEALTH_UNAVAILABLE_FIELDS = (
    "gross_return: performance_attribution.py records only net return",
    "win_count/loss_count/win_rate: not computed by StrategyHealthAssessment",
    "average_trade_return/median_trade_return: realized_net_return is a "
    "cumulative sum across trials, not a per-trade figure",
    "turnover: not computed by StrategyHealthAssessment",
    "spread_bps/slippage_bps: only realized fee_variance/slippage_variance "
    "(estimate-vs-actual deltas) are recorded, not the assumptions used",
)


def evaluate_internal_strategy_from_health(
    definition: StrategyDefinition, assessment: StrategyHealthAssessment
) -> StrategyEvaluationResult:
    if definition.source_kind is not StrategySourceKind.INTERNAL:
        raise StrategyEvaluationError(
            "evaluate_internal_strategy_from_health requires an INTERNAL definition"
        )
    if definition.rule_parameters.referenced_internal_method_id != assessment.strategy_or_method_id:
        raise StrategyEvaluationError(
            "assessment.strategy_or_method_id does not match this "
            "definition's referenced_internal_method_id"
        )
    payload = (
        definition.strategy_id,
        definition.version,
        "INTERNAL_PAPER_ATTRIBUTION",
        assessment.canonical_hash,
    )
    return StrategyEvaluationResult(
        strategy_id=definition.strategy_id,
        strategy_version=definition.version,
        source_kind=StrategySourceKind.INTERNAL,
        evaluation_kind=StrategyEvaluationKind.INTERNAL_PAPER_ATTRIBUTION,
        benchmark_identity=assessment.strategy_or_method_id,
        window_start=assessment.window_start,
        window_end=assessment.window_end,
        candidate_count=assessment.paper_trial_count,
        trade_count=assessment.fill_count,
        abstained_count=max(assessment.paper_trial_count - assessment.fill_count, 0),
        sample_size=assessment.settled_outcome_count,
        win_count=None,
        loss_count=None,
        win_rate=None,
        average_trade_return=None,
        median_trade_return=None,
        gross_return=None,
        net_return=assessment.realized_net_return,
        maximum_drawdown=assessment.maximum_drawdown,
        turnover=None,
        total_fees=None,
        total_slippage=None,
        total_spread=None,
        fee_bps=None,
        spread_bps=None,
        slippage_bps=None,
        evidence_class=assessment.evidence_class.value,
        evidence_provenance=assessment.health_id,
        unavailable_fields=_HEALTH_UNAVAILABLE_FIELDS,
        canonical_hash=content_digest(payload),
    )


_COUNTERFACTUAL_UNAVAILABLE_FIELDS = (
    "trade_count/win_count/loss_count: this cohort is decision-level, not "
    "trade-level -- no PAPER fill ever executed; win/loss here means "
    "whether the SUBSEQUENT price move would have been favorable, not a "
    "realized trade outcome",
    "gross_return vs net_return: mean/median_subsequent_return is a raw "
    "price-movement return, not cost-adjusted -- see average/median_trade_return instead",
    "maximum_drawdown/turnover/fees/spread/slippage: this cohort never reached execution",
)


def evaluate_internal_strategy_from_counterfactual_outcome(
    definition: StrategyDefinition, aggregate: CounterfactualOutcomeAggregate
) -> StrategyEvaluationResult:
    if definition.source_kind is not StrategySourceKind.INTERNAL:
        raise StrategyEvaluationError(
            "evaluate_internal_strategy_from_counterfactual_outcome requires an INTERNAL definition"
        )
    if definition.rule_parameters.referenced_internal_method_id != aggregate.strategy_or_method_id:
        raise StrategyEvaluationError(
            "aggregate.strategy_or_method_id does not match this "
            "definition's referenced_internal_method_id"
        )
    if aggregate.specificity is not None:
        # Counterfactual Outcome Evidence Specificity v3: a V3 aggregate's
        # statistic is a peer-relative gate-complement spread over
        # independent windows -- not a per-trade return, win rate, or
        # sample of trades -- so it must never be silently adapted into a
        # ``StrategyEvaluationResult`` whose fields mean exactly those.
        raise StrategyEvaluationError("RELATIVE_RETURN_IS_NOT_A_TRADE_RETURN")
    payload = (
        definition.strategy_id,
        definition.version,
        "INTERNAL_COUNTERFACTUAL_COHORT",
        aggregate.canonical_hash,
    )
    return StrategyEvaluationResult(
        strategy_id=definition.strategy_id,
        strategy_version=definition.version,
        source_kind=StrategySourceKind.INTERNAL,
        evaluation_kind=StrategyEvaluationKind.INTERNAL_COUNTERFACTUAL_COHORT,
        benchmark_identity=f"{aggregate.strategy_or_method_id}:{aggregate.reason_family.value}",
        window_start=None,
        window_end=aggregate.generated_at,
        candidate_count=aggregate.total_observations_considered,
        trade_count=0,
        abstained_count=(
            aggregate.evidence_ineligible_observation_count + aggregate.unassessed_observation_count
        ),
        sample_size=aggregate.eligible_observation_count,
        win_count=aggregate.positive_outcome_count,
        loss_count=aggregate.negative_outcome_count,
        win_rate=aggregate.positive_rate,
        average_trade_return=aggregate.mean_subsequent_return,
        median_trade_return=aggregate.median_subsequent_return,
        gross_return=None,
        net_return=None,
        maximum_drawdown=None,
        turnover=None,
        total_fees=None,
        total_slippage=None,
        total_spread=None,
        fee_bps=None,
        spread_bps=None,
        slippage_bps=None,
        evidence_class="RETAINED_COUNTERFACTUAL_EVIDENCE",
        evidence_provenance=aggregate.aggregate_id,
        unavailable_fields=_COUNTERFACTUAL_UNAVAILABLE_FIELDS,
        canonical_hash=content_digest(payload),
    )


def promote_to_paper_eligible(
    definition: StrategyDefinition,
    *,
    evaluation: StrategyEvaluationResult,
    promoted_at: datetime,
) -> StrategyDefinition:
    """The one deliberate, evidence-gated RESEARCH_ONLY -> PAPER_ELIGIBLE
    transition this milestone implements. Requires a real evaluation with
    adequate sample size and a strictly positive net return, and requires the
    evaluation to be of this exact version -- see module docstring for every
    caller, including the recurring cycle's in-memory admission gate."""

    if evaluation.strategy_id != definition.strategy_id:
        raise StrategyEvaluationError("evaluation does not belong to this strategy")
    if evaluation.strategy_version != definition.version:
        # Strategy Version Provenance v1 (gap G3): an evaluation justifies only
        # the exact version it evaluated, never a sibling version of the same
        # strategy.
        raise StrategyEvaluationError("evaluation does not belong to this strategy version")
    if definition.lifecycle_state is not StrategyLifecycleState.RESEARCH_ONLY:
        raise StrategyEvaluationError(
            f"cannot promote a definition already in lifecycle_state {definition.lifecycle_state}"
        )
    if evaluation.sample_size < MINIMUM_PROMOTION_SAMPLE_SIZE:
        raise StrategyEvaluationError(
            f"insufficient sample_size for promotion: {evaluation.sample_size} "
            f"< {MINIMUM_PROMOTION_SAMPLE_SIZE}"
        )
    if evaluation.net_return is None:
        raise StrategyEvaluationError("cannot promote a strategy whose net_return is unavailable")
    if evaluation.net_return <= 0:
        raise StrategyEvaluationError("cannot promote a strategy with a non-positive net_return")
    return next_version(
        definition,
        lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE,
        created_at=promoted_at,
    )


def promote_to_active(
    definition: StrategyDefinition,
    *,
    evaluation: StrategyEvaluationResult,
    promoted_at: datetime,
) -> StrategyDefinition:
    """The one deliberate, evidence-gated PAPER_ELIGIBLE -> ACTIVE transition
    (Recurring Strategy PAPER Orchestration v1, Phase H Strategy Lifecycle
    Guard). Mirrors ``promote_to_paper_eligible``'s exact gate (adequate
    sample size, strictly positive net return) one lifecycle step later.

    Deliberately does not call ``strategy_definition.next_version`` --
    that function now refuses ``ACTIVE`` outright (see its own docstring),
    precisely so no generic version bump can reach ACTIVE by any path other
    than this one, explicitly evidence-gated function. The construction
    below is otherwise identical to what ``next_version`` already does:
    carry every descriptive field forward unchanged, increment ``version``,
    and record exact parent lineage.

    ACTIVE still grants no LIVE authority: see module docstring. Never
    invoked automatically anywhere in this codebase (no production caller).
    """

    if evaluation.strategy_id != definition.strategy_id:
        raise StrategyEvaluationError("evaluation does not belong to this strategy")
    if evaluation.strategy_version != definition.version:
        # Strategy Version Provenance v1 (gap G3): an evaluation justifies only
        # the exact version it evaluated, never a sibling version of the same
        # strategy.
        raise StrategyEvaluationError("evaluation does not belong to this strategy version")
    if definition.lifecycle_state is not StrategyLifecycleState.PAPER_ELIGIBLE:
        raise StrategyEvaluationError(
            f"cannot promote to ACTIVE from lifecycle_state {definition.lifecycle_state}"
        )
    if evaluation.sample_size < MINIMUM_PROMOTION_SAMPLE_SIZE:
        raise StrategyEvaluationError(
            f"insufficient sample_size for ACTIVE promotion: {evaluation.sample_size} "
            f"< {MINIMUM_PROMOTION_SAMPLE_SIZE}"
        )
    if evaluation.net_return is None:
        raise StrategyEvaluationError("cannot promote a strategy whose net_return is unavailable")
    if evaluation.net_return <= 0:
        raise StrategyEvaluationError("cannot promote a strategy with a non-positive net_return")
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
        created_at=promoted_at,
        version=definition.version + 1,
        parent_strategy_id=definition.strategy_id,
        lifecycle_state=StrategyLifecycleState.ACTIVE,
    )


@dataclass(frozen=True, slots=True)
class StrategyComparison:
    candidate_strategy_id: str
    candidate_version: int
    candidate_evaluation_kind: StrategyEvaluationKind
    benchmark_strategy_id: str
    benchmark_version: int
    benchmark_evaluation_kind: StrategyEvaluationKind
    net_return_delta: Decimal | None
    win_rate_delta: Decimal | None
    maximum_drawdown_delta: Decimal | None
    comparability_notes: tuple[str, ...]
    verdict: ComparisonVerdict
    compared_at: datetime
    canonical_hash: str


def compare_strategy_evaluations(
    candidate: StrategyEvaluationResult,
    benchmark: StrategyEvaluationResult,
    *,
    compared_at: datetime,
) -> StrategyComparison:
    """Pure, descriptive delta comparison. Never promotes, authorizes, or
    mutates either strategy -- purely evidence for a human or a later,
    separately-gated decision (see ``promote_to_paper_eligible``)."""

    net_return_delta = (
        _quantize(candidate.net_return - benchmark.net_return)
        if candidate.net_return is not None and benchmark.net_return is not None
        else None
    )
    win_rate_delta = (
        _quantize(candidate.win_rate - benchmark.win_rate)
        if candidate.win_rate is not None and benchmark.win_rate is not None
        else None
    )
    drawdown_delta = (
        _quantize(candidate.maximum_drawdown - benchmark.maximum_drawdown)
        if candidate.maximum_drawdown is not None and benchmark.maximum_drawdown is not None
        else None
    )

    notes: list[str] = []
    # Comparability Guard (Recurring Strategy PAPER Orchestration v1, Phase
    # I): two evaluations with a genuinely known covered window (both sides'
    # ``window_start`` populated) must overlap to be legitimately comparable
    # on a single return-delta axis -- a candidate backtested over one
    # historical period and a benchmark backtested over a disjoint period
    # describe different market regimes, not the same question answered two
    # ways. A side with ``window_start is None`` (e.g. an
    # ``INTERNAL_COUNTERFACTUAL_COHORT`` evaluation, which has no covered
    # window at all -- see ``evaluate_internal_strategy_from_counterfactual_
    # outcome``) has no window claim to conflict with, so this check is
    # deliberately silent when either side lacks one.
    #
    # This guard deliberately does NOT also require matching
    # ``evaluation_kind``: comparing across kinds (an internal strategy vs
    # an external benchmark) is this function's primary intended use (see
    # module docstring), and ``net_return`` is already the honest common
    # comparison unit -- a side that cannot honestly state one (only
    # ``INTERNAL_COUNTERFACTUAL_COHORT`` -- decision-level evidence, not a
    # realized return) reports ``net_return=None`` and falls through to
    # ``INCONCLUSIVE_INSUFFICIENT_EVIDENCE`` below on its own merits,
    # without needing a redundant identity gate here.
    comparable_windows = (
        candidate.window_start is None
        or benchmark.window_start is None
        or (
            candidate.window_start <= benchmark.window_end
            and benchmark.window_start <= candidate.window_end
        )
    )
    if not comparable_windows:
        notes.append(
            f"covered windows do not overlap (candidate {candidate.window_start} - "
            f"{candidate.window_end} vs benchmark {benchmark.window_start} - "
            f"{benchmark.window_end}) -- different market periods"
        )
    minimum_sample = min(candidate.sample_size, benchmark.sample_size)
    if minimum_sample < MINIMUM_PROMOTION_SAMPLE_SIZE:
        notes.append(
            f"sample size below the {MINIMUM_PROMOTION_SAMPLE_SIZE}-sample floor "
            f"({minimum_sample}) -- treat as exploratory"
        )

    if not comparable_windows:
        # A genuine period mismatch makes any return delta meaningless --
        # never silently rank it, regardless of sample size or the sign of
        # net_return_delta (never downgraded to merely "inconclusive").
        verdict = ComparisonVerdict.NON_COMPARABLE
    elif net_return_delta is None or minimum_sample < MINIMUM_PROMOTION_SAMPLE_SIZE:
        verdict = ComparisonVerdict.INCONCLUSIVE_INSUFFICIENT_EVIDENCE
    elif net_return_delta > 0:
        verdict = ComparisonVerdict.CANDIDATE_OUTPERFORMS_BENCHMARK
    elif net_return_delta < 0:
        verdict = ComparisonVerdict.BENCHMARK_OUTPERFORMS_CANDIDATE
    else:
        verdict = ComparisonVerdict.INCONCLUSIVE_INSUFFICIENT_EVIDENCE

    payload = (
        candidate.strategy_id,
        candidate.strategy_version,
        candidate.canonical_hash,
        benchmark.strategy_id,
        benchmark.strategy_version,
        benchmark.canonical_hash,
        compared_at,
    )
    return StrategyComparison(
        candidate_strategy_id=candidate.strategy_id,
        candidate_version=candidate.strategy_version,
        candidate_evaluation_kind=candidate.evaluation_kind,
        benchmark_strategy_id=benchmark.strategy_id,
        benchmark_version=benchmark.strategy_version,
        benchmark_evaluation_kind=benchmark.evaluation_kind,
        net_return_delta=net_return_delta,
        win_rate_delta=win_rate_delta,
        maximum_drawdown_delta=drawdown_delta,
        comparability_notes=tuple(notes),
        verdict=verdict,
        compared_at=compared_at,
        canonical_hash=content_digest(payload),
    )
