"""Securities Follow-Up Temporal Integrity v1 -- offline proofs.

Fixtures reproduce the exact shape of the production rows found in the
reconstruction (docs/SECURITIES_FOLLOWUP_TEMPORAL_INTEGRITY_V1.md): SECURITIES
decisions evaluated at the day-scoped cycle instant ``00:00:54Z``, closing
chains captured at ``20:00:16Z``, and outage-era targets (2026-09-05..09-12)
resolved by the 2026-09-21 snapshot.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.counterfactual_followup import (
    AssessFollowUpEvidenceQuality,
    SyncFollowUpObservations,
)
from apex.domain.counterfactual_followup import (
    CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    FOLLOWUP_POLICY_V2,
    CounterfactualFollowUpError,
    DecisionFollowUpObservation,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    SecuritiesTerminalEvidenceTiming,
    SecuritiesTerminalTemporalStatus,
    assess_followup_evidence_quality,
    build_securities_structure_terminal_observation,
    current_followup_evidence_quality_policy_version,
    evidence_quality_assessment_canonical_hash,
    followup_evidence_quality_policy_supports_venue,
    is_evidence_eligible_for_downstream_attribution,
    resolve_securities_terminal_evidence_timing,
    securities_terminal_snapshot_id,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.options_research_operations import OptionsCaptureWindowPolicy
from apex.domain.options_structures import StructureType
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities_structure_outcome import (
    CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION,
    build_securities_structure_outcome_aggregates,
)
from apex.domain.shared_opportunity import SharedVenue
from tests.test_securities_shared_opportunity_bridge import (
    NOW as BRIDGE_NOW,
)
from tests.test_securities_shared_opportunity_bridge import (
    _due_securities_decision,
    _FakeDecisionSource,
    _FakeRepository,
    _FakeSecuritiesPrices,
    _FakeSubjectSource,
    _RaisingKalshiSource,
)

WINDOW_POLICY = OptionsCaptureWindowPolicy.create()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _sessions(target: datetime) -> tuple:  # type: ignore[type-arg]
    return ReviewedXNYSCalendar(
        start_date=(target - timedelta(days=10)).date().isoformat(),
        end_date=(target + timedelta(days=10)).date().isoformat(),
    ).sessions


def _decision(decision_id: str, evaluated_at: datetime) -> CounterfactualDecision:
    return CounterfactualDecision(
        decision_id=decision_id,
        origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        origin_decision_id=f"{decision_id}-origin",
        origin_canonical_hash="a" * 64,
        origin_state="ELIGIBLE",
        subject_projection_id=f"projection-{decision_id}",
        subject_source_record_type="OptionStructureCandidate",
        subject_source_record_id=f"structure-{decision_id}",
        venue=SharedVenue.SECURITIES,
        opportunity_set_id="set-1",
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=evaluated_at,
        disposition=DecisionDisposition.ADVANCED,
        raw_reason_codes=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
        explanation="production-shaped fixture",
        canonical_hash="b" * 64,
        authority="NONE",
    )


def _observation(
    *,
    decision_id: str,
    evaluated_at: str,
    horizon_days: int,
    terminal_snapshot_id: str,
    horizon_kind: FollowUpHorizonKind = FollowUpHorizonKind.SHORT_HORIZON,
    terminal_price: Decimal = Decimal("712"),
) -> DecisionFollowUpObservation:
    """Built by the real builder, so its canonical hash is genuine."""

    decision = _decision(decision_id, _at(evaluated_at))
    return build_securities_structure_terminal_observation(
        decision=decision,
        horizon_kind=horizon_kind,
        horizon_days=horizon_days,
        target_time=decision.evaluated_at + timedelta(days=horizon_days),
        subject_instrument_id="apex_stable_security_spy",
        subject_product_or_market_id=f"structure-{decision_id}",
        structure_type=StructureType.CALL_CREDIT_VERTICAL,
        long_strike=Decimal("721"),
        short_strike=Decimal("714"),
        multiplier=Decimal("100"),
        quantity=1,
        initial_cash_flow=Decimal("194.95"),
        capital_required=Decimal("505.05"),
        underlying_price_at_baseline=Decimal("711.20"),
        underlying_price_at_baseline_source_id=f"baseline-snapshot-{decision_id}",
        underlying_price_at_target=terminal_price,
        underlying_price_at_target_source_id=terminal_snapshot_id,
        observed_at=_at("2026-09-29T02:00:00"),
        policy_version=FOLLOWUP_POLICY_V2,
    )


def _timing(
    observation: DecisionFollowUpObservation, captured_at: datetime | None
) -> SecuritiesTerminalEvidenceTiming:
    return resolve_securities_terminal_evidence_timing(
        target_time=observation.target_time,
        terminal_snapshot_id=securities_terminal_snapshot_id(observation),
        terminal_captured_at=captured_at,
        sessions=_sessions(observation.target_time),
        window_policy=WINDOW_POLICY,
    )


def _assess_v2(
    observation: DecisionFollowUpObservation,
    captured_at: datetime | None,
    *,
    assessed_at: datetime | None = None,
) -> FollowUpEvidenceQualityAssessment:
    return assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=None,
        target_evidence_class=None,
        lineage_resolved=True,
        assessed_at=assessed_at or _at("2026-09-29T06:00:00"),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
        securities_terminal_timing=_timing(observation, captured_at),
    )


def _assess_v1(observation: DecisionFollowUpObservation) -> FollowUpEvidenceQualityAssessment:
    return assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=None,
        target_evidence_class=None,
        lineage_resolved=True,
        assessed_at=_at("2026-09-29T06:00:00"),
    )


# --------------------------------------------------------------------------
# Production-shaped fixtures (exact timestamps observed in the retained rows)
# --------------------------------------------------------------------------

# Ordinary weekday: target Mon 2026-08-24 00:00:54Z, same-session close chain.
ORDINARY = _observation(
    decision_id="ordinary",
    evaluated_at="2026-08-23T00:00:54.443490",
    horizon_days=1,
    terminal_snapshot_id="options_chain_snapshot_0824",
)
ORDINARY_CAPTURED = _at("2026-08-24T20:00:16.225183")

# Weekend: target Sat 2026-08-29, first session Mon 2026-08-31.
WEEKEND = _observation(
    decision_id="weekend",
    evaluated_at="2026-08-28T00:00:30.652625",
    horizon_days=1,
    terminal_snapshot_id="options_chain_snapshot_0831",
)
WEEKEND_CAPTURED = _at("2026-08-31T20:00:30.933183")

# Outage SHORT: target Sat 2026-09-05; Labor Day 09-07; first eligible
# session Tue 2026-09-08; resolved by the 2026-09-21 capture (16.8d late).
OUTAGE_SHORT = _observation(
    decision_id="outage-short",
    evaluated_at="2026-09-04T00:00:54.000000",
    horizon_days=1,
    terminal_snapshot_id="options_chain_snapshot_0921",
)
# Outage STRATEGY: DTE 5 -> target Wed 2026-09-09, resolved 2026-09-21.
OUTAGE_STRATEGY = _observation(
    decision_id="outage-strategy",
    evaluated_at="2026-09-04T00:00:54.000000",
    horizon_days=5,
    horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
    terminal_snapshot_id="options_chain_snapshot_0921",
)
# Outage STRATEGY: DTE 8 -> target Sat 2026-09-12, resolved 2026-09-21.
OUTAGE_STRATEGY_WEEKEND = _observation(
    decision_id="outage-strategy-weekend",
    evaluated_at="2026-09-04T00:00:54.000000",
    horizon_days=8,
    horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
    terminal_snapshot_id="options_chain_snapshot_0921",
)
OUTAGE_CAPTURED = _at("2026-09-21T20:00:16.353224")

# Ordinary single missed window: target Mon 2026-09-28 00:00:54Z (the
# 2026-09-28 window was missed during the Docker outage); tonight's
# 2026-09-29 capture would otherwise resolve it one full session late.
MISSED_ONE_SESSION = _observation(
    decision_id="missed-one-session",
    evaluated_at="2026-09-27T00:00:54.000000",
    horizon_days=1,
    terminal_snapshot_id="options_chain_snapshot_0929",
)
MISSED_ONE_SESSION_CAPTURED = _at("2026-09-29T20:00:17.000000")


# --------------------------------------------------------------------------
# 1. Within the permitted window -> ELIGIBLE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "captured_at", "session_date"),
    [
        (ORDINARY, ORDINARY_CAPTURED, "2026-08-24"),
        (WEEKEND, WEEKEND_CAPTURED, "2026-08-31"),
    ],
)
def test_first_eligible_window_capture_is_eligible(
    observation: DecisionFollowUpObservation, captured_at: datetime, session_date: str
) -> None:
    assessment = _assess_v2(observation, captured_at)

    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.NONE
    assert is_evidence_eligible_for_downstream_attribution(assessment)
    timing = assessment.securities_terminal_timing
    assert timing is not None
    assert timing.status is SecuritiesTerminalTemporalStatus.WITHIN_FIRST_ELIGIBLE_CAPTURE_WINDOW
    assert timing.eligible_session_date == session_date
    assert timing.seconds_beyond_permitted_window == 0
    assert timing.terminal_captured_at == captured_at


def test_window_close_boundary_is_inclusive() -> None:
    at_close = _at("2026-08-24T20:15:00")
    assessment = _assess_v2(ORDINARY, at_close)
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE


def test_labor_day_bridging_is_not_late() -> None:
    """Target Sat 2026-09-05; Mon 09-07 is Labor Day. The Tue 09-08 close is
    the first eligible chain, three days after the target, and still
    contemporaneous under the session model."""

    captured = _at("2026-09-08T20:00:20")
    assessment = _assess_v2(OUTAGE_SHORT, captured)
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    timing = assessment.securities_terminal_timing
    assert timing is not None and timing.eligible_session_date == "2026-09-08"


def test_early_close_narrows_the_window() -> None:
    """Day after Thanksgiving 2026-11-27 closes 18:00Z; a 20:00Z capture is
    after that session's window and must never be read as its close."""

    observation = _observation(
        decision_id="early-close",
        evaluated_at="2026-11-26T00:00:54",
        horizon_days=1,
        terminal_snapshot_id="snapshot-1127",
    )
    ok = _assess_v2(observation, _at("2026-11-27T18:05:00"))
    late = _assess_v2(observation, _at("2026-11-27T20:00:16"))
    assert ok.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert late.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (
        late.evidence_defect_type is FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID
    )


