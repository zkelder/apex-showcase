"""Per-instrument genuine data-quality gate (milestone section 9).

``real_securities_data.evaluate_securities_data_quality`` (reused unmodified
by ``historical_market_data.EvaluateHistoricalDataQuality``) already
evaluates a batch in *aggregate*: one duplicate count, one invalid-OHLC
count, and so on, for the whole retained batch. That is the right shape for
"is this batch internally consistent", but it cannot answer "which specific
instruments are actually fit to enter the canonical cross-market research
cohort" -- a batch can be 92/93 clean and still have one instrument with a
genuine defect. This module adds that missing per-instrument layer on top
of (never instead of) the existing aggregate report.

A key honesty distinction this module makes that a naive "sessions missing
from the full policy window" count cannot: this cohort's retained coverage
genuinely does not start on the same date for every instrument (a real,
empirically observed Alpaca IEX-feed account/plan characteristic -- see
milestone section 5/6 evidence). A session missing *before* an instrument's
own first retained bar is a coverage-window fact, not a data-quality defect.
A session missing *between* an instrument's own first and last retained bar
is a genuine gap. Only the latter counts toward ``missing_sessions_within_
coverage_count`` below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from apex.domain.corporate_action_consistency import (
    KNOWN_SPLIT_FACTS_V1,
    SplitConsistencyOutcome,
    check_split_consistency,
)
from apex.domain.securities import SecuritiesBarEvidence, SecuritiesCalendarSession
from apex.domain.values import content_digest, deterministic_id

GENUINE_DATA_QUALITY_GATE_V1 = "GENUINE_DATA_QUALITY_GATE_V1"
MINIMUM_SESSIONS_FOR_SUFFICIENT_HISTORY = 252
EXTREME_RETURN_THRESHOLD = Decimal("0.50")
MAXIMUM_UNEXPLAINED_DISCONTINUITIES_BEFORE_REJECTION = 2


class InstrumentQualityClassification(StrEnum):
    USABLE = "USABLE"
    USABLE_WITH_WARNINGS = "USABLE_WITH_WARNINGS"
    INSUFFICIENT = "INSUFFICIENT"
    REJECTED = "REJECTED"


class GenuineDataQualityGateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InstrumentQualityAssessment:
    stable_security_id: str
    symbol: str
    instrument_id: str
    classification: InstrumentQualityClassification
    reasons: tuple[str, ...]
    observation_count: int
    coverage_start: str | None
    coverage_end: str | None
    duplicate_timestamp_count: int
    invalid_ohlc_count: int
    nonpositive_price_count: int
    missing_sessions_within_coverage_count: int
    volume_anomaly_count: int
    unexplained_discontinuity_count: int
    explained_discontinuity_count: int
    adjustment_states: tuple[str, ...]
    source_batch_ids: tuple[str, ...]


def _classify(
    *,
    duplicate: int,
    invalid_ohlc: int,
    nonpositive: int,
    missing_within_coverage: int,
    volume_anomaly: int,
    unexplained_discontinuities: int,
    adjustment_state_count: int,
    observation_count: int,
) -> tuple[InstrumentQualityClassification, tuple[str, ...]]:
    reasons: list[str] = []
    if duplicate > 0:
        reasons.append("DUPLICATE_TIMESTAMPS_PRESENT")
    if invalid_ohlc > 0:
        reasons.append("INVALID_OHLC_RELATIONSHIP_PRESENT")
    if nonpositive > 0:
        reasons.append("NONPOSITIVE_PRICE_PRESENT")
    if observation_count == 0:
        reasons.append("ZERO_OBSERVATIONS")
    if unexplained_discontinuities > MAXIMUM_UNEXPLAINED_DISCONTINUITIES_BEFORE_REJECTION:
        reasons.append("REPEATED_UNEXPLAINED_PRICE_DISCONTINUITIES")
    if reasons:
        return InstrumentQualityClassification.REJECTED, tuple(reasons)

    if observation_count < MINIMUM_SESSIONS_FOR_SUFFICIENT_HISTORY:
        return (
            InstrumentQualityClassification.INSUFFICIENT,
            (f"RETAINED_SESSION_COUNT_BELOW_MINIMUM:{observation_count}",),
        )

    warnings: list[str] = []
    if missing_within_coverage > 0:
        warnings.append(f"MISSING_SESSIONS_WITHIN_OWN_COVERAGE:{missing_within_coverage}")
    if volume_anomaly > 0:
        warnings.append(f"VOLUME_ANOMALY_COUNT:{volume_anomaly}")
    if unexplained_discontinuities > 0:
        warnings.append(f"UNEXPLAINED_DISCONTINUITY_COUNT:{unexplained_discontinuities}")
    if adjustment_state_count > 1:
        warnings.append("INCONSISTENT_ADJUSTMENT_STATE_LINEAGE")
    if warnings:
        return InstrumentQualityClassification.USABLE_WITH_WARNINGS, tuple(warnings)
    return InstrumentQualityClassification.USABLE, ()


def assess_instrument_quality(
    *,
    stable_security_id: str,
    symbol: str,
    instrument_id: str,
    bars: Sequence[SecuritiesBarEvidence],
    calendar_sessions: Sequence[SecuritiesCalendarSession],
    source_batch_ids: Sequence[str],
) -> InstrumentQualityAssessment:
    """Pure, per-instrument classification. ``calendar_sessions`` should be
    every retained trading session for the *whole* cohort's declared date
    range -- this function itself narrows to the instrument's own observed
    ``[coverage_start, coverage_end]`` before counting anything missing, so
    passing the full-range calendar is always safe and never double-counts
    a pre-coverage session as a defect."""

    ordered = sorted(bars, key=lambda item: item.period_start)
    observation_count = len(ordered)
    if not ordered:
        classification, reasons = _classify(
            duplicate=0,
            invalid_ohlc=0,
            nonpositive=0,
            missing_within_coverage=0,
            volume_anomaly=0,
            unexplained_discontinuities=0,
            adjustment_state_count=0,
            observation_count=0,
        )
        return InstrumentQualityAssessment(
            stable_security_id=stable_security_id,
            symbol=symbol,
            instrument_id=instrument_id,
            classification=classification,
            reasons=reasons,
            observation_count=0,
            coverage_start=None,
            coverage_end=None,
            duplicate_timestamp_count=0,
            invalid_ohlc_count=0,
            nonpositive_price_count=0,
            missing_sessions_within_coverage_count=0,
            volume_anomaly_count=0,
            unexplained_discontinuity_count=0,
            explained_discontinuity_count=0,
            adjustment_states=(),
            source_batch_ids=tuple(sorted(set(source_batch_ids))),
        )

    dates = [bar.period_start.date().isoformat() for bar in ordered]
    coverage_start, coverage_end = dates[0], dates[-1]
    duplicate = len(dates) - len(set(dates))
    invalid_ohlc = sum(
        1
        for bar in ordered
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close)
    )
    nonpositive = sum(
        1
        for bar in ordered
        if any(value <= 0 for value in (bar.open, bar.high, bar.low, bar.close))
    )
    zero_volume = sum(1 for bar in ordered if bar.volume == 0)
    expected_within_coverage = {
        session.session_date
        for session in calendar_sessions
        if coverage_start <= session.session_date <= coverage_end
    }
    missing_within_coverage = len(expected_within_coverage - set(dates))

    extreme_moves = 0
    unexplained = 0
    explained = 0
    known_by_symbol = {
        fact.effective_date: fact for fact in KNOWN_SPLIT_FACTS_V1 if fact.symbol == symbol
    }
    for earlier, later in pairwise(ordered):
        if earlier.close <= 0:
            continue
        move = abs((later.close - earlier.close) / earlier.close)
        if move <= EXTREME_RETURN_THRESHOLD:
            continue
        extreme_moves += 1
        later_date = later.period_start.date().isoformat()
        fact = known_by_symbol.get(later_date)
        if fact is not None:
            result = check_split_consistency(ordered, fact=fact)
            if result.outcome is SplitConsistencyOutcome.CONSISTENT:
                explained += 1
                continue
        unexplained += 1

    adjustment_states = tuple(sorted({bar.adjustment_state.value for bar in ordered}))
    classification, reasons = _classify(
        duplicate=duplicate,
        invalid_ohlc=invalid_ohlc,
        nonpositive=nonpositive,
        missing_within_coverage=missing_within_coverage,
        volume_anomaly=zero_volume,
        unexplained_discontinuities=unexplained,
        adjustment_state_count=len(adjustment_states),
        observation_count=observation_count,
    )
    return InstrumentQualityAssessment(
        stable_security_id=stable_security_id,
        symbol=symbol,
        instrument_id=instrument_id,
        classification=classification,
        reasons=reasons,
        observation_count=observation_count,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        duplicate_timestamp_count=duplicate,
        invalid_ohlc_count=invalid_ohlc,
        nonpositive_price_count=nonpositive,
        missing_sessions_within_coverage_count=missing_within_coverage,
        volume_anomaly_count=zero_volume,
        unexplained_discontinuity_count=unexplained,
        explained_discontinuity_count=explained,
        adjustment_states=adjustment_states,
        source_batch_ids=tuple(sorted(set(source_batch_ids))),
    )


@dataclass(frozen=True, slots=True)
class GenuineCohortQualityGateReport:
    report_id: str
    snapshot_id: str
    cohort_policy_id: str
    requested_instrument_count: int
    successful_instrument_count: int
    missing_instrument_symbols: tuple[str, ...]
    usable_count: int
    usable_with_warnings_count: int
    insufficient_count: int
    rejected_count: int
    coverage_start: str | None
    coverage_end: str | None
    point_in_time_limitations: tuple[str, ...]
    assessments: tuple[InstrumentQualityAssessment, ...]
    source_hashes: tuple[str, ...]
    created_at: datetime
    canonical_hash: str

    @classmethod
    def build(
        cls,
        *,
        snapshot_id: str,
        cohort_policy_id: str,
        requested_symbols: Sequence[str],
        assessments: Sequence[InstrumentQualityAssessment],
        point_in_time_limitations: Sequence[str],
        source_hashes: Sequence[str],
        created_at: datetime,
    ) -> GenuineCohortQualityGateReport:
        if created_at.tzinfo is None:
            raise GenuineDataQualityGateError("QUALITY_GATE_CREATED_AT_MUST_BE_AWARE")
        ordered = tuple(sorted(assessments, key=lambda item: item.symbol))
        present_symbols = {item.symbol for item in ordered}
        missing = tuple(sorted(set(requested_symbols) - present_symbols))
        starts = [item.coverage_start for item in ordered if item.coverage_start is not None]
        ends = [item.coverage_end for item in ordered if item.coverage_end is not None]
        counts = {state: 0 for state in InstrumentQualityClassification}
        for item in ordered:
            counts[item.classification] += 1
        payload = (
            GENUINE_DATA_QUALITY_GATE_V1,
            snapshot_id,
            cohort_policy_id,
            tuple(sorted(requested_symbols)),
            tuple(
                (
                    item.symbol,
                    item.stable_security_id,
                    item.classification.value,
                    tuple(sorted(item.reasons)),
                    item.observation_count,
                )
                for item in ordered
            ),
            tuple(sorted(set(point_in_time_limitations))),
            tuple(sorted(set(source_hashes))),
        )
        return cls(
            report_id=deterministic_id("genuine_cohort_quality_gate_report", payload),
            snapshot_id=snapshot_id,
            cohort_policy_id=cohort_policy_id,
            requested_instrument_count=len(set(requested_symbols)),
            successful_instrument_count=len(present_symbols),
            missing_instrument_symbols=missing,
            usable_count=counts[InstrumentQualityClassification.USABLE],
            usable_with_warnings_count=counts[InstrumentQualityClassification.USABLE_WITH_WARNINGS],
            insufficient_count=counts[InstrumentQualityClassification.INSUFFICIENT],
            rejected_count=counts[InstrumentQualityClassification.REJECTED],
            coverage_start=min(starts) if starts else None,
            coverage_end=max(ends) if ends else None,
            point_in_time_limitations=tuple(sorted(set(point_in_time_limitations))),
            assessments=ordered,
            source_hashes=tuple(sorted(set(source_hashes))),
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )

    def canonical_research_cohort_symbols(self) -> tuple[str, ...]:
        """Only ``USABLE``/``USABLE_WITH_WARNINGS`` instruments may flow into
        the canonical cross-market research cohort (milestone section 9)."""

        eligible = {
            InstrumentQualityClassification.USABLE,
            InstrumentQualityClassification.USABLE_WITH_WARNINGS,
        }
        return tuple(
            sorted(item.symbol for item in self.assessments if item.classification in eligible)
        )
