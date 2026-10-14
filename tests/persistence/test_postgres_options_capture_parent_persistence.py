"""Real-Postgres regression suite for "Options Automatic Capture Parent
Persistence & Partial-Evidence Recovery v1".

The exact 2026-08-12 production defect: the unattended operating-cycle
capture path derived an ``OptionChainCaptureRun.run_id`` and referenced it
from ``options_chain_snapshot.run_id`` (a foreign key) without ever
persisting the corresponding ``options_chain_capture_run`` row -- so the
first genuine Alpaca capture (AAPL) persisted 200 real
``options_quote_observation`` rows and then failed with
``options_chain_snapshot_run_id_fkey`` violated. This file:

* proves the fixed automatic path (``AlpacaOptionsResearchEvidenceCapture.
  run``) builds the complete plan -> run -> observations -> snapshot ->
  structures -> checkpoint graph against real PostgreSQL FK constraints,
  starting from no pre-seeded run/snapshot/observation parents (only the
  plan -- an out-of-scope, unchanged, already-manual bootstrap step -- is
  created, and only via the same reviewed idempotent service the real
  bootstrap used);
* reproduces the exact 2026-08-12 evidence shape (observations durable,
  snapshot absent, no checkpoint, session not banked) against a *new*
  failure trigger (the real FK bug is fixed, so a controlled fault is
  injected at the identical boundary instead) and proves restart/replay
  recovers cleanly without duplicating anything;
* proves a later, distinct session is never contaminated by an earlier
  failed one's orphaned evidence;
* proves genuine provider-request accounting and alert behavior survive a
  downstream failure through the real operating-cycle dispatch path;
* proves the checkpoint task-count diagnostic reads live task results
  instead of a stale, once-per-cycle retained brief.

No network, no real Alpaca credentials: a fake, credentials-available
capture client stands in for the provider, but every persistence call in
these tests (except where a fault is deliberately injected) reaches real
PostgreSQL with its real foreign-key constraints enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.alpaca_options_quote_source import (
    ParsedAlpacaOptionSnapshotEntry,
    UnderlyingCaptureOutcome,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
from apex.adapters.options_quote_intelligence_postgres import (
    OptionsQuoteIntelligencePersistenceError,
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_research_capture import (
    AlpacaOptionsResearchEvidenceCapture,
    OptionsResearchEvidenceCaptureCommand,
)
from apex.adapters.options_research_operations_postgres import (
    PostgresOptionsResearchOperationsRepository,
)
from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.operating_cycle import (
    ExecuteOperatingCycle,
    RegisterOperatingCyclePlan,
    derive_operating_cycle_alert_candidates,
)
from apex.application.options_quote_intelligence import (
    CreateOptionChainCapturePlan,
    ResolveRealizedVolatility,
)
from apex.cli import _build_operating_daily_checkpoint, _deliver_operating_cycle_alerts
from apex.domain.operating_cycle import (
    OperatingBrief,
    OperatingTaskResult,
    OperatingTaskStatus,
    OperatingTaskType,
    TaskExecution,
)
from apex.domain.options_intelligence import OptionType
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    ChainSampleCandidate,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
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
from apex.domain.research_cohort import (
    GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    CohortRole,
    CohortSector,
    ResolvedCohortMember,
)
from tests.persistence.test_postgres_operating_cycle import (
    OPTIONS_SESSION_DAY,
    _due_task,
    _options_capability,
    _row_count,
    _save_minimal_cycle,
)

pytestmark = pytest.mark.postgres

# Mirrors the real 2026-08-12 XNYS session/window this milestone's own
# incident happened in (see AGENTS.md / the memory this milestone was
# scoped from), so assertions read as "the real shape, fixed" rather than
# an arbitrary fixture date.
SESSION_1_ID = "2026-08-12"
WINDOW_1_OPEN = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
WINDOW_1_CLOSE = datetime(2026, 8, 12, 20, 15, tzinfo=UTC)
NOW_1 = WINDOW_1_OPEN + timedelta(minutes=5)

# The next distinct genuine XNYS session -- Part 17's "next eligible
# session" proof needs a second, later window with its own identity.
SESSION_2_ID = "2026-08-13"
WINDOW_2_OPEN = datetime(2026, 8, 13, 20, 0, tzinfo=UTC)
WINDOW_2_CLOSE = datetime(2026, 8, 13, 20, 15, tzinfo=UTC)
NOW_2 = WINDOW_2_OPEN + timedelta(minutes=5)


def _plan(
    *, cohort_policy_id: str = "options-parent-persistence-test-cohort"
) -> OptionChainCapturePlan:
    """The full, real 19-member methodology cohort (Part 15: unchanged),
    synthetic ``stable_security_id``s -- identical convention to every
    other options test in this repository, real securities identity
    resolution is a separate, out-of-scope concern."""

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
        cohort_policy_id=cohort_policy_id,
        cohort_policy_hash="0" * 64,
        members=members,
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=WINDOW_1_OPEN - timedelta(days=2),
    )


def _due(
    session_id: str, window_open: datetime, window_close: datetime
) -> OptionsCaptureDueDecision:
    return OptionsCaptureDueDecision(
        OptionsCaptureDueStatus.DUE, session_id, window_open, window_close, ("DUE",), None
    )


def _occ_symbol(root: str, expiration_iso: str, option_type: OptionType, strike: Decimal) -> str:
    yy, mm, dd = expiration_iso.split("-")
    side = "C" if option_type is OptionType.CALL else "P"
    strike_digits = f"{int(strike * 1000):08d}"
    return f"{root}{yy[2:]}{mm}{dd}{side}{strike_digits}"


def _entry(
    symbol: str, bid: str, ask: str, *, quote_timestamp: datetime
) -> ParsedAlpacaOptionSnapshotEntry:
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
        quote_timestamp=quote_timestamp,
    )


def _build_outcome(
    symbol: str, stable_id: str, *, captured_at: datetime, request_count: int = 2
) -> UnderlyingCaptureOutcome:
    """A small, genuine three-strike call chain -- real enough for
    ``select_contracts_stratified``/``generate_structure_candidates`` to
    run for real and retain at least one structure, exactly like the
    existing adapter-level test's identically-named helper."""

    expiration = (captured_at + timedelta(days=20)).date().isoformat()
    price = Decimal("500")
    strikes = (price - Decimal("5"), price, price + Decimal("5"))
    entries: dict[str, ParsedAlpacaOptionSnapshotEntry] = {}
    candidates = []
    for strike in strikes:
        occ = _occ_symbol(symbol, expiration, OptionType.CALL, strike)
        entries[occ] = (
            _entry(occ, "8.00", "8.20", quote_timestamp=captured_at)
            if strike == strikes[0]
            else _entry(occ, "4.00", "4.20", quote_timestamp=captured_at)
            if strike == strikes[1]
            else _entry(occ, "1.50", "1.70", quote_timestamp=captured_at)
        )
        candidates.append(ChainSampleCandidate(occ, expiration, OptionType.CALL, strike))
    selection = select_contracts_stratified(
        tuple(candidates), underlying_price=price, captured_at=captured_at, max_contracts=200
    )
    contract_entries = tuple(entries[s] for s in selection.selected_contract_symbols)
    return UnderlyingCaptureOutcome(
        symbol,
        stable_id,
        "CAPTURED",
        price,
        "ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at,
        contract_entries,
        request_count,
        4096,
        None,
        (f"receipt-{symbol}-{captured_at.isoformat()}",),
        selection,
        len(entries),
    )