# --------------------------------------------------------------------------
# 2. Just outside the permitted window -> rejected
# --------------------------------------------------------------------------


def test_one_second_past_the_window_is_rejected() -> None:
    assessment = _assess_v2(ORDINARY, _at("2026-08-24T20:15:01"))

    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (
        assessment.evidence_defect_type
        is FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID
    )
    assert assessment.reason_code == (
        "SECURITIES_TERMINAL_SNAPSHOT_AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW"
    )
    timing = assessment.securities_terminal_timing
    assert timing is not None and timing.seconds_beyond_permitted_window == 1
    assert not is_evidence_eligible_for_downstream_attribution(assessment)


def test_capture_after_target_but_before_the_close_is_not_a_closing_chain() -> None:
    """An intraday or weekend capture after the target is not a closing
    chain, so it is rejected. It is never accepted just for being the
    nearest snapshot."""

    intraday = _assess_v2(ORDINARY, _at("2026-08-24T15:30:00"))
    weekend = _assess_v2(WEEKEND, _at("2026-08-29T14:00:00"))
    for assessment in (intraday, weekend):
        assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
        assert assessment.reason_code == (
            "SECURITIES_TERMINAL_SNAPSHOT_OUTSIDE_ANY_ELIGIBLE_CAPTURE_WINDOW"
        )


