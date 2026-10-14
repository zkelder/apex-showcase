"""Deterministic synthetic Securities Stage 1 demonstration evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from apex.adapters.securities_data import (
    LocalSecuritiesDatasetAdapter,
    securities_record,
)
from apex.adapters.securities_external import (
    DeterministicLeanFixtureAdapter,
    LeanSecuritiesEngineAdapter,
)
from apex.adapters.securities_strategy_lab import (
    SECURITIES_STRATEGY_LAB_FEATURE_NAMES,
    SECURITIES_STRATEGY_LAB_SCHEMA_HASH,
    StrategyLabSecuritiesSignalBuilder,
)
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    InMemorySecuritiesCampaignBoundary,
    InMemorySecuritiesRepository,
    RunSecuritiesConformance,
    RunSecuritiesConformanceCommand,
    RunSecuritiesResearch,
    RunSecuritiesResearchCommand,
    SecuritiesIdempotencyConflictError,
)
from apex.application.strategy_lab import ModelFamily, runtime_identity
from apex.application.strategy_lab_shadow import (
    DeploymentRole,
    DeploymentState,
    StrategyLabModelDeployment,
)
from apex.domain.securities import (
    SECURITIES_SHADOW_WARNING,
    SecuritiesAdjustmentState,
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesConformanceClassification,
    SecuritiesCorporateActionEvidence,
    SecuritiesCorporateActionType,
    SecuritiesError,
    SecuritiesInstrument,
    SecuritiesStrategyKind,
    SecuritiesStrategySpecification,
    validate_dataset_evidence,
)
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id

DEMO_CAMPAIGN_ID = "campaign-securities-shadow-demo"


def run_securities_shadow_demo(
    *,
    data_root: Path | None = None,
    artifact_root: Path | None = None,
) -> dict[str, object]:
    """Run the Stage 1 services from generated evidence, never from canned totals."""

    dataset_adapter = LocalSecuritiesDatasetAdapter(_as_path(data_root))
    artifacts = LocalStrategyLabArtifactStore(_as_path(artifact_root))
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    records = tuple(securities_record(item) for item in (*instruments, *sessions, *bars, *actions))
    source_hash = content_digest(records)
    managed_source = dataset_adapter.write_generated_jsonl(
        relative_location=f"generated/securities-stage1-{source_hash}.jsonl",
        records=records,
    )
    repository = InMemorySecuritiesRepository()
    importer = ImportSecuritiesData(repository=repository, market_data=dataset_adapter)
    imported_at = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    import_command = ImportSecuritiesDataCommand(
        managed_source=managed_source,
        dataset_name="Apex deterministic 12-instrument Stage 1 evidence",
        idempotency_key="securities-demo-import-v1",
        imported_at=imported_at,
    )
    batch = importer.execute(import_command)
    replayed_batch = importer.execute(import_command)
    import_conflict_rejected = False
    try:
        importer.execute(
            ImportSecuritiesDataCommand(
                managed_source=managed_source,
                dataset_name="conflicting dataset identity",
                idempotency_key=import_command.idempotency_key,
                imported_at=imported_at,
            )
        )
    except SecuritiesIdempotencyConflictError:
        import_conflict_rejected = True

    created_at = datetime(2026, 7, 26, 12, 1, tzinfo=UTC)
    specifications = (
        SecuritiesStrategySpecification.create(
            strategy_id="securities-cash-baseline-v1",
            kind=SecuritiesStrategyKind.CASH_BASELINE,
            version="1",
            created_at=created_at,
        ),
        SecuritiesStrategySpecification.create(
            strategy_id="securities-buy-hold-v1",
            kind=SecuritiesStrategyKind.BUY_AND_HOLD,
            version="1",
            created_at=created_at,
        ),
        SecuritiesStrategySpecification.create(
            strategy_id="securities-time-series-momentum-v1",
            kind=SecuritiesStrategyKind.TIME_SERIES_MOMENTUM,
            version="1",
            lookback_sessions=20,
            rebalance_every_sessions=5,
            created_at=created_at,
        ),
        SecuritiesStrategySpecification.create(
            strategy_id="securities-cross-sectional-momentum-v1",
            kind=SecuritiesStrategyKind.CROSS_SECTIONAL_MOMENTUM,
            version="1",
            lookback_sessions=20,
            rebalance_every_sessions=5,
            top_quantile="0.25",
            created_at=created_at,
        ),
    )
    for specification in specifications:
        repository.save_specification(specification)

    deployment, model_builder = _strategy_lab_fixture_deployment(
        artifacts=artifacts, created_at=created_at
    )
    model_specification = SecuritiesStrategySpecification.create(
        strategy_id="securities-strategy-lab-model-v1",
        kind=SecuritiesStrategyKind.STRATEGY_LAB_MODEL,
        version="1",
        lookback_sessions=20,
        rebalance_every_sessions=1,
        strategy_lab_deployment_id=deployment.deployment_id,
        created_at=created_at,
    )
    repository.save_specification(model_specification)

    boundary = InMemorySecuritiesCampaignBoundary(allowed_campaign_ids=(DEMO_CAMPAIGN_ID,))
    baseline_runner = RunSecuritiesResearch(
        repository=repository,
        campaigns=boundary,
        market_data=dataset_adapter,
    )
    model_runner = RunSecuritiesResearch(
        repository=repository,
        campaigns=boundary,
        market_data=dataset_adapter,
        model_signal_builder=model_builder,
    )
    universe = tuple(instrument.instrument_id for instrument in instruments)
    start_date = sessions[0].session_date
    end_date = sessions[-1].session_date
    cutoff = sessions[-1].closes_at + timedelta(days=1)
    runs = []
    for specification in specifications:
        runs.append(
            baseline_runner.execute(
                RunSecuritiesResearchCommand(
                    campaign_id=DEMO_CAMPAIGN_ID,
                    strategy_id=specification.strategy_id,
                    data_batch_id=batch.batch_id,
                    universe=universe,
                    start_date=start_date,
                    end_date=end_date,
                    decision_cutoff=cutoff,
                    benchmark_instrument_id=universe[0],
                    idempotency_key=f"securities-demo-{specification.kind.value.lower()}",
                )
            )
        )
    model_run = model_runner.execute(
        RunSecuritiesResearchCommand(
            campaign_id=DEMO_CAMPAIGN_ID,
            strategy_id=model_specification.strategy_id,
            data_batch_id=batch.batch_id,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            decision_cutoff=cutoff,
            benchmark_instrument_id=universe[0],
            idempotency_key="securities-demo-strategy-lab",
        )
    )
    runs.append(model_run)
    research_replay = model_runner.execute(
        RunSecuritiesResearchCommand(
            campaign_id=DEMO_CAMPAIGN_ID,
            strategy_id=model_specification.strategy_id,
            data_batch_id=batch.batch_id,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            decision_cutoff=cutoff,
            benchmark_instrument_id=universe[0],
            idempotency_key="securities-demo-strategy-lab",
        )
    )
    research_conflict_rejected = False
    try:
        model_runner.execute(
            RunSecuritiesResearchCommand(
                campaign_id=DEMO_CAMPAIGN_ID,
                strategy_id=model_specification.strategy_id,
                data_batch_id=batch.batch_id,
                universe=universe,
                start_date=start_date,
                end_date=sessions[-2].session_date,
                decision_cutoff=cutoff,
                benchmark_instrument_id=universe[0],
                idempotency_key="securities-demo-strategy-lab",
            )
        )
    except SecuritiesIdempotencyConflictError:
        research_conflict_rejected = True

    exact_engine = DeterministicLeanFixtureAdapter(engine_identity="LEAN_FIXTURE_EXACT")
    explained_engine = DeterministicLeanFixtureAdapter(
        explained_difference=True,
        engine_identity="LEAN_FIXTURE_EXPLAINED",
    )
    conformance = RunSecuritiesConformance(
        repository=repository,
        engines=(exact_engine, explained_engine),
    )
    exact_report = conformance.execute(
        RunSecuritiesConformanceCommand(
            research_run_id=model_run.research_run_id,
            external_engine=exact_engine.engine_identity,
            created_at=cutoff + timedelta(minutes=1),
        )
    )
    explained_report = conformance.execute(
        RunSecuritiesConformanceCommand(
            research_run_id=model_run.research_run_id,
            external_engine=explained_engine.engine_identity,
            created_at=cutoff + timedelta(minutes=2),
        )
    )
    rejection_codes = _exercise_rejections(
        instruments=instruments,
        sessions=sessions,
        bars=bars,
        actions=actions,
    )
    lean_available, lean_version = LeanSecuritiesEngineAdapter().availability()
    return {
        "warning": SECURITIES_SHADOW_WARNING,
        "dataset": {
            "batch_id": batch.batch_id,
            "instrument_count": batch.instrument_count,
            "session_count": batch.session_count,
            "bar_count": batch.bar_count,
            "corporate_action_count": batch.corporate_action_count,
            "adjustment_states": sorted({bar.adjustment_state.value for bar in bars}),
            "early_close_count": sum(session.is_early_close for session in sessions),
            "corporate_action_types": sorted({action.action_type.value for action in actions}),
            "source_sha256": batch.source_sha256,
            "canonical_sha256": batch.canonical_sha256,
        },
        "strategy_lab_deployment": {
            "deployment_id": deployment.deployment_id,
            "state": deployment.state.value,
            "feature_schema_hash": deployment.feature_schema_hash,
            "artifact_sha256": deployment.artifact.sha256,
        },
        "research_runs": [
            {
                "research_run_id": run.research_run_id,
                "strategy_id": repository.get_specification(run.specification_id).strategy_id,
                "state": run.state.value,
                "signal_count": run.metrics.signal_count,
                "benchmark_return": run.metrics.benchmark_return,
                "hypothetical_gross_return": run.metrics.hypothetical_gross_return,
                "hypothetical_cost": run.metrics.hypothetical_cost,
                "hypothetical_net_return": run.metrics.hypothetical_net_return,
                "maximum_drawdown": run.metrics.maximum_drawdown,
                "turnover": run.metrics.turnover,
                "hit_rate": run.metrics.hit_rate,
                "average_gross_exposure": run.metrics.average_gross_exposure,
            }
            for run in runs
        ],
        "rejections_proved": rejection_codes,
        "conformance": {
            "exact": exact_report.classification.value,
            "explained": explained_report.classification.value,
            "actual_lean_available": lean_available,
            "actual_lean_version": lean_version,
            "actual_lean_executed": False,
        },
        "idempotency": {
            "import_replay_same_batch": replayed_batch.batch_id == batch.batch_id,
            "import_conflict_rejected": import_conflict_rejected,
            "research_replay_same_run": (
                research_replay.research_run_id == model_run.research_run_id
            ),
            "research_conflict_rejected": research_conflict_rejected,
        },
        "authority_boundary": {
            "provider_calls": 0,
            "broker_calls": 0,
            "proposals": 0,
            "allocations": 0,
            "risk_reservations": 0,
            "orders": 0,
            "positions": 0,
            "settlements": 0,
            "journals": 0,
            "capital_reserved": "0",
        },
        "conformance_expectations_met": (
            exact_report.classification is SecuritiesConformanceClassification.CONFORMANT
            and explained_report.classification
            is SecuritiesConformanceClassification.EXPLAINED_DIFFERENCE
        ),
    }


def synthetic_securities_dataset() -> tuple[
    tuple[SecuritiesInstrument, ...],
    tuple[SecuritiesCalendarSession, ...],
    tuple[SecuritiesBarEvidence, ...],
    tuple[SecuritiesCorporateActionEvidence, ...],
]:
    session_dates = _business_dates(date(2024, 1, 2), count=500)
    sessions = tuple(
        SecuritiesCalendarSession.create(
            calendar_identity="XNYS",
            calendar_version="apex-synthetic-xnys-v1",
            timezone="America/New_York",
            session_date=session_date.isoformat(),
            opens_at=datetime.combine(session_date, time(14, 30), tzinfo=UTC),
            closes_at=datetime.combine(
                session_date,
                time(18, 0) if index == 250 else time(21, 0),
                tzinfo=UTC,
            ),
            is_early_close=index == 250,
            source_identity="apex-synthetic-calendar-v1",
        )
        for index, session_date in enumerate(session_dates)
    )
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=f"securities-instrument-{index:02d}",
            symbol=f"APX{index:02d}",
            asset_class=(
                SecuritiesAssetClass.ETF if index in {0, 11} else SecuritiesAssetClass.COMMON_STOCK
            ),
            exchange_mic="XNYS",
            currency="USD",
            timezone="America/New_York",
            valid_from=sessions[0].session_date,
            valid_to=sessions[-1].session_date if index == 4 else None,
            source_identity="apex-synthetic-instruments-v1",
        )
        for index in range(12)
    )
    bars: list[SecuritiesBarEvidence] = []
    for instrument_index, instrument in enumerate(instruments):
        base = Decimal(40 + instrument_index * 5)
        for session_index, session in enumerate(sessions):
            drift = Decimal(session_index) * Decimal("0.01")
            cycle = Decimal((session_index + instrument_index) % 11 - 5) * Decimal("0.03")
            raw_close = base + drift + cycle
            for adjustment_state in SecuritiesAdjustmentState:
                multiplier = {
                    SecuritiesAdjustmentState.RAW: Decimal(1),
                    SecuritiesAdjustmentState.SPLIT_ADJUSTED: (
                        Decimal("0.5")
                        if instrument_index == 1 and session_index < 200
                        else Decimal(1)
                    ),
                    SecuritiesAdjustmentState.TOTAL_RETURN_ADJUSTED: (
                        Decimal("1.002")
                        if instrument_index == 2 and session_index >= 300
                        else Decimal(1)
                    ),
                }[adjustment_state]
                close = (raw_close * multiplier).quantize(Decimal("0.0001"))
                open_value = (close - Decimal("0.05")).quantize(Decimal("0.0001"))
                high = (close + Decimal("0.10")).quantize(Decimal("0.0001"))
                low = (open_value - Decimal("0.10")).quantize(Decimal("0.0001"))
                bars.append(
                    SecuritiesBarEvidence(
                        instrument_id=instrument.instrument_id,
                        interval="ONE_DAY",
                        adjustment_state=adjustment_state,
                        period_start=session.opens_at,
                        period_end=session.closes_at,
                        available_at=session.closes_at + timedelta(minutes=1),
                        open=open_value,
                        high=high,
                        low=low,
                        close=close,
                        volume=str(1_000_000 + instrument_index * 10_000 + session_index),
                        source_identity="apex-synthetic-bars-v1",
                    )
                )
    actions = (
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[1].instrument_id,
            action_type=SecuritiesCorporateActionType.SPLIT,
            effective_date=sessions[200].session_date,
            available_at=sessions[199].closes_at + timedelta(minutes=5),
            split_ratio="2",
            source_identity="apex-synthetic-actions-v1",
        ),
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[2].instrument_id,
            action_type=SecuritiesCorporateActionType.CASH_DIVIDEND,
            effective_date=sessions[300].session_date,
            available_at=sessions[295].closes_at + timedelta(minutes=5),
            cash_amount="0.25",
            source_identity="apex-synthetic-actions-v1",
        ),
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[3].instrument_id,
            action_type=SecuritiesCorporateActionType.SYMBOL_CHANGE,
            effective_date=sessions[350].session_date,
            available_at=sessions[345].closes_at + timedelta(minutes=5),
            successor_symbol="APX3N",
            source_identity="apex-synthetic-actions-v1",
        ),
        SecuritiesCorporateActionEvidence(
            instrument_id=instruments[4].instrument_id,
            action_type=SecuritiesCorporateActionType.DELISTING,
            effective_date=sessions[-1].session_date,
            available_at=sessions[-2].closes_at + timedelta(minutes=5),
            source_identity="apex-synthetic-actions-v1",
        ),
    )
    validate_dataset_evidence(
        instruments=instruments,
        sessions=sessions,
        bars=bars,
        corporate_actions=actions,
    )
    return instruments, sessions, tuple(bars), actions


def _strategy_lab_fixture_deployment(
    *,
    artifacts: LocalStrategyLabArtifactStore,
    created_at: datetime,
) -> tuple[StrategyLabModelDeployment, StrategyLabSecuritiesSignalBuilder]:
    artifact_payload = canonical_json_bytes(
        {
            "artifact_type": "APEX_STRATEGY_LAB_MODEL_V1",
            "model_family": ModelFamily.LOGISTIC_REGRESSION.value,
            "feature_names": SECURITIES_STRATEGY_LAB_FEATURE_NAMES,
            "calibration": "none",
            "runtime": runtime_identity(),
            "training_warnings": (),
            "coefficients": ("3", "2", "0.05", "0.1"),
            "intercept": "-0.05",
            "classes": (0, 1),
            "calibration_parameters": None,
        }
    )
    artifact_hash = content_digest(
        {
            "feature_schema_hash": SECURITIES_STRATEGY_LAB_SCHEMA_HASH,
            "runtime": runtime_identity(),
        }
    )
    artifact = artifacts.write_bytes(f"securities/{artifact_hash}.json", artifact_payload)
    deployment_id = deterministic_id(
        "strategy_lab_deployment",
        {
            "feature_schema_hash": SECURITIES_STRATEGY_LAB_SCHEMA_HASH,
            "artifact_sha256": artifact.sha256,
        },
    )
    canonical_payload: dict[str, object] = {
        "feature_names": SECURITIES_STRATEGY_LAB_FEATURE_NAMES,
        "vertical": "SECURITIES",
        "authority": "RESEARCH_ONLY",
    }
    deployment = StrategyLabModelDeployment(
        deployment_id=deployment_id,
        review_id="synthetic-securities-review",
        candidate_id="synthetic-securities-candidate",
        role=DeploymentRole.CHALLENGER,
        vertical="SECURITIES",
        research_family="CONTINUOUS_MARKET_STAGE1",
        model_family=ModelFamily.LOGISTIC_REGRESSION,
        feature_schema_id="securities-forward-v1",
        feature_schema_hash=SECURITIES_STRATEGY_LAB_SCHEMA_HASH,
        label_definition_id="next-bar-positive-return-v1",
        label_definition_hash=content_digest({"label": "NEXT_BAR_POSITIVE_RETURN"}),
        dataset_id="synthetic-securities-strategy-lab-dataset",
        dataset_hash=content_digest({"dataset": "synthetic-securities-strategy-lab"}),
        training_run_id="synthetic-securities-training",
        training_hash=content_digest({"training": "synthetic-securities"}),
        evaluation_id="synthetic-securities-evaluation",
        evaluation_hash=content_digest({"evaluation": "synthetic-securities"}),
        artifact=artifact,
        calibration_identity="none",
        runtime_identity=content_digest(runtime_identity()),
        state=DeploymentState.ACTIVE,
        created_at=created_at,
        activated_at=created_at,
        paused_at=None,
        retired_at=None,
        invalidated_at=None,
        operator_action_id="synthetic-securities-approval",
        canonical_payload=canonical_payload,
        canonical_hash=content_digest(canonical_payload),
        last_integrity_check_at=created_at,
    )
    return (
        deployment,
        StrategyLabSecuritiesSignalBuilder(
            deployment=deployment,
            artifact_store=artifacts,
            model_loader=StrategyLabManagedModelLoader(),
        ),
    )


def _exercise_rejections(
    *,
    instruments: tuple[SecuritiesInstrument, ...],
    sessions: tuple[SecuritiesCalendarSession, ...],
    bars: tuple[SecuritiesBarEvidence, ...],
    actions: tuple[SecuritiesCorporateActionEvidence, ...],
) -> tuple[str, ...]:
    rejected: list[str] = []
    try:
        SecuritiesBarEvidence(
            instrument_id=instruments[0].instrument_id,
            interval="ONE_DAY",
            adjustment_state="RAW",
            period_start=sessions[0].opens_at,
            period_end=sessions[0].closes_at,
            available_at=sessions[0].closes_at + timedelta(minutes=1),
            open="10",
            high="9",
            low="8",
            close="10",
            volume="1",
            source_identity="invalid-ohlc",
        )
    except SecuritiesError:
        rejected.append("INVALID_OHLC_REJECTED")
    try:
        validate_dataset_evidence(
            instruments=instruments,
            sessions=sessions,
            bars=(bars[0], bars[0]),
            corporate_actions=(),
        )
    except SecuritiesError:
        rejected.append("DUPLICATE_BAR_REJECTED")
    try:
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
            source_identity="invalid-lookahead",
        )
    except SecuritiesError:
        rejected.append("NO_LOOKAHEAD_REJECTED")
    after_delisting = SecuritiesCorporateActionEvidence(
        instrument_id=instruments[4].instrument_id,
        action_type=SecuritiesCorporateActionType.CASH_DIVIDEND,
        effective_date=(
            date.fromisoformat(sessions[-1].session_date) + timedelta(days=1)
        ).isoformat(),
        available_at=sessions[-1].closes_at + timedelta(days=1),
        cash_amount="0.1",
        source_identity="invalid-symbol-lifecycle",
    )
    try:
        validate_dataset_evidence(
            instruments=instruments,
            sessions=sessions,
            bars=bars,
            corporate_actions=(*actions, after_delisting),
        )
    except SecuritiesError:
        rejected.append("SYMBOL_LIFECYCLE_REJECTED")
    return tuple(rejected)


def _business_dates(start: date, *, count: int) -> tuple[date, ...]:
    values: list[date] = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return tuple(values)


def _as_path(value: Path | None) -> Path | None:
    return value