@dataclass
class FakeCaptureClient:
    """Identical in spirit to ``tests/test_options_research_capture.py``'s
    fixture of the same name, extended with ``raise_on`` so a test can
    simulate a genuine mid-cohort provider failure (e.g. a transport error)
    *after* an earlier member has already made real, counted requests."""

    credentials_available: bool = True
    outcomes: dict[str, UnderlyingCaptureOutcome] | None = None
    raise_on: dict[str, Exception] | None = None
    calls: list[str] = field(default_factory=list)

    def capture_underlying(self, *, member, bounds, feed, captured_at, timeout_seconds=15):
        self.calls.append(member.symbol)
        if self.raise_on and member.symbol in self.raise_on:
            raise self.raise_on[member.symbol]
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


class _SnapshotFailsOnceRepository:
    """Wraps a real ``PostgresOptionsQuoteIntelligenceRepository``,
    delegating every call unchanged except ``save_snapshot``, whose first
    invocation raises a genuine-shaped persistence error instead of
    reaching Postgres. Reproduces the exact "observations/run durable,
    snapshot absent" crash boundary the real 2026-08-12 FK bug produced,
    without reintroducing that real bug (which this milestone's fix
    removes) to trigger it -- Part 10's mandatory crash-boundary proof."""

    def __init__(self, real: PostgresOptionsQuoteIntelligenceRepository) -> None:
        self._real = real
        self.save_snapshot_attempts = 0

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def save_snapshot(self, snapshot):
        self.save_snapshot_attempts += 1
        if self.save_snapshot_attempts == 1:
            raise OptionsQuoteIntelligencePersistenceError(
                "INJECTED_SNAPSHOT_PERSISTENCE_FAILURE_FOR_TEST"
            )
        return self._real.save_snapshot(snapshot)


def _policies(structures_repo: PostgresOptionsStructuresRepository, *, created_at: datetime):
    pricing = structures_repo.save_pricing_policy(StructurePricingPolicy.create())
    fee = structures_repo.save_fee_policy(OptionsFeePolicy.create())
    slippage = structures_repo.save_slippage_policy(OptionsSlippagePolicy.create())
    structure = structures_repo.save_structure_policy(
        OptionsStructureResearchPolicy.create(
            pricing_policy=pricing, fee_policy=fee, slippage_policy=slippage, created_at=created_at
        )
    )
    return fee, slippage, structure


