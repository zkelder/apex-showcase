"""Corporate Research Calibration Depth v1.

Answers one sharpened question about the retained Cross-Market Research v3
agenda (``domain.corporate_market_research``): *for each corporate research
design Apex intends to use, what does equally strong evidence look like
under structurally matched null experiments?* This is methodology
calibration, not corporate hypothesis discovery -- it appends new evidence
and derived assessments on top of the 20 genuine v3 hypotheses and 17 v3
negative controls; it rewrites none of them.

Strictly additive on top of three existing modules, every genuinely shared
primitive reused unmodified:

* ``domain.corporate_market_research`` supplies the entire corporate feature/
  outcome/family vocabulary (``CorporateFeatureId``, ``CorporateOutcomeId``,
  ``CorporateHypothesisFamily``, ``GENUINE_CORPORATE_FEATURE_IDS``,
  ``CHANGE_BASED_ELIGIBLE_FEATURE_IDS``) and the already-retained v3 agenda/
  hypothesis/execution records this module reads but never rewrites.
* ``domain.research_calibration_v2`` supplies ``ResearchDesign`` (the v3
  commit already added ``CROSS_SECTIONAL``/``CHANGE_BASED``/``FILING_EVENT``
  to this taxonomy for exactly this purpose), ``ConditioningShape``,
  ``MatchedCalibrationResult``, ``StabilitySlice``, ``FollowUpQualification``/
  ``FollowUpQualificationV2``, and ``classify_follow_up_v2`` -- all reused
  verbatim; none of their shapes are corporate-specific in a way that would
  require widening.
* ``domain.research_calibration`` supplies ``EvidenceGrade``/
  ``EvidenceGradePolicy``/``EVIDENCE_GRADE_POLICY_V1``/``derive_evidence_
  grade``, ``EvidenceProfile``, ``PracticalSignificance``/``classify_
  practical_significance``, ``CalibrationRateSummary``, ``NullGeneration
  Method``, ``EffectSizeFloorPolicy``/``EFFECT_SIZE_FLOOR_POLICY_V1``/
  ``effect_meets_floor``, and ``CalibrationRunStatus`` -- all reused
  verbatim.

What is genuinely new here (no faithful representation in any of the
above): a corporate-specific null-generation-mechanism vocabulary bound to
the five research designs the v3 agenda actually used (section 4); a
declared, bounded, per-stratum calibration plan built from an explicit
strata inventory over the 20 genuine v3 hypotheses (sections 2, 10); a
provenance record for each generated null control retaining generation
method, seed, preserved properties, and destroyed relationship (section
11); a resumable calibration-run cursor; a non-destructive reassessment
record covering both the 20 genuine hypotheses (section 15) and the 17
original v3 controls (section 16) under the deepened battery, including an
optional incremental-contribution calibration slice for the four
conditional/interaction hypotheses (section 17); and one retained quality
report (section 22).

Why ``research_calibration_plan``/``research_calibration_v2_null_control``
and their sibling v1/v2 tables are *not* reused for the durable records
this module needs, even though several of their shapes look similar:
``research_calibration_plan.source_agenda_id`` foreign-keys to
``cross_market_research_agenda`` (not ``corporate_market_research_agenda``)
and ``dataset_snapshot_id``/``cross_market_dataset_id`` to price-only
dataset lineage; ``research_calibration_v2_null_control``'s and
``research_calibration_v2_design_classification``'s ``research_design``
CHECK constraints are closed over v1/v2's original six-member taxonomy and
do not admit ``CROSS_SECTIONAL``/``CHANGE_BASED``/``FILING_EVENT`` at the
database layer even though the Python enum already carries them;
``research_calibration_prior_recalibration``/``research_calibration_v2_
follow_up_qualification`` foreign-key ``hypothesis_id``/``parent_
hypothesis_id`` to ``cross_market_hypothesis``, not ``corporate_market_
hypothesis``. This is the exact same "generic governed-trial tables reused
unmodified, agenda-shaped wrapper tables re-declared" split migration
``0089`` itself already documents and justifies for the same reason.
Migration ``0090`` therefore reuses ``0079``'s governed trial lifecycle
(``research_governance_policy``, ``research_trial``, ``research_trial_
family``, ``robustness_check_run``, ``research_conclusion``, ``research_
promotion_evaluation``) unmodified for every null control's actual
statistical evidence, and only re-declares the agenda-shaped wrapper
records those generic tables have no concept of.

Every record here is ``authority = "NONE"``. Nothing here creates a
Strategy, Shared Opportunity, PAPER order, execution authority, or a new
corporate hypothesis-discovery budget -- it only asks how much the v3
agenda's own findings should be trusted once compared against a
sufficiently deep, structurally matched null population, and it never
executes the 13 retained v3 follow-up proposals it requalifies.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.corporate_market_research import CorporateFeatureId, CorporateOutcomeId
from apex.domain.research_calibration import CalibrationRunStatus, EvidenceGrade
from apex.domain.research_calibration_v2 import ConditioningShape, ResearchDesign
from apex.domain.values import content_digest, deterministic_id

CORPORATE_CALIBRATION_DEPTH_V1 = "CORPORATE_RESEARCH_CALIBRATION_DEPTH_V1"
CORPORATE_CALIBRATION_DEPTH_WARNING = (
    "CORPORATE RESEARCH CALIBRATION DEPTH EVIDENCE ONLY -- METHODOLOGY CALIBRATION OVER THE "
    "ALREADY-RETAINED CROSS-MARKET RESEARCH V3 AGENDA, NOT NEW CORPORATE HYPOTHESIS DISCOVERY; "
    "NO STRATEGY, OPPORTUNITY, PAPER, OR LIVE AUTHORITY"
)
V3_EVIDENCE_PRESERVATION_NOTE = (
    "This module never rewrites the v3 agenda, its 20 genuine hypotheses, its 17 negative "
    "controls, any research_trial/research_conclusion/research_knowledge_entry row, its one "
    "retained contradiction, or its 13 follow-up proposals -- every fact here is a new, "
    "separately retained record that references v3 evidence by id."
)
FOLLOW_UP_EXECUTION_PROHIBITION_NOTE = (
    "Requalifying a v3 follow-up proposal (section 23) never executes it; execution is deferred "
    "to a possible later replication milestone gated on CALIBRATION_QUALIFIED status."
)
MINIMUM_MATCHED_NULL_COUNT = 12  # never lowered from research_calibration_v2's own floor.


class CorporateCalibrationDepthError(ValueError):
    """A corporate calibration-depth domain invariant was violated."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CorporateCalibrationDepthError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _text(value: str, code: str, *, maximum: int = 2_000) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CorporateCalibrationDepthError(code)
    return normalized


