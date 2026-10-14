"""Quant research methodology and walk-forward validation controls.

This module makes Apex's historical research results difficult to fool,
difficult to leak future information into, and explicit about what level of
evidence they actually represent. It is built entirely on top of the
existing ``domain.historical_replay`` foundation (``ReplayRun``,
``ReplayFeatureFrame``, ``HistoricalResearchEvaluation``,
``ResearchPeriodPolicy``, ``guard_holdout_access``, ``summarize_values``,
``detect_overlapping_outcomes``) and the existing
``domain.research_evidence`` hypothesis lifecycle -- it never redefines
point-in-time slicing, outcome isolation, or the evidence-class vocabulary,
and it never touches a previously-persisted record.

The target research lifecycle is:

    Question -> Hypothesis -> Exploratory Research -> Validation ->
    Locked Holdout -> Walk-Forward Evaluation -> Robustness Checks ->
    Retained Conclusion

This is a research-quality milestone. No record here ever carries Strategy,
Opportunity, Order, Position, PAPER, or LIVE authority; every retained
record below is ``authority = "NONE"``. Nothing here grants execution
authority, and nothing here builds a general parameter optimizer -- every
"configuration" concept exists to make already-chosen configurations
honest and traceable, not to search for better ones.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.historical_replay import (
    HistoricalResearchEvaluation,
    OutcomeCompletenessState,
    ReplayFeatureFrame,
    ReplayStatisticalSummary,
    ResearchMethodId,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
    SummaryKind,
    detect_overlapping_outcomes,
    group_summaries,
    guard_holdout_access,
    summarize_values,
)
from apex.domain.values import content_digest, deterministic_id

RESEARCH_METHODOLOGY_V1 = "RESEARCH_METHODOLOGY_V1"
METHODOLOGY_WARNING = "RESEARCH METHODOLOGY EVIDENCE ONLY -- NO STRATEGY, PAPER, OR LIVE AUTHORITY"
INSUFFICIENT_EFFECTIVE_SAMPLE = "INSUFFICIENT_EFFECTIVE_SAMPLE"
SAMPLE_SIZE_POLICY_ID = "RESEARCH_SAMPLE_SIZE_POLICY_V1"
STATISTICS_V2_POLICY_ID = "RESEARCH_STATISTICAL_DIAGNOSTICS_V2"
BOOTSTRAP_METHOD_ID = "SEEDED_PERCENTILE_BOOTSTRAP_V1"
HAC_METHOD_ID = "NEWEY_WEST_HAC_V1"
IID_SE_METHOD_ID = "IID_NORMAL_APPROXIMATION_V1"
Z_TEST_METHOD_ID = "TWO_SIDED_Z_TEST_NORMAL_APPROXIMATION_V1"
PROMOTION_POLICY_ID = "RESEARCH_PROMOTION_POLICY_V1"
COST_DATA_UNAVAILABLE = (
    "COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL_IN_DESCRIPTIVE_RESEARCH_METHODS"
)


class ResearchMethodologyError(ValueError):
    """A research-methodology invariant was violated."""


class MethodologyConflictError(ResearchMethodologyError):
    """A declared identity was reconstructed with genuinely different content."""


class InsufficientSampleError(ResearchMethodologyError):
    """The effective, non-overlapping sample is too small for the requested
    inference -- descriptive statistics remain available, but a confidence
    claim is refused."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchMethodologyError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _text(value: str, code: str, *, maximum: int = 500) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ResearchMethodologyError(code)
    return normalized


def _bounded_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def bound_statistical_decimal(value: Decimal) -> Decimal:
    """Public wrapper over the module's fixed 8-decimal-place quantization
    -- any Decimal an application-layer caller derives ad hoc (e.g. a mean
    computed over a filtered subset for a ``RobustnessCheckResult``) must be
    bounded through this before it can safely reach ``content_digest``
    (which enforces ``MAX_PRECISION`` significant digits)."""

    return _bounded_decimal(value)


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        if value < 0:
            raise ResearchMethodologyError("METHODOLOGY_SQRT_NEGATIVE_INPUT")
        return +value.sqrt()


def _normal_cdf(value: Decimal) -> Decimal:
    probability = (1.0 + math.erf(float(value) / math.sqrt(2.0))) / 2.0
    return Decimal(str(max(0.0, min(1.0, probability))))


# --------------------------------------------------------------------------
# Section 1: research phase model
# --------------------------------------------------------------------------


class ResearchPhase(StrEnum):
    """Every retained research result identifies its phase. An exploratory
    result may never be presented as validation; validation performance may
    never be presented as holdout performance -- enforced structurally by
    ``ResearchTrial.create`` below, not only by convention."""

    EXPLORATORY = "EXPLORATORY"
    VALIDATION = "VALIDATION"
    LOCKED_HOLDOUT = "LOCKED_HOLDOUT"
    WALK_FORWARD = "WALK_FORWARD"
    FORWARD_PAPER = "FORWARD_PAPER"


_GOVERNED_PHASE_ROLE: dict[ResearchPhase, ResearchPeriodRole] = {
    ResearchPhase.VALIDATION: ResearchPeriodRole.VALIDATION,
    ResearchPhase.LOCKED_HOLDOUT: ResearchPeriodRole.HOLDOUT,
}


# --------------------------------------------------------------------------
# Section 3: train / validation / holdout integrity (ordering, stricter than
# ResearchPeriodPolicy.create's bare non-overlap check)
# --------------------------------------------------------------------------

_ROLE_ORDER: dict[ResearchPeriodRole, int] = {
    ResearchPeriodRole.TRAIN: 0,
    ResearchPeriodRole.VALIDATION: 1,
    ResearchPeriodRole.HOLDOUT: 2,
}


def assert_ordered_research_periods(policy: ResearchPeriodPolicy) -> None:
    """``ResearchPeriodPolicy.create`` already rejects overlapping ranges
    and reversed start/end dates for a single period. This adds the
    stricter promotion-track rule: whichever of TRAIN/VALIDATION/HOLDOUT
    are declared must also be *chronologically* ordered in that exact role
    order -- not merely non-overlapping in whatever order they happen to
    fall. Walk-forward plans never call this: their own fold generator
    defines its own rolling/expanding boundaries directly."""

    present = [item for item in policy.periods if item.role in _ROLE_ORDER]
    by_role = sorted(present, key=lambda item: _ROLE_ORDER[item.role])
    by_date = sorted(present, key=lambda item: item.start_date)
    if [item.role for item in by_role] != [item.role for item in by_date]:
        raise ResearchMethodologyError(
            "RESEARCH_PERIODS_MUST_BE_ORDERED_TRAIN_BEFORE_VALIDATION_BEFORE_HOLDOUT"
        )


# --------------------------------------------------------------------------
# Section 2: holdout-by-default governance policy
# --------------------------------------------------------------------------


class GovernanceIntent(StrEnum):
    EXPLORATORY_ONLY = "EXPLORATORY_ONLY"
    PROMOTION_TRACK = "PROMOTION_TRACK"


@dataclass(frozen=True, slots=True)
class ResearchGovernancePolicy:
    """A hypothesis's declared research-governance intent, wrapping an
    (unmodified, reused) ``ResearchPeriodPolicy``. Fully immutable: there is
    no update method. Redeclaring for the same hypothesis/dataset/replay
    scope always produces a genuinely new, independently retained identity
    -- the old declaration, and any exposure events already recorded
    against it, are never deleted or hidden (``supersedes_governance_policy_id``
    makes the lineage explicit, mirroring ``ResearchHypothesisVersion``'s
    own revision/supersedes pattern)."""

    governance_policy_id: str
    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    intent: GovernanceIntent
    period_policy: ResearchPeriodPolicy
    revision: int
    supersedes_governance_policy_id: str | None
    declared_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_version_id: str,
        dataset_snapshot_id: str,
        replay_run_id: str,
        intent: GovernanceIntent | str,
        period_policy: ResearchPeriodPolicy,
        revision: int = 1,
        supersedes_governance_policy_id: str | None = None,
        declared_at: datetime,
    ) -> ResearchGovernancePolicy:
        if not hypothesis_version_id or not dataset_snapshot_id or not replay_run_id:
            raise ResearchMethodologyError("GOVERNANCE_POLICY_LINEAGE_REQUIRED")
        if revision < 1:
            raise ResearchMethodologyError("GOVERNANCE_POLICY_REVISION_INVALID")
        if (revision == 1) != (supersedes_governance_policy_id is None):
            raise ResearchMethodologyError("GOVERNANCE_POLICY_REVISION_LINEAGE_INVALID")
        intent_value = GovernanceIntent(intent)
        if intent_value is GovernanceIntent.PROMOTION_TRACK:
            holdout = next(
                (item for item in period_policy.periods if item.role is ResearchPeriodRole.HOLDOUT),
                None,
            )
            if holdout is None:
                raise ResearchMethodologyError("PROMOTION_TRACK_REQUIRES_DECLARED_HOLDOUT")
            if not holdout.locked:
                raise ResearchMethodologyError("PROMOTION_TRACK_HOLDOUT_MUST_BE_LOCKED")
            assert_ordered_research_periods(period_policy)
        declared = _aware(declared_at, "GOVERNANCE_POLICY_DECLARED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            hypothesis_version_id,
            dataset_snapshot_id,
            replay_run_id,
            intent_value,
            period_policy.policy_id,
            period_policy.canonical_hash,
            revision,
            supersedes_governance_policy_id,
            declared,
        )
        return cls(
            governance_policy_id=deterministic_id("research_governance_policy", payload),
            hypothesis_version_id=hypothesis_version_id,
            dataset_snapshot_id=dataset_snapshot_id,
            replay_run_id=replay_run_id,
            intent=intent_value,
            period_policy=period_policy,
            revision=revision,
            supersedes_governance_policy_id=supersedes_governance_policy_id,
            declared_at=declared,
            canonical_hash=content_digest(payload),
        )

    def role_for_phase(self, phase: ResearchPhase) -> ResearchPeriodRole | None:
        return _GOVERNED_PHASE_ROLE.get(phase)


