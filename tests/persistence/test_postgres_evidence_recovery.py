"""Historical Evidence Gap Recovery v1 against real PostgreSQL.

Proves the append-only ledger, the database-level live-only protections, the
quarantine of recovered crypto candles, and an end-to-end securities recovery
through the genuine historical capture pipeline with a scripted provider
transport (zero real network I/O).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError

from apex.adapters.alpaca_canonicalization import HistoricalAlpacaCanonicalizer
from apex.adapters.alpaca_market_data import (
    AlpacaMarketDataHttpAdapter,
    LocalAlpacaRawArtifactStore,
    ProviderHttpResponse,
)
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.evidence_recovery_postgres import (
    PostgresEvidenceRecoveryRepository,
    PostgresSecuritiesCoverage,
)
from apex.adapters.evidence_recovery_runner import run_evidence_gap_recovery
from apex.adapters.evidence_recovery_sources import (
    HistoricalPipelineSecuritiesFetcher,
    PostgresCryptoCoverage,
)
from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.application.historical_market_data import (
    CreateHistoricalCapturePlan,
    CreateHistoricalCapturePlanCommand,
    RunHistoricalCaptureChunk,
    RunHistoricalCaptureChunkCommand,
)
from apex.application.real_securities_data import (
    CreateRealProviderConfiguration,
    CreateRealProviderConfigurationCommand,
    FetchSecuritiesProviderOperation,
)
from apex.domain.crypto_universe import CryptoDailyObservation, CryptoProductObservation
from apex.domain.evidence_recovery import (
    RECOVERY_CAPTURE_ID_PREFIX,
    AcquisitionPath,
    EvidenceCoverageGapEvent,
    EvidenceFamily,
    EvidenceRecoveryError,
    EvidenceRecoveryRun,
    EvidenceVintage,
    GapDisposition,
    RecoveryRunStatus,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
R0117 = "0117_trial_outcome_assessment"
R0118 = "0118_evidence_gap_recovery"
NOW = datetime(2026, 10, 2, 0, 5, tzinfo=UTC)
TABLES = ("evidence_recovery_run", "evidence_coverage_gap", "evidence_vintage")


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _run(
    *, cycle: str = "cycle-1", keys: tuple[str, ...] = ("gap-a",), requests: int = 2
) -> EvidenceRecoveryRun:
    return EvidenceRecoveryRun.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        cycle_key=cycle,
        scope_start="2026-08-07",
        scope_end="2026-10-01",
        gap_keys=keys,
        status=RecoveryRunStatus.COMPLETED,
        request_ceiling=10,
        provider_requests=requests,
        gaps_recovered=1,
    )


def _insert_run(engine: Engine, **overrides: object) -> None:
    values: dict[str, object] = {
        "run_id": "r1",
        "evidence_family": "SECURITIES_DAILY_BAR",
        "acquisition_path": "RECOVERY_BACKFILL",
        "request_ceiling": 1,
        "provider_requests": 0,
        "authority": "NONE",
        "canonical_hash": "c" * 64,
        "recorded_at": NOW,
    }
    values.update(overrides)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO evidence_recovery_run (run_id, evidence_family, acquisition_path, "
                "cycle_key, scope_start, scope_end, gap_keys, status, request_ceiling, "
                "provider_requests, gaps_examined, gaps_recovered, gaps_partial, "
                "gaps_irrecoverable, gaps_conflicted, reason_codes, canonical_hash, "
                "recorded_at, authority) VALUES (:run_id, :evidence_family, :acquisition_path, "
                "'c', 'a', 'b', '[]', 'COMPLETED', :request_ceiling, :provider_requests, "
                "0, 0, 0, 0, 0, '[]', :canonical_hash, :recorded_at, :authority)"
            ),
            values,
        )


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")) or 0)  # nosec B608


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------


def test_run_gap_and_vintage_round_trip_and_replay(postgres_engine: Engine) -> None:
    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    assert repository.register_run(run=run, recorded_at=NOW) is False
    assert repository.register_run(run=run, recorded_at=NOW + timedelta(hours=1)) is True
    assert PostgresEvidenceRecoveryRepository(postgres_engine).run(run.run_id) == run
    with pytest.raises(EvidenceRecoveryError, match="IDENTITY_CONFLICT"):
        repository.register_run(run=_run(requests=3), recorded_at=NOW)

    first = EvidenceCoverageGapEvent.detect(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst-1",
        interval_start="2026-08-07",
        interval_end="2026-08-14",
    )
    assert repository.register_gap_event(event=first, recorded_at=NOW) is False
    assert repository.register_gap_event(event=first, recorded_at=NOW) is True
    done = first.transition(
        disposition=GapDisposition.RECOVERED,
        reason_codes=("RECOVERED_FROM_PROVIDER_HISTORY",),
        recovery_run_id=run.run_id,
        recovered_evidence_ids=("bar-1",),
    )
    assert repository.register_gap_event(event=done, recorded_at=NOW) is False
    history = repository.gap_history(first.gap_key)
    assert [item.disposition for item in history] == [GapDisposition.OPEN, GapDisposition.RECOVERED]
    assert repository.current_gaps()[0] == done
    assert repository.unresolved_gaps(evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR) == ()
    # A terminal gap cannot move again, and a skipped ordinal is refused.
    with pytest.raises(EvidenceRecoveryError, match="TRANSITION_INVALID"):
        done.transition(
            disposition=GapDisposition.IRRECOVERABLE, reason_codes=("x",), recovery_run_id="r"
        )
    skipped = EvidenceCoverageGapEvent.detect(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst-2",
        interval_start="2026-08-07",
        interval_end="2026-08-14",
    ).transition(
        disposition=GapDisposition.OPEN,
        reason_codes=(),
        recovery_run_id=None,
        interval_end="2026-08-15",
    )
    with pytest.raises(EvidenceRecoveryError, match="OUT_OF_SEQUENCE"):
        repository.register_gap_event(event=skipped, recorded_at=NOW)

    vintage = EvidenceVintage.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        evidence_id="bar-1",
        market_time=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
        available_at=datetime(2026, 8, 7, 20, 30, tzinfo=UTC),
        acquired_at=NOW,
        acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
        recovery_run_id=run.run_id,
    )
    assert repository.register_vintages(vintages=(vintage,), recorded_at=NOW) == 1
    assert repository.register_vintages(vintages=(vintage,), recorded_at=NOW) == 0
    stored = repository.vintages_for(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR, evidence_id="bar-1"
    )
    assert stored == (vintage,)
    assert repository.vintage_count() == 1


# ---------------------------------------------------------------------------
# Database-level protections
# ---------------------------------------------------------------------------


def _insert_vintage(engine: Engine, **overrides: object) -> None:
    values: dict[str, object] = {
        "vintage_id": "v1",
        "evidence_family": "SECURITIES_DAILY_BAR",
        "evidence_id": "e1",
        "market_time": datetime(2026, 9, 1, tzinfo=UTC),
        "available_at": None,
        "acquired_at": datetime(2026, 10, 1, tzinfo=UTC),
        "acquisition_path": "SCHEDULED_LIVE",
        "recovery_run_id": None,
        "canonical_hash": "a" * 64,
        "recorded_at": NOW,
        "authority": "NONE",
    }
    values.update(overrides)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO evidence_vintage (vintage_id, evidence_family, evidence_id, "
                "market_time, available_at, acquired_at, acquisition_path, recovery_run_id, "
                "canonical_hash, recorded_at, authority) VALUES (:vintage_id, :evidence_family, "
                ":evidence_id, :market_time, :available_at, :acquired_at, :acquisition_path, "
                ":recovery_run_id, :canonical_hash, :recorded_at, :authority)"
            ),
            values,
        )


def test_database_refuses_recovered_evidence_for_live_only_families(
    postgres_engine: Engine,
) -> None:
    """Test 6: recovered evidence cannot be recorded for any live-only family,
    and a recovered vintage always names its recovery run."""

    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    repository.register_run(run=run, recorded_at=NOW)
    for family in (
        "OPTIONS_CHAIN_SESSION",
        "CRYPTO_ORDER_BOOK",
        "LIVE_OBSERVATION_WINDOW",
        "PAPER_EXECUTION_FACT",
        "KALSHI_REFRESH_DATE",
    ):
        with pytest.raises(
            IntegrityError, match=r"vintage_recovered_shape|vintage_live_only_family"
        ):
            _insert_vintage(
                postgres_engine,
                evidence_family=family,
                acquisition_path="RECOVERY_BACKFILL",
                recovery_run_id=run.run_id,
            )
        with pytest.raises(IntegrityError, match="vintage_live_only_family"):
            _insert_vintage(
                postgres_engine, evidence_family=family, acquisition_path="OPERATOR_IMPORT"
            )
    with pytest.raises(IntegrityError, match="vintage_recovered_shape"):
        _insert_vintage(postgres_engine, acquisition_path="RECOVERY_BACKFILL", recovery_run_id=None)
    with pytest.raises(IntegrityError, match="vintage_path"):
        _insert_vintage(postgres_engine, acquisition_path="UNKNOWN_LEGACY")
    # A live-only family may only carry contemporaneous provenance.
    _insert_vintage(postgres_engine, evidence_family="CRYPTO_ORDER_BOOK")


def test_database_refuses_recovering_a_live_only_gap_and_unreasoned_irrecoverable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    repository.register_run(run=run, recorded_at=NOW)

    def insert(**overrides: object) -> None:
        values: dict[str, object] = {
            "event_id": "ev-1",
            "gap_key": "gk-1",
            "ordinal": 1,
            "evidence_family": "OPTIONS_CHAIN_SESSION",
            "subject_id": "s",
            "interval_start": "2026-09-08",
            "interval_end": "2026-09-18",
            "disposition": "IRRECOVERABLE",
            "reason_codes": '["OPTION_CHAIN_NOT_RECONSTRUCTIBLE"]',
            "recovery_run_id": None,
            "recovered_evidence_ids": "[]",
            "canonical_hash": "b" * 64,
            "recorded_at": NOW,
            "authority": "NONE",
        }
        values.update(overrides)
        with postgres_engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO evidence_coverage_gap (event_id, gap_key, ordinal, "
                    "evidence_family, subject_id, interval_start, interval_end, disposition, "
                    "reason_codes, recovery_run_id, recovered_evidence_ids, canonical_hash, "
                    "recorded_at, authority) VALUES (:event_id, :gap_key, :ordinal, "
                    ":evidence_family, :subject_id, :interval_start, :interval_end, "
                    ":disposition, :reason_codes, :recovery_run_id, :recovered_evidence_ids, "
                    ":canonical_hash, :recorded_at, :authority)"
                ),
                values,
            )

    with pytest.raises(IntegrityError, match="gap_live_only_irrecoverable"):
        insert(disposition="RECOVERED", ordinal=2, recovery_run_id=run.run_id)
    with pytest.raises(IntegrityError, match="gap_live_only_irrecoverable"):
        insert(disposition="PARTIALLY_RECOVERED", ordinal=2, recovery_run_id=run.run_id)
    with pytest.raises(IntegrityError, match="gap_irrecoverable_reason"):
        insert(reason_codes="[]")
    with pytest.raises(IntegrityError, match="gap_first_event"):
        insert(
            evidence_family="SECURITIES_DAILY_BAR",
            disposition="RECOVERED",
            recovery_run_id=run.run_id,
        )
    with pytest.raises(IntegrityError, match="gap_resolution_run"):
        insert(
            evidence_family="SECURITIES_DAILY_BAR",
            disposition="RECOVERED",
            ordinal=2,
            recovery_run_id=None,
        )
    with pytest.raises(IntegrityError, match="gap_authority"):
        insert(authority="LIVE")
    insert()  # a well-formed irrecoverable option gap is accepted


def test_run_and_ledger_tables_are_immutable_with_authority_none(postgres_engine: Engine) -> None:
    """Test 16 plus immutability."""

    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    repository.register_run(run=run, recorded_at=NOW)
    for statement in (
        "UPDATE evidence_recovery_run SET status = 'FAILED'",
        "DELETE FROM evidence_recovery_run",
    ):
        with (
            pytest.raises(
                (InternalError, ProgrammingError, IntegrityError, Exception), match="immutable"
            ),
            postgres_engine.begin() as connection,
        ):
            connection.execute(sa.text(statement))
    with pytest.raises(IntegrityError, match="run_authority"):
        _insert_run(postgres_engine, run_id="r2", authority="LIVE")
    with pytest.raises(IntegrityError, match="run_family_recoverable"):
        _insert_run(postgres_engine, run_id="r3", evidence_family="OPTIONS_CHAIN_SESSION")
    with pytest.raises(IntegrityError, match="run_path"):
        _insert_run(postgres_engine, run_id="r4", acquisition_path="SCHEDULED_LIVE")
    with pytest.raises(IntegrityError, match="run_request_ceiling"):
        _insert_run(postgres_engine, run_id="r5", provider_requests=5)


def test_live_observation_table_cannot_accept_a_recovered_evidence_class(
    postgres_engine: Engine,
) -> None:
    """Test 6: the live-only observation table has a closed evidence-class set
    that contains no recovery class, so recovered evidence cannot enter it."""

    with (
        pytest.raises(IntegrityError, match="ck_live_market_observation_evidence_class"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO live_market_observation (observation_id, round_id, venue, "
                "instrument_id, observation_type, captured_at, evidence_class, quality, "
                "payload, canonical_hash, authority) VALUES ('o1','r1','CRYPTO','BTC-USD',"
                "'BAR', :t, 'RECOVERED','PASSED','{}', :h,'NONE')"
            ),
            {"t": NOW, "h": "e" * 64},
        )


# ---------------------------------------------------------------------------
# Crypto quarantine
# ---------------------------------------------------------------------------


def _observation(observation_id: str, capture_id: str, day: date) -> CryptoProductObservation:
    candle = CryptoDailyObservation.create(
        bucket_start=datetime.combine(day, datetime.min.time(), UTC), close="100", volume="5"
    )
    return CryptoProductObservation.create(
        observation_id=observation_id,
        source_capture_id=capture_id,
        product_id="BTC-USD",
        interval_start=candle.bucket_start,
        interval_end=candle.bucket_start + timedelta(days=1),
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        candles=(candle,),
        missing_buckets=0,
        duplicate_records=0,
        rejected_records=0,
        request_count=1,
        raw_evidence_hashes=("f" * 64,),
        source_contract_version="COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_V1",
        parser_version="COINBASE_PUBLIC_DAILY_CANDLE_PARSER_V1",
        checkpoint_ordinal=1,
        completed_at=NOW,
        canonical_asset_id="BTC",
    )


def test_recovered_crypto_candles_are_hidden_from_every_ordinary_consumer(
    postgres_engine: Engine,
) -> None:
    """Test 7: a recovered candle cannot become a follow-up price, pulse reuse
    or PAPER freshness source; only the recovery coverage reader sees it."""

    repository = PostgresCryptoUniverseRepository(postgres_engine)
    repository.save_observation(_observation("obs-live", "capture-live", date(2026, 9, 1)))
    repository.save_observation(
        _observation("obs-recovered", RECOVERY_CAPTURE_ID_PREFIX + "abc", date(2026, 9, 2))
    )
    assert [item.observation_id for item in repository.observations()] == ["obs-live"]
    assert {item.observation_id for item in repository.observations(include_recovered=True)} == {
        "obs-live",
        "obs-recovered",
    }
    coverage = PostgresCryptoCoverage(_PulseLikeRepository(repository)).coverage()
    assert coverage[0].retained_days == frozenset({date(2026, 9, 1), date(2026, 9, 2)})


class _PulseLikeRepository:
    """Adds the (empty) pulse reads ``PostgresCryptoCoverage`` composes."""

    def __init__(self, inner: PostgresCryptoUniverseRepository) -> None:
        self._inner = inner

    def observations(self, *, include_recovered: bool = False):  # type: ignore[no-untyped-def]
        return self._inner.observations(include_recovered=include_recovered)

    def refresh_runs(self) -> tuple[object, ...]:
        return ()

    def pulse_checkpoints(self, refresh_id: str) -> tuple[object, ...]:
        return ()


# ---------------------------------------------------------------------------
# End-to-end securities recovery through the genuine pipeline
# ---------------------------------------------------------------------------


class _Transport:
    def __init__(self, responses: list[ProviderHttpResponse]) -> None:
        self.responses = responses
        self.requests = 0

    def request(self, **_kwargs: object) -> ProviderHttpResponse:
        self.requests += 1
        return self.responses.pop(0)


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


class _IdentitySource:
    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]:
        return (_identity("AAPL", "0000320193"), _identity("MSFT", "0000789019"))


def _bar(day: str, base: int) -> dict[str, str]:
    return {
        "t": f"{day}T00:00:00Z",
        "o": str(base),
        "h": str(base + 2),
        "l": str(base - 1),
        "c": str(base + 1),
        "v": "1000",
    }


def _response(bars: dict[str, list[dict[str, str]]]) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=200,
        headers={},
        body=json.dumps({"bars": bars}, separators=(",", ":")).encode("utf-8"),
        received_at=datetime(2024, 1, 10, 0, 1, tzinfo=UTC),
    )


def _paper_and_live_counts(engine: Engine) -> dict[str, int]:
    tables = (
        "apex_crypto_paper_account",
        "apex_kalshi_paper_account",
        "paper_trial_authorization",
        "internal_strategy_paper_trial",
        "internal_strategy_paper_trial_closure",
        "live_market_observation",
        "live_market_observation_round",
        "apex_operating_cycle_run",
        "apex_operating_task_result",
        "options_chain_snapshot",
    )
    return {table: _count(engine, table) for table in tables}


def test_end_to_end_securities_recovery_is_exact_idempotent_and_authority_free(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tests 1, 2, 3, 11, 15, 17-ish, 18 and 8 against the genuine capture
    pipeline: a multi-day gap after retained 2024-01-02/03 bars is recovered
    exactly, a replay makes zero requests, and no PAPER/order/live row exists."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APCA_API_KEY_ID", "fixture-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fixture-secret")
    securities = PostgresSecuritiesRepository(postgres_engine)
    real = PostgresRealSecuritiesDataRepository(postgres_engine)
    historical = PostgresHistoricalMarketDataRepository(postgres_engine)
    configuration = CreateRealProviderConfiguration(real).execute(
        CreateRealProviderConfigurationCommand(
            environment="TEST",
            adapter_version=AlpacaMarketDataHttpAdapter.adapter_version,
            endpoint_policy_id="TEST_POLICY",
            endpoint_policy_hash="a" * 64,
            operator_action_id="test-operator",
            created_at=NOW,
        )
    )

    seed_transport = _Transport(
        [
            _response(
                {
                    "AAPL": [_bar("2024-01-02", 185), _bar("2024-01-03", 186)],
                    "MSFT": [_bar("2024-01-02", 370), _bar("2024-01-03", 371)],
                }
            )
        ]
    )
    seed_plan = CreateHistoricalCapturePlan(
        repository=historical, identity_source=_IdentitySource()
    ).execute(
        CreateHistoricalCapturePlanCommand(
            symbols=("AAPL", "MSFT"),
            etf_symbols=frozenset(),
            provider="ALPACA_MARKET_DATA_V2",
            start_date="2024-01-02",
            end_date="2024-01-03",
            chunk_size=20,
            maximum_instruments=10,
            maximum_requests=10,
            maximum_bytes=5_000_000,
            evidence_class="HISTORICAL_PUBLIC",
            operator_action_id="seed",
            idempotency_key="seed-plan",
            created_at=NOW,
        )
    )
    RunHistoricalCaptureChunk(
        repository=historical,
        fetch_operation=FetchSecuritiesProviderOperation(
            repository=real,
            provider=AlpacaMarketDataHttpAdapter(transport=seed_transport),
            artifacts=LocalAlpacaRawArtifactStore(),
        ),
        canonicalizer=HistoricalAlpacaCanonicalizer(
            securities_repository=securities, dataset=LocalSecuritiesDatasetAdapter()
        ),
        configuration_id=configuration.configuration_id,
    ).execute(RunHistoricalCaptureChunkCommand(plan_id=seed_plan.plan_id, requested_at=NOW))

    coverage = PostgresSecuritiesCoverage(postgres_engine)
    assert {item.symbol for item in coverage.instruments()} == {"AAPL", "MSFT"}
    assert len(coverage.bars(start=date(2024, 1, 1), end=date(2024, 1, 31))) == 4
    before = _paper_and_live_counts(postgres_engine)

    # The workstation was off Jan 4-9. Provider history has every session.
    recovery_transport = _Transport(
        [
            _response(
                {
                    "AAPL": [
                        _bar("2024-01-04", 187),
                        _bar("2024-01-05", 188),
                        _bar("2024-01-08", 189),
                        _bar("2024-01-09", 190),
                    ],
                    "MSFT": [
                        _bar("2024-01-04", 372),
                        _bar("2024-01-05", 373),
                        _bar("2024-01-08", 374),
                        _bar("2024-01-09", 375),
                    ],
                }
            )
        ]
    )
    fetcher = HistoricalPipelineSecuritiesFetcher(
        historical_repository=historical,
        identity_source=_IdentitySource(),
        real_repository=real,
        securities_repository=securities,
        provider=AlpacaMarketDataHttpAdapter(transport=recovery_transport),
        artifacts=LocalAlpacaRawArtifactStore(),
        dataset=LocalSecuritiesDatasetAdapter(),
    )
    as_of = datetime(2024, 1, 10, 0, 0, tzinfo=UTC)
    report = run_evidence_gap_recovery(
        engine=postgres_engine,
        as_of=as_of,
        executed_at=as_of + timedelta(minutes=5),
        cycle_key="cycle-20240110",
        securities_fetcher=fetcher,
        crypto_fetcher=None,
        crypto_repository=None,
    )
    assert report.errors == ()
    summary = report.securities
    assert summary is not None and summary.status is RecoveryRunStatus.COMPLETED
    assert recovery_transport.requests == 1 and summary.provider_requests == 1
    assert summary.bars_acquired == 8

    recovered = coverage.bars(start=date(2024, 1, 4), end=date(2024, 1, 9))
    assert len(recovered) == 8
    assert len({(item.instrument_id, item.session_date) for item in recovered}) == 8
    assert {item.values.close for item in recovered if item.session_date == date(2024, 1, 9)} == {
        191,
        376,
    }
    # Retained history is untouched (immutable), sessions are XNYS sessions.
    assert {item.session_date for item in recovered} == {
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
    }

    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    gaps = repository.current_gaps(evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR)
    assert gaps and all(item.disposition is GapDisposition.RECOVERED for item in gaps)
    run = repository.run(summary.run.run_id)  # type: ignore[union-attr]
    assert run is not None and run.provider_requests == 1
    old = [
        item
        for item in _all_vintages(postgres_engine)
        if item.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
    ]
    assert old
    assert all(
        item.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
        for item in _all_vintages(postgres_engine)
    )
    assert all(item.recovery_run_id == run.run_id for item in old)

    # Idempotent: the scripted transport is exhausted, so any request would fail.
    again = run_evidence_gap_recovery(
        engine=postgres_engine,
        as_of=as_of,
        executed_at=as_of + timedelta(minutes=10),
        cycle_key="cycle-20240110",
        securities_fetcher=fetcher,
        crypto_fetcher=None,
        crypto_repository=None,
    )
    assert again.errors == () and recovery_transport.requests == 1
    assert (again.securities is None) or again.securities.provider_requests == 0
    assert len(coverage.bars(start=date(2024, 1, 4), end=date(2024, 1, 9))) == 8
    assert _count(postgres_engine, "evidence_recovery_run") == 1

    # No order, fill, reconciliation, cycle, live observation or option row.
    assert _paper_and_live_counts(postgres_engine) == before
    for table in ("evidence_recovery_run", "evidence_coverage_gap", "evidence_vintage"):
        with postgres_engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(f"SELECT COUNT(*) FROM {table} WHERE authority <> 'NONE'")
                )  # nosec B608
                == 0
            )


def _all_vintages(engine: Engine) -> list[EvidenceVintage]:
    repository = PostgresEvidenceRecoveryRepository(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT evidence_family, evidence_id FROM evidence_vintage")
        ).all()
    found: list[EvidenceVintage] = []
    for family, evidence_id in rows:
        found.extend(
            repository.vintages_for(evidence_family=EvidenceFamily(family), evidence_id=evidence_id)
        )
    return found


def test_securities_recovery_without_credentials_is_blocked_with_zero_requests(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    # Seed one bar set through the importer directly is unnecessary: with no
    # retained instrument there is simply nothing to recover.
    report = run_evidence_gap_recovery(
        engine=postgres_engine,
        as_of=datetime(2024, 1, 10, tzinfo=UTC),
        executed_at=datetime(2024, 1, 10, 0, 5, tzinfo=UTC),
        cycle_key="cycle-empty",
        securities_fetcher=None,
        crypto_fetcher=None,
        crypto_repository=None,
    )
    assert report.errors == ()
    assert report.securities is not None
    assert report.securities.status is RecoveryRunStatus.NOTHING_TO_DO
    assert report.provider_requests == 0


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_downgrade_refuses_while_recovery_evidence_is_retained_and_round_trips_when_empty(
    postgres_engine: Engine,
) -> None:
    PostgresEvidenceRecoveryRepository(postgres_engine).register_run(run=_run(), recorded_at=NOW)
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="cannot downgrade 0118"):
            command.downgrade(_config(connection), R0117)
        connection.rollback()
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == R0118
    with postgres_engine.begin() as connection:
        connection.execute(sa.text("ALTER TABLE evidence_recovery_run DISABLE TRIGGER USER"))
        connection.execute(sa.text("DELETE FROM evidence_recovery_run"))
        connection.execute(sa.text("ALTER TABLE evidence_recovery_run ENABLE TRIGGER USER"))
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), R0117)
        connection.commit()
    with postgres_engine.connect() as connection:
        for table in TABLES:
            assert connection.scalar(sa.text(f"SELECT to_regclass('{table}')")) is None
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
        connection.commit()
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == R0118
        for table in TABLES:
            assert connection.scalar(sa.text(f"SELECT to_regclass('{table}')")) is not None


# ---------------------------------------------------------------------------
# Crypto fetch composition and the recurring operating-cycle task
# ---------------------------------------------------------------------------


class _CandleSource:
    """Scripted public candle source: returns every requested day."""

    def __init__(self, *, close: str = "100") -> None:
        self.calls = 0
        self._close = close

    def capture_daily_observations(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ):  # type: ignore[no-untyped-def]
        from apex.application.crypto_universe import CapturedObservationPage

        self.calls += 1
        assert product_id == "BTC-USD"
        assert 91 <= (interval_end - interval_start).days <= 300
        days = (interval_end - interval_start).days
        candles = tuple(
            CryptoDailyObservation.create(
                bucket_start=interval_start + timedelta(days=n), close=self._close, volume="5"
            )
            for n in range(days)
        )
        return CapturedObservationPage(requested_at, b"{}", candles, 0, 0, 0, "fixture://", 1)


def test_crypto_recovery_retains_a_quarantined_observation_and_closes_the_gap(
    postgres_engine: Engine,
) -> None:
    from apex.adapters.evidence_recovery_sources import CoinbaseCryptoRecoveryFetcher
    from apex.application.evidence_recovery import RecoverCryptoCandles

    repository = PostgresCryptoUniverseRepository(postgres_engine)
    start = date(2026, 1, 1)
    # 280 retained days with a five-day hole, so the gap sits inside the window.
    retained = [start + timedelta(days=n) for n in range(280)]
    hole = {date(2026, 4, 1) + timedelta(days=n) for n in range(5)}
    candles = tuple(
        CryptoDailyObservation.create(
            bucket_start=datetime.combine(day, datetime.min.time(), UTC), close="90", volume="5"
        )
        for day in retained
        if day not in hole
    )
    repository.save_observation(
        CryptoProductObservation.create(
            observation_id="obs-history",
            source_capture_id="capture-history",
            product_id="BTC-USD",
            interval_start=candles[0].bucket_start,
            interval_end=candles[-1].bucket_start + timedelta(days=1),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            candles=candles,
            missing_buckets=5,
            duplicate_records=0,
            rejected_records=0,
            request_count=1,
            raw_evidence_hashes=("f" * 64,),
            source_contract_version="COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_V1",
            parser_version="COINBASE_PUBLIC_DAILY_CANDLE_PARSER_V1",
            checkpoint_ordinal=1,
            completed_at=NOW,
            canonical_asset_id="BTC",
        )
    )
    source = _CandleSource()
    ledger = PostgresEvidenceRecoveryRepository(postgres_engine)
    as_of = datetime(2026, 10, 1, tzinfo=UTC)
    summary = RecoverCryptoCandles(
        repository=ledger,
        coverage=PostgresCryptoCoverage(_PulseLikeRepository(repository)),
        fetcher=CoinbaseCryptoRecoveryFetcher(source=source, repository=repository),
    ).execute(as_of=as_of, executed_at=as_of + timedelta(minutes=1), cycle_key="cycle-crypto")
    assert summary.status is RecoveryRunStatus.COMPLETED and source.calls == 1
    head = next(
        item
        for item in ledger.current_gaps(evidence_family=EvidenceFamily.CRYPTO_DAILY_CANDLE)
        if item.interval_start == "2026-04-01"
    )
    assert head.disposition is GapDisposition.RECOVERED
    # The recovered candles exist but no ordinary reader can see them.
    visible = {item.observation_id for item in repository.observations()}
    assert visible == {"obs-history"}
    recovered = [
        item
        for item in repository.observations(include_recovered=True)
        if item.source_capture_id.startswith(RECOVERY_CAPTURE_ID_PREFIX)
    ]
    assert len(recovered) == 1 and len(recovered[0].candles) == 5
    assert {c.bucket_start.date() for c in recovered[0].candles} == hole
    vintages = _all_vintages(postgres_engine)
    assert len(vintages) == 5
    assert all(item.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL for item in vintages)
    # Replay: no further request.
    again = RecoverCryptoCandles(
        repository=ledger,
        coverage=PostgresCryptoCoverage(_PulseLikeRepository(repository)),
        fetcher=CoinbaseCryptoRecoveryFetcher(source=source, repository=repository),
    ).execute(as_of=as_of, executed_at=as_of + timedelta(minutes=2), cycle_key="cycle-crypto")
    assert source.calls == 1 and again.provider_requests == 0


def test_recurring_task_runs_through_the_real_executor_without_authority(
    postgres_engine: Engine,
) -> None:
    from test_postgres_strategy_paper_trial_closure_readiness import _Executors, _plan

    from apex.domain.operating_cycle import (
        DueDisposition,
        DueTask,
        OperatingTaskStatus,
        OperatingTaskType,
    )

    env = _Executors(postgres_engine)
    as_of = datetime(2026, 10, 2, tzinfo=UTC)
    cycle_id = "evidence-gap-recovery-20261002"
    before = _paper_and_live_counts(postgres_engine)
    result = env.executor.execute(
        task=DueTask.create(
            task_type=OperatingTaskType.EVIDENCE_GAP_RECOVERY,
            disposition=DueDisposition.DUE,
            reasons=("EVIDENCE_GAP_RECOVERY_MANAGED_EVERY_CYCLE",),
            dependency_types=(),
            source_evidence_ids=(),
            cycle_id=cycle_id,
            network_behavior="BOUNDED_PUBLIC_HISTORICAL_READ_ONLY_GETS",
        ),
        cycle_id=cycle_id,
        plan=_plan(created_at=as_of),
        as_of=as_of,
        executed_at=as_of + timedelta(minutes=5),
    )
    assert result.status is OperatingTaskStatus.COMPLETED
    assert result.network_requests == 0 and result.error_code is None
    assert "LIVE_AUTHORITY_NONE" in result.warnings
    assert _paper_and_live_counts(postgres_engine) == before


# ---------------------------------------------------------------------------
# Review hardening
# ---------------------------------------------------------------------------


def test_commit_recovery_is_atomic(postgres_engine: Engine) -> None:
    """A run, its gap resolutions and its vintages are retained together or
    not at all: an illegal resolution rolls back the run and the vintages."""

    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    opened = EvidenceCoverageGapEvent.detect(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst-1",
        interval_start="2026-08-07",
        interval_end="2026-08-14",
    )
    repository.register_gap_event(event=opened, recorded_at=NOW)
    resolved = opened.transition(
        disposition=GapDisposition.RECOVERED,
        reason_codes=("RECOVERED_FROM_PROVIDER_HISTORY",),
        recovery_run_id=run.run_id,
    )
    vintage = EvidenceVintage.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        evidence_id="bar-atomic",
        market_time=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
        available_at=datetime(2026, 8, 7, 20, 30, tzinfo=UTC),
        acquired_at=NOW,
        acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
        recovery_run_id=run.run_id,
    )
    out_of_sequence = EvidenceCoverageGapEvent.detect(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst-2",
        interval_start="2026-08-07",
        interval_end="2026-08-14",
    ).transition(
        disposition=GapDisposition.OPEN,
        reason_codes=(),
        recovery_run_id=None,
        interval_end="2026-08-20",
    )
    with pytest.raises(EvidenceRecoveryError, match="OUT_OF_SEQUENCE"):
        repository.commit_recovery(
            run=run, events=(resolved, out_of_sequence), vintages=(vintage,), recorded_at=NOW
        )
    assert repository.run(run.run_id) is None
    assert repository.vintage_count() == 0
    assert [item.disposition for item in repository.gap_history(opened.gap_key)] == [
        GapDisposition.OPEN
    ]
    # The same commit without the bad event is retained whole, and replays.
    assert (
        repository.commit_recovery(
            run=run, events=(resolved,), vintages=(vintage,), recorded_at=NOW
        )
        == 1
    )
    assert (
        repository.commit_recovery(
            run=run, events=(resolved,), vintages=(vintage,), recorded_at=NOW
        )
        == 0
    )
    assert repository.run(run.run_id) == run


def test_revision_conflict_may_be_a_first_event_but_a_live_only_family_may_not(
    postgres_engine: Engine,
) -> None:
    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    conflict = EvidenceCoverageGapEvent.detect_conflict(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst-1#revision",
        interval_start="2026-08-06",
        interval_end="2026-08-06",
        recovery_run_id=run.run_id,
        reason_codes=("PROVIDER_REVISION_CONFLICT",),
    )
    assert (
        repository.commit_recovery(run=run, events=(conflict,), vintages=(), recorded_at=NOW) == 0
    )
    assert repository.current_gaps()[0].disposition is GapDisposition.CONFLICTED
    with pytest.raises(EvidenceRecoveryError, match="LIVE_ONLY_GAP_CANNOT_BE_CONFLICTED"):
        EvidenceCoverageGapEvent.detect_conflict(
            evidence_family=EvidenceFamily.OPTIONS_CHAIN_SESSION,
            subject_id="x",
            interval_start="a",
            interval_end="a",
            recovery_run_id="r",
            reason_codes=("x",),
        )


def test_crypto_coverage_ignores_products_outside_the_active_universe(
    postgres_engine: Engine,
) -> None:
    repository = PostgresCryptoUniverseRepository(postgres_engine)
    repository.save_observation(_observation("obs-live", "capture-live", date(2026, 9, 1)))
    inner = _PulseLikeRepository(repository)
    assert PostgresCryptoCoverage(inner, active_product_ids=frozenset({"BTC-USD"})).coverage()
    assert (
        PostgresCryptoCoverage(inner, active_product_ids=frozenset({"BTC-USDC"})).coverage() == ()
    )
    assert PostgresCryptoCoverage(inner, active_product_ids=frozenset()).coverage() == ()


def _legacy_row(engine: Engine, vintage: EvidenceVintage) -> None:
    _insert_vintage(
        engine,
        vintage_id=vintage.vintage_id,
        evidence_id=vintage.evidence_id,
        market_time=vintage.market_time,
        available_at=vintage.available_at,
        acquired_at=vintage.acquired_at,
        acquisition_path=vintage.acquisition_path.value,
        recovery_run_id=vintage.recovery_run_id,
        canonical_hash=vintage.canonical_hash,
    )


def _path_vintage(evidence_id: str, path: AcquisitionPath, run_id: str | None) -> EvidenceVintage:
    return EvidenceVintage.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        evidence_id=evidence_id,
        market_time=datetime(2026, 10, 1, 13, 30, tzinfo=UTC),
        available_at=datetime(2026, 10, 1, 20, 30, tzinfo=UTC),
        acquired_at=datetime(2026, 10, 2, 0, 5, tzinfo=UTC),
        acquisition_path=path,
        recovery_run_id=run_id,
    )


def test_acquisition_path_correction_is_append_only_exact_and_idempotent(
    postgres_engine: Engine,
) -> None:
    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    repository.register_run(run=run, recorded_at=NOW)
    mislabelled = [
        _path_vintage(f"m{i}", AcquisitionPath.SCHEDULED_LIVE, run.run_id) for i in range(2)
    ]
    unrelated_live = _path_vintage("u1", AcquisitionPath.SCHEDULED_LIVE, None)
    already_recovery = _path_vintage("r1", AcquisitionPath.RECOVERY_BACKFILL, run.run_id)
    for row in (*mislabelled, unrelated_live, already_recovery):
        _legacy_row(postgres_engine, row)
    before = {item.vintage_id: item for item in _all_vintages(postgres_engine)}

    assert {item.evidence_id for item in repository.mislabelled_recovery_vintages()} == {
        "m0",
        "m1",
    }
    with pytest.raises(EvidenceRecoveryError, match="COUNT_MISMATCH"):
        repository.correct_acquisition_paths(recorded_at=NOW, expected_count=3)
    assert repository.vintage_count() == len(before)

    assert repository.correct_acquisition_paths(recorded_at=NOW, expected_count=2) == 2
    assert repository.correct_acquisition_paths(recorded_at=NOW, expected_count=0) == 0
    after = {item.vintage_id: item for item in _all_vintages(postgres_engine)}
    assert before.items() <= after.items(), "original rows and hashes are untouched"
    added = [item for key, item in after.items() if key not in before]
    assert {item.evidence_id for item in added} == {"m0", "m1"}
    assert all(
        item.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
        and item.recovery_run_id == run.run_id
        for item in added
    )
    # The unrelated rows gained nothing.
    assert (
        len(
            repository.vintages_for(
                evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR, evidence_id="u1"
            )
        )
        == 1
    )


def test_writer_refuses_new_scheduled_live_vintage_carrying_a_recovery_run(
    postgres_engine: Engine,
) -> None:
    repository = PostgresEvidenceRecoveryRepository(postgres_engine)
    run = _run()
    repository.register_run(run=run, recorded_at=NOW)
    with pytest.raises(EvidenceRecoveryError, match="CANNOT_CARRY_RECOVERY_RUN"):
        repository.register_vintages(
            vintages=(_path_vintage("x", AcquisitionPath.SCHEDULED_LIVE, run.run_id),),
            recorded_at=NOW,
        )
