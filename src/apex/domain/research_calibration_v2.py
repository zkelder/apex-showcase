"""Research Calibration v2: Template-Matched Nulls and Stability.

Answers a sharper question than v1's pooled calibration did: *does a
99th-percentile result remain unusual when compared with null experiments
that use the same hypothesis template, horizon, conditioning structure,
dependence structure, and research methodology* -- rather than against a
single pooled population dominated by a majority of unrelated designs.

This module is strictly additive on top of ``domain.research_calibration``
(v1): every v1 dataclass, enum, and constant is imported and reused
unmodified wherever its shape already fits (``CalibrationPlan``,
``NullControlOutcome``, ``CalibrationReport``, ``PriorHypothesisRecalibration``,
``StatisticalMethodComparison``, ``EvidenceGrade``, ``EvidenceGradePolicy``,
``derive_evidence_grade``, ``EFFECT_SIZE_FLOOR_POLICY_V1``,
``CALIBRATED_PROMOTION_POLICY``, ...). Nothing here rewrites, widens, or
reinterprets a v1 record; every v2 record is a new, separately retained
fact that references v1 evidence by id where relevant (lineage), never by
mutation.

Four genuinely new fact types have no faithful v1 representation and get
their own dataclasses (persisted through four new, narrowly-scoped, append-
only tables in migration ``0083``):

* :class:`ResearchDesign` classification of a null control or hypothesis
  (v1's ``NullControlDesignFamily`` classifies *which market domain* a
  control mimics; this is the orthogonal *which statistical/template shape*
  axis section 2 of the milestone requires, and reuses
  ``cross_market_research.HypothesisTemplateId`` vocabulary wherever a
  template already exists).
* a matched-stratum, template-matched-null percentile result
  (:class:`DesignClassification`), fail-closed to
  ``CALIBRATION_STRATUM_INSUFFICIENT`` when too few matched nulls exist.
* a time-period/volatility-regime :class:`StabilitySlice` -- deliberately
  *not* a re-use of v1's ``CalibrationBreakdown`` (whose shape presumes a
  full governed exploratory/validation/holdout/robustness pipeline ran per
  slice; a stability slice here is an honest, cheaper, purely-recomputed
  raw/corrected significance count over already-retained trial evidence,
  and claiming holdout/robustness survival for it would overstate what was
  actually evaluated).
* a bounded walk-forward-null :class:`WalkForwardNullSummary` and a
  :class:`FollowUpQualificationV2` record using this milestone's own new
  vocabulary (``CALIBRATION_QUALIFIED``/``CALIBRATION_INSUFFICIENT``/
  ``NO_LONGER_JUSTIFIED``/``REJECTED``) -- genuinely different from v1's
  closed ``FollowUpPriorityClass`` and therefore unrepresentable inside its
  CHECK-constrained column.

Every record here is ``authority = "NONE"``. Nothing here creates a
Strategy, Shared Opportunity, PAPER order, or execution authority, and
nothing here claims trading edge or alpha -- it only measures how far a
result stands out from null experiments that were actually built to
resemble its own design.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.cross_market_research import (
    FeatureFamily,
    HypothesisTemplateId,
    MarketId,
    OutcomeFamily,
)
from apex.domain.research_calibration import (
    EvidenceGrade,
    NullControlDesignFamily,
    NullControlFamily,
    NullControlNature,
    NullGenerationMethod,
)
from apex.domain.values import content_digest, deterministic_id

RESEARCH_SIGNAL_CALIBRATION_V2 = "RESEARCH_SIGNAL_CALIBRATION_V2"
CALIBRATION_V2_WARNING = (
    "RESEARCH SIGNAL CALIBRATION V2 EVIDENCE ONLY -- METHODOLOGY CALIBRATION, NOT STRATEGY "
    "RESEARCH; NO STRATEGY, PAPER, OR LIVE AUTHORITY; GRANTS NO EXECUTION AUTHORITY"
)
CALIBRATION_V2_LINEAGE_NOTE = (
    "Calibration v2 is strictly additive on top of Research Signal Calibration v1: no v1 "
    "CalibrationPlan, NullControlSpec, ResearchConclusion, or ResearchKnowledgeEntry is ever "
    "rewritten. Every v2 result retains lineage to v1 evidence by id where relevant."
)
CALIBRATION_FINITE_SAMPLE_WARNING = (
    "Section 20: an observed 0/N corrected false-positive rate is a finite-sample empirical "
    "estimate over N retained controls, never proof that the true false-discovery rate is "
    "exactly zero. Every retained rate here carries an implicit finite-sample margin that "
    "shrinks only as N grows; operator and report surfaces must show the control count "
    "alongside any observed rate and must not present a zero-observed-error result as "
    "exact precision."
)
CALIBRATION_STRATUM_INSUFFICIENT = "CALIBRATION_STRATUM_INSUFFICIENT"
MINIMUM_MATCHED_NULL_COUNT = 12


class CalibrationV2Error(ValueError):
    """A v2 calibration domain invariant was violated."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CalibrationV2Error(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _text(value: str, code: str, *, maximum: int = 500) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CalibrationV2Error(code)
    return normalized


