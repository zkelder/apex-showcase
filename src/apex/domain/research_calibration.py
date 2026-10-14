"""Research Signal Calibration and Negative-Control Hardening v1.

Answers one question honestly: *when Apex's research methodology says a
result is interesting, how often would it have said the same thing about
noise?* This module never touches the retained 25-hypothesis Cross-Market
Research v1 agenda (``domain.cross_market_research``) -- it declares a
separate, additive, bounded null/negative-control battery, runs each
control through the *same* governed statistics used for genuine hypotheses
(``domain.research_methodology``'s trial/robustness/conclusion/promotion
machinery, entirely reused and unmodified), and retains an honest empirical
false-discovery calibration plus a derived, non-destructive reassessment of
the prior agenda's already-retained conclusions.

Every record here is ``authority = "NONE"``. Nothing here creates a
Strategy, Shared Opportunity, PAPER order, execution authority, or new
research budget for the prior agenda -- it only asks how much the prior
agenda's own findings should be trusted, and calibrates the vocabulary used
to answer that question. No historical ``ResearchConclusion`` or
``ResearchKnowledgeEntry`` is ever mutated; every recalibration produced
here is a new, separate, retained record that references the original by
id.

This is methodology calibration, not strategy research. This module makes
no probability, edge, or alpha claim; it only measures how the existing
methodology behaves against constructions known (or strongly suspected) to
carry no genuine predictive relationship.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.cross_market_research import (
    DatedValue,
    FeatureFamily,
    KnowledgeStatus,
    OutcomeFamily,
)
from apex.domain.research_methodology import (
    MultipleTestingCorrectionMethod,
    ResearchConclusionState,
    bound_statistical_decimal,
)
from apex.domain.values import content_digest, deterministic_id

RESEARCH_SIGNAL_CALIBRATION_V1 = "RESEARCH_SIGNAL_CALIBRATION_V1"
CALIBRATION_WARNING = (
    "RESEARCH SIGNAL CALIBRATION EVIDENCE ONLY -- METHODOLOGY CALIBRATION, NOT STRATEGY "
    "RESEARCH; NO STRATEGY, PAPER, OR LIVE AUTHORITY; GRANTS NO EXECUTION AUTHORITY"
)
CALIBRATION_ALPHA_DISCLAIMER = (
    "A calibration result never claims trading edge, alpha, or profitability -- it measures "
    "only how often the current research methodology's own vocabulary would apply itself to "
    "constructions known or strongly suspected to carry no genuine predictive relationship."
)


class CalibrationError(ValueError):
    """A calibration domain invariant was violated."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CalibrationError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _text(value: str, code: str, *, maximum: int = 500) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CalibrationError(code)
    return normalized


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        if value < 0:
            raise CalibrationError("CALIBRATION_SQRT_NEGATIVE_INPUT")
        return +value.sqrt()


# ==========================================================================
# Section 1: null-generation primitives (pure, deterministic, additive to
# ``domain.cross_market_research.deterministic_shuffle`` / ``lag_displace`` /
# ``deterministic_noise_series``, which this milestone's controls reuse
# directly rather than reimplementing). Every function here is a pure
# transform of an already-retained ``DatedValue`` series -- never a new
# provider request, never future-looking relative to the source series.
# ==========================================================================


def block_permute(
    series: Sequence[DatedValue], *, seed: int, block_length: int
) -> tuple[DatedValue, ...]:
    """Splits ``series`` into contiguous, chronologically ordered blocks of
    ``block_length`` observations and deterministically shuffles the
    *order of the blocks* (never the order of values within a block) using
    a fixed, documented seed. Unlike an IID date shuffle, this preserves
    within-block short-range autocorrelation while still destroying the
    genuine date alignment between a feature and same-date evidence --
    the "at least one block-based null method" this milestone requires
    (section 3), since realistic time series are rarely IID."""

    if block_length < 1:
        raise CalibrationError("BLOCK_PERMUTE_BLOCK_LENGTH_INVALID")
    ordered = tuple(sorted(series, key=lambda item: item.session_date))
    dates = [item.session_date for item in ordered]
    values = [item.value for item in ordered]
    blocks = [values[index : index + block_length] for index in range(0, len(values), block_length)]
    random.Random(seed).shuffle(blocks)
    permuted_values = [value for block in blocks for value in block]
    return tuple(
        DatedValue(date, value) for date, value in zip(dates, permuted_values, strict=True)
    )


def sign_randomize(series: Sequence[DatedValue], *, seed: int) -> tuple[DatedValue, ...]:
    """Deterministically flips the sign of each observation with
    probability one-half using a fixed seed, preserving the magnitude
    (and therefore the marginal volatility) of ``series`` while destroying
    any genuine directional/predictive content -- the "sign/randomized
    series transformation" this milestone's deterministic-permutation
    family names explicitly (section 3)."""

    generator = random.Random(seed)
    dates = [item.session_date for item in series]
    flipped = [(-item.value if generator.random() < 0.5 else item.value) for item in series]
    return tuple(DatedValue(date, value) for date, value in zip(dates, flipped, strict=True))


def synthetic_ar1_surrogate(series: Sequence[DatedValue], *, seed: int) -> tuple[DatedValue, ...]:
    """Generates a deterministic seeded AR(1) surrogate series over the
    *same dates* as ``series`` (missingness preserved exactly: a ``None``
    stays ``None``), fit only to ``series``'s own retained mean, sample
    volatility, and lag-1 autocorrelation -- never to any other market's
    evidence. This is a genuinely synthetic series: its date-by-date path,
    and therefore any relationship it might show to a real outcome, carries
    no information from any genuine market observation beyond those three
    summary statistics. Preserves: mean, volatility, lag-1 autocorrelation
    (approximately, by construction). Destroys: the true historical path,
    regime timing, and any higher-order dependence structure."""

    dates = [item.session_date for item in series]
    values = [item.value for item in series]
    count = len(values)
    if count < 3:
        fallback = values[0] if values else Decimal(0)
        return tuple(DatedValue(date, fallback) for date in dates)
    with localcontext() as context:
        context.prec = 28
        mean = sum(values, Decimal(0)) / Decimal(count)
        variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / Decimal(count)
        stdev = _decimal_sqrt(variance)
        if stdev == 0:
            phi = Decimal(0)
        else:
            numerator = sum(
                ((values[i] - mean) * (values[i - 1] - mean) for i in range(1, count)), Decimal(0)
            )
            phi = numerator / (variance * Decimal(count - 1)) if variance != 0 else Decimal(0)
            # Clip to a stable, stationary range -- an AR(1) surrogate must
            # never be allowed to explode regardless of what the retained
            # sample's raw lag-1 correlation happens to compute to.
            phi = max(Decimal("-0.95"), min(Decimal("0.95"), phi))
        innovation_scale = _decimal_sqrt(max(Decimal(0), Decimal(1) - phi * phi)) * stdev
        generator = random.Random(seed)
        surrogate: list[Decimal] = []
        previous = mean
        for _ in range(count):
            shock = Decimal(str(generator.gauss(0.0, 1.0)))
            current = mean + phi * (previous - mean) + innovation_scale * shock
            surrogate.append(current)
            previous = current
    return tuple(
        DatedValue(date, bound_statistical_decimal(value))
        for date, value in zip(dates, surrogate, strict=True)
    )