def _command(
    *,
    plan: OptionChainCapturePlan,
    due: OptionsCaptureDueDecision,
    structures_repo: PostgresOptionsStructuresRepository,
    now: datetime,
) -> OptionsResearchEvidenceCaptureCommand:
    fee_policy, slippage_policy, structure_policy = _policies(structures_repo, created_at=now)
    return OptionsResearchEvidenceCaptureCommand(
        plan=plan,
        due=due,
        window_policy=OptionsCaptureWindowPolicy.create(),
        sessions=(),
        coverage_policy=ChainCoverageQualityPolicy.create(),
        structure_policy=structure_policy,
        fee_policy=fee_policy,
        slippage_policy=slippage_policy,
        feed_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
        now=now,
    )


def _rv_resolver(engine: Engine) -> ResolveRealizedVolatility:
    """The real service, wired to real (empty in these tests) Postgres
    repositories -- exactly what the unattended operating-cycle executor
    constructs. Reports ``HISTORICAL_DATASET_UNAVAILABLE`` here (no
    genuine multi-asset dataset is seeded), a fully handled, already-tested
    state -- realized volatility is not this milestone's concern."""

    return ResolveRealizedVolatility(
        historical_repository=PostgresHistoricalMarketDataRepository(engine),
        evidence=PostgresSecuritiesRepository(engine),
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )


def _capture(
    engine: Engine,
    *,
    quote_repo: object | None = None,
    client: FakeCaptureClient | None = None,
) -> tuple[
    AlpacaOptionsResearchEvidenceCapture, object, PostgresOptionsStructuresRepository, object
]:
    quote_repository = quote_repo or PostgresOptionsQuoteIntelligenceRepository(engine)
    structures_repository = PostgresOptionsStructuresRepository(engine)
    checkpoint_repository = PostgresOptionsResearchOperationsRepository(engine)
    capture = AlpacaOptionsResearchEvidenceCapture(
        quote_repository=quote_repository,  # type: ignore[arg-type]
        structures_repository=structures_repository,
        checkpoint_repository=checkpoint_repository,
        rv_resolver=_rv_resolver(engine),
        capture_client=client or FakeCaptureClient(),
    )
    return capture, quote_repository, structures_repository, checkpoint_repository


# --------------------------------------------------------------------------
# Part 9: the mandatory real-Postgres, no-pre-seeded-parents FK graph proof.
# --------------------------------------------------------------------------


