from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.securities_data import (
    LocalSecuritiesDatasetAdapter,
    RetainedSecuritiesCalendar,
    SecuritiesManagedDataError,
    securities_record,
)
from apex.adapters.securities_external import DeterministicLeanFixtureAdapter
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    InMemorySecuritiesCampaignBoundary,
    InMemorySecuritiesRepository,
    RunSecuritiesConformance,
    RunSecuritiesConformanceCommand,
    RunSecuritiesResearch,
    RunSecuritiesResearchCommand,
    SecuritiesApplicationError,
    SecuritiesIdempotencyConflictError,
)
from apex.domain.securities import (
    SECURITIES_SHADOW_WARNING,
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesConformanceClassification,
    SecuritiesCorporateActionEvidence,
    SecuritiesError,
    SecuritiesInstrument,
    SecuritiesSignalDirection,
    SecuritiesStrategyKind,
    SecuritiesStrategySpecification,
    build_baseline_signals,
    validate_dataset_evidence,
)
from apex.domain.values import ValueValidationError

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _evidence(
    *, session_count: int = 8
) -> tuple[
    tuple[SecuritiesInstrument, ...],
    tuple[SecuritiesCalendarSession, ...],
    tuple[SecuritiesBarEvidence, ...],
    tuple[SecuritiesCorporateActionEvidence, ...],
]:
    dates = tuple(date(2026, 1, 2) + timedelta(days=index) for index in range(session_count))
    sessions = tuple(
        SecuritiesCalendarSession.create(
            calendar_identity="XNYS",
            calendar_version="test-v1",
            timezone="America/New_York",
            session_date=value.isoformat(),
            opens_at=datetime.combine(value, time(14, 30), tzinfo=UTC),
            closes_at=datetime.combine(value, time(21), tzinfo=UTC),
            is_early_close=index == 2,
            source_identity="test-calendar",
        )
        for index, value in enumerate(dates)
    )
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=f"instrument-{index}",
            symbol=f"T{index}",
            asset_class="ETF" if index == 0 else "COMMON_STOCK",
            exchange_mic="XNYS",
            currency="USD",
            timezone="America/New_York",
            valid_from=sessions[0].session_date,
            valid_to=None,
            source_identity="test-instruments",
        )
        for index in range(2)
    )
    bars = tuple(
        SecuritiesBarEvidence(
            instrument_id=instrument.instrument_id,
            interval="ONE_DAY",
            adjustment_state=SecuritiesAdjustmentState.RAW,
            period_start=session.opens_at,
            period_end=session.closes_at,
            available_at=session.closes_at + timedelta(minutes=1),
            open=str(100 + instrument_index + session_index),
            high=str(102 + instrument_index + session_index),
            low=str(99 + instrument_index + session_index),
            close=str(101 + instrument_index + session_index),
            volume=str(1_000 + session_index),
            source_identity="test-bars",
        )
        for instrument_index, instrument in enumerate(instruments)
        for session_index, session in enumerate(sessions)
    )
    actions = (
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[1].instrument_id,
            action_type="CASH_DIVIDEND",
            effective_date=sessions[4].session_date,
            available_at=sessions[3].closes_at + timedelta(minutes=1),
            cash_amount="0.25",
            source_identity="test-actions",
        ),
    )
    return instruments, sessions, bars, actions


def _managed_dataset(
    tmp_path: Path,
) -> tuple[
    LocalSecuritiesDatasetAdapter,
    str,
    tuple[SecuritiesInstrument, ...],
    tuple[SecuritiesCalendarSession, ...],
]:
    instruments, sessions, bars, actions = _evidence()
    adapter = LocalSecuritiesDatasetAdapter(tmp_path / "managed")
    source = adapter.write_generated_jsonl(
        relative_location="incoming/evidence.jsonl",
        records=(securities_record(item) for item in (*instruments, *sessions, *bars, *actions)),
    )
    return adapter, source, instruments, sessions


