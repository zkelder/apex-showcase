"""Orchestration-level proofs for historical market-data capture: plan
creation against a stable-identity source, chunked/checkpointed/resumable
capture (including the honest credentials-absent path with zero unauthorized
requests), deterministic snapshot materialization, data-quality reporting,
and the point-in-time replay seam -- all against in-memory fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.historical_market_data import (
    CreateHistoricalCapturePlan,
    CreateHistoricalCapturePlanCommand,
    EvaluateHistoricalDataQuality,
    EvaluateHistoricalDataQualityCommand,
    HistoricalChunkCanonicalizationResult,
    HistoricalMarketDataApplicationError,
    HistoricalMarketDataConflict,
    HistoricalPointInTimeReplay,
    InMemoryHistoricalMarketDataRepository,
    MaterializeHistoricalDatasetSnapshot,
    MaterializeHistoricalDatasetSnapshotCommand,
    RunHistoricalCaptureChunk,
    RunHistoricalCaptureChunkCommand,
    plan_summary_state,
)
from apex.domain.historical_market_data import (
    HistoricalCaptureChunkState,
    HistoricalCapturePlanState,
    HistoricalClock,
    HistoricalDatasetCompletenessState,
)
from apex.domain.real_securities_data import SecuritiesProviderFetchOperationState
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesDataBatchState,
    SecuritiesMarketDataBatch,
)
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def _identity(symbol: str, cik: str) -> StableSecurityIdentity:
    return StableSecurityIdentity(
        stable_security_id=f"stable_{symbol}",
        identity_basis=IdentityBasis.CIK_AND_SYMBOL,
        cik=cik,
        normalized_symbol=symbol,
        current_symbol=symbol,
        source_identity_hash="a" * 64,
        source_canonical_security_id="legacy_x",
        review_required=False,
        canonical_hash=content_digest(("identity", symbol, cik)),
    )


class FakeIdentitySource:
    def __init__(self, rows: list[StableSecurityIdentity]) -> None:
        self._rows = rows

    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]:
        return tuple(self._rows)


@dataclass(frozen=True, slots=True)
class _FakeOperation:
    state: SecuritiesProviderFetchOperationState
    operation_id: str | None = None
    raw_artifact_set_hash: str | None = None
    failure_code: str | None = None


class FakeFetchOperationCredentialsRequired:
    """Mirrors the real ``FetchSecuritiesProviderOperation``: an operation is
    reserved locally (with a real identity) before the credentials check, and
    zero network I/O is ever attempted."""

    def __init__(self) -> None:
        self.call_count = 0

    def execute(self, command: object) -> _FakeOperation:
        self.call_count += 1
        idempotency_key = command.idempotency_key
        return _FakeOperation(
            state=SecuritiesProviderFetchOperationState.CREDENTIALS_REQUIRED,
            operation_id=f"reserved_{idempotency_key}",
            failure_code="ALPACA_CREDENTIALS_REQUIRED",
        )


class FakeFetchOperationCompleted:
    def __init__(self) -> None:
        self.call_count = 0

    def execute(self, command: object) -> _FakeOperation:
        self.call_count += 1
        idempotency_key = command.idempotency_key
        return _FakeOperation(
            state=SecuritiesProviderFetchOperationState.COMPLETED,
            operation_id=f"op_{idempotency_key}",
            raw_artifact_set_hash="b" * 64,
        )


class FakeFetchOperationFailed:
    def execute(self, command: object) -> _FakeOperation:
        return _FakeOperation(
            state=SecuritiesProviderFetchOperationState.FAILED, failure_code="PROVIDER_HTTP_500"
        )


class FakeEvidenceStore:
    """Satisfies the narrow ``SecuritiesEvidencePort`` (``get_dataset``)."""

    def __init__(self) -> None:
        self.datasets: dict[str, tuple] = {}

    def get_dataset(self, batch_id: str) -> tuple:
        return self.datasets[batch_id]


class FakeCanonicalizer:
    def __init__(
        self, evidence_store: FakeEvidenceStore, bars_by_symbol: dict[str, list[Decimal]]
    ) -> None:
        self._store = evidence_store
        self._bars_by_symbol = bars_by_symbol
        self.call_count = 0

    def canonicalize_chunk(
        self, *, historical_operation, instruments, idempotency_key, imported_at
    ) -> HistoricalChunkCanonicalizationResult:
        self.call_count += 1
        bars: list[SecuritiesBarEvidence] = []
        counts: list[tuple[str, int]] = []
        for instrument in instruments:
            closes = self._bars_by_symbol.get(instrument.symbol, [])
            instrument_bars = []
            for index, close in enumerate(closes):
                start = datetime(2020, 1, index + 1, tzinfo=UTC)
                end = start + timedelta(days=1)
                instrument_bars.append(
                    SecuritiesBarEvidence(
                        instrument_id=instrument.instrument_id,
                        interval=SecuritiesBarInterval.ONE_DAY,
                        adjustment_state=SecuritiesAdjustmentState.RAW,
                        period_start=start,
                        period_end=end,
                        available_at=end + timedelta(minutes=30),
                        open=close,
                        high=Decimal(close) + 1,
                        low=Decimal(close) - 1,
                        close=close,
                        volume=1000,
                        source_identity="FAKE_ALPACA",
                    )
                )
            bars.extend(instrument_bars)
            counts.append((instrument.instrument_id, len(instrument_bars)))
        batch_id = f"batch_{idempotency_key}"
        batch = SecuritiesMarketDataBatch(
            batch_id=batch_id,
            dataset_name="test",
            state=SecuritiesDataBatchState.READY,
            imported_at=imported_at,
            source_artifact="x",
            source_sha256="a" * 64,
            source_byte_size=1,
            canonical_artifact="y",
            canonical_sha256="b" * 64,
            canonical_byte_size=1,
            instrument_count=len(instruments),
            session_count=0,
            bar_count=len(bars),
            corporate_action_count=0,
            first_period_start=datetime(2020, 1, 1, tzinfo=UTC),
            last_period_end=datetime(2020, 1, 2, tzinfo=UTC),
            idempotency_key=idempotency_key,
            warnings=(),
            canonical_hash=content_digest(idempotency_key),
        )
        self._store.datasets[batch_id] = (instruments, (), tuple(bars), ())
        return HistoricalChunkCanonicalizationResult(
            batch=batch, instrument_bar_counts=tuple(counts)
        )


def _plan_command(**overrides: object) -> CreateHistoricalCapturePlanCommand:
    defaults: dict[str, object] = {
        "symbols": ("AAA", "BBB", "CCC"),
        "etf_symbols": frozenset(),
        "provider": "ALPACA_MARKET_DATA_V2",
        "start_date": "2020-01-01",
        "end_date": "2020-01-31",
        "chunk_size": 2,
        "maximum_instruments": 10,
        "maximum_requests": 10,
        "maximum_bytes": 5_000_000,
        "evidence_class": "FIXTURE",
        "operator_action_id": "op-1",
        "idempotency_key": "plan-1",
        "created_at": NOW,
    }
    defaults.update(overrides)
    return CreateHistoricalCapturePlanCommand(**defaults)  # type: ignore[arg-type]


IDENTITIES = [_identity("AAA", "0001"), _identity("BBB", "0002"), _identity("CCC", "0003")]


def test_plan_creation_resolves_symbols_against_stable_identity_source() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=FakeIdentitySource(IDENTITIES)
    )
    plan = service.execute(_plan_command())
    assert len(plan.universe) == 3
    assert {item.stable_security_id for item in plan.universe} == {
        "stable_AAA",
        "stable_BBB",
        "stable_CCC",
    }
    # idempotent replay returns the identical retained plan
    replayed = service.execute(_plan_command())
    assert replayed.plan_id == plan.plan_id


def test_plan_creation_rejects_unresolved_symbols() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=FakeIdentitySource(IDENTITIES)
    )
    with pytest.raises(HistoricalMarketDataApplicationError):
        service.execute(_plan_command(symbols=("AAA", "ZZZZ")))


def test_plan_creation_conflicting_replay_is_rejected() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=FakeIdentitySource(IDENTITIES)
    )
    service.execute(_plan_command())
    with pytest.raises(HistoricalMarketDataConflict):
        service.execute(_plan_command(end_date="2020-02-29"))  # same key, different content


def test_plan_creation_honors_etf_symbols_override() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=FakeIdentitySource(IDENTITIES)
    )
    plan = service.execute(_plan_command(etf_symbols=frozenset({"AAA"})))
    by_symbol = {item.symbol_at_capture: item.asset_class for item in plan.universe}
    assert by_symbol["AAA"] == "ETF"
    assert by_symbol["BBB"] == "COMMON_STOCK"


def _build_plan(repository: InMemoryHistoricalMarketDataRepository, **overrides: object):
    return CreateHistoricalCapturePlan(
        repository=repository, identity_source=FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command(**overrides))


def test_capture_chunk_credentials_absent_makes_zero_provider_write_attempts() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository)
    evidence_store = FakeEvidenceStore()
    fetch = FakeFetchOperationCredentialsRequired()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=fetch,  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(evidence_store, {}),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    chunk = runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    assert chunk.state is HistoricalCaptureChunkState.CREDENTIALS_REQUIRED
    assert chunk.operation_id is not None  # local reservation lineage retained
    assert chunk.batch_id is None
    assert fetch.call_count == 1
    assert plan_summary_state(repository=repository, plan=plan) is (
        HistoricalCapturePlanState.CREDENTIALS_REQUIRED
    )


def test_capture_chunk_resume_advances_to_next_pending_ordinal() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository)  # 3 symbols, chunk_size=2 -> 2 chunks
    evidence_store = FakeEvidenceStore()
    bars_by_symbol = {"AAA": [Decimal(100 + i) for i in range(5)], "BBB": [], "CCC": []}
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationCompleted(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(evidence_store, bars_by_symbol),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    first = runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    assert first.ordinal == 0
    second = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW)
    )
    assert second.ordinal == 1
    assert second.previous_chunk_hash == first.canonical_result_hash
    with pytest.raises(HistoricalMarketDataApplicationError):
        runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))


def test_capture_chunk_already_completed_is_idempotent_and_makes_no_new_attempt() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository, symbols=("AAA",), maximum_instruments=1)
    evidence_store = FakeEvidenceStore()
    fetch = FakeFetchOperationCompleted()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=fetch,  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(evidence_store, {"AAA": [Decimal(100)]}),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    first = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, ordinal=0, requested_at=NOW)
    )
    assert first.state is HistoricalCaptureChunkState.COMPLETED
    replay = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, ordinal=0, requested_at=NOW)
    )
    assert replay.chunk_id == first.chunk_id
    assert fetch.call_count == 1  # no second network attempt for an already-completed chunk


def test_capture_chunk_retry_after_credentials_required_makes_a_new_attempt() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository, symbols=("AAA",), maximum_instruments=1)
    evidence_store = FakeEvidenceStore()
    fetch = FakeFetchOperationCredentialsRequired()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=fetch,  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(evidence_store, {}),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    first = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, ordinal=0, requested_at=NOW)
    )
    second = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, ordinal=0, requested_at=NOW)
    )
    assert fetch.call_count == 2
    assert second.retry_count == 1
    assert second.chunk_id == first.chunk_id


def test_capture_chunk_provider_failure_marks_chunk_failed_not_silently_partial() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository, symbols=("AAA",), maximum_instruments=1)
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationFailed(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(FakeEvidenceStore(), {}),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    chunk = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, ordinal=0, requested_at=NOW)
    )
    assert chunk.state is HistoricalCaptureChunkState.FAILED
    assert chunk.failure_code == "PROVIDER_HTTP_500"


def test_materialize_snapshot_marks_partial_when_a_subset_captured() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository)
    evidence_store = FakeEvidenceStore()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationCompleted(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(
            evidence_store, {"AAA": [Decimal(100), Decimal(101)], "BBB": [], "CCC": []}
        ),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    # only one of two chunks attempted -> a failed subset must not silently
    # appear complete
    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))
    assert snapshot.completeness_state is HistoricalDatasetCompletenessState.PARTIAL
    assert snapshot.observation_count == 2


def test_materialize_snapshot_is_deterministic_across_repeated_calls() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository, symbols=("AAA",), maximum_instruments=1)
    evidence_store = FakeEvidenceStore()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationCompleted(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(evidence_store, {"AAA": [Decimal(100), Decimal(101)]}),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    service = MaterializeHistoricalDatasetSnapshot(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    )
    first = service.execute(
        MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW)
    )
    second = service.execute(
        MaterializeHistoricalDatasetSnapshotCommand(
            plan_id=plan.plan_id, created_at=NOW + timedelta(hours=1)
        )
    )
    assert first.snapshot_id == second.snapshot_id


def test_quality_report_credential_absent_reports_every_security_as_failed() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository)
    evidence_store = FakeEvidenceStore()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationCredentialsRequired(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(evidence_store, {}),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    for _ in range(2):
        runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))
    assert snapshot.observation_count == 0
    report = EvaluateHistoricalDataQuality(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    ).execute(
        EvaluateHistoricalDataQualityCommand(snapshot_id=snapshot.snapshot_id, created_at=NOW)
    )
    assert report.captured_security_count == 0
    assert report.requested_security_count == 3
    assert all(
        outcome.value == "REQUEST_FAILED" for _sid, outcome, _reason in report.security_outcomes
    )


# --------------------------------------------------------------------------
# Point-in-time replay seam
# --------------------------------------------------------------------------


def test_replay_seam_universe_and_evidence_and_calculation() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository)
    evidence_store = FakeEvidenceStore()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationCompleted(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(
            evidence_store,
            {
                "AAA": [Decimal(100 + i) for i in range(10)],
                "BBB": [Decimal(200 + i) for i in range(10)],
                "CCC": [],
            },
        ),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))

    replay = HistoricalPointInTimeReplay(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    )
    universe = replay.universe_as_of(snapshot.snapshot_id)
    assert {item.stable_security_id for item in universe} == {
        "stable_AAA",
        "stable_BBB",
        "stable_CCC",
    }

    clock = HistoricalClock(as_of=datetime(2020, 1, 10, 1, tzinfo=UTC))
    bars = replay.evidence_as_of(
        snapshot_id=snapshot.snapshot_id, stable_security_id="stable_AAA", clock=clock
    )
    assert len(bars) == 9  # day 10's bar is not yet available at the clock

    value = replay.calculate(
        snapshot_id=snapshot.snapshot_id,
        stable_security_id="stable_AAA",
        clock=clock,
        calculation="simple_return",
    )
    assert value is not None

    correlation = replay.correlation_as_of(
        snapshot_id=snapshot.snapshot_id,
        stable_security_id_a="stable_AAA",
        stable_security_id_b="stable_BBB",
        clock=clock,
        window=5,
    )
    assert correlation is not None

    with pytest.raises(HistoricalMarketDataApplicationError):
        replay.calculate(
            snapshot_id=snapshot.snapshot_id,
            stable_security_id="stable_AAA",
            clock=clock,
            calculation="not_a_real_calculation",
        )


def test_replay_seam_rejects_lookahead_via_early_clock() -> None:
    repository = InMemoryHistoricalMarketDataRepository()
    plan = _build_plan(repository, symbols=("AAA",), maximum_instruments=1)
    evidence_store = FakeEvidenceStore()
    runner = RunHistoricalCaptureChunk(
        repository=repository,
        fetch_operation=FakeFetchOperationCompleted(),  # type: ignore[arg-type]
        canonicalizer=FakeCanonicalizer(
            evidence_store, {"AAA": [Decimal(100 + i) for i in range(10)]}
        ),  # type: ignore[arg-type]
        configuration_id="cfg-1",
    )
    runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))
    replay = HistoricalPointInTimeReplay(
        repository=repository,
        evidence=evidence_store,  # type: ignore[arg-type]
    )
    early_clock = HistoricalClock(as_of=datetime(2019, 1, 1, tzinfo=UTC))
    bars = replay.evidence_as_of(
        snapshot_id=snapshot.snapshot_id, stable_security_id="stable_AAA", clock=early_clock
    )
    assert bars == ()