def test_capture_completes_full_persistence_graph_from_empty_parent_state(
    postgres_engine: Engine,
) -> None:
    """Starting with *no* capture-run parent, no snapshot, and no quote
    observations (only a plan, created through the same reviewed idempotent
    service the real one-time bootstrap uses -- never a raw insert), the
    exact automatic/adapter path used by the unattended operating task
    builds the complete plan -> run -> observations -> snapshot ->
    structures -> checkpoint graph, with every real PostgreSQL foreign key
    enforced throughout. This is the test the milestone requires to fail
    against pre-fix ``d668149`` (verified separately, see the final
    report)."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    assert _row_count(postgres_engine, "options_chain_capture_plan") == 1
    assert _row_count(postgres_engine, "options_chain_capture_run") == 0
    assert _row_count(postgres_engine, "options_chain_snapshot") == 0
    assert _row_count(postgres_engine, "options_quote_observation") == 0

    aapl = next(m for m in plan.members if m.symbol == "AAPL")
    spy = next(m for m in plan.members if m.symbol == "SPY")
    qqq = next(m for m in plan.members if m.symbol == "QQQ")
    client = FakeCaptureClient(
        outcomes={
            "AAPL": _build_outcome("AAPL", aapl.stable_security_id, captured_at=NOW_1),
            "SPY": _build_outcome("SPY", spy.stable_security_id, captured_at=NOW_1),
            "QQQ": _build_outcome("QQQ", qqq.stable_security_id, captured_at=NOW_1),
        }
    )
    capture, quote_repo, structures_repo, checkpoint_repo = _capture(postgres_engine, client=client)
    command = _command(
        plan=plan,
        due=_due(SESSION_1_ID, WINDOW_1_OPEN, WINDOW_1_CLOSE),
        structures_repo=structures_repo,
        now=NOW_1,
    )

    outcome = capture.run(command)

    expected_run_id = OptionChainCaptureRun.derive_run_id(
        plan_id=plan.plan_id, requested_at=WINDOW_1_OPEN
    )
    run = quote_repo.run_by_id(expected_run_id)
    assert run is not None, "the run parent must be genuinely persisted, not merely derivable"
    assert run.plan_id == plan.plan_id
    assert run.status.value == "PARTIAL"  # 3 of 19 members genuinely captured
    assert run.underlyings_captured == 3
    # 2 requests for each of the 3 genuinely captured members, plus 1
    # request for each of the other 16 members (the default fake outcome
    # is "EMPTY", not "no request attempted" -- an empty chain is still a
    # genuine, counted provider read, exactly like the real Alpaca client).
    assert run.provider_request_count == 3 * 2 + 16 * 1
    # ``OptionChainCaptureRun`` itself carries no ``authority`` field (only
    # the Postgres row does, fixed at "NONE" -- confirmed directly below).
    with postgres_engine.connect() as connection:
        row_authority = connection.execute(
            sa.text("SELECT authority FROM options_chain_capture_run WHERE run_id = :run_id"),
            {"run_id": run.run_id},
        ).scalar_one()
    assert row_authority == "NONE"

    for symbol, _member in (("AAPL", aapl), ("SPY", spy), ("QQQ", qqq)):
        evidence = next(item for item in outcome.per_underlying if item.symbol == symbol)
        assert evidence.status == "CAPTURED_FRESH"
        assert evidence.snapshot_id is not None
        snapshot = quote_repo.snapshot_by_id(evidence.snapshot_id)
        assert snapshot is not None
        assert snapshot.run_id == expected_run_id
        assert snapshot.plan_id == plan.plan_id
        assert snapshot.authority == "NONE"
        observations = quote_repo.observations_by_ids(snapshot.observation_ids)
        assert observations, f"{symbol} must have real persisted observations"
        assert evidence.structures_evaluated >= 1
        assert evidence.structure_research_snapshot_id is not None

    assert _row_count(postgres_engine, "options_chain_capture_run") == 1
    assert _row_count(postgres_engine, "options_chain_snapshot") == 3
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1
    assert outcome.checkpoint.underlyings_captured_count == 3
    assert outcome.checkpoint.authority == "NONE"
    # Only 3 of the 19 real methodology members were configured as
    # genuinely captured here (this test's purpose is the FK graph, not
    # full-cohort completion) -- ``completed_session_ids`` correctly does
    # NOT bank a partial cohort (requires captured == requested); the full,
    # genuinely-banked 0/20 -> 1/20 case is proven separately below.
    assert checkpoint_repo.completed_session_ids(plan.plan_id) == frozenset()


def test_repeated_invocation_same_window_does_not_duplicate_plan_or_run(
    postgres_engine: Engine,
) -> None:
    """Part 3 + Part 4 + Part 7: a resumed invocation within the identical
    still-open window (the normal shape of a 5-minute unattended timer
    polling across one 15-minute capture window) must never create a
    second plan or run row, and must make zero further provider requests
    for a member it already genuinely captured."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    aapl = next(m for m in plan.members if m.symbol == "AAPL")
    client = FakeCaptureClient(
        outcomes={"AAPL": _build_outcome("AAPL", aapl.stable_security_id, captured_at=NOW_1)}
    )
    capture, quote_repo, structures_repo, _checkpoint_repo = _capture(
        postgres_engine, client=client
    )
    command = _command(
        plan=plan,
        due=_due(SESSION_1_ID, WINDOW_1_OPEN, WINDOW_1_CLOSE),
        structures_repo=structures_repo,
        now=NOW_1,
    )

    first = capture.run(command)
    second = capture.run(command)

    assert len(quote_repo.plans()) == 1
    assert len(quote_repo.runs_for_plan(plan.plan_id)) == 1
    assert client.calls.count("AAPL") == 1, (
        "AAPL must be fetched once, then reused, never re-fetched"
    )
    aapl_second = next(item for item in second.per_underlying if item.symbol == "AAPL")
    assert aapl_second.status == "REUSED_ALREADY_CAPTURED_THIS_SESSION"
    assert (
        aapl_second.snapshot_id
        == next(item for item in first.per_underlying if item.symbol == "AAPL").snapshot_id
    )
    assert first.checkpoint.checkpoint_id == second.checkpoint.checkpoint_id
    assert _row_count(postgres_engine, "options_chain_snapshot") == 1


# --------------------------------------------------------------------------
# Part 6, 10, 11: the crash-boundary shape, evidence preservation, and
# multi-underlying abort semantics.
# --------------------------------------------------------------------------


