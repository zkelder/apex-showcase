"""Counterfactual Outcome Evidence Specificity v3 -- domain proofs.

Adversarial by design: every case below is a way the V3 qualification
contract could have manufactured confidence it had not earned (market beta
read as edge, one economic move counted once per asset, one decision counted
once per satisfied gate, overlapping windows counted as independent samples,
a partially-drained follow-up backlog qualifying a cohort early, a
whole-universe cohort "outperforming" a zero-by-construction peer), and the
assertion that it does not.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.counterfactual_outcome import _is_benign_evidence_maturation
from apex.domain.counterfactual_classification import (
    REASON_CLASSIFIER_V2,
    CounterfactualDecisionClassification,
    ReasonFamily,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    FOLLOWUP_POLICY_V3,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.counterfactual_outcome import (
    CANONICAL_REASON_FAMILY_PRECEDENCE_V1,
    COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS,
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V2,
    COUNTERFACTUAL_OUTCOME_POLICY_V3,
    COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH,
    DEFECTIVE_OUTCOME_POLICY_VERSIONS,
    SPECIFICITY_MINIMUM_PEER_PRODUCTS,
    V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING,
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeError,
    CounterfactualOutcomeQualificationState,
    SubjectProjectionFacts,
    build_counterfactual_outcome_aggregates,
    canonical_reason_family,
    counterfactual_outcome_aggregate_canonical_hash,
    counterfactual_outcome_sourcing_exclusion,
    outcome_policy_version_may_source_new_research,
    window_is_on_independent_grid,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import content_digest

METHOD = "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"
OTHER_METHOD = "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1"
PRODUCTS = tuple(f"{symbol}-USD" for symbol in ("BTC", "ETH", "SOL", "ADA", "XRP"))
DAY0 = datetime(2026, 8, 4, tzinfo=UTC)  # 2026-08-04 is a whole multiple of 14 days from epoch? no
RUN_ID = "cycle-2026-09-26-counterfactual-outcome-v3"
GENERATED_AT = datetime(2026, 9, 26, tzinfo=UTC)
FLAT = Decimal("0")

# ---------------------------------------------------------------------------
# Fixture world: one method, N products, one decision per product per day,
# ORIGIN_EVIDENCE anchored at the day-scoped midnight (W0 == day), returns
# supplied per (product, day).
# ---------------------------------------------------------------------------


def _decision(
    *,
    decision_id: str,
    projection_id: str,
    evaluated_at: datetime,
    origin: DecisionOrigin = DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
    venue: SharedVenue = SharedVenue.CRYPTO,
) -> CounterfactualDecision:
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=origin,
        origin_decision_id=f"{decision_id}-origin",
        origin_canonical_hash="a" * 64,
        origin_state="REJECTED",
        subject_projection_id=projection_id,
        subject_source_record_type="CryptoResearchOpportunity",
        subject_source_record_id=f"{decision_id}-record",
        venue=venue,
        opportunity_set_id="set-1",
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=evaluated_at,
        disposition=DecisionDisposition.NOT_ADVANCED,
        raw_reason_codes=("SOURCE:TEST",),
        explanation="test decision",
        canonical_hash="b" * 64,
        authority="NONE",
    )


def _classification(
    decision_id: str, families: tuple[ReasonFamily, ...]
) -> CounterfactualDecisionClassification:
    return CounterfactualDecisionClassification(
        classification_id=f"{decision_id}-classification",
        decision_id=decision_id,
        decision_canonical_hash="b" * 64,
        classifier_version=REASON_CLASSIFIER_V2,
        classifier_policy_hash="c" * 64,
        reason_families=families,
        classified_at=GENERATED_AT,
        canonical_hash="d" * 64,
        authority="NONE",
    )


def _v3_observation(
    *,
    decision: CounterfactualDecision,
    product: str,
    window_start: datetime,
    horizon_days: int,
    subsequent_return: Decimal | None,
    horizon_kind: FollowUpHorizonKind = FollowUpHorizonKind.SHORT_HORIZON,
    baseline_kind: FollowUpBaselineKind = FollowUpBaselineKind.ORIGIN_EVIDENCE,
    policy_version: str = FOLLOWUP_POLICY_V3,
    policy_anchor_at: datetime | None = None,
    venue: SharedVenue = SharedVenue.CRYPTO,
) -> DecisionFollowUpObservation:
    window_end = window_start + timedelta(days=horizon_days)
    is_v3 = policy_version == FOLLOWUP_POLICY_V3
    observation_id = (
        f"{decision.decision_id}-{horizon_kind.value}-{baseline_kind.value}-{policy_version}"
    )
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=venue,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=window_start,
        target_time=window_end,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id=product.split("-")[0],
        subject_product_or_market_id=product,
        provenance=EvidenceClass.HISTORICAL_PUBLIC,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        evidence_eligibility_policy_version="CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1",
        observed_at=GENERATED_AT,
        source_lineage=("batch-start", "batch-end"),
        data_limitations=(),
        reference_price_at_baseline=Decimal("100"),
        reference_price_at_target=(
            None
            if subsequent_return is None
            else (Decimal("100") * (Decimal(1) + subsequent_return))
        ),
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        measurement_as_of=GENERATED_AT if is_v3 else None,
        policy_anchor_at=(
            (policy_anchor_at if policy_anchor_at is not None else window_start) if is_v3 else None
        ),
        decision_input_reference_price=Decimal("99") if is_v3 else None,
        decision_input_reference_bucket_start=(window_start - timedelta(days=2) if is_v3 else None),
        decision_input_source_observation_id="batch-decision-input" if is_v3 else None,
        decision_input_evidence_class=EvidenceClass.HISTORICAL_PUBLIC if is_v3 else None,
        canonical_hash=content_digest((observation_id, subsequent_return, window_start)),
        authority="NONE",
    )


def _assessment(
    observation: DecisionFollowUpObservation, *, eligible: bool = True
) -> FollowUpEvidenceQualityAssessment:
    return FollowUpEvidenceQualityAssessment(
        assessment_id=f"{observation.observation_id}-assessment",
        observation_id=observation.observation_id,
        observation_canonical_hash=observation.canonical_hash,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        baseline_evidence_class=observation.baseline_evidence_class,
        target_evidence_class=observation.target_evidence_class,
        verdict=(
            FollowUpEvidenceQualityVerdict.ELIGIBLE
            if eligible
            else FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
        ),
        reason_code="TEST",
        evidence_defect_type=(
            FollowUpEvidenceDefectType.NONE
            if eligible
            else FollowUpEvidenceDefectType.TARGET_EVIDENCE_INELIGIBLE
        ),
        assessed_at=GENERATED_AT,
        canonical_hash=f"{observation.observation_id}-assessment".ljust(64, "0")[:64],
        authority="NONE",
    )


class World:
    """A small in-memory corpus builder mirroring exactly what the
    application layer hands ``build_counterfactual_outcome_aggregates``."""

    def __init__(self) -> None:
        self.decisions: list[CounterfactualDecision] = []
        self.classifications: list[CounterfactualDecisionClassification] = []
        self.observations: list[DecisionFollowUpObservation] = []
        self.assessments: list[FollowUpEvidenceQualityAssessment] = []
        self.subject_facts: dict[str, SubjectProjectionFacts] = {}

    def add(
        self,
        *,
        product: str,
        day: datetime,
        families: tuple[ReasonFamily, ...],
        subsequent_return: Decimal | None,
        method: str = METHOD,
        horizon_days: int = 1,
        origin: DecisionOrigin = DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        baseline_kind: FollowUpBaselineKind = FollowUpBaselineKind.ORIGIN_EVIDENCE,
        horizon_kind: FollowUpHorizonKind = FollowUpHorizonKind.SHORT_HORIZON,
        with_observation: bool = True,
        eligible: bool = True,
        policy_version: str = FOLLOWUP_POLICY_V3,
        evaluated_at: datetime | None = None,
        decision_suffix: str = "",
    ) -> CounterfactualDecision:
        stamp = day.strftime("%Y%m%d")
        decision_id = f"d-{method}-{origin.value}-{product}-{stamp}{decision_suffix}"
        projection_id = f"p-{method}-{product}-{stamp}{decision_suffix}"
        evaluated = evaluated_at if evaluated_at is not None else day + timedelta(seconds=20)
        decision = _decision(
            decision_id=decision_id,
            projection_id=projection_id,
            evaluated_at=evaluated,
            origin=origin,
        )
        self.decisions.append(decision)
        self.classifications.append(_classification(decision_id, families))
        self.subject_facts[projection_id] = SubjectProjectionFacts(
            strategy_or_method_id=method, product_or_market_id=product, generated_at=day
        )
        if with_observation:
            window_start = (
                day
                if baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE
                else day + timedelta(days=1)
            )
            observation = _v3_observation(
                decision=decision,
                product=product,
                window_start=window_start,
                horizon_days=horizon_days,
                subsequent_return=subsequent_return,
                horizon_kind=horizon_kind,
                baseline_kind=baseline_kind,
                policy_version=policy_version,
            )
            self.observations.append(observation)
            self.assessments.append(_assessment(observation, eligible=eligible))
        return decision

    def build(
        self, *, outcome_policy_version: str = COUNTERFACTUAL_OUTCOME_POLICY_V3
    ) -> tuple[CounterfactualOutcomeAggregate, ...]:
        return build_counterfactual_outcome_aggregates(
            decisions=tuple(self.decisions),
            classifications=tuple(self.classifications),
            observations=tuple(self.observations),
            assessments=tuple(self.assessments),
            strategy_or_method_id_by_projection={
                key: value.strategy_or_method_id for key, value in self.subject_facts.items()
            },
            run_id=RUN_ID,
            generated_at=GENERATED_AT,
            outcome_policy_version=outcome_policy_version,
            subject_facts_by_projection=dict(self.subject_facts),
        )


def _only(aggregates: tuple[CounterfactualOutcomeAggregate, ...], **kwargs: object):
    matches = [
        item
        for item in aggregates
        if all(getattr(item, key) == value for key, value in kwargs.items())
    ]
    assert len(matches) == 1, f"expected exactly one match for {kwargs}, got {len(matches)}"
    return matches[0]


def _days(count: int, *, start: datetime = DAY0) -> list[datetime]:
    return [start + timedelta(days=index) for index in range(count)]


def _fill(
    world: World,
    *,
    days: list[datetime],
    cohort_products: tuple[str, ...],
    cohort_return: Decimal,
    complement_return: Decimal,
    cohort_families: tuple[ReasonFamily, ...] = (ReasonFamily.ECONOMICS,),
    complement_families: tuple[ReasonFamily, ...] = (),
    horizon_days: int = 1,
) -> None:
    for day in days:
        for product in PRODUCTS:
            in_cohort = product in cohort_products
            world.add(
                product=product,
                day=day,
                families=cohort_families if in_cohort else complement_families,
                subsequent_return=cohort_return if in_cohort else complement_return,
                horizon_days=horizon_days,
            )


def _specificity(aggregate: CounterfactualOutcomeAggregate):
    assert aggregate.specificity is not None
    return aggregate.specificity


# ---------------------------------------------------------------------------
# Market beta is not edge
# ---------------------------------------------------------------------------


def test_market_rally_where_the_cohort_merely_tracks_its_complement_is_zero_spread() -> None:
    world = World()
    _fill(
        world,
        days=_days(35),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.05"),
        complement_return=Decimal("0.05"),
    )
    aggregate = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    spec = _specificity(aggregate)
    assert aggregate.qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED
    assert spec.independent_window_count == 35
    assert spec.mean_relative_window_return == Decimal("0")
    assert spec.relative_positive_window_rate == Decimal("0")
    # The raw economics are not hidden: the rally is visible as descriptive.
    assert spec.descriptive_mean_raw_return == Decimal("0.05")
    assert spec.descriptive_mean_complement_return == Decimal("0.05")
    # And the legacy absolute fields are deliberately absent under V3.
    assert aggregate.mean_subsequent_return is None
    assert aggregate.positive_rate is None
    assert "PEER-RELATIVE, WINDOW-LEVEL" in aggregate.explanation


def test_cohort_genuinely_outperforming_its_complement_is_positive_spread() -> None:
    world = World()
    _fill(
        world,
        days=_days(31),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.03"),
        complement_return=Decimal("-0.01"),
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.mean_relative_window_return == Decimal("0.04")
    assert spec.relative_positive_window_rate == Decimal("1")
    assert spec.relative_window_return_standard_deviation == Decimal("0")


def test_cohort_underperforming_its_complement_is_negative_spread() -> None:
    world = World()
    _fill(
        world,
        days=_days(31),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("-0.02"),
        complement_return=Decimal("0.01"),
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.mean_relative_window_return == Decimal("-0.03")
    assert spec.relative_positive_window_rate == Decimal("0")
    assert spec.relative_negative_window_count == 31


# ---------------------------------------------------------------------------
# One economic move is one sample
# ---------------------------------------------------------------------------


def test_many_assets_in_one_window_collapse_to_one_independent_sample() -> None:
    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.raw_return_observation_count == 2
    assert spec.raw_window_count == 1
    assert spec.independent_window_count == 1


def test_large_raw_count_from_overlapping_strategy_windows_does_not_qualify() -> None:
    """14-day windows anchored on 40 consecutive days: 80 raw cohort
    observations, 40 raw windows, but only the fixed-phase grid slots
    (every 14th day) are independent -- far below the floor."""

    world = World()
    _fill(
        world,
        days=_days(40),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.10"),
        complement_return=Decimal("0.00"),
        horizon_days=14,
    )
    aggregate = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    spec = _specificity(aggregate)
    assert spec.raw_return_observation_count == 80
    assert spec.raw_window_count == 40
    assert spec.independent_window_count <= 3
    assert spec.off_grid_window_count == spec.raw_window_count - spec.independent_window_count
    assert aggregate.qualification_state is (
        CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    # The overlapping evidence is still visible, ungated and labelled.
    assert spec.descriptive_informative_window_count == 40
    assert spec.descriptive_mean_window_spread == Decimal("0.10")


def test_non_overlapping_daily_windows_are_each_independent() -> None:
    world = World()
    _fill(
        world,
        days=_days(10),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.independent_window_count == 10
    assert spec.off_grid_window_count == 0
    assert spec.window_horizon_days == 1
    assert len(spec.independent_window_keys) == 10
    assert spec.independent_window_keys[0].startswith("2026-08-04T00:00:00Z/2026-08-05T00:00:00Z")
    assert spec.independent_window_keys[0].endswith(":cohort=2:complement=3")


def test_grid_tiling_accepts_exactly_one_window_per_horizon_slot() -> None:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert window_is_on_independent_grid(window_start=epoch, horizon_days=14)
    assert window_is_on_independent_grid(window_start=epoch + timedelta(days=28), horizon_days=14)
    assert not window_is_on_independent_grid(
        window_start=epoch + timedelta(days=29), horizon_days=14
    )
    assert not window_is_on_independent_grid(
        window_start=epoch + timedelta(hours=3), horizon_days=1
    )
    assert not window_is_on_independent_grid(window_start=epoch, horizon_days=0)
    assert all(
        window_is_on_independent_grid(window_start=epoch + timedelta(days=n), horizon_days=1)
        for n in range(0, 40)
    )


def test_grid_acceptance_is_monotone_under_evidence_growth() -> None:
    """A late-arriving older window never re-phases the accepted set --
    the property greedy interval scheduling lacked."""

    world = World()
    later_days = _days(20, start=DAY0 + timedelta(days=14))
    _fill(
        world,
        days=later_days,
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.00"),
        horizon_days=14,
    )
    before = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    _fill(
        world,
        days=_days(14),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.00"),
        horizon_days=14,
    )
    after = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert set(before.independent_window_keys) <= set(after.independent_window_keys)
    assert after.independent_window_count >= before.independent_window_count


# ---------------------------------------------------------------------------
# Replay determinism
# ---------------------------------------------------------------------------


def test_duplicate_and_reordered_inputs_produce_byte_identical_aggregates() -> None:
    world = World()
    _fill(
        world,
        days=_days(5),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    baseline = world.build()
    shuffled = World()
    shuffled.decisions = list(reversed(world.decisions))
    shuffled.classifications = list(reversed(world.classifications))
    shuffled.observations = list(reversed(world.observations)) + world.observations[:3]
    shuffled.assessments = list(reversed(world.assessments)) + world.assessments[:3]
    shuffled.subject_facts = dict(reversed(list(world.subject_facts.items())))
    replayed = shuffled.build()
    assert [item.canonical_hash for item in baseline] == [item.canonical_hash for item in replayed]
    assert [item.specificity for item in baseline] == [item.specificity for item in replayed]


def test_replay_at_a_different_wall_clock_instant_is_byte_identical() -> None:
    world = World()
    _fill(
        world,
        days=_days(3),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    first = world.build()
    second = build_counterfactual_outcome_aggregates(
        decisions=tuple(world.decisions),
        classifications=tuple(world.classifications),
        observations=tuple(world.observations),
        assessments=tuple(world.assessments),
        strategy_or_method_id_by_projection={
            key: value.strategy_or_method_id for key, value in world.subject_facts.items()
        },
        run_id=RUN_ID,
        generated_at=GENERATED_AT + timedelta(hours=5),
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        subject_facts_by_projection=dict(world.subject_facts),
    )
    assert [item.canonical_hash for item in first] == [item.canonical_hash for item in second]


# ---------------------------------------------------------------------------
# Canonical gate attribution
# ---------------------------------------------------------------------------


def test_precedence_is_a_permutation_of_every_reason_family() -> None:
    assert sorted(CANONICAL_REASON_FAMILY_PRECEDENCE_V1) == sorted(ReasonFamily)
    assert len(set(CANONICAL_REASON_FAMILY_PRECEDENCE_V1)) == len(ReasonFamily)


def test_canonical_family_is_deterministic_and_input_order_independent() -> None:
    families = (
        ReasonFamily.ECONOMICS,
        ReasonFamily.RESEARCH_CONFIDENCE,
        ReasonFamily.OTHER_EXPLICIT,
    )
    assert canonical_reason_family(families) is ReasonFamily.RESEARCH_CONFIDENCE
    assert canonical_reason_family(tuple(reversed(families))) is ReasonFamily.RESEARCH_CONFIDENCE
    assert canonical_reason_family(()) is None
    assert canonical_reason_family((ReasonFamily.OTHER_EXPLICIT,)) is ReasonFamily.OTHER_EXPLICIT


def test_a_decision_with_several_true_gates_counts_toward_exactly_one_cohort() -> None:
    """Under V1 the identical outcome would appear in ECONOMICS,
    RESEARCH_CONFIDENCE and OTHER_EXPLICIT; under V3 only in the canonical
    (most upstream) one, and the collapsed fan-out is disclosed."""

    world = World()
    multi = (ReasonFamily.ECONOMICS, ReasonFamily.RESEARCH_CONFIDENCE, ReasonFamily.OTHER_EXPLICIT)
    for day in _days(3):
        for index, product in enumerate(PRODUCTS):
            world.add(
                product=product,
                day=day,
                families=multi if index == 0 else (),
                subsequent_return=Decimal("0.01"),
            )
    v3 = world.build()
    assert {item.reason_family for item in v3} == {ReasonFamily.RESEARCH_CONFIDENCE}
    confidence = _only(v3, reason_family=ReasonFamily.RESEARCH_CONFIDENCE)
    assert confidence.total_observations_considered == 3
    assert _specificity(confidence).multi_family_decision_count == 3
    v1 = world.build(outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1)
    assert {item.reason_family for item in v1} == set(multi)


def test_precedence_never_defines_the_complement() -> None:
    """A product carrying the cohort's family but attributed upstream is in
    neither side, so precedence cannot introduce outcome-correlated bias
    through the counterfactual side."""

    world = World()
    for day in _days(2):
        # Two products: rejected purely on ECONOMICS.
        for product in PRODUCTS[:2]:
            world.add(
                product=product,
                day=day,
                families=(ReasonFamily.ECONOMICS,),
                subsequent_return=Decimal("0.05"),
            )
        # One product: carries ECONOMICS but is attributed to STALE.
        world.add(
            product=PRODUCTS[2],
            day=day,
            families=(ReasonFamily.ECONOMICS, ReasonFamily.STALE),
            subsequent_return=Decimal("0.50"),
        )
        # Two gate-passers.
        for product in PRODUCTS[3:]:
            world.add(product=product, day=day, families=(), subsequent_return=Decimal("0.01"))
    economics = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    # Complement has only the two gate-passers (< 3): every window is
    # degenerate, and the STALE-attributed product's +50% never leaked into
    # either side.
    assert economics.degenerate_window_count == 2
    assert economics.independent_window_count == 0
    assert economics.descriptive_informative_window_count == 0


def test_unattributed_gate_passers_join_the_complement_and_are_counted() -> None:
    world = World()
    _fill(
        world,
        days=_days(2),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.method_unattributed_decision_count == 8
    assert spec.independent_window_keys[0].endswith(":cohort=1:complement=4")


# ---------------------------------------------------------------------------
# Degenerate, inconsistent, mixed, heterogeneous, mismatched
# ---------------------------------------------------------------------------


def test_a_gate_that_rejects_the_whole_universe_has_no_informative_window() -> None:
    world = World()
    _fill(
        world,
        days=_days(40),
        cohort_products=PRODUCTS,
        cohort_return=Decimal("0.30"),
        complement_return=Decimal("0"),
    )
    aggregate = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    spec = _specificity(aggregate)
    assert spec.raw_return_observation_count == 200
    assert spec.degenerate_window_count == 40
    assert spec.independent_window_count == 0
    assert aggregate.qualification_state is (
        CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert "40 degenerate" in aggregate.explanation
    # Raw beta still visible, never presented as a qualified statistic.
    assert spec.descriptive_mean_raw_return == Decimal("0.30")
    assert spec.mean_relative_window_return is None


def test_complement_below_minimum_peer_products_is_degenerate() -> None:
    world = World()
    cohort = PRODUCTS[: len(PRODUCTS) - SPECIFICITY_MINIMUM_PEER_PRODUCTS + 1]
    _fill(
        world,
        days=_days(2),
        cohort_products=cohort,
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.degenerate_window_count == 2
    assert spec.independent_window_count == 0


def test_conflicting_product_return_in_one_window_voids_that_window_only() -> None:
    world = World()
    _fill(
        world,
        days=_days(3),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    # A second decision (other method) about a complement product on day 0
    # retains a *different* return over the identical window.
    world.add(
        product=PRODUCTS[3],
        day=DAY0,
        families=(),
        subsequent_return=Decimal("0.99"),
        method=OTHER_METHOD,
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.inconsistent_window_count == 1
    assert spec.independent_window_count == 2


def test_mixed_origin_window_is_excluded() -> None:
    world = World()
    _fill(
        world,
        days=_days(2),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    # A mandate-origin decision about a sixth product, attributed to the
    # same family, lands in the same cohort and the same window.
    world.add(
        product="LINK-USD",
        day=DAY0,
        families=(ReasonFamily.ECONOMICS,),
        subsequent_return=Decimal("0.02"),
        origin=DecisionOrigin.INVESTMENT_MANDATE_EVALUATION,
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.inconsistent_window_count == 0
    assert spec.mixed_origin_window_count == 1
    assert spec.independent_window_count == 1


def test_heterogeneous_horizon_days_within_a_cohort_fails_closed() -> None:
    world = World()
    _fill(
        world,
        days=_days(3),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
        horizon_days=14,
    )
    _fill(
        world,
        days=_days(3, start=DAY0 + timedelta(days=60)),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
        horizon_days=5,
    )
    aggregate = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    spec = _specificity(aggregate)
    assert spec.horizon_days_heterogeneous is True
    assert spec.window_horizon_days is None
    assert spec.independent_window_count == 0
    assert "HORIZON_DAYS_HETEROGENEOUS" in aggregate.explanation


def test_observation_whose_window_disagrees_with_its_decision_anchor_is_excluded() -> None:
    world = World()
    _fill(
        world,
        days=_days(2),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    tampered = replace(
        world.observations[0],
        baseline_time=world.observations[0].baseline_time + timedelta(days=3),
        target_time=world.observations[0].target_time + timedelta(days=3),
    )
    world.observations[0] = tampered
    world.assessments[0] = _assessment(tampered)
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.anchor_mismatch_observation_count == 1
    assert spec.independent_window_count == 1


# ---------------------------------------------------------------------------
# Peer coverage, forward-time boundary, quarantine
# ---------------------------------------------------------------------------


def test_a_window_with_an_unobserved_peer_awaits_completion_then_counts() -> None:
    """A cohort can never first qualify on a partially-drained backlog."""

    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    # A fifth... a sixth product had a decision that day but no V3 row yet.
    world.add(
        product="LINK-USD", day=DAY0, families=(), subsequent_return=None, with_observation=False
    )
    before = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert before.awaiting_peer_completion_window_count == 1
    assert before.products_with_decision_but_no_return_count == 1
    assert before.independent_window_count == 0
    # The spread is still visible descriptively while the window waits.
    assert before.descriptive_informative_window_count == 1
    # The backlog drains: the same window now counts.
    world.decisions = [item for item in world.decisions if "LINK" not in item.decision_id]
    world.classifications = [
        item for item in world.classifications if "LINK" not in item.decision_id
    ]
    world.add(product="LINK-USD", day=DAY0, families=(), subsequent_return=Decimal("0.01"))
    after = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert after.awaiting_peer_completion_window_count == 0
    assert after.independent_window_count == 1


def test_peer_reference_never_reads_a_sibling_beyond_the_windows_own_end() -> None:
    """A 30-day observation of a complement product anchored the same day
    must not enter a 1-day window's complement -- the only inputs are rows
    over the identical (W0, W1)."""

    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    long_horizon = world.add(
        product=PRODUCTS[4],
        day=DAY0,
        families=(),
        subsequent_return=Decimal("5.00"),
        horizon_days=30,
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        decision_suffix="-strategy",
    )
    assert long_horizon.decision_id.endswith("-strategy")
    short = _specificity(
        _only(
            world.build(),
            reason_family=ReasonFamily.ECONOMICS,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        )
    )
    assert short.descriptive_mean_complement_return == Decimal("0.01")
    assert short.mean_relative_window_return is None  # 1 window, not qualified
    assert short.descriptive_mean_window_spread == Decimal("0.01")


def test_quarantined_crypto_v2_observations_never_enter_a_v3_build() -> None:
    world = World()
    _fill(
        world,
        days=_days(2),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    for product in PRODUCTS:
        world.add(
            product=product,
            day=DAY0 + timedelta(days=10),
            families=(ReasonFamily.ECONOMICS,),
            subsequent_return=Decimal("9"),
            policy_version=FOLLOWUP_POLICY_V2,
            decision_suffix="-v2",
        )
    aggregates = world.build()
    assert {item.followup_policy_version for item in aggregates} == {FOLLOWUP_POLICY_V3}
    spec = _specificity(_only(aggregates, reason_family=ReasonFamily.ECONOMICS))
    assert spec.raw_return_observation_count == 2


def test_v3_scope_is_crypto_only_and_requires_subject_facts() -> None:
    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    kalshi = _decision(
        decision_id="kalshi-1",
        projection_id="kalshi-projection",
        evaluated_at=DAY0,
        venue=SharedVenue.KALSHI,
    )
    world.decisions.append(kalshi)
    world.classifications.append(_classification("kalshi-1", (ReasonFamily.LIQUIDITY,)))
    world.subject_facts["kalshi-projection"] = SubjectProjectionFacts(
        strategy_or_method_id="KALSHI_METHOD", product_or_market_id="KX-1", generated_at=DAY0
    )
    aggregates = world.build()
    assert {item.venue for item in aggregates} == {SharedVenue.CRYPTO}
    assert all(
        item.outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3 for item in aggregates
    )
    with pytest.raises(CounterfactualOutcomeError, match="SUBJECT_FACTS_REQUIRED"):
        build_counterfactual_outcome_aggregates(
            decisions=tuple(world.decisions),
            classifications=tuple(world.classifications),
            observations=tuple(world.observations),
            assessments=tuple(world.assessments),
            strategy_or_method_id_by_projection={
                key: value.strategy_or_method_id for key, value in world.subject_facts.items()
            },
            run_id=RUN_ID,
            generated_at=GENERATED_AT,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        )


# ---------------------------------------------------------------------------
# Qualification floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("windows", "expected"),
    [
        (
            COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS - 1,
            CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE,
        ),
        (
            COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS,
            CounterfactualOutcomeQualificationState.QUALIFIED,
        ),
        (
            COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS + 1,
            CounterfactualOutcomeQualificationState.QUALIFIED,
        ),
    ],
)
def test_qualification_boundary_is_counted_in_independent_windows(
    windows: int, expected: CounterfactualOutcomeQualificationState
) -> None:
    world = World()
    _fill(
        world,
        days=_days(windows),
        cohort_products=PRODUCTS[:2],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    aggregate = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    assert _specificity(aggregate).independent_window_count == windows
    assert aggregate.qualification_state is expected
    if expected is CounterfactualOutcomeQualificationState.QUALIFIED:
        assert _specificity(aggregate).mean_relative_window_return == Decimal("0.01")
        assert len(aggregate.contributing_decision_ids) == windows * 2
    else:
        assert _specificity(aggregate).mean_relative_window_return is None


def test_floor_is_the_existing_thirty_and_the_policy_hash_pins_it() -> None:
    assert COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS == 30
    assert len(COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH) == 64


# ---------------------------------------------------------------------------
# Historical compatibility
# ---------------------------------------------------------------------------


def test_v1_and_v2_aggregates_carry_no_specificity_and_hash_exactly_as_before() -> None:
    world = World()
    _fill(
        world,
        days=_days(2),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    v1 = _only(
        world.build(outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1),
        reason_family=ReasonFamily.ECONOMICS,
    )
    assert v1.specificity is None
    legacy_payload = (
        v1.outcome_policy_version,
        v1.aggregate_id,
        v1.cohort_id,
        v1.run_id,
        v1.venue.value,
        v1.strategy_or_method_id,
        v1.reason_family.value,
        v1.horizon_kind.value,
        v1.baseline_kind.value,
        v1.followup_policy_version,
        v1.classifier_version,
        v1.evidence_quality_policy_version,
        v1.total_observations_considered,
        v1.mature_observation_count,
        v1.eligible_observation_count,
        v1.evidence_ineligible_observation_count,
        v1.unassessed_observation_count,
        v1.unavailable_observation_count,
        v1.positive_outcome_count,
        v1.negative_outcome_count,
        v1.zero_outcome_count,
        v1.mean_subsequent_return,
        v1.median_subsequent_return,
        v1.positive_rate,
        v1.qualification_state.value,
        v1.contributing_decision_ids,
        v1.explanation,
        "NONE",
    )
    assert v1.canonical_hash == content_digest(legacy_payload)
    assert world.build(outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2) == ()


def test_every_v3_aggregate_carries_specificity_and_the_hash_block() -> None:
    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    for aggregate in world.build():
        assert aggregate.specificity is not None
        assert aggregate.specificity.policy_hash == COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH
        without_block = counterfactual_outcome_aggregate_canonical_hash(
            aggregate_id=aggregate.aggregate_id,
            cohort_id=aggregate.cohort_id,
            outcome_policy_version=aggregate.outcome_policy_version,
            run_id=aggregate.run_id,
            venue=aggregate.venue.value,
            strategy_or_method_id=aggregate.strategy_or_method_id,
            reason_family=aggregate.reason_family.value,
            horizon_kind=aggregate.horizon_kind.value,
            baseline_kind=aggregate.baseline_kind.value,
            followup_policy_version=aggregate.followup_policy_version,
            classifier_version=aggregate.classifier_version,
            evidence_quality_policy_version=aggregate.evidence_quality_policy_version,
            total_observations_considered=aggregate.total_observations_considered,
            mature_observation_count=aggregate.mature_observation_count,
            eligible_observation_count=aggregate.eligible_observation_count,
            evidence_ineligible_observation_count=aggregate.evidence_ineligible_observation_count,
            unassessed_observation_count=aggregate.unassessed_observation_count,
            unavailable_observation_count=aggregate.unavailable_observation_count,
            positive_outcome_count=aggregate.positive_outcome_count,
            negative_outcome_count=aggregate.negative_outcome_count,
            zero_outcome_count=aggregate.zero_outcome_count,
            mean_subsequent_return=aggregate.mean_subsequent_return,
            median_subsequent_return=aggregate.median_subsequent_return,
            positive_rate=aggregate.positive_rate,
            qualification_state=aggregate.qualification_state.value,
            contributing_decision_ids=aggregate.contributing_decision_ids,
            explanation=aggregate.explanation,
        )
        assert without_block != aggregate.canonical_hash


def test_v3_cohort_ids_never_collide_with_v1_cohort_ids() -> None:
    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    v1_ids = {
        item.cohort_id
        for item in world.build(outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1)
    }
    v3_ids = {item.cohort_id for item in world.build()}
    assert v1_ids and v3_ids and not (v1_ids & v3_ids)


# ---------------------------------------------------------------------------
# Research sourcing
# ---------------------------------------------------------------------------


def test_crypto_outcome_policy_v1_is_quarantined_from_sourcing_new_research() -> None:
    assert not outcome_policy_version_may_source_new_research(
        venue=SharedVenue.CRYPTO, outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1
    )
    assert outcome_policy_version_may_source_new_research(
        venue=SharedVenue.CRYPTO, outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3
    )
    for venue in (SharedVenue.KALSHI, SharedVenue.SECURITIES):
        for version in (COUNTERFACTUAL_OUTCOME_POLICY_V1, COUNTERFACTUAL_OUTCOME_POLICY_V2):
            assert outcome_policy_version_may_source_new_research(
                venue=venue, outcome_policy_version=version
            )
    for defect in DEFECTIVE_OUTCOME_POLICY_VERSIONS:
        assert defect.corrected_by_outcome_policy_version != defect.outcome_policy_version


def test_sourcing_exclusion_consults_all_three_gates_in_order() -> None:
    followup = counterfactual_outcome_sourcing_exclusion(
        venue=SharedVenue.CRYPTO,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        followup_policy_version=FOLLOWUP_POLICY_V2,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    assert followup is not None and followup.corrected_by == FOLLOWUP_POLICY_V3
    outcome = counterfactual_outcome_sourcing_exclusion(
        venue=SharedVenue.CRYPTO,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        followup_policy_version=FOLLOWUP_POLICY_V3,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    assert outcome is not None and outcome.corrected_by == COUNTERFACTUAL_OUTCOME_POLICY_V3
    lagged = counterfactual_outcome_sourcing_exclusion(
        venue=SharedVenue.CRYPTO,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        followup_policy_version=FOLLOWUP_POLICY_V3,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert lagged is not None
    assert lagged.reason_code == V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING
    assert (
        counterfactual_outcome_sourcing_exclusion(
            venue=SharedVenue.CRYPTO,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
            followup_policy_version=FOLLOWUP_POLICY_V3,
            baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
        )
        is None
    )
    # KALSHI V2 (binary-complement reduction) still sources normally.
    assert (
        counterfactual_outcome_sourcing_exclusion(
            venue=SharedVenue.KALSHI,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V2,
            followup_policy_version=FOLLOWUP_POLICY_V2,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        )
        is None
    )
    # CRYPTO V1 follow-up evidence stays quarantined regardless of outcome policy.
    assert (
        counterfactual_outcome_sourcing_exclusion(
            venue=SharedVenue.CRYPTO,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
            followup_policy_version=FOLLOWUP_POLICY_V1,
            baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Same-day maturation guard
# ---------------------------------------------------------------------------


def test_benign_maturation_guard_requires_v3_counts_to_be_non_decreasing() -> None:
    world = World()
    _fill(
        world,
        days=_days(2),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    grown = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    spec = _specificity(grown)
    shrunk = replace(
        grown,
        specificity=replace(spec, independent_window_count=spec.independent_window_count - 1),
    )
    assert _is_benign_evidence_maturation(existing=shrunk, fresh=grown)
    assert not _is_benign_evidence_maturation(existing=grown, fresh=shrunk)
    assert not _is_benign_evidence_maturation(
        existing=replace(grown, specificity=None), fresh=grown
    )


# ---------------------------------------------------------------------------
# The shape production actually has: several decisions per product per W0
# ---------------------------------------------------------------------------


def test_several_same_day_decisions_about_one_product_are_one_cohort_product() -> None:
    world = World()
    days = _days(2)
    for day in days:
        for product in PRODUCTS[2:]:
            world.add(product=product, day=day, families=(), subsequent_return=Decimal("0.01"))
        for suffix in ("", "-second", "-third"):
            world.add(
                product=PRODUCTS[0],
                day=day,
                families=(ReasonFamily.ECONOMICS,),
                subsequent_return=Decimal("0.02"),
                decision_suffix=suffix,
            )
    aggregate = _only(world.build(), reason_family=ReasonFamily.ECONOMICS)
    spec = _specificity(aggregate)
    assert spec.raw_return_observation_count == 6
    assert spec.raw_window_count == 2
    assert spec.independent_window_count == 2
    assert all(key.endswith(":cohort=1:complement=3") for key in spec.independent_window_keys)
    assert spec.descriptive_mean_window_spread == Decimal("0.01")
    assert len(aggregate.contributing_decision_ids) == 6


def test_a_product_on_both_sides_of_one_window_is_removed_from_both_and_counted() -> None:
    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    # A second decision about a complement product, attributed to ECONOMICS,
    # over the identical window and with the identical return.
    world.add(
        product=PRODUCTS[1],
        day=DAY0,
        families=(ReasonFamily.ECONOMICS,),
        subsequent_return=Decimal("0.01"),
        decision_suffix="-conflict",
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.membership_conflict_product_count == 1
    assert spec.inconsistent_window_count == 0
    assert spec.independent_window_count == 1
    assert spec.independent_window_keys[0].endswith(":cohort=1:complement=3")


def test_an_evidence_ineligible_complement_peer_leaves_the_window_awaiting() -> None:
    world = World()
    _fill(
        world,
        days=_days(1),
        cohort_products=PRODUCTS[:1],
        cohort_return=Decimal("0.02"),
        complement_return=Decimal("0.01"),
    )
    world.add(
        product="LINK-USD",
        day=DAY0,
        families=(),
        subsequent_return=Decimal("0.01"),
        eligible=False,
    )
    spec = _specificity(_only(world.build(), reason_family=ReasonFamily.ECONOMICS))
    assert spec.awaiting_peer_completion_window_count == 1
    assert spec.products_with_decision_but_no_return_count == 1
    assert spec.independent_window_count == 0


def test_decision_time_windows_anchor_and_accept_but_never_source_research() -> None:
    world = World()
    for day in _days(3):
        for product in PRODUCTS:
            world.add(
                product=product,
                day=day,
                families=(ReasonFamily.ECONOMICS,) if product == PRODUCTS[0] else (),
                subsequent_return=Decimal("0.02") if product == PRODUCTS[0] else Decimal("0.01"),
                baseline_kind=FollowUpBaselineKind.DECISION_TIME,
            )
            world.add(
                product=product,
                day=day,
                families=(ReasonFamily.ECONOMICS,) if product == PRODUCTS[0] else (),
                subsequent_return=Decimal("0.02") if product == PRODUCTS[0] else Decimal("0.01"),
                baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
                decision_suffix="-origin",
            )
    aggregates = world.build()
    decision_time = _only(
        aggregates,
        reason_family=ReasonFamily.ECONOMICS,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    origin = _only(
        aggregates,
        reason_family=ReasonFamily.ECONOMICS,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    for aggregate in (decision_time, origin):
        spec = _specificity(aggregate)
        assert spec.anchor_mismatch_observation_count == 0
        assert spec.independent_window_count == 3
    assert (
        _specificity(decision_time)
        .independent_window_keys[0]
        .startswith("2026-08-05T00:00:00Z/2026-08-06T00:00:00Z")
    )
    assert (
        _specificity(origin)
        .independent_window_keys[0]
        .startswith("2026-08-04T00:00:00Z/2026-08-05T00:00:00Z")
    )
    for aggregate, expect_sourced in ((decision_time, False), (origin, True)):
        exclusion = counterfactual_outcome_sourcing_exclusion(
            venue=aggregate.venue,
            outcome_policy_version=aggregate.outcome_policy_version,
            followup_policy_version=aggregate.followup_policy_version,
            baseline_kind=aggregate.baseline_kind,
        )
        assert (exclusion is None) is expect_sourced
