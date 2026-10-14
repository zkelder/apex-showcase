"""Domain-layer proofs for Fresh Options Evidence Operations and Structure
Research Baseline v1: capture-due policy (genuine XNYS calendar evidence,
never a hand-rolled weekday check), chain-coverage quality, distinct-session
forward-history readiness, the retained checkpoint, storage projection, and
the descriptive cross-sectional baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.domain.options_intelligence import OptionType
from apex.domain.options_quote_intelligence import (
    ChainSampleCandidate,
    select_contracts_stratified,
)
from apex.domain.options_research_operations import (
    MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
    ChainCoverageQuality,
    ChainCoverageQualityPolicy,
    OptionsCaptureDueStatus,
    OptionsCaptureWindowPolicy,
    OptionsForwardEvidenceCheckpoint,
    OptionsResearchOperationsError,
    ResearchReadinessState,
    build_chain_coverage_diagnostic,
    build_options_cross_sectional_baseline,
    determine_options_capture_due_state,
    evaluate_options_history_readiness,
    project_forward_storage,
    session_id_for_captured_at,
)
from apex.domain.securities import SecuritiesCalendarSession
from apex.domain.values import ValueValidationError

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)


def _calendar(
    start: str = "2026-08-01", end: str = "2026-08-15"
) -> tuple[SecuritiesCalendarSession, ...]:
    return ReviewedXNYSCalendar(start_date=start, end_date=end).sessions


def _policy() -> OptionsCaptureWindowPolicy:
    return OptionsCaptureWindowPolicy.create()


# --------------------------------------------------------------------------
# Capture-window policy
# --------------------------------------------------------------------------


class TestOptionsCaptureWindowPolicy:
    def test_default_policy_derives_from_the_existing_freshness_threshold(self) -> None:
        policy = OptionsCaptureWindowPolicy.create()
        assert policy.freshness_threshold_seconds == 900
        assert policy.post_close_grace_seconds == 900
        assert policy.calendar_identity == "XNYS"

    def test_window_is_anchored_on_official_close_not_a_fixed_clock_time(self) -> None:
        sessions = _calendar()
        monday = next(item for item in sessions if item.session_date == "2026-08-10")
        window_open, window_close = _policy().window_for_session(monday)
        assert window_open == monday.closes_at
        assert window_close == monday.closes_at + timedelta(seconds=900)

    def test_early_close_narrows_the_window_automatically(self) -> None:
        # Day after Thanksgiving 2026 (2026-11-27): genuine XNYS early close.
        sessions = ReviewedXNYSCalendar(start_date="2026-11-20", end_date="2026-11-30").sessions
        early = next(item for item in sessions if item.session_date == "2026-11-27")
        assert early.is_early_close
        window_open, window_close = _policy().window_for_session(early)
        assert window_open == early.closes_at
        assert window_open.hour < 19  # well before a normal ~20:00 UTC close
        assert window_close == early.closes_at + timedelta(seconds=900)

    def test_rejects_a_mismatched_calendar_identity(self) -> None:
        sessions = _calendar()
        monday = next(item for item in sessions if item.session_date == "2026-08-10")
        from dataclasses import replace

        foreign = replace(monday, calendar_identity="NOT_XNYS")
        with pytest.raises(OptionsResearchOperationsError):
            _policy().window_for_session(foreign)


# --------------------------------------------------------------------------
# determine_options_capture_due_state -- the required due/session matrix
# --------------------------------------------------------------------------


class TestCaptureDueState:
    def test_normal_session_inside_window_is_due(self) -> None:
        sessions = _calendar()
        now = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)  # inside [20:00, 20:15]
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=now, capability_supported=True
        )
        assert decision.status is OptionsCaptureDueStatus.DUE
        assert decision.session_id == "2026-08-10"
        assert decision.window_open_at == datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
        assert decision.window_close_at == datetime(2026, 8, 10, 20, 15, tzinfo=UTC)

    def test_weekend_is_not_due_via_genuine_calendar_evidence_not_a_weekday_check(self) -> None:
        sessions = _calendar()
        saturday = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=saturday,
            capability_supported=True,
            captured_session_ids=frozenset({"2026-08-07"}),  # Friday already handled
        )
        assert decision.status is OptionsCaptureDueStatus.NOT_DUE
        assert "NOT_A_TRADING_SESSION" in decision.reasons

    def test_exchange_holiday_is_not_due_via_genuine_calendar_evidence(self) -> None:
        # Thanksgiving 2026-11-26 itself: no XNYS session at all.
        sessions = ReviewedXNYSCalendar(start_date="2026-11-20", end_date="2026-11-30").sessions
        assert not any(item.session_date == "2026-11-26" for item in sessions)
        holiday = datetime(2026, 11, 26, 18, 0, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=holiday,
            capability_supported=True,
            captured_session_ids=frozenset({"2026-11-25"}),
        )
        assert decision.status is OptionsCaptureDueStatus.NOT_DUE
        assert "NOT_A_TRADING_SESSION" in decision.reasons

    def test_early_close_session_is_due_inside_its_own_narrowed_window(self) -> None:
        sessions = ReviewedXNYSCalendar(start_date="2026-11-20", end_date="2026-11-30").sessions
        early = next(item for item in sessions if item.session_date == "2026-11-27")
        now = early.closes_at + timedelta(minutes=1)
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=now, capability_supported=True
        )
        assert decision.status is OptionsCaptureDueStatus.DUE
        assert decision.session_id == "2026-11-27"

    def test_before_window_on_a_genuine_session_day_is_not_due(self) -> None:
        sessions = _calendar()
        midday = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)  # market open, well before close
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=midday,
            capability_supported=True,
            # Isolate "before today's window" from "an earlier session's
            # window was missed" (Friday 2026-08-07 predates this range and
            # would otherwise correctly dominate -- see the missed-window
            # tests above for that exact behavior, proven separately).
            captured_session_ids=frozenset({"2026-08-07"}),
        )
        assert decision.status is OptionsCaptureDueStatus.NOT_DUE
        assert decision.reasons == ("BEFORE_CAPTURE_WINDOW",)
        assert decision.session_id == "2026-08-10"

    def test_exactly_at_window_boundaries_is_due_inclusive(self) -> None:
        sessions = _calendar()
        at_open = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=datetime(2026, 8, 10, 20, 0, tzinfo=UTC),
            capability_supported=True,
        )
        at_close = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=datetime(2026, 8, 10, 20, 15, tzinfo=UTC),
            capability_supported=True,
        )
        assert at_open.status is OptionsCaptureDueStatus.DUE
        assert at_close.status is OptionsCaptureDueStatus.DUE

    def test_one_second_past_window_close_is_missed_not_due(self) -> None:
        sessions = _calendar()
        just_after = datetime(2026, 8, 10, 20, 15, 1, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=just_after, capability_supported=True
        )
        assert decision.status is OptionsCaptureDueStatus.MISSED_CAPTURE_WINDOW
        assert decision.reasons == ("CAPTURE_WINDOW_ALREADY_PASSED",)

    def test_missed_window_persists_until_captured_or_a_new_session_opens(self) -> None:
        sessions = _calendar()
        # Well after Monday's window closed, before Tuesday's opens.
        later_same_day = datetime(2026, 8, 10, 23, 0, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=later_same_day, capability_supported=True
        )
        assert decision.status is OptionsCaptureDueStatus.MISSED_CAPTURE_WINDOW
        assert decision.session_id == "2026-08-10"
        # Options Capture Temporal Reevaluation Correctness v2: the reported
        # single most-relevant session stays Monday's missed one (unchanged
        # priority), but the raw "when could this next become due" fact
        # already correctly points at Tuesday's own window.
        assert decision.next_capture_window_open_at == datetime(2026, 8, 11, 20, 0, tzinfo=UTC)

    def test_same_session_already_captured_is_reported_distinctly_from_due(self) -> None:
        sessions = _calendar()
        now = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=now,
            capability_supported=True,
            captured_session_ids=frozenset({"2026-08-10"}),
        )
        assert decision.status is OptionsCaptureDueStatus.ALREADY_CAPTURED
        assert decision.reasons == ("ALREADY_CAPTURED_THIS_SESSION",)

    def test_capability_unavailable_short_circuits_regardless_of_calendar(self) -> None:
        sessions = _calendar()
        now = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)  # would otherwise be DUE
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=now, capability_supported=False
        )
        assert decision.status is OptionsCaptureDueStatus.UNAVAILABLE
        assert decision.session_id is None

    def test_now_must_be_timezone_aware(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            determine_options_capture_due_state(
                sessions=(),
                policy=_policy(),
                now=datetime(2026, 8, 10, 20, 5),
                capability_supported=True,
            )

    def test_no_session_evidence_in_bounded_range_is_not_due(self) -> None:
        decision = determine_options_capture_due_state(
            sessions=(), policy=_policy(), now=NOW, capability_supported=True
        )
        assert decision.status is OptionsCaptureDueStatus.NOT_DUE
        assert decision.reasons == ("NO_SESSION_EVIDENCE_IN_BOUNDED_RANGE",)
        assert decision.next_capture_window_open_at is None


class TestNextCaptureWindowOpenAt:
    """Options Capture Temporal Reevaluation Correctness v2: the raw "next
    eligible window" fact is orthogonal to whichever single session
    ``status``/``session_id`` reports -- proved across every branch of
    ``determine_options_capture_due_state``, independent of how a caller
    (``domain.operating_cycle``) later interprets it against its own
    horizon."""

    def test_before_window_reports_itself_as_the_next_window(self) -> None:
        sessions = _calendar()
        midday = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=midday,
            capability_supported=True,
            # Isolate "before today's window" from "an earlier session's
            # window was missed" -- see the identical isolation in
            # ``test_before_window_on_a_genuine_session_day_is_not_due``.
            captured_session_ids=frozenset({"2026-08-07"}),
        )
        assert decision.status is OptionsCaptureDueStatus.NOT_DUE
        assert decision.next_capture_window_open_at == decision.window_open_at
        assert decision.next_capture_window_open_at == datetime(2026, 8, 10, 20, 0, tzinfo=UTC)

    def test_currently_due_still_reports_the_following_sessions_window(self) -> None:
        sessions = _calendar()
        now = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=now, capability_supported=True
        )
        assert decision.status is OptionsCaptureDueStatus.DUE
        assert decision.next_capture_window_open_at == datetime(2026, 8, 11, 20, 0, tzinfo=UTC)

    def test_already_captured_reports_the_following_sessions_window(self) -> None:
        sessions = _calendar()
        now = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=now,
            capability_supported=True,
            captured_session_ids=frozenset({"2026-08-10"}),
        )
        assert decision.status is OptionsCaptureDueStatus.ALREADY_CAPTURED
        assert decision.next_capture_window_open_at == datetime(2026, 8, 11, 20, 0, tzinfo=UTC)

    def test_friday_to_monday_transition_skips_the_weekend_correctly(self) -> None:
        """Friday 2026-08-07's own session closes missed; the next eligible
        window must be Monday 2026-08-10's -- never Saturday/Sunday, and
        never a naive "add one calendar day" -- proving this reads genuine
        calendar evidence, not a weekday-arithmetic assumption."""

        sessions = _calendar()
        friday_close = next(
            item for item in sessions if item.session_date == "2026-08-07"
        ).closes_at
        just_after_friday_window = friday_close + timedelta(minutes=16)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=just_after_friday_window,
            capability_supported=True,
        )
        assert decision.status is OptionsCaptureDueStatus.MISSED_CAPTURE_WINDOW
        assert decision.session_id == "2026-08-07"
        assert decision.next_capture_window_open_at == datetime(2026, 8, 10, 20, 0, tzinfo=UTC)

    def test_no_further_session_within_the_bounded_range_is_none(self) -> None:
        sessions = _calendar(start="2026-08-01", end="2026-08-10")
        # The very last session in this deliberately narrow range: nothing
        # further exists within the bounded calendar evidence at all.
        after_last_session = datetime(2026, 8, 10, 23, 0, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions,
            policy=_policy(),
            now=after_last_session,
            capability_supported=True,
        )
        assert decision.status is OptionsCaptureDueStatus.MISSED_CAPTURE_WINDOW
        assert decision.next_capture_window_open_at is None

    def test_capability_unavailable_reports_no_next_window(self) -> None:
        sessions = _calendar()
        now = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
        decision = determine_options_capture_due_state(
            sessions=sessions, policy=_policy(), now=now, capability_supported=False
        )
        assert decision.status is OptionsCaptureDueStatus.UNAVAILABLE
        assert decision.next_capture_window_open_at is None


class TestSessionIdForCapturedAt:
    def test_a_genuinely_fresh_capture_resolves_to_its_session(self) -> None:
        sessions = _calendar()
        captured_at = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
        assert (
            session_id_for_captured_at(captured_at=captured_at, sessions=sessions, policy=_policy())
            == "2026-08-10"
        )

    def test_a_provider_timestamp_anomaly_outside_every_window_resolves_to_none(self) -> None:
        sessions = _calendar()
        # ~48h after Friday's close: mirrors the retained 2026-08-08
        # Saturday stale-evidence-acceptance reproduction -- must never be
        # attributed to any session.
        anomalous = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
        assert (
            session_id_for_captured_at(captured_at=anomalous, sessions=sessions, policy=_policy())
            is None
        )


# --------------------------------------------------------------------------
# Chain-coverage-quality policy
# --------------------------------------------------------------------------


def _dense_selection(max_contracts: int = 200):
    candidates = []
    for dte in (3, 7, 14, 21, 30, 45, 60, 90):
        expiration = (NOW.date() + timedelta(days=dte)).isoformat()
        for offset in range(-20, 20):
            strike = Decimal("500") + Decimal(offset) * Decimal("5")
            if strike <= 0:
                continue
            for option_type in (OptionType.CALL, OptionType.PUT):
                side = "C" if option_type is OptionType.CALL else "P"
                date_part = expiration[2:].replace("-", "")
                symbol = f"SPY{date_part}{side}{int(strike * 1000):08d}"
                candidates.append(ChainSampleCandidate(symbol, expiration, option_type, strike))
    return select_contracts_stratified(
        tuple(candidates),
        underlying_price=Decimal("500"),
        captured_at=NOW,
        max_contracts=max_contracts,
    )


class TestChainCoverageQuality:
    def test_dense_chain_with_full_budget_is_good_or_partial_never_starved(self) -> None:
        selection = _dense_selection(max_contracts=200)
        diagnostic = build_chain_coverage_diagnostic(
            underlying_symbol="SPY",
            underlying_canonical_security_id="sec-spy",
            contracts_returned_by_provider=selection.contracts_available,
            selection=selection,
            policy=ChainCoverageQualityPolicy.create(),
        )
        assert diagnostic.expiration_count_selected == 8
        assert diagnostic.call_count_selected > 0
        assert diagnostic.put_count_selected > 0
        assert diagnostic.quality is not ChainCoverageQuality.INSUFFICIENT_COVERAGE

    def test_heavy_cap_pressure_is_reported_as_cap_constrained(self) -> None:
        selection = _dense_selection(max_contracts=50)
        diagnostic = build_chain_coverage_diagnostic(
            underlying_symbol="SPY",
            underlying_canonical_security_id="sec-spy",
            contracts_returned_by_provider=selection.contracts_available,
            selection=selection,
            policy=ChainCoverageQualityPolicy.create(cap_pressure_threshold="0.30"),
        )
        assert diagnostic.quality is ChainCoverageQuality.CAP_CONSTRAINED
        assert diagnostic.contracts_excluded_by_cap > 0

    def test_narrow_single_expiration_chain_is_insufficient_coverage(self) -> None:
        candidates = tuple(
            ChainSampleCandidate(
                f"SPY260817C{int(Decimal('500') + i):08d}000",
                "2026-08-17",
                OptionType.CALL,
                Decimal("500") + i,
            )
            for i in range(3)
        )
        selection = select_contracts_stratified(
            candidates, underlying_price=Decimal("500"), captured_at=NOW, max_contracts=200
        )
        diagnostic = build_chain_coverage_diagnostic(
            underlying_symbol="SPY",
            underlying_canonical_security_id="sec-spy",
            contracts_returned_by_provider=3,
            selection=selection,
            policy=ChainCoverageQualityPolicy.create(),
        )
        assert diagnostic.quality is ChainCoverageQuality.INSUFFICIENT_COVERAGE
        assert "MISSING_CALL_OR_PUT_REPRESENTATION" in diagnostic.quality_reasons

    def test_full_retention_with_no_cap_pressure_is_good_coverage(self) -> None:
        selection = _dense_selection(max_contracts=10_000)  # never actually binds
        diagnostic = build_chain_coverage_diagnostic(
            underlying_symbol="SPY",
            underlying_canonical_security_id="sec-spy",
            contracts_returned_by_provider=selection.contracts_available,
            selection=selection,
            policy=ChainCoverageQualityPolicy.create(),
        )
        assert diagnostic.contracts_excluded_by_cap == 0
        assert diagnostic.quality is ChainCoverageQuality.GOOD_COVERAGE

    def test_policy_thresholds_are_validated(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            ChainCoverageQualityPolicy.create(minimum_expiration_count_selected=0)
        # Zero fails the shared ``parse_decimal(..., allow_zero=False)``
        # authoritative-decimal guard itself (the same codebase-wide
        # convention ``OptionsFeePolicy``/``OptionsSlippagePolicy`` already
        # rely on), not this policy's own bound check.
        with pytest.raises(ValueValidationError):
            ChainCoverageQualityPolicy.create(cap_pressure_threshold="0")
        with pytest.raises(OptionsResearchOperationsError):
            ChainCoverageQualityPolicy.create(cap_pressure_threshold="1.5")


# --------------------------------------------------------------------------
# Distinct-session history readiness
# --------------------------------------------------------------------------


class TestOptionsHistoryReadiness:
    def test_no_snapshots_is_insufficient_history(self) -> None:
        readiness = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy", session_ids=()
        )
        assert readiness.state is ResearchReadinessState.INSUFFICIENT_HISTORY
        assert readiness.distinct_session_count == 0

    def test_one_distinct_session_is_current_snapshot_available(self) -> None:
        readiness = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy", session_ids=("2026-08-10",)
        )
        assert readiness.state is ResearchReadinessState.CURRENT_SNAPSHOT_AVAILABLE
        assert readiness.distinct_session_count == 1

    def test_same_session_replay_does_not_inflate_distinct_count(self) -> None:
        readiness = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy",
            session_ids=("2026-08-10", "2026-08-10", "2026-08-10"),
        )
        assert readiness.distinct_session_count == 1
        assert readiness.valid_snapshot_count == 3
        assert readiness.state is ResearchReadinessState.CURRENT_SNAPSHOT_AVAILABLE

    def test_a_genuinely_new_session_advances_readiness(self) -> None:
        one_session = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy", session_ids=("2026-08-10",)
        )
        two_sessions = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy", session_ids=("2026-08-10", "2026-08-11")
        )
        assert one_session.distinct_session_count == 1
        assert two_sessions.distinct_session_count == 2
        assert two_sessions.state is ResearchReadinessState.FORWARD_HISTORY_ACCUMULATING
        assert two_sessions.latest_session_id == "2026-08-11"

    def test_minimum_threshold_reuses_the_existing_iv_rank_minimum(self) -> None:
        assert MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH == 20
        sessions = tuple(f"2026-08-{10 + i:02d}" for i in range(19))
        below = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy", session_ids=sessions
        )
        assert below.state is ResearchReadinessState.FORWARD_HISTORY_ACCUMULATING
        at_minimum = evaluate_options_history_readiness(
            underlying_canonical_security_id="sec-spy", session_ids=(*sessions, "2026-08-29")
        )
        assert at_minimum.state is ResearchReadinessState.LONGITUDINAL_RESEARCH_READY

    def test_minimum_required_sessions_must_be_positive(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            evaluate_options_history_readiness(
                underlying_canonical_security_id="sec-spy",
                session_ids=(),
                minimum_required_sessions=0,
            )


# --------------------------------------------------------------------------
# Retained checkpoint
# --------------------------------------------------------------------------


class TestOptionsForwardEvidenceCheckpoint:
    def _kwargs(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = dict(
            plan_id="plan-1",
            session_id="2026-08-10",
            capture_window_policy_id="policy-1",
            window_open_at=datetime(2026, 8, 10, 20, 0, tzinfo=UTC),
            window_close_at=datetime(2026, 8, 10, 20, 15, tzinfo=UTC),
            provider="ALPACA_OPTIONS_MARKET_DATA",
            feed="indicative",
            feed_entitlement_state="INDICATIVE_ONLY",
            underlying_symbols_captured=("SPY",),
            underlyings_requested_count=19,
            underlyings_captured_count=1,
            fresh_snapshot_ids=("snap-1",),
            stale_snapshot_count=0,
            quote_quality_counts=(("OBSERVED", 3),),
            chain_coverage_quality_counts=(("GOOD_COVERAGE", 1),),
            cap_constrained_underlyings=(),
            structure_snapshot_ids=("struct-snap-1",),
            structures_evaluated=4,
            structures_retained=2,
            fee_policy_id="fee-1",
            fee_evidence_status="EVIDENCED",
            opra_entitlement_state="INDICATIVE_ONLY",
            open_interest_limitation="OPEN_INTEREST_UNAVAILABLE",
            iv_history_minimum_required=20,
            iv_history_ready_underlying_count=0,
            forward_history_underlying_count=1,
            created_at=NOW,
        )
        base.update(overrides)
        return base

    def test_creates_with_authority_none(self) -> None:
        checkpoint = OptionsForwardEvidenceCheckpoint.create(**self._kwargs())
        assert checkpoint.authority == "NONE"
        assert checkpoint.checkpoint_id.startswith("options_forward_evidence_checkpoint_")

    def test_rejects_captured_exceeding_requested(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            OptionsForwardEvidenceCheckpoint.create(
                **self._kwargs(underlyings_requested_count=1, underlyings_captured_count=2)
            )

    def test_rejects_retained_exceeding_evaluated(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            OptionsForwardEvidenceCheckpoint.create(
                **self._kwargs(structures_evaluated=1, structures_retained=2)
            )

    def test_identical_stable_evidence_is_idempotent(self) -> None:
        first = OptionsForwardEvidenceCheckpoint.create(**self._kwargs())
        second = OptionsForwardEvidenceCheckpoint.create(**self._kwargs())
        assert first.checkpoint_id == second.checkpoint_id

    def test_coverage_quality_fluctuation_alone_does_not_change_identity(self) -> None:
        # See AlpacaOptionsResearchEvidenceCapture: a reused (not freshly
        # captured) underlying cannot recompute its coverage diagnostic, so
        # this field alone must not be part of checkpoint identity.
        first = OptionsForwardEvidenceCheckpoint.create(
            **self._kwargs(chain_coverage_quality_counts=(("GOOD_COVERAGE", 1),))
        )
        second = OptionsForwardEvidenceCheckpoint.create(
            **self._kwargs(chain_coverage_quality_counts=())
        )
        assert first.checkpoint_id == second.checkpoint_id

    def test_a_new_underlying_captured_changes_identity(self) -> None:
        first = OptionsForwardEvidenceCheckpoint.create(**self._kwargs())
        second = OptionsForwardEvidenceCheckpoint.create(
            **self._kwargs(
                underlying_symbols_captured=("SPY", "QQQ"),
                underlyings_captured_count=2,
                fresh_snapshot_ids=("snap-1", "snap-2"),
            )
        )
        assert first.checkpoint_id != second.checkpoint_id


# --------------------------------------------------------------------------
# Storage projection
# --------------------------------------------------------------------------


class TestStorageProjection:
    def test_projects_the_declared_horizons_from_genuine_capture_size(self) -> None:
        projections = project_forward_storage(observations_per_session=3668)
        by_sessions = {item.sessions: item.projected_observations for item in projections}
        assert by_sessions[20] == 73_360
        assert by_sessions[60] == 220_080
        assert by_sessions[252] == 924_336

    def test_rejects_invalid_inputs(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            project_forward_storage(observations_per_session=-1)
        with pytest.raises(OptionsResearchOperationsError):
            project_forward_storage(observations_per_session=100, sessions=(0,))


# --------------------------------------------------------------------------
# Cross-sectional baseline -- descriptive only, never a hypothesis test
# --------------------------------------------------------------------------


class TestOptionsCrossSectionalBaseline:
    def test_builds_descriptive_distributions_not_a_ranking(self) -> None:
        baseline = build_options_cross_sectional_baseline(
            session_id="2026-08-10",
            created_at=NOW,
            underlying_count=3,
            metric_values=(
                ("ATM_IV", (Decimal("0.18"), Decimal("0.22"), Decimal("0.31"))),
                ("SKEW_TARGET_DELTA", (Decimal("0.02"), Decimal("0.05"))),
            ),
            quality_counts=(("GOOD_COVERAGE", 3),),
        )
        assert baseline.authority == "NONE"
        atm_iv = next(item for item in baseline.distributions if item.metric == "ATM_IV")
        assert atm_iv.count == 3
        assert atm_iv.minimum == Decimal("0.18")
        assert atm_iv.maximum == Decimal("0.31")
        assert atm_iv.median == Decimal("0.22")
        assert (
            "DESCRIPTIVE CROSS-SECTIONAL BASELINE ONLY -- NOT A HYPOTHESIS TEST"
            in baseline.warnings
        )
        assert any("BEST TRADE" in warning for warning in baseline.warnings)

    def test_empty_metric_values_report_zero_count_not_a_fabricated_stat(self) -> None:
        baseline = build_options_cross_sectional_baseline(
            session_id="2026-08-10",
            created_at=NOW,
            underlying_count=1,
            metric_values=(("ATM_IV", ()),),
            quality_counts=(),
        )
        atm_iv = baseline.distributions[0]
        assert atm_iv.count == 0
        assert atm_iv.mean is None
        assert atm_iv.minimum is None

    def test_requires_at_least_one_underlying(self) -> None:
        with pytest.raises(OptionsResearchOperationsError):
            build_options_cross_sectional_baseline(
                session_id="2026-08-10",
                created_at=NOW,
                underlying_count=0,
                metric_values=(),
                quality_counts=(),
            )