def test_snapshot_persistence_failure_preserves_lineage_and_observations_but_no_checkpoint(
    postgres_engine: Engine,
) -> None:
    """Reproduces the exact 2026-08-12 evidence shape against a controlled
    (not the real, now-fixed FK) fault: plan and run lineage are durable,
    every attempted member's raw observations are durable (Phase 1 always
    completes for the whole cohort before any snapshot is persisted), but
    the first snapshot persistence failure aborts the rest of Phase 3 --
    no snapshot, no structures, and no checkpoint for *any* member, even
    ones whose observations already made it in. Persistence-integrity
    failure is fatal to the whole invocation, unlike an isolated per-member
    provider failure (Part 11)."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    aapl = next(
        m for m in plan.members if m.symbol == "AAPL"
    )  # alphabetically first, like 2026-08-12
    spy = next(m for m in plan.members if m.symbol == "SPY")
    client = FakeCaptureClient(
        outcomes={
            "AAPL": _build_outcome("AAPL", aapl.stable_security_id, captured_at=NOW_1),
            "SPY": _build_outcome("SPY", spy.stable_security_id, captured_at=NOW_1),
        }
    )
    faulty_repo = _SnapshotFailsOnceRepository(real_quote_repo)
    capture, _quote_repo, structures_repo, _checkpoint_repo = _capture(
        postgres_engine, quote_repo=faulty_repo, client=client
    )
    command = _command(
        plan=plan,
        due=_due(SESSION_1_ID, WINDOW_1_OPEN, WINDOW_1_CLOSE),
        structures_repo=structures_repo,
        now=NOW_1,
    )

    with pytest.raises(OptionsQuoteIntelligencePersistenceError):
        capture.run(command)

    # Lineage: durable. Phase 1 (fetch + persist observations for the whole
    # cohort) genuinely completed before Phase 3 (snapshot persistence)
    # ever started, so *both* AAPL's and SPY's raw observations survive --
    # strictly more evidence than the real 2026-08-12 shape preserved,
    # because the old code fetched and persisted member-by-member in one
    # combined step and never even reached a second member.
    assert _row_count(postgres_engine, "options_chain_capture_plan") == 1
    assert _row_count(postgres_engine, "options_chain_capture_run") == 1
    assert _row_count(postgres_engine, "options_quote_observation") > 0
    aapl_observations = tuple(
        item for item in real_quote_repo.snapshots_for_underlying(aapl.stable_security_id)
    )
    assert aapl_observations == (), "no snapshot exists yet for AAPL"
    spy_observations = real_quote_repo.snapshots_for_underlying(spy.stable_security_id)
    assert spy_observations == (), "no snapshot exists yet for SPY either"

    # No snapshot, no structures, no checkpoint -- the session is not
    # banked, exactly like the real incident.
    assert _row_count(postgres_engine, "options_chain_snapshot") == 0
    assert _row_count(postgres_engine, "option_structure_candidate") == 0
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0
    checkpoint_repo = PostgresOptionsResearchOperationsRepository(postgres_engine)
    assert checkpoint_repo.completed_session_ids(plan.plan_id) == frozenset()


def test_replay_after_snapshot_failure_completes_cleanly_without_duplicating_evidence(
    postgres_engine: Engine,
) -> None:
    """Part 7 + Part 10: after a partial-persistence failure, a later
    (real, unwrapped-repository) replay for the *same* still-open window
    must not duplicate the plan, must not duplicate the run (the run row's
    stats stay exactly what the failed first attempt recorded -- immutable,
    like every table in this evidence family, a known and accepted
    consequence, not a bug), must not duplicate the already-durable
    observations, and must reach a genuine, complete, single checkpoint."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    aapl = next(m for m in plan.members if m.symbol == "AAPL")
    outcome_aapl = _build_outcome("AAPL", aapl.stable_security_id, captured_at=NOW_1)
    client = FakeCaptureClient(outcomes={"AAPL": outcome_aapl})
    faulty_repo = _SnapshotFailsOnceRepository(real_quote_repo)
    failing_capture, _q, failing_structures_repo, _c = _capture(
        postgres_engine, quote_repo=faulty_repo, client=client
    )
    command = _command(
        plan=plan,
        due=_due(SESSION_1_ID, WINDOW_1_OPEN, WINDOW_1_CLOSE),
        structures_repo=failing_structures_repo,
        now=NOW_1,
    )
    with pytest.raises(OptionsQuoteIntelligencePersistenceError):
        failing_capture.run(command)

    observations_after_failure = _row_count(postgres_engine, "options_quote_observation")
    assert observations_after_failure > 0
    run_id = OptionChainCaptureRun.derive_run_id(plan_id=plan.plan_id, requested_at=WINDOW_1_OPEN)
    run_after_failure = real_quote_repo.run_by_id(run_id)
    assert run_after_failure is not None
    assert run_after_failure.underlyings_captured == 1, (
        "the failed attempt's own persisted run summary reflects Phase 1's "
        "view -- AAPL's evidence was genuinely gathered before Phase 3 ever "
        "attempted to persist a snapshot for it, so it counts as 'captured' "
        "here even though no snapshot row exists yet; a later successful "
        "replay does not retroactively rewrite this row (immutable)"
    )

    # A fresh capture instance, the *real* (unwrapped) repository, replays
    # the identical command (same session, same window, same fake client
    # outcome -- deterministic content, so re-fetching AAPL reconstructs
    # byte-identical observations rather than genuinely new ones).
    replay_client = FakeCaptureClient(outcomes={"AAPL": outcome_aapl})
    replay_capture, replay_quote_repo, replay_structures_repo, replay_checkpoint_repo = _capture(
        postgres_engine, client=replay_client
    )
    replay_command = _command(
        plan=plan,
        due=_due(SESSION_1_ID, WINDOW_1_OPEN, WINDOW_1_CLOSE),
        structures_repo=replay_structures_repo,
        now=NOW_1,
    )
    replayed = replay_capture.run(replay_command)

    assert len(replay_quote_repo.plans()) == 1
    assert len(replay_quote_repo.runs_for_plan(plan.plan_id)) == 1, "still exactly the one run row"
    assert _row_count(postgres_engine, "options_quote_observation") == observations_after_failure, (
        "AAPL's observations were re-derived (same content, same "
        "deterministic ids) but never duplicated -- save_observations "
        "skips ids it already has"
    )
    aapl_evidence = next(item for item in replayed.per_underlying if item.symbol == "AAPL")
    assert aapl_evidence.status == "CAPTURED_FRESH"
    assert aapl_evidence.snapshot_id is not None
    assert replay_quote_repo.snapshot_by_id(aapl_evidence.snapshot_id) is not None
    assert _row_count(postgres_engine, "options_chain_snapshot") == 1
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1
    assert replayed.checkpoint.underlyings_captured_count == 1
    # Only AAPL (1 of 19) is configured here -- this test's purpose is
    # crash/replay correctness, not full-cohort banking (proven separately
    # below) -- so this session correctly still does not bank.
    assert replay_checkpoint_repo.completed_session_ids(plan.plan_id) == frozenset()