# ==========================================================================
# Section 2: research-design taxonomy
# ==========================================================================


class ResearchDesign(StrEnum):
    """Section 2's minimum required taxonomy. The first five map 1:1 onto
    ``cross_market_research.HypothesisTemplateId`` (reusing that vocabulary
    directly, never redefining it); ``REGIME_COMPARISON`` is a sixth design
    this milestone adds because no genuine hypothesis template currently
    exercises it -- only calibration-only null controls do, so the
    taxonomy is complete and every possible classification is exercised by
    at least one retained control, never a dead enum member."""

    PREDICTIVE = "PREDICTIVE"
    CONDITIONAL = "CONDITIONAL"
    INTERACTION = "INTERACTION"
    CROSS_MARKET = "CROSS_MARKET"
    PERSISTENCE = "PERSISTENCE"
    REGIME_COMPARISON = "REGIME_COMPARISON"
    # Added for Cross-Market Research v3 (``domain.corporate_market_research``):
    # two genuinely new design shapes v1/v2's price-only taxonomy never
    # needed -- many companies compared cross-sectionally at one as_of, and
    # a change in a corporate feature between two periods. ``CONDITIONAL``/
    # ``INTERACTION`` above are reused verbatim by v3's own conditional/
    # interaction designs. ``FILING_EVENT`` is a third new member: a
    # filing-availability event study has no v1/v2 analogue either.
    CROSS_SECTIONAL = "CROSS_SECTIONAL"
    CHANGE_BASED = "CHANGE_BASED"
    FILING_EVENT = "FILING_EVENT"


class ConditioningShape(StrEnum):
    """The dependence/conditioning structure a control or hypothesis uses
    -- one further axis ``MatchedStratumKey`` folds in alongside design,
    outcome family, horizon, and market scope (section 5)."""

    UNCONDITIONAL = "UNCONDITIONAL"
    NUMERIC_SPLIT = "NUMERIC_SPLIT"
    LABEL_SPLIT = "LABEL_SPLIT"
    NUMERIC_INTERACTION = "NUMERIC_INTERACTION"
    REGIME_LABEL_COMPARISON = "REGIME_LABEL_COMPARISON"
    STATE_TRANSITION = "STATE_TRANSITION"


_TEMPLATE_TO_DESIGN: dict[HypothesisTemplateId, ResearchDesign] = {
    HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1: ResearchDesign.PREDICTIVE,
    HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1: ResearchDesign.CONDITIONAL,
    HypothesisTemplateId.INTERACTION_V1: ResearchDesign.INTERACTION,
    HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1: ResearchDesign.CROSS_MARKET,
    HypothesisTemplateId.PERSISTENCE_V1: ResearchDesign.PERSISTENCE,
}