def test_exact_ohlcv_and_point_in_time_invariants_reject_bad_evidence() -> None:
    instruments, sessions, _bars, _actions = _evidence()
    with pytest.raises(SecuritiesError, match="high"):
        SecuritiesBarEvidence(
            instrument_id=instruments[0].instrument_id,
            interval="ONE_DAY",
            adjustment_state="RAW",
            period_start=sessions[0].opens_at,
            period_end=sessions[0].closes_at,
            available_at=sessions[0].closes_at,
            open="10",
            high="9",
            low="8",
            close="10",
            volume="1",
            source_identity="invalid",
        )
    with pytest.raises(SecuritiesError, match="available"):
        SecuritiesBarEvidence(
            instrument_id=instruments[0].instrument_id,
            interval="ONE_DAY",
            adjustment_state="RAW",
            period_start=sessions[0].opens_at,
            period_end=sessions[0].closes_at,
            available_at=sessions[0].opens_at,
            open="10",
            high="11",
            low="9",
            close="10",
            volume="1",
            source_identity="invalid",
        )
    with pytest.raises(ValueValidationError):
        SecuritiesBarEvidence(
            instrument_id=instruments[0].instrument_id,
            interval="ONE_DAY",
            adjustment_state="RAW",
            period_start=sessions[0].opens_at,
            period_end=sessions[0].closes_at,
            available_at=sessions[0].closes_at,
            open=10.0,  # type: ignore[arg-type]
            high="11",
            low="9",
            close="10",
            volume="1",
            source_identity="invalid",
        )


def test_dataset_rejects_duplicates_overlap_and_unknown_instruments() -> None:
    instruments, sessions, bars, actions = _evidence()
    assert validate_dataset_evidence(
        instruments=instruments,
        sessions=sessions,
        bars=bars,
        corporate_actions=actions,
    ) == ("SINGLE_ADJUSTMENT_STATE",)
    with pytest.raises(SecuritiesError, match="duplicate bar"):
        validate_dataset_evidence(
            instruments=instruments,
            sessions=sessions,
            bars=(*bars, bars[0]),
            corporate_actions=actions,
        )
    unknown = SecuritiesBarEvidence(
        instrument_id="unknown",
        interval="ONE_DAY",
        adjustment_state="RAW",
        period_start=sessions[0].opens_at,
        period_end=sessions[0].closes_at,
        available_at=sessions[0].closes_at,
        open="10",
        high="11",
        low="9",
        close="10",
        volume="1",
        source_identity="invalid",
    )
    with pytest.raises(SecuritiesError, match="unknown instrument"):
        validate_dataset_evidence(
            instruments=instruments,
            sessions=sessions,
            bars=(unknown,),
            corporate_actions=(),
        )


def test_managed_jsonl_is_canonical_immutable_and_hash_verified(tmp_path: Path) -> None:
    adapter, source, instruments, sessions = _managed_dataset(tmp_path)
    dataset = adapter.load_managed_source(managed_source=source, dataset_name="test data")
    assert len(dataset.instruments) == 2
    assert len(dataset.sessions) == 8
    assert dataset.source_sha256 == dataset.canonical_sha256
    batch = ImportSecuritiesData(
        repository=InMemorySecuritiesRepository(), market_data=adapter
    ).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="test data",
            idempotency_key="import-v1",
            imported_at=NOW,
        )
    )
    adapter.verify_batch_artifact(batch)
    assert batch.instrument_count == len(instruments)
    assert batch.session_count == len(sessions)
    canonical_path = adapter.root / batch.canonical_artifact
    canonical_path.write_bytes(b"changed\n")
    with pytest.raises(SecuritiesManagedDataError, match="hash"):
        adapter.verify_batch_artifact(batch)


def test_managed_root_rejects_escape_symlink_float_and_collision(
    tmp_path: Path,
) -> None:
    adapter = LocalSecuritiesDatasetAdapter(tmp_path / "managed")
    with pytest.raises(SecuritiesManagedDataError, match="safe relative"):
        adapter.load_managed_source(managed_source="../outside.jsonl", dataset_name="unsafe")
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"record_type":"bar","open":"1"}\n', encoding="utf-8")
    symlink = adapter.root / "linked.jsonl"
    symlink.symlink_to(outside)
    with pytest.raises(SecuritiesManagedDataError, match="escapes"):
        adapter.load_managed_source(managed_source="linked.jsonl", dataset_name="unsafe")
    float_source = adapter.root / "float.jsonl"
    float_source.write_text('{"record_type":"bar","open":1.2}\n', encoding="utf-8")
    with pytest.raises(SecuritiesManagedDataError, match="line 1"):
        adapter.load_managed_source(managed_source="float.jsonl", dataset_name="float")
    adapter.write_generated_jsonl(
        relative_location="immutable/data.jsonl",
        records=({"record_type": "instrument", "instrument_id": "one"},),
    )
    with pytest.raises(SecuritiesManagedDataError, match="different evidence"):
        adapter.write_generated_jsonl(
            relative_location="immutable/data.jsonl",
            records=({"record_type": "instrument", "instrument_id": "two"},),
        )


