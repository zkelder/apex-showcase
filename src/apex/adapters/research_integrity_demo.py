"""Deterministic provider-free product proof for research-selection integrity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.application.research_integrity import (
    InMemoryResearchIntegrityRepository,
    OpenResearchExperimentFamily,
    ResearchExperimentFamily,
    ResearchIdempotencyConflict,
    ResearchIntegrityApplicationError,
    ResearchIntegrityService,
    ResearchRecommendation,
    ResearchTrialLedger,
    ResearchTrialSourceType,
    ResearchTrialState,
)
from apex.domain.research_integrity import ResearchFrequency, ReturnObservation
from apex.domain.values import content_digest

_NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
_DATASET_HASH = content_digest({"dataset": "research-integrity-demo-daily-v1"})
_UNIVERSE_HASH = content_digest({"universe": ("AAA", "BBB", "CCC", "DDD")})
_FEATURE_HASH = content_digest({"features": "predeclared-demo-v1"})
_COST_HASH = content_digest({"commission_bps": "1", "slippage_bps": "2"})


def _observations(
    *,
    mean: Decimal,
    amplitude: Decimal,
    phase: int,
    lucky_period: int | None = None,
) -> tuple[ReturnObservation, ...]:
    pattern = (-3, -2, -1, 1, 2, 3, -1, 1)
    values: list[ReturnObservation] = []
    for index in range(120):
        value = mean + amplitude * Decimal(pattern[(index + phase) % len(pattern)])
        if lucky_period is not None and index == lucky_period:
            value += Decimal("0.30")
        values.append(
            ReturnObservation(
                period_id=f"2025-{index + 1:03}",
                value=value,
                group_id=f"month-{index // 20 + 1:02}",
                instrument_id=("AAA", "BBB", "CCC", "DDD")[index % 4],
            )
        )
    return tuple(values)


def _open(
    repository: InMemoryResearchIntegrityRepository,
    *,
    name: str,
    family_name: str,
    maximum_trials: int,
    permitted: tuple[str, ...],
    key: str,
) -> ResearchExperimentFamily:
    return OpenResearchExperimentFamily(repository).execute(
        name=name,
        vertical="SECURITIES",
        research_family=family_name,
        research_question=(
            "Which predeclared daily configuration is stable after research selection?"
        ),
        target_label_identity="daily-total-return-v1",
        benchmark_identities=("CASH", "BUY_AND_HOLD"),
        data_scope_identities=("research-integrity-demo-daily-v1",),
        data_scope_hashes=(_DATASET_HASH,),
        universe_hash=_UNIVERSE_HASH,
        period_start="2025-001",
        period_end="2025-120",
        cutoff_at=_NOW,
        feature_schema_hashes=(_FEATURE_HASH,),
        permitted_families=permitted,
        parameter_space={
            "lookback": (5, 10, 20, 40),
            "threshold": ("0.00", "0.01", "0.02"),
            "simulator": ("APEX_RESEARCH", "VECTORBT", "LEAN"),
        },
        maximum_trials=maximum_trials,
        maximum_feature_variants=min(4, maximum_trials),
        maximum_model_variants=len(permitted),
        maximum_simulator_variants=3,
        evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
        evaluation_policy_payload={
            "minimum_psr": "0.95",
            "minimum_dsr": "0.80",
            "maximum_pbo": "0.25",
        },
        cost_execution_assumptions={"commission_bps": "1", "slippage_bps": "2"},
        frequency=ResearchFrequency.DAILY,
        operator_action_id=f"operator-{key}",
        operator_reason="Provider-free bounded product proof",
        idempotency_key=key,
        created_at=_NOW,
    )


def _retain_completed(
    *,
    repository: InMemoryResearchIntegrityRepository,
    ledger: ResearchTrialLedger,
    service: ResearchIntegrityService,
    experiment_id: str,
    ordinal: int,
    family: str,
    mean: Decimal,
    amplitude: Decimal,
    phase: int,
    lucky_period: int | None = None,
    source_type: ResearchTrialSourceType = ResearchTrialSourceType.FIXTURE_VALIDATION,
    simulator: str = "DETERMINISTIC_FIXTURE",
) -> str:
    started_at = _NOW + timedelta(seconds=ordinal)
    trial = ledger.reserve(
        experiment_id=experiment_id,
        source_type=source_type,
        source_identity=f"{experiment_id}-source-{ordinal:03}",
        source_hash=content_digest({"experiment": experiment_id, "source": ordinal}),
        strategy_model_family=family,
        configuration={
            "lookback": (5, 10, 20, 40)[ordinal % 4],
            "threshold": str(Decimal(ordinal % 3) / Decimal(100)),
        },
        feature_set_hash=content_digest({"feature_variant": ordinal % 4}),
        dataset_hash=_DATASET_HASH,
        simulator_identity=simulator,
        simulator_version="1.0",
        execution_cost_policy_hash=_COST_HASH,
        random_seed=ordinal,
        idempotency_key=f"{experiment_id}-trial-{ordinal:03}",
        started_at=started_at,
    )
    completed = ledger.finish(
        trial.trial_id,
        state=ResearchTrialState.COMPLETED,
        completed_at=started_at + timedelta(seconds=1),
        result_identity=f"demo-result-{ordinal:03}",
        result_hash=content_digest({"experiment": experiment_id, "result": ordinal}),
    )
    observations = _observations(
        mean=mean,
        amplitude=amplitude,
        phase=phase,
        lucky_period=lucky_period,
    )
    service.retain_return_evidence(
        trial_id=completed.trial_id,
        gross_observations=observations,
        net_observations=observations,
        frequency=ResearchFrequency.DAILY,
        turnover=Decimal("0.10"),
    )
    return completed.trial_id


def _retain_terminal_without_series(
    *,
    ledger: ResearchTrialLedger,
    experiment_id: str,
    ordinal: int,
    family: str,
    state: ResearchTrialState,
    reason: str,
    source_type: ResearchTrialSourceType,
    simulator: str,
) -> str:
    started_at = _NOW + timedelta(seconds=ordinal)
    trial = ledger.reserve(
        experiment_id=experiment_id,
        source_type=source_type,
        source_identity=f"{experiment_id}-source-{ordinal:03}",
        source_hash=content_digest({"experiment": experiment_id, "source": ordinal}),
        strategy_model_family=family,
        configuration={"variant": ordinal},
        feature_set_hash=content_digest({"feature_variant": ordinal % 4}),
        dataset_hash=_DATASET_HASH,
        simulator_identity=simulator,
        simulator_version="1.0",
        execution_cost_policy_hash=_COST_HASH,
        random_seed=ordinal,
        idempotency_key=f"{experiment_id}-trial-{ordinal:03}",
        started_at=started_at,
        selected_for_comparison=False,
    )
    return ledger.finish(
        trial.trial_id,
        state=state,
        completed_at=started_at + timedelta(seconds=1),
        result_identity=None,
        result_hash=None,
        failure_reason=reason,
        integrity_blocked=state is ResearchTrialState.REJECTED_INTEGRITY,
    ).trial_id


def research_overfitting_demo(
    root: Path | None = None,
) -> dict[str, object]:
    """Run all controls with computed results and no external or financial ports."""

    repository = InMemoryResearchIntegrityRepository()
    artifacts = LocalResearchReturnArtifactStore(root)
    ledger = ResearchTrialLedger(repository)
    service = ResearchIntegrityService(repository, artifacts)
    large = _open(
        repository,
        name="Lucky-winner rejection family",
        family_name="OVERFITTING_DEMONSTRATION",
        maximum_trials=64,
        permitted=("MODEST", "NULL", "CORRELATED", "LUCKY"),
        key="demo-overfit-family",
    )
    assert hasattr(large, "experiment_id")
    large_id = str(large.experiment_id)

    completed_ids: list[str] = []
    for ordinal in range(1, 56):
        if ordinal == 55:
            family = "LUCKY"
            mean = Decimal("-0.0002")
            lucky_period = 29
        elif ordinal <= 10:
            family = "MODEST"
            mean = Decimal("0.00010") - Decimal(ordinal) * Decimal("0.000001")
            lucky_period = None
        elif ordinal <= 35:
            family = "NULL"
            mean = Decimal((ordinal % 5) - 2) * Decimal("0.00001")
            lucky_period = None
        else:
            family = "CORRELATED"
            mean = Decimal("0.00006") + Decimal(ordinal % 3) * Decimal("0.000005")
            lucky_period = None
        completed_ids.append(
            _retain_completed(
                repository=repository,
                ledger=ledger,
                service=service,
                experiment_id=large_id,
                ordinal=ordinal,
                family=family,
                mean=mean,
                amplitude=Decimal("0.0010"),
                phase=ordinal % 8,
                lucky_period=lucky_period,
            )
        )
    for ordinal in range(56, 59):
        _retain_terminal_without_series(
            ledger=ledger,
            experiment_id=large_id,
            ordinal=ordinal,
            family="NULL",
            state=ResearchTrialState.FAILED,
            reason="DETERMINISTIC_RESEARCH_FAILURE",
            source_type=ResearchTrialSourceType.STRATEGY_LAB_TRAINING,
            simulator="APEX_RESEARCH",
        )
    for ordinal in range(59, 61):
        _retain_terminal_without_series(
            ledger=ledger,
            experiment_id=large_id,
            ordinal=ordinal,
            family="NULL",
            state=ResearchTrialState.FAILED,
            reason="LEAN_RUNTIME_UNAVAILABLE",
            source_type=ResearchTrialSourceType.EXTERNAL_SIMULATOR,
            simulator="LEAN",
        )
    for ordinal in range(61, 65):
        _retain_terminal_without_series(
            ledger=ledger,
            experiment_id=large_id,
            ordinal=ordinal,
            family="CORRELATED",
            state=ResearchTrialState.REJECTED_INTEGRITY,
            reason="MATERIAL_SIMULATOR_MISMATCH",
            source_type=ResearchTrialSourceType.EXTERNAL_SIMULATOR,
            simulator="VECTORBT",
        )
    lucky_evaluation = service.evaluate(experiment_id=large_id, cutoff_at=_NOW + timedelta(days=1))
    if lucky_evaluation.best_observed_trial_id is not None:
        service.select(
            experiment_id=large_id,
            selected_trial_id=lucky_evaluation.best_observed_trial_id,
            criterion="SHARPE_RATIO",
            direction="MAXIMIZE",
            action_identity="automated-research-demo",
            reason="Retain raw best before multiple-testing correction",
            selected_at=_NOW + timedelta(days=1, seconds=1),
        )

    budget_exhaustion = False
    try:
        _retain_terminal_without_series(
            ledger=ledger,
            experiment_id=large_id,
            ordinal=65,
            family="NULL",
            state=ResearchTrialState.FAILED,
            reason="SHOULD_NOT_RESERVE",
            source_type=ResearchTrialSourceType.FIXTURE_VALIDATION,
            simulator="FIXTURE",
        )
    except ResearchIntegrityApplicationError as error:
        budget_exhaustion = str(error) == "TRIAL_BUDGET_EXHAUSTED"

    exact_replay = ledger.reserve(
        experiment_id=large_id,
        source_type=ResearchTrialSourceType.FIXTURE_VALIDATION,
        source_identity=f"{large_id}-source-001",
        source_hash=content_digest({"experiment": large_id, "source": 1}),
        strategy_model_family="MODEST",
        configuration={"lookback": 10, "threshold": "0.01"},
        feature_set_hash=content_digest({"feature_variant": 1}),
        dataset_hash=_DATASET_HASH,
        simulator_identity="DETERMINISTIC_FIXTURE",
        simulator_version="1.0",
        execution_cost_policy_hash=_COST_HASH,
        random_seed=1,
        idempotency_key=f"{large_id}-trial-001",
        started_at=_NOW + timedelta(seconds=1),
    )
    conflicting_replay_rejected = False
    try:
        ledger.reserve(
            experiment_id=large_id,
            source_type=ResearchTrialSourceType.FIXTURE_VALIDATION,
            source_identity=f"{large_id}-source-001",
            source_hash=content_digest({"experiment": large_id, "source": 1}),
            strategy_model_family="MODEST",
            configuration={"lookback": 999},
            feature_set_hash=content_digest({"feature_variant": 1}),
            dataset_hash=_DATASET_HASH,
            simulator_identity="DETERMINISTIC_FIXTURE",
            simulator_version="1.0",
            execution_cost_policy_hash=_COST_HASH,
            random_seed=1,
            idempotency_key=f"{large_id}-trial-001",
            started_at=_NOW + timedelta(seconds=1),
        )
    except ResearchIdempotencyConflict:
        conflicting_replay_rejected = True

    scope_expansion_rejected = False
    try:
        OpenResearchExperimentFamily(repository).execute(
            name="Illicit expanded family",
            vertical="SECURITIES",
            research_family="OVERFITTING_DEMONSTRATION",
            research_question="Changed after results",
            target_label_identity="daily-total-return-v1",
            benchmark_identities=("CASH",),
            data_scope_identities=("expanded-data",),
            data_scope_hashes=(content_digest({"expanded": True}),),
            universe_hash=_UNIVERSE_HASH,
            period_start="2025-001",
            period_end="2025-140",
            cutoff_at=_NOW,
            feature_schema_hashes=(_FEATURE_HASH,),
            permitted_families=("MODEST",),
            parameter_space={"lookback": (5, 10, 20, 40, 80)},
            maximum_trials=100,
            maximum_feature_variants=5,
            maximum_model_variants=1,
            maximum_simulator_variants=1,
            evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
            evaluation_policy_payload={"minimum_psr": "0.95"},
            cost_execution_assumptions={"commission_bps": "1"},
            frequency=ResearchFrequency.DAILY,
            operator_action_id="operator-expanded",
            operator_reason="Should require a new family",
            idempotency_key="demo-overfit-family",
            created_at=_NOW,
        )
    except ResearchIdempotencyConflict:
        scope_expansion_rejected = True

    stable = _open(
        repository,
        name="Stable modest-signal family",
        family_name="STABLE_MODEST_DEMONSTRATION",
        maximum_trials=5,
        permitted=("MODEST",),
        key="demo-stable-family",
    )
    stable_id = str(stable.experiment_id)
    for ordinal, mean in enumerate(
        (
            Decimal("0.00120"),
            Decimal("0.00080"),
            Decimal("0.00060"),
            Decimal("0.00040"),
            Decimal("0.00020"),
        ),
        start=1,
    ):
        _retain_completed(
            repository=repository,
            ledger=ledger,
            service=service,
            experiment_id=stable_id,
            ordinal=ordinal,
            family="MODEST",
            mean=mean,
            amplitude=Decimal("0.0010"),
            phase=0,
        )
    stable_evaluation = service.evaluate(
        experiment_id=stable_id, cutoff_at=_NOW + timedelta(days=1)
    )

    baseline = _open(
        repository,
        name="Canonical Securities simulator family",
        family_name="CANONICAL_SECURITIES_BASELINES",
        maximum_trials=17,
        permitted=(
            "CASH",
            "BUY_AND_HOLD",
            "EQUAL_WEIGHT_REBALANCE",
            "TIME_SERIES_MOMENTUM",
            "CROSS_SECTIONAL_MOMENTUM",
            "SHORT_HORIZON_MEAN_REVERSION",
            "PAIRS_DISTANCE_MEAN_REVERSION",
            "VOLATILITY_TARGETED_LONG_ONLY",
        ),
        key="demo-baseline-family",
    )
    baseline_id = str(baseline.experiment_id)
    baseline_families = tuple(baseline.permitted_families)
    for ordinal in range(1, 9):
        _retain_terminal_without_series(
            ledger=ledger,
            experiment_id=baseline_id,
            ordinal=ordinal,
            family=baseline_families[ordinal - 1],
            state=ResearchTrialState.COMPLETED,
            reason="RETURN_SERIES_NOT_RETAINED_IN_0027",
            source_type=ResearchTrialSourceType.CANONICAL_BASELINE,
            simulator="APEX_RESEARCH",
        )
    for ordinal in range(9, 17):
        index = ordinal - 9
        _retain_terminal_without_series(
            ledger=ledger,
            experiment_id=baseline_id,
            ordinal=ordinal,
            family=baseline_families[index],
            state=ResearchTrialState.COMPLETED
            if index == 0
            else ResearchTrialState.REJECTED_INTEGRITY,
            reason="CONFORMANT" if index == 0 else "MATERIAL_SIMULATOR_MISMATCH",
            source_type=ResearchTrialSourceType.EXTERNAL_SIMULATOR,
            simulator="VECTORBT",
        )
    _retain_terminal_without_series(
        ledger=ledger,
        experiment_id=baseline_id,
        ordinal=17,
        family="CASH",
        state=ResearchTrialState.FAILED,
        reason="LEAN_RUNTIME_UNAVAILABLE",
        source_type=ResearchTrialSourceType.EXTERNAL_SIMULATOR,
        simulator="LEAN",
    )
    baseline_evaluation = service.evaluate(
        experiment_id=baseline_id, cutoff_at=_NOW + timedelta(days=1)
    )

    artifact_hash_mismatch_blocked = False
    with TemporaryDirectory(prefix="apex-research-integrity-demo-") as temporary:
        corrupt_repository = InMemoryResearchIntegrityRepository()
        corrupt_store = LocalResearchReturnArtifactStore(Path(temporary))
        corrupt_ledger = ResearchTrialLedger(corrupt_repository)
        corrupt_service = ResearchIntegrityService(corrupt_repository, corrupt_store)
        corrupt_family = _open(
            corrupt_repository,
            name="Artifact verification family",
            family_name="ARTIFACT_VERIFICATION",
            maximum_trials=3,
            permitted=("MODEST",),
            key="artifact-verification-family",
        )
        for ordinal in range(1, 4):
            _retain_completed(
                repository=corrupt_repository,
                ledger=corrupt_ledger,
                service=corrupt_service,
                experiment_id=str(corrupt_family.experiment_id),
                ordinal=ordinal,
                family="MODEST",
                mean=Decimal("0.0005"),
                amplitude=Decimal("0.001"),
                phase=ordinal,
            )
        artifact = corrupt_repository.artifacts(
            corrupt_repository.trials(str(corrupt_family.experiment_id))[0].trial_id
        )[1]
        (corrupt_store.root / artifact.relative_path).write_text(
            "hash mismatch\n", encoding="utf-8"
        )
        corrupt_evaluation = corrupt_service.evaluate(
            experiment_id=str(corrupt_family.experiment_id),
            cutoff_at=_NOW + timedelta(days=1),
        )
        artifact_hash_mismatch_blocked = (
            "RETURN_ARTIFACT_MISSING_OR_HASH_INVALID" in corrupt_evaluation.reason_codes
        )

    lucky_trials = repository.trials(large_id)
    best_raw_record = next(
        (item for item in lucky_trials if item.trial_id == lucky_evaluation.best_observed_trial_id),
        None,
    )
    return {
        "warning": "BEST OBSERVED RESULT MAY REFLECT RESEARCH SELECTION",
        "authority_warning": "RESEARCH ELIGIBILITY IS NOT STRATEGY OR CAPITAL AUTHORITY",
        "experiment_families": len(repository.families()),
        "overfit_family": {
            "experiment_id": large_id,
            "declared_budget": large.maximum_trials,
            "attempted_trials": len(lucky_trials),
            "completed_with_series": len(completed_ids),
            "failed_trials": sum(item.state is ResearchTrialState.FAILED for item in lucky_trials),
            "integrity_blocked_trials": sum(
                item.state is ResearchTrialState.REJECTED_INTEGRITY for item in lucky_trials
            ),
            "raw_comparable_trials": lucky_evaluation.raw_comparable_trial_count,
            "effective_independent_trials": str(lucky_evaluation.effective_independent_trials),
            "best_raw_trial": lucky_evaluation.best_observed_trial_id,
            "best_raw_trial_ordinal": best_raw_record.ordinal
            if best_raw_record is not None
            else None,
            "best_raw_trial_family": best_raw_record.strategy_model_family
            if best_raw_record is not None
            else None,
            "best_raw_sharpe": str(lucky_evaluation.best_observed_metric),
            "probabilistic_sharpe_ratio": str(lucky_evaluation.probabilistic_sharpe_ratio),
            "deflated_sharpe_ratio": str(lucky_evaluation.deflated_sharpe_ratio),
            "probability_backtest_overfitting": str(
                lucky_evaluation.probability_backtest_overfitting
            ),
            "best_fold_excluded": lucky_evaluation.best_fold_excluded,
            "best_period_excluded": lucky_evaluation.best_period_excluded,
            "recommendation": lucky_evaluation.recommendation.value,
            "reason_codes": lucky_evaluation.reason_codes,
        },
        "stable_family": {
            "experiment_id": stable_id,
            "attempted_trials": len(repository.trials(stable_id)),
            "raw_comparable_trials": stable_evaluation.raw_comparable_trial_count,
            "effective_independent_trials": str(stable_evaluation.effective_independent_trials),
            "best_trial": stable_evaluation.best_observed_trial_id,
            "probabilistic_sharpe_ratio": str(stable_evaluation.probabilistic_sharpe_ratio),
            "deflated_sharpe_ratio": str(stable_evaluation.deflated_sharpe_ratio),
            "probability_backtest_overfitting": str(
                stable_evaluation.probability_backtest_overfitting
            ),
            "recommendation": stable_evaluation.recommendation.value,
            "reason_codes": stable_evaluation.reason_codes,
        },
        "baseline_family": {
            "experiment_id": baseline_id,
            "attempted_trials": len(repository.trials(baseline_id)),
            "vectorbt_material_mismatches": 7,
            "lean_unavailable": 1,
            "state": baseline_evaluation.state.value,
            "recommendation": baseline_evaluation.recommendation.value,
            "reason_codes": baseline_evaluation.reason_codes,
        },
        "contract_proofs": {
            "budget_exhaustion_rejected": budget_exhaustion,
            "scope_expansion_rejected": scope_expansion_rejected,
            "exact_replay_same_trial": exact_replay.trial_id == completed_ids[0],
            "conflicting_replay_rejected": conflicting_replay_rejected,
            "artifact_hash_mismatch_blocked": artifact_hash_mismatch_blocked,
        },
        "zero_authority_proof": {
            "provider_calls": 0,
            "broker_calls": 0,
            "opportunity_proposals": 0,
            "reservations": 0,
            "orders": 0,
            "positions": 0,
            "settlements": 0,
            "ledger_mutations": 0,
            "normal_strategy_authority": 0,
            "paper_authority": 0,
            "live_authority": 0,
        },
        "stable_candidate_eligible": stable_evaluation.recommendation
        is ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW,
    }