# ==========================================================================
# Section 4: corporate null-design taxonomy. Ten mechanisms, four per
# cross-sectional/change-based design (sections 5/6), three per filing-
# event design (section 7), three per conditional/interaction design
# (section 8) -- never one pooled "shuffle the feature" null for every
# shape. Each mechanism below maps to exactly one reused ``research_
# calibration.NullGenerationMethod`` member (declared alongside it in the
# application layer); this enum names the corporate-specific *intent* the
# generic method serves, matching how ``domain.corporate_market_research.
# CORPORATE_NEGATIVE_CONTROL_KINDS`` already names v3's five original
# control kinds one level above their own reused generation primitives.
# ==========================================================================


class CorporateNullMechanism(StrEnum):
    """Section 5/6 (cross-sectional level and change-based designs)."""

    SEEDED_TEMPORAL_DISPLACEMENT = "SEEDED_TEMPORAL_DISPLACEMENT"
    SEEDED_ISSUER_DERANGEMENT = "SEEDED_ISSUER_DERANGEMENT"
    SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION = "SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION"
    SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION = "SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION"

    # Section 7 (filing-event design).
    SEEDED_RANDOM_NON_EVENT_DATE_MATCHED = "SEEDED_RANDOM_NON_EVENT_DATE_MATCHED"
    SEEDED_DISPLACED_FILING_EVENT_DATE = "SEEDED_DISPLACED_FILING_EVENT_DATE"
    SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT = "SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT"

    # Section 8 (conditional market-regime and fundamental x market
    # interaction designs) -- every one nulls the OUTCOME while preserving
    # the real conditioning-group membership and real feature values
    # exactly, mirroring ``research_calibration_v2``'s own "null the
    # outcome, keep the real conditioning structure" precedent for these
    # two shapes.
    SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION = (
        "SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION"
    )
    SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION = (
        "SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION"
    )
    SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION = "SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION"


CROSS_SECTIONAL_MECHANISMS: tuple[CorporateNullMechanism, ...] = (
    CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT,
    CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
    CorporateNullMechanism.SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION,
    CorporateNullMechanism.SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION,
)
FILING_EVENT_MECHANISMS: tuple[CorporateNullMechanism, ...] = (
    CorporateNullMechanism.SEEDED_RANDOM_NON_EVENT_DATE_MATCHED,
    CorporateNullMechanism.SEEDED_DISPLACED_FILING_EVENT_DATE,
    CorporateNullMechanism.SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT,
)
CONDITIONAL_INTERACTION_MECHANISMS: tuple[CorporateNullMechanism, ...] = (
    CorporateNullMechanism.SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION,
    CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION,
    CorporateNullMechanism.SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION,
)
MECHANISMS_BY_DESIGN: dict[ResearchDesign, tuple[CorporateNullMechanism, ...]] = {
    ResearchDesign.CROSS_SECTIONAL: CROSS_SECTIONAL_MECHANISMS,
    ResearchDesign.CHANGE_BASED: CROSS_SECTIONAL_MECHANISMS,
    ResearchDesign.FILING_EVENT: FILING_EVENT_MECHANISMS,
    ResearchDesign.CONDITIONAL: CONDITIONAL_INTERACTION_MECHANISMS,
    ResearchDesign.INTERACTION: CONDITIONAL_INTERACTION_MECHANISMS,
}
NULL_TARGETS_BY_MECHANISM: dict[CorporateNullMechanism, str] = {
    CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT: "FEATURE",
    CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT: "FEATURE",
    CorporateNullMechanism.SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION: "FEATURE",
    CorporateNullMechanism.SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION: "FEATURE",
    CorporateNullMechanism.SEEDED_RANDOM_NON_EVENT_DATE_MATCHED: "EVENT_DATE",
    CorporateNullMechanism.SEEDED_DISPLACED_FILING_EVENT_DATE: "EVENT_DATE",
    CorporateNullMechanism.SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT: "EVENT_ISSUER",
    CorporateNullMechanism.SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION: "OUTCOME",
    CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION: "OUTCOME",
    CorporateNullMechanism.SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION: "OUTCOME",
}