# --------------------------------------------------------------------------
# 3. Multi-day-late snapshots (the actual September population) -> never
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "session_date", "minimum_late_days"),
    [
        (OUTAGE_SHORT, "2026-09-08", 16),
        (OUTAGE_STRATEGY, "2026-09-09", 12),
        (OUTAGE_STRATEGY_WEEKEND, "2026-09-14", 9),
    ],
)
def test_outage_era_terminal_snapshots_are_never_eligible(
    observation: DecisionFollowUpObservation, session_date: str, minimum_late_days: int
) -> None:
    v1 = _assess_v1(observation)
    v2 = _assess_v2(observation, OUTAGE_CAPTURED)

    # Before: V1 stamped every one of these ELIGIBLE (the production state).
    assert v1.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    # After: V2 rejects each one and explains exactly why.
    assert v2.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    timing = v2.securities_terminal_timing
    assert timing is not None
    assert timing.status is SecuritiesTerminalTemporalStatus.AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW
    assert timing.eligible_session_date == session_date
    assert timing.terminal_snapshot_id == "options_chain_snapshot_0921"
    assert timing.target_time == observation.target_time
    assert timing.lateness_seconds is not None
    assert timing.lateness_seconds >= minimum_late_days * 86_400
    assert timing.seconds_beyond_permitted_window is not None
    assert timing.seconds_beyond_permitted_window > 86_400