def research_design_for_hypothesis(
    *, template_id: HypothesisTemplateId, market_scope: tuple[MarketId, ...]
) -> ResearchDesign:
    """Section 2's classification rule for a genuine, already-retained
    hypothesis. ``PREDICTIVE_RELATIONSHIP_V1`` mechanically covers both
    same-market predictive checks *and* the historical negative control
    (a single-feature/single-outcome check whose declared ``market_scope``
    spans two markets, e.g. equity sector dispersion -> BTC volatility) --
    the milestone explicitly asks that control be evaluated against
    cross-market nulls (section 19), so the hypothesis's own declared
    market scope, not template mechanics alone, decides ``CROSS_MARKET``
    here. Every other template already maps directly."""

    mapped = _TEMPLATE_TO_DESIGN.get(template_id)
    if mapped is not None and mapped is not ResearchDesign.PREDICTIVE:
        return mapped
    if len(set(market_scope)) > 1:
        return ResearchDesign.CROSS_MARKET
    return ResearchDesign.PREDICTIVE


def matched_stratum_key(
    *,
    research_design: ResearchDesign,
    outcome_family: OutcomeFamily,
    horizon_sessions: int,
    market_scope: tuple[MarketId, ...],
    conditioning_shape: ConditioningShape,
) -> str:
    """Section 5's compatible-null-comparison key. Two subjects are
    calibration-compatible only when every one of these fields matches
    exactly; regime/time-period are deliberately excluded (they are
    stability dimensions -- section 5 -- evaluated separately in
    :class:`StabilitySlice` rather than folded into the matching key,
    which would otherwise make an already-narrow stratum sparser still)."""

    scope = "+".join(sorted({item.value for item in market_scope}))
    return (
        f"{research_design.value}|{outcome_family.value}|H{horizon_sessions}|"
        f"{scope}|{conditioning_shape.value}"
    )


# ==========================================================================
# Section 3: template-matched null-control specification
# ==========================================================================

# Extends (never redefines) ``research_calibration.NULL_CONTROL_KINDS``:
# every one of v1's eleven kinds remains valid for a v1 record; v2's own
# larger, template-matched battery additionally uses the kinds below,
# exactly mirroring how v1 itself extended ``cross_market_research.
# NEGATIVE_CONTROL_KINDS`` in its own, separate module-level tuple rather
# than editing the original.
NULL_CONTROL_KINDS_V2 = (
    "CONDITIONAL_OUTCOME_NULLED",
    "INTERACTION_OUTCOME_NULLED",
    "REGIME_COMPARISON_OUTCOME_NULLED",
    "CROSS_MARKET_BLOCK_DISPLACED",
    "CROSS_MARKET_LAG_DISPLACED",
    "CROSS_MARKET_SIGN_RANDOMIZED",
    "PERSISTENCE_STATE_BLOCK_PERMUTED",
)

MINIMUM_V2_CONTROL_BUDGET = 120
MAXIMUM_V2_CONTROL_BUDGET = 180