@pytest.mark.parametrize(
    ("kind", "expected_directions"),
    (
        (SecuritiesStrategyKind.CASH_BASELINE, {SecuritiesSignalDirection.FLAT}),
        (
            SecuritiesStrategyKind.BUY_AND_HOLD,
            {SecuritiesSignalDirection.LONG, SecuritiesSignalDirection.FLAT},
        ),
        (
            SecuritiesStrategyKind.TIME_SERIES_MOMENTUM,
            {SecuritiesSignalDirection.LONG, SecuritiesSignalDirection.FLAT},
        ),
    ),
)
def test_transparent_baselines_are_deterministic_and_terminally_bounded(
    kind: SecuritiesStrategyKind,
    expected_directions: set[SecuritiesSignalDirection],
) -> None:
    instruments, sessions, bars, _actions = _evidence()
    specification = SecuritiesStrategySpecification.create(
        strategy_id=f"test-{kind.value}",
        kind=kind,
        version="1",
        lookback_sessions=2,
        rebalance_every_sessions=1,
        created_at=NOW,
    )
    request = {
        "research_run_id": "run",
        "specification": specification,
        "bars": bars,
        "universe": tuple(item.instrument_id for item in instruments),
        "start_date": sessions[0].session_date,
        "end_date": sessions[-1].session_date,
        "decision_cutoff": sessions[-1].closes_at + timedelta(hours=1),
    }
    first = build_baseline_signals(**request)
    second = build_baseline_signals(**request)
    assert first == second
    assert {signal.direction for signal in first} == expected_directions
    assert all(abs(signal.target_weight) <= Decimal(1) for signal in first)


def test_cross_sectional_momentum_is_equal_weight_and_top_quantile_only() -> None:
    instruments, sessions, bars, _actions = _evidence()
    specification = SecuritiesStrategySpecification.create(
        strategy_id="cross-sectional",
        kind="CROSS_SECTIONAL_MOMENTUM",
        version="1",
        lookback_sessions=2,
        rebalance_every_sessions=1,
        top_quantile="0.5",
        created_at=NOW,
    )
    signals = build_baseline_signals(
        research_run_id="run",
        specification=specification,
        bars=bars,
        universe=tuple(item.instrument_id for item in instruments),
        start_date=sessions[0].session_date,
        end_date=sessions[-1].session_date,
        decision_cutoff=sessions[-1].closes_at + timedelta(hours=1),
    )
    for signal_at in {signal.signal_at for signal in signals}:
        selected = [
            signal
            for signal in signals
            if signal.signal_at == signal_at and signal.target_weight > 0
        ]
        assert len(selected) == 1
        assert selected[0].target_weight == Decimal(1)


def test_research_service_persists_metrics_without_financial_authority(
    tmp_path: Path,
) -> None:
    adapter, source, instruments, sessions = _managed_dataset(tmp_path)
    repository = InMemorySecuritiesRepository()
    batch = ImportSecuritiesData(repository=repository, market_data=adapter).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="service data",
            idempotency_key="service-import",
            imported_at=NOW,
        )
    )
    specification = SecuritiesStrategySpecification.create(
        strategy_id="service-momentum",
        kind="TIME_SERIES_MOMENTUM",
        version="1",
        lookback_sessions=2,
        rebalance_every_sessions=1,
        created_at=NOW,
    )
    repository.save_specification(specification)
    service = RunSecuritiesResearch(
        repository=repository,
        campaigns=InMemorySecuritiesCampaignBoundary(allowed_campaign_ids=("campaign-research",)),
        market_data=adapter,
    )
    command = RunSecuritiesResearchCommand(
        campaign_id="campaign-research",
        strategy_id=specification.strategy_id,
        data_batch_id=batch.batch_id,
        universe=tuple(item.instrument_id for item in instruments),
        start_date=sessions[0].session_date,
        end_date=sessions[-1].session_date,
        decision_cutoff=sessions[-1].closes_at + timedelta(hours=1),
        benchmark_instrument_id=instruments[0].instrument_id,
        idempotency_key="research-v1",
    )
    run = service.execute(command)
    assert run.warning_banner == SECURITIES_SHADOW_WARNING
    assert run.metrics.signal_count == len(repository.list_signals(run.research_run_id))
    assert run.metrics.short_count == 0
    assert service.execute(command) == run
    with pytest.raises(SecuritiesIdempotencyConflictError):
        service.execute(replace(command, end_date=sessions[-2].session_date))
    assert not any(
        name in repository.__dict__ for name in ("orders", "positions", "reservations", "journals")
    )