def test_a_single_ordinary_missed_window_is_also_late() -> None:
    """Not an outage-only defect: one missed 15-minute window is enough."""

    assessment = _assess_v2(MISSED_ONE_SESSION, MISSED_ONE_SESSION_CAPTURED)
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    timing = assessment.securities_terminal_timing
    assert timing is not None and timing.eligible_session_date == "2026-09-28"


def test_production_shaped_before_after_counts() -> None:
    """A fixture population with the production mix of ordinary, weekend,
    and outage rows: every row is V1-ELIGIBLE before; after, exactly the
    late rows are rejected."""

    population = (
        (ORDINARY, ORDINARY_CAPTURED),
        (WEEKEND, WEEKEND_CAPTURED),
        (OUTAGE_SHORT, OUTAGE_CAPTURED),
        (OUTAGE_STRATEGY, OUTAGE_CAPTURED),
        (OUTAGE_STRATEGY_WEEKEND, OUTAGE_CAPTURED),
        (MISSED_ONE_SESSION, MISSED_ONE_SESSION_CAPTURED),
    )
    before = [_assess_v1(item).verdict for item, _ in population]
    after = [_assess_v2(item, captured).verdict for item, captured in population]

    assert before.count(FollowUpEvidenceQualityVerdict.ELIGIBLE) == 6
    assert after.count(FollowUpEvidenceQualityVerdict.ELIGIBLE) == 2
    assert after.count(FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE) == 4


# --------------------------------------------------------------------------
# 4. No terminal evidence -> existing missing-evidence semantics preserved
# --------------------------------------------------------------------------


def test_missing_terminal_snapshot_still_blocks_observation_creation() -> None:
    """Unchanged sync behaviour: with no retained snapshot at or after the
    target, no observation is created at all (``BLOCKED_UNAVAILABLE``)."""

    decision, projection = _due_securities_decision()
    repository = _FakeRepository()
    result = SyncFollowUpObservations(
        _FakeDecisionSource((decision,)),
        _FakeSubjectSource({projection.projection_id: projection}),
        crypto_prices=object(),
        kalshi_source=_RaisingKalshiSource(),
        repository=repository,
        securities_prices=_FakeSecuritiesPrices(None),
    ).execute(as_of=BRIDGE_NOW + timedelta(days=1), policy_version=FOLLOWUP_POLICY_V2)

    assert result.created_count == 0
    assert result.entries[0].outcome == "BLOCKED_UNAVAILABLE"
    assert result.entries[0].reasons == ("SECURITIES_TERMINAL_SNAPSHOT_NOT_YET_RETAINED",)


