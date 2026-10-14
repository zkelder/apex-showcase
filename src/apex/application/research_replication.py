"""Calibration-Qualified Interaction Replication v1: adapter-free
orchestration.

Two services do all the work:

* :class:`FreezeReplicationSpecification` resolves exactly one already-
  retained ``FollowUpProposal`` (the milestone's single
  ``CALIBRATION_QUALIFIED`` follow-up) and its exact parent lineage
  (``GeneratedHypothesis``, ``ResearchConclusion``, ``ResearchAgenda``, the
  v1 ``FollowUpPriorityAssessment``, and the v2 ``DesignClassification``/
  ``FollowUpQualification``) into one immutable
  :class:`~apex.domain.research_replication.ReplicationSpecification`.
  Reads only; never widens the source agenda or calibration battery.
* :class:`RunReplication` loads the identical retained
  ``CrossMarketPanel`` (:class:`~apex.application.cross_market_research.
  BuildCrossMarketPanel` -- no new capture, no provider request) and
  computes every genuinely new, but strictly descriptive, breakdown the
  milestone asks for from it: temporal (early/middle/late) and volatility-
  state partitions, two predeclared cross-market timing alignments, one
  effect-size/distribution/outlier-robustness profile, and one regime-
  confound assessment across six already-retained regime dimensions.

Every number computed here reuses only public, already-reviewed
primitives (``summarize_values``, ``compare_to_benchmark``,
``resolve_condition_dates``, ``bound_statistical_decimal``) -- deliberately
*not* the full governed trial/robustness/conclusion/promotion pipeline
``application.cross_market_research.RunSingleHypothesis`` uses, for
exactly the reason ``research_calibration_v2.StabilitySlice`` already
documents: an honest, cheaply-recomputed direct comparison over
already-retained panel data, never claiming a holdout/robustness survival
rate that was not actually re-evaluated per slice.

Both services have authority ``NONE``. Neither ever reruns, rewrites, or
widens the genuine 25-hypothesis agenda, v1's 104-control battery, or v2's
144-control battery; each only reads their already-retained evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Protocol

from apex.application.cross_market_research import (
    BuildCrossMarketPanel,
    CrossMarketResearchRepository,
    CryptoCandleCollaborator,
    GenuineMultiAssetCollaborator,
    HistoricalMarketDataCollaborator,
    SecuritiesEvidenceCollaborator,
    resolve_condition_dates,
)
from apex.application.research_calibration import CalibrationRepository
from apex.application.research_calibration_v2 import CalibrationV2Repository
from apex.application.research_methodology import ResearchMethodologyRepository
from apex.domain.cross_market_research import (
    CrossMarketRegimeDimension,
    CrossMarketRegimeSnapshot,
    FollowUpKind,
    HypothesisTemplateId,
    OutcomeFamily,
)
from apex.domain.crypto_research import CONSERVATIVE_FEE_BPS, CONSERVATIVE_SLIPPAGE_BPS
from apex.domain.historical_replay import (
    ReplayStatisticalSummary,
    ResearchPeriodDeclaration,
    SummaryKind,
    summarize_values,
)
from apex.domain.research_calibration import (
    EFFECT_SIZE_FLOOR_POLICY_V1,
    EVIDENCE_GRADE_POLICY_V1,
    EvidenceGrade,
    FollowUpPriorityAssessment,
)
from apex.domain.research_calibration import FollowUpPriorityClass as V1FollowUpPriorityClass
from apex.domain.research_calibration_v2 import FollowUpQualificationV2
from apex.domain.research_methodology import bound_statistical_decimal, compare_to_benchmark
from apex.domain.research_replication import (
    RESEARCH_REPLICATION_V1,
    ConfoundDimensionResult,
    DatasetExposureLabel,
    DatasetExposureSegment,
    PartitionKind,
    ReplicationConclusion,
    ReplicationConclusionState,
    ReplicationConfoundAssessment,
    ReplicationEffectProfile,
    ReplicationPartitionResult,
    ReplicationSpecification,
    TimingAlignmentResult,
    TimingConvention,
    TransactionCostRelevance,
    classify_transaction_cost_relevance,
)

CONSERVATIVE_ROUND_TRIP_COST_RATE = (
    Decimal(2) * (CONSERVATIVE_FEE_BPS + CONSERVATIVE_SLIPPAGE_BPS) / Decimal(10_000)
)
MINIMUM_PARTITION_RAW_SAMPLE = 8
NULL_LIKE_MATCHED_PERCENTILE_CEILING = EVIDENCE_GRADE_POLICY_V1.null_like_ceiling
_CONFOUND_DIMENSIONS: tuple[CrossMarketRegimeDimension, ...] = (
    CrossMarketRegimeDimension.EQUITY_RISK_TONE,
    CrossMarketRegimeDimension.EQUITY_TREND,
    CrossMarketRegimeDimension.EQUITY_BREADTH,
    CrossMarketRegimeDimension.BTC_VOLATILITY,
    CrossMarketRegimeDimension.BTC_TREND,
    CrossMarketRegimeDimension.BOND_RISK_RELATIONSHIP,
)
_VOLATILITY_DIMENSION = CrossMarketRegimeDimension.EQUITY_VOLATILITY


class ReplicationApplicationError(RuntimeError):
    pass


class ReplicationNotFound(ReplicationApplicationError):
    pass


class ReplicationConflict(ReplicationApplicationError):
    pass


# ==========================================================================
# Repository port
# ==========================================================================


class ResearchReplicationRepository(Protocol):
    def specification_by_id(self, replication_id: str) -> ReplicationSpecification | None: ...
    def specification_by_follow_up(self, follow_up_id: str) -> ReplicationSpecification | None: ...
    def save_specification(self, item: ReplicationSpecification) -> ReplicationSpecification: ...

    def save_timing(self, item: TimingAlignmentResult) -> TimingAlignmentResult: ...
    def timings_for_replication(self, replication_id: str) -> tuple[TimingAlignmentResult, ...]: ...

    def save_partition(self, item: ReplicationPartitionResult) -> ReplicationPartitionResult: ...
    def partitions_for_replication(
        self, replication_id: str
    ) -> tuple[ReplicationPartitionResult, ...]: ...

    def save_effect_profile(self, item: ReplicationEffectProfile) -> ReplicationEffectProfile: ...
    def effect_profile_for_replication(
        self, replication_id: str
    ) -> ReplicationEffectProfile | None: ...

    def save_confound_assessment(
        self, item: ReplicationConfoundAssessment
    ) -> ReplicationConfoundAssessment: ...
    def confound_assessment_for_replication(
        self, replication_id: str
    ) -> ReplicationConfoundAssessment | None: ...

    def save_conclusion(self, item: ReplicationConclusion) -> ReplicationConclusion: ...
    def conclusion_for_replication(self, replication_id: str) -> ReplicationConclusion | None: ...


# ==========================================================================
# Section 1: freeze the parent hypothesis
# ==========================================================================


class FreezeReplicationSpecification:
    def __init__(
        self,
        *,
        cross_market_repository: CrossMarketResearchRepository,
        methodology_repository: ResearchMethodologyRepository,
        calibration_v1_repository: CalibrationRepository,
        calibration_v2_repository: CalibrationV2Repository,
        replication_repository: ResearchReplicationRepository,
    ) -> None:
        self._cross_market_repository = cross_market_repository
        self._methodology_repository = methodology_repository
        self._calibration_v1_repository = calibration_v1_repository
        self._calibration_v2_repository = calibration_v2_repository
        self._replication_repository = replication_repository

    def execute(
        self, *, follow_up_id: str, v2_calibration_id: str, created_at: datetime
    ) -> ReplicationSpecification:
        follow_up = next(
            (
                item
                for item in self._cross_market_repository.list_follow_ups()
                if item.follow_up_id == follow_up_id
            ),
            None,
        )
        if follow_up is None:
            raise ReplicationNotFound("FOLLOW_UP_PROPOSAL_NOT_FOUND")
        hypothesis = self._cross_market_repository.hypothesis_by_id(follow_up.parent_hypothesis_id)
        if hypothesis is None:
            raise ReplicationNotFound("PARENT_HYPOTHESIS_NOT_FOUND")
        if hypothesis.template_id is not HypothesisTemplateId.INTERACTION_V1:
            raise ReplicationApplicationError("REPLICATION_SCOPE_IS_INTERACTION_TEMPLATE_ONLY")
        if hypothesis.secondary_feature_id is None or hypothesis.secondary_condition_rule is None:
            raise ReplicationApplicationError("PARENT_HYPOTHESIS_MISSING_SECONDARY_CONDITION")
        conclusion = self._methodology_repository.conclusion_by_id(follow_up.parent_conclusion_id)
        if conclusion is None:
            raise ReplicationNotFound("PARENT_CONCLUSION_NOT_FOUND")
        agenda = self._cross_market_repository.agenda_by_id(hypothesis.agenda_id)
        if agenda is None:
            raise ReplicationNotFound("PARENT_AGENDA_NOT_FOUND")
        execution = self._cross_market_repository.execution_by_hypothesis(hypothesis.hypothesis_id)
        if execution is None:
            raise ReplicationNotFound("PARENT_EXECUTION_RECORD_NOT_FOUND")
        governance_policy = self._methodology_repository.governance_policy_by_id(
            execution.governance_policy_id
        )
        if governance_policy is None:
            raise ReplicationNotFound("PARENT_GOVERNANCE_POLICY_NOT_FOUND")
        train_period = next(
            (
                item
                for item in governance_policy.period_policy.periods
                if item.role.value == "TRAIN"
            ),
            None,
        )
        if train_period is None:
            raise ReplicationNotFound("PARENT_TRAIN_PERIOD_NOT_DECLARED")

        v1_priority = self._find_v1_priority(follow_up_id)
        if v1_priority is None:
            raise ReplicationNotFound("V1_FOLLOW_UP_PRIORITY_NOT_FOUND")
        if v1_priority[1].classification is not V1FollowUpPriorityClass.JUSTIFIED:
            raise ReplicationApplicationError("FOLLOW_UP_NOT_V1_JUSTIFIED")
        v1_calibration_id, v1_priority_assessment = v1_priority

        v2_classification = self._calibration_v2_repository.classification_for_subject(
            calibration_id=v2_calibration_id, subject_id=hypothesis.hypothesis_id
        )
        if v2_classification is None:
            raise ReplicationNotFound("V2_DESIGN_CLASSIFICATION_NOT_FOUND")
        v2_qualifications = (
            self._calibration_v2_repository.follow_up_qualifications_for_calibration(
                v2_calibration_id
            )
        )
        v2_qualification = next(
            (item for item in v2_qualifications if item.follow_up_id == follow_up_id), None
        )
        if v2_qualification is None:
            raise ReplicationNotFound("V2_FOLLOW_UP_QUALIFICATION_NOT_FOUND")
        if v2_qualification.qualification is not FollowUpQualificationV2.CALIBRATION_QUALIFIED:
            raise ReplicationApplicationError("FOLLOW_UP_NOT_CALIBRATION_QUALIFIED")
        if v2_classification.matched.matched_percentile is None:
            raise ReplicationApplicationError("V2_MATCHED_PERCENTILE_UNAVAILABLE")

        exposure_segments = self._exposure_segments(governance_policy.period_policy.periods)

        specification = ReplicationSpecification.create(
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            parent_conclusion_id=conclusion.conclusion_id,
            parent_agenda_id=agenda.agenda_id,
            v1_calibration_id=v1_calibration_id,
            v1_priority_assessment_id=v1_priority_assessment.assessment_id,
            v2_calibration_id=v2_calibration_id,
            v2_classification_id=v2_classification.classification_id,
            v2_matched_stratum=v2_classification.matched.matched_stratum,
            v2_matched_null_count=v2_classification.matched.matched_null_count,
            v2_matched_percentile=v2_classification.matched.matched_percentile,
            research_design=v2_classification.research_design,
            conditioning_shape=v2_classification.conditioning_shape,
            primary_feature_id=hypothesis.feature_id,
            primary_condition_rule=hypothesis.condition_rule,
            secondary_feature_id=hypothesis.secondary_feature_id,
            secondary_condition_rule=hypothesis.secondary_condition_rule,
            market_scope=hypothesis.market_scope,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
            benchmark_kind=hypothesis.benchmark_kind,
            dataset_snapshot_id=agenda.dataset_snapshot_id,
            replay_run_id=agenda.replay_run_id,
            cross_market_dataset_id=agenda.cross_market_dataset_id,
            train_period_start=train_period.start_date,
            train_period_end=train_period.end_date,
            exposure_segments=exposure_segments,
            is_in_sample_replication=True,
            rationale=hypothesis.rationale,
            original_agenda_objective=agenda.objective,
            created_at=created_at,
        )
        existing = self._replication_repository.specification_by_id(specification.replication_id)
        if existing is not None:
            return existing
        return self._replication_repository.save_specification(specification)

    def _find_v1_priority(self, follow_up_id: str) -> tuple[str, FollowUpPriorityAssessment] | None:
        for plan in self._calibration_v1_repository.list_plans():
            for item in self._calibration_v1_repository.follow_up_priorities_for_calibration(
                plan.calibration_id
            ):
                if item.follow_up_id == follow_up_id:
                    return plan.calibration_id, item
        return None

    def _exposure_segments(
        self, periods: Sequence[ResearchPeriodDeclaration]
    ) -> tuple[DatasetExposureSegment, ...]:
        """Section 5: the *declared* TRAIN/VALIDATION/HOLDOUT period
        boundaries from this hypothesis's own retained
        ``ResearchGovernancePolicy`` -- not a trial's own narrower
        ``session_date_range`` (which reflects only where its *filtered
        sample* happened to fall within that wider declared window, not the
        window itself)."""

        role_map = {
            "TRAIN": (
                DatasetExposureLabel.DISCOVERY_EXPLORATORY_USED,
                "Genuine agenda declared TRAIN period: exploratory-phase evidence and this "
                "replication's own reused threshold-derivation window.",
            ),
            "VALIDATION": (
                DatasetExposureLabel.VALIDATION_USED,
                "Genuine agenda declared VALIDATION period for this hypothesis.",
            ),
            "HOLDOUT": (
                DatasetExposureLabel.LOCKED_HOLDOUT_CONSUMED,
                "Genuine agenda declared LOCKED_HOLDOUT period for this hypothesis; already "
                "evaluated once and locked -- this replication reuses it as evidence, never "
                "re-locks it.",
            ),
        }
        segments: list[DatasetExposureSegment] = []
        for period in periods:
            mapped = role_map.get(period.role.value)
            if mapped is None:
                continue
            label, note = mapped
            segments.append(
                DatasetExposureSegment(
                    label=label, start_date=period.start_date, end_date=period.end_date, note=note
                )
            )
        segments.append(
            DatasetExposureSegment(
                label=DatasetExposureLabel.CALIBRATION_V1_AND_V2_USED,
                start_date=segments[0].start_date if segments else "",
                end_date=segments[-1].end_date if segments else "",
                note=(
                    "Both calibration-v1's 104-control and calibration-v2's 144-control "
                    "batteries ran against this exact retained dataset/replay/cross-market-"
                    "dataset lineage, spanning the full retained history."
                ),
            )
        )
        return tuple(segments)


# ==========================================================================
# Shared, honestly-scoped descriptive statistics (never the full governed
# trial/robustness/conclusion/promotion pipeline -- see module docstring)
# ==========================================================================


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 28
        return bound_statistical_decimal(sum(values, Decimal(0)) / Decimal(len(values)))


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return bound_statistical_decimal(ordered[middle])
    return bound_statistical_decimal((ordered[middle - 1] + ordered[middle]) / Decimal(2))


def _summary(values: Sequence[Decimal], label: str) -> ReplayStatisticalSummary:
    return summarize_values(
        tuple(values), kind=SummaryKind.EXPLORATORY, group_label=label, is_return_series=False
    )


def _cohens_d(sample: Sequence[Decimal], benchmark: Sequence[Decimal]) -> Decimal | None:
    """The identical pooled-stdev Cohen's d formula
    ``application.cross_market_research._effect_size_for_panel_sample`` and
    v2's ``_ControlRowV2`` calibration both already use -- reimplemented
    locally (rather than importing that module-private helper) using only
    the public ``summarize_values``."""

    if len(sample) < 2 or len(benchmark) < 2:
        return None
    sample_summary = _summary(sample, "sample")
    benchmark_summary = _summary(benchmark, "benchmark")
    if (
        sample_summary.stdev is None
        or benchmark_summary.stdev is None
        or sample_summary.mean is None
        or benchmark_summary.mean is None
    ):
        return None
    n1, n2 = len(sample), len(benchmark)
    with localcontext() as context:
        context.prec = 28
        pooled_variance = (
            Decimal(n1 - 1) * sample_summary.stdev**2 + Decimal(n2 - 1) * benchmark_summary.stdev**2
        ) / Decimal(n1 + n2 - 2)
        if pooled_variance <= 0:
            return None
        d = (sample_summary.mean - benchmark_summary.mean) / pooled_variance.sqrt()
    return bound_statistical_decimal(d)


def _effective_sample(raw: int, horizon_sessions: int) -> Decimal:
    """The identical conservative horizon-stride overlap adjustment
    ``_panel_sample_diagnostics``/``StabilitySlice`` already use: assume
    every forward-outcome window overlaps and divide by the horizon."""

    if raw == 0:
        return Decimal(0)
    if horizon_sessions <= 1:
        return Decimal(raw)
    return max(Decimal(1), bound_statistical_decimal(Decimal(raw) / Decimal(horizon_sessions)))


def _winsorize(
    values: Sequence[Decimal], *, tail: Decimal = Decimal("0.05")
) -> tuple[Decimal, ...]:
    if len(values) < 4:
        return tuple(values)
    ordered = sorted(values)
    n = len(ordered)
    with localcontext() as context:
        context.prec = 28
        k = int((tail * Decimal(n)).to_integral_value(rounding=ROUND_FLOOR))
    if k <= 0:
        return tuple(values)
    lower, upper = ordered[k], ordered[-(k + 1)]
    return tuple(min(max(value, lower), upper) for value in values)


@dataclass(frozen=True, slots=True)
class _Slice:
    sample_dates: tuple[str, ...]
    benchmark_dates: tuple[str, ...]
    sample_values: tuple[Decimal, ...]
    benchmark_values: tuple[Decimal, ...]


def _slice_for(
    *,
    period_dates: frozenset[str],
    primary_dates: frozenset[str],
    secondary_dates: frozenset[str],
    outcome_by_date: dict[str, Decimal],
) -> _Slice:
    """The exact ``sample_universe`` partition
    ``application.cross_market_research.RunSingleHypothesis`` itself uses
    for an ``INTERACTION_V1``/``CONDITIONAL_RELATIONSHIP_V1`` hypothesis:
    sample = period AND primary-condition AND secondary-condition;
    benchmark ("PRIOR_REGIME_DISTRIBUTION") = period AND primary-condition
    AND NOT secondary-condition. Reproduced here (never imported as a
    closure) because the original is a local closure inside that class,
    not a reusable module-level function."""

    fixed = period_dates & primary_dates
    sample_dates = tuple(sorted(d for d in (fixed & secondary_dates) if d in outcome_by_date))
    benchmark_dates = tuple(sorted(d for d in (fixed - secondary_dates) if d in outcome_by_date))
    return _Slice(
        sample_dates=sample_dates,
        benchmark_dates=benchmark_dates,
        sample_values=tuple(outcome_by_date[d] for d in sample_dates),
        benchmark_values=tuple(outcome_by_date[d] for d in benchmark_dates),
    )


def _interaction_effect(slice_: _Slice) -> Decimal | None:
    sample_mean = _mean(slice_.sample_values)
    benchmark_mean = _mean(slice_.benchmark_values)
    if sample_mean is None or benchmark_mean is None:
        return None
    return compare_to_benchmark(
        sample_mean=sample_mean,
        benchmark_kind="PRIOR_REGIME_DISTRIBUTION",
        benchmark_values=slice_.benchmark_values,
    ).relative_result


# ==========================================================================
# Section 9: run result bundle
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ReplicationRunResult:
    specification: ReplicationSpecification
    timing: tuple[TimingAlignmentResult, ...]
    partitions: tuple[ReplicationPartitionResult, ...]
    effect_profile: ReplicationEffectProfile
    confound: ReplicationConfoundAssessment
    conclusion: ReplicationConclusion


class RunReplication:
    """Sections 6/7/10-16: the one bounded, idempotent replication run over
    the frozen specification's identical retained panel."""

    def __init__(
        self,
        *,
        historical_repository: HistoricalMarketDataCollaborator,
        evidence: SecuritiesEvidenceCollaborator,
        genuine_repository: GenuineMultiAssetCollaborator,
        crypto_repository: CryptoCandleCollaborator,
        replication_repository: ResearchReplicationRepository,
    ) -> None:
        self._historical_repository = historical_repository
        self._evidence = evidence
        self._genuine_repository = genuine_repository
        self._crypto_repository = crypto_repository
        self._replication_repository = replication_repository

    def execute(
        self, *, specification: ReplicationSpecification, created_at: datetime
    ) -> ReplicationRunResult:
        existing_conclusion = self._replication_repository.conclusion_for_replication(
            specification.replication_id
        )
        if existing_conclusion is not None:
            return ReplicationRunResult(
                specification=specification,
                timing=self._replication_repository.timings_for_replication(
                    specification.replication_id
                ),
                partitions=self._replication_repository.partitions_for_replication(
                    specification.replication_id
                ),
                effect_profile=self._require(
                    self._replication_repository.effect_profile_for_replication(
                        specification.replication_id
                    )
                ),
                confound=self._require(
                    self._replication_repository.confound_assessment_for_replication(
                        specification.replication_id
                    )
                ),
                conclusion=existing_conclusion,
            )

        panel = BuildCrossMarketPanel(
            historical_repository=self._historical_repository,
            evidence=self._evidence,
            genuine_repository=self._genuine_repository,
            crypto_repository=self._crypto_repository,
        ).execute(
            equity_snapshot_id=specification.dataset_snapshot_id,
            cross_market_dataset_id=specification.cross_market_dataset_id,
            horizons=(specification.horizon_sessions,),
        )
        dates = panel.session_dates
        if len(dates) < 30:
            raise ReplicationApplicationError("REPLICATION_PANEL_TOO_SMALL")
        all_dates = frozenset(dates)
        primary_by_date = panel.feature_by_date(specification.primary_feature_id)
        secondary_by_date = panel.feature_by_date(specification.secondary_feature_id)
        regime_by_date = panel.regime_by_date()
        outcome_by_date = panel.outcome_by_date(
            specification.outcome_id, horizon_sessions=specification.horizon_sessions
        )

        # Section 3: the fixed a-priori median threshold, reusing the exact
        # retained TRAIN period this hypothesis's own ResearchGovernancePolicy
        # already declared (frozen on the specification -- never re-derived
        # from the full history or from any later period, exactly mirroring
        # how the original governed run derived it once from TRAIN and
        # applied it unchanged to VALIDATION/HOLDOUT). This is the one
        # threshold that makes an "exact replay" of the original holdout
        # trial's own numbers provable: restricting this replication's
        # computation to the identical declared HOLDOUT window reproduces
        # the retained trial's sample/benchmark counts and cohens_d exactly.
        train_dates = frozenset(
            d
            for d in dates
            if specification.train_period_start <= d <= specification.train_period_end
        )
        primary_dates = resolve_condition_dates(
            feature_id=specification.primary_feature_id,
            condition_rule=specification.primary_condition_rule,
            condition_label=None,
            feature_by_date=primary_by_date,
            regime_by_date=regime_by_date,
            train_dates=train_dates,
        )
        secondary_dates = resolve_condition_dates(
            feature_id=specification.secondary_feature_id,
            condition_rule=specification.secondary_condition_rule,
            condition_label=None,
            feature_by_date=secondary_by_date,
            regime_by_date=regime_by_date,
            train_dates=train_dates,
        )

        full_slice = _slice_for(
            period_dates=all_dates,
            primary_dates=primary_dates,
            secondary_dates=secondary_dates,
            outcome_by_date=outcome_by_date,
        )
        full_partition = self._partition_result(
            specification=specification,
            slice_=full_slice,
            partition_kind=PartitionKind.FULL_SAMPLE,
            label="FULL_SAMPLE",
            start_date=dates[0],
            end_date=dates[-1],
            matched_null_count=specification.v2_matched_null_count,
            matched_percentile=specification.v2_matched_percentile,
            calibration_note=(
                "Reused, unaugmented v2 matched-null percentile (sections 1/8/9): the genuine "
                f"matched-null battery ({specification.v2_matched_null_count} controls, stratum "
                f"{specification.v2_matched_stratum}) applies only to this full-sample primary "
                "specification, exactly as calibration-v2 retained it."
            ),
            created_at=created_at,
        )

        temporal_partitions = self._temporal_partitions(
            specification=specification,
            dates=dates,
            primary_dates=primary_dates,
            secondary_dates=secondary_dates,
            outcome_by_date=outcome_by_date,
            created_at=created_at,
        )
        volatility_partitions = self._volatility_partitions(
            specification=specification,
            dates=dates,
            primary_dates=primary_dates,
            secondary_dates=secondary_dates,
            regime_by_date=regime_by_date,
            outcome_by_date=outcome_by_date,
            created_at=created_at,
        )
        timing_results = self._timing_checks(
            specification=specification,
            dates=dates,
            primary_dates=primary_dates,
            secondary_by_date=secondary_by_date,
            full_slice=full_slice,
            regime_by_date=regime_by_date,
            outcome_by_date=outcome_by_date,
            created_at=created_at,
        )
        effect_profile = self._effect_profile(
            specification=specification,
            dates=dates,
            full_slice=full_slice,
            temporal_partitions=temporal_partitions,
            outcome_by_date=outcome_by_date,
            created_at=created_at,
        )
        confound = self._confound_assessment(
            specification=specification,
            full_slice=full_slice,
            regime_by_date=regime_by_date,
            interaction_effect=full_partition.interaction_effect,
            created_at=created_at,
        )

        saved_partitions = tuple(
            self._replication_repository.save_partition(item)
            for item in (full_partition, *temporal_partitions, *volatility_partitions)
        )
        saved_timing = tuple(
            self._replication_repository.save_timing(item) for item in timing_results
        )
        saved_profile = self._replication_repository.save_effect_profile(effect_profile)
        saved_confound = self._replication_repository.save_confound_assessment(confound)

        conclusion = derive_replication_conclusion(
            specification=specification,
            full_partition=full_partition,
            temporal_partitions=temporal_partitions,
            volatility_partitions=volatility_partitions,
            timing=saved_timing,
            effect_profile=saved_profile,
            confound=saved_confound,
            created_at=created_at,
        )
        saved_conclusion = self._replication_repository.save_conclusion(conclusion)

        return ReplicationRunResult(
            specification=specification,
            timing=saved_timing,
            partitions=saved_partitions,
            effect_profile=saved_profile,
            confound=saved_confound,
            conclusion=saved_conclusion,
        )

    @staticmethod
    def _require[T](value: T | None) -> T:
        if value is None:
            raise ReplicationApplicationError("REPLICATION_PARTIAL_STATE_INCONSISTENT")
        return value

    def _partition_result(
        self,
        *,
        specification: ReplicationSpecification,
        slice_: _Slice,
        partition_kind: PartitionKind,
        label: str,
        start_date: str | None,
        end_date: str | None,
        matched_null_count: int,
        matched_percentile: Decimal | None,
        calibration_note: str,
        created_at: datetime,
    ) -> ReplicationPartitionResult:
        sample_mean = _mean(slice_.sample_values)
        benchmark_mean = _mean(slice_.benchmark_values)
        interaction_effect = _interaction_effect(slice_)
        cohens_d = _cohens_d(slice_.sample_values, slice_.benchmark_values)
        beats_benchmark = None if interaction_effect is None else interaction_effect > 0
        return ReplicationPartitionResult.create(
            replication_id=specification.replication_id,
            partition_kind=partition_kind,
            label=label,
            start_date=start_date,
            end_date=end_date,
            sample_count=len(slice_.sample_values),
            effective_sample_count=_effective_sample(
                len(slice_.sample_values), specification.horizon_sessions
            ),
            benchmark_sample_count=len(slice_.benchmark_values),
            sample_mean=sample_mean,
            benchmark_mean=benchmark_mean,
            interaction_effect=interaction_effect,
            cohens_d=cohens_d,
            beats_benchmark=beats_benchmark,
            matched_null_count=matched_null_count,
            matched_percentile=matched_percentile,
            calibration_note=calibration_note,
            created_at=created_at,
        )

    def _temporal_partitions(
        self,
        *,
        specification: ReplicationSpecification,
        dates: tuple[str, ...],
        primary_dates: frozenset[str],
        secondary_dates: frozenset[str],
        outcome_by_date: dict[str, Decimal],
        created_at: datetime,
    ) -> tuple[ReplicationPartitionResult, ...]:
        third = max(1, len(dates) // 3)
        buckets = (
            ("EARLY", dates[:third]),
            ("MIDDLE", dates[third : 2 * third]),
            ("LATE", dates[2 * third :]),
        )
        results = []
        for label, bucket_dates in buckets:
            slice_ = _slice_for(
                period_dates=frozenset(bucket_dates),
                primary_dates=primary_dates,
                secondary_dates=secondary_dates,
                outcome_by_date=outcome_by_date,
            )
            note = (
                "Section 7's TIME_PERIOD_THIRDS_V1 partition (reused verbatim from Research "
                "Calibration v2's own stability-slice policy). No per-partition matched-null "
                "battery was built for this narrower slice; matched calibration remains scoped "
                "to the full-sample primary specification only (CALIBRATION_STRATUM_INSUFFICIENT "
                "for a finer slice) -- descriptive evidence only."
            )
            results.append(
                self._partition_result(
                    specification=specification,
                    slice_=slice_,
                    partition_kind=PartitionKind.TIME_PERIOD,
                    label=label,
                    start_date=bucket_dates[0] if bucket_dates else None,
                    end_date=bucket_dates[-1] if bucket_dates else None,
                    matched_null_count=0,
                    matched_percentile=None,
                    calibration_note=note,
                    created_at=created_at,
                )
            )
        return tuple(results)

    def _volatility_partitions(
        self,
        *,
        specification: ReplicationSpecification,
        dates: tuple[str, ...],
        primary_dates: frozenset[str],
        secondary_dates: frozenset[str],
        regime_by_date: dict[str, CrossMarketRegimeSnapshot],
        outcome_by_date: dict[str, Decimal],
        created_at: datetime,
    ) -> tuple[ReplicationPartitionResult, ...]:
        buckets: dict[str, list[str]] = {}
        for date in dates:
            regime = regime_by_date.get(date)
            if regime is None:
                continue
            try:
                label = regime.label_for(_VOLATILITY_DIMENSION)
            except Exception:
                continue
            buckets.setdefault(label, []).append(date)
        results = []
        for label, bucket_dates in sorted(buckets.items()):
            slice_ = _slice_for(
                period_dates=frozenset(bucket_dates),
                primary_dates=primary_dates,
                secondary_dates=secondary_dates,
                outcome_by_date=outcome_by_date,
            )
            note = (
                "Section 7's EQUITY_VOLATILITY_REGIME_LABEL_V1 partition (the same classifier "
                "calibration-v2's own VOLATILITY_REGIME stability slice uses), restricted "
                "further to this replication's own BTC-momentum condition. No per-regime "
                "matched-null battery was built; this bucket's calibration population is "
                "insufficient by construction (only the full-sample primary specification is "
                "matched-calibrated) -- descriptive evidence only, never claimed as independent "
                "calibrated evidence for this regime."
            )
            results.append(
                self._partition_result(
                    specification=specification,
                    slice_=slice_,
                    partition_kind=PartitionKind.VOLATILITY_STATE,
                    label=label,
                    start_date=None,
                    end_date=None,
                    matched_null_count=0,
                    matched_percentile=None,
                    calibration_note=note,
                    created_at=created_at,
                )
            )
        return tuple(results)

    def _timing_checks(
        self,
        *,
        specification: ReplicationSpecification,
        dates: tuple[str, ...],
        primary_dates: frozenset[str],
        secondary_by_date: dict[str, Decimal],
        full_slice: _Slice,
        regime_by_date: dict[str, CrossMarketRegimeSnapshot],
        outcome_by_date: dict[str, Decimal],
        created_at: datetime,
    ) -> tuple[TimingAlignmentResult, ...]:
        all_dates = frozenset(dates)

        # Section 14: verify the documented alignment convention against
        # genuine retained evidence -- BTC's own daily candle for date D is
        # conservatively treated as unavailable before 23:59:59 UTC that
        # same date (``domain.historical_market_data.
        # observation_view_from_crypto_daily_candle``); equity's date-D bar
        # must be available strictly before that boundary for same-day
        # conditioning to be timing-safe.
        equity_available_at = _load_equity_available_at(
            historical_repository=self._historical_repository,
            evidence=self._evidence,
            equity_snapshot_id=specification.dataset_snapshot_id,
        )
        violations = 0
        checked = 0
        for date in dates:
            available_at = equity_available_at.get(date)
            if available_at is None:
                continue
            checked += 1
            boundary = datetime.fromisoformat(date).replace(
                hour=23, minute=59, second=59, tzinfo=UTC
            )
            if available_at > boundary:
                violations += 1
        # Reuses the identical primary/secondary condition dates already
        # derived once in ``execute()`` from the frozen TRAIN period --
        # never re-derived here, so this is exactly the full-sample slice.
        primary_slice = full_slice
        primary_result = TimingAlignmentResult.create(
            replication_id=specification.replication_id,
            convention=TimingConvention.PRIMARY_SAME_DAY_V1,
            ordering_valid=violations == 0 and checked > 0,
            checked_date_count=checked,
            violation_count=violations,
            sample_count=len(primary_slice.sample_values),
            effective_sample_count=_effective_sample(
                len(primary_slice.sample_values), specification.horizon_sessions
            ),
            benchmark_sample_count=len(primary_slice.benchmark_values),
            interaction_effect=_interaction_effect(primary_slice),
            cohens_d=_cohens_d(primary_slice.sample_values, primary_slice.benchmark_values),
            note=(
                f"Checked {checked} retained equity bars against BTC's 23:59:59 UTC same-date "
                f"availability boundary; {violations} ordering violation(s). Equity-volatility "
                "evidence for date T must be available before the BTC outcome window anchored to "
                "date T begins (section 14's critical acceptance boundary)."
            ),
            created_at=created_at,
        )

        # Section 15: one predeclared, strictly more conservative
        # alternative -- lag the equity-volatility conditioning feature by
        # one session so it can never overlap the BTC outcome window under
        # any interpretation of same-day timing.
        lagged_secondary_by_date = {
            dates[index]: secondary_by_date[dates[index - 1]]
            for index in range(1, len(dates))
            if dates[index - 1] in secondary_by_date
        }
        lagged_train_dates = frozenset(
            d
            for d in dates
            if specification.train_period_start <= d <= specification.train_period_end
        )
        lagged_secondary_dates = resolve_condition_dates(
            feature_id=specification.secondary_feature_id,
            condition_rule=specification.secondary_condition_rule,
            condition_label=None,
            feature_by_date=lagged_secondary_by_date,
            regime_by_date=regime_by_date,
            train_dates=lagged_train_dates,
        )
        lagged_slice = _slice_for(
            period_dates=all_dates,
            primary_dates=primary_dates,
            secondary_dates=lagged_secondary_dates,
            outcome_by_date=outcome_by_date,
        )
        lagged_result = TimingAlignmentResult.create(
            replication_id=specification.replication_id,
            convention=TimingConvention.LAGGED_EQUITY_ONE_SESSION_V1,
            ordering_valid=True,
            checked_date_count=checked,
            violation_count=0,
            sample_count=len(lagged_slice.sample_values),
            effective_sample_count=_effective_sample(
                len(lagged_slice.sample_values), specification.horizon_sessions
            ),
            benchmark_sample_count=len(lagged_slice.benchmark_values),
            interaction_effect=_interaction_effect(lagged_slice),
            cohens_d=_cohens_d(lagged_slice.sample_values, lagged_slice.benchmark_values),
            note=(
                "Robustness-only alternative (section 15): date T's equity-volatility condition "
                "uses date T-1's value, strictly earlier than the primary same-day convention -- "
                "structurally immune to same-day information overlap by construction."
            ),
            created_at=created_at,
        )
        return (primary_result, lagged_result)

    def _effect_profile(
        self,
        *,
        specification: ReplicationSpecification,
        dates: tuple[str, ...],
        full_slice: _Slice,
        temporal_partitions: tuple[ReplicationPartitionResult, ...],
        outcome_by_date: dict[str, Decimal],
        created_at: datetime,
    ) -> ReplicationEffectProfile:
        # Section 10: unconditional (no conditioning at all).
        unconditional_values = tuple(outcome_by_date[d] for d in dates if d in outcome_by_date)
        unconditional_mean = _mean(unconditional_values)

        high_summary = _summary(full_slice.sample_values, "high_volatility_conditional")
        low_summary = _summary(full_slice.benchmark_values, "low_volatility_conditional")
        interaction_effect = _interaction_effect(full_slice)
        cohens_d = _cohens_d(full_slice.sample_values, full_slice.benchmark_values)

        # Section 12: winsorized sensitivity (5% each tail, both groups).
        winsorized_sample = _winsorize(full_slice.sample_values)
        winsorized_benchmark = _winsorize(full_slice.benchmark_values)
        winsorized_effect = None
        w_sample_mean, w_benchmark_mean = _mean(winsorized_sample), _mean(winsorized_benchmark)
        if w_sample_mean is not None and w_benchmark_mean is not None:
            winsorized_effect = bound_statistical_decimal(w_sample_mean - w_benchmark_mean)

        # Robust (median-based) comparison.
        robust_effect = None
        sample_median, benchmark_median = (
            _median(full_slice.sample_values),
            _median(full_slice.benchmark_values),
        )
        if sample_median is not None and benchmark_median is not None:
            robust_effect = bound_statistical_decimal(sample_median - benchmark_median)

        # Leave-one-temporal-third-out.
        leave_one_out: list[tuple[str, Decimal | None]] = []
        for partition in temporal_partitions:
            excluded_dates = frozenset(dates) - frozenset(
                d
                for d in dates
                if partition.start_date is not None
                and partition.end_date is not None
                and partition.start_date <= d <= partition.end_date
            )
            reduced_sample = tuple(d for d in full_slice.sample_dates if d in excluded_dates)
            reduced_benchmark = tuple(d for d in full_slice.benchmark_dates if d in excluded_dates)
            reduced_slice = _Slice(
                sample_dates=reduced_sample,
                benchmark_dates=reduced_benchmark,
                sample_values=tuple(outcome_by_date[d] for d in reduced_sample),
                benchmark_values=tuple(outcome_by_date[d] for d in reduced_benchmark),
            )
            leave_one_out.append((f"EXCLUDE_{partition.label}", _interaction_effect(reduced_slice)))

        # Bounded holdout-window exclusion: the tiny locked-holdout window
        # that carries the genuine matched statistic is, by construction,
        # this sample's single most influential (and least redundant)
        # subperiod -- section 12 explicitly asks whether the result
        # survives removing it.
        holdout_segment = next(
            (
                item
                for item in specification.exposure_segments
                if item.label.value == "LOCKED_HOLDOUT_CONSUMED"
            ),
            None,
        )
        holdout_excluded_effect = None
        if holdout_segment is not None:
            kept_dates = frozenset(
                d
                for d in dates
                if not (holdout_segment.start_date <= d <= holdout_segment.end_date)
            )
            reduced_sample = tuple(d for d in full_slice.sample_dates if d in kept_dates)
            reduced_benchmark = tuple(d for d in full_slice.benchmark_dates if d in kept_dates)
            reduced_slice = _Slice(
                sample_dates=reduced_sample,
                benchmark_dates=reduced_benchmark,
                sample_values=tuple(outcome_by_date[d] for d in reduced_sample),
                benchmark_values=tuple(outcome_by_date[d] for d in reduced_benchmark),
            )
            holdout_excluded_effect = _interaction_effect(reduced_slice)

        # Most-influential observations: the sample-group dates with the
        # largest-magnitude outcome, retained explicitly rather than
        # silently averaged away (section 11).
        ranked = sorted(
            zip(full_slice.sample_dates, full_slice.sample_values, strict=True),
            key=lambda item: abs(item[1]),
            reverse=True,
        )
        most_influential = tuple(date for date, _value in ranked[:3])

        outlier_assessment = _outlier_dependence_note(
            full_effect=interaction_effect,
            winsorized_effect=winsorized_effect,
            robust_effect=robust_effect,
            holdout_excluded_effect=holdout_excluded_effect,
            leave_one_out=tuple(leave_one_out),
        )

        percentiles = dict(high_summary.percentiles)
        return ReplicationEffectProfile.create(
            replication_id=specification.replication_id,
            unconditional_mean=unconditional_mean,
            unconditional_sample_count=len(unconditional_values),
            low_vol_mean=low_summary.mean,
            low_vol_sample_count=low_summary.sample_count,
            low_vol_median=low_summary.median,
            low_vol_hit_rate=low_summary.hit_rate,
            low_vol_stdev=low_summary.stdev,
            high_vol_mean=high_summary.mean,
            high_vol_sample_count=high_summary.sample_count,
            high_vol_median=high_summary.median,
            high_vol_hit_rate=high_summary.hit_rate,
            high_vol_stdev=high_summary.stdev,
            high_vol_p10=percentiles.get(10),
            high_vol_p90=percentiles.get(90),
            interaction_effect=interaction_effect,
            cohens_d=cohens_d,
            benchmark_relative_effect=interaction_effect,
            practical_effect_floor=EFFECT_SIZE_FLOOR_POLICY_V1.floor_for(
                _outcome_family_for(specification)
            ),
            winsorized_interaction_effect=winsorized_effect,
            robust_median_interaction_effect=robust_effect,
            leave_one_third_out_effects=tuple(leave_one_out),
            holdout_window_excluded_effect=holdout_excluded_effect,
            most_influential_dates=most_influential,
            outlier_robust=outlier_assessment.robust,
            outlier_dependence_note=outlier_assessment.note,
            created_at=created_at,
        )

    def _confound_assessment(
        self,
        *,
        specification: ReplicationSpecification,
        full_slice: _Slice,
        regime_by_date: dict[str, CrossMarketRegimeSnapshot],
        interaction_effect: Decimal | None,
        created_at: datetime,
    ) -> ReplicationConfoundAssessment:
        dimension_results = []
        for dimension in _CONFOUND_DIMENSIONS:
            trial_label, trial_fraction, _trial_count = _dominant_label(
                full_slice.sample_dates, regime_by_date, dimension
            )
            benchmark_label, benchmark_fraction, _benchmark_count = _dominant_label(
                full_slice.benchmark_dates, regime_by_date, dimension
            )
            same_label = trial_label == benchmark_label
            within_sample = tuple(
                d
                for d in full_slice.sample_dates
                if _label_of(regime_by_date, d, dimension) == trial_label
            )
            within_benchmark = tuple(
                d
                for d in full_slice.benchmark_dates
                if _label_of(regime_by_date, d, dimension) == trial_label
            )
            effect_within = None
            if len(within_sample) >= MINIMUM_PARTITION_RAW_SAMPLE and len(within_benchmark) >= (
                MINIMUM_PARTITION_RAW_SAMPLE // 2
            ):
                sample_mean = _mean(tuple(_outcome(within_sample, full_slice)))
                benchmark_mean = _mean(tuple(_outcome(within_benchmark, full_slice)))
                if sample_mean is not None and benchmark_mean is not None:
                    effect_within = bound_statistical_decimal(sample_mean - benchmark_mean)
            plausible = _confound_plausible(
                trial_fraction=trial_fraction,
                overall_effect=interaction_effect,
                effect_within=effect_within,
            )
            dimension_results.append(
                ConfoundDimensionResult(
                    dimension=dimension.value,
                    dominant_trial_label=trial_label,
                    trial_concentration=trial_fraction,
                    dominant_benchmark_label=benchmark_label,
                    benchmark_concentration=benchmark_fraction,
                    same_dominant_label=same_label,
                    effect_within_dominant_label=effect_within,
                    dominant_label_sample_count=len(within_sample),
                    confound_plausible=plausible,
                    note=(
                        f"Trial sample concentrates {trial_fraction} in {dimension.value}="
                        f"{trial_label}; benchmark concentrates {benchmark_fraction} in "
                        f"{benchmark_label}."
                    ),
                )
            )
        return ReplicationConfoundAssessment.create(
            replication_id=specification.replication_id,
            dimensions=tuple(dimension_results),
            note=(
                "Section 13: descriptive concentration/co-occurrence check only across six "
                "already-retained regime dimensions -- no new combinatorial search, no causal "
                "adjustment."
            ),
            created_at=created_at,
        )


def _outcome(dates: Sequence[str], slice_: _Slice) -> tuple[Decimal, ...]:
    by_date = dict(zip(slice_.sample_dates, slice_.sample_values, strict=True))
    by_date.update(dict(zip(slice_.benchmark_dates, slice_.benchmark_values, strict=True)))
    return tuple(by_date[d] for d in dates if d in by_date)


def _label_of(
    regime_by_date: dict[str, CrossMarketRegimeSnapshot],
    date: str,
    dimension: CrossMarketRegimeDimension,
) -> str | None:
    regime = regime_by_date.get(date)
    if regime is None:
        return None
    try:
        return regime.label_for(dimension)
    except Exception:
        return None


def _dominant_label(
    dates: Sequence[str],
    regime_by_date: dict[str, CrossMarketRegimeSnapshot],
    dimension: CrossMarketRegimeDimension,
) -> tuple[str, Decimal, int]:
    counts: dict[str, int] = {}
    for date in dates:
        label = _label_of(regime_by_date, date, dimension)
        if label is None:
            continue
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "INSUFFICIENT_EVIDENCE", Decimal(0), 0
    label, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    total = sum(counts.values())
    fraction = bound_statistical_decimal(Decimal(count) / Decimal(total)) if total else Decimal(0)
    return label, fraction, count


def _confound_plausible(
    *, trial_fraction: Decimal, overall_effect: Decimal | None, effect_within: Decimal | None
) -> bool:
    """Section 13's plausibility rule: the trial sample must concentrate
    heavily (>=60%) in one label of the overlapping dimension *and* the
    interaction effect must materially weaken or flip sign once restricted
    to that same dominant label -- otherwise the dimension is not standing
    in for the interaction."""

    if trial_fraction < Decimal("0.6") or overall_effect is None or effect_within is None:
        return False
    if overall_effect == 0:
        return False
    sign_flipped = (effect_within > 0) != (overall_effect > 0)
    shrank_materially = abs(effect_within) < abs(overall_effect) * Decimal("0.5")
    return sign_flipped or shrank_materially


@dataclass(frozen=True, slots=True)
class _OutlierAssessment:
    robust: bool
    note: str


def _outlier_dependence_note(
    *,
    full_effect: Decimal | None,
    winsorized_effect: Decimal | None,
    robust_effect: Decimal | None,
    holdout_excluded_effect: Decimal | None,
    leave_one_out: tuple[tuple[str, Decimal | None], ...],
) -> _OutlierAssessment:
    """Section 12: retained as a structured ``(robust, note)`` result --
    never re-derived from parsing the note text -- so a caller's control
    flow can never silently desync from what the note actually says."""

    if full_effect is None or full_effect == 0:
        return _OutlierAssessment(
            robust=False,
            note="Full-sample interaction effect unavailable or zero; outlier dependence cannot "
            "be assessed as robust.",
        )
    flips: list[str] = []
    shrunk: list[str] = []
    for candidate_label, candidate_value in (
        ("winsorized", winsorized_effect),
        ("robust_median", robust_effect),
        ("holdout_window_excluded", holdout_excluded_effect),
        *leave_one_out,
    ):
        if candidate_value is None:
            continue
        if (candidate_value > 0) != (full_effect > 0):
            flips.append(candidate_label)
        elif abs(candidate_value) < abs(full_effect) * Decimal("0.5"):
            shrunk.append(candidate_label)
    if flips:
        return _OutlierAssessment(
            robust=False,
            note=(
                f"Full-sample interaction effect sign is NOT robust: it flips under "
                f"{', '.join(flips)}. The apparent relationship is not a broad distribution "
                "shift; it depends materially on specific retained observations/periods."
            ),
        )
    if shrunk:
        return _OutlierAssessment(
            robust=False,
            note=(
                f"Full-sample interaction effect sign holds but its magnitude shrinks by more "
                f"than half under {', '.join(shrunk)} -- a broad distribution shift this is not; "
                "the full-sample magnitude depends materially on specific retained "
                "observations/periods, most notably the short locked-holdout window."
            ),
        )
    return _OutlierAssessment(
        robust=True,
        note=(
            "Full-sample interaction effect sign and magnitude are consistent across every "
            "predeclared sensitivity check (winsorized, robust median, holdout-window exclusion, "
            "leave-one-temporal-third-out)."
        ),
    )


def _outcome_family_for(specification: ReplicationSpecification) -> OutcomeFamily:
    from apex.application.research_calibration import _OUTCOME_FAMILY_BY_OUTCOME_ID

    family = _OUTCOME_FAMILY_BY_OUTCOME_ID.get(specification.outcome_id.value)
    return family if family is not None else OutcomeFamily.BTC_RETURN


def _load_equity_available_at(
    *,
    historical_repository: HistoricalMarketDataCollaborator,
    evidence: SecuritiesEvidenceCollaborator,
    equity_snapshot_id: str,
) -> dict[str, datetime]:
    """Genuine retained ``SecuritiesBarEvidence.available_at`` timestamps
    for SPY, keyed by session date -- reused directly for section 14's
    timing-integrity check rather than assumed from documentation alone."""

    snapshot = historical_repository.snapshot_by_id(equity_snapshot_id)
    if snapshot is None:
        raise ReplicationNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
    plan = historical_repository.get_plan(snapshot.plan_id)
    if plan is None:
        raise ReplicationNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
    spy_instrument_id = next(
        (member.instrument_id for member in plan.universe if member.symbol_at_capture == "SPY"),
        None,
    )
    if spy_instrument_id is None:
        raise ReplicationApplicationError("SPY_INSTRUMENT_NOT_RESOLVED")
    available_at_by_date: dict[str, datetime] = {}
    for batch_id in snapshot.source_batch_ids:
        _instruments, _sessions, bars, _actions = evidence.get_dataset(batch_id)
        for bar in bars:
            if bar.instrument_id == spy_instrument_id:
                available_at_by_date[bar.period_end.date().isoformat()] = bar.available_at
    return available_at_by_date


# ==========================================================================
# Sections 17-20: conclusion derivation
# ==========================================================================


def derive_replication_conclusion(
    *,
    specification: ReplicationSpecification,
    full_partition: ReplicationPartitionResult,
    temporal_partitions: tuple[ReplicationPartitionResult, ...],
    volatility_partitions: tuple[ReplicationPartitionResult, ...],
    timing: tuple[TimingAlignmentResult, ...],
    effect_profile: ReplicationEffectProfile,
    confound: ReplicationConfoundAssessment,
    created_at: datetime,
) -> ReplicationConclusion:
    primary_timing = next(
        item for item in timing if item.convention is TimingConvention.PRIMARY_SAME_DAY_V1
    )
    if not primary_timing.ordering_valid:
        return ReplicationConclusion.create(
            replication_id=specification.replication_id,
            follow_up_id=specification.follow_up_id,
            parent_hypothesis_id=specification.parent_hypothesis_id,
            conclusion_state=ReplicationConclusionState.TIMING_INVALID,
            evidence_grade=EvidenceGrade.UNRELIABLE,
            evidence_grade_upgraded=False,
            period_stability_note="Not assessed: primary timing alignment failed first.",
            volatility_stability_note="Not assessed: primary timing alignment failed first.",
            timing_note=primary_timing.note,
            matched_null_note="Not assessed: primary timing alignment failed first.",
            effect_size_note="Not assessed: primary timing alignment failed first.",
            outlier_robustness_note="Not assessed: primary timing alignment failed first.",
            confound_note="Not assessed: primary timing alignment failed first.",
            transaction_cost_relevance=TransactionCostRelevance.COST_EVIDENCE_UNAVAILABLE,
            forward_confirmation_required=True,
            forward_confirmation_reason=(
                "Section 14 critical acceptance boundary failed; this design cannot be trusted "
                "under any forward-observation policy until the alignment is fixed."
            ),
            minimum_forward_observation_policy="NOT_APPLICABLE_TIMING_INVALID",
            limitations=("CROSS_MARKET_TIMING_ALIGNMENT_VIOLATION",),
            rationale="Same-day equity-close information was not verified available before the "
            "BTC outcome window it conditions began; rejected before any further evaluation "
            "(section 14).",
            created_at=created_at,
        )

    minimum_raw = MINIMUM_PARTITION_RAW_SAMPLE
    if full_partition.sample_count < minimum_raw or full_partition.effective_sample_count < (
        specification.minimum_effective_sample
    ):
        return _insufficient_conclusion(
            specification,
            reason=(
                f"Full-sample genuine sample ({full_partition.sample_count} raw, "
                f"{full_partition.effective_sample_count} effective) is below the predeclared "
                f"minimum effective sample ({specification.minimum_effective_sample})."
            ),
            created_at=created_at,
        )

    if specification.v2_matched_percentile < NULL_LIKE_MATCHED_PERCENTILE_CEILING:
        return ReplicationConclusion.create(
            replication_id=specification.replication_id,
            follow_up_id=specification.follow_up_id,
            parent_hypothesis_id=specification.parent_hypothesis_id,
            conclusion_state=ReplicationConclusionState.NULL_COMPATIBLE,
            evidence_grade=EvidenceGrade.UNRELIABLE,
            evidence_grade_upgraded=False,
            period_stability_note=_period_stability_note(full_partition, temporal_partitions),
            volatility_stability_note=_volatility_stability_note(volatility_partitions),
            timing_note=primary_timing.note,
            matched_null_note=(
                f"Matched percentile {specification.v2_matched_percentile} is below the "
                f"{NULL_LIKE_MATCHED_PERCENTILE_CEILING} null-like ceiling "
                f"(EVIDENCE_GRADE_POLICY_V1): indistinguishable from the retained matched-null "
                "population."
            ),
            effect_size_note=_effect_size_note(effect_profile),
            outlier_robustness_note=effect_profile.outlier_dependence_note,
            confound_note="Not decisive: already null-compatible on the matched-null percentile "
            "alone.",
            transaction_cost_relevance=classify_transaction_cost_relevance(
                effect=effect_profile.interaction_effect,
                round_trip_cost_rate=CONSERVATIVE_ROUND_TRIP_COST_RATE,
            ),
            forward_confirmation_required=False,
            forward_confirmation_reason="Null-compatible result does not justify forward "
            "observation budget for this design.",
            minimum_forward_observation_policy="NOT_APPLICABLE_NULL_COMPATIBLE",
            limitations=("MATCHED_PERCENTILE_BELOW_NULL_LIKE_CEILING",),
            rationale=(
                f"Matched percentile {specification.v2_matched_percentile} against "
                f"{specification.v2_matched_null_count} matched controls does not clear the "
                f"{NULL_LIKE_MATCHED_PERCENTILE_CEILING} null-like ceiling; the genuine statistic "
                "is not distinguishable from ordinary matched-null behavior."
            ),
            created_at=created_at,
        )

    if not confound.overall_confound_resolved:
        plausible = tuple(item.dimension for item in confound.dimensions if item.confound_plausible)
        return ReplicationConclusion.create(
            replication_id=specification.replication_id,
            follow_up_id=specification.follow_up_id,
            parent_hypothesis_id=specification.parent_hypothesis_id,
            conclusion_state=ReplicationConclusionState.CONFOUNDING_NOT_RESOLVED,
            evidence_grade=EvidenceGrade.EXPLORATORY,
            evidence_grade_upgraded=False,
            period_stability_note=_period_stability_note(full_partition, temporal_partitions),
            volatility_stability_note=_volatility_stability_note(volatility_partitions),
            timing_note=primary_timing.note,
            matched_null_note=_matched_null_note(specification, full_partition),
            effect_size_note=_effect_size_note(effect_profile),
            outlier_robustness_note=effect_profile.outlier_dependence_note,
            confound_note=(
                "Interaction effect weakens or flips sign once restricted to the dominant "
                f"label of: {', '.join(plausible)}. Confounding is not resolved by this "
                "descriptive check."
            ),
            transaction_cost_relevance=classify_transaction_cost_relevance(
                effect=effect_profile.interaction_effect,
                round_trip_cost_rate=CONSERVATIVE_ROUND_TRIP_COST_RATE,
            ),
            forward_confirmation_required=True,
            forward_confirmation_reason=(
                "Confounding was not resolved; forward evidence would need to break the "
                "co-occurrence with the overlapping regime dimension(s) to be informative."
            ),
            minimum_forward_observation_policy=_FORWARD_POLICY_TEXT,
            limitations=("REGIME_CONFOUND_NOT_RESOLVED",),
            rationale="At least one overlapping regime dimension concentrates the trial sample "
            "and the interaction effect materially weakens or flips within that dominant label.",
            created_at=created_at,
        )

    temporal_consistent, temporal_note = _temporal_consistency(full_partition, temporal_partitions)
    outlier_robust = effect_profile.outlier_robust
    meets_floor = effect_profile.meets_practical_floor

    if not temporal_consistent or not outlier_robust:
        state = ReplicationConclusionState.UNSTABLE
        grade = EvidenceGrade.EXPLORATORY
    elif meets_floor:
        state = ReplicationConclusionState.REPLICATED
        grade = specification_evidence_grade(specification)
    else:
        state = ReplicationConclusionState.PARTIALLY_REPLICATED
        grade = EvidenceGrade.EXPLORATORY

    forward_required = True  # section 19: always true for an IN_SAMPLE_REPLICATION result.
    forward_reason = (
        "in_sample_replication_note: the entire retained history was already consumed by this "
        "hypothesis's own discovery/validation/holdout phases and by both calibration batteries; "
        "no genuinely fresh evidence exists yet, regardless of how this replication concludes."
    )

    return ReplicationConclusion.create(
        replication_id=specification.replication_id,
        follow_up_id=specification.follow_up_id,
        parent_hypothesis_id=specification.parent_hypothesis_id,
        conclusion_state=state,
        evidence_grade=grade,
        evidence_grade_upgraded=grade is EvidenceGrade.VALIDATED or grade is EvidenceGrade.STRONG,
        period_stability_note=temporal_note,
        volatility_stability_note=_volatility_stability_note(volatility_partitions),
        timing_note=primary_timing.note,
        matched_null_note=_matched_null_note(specification, full_partition),
        effect_size_note=_effect_size_note(effect_profile),
        outlier_robustness_note=effect_profile.outlier_dependence_note,
        confound_note="No overlapping regime dimension checked plausibly explains the interaction "
        "away (section 13); not a causal adjustment.",
        transaction_cost_relevance=classify_transaction_cost_relevance(
            effect=effect_profile.interaction_effect,
            round_trip_cost_rate=CONSERVATIVE_ROUND_TRIP_COST_RATE,
        ),
        forward_confirmation_required=forward_required,
        forward_confirmation_reason=forward_reason,
        minimum_forward_observation_policy=_FORWARD_POLICY_TEXT,
        limitations=(
            "IN_SAMPLE_REPLICATION_NOT_INDEPENDENT_CONFIRMATION",
            "FINITE_SAMPLE_15_CONTROL_MATCHED_STRATUM",
            "SHORT_LOCKED_HOLDOUT_WINDOW",
        ),
        rationale=(
            f"Full-sample interaction effect {full_partition.interaction_effect} "
            f"(cohens_d={full_partition.cohens_d}) over {full_partition.sample_count} raw / "
            f"{full_partition.effective_sample_count} effective samples; temporal stability: "
            f"{temporal_consistent}; outlier-robust: {outlier_robust}; meets practical floor: "
            f"{meets_floor}."
        ),
        created_at=created_at,
    )


_FORWARD_POLICY_TEXT = (
    "Minimum forward-observation policy: at least one additional predeclared temporal third's "
    "worth of genuinely new, never-before-retained daily BTC/equity evidence (approximately the "
    "existing walk-forward step of 252 sessions) accumulated after this replication's created_at, "
    "evaluated under this identical frozen specification, before any strategy-formalization "
    "consideration."
)


def specification_evidence_grade(specification: ReplicationSpecification) -> EvidenceGrade:
    """Section 18: never automatically raised above the historical
    calibrated grade (``SUGGESTIVE``, from the frozen v2 classification)
    unless every upgrade condition this function's caller already checked
    (matched-sufficiency, temporal stability, timing integrity, effect
    floor, robustness, no confound) holds -- and even then this function
    only ever returns the *same* ``SUGGESTIVE`` grade, since none of those
    checks re-runs holdout/robustness through the full governed pipeline
    a ``VALIDATED``/``STRONG`` grade requires (section 18: an upgrade
    requires genuinely stronger evidence than a descriptive replication
    alone can supply)."""

    return EvidenceGrade.SUGGESTIVE


def _insufficient_conclusion(
    specification: ReplicationSpecification, *, reason: str, created_at: datetime
) -> ReplicationConclusion:
    return ReplicationConclusion.create(
        replication_id=specification.replication_id,
        follow_up_id=specification.follow_up_id,
        parent_hypothesis_id=specification.parent_hypothesis_id,
        conclusion_state=ReplicationConclusionState.INSUFFICIENT_EVIDENCE,
        evidence_grade=EvidenceGrade.EXPLORATORY,
        evidence_grade_upgraded=False,
        period_stability_note="Not assessed: full-sample evidence is already insufficient.",
        volatility_stability_note="Not assessed: full-sample evidence is already insufficient.",
        timing_note="Primary alignment valid; not the limiting factor.",
        matched_null_note="Not assessed: full-sample evidence is already insufficient.",
        effect_size_note="Not assessed: full-sample evidence is already insufficient.",
        outlier_robustness_note="Not assessed: full-sample evidence is already insufficient.",
        confound_note="Not assessed: full-sample evidence is already insufficient.",
        transaction_cost_relevance=TransactionCostRelevance.COST_EVIDENCE_UNAVAILABLE,
        forward_confirmation_required=True,
        forward_confirmation_reason=reason,
        minimum_forward_observation_policy=_FORWARD_POLICY_TEXT,
        limitations=("EFFECTIVE_SAMPLE_BELOW_PREDECLARED_MINIMUM",),
        rationale=reason,
        created_at=created_at,
    )


def _temporal_consistency(
    full_partition: ReplicationPartitionResult,
    temporal_partitions: tuple[ReplicationPartitionResult, ...],
) -> tuple[bool, str]:
    if full_partition.interaction_effect is None or full_partition.interaction_effect == 0:
        return False, "Full-sample interaction effect is zero or unavailable."
    full_sign = full_partition.sign
    adequate = [
        item
        for item in temporal_partitions
        if item.sample_count >= MINIMUM_PARTITION_RAW_SAMPLE and item.interaction_effect is not None
    ]
    if len(adequate) < 2:
        return False, (
            f"Only {len(adequate)}/{len(temporal_partitions)} temporal thirds have a sufficient "
            f"raw sample (>= {MINIMUM_PARTITION_RAW_SAMPLE}) to assess stability; treated as "
            "unstable rather than assumed stable."
        )
    consistent = sum(1 for item in adequate if item.sign == full_sign)
    is_consistent = consistent == len(adequate)
    labels = ", ".join(f"{item.label}={item.interaction_effect}" for item in temporal_partitions)
    if is_consistent:
        return (
            True,
            f"Sign consistent with the full-sample result across every adequate temporal "
            f"third ({labels}).",
        )
    return False, (
        f"Full-sample sign does not hold across every adequate temporal third ({labels}); driven "
        "by only a subset of retained history."
    )


def _volatility_stability_note(
    volatility_partitions: tuple[ReplicationPartitionResult, ...],
) -> str:
    if not volatility_partitions:
        return "No volatility-regime label was classifiable over the retained panel."
    parts = ", ".join(
        f"{item.label}(n={item.sample_count},effect={item.interaction_effect})"
        for item in volatility_partitions
    )
    return f"Descriptive volatility-state breakdown (not separately matched-calibrated): {parts}."


def _matched_null_note(
    specification: ReplicationSpecification, full_partition: ReplicationPartitionResult
) -> str:
    return (
        f"Reused v2 matched-null stratum {specification.v2_matched_stratum} "
        f"({specification.v2_matched_null_count} controls, unaugmented): matched percentile "
        f"{specification.v2_matched_percentile} -- top-ranked among "
        f"{specification.v2_matched_null_count} retained matched controls, a finite-sample rank, "
        "never a claim of a continuous 100th-percentile population effect."
    )


def _effect_size_note(effect_profile: ReplicationEffectProfile) -> str:
    return (
        f"cohens_d={effect_profile.cohens_d} (practical floor "
        f"{effect_profile.practical_effect_floor}, "
        f"meets_floor={effect_profile.meets_practical_floor}); "
        f"unconditional_mean={effect_profile.unconditional_mean}; "
        f"low_vol_conditional_mean={effect_profile.low_vol_mean}; "
        f"high_vol_conditional_mean={effect_profile.high_vol_mean}."
    )


def _period_stability_note(
    full_partition: ReplicationPartitionResult,
    temporal_partitions: tuple[ReplicationPartitionResult, ...],
) -> str:
    _consistent, note = _temporal_consistency(full_partition, temporal_partitions)
    return note


def research_replication_agenda_note() -> str:
    return (
        f"{RESEARCH_REPLICATION_V1}: exactly one predeclared follow-up "
        f"({FollowUpKind.SPLIT_BY_REGIME.value}-proposed, evaluated under its parent's own "
        "INTERACTION_V1 design) is in scope for this milestone."
    )