# ==========================================================================
# Section 2/10: strata inventory and the bounded per-stratum calibration
# target derived from it. Built once, before any control is generated or
# observed (section 10's "declare a bounded calibration plan before
# observing its results").
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateStratumTarget:
    """One matched-calibration stratum (section 2's compatibility key:
    research design, outcome, horizon, and conditioning shape -- the exact
    fields ``application.corporate_market_research.corporate_matched_
    stratum_key`` already uses) together with the genuine v3 hypotheses it
    backs and the new-control budget this plan commits to before running
    anything (section 10)."""

    matched_stratum: str
    research_design: ResearchDesign
    conditioning_shape: ConditioningShape
    outcome_id: CorporateOutcomeId
    horizon_sessions: int
    genuine_hypothesis_ids: tuple[str, ...]
    backing_feature_ids: tuple[CorporateFeatureId, ...]
    secondary_conditions: tuple[tuple[CorporateFeatureId, str, str], ...]
    original_matched_null_count: int
    original_sufficient: bool
    mechanisms: tuple[CorporateNullMechanism, ...]
    target_new_control_count: int

    @classmethod
    def create(
        cls,
        *,
        matched_stratum: str,
        research_design: ResearchDesign | str,
        conditioning_shape: ConditioningShape | str,
        outcome_id: CorporateOutcomeId | str,
        horizon_sessions: int,
        genuine_hypothesis_ids: tuple[str, ...],
        backing_feature_ids: tuple[CorporateFeatureId | str, ...],
        original_matched_null_count: int,
        target_new_control_count: int,
        secondary_conditions: tuple[tuple[CorporateFeatureId | str, str, str], ...] = (),
    ) -> CorporateStratumTarget:
        if not matched_stratum:
            raise CorporateCalibrationDepthError("STRATUM_TARGET_KEY_REQUIRED")
        design = ResearchDesign(research_design)
        if design not in MECHANISMS_BY_DESIGN:
            raise CorporateCalibrationDepthError("STRATUM_TARGET_DESIGN_NOT_CORPORATE_RELEVANT")
        shape = ConditioningShape(conditioning_shape)
        outcome = CorporateOutcomeId(outcome_id)
        if horizon_sessions < 1:
            raise CorporateCalibrationDepthError("STRATUM_TARGET_HORIZON_INVALID")
        if not genuine_hypothesis_ids:
            raise CorporateCalibrationDepthError("STRATUM_TARGET_REQUIRES_A_BACKING_HYPOTHESIS")
        features = tuple(CorporateFeatureId(item) for item in backing_feature_ids)
        if not features:
            raise CorporateCalibrationDepthError("STRATUM_TARGET_REQUIRES_A_BACKING_FEATURE")
        if original_matched_null_count < 0:
            raise CorporateCalibrationDepthError("STRATUM_TARGET_ORIGINAL_COUNT_INVALID")
        if not (20 <= target_new_control_count <= 40):
            raise CorporateCalibrationDepthError("STRATUM_TARGET_NEW_CONTROL_COUNT_OUT_OF_BOUNDS")
        conditions = tuple(
            (CorporateFeatureId(feature), dimension, label)
            for feature, dimension, label in secondary_conditions
        )
        requires_conditions = design in (ResearchDesign.CONDITIONAL, ResearchDesign.INTERACTION)
        if requires_conditions and not conditions:
            raise CorporateCalibrationDepthError(
                "STRATUM_TARGET_CONDITIONAL_OR_INTERACTION_REQUIRES_SECONDARY_CONDITIONS"
            )
        if not requires_conditions and conditions:
            raise CorporateCalibrationDepthError(
                "STRATUM_TARGET_SECONDARY_CONDITIONS_ONLY_VALID_FOR_CONDITIONAL_OR_INTERACTION"
            )
        return cls(
            matched_stratum=matched_stratum,
            research_design=design,
            conditioning_shape=shape,
            outcome_id=outcome,
            horizon_sessions=horizon_sessions,
            genuine_hypothesis_ids=tuple(sorted(set(genuine_hypothesis_ids))),
            backing_feature_ids=tuple(sorted(set(features), key=lambda item: item.value)),
            secondary_conditions=tuple(
                sorted(set(conditions), key=lambda item: (item[0].value, item[1]))
            ),
            original_matched_null_count=original_matched_null_count,
            original_sufficient=original_matched_null_count >= MINIMUM_MATCHED_NULL_COUNT,
            mechanisms=MECHANISMS_BY_DESIGN[design],
            target_new_control_count=target_new_control_count,
        )