def test_research_rejects_missing_campaign_unknown_universe_and_post_cutoff(
    tmp_path: Path,
) -> None:
    adapter, source, instruments, sessions = _managed_dataset(tmp_path)
    repository = InMemorySecuritiesRepository()
    batch = ImportSecuritiesData(repository=repository, market_data=adapter).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="reject data",
            idempotency_key="reject-import",
            imported_at=NOW,
        )
    )
    specification = SecuritiesStrategySpecification.create(
        strategy_id="reject-cash",
        kind="CASH_BASELINE",
        version="1",
        created_at=NOW,
    )
    repository.save_specification(specification)
    service = RunSecuritiesResearch(
        repository=repository,
        campaigns=InMemorySecuritiesCampaignBoundary(allowed_campaign_ids=()),
        market_data=adapter,
    )
    with pytest.raises(SecuritiesApplicationError, match="campaign"):
        service.execute(
            RunSecuritiesResearchCommand(
                campaign_id="missing",
                strategy_id=specification.strategy_id,
                data_batch_id=batch.batch_id,
                universe=(instruments[0].instrument_id,),
                start_date=sessions[0].session_date,
                end_date=sessions[-1].session_date,
                decision_cutoff=sessions[-1].closes_at,
                benchmark_instrument_id=instruments[0].instrument_id,
                idempotency_key="missing",
            )
        )


def test_conformance_classifies_exact_and_bounded_difference(tmp_path: Path) -> None:
    adapter, source, instruments, sessions = _managed_dataset(tmp_path)
    repository = InMemorySecuritiesRepository()
    batch = ImportSecuritiesData(repository=repository, market_data=adapter).execute(
        ImportSecuritiesDataCommand(
            managed_source=source,
            dataset_name="conformance data",
            idempotency_key="conformance-import",
            imported_at=NOW,
        )
    )
    specification = SecuritiesStrategySpecification.create(
        strategy_id="conformance-hold",
        kind="BUY_AND_HOLD",
        version="1",
        created_at=NOW,
    )
    repository.save_specification(specification)
    research = RunSecuritiesResearch(
        repository=repository,
        campaigns=InMemorySecuritiesCampaignBoundary(allowed_campaign_ids=("campaign",)),
        market_data=adapter,
    ).execute(
        RunSecuritiesResearchCommand(
            campaign_id="campaign",
            strategy_id=specification.strategy_id,
            data_batch_id=batch.batch_id,
            universe=tuple(item.instrument_id for item in instruments),
            start_date=sessions[0].session_date,
            end_date=sessions[-1].session_date,
            decision_cutoff=sessions[-1].closes_at + timedelta(hours=1),
            benchmark_instrument_id=instruments[0].instrument_id,
            idempotency_key="conformance-research",
        )
    )
    exact = DeterministicLeanFixtureAdapter(engine_identity="EXACT")
    explained = DeterministicLeanFixtureAdapter(
        engine_identity="EXPLAINED", explained_difference=True
    )
    service = RunSecuritiesConformance(repository=repository, engines=(exact, explained))
    exact_report = service.execute(
        RunSecuritiesConformanceCommand(
            research_run_id=research.research_run_id,
            external_engine="EXACT",
            created_at=NOW,
        )
    )
    explained_report = service.execute(
        RunSecuritiesConformanceCommand(
            research_run_id=research.research_run_id,
            external_engine="EXPLAINED",
            created_at=NOW + timedelta(seconds=1),
        )
    )
    assert exact_report.classification is SecuritiesConformanceClassification.CONFORMANT
    assert (
        explained_report.classification is SecuritiesConformanceClassification.EXPLAINED_DIFFERENCE
    )


def test_retained_calendar_answers_membership_and_neighbors() -> None:
    _instruments, sessions, _bars, _actions = _evidence()
    calendar = RetainedSecuritiesCalendar(sessions)
    assert calendar.is_session(calendar_identity="XNYS", session_date=sessions[2].session_date)
    assert (
        calendar.previous_session(calendar_identity="XNYS", session_date=sessions[2].session_date)
        == sessions[1]
    )
    assert (
        calendar.next_session(calendar_identity="XNYS", session_date=sessions[2].session_date)
        == sessions[3]
    )