# --------------------------------------------------------------------------
# Part 16, 17: the missed session stays missed forever; the next genuinely
# distinct session is not contaminated by it and banks independently.
# --------------------------------------------------------------------------


def test_next_session_after_failed_session_gets_distinct_identity_and_banks_independently(
    postgres_engine: Engine,
) -> None:
    """Using test-controlled calendar time (two distinct, directly
    constructed ``OptionsCaptureDueDecision`` windows -- no real future
    market window is waited on or invoked): session 1 fails exactly like
    2026-08-12 and is never backfilled; session 2 -- a genuinely later,
    distinct session -- gets its own run identity, is untouched by session
    1's orphaned AAPL evidence, completes cleanly, and is the *only*
    session banked."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    aapl = next(m for m in plan.members if m.symbol == "AAPL")

    # Session 1: fails exactly like the real incident (controlled fault).
    session_1_outcome = _build_outcome("AAPL", aapl.stable_security_id, captured_at=NOW_1)
    faulty_repo = _SnapshotFailsOnceRepository(real_quote_repo)
    failing_capture, _q, failing_structures_repo, _c = _capture(
        postgres_engine,
        quote_repo=faulty_repo,
        client=FakeCaptureClient(outcomes={"AAPL": session_1_outcome}),
    )
    session_1_command = _command(
        plan=plan,
        due=_due(SESSION_1_ID, WINDOW_1_OPEN, WINDOW_1_CLOSE),
        structures_repo=failing_structures_repo,
        now=NOW_1,
    )
    with pytest.raises(OptionsQuoteIntelligencePersistenceError):
        failing_capture.run(session_1_command)

    session_1_run_id = OptionChainCaptureRun.derive_run_id(
        plan_id=plan.plan_id, requested_at=WINDOW_1_OPEN
    )
    assert real_quote_repo.run_by_id(session_1_run_id) is not None
    assert _row_count(postgres_engine, "options_chain_snapshot") == 0
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 0

    # Session 2: a genuinely later, distinct window. Real (unwrapped)
    # repository this time -- the normal healthy path. Every cohort member
    # genuinely captured -- ``completed_session_ids`` only counts a session
    # banked once ``underlyings_captured_count == underlyings_requested_
    # count`` (the real "0/20 -> 1/20" bar, Part 15's unchanged 19-member
    # methodology), so a partial capture would never bank either.
    session_2_outcomes = {
        member.symbol: _build_outcome(member.symbol, member.stable_security_id, captured_at=NOW_2)
        for member in plan.members
    }
    (
        session_2_capture,
        session_2_quote_repo,
        session_2_structures_repo,
        session_2_checkpoint_repo,
    ) = _capture(postgres_engine, client=FakeCaptureClient(outcomes=session_2_outcomes))
    session_2_command = _command(
        plan=plan,
        due=_due(SESSION_2_ID, WINDOW_2_OPEN, WINDOW_2_CLOSE),
        structures_repo=session_2_structures_repo,
        now=NOW_2,
    )
    session_2_result = session_2_capture.run(session_2_command)

    session_2_run_id = OptionChainCaptureRun.derive_run_id(
        plan_id=plan.plan_id, requested_at=WINDOW_2_OPEN
    )
    assert session_2_run_id != session_1_run_id, "distinct session, distinct run identity"
    session_2_run = session_2_quote_repo.run_by_id(session_2_run_id)
    assert session_2_run is not None
    assert session_2_run.underlyings_captured == len(plan.members)
    assert session_2_run.status.value == "COMPLETE"

    aapl_evidence = next(item for item in session_2_result.per_underlying if item.symbol == "AAPL")
    assert aapl_evidence.status == "CAPTURED_FRESH", (
        "not contaminated into a false REUSED by session 1"
    )
    session_2_snapshot = session_2_quote_repo.snapshot_by_id(aapl_evidence.snapshot_id)
    assert session_2_snapshot is not None
    assert session_2_snapshot.captured_at == NOW_2
    assert session_2_snapshot.run_id == session_2_run_id

    assert _row_count(postgres_engine, "options_chain_capture_run") == 2
    assert _row_count(postgres_engine, "options_chain_snapshot") == len(plan.members), (
        "only session 2's snapshots exist -- session 1 never persisted one"
    )
    assert _row_count(postgres_engine, "options_forward_evidence_checkpoint") == 1

    banked = session_2_checkpoint_repo.completed_session_ids(plan.plan_id)
    assert banked == frozenset({SESSION_2_ID}), (
        f"session 1 ({SESSION_1_ID}) must never be counted as banked -- got {banked}"
    )
    assert len(banked) == 1


# --------------------------------------------------------------------------
# Part 12: genuine provider-request accounting survives a downstream
# failure through the real dispatch path (not just the adapter itself).
# --------------------------------------------------------------------------


def test_executor_preserves_genuine_network_requests_on_downstream_provider_failure(
    postgres_engine: Engine,
) -> None:
    """A real, legitimate mid-cohort provider failure (a transport error on
    the second member, after the first genuinely succeeded) proves the
    exact mechanism Part 12 requires: the exception propagating out of
    ``ExistingCapabilityOperatingTaskExecutor.execute`` carries the genuine
    request count made *before* the failure, not zero."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    real_quote_repo.save_capability(_options_capability(credentials_available=True))
    aapl = next(m for m in plan.members if m.symbol == "AAPL")

    client = FakeCaptureClient(
        outcomes={
            "AAPL": _build_outcome(
                "AAPL", aapl.stable_security_id, captured_at=NOW_1, request_count=3
            )
        },
        raise_on={"AMZN": RuntimeError("SIMULATED_PROVIDER_TRANSPORT_FAILURE")},
    )
    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(
        postgres_engine,
        state_source,
        options_capture_client=client,  # type: ignore[arg-type]
    )
    operating_plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="options-request-accounting-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=WINDOW_1_OPEN,
        idempotency_key="options-request-accounting-plan-key",
    )
    task = _due_task(
        OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE,
        cycle_id="options-request-accounting-cycle",
    )

    with pytest.raises(RuntimeError) as excinfo:
        executor.execute(
            task=task,
            cycle_id="options-request-accounting-cycle",
            plan=operating_plan,
            as_of=WINDOW_1_OPEN,
            executed_at=NOW_1,
        )
    assert getattr(excinfo.value, "network_requests_before_failure", None) == 3, (
        "AAPL's 3 genuine requests must survive onto the propagating exception "
        "even though AMZN's failure aborted the whole task"
    )