def test_unresolvable_terminal_snapshot_fails_closed() -> None:
    assessment = _assess_v2(ORDINARY, None)
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.LINEAGE_NOT_RESOLVABLE


def test_calendar_that_does_not_bracket_the_target_fails_closed() -> None:
    later_only = tuple(
        item for item in _sessions(ORDINARY.target_time) if item.closes_at > ORDINARY.target_time
    )
    timing = resolve_securities_terminal_evidence_timing(
        target_time=ORDINARY.target_time,
        terminal_snapshot_id="options_chain_snapshot_0824",
        terminal_captured_at=ORDINARY_CAPTURED,
        sessions=later_only,
        window_policy=WINDOW_POLICY,
    )
    assert timing.status is SecuritiesTerminalTemporalStatus.TARGET_SESSION_NOT_RESOLVABLE
    assessment = assess_followup_evidence_quality(
        observation=ORDINARY,
        baseline_evidence_class=None,
        target_evidence_class=None,
        lineage_resolved=True,
        assessed_at=_at("2026-09-29T06:00:00"),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
        securities_terminal_timing=timing,
    )
    assert (
        assessment.evidence_defect_type is FollowUpEvidenceDefectType.TARGET_SESSION_NOT_RESOLVABLE
    )


def test_terminal_snapshot_captured_before_target_is_rejected() -> None:
    """Defensive: a one-element lineage means the terminal side *is* the
    decision-time snapshot, captured before the target."""

    same_snapshot = replace(ORDINARY, source_lineage=("baseline-snapshot-ordinary",))
    timing = resolve_securities_terminal_evidence_timing(
        target_time=same_snapshot.target_time,
        terminal_snapshot_id=securities_terminal_snapshot_id(same_snapshot),
        terminal_captured_at=_at("2026-08-21T20:00:56"),
        sessions=_sessions(same_snapshot.target_time),
        window_policy=WINDOW_POLICY,
    )
    assert timing.status is SecuritiesTerminalTemporalStatus.CAPTURED_BEFORE_TARGET


# --------------------------------------------------------------------------
# 5. A later retry can never convert a late snapshot into contemporaneous
# --------------------------------------------------------------------------


def test_later_reassessment_and_later_recapture_never_rehabilitate() -> None:
    first = _assess_v2(OUTAGE_SHORT, OUTAGE_CAPTURED, assessed_at=_at("2026-09-29T06:00:00"))
    retry = _assess_v2(OUTAGE_SHORT, OUTAGE_CAPTURED, assessed_at=_at("2026-12-01T06:00:00"))
    assert retry.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (retry.assessment_id, retry.canonical_hash) == (
        first.assessment_id,
        first.canonical_hash,
    )
    # A re-capture made later the same evening (after the window) is still
    # late: only the instant Apex actually fetched the chain counts.
    same_evening_retry = _assess_v2(ORDINARY, _at("2026-08-24T23:00:00"))
    assert same_evening_retry.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE


def test_observed_at_plays_no_role_in_the_verdict() -> None:
    """``observed_at`` is when sync ran, not market time. Changing it cannot
    move a verdict."""

    early_sync = replace(OUTAGE_SHORT, observed_at=_at("2026-09-08T20:30:00"))
    assert (
        _timing(early_sync, OUTAGE_CAPTURED).status is _timing(OUTAGE_SHORT, OUTAGE_CAPTURED).status
    )


# --------------------------------------------------------------------------
# 6. Deterministic replay
# --------------------------------------------------------------------------


def test_v2_assessment_is_deterministic_and_independent_of_assessed_at() -> None:
    one = _assess_v2(WEEKEND, WEEKEND_CAPTURED, assessed_at=_at("2026-09-29T00:00:00"))
    two = _assess_v2(WEEKEND, WEEKEND_CAPTURED, assessed_at=_at("2027-01-01T00:00:00"))
    assert one.canonical_hash == two.canonical_hash
    assert one.assessment_id == two.assessment_id
    assert one.securities_terminal_timing == two.securities_terminal_timing