@dataclass(frozen=True, slots=True)
class CorporateCalibrationPlan:
    """Section 10's declared, bounded, immutable calibration plan --
    registered *before* any control is generated or observed. Every
    ``CorporateStratumTarget`` inside ``stratum_targets`` was built from
    section 2's inventory of the 20 already-retained genuine v3
    hypotheses; the budget is never widened after this is registered."""

    calibration_id: str
    version: str
    objective: str
    source_agenda_id: str
    corporate_dataset_id: str
    equity_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    stratum_targets: tuple[CorporateStratumTarget, ...]
    controls_per_stratum: int
    total_new_control_budget: int
    minimum_matched_null_count: int
    random_seed_policy: str
    registered_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        version: str,
        objective: str,
        source_agenda_id: str,
        corporate_dataset_id: str,
        equity_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
        stratum_targets: tuple[CorporateStratumTarget, ...],
        controls_per_stratum: int,
        random_seed_policy: str,
        registered_at: datetime,
        minimum_matched_null_count: int = MINIMUM_MATCHED_NULL_COUNT,
    ) -> CorporateCalibrationPlan:
        if (
            not source_agenda_id
            or not corporate_dataset_id
            or not equity_snapshot_id
            or not replay_run_id
            or not cross_market_dataset_id
        ):
            raise CorporateCalibrationDepthError("CALIBRATION_PLAN_LINEAGE_REQUIRED")
        if not stratum_targets:
            raise CorporateCalibrationDepthError("CALIBRATION_PLAN_REQUIRES_AT_LEAST_ONE_STRATUM")
        keys = [item.matched_stratum for item in stratum_targets]
        if len(keys) != len(set(keys)):
            raise CorporateCalibrationDepthError("CALIBRATION_PLAN_STRATA_MUST_BE_DISTINCT")
        if not (20 <= controls_per_stratum <= 40):
            raise CorporateCalibrationDepthError(
                "CALIBRATION_PLAN_CONTROLS_PER_STRATUM_OUT_OF_BOUNDS"
            )
        if any(item.target_new_control_count != controls_per_stratum for item in stratum_targets):
            raise CorporateCalibrationDepthError(
                "CALIBRATION_PLAN_STRATUM_BUDGET_MUST_MATCH_DECLARED_CONTROLS_PER_STRATUM"
            )
        total_budget = controls_per_stratum * len(stratum_targets)
        if total_budget > 500:
            raise CorporateCalibrationDepthError("CALIBRATION_PLAN_TOTAL_BUDGET_UNBOUNDED")
        if minimum_matched_null_count < MINIMUM_MATCHED_NULL_COUNT:
            raise CorporateCalibrationDepthError(
                "CALIBRATION_PLAN_MUST_NOT_LOWER_MINIMUM_MATCHED_NULL_COUNT"
            )
        ordered_targets = tuple(sorted(stratum_targets, key=lambda item: item.matched_stratum))
        registered = _aware(registered_at, "CALIBRATION_PLAN_REGISTERED_AT")
        payload = (
            CORPORATE_CALIBRATION_DEPTH_V1,
            _text(version, "CALIBRATION_PLAN_VERSION_INVALID", maximum=80),
            _text(objective, "CALIBRATION_PLAN_OBJECTIVE_INVALID"),
            source_agenda_id,
            corporate_dataset_id,
            equity_snapshot_id,
            replay_run_id,
            cross_market_dataset_id,
            tuple(item.matched_stratum for item in ordered_targets),
            controls_per_stratum,
            total_budget,
            minimum_matched_null_count,
            _text(random_seed_policy, "CALIBRATION_PLAN_SEED_POLICY_INVALID"),
        )
        return cls(
            calibration_id=deterministic_id("corporate_calibration_plan", payload),
            version=payload[1],
            objective=payload[2],
            source_agenda_id=source_agenda_id,
            corporate_dataset_id=corporate_dataset_id,
            equity_snapshot_id=equity_snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            stratum_targets=ordered_targets,
            controls_per_stratum=controls_per_stratum,
            total_new_control_budget=total_budget,
            minimum_matched_null_count=minimum_matched_null_count,
            random_seed_policy=payload[12],
            registered_at=registered,
            canonical_hash=content_digest(payload),
        )

    def stratum(self, matched_stratum: str) -> CorporateStratumTarget | None:
        for item in self.stratum_targets:
            if item.matched_stratum == matched_stratum:
                return item
        return None


# ==========================================================================
# Section 10 (run cursor): a bounded, resumable, mutable run over the
# declared plan's generated spec ids -- exactly mirrors ``research_
# calibration.CalibrationRun``/``cross_market_research.AgendaRun`` in
# shape and update discipline (append-only progress, no widening).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateCalibrationRun:
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
        cls,
        *,
        calibration: CorporateCalibrationPlan,
        spec_ids: tuple[str, ...],
        created_at: datetime,
    ) -> CorporateCalibrationRun:
        if not spec_ids:
            raise CorporateCalibrationDepthError("CALIBRATION_RUN_REQUIRES_AT_LEAST_ONE_SPEC")
        if len(spec_ids) > calibration.total_new_control_budget:
            raise CorporateCalibrationDepthError(
                "CALIBRATION_RUN_SPEC_COUNT_EXCEEDS_DECLARED_BUDGET"
            )
        created = _aware(created_at, "CALIBRATION_RUN_CREATED_AT")
        payload = (CORPORATE_CALIBRATION_DEPTH_V1, calibration.calibration_id, tuple(spec_ids))
        return cls(
            calibration_run_id=deterministic_id("corporate_calibration_run", payload),
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
    ) -> CorporateCalibrationRun:
        if self.status in (CalibrationRunStatus.COMPLETE, CalibrationRunStatus.FAILED):
            raise CorporateCalibrationDepthError(
                "CALIBRATION_RUN_CANNOT_ADVANCE_FROM_TERMINAL_STATE"
            )
        if trials_added < 0:
            raise CorporateCalibrationDepthError("CALIBRATION_RUN_TRIALS_ADDED_INVALID")
        return replace(
            self,
            cursor_ordinal=self.cursor_ordinal + 1,
            completed_spec_ids=(*self.completed_spec_ids, spec_id),
            status=CalibrationRunStatus.COMPLETE if is_final else CalibrationRunStatus.IN_PROGRESS,
            trials_executed=self.trials_executed + trials_added,
            updated_at=_aware(at, "CALIBRATION_RUN_UPDATED_AT"),
        )

    def fail(self, *, reason: str, at: datetime) -> CorporateCalibrationRun:
        if self.status is CalibrationRunStatus.COMPLETE:
            raise CorporateCalibrationDepthError("CALIBRATION_RUN_ALREADY_COMPLETE")
        return replace(
            self,
            status=CalibrationRunStatus.FAILED,
            failure_reason=_text(reason, "CALIBRATION_RUN_FAILURE_REASON_INVALID", maximum=500),
            updated_at=_aware(at, "CALIBRATION_RUN_UPDATED_AT"),
        )


