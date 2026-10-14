"""External Strategy Evaluation v1: declarative ingestion, point-in-time
evidence windowing, and evaluation-lineage identity for a strategy defined
OUTSIDE Apex (a paper, a book, a blog, a forum post, an analyst note, or a
manually typed hypothesis) that an operator wants Apex to reproduce and
judge honestly.

This module adds ZERO new strategy-representation machinery. Every external
strategy this module can ever construct is exactly a
``strategy_definition.StrategyDefinition`` with ``source_kind=EXTERNAL`` and
one of the four already-reviewed, already-tested declarative rule kinds
(``BUY_AND_HOLD`` / ``MOVING_AVERAGE_TREND`` / ``TIME_SERIES_MOMENTUM`` /
``MEAN_REVERSION`` -- see ``strategy_definition.StrategyRuleKind`` and
``strategy_benchmark.compute_daily_signal``, both unchanged); every backtest
and evaluation this module ever produces is exactly
``strategy_benchmark.simulate_strategy_backtest`` and
``strategy_evaluation.evaluate_external_strategy``, unchanged. What is new
here is narrowly: (1) a JSON-safe, allowlisted input boundary that turns an
untrusted declarative mapping into a ``StrategyDefinition`` without ever
executing anything the caller supplied (``strategy_definition_from_
specification``); (2) point-in-time candle windowing so a caller-supplied
``as_of`` genuinely bounds what evidence an evaluation may see
(``point_in_time_candle_window``); (3) a non-mutating lifecycle
classification mirroring ``strategy_evaluation.promote_to_paper_eligible``'s
real gate, so an operator can see "would this qualify" without Apex ever
silently promoting anything (``classify_qualification``); and (4) one
immutable, content-addressed evidence record tying a specific evaluation to
its exact strategy version, evidence window, benchmark comparisons, and
qualification outcome (``ExternalStrategyEvaluationRecord``).

External source does NOT mean trusted source: every external strategy is
evaluated by this exact same machinery, held to this exact same evidence
bar, as any of Apex's own research. See ``EXTERNAL_STRATEGY_WARNING`` and
``strategy_definition.STRATEGY_EXTERNAL_PROVENANCE_WARNING`` (unchanged,
reused verbatim).

Authority ``NONE`` throughout. Nothing in this module can create an
Opportunity, Campaign, Order, Position, PAPER trial, provider request, or
LIVE authority -- see ``application.external_strategy_research`` for the
one, deliberately separate, explicit action
(``PromoteExternalStrategy``) that can move a definition's
``lifecycle_state``, and ``application.strategy_paper_trial.
ExecuteStrategyPaperTrial`` (never called from anywhere in this module or
its application layer) for the only code in this codebase that can ever
place a PAPER order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from apex.domain.crypto_campaign import FIXED_HOLDOUT_CAPTURE_ID
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_definition import (
    MINIMUM_PROMOTION_SAMPLE_SIZE,
    StrategyDefinition,
    StrategyDefinitionError,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import StrategyComparison, StrategyEvaluationResult
from apex.domain.values import ValueValidationError, content_digest, deterministic_id, parse_decimal

EXTERNAL_STRATEGY_WARNING = (
    "EXTERNAL STRATEGY -- REPRODUCED AND INDEPENDENTLY EVALUATED BY APEX; "
    "RESEARCH EVIDENCE ONLY, NO PAPER OR LIVE AUTHORITY"
)

# Versioned, named contracts -- matches the "named + versioned policy"
# convention used throughout Apex (e.g. ``SHARED_OPPORTUNITY_ELIGIBILITY_
# POLICY_V1``, ``KALSHI_BASELINE_QUOTE_POLICY_V1``). Bumping either requires
# a new version string, never a silent redefinition of the same one -- see
# each constant's own use below.
EXTERNAL_STRATEGY_SPECIFICATION_V1 = "EXTERNAL_STRATEGY_SPECIFICATION_V1"
EXTERNAL_STRATEGY_EVALUATION_POLICY_V1 = "EXTERNAL_STRATEGY_EVALUATION_POLICY_V1"

# The only rule kinds a declarative external specification may select.
# ``INTERNAL_METHOD_REFERENCE`` is deliberately excluded here even though
# ``StrategyRuleKind`` itself defines it -- ``StrategyDefinition.create``
# already refuses it for ``source_kind=EXTERNAL`` (see ``strategy_
# definition._validate_rule``), but this module fails closed on it BEFORE
# ever reaching that constructor, with an external-ingestion-specific
# reason code rather than a generic one.
_SUPPORTED_EXTERNAL_RULE_KINDS = frozenset(
    {
        StrategyRuleKind.BUY_AND_HOLD,
        StrategyRuleKind.MOVING_AVERAGE_TREND,
        StrategyRuleKind.TIME_SERIES_MOMENTUM,
        StrategyRuleKind.MEAN_REVERSION,
    }
)

# Per-rule-kind allowed ``rule_parameters`` keys and how to parse each one.
# "int" fields must be genuine JSON integers; "decimal" fields must be JSON
# STRINGS (never a JSON number) -- ``domain.values.parse_decimal`` already
# refuses binary floats outright, matching Apex's codebase-wide "no binary
# floats" discipline, and this module never carves out an exception for
# operator-supplied input.
_INT_FIELDS = frozenset(
    {
        "fast_window_days",
        "slow_window_days",
        "momentum_lookback_days",
        "reversion_lookback_days",
        "maximum_holding_days",
    }
)
_DECIMAL_FIELDS = frozenset({"momentum_entry_threshold"})
# Parsed identically to _DECIMAL_FIELDS below (allow_negative=True either
# way -- momentum_entry_threshold has no sign constraint at all, and this
# module deliberately does not re-enforce reversion_entry_threshold's own
# "must be negative" business rule itself; StrategyDefinition.create's own
# validation is the single source of truth for that, so a value here that
# fails it still falls through honestly to ExternalStrategySpecificationError
# via strategy_definition_from_specification's own StrategyDefinitionError
# translation). Named and kept separate from _DECIMAL_FIELDS purely for
# documentation clarity about which field this is.
_NEGATIVE_DECIMAL_FIELDS = frozenset({"reversion_entry_threshold"})

_RULE_KIND_FIELDS: dict[StrategyRuleKind, frozenset[str]] = {
    StrategyRuleKind.BUY_AND_HOLD: frozenset(),
    StrategyRuleKind.MOVING_AVERAGE_TREND: frozenset({"fast_window_days", "slow_window_days"}),
    StrategyRuleKind.TIME_SERIES_MOMENTUM: frozenset(
        {"momentum_lookback_days", "momentum_entry_threshold", "maximum_holding_days"}
    ),
    StrategyRuleKind.MEAN_REVERSION: frozenset(
        {"reversion_lookback_days", "reversion_entry_threshold", "maximum_holding_days"}
    ),
}
_RULE_KIND_REQUIRED_FIELDS: dict[StrategyRuleKind, frozenset[str]] = {
    StrategyRuleKind.BUY_AND_HOLD: frozenset(),
    StrategyRuleKind.MOVING_AVERAGE_TREND: frozenset({"fast_window_days", "slow_window_days"}),
    StrategyRuleKind.TIME_SERIES_MOMENTUM: frozenset(
        {"momentum_lookback_days", "momentum_entry_threshold"}
    ),
    StrategyRuleKind.MEAN_REVERSION: frozenset(
        {"reversion_lookback_days", "reversion_entry_threshold"}
    ),
}
# ``+1`` matches ``strategy_benchmark._evidence_requirements`` and its own
# ``canonical_strategy_benchmarks`` callers exactly (a signal needs one more
# completed close than its own lookback/slow-window to ever become
# "informed" -- see ``strategy_benchmark.compute_daily_signal``): a rule
# whose defining lookback is N days needs N+1 retained closes before its
# first genuinely informed signal, never fewer. Derived here, never taken
# from caller input, so a spec cannot understate its own data requirement.
_MinimumHistoryFn = Callable[[StrategyRuleParameters], int]
_MINIMUM_HISTORY_DAYS_BY_RULE_KIND: dict[StrategyRuleKind, _MinimumHistoryFn] = {
    StrategyRuleKind.BUY_AND_HOLD: lambda params: 1,
    StrategyRuleKind.MOVING_AVERAGE_TREND: lambda params: (params.slow_window_days or 0) + 1,
    StrategyRuleKind.TIME_SERIES_MOMENTUM: lambda params: (params.momentum_lookback_days or 0) + 1,
    StrategyRuleKind.MEAN_REVERSION: lambda params: (params.reversion_lookback_days or 0) + 1,
}


class ExternalStrategySpecificationError(ValueError):
    """A declarative external-strategy specification was malformed,
    unsupported, or otherwise could not be safely turned into a
    ``StrategyDefinition`` without guessing. Fails closed -- never partially
    constructs a definition from an ambiguous or incomplete specification."""


class ExternalStrategyEvaluationError(ValueError):
    """An external-strategy evaluation could not honestly proceed (missing
    or sealed evidence, an unsupported instrument count, a mismatched
    definition/evaluation pairing, ...). Fails closed."""


def _require_str(spec: Mapping[str, object], key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExternalStrategySpecificationError(f"EXTERNAL_STRATEGY_SPEC_MISSING_FIELD:{key}")
    return value


def _parse_rule_parameters(rule_kind: StrategyRuleKind, raw: object) -> StrategyRuleParameters:
    if not isinstance(raw, Mapping):
        raise ExternalStrategySpecificationError(
            "EXTERNAL_STRATEGY_SPEC_RULE_PARAMETERS_NOT_OBJECT"
        )
    allowed = _RULE_KIND_FIELDS[rule_kind]
    required = _RULE_KIND_REQUIRED_FIELDS[rule_kind]
    unknown = set(raw) - allowed
    if unknown:
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_UNKNOWN_RULE_PARAMETERS:{sorted(unknown)}"
        )
    missing = required - set(raw)
    if missing:
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_MISSING_RULE_PARAMETERS:{sorted(missing)}"
        )
    fields: dict[str, object] = {}
    for name, value in raw.items():
        if name in _INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ExternalStrategySpecificationError(
                    f"EXTERNAL_STRATEGY_SPEC_FIELD_NOT_INTEGER:{name}"
                )
            fields[name] = value
        elif name in _DECIMAL_FIELDS or name in _NEGATIVE_DECIMAL_FIELDS:
            if not isinstance(value, str):
                raise ExternalStrategySpecificationError(
                    f"EXTERNAL_STRATEGY_SPEC_DECIMAL_FIELD_MUST_BE_STRING:{name}"
                )
            try:
                fields[name] = parse_decimal(value, allow_negative=True)
            except ValueValidationError as error:
                raise ExternalStrategySpecificationError(
                    f"EXTERNAL_STRATEGY_SPEC_MALFORMED_DECIMAL:{name}"
                ) from error
        else:  # pragma: no cover - unreachable: unknown already rejected above
            raise ExternalStrategySpecificationError(f"EXTERNAL_STRATEGY_SPEC_UNKNOWN_FIELD:{name}")
    return StrategyRuleParameters(**fields)  # type: ignore[arg-type]


def strategy_definition_from_specification(
    spec: Mapping[str, object], *, created_at: datetime
) -> StrategyDefinition:
    """Turn one untrusted, JSON-safe declarative mapping into a genuine
    ``StrategyDefinition`` with ``source_kind=EXTERNAL`` at ``version=1``
    (``RESEARCH_ONLY``). Never executes anything from ``spec`` -- every field
    is either a plain string/int, or a string parsed through ``domain.
    values.parse_decimal``. Unknown top-level keys are ignored (forward
    compatible with additive future spec fields); every REQUIRED key missing
    or malformed fails closed with an ``ExternalStrategySpecificationError``
    naming exactly which key.

    Deliberately auto-derives ``horizon_kind`` (bounded iff
    ``maximum_holding_days`` is set -- see ``strategy_benchmark``'s own four
    canonical definitions for the identical convention) and
    ``evidence_requirements.minimum_history_days`` (see
    ``_MINIMUM_HISTORY_DAYS_BY_RULE_KIND``) rather than trusting caller
    input for either -- both are pure functions of ``rule_kind``/
    ``rule_parameters`` already, so accepting them separately could only
    ever introduce an avoidable inconsistency (e.g. a spec understating its
    own real data requirement), never a genuine new degree of freedom.
    ``requires_contemporaneous_observation`` is always ``False``: that field
    only matters to the live recurring ``STRATEGY_PAPER_TRIAL_EXECUTION``
    catalog (``domain.strategy_benchmark.canonical_strategy_benchmarks``),
    which this module's definitions are never fed into (see module
    docstring) -- a historical backtest evaluation never needs a
    contemporaneous quote.
    """

    specification_version = _require_str(spec, "specification_version")
    if specification_version != EXTERNAL_STRATEGY_SPECIFICATION_V1:
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_UNSUPPORTED_VERSION:{specification_version}"
        )
    display_name = _require_str(spec, "display_name")
    venue_raw = _require_str(spec, "venue")
    try:
        venue = SharedVenue(venue_raw)
    except ValueError as error:
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_UNKNOWN_VENUE:{venue_raw}"
        ) from error
    if venue is not SharedVenue.CRYPTO:
        # Explicit, not a silent coercion (see module/class docstrings):
        # External Strategy Evaluation v1 supports CRYPTO only, and a spec
        # naming any other venue must be told so directly rather than
        # having its venue field quietly overridden.
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_UNSUPPORTED_VENUE:{venue.value}"
        )
    instrument_id = _require_str(spec, "instrument_id")
    rule_kind_raw = _require_str(spec, "rule_kind")
    try:
        rule_kind = StrategyRuleKind(rule_kind_raw)
    except ValueError as error:
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_UNKNOWN_RULE_KIND:{rule_kind_raw}"
        ) from error
    if rule_kind not in _SUPPORTED_EXTERNAL_RULE_KINDS:
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_UNSUPPORTED_RULE_KIND:{rule_kind.value}"
        )
    rule_parameters = _parse_rule_parameters(rule_kind, spec.get("rule_parameters", {}))
    reference_description = _require_str(spec, "reference_description")
    origin_note = _require_str(spec, "origin_note")

    horizon_kind = (
        StrategyHorizonKind.DAILY_SIGNAL_BOUNDED_HOLD
        if rule_parameters.maximum_holding_days is not None
        else StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD
    )
    minimum_history_days = _MINIMUM_HISTORY_DAYS_BY_RULE_KIND[rule_kind](rule_parameters)

    try:
        return StrategyDefinition.create(
            source_kind=StrategySourceKind.EXTERNAL,
            display_name=display_name,
            venue=venue,
            instrument_ids=(instrument_id,),
            horizon_kind=horizon_kind,
            rule_kind=rule_kind,
            rule_parameters=rule_parameters,
            evidence_requirements=StrategyEvidenceRequirements(
                minimum_history_days=minimum_history_days,
                requires_contemporaneous_observation=False,
            ),
            provenance=StrategyProvenance(
                reference_kind="PUBLISHED_CONVENTION",
                reference_description=reference_description,
                origin_note=origin_note,
            ),
            created_at=created_at,
        )
    except StrategyDefinitionError as error:
        # Translate a genuine construction-time rejection (e.g. "fast_window
        # must be strictly less than slow_window") into this module's own
        # exception type -- callers of this ingestion boundary should never
        # need to catch two different error types for one "spec was bad"
        # concern.
        raise ExternalStrategySpecificationError(
            f"EXTERNAL_STRATEGY_SPEC_REJECTED:{error}"
        ) from error


def ensure_capture_eligible(capture_id: str) -> None:
    """Fail closed if ``capture_id`` names Apex's one sealed BTC-USD
    research holdout (``domain.crypto_campaign.FIXED_HOLDOUT_CAPTURE_ID``,
    2020-07-01 through 2022-07-01). That holdout is a separate, single-use,
    explicitly governed research-integrity mechanism for the BTC campaign
    candidate lineage (see ``domain.crypto_campaign``'s own module
    docstring) -- an unrelated general-purpose external-strategy evaluation
    path must never be able to spend it, even though the same
    ``crypto_daily_candle`` table physically retains it under this exact,
    self-describing ``capture_id``."""

    if capture_id == FIXED_HOLDOUT_CAPTURE_ID:
        raise ExternalStrategyEvaluationError("EXTERNAL_STRATEGY_CAPTURE_IS_SEALED_HOLDOUT")


def point_in_time_candle_window(
    candles: Sequence[CryptoDailyCandle], *, instrument_id: str, as_of: datetime
) -> tuple[CryptoDailyCandle, ...]:
    """Every retained candle for ``instrument_id`` whose own trading day has
    genuinely, fully elapsed by ``as_of`` -- ``bucket_start`` is the day a
    daily candle COVERS, so its close is only actually knowable once that
    whole day has passed (matches the "excludes the decision day's own
    close"/"excludes the incomplete current UTC day" convention used
    throughout Apex's crypto research modules, e.g. ``strategy_benchmark.
    compute_daily_signal``'s own module docstring). A candle whose day ends
    strictly after ``as_of`` is silently excluded here, never fed to
    ``strategy_benchmark.simulate_strategy_backtest`` -- this is the one
    seam in this module that actually enforces "no future observations,"
    which is why it is a small, independently testable, pure function
    rather than folded into a bigger method."""

    return tuple(
        sorted(
            (
                candle
                for candle in candles
                if candle.product_id == instrument_id
                and candle.bucket_start + timedelta(days=1) <= as_of
            ),
            key=lambda candle: candle.bucket_start,
        )
    )


class ExternalStrategyQualification(StrEnum):
    """Non-mutating classification of whether a candidate evaluation WOULD
    satisfy ``strategy_evaluation.promote_to_paper_eligible``'s real gate --
    computed by re-reading that exact gate's own imported threshold
    (``MINIMUM_PROMOTION_SAMPLE_SIZE``) and comparison
    (``net_return > 0``), never a re-derived or looser copy of it. Producing
    this classification NEVER calls ``promote_to_paper_eligible`` itself and
    NEVER mutates or re-versions the evaluated ``StrategyDefinition`` -- see
    ``application.external_strategy_research.PromoteExternalStrategy`` for
    the one, separate, explicit action that actually does."""

    RESEARCH_ONLY_INSUFFICIENT_SAMPLE = "RESEARCH_ONLY_INSUFFICIENT_SAMPLE"
    RESEARCH_ONLY_NONPOSITIVE_NET_RETURN = "RESEARCH_ONLY_NONPOSITIVE_NET_RETURN"
    WOULD_QUALIFY_FOR_PAPER_ELIGIBLE = "WOULD_QUALIFY_FOR_PAPER_ELIGIBLE"


def classify_qualification(evaluation: StrategyEvaluationResult) -> ExternalStrategyQualification:
    if evaluation.sample_size < MINIMUM_PROMOTION_SAMPLE_SIZE:
        return ExternalStrategyQualification.RESEARCH_ONLY_INSUFFICIENT_SAMPLE
    if evaluation.net_return is None or evaluation.net_return <= 0:
        return ExternalStrategyQualification.RESEARCH_ONLY_NONPOSITIVE_NET_RETURN
    return ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE


@dataclass(frozen=True, slots=True)
class ExternalStrategyEvaluationRecord:
    """One immutable, content-addressed evaluation event: exactly which
    strategy definition/version was evaluated, against exactly which
    retained evidence window (``capture_id`` + the effective ``as_of``
    cutoff), under exactly which evaluation/benchmark/cost policy, producing
    exactly which candidate result, benchmark comparisons, and lifecycle
    qualification. Mirrors the "immutable evidence record with a
    deterministic id, an ``as_of``, and a ``canonical_hash``" idiom already
    used throughout Apex (e.g. ``counterfactual_outcome.
    CounterfactualOutcomeAggregate``, ``kalshi_resolution.
    KalshiMarketResolutionFact``) -- see this module's own docstring.

    ``benchmark_comparisons`` is always computed against every member of
    ``strategy_benchmark.canonical_strategy_benchmarks()`` (Benchmark
    Discipline, Part 9): the benchmark set is a deterministic function of
    ``evaluation_policy_version`` alone, never hand-picked per candidate
    after seeing its result.
    """

    evaluation_id: str
    strategy_id: str
    strategy_version: int
    as_of: datetime
    capture_id: str
    evaluation_policy_version: str
    evaluated_at: datetime
    candidate: StrategyEvaluationResult
    benchmark_comparisons: tuple[StrategyComparison, ...]
    qualification: ExternalStrategyQualification
    canonical_hash: str
    authority: str = "NONE"


def build_external_strategy_evaluation_record(
    *,
    strategy_id: str,
    strategy_version: int,
    as_of: datetime,
    capture_id: str,
    evaluated_at: datetime,
    candidate: StrategyEvaluationResult,
    benchmark_comparisons: tuple[StrategyComparison, ...],
) -> ExternalStrategyEvaluationRecord:
    """Pure constructor. ``evaluation_id``/``canonical_hash`` are derived
    only from ``candidate.canonical_hash`` (which already, transitively,
    folds in ``strategy_id``/``strategy_version``/the exact signal/backtest
    content -- see ``strategy_benchmark.simulate_strategy_backtest``'s own
    ``canonical_hash``) plus each comparison's own deterministic RESULT
    fields and this record's ``as_of``/``capture_id``/
    ``evaluation_policy_version`` -- so replaying the identical evaluation
    (same definition, same ``capture_id``, same effective ``as_of``) always
    reproduces the identical id, and any genuine change to any input
    changes it (Part 15, Deterministic Identity).

    Deliberately does NOT fold in ``StrategyComparison.canonical_hash``
    itself (or its ``compared_at``): ``strategy_evaluation.compare_strategy_
    evaluations`` -- unmodified, existing, reviewed code this module must
    not change -- bakes its own ``compared_at`` wall-clock argument into
    that hash, so two otherwise byte-identical replays computed at two
    different real times would otherwise mint two different, contradictory
    ``evaluation_id``s for the exact same evidence and result (violating
    Part 15). Each comparison's own RESULT content (deltas/verdict/notes,
    keyed by which benchmark it is against) is itself fully deterministic
    given ``candidate`` and the fixed canonical benchmark catalog, and is
    what is used here instead.
    """

    qualification = classify_qualification(candidate)
    comparison_identity = tuple(
        (
            item.benchmark_strategy_id,
            item.benchmark_version,
            item.net_return_delta,
            item.win_rate_delta,
            item.maximum_drawdown_delta,
            item.verdict.value,
            item.comparability_notes,
        )
        for item in benchmark_comparisons
    )
    identity_payload: tuple[object, ...] = (
        strategy_id,
        strategy_version,
        as_of,
        capture_id,
        EXTERNAL_STRATEGY_EVALUATION_POLICY_V1,
        candidate.canonical_hash,
        comparison_identity,
    )
    evaluation_id = deterministic_id("external_strategy_evaluation", identity_payload)
    canonical_hash = content_digest(identity_payload)
    return ExternalStrategyEvaluationRecord(
        evaluation_id=evaluation_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        as_of=as_of,
        capture_id=capture_id,
        evaluation_policy_version=EXTERNAL_STRATEGY_EVALUATION_POLICY_V1,
        evaluated_at=evaluated_at,
        candidate=candidate,
        benchmark_comparisons=benchmark_comparisons,
        qualification=qualification,
        canonical_hash=canonical_hash,
    )