def test_timing_facts_are_bound_into_the_v2_hash() -> None:
    ok = _assess_v2(ORDINARY, ORDINARY_CAPTURED)
    shifted = _assess_v2(ORDINARY, ORDINARY_CAPTURED + timedelta(seconds=1))
    assert ok.verdict is shifted.verdict
    assert ok.canonical_hash != shifted.canonical_hash


# --------------------------------------------------------------------------
# 7/8. V1 immutability and V2 selection
# --------------------------------------------------------------------------


def test_v1_assessment_hash_formula_is_unchanged() -> None:
    v1 = _assess_v1(OUTAGE_SHORT)
    assert v1.securities_terminal_timing is None
    assert v1.canonical_hash == evidence_quality_assessment_canonical_hash(
        assessment_id=v1.assessment_id,
        observation_id=v1.observation_id,
        observation_canonical_hash=v1.observation_canonical_hash,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        baseline_evidence_class=None,
        target_evidence_class=None,
        verdict=FollowUpEvidenceQualityVerdict.ELIGIBLE,
        reason_code="SECURITIES_OBSERVATION_ALWAYS_GENUINE_PUBLIC_ALPACA_CAPTURE",
        evidence_defect_type=FollowUpEvidenceDefectType.NONE,
    )


def test_v1_and_v2_are_distinct_rows_for_the_same_observation() -> None:
    v1 = _assess_v1(OUTAGE_SHORT)
    v2 = _assess_v2(OUTAGE_SHORT, OUTAGE_CAPTURED)
    assert v1.assessment_id != v2.assessment_id
    assert v1.observation_canonical_hash == v2.observation_canonical_hash


def test_canonical_version_is_selected_per_venue() -> None:
    assert (
        current_followup_evidence_quality_policy_version(SharedVenue.SECURITIES)
        == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )
    assert (
        current_followup_evidence_quality_policy_version(SharedVenue.CRYPTO)
        == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    )
    assert (
        current_followup_evidence_quality_policy_version(SharedVenue.KALSHI)
        == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    )
    assert CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    assert (
        CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION
        == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    )


def test_v1_refuses_timing_and_v2_requires_it() -> None:
    timing = _timing(ORDINARY, ORDINARY_CAPTURED)
    with pytest.raises(CounterfactualFollowUpError, match="V1_DOES_NOT_ACCEPT"):
        assess_followup_evidence_quality(
            observation=ORDINARY,
            baseline_evidence_class=None,
            target_evidence_class=None,
            lineage_resolved=True,
            assessed_at=_at("2026-09-29T06:00:00"),
            securities_terminal_timing=timing,
        )
    with pytest.raises(CounterfactualFollowUpError, match="V2_REQUIRES_SECURITIES_TERMINAL_TIMING"):
        assess_followup_evidence_quality(
            observation=ORDINARY,
            baseline_evidence_class=None,
            target_evidence_class=None,
            lineage_resolved=True,
            assessed_at=_at("2026-09-29T06:00:00"),
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
        )
    mismatched = _timing(WEEKEND, WEEKEND_CAPTURED)
    with pytest.raises(CounterfactualFollowUpError, match="TIMING_TARGET_MISMATCH"):
        assess_followup_evidence_quality(
            observation=ORDINARY,
            baseline_evidence_class=None,
            target_evidence_class=None,
            lineage_resolved=True,
            assessed_at=_at("2026-09-29T06:00:00"),
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
            securities_terminal_timing=mismatched,
        )


def test_tampered_observation_hash_still_overrides_under_v2() -> None:
    tampered = replace(ORDINARY, subsequent_return=Decimal("9"))
    assessment = _assess_v2(tampered, ORDINARY_CAPTURED)
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.OBSERVATION_HASH_INVALID


# --------------------------------------------------------------------------
# Application: the V2 lane assesses SECURITIES only, from retained state only
# --------------------------------------------------------------------------


