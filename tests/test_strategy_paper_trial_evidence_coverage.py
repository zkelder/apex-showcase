"""Trial Evidence Coverage Disclosure v1: pure, offline tests for
``domain.strategy_paper_trial_evidence_coverage``. No database, no
network -- every observation is a directly-constructed fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.live_market_observation import (
    LiveMarketObservation,
    LiveObservationQuality,
    LiveObservationType,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_paper_trial_evidence_coverage import (
    MATERIAL_EVIDENCE_GAP_SECONDS,
    TRIAL_EVIDENCE_COVERAGE_POLICY_V1,
    TrialEvidenceCoverageError,
    evaluate_trial_evidence_coverage,
    is_coverage_eligible_observation,
    is_material_coverage_gap,
)

INSTRUMENT_ID = "BTC-USD"
OTHER_INSTRUMENT_ID = "ETH-USD"
OPENED_AT = datetime(2026, 8, 30, 0, 0, 30, tzinfo=UTC)
HORIZON_DAYS = 30
CLOSED_AT = OPENED_AT + timedelta(days=HORIZON_DAYS)


def _observation(
    *,
    captured_at: datetime,
    instrument_id: str = INSTRUMENT_ID,
    quality: LiveObservationQuality = LiveObservationQuality.PASSED,
    evidence_class: EvidenceClass = EvidenceClass.GENUINE_PUBLIC,
    round_id: str | None = None,
) -> LiveMarketObservation:
    kwargs: dict[str, object] = {}
    if quality is LiveObservationQuality.FAILED:
        kwargs["quality_reasons"] = ("TEST_INDUCED_FAILURE",)
    return LiveMarketObservation.create(
        round_id=round_id or f"round-{captured_at.isoformat()}",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_id=instrument_id,
        observation_type=LiveObservationType.QUOTE,
        captured_at=captured_at,
        bid_price=Decimal("100"),
        ask_price=Decimal("100.01"),
        evidence_class=evidence_class,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=quality,
        **kwargs,  # type: ignore[arg-type]
    )


def _minutely(start: datetime, end: datetime) -> tuple[LiveMarketObservation, ...]:
    observations = []
    current = start
    while current <= end:
        observations.append(_observation(captured_at=current))
        current += timedelta(minutes=1)
    return tuple(observations)


class TestEligibility:
    def test_passed_genuine_public_is_eligible(self) -> None:
        assert is_coverage_eligible_observation(
            _observation(captured_at=OPENED_AT, evidence_class=EvidenceClass.GENUINE_PUBLIC)
        )

    def test_passed_historical_public_is_eligible(self) -> None:
        assert is_coverage_eligible_observation(
            _observation(captured_at=OPENED_AT, evidence_class=EvidenceClass.HISTORICAL_PUBLIC)
        )

    @pytest.mark.parametrize(
        "evidence_class",
        [EvidenceClass.FIXTURE, EvidenceClass.SANDBOX, EvidenceClass.PAPER, EvidenceClass.LIVE],
    )
    def test_other_evidence_classes_are_ineligible(self, evidence_class: EvidenceClass) -> None:
        assert not is_coverage_eligible_observation(
            _observation(captured_at=OPENED_AT, evidence_class=evidence_class)
        )

    def test_failed_quality_is_ineligible_regardless_of_evidence_class(self) -> None:
        assert not is_coverage_eligible_observation(
            _observation(captured_at=OPENED_AT, quality=LiveObservationQuality.FAILED)
        )


class TestWindowValidation:
    def test_inverted_window_is_rejected(self) -> None:
        with pytest.raises(TrialEvidenceCoverageError):
            evaluate_trial_evidence_coverage(
                instrument_id=INSTRUMENT_ID,
                window_start=CLOSED_AT,
                window_end=OPENED_AT,
                observations=(),
            )

    def test_zero_duration_window_does_not_raise(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=OPENED_AT,
            observations=(),
        )
        assert result.window_duration_seconds == 0
        assert result.nominal_days == 1
        assert result.covered_days == 0
        assert result.largest_gap_seconds == 0


class TestContinuousCoverage:
    def test_dense_minutely_observations_report_zero_material_gap(self) -> None:
        observations = _minutely(OPENED_AT, OPENED_AT + timedelta(hours=2))
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=OPENED_AT + timedelta(hours=2),
            observations=observations,
        )
        assert result.qualifying_observation_count == len(observations)
        assert result.largest_gap_seconds == 60
        assert is_material_coverage_gap(result.largest_gap_seconds) is False
        assert result.covered_days == 1
        assert result.nominal_days == 1

    def test_policy_version_is_stamped(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(),
        )
        assert result.policy_version == TRIAL_EVIDENCE_COVERAGE_POLICY_V1


class TestZeroObservations:
    def test_zero_observations_reports_full_window_as_the_gap(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(),
        )
        assert result.qualifying_observation_count == 0
        assert result.covered_days == 0
        assert result.largest_gap_seconds == result.window_duration_seconds
        assert is_material_coverage_gap(result.largest_gap_seconds) is True

    def test_never_backfills_or_fabricates_a_reading_for_an_uncovered_day(self) -> None:
        # A single observation on day 1 only -- day 2..30 must read as
        # genuinely uncovered, never inferred from day 1's own reading.
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(_observation(captured_at=OPENED_AT),),
        )
        assert result.covered_days == 1
        assert result.nominal_days == HORIZON_DAYS + 1


class TestOnlyCloseObservationExists:
    def test_single_observation_at_close_reports_a_material_gap(self) -> None:
        # "zero retained observations except the close observation" -- the
        # real scenario a trial's own contemporaneous-evidence-for-close
        # gate structurally guarantees at minimum.
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(_observation(captured_at=CLOSED_AT),),
        )
        assert result.qualifying_observation_count == 1
        assert result.covered_days == 1
        assert result.largest_gap_seconds == result.window_duration_seconds
        assert is_material_coverage_gap(result.largest_gap_seconds) is True


class TestRealOutageShape:
    """Models today's actual production shape: valid observations exist
    initially, a long multi-day observation gap occurs, valid observations
    resume, and a fresh contemporaneous observation exists at close."""

    def test_multi_day_gap_in_the_middle_is_reported_honestly(self) -> None:
        pre_gap = _minutely(OPENED_AT, OPENED_AT + timedelta(hours=1))
        gap_days = 16
        post_gap_start = OPENED_AT + timedelta(hours=1) + timedelta(days=gap_days)
        post_gap = _minutely(post_gap_start, CLOSED_AT)
        observations = pre_gap + post_gap

        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=observations,
        )
        expected_gap_seconds = int(
            (post_gap_start - (OPENED_AT + timedelta(hours=1))).total_seconds()
        )
        assert result.largest_gap_seconds == expected_gap_seconds
        assert is_material_coverage_gap(result.largest_gap_seconds) is True
        # Covered days: 1 (pre-gap) + the post-gap span's own distinct
        # dates -- genuinely fewer than nominal_days, never equal to it.
        assert result.covered_days < result.nominal_days
        assert result.qualifying_observation_count == len(observations)

    def test_p_and_l_relevant_fields_are_untouched_by_this_module(self) -> None:
        # This module has no field, parameter, or return value that could
        # even syntactically reach a P&L/price computation -- structurally
        # verified by construction, not merely by convention: the return
        # type carries no price-shaped field at all.
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(),
        )
        field_names = set(result.__dataclass_fields__)
        assert not field_names & {"price", "realized_pnl", "close_price", "pnl"}


class TestSparsePartialCoverage:
    def test_a_few_scattered_days_covered_out_of_the_full_window(self) -> None:
        scattered = (
            _observation(captured_at=OPENED_AT),
            _observation(captured_at=OPENED_AT + timedelta(days=5)),
            _observation(captured_at=OPENED_AT + timedelta(days=10)),
            _observation(captured_at=CLOSED_AT),
        )
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=scattered,
        )
        assert result.covered_days == 4
        assert result.qualifying_observation_count == 4
        # Gaps are 5d, 5d, then 20d from the last observation (day 10) to
        # window_end (day 30) -- that trailing 20d gap is the largest, not
        # the evenly-spaced 5d ones between the first three observations.
        assert result.largest_gap_seconds == int(timedelta(days=20).total_seconds())
        assert is_material_coverage_gap(result.largest_gap_seconds) is True


class TestFiltering:
    def test_observations_for_a_different_instrument_are_excluded(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(_observation(captured_at=OPENED_AT, instrument_id=OTHER_INSTRUMENT_ID),),
        )
        assert result.qualifying_observation_count == 0
        assert is_material_coverage_gap(result.largest_gap_seconds) is True

    def test_observations_outside_the_window_are_excluded(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(
                _observation(captured_at=OPENED_AT - timedelta(days=1)),
                _observation(captured_at=CLOSED_AT + timedelta(days=1)),
            ),
        )
        assert result.qualifying_observation_count == 0

    def test_window_boundary_observations_are_included_inclusive(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(
                _observation(captured_at=OPENED_AT, round_id="at-start"),
                _observation(captured_at=CLOSED_AT, round_id="at-end"),
            ),
        )
        assert result.qualifying_observation_count == 2

    def test_failed_quality_observations_never_count_as_coverage(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(
                _observation(captured_at=OPENED_AT, quality=LiveObservationQuality.FAILED),
            ),
        )
        assert result.qualifying_observation_count == 0
        assert result.covered_days == 0

    def test_fixture_evidence_class_never_counts_as_coverage(self) -> None:
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=(
                _observation(captured_at=OPENED_AT, evidence_class=EvidenceClass.FIXTURE),
            ),
        )
        assert result.qualifying_observation_count == 0


class TestMaterialGapThreshold:
    def test_gap_at_exactly_the_threshold_is_not_material(self) -> None:
        window_end = OPENED_AT + timedelta(seconds=MATERIAL_EVIDENCE_GAP_SECONDS)
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=window_end,
            observations=(_observation(captured_at=window_end),),
        )
        assert result.largest_gap_seconds == MATERIAL_EVIDENCE_GAP_SECONDS
        assert is_material_coverage_gap(result.largest_gap_seconds) is False

    def test_gap_one_second_past_the_threshold_is_material(self) -> None:
        window_end = OPENED_AT + timedelta(seconds=MATERIAL_EVIDENCE_GAP_SECONDS + 1)
        result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=window_end,
            observations=(_observation(captured_at=window_end),),
        )
        assert result.largest_gap_seconds == MATERIAL_EVIDENCE_GAP_SECONDS + 1
        assert is_material_coverage_gap(result.largest_gap_seconds) is True


class TestDeterminism:
    def test_identical_inputs_produce_an_identical_result(self) -> None:
        observations = _minutely(OPENED_AT, OPENED_AT + timedelta(hours=1))
        first = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=observations,
        )
        second = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=observations,
        )
        assert first == second

    def test_observation_order_does_not_affect_the_result(self) -> None:
        observations = _minutely(OPENED_AT, OPENED_AT + timedelta(hours=1))
        forward = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=observations,
        )
        reversed_result = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=CLOSED_AT,
            observations=tuple(reversed(observations)),
        )
        assert forward == reversed_result

    def test_extra_wider_batch_from_a_caller_does_not_change_the_result(self) -> None:
        # A caller is free to pass a slightly wider read-model batch --
        # this module re-filters defensively rather than trusting bounds.
        narrow = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=OPENED_AT + timedelta(hours=1),
            observations=_minutely(OPENED_AT, OPENED_AT + timedelta(hours=1)),
        )
        wider_batch = _minutely(OPENED_AT - timedelta(hours=5), OPENED_AT + timedelta(hours=6))
        wide = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=OPENED_AT,
            window_end=OPENED_AT + timedelta(hours=1),
            observations=wider_batch,
        )
        assert narrow == wide