@dataclass(frozen=True, slots=True)
class NullControlSpecV2:
    """One declared, template-matched control (sections 3/4). Unlike v1's
    ``NullControlSpec`` (which always nulls the *feature* and always uses
    an unconditional/single-split sample shape), a v2 spec may null the
    *outcome* or the *state series* instead while preserving the real
    conditioning/interaction/persistence structure exactly -- so its
    ``secondary_feature_id``/``secondary_condition_*`` fields carry the
    real, untransformed conditioning evidence the application layer needs
    to reproduce the exact same sample/benchmark partition a matched
    genuine hypothesis of the same design would use."""

    spec_id: str
    calibration_id: str
    research_design: ResearchDesign
    conditioning_shape: ConditioningShape
    family: NullControlFamily
    nature: NullControlNature
    design_family: NullControlDesignFamily
    control_kind: str
    null_method: NullGenerationMethod
    null_target: str
    source_feature_id: str
    source_feature_family: FeatureFamily
    secondary_feature_id: str | None
    secondary_condition_rule: str | None
    secondary_condition_label: str | None
    outcome_id: str
    outcome_family: OutcomeFamily
    horizon_sessions: int
    market_scope: tuple[MarketId, ...]
    method_params: tuple[tuple[str, str], ...]
    matched_stratum: str
    rationale: str
    ordinal: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        research_design: ResearchDesign | str,
        conditioning_shape: ConditioningShape | str,
        family: NullControlFamily | str,
        nature: NullControlNature | str,
        design_family: NullControlDesignFamily | str,
        control_kind: str,
        null_method: NullGenerationMethod | str,
        null_target: str,
        source_feature_id: str,
        source_feature_family: FeatureFamily | str,
        secondary_feature_id: str | None,
        secondary_condition_rule: str | None,
        secondary_condition_label: str | None,
        outcome_id: str,
        outcome_family: OutcomeFamily | str,
        horizon_sessions: int,
        market_scope: tuple[MarketId | str, ...],
        method_params: tuple[tuple[str, str], ...],
        rationale: str,
        ordinal: int,
        created_at: datetime,
    ) -> NullControlSpecV2:
        if not calibration_id:
            raise CalibrationV2Error("NULL_CONTROL_V2_CALIBRATION_ID_REQUIRED")
        design_value = ResearchDesign(research_design)
        shape_value = ConditioningShape(conditioning_shape)
        family_value = NullControlFamily(family)
        nature_value = NullControlNature(nature)
        design_family_value = NullControlDesignFamily(design_family)
        if control_kind not in NULL_CONTROL_KINDS_V2:
            raise CalibrationV2Error("NULL_CONTROL_V2_KIND_INVALID")
        method_value = NullGenerationMethod(null_method)
        if null_target not in ("FEATURE", "OUTCOME", "STATE_SERIES"):
            raise CalibrationV2Error("NULL_CONTROL_V2_TARGET_INVALID")
        if not source_feature_id or not outcome_id:
            raise CalibrationV2Error("NULL_CONTROL_V2_SOURCE_AND_OUTCOME_REQUIRED")
        feature_family_value = FeatureFamily(source_feature_family)
        outcome_family_value = OutcomeFamily(outcome_family)
        if (secondary_feature_id is None) != (secondary_condition_rule is None):
            raise CalibrationV2Error("NULL_CONTROL_V2_SECONDARY_FIELDS_MUST_BE_DECLARED_TOGETHER")
        if (
            shape_value
            in (
                ConditioningShape.NUMERIC_SPLIT,
                ConditioningShape.LABEL_SPLIT,
                ConditioningShape.NUMERIC_INTERACTION,
            )
            and secondary_feature_id is None
        ):
            raise CalibrationV2Error("NULL_CONTROL_V2_CONDITIONING_SHAPE_REQUIRES_SECONDARY_FIELD")
        if shape_value is ConditioningShape.REGIME_LABEL_COMPARISON and not any(
            key == "label_a" for key, _ in method_params
        ):
            raise CalibrationV2Error("NULL_CONTROL_V2_REGIME_COMPARISON_REQUIRES_LABEL_A_PARAM")
        if horizon_sessions < 1:
            raise CalibrationV2Error("NULL_CONTROL_V2_HORIZON_INVALID")
        scope = tuple(MarketId(item) for item in market_scope)
        if not scope:
            raise CalibrationV2Error("NULL_CONTROL_V2_MARKET_SCOPE_REQUIRED")
        if ordinal < 0:
            raise CalibrationV2Error("NULL_CONTROL_V2_ORDINAL_INVALID")
        params = tuple(sorted(method_params))
        stratum = matched_stratum_key(
            research_design=design_value,
            outcome_family=outcome_family_value,
            horizon_sessions=horizon_sessions,
            market_scope=scope,
            conditioning_shape=shape_value,
        )
        created = _aware(created_at, "NULL_CONTROL_V2_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V2,
            calibration_id,
            design_value,
            shape_value,
            family_value,
            nature_value,
            design_family_value,
            control_kind,
            method_value,
            null_target,
            source_feature_id,
            feature_family_value,
            secondary_feature_id,
            secondary_condition_rule,
            secondary_condition_label,
            outcome_id,
            outcome_family_value,
            horizon_sessions,
            scope,
            params,
            stratum,
            _text(rationale, "NULL_CONTROL_V2_RATIONALE_INVALID", maximum=1_000),
            ordinal,
        )
        return cls(
            spec_id=deterministic_id("research_calibration_v2_null_control", payload),
            calibration_id=calibration_id,
            research_design=design_value,
            conditioning_shape=shape_value,
            family=family_value,
            nature=nature_value,
            design_family=design_family_value,
            control_kind=control_kind,
            null_method=method_value,
            null_target=null_target,
            source_feature_id=source_feature_id,
            source_feature_family=feature_family_value,
            secondary_feature_id=secondary_feature_id,
            secondary_condition_rule=secondary_condition_rule,
            secondary_condition_label=secondary_condition_label,
            outcome_id=outcome_id,
            outcome_family=outcome_family_value,
            horizon_sessions=horizon_sessions,
            market_scope=scope,
            method_params=params,
            matched_stratum=stratum,
            rationale=payload[21],
            ordinal=ordinal,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 5/6/7: matched-stratum compatibility and template-matched
# percentile
# ==========================================================================


@dataclass(frozen=True, slots=True)
class MatchedCalibrationResult:
    """The pure result of comparing one subject's effect size against its
    matched-stratum null population (sections 6/7) -- never persisted on
    its own; always embedded on a retained :class:`DesignClassification`.
    ``sufficient is False`` means ``matched_percentile`` is ``None`` by
    construction (fail-closed: sections 6's requirement is enforced here,
    not left to a caller to remember)."""

    matched_stratum: str
    matched_null_count: int
    sufficient: bool
    matched_percentile: Decimal | None
    pooled_null_count: int
    pooled_percentile: Decimal | None

    @classmethod
    def evaluate(
        cls,
        *,
        statistic: Decimal | None,
        matched_null_statistics: Sequence[Decimal],
        pooled_null_statistics: Sequence[Decimal],
        matched_stratum: str,
        minimum_matched_count: int = MINIMUM_MATCHED_NULL_COUNT,
    ) -> MatchedCalibrationResult:
        from apex.domain.research_calibration import null_calibration_percentile

        matched_count = len(matched_null_statistics)
        sufficient = matched_count >= minimum_matched_count
        matched_percentile = (
            null_calibration_percentile(
                statistic=statistic, null_statistics=matched_null_statistics
            )
            if sufficient and statistic is not None
            else None
        )
        pooled_percentile = (
            null_calibration_percentile(statistic=statistic, null_statistics=pooled_null_statistics)
            if statistic is not None
            else None
        )
        return cls(
            matched_stratum=matched_stratum,
            matched_null_count=matched_count,
            sufficient=sufficient,
            matched_percentile=matched_percentile,
            pooled_null_count=len(pooled_null_statistics),
            pooled_percentile=pooled_percentile,
        )


@dataclass(frozen=True, slots=True)
class DesignClassification:
    """Section 2's mandatory design declaration plus section 7's required
    template-matched percentile result, for exactly one subject (a null
    control -- v1 *or* v2 -- or a genuine hypothesis). One row per
    ``(calibration_id, subject_id)``; a v1 spec_id classified here is a
    new, additive, non-destructive fact that never rewrites the original
    ``research_calibration_null_control`` row it references."""

    classification_id: str
    calibration_id: str
    subject_id: str
    subject_kind: str
    research_design: ResearchDesign
    conditioning_shape: ConditioningShape
    matched: MatchedCalibrationResult
    raw_significant: bool | None
    corrected_significant: bool | None
    cohens_d: Decimal | None
    evidence_grade_v2: EvidenceGrade
    insufficiency_code: str | None
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        subject_id: str,
        subject_kind: str,
        research_design: ResearchDesign | str,
        conditioning_shape: ConditioningShape | str,
        matched: MatchedCalibrationResult,
        raw_significant: bool | None,
        corrected_significant: bool | None,
        cohens_d: Decimal | None,
        evidence_grade_v2: EvidenceGrade | str,
        created_at: datetime,
    ) -> DesignClassification:
        if not calibration_id or not subject_id:
            raise CalibrationV2Error("DESIGN_CLASSIFICATION_IDENTITY_REQUIRED")
        if subject_kind not in ("NULL_CONTROL", "HYPOTHESIS"):
            raise CalibrationV2Error("DESIGN_CLASSIFICATION_SUBJECT_KIND_INVALID")
        design_value = ResearchDesign(research_design)
        shape_value = ConditioningShape(conditioning_shape)
        grade_value = EvidenceGrade(evidence_grade_v2)
        insufficiency = None if matched.sufficient else CALIBRATION_STRATUM_INSUFFICIENT
        created = _aware(created_at, "DESIGN_CLASSIFICATION_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V2,
            calibration_id,
            subject_id,
            subject_kind,
            design_value,
            shape_value,
            matched.matched_stratum,
            matched.matched_null_count,
            matched.sufficient,
            matched.matched_percentile,
            matched.pooled_null_count,
            matched.pooled_percentile,
            raw_significant,
            corrected_significant,
            cohens_d,
            grade_value,
            insufficiency,
        )
        return cls(
            classification_id=deterministic_id(
                "research_calibration_v2_design_classification", payload
            ),
            calibration_id=calibration_id,
            subject_id=subject_id,
            subject_kind=subject_kind,
            research_design=design_value,
            conditioning_shape=shape_value,
            matched=matched,
            raw_significant=raw_significant,
            corrected_significant=corrected_significant,
            cohens_d=cohens_d,
            evidence_grade_v2=grade_value,
            insufficiency_code=insufficiency,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 10/11: time-period and volatility-regime stability slices
# ==========================================================================


@dataclass(frozen=True, slots=True)
class StabilitySlice:
    """One honest, cheaply-recomputed raw/corrected false-discovery slice
    over an already-evaluated null population, restricted to one
    time-period or volatility-regime partition (sections 10/11).
    Deliberately does not claim a holdout or robustness survival rate --
    no full governed pipeline reran per partition, only a direct
    mean-comparison significance check over the same already-retained
    trial evidence, restricted to that partition's dates. ``sample_adequate
    is False`` still retains the slice (never hides it) but marks it
    unreliable for a per-slice conclusion, exactly mirroring v1's
    ``CalibrationBreakdown.sample_adequate`` convention."""

    slice_id: str
    calibration_id: str
    dimension_kind: str
    dimension_label: str
    control_count: int
    sample_adequate: bool
    raw_positive_count: int
    raw_false_positive_rate: Decimal
    corrected_positive_count: int
    corrected_false_positive_rate: Decimal
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
        raw_positive_count: int,
        corrected_positive_count: int,
        minimum_adequate_count: int,
        note: str,
        created_at: datetime,
    ) -> StabilitySlice:
        from apex.domain.research_methodology import bound_statistical_decimal

        if dimension_kind not in ("TIME_PERIOD", "VOLATILITY_REGIME"):
            raise CalibrationV2Error("STABILITY_SLICE_DIMENSION_KIND_INVALID")
        if not calibration_id:
            raise CalibrationV2Error("STABILITY_SLICE_CALIBRATION_ID_REQUIRED")
        if control_count < 0 or raw_positive_count < 0 or corrected_positive_count < 0:
            raise CalibrationV2Error("STABILITY_SLICE_COUNTS_INVALID")
        if raw_positive_count > control_count or corrected_positive_count > control_count:
            raise CalibrationV2Error("STABILITY_SLICE_COUNTS_OUT_OF_BOUNDS")
        raw_rate = (
            bound_statistical_decimal(Decimal(raw_positive_count) / Decimal(control_count))
            if control_count > 0
            else Decimal(0)
        )
        corrected_rate = (
            bound_statistical_decimal(Decimal(corrected_positive_count) / Decimal(control_count))
            if control_count > 0
            else Decimal(0)
        )
        adequate = control_count >= minimum_adequate_count
        created = _aware(created_at, "STABILITY_SLICE_CREATED_AT")
        label = _text(dimension_label, "STABILITY_SLICE_LABEL_INVALID", maximum=80)
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V2,
            calibration_id,
            dimension_kind,
            label,
            control_count,
            adequate,
            raw_positive_count,
            corrected_positive_count,
            _text(note, "STABILITY_SLICE_NOTE_INVALID", maximum=1_000),
        )
        return cls(
            slice_id=deterministic_id("research_calibration_v2_stability_slice", payload),
            calibration_id=calibration_id,
            dimension_kind=dimension_kind,
            dimension_label=label,
            control_count=control_count,
            sample_adequate=adequate,
            raw_positive_count=raw_positive_count,
            raw_false_positive_rate=raw_rate,
            corrected_positive_count=corrected_positive_count,
            corrected_false_positive_rate=corrected_rate,
            note=payload[8],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 12: bounded walk-forward null subset
# ==========================================================================


@dataclass(frozen=True, slots=True)
class WalkForwardNullSummary:
    """Section 12's required per-control fold-survival record for the
    bounded, predeclared walk-forward-null subset. ``final_evidence_grade``
    reuses v1's ``EvidenceGrade`` vocabulary unmodified."""

    summary_id: str
    calibration_id: str
    spec_id: str
    fold_count: int
    folds_beating_benchmark: int
    fold_survival_rate: Decimal
    validation_survived: bool
    holdout_survived: bool
    robustness_survived: bool
    final_evidence_grade: EvidenceGrade
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        spec_id: str,
        fold_count: int,
        folds_beating_benchmark: int,
        validation_survived: bool,
        holdout_survived: bool,
        robustness_survived: bool,
        final_evidence_grade: EvidenceGrade | str,
        created_at: datetime,
    ) -> WalkForwardNullSummary:
        from apex.domain.research_methodology import bound_statistical_decimal

        if not calibration_id or not spec_id:
            raise CalibrationV2Error("WALK_FORWARD_NULL_SUMMARY_IDENTITY_REQUIRED")
        if fold_count < 0 or folds_beating_benchmark < 0 or folds_beating_benchmark > fold_count:
            raise CalibrationV2Error("WALK_FORWARD_NULL_SUMMARY_FOLD_COUNTS_INVALID")
        grade_value = EvidenceGrade(final_evidence_grade)
        rate = (
            bound_statistical_decimal(Decimal(folds_beating_benchmark) / Decimal(fold_count))
            if fold_count > 0
            else Decimal(0)
        )
        created = _aware(created_at, "WALK_FORWARD_NULL_SUMMARY_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V2,
            calibration_id,
            spec_id,
            fold_count,
            folds_beating_benchmark,
            validation_survived,
            holdout_survived,
            robustness_survived,
            grade_value,
        )
        return cls(
            summary_id=deterministic_id(
                "research_calibration_v2_walk_forward_null_summary", payload
            ),
            calibration_id=calibration_id,
            spec_id=spec_id,
            fold_count=fold_count,
            folds_beating_benchmark=folds_beating_benchmark,
            fold_survival_rate=rate,
            validation_survived=validation_survived,
            holdout_survived=holdout_survived,
            robustness_survived=robustness_survived,
            final_evidence_grade=grade_value,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 17: follow-up requalification (new vocabulary, cannot reuse v1's
# closed FollowUpPriorityClass column)
# ==========================================================================


class FollowUpQualificationV2(StrEnum):
    CALIBRATION_QUALIFIED = "CALIBRATION_QUALIFIED"
    CALIBRATION_INSUFFICIENT = "CALIBRATION_INSUFFICIENT"
    NO_LONGER_JUSTIFIED = "NO_LONGER_JUSTIFIED"
    REJECTED = "REJECTED"


def classify_follow_up_v2(
    *, matched: MatchedCalibrationResult, evidence_grade: EvidenceGrade
) -> FollowUpQualificationV2:
    """Section 17's deterministic requalification rule, derived entirely
    from retained matched-calibration evidence -- never from whether the
    outcome is convenient for the three real follow-ups (section 15's
    "no threshold may be selected based on whether it helps the three real
    follow-ups" applies here too)."""

    if not matched.sufficient:
        return FollowUpQualificationV2.CALIBRATION_INSUFFICIENT
    if evidence_grade is EvidenceGrade.UNRELIABLE:
        return FollowUpQualificationV2.REJECTED
    if evidence_grade is EvidenceGrade.EXPLORATORY:
        return FollowUpQualificationV2.NO_LONGER_JUSTIFIED
    return FollowUpQualificationV2.CALIBRATION_QUALIFIED


@dataclass(frozen=True, slots=True)
class FollowUpQualification:
    """One retained requalification of an already-retained
    ``cross_market_research.FollowUpProposal`` (section 17). Never
    modifies the historical proposal row it references."""

    assessment_id: str
    calibration_id: str
    follow_up_id: str
    parent_hypothesis_id: str
    research_design: ResearchDesign
    horizon_sessions: int
    matched: MatchedCalibrationResult
    evidence_grade_v2: EvidenceGrade
    qualification: FollowUpQualificationV2
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
        research_design: ResearchDesign | str,
        horizon_sessions: int,
        matched: MatchedCalibrationResult,
        evidence_grade_v2: EvidenceGrade | str,
        qualification: FollowUpQualificationV2 | str,
        rationale: str,
        created_at: datetime,
    ) -> FollowUpQualification:
        if not calibration_id or not follow_up_id or not parent_hypothesis_id:
            raise CalibrationV2Error("FOLLOW_UP_QUALIFICATION_LINEAGE_REQUIRED")
        if horizon_sessions < 1:
            raise CalibrationV2Error("FOLLOW_UP_QUALIFICATION_HORIZON_INVALID")
        design_value = ResearchDesign(research_design)
        grade_value = EvidenceGrade(evidence_grade_v2)
        qualification_value = FollowUpQualificationV2(qualification)
        created = _aware(created_at, "FOLLOW_UP_QUALIFICATION_CREATED_AT")
        payload = (
            RESEARCH_SIGNAL_CALIBRATION_V2,
            calibration_id,
            follow_up_id,
            parent_hypothesis_id,
            design_value,
            horizon_sessions,
            matched.matched_stratum,
            matched.matched_null_count,
            matched.sufficient,
            matched.matched_percentile,
            grade_value,
            qualification_value,
            _text(rationale, "FOLLOW_UP_QUALIFICATION_RATIONALE_INVALID", maximum=2_000),
        )
        return cls(
            assessment_id=deterministic_id(
                "research_calibration_v2_follow_up_qualification", payload
            ),
            calibration_id=calibration_id,
            follow_up_id=follow_up_id,
            parent_hypothesis_id=parent_hypothesis_id,
            research_design=design_value,
            horizon_sessions=horizon_sessions,
            matched=matched,
            evidence_grade_v2=grade_value,
            qualification=qualification_value,
            rationale=payload[12],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def research_calibration_v2_warnings() -> tuple[str, ...]:
    from apex.domain.research_calibration import CALIBRATION_ALPHA_DISCLAIMER

    return (
        CALIBRATION_V2_WARNING,
        CALIBRATION_ALPHA_DISCLAIMER,
        CALIBRATION_V2_LINEAGE_NOTE,
        CALIBRATION_FINITE_SAMPLE_WARNING,
    )