class _Source:
    def __init__(
        self,
        observations: tuple[DecisionFollowUpObservation, ...],
        captured_at: dict[str, datetime],
    ) -> None:
        self._observations = observations
        self._captured_at = captured_at
        self.timing_calls: list[str] = []

    def observations(self) -> tuple[DecisionFollowUpObservation, ...]:
        return self._observations

    def resolve_observation_evidence_lineage(
        self, observation: DecisionFollowUpObservation
    ) -> tuple[EvidenceClass | None, EvidenceClass | None, bool]:
        return EvidenceClass.GENUINE_PUBLIC, EvidenceClass.GENUINE_PUBLIC, True

    def resolve_securities_terminal_evidence_timing(
        self, observation: DecisionFollowUpObservation
    ) -> SecuritiesTerminalEvidenceTiming:
        self.timing_calls.append(observation.observation_id)
        return _timing(observation, self._captured_at.get(observation.observation_id))


class _Repository:
    def __init__(self) -> None:
        self.saved: dict[str, FollowUpEvidenceQualityAssessment] = {}

    def save_assessment(self, *, assessment: FollowUpEvidenceQualityAssessment) -> bool:
        existing = self.saved.get(assessment.assessment_id)
        if existing is not None:
            assert existing.canonical_hash == assessment.canonical_hash
            return True
        self.saved[assessment.assessment_id] = assessment
        return False


def _crypto_like(observation: DecisionFollowUpObservation) -> DecisionFollowUpObservation:
    return replace(
        observation, observation_id=f"{observation.observation_id}-crypto", venue=SharedVenue.CRYPTO
    )