# ==========================================================================
# Section 11: negative-control provenance. Every declared field below is
# one of the eleven facts section 11 requires be retained per control.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateNullControlSpec:
    spec_id: str
    calibration_id: str
    matched_stratum: str
    research_design: ResearchDesign
    conditioning_shape: ConditioningShape
    template_id: str
    mechanism: CorporateNullMechanism
    null_target: str
    feature_id: CorporateFeatureId
    outcome_id: CorporateOutcomeId
    horizon_sessions: int
    secondary_context_dimension: str | None
    secondary_condition_label: str | None
    seed: int
    preserved_properties: tuple[str, ...]
    destroyed_relationship: str
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
        matched_stratum: str,
        research_design: ResearchDesign | str,
        conditioning_shape: ConditioningShape | str,
        template_id: str,
        mechanism: CorporateNullMechanism | str,
        feature_id: CorporateFeatureId | str,
        outcome_id: CorporateOutcomeId | str,
        horizon_sessions: int,
        secondary_context_dimension: str | None,
        secondary_condition_label: str | None,
        seed: int,
        preserved_properties: tuple[str, ...],
        destroyed_relationship: str,
        rationale: str,
        ordinal: int,
        created_at: datetime,
    ) -> CorporateNullControlSpec:
        if not calibration_id or not matched_stratum:
            raise CorporateCalibrationDepthError("NULL_CONTROL_SPEC_LINEAGE_REQUIRED")
        design = ResearchDesign(research_design)
        shape = ConditioningShape(conditioning_shape)
        mechanism_value = CorporateNullMechanism(mechanism)
        if mechanism_value not in MECHANISMS_BY_DESIGN.get(design, ()):
            raise CorporateCalibrationDepthError("NULL_CONTROL_SPEC_MECHANISM_NOT_VALID_FOR_DESIGN")
        feature = CorporateFeatureId(feature_id)
        outcome = CorporateOutcomeId(outcome_id)
        if horizon_sessions < 1:
            raise CorporateCalibrationDepthError("NULL_CONTROL_SPEC_HORIZON_INVALID")
        if (secondary_context_dimension is None) != (secondary_condition_label is None):
            raise CorporateCalibrationDepthError(
                "NULL_CONTROL_SPEC_SECONDARY_FIELDS_MUST_BE_DECLARED_TOGETHER"
            )
        if design in (ResearchDesign.CONDITIONAL, ResearchDesign.INTERACTION) and (
            secondary_context_dimension is None
        ):
            raise CorporateCalibrationDepthError(
                "NULL_CONTROL_SPEC_CONDITIONAL_OR_INTERACTION_REQUIRES_SECONDARY_CONTEXT"
            )
        if seed < 0:
            raise CorporateCalibrationDepthError("NULL_CONTROL_SPEC_SEED_INVALID")
        if not preserved_properties:
            raise CorporateCalibrationDepthError("NULL_CONTROL_SPEC_PRESERVED_PROPERTIES_REQUIRED")
        if ordinal < 0:
            raise CorporateCalibrationDepthError("NULL_CONTROL_SPEC_ORDINAL_INVALID")
        created = _aware(created_at, "NULL_CONTROL_SPEC_CREATED_AT")
        preserved = tuple(sorted(set(preserved_properties)))
        payload = (
            CORPORATE_CALIBRATION_DEPTH_V1,
            calibration_id,
            matched_stratum,
            design,
            shape,
            template_id,
            mechanism_value,
            NULL_TARGETS_BY_MECHANISM[mechanism_value],
            feature,
            outcome,
            horizon_sessions,
            secondary_context_dimension,
            secondary_condition_label,
            seed,
            preserved,
            _text(destroyed_relationship, "NULL_CONTROL_SPEC_DESTROYED_RELATIONSHIP_INVALID"),
            _text(rationale, "NULL_CONTROL_SPEC_RATIONALE_INVALID"),
            ordinal,
        )
        return cls(
            spec_id=deterministic_id("corporate_calibration_null_control", payload),
            calibration_id=calibration_id,
            matched_stratum=matched_stratum,
            research_design=design,
            conditioning_shape=shape,
            template_id=template_id,
            mechanism=mechanism_value,
            null_target=payload[7],
            feature_id=feature,
            outcome_id=outcome,
            horizon_sessions=horizon_sessions,
            secondary_context_dimension=secondary_context_dimension,
            secondary_condition_label=secondary_condition_label,
            seed=seed,
            preserved_properties=preserved,
            destroyed_relationship=payload[15],
            rationale=payload[16],
            ordinal=ordinal,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class CorporateNullControlOutcome:
    """The retained, per-control governed-trial-execution bundle -- mirrors
    ``research_calibration.NullControlOutcome`` exactly, scoped to a
    ``CorporateNullControlSpec`` instead of a v1/v2 ``NullControlSpec``."""

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
    knowledge_status: str
    cohens_d: Decimal | None
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
        knowledge_status: str,
        cohens_d: Decimal | None,
        blockers: tuple[str, ...],
        created_at: datetime,
    ) -> CorporateNullControlOutcome:
        if not spec_id or not calibration_run_id or not governance_policy_id:
            raise CorporateCalibrationDepthError("NULL_CONTROL_OUTCOME_LINEAGE_REQUIRED")
        status = _text(knowledge_status, "NULL_CONTROL_OUTCOME_STATUS_INVALID", maximum=40)
        block_values = tuple(sorted(set(blockers)))
        created = _aware(created_at, "NULL_CONTROL_OUTCOME_CREATED_AT")
        payload = (
            CORPORATE_CALIBRATION_DEPTH_V1,
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
            cohens_d,
            block_values,
        )
        return cls(
            outcome_id=deterministic_id("corporate_calibration_null_outcome", payload),
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
            cohens_d=cohens_d,
            blockers=block_values,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Sections 15/16/17: non-destructive reassessment. One shape covers both a
# genuine hypothesis (section 15) and an original v3 negative control
# (section 16) -- distinguished by ``subject_kind``, exactly mirroring how
# ``research_calibration_v2.DesignClassification`` already distinguishes
# ``"HYPOTHESIS"``/``"NULL_CONTROL"`` rather than declaring two shapes.
# The four conditional/interaction genuine hypotheses additionally carry
# section 17's incremental-contribution calibration in the same row
# (``incremental_*`` fields, ``None`` for every other subject).
# ==========================================================================


class CorporateReassessmentSubjectKind(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    NEGATIVE_CONTROL = "NEGATIVE_CONTROL"


class IncrementalCalibrationVerdict(StrEnum):
    GENUINELY_INCREMENTAL = "GENUINELY_INCREMENTAL"
    REDUNDANT_CORPORATE_INFORMATION = "REDUNDANT_CORPORATE_INFORMATION"
    ORDINARY_NULL_VARIATION = "ORDINARY_NULL_VARIATION"
    INSUFFICIENT_CALIBRATION = "INSUFFICIENT_CALIBRATION"


@dataclass(frozen=True, slots=True)
class CorporateReassessment:
    reassessment_id: str
    calibration_id: str
    subject_id: str
    subject_kind: CorporateReassessmentSubjectKind
    matched_stratum: str
    original_conclusion_id: str
    original_conclusion_state: str
    original_evidence_grade: str
    original_matched_null_count: int
    new_matched_null_count: int
    new_matched_percentile: Decimal | None
    new_pooled_null_count: int
    new_evidence_grade: EvidenceGrade
    cohens_d: Decimal | None
    assessment_changed: bool
    follow_up_eligible: bool
    incremental_null_delta_count: int | None
    incremental_null_delta_percentile: Decimal | None
    incremental_verdict: IncrementalCalibrationVerdict | None
    rationale: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        calibration_id: str,
        subject_id: str,
        subject_kind: CorporateReassessmentSubjectKind | str,
        matched_stratum: str,
        original_conclusion_id: str,
        original_conclusion_state: str,
        original_evidence_grade: str,
        original_matched_null_count: int,
        new_matched_null_count: int,
        new_matched_percentile: Decimal | None,
        new_pooled_null_count: int,
        new_evidence_grade: EvidenceGrade | str,
        cohens_d: Decimal | None,
        rationale: str,
        created_at: datetime,
        incremental_null_delta_count: int | None = None,
        incremental_null_delta_percentile: Decimal | None = None,
        incremental_verdict: IncrementalCalibrationVerdict | str | None = None,
    ) -> CorporateReassessment:
        if not calibration_id or not subject_id or not original_conclusion_id:
            raise CorporateCalibrationDepthError("REASSESSMENT_LINEAGE_REQUIRED")
        kind = CorporateReassessmentSubjectKind(subject_kind)
        if not matched_stratum:
            raise CorporateCalibrationDepthError("REASSESSMENT_STRATUM_REQUIRED")
        if (
            new_matched_null_count < 0
            or original_matched_null_count < 0
            or new_pooled_null_count < 0
        ):
            raise CorporateCalibrationDepthError("REASSESSMENT_COUNTS_INVALID")
        grade = EvidenceGrade(new_evidence_grade)
        verdict = (
            IncrementalCalibrationVerdict(incremental_verdict)
            if incremental_verdict is not None
            else None
        )
        if (incremental_null_delta_count is None) != (verdict is None):
            raise CorporateCalibrationDepthError(
                "REASSESSMENT_INCREMENTAL_FIELDS_MUST_BE_DECLARED_TOGETHER"
            )
        changed = (
            original_evidence_grade != grade.value
            or new_matched_null_count != original_matched_null_count
        )
        sufficient = new_matched_null_count >= MINIMUM_MATCHED_NULL_COUNT
        eligible = sufficient and grade in (EvidenceGrade.SUGGESTIVE, EvidenceGrade.VALIDATED)
        created = _aware(created_at, "REASSESSMENT_CREATED_AT")
        payload = (
            CORPORATE_CALIBRATION_DEPTH_V1,
            calibration_id,
            subject_id,
            kind,
            matched_stratum,
            original_conclusion_id,
            _text(original_conclusion_state, "REASSESSMENT_ORIGINAL_STATE_INVALID", maximum=80),
            _text(original_evidence_grade, "REASSESSMENT_ORIGINAL_GRADE_INVALID", maximum=80),
            original_matched_null_count,
            new_matched_null_count,
            new_matched_percentile,
            new_pooled_null_count,
            grade,
            cohens_d,
            changed,
            eligible,
            incremental_null_delta_count,
            incremental_null_delta_percentile,
            verdict,
            _text(rationale, "REASSESSMENT_RATIONALE_INVALID", maximum=2_000),
        )
        return cls(
            reassessment_id=deterministic_id("corporate_calibration_reassessment", payload),
            calibration_id=calibration_id,
            subject_id=subject_id,
            subject_kind=kind,
            matched_stratum=matched_stratum,
            original_conclusion_id=original_conclusion_id,
            original_conclusion_state=payload[6],
            original_evidence_grade=payload[7],
            original_matched_null_count=original_matched_null_count,
            new_matched_null_count=new_matched_null_count,
            new_matched_percentile=new_matched_percentile,
            new_pooled_null_count=new_pooled_null_count,
            new_evidence_grade=grade,
            cohens_d=cohens_d,
            assessment_changed=changed,
            follow_up_eligible=eligible,
            incremental_null_delta_count=incremental_null_delta_count,
            incremental_null_delta_percentile=incremental_null_delta_percentile,
            incremental_verdict=verdict,
            rationale=payload[19],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 13: raw WEAK_SUPPORT diagnostic -- a pure, deterministic
# classifier over already-retained evidence, never a threshold change to
# WEAK_SUPPORT itself.
# ==========================================================================


class WeakSupportCause(StrEnum):
    ORDINARY_NULL_BEHAVIOR = "ORDINARY_NULL_BEHAVIOR"
    TEMPLATE_PERMISSIVENESS = "TEMPLATE_PERMISSIVENESS"
    SPARSE_SAMPLE = "SPARSE_SAMPLE"
    PANEL_DEPENDENCE = "PANEL_DEPENDENCE"
    ROBUSTNESS_OR_HOLDOUT_ORDERING = "ROBUSTNESS_OR_HOLDOUT_ORDERING"
    UNRESOLVED = "UNRESOLVED"


def classify_weak_support_cause(
    *,
    effective_sample: Decimal,
    minimum_effective_sample: Decimal,
    inference_available: bool,
    validation_beats_benchmark: bool | None,
    holdout_beats_benchmark: bool | None,
    robustness_passed: bool | None,
    control_weak_rate_for_stratum: Decimal | None,
) -> WeakSupportCause:
    """Section 13's deterministic rule, checked in a fixed order so a
    finding is never attributed to more than one cause: an inadequate or
    inference-suppressed panel sample is ``SPARSE_SAMPLE``/
    ``PANEL_DEPENDENCE`` first (the two most mechanical explanations);
    absent those, a validation/holdout direction disagreement or a failed
    robustness check is ``ROBUSTNESS_OR_HOLDOUT_ORDERING`` (the finding
    flipped somewhere in the governed pipeline); absent those too, a
    matched-null population that itself reaches ``WEAK_SUPPORT`` about as
    often is ``ORDINARY_NULL_BEHAVIOR``, while a materially *lower*
    matched-null ``WEAK_SUPPORT`` rate is ``TEMPLATE_PERMISSIVENESS``
    (this specific template's threshold looks unusually easy to clear
    relative to its own matched nulls). ``UNRESOLVED`` only when none of
    the above evidence is available at all."""

    if not inference_available:
        return WeakSupportCause.PANEL_DEPENDENCE
    if effective_sample < minimum_effective_sample:
        return WeakSupportCause.SPARSE_SAMPLE
    if robustness_passed is False:
        return WeakSupportCause.ROBUSTNESS_OR_HOLDOUT_ORDERING
    if (
        validation_beats_benchmark is not None
        and holdout_beats_benchmark is not None
        and validation_beats_benchmark != holdout_beats_benchmark
    ):
        return WeakSupportCause.ROBUSTNESS_OR_HOLDOUT_ORDERING
    if control_weak_rate_for_stratum is None:
        return WeakSupportCause.UNRESOLVED
    if control_weak_rate_for_stratum >= Decimal("0.15"):
        return WeakSupportCause.ORDINARY_NULL_BEHAVIOR
    return WeakSupportCause.TEMPLATE_PERMISSIVENESS


# ==========================================================================
# Sections 19/20: bounded, report-time-only bucketing helpers (pure
# functions, no persisted type -- consistent with this codebase's existing
# "never store a report that is exactly rebuildable from already-retained
# evidence" convention, restated explicitly by migration 0089 itself).
# ==========================================================================


class EffectiveSampleBucket(StrEnum):
    SPARSE = "SPARSE_LT_8"
    MODERATE = "MODERATE_8_TO_20"
    ADEQUATE = "ADEQUATE_GE_20"


def bucket_effective_sample(effective_sample: Decimal) -> EffectiveSampleBucket:
    if effective_sample < 8:
        return EffectiveSampleBucket.SPARSE
    if effective_sample < 20:
        return EffectiveSampleBucket.MODERATE
    return EffectiveSampleBucket.ADEQUATE


class TemporalRegion(StrEnum):
    EARLY = "EARLY"
    MIDDLE = "MIDDLE"
    RECENT = "RECENT"


def temporal_region_for_date(
    session_date: str, *, ordered_dates: Sequence[str]
) -> TemporalRegion | None:
    """Section 20's early/middle/recent tercile partition over the
    calibration's own retained date axis (``ordered_dates`` must already
    be sorted ascending) -- the corporate analogue of ``application.
    corporate_market_research.compute_stability``'s tercile split, reused
    at report time across the whole null battery rather than one sample."""

    if session_date not in ordered_dates:
        return None
    count = len(ordered_dates)
    if count < 3:
        return None
    third = max(1, count // 3)
    index = ordered_dates.index(session_date)
    if index < third:
        return TemporalRegion.EARLY
    if index < 2 * third:
        return TemporalRegion.MIDDLE
    return TemporalRegion.RECENT


# ==========================================================================
# Section 22: the retained corporate calibration quality report.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateCalibrationReport:
    """One retained answer to "when Apex calls a corporate/fundamental
    result interesting, how often does the same design produce equally
    strong evidence when the corporate relationship is deliberately
    broken?" (section 22). References every breakdown by id/label rather
    than embedding a full nested pipeline result, matching this codebase's
    existing preference for small, composable records; the counts and
    rates below are exactly the report body section 22 requires."""

    report_id: str
    calibration_id: str
    calibration_run_id: str
    strata_evaluated: int
    controls_generated: int
    controls_per_stratum: int
    raw_false_positive_rate: Decimal
    corrected_false_positive_rate: Decimal
    false_weak_rate: Decimal
    false_suggestive_rate: Decimal
    false_validated_rate: Decimal
    false_promotion_rate: Decimal
    design_breakdown_labels: tuple[str, ...]
    stability_slice_labels: tuple[str, ...]
    sector_concentration_note: str
    filing_event_sparsity_note: str
    weak_support_diagnostic_note: str
    genuine_reassessment_count: int
    genuine_assessment_changed_count: int
    control_reassessment_count: int
    control_now_validated_or_strong_count: int
    incremental_calibration_count: int
    policy_v2_justified: bool
    policy_v2_id: str | None
    policy_recommendation_rationale: str
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
        strata_evaluated: int,
        controls_generated: int,
        controls_per_stratum: int,
        raw_false_positive_rate: Decimal,
        corrected_false_positive_rate: Decimal,
        false_weak_rate: Decimal,
        false_suggestive_rate: Decimal,
        false_validated_rate: Decimal,
        false_promotion_rate: Decimal,
        design_breakdown_labels: tuple[str, ...],
        stability_slice_labels: tuple[str, ...],
        sector_concentration_note: str,
        filing_event_sparsity_note: str,
        weak_support_diagnostic_note: str,
        genuine_reassessment_count: int,
        genuine_assessment_changed_count: int,
        control_reassessment_count: int,
        control_now_validated_or_strong_count: int,
        incremental_calibration_count: int,
        policy_v2_justified: bool,
        policy_v2_id: str | None,
        policy_recommendation_rationale: str,
        limitations: tuple[str, ...],
        created_at: datetime,
    ) -> CorporateCalibrationReport:
        if not calibration_id or not calibration_run_id:
            raise CorporateCalibrationDepthError("CALIBRATION_REPORT_LINEAGE_REQUIRED")
        if strata_evaluated < 0 or controls_generated < 0:
            raise CorporateCalibrationDepthError("CALIBRATION_REPORT_COUNTS_INVALID")
        if policy_v2_justified and not policy_v2_id:
            raise CorporateCalibrationDepthError("CALIBRATION_REPORT_POLICY_V2_ID_REQUIRED")
        if not policy_v2_justified and policy_v2_id:
            raise CorporateCalibrationDepthError("CALIBRATION_REPORT_POLICY_V2_ID_MUST_BE_ABSENT")
        created = _aware(created_at, "CALIBRATION_REPORT_CREATED_AT")
        payload = (
            CORPORATE_CALIBRATION_DEPTH_V1,
            calibration_id,
            calibration_run_id,
            strata_evaluated,
            controls_generated,
            controls_per_stratum,
            raw_false_positive_rate,
            corrected_false_positive_rate,
            false_weak_rate,
            false_suggestive_rate,
            false_validated_rate,
            false_promotion_rate,
            tuple(sorted(set(design_breakdown_labels))),
            tuple(sorted(set(stability_slice_labels))),
            _text(
                sector_concentration_note, "CALIBRATION_REPORT_SECTOR_NOTE_INVALID", maximum=2_000
            ),
            _text(
                filing_event_sparsity_note, "CALIBRATION_REPORT_FILING_NOTE_INVALID", maximum=2_000
            ),
            _text(
                weak_support_diagnostic_note, "CALIBRATION_REPORT_WEAK_NOTE_INVALID", maximum=2_000
            ),
            genuine_reassessment_count,
            genuine_assessment_changed_count,
            control_reassessment_count,
            control_now_validated_or_strong_count,
            incremental_calibration_count,
            policy_v2_justified,
            policy_v2_id,
            _text(
                policy_recommendation_rationale,
                "CALIBRATION_REPORT_POLICY_RATIONALE_INVALID",
                maximum=4_000,
            ),
            tuple(sorted(set(limitations))),
        )
        return cls(
            report_id=deterministic_id("corporate_calibration_report", payload),
            calibration_id=calibration_id,
            calibration_run_id=calibration_run_id,
            strata_evaluated=strata_evaluated,
            controls_generated=controls_generated,
            controls_per_stratum=controls_per_stratum,
            raw_false_positive_rate=raw_false_positive_rate,
            corrected_false_positive_rate=corrected_false_positive_rate,
            false_weak_rate=false_weak_rate,
            false_suggestive_rate=false_suggestive_rate,
            false_validated_rate=false_validated_rate,
            false_promotion_rate=false_promotion_rate,
            design_breakdown_labels=payload[12],
            stability_slice_labels=payload[13],
            sector_concentration_note=payload[14],
            filing_event_sparsity_note=payload[15],
            weak_support_diagnostic_note=payload[16],
            genuine_reassessment_count=genuine_reassessment_count,
            genuine_assessment_changed_count=genuine_assessment_changed_count,
            control_reassessment_count=control_reassessment_count,
            control_now_validated_or_strong_count=control_now_validated_or_strong_count,
            incremental_calibration_count=incremental_calibration_count,
            policy_v2_justified=policy_v2_justified,
            policy_v2_id=policy_v2_id,
            policy_recommendation_rationale=payload[24],
            limitations=payload[25],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def corporate_calibration_depth_warnings() -> tuple[str, ...]:
    return (
        CORPORATE_CALIBRATION_DEPTH_WARNING,
        V3_EVIDENCE_PRESERVATION_NOTE,
        FOLLOW_UP_EXECUTION_PROHIBITION_NOTE,
    )