def constant_series(dates: Sequence[str], *, value: Decimal = Decimal(1)) -> tuple[DatedValue, ...]:
    """A trivial constant series -- every date carries the identical value.
    Splitting this by "above train median" is structurally vacuous (the
    median of a constant series is that constant, so every date qualifies),
    making the resulting sample identical to the full universe. Reused,
    with new outcome/horizon pairings, for the ``UNCONDITIONAL_BASELINE``
    null-control kind: an honest test of whether the methodology ever
    fabricates an apparent edge from a condition that conditions on
    nothing."""

    return tuple(DatedValue(date, value) for date in dates)


# ==========================================================================
# Section 2: the calibration plan (declared, versioned, before any result)
# ==========================================================================


class NullGenerationMethod(StrEnum):
    """Every null-construction method a control in this milestone's battery
    may use. ``NONE_ECONOMIC_PAIRING`` intentionally applies no transform
    at all -- its nullness comes entirely from pairing genuinely retained,
    untransformed evidence that has no plausible transmission channel."""

    IID_DATE_SHUFFLE = "IID_DATE_SHUFFLE"
    BLOCK_PERMUTATION = "BLOCK_PERMUTATION"
    SIGN_RANDOMIZATION = "SIGN_RANDOMIZATION"
    TEMPORAL_LAG_DISPLACEMENT = "TEMPORAL_LAG_DISPLACEMENT"
    SYNTHETIC_AR1_SURROGATE = "SYNTHETIC_AR1_SURROGATE"
    RANDOM_NOISE_BASELINE = "RANDOM_NOISE_BASELINE"
    UNCONDITIONAL_BASELINE = "UNCONDITIONAL_BASELINE"
    NONE_ECONOMIC_PAIRING = "NONE_ECONOMIC_PAIRING"


class NullControlFamily(StrEnum):
    """The five control families section 2 of the milestone names
    explicitly. Multiple-testing correction and empirical false-discovery
    reporting are both broken out per family (never pooled only)."""

    TEMPORAL_DISPLACEMENT = "TEMPORAL_DISPLACEMENT"
    ECONOMIC_NEGATIVE_CONTROL = "ECONOMIC_NEGATIVE_CONTROL"
    DETERMINISTIC_PERMUTATION = "DETERMINISTIC_PERMUTATION"
    SEEDED_SYNTHETIC_NULL = "SEEDED_SYNTHETIC_NULL"
    PERSISTENCE_RANDOM_BASELINE = "PERSISTENCE_RANDOM_BASELINE"


class NullControlNature(StrEnum):
    """Section 15's required type classification. Mathematical/synthetic
    nulls are the strongest calibration evidence (their nullness is true by
    construction); economic-negative controls are the weakest (their
    nullness is an economic judgment call, not a mathematical guarantee),
    and must never silently be treated as equally strong evidence."""

    MATHEMATICAL_SYNTHETIC_NULL = "MATHEMATICAL_SYNTHETIC_NULL"
    TEMPORAL_NULL = "TEMPORAL_NULL"
    ECONOMIC_NEGATIVE_CONTROL = "ECONOMIC_NEGATIVE_CONTROL"


class NullControlDesignFamily(StrEnum):
    """Section 11's "which research design does this null control's search
    shape resemble" classification -- independent of ``NullControlFamily``
    (which classifies *how the null was constructed*). Lets false-discovery
    behavior be compared against the genuine agenda's own
    ``HypothesisFamily`` partitions without reusing that enum's identity
    (a null control is never a member of e.g. ``EQUITY_INTERNAL`` -- it
    only *mimics* that family's search shape for calibration purposes)."""

    EQUITY_INTERNAL = "EQUITY_INTERNAL"
    EQUITY_BTC = "EQUITY_BTC"
    FIXED_INCOME_CONTEXT = "FIXED_INCOME_CONTEXT"
    COMMODITY_CONTEXT = "COMMODITY_CONTEXT"
    INTERACTION_CONDITIONAL = "INTERACTION_CONDITIONAL"
    PURE_MATHEMATICAL_NULL = "PURE_MATHEMATICAL_NULL"


# Extends (never redefines) ``domain.cross_market_research.NEGATIVE_CONTROL_KINDS``:
# every one of the six original kinds remains valid; this milestone's larger battery
# additionally uses the kinds below.
NULL_CONTROL_KINDS = (
    "SHUFFLED_FEATURE",
    "LAG_DISPLACED_FEATURE",
    "ECONOMICALLY_UNRELATED_PAIR",
    "RANDOM_DATE_BASELINE",
    "PERSISTENCE_BASELINE",
    "UNCONDITIONAL_OUTCOME",
    "BLOCK_PERMUTED_FEATURE",
    "SIGN_RANDOMIZED_FEATURE",
    "FAR_LAG_DISPLACED_FEATURE",
    "SYNTHETIC_AR1_SURROGATE_FEATURE",
    "UNCONDITIONAL_BASELINE",
)