# Note: the Postgres-free proof of ``ExecuteOperatingCycle._dispatch_task``'s
# own ``getattr(error, "network_requests_before_failure", 0)`` mechanism in
# isolation lives in ``tests/test_operating_cycle.py`` (no real Postgres is
# needed for it) as
# ``test_dispatch_reports_genuine_request_count_on_failure_and_zero_when_absent``.


# --------------------------------------------------------------------------
# Part 14: alert behavior around a genuine options-capture failure.
# --------------------------------------------------------------------------


def test_operating_cycle_alert_and_idempotent_replay_on_options_capture_failure(
    postgres_engine: Engine,
) -> None:
    """The full real dispatch path: a genuine mid-cohort provider failure
    produces exactly one HIGH ``OPERATING_TASK_FAILED`` alert carrying the
    genuine (non-zero) request count on the retained task result, and a
    later idempotent replay of the identical cycle delivers zero further
    alerts (never a duplicate)."""

    real_quote_repo = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = CreateOptionChainCapturePlan(real_quote_repo).execute(plan=_plan())
    real_quote_repo.save_capability(_options_capability(credentials_available=True))
    aapl = next(m for m in plan.members if m.symbol == "AAPL")

    client = FakeCaptureClient(
        outcomes={
            "AAPL": _build_outcome(
                "AAPL", aapl.stable_security_id, captured_at=NOW_1, request_count=4
            )
        },
        raise_on={"AMZN": RuntimeError("SIMULATED_PROVIDER_TRANSPORT_FAILURE")},
    )
    plan_repository = PostgresOperatingCycleRepository(postgres_engine)
    state_source = PostgresOperatingStateSource(postgres_engine, plan_repository)
    executor = ExistingCapabilityOperatingTaskExecutor(
        postgres_engine,
        state_source,
        options_capture_client=client,  # type: ignore[arg-type]
    )
    operating_plan, _ = RegisterOperatingCyclePlan(plan_repository).execute(
        plan_id="options-alert-behavior-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=WINDOW_1_OPEN,
        idempotency_key="options-alert-behavior-plan-key",
    )
    cycle_id = "options-alert-behavior-cycle"

    result = ExecuteOperatingCycle(plan_repository, state_source, executor).execute(
        cycle_id=cycle_id,
        plan_id=operating_plan.plan_id,
        as_of=WINDOW_1_OPEN,
        executed_at=NOW_1,
        idempotency_key=f"{cycle_id}-key",
    )
    options_result = next(
        item for item in result.tasks if item.task_type.value == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"
    )
    assert options_result.status.value == "FAILED"
    assert options_result.network_requests == 4
    assert not result.idempotent_replay

    delivered = _deliver_operating_cycle_alerts(
        engine=postgres_engine, result=result, executed_at=NOW_1
    )
    failed_alerts = [item for item in delivered if item["category"] == "OPERATING_TASK_FAILED"]
    assert len(failed_alerts) == 1
    assert failed_alerts[0]["status"] == "DELIVERED"
    candidates = derive_operating_cycle_alert_candidates(result)
    failed_candidates = [item for item in candidates if item.category == "OPERATING_TASK_FAILED"]
    assert len(failed_candidates) == 1
    assert failed_candidates[0].severity.value == "HIGH"

    # Idempotent replay: the identical cycle, called again. No further
    # dispatch, so no further alerts.
    replay = ExecuteOperatingCycle(plan_repository, state_source, executor).execute(
        cycle_id=cycle_id,
        plan_id=operating_plan.plan_id,
        as_of=WINDOW_1_OPEN,
        executed_at=NOW_1 + timedelta(minutes=5),
        idempotency_key=f"{cycle_id}-key",
    )
    assert replay.idempotent_replay
    replay_delivered = _deliver_operating_cycle_alerts(
        engine=postgres_engine, result=replay, executed_at=NOW_1 + timedelta(minutes=5)
    )
    assert replay_delivered == ()