def test_v2_lane_assesses_securities_only_and_replays_idempotently() -> None:
    crypto = _crypto_like(ORDINARY)
    source = _Source(
        (ORDINARY, OUTAGE_SHORT, crypto),
        {
            ORDINARY.observation_id: ORDINARY_CAPTURED,
            OUTAGE_SHORT.observation_id: OUTAGE_CAPTURED,
        },
    )
    repository = _Repository()
    service = AssessFollowUpEvidenceQuality(source, repository)

    first = service.execute(
        assessed_at=_at("2026-09-29T06:00:00"),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    second = service.execute(
        assessed_at=_at("2026-09-30T06:00:00"),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )

    assert first.considered_observations == 2  # the CRYPTO row is not V2's
    assert (first.eligible_count, first.ineligible_count, first.created_count) == (1, 1, 2)
    assert (second.created_count, second.idempotent_replay_count) == (0, 2)
    assert crypto.observation_id not in source.timing_calls
    assert all(item.authority == "NONE" for item in repository.saved.values())


def test_v2_never_applies_to_crypto_or_kalshi() -> None:
    for venue in (SharedVenue.CRYPTO, SharedVenue.KALSHI):
        assert not followup_evidence_quality_policy_supports_venue(
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2, venue=venue
        )
        other = replace(ORDINARY, venue=venue)
        with pytest.raises(CounterfactualFollowUpError, match="UNSUPPORTED_FOR_VENUE"):
            assess_followup_evidence_quality(
                observation=other,
                baseline_evidence_class=None,
                target_evidence_class=None,
                lineage_resolved=True,
                assessed_at=_at("2026-09-29T06:00:00"),
                assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
                securities_terminal_timing=_timing(ORDINARY, ORDINARY_CAPTURED),
            )


def test_v1_lane_behaviour_is_unchanged_for_every_venue() -> None:
    crypto = _crypto_like(ORDINARY)
    source = _Source((ORDINARY, OUTAGE_SHORT, crypto), {})
    result = AssessFollowUpEvidenceQuality(source, _Repository()).execute(
        assessed_at=_at("2026-09-29T06:00:00")
    )
    assert result.considered_observations == 3
    assert source.timing_calls == []  # V1 never consults timing


# --------------------------------------------------------------------------
# 9. Downstream aggregates no longer count temporally invalid rows
# --------------------------------------------------------------------------


def test_v2_aggregate_excludes_late_rows_that_v1_counted() -> None:
    rows = (
        (ORDINARY, ORDINARY_CAPTURED),
        (WEEKEND, WEEKEND_CAPTURED),
        (OUTAGE_SHORT, OUTAGE_CAPTURED),
        (MISSED_ONE_SESSION, MISSED_ONE_SESSION_CAPTURED),
    )
    observations = tuple(item for item, _ in rows)
    decisions = tuple(
        _decision(item.decision_id, item.decision_evaluated_at) for item in observations
    )
    projections = {
        decision.subject_projection_id: "SECURITIES_STRUCTURE_RESEARCH" for decision in decisions
    }
    structures = {decision.subject_projection_id: "CALL_CREDIT_VERTICAL" for decision in decisions}
    assessments = (
        *(_assess_v1(item) for item in observations),
        *(_assess_v2(item, captured) for item, captured in rows),
    )

    def build(version: str):  # type: ignore[no-untyped-def]
        (aggregate,) = build_securities_structure_outcome_aggregates(
            decisions=decisions,
            observations=observations,
            assessments=assessments,
            strategy_or_method_id_by_projection=projections,
            structure_type_by_projection=structures,
            run_id="cycle-2026-09-29",
            generated_at=_at("2026-09-29T06:00:00"),
            evidence_quality_policy_version=version,
        )
        return aggregate

    v1 = build(FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1)
    v2 = build(FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2)

    assert (v1.eligible_observation_count, v1.evidence_ineligible_observation_count) == (4, 0)
    assert (v2.eligible_observation_count, v2.evidence_ineligible_observation_count) == (2, 2)
    assert set(v2.contributing_decision_ids) == {"ordinary", "weekend"}
    assert v1.cohort_id != v2.cohort_id  # a V2 cohort never overwrites a V1 row
    assert v2.authority == "NONE"


def test_v2_aggregate_treats_missing_v2_assessment_as_unassessed() -> None:
    decision = _decision(ORDINARY.decision_id, ORDINARY.decision_evaluated_at)
    (aggregate,) = build_securities_structure_outcome_aggregates(
        decisions=(decision,),
        observations=(ORDINARY,),
        assessments=(_assess_v1(ORDINARY),),  # V1 exists, V2 not yet written
        strategy_or_method_id_by_projection={decision.subject_projection_id: "M"},
        structure_type_by_projection={decision.subject_projection_id: "CALL_CREDIT_VERTICAL"},
        run_id="cycle",
        generated_at=_at("2026-09-29T06:00:00"),
        evidence_quality_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    )
    assert aggregate.unassessed_observation_count == 1
    assert aggregate.eligible_observation_count == 0


# --------------------------------------------------------------------------
# Fable review follow-ups
# --------------------------------------------------------------------------


def test_target_inside_window_is_distinguished_from_outage_lateness() -> None:
    """Target 20:05Z sits inside Tuesday's [20:00, 20:15] window; the
    selector skipped the 20:00:30 chain (before target) and retained the next
    session's. Still ineligible, but never miscounted as outage lateness."""

    target = _at("2026-09-29T20:05:00")
    timing = resolve_securities_terminal_evidence_timing(
        target_time=target,
        terminal_snapshot_id="snapshot-0930",
        terminal_captured_at=_at("2026-09-30T20:00:30"),
        sessions=_sessions(target),
        window_policy=WINDOW_POLICY,
    )
    assert (
        timing.status
        is SecuritiesTerminalTemporalStatus.TARGET_INSIDE_WINDOW_SAME_SESSION_CHAIN_NOT_SELECTED
    )
    assert timing.eligible_session_date == "2026-09-29"


def test_sub_second_overrun_is_reported_as_at_least_one_second() -> None:
    timing = _timing(ORDINARY, _at("2026-08-24T20:15:00.500000"))
    assert timing.status is SecuritiesTerminalTemporalStatus.AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW
    assert timing.seconds_beyond_permitted_window == 1


def test_calendar_version_is_retained_in_provenance() -> None:
    timing = _timing(ORDINARY, ORDINARY_CAPTURED)
    assert timing.calendar_version is not None
    assert timing.calendar_version.startswith("pandas-market-calendars==")
