"""Genuine orchestration proofs for ``AlpacaOptionsResearchEvidenceCapture``
and ``resolve_options_capture_due_decision`` (Fresh Options Evidence
Operations and Structure Research Baseline v1) -- fresh evidence accepted,
same-session replay reused (zero further provider requests, no duplicate
snapshots/structures), provider/credential failure isolated per underlying,
and the retained checkpoint genuinely reflects every outcome. No network,
no Postgres: a fake capture client plus the in-memory repositories every
other options application-layer test file already uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.alpaca_options_quote_source import (
    ParsedAlpacaOptionSnapshotEntry,
    UnderlyingCaptureOutcome,
)
from apex.adapters.options_research_capture import (
    AlpacaOptionsResearchEvidenceCapture,
    OptionsResearchCaptureError,
    OptionsResearchEvidenceCaptureCommand,
    resolve_options_capture_due_decision,
)
from apex.application.options_quote_intelligence import (
    InMemoryOptionsQuoteIntelligenceRepository,
    RealizedVolatilityResolution,
)
from apex.application.options_research_operations import (
    InMemoryOptionsResearchOperationsRepository,
)
from apex.application.options_structures import InMemoryOptionsStructuresRepository
from apex.domain.options_intelligence import OptionType
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    ChainSampleCandidate,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionsChainSnapshot,
    select_contracts_stratified,
)
from apex.domain.options_research_operations import (
    ChainCoverageQualityPolicy,
    OptionsCaptureDueDecision,
    OptionsCaptureDueStatus,
    OptionsCaptureWindowPolicy,
)
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
    StructurePricingPolicy,
)
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember
from apex.domain.securities import SecuritiesCalendarSession

NOW = datetime(2026, 8, 10, 20, 5, tzinfo=UTC)
WINDOW_OPEN = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
WINDOW_CLOSE = datetime(2026, 8, 10, 20, 15, tzinfo=UTC)
SESSION_ID = "2026-08-10"


def _session() -> SecuritiesCalendarSession:
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="test",
        timezone="America/New_York",
        session_date=SESSION_ID,
        opens_at=datetime(2026, 8, 10, 13, 30, tzinfo=UTC),
        closes_at=WINDOW_OPEN,
        is_early_close=False,
        source_identity="test-fixture",
    )


def _due(
    *, status: OptionsCaptureDueStatus = OptionsCaptureDueStatus.DUE
) -> OptionsCaptureDueDecision:
    if status is OptionsCaptureDueStatus.DUE:
        return OptionsCaptureDueDecision(
            status, SESSION_ID, WINDOW_OPEN, WINDOW_CLOSE, ("DUE",), None
        )
    return OptionsCaptureDueDecision(status, None, None, None, ("NOT_DUE",), None)


def _plan() -> OptionChainCapturePlan:
    members = tuple(
        ResolvedCohortMember(
            stable_security_id=f"security-{symbol.lower()}-stable",
            symbol=symbol,
            sector=CohortSector.BROAD_MARKET,
            roles=(CohortRole.BROAD_INDEX_ETF,),
            asset_class="ETF",
        )
        for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS
    )
    return OptionChainCapturePlan.create(
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        members=members,
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=NOW,
    )


def _occ_symbol(root: str, expiration_iso: str, option_type: OptionType, strike: Decimal) -> str:
    yy, mm, dd = expiration_iso.split("-")
    side = "C" if option_type is OptionType.CALL else "P"
    strike_digits = f"{int(strike * 1000):08d}"
    return f"{root}{yy[2:]}{mm}{dd}{side}{strike_digits}"


def _entry(symbol: str, bid: str, ask: str) -> ParsedAlpacaOptionSnapshotEntry:
    return ParsedAlpacaOptionSnapshotEntry(
        contract_symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("50"),
        ask_size=Decimal("50"),
        last_trade=Decimal(bid),
        trade_size=Decimal("5"),
        daily_volume=Decimal("100"),
        implied_volatility=Decimal("0.22"),
        delta=Decimal("0.45"),
        gamma=Decimal("0.02"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        rho=Decimal("0.01"),
        quote_timestamp=NOW,
    )


def _underlying_price() -> Decimal:
    return Decimal("500")


def _expiration() -> str:
    return (NOW + timedelta(days=20)).date().isoformat()


def _build_outcome(symbol: str, stable_id: str) -> UnderlyingCaptureOutcome:
    """A small, genuine two-strike call chain -- enough for
    ``select_contracts_stratified`` to run for real and for
    ``generate_structure_candidates`` to find one valid vertical."""

    expiration = _expiration()
    price = _underlying_price()
    strikes = (price - Decimal("5"), price, price + Decimal("5"))
    entries: dict[str, ParsedAlpacaOptionSnapshotEntry] = {}
    candidates = []
    for strike in strikes:
        occ = _occ_symbol(symbol, expiration, OptionType.CALL, strike)
        entries[occ] = (
            _entry(occ, "8.00", "8.20")
            if strike == strikes[0]
            else _entry(occ, "4.00", "4.20")
            if strike == strikes[1]
            else _entry(occ, "1.50", "1.70")
        )
        candidates.append(ChainSampleCandidate(occ, expiration, OptionType.CALL, strike))
    selection = select_contracts_stratified(
        tuple(candidates), underlying_price=price, captured_at=NOW, max_contracts=200
    )
    contract_entries = tuple(entries[s] for s in selection.selected_contract_symbols)
    return UnderlyingCaptureOutcome(
        symbol,
        stable_id,
        "CAPTURED",
        price,
        "ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        NOW,
        contract_entries,
        2,
        4096,
        None,
        (f"receipt-{symbol}",),
        selection,
        len(entries),
    )


@dataclass
class FakeCaptureClient:
    credentials_available: bool = True
    outcomes: dict[str, UnderlyingCaptureOutcome] | None = None
    calls: list[str] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def capture_underlying(self, *, member, bounds, feed, captured_at, timeout_seconds=15):
        assert self.calls is not None
        self.calls.append(member.symbol)
        if self.outcomes and member.symbol in self.outcomes:
            return self.outcomes[member.symbol]
        return UnderlyingCaptureOutcome(
            member.symbol,
            member.stable_security_id,
            "EMPTY",
            None,
            None,
            None,
            (),
            1,
            256,
            "NO_CONTRACTS_IN_BOUNDED_RANGE",
            (f"receipt-{member.symbol}",),
            None,
            0,
        )


class FakeRealizedVolatilityResolver:
    def resolve(self, *, stable_security_id: str, as_of: datetime) -> RealizedVolatilityResolution:
        return RealizedVolatilityResolution(
            "HISTORICAL_DATASET_UNAVAILABLE", 20, None, 60, None, None, 0, None, ("FAKE",)
        )


def _policies(structures_repo: InMemoryOptionsStructuresRepository):
    pricing = structures_repo.save_pricing_policy(StructurePricingPolicy.create())
    fee = structures_repo.save_fee_policy(OptionsFeePolicy.create())
    slippage = structures_repo.save_slippage_policy(OptionsSlippagePolicy.create())
    structure = structures_repo.save_structure_policy(
        OptionsStructureResearchPolicy.create(
            pricing_policy=pricing, fee_policy=fee, slippage_policy=slippage, created_at=NOW
        )
    )
    return fee, slippage, structure


def _capture(
    quote_repo=None,
    structures_repo=None,
    checkpoint_repo=None,
    client: FakeCaptureClient | None = None,
):
    quote_repo = quote_repo or InMemoryOptionsQuoteIntelligenceRepository()
    structures_repo = structures_repo or InMemoryOptionsStructuresRepository()
    checkpoint_repo = checkpoint_repo or InMemoryOptionsResearchOperationsRepository()
    client = client or FakeCaptureClient()
    capture = AlpacaOptionsResearchEvidenceCapture(
        quote_repository=quote_repo,
        structures_repository=structures_repo,
        checkpoint_repository=checkpoint_repo,
        rv_resolver=FakeRealizedVolatilityResolver(),  # type: ignore[arg-type]
        capture_client=client,  # type: ignore[arg-type]
    )
    return capture, quote_repo, structures_repo, checkpoint_repo, client


def _command(structures_repo, *, feed_state: str = "INDICATIVE_ONLY_OPRA_UNCONFIRMED"):
    fee_policy, slippage_policy, structure_policy = _policies(structures_repo)
    return OptionsResearchEvidenceCaptureCommand(
        plan=_plan(),
        due=_due(),
        window_policy=OptionsCaptureWindowPolicy.create(),
        sessions=(_session(),),
        coverage_policy=ChainCoverageQualityPolicy.create(),
        structure_policy=structure_policy,
        fee_policy=fee_policy,
        slippage_policy=slippage_policy,
        feed_entitlement_state=feed_state,
        now=NOW,
    )


class TestFreshCaptureAccepted:
    def test_fresh_capture_builds_snapshot_structures_and_checkpoint(self) -> None:
        plan = _plan()
        spy = next(m for m in plan.members if m.symbol == "SPY")
        outcome = _build_outcome("SPY", spy.stable_security_id)
        capture, quote_repo, structures_repo, _checkpoint_repo, _client = _capture(
            client=FakeCaptureClient(outcomes={"SPY": outcome})
        )
        command = _command(structures_repo)
        result = capture.run(command)

        checkpoint = result.checkpoint
        assert checkpoint.authority == "NONE"
        assert checkpoint.session_id == SESSION_ID
        assert checkpoint.underlyings_requested_count == len(plan.members)
        # Only SPY has a genuine "CAPTURED" outcome configured; every other
        # cohort member's default fake outcome is EMPTY (no contracts in
        # the bounded range) and correctly does not count as captured.
        assert checkpoint.underlyings_captured_count == 1
        assert checkpoint.underlying_symbols_captured == ("SPY",)
        spy_evidence = next(item for item in result.per_underlying if item.symbol == "SPY")
        assert spy_evidence.status == "CAPTURED_FRESH"
        assert spy_evidence.snapshot_id is not None
        assert spy_evidence.coverage is not None
        assert spy_evidence.structures_retained >= 1, (
            "a clean 3-strike call chain must yield ≥1 vertical"
        )
        snapshot = quote_repo.snapshot_by_id(spy_evidence.snapshot_id)
        assert isinstance(snapshot, OptionsChainSnapshot)
        assert snapshot.captured_at == NOW
        # Fresh capture: the source observations are all within the 900s
        # freshness bound (quote_timestamp == captured_at == NOW here).
        for observation_id in snapshot.observation_ids:
            observation = quote_repo.observation_by_id(observation_id)
            assert observation is not None
            assert observation.staleness_seconds == 0
        structures = structures_repo.structures_for_underlying(spy.stable_security_id)
        assert structures
        for structure in structures:
            assert structure.authority == "NONE"
            assert structure.quality.reasons.count("QUOTE_STALE") == 0, (
                "a structure built from a genuinely fresh capture must never carry QUOTE_STALE"
            )


class TestSameSessionReplay:
    def test_same_session_replay_reuses_snapshot_and_makes_no_further_requests(self) -> None:
        plan = _plan()
        spy = next(m for m in plan.members if m.symbol == "SPY")
        outcome = _build_outcome("SPY", spy.stable_security_id)
        client = FakeCaptureClient(outcomes={"SPY": outcome})
        capture, quote_repo, structures_repo, checkpoint_repo, _client = _capture(client=client)
        command = _command(structures_repo)

        first = capture.run(command)
        first_calls = list(client.calls or [])
        assert "SPY" in first_calls

        # Re-run within the identical still-open session window: SPY (the
        # only member with a genuine captured snapshot) must be recognized
        # as already captured and reused -- zero further provider requests
        # for it specifically. Every other member's fake outcome is EMPTY
        # (never produces a snapshot), so those are genuinely re-requested
        # each poll -- that is expected, not a same-session-idempotency bug.
        second = capture.run(command)
        assert client.calls is not None
        assert client.calls.count("SPY") == 1, (
            "SPY must be captured once, then reused, never re-fetched"
        )
        for symbol in set(first_calls) - {"SPY"}:
            assert client.calls.count(symbol) == 2
        spy_first = next(item for item in first.per_underlying if item.symbol == "SPY")
        spy_second = next(item for item in second.per_underlying if item.symbol == "SPY")
        assert spy_second.status == "REUSED_ALREADY_CAPTURED_THIS_SESSION"
        assert spy_second.snapshot_id == spy_first.snapshot_id
        assert len(quote_repo.snapshots_for_underlying(spy.stable_security_id)) == 1, (
            "same-session replay must never duplicate a snapshot"
        )
        # The checkpoint itself is idempotent: identical evidence -> identical id.
        assert first.checkpoint.checkpoint_id == second.checkpoint.checkpoint_id
        assert len(checkpoint_repo.checkpoints_for_plan(plan.plan_id)) == 1


class TestNotDueRefusal:
    def test_run_raises_when_not_genuinely_due(self) -> None:
        capture, _q, structures_repo, _c, _client = _capture()
        command = _command(structures_repo)
        not_due_command = OptionsResearchEvidenceCaptureCommand(
            plan=command.plan,
            due=_due(status=OptionsCaptureDueStatus.NOT_DUE),
            window_policy=command.window_policy,
            sessions=command.sessions,
            coverage_policy=command.coverage_policy,
            structure_policy=command.structure_policy,
            fee_policy=command.fee_policy,
            slippage_policy=command.slippage_policy,
            feed_entitlement_state=command.feed_entitlement_state,
            now=NOW,
        )
        with pytest.raises(OptionsResearchCaptureError, match="CAPTURE_INVOKED_WHILE_NOT_DUE"):
            capture.run(not_due_command)


class TestCredentialsAbsent:
    def test_credentials_absent_produces_blocked_evidence_without_raising(self) -> None:
        capture, _q, structures_repo, _c, _client = _capture(
            client=FakeCaptureClient(credentials_available=False)
        )
        command = _command(structures_repo)
        result = capture.run(command)
        assert result.checkpoint.underlyings_captured_count == 0
        assert all(item.status == "BLOCKED_CREDENTIALS_ABSENT" for item in result.per_underlying)
        assert "ALPACA_CREDENTIALS_ABSENT" in result.checkpoint.errors


class TestPartialProviderFailureIsolated:
    def test_one_underlying_provider_failure_does_not_block_others(self) -> None:
        plan = _plan()
        spy = next(m for m in plan.members if m.symbol == "SPY")
        qqq = next(m for m in plan.members if m.symbol == "QQQ")
        spy_outcome = _build_outcome("SPY", spy.stable_security_id)
        qqq_failure = UnderlyingCaptureOutcome(
            "QQQ",
            qqq.stable_security_id,
            "BLOCKED_PROVIDER_ERROR",
            None,
            None,
            None,
            (),
            1,
            128,
            "PROVIDER_TIMEOUT",
            (),
            None,
            0,
        )
        client = FakeCaptureClient(outcomes={"SPY": spy_outcome, "QQQ": qqq_failure})
        capture, _q, structures_repo, _c, _client = _capture(client=client)
        command = _command(structures_repo)
        result = capture.run(command)

        spy_evidence = next(item for item in result.per_underlying if item.symbol == "SPY")
        qqq_evidence = next(item for item in result.per_underlying if item.symbol == "QQQ")
        assert spy_evidence.status == "CAPTURED_FRESH"
        assert spy_evidence.snapshot_id is not None
        assert qqq_evidence.status == "BLOCKED_PROVIDER_ERROR"
        assert qqq_evidence.snapshot_id is None
        assert any(
            "QQQ" in item and "PROVIDER_TIMEOUT" in item for item in result.checkpoint.errors
        )


class TestResolveOptionsCaptureDueDecision:
    def test_no_retained_plan_is_unavailable(self) -> None:
        quote_repo = InMemoryOptionsQuoteIntelligenceRepository()
        checkpoint_repo = InMemoryOptionsResearchOperationsRepository()
        plan, _policy, _sessions, decision = resolve_options_capture_due_decision(
            quote_repository=quote_repo, checkpoint_repository=checkpoint_repo, as_of=NOW
        )
        assert plan is None
        assert decision.status is OptionsCaptureDueStatus.UNAVAILABLE
        assert decision.reasons == ("NO_RETAINED_OPTIONS_CAPTURE_PLAN",)
