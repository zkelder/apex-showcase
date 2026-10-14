"""PostgreSQL persistence proofs for historical market-data evidence:
migration 0077 round-trip, checkpoint/resume across fresh repository
instances, a genuine (fake-transport, real-adapter) two-chunk Alpaca capture
into the unmodified canonical Securities bar store, the credentials-absent
path making zero request attempts, immutable-evidence enforcement, and
downgrade protection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.alpaca_canonicalization import HistoricalAlpacaCanonicalizer
from apex.adapters.alpaca_market_data import AlpacaMarketDataHttpAdapter, ProviderHttpResponse
from apex.adapters.historical_market_data_postgres import (
    HISTORICAL_MARKET_DATA_TABLES,
    PostgresHistoricalMarketDataRepository,
)
from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.historical_market_data import (
    CreateHistoricalCapturePlan,
    CreateHistoricalCapturePlanCommand,
    EvaluateHistoricalDataQuality,
    EvaluateHistoricalDataQualityCommand,
    HistoricalMarketDataApplicationError,
    HistoricalPointInTimeReplay,
    MaterializeHistoricalDatasetSnapshot,
    MaterializeHistoricalDatasetSnapshotCommand,
    RunHistoricalCaptureChunk,
    RunHistoricalCaptureChunkCommand,
    plan_summary_state,
)
from apex.application.real_securities_data import (
    CreateRealProviderConfiguration,
    CreateRealProviderConfigurationCommand,
    FetchSecuritiesProviderOperation,
)
from apex.domain.historical_market_data import (
    HistoricalCaptureChunkState,
    HistoricalCapturePlanState,
    HistoricalClock,
    HistoricalDatasetCompletenessState,
    PointInTimeUniverseState,
)
from apex.domain.real_securities_data import SecuritiesSurvivorshipState
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 6, 15, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


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


class _FakeIdentitySource:
    def __init__(self, rows: list[StableSecurityIdentity]) -> None:
        self._rows = rows

    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]:
        return tuple(self._rows)


IDENTITIES = [_identity("AAPL", "0000320193"), _identity("MSFT", "0000789019")]


class _Transport:
    """Mirrors ``tests/test_real_securities_data.py``'s ``_Transport``: a
    scripted, in-process HTTP double so the genuine adapter/normalizer/
    canonicalizer/persistence pipeline runs unmodified end-to-end without a
    real network call."""

    def __init__(self, responses: list[ProviderHttpResponse]) -> None:
        self._responses = responses

    def request(
        self,
        *,
        host: str,
        target: str,
        headers: dict[str, str],
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> ProviderHttpResponse:
        del host, target, headers, timeout_seconds, maximum_response_bytes
        return self._responses.pop(0)


def _bars_response(symbol_bars: dict[str, list[dict[str, str]]]) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=200,
        headers={},
        body=json.dumps({"bars": symbol_bars}, separators=(",", ":")).encode("utf-8"),
        received_at=NOW,
    )


def _historical_market_data_stack(
    postgres_engine: Engine,
) -> tuple[
    PostgresHistoricalMarketDataRepository,
    PostgresSecuritiesRepository,
    PostgresRealSecuritiesDataRepository,
    str,
]:
    historical_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    securities_repository = PostgresSecuritiesRepository(postgres_engine)
    real_repository = PostgresRealSecuritiesDataRepository(postgres_engine)
    configuration = CreateRealProviderConfiguration(real_repository).execute(
        CreateRealProviderConfigurationCommand(
            environment="TEST",
            adapter_version=AlpacaMarketDataHttpAdapter.adapter_version,
            endpoint_policy_id="TEST_POLICY",
            endpoint_policy_hash="a" * 64,
            operator_action_id="test-operator",
            created_at=NOW,
        )
    )
    return (
        historical_repository,
        securities_repository,
        real_repository,
        configuration.configuration_id,
    )


def _plan_command(**overrides: object) -> CreateHistoricalCapturePlanCommand:
    defaults: dict[str, object] = {
        "symbols": ("AAPL", "MSFT"),
        "etf_symbols": frozenset(),
        "provider": "ALPACA_MARKET_DATA_V2",
        "start_date": "2024-01-02",
        "end_date": "2024-01-03",
        "chunk_size": 1,
        "maximum_instruments": 10,
        "maximum_requests": 10,
        "maximum_bytes": 5_000_000,
        "evidence_class": "FIXTURE",
        "operator_action_id": "test-operator",
        "idempotency_key": "test-plan-1",
        "created_at": NOW,
    }
    defaults.update(overrides)
    return CreateHistoricalCapturePlanCommand(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Schema presence
# --------------------------------------------------------------------------


def test_historical_market_data_tables_exist_at_head(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(HISTORICAL_MARKET_DATA_TABLES)


# --------------------------------------------------------------------------
# Plan/chunk/snapshot/quality-report round-trip
# --------------------------------------------------------------------------


def test_plan_round_trips_exactly_through_a_fresh_repository_instance(
    postgres_engine: Engine,
) -> None:
    repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=_FakeIdentitySource(IDENTITIES)
    )
    plan = service.execute(_plan_command())

    fresh = PostgresHistoricalMarketDataRepository(postgres_engine)
    reloaded = fresh.get_plan(plan.plan_id)
    assert reloaded == plan
    assert fresh.plan_by_idempotency("test-plan-1") == plan
    assert plan in fresh.list_plans()


def test_plan_idempotent_replay_returns_retained_row_without_duplicate_insert(
    postgres_engine: Engine,
) -> None:
    repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=_FakeIdentitySource(IDENTITIES)
    )
    first = service.execute(_plan_command())
    second = service.execute(_plan_command())
    assert first.plan_id == second.plan_id
    assert len(repository.list_plans()) == 1


def test_instrument_identity_lineage_is_persisted_and_stable_id_recoverable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    plan = CreateHistoricalCapturePlan(
        repository=repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command())
    member = next(item for item in plan.universe if item.symbol_at_capture == "AAPL")
    lineage = repository.instrument_identity(member.instrument_id)
    assert lineage == ("stable_AAPL", "AAPL", "COMMON_STOCK")


def test_chunk_insert_then_update_round_trips_state_transitions(postgres_engine: Engine) -> None:
    repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    plan = CreateHistoricalCapturePlan(
        repository=repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command())
    from apex.domain.historical_market_data import HistoricalCaptureChunk

    pending = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    repository.save_chunk(pending)
    running = pending.start(started_at=NOW)
    repository.save_chunk(running)
    completed = running.fail(
        state=HistoricalCaptureChunkState.CREDENTIALS_REQUIRED,
        failure_code="ALPACA_CREDENTIALS_REQUIRED",
        completed_at=NOW,
        operation_id="reserved-op-1",
    )
    repository.save_chunk(completed)

    fresh = PostgresHistoricalMarketDataRepository(postgres_engine)
    (reloaded,) = fresh.list_chunks(plan.plan_id)
    assert reloaded == completed
    assert reloaded.state is HistoricalCaptureChunkState.CREDENTIALS_REQUIRED


# --------------------------------------------------------------------------
# Genuine (fake-transport, real-adapter) two-chunk capture pipeline
# --------------------------------------------------------------------------


def test_genuine_two_chunk_capture_persists_bars_in_the_canonical_securities_store(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APCA_API_KEY_ID", "fixture-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fixture-secret")

    historical_repository, securities_repository, _real, configuration_id = (
        _historical_market_data_stack(postgres_engine)
    )
    plan = CreateHistoricalCapturePlan(
        repository=historical_repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command())
    assert len(plan.chunk_plan) == 2  # chunk_size=1, two symbols

    transport = _Transport(
        [
            _bars_response(
                {
                    "AAPL": [
                        {
                            "t": "2024-01-02T00:00:00Z",
                            "o": "185",
                            "h": "186",
                            "l": "184",
                            "c": "185.5",
                            "v": "1000",
                        },
                        {
                            "t": "2024-01-03T00:00:00Z",
                            "o": "185.5",
                            "h": "187",
                            "l": "185",
                            "c": "186.5",
                            "v": "1200",
                        },
                    ]
                }
            ),
            _bars_response(
                {
                    "MSFT": [
                        {
                            "t": "2024-01-02T00:00:00Z",
                            "o": "370",
                            "h": "372",
                            "l": "369",
                            "c": "371",
                            "v": "900",
                        },
                        {
                            "t": "2024-01-03T00:00:00Z",
                            "o": "371",
                            "h": "373",
                            "l": "370",
                            "c": "372",
                            "v": "950",
                        },
                    ]
                }
            ),
        ]
    )
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    fetch_operation = FetchSecuritiesProviderOperation(
        repository=_real_repository_for(postgres_engine),
        provider=adapter,
        artifacts=_local_artifact_store(),
    )
    canonicalizer = HistoricalAlpacaCanonicalizer(
        securities_repository=securities_repository,
        dataset=LocalSecuritiesDatasetAdapter(),
        raw_root=tmp_path / ".local" / "securities" / "providers" / "alpaca",
    )
    runner = RunHistoricalCaptureChunk(
        repository=historical_repository,
        fetch_operation=fetch_operation,
        canonicalizer=canonicalizer,
        configuration_id=configuration_id,
    )
    first = runner.execute(RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW))
    assert first.state is HistoricalCaptureChunkState.COMPLETED
    second = runner.execute(
        RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW)
    )
    assert second.state is HistoricalCaptureChunkState.COMPLETED
    assert second.previous_chunk_hash == first.canonical_result_hash

    # checkpoint/resume across a *fresh* repository instance -- proves state
    # survives independent of any in-process object, as a separate CLI
    # invocation would see it.
    fresh_repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    assert plan_summary_state(repository=fresh_repository, plan=plan) is (
        HistoricalCapturePlanState.COMPLETE
    )

    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=fresh_repository, evidence=securities_repository
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))
    assert snapshot.completeness_state is HistoricalDatasetCompletenessState.COMPLETE
    assert snapshot.observation_count == 4
    assert snapshot.point_in_time_universe_state is (
        PointInTimeUniverseState.POINT_IN_TIME_UNIVERSE_INCOMPLETE
    )
    assert snapshot.survivorship_state is SecuritiesSurvivorshipState.SURVIVORSHIP_LIMITED

    report = EvaluateHistoricalDataQuality(
        repository=fresh_repository, evidence=securities_repository
    ).execute(
        EvaluateHistoricalDataQualityCommand(snapshot_id=snapshot.snapshot_id, created_at=NOW)
    )
    assert report.captured_security_count == 2
    assert report.observation_count == 4

    # point-in-time replay: as_of the first session excludes the second bar
    replay = HistoricalPointInTimeReplay(
        repository=fresh_repository, evidence=securities_repository
    )
    # After 2024-01-02's session close (~21:00 UTC, EST) plus the 30-minute
    # derived-availability delay, but well before 2024-01-03's own close --
    # exactly one bar should be point-in-time-visible here.
    early_clock = HistoricalClock(as_of=datetime(2024, 1, 3, 10, tzinfo=UTC))
    early_bars = replay.evidence_as_of(
        snapshot_id=snapshot.snapshot_id, stable_security_id="stable_AAPL", clock=early_clock
    )
    assert len(early_bars) == 1
    late_clock = HistoricalClock(as_of=datetime(2024, 1, 4, tzinfo=UTC))
    late_bars = replay.evidence_as_of(
        snapshot_id=snapshot.snapshot_id, stable_security_id="stable_AAPL", clock=late_clock
    )
    assert len(late_bars) == 2
    ret = replay.calculate(
        snapshot_id=snapshot.snapshot_id,
        stable_security_id="stable_AAPL",
        clock=late_clock,
        calculation="simple_return",
    )
    assert ret is not None


def _real_repository_for(engine: Engine) -> PostgresRealSecuritiesDataRepository:
    return PostgresRealSecuritiesDataRepository(engine)


def _local_artifact_store():
    from apex.adapters.alpaca_market_data import LocalAlpacaRawArtifactStore

    return LocalAlpacaRawArtifactStore()


# --------------------------------------------------------------------------
# Credentials-absent path: zero request attempts, honest evidence
# --------------------------------------------------------------------------


def test_credentials_absent_makes_zero_requests_and_snapshot_is_honestly_empty(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)

    historical_repository, securities_repository, _real, configuration_id = (
        _historical_market_data_stack(postgres_engine)
    )
    plan = CreateHistoricalCapturePlan(
        repository=historical_repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command(idempotency_key="test-plan-creds-absent"))

    def _unreachable_transport_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("zero network requests must be attempted without credentials")

    class _UnreachableTransport:
        request = staticmethod(_unreachable_transport_request)

    adapter = AlpacaMarketDataHttpAdapter(transport=_UnreachableTransport())
    assert adapter.credentials_available is False
    fetch_operation = FetchSecuritiesProviderOperation(
        repository=_real_repository_for(postgres_engine),
        provider=adapter,
        artifacts=_local_artifact_store(),
    )
    runner = RunHistoricalCaptureChunk(
        repository=historical_repository,
        fetch_operation=fetch_operation,
        canonicalizer=HistoricalAlpacaCanonicalizer(
            securities_repository=securities_repository,
            dataset=LocalSecuritiesDatasetAdapter(),
        ),
        configuration_id=configuration_id,
    )
    for _ in range(2):
        chunk = runner.execute(
            RunHistoricalCaptureChunkCommand(plan_id=plan.plan_id, requested_at=NOW)
        )
        assert chunk.state is HistoricalCaptureChunkState.CREDENTIALS_REQUIRED
        assert chunk.operation_id is not None

    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=historical_repository, evidence=securities_repository
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))
    assert snapshot.observation_count == 0
    assert snapshot.source_batch_ids == ()
    assert snapshot.completeness_state is HistoricalDatasetCompletenessState.PARTIAL


# --------------------------------------------------------------------------
# Immutable evidence enforcement at the database boundary
# --------------------------------------------------------------------------


def test_plan_snapshot_report_and_identity_rows_are_immutable(postgres_engine: Engine) -> None:
    historical_repository, securities_repository, _real, _configuration_id = (
        _historical_market_data_stack(postgres_engine)
    )
    plan = CreateHistoricalCapturePlan(
        repository=historical_repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command())
    from apex.domain.historical_market_data import HistoricalCaptureChunk

    chunk = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    historical_repository.save_chunk(chunk)
    running = chunk.start(started_at=NOW)
    historical_repository.save_chunk(running)  # each real transition is separately persisted
    failed = running.fail(
        state=HistoricalCaptureChunkState.FAILED,
        failure_code="TEST_FAILURE",
        completed_at=NOW,
        operation_id="op-x",
    )
    historical_repository.save_chunk(failed)
    snapshot = MaterializeHistoricalDatasetSnapshot(
        repository=historical_repository, evidence=securities_repository
    ).execute(MaterializeHistoricalDatasetSnapshotCommand(plan_id=plan.plan_id, created_at=NOW))
    report = EvaluateHistoricalDataQuality(
        repository=historical_repository, evidence=securities_repository
    ).execute(
        EvaluateHistoricalDataQualityCommand(snapshot_id=snapshot.snapshot_id, created_at=NOW)
    )

    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE historical_capture_plan SET provider = 'OTHER' WHERE plan_id = :plan_id"
                ),
                {"plan_id": plan.plan_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM historical_capture_plan"))
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE historical_dataset_snapshot SET observation_count = 999 "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"snapshot_id": snapshot.snapshot_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE historical_data_quality_report SET captured_security_count = 999 "
                    "WHERE report_id = :report_id"
                ),
                {"report_id": report.report_id},
            )
        connection.rollback()
        with pytest.raises(Exception, match="immutable"):
            connection.execute(sa.text("DELETE FROM historical_instrument_identity"))
        connection.rollback()


def test_chunk_forbids_delete_and_invalid_state_transitions(postgres_engine: Engine) -> None:
    historical_repository, _securities, _real, _configuration_id = _historical_market_data_stack(
        postgres_engine
    )
    plan = CreateHistoricalCapturePlan(
        repository=historical_repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command())
    from apex.domain.historical_market_data import HistoricalCaptureChunk

    chunk = HistoricalCaptureChunk.pending(plan=plan, ordinal=0, previous_chunk_hash=None)
    historical_repository.save_chunk(chunk)
    running = chunk.start(started_at=NOW)
    historical_repository.save_chunk(running)

    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="cannot be deleted"):
            connection.execute(sa.text("DELETE FROM historical_capture_chunk"))
        connection.rollback()
        # RUNNING -> PENDING (backward) is not a valid transition; batch_id
        # stays NULL throughout so this exercises the state-machine trigger
        # itself rather than the batch-consistency check constraint.
        with pytest.raises(Exception, match="transition is invalid"):
            connection.execute(
                sa.text(
                    "UPDATE historical_capture_chunk SET state = 'PENDING' "
                    "WHERE chunk_id = :chunk_id"
                ),
                {"chunk_id": chunk.chunk_id},
            )
        connection.rollback()
        # an identity-defining field cannot change even on an otherwise-legal
        # RUNNING -> FAILED transition.
        with pytest.raises(Exception, match="immutable"):
            connection.execute(
                sa.text(
                    "UPDATE historical_capture_chunk "
                    "SET plan_id = 'other-plan', state = 'FAILED' "
                    "WHERE chunk_id = :chunk_id"
                ),
                {"chunk_id": chunk.chunk_id},
            )
        connection.rollback()


# --------------------------------------------------------------------------
# Downgrade protection
# --------------------------------------------------------------------------


def test_retained_historical_evidence_blocks_destructive_0077_downgrade(
    postgres_engine: Engine,
) -> None:
    historical_repository, _securities, _real, _configuration_id = _historical_market_data_stack(
        postgres_engine
    )
    CreateHistoricalCapturePlan(
        repository=historical_repository, identity_source=_FakeIdentitySource(IDENTITIES)
    ).execute(_plan_command())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0077 with retained historical market-data evidence",
        ):
            command.downgrade(_config(connection), "0076_stable_security_identity")
        connection.rollback()
    with postgres_engine.connect() as connection:
        # The multi-step downgrade cascades 0078 -> 0077 (succeeds, no replay
        # evidence retained in this test's scope) then 0077 -> 0076 (rejected
        # by the historical-market-data guard above). Each step commits
        # independently, so the failure leaves alembic_version at the last
        # successfully completed step: 0077, not the original head.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0077_historical_market_data"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0077_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(HISTORICAL_MARKET_DATA_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0076_stable_security_identity")
    assert set(HISTORICAL_MARKET_DATA_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(HISTORICAL_MARKET_DATA_TABLES)


# --------------------------------------------------------------------------
# Anti-lookahead / conflicting replay proof against genuinely persisted rows
# --------------------------------------------------------------------------


def test_conflicting_plan_replay_is_rejected_not_silently_overwritten(
    postgres_engine: Engine,
) -> None:
    repository = PostgresHistoricalMarketDataRepository(postgres_engine)
    service = CreateHistoricalCapturePlan(
        repository=repository, identity_source=_FakeIdentitySource(IDENTITIES)
    )
    service.execute(_plan_command(idempotency_key="conflict-key"))
    with pytest.raises(HistoricalMarketDataApplicationError):
        service.execute(_plan_command(idempotency_key="conflict-key", end_date="2024-02-01"))
