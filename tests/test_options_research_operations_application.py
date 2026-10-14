"""Application-layer proofs for Fresh Options Evidence Operations and
Structure Research Baseline v1: due-state resolution from retained
capability/checkpoint evidence, idempotent checkpoint recording, and
distinct-session history readiness -- all against the in-memory
repositories, no Postgres required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.options_research_operations import (
    ComputeOptionsHistoryReadiness,
    DetermineOptionsResearchCaptureDueState,
    InMemoryOptionsResearchOperationsRepository,
    RecordOptionsForwardEvidenceCheckpoint,
)
from apex.domain.options_intelligence import CapabilityState, OptionProviderCapabilities
from apex.domain.options_quote_intelligence import OptionsQuoteProviderCapabilitySnapshot
from apex.domain.options_research_operations import (
    OptionsCaptureDueStatus,
    OptionsCaptureWindowPolicy,
    OptionsForwardEvidenceCheckpoint,
    ResearchReadinessState,
)

NOW = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
PROVIDER = "ALPACA_OPTIONS_MARKET_DATA"


def _capability(*, credentials_available: bool) -> OptionsQuoteProviderCapabilitySnapshot:
    capability = OptionProviderCapabilities.create(
        provider=PROVIDER,
        contract_discovery=CapabilityState.AVAILABLE,
        quotes=CapabilityState.AVAILABLE,
        trades=CapabilityState.AVAILABLE,
        snapshots=CapabilityState.AVAILABLE,
        implied_volatility=CapabilityState.AVAILABLE,
        greeks=CapabilityState.AVAILABLE,
        open_interest=CapabilityState.UNAVAILABLE,
        volume=CapabilityState.AVAILABLE,
        limitations=(),
    )
    return OptionsQuoteProviderCapabilitySnapshot.create(
        checked_at=NOW,
        credentials_available=credentials_available,
        requests_made=3 if credentials_available else 0,
        capability=capability,
        unavailable_reason=None if credentials_available else "ALPACA_CREDENTIALS_ABSENT",
    )


class FakeCapabilityReader:
    def __init__(self, snapshot: OptionsQuoteProviderCapabilitySnapshot | None) -> None:
        self._snapshot = snapshot

    def latest_capability(self, provider: str) -> OptionsQuoteProviderCapabilitySnapshot | None:
        return self._snapshot if provider == PROVIDER else None


def _checkpoint(session_id: str, plan_id: str = "plan-1") -> OptionsForwardEvidenceCheckpoint:
    return OptionsForwardEvidenceCheckpoint.create(
        plan_id=plan_id,
        session_id=session_id,
        capture_window_policy_id="policy-1",
        window_open_at=NOW - timedelta(minutes=5),
        window_close_at=NOW,
        provider=PROVIDER,
        feed="indicative",
        feed_entitlement_state="INDICATIVE_ONLY",
        underlying_symbols_captured=("SPY",),
        underlyings_requested_count=1,
        underlyings_captured_count=1,
        fresh_snapshot_ids=(f"snap-{session_id}",),
        stale_snapshot_count=0,
        quote_quality_counts=(),
        chain_coverage_quality_counts=(),
        cap_constrained_underlyings=(),
        structure_snapshot_ids=(),
        structures_evaluated=0,
        structures_retained=0,
        fee_policy_id="fee-1",
        fee_evidence_status="EVIDENCED",
        opra_entitlement_state="INDICATIVE_ONLY",
        open_interest_limitation="OPEN_INTEREST_UNAVAILABLE",
        iv_history_minimum_required=20,
        iv_history_ready_underlying_count=0,
        forward_history_underlying_count=1,
        created_at=NOW,
    )


class TestDetermineOptionsResearchCaptureDueState:
    def test_credentials_absent_is_unavailable(self) -> None:
        checkpoint_repo = InMemoryOptionsResearchOperationsRepository()
        service = DetermineOptionsResearchCaptureDueState(
            checkpoint_repository=checkpoint_repo,
            capability_reader=FakeCapabilityReader(_capability(credentials_available=False)),
        )
        sessions = ReviewedXNYSCalendar(start_date="2026-08-01", end_date="2026-08-15").sessions
        decision = service.execute(
            plan_id="plan-1",
            provider=PROVIDER,
            sessions=sessions,
            policy=OptionsCaptureWindowPolicy.create(),
            now=NOW,
        )
        assert decision.status is OptionsCaptureDueStatus.UNAVAILABLE

    def test_no_capability_evidence_at_all_is_unavailable(self) -> None:
        checkpoint_repo = InMemoryOptionsResearchOperationsRepository()
        service = DetermineOptionsResearchCaptureDueState(
            checkpoint_repository=checkpoint_repo, capability_reader=FakeCapabilityReader(None)
        )
        sessions = ReviewedXNYSCalendar(start_date="2026-08-01", end_date="2026-08-15").sessions
        decision = service.execute(
            plan_id="plan-1",
            provider=PROVIDER,
            sessions=sessions,
            policy=OptionsCaptureWindowPolicy.create(),
            now=NOW,
        )
        assert decision.status is OptionsCaptureDueStatus.UNAVAILABLE

    def test_completed_session_from_checkpoint_repository_is_already_captured(self) -> None:
        checkpoint_repo = InMemoryOptionsResearchOperationsRepository()
        checkpoint_repo.save_checkpoint(_checkpoint("2026-08-10"))
        service = DetermineOptionsResearchCaptureDueState(
            checkpoint_repository=checkpoint_repo,
            capability_reader=FakeCapabilityReader(_capability(credentials_available=True)),
        )
        sessions = ReviewedXNYSCalendar(start_date="2026-08-01", end_date="2026-08-15").sessions
        decision = service.execute(
            plan_id="plan-1",
            provider=PROVIDER,
            sessions=sessions,
            policy=OptionsCaptureWindowPolicy.create(),
            now=NOW,
        )
        assert decision.status is OptionsCaptureDueStatus.ALREADY_CAPTURED

    def test_a_different_plans_checkpoint_does_not_leak_across_plans(self) -> None:
        checkpoint_repo = InMemoryOptionsResearchOperationsRepository()
        checkpoint_repo.save_checkpoint(_checkpoint("2026-08-10", plan_id="other-plan"))
        service = DetermineOptionsResearchCaptureDueState(
            checkpoint_repository=checkpoint_repo,
            capability_reader=FakeCapabilityReader(_capability(credentials_available=True)),
        )
        sessions = ReviewedXNYSCalendar(start_date="2026-08-01", end_date="2026-08-15").sessions
        decision = service.execute(
            plan_id="plan-1",
            provider=PROVIDER,
            sessions=sessions,
            policy=OptionsCaptureWindowPolicy.create(),
            now=NOW,
        )
        assert decision.status is OptionsCaptureDueStatus.DUE


class TestRecordOptionsForwardEvidenceCheckpoint:
    def test_idempotent_recording_returns_the_retained_row(self) -> None:
        repo = InMemoryOptionsResearchOperationsRepository()
        recorder = RecordOptionsForwardEvidenceCheckpoint(repo)
        checkpoint = _checkpoint("2026-08-10")
        first = recorder.execute(checkpoint=checkpoint)
        second = recorder.execute(checkpoint=checkpoint)
        assert first.checkpoint_id == second.checkpoint_id
        assert len(repo.checkpoints_for_plan("plan-1")) == 1

    def test_completed_session_ids_requires_full_underlying_coverage(self) -> None:
        repo = InMemoryOptionsResearchOperationsRepository()
        partial = OptionsForwardEvidenceCheckpoint.create(
            plan_id="plan-1",
            session_id="2026-08-07",
            capture_window_policy_id="policy-1",
            window_open_at=NOW - timedelta(days=3),
            window_close_at=NOW - timedelta(days=3),
            provider=PROVIDER,
            feed="indicative",
            feed_entitlement_state="INDICATIVE_ONLY",
            underlying_symbols_captured=("SPY",),
            underlyings_requested_count=19,
            underlyings_captured_count=1,
            fresh_snapshot_ids=("snap-partial",),
            stale_snapshot_count=0,
            quote_quality_counts=(),
            chain_coverage_quality_counts=(),
            cap_constrained_underlyings=(),
            structure_snapshot_ids=(),
            structures_evaluated=0,
            structures_retained=0,
            fee_policy_id="fee-1",
            fee_evidence_status="EVIDENCED",
            opra_entitlement_state="INDICATIVE_ONLY",
            open_interest_limitation="OPEN_INTEREST_UNAVAILABLE",
            iv_history_minimum_required=20,
            iv_history_ready_underlying_count=0,
            forward_history_underlying_count=1,
            created_at=NOW,
        )
        repo.save_checkpoint(partial)
        assert repo.completed_session_ids("plan-1") == frozenset()
        repo.save_checkpoint(_checkpoint("2026-08-10"))
        assert repo.completed_session_ids("plan-1") == frozenset({"2026-08-10"})


class FakeSnapshot:
    def __init__(self, captured_at: datetime) -> None:
        self.captured_at = captured_at


class FakeSnapshotReader:
    def __init__(self, snapshots: tuple[FakeSnapshot, ...]) -> None:
        self._snapshots = snapshots

    def snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[FakeSnapshot, ...]:
        return self._snapshots


class TestComputeOptionsHistoryReadiness:
    def test_only_snapshots_inside_a_genuine_session_window_count(self) -> None:
        sessions = ReviewedXNYSCalendar(start_date="2026-08-01", end_date="2026-08-15").sessions
        fresh = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)  # inside Monday's window
        stale_reproduction = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)  # Saturday, no session
        reader = FakeSnapshotReader((FakeSnapshot(fresh), FakeSnapshot(stale_reproduction)))
        service = ComputeOptionsHistoryReadiness(quote_repository=reader)  # type: ignore[arg-type]
        readiness = service.execute(
            underlying_canonical_security_id="sec-spy",
            sessions=sessions,
            policy=OptionsCaptureWindowPolicy.create(),
        )
        assert readiness.distinct_session_count == 1
        assert readiness.valid_snapshot_count == 1
        assert readiness.state is ResearchReadinessState.CURRENT_SNAPSHOT_AVAILABLE