def guard_governed_holdout_access(policy: ResearchGovernancePolicy, *, purpose: str) -> None:
    """Thin, explicit entry point over the reused ``guard_holdout_access`` --
    locked holdout cannot be queried through exploratory/parameter-development
    paths, full stop."""

    guard_holdout_access(policy.period_policy, role=ResearchPeriodRole.HOLDOUT, purpose=purpose)


@dataclass(frozen=True, slots=True)
class HoldoutExposureEvent:
    """Append-only: consuming a holdout result records that it was exposed.
    Content-addressed on (policy, actor, purpose, consumed trial, instant) so
    a genuinely repeated identical read never creates a phantom duplicate,
    matching this codebase's general idempotent-replay-safety convention."""

    exposure_id: str
    governance_policy_id: str
    exposed_at: datetime
    actor: str
    purpose: str
    consumed_trial_id: str | None
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        governance_policy: ResearchGovernancePolicy,
        exposed_at: datetime,
        actor: str,
        purpose: str,
        consumed_trial_id: str | None,
    ) -> HoldoutExposureEvent:
        holdout = next(
            (
                item
                for item in governance_policy.period_policy.periods
                if item.role is ResearchPeriodRole.HOLDOUT
            ),
            None,
        )
        if holdout is None:
            raise ResearchMethodologyError("EXPOSURE_REQUIRES_DECLARED_HOLDOUT")
        exposed = _aware(exposed_at, "EXPOSURE_EXPOSED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            governance_policy.governance_policy_id,
            exposed,
            _text(actor, "EXPOSURE_ACTOR_INVALID", maximum=160),
            _text(purpose, "EXPOSURE_PURPOSE_INVALID", maximum=160),
            consumed_trial_id,
        )
        return cls(
            exposure_id=deterministic_id("research_governance_holdout_exposure", payload),
            governance_policy_id=governance_policy.governance_policy_id,
            exposed_at=exposed,
            actor=payload[3],
            purpose=payload[4],
            consumed_trial_id=consumed_trial_id,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 5: parameter / configuration provenance
# --------------------------------------------------------------------------


class ConfigurationOrigin(StrEnum):
    """Deliberately has no ``SELECTED_USING_HOLDOUT`` member: a configuration
    selected using holdout performance is structurally unrepresentable, not
    merely disallowed by convention."""

    FIXED_A_PRIORI = "FIXED_A_PRIORI"
    EXPLORATORY = "EXPLORATORY"
    SELECTED_USING_TRAINING = "SELECTED_USING_TRAINING"
    SELECTED_USING_VALIDATION = "SELECTED_USING_VALIDATION"


_SELECTED_ORIGINS = frozenset(
    {ConfigurationOrigin.SELECTED_USING_TRAINING, ConfigurationOrigin.SELECTED_USING_VALIDATION}
)


@dataclass(frozen=True, slots=True)
class ResearchConfiguration:
    configuration_id: str
    method_id: ResearchMethodId
    method_version: str
    parameter_values: tuple[tuple[str, str], ...]
    origin: ConfigurationOrigin
    candidates_examined_count: int
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        method_id: ResearchMethodId | str,
        method_version: str,
        parameter_values: tuple[tuple[str, str], ...],
        origin: ConfigurationOrigin | str,
        candidates_examined_count: int,
    ) -> ResearchConfiguration:
        method = ResearchMethodId(method_id)
        origin_value = ConfigurationOrigin(origin)
        params = tuple(sorted(parameter_values, key=lambda item: item[0]))
        if len({name for name, _ in params}) != len(params):
            raise ResearchMethodologyError("CONFIGURATION_PARAMETER_NAMES_MUST_BE_UNIQUE")
        if candidates_examined_count < 1:
            raise ResearchMethodologyError("CANDIDATES_EXAMINED_COUNT_INVALID")
        if origin_value in _SELECTED_ORIGINS and candidates_examined_count < 2:
            raise ResearchMethodologyError("SELECTED_CONFIGURATION_REQUIRES_MULTIPLE_CANDIDATES")
        if origin_value is ConfigurationOrigin.FIXED_A_PRIORI and candidates_examined_count != 1:
            raise ResearchMethodologyError("FIXED_A_PRIORI_CONFIGURATION_EXAMINES_EXACTLY_ONE")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            method,
            _text(method_version, "CONFIGURATION_METHOD_VERSION_INVALID", maximum=40),
            params,
            origin_value,
            candidates_examined_count,
        )
        return cls(
            configuration_id=deterministic_id("research_configuration", payload),
            method_id=method,
            method_version=payload[2],
            parameter_values=params,
            origin=origin_value,
            candidates_examined_count=candidates_examined_count,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 8: sample-size policy (replaces the placeholder minimum of 5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampleSizeRequirement:
    policy_id: str
    minimum_raw_sample: int
    minimum_effective_sample: Decimal


_PHASE_MINIMUM_RAW_SAMPLE: dict[ResearchPhase, int] = {
    ResearchPhase.EXPLORATORY: 20,
    ResearchPhase.VALIDATION: 30,
    ResearchPhase.LOCKED_HOLDOUT: 30,
    ResearchPhase.WALK_FORWARD: 10,
    ResearchPhase.FORWARD_PAPER: 20,
}
_GROUPED_REGIME_MINIMUM_PER_GROUP = 10


def resolve_minimum_sample_requirement(
    *,
    phase: ResearchPhase,
    is_grouped_regime_comparison: bool = False,
    group_count: int = 1,
) -> SampleSizeRequirement:
    """Conservative, versioned, phase-dependent defaults -- not one universal
    number. Grouped/regime-sliced comparisons scale with how many groups are
    being compared, since each group needs its own adequate sample."""

    if group_count < 1:
        raise ResearchMethodologyError("GROUP_COUNT_INVALID")
    base = _PHASE_MINIMUM_RAW_SAMPLE[phase]
    if is_grouped_regime_comparison:
        base = max(base, _GROUPED_REGIME_MINIMUM_PER_GROUP * group_count)
    minimum_effective = Decimal(max(5, base // 3))
    return SampleSizeRequirement(
        policy_id=SAMPLE_SIZE_POLICY_ID,
        minimum_raw_sample=base,
        minimum_effective_sample=minimum_effective,
    )


def assert_effective_sample(
    *, effective_sample_count: Decimal, requirement: SampleSizeRequirement
) -> None:
    if effective_sample_count < requirement.minimum_effective_sample:
        raise InsufficientSampleError(
            f"{INSUFFICIENT_EFFECTIVE_SAMPLE}:{effective_sample_count}<"
            f"{requirement.minimum_effective_sample}"
        )


class SampleQualityState(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_EFFECTIVE_SAMPLE = "INSUFFICIENT_EFFECTIVE_SAMPLE"


# --------------------------------------------------------------------------
# Section 7: overlapping-outcome handling
# --------------------------------------------------------------------------


class OverlapHandlingMode(StrEnum):
    RETAINED_DESCRIPTIVE = "RETAINED_DESCRIPTIVE"
    DOWNSAMPLED = "DOWNSAMPLED"
    GROUPED = "GROUPED"
    EXCLUDED_FROM_INFERENCE = "EXCLUDED_FROM_INFERENCE"


@dataclass(frozen=True, slots=True)
class OverlapDiagnostics:
    overlapping_pair_count: int
    raw_sample_count: int
    effective_sample_count: Decimal
    handling_mode: OverlapHandlingMode
    inference_available: bool
    note: str


def compute_overlap_diagnostics(
    evaluations: Sequence[HistoricalResearchEvaluation], *, outcome_horizon_sessions: int
) -> OverlapDiagnostics:
    """Reuses ``detect_overlapping_outcomes`` unmodified. When overlap is
    detected, the effective (approximately non-overlapping) sample count is
    a simple, conservative stride estimate -- raw complete-outcome count
    divided by the outcome horizon -- deliberately *not* the eigenvalue/HAC
    machinery in ``domain.research_integrity`` (a Sharpe-ratio-return-series
    tool built for a different purpose: selecting among many strategy
    candidates, not diagnosing one hypothesis's own outcome dependence).
    Descriptive statistics always remain available; inference is suppressed
    (not silently computed as if the samples were independent) whenever
    overlap is present, unless the caller explicitly downsamples or groups
    first."""

    if outcome_horizon_sessions < 1:
        raise ResearchMethodologyError("OUTCOME_HORIZON_SESSIONS_INVALID")
    overlaps = detect_overlapping_outcomes(evaluations)
    raw = sum(
        1
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
    )
    if not overlaps or raw == 0:
        return OverlapDiagnostics(
            overlapping_pair_count=0,
            raw_sample_count=raw,
            effective_sample_count=Decimal(raw),
            handling_mode=OverlapHandlingMode.RETAINED_DESCRIPTIVE,
            inference_available=True,
            note="NO_OVERLAP_DETECTED",
        )
    effective = max(Decimal(1), Decimal(raw) / Decimal(outcome_horizon_sessions))
    return OverlapDiagnostics(
        overlapping_pair_count=len(overlaps),
        raw_sample_count=raw,
        effective_sample_count=_bounded_decimal(effective),
        handling_mode=OverlapHandlingMode.EXCLUDED_FROM_INFERENCE,
        inference_available=False,
        note=(
            "OVERLAPPING_FORWARD_RETURN_WINDOWS_DETECTED_INFERENCE_SUPPRESSED_"
            "DESCRIPTIVE_STATISTICS_RETAINED"
        ),
    )


def downsample_non_overlapping(
    evaluations: Sequence[HistoricalResearchEvaluation], *, outcome_horizon_sessions: int
) -> tuple[HistoricalResearchEvaluation, ...]:
    """An explicit, honest alternative to suppressing inference: keep only
    every Nth complete evaluation (N = outcome horizon), in as_of order, so
    the retained subset's outcome windows no longer overlap. The caller
    decides whether to use this (``OverlapHandlingMode.DOWNSAMPLED``) or to
    exclude inference instead -- this module never silently substitutes one
    for the other."""

    if outcome_horizon_sessions < 1:
        raise ResearchMethodologyError("OUTCOME_HORIZON_SESSIONS_INVALID")
    complete = sorted(
        (
            item
            for item in evaluations
            if item.outcome is not None
            and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        ),
        key=lambda item: item.as_of,
    )
    return tuple(complete[::outcome_horizon_sessions])


# --------------------------------------------------------------------------
# Section 10: benchmark and null-model requirement
# --------------------------------------------------------------------------


class BenchmarkKind(StrEnum):
    UNCONDITIONAL_SUBSEQUENT_RETURN = "UNCONDITIONAL_SUBSEQUENT_RETURN"
    MARKET_BENCHMARK = "MARKET_BENCHMARK"
    EQUAL_WEIGHT_COHORT = "EQUAL_WEIGHT_COHORT"
    RANDOM_DATE_BASELINE = "RANDOM_DATE_BASELINE"
    PRIOR_REGIME_DISTRIBUTION = "PRIOR_REGIME_DISTRIBUTION"


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    benchmark_kind: BenchmarkKind
    benchmark_sample_count: int
    benchmark_mean: Decimal
    absolute_result: Decimal
    relative_result: Decimal
    beats_benchmark: bool
    canonical_hash: str


def compare_to_benchmark(
    *,
    sample_mean: Decimal,
    benchmark_kind: BenchmarkKind | str,
    benchmark_values: Sequence[Decimal],
) -> BenchmarkComparison:
    if not benchmark_values:
        raise ResearchMethodologyError("BENCHMARK_SAMPLE_REQUIRED")
    kind = BenchmarkKind(benchmark_kind)
    # Bound every Decimal that reaches the payload/returned fields -- an
    # unbounded caller-supplied or freshly-divided mean can carry more than
    # MAX_PRECISION significant digits, which content_digest (via
    # canonical_decimal/parse_decimal) rejects outright.
    sample = _bounded_decimal(sample_mean)
    benchmark_mean = _bounded_decimal(
        sum(benchmark_values, Decimal()) / Decimal(len(benchmark_values))
    )
    relative = _bounded_decimal(sample - benchmark_mean)
    payload = (kind, len(benchmark_values), benchmark_mean, sample, relative)
    return BenchmarkComparison(
        benchmark_kind=kind,
        benchmark_sample_count=len(benchmark_values),
        benchmark_mean=benchmark_mean,
        absolute_result=sample,
        relative_result=relative,
        beats_benchmark=relative > 0,
        canonical_hash=content_digest(payload),
    )


# --------------------------------------------------------------------------
# Section 9: statistical diagnostics v2
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectSize:
    cohens_d: Decimal | None
    method_id: str = "COHENS_D_POOLED_STDEV_V1"


@dataclass(frozen=True, slots=True)
class StandardErrorEstimate:
    value: Decimal
    method_id: str
    lag: int | None = None


@dataclass(frozen=True, slots=True)
class BootstrapConfidenceInterval:
    lower: Decimal
    upper: Decimal
    resample_count: int
    seed: int
    method_id: str = BOOTSTRAP_METHOD_ID


@dataclass(frozen=True, slots=True)
class StatisticalDiagnosticsV2:
    """Wraps the existing, unmodified ``ReplayStatisticalSummary`` -- adds
    only the high-value diagnostics this milestone requires. Optional fields
    (HAC standard error, bootstrap CI) are ``None`` unless explicitly
    requested; they are never computed silently or presented as mandatory."""

    base: ReplayStatisticalSummary
    overlap: OverlapDiagnostics
    standard_error: StandardErrorEstimate | None
    effect_size: EffectSize | None
    bootstrap_confidence_interval: BootstrapConfidenceInterval | None
    grouped: tuple[tuple[str, ReplayStatisticalSummary], ...]
    inference_available: bool
    methodology_notes: tuple[str, ...]


def compute_effect_size(
    *, sample_values: Sequence[Decimal], benchmark_values: Sequence[Decimal]
) -> EffectSize:
    if len(sample_values) < 2 or len(benchmark_values) < 2:
        return EffectSize(cohens_d=None)
    sample_summary = summarize_values(
        tuple(sample_values), kind=SummaryKind.EXPLORATORY, group_label="effect_size_sample"
    )
    benchmark_summary = summarize_values(
        tuple(benchmark_values), kind=SummaryKind.EXPLORATORY, group_label="effect_size_benchmark"
    )
    if (
        sample_summary.stdev is None
        or benchmark_summary.stdev is None
        or sample_summary.mean is None
        or benchmark_summary.mean is None
    ):
        return EffectSize(cohens_d=None)
    n1, n2 = len(sample_values), len(benchmark_values)
    with localcontext() as context:
        context.prec = 28
        pooled_variance = (
            Decimal(n1 - 1) * sample_summary.stdev**2 + Decimal(n2 - 1) * benchmark_summary.stdev**2
        ) / Decimal(n1 + n2 - 2)
        if pooled_variance <= 0:
            return EffectSize(cohens_d=None)
        pooled_stdev = _decimal_sqrt(pooled_variance)
        cohens_d = (sample_summary.mean - benchmark_summary.mean) / pooled_stdev
    return EffectSize(cohens_d=_bounded_decimal(cohens_d))


def newey_west_standard_error(
    values: Sequence[Decimal], *, lag: int
) -> StandardErrorEstimate | None:
    """HAC (heteroskedasticity- and autocorrelation-consistent) standard
    error using a Bartlett kernel -- only meaningful with enough observations
    relative to the requested lag; returns ``None`` rather than an unstable
    estimate when the sample is too thin."""

    if lag < 1:
        raise ResearchMethodologyError("HAC_LAG_INVALID")
    count = len(values)
    if count < max(8, lag * 3):
        return None
    with localcontext() as context:
        context.prec = 28
        mean = sum(values, Decimal()) / Decimal(count)
        centered = [value - mean for value in values]
        gamma0 = sum((item * item for item in centered), Decimal()) / Decimal(count)
        total = gamma0
        for lag_index in range(1, lag + 1):
            weight = Decimal(1) - Decimal(lag_index) / Decimal(lag + 1)
            gamma = sum(
                (centered[t] * centered[t - lag_index] for t in range(lag_index, count)), Decimal()
            ) / Decimal(count)
            total += 2 * weight * gamma
        long_run_variance = max(Decimal(0), total)
        standard_error = _decimal_sqrt(long_run_variance / Decimal(count))
    return StandardErrorEstimate(
        value=_bounded_decimal(standard_error), method_id=HAC_METHOD_ID, lag=lag
    )


def seeded_bootstrap_confidence_interval(
    values: Sequence[Decimal],
    *,
    seed: int,
    resample_count: int = 500,
    confidence: Decimal = Decimal("0.95"),
) -> BootstrapConfidenceInterval | None:
    """Deterministic percentile bootstrap using the stdlib ``random.Random``
    (no third-party numerical dependency is permitted in this runtime) --
    identical seed and identical input always produce an identical interval."""

    count = len(values)
    if count < 8 or not (5 <= resample_count <= 5_000):
        return None
    if not (Decimal("0.5") < confidence < Decimal("1")):
        raise ResearchMethodologyError("BOOTSTRAP_CONFIDENCE_INVALID")
    generator = random.Random(seed)
    ordered = tuple(values)
    means: list[Decimal] = []
    with localcontext() as context:
        context.prec = 28
        for _ in range(resample_count):
            resample = [ordered[generator.randrange(count)] for _ in range(count)]
            means.append(sum(resample, Decimal()) / Decimal(count))
    means.sort()
    tail = (Decimal(1) - confidence) / 2
    lower_rank = int((tail * Decimal(resample_count)).to_integral_value())
    lower_rank = max(0, min(resample_count - 1, lower_rank))
    upper_rank = max(lower_rank, resample_count - 1 - lower_rank)
    return BootstrapConfidenceInterval(
        lower=_bounded_decimal(means[lower_rank]),
        upper=_bounded_decimal(means[upper_rank]),
        resample_count=resample_count,
        seed=seed,
    )


def two_sided_z_test_p_value(
    *,
    sample_mean: Decimal,
    benchmark_mean: Decimal,
    sample_stdev: Decimal,
    effective_sample_count: Decimal,
) -> Decimal | None:
    """A one-sample z-test of H0: sample mean == benchmark mean, using the
    *effective* (non-overlap-adjusted) sample count rather than the raw
    count -- the same normal-approximation methodology already used
    throughout this codebase's statistics, deliberately not an exotic
    distributional assumption."""

    if sample_stdev <= 0 or effective_sample_count < 2:
        return None
    with localcontext() as context:
        context.prec = 28
        standard_error = sample_stdev / _decimal_sqrt(effective_sample_count)
        if standard_error == 0:
            return None
        z = (sample_mean - benchmark_mean) / standard_error
    upper_tail = Decimal(1) - _normal_cdf(abs(z))
    return _bounded_decimal(min(Decimal(1), Decimal(2) * upper_tail))


def build_diagnostics_v2(
    *,
    sample_values: Sequence[Decimal],
    evaluations: Sequence[HistoricalResearchEvaluation],
    outcome_horizon_sessions: int,
    benchmark_values: Sequence[Decimal] | None = None,
    grouped_pairs: tuple[tuple[str, Decimal], ...] = (),
    hac_lag: int | None = None,
    bootstrap_seed: int | None = None,
) -> StatisticalDiagnosticsV2:
    base = summarize_values(tuple(sample_values), kind=SummaryKind.EXPLORATORY, group_label="trial")
    overlap = compute_overlap_diagnostics(
        evaluations, outcome_horizon_sessions=outcome_horizon_sessions
    )
    notes: list[str] = []
    standard_error: StandardErrorEstimate | None = None
    if base.stdev is not None and overlap.effective_sample_count >= 2:
        if hac_lag is not None:
            standard_error = newey_west_standard_error(tuple(sample_values), lag=hac_lag)
            if standard_error is None:
                notes.append("HAC_STANDARD_ERROR_UNAVAILABLE_SAMPLE_TOO_THIN_FOR_REQUESTED_LAG")
        else:
            standard_error = StandardErrorEstimate(
                value=_bounded_decimal(base.stdev / _decimal_sqrt(overlap.effective_sample_count)),
                method_id=IID_SE_METHOD_ID,
            )
    effect_size = (
        compute_effect_size(sample_values=sample_values, benchmark_values=benchmark_values)
        if benchmark_values
        else None
    )
    bootstrap = (
        seeded_bootstrap_confidence_interval(tuple(sample_values), seed=bootstrap_seed)
        if bootstrap_seed is not None
        else None
    )
    grouped = group_summaries(grouped_pairs, kind=SummaryKind.EXPLORATORY) if grouped_pairs else ()
    if not overlap.inference_available:
        notes.append(overlap.note)
    return StatisticalDiagnosticsV2(
        base=base,
        overlap=overlap,
        standard_error=standard_error,
        effect_size=effect_size,
        bootstrap_confidence_interval=bootstrap,
        grouped=grouped,
        inference_available=overlap.inference_available,
        methodology_notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# Research trial: phase + configuration provenance + diagnostics, tied to
# one closed, sorted, content-addressed set of already-retained evaluations.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchTrial:
    trial_id: str
    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    phase: ResearchPhase
    market_scope: tuple[str, ...]
    configuration: ResearchConfiguration
    evaluation_ids: tuple[str, ...]
    session_date_range: tuple[str, str]
    diagnostics: StatisticalDiagnosticsV2
    benchmark: BenchmarkComparison | None
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_version_id: str,
        dataset_snapshot_id: str,
        replay_run_id: str,
        phase: ResearchPhase | str,
        market_scope: tuple[str, ...],
        configuration: ResearchConfiguration,
        dated_evaluation_ids: tuple[tuple[str, str], ...],
        diagnostics: StatisticalDiagnosticsV2,
        benchmark: BenchmarkComparison | None,
        created_at: datetime,
        governance_policy: ResearchGovernancePolicy | None = None,
    ) -> ResearchTrial:
        phase_value = ResearchPhase(phase)
        if not dated_evaluation_ids:
            raise ResearchMethodologyError("TRIAL_REQUIRES_AT_LEAST_ONE_EVALUATION")
        ordered = tuple(sorted(dated_evaluation_ids))
        session_dates = tuple(item[0] for item in ordered)
        evaluation_ids = tuple(sorted({item[1] for item in ordered}))
        if (
            phase_value is ResearchPhase.LOCKED_HOLDOUT
            and configuration.origin is ConfigurationOrigin.EXPLORATORY
        ):
            raise ResearchMethodologyError(
                "EXPLORATORY_CONFIGURATION_CANNOT_BE_EVALUATED_AS_HOLDOUT"
            )
        required_role = _GOVERNED_PHASE_ROLE.get(phase_value)
        if required_role is not None and governance_policy is not None:
            for session_date in session_dates:
                declared_role = governance_policy.period_policy.role_for_date(session_date)
                if declared_role is not required_role:
                    raise ResearchMethodologyError(
                        "TRIAL_EVALUATION_DATE_OUTSIDE_DECLARED_PERIOD_ROLE"
                    )
        scope = tuple(sorted({item.strip().upper() for item in market_scope if item.strip()}))
        if not scope:
            raise ResearchMethodologyError("TRIAL_MARKET_SCOPE_REQUIRED")
        created = _aware(created_at, "TRIAL_CREATED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            hypothesis_version_id,
            dataset_snapshot_id,
            replay_run_id,
            phase_value,
            scope,
            configuration.configuration_id,
            evaluation_ids,
        )
        return cls(
            trial_id=deterministic_id("research_trial", payload),
            hypothesis_version_id=hypothesis_version_id,
            dataset_snapshot_id=dataset_snapshot_id,
            replay_run_id=replay_run_id,
            phase=phase_value,
            market_scope=scope,
            configuration=configuration,
            evaluation_ids=evaluation_ids,
            session_date_range=(session_dates[0], session_dates[-1]),
            diagnostics=diagnostics,
            benchmark=benchmark,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 6: multiple-testing awareness
# --------------------------------------------------------------------------


class TrialSelectionMethod(StrEnum):
    PREDECLARED_SINGLE = "PREDECLARED_SINGLE"
    BEST_OF_FAMILY = "BEST_OF_FAMILY"


class MultipleTestingCorrectionMethod(StrEnum):
    NONE = "NONE"
    BONFERRONI = "BONFERRONI"
    BENJAMINI_HOCHBERG_FDR = "BENJAMINI_HOCHBERG_FDR"


@dataclass(frozen=True, slots=True)
class TrialTestResult:
    trial_id: str
    label: str
    p_value: Decimal | None


@dataclass(frozen=True, slots=True)
class CorrectedTestResult:
    trial_id: str
    label: str
    raw_p_value: Decimal | None
    adjusted_p_value: Decimal | None
    rejected_null: bool | None


def apply_bonferroni_correction(
    results: Sequence[TrialTestResult], *, family_wise_alpha: Decimal = Decimal("0.05")
) -> tuple[CorrectedTestResult, ...]:
    testable = [item for item in results if item.p_value is not None]
    count = len(testable)
    corrected: list[CorrectedTestResult] = []
    for item in results:
        if item.p_value is None or count == 0:
            corrected.append(
                CorrectedTestResult(item.trial_id, item.label, item.p_value, None, None)
            )
            continue
        adjusted = min(Decimal(1), item.p_value * Decimal(count))
        corrected.append(
            CorrectedTestResult(
                trial_id=item.trial_id,
                label=item.label,
                raw_p_value=item.p_value,
                adjusted_p_value=_bounded_decimal(adjusted),
                rejected_null=adjusted <= family_wise_alpha,
            )
        )
    return tuple(corrected)


def apply_benjamini_hochberg_correction(
    results: Sequence[TrialTestResult], *, false_discovery_rate: Decimal = Decimal("0.10")
) -> tuple[CorrectedTestResult, ...]:
    """Standard BH step-up procedure: sort ascending by raw p-value, find the
    largest rank k where p_(k) <= (k/m) * Q, reject every trial at or below
    that rank, and report the usual monotone step-up adjusted p-value for
    each (min over the current and all higher ranks of p_(j) * m / j)."""

    testable = sorted(
        (item for item in results if item.p_value is not None),
        key=lambda item: (item.p_value, item.trial_id),
    )
    m = len(testable)
    by_id: dict[str, CorrectedTestResult] = {}
    if m > 0:
        raw_adjusted = [
            min(Decimal(1), item.p_value * Decimal(m) / Decimal(rank))  # type: ignore[operator]
            for rank, item in enumerate(testable, start=1)
        ]
        adjusted: list[Decimal] = [Decimal(0)] * m
        running_min = Decimal(1)
        for index in range(m - 1, -1, -1):
            running_min = min(running_min, raw_adjusted[index])
            adjusted[index] = running_min
        largest_rejected_rank = 0
        for rank, item in enumerate(testable, start=1):
            threshold = Decimal(rank) / Decimal(m) * false_discovery_rate
            if item.p_value is not None and item.p_value <= threshold:
                largest_rejected_rank = rank
        for index, item in enumerate(testable):
            rank = index + 1
            by_id[item.trial_id] = CorrectedTestResult(
                trial_id=item.trial_id,
                label=item.label,
                raw_p_value=item.p_value,
                adjusted_p_value=_bounded_decimal(adjusted[index]),
                rejected_null=rank <= largest_rejected_rank,
            )
    return tuple(
        by_id.get(item.trial_id)
        or CorrectedTestResult(item.trial_id, item.label, item.p_value, None, None)
        for item in results
    )


def apply_multiple_testing_correction(
    results: Sequence[TrialTestResult],
    *,
    method: MultipleTestingCorrectionMethod,
    family_wise_alpha: Decimal = Decimal("0.05"),
    false_discovery_rate: Decimal = Decimal("0.10"),
) -> tuple[CorrectedTestResult, ...]:
    if method is MultipleTestingCorrectionMethod.BONFERRONI:
        return apply_bonferroni_correction(results, family_wise_alpha=family_wise_alpha)
    if method is MultipleTestingCorrectionMethod.BENJAMINI_HOCHBERG_FDR:
        return apply_benjamini_hochberg_correction(
            results, false_discovery_rate=false_discovery_rate
        )
    return tuple(
        CorrectedTestResult(item.trial_id, item.label, item.p_value, None, None) for item in results
    )


@dataclass(frozen=True, slots=True)
class ResearchTrialFamily:
    trial_family_id: str
    hypothesis_id: str
    member_trial_ids: tuple[str, ...]
    candidate_count: int
    test_count: int
    asset_count: int
    outcome_definition_count: int
    regime_slice_count: int
    selection_method: TrialSelectionMethod
    correction_applied: bool
    correction_method: MultipleTestingCorrectionMethod
    raw_results: tuple[TrialTestResult, ...]
    corrected_results: tuple[CorrectedTestResult, ...]
    uncorrected_warning: str | None
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_id: str,
        candidate_count: int,
        asset_count: int,
        outcome_definition_count: int,
        regime_slice_count: int,
        selection_method: TrialSelectionMethod | str,
        raw_results: tuple[TrialTestResult, ...],
        correction_method: MultipleTestingCorrectionMethod
        | str = MultipleTestingCorrectionMethod.NONE,
        false_discovery_rate: Decimal = Decimal("0.10"),
        family_wise_alpha: Decimal = Decimal("0.05"),
        created_at: datetime,
    ) -> ResearchTrialFamily:
        if not raw_results:
            raise ResearchMethodologyError("TRIAL_FAMILY_REQUIRES_AT_LEAST_ONE_TRIAL")
        member_ids = tuple(sorted({item.trial_id for item in raw_results}))
        test_count = len(raw_results)
        if candidate_count < test_count:
            raise ResearchMethodologyError("CANDIDATE_COUNT_CANNOT_BE_LESS_THAN_TEST_COUNT")
        if asset_count < 1 or outcome_definition_count < 1 or regime_slice_count < 1:
            raise ResearchMethodologyError("TRIAL_FAMILY_DIMENSION_COUNTS_INVALID")
        selection = TrialSelectionMethod(selection_method)
        correction = MultipleTestingCorrectionMethod(correction_method)
        correction_applied = correction is not MultipleTestingCorrectionMethod.NONE
        corrected = apply_multiple_testing_correction(
            raw_results,
            method=correction,
            family_wise_alpha=family_wise_alpha,
            false_discovery_rate=false_discovery_rate,
        )
        warning = None
        if not correction_applied and test_count > 1:
            warning = (
                f"UNCORRECTED_MULTIPLE_TESTING_ACROSS_{test_count}_TRIALS_"
                "RAW_P_VALUES_ARE_NOT_MEANINGFUL_ON_THEIR_OWN"
            )
        created = _aware(created_at, "TRIAL_FAMILY_CREATED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            hypothesis_id,
            member_ids,
            candidate_count,
            asset_count,
            outcome_definition_count,
            regime_slice_count,
            selection,
            correction,
        )
        return cls(
            trial_family_id=deterministic_id("research_trial_family", payload),
            hypothesis_id=hypothesis_id,
            member_trial_ids=member_ids,
            candidate_count=candidate_count,
            test_count=test_count,
            asset_count=asset_count,
            outcome_definition_count=outcome_definition_count,
            regime_slice_count=regime_slice_count,
            selection_method=selection,
            correction_applied=correction_applied,
            correction_method=correction,
            raw_results=tuple(raw_results),
            corrected_results=corrected,
            uncorrected_warning=warning,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 4: walk-forward evaluation engine (pure fold-boundary generation;
# the stepping/persistence orchestration lives in the application layer)
# --------------------------------------------------------------------------


class TrainingWindowMode(StrEnum):
    EXPANDING = "EXPANDING"
    ROLLING = "ROLLING"


class WalkForwardCompletionState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class FoldOutcomeState(StrEnum):
    READY = "READY"
    OUTCOME_PENDING = "OUTCOME_PENDING"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


@dataclass(frozen=True, slots=True)
class FoldWindow:
    fold_ordinal: int
    train_start: str
    train_end: str
    validation_start: str | None
    validation_end: str | None
    test_start: str
    test_end: str


def generate_fold_windows(
    *,
    session_dates: tuple[str, ...],
    training_window_sessions: int,
    validation_window_sessions: int,
    test_window_sessions: int,
    step_sessions: int,
    window_mode: TrainingWindowMode | str,
) -> tuple[FoldWindow, ...]:
    """Pure, deterministic fold-boundary generator over an already-resolved
    ordered list of session dates (the caller resolves this from the bound
    dataset/calendar, exactly as ``CreateReplayRun`` resolves sessions today
    -- this function performs no calendar arithmetic of its own).

    EXPANDING: every fold's training window starts at ``session_dates[0]``
    and grows. ROLLING: every fold's training window has a fixed length and
    slides forward by ``step_sessions``. ``validation_window_sessions == 0``
    omits the validation segment entirely. A fold is only emitted when its
    full train(+validation)+test span fits inside ``session_dates`` -- this
    function never produces a fold whose test window would run past the end
    of the available range."""

    mode = TrainingWindowMode(window_mode)
    if training_window_sessions < 1 or test_window_sessions < 1 or step_sessions < 1:
        raise ResearchMethodologyError("WALK_FORWARD_WINDOW_LENGTHS_INVALID")
    if validation_window_sessions < 0:
        raise ResearchMethodologyError("WALK_FORWARD_VALIDATION_WINDOW_INVALID")
    total = len(session_dates)
    folds: list[FoldWindow] = []
    ordinal = 1
    train_start_index = 0
    test_start_index = training_window_sessions + validation_window_sessions
    while test_start_index + test_window_sessions <= total:
        train_end_index = (
            # EXPANDING: the training window is *everything* before the
            # validation/test segment, so it grows every fold as
            # test_start_index advances -- a fixed offset from
            # train_start_index (which never moves) would not grow at all.
            test_start_index - validation_window_sessions - 1
            if mode is TrainingWindowMode.EXPANDING
            else train_start_index + training_window_sessions - 1
        )
        validation_start_index = train_end_index + 1
        validation_end_index = validation_start_index + validation_window_sessions - 1
        test_end_index = test_start_index + test_window_sessions - 1
        folds.append(
            FoldWindow(
                fold_ordinal=ordinal,
                train_start=session_dates[train_start_index],
                train_end=session_dates[train_end_index],
                validation_start=(
                    session_dates[validation_start_index]
                    if validation_window_sessions > 0
                    else None
                ),
                validation_end=(
                    session_dates[validation_end_index] if validation_window_sessions > 0 else None
                ),
                test_start=session_dates[test_start_index],
                test_end=session_dates[test_end_index],
            )
        )
        ordinal += 1
        if mode is TrainingWindowMode.ROLLING:
            train_start_index += step_sessions
        test_start_index += step_sessions
    return tuple(folds)


@dataclass(frozen=True, slots=True)
class WalkForwardRun:
    """Combines the immutable plan declaration and the mutable progress
    cursor in one record, exactly like ``ReplayRun`` -- the persistence
    layer's guard trigger protects only the cursor/completion/failure/
    payload columns, never the plan columns, so resuming can never silently
    reinterpret what was declared at creation."""

    walk_forward_run_id: str
    replay_run_id: str
    dataset_snapshot_id: str
    universe: tuple[str, ...]
    method_id: ResearchMethodId
    feature_policy_hash: str
    outcome_metric_id: str
    training_window_sessions: int
    validation_window_sessions: int
    test_window_sessions: int
    step_sessions: int
    window_mode: TrainingWindowMode
    minimum_samples: int
    cost_information_state: str
    start_date: str
    end_date: str
    hypothesis_version_id: str | None
    cursor_fold_ordinal: int
    completed_fold_count: int
    completion_state: WalkForwardCompletionState
    failure_reason: str | None
    operator_action_id: str
    idempotency_key: str
    created_at: datetime
    canonical_input_hash: str
    authority: str = "NONE"

    @classmethod
    def draft(
        cls,
        *,
        replay_run_id: str,
        dataset_snapshot_id: str,
        universe: tuple[str, ...],
        method_id: ResearchMethodId | str,
        feature_policy_hash: str,
        outcome_metric_id: str,
        training_window_sessions: int,
        validation_window_sessions: int,
        test_window_sessions: int,
        step_sessions: int,
        window_mode: TrainingWindowMode | str,
        minimum_samples: int,
        cost_information_state: str,
        start_date: str,
        end_date: str,
        hypothesis_version_id: str | None,
        operator_action_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> WalkForwardRun:
        if (
            not replay_run_id
            or not dataset_snapshot_id
            or not operator_action_id
            or not idempotency_key
        ):
            raise ResearchMethodologyError("WALK_FORWARD_RUN_IDENTITY_REQUIRED")
        ordered_universe = tuple(sorted(set(universe)))
        if not ordered_universe:
            raise ResearchMethodologyError("WALK_FORWARD_RUN_UNIVERSE_REQUIRED")
        if training_window_sessions < 1 or test_window_sessions < 1 or step_sessions < 1:
            raise ResearchMethodologyError("WALK_FORWARD_WINDOW_LENGTHS_INVALID")
        if validation_window_sessions < 0:
            raise ResearchMethodologyError("WALK_FORWARD_VALIDATION_WINDOW_INVALID")
        if minimum_samples < 1:
            raise ResearchMethodologyError("WALK_FORWARD_MINIMUM_SAMPLES_INVALID")
        if start_date > end_date:
            raise ResearchMethodologyError("WALK_FORWARD_RUN_DATE_RANGE_INVALID")
        method = ResearchMethodId(method_id)
        mode = TrainingWindowMode(window_mode)
        created = _aware(created_at, "WALK_FORWARD_RUN_CREATED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            replay_run_id,
            dataset_snapshot_id,
            ordered_universe,
            method,
            feature_policy_hash,
            _text(outcome_metric_id, "WALK_FORWARD_OUTCOME_METRIC_INVALID", maximum=160),
            training_window_sessions,
            validation_window_sessions,
            test_window_sessions,
            step_sessions,
            mode,
            minimum_samples,
            _text(cost_information_state, "WALK_FORWARD_COST_STATE_INVALID", maximum=160),
            start_date,
            end_date,
            hypothesis_version_id,
            operator_action_id,
            idempotency_key,
        )
        return cls(
            walk_forward_run_id=deterministic_id("walk_forward_run", payload),
            replay_run_id=replay_run_id,
            dataset_snapshot_id=dataset_snapshot_id,
            universe=ordered_universe,
            method_id=method,
            feature_policy_hash=feature_policy_hash,
            outcome_metric_id=payload[6],
            training_window_sessions=training_window_sessions,
            validation_window_sessions=validation_window_sessions,
            test_window_sessions=test_window_sessions,
            step_sessions=step_sessions,
            window_mode=mode,
            minimum_samples=minimum_samples,
            cost_information_state=payload[13],
            start_date=start_date,
            end_date=end_date,
            hypothesis_version_id=hypothesis_version_id,
            cursor_fold_ordinal=0,
            completed_fold_count=0,
            completion_state=WalkForwardCompletionState.NOT_STARTED,
            failure_reason=None,
            operator_action_id=operator_action_id,
            idempotency_key=idempotency_key,
            created_at=created,
            canonical_input_hash=content_digest(payload),
        )

    def advance(self, *, fold_ordinal: int, is_final: bool) -> WalkForwardRun:
        if self.completion_state is WalkForwardCompletionState.COMPLETE:
            raise ResearchMethodologyError("WALK_FORWARD_RUN_ALREADY_COMPLETE")
        if self.completion_state is WalkForwardCompletionState.FAILED:
            raise ResearchMethodologyError("WALK_FORWARD_RUN_FAILED_CANNOT_ADVANCE")
        if fold_ordinal <= self.cursor_fold_ordinal:
            raise ResearchMethodologyError("WALK_FORWARD_RUN_CURSOR_MUST_ADVANCE_FORWARD")
        return replace(
            self,
            cursor_fold_ordinal=fold_ordinal,
            completed_fold_count=self.completed_fold_count + 1,
            completion_state=(
                WalkForwardCompletionState.COMPLETE
                if is_final
                else WalkForwardCompletionState.IN_PROGRESS
            ),
        )

    def fail(self, *, reason: str) -> WalkForwardRun:
        if self.completion_state is WalkForwardCompletionState.COMPLETE:
            raise ResearchMethodologyError("WALK_FORWARD_RUN_ALREADY_COMPLETE")
        return replace(
            self,
            completion_state=WalkForwardCompletionState.FAILED,
            failure_reason=_text(reason, "WALK_FORWARD_RUN_FAILURE_REASON_INVALID", maximum=500),
        )


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One immutable, append-only fold result. ``strict_cutoff`` is the
    instant beyond which the fold's train/validation development may never
    read: it is always at or before ``test_start``, and ``test_start`` is
    always strictly after ``train_end`` (and ``validation_end`` when
    present) -- checked structurally below, not only documented."""

    fold_id: str
    walk_forward_run_id: str
    fold_ordinal: int
    train_start: str
    train_end: str
    validation_start: str | None
    validation_end: str | None
    test_start: str
    test_end: str
    strict_cutoff: str
    train_summary: ReplayStatisticalSummary | None
    validation_summary: ReplayStatisticalSummary | None
    outcome_state: FoldOutcomeState
    test_trial_id: str | None
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        run: WalkForwardRun,
        window: FoldWindow,
        train_summary: ReplayStatisticalSummary | None,
        validation_summary: ReplayStatisticalSummary | None,
        outcome_state: FoldOutcomeState | str,
        test_trial_id: str | None,
        created_at: datetime,
    ) -> WalkForwardFold:
        if window.test_start <= window.train_end:
            raise ResearchMethodologyError("FOLD_TEST_WINDOW_MUST_START_STRICTLY_AFTER_TRAIN_END")
        if window.validation_end is not None and window.test_start <= window.validation_end:
            raise ResearchMethodologyError(
                "FOLD_TEST_WINDOW_MUST_START_STRICTLY_AFTER_VALIDATION_END"
            )
        strict_cutoff = window.validation_end or window.train_end
        state = FoldOutcomeState(outcome_state)
        if state is FoldOutcomeState.READY and test_trial_id is None:
            raise ResearchMethodologyError("READY_FOLD_REQUIRES_A_TEST_TRIAL")
        created = _aware(created_at, "FOLD_CREATED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            run.walk_forward_run_id,
            window.fold_ordinal,
            window.train_start,
            window.train_end,
            window.validation_start,
            window.validation_end,
            window.test_start,
            window.test_end,
            test_trial_id,
        )
        return cls(
            fold_id=deterministic_id("walk_forward_fold", payload),
            walk_forward_run_id=run.walk_forward_run_id,
            fold_ordinal=window.fold_ordinal,
            train_start=window.train_start,
            train_end=window.train_end,
            validation_start=window.validation_start,
            validation_end=window.validation_end,
            test_start=window.test_start,
            test_end=window.test_end,
            strict_cutoff=strict_cutoff,
            train_summary=train_summary,
            validation_summary=validation_summary,
            outcome_state=state,
            test_trial_id=test_trial_id,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    walk_forward_run_id: str
    fold_count: int
    ready_fold_count: int
    test_outcome_means: tuple[Decimal, ...]
    aggregate_test_summary: ReplayStatisticalSummary
    consistent_direction_fraction: Decimal | None


def summarize_walk_forward_folds(
    folds: Sequence[WalkForwardFold], *, test_trial_means: tuple[tuple[str, Decimal], ...]
) -> WalkForwardSummary:
    """``test_trial_means`` is ``(trial_id, mean_test_outcome)`` for every
    ready fold's test-window trial -- the caller (application layer) reads
    these from the already-built ``ResearchTrial`` records; this function
    never recomputes a fold's underlying statistics itself."""

    if not folds:
        raise ResearchMethodologyError("WALK_FORWARD_SUMMARY_REQUIRES_AT_LEAST_ONE_FOLD")
    ready = tuple(item for item in folds if item.outcome_state is FoldOutcomeState.READY)
    means_by_trial = dict(test_trial_means)
    values = tuple(
        means_by_trial[item.test_trial_id]
        for item in ready
        if item.test_trial_id is not None and item.test_trial_id in means_by_trial
    )
    aggregate = summarize_values(
        values, kind=SummaryKind.WALK_FORWARD_AGGREGATE, group_label="test_folds"
    )
    consistent: Decimal | None = None
    if len(values) >= 2:
        positive = sum(1 for item in values if item > 0)
        negative = sum(1 for item in values if item < 0)
        consistent = _bounded_decimal(Decimal(max(positive, negative)) / Decimal(len(values)))
    return WalkForwardSummary(
        walk_forward_run_id=folds[0].walk_forward_run_id,
        fold_count=len(folds),
        ready_fold_count=len(ready),
        test_outcome_means=values,
        aggregate_test_summary=aggregate,
        consistent_direction_fraction=consistent,
    )


# --------------------------------------------------------------------------
# Section 11: robustness checks (reusable, not strategy-specific)
# --------------------------------------------------------------------------


class RobustnessCheckType(StrEnum):
    ALTERNATE_LOOKBACK = "ALTERNATE_LOOKBACK"
    NEIGHBORING_THRESHOLD = "NEIGHBORING_THRESHOLD"
    SUBPERIOD_SPLIT = "SUBPERIOD_SPLIT"
    REGIME_SPLIT = "REGIME_SPLIT"
    LEAVE_ONE_PERIOD_OUT = "LEAVE_ONE_PERIOD_OUT"
    COST_STRESS = "COST_STRESS"
    ASSET_COHORT_EXCLUSION = "ASSET_COHORT_EXCLUSION"
    MISSING_DATA_SENSITIVITY = "MISSING_DATA_SENSITIVITY"
    # Added for Cross-Market Research v3 (``domain.corporate_market_research``):
    # corporate growth/earnings values can contain extreme observations
    # (milestone section 19), so a genuinely bounded-winsorization and a
    # median-vs-mean comparison join the existing battery above -- both
    # reuse this identical ``RobustnessCheckResult`` shape and
    # ``summarize_robustness`` majority-consistency rule unmodified.
    WINSORIZED_SENSITIVITY = "WINSORIZED_SENSITIVITY"
    MEDIAN_VS_MEAN_COMPARISON = "MEDIAN_VS_MEAN_COMPARISON"


@dataclass(frozen=True, slots=True)
class RobustnessCheckResult:
    check_type: RobustnessCheckType
    variant_label: str
    sample_count: int
    result_mean: Decimal | None
    direction_consistent_with_base: bool | None
    note: str


@dataclass(frozen=True, slots=True)
class RobustnessSummary:
    checks_run: int
    checks_consistent: int
    checks_inconsistent: int
    checks_insufficient_data: int
    passed: bool
    results: tuple[RobustnessCheckResult, ...]


def _direction_consistent(base_mean: Decimal, candidate_mean: Decimal | None) -> bool | None:
    if candidate_mean is None or base_mean == 0:
        return None
    return (candidate_mean > 0) == (base_mean > 0)


def summarize_robustness(results: Sequence[RobustnessCheckResult]) -> RobustnessSummary:
    """Passes when there is nothing evaluable to contradict the base result,
    or when at least three in five evaluable checks agree in direction with
    it -- a simple, explicit majority-consistency bar, not a demand that
    every single perturbation agree."""

    consistent = sum(1 for item in results if item.direction_consistent_with_base is True)
    inconsistent = sum(1 for item in results if item.direction_consistent_with_base is False)
    insufficient = sum(1 for item in results if item.direction_consistent_with_base is None)
    evaluable = consistent + inconsistent
    passed = evaluable == 0 or Decimal(consistent) / Decimal(evaluable) >= Decimal("0.6")
    return RobustnessSummary(
        checks_run=len(results),
        checks_consistent=consistent,
        checks_inconsistent=inconsistent,
        checks_insufficient_data=insufficient,
        passed=bool(passed),
        results=tuple(results),
    )


@dataclass(frozen=True, slots=True)
class RobustnessCheckRun:
    robustness_run_id: str
    base_trial_id: str
    summary: RobustnessSummary
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        base_trial_id: str,
        results: tuple[RobustnessCheckResult, ...],
        created_at: datetime,
    ) -> RobustnessCheckRun:
        if not results:
            raise ResearchMethodologyError("ROBUSTNESS_RUN_REQUIRES_AT_LEAST_ONE_CHECK")
        created = _aware(created_at, "ROBUSTNESS_RUN_CREATED_AT")
        summary = summarize_robustness(results)
        payload = (
            RESEARCH_METHODOLOGY_V1,
            base_trial_id,
            tuple(
                (item.check_type, item.variant_label, item.sample_count, str(item.result_mean))
                for item in results
            ),
        )
        return cls(
            robustness_run_id=deterministic_id("robustness_check_run", payload),
            base_trial_id=base_trial_id,
            summary=summary,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def _complete_with_outcome(
    evaluations: Sequence[HistoricalResearchEvaluation],
) -> tuple[HistoricalResearchEvaluation, ...]:
    return tuple(
        item
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )


def _outcome_values(evaluations: Sequence[HistoricalResearchEvaluation]) -> tuple[Decimal, ...]:
    """Extracts outcome values from evaluations, tolerating (rather than
    assuming) an incomplete/missing outcome -- a properly narrowed
    ``tuple[Decimal, ...]`` return type, unlike accessing
    ``item.outcome.outcome_value`` inline against a still-``Decimal | None``
    field."""

    values: list[Decimal] = []
    for item in evaluations:
        if item.outcome is not None and item.outcome.outcome_value is not None:
            values.append(item.outcome.outcome_value)
    return tuple(values)


def robustness_subperiod_split(
    evaluations: Sequence[HistoricalResearchEvaluation], *, base_mean: Decimal, split_count: int = 2
) -> tuple[RobustnessCheckResult, ...]:
    complete = sorted(_complete_with_outcome(evaluations), key=lambda item: item.as_of)
    if not complete:
        return (
            RobustnessCheckResult(
                RobustnessCheckType.SUBPERIOD_SPLIT,
                "no_data",
                0,
                None,
                None,
                "NO_COMPLETE_OUTCOMES",
            ),
        )
    chunk_size = max(1, len(complete) // split_count)
    results: list[RobustnessCheckResult] = []
    for index in range(split_count):
        start = index * chunk_size
        end = len(complete) if index == split_count - 1 else start + chunk_size
        chunk = complete[start:end]
        values = _outcome_values(chunk)
        mean = _bounded_decimal(sum(values, Decimal()) / Decimal(len(values))) if values else None
        results.append(
            RobustnessCheckResult(
                check_type=RobustnessCheckType.SUBPERIOD_SPLIT,
                variant_label=f"subperiod_{index + 1}_of_{split_count}",
                sample_count=len(values),
                result_mean=mean,
                direction_consistent_with_base=_direction_consistent(base_mean, mean),
                note="OK" if values else "INSUFFICIENT_SUBPERIOD_DATA",
            )
        )
    return tuple(results)


def robustness_regime_split(
    evaluations: Sequence[HistoricalResearchEvaluation],
    frames: Sequence[ReplayFeatureFrame],
    *,
    base_mean: Decimal,
    dimension_label: str = "RISK_TONE",
) -> tuple[RobustnessCheckResult, ...]:
    frames_by_id = {item.frame_id: item for item in frames}
    grouped: dict[str, list[Decimal]] = {}
    for item in _complete_with_outcome(evaluations):
        if item.outcome is None or item.outcome.outcome_value is None:
            continue
        frame = frames_by_id.get(item.frame_id)
        if frame is None:
            continue
        label = next(
            (
                classification.label
                for classification in frame.regime.classifications
                if classification.dimension.value == dimension_label
            ),
            None,
        )
        if label is None:
            continue
        grouped.setdefault(label, []).append(item.outcome.outcome_value)
    if not grouped:
        return (
            RobustnessCheckResult(
                RobustnessCheckType.REGIME_SPLIT,
                "no_data",
                0,
                None,
                None,
                "REGIME_EVIDENCE_UNAVAILABLE",
            ),
        )
    results = []
    for label, values in sorted(grouped.items()):
        mean = _bounded_decimal(sum(values, Decimal()) / Decimal(len(values)))
        results.append(
            RobustnessCheckResult(
                check_type=RobustnessCheckType.REGIME_SPLIT,
                variant_label=label,
                sample_count=len(values),
                result_mean=mean,
                direction_consistent_with_base=_direction_consistent(base_mean, mean),
                note="OK",
            )
        )
    return tuple(results)


def robustness_leave_one_period_out(
    evaluations: Sequence[HistoricalResearchEvaluation],
    *,
    base_mean: Decimal,
    period_count: int = 4,
) -> tuple[RobustnessCheckResult, ...]:
    complete = sorted(_complete_with_outcome(evaluations), key=lambda item: item.as_of)
    if len(complete) < period_count:
        return (
            RobustnessCheckResult(
                RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
                "insufficient_data",
                len(complete),
                None,
                None,
                "TOO_FEW_OBSERVATIONS_FOR_LEAVE_ONE_PERIOD_OUT",
            ),
        )
    chunk_size = max(1, len(complete) // period_count)
    results = []
    for index in range(period_count):
        start = index * chunk_size
        end = len(complete) if index == period_count - 1 else start + chunk_size
        remaining = complete[:start] + complete[end:]
        values = _outcome_values(remaining)
        mean = _bounded_decimal(sum(values, Decimal()) / Decimal(len(values))) if values else None
        results.append(
            RobustnessCheckResult(
                check_type=RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
                variant_label=f"excluding_period_{index + 1}_of_{period_count}",
                sample_count=len(values),
                result_mean=mean,
                direction_consistent_with_base=_direction_consistent(base_mean, mean),
                note="OK" if values else "INSUFFICIENT_DATA",
            )
        )
    return tuple(results)


def robustness_asset_cohort_exclusion(
    evaluations: Sequence[HistoricalResearchEvaluation],
    *,
    base_mean: Decimal,
    excluded_hint: str | None = None,
) -> tuple[RobustnessCheckResult, ...]:
    """The three engineering research methods this milestone ships are
    universe-level (breadth) or single-benchmark (volatility, crypto-equity
    comparison) rather than per-asset -- an asset/cohort exclusion is only
    meaningful for a per-asset method. Reported honestly as
    ``NOT_APPLICABLE`` rather than fabricated when no per-asset breakdown
    exists in the supplied evaluations."""

    if excluded_hint is None:
        return (
            RobustnessCheckResult(
                check_type=RobustnessCheckType.ASSET_COHORT_EXCLUSION,
                variant_label="not_applicable",
                sample_count=0,
                result_mean=None,
                direction_consistent_with_base=None,
                note="NOT_APPLICABLE_METHOD_IS_UNIVERSE_LEVEL_OR_SINGLE_BENCHMARK_NOT_PER_ASSET",
            ),
        )
    remaining = tuple(
        item
        for item in _complete_with_outcome(evaluations)
        if not any(value == excluded_hint for _key, value in item.result)
    )
    values = _outcome_values(remaining)
    mean = _bounded_decimal(sum(values, Decimal()) / Decimal(len(values))) if values else None
    return (
        RobustnessCheckResult(
            check_type=RobustnessCheckType.ASSET_COHORT_EXCLUSION,
            variant_label=f"excluding_{excluded_hint}",
            sample_count=len(values),
            result_mean=mean,
            direction_consistent_with_base=_direction_consistent(base_mean, mean),
            note="OK" if values else "INSUFFICIENT_DATA_AFTER_EXCLUSION",
        ),
    )


def robustness_missing_data_sensitivity(
    evaluations: Sequence[HistoricalResearchEvaluation], *, base_mean: Decimal
) -> RobustnessCheckResult:
    complete = _complete_with_outcome(evaluations)
    clean = tuple(item for item in complete if not item.blockers)
    if not clean:
        return RobustnessCheckResult(
            check_type=RobustnessCheckType.MISSING_DATA_SENSITIVITY,
            variant_label="blocker_free_subset",
            sample_count=0,
            result_mean=None,
            direction_consistent_with_base=None,
            note="NO_BLOCKER_FREE_OBSERVATIONS",
        )
    values = _outcome_values(clean)
    mean = _bounded_decimal(sum(values, Decimal()) / Decimal(len(values)))
    return RobustnessCheckResult(
        check_type=RobustnessCheckType.MISSING_DATA_SENSITIVITY,
        variant_label="blocker_free_subset",
        sample_count=len(values),
        result_mean=mean,
        direction_consistent_with_base=_direction_consistent(base_mean, mean),
        note="OK",
    )


def robustness_cost_stress() -> RobustnessCheckResult:
    """None of this milestone's descriptive research methods model
    transaction costs -- research evaluation is not execution. Reported
    honestly as unavailable rather than skipped silently or fabricated."""

    return RobustnessCheckResult(
        check_type=RobustnessCheckType.COST_STRESS,
        variant_label="not_applicable",
        sample_count=0,
        result_mean=None,
        direction_consistent_with_base=None,
        note=COST_DATA_UNAVAILABLE,
    )


def label_momentum_signal(*, signal_return: Decimal | None, positive_threshold: Decimal) -> str:
    if signal_return is None:
        return "SIGNAL_UNAVAILABLE"
    return (
        "POSITIVE_MOMENTUM_SIGNAL"
        if signal_return > positive_threshold
        else "NOT_POSITIVE_MOMENTUM_SIGNAL"
    )


def pair_momentum_signal_with_breadth_outcome(
    frames: Sequence[ReplayFeatureFrame],
    evaluations: Sequence[HistoricalResearchEvaluation],
    *,
    benchmark_stable_security_id: str,
    lookback_window: int,
    positive_threshold: Decimal,
) -> tuple[tuple[str, str, Decimal], ...]:
    """Genuinely reuses the *already-computed, already point-in-time-safe*
    subsequent-benchmark-return outcome the existing
    ``BREADTH_VS_SUBSEQUENT_RETURN_V1`` method already attaches -- no new
    bar access, no new outcome computation, and no change to the replay
    engine. Only the *signal* (a trailing-return momentum reading, read
    directly from the frame's already-computed feature vector) is new, and
    it is deliberately parameterized so ``ALTERNATE_LOOKBACK`` and
    ``NEIGHBORING_THRESHOLD`` robustness checks have something genuine to
    perturb. Returns ``(session_date, label, outcome_value)`` triples."""

    frames_by_id = {item.frame_id: item for item in frames}
    pairs: list[tuple[str, str, Decimal]] = []
    for item in evaluations:
        if item.method_id is not ResearchMethodId.BREADTH_VS_SUBSEQUENT_RETURN_V1:
            continue
        if (
            item.outcome is None
            or item.outcome.completeness is not OutcomeCompletenessState.COMPLETE
            or item.outcome.outcome_value is None
        ):
            continue
        frame = frames_by_id.get(item.frame_id)
        if frame is None:
            continue
        security = frame.security(benchmark_stable_security_id)
        signal = (
            None
            if security is None
            else next(
                (value for window, value in security.returns if window == lookback_window), None
            )
        )
        label = label_momentum_signal(signal_return=signal, positive_threshold=positive_threshold)
        if label == "SIGNAL_UNAVAILABLE":
            continue
        pairs.append((frame.session_date, label, item.outcome.outcome_value))
    return tuple(pairs)


# --------------------------------------------------------------------------
# Section 12: research conclusion model
# --------------------------------------------------------------------------


class ResearchConclusionState(StrEnum):
    SUPPORTED = "SUPPORTED"
    WEAK_SUPPORT = "WEAK_SUPPORT"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"
    ROBUSTNESS_FAILED = "ROBUSTNESS_FAILED"
    HOLDOUT_FAILED = "HOLDOUT_FAILED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def derive_conclusion_state(
    *,
    sample_quality: SampleQualityState,
    robustness_summary: RobustnessSummary | None,
    validation_beats_benchmark: bool | None,
    holdout_beats_benchmark: bool | None,
    multiple_testing_corrected_significant: bool | None,
) -> ResearchConclusionState:
    """Explicit, ordered priority: a failure of sample adequacy, robustness,
    or holdout confirmation always takes precedence over a nominally
    positive raw result -- a method cannot be called supported merely
    because its headline number looks good."""

    if sample_quality is not SampleQualityState.SUFFICIENT:
        return ResearchConclusionState.INSUFFICIENT_EVIDENCE
    if robustness_summary is not None and not robustness_summary.passed:
        return ResearchConclusionState.ROBUSTNESS_FAILED
    if holdout_beats_benchmark is False:
        return ResearchConclusionState.HOLDOUT_FAILED
    if validation_beats_benchmark is None:
        return ResearchConclusionState.INCONCLUSIVE
    if validation_beats_benchmark is False:
        return ResearchConclusionState.REJECTED
    if holdout_beats_benchmark is True and multiple_testing_corrected_significant is True:
        return ResearchConclusionState.SUPPORTED
    if holdout_beats_benchmark is True or multiple_testing_corrected_significant is True:
        return ResearchConclusionState.WEAK_SUPPORT
    if holdout_beats_benchmark is None:
        return ResearchConclusionState.WEAK_SUPPORT
    return ResearchConclusionState.INCONCLUSIVE


@dataclass(frozen=True, slots=True)
class ResearchConclusion:
    conclusion_id: str
    hypothesis_version_id: str
    dataset_snapshot_id: str
    replay_run_id: str
    phase: ResearchPhase
    training_trial_id: str | None
    validation_trial_id: str | None
    holdout_trial_id: str | None
    walk_forward_run_id: str | None
    walk_forward_fold_count: int
    robustness_run_id: str | None
    trial_family_id: str | None
    sample_quality_state: SampleQualityState
    point_in_time_limitations: tuple[str, ...]
    benchmark_kind: BenchmarkKind | None
    beats_benchmark: bool | None
    conclusion_state: ResearchConclusionState
    rationale: str
    blockers: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_version_id: str,
        dataset_snapshot_id: str,
        replay_run_id: str,
        phase: ResearchPhase | str,
        training_trial_id: str | None,
        validation_trial_id: str | None,
        holdout_trial_id: str | None,
        walk_forward_run_id: str | None,
        walk_forward_fold_count: int,
        robustness_run_id: str | None,
        trial_family_id: str | None,
        sample_quality_state: SampleQualityState | str,
        point_in_time_limitations: tuple[str, ...],
        benchmark_kind: BenchmarkKind | str | None,
        beats_benchmark: bool | None,
        conclusion_state: ResearchConclusionState | str,
        rationale: str,
        blockers: tuple[str, ...],
        created_at: datetime,
    ) -> ResearchConclusion:
        if not hypothesis_version_id or not dataset_snapshot_id or not replay_run_id:
            raise ResearchMethodologyError("CONCLUSION_LINEAGE_REQUIRED")
        if walk_forward_fold_count < 0:
            raise ResearchMethodologyError("CONCLUSION_FOLD_COUNT_INVALID")
        created = _aware(created_at, "CONCLUSION_CREATED_AT")
        phase_value = ResearchPhase(phase)
        quality = SampleQualityState(sample_quality_state)
        state = ResearchConclusionState(conclusion_state)
        kind = BenchmarkKind(benchmark_kind) if benchmark_kind is not None else None
        limitations = tuple(sorted(set(point_in_time_limitations)))
        block_values = tuple(sorted(set(blockers)))
        payload = (
            RESEARCH_METHODOLOGY_V1,
            hypothesis_version_id,
            dataset_snapshot_id,
            replay_run_id,
            phase_value,
            training_trial_id,
            validation_trial_id,
            holdout_trial_id,
            walk_forward_run_id,
            walk_forward_fold_count,
            robustness_run_id,
            trial_family_id,
            quality,
            limitations,
            kind,
            beats_benchmark,
            state,
            _text(rationale, "CONCLUSION_RATIONALE_INVALID", maximum=2_000),
            block_values,
            created,
        )
        return cls(
            conclusion_id=deterministic_id("research_conclusion", payload),
            hypothesis_version_id=hypothesis_version_id,
            dataset_snapshot_id=dataset_snapshot_id,
            replay_run_id=replay_run_id,
            phase=phase_value,
            training_trial_id=training_trial_id,
            validation_trial_id=validation_trial_id,
            holdout_trial_id=holdout_trial_id,
            walk_forward_run_id=walk_forward_run_id,
            walk_forward_fold_count=walk_forward_fold_count,
            robustness_run_id=robustness_run_id,
            trial_family_id=trial_family_id,
            sample_quality_state=quality,
            point_in_time_limitations=limitations,
            benchmark_kind=kind,
            beats_benchmark=beats_benchmark,
            conclusion_state=state,
            rationale=payload[17],
            blockers=block_values,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Section 13: strategy-promotion boundary (fails closed; grants no authority)
# --------------------------------------------------------------------------

_NON_PROMOTABLE_CONCLUSION_STATES = frozenset(
    {
        ResearchConclusionState.INCONCLUSIVE,
        ResearchConclusionState.REJECTED,
        ResearchConclusionState.ROBUSTNESS_FAILED,
        ResearchConclusionState.HOLDOUT_FAILED,
        ResearchConclusionState.INSUFFICIENT_EVIDENCE,
    }
)


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """``require_holdout_consumed`` is an explicit, documented policy
    decision (spec section 13): defaults to ``False`` -- a locked holdout
    must be *declared* to promote, but this default does not additionally
    require it to already have been *exposed*. A deployment may set this
    ``True`` to require actual holdout consumption before promotion."""

    policy_id: str = PROMOTION_POLICY_ID
    require_holdout_consumed: bool = False


# ``PromotionPolicy`` above stays generic/configurable (e.g. for exploratory
# or test evaluation) -- this is the one, canonical, *named* policy any
# caller intending to evaluate a real PAPER-strategy candidate must use.
# Unlike the generic default, it fixes ``require_holdout_consumed = True``:
# a declared-but-unconsumed holdout is not enough to reach the production
# track, only an actually-exposed one is. Exploratory research keeps using
# the generic ``PromotionPolicy()`` default (or any other configuration) --
# it was never eligible for promotion regardless of holdout consumption
# (``PHASE_MUST_NOT_BE_EXPLORATORY`` already fails it closed), so this
# stricter policy changes nothing for exploratory work and only tightens
# the boundary that actually matters: a validation/holdout-phase conclusion
# that looks otherwise excellent but never actually consumed its locked
# holdout. This is a fixed, distinct identity, never mutated in place --
# a future stricter production policy gets its own new id and constant,
# this one keeps meaning exactly what it means today.
APEX_PRODUCTION_PROMOTION_POLICY_ID = "APEX_PRODUCTION_PAPER_CANDIDATE_POLICY_V1"
APEX_PRODUCTION_PROMOTION_POLICY = PromotionPolicy(
    policy_id=APEX_PRODUCTION_PROMOTION_POLICY_ID, require_holdout_consumed=True
)


@dataclass(frozen=True, slots=True)
class PromotionEvaluation:
    promotion_evaluation_id: str
    conclusion_id: str
    policy_id: str
    eligible: bool
    reasons: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        conclusion: ResearchConclusion,
        policy: PromotionPolicy,
        holdout_exposed: bool,
        robustness_completed: bool,
        lookahead_violation: bool,
        created_at: datetime,
    ) -> PromotionEvaluation:
        reasons: list[str] = []
        if conclusion.phase is ResearchPhase.EXPLORATORY:
            reasons.append("PHASE_MUST_NOT_BE_EXPLORATORY")
        if conclusion.sample_quality_state is not SampleQualityState.SUFFICIENT:
            reasons.append("EFFECTIVE_SAMPLE_INSUFFICIENT")
        if conclusion.validation_trial_id is None:
            reasons.append("VALIDATION_EVIDENCE_MISSING")
        if conclusion.holdout_trial_id is None:
            reasons.append("LOCKED_HOLDOUT_NOT_DECLARED")
        if lookahead_violation:
            reasons.append("LOOKAHEAD_VIOLATION_DETECTED")
        if conclusion.blockers:
            reasons.append("UNRESOLVED_DATA_QUALITY_BLOCKER")
        if not robustness_completed:
            reasons.append("ROBUSTNESS_CHECKS_NOT_COMPLETED")
        if conclusion.benchmark_kind is None:
            reasons.append("BENCHMARK_COMPARISON_UNAVAILABLE")
        if conclusion.trial_family_id is None:
            reasons.append("MULTIPLE_TESTING_STATE_NOT_DISCLOSED")
        if policy.require_holdout_consumed and not holdout_exposed:
            reasons.append("HOLDOUT_NOT_YET_CONSUMED")
        if conclusion.conclusion_state in _NON_PROMOTABLE_CONCLUSION_STATES:
            reasons.append("CONCLUSION_STATE_DOES_NOT_SUPPORT_PROMOTION")
        eligible = not reasons
        created = _aware(created_at, "PROMOTION_EVALUATION_CREATED_AT")
        payload = (
            RESEARCH_METHODOLOGY_V1,
            conclusion.conclusion_id,
            policy.policy_id,
            policy.require_holdout_consumed,
            eligible,
            tuple(sorted(reasons)),
        )
        return cls(
            promotion_evaluation_id=deterministic_id("research_promotion_evaluation", payload),
            conclusion_id=conclusion.conclusion_id,
            policy_id=policy.policy_id,
            eligible=eligible,
            reasons=tuple(sorted(reasons)),
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def research_methodology_warnings() -> tuple[str, ...]:
    return (METHODOLOGY_WARNING,)