# --------------------------------------------------------------------------
# Part 13: the checkpoint task-count diagnostic must reflect live task
# results, not a stale once-per-cycle retained brief.
# --------------------------------------------------------------------------


def test_daily_checkpoint_task_counts_reflect_live_results_not_a_stale_brief(
    postgres_engine: Engine,
) -> None:
    """Reproduces the exact real-world diagnostic inconsistency: a
    ``FAILED`` options task genuinely retained on this cycle, next to a
    stale ``OperatingBrief`` (built earlier the same cycle, before the
    options task had resolved -- ``ExecuteOperatingCycle._resume_cycle``
    only ever builds/persists a cycle's brief once) whose own ``task_
    counts`` still claims zero failures. ``operating-daily-checkpoint-
    inspect`` must report the live, correct count -- not echo the brief's
    stale one -- without touching ``OperatingBrief``/``build_operating_
    brief`` themselves, which stay intentionally build-once-per-cycle."""

    repository = PostgresOperatingCycleRepository(postgres_engine)
    plan, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id="checkpoint-task-count-plan-v1",
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=OPTIONS_SESSION_DAY,
        idempotency_key="checkpoint-task-count-plan-key",
    )
    cycle_id = "checkpoint-task-count-cycle"
    _save_minimal_cycle(repository, plan_id=plan.plan_id, cycle_id=cycle_id)

    task = _due_task(OperatingTaskType.OPTIONS_RESEARCH_EVIDENCE_CAPTURE, cycle_id=cycle_id)
    execution = TaskExecution(
        OperatingTaskStatus.FAILED,
        (),
        (),
        False,
        3,
        (),
        "OptionsQuoteIntelligencePersistenceError",
        True,
    )
    failed_result = OperatingTaskResult.create(
        cycle_id=cycle_id,
        task=task,
        execution=execution,
        started_at=OPTIONS_SESSION_DAY,
        completed_at=OPTIONS_SESSION_DAY,
    )
    repository.save_task_result(failed_result)

    # A brief built as if the options task had never happened -- the exact
    # real shape: ``OPERATING_BRIEF_BUILD`` completes hours before the
    # options window even opens, so the morning brief's own task_results
    # snapshot never included it, let alone as FAILED.
    stale_task_counts = (
        ("COMPLETED", 1),
        ("REUSED", 0),
        ("SKIPPED_NOT_DUE", 0),
        ("SKIPPED_PAUSED", 0),
        ("BLOCKED", 0),
        ("UNAVAILABLE", 0),
        ("FAILED", 0),
    )
    stale_brief = OperatingBrief(
        brief_id=f"{cycle_id}-operating-brief",
        cycle_id=cycle_id,
        as_of=OPTIONS_SESSION_DAY,
        overall_system_state="HEALTHY",
        task_counts=stale_task_counts,
        crypto_universe_id=None,
        crypto_pulse_id=None,
        total_market_context_id=None,
        total_market_state=None,
        kalshi_status="UNAVAILABLE",
        shared_opportunity_set_id=None,
        opportunity_counts=(),
        allocation_id=None,
        unallocated_cash=None,
        largest_crypto_score_changes=(),
        stale_or_unavailable=(),
        warnings=(),
        failures=(),
        operator_attention=(),
        source_state_hash="s" * 64,
        canonical_hash="f" * 64,
    )
    repository.save_brief(stale_brief)
    assert repository.brief(f"{cycle_id}-operating-brief").task_counts == stale_task_counts

    checkpoint = _build_operating_daily_checkpoint(
        engine=postgres_engine,
        repository=repository,
        plan_id=plan.plan_id,
        cycle_id=cycle_id,
        runner_id="checkpoint-task-count-runner",
    )
    live_counts = dict(checkpoint["task_counts"])
    assert live_counts["FAILED"] == 1, (
        "must reflect this cycle's genuine live FAILED task result, not the "
        f"stale retained brief's own FAILED=0 -- got {checkpoint['task_counts']}"
    )
    assert live_counts["COMPLETED"] == 0, (
        "the stale brief's COMPLETED=1 must not leak through either"
    )