MINIMUM_NEGATIVE_CONTROL_BUDGET = 50
MAXIMUM_NEGATIVE_CONTROL_BUDGET = 150


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    """Section 1's "versioned research-calibration concept," declared and
    retained *before* any control is executed. Reuses the genuine agenda's
    exact dataset/replay lineage (``source_agenda_id`` plus the same
    dataset/replay/cross-market-dataset ids) so the calibration is a
    genuinely apples-to-apples measurement of the same methodology against
    the same retained history -- never a different or refreshed dataset."""

    calibration_id: str
    version: str
    objective: str
    dataset_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    source_agenda_id: str
    research_methodology_policy_id: str
    allowed_feature_families: tuple[FeatureFamily, ...]
    allowed_outcome_families: tuple[OutcomeFamily, ...]
    allowed_horizons_sessions: tuple[int, ...]
    null_generation_methods: tuple[NullGenerationMethod, ...]
    negative_control_budget: int
    random_seed_policy: str
    multiple_testing_method: MultipleTestingCorrectionMethod
    block_length_policy: str
    conclusion_policy_id: str
    registered_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        version: str,
        objective: str,
        dataset_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
        source_agenda_id: str,
        research_methodology_policy_id: str,
        allowed_feature_families: tuple[FeatureFamily | str, ...],
        allowed_outcome_families: tuple[OutcomeFamily | str, ...],
        allowed_horizons_sessions: tuple[int, ...],
        null_generation_methods: tuple[NullGenerationMethod | str, ...],
        negative_control_budget: int,
        random_seed_policy: str,
        multiple_testing_method: MultipleTestingCorrectionMethod | str,
        block_length_policy: str,
        conclusion_policy_id: str,
        registered_at: datetime,
    ) -> CalibrationPlan:
        if not (
            MINIMUM_NEGATIVE_CONTROL_BUDGET
            <= negative_control_budget
            <= MAXIMUM_NEGATIVE_CONTROL_BUDGET
        ):
            raise CalibrationError("CALIBRATION_NEGATIVE_CONTROL_BUDGET_OUT_OF_BOUNDS")
        if not dataset_snapshot_id or not replay_run_id or not cross_market_dataset_id:
            raise CalibrationError("CALIBRATION_DATASET_LINEAGE_REQUIRED")
        if not source_agenda_id:
            raise CalibrationError("CALIBRATION_SOURCE_AGENDA_REQUIRED")
        feature_families = tuple(FeatureFamily(item) for item in allowed_feature_families)
        outcome_families = tuple(OutcomeFamily(item) for item in allowed_outcome_families)
        if not feature_families or not outcome_families:
            raise CalibrationError("CALIBRATION_REQUIRES_FEATURE_AND_OUTCOME_FAMILIES")
        horizons = tuple(sorted(set(allowed_horizons_sessions)))
        if not horizons or any(item < 1 for item in horizons):
            raise CalibrationError("CALIBRATION_HORIZON_SET_INVALID")
        methods = tuple(NullGenerationMethod(item) for item in null_generation_methods)
        if not methods:
            raise CalibrationError("CALIBRATION_REQUIRES_AT_LEAST_ONE_NULL_GENERATION_METHOD")
        if NullGenerationMethod.BLOCK_PERMUTATION not in methods:
            raise CalibrationError("CALIBRATION_REQUIRES_AT_LEAST_ONE_BLOCK_BASED_NULL_METHOD")
        correction = MultipleTestingCorrectionMethod(multiple_testing_method)
        registered = _aware(registered_at, "CALIBRATION_REGISTERED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            _text(version, "CALIBRATION_VERSION_INVALID", maximum=80),
            _text(objective, "CALIBRATION_OBJECTIVE_INVALID", maximum=2_000),
            dataset_snapshot_id,
            replay_run_id,
            cross_market_dataset_id,
            source_agenda_id,
            _text(
                research_methodology_policy_id,
                "CALIBRATION_METHODOLOGY_POLICY_ID_INVALID",
                maximum=200,
            ),
            feature_families,
            outcome_families,
            horizons,
            methods,
            negative_control_budget,
            _text(random_seed_policy, "CALIBRATION_SEED_POLICY_INVALID", maximum=500),
            correction,
            _text(block_length_policy, "CALIBRATION_BLOCK_LENGTH_POLICY_INVALID", maximum=500),
            _text(conclusion_policy_id, "CALIBRATION_CONCLUSION_POLICY_ID_INVALID", maximum=200),
        )
        return cls(
            calibration_id=deterministic_id("research_calibration_plan", payload),
            version=payload[1],
            objective=payload[2],
            dataset_snapshot_id=dataset_snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            source_agenda_id=source_agenda_id,
            research_methodology_policy_id=payload[7],
            allowed_feature_families=feature_families,
            allowed_outcome_families=outcome_families,
            allowed_horizons_sessions=horizons,
            null_generation_methods=methods,
            negative_control_budget=negative_control_budget,
            random_seed_policy=payload[13],
            multiple_testing_method=correction,
            block_length_policy=payload[15],
            conclusion_policy_id=payload[16],
            registered_at=registered,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class NullControlSpec:
    """One declared control (section 2/3). ``source_feature_id``/
    ``outcome_id`` reference the genuine, already-retained
    ``domain.cross_market_research.FeatureId``/``OutcomeId`` vocabulary by
    value (as plain text, deliberately not importing those enums here --
    this module never widens or depends on that agenda-specific catalog);
    the derived null series a control actually evaluates is a pure
    transform of that real, retained feature, computed by the application
    layer and never persisted as network evidence of its own."""

    spec_id: str
    calibration_id: str
    family: NullControlFamily
    nature: NullControlNature
    design_family: NullControlDesignFamily
    control_kind: str
    null_method: NullGenerationMethod
    source_feature_id: str
    source_feature_family: FeatureFamily
    outcome_id: str
    outcome_family: OutcomeFamily
    horizon_sessions: int
    method_params: tuple[tuple[str, str], ...]
    rationale: str
    ordinal: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration: CalibrationPlan,
        family: NullControlFamily | str,
        nature: NullControlNature | str,
        design_family: NullControlDesignFamily | str,
        control_kind: str,
        null_method: NullGenerationMethod | str,
        source_feature_id: str,
        source_feature_family: FeatureFamily | str,
        outcome_id: str,
        outcome_family: OutcomeFamily | str,
        horizon_sessions: int,
        method_params: tuple[tuple[str, str], ...],
        rationale: str,
        ordinal: int,
        created_at: datetime,
    ) -> NullControlSpec:
        family_value = NullControlFamily(family)
        nature_value = NullControlNature(nature)
        design_value = NullControlDesignFamily(design_family)
        if control_kind not in NULL_CONTROL_KINDS:
            raise CalibrationError("NULL_CONTROL_KIND_INVALID")
        method_value = NullGenerationMethod(null_method)
        if method_value not in calibration.null_generation_methods:
            raise CalibrationError("NULL_CONTROL_METHOD_NOT_APPROVED_BY_CALIBRATION_PLAN")
        if not source_feature_id or not outcome_id:
            raise CalibrationError("NULL_CONTROL_SOURCE_AND_OUTCOME_REQUIRED")
        feature_family_value = FeatureFamily(source_feature_family)
        outcome_family_value = OutcomeFamily(outcome_family)
        if horizon_sessions not in calibration.allowed_horizons_sessions:
            raise CalibrationError("NULL_CONTROL_HORIZON_NOT_APPROVED_BY_CALIBRATION_PLAN")
        if ordinal < 0:
            raise CalibrationError("NULL_CONTROL_ORDINAL_INVALID")
        params = tuple(sorted(method_params))
        created = _aware(created_at, "NULL_CONTROL_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            calibration.calibration_id,
            family_value,
            nature_value,
            design_value,
            control_kind,
            method_value,
            source_feature_id,
            feature_family_value,
            outcome_id,
            outcome_family_value,
            horizon_sessions,
            params,
            _text(rationale, "NULL_CONTROL_RATIONALE_INVALID", maximum=1_000),
            ordinal,
        )
        return cls(
            spec_id=deterministic_id("research_calibration_null_control", payload),
            calibration_id=calibration.calibration_id,
            family=family_value,
            nature=nature_value,
            design_family=design_value,
            control_kind=control_kind,
            null_method=method_value,
            source_feature_id=source_feature_id,
            source_feature_family=feature_family_value,
            outcome_id=outcome_id,
            outcome_family=outcome_family_value,
            horizon_sessions=horizon_sessions,
            method_params=params,
            rationale=payload[13],
            ordinal=ordinal,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class NullControlOutcome:
    """The retained, per-control evidence bundle (mirrors
    ``domain.cross_market_research.HypothesisExecutionRecord`` in spirit,
    scoped to what a null control needs): every governed-methodology phase
    id this control actually reached, its conclusion/promotion evaluation,
    and the resulting ``KnowledgeStatus`` -- computed by the exact same
    ``derive_knowledge_status`` function real hypotheses use, deliberately
    without a null-control special case, so a control that happens to
    reach ``WEAK`` or better is itself calibration evidence, never
    silently corrected."""

    outcome_id: str
    spec_id: str
    calibration_run_id: str
    governance_policy_id: str
    exploratory_trial_id: str | None
    validation_trial_id: str | None
    holdout_trial_id: str | None
    robustness_run_id: str | None
    conclusion_id: str | None
    promotion_evaluation_id: str | None
    knowledge_status: KnowledgeStatus
    blockers: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        spec_id: str,
        calibration_run_id: str,
        governance_policy_id: str,
        exploratory_trial_id: str | None,
        validation_trial_id: str | None,
        holdout_trial_id: str | None,
        robustness_run_id: str | None,
        conclusion_id: str | None,
        promotion_evaluation_id: str | None,
        knowledge_status: KnowledgeStatus | str,
        blockers: tuple[str, ...],
        created_at: datetime,
    ) -> NullControlOutcome:
        if not spec_id or not calibration_run_id or not governance_policy_id:
            raise CalibrationError("NULL_CONTROL_OUTCOME_LINEAGE_REQUIRED")
        status = KnowledgeStatus(knowledge_status)
        block_values = tuple(sorted(set(blockers)))
        created = _aware(created_at, "NULL_CONTROL_OUTCOME_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            spec_id,
            calibration_run_id,
            governance_policy_id,
            exploratory_trial_id,
            validation_trial_id,
            holdout_trial_id,
            robustness_run_id,
            conclusion_id,
            promotion_evaluation_id,
            status,
            block_values,
        )
        return cls(
            outcome_id=deterministic_id("research_calibration_null_outcome", payload),
            spec_id=spec_id,
            calibration_run_id=calibration_run_id,
            governance_policy_id=governance_policy_id,
            exploratory_trial_id=exploratory_trial_id,
            validation_trial_id=validation_trial_id,
            holdout_trial_id=holdout_trial_id,
            robustness_run_id=robustness_run_id,
            conclusion_id=conclusion_id,
            promotion_evaluation_id=promotion_evaluation_id,
            knowledge_status=status,
            blockers=block_values,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 3: effect-size floor (section 6) -- outcome-family-specific, not
# one universal economic threshold.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class EffectSizeFloorPolicy:
    """A versioned, outcome-family-specific minimum standardized effect
    (Cohen's d) required before a result may be called practically/
    materially meaningful, independent of whether it is statistically
    distinguishable from zero. ``Decimal("0.20")`` for every family this
    milestone declares is Cohen's own conventional "small effect"
    threshold -- deliberately conservative (i.e. permissive) rather than
    tuned to whatever the genuine agenda's own results happen to show,
    since a floor chosen *after* seeing the results it will be applied to
    would not be a floor at all."""

    policy_id: str
    floors_by_outcome_family: tuple[tuple[OutcomeFamily, Decimal], ...]

    def floor_for(self, outcome_family: OutcomeFamily) -> Decimal:
        for family, floor in self.floors_by_outcome_family:
            if family is outcome_family:
                return floor
        return Decimal("0.20")


EFFECT_SIZE_FLOOR_POLICY_V1 = EffectSizeFloorPolicy(
    policy_id="EFFECT_SIZE_FLOOR_POLICY_V1",
    floors_by_outcome_family=tuple((family, Decimal("0.20")) for family in OutcomeFamily),
)


def effect_meets_floor(
    *, outcome_family: OutcomeFamily, cohens_d: Decimal | None, policy: EffectSizeFloorPolicy
) -> bool:
    if cohens_d is None:
        return False
    return abs(cohens_d) >= policy.floor_for(outcome_family)


class PracticalSignificance(StrEnum):
    NEITHER = "NEITHER"
    STATISTICAL_ONLY = "STATISTICAL_ONLY"
    PRACTICAL_ONLY = "PRACTICAL_ONLY"
    BOTH = "BOTH"


def classify_practical_significance(
    *, statistically_significant: bool | None, meets_effect_floor: bool
) -> PracticalSignificance:
    significant = bool(statistically_significant)
    if significant and meets_effect_floor:
        return PracticalSignificance.BOTH
    if significant:
        return PracticalSignificance.STATISTICAL_ONLY
    if meets_effect_floor:
        return PracticalSignificance.PRACTICAL_ONLY
    return PracticalSignificance.NEITHER


def null_calibration_percentile(
    *, statistic: Decimal, null_statistics: Sequence[Decimal]
) -> Decimal | None:
    """The fraction of the retained null-control distribution whose
    absolute magnitude is at or below ``abs(statistic)`` -- a standard,
    transparent empirical (permutation-style) percentile, computed only
    from already-retained evidence. Returns ``None`` (never a fabricated
    0/1) when there is no null distribution to compare against."""

    if not null_statistics:
        return None
    magnitude = abs(statistic)
    at_or_below = sum(1 for item in null_statistics if abs(item) <= magnitude)
    return bound_statistical_decimal(Decimal(at_or_below) / Decimal(len(null_statistics)))


# ==========================================================================
# Section 4: evidence profile and null-calibrated evidence grade (sections
# 7/8) -- a structured profile plus an optional derived grade, not one
# black-box score.
# ==========================================================================


class EvidenceGrade(StrEnum):
    """Ordered worst-to-best; ``EVIDENCE_GRADE_ORDER`` below is the single
    source of truth for that ordering."""

    UNRELIABLE = "UNRELIABLE"
    EXPLORATORY = "EXPLORATORY"
    SUGGESTIVE = "SUGGESTIVE"
    VALIDATED = "VALIDATED"
    STRONG = "STRONG"


EVIDENCE_GRADE_ORDER: tuple[EvidenceGrade, ...] = (
    EvidenceGrade.UNRELIABLE,
    EvidenceGrade.EXPLORATORY,
    EvidenceGrade.SUGGESTIVE,
    EvidenceGrade.VALIDATED,
    EvidenceGrade.STRONG,
)


def evidence_grade_at_least(grade: EvidenceGrade, minimum: EvidenceGrade) -> bool:
    return EVIDENCE_GRADE_ORDER.index(grade) >= EVIDENCE_GRADE_ORDER.index(minimum)


@dataclass(frozen=True, slots=True)
class EvidenceProfile:
    """Section 7's structured evidence profile: every field is a fact
    already retained elsewhere (a benchmark comparison, a robustness
    summary, a corrected significance flag, an effective sample count, a
    null-calibration percentile) assembled in one place for interpretation
    -- never collapsed into a single opaque number before ``EvidenceGrade``
    is derived from it."""

    profile_id: str
    subject_id: str
    subject_kind: str
    benchmark_relative_effect: Decimal | None
    cohens_d: Decimal | None
    validation_beats_benchmark: bool | None
    holdout_result: str
    walk_forward_consistency: Decimal | None
    robustness_passed: bool | None
    corrected_significant: bool | None
    effective_sample: Decimal
    regime_concentration: str
    null_percentile: Decimal | None
    practical_significance: PracticalSignificance
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        subject_kind: str,
        benchmark_relative_effect: Decimal | None,
        cohens_d: Decimal | None,
        validation_beats_benchmark: bool | None,
        holdout_result: str,
        walk_forward_consistency: Decimal | None,
        robustness_passed: bool | None,
        corrected_significant: bool | None,
        effective_sample: Decimal,
        regime_concentration: str,
        null_percentile: Decimal | None,
        practical_significance: PracticalSignificance | str,
        created_at: datetime,
    ) -> EvidenceProfile:
        if not subject_id:
            raise CalibrationError("EVIDENCE_PROFILE_SUBJECT_ID_REQUIRED")
        if subject_kind not in ("HYPOTHESIS", "NULL_CONTROL"):
            raise CalibrationError("EVIDENCE_PROFILE_SUBJECT_KIND_INVALID")
        if effective_sample < 0:
            raise CalibrationError("EVIDENCE_PROFILE_EFFECTIVE_SAMPLE_INVALID")
        practical = PracticalSignificance(practical_significance)
        created = _aware(created_at, "EVIDENCE_PROFILE_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            subject_id,
            subject_kind,
            benchmark_relative_effect,
            cohens_d,
            validation_beats_benchmark,
            _text(holdout_result, "EVIDENCE_PROFILE_HOLDOUT_RESULT_INVALID", maximum=80),
            walk_forward_consistency,
            robustness_passed,
            corrected_significant,
            bound_statistical_decimal(effective_sample),
            _text(
                regime_concentration, "EVIDENCE_PROFILE_REGIME_CONCENTRATION_INVALID", maximum=80
            ),
            null_percentile,
            practical,
        )
        return cls(
            profile_id=deterministic_id("research_calibration_evidence_profile", payload),
            subject_id=subject_id,
            subject_kind=subject_kind,
            benchmark_relative_effect=benchmark_relative_effect,
            cohens_d=cohens_d,
            validation_beats_benchmark=validation_beats_benchmark,
            holdout_result=payload[6],
            walk_forward_consistency=walk_forward_consistency,
            robustness_passed=robustness_passed,
            corrected_significant=corrected_significant,
            effective_sample=payload[10],
            regime_concentration=payload[11],
            null_percentile=null_percentile,
            practical_significance=practical,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class EvidenceGradePolicy:
    """Every numeric threshold ``derive_evidence_grade`` uses, named and
    versioned so a future stricter or looser policy is a new, distinct,
    retained identity -- never a silent change to this one's meaning."""

    policy_id: str
    null_like_ceiling: Decimal
    suggestive_floor: Decimal
    validated_floor: Decimal
    strong_floor: Decimal


EVIDENCE_GRADE_POLICY_V1 = EvidenceGradePolicy(
    policy_id="EVIDENCE_GRADE_POLICY_V1",
    null_like_ceiling=Decimal("0.90"),
    suggestive_floor=Decimal("0.90"),
    validated_floor=Decimal("0.95"),
    strong_floor=Decimal("0.99"),
)


def derive_evidence_grade(
    *,
    conclusion_state: ResearchConclusionState,
    profile: EvidenceProfile,
    effect_meets_floor: bool,
    policy: EvidenceGradePolicy = EVIDENCE_GRADE_POLICY_V1,
) -> EvidenceGrade:
    """Section 8's grading rule, explicit and ordered:

    1. A conclusion that already failed (``REJECTED``/``HOLDOUT_FAILED``/
       ``ROBUSTNESS_FAILED``) is ``UNRELIABLE`` -- worse than merely
       unproven.
    2. Without a null-calibration percentile to compare against, or with a
       percentile below ``null_like_ceiling`` (indistinguishable from
       ordinary negative-control behavior), the grade can never exceed
       ``EXPLORATORY`` -- this is the hard ceiling section 8 requires.
    3. A percentile at or above ``suggestive_floor`` but not meeting the
       stronger bar below is ``SUGGESTIVE`` -- tail-of-null-distribution,
       not yet validated.
    4. ``VALIDATED``/``STRONG`` additionally require an actually
       ``SUPPORTED`` conclusion (never merely ``WEAK_SUPPORT``), a holdout
       result that beats its benchmark, passed robustness, corrected
       significance, and an effect size clearing the outcome-family floor
       -- every one of holdout, robustness, correction, and effect
       evidence simultaneously, not any single one alone. This is what
       makes "``WEAK_SUPPORT`` alone" structurally incapable of reaching
       ``VALIDATED``: only conclusion state ``SUPPORTED`` can.
    """

    if conclusion_state in (
        ResearchConclusionState.REJECTED,
        ResearchConclusionState.HOLDOUT_FAILED,
        ResearchConclusionState.ROBUSTNESS_FAILED,
    ):
        return EvidenceGrade.UNRELIABLE
    if profile.null_percentile is None:
        return EvidenceGrade.EXPLORATORY
    if profile.null_percentile < policy.null_like_ceiling:
        return EvidenceGrade.EXPLORATORY
    strong_evidence = (
        conclusion_state is ResearchConclusionState.SUPPORTED
        and profile.holdout_result == "BEATS_BENCHMARK"
        and profile.robustness_passed is True
        and profile.corrected_significant is True
        and effect_meets_floor
    )
    if strong_evidence and profile.null_percentile >= policy.strong_floor:
        return EvidenceGrade.STRONG
    if strong_evidence and profile.null_percentile >= policy.validated_floor:
        return EvidenceGrade.VALIDATED
    if profile.null_percentile >= policy.suggestive_floor:
        return EvidenceGrade.SUGGESTIVE
    return EvidenceGrade.EXPLORATORY


# ==========================================================================
# Section 5: empirical false-discovery calibration (section 4)
# ==========================================================================


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal(0)
    return bound_statistical_decimal(Decimal(numerator) / Decimal(denominator))


@dataclass(frozen=True, slots=True)
class CalibrationRateSummary:
    """Section 4's required counts and derived empirical rates. Every rate
    is a retained *fact about this calibration's own controls*, never a
    nominal p-value threshold presented as if it were an empirical
    system-wide error rate."""

    total_controls: int
    validation_candidates: int
    raw_positive_findings: int
    bh_fdr_survivors: int
    holdout_survivors: int
    robustness_survivors: int
    weak_count: int
    supported_count: int
    promotion_eligible_count: int
    raw_false_positive_rate: Decimal
    corrected_false_positive_rate: Decimal
    false_weak_rate: Decimal
    false_supported_rate: Decimal
    false_promotion_rate: Decimal

    @classmethod
    def compute(
        cls,
        *,
        total_controls: int,
        validation_candidates: int,
        raw_positive_findings: int,
        bh_fdr_survivors: int,
        holdout_survivors: int,
        robustness_survivors: int,
        weak_count: int,
        supported_count: int,
        promotion_eligible_count: int,
    ) -> CalibrationRateSummary:
        if total_controls < 0 or validation_candidates < 0:
            raise CalibrationError("CALIBRATION_RATE_SUMMARY_COUNTS_INVALID")
        for label, value in (
            ("raw_positive_findings", raw_positive_findings),
            ("bh_fdr_survivors", bh_fdr_survivors),
            ("holdout_survivors", holdout_survivors),
            ("robustness_survivors", robustness_survivors),
            ("weak_count", weak_count),
            ("supported_count", supported_count),
            ("promotion_eligible_count", promotion_eligible_count),
        ):
            if value < 0 or value > total_controls:
                raise CalibrationError(f"CALIBRATION_RATE_SUMMARY_COUNT_OUT_OF_BOUNDS:{label}")
        return cls(
            total_controls=total_controls,
            validation_candidates=validation_candidates,
            raw_positive_findings=raw_positive_findings,
            bh_fdr_survivors=bh_fdr_survivors,
            holdout_survivors=holdout_survivors,
            robustness_survivors=robustness_survivors,
            weak_count=weak_count,
            supported_count=supported_count,
            promotion_eligible_count=promotion_eligible_count,
            raw_false_positive_rate=_rate(raw_positive_findings, validation_candidates),
            corrected_false_positive_rate=_rate(bh_fdr_survivors, validation_candidates),
            false_weak_rate=_rate(weak_count, total_controls),
            false_supported_rate=_rate(supported_count, total_controls),
            false_promotion_rate=_rate(promotion_eligible_count, total_controls),
        )


MINIMUM_ADEQUATE_CONTROL_COUNT = 10


@dataclass(frozen=True, slots=True)
class CalibrationBreakdown:
    """One ``CalibrationRateSummary`` sliced along one reporting dimension
    (section 11/12/16: family, horizon, time period, or volatility regime).
    ``sample_adequate`` is ``False`` whenever ``control_count`` is below
    ``MINIMUM_ADEQUATE_CONTROL_COUNT`` -- the report must still retain the
    slice (never hide it) but must not present its rate as a reliable
    family/horizon/period-specific calibration."""

    breakdown_id: str
    calibration_id: str
    dimension_kind: str
    dimension_label: str
    control_count: int
    sample_adequate: bool
    summary: CalibrationRateSummary
    note: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        dimension_kind: str,
        dimension_label: str,
        control_count: int,
        summary: CalibrationRateSummary,
        note: str,
        created_at: datetime,
    ) -> CalibrationBreakdown:
        if not calibration_id:
            raise CalibrationError("CALIBRATION_BREAKDOWN_CALIBRATION_ID_REQUIRED")
        if control_count < 0:
            raise CalibrationError("CALIBRATION_BREAKDOWN_CONTROL_COUNT_INVALID")
        kind = _text(dimension_kind, "CALIBRATION_BREAKDOWN_DIMENSION_KIND_INVALID", maximum=40)
        label = _text(dimension_label, "CALIBRATION_BREAKDOWN_DIMENSION_LABEL_INVALID", maximum=80)
        adequate = control_count >= MINIMUM_ADEQUATE_CONTROL_COUNT
        created = _aware(created_at, "CALIBRATION_BREAKDOWN_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            calibration_id,
            kind,
            label,
            control_count,
            adequate,
            summary,
            _text(note, "CALIBRATION_BREAKDOWN_NOTE_INVALID", maximum=1_000),
        )
        return cls(
            breakdown_id=deterministic_id("research_calibration_breakdown", payload),
            calibration_id=calibration_id,
            dimension_kind=kind,
            dimension_label=label,
            control_count=control_count,
            sample_adequate=adequate,
            summary=summary,
            note=payload[7],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class StatisticalMethodComparison:
    """Section 17's required diagnostic-method comparison: how many null
    controls each candidate statistical method would have called
    significant, retained side by side rather than silently picking
    whichever method produces the fewest false positives."""

    comparison_id: str
    calibration_id: str
    method_id: str
    survivors_on_nulls: int
    control_count: int
    survivor_rate: Decimal
    conservatism_rank: int
    notes: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        method_id: str,
        survivors_on_nulls: int,
        control_count: int,
        conservatism_rank: int,
        notes: str,
        created_at: datetime,
    ) -> StatisticalMethodComparison:
        if not calibration_id or not method_id:
            raise CalibrationError("METHOD_COMPARISON_IDENTITY_REQUIRED")
        if survivors_on_nulls < 0 or control_count < 0 or survivors_on_nulls > control_count:
            raise CalibrationError("METHOD_COMPARISON_COUNTS_INVALID")
        if conservatism_rank < 1:
            raise CalibrationError("METHOD_COMPARISON_RANK_INVALID")
        created = _aware(created_at, "METHOD_COMPARISON_CREATED_AT")
        rate = _rate(survivors_on_nulls, control_count)
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            calibration_id,
            method_id,
            survivors_on_nulls,
            control_count,
            conservatism_rank,
            _text(notes, "METHOD_COMPARISON_NOTES_INVALID", maximum=1_000),
        )
        return cls(
            comparison_id=deterministic_id("research_calibration_method_comparison", payload),
            calibration_id=calibration_id,
            method_id=method_id,
            survivors_on_nulls=survivors_on_nulls,
            control_count=control_count,
            survivor_rate=rate,
            conservatism_rank=conservatism_rank,
            notes=payload[6],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 6: prior-agenda recalibration (section 10) and follow-up
# priority (section 14) -- both strictly additive, never touching a
# historical ``ResearchConclusion``/``ResearchKnowledgeEntry``.
# ==========================================================================


class FollowUpPriorityClass(StrEnum):
    JUSTIFIED = "JUSTIFIED"
    LOW_PRIORITY = "LOW_PRIORITY"
    UNSUPPORTED_AFTER_CALIBRATION = "UNSUPPORTED_AFTER_CALIBRATION"
    REJECTED = "REJECTED"


class NegativeControlQualityVerdict(StrEnum):
    """Section 15's required judgment about a negative control that itself
    reached an interesting-looking conclusion, derived from retained
    evidence (null percentile, robustness, effective sample) rather than
    intuition."""

    METHODOLOGY_FALSE_POSITIVE = "METHODOLOGY_FALSE_POSITIVE"
    PLAUSIBLE_REAL_ASSOCIATION = "PLAUSIBLE_REAL_ASSOCIATION"
    UNSTABLE_ARTIFACT = "UNSTABLE_ARTIFACT"
    UNRESOLVED = "UNRESOLVED"


def assess_negative_control_quality(
    *,
    null_percentile: Decimal | None,
    robustness_passed: bool | None,
    effective_sample: Decimal,
    minimum_effective_sample: Decimal,
    policy: EvidenceGradePolicy = EVIDENCE_GRADE_POLICY_V1,
) -> NegativeControlQualityVerdict:
    """Deterministic, evidence-based (section 15: "do not decide based only
    on intuition") classification of a negative control's own result:

    * No comparable null distribution to rank it against -> ``UNRESOLVED``.
    * A magnitude typical of this calibration's own economic-negative-
      control nulls (below the ``SUGGESTIVE`` floor) -> a straightforward
      ``METHODOLOGY_FALSE_POSITIVE``: this is exactly what the methodology
      is expected to occasionally produce from noise.
    * An unusually large magnitude (at/above the ``SUGGESTIVE`` floor)
      backed by an inadequate effective sample or failed robustness ->
      ``UNSTABLE_ARTIFACT``: the surprising result does not survive its
      own perturbation checks.
    * An unusually large magnitude with an adequate sample and passed
      robustness -> ``PLAUSIBLE_REAL_ASSOCIATION``: the calibration cannot
      rule out a genuine (if weak and economically unexplained)
      cross-market relationship, and this should be investigated
      separately rather than dismissed.
    """

    if null_percentile is None:
        return NegativeControlQualityVerdict.UNRESOLVED
    if effective_sample < minimum_effective_sample:
        return (
            NegativeControlQualityVerdict.UNSTABLE_ARTIFACT
            if null_percentile >= policy.suggestive_floor
            else NegativeControlQualityVerdict.METHODOLOGY_FALSE_POSITIVE
        )
    if null_percentile < policy.suggestive_floor:
        return NegativeControlQualityVerdict.METHODOLOGY_FALSE_POSITIVE
    if robustness_passed is not True:
        return NegativeControlQualityVerdict.UNSTABLE_ARTIFACT
    return NegativeControlQualityVerdict.PLAUSIBLE_REAL_ASSOCIATION


@dataclass(frozen=True, slots=True)
class PriorHypothesisRecalibration:
    """Section 10's required "new derived assessment" of one already-
    retained genuine hypothesis, built entirely from already-retained
    facts plus this calibration's null distribution. Never edits, deletes,
    or supersedes the referenced ``ResearchConclusion``/
    ``ResearchKnowledgeEntry`` -- ``original_conclusion_state``/
    ``original_knowledge_status`` are retained verbatim precisely so this
    record can never be mistaken for a rewrite of history."""

    recalibration_id: str
    calibration_id: str
    hypothesis_id: str
    original_conclusion_id: str
    original_conclusion_state: str
    original_knowledge_status: str
    calibrated_evidence_grade: EvidenceGrade
    null_percentile: Decimal | None
    still_promotion_ineligible: bool
    deserves_follow_up: bool
    follow_up_priority: FollowUpPriorityClass
    negative_control_verdict: NegativeControlQualityVerdict | None
    rationale: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        hypothesis_id: str,
        original_conclusion_id: str,
        original_conclusion_state: str,
        original_knowledge_status: str,
        calibrated_evidence_grade: EvidenceGrade | str,
        null_percentile: Decimal | None,
        still_promotion_ineligible: bool,
        deserves_follow_up: bool,
        follow_up_priority: FollowUpPriorityClass | str,
        negative_control_verdict: NegativeControlQualityVerdict | str | None,
        rationale: str,
        created_at: datetime,
    ) -> PriorHypothesisRecalibration:
        if not calibration_id or not hypothesis_id or not original_conclusion_id:
            raise CalibrationError("PRIOR_RECALIBRATION_LINEAGE_REQUIRED")
        grade = EvidenceGrade(calibrated_evidence_grade)
        priority = FollowUpPriorityClass(follow_up_priority)
        verdict = (
            NegativeControlQualityVerdict(negative_control_verdict)
            if negative_control_verdict is not None
            else None
        )
        created = _aware(created_at, "PRIOR_RECALIBRATION_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            calibration_id,
            hypothesis_id,
            original_conclusion_id,
            _text(
                original_conclusion_state, "PRIOR_RECALIBRATION_ORIGINAL_STATE_INVALID", maximum=80
            ),
            _text(
                original_knowledge_status, "PRIOR_RECALIBRATION_ORIGINAL_STATUS_INVALID", maximum=80
            ),
            grade,
            null_percentile,
            still_promotion_ineligible,
            deserves_follow_up,
            priority,
            verdict,
            _text(rationale, "PRIOR_RECALIBRATION_RATIONALE_INVALID", maximum=2_000),
        )
        return cls(
            recalibration_id=deterministic_id("research_calibration_prior_recalibration", payload),
            calibration_id=calibration_id,
            hypothesis_id=hypothesis_id,
            original_conclusion_id=original_conclusion_id,
            original_conclusion_state=payload[4],
            original_knowledge_status=payload[5],
            calibrated_evidence_grade=grade,
            null_percentile=null_percentile,
            still_promotion_ineligible=still_promotion_ineligible,
            deserves_follow_up=deserves_follow_up,
            follow_up_priority=priority,
            negative_control_verdict=verdict,
            rationale=payload[12],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class FollowUpPriorityAssessment:
    """Section 14's required classification of one already-retained
    ``domain.cross_market_research.FollowUpProposal``. Never itself
    authorizes running the follow-up -- only ranks it."""

    assessment_id: str
    calibration_id: str
    follow_up_id: str
    parent_hypothesis_id: str
    classification: FollowUpPriorityClass
    rationale: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        follow_up_id: str,
        parent_hypothesis_id: str,
        classification: FollowUpPriorityClass | str,
        rationale: str,
        created_at: datetime,
    ) -> FollowUpPriorityAssessment:
        if not calibration_id or not follow_up_id or not parent_hypothesis_id:
            raise CalibrationError("FOLLOW_UP_PRIORITY_LINEAGE_REQUIRED")
        classification_value = FollowUpPriorityClass(classification)
        created = _aware(created_at, "FOLLOW_UP_PRIORITY_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            calibration_id,
            follow_up_id,
            parent_hypothesis_id,
            classification_value,
            _text(rationale, "FOLLOW_UP_PRIORITY_RATIONALE_INVALID", maximum=1_000),
        )
        return cls(
            assessment_id=deterministic_id("research_calibration_follow_up_priority", payload),
            calibration_id=calibration_id,
            follow_up_id=follow_up_id,
            parent_hypothesis_id=parent_hypothesis_id,
            classification=classification_value,
            rationale=payload[5],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 7: the retained calibration report (section 18)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """One retained answer to "when Apex says a result is interesting, how
    often would it have said the same thing about noise?" (section 18).
    References every breakdown/comparison by id rather than embedding them,
    matching this codebase's existing preference for small, composable,
    independently retrievable records over one giant nested blob."""

    report_id: str
    calibration_id: str
    calibration_run_id: str
    overall_summary: CalibrationRateSummary
    family_breakdown_ids: tuple[str, ...]
    horizon_breakdown_ids: tuple[str, ...]
    time_period_breakdown_ids: tuple[str, ...]
    volatility_regime_breakdown_ids: tuple[str, ...]
    method_comparison_ids: tuple[str, ...]
    effect_size_floor_policy_id: str
    evidence_grade_policy_id: str
    recommended_evidence_policy_notes: str
    prior_agenda_hypotheses_recalibrated: int
    justified_follow_up_count: int
    limitations: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        calibration_run_id: str,
        overall_summary: CalibrationRateSummary,
        family_breakdown_ids: tuple[str, ...],
        horizon_breakdown_ids: tuple[str, ...],
        time_period_breakdown_ids: tuple[str, ...],
        volatility_regime_breakdown_ids: tuple[str, ...],
        method_comparison_ids: tuple[str, ...],
        effect_size_floor_policy_id: str,
        evidence_grade_policy_id: str,
        recommended_evidence_policy_notes: str,
        prior_agenda_hypotheses_recalibrated: int,
        justified_follow_up_count: int,
        limitations: tuple[str, ...],
        created_at: datetime,
    ) -> CalibrationReport:
        if not calibration_id or not calibration_run_id:
            raise CalibrationError("CALIBRATION_REPORT_LINEAGE_REQUIRED")
        if prior_agenda_hypotheses_recalibrated < 0 or justified_follow_up_count < 0:
            raise CalibrationError("CALIBRATION_REPORT_COUNTS_INVALID")
        created = _aware(created_at, "CALIBRATION_REPORT_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            calibration_id,
            calibration_run_id,
            overall_summary,
            tuple(sorted(set(family_breakdown_ids))),
            tuple(sorted(set(horizon_breakdown_ids))),
            tuple(sorted(set(time_period_breakdown_ids))),
            tuple(sorted(set(volatility_regime_breakdown_ids))),
            tuple(sorted(set(method_comparison_ids))),
            _text(
                effect_size_floor_policy_id,
                "CALIBRATION_REPORT_EFFECT_POLICY_ID_INVALID",
                maximum=100,
            ),
            _text(
                evidence_grade_policy_id, "CALIBRATION_REPORT_GRADE_POLICY_ID_INVALID", maximum=100
            ),
            _text(
                recommended_evidence_policy_notes,
                "CALIBRATION_REPORT_RECOMMENDATION_INVALID",
                maximum=4_000,
            ),
            prior_agenda_hypotheses_recalibrated,
            justified_follow_up_count,
            tuple(sorted(set(limitations))),
        )
        return cls(
            report_id=deterministic_id("research_calibration_report", payload),
            calibration_id=calibration_id,
            calibration_run_id=calibration_run_id,
            overall_summary=overall_summary,
            family_breakdown_ids=payload[4],
            horizon_breakdown_ids=payload[5],
            time_period_breakdown_ids=payload[6],
            volatility_regime_breakdown_ids=payload[7],
            method_comparison_ids=payload[8],
            effect_size_floor_policy_id=payload[9],
            evidence_grade_policy_id=payload[10],
            recommended_evidence_policy_notes=payload[11],
            prior_agenda_hypotheses_recalibrated=prior_agenda_hypotheses_recalibrated,
            justified_follow_up_count=justified_follow_up_count,
            limitations=payload[14],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 8: the calibration run (bounded mutable cursor -- the one
# exception to this module's immutability, exactly mirroring
# ``domain.cross_market_research.AgendaRun``)
# ==========================================================================


class CalibrationRunStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CalibrationRun:
    calibration_run_id: str
    calibration_id: str
    spec_ids: tuple[str, ...]
    cursor_ordinal: int
    completed_spec_ids: tuple[str, ...]
    status: CalibrationRunStatus
    failure_reason: str | None
    trials_executed: int
    created_at: datetime
    updated_at: datetime
    canonical_input_hash: str
    authority: str = "NONE"

    @classmethod
    def draft(
        cls, *, calibration: CalibrationPlan, spec_ids: tuple[str, ...], created_at: datetime
    ) -> CalibrationRun:
        if not spec_ids:
            raise CalibrationError("CALIBRATION_RUN_REQUIRES_AT_LEAST_ONE_SPEC")
        if len(spec_ids) > calibration.negative_control_budget:
            raise CalibrationError("CALIBRATION_RUN_SPEC_COUNT_EXCEEDS_DECLARED_BUDGET")
        created = _aware(created_at, "CALIBRATION_RUN_CREATED_AT")
        payload = (RESEARCH_SIGNAL_CALIBRATION_V1, calibration.calibration_id, tuple(spec_ids))
        return cls(
            calibration_run_id=deterministic_id("research_calibration_run", payload),
            calibration_id=calibration.calibration_id,
            spec_ids=tuple(spec_ids),
            cursor_ordinal=0,
            completed_spec_ids=(),
            status=CalibrationRunStatus.NOT_STARTED,
            failure_reason=None,
            trials_executed=0,
            created_at=created,
            updated_at=created,
            canonical_input_hash=content_digest(payload),
        )

    def advance(
        self, *, spec_id: str, trials_added: int, is_final: bool, at: datetime
    ) -> CalibrationRun:
        if self.status in (CalibrationRunStatus.COMPLETE, CalibrationRunStatus.FAILED):
            raise CalibrationError("CALIBRATION_RUN_CANNOT_ADVANCE_FROM_TERMINAL_STATE")
        if trials_added < 0:
            raise CalibrationError("CALIBRATION_RUN_TRIALS_ADDED_INVALID")
        return replace(
            self,
            cursor_ordinal=self.cursor_ordinal + 1,
            completed_spec_ids=(*self.completed_spec_ids, spec_id),
            status=CalibrationRunStatus.COMPLETE if is_final else CalibrationRunStatus.IN_PROGRESS,
            trials_executed=self.trials_executed + trials_added,
            updated_at=_aware(at, "CALIBRATION_RUN_UPDATED_AT"),
        )

    def fail(self, *, reason: str, at: datetime) -> CalibrationRun:
        if self.status is CalibrationRunStatus.COMPLETE:
            raise CalibrationError("CALIBRATION_RUN_ALREADY_COMPLETE")
        return replace(
            self,
            status=CalibrationRunStatus.FAILED,
            failure_reason=_text(reason, "CALIBRATION_RUN_FAILURE_REASON_INVALID", maximum=500),
            updated_at=_aware(at, "CALIBRATION_RUN_UPDATED_AT"),
        )


# ==========================================================================
# Section 9: promotion-policy calibration (section 9) -- strictly additive
# on top of the existing, unmodified ``research_methodology.PromotionPolicy``
# / ``PromotionEvaluation``. Never mutates either.
# ==========================================================================

CALIBRATED_PROMOTION_POLICY_ID = "CALIBRATED_PROMOTION_POLICY_V1"


@dataclass(frozen=True, slots=True)
class CalibratedPromotionPolicy:
    """Layers one additional, fail-closed requirement on top of whatever
    base ``research_methodology.PromotionPolicy`` a conclusion was already
    evaluated against: the conclusion's calibrated evidence grade must
    reach ``minimum_evidence_grade``. Defaults to ``VALIDATED`` -- strictly
    above ``SUGGESTIVE`` (the ceiling a ``WEAK_SUPPORT`` conclusion can
    structurally reach per ``derive_evidence_grade``), so a
    ``WEAK_SUPPORT``-only conclusion can never alone satisfy this policy
    regardless of any other fact about it."""

    policy_id: str = CALIBRATED_PROMOTION_POLICY_ID
    base_policy_id: str = "APEX_PRODUCTION_PAPER_CANDIDATE_POLICY_V1"
    minimum_evidence_grade: EvidenceGrade = EvidenceGrade.VALIDATED


CALIBRATED_PROMOTION_POLICY = CalibratedPromotionPolicy()


@dataclass(frozen=True, slots=True)
class CalibratedPromotionEvaluation:
    """One retained, additive gate result. ``base_eligible`` retains the
    already-computed, unmodified ``PromotionEvaluation.eligible`` verbatim
    so this record can never be mistaken for a replacement of it."""

    calibrated_promotion_evaluation_id: str
    promotion_evaluation_id: str
    calibration_id: str
    policy_id: str
    evidence_grade: EvidenceGrade
    base_eligible: bool
    eligible: bool
    reasons: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        promotion_evaluation_id: str,
        calibration_id: str,
        base_eligible: bool,
        base_reasons: tuple[str, ...],
        evidence_grade: EvidenceGrade | str,
        policy: CalibratedPromotionPolicy,
        created_at: datetime,
    ) -> CalibratedPromotionEvaluation:
        if not promotion_evaluation_id or not calibration_id:
            raise CalibrationError("CALIBRATED_PROMOTION_LINEAGE_REQUIRED")
        grade = EvidenceGrade(evidence_grade)
        reasons = list(base_reasons)
        if not evidence_grade_at_least(grade, policy.minimum_evidence_grade):
            reasons.append("EVIDENCE_GRADE_BELOW_CALIBRATED_PRODUCTION_MINIMUM")
        eligible = base_eligible and evidence_grade_at_least(grade, policy.minimum_evidence_grade)
        created = _aware(created_at, "CALIBRATED_PROMOTION_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V1,
            promotion_evaluation_id,
            calibration_id,
            policy.policy_id,
            grade,
            base_eligible,
            eligible,
            tuple(sorted(set(reasons))),
        )
        return cls(
            calibrated_promotion_evaluation_id=deterministic_id(
                "research_calibration_promotion_evaluation", payload
            ),
            promotion_evaluation_id=promotion_evaluation_id,
            calibration_id=calibration_id,
            policy_id=policy.policy_id,
            evidence_grade=grade,
            base_eligible=base_eligible,
            eligible=eligible,
            reasons=payload[7],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def research_calibration_warnings() -> tuple[str, ...]:
    return (CALIBRATION_WARNING, CALIBRATION_ALPHA_DISCLAIMER)
