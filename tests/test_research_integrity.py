from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.research_integrity_artifacts import (
    LocalResearchReturnArtifactStore,
)
from apex.application.research_integrity import (
    InMemoryResearchIntegrityRepository,
    OpenResearchExperimentFamily,
    ResearchIdempotencyConflict,
    ResearchIntegrityApplicationError,
    ResearchIntegrityService,
    ResearchRecommendation,
    ResearchTrialLedger,
    ResearchTrialSourceType,
    ResearchTrialState,
    strategy_lab_research_integrity_extension,
)
from apex.application.strategy_lab import (
    research_integrity_candidate_rejection_reasons,
)
from apex.domain.research_integrity import (
    ResearchFrequency,
    ResearchIntegrityError,
    ReturnObservation,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
    estimate_effective_trial_count,
    normalize_return_metrics,
    probabilistic_sharpe_ratio,
    selection_stability,
)
from apex.domain.values import content_digest
from apex.operator_api import create_app

HASH = "a" * 64
NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _open_family(
    repository: InMemoryResearchIntegrityRepository,
    *,
    maximum_trials: int = 6,
):
    return OpenResearchExperimentFamily(repository).execute(
        name="Bounded daily experiment",
        vertical="SECURITIES",
        research_family="CANONICAL_BASELINES",
        research_question="Does a predeclared daily rule exceed cash after costs?",
        target_label_identity="daily-total-return-v1",
        benchmark_identities=("CASH",),
        data_scope_identities=("fixture-daily-v1",),
        data_scope_hashes=(HASH,),
        universe_hash="b" * 64,
        period_start="2025-01-01",
        period_end="2025-06-30",
        cutoff_at=NOW,
        feature_schema_hashes=("c" * 64,),
        permitted_families=("MODEST", "NULL", "LUCKY"),
        parameter_space={"lookback": [5, 10], "threshold": ["0.01", "0.02"]},
        maximum_trials=maximum_trials,
        maximum_feature_variants=2,
        maximum_model_variants=3,
        maximum_simulator_variants=2,
        evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
        evaluation_policy_payload={"psr": "0.95", "dsr": "0.80", "pbo": "0.25"},
        cost_execution_assumptions={"commission_bps": "1"},
        frequency=ResearchFrequency.DAILY,
        operator_action_id="operator-research-create",
        operator_reason="Bounded integrity proof",
        idempotency_key="family-one",
        created_at=NOW,
    )


def _observations(
    values: tuple[Decimal, ...], *, group_size: int = 20
) -> tuple[ReturnObservation, ...]:
    return tuple(
        ReturnObservation(
            period_id=f"2025-{index + 1:03}",
            value=value,
            group_id=f"group-{index // group_size:02}",
            instrument_id=f"instrument-{index % 8:02}",
        )
        for index, value in enumerate(values)
    )


def _series(
    *,
    mean: Decimal,
    amplitude: Decimal,
    phase: int = 0,
    lucky_period: int | None = None,
) -> tuple[ReturnObservation, ...]:
    values = []
    pattern = (-3, -2, -1, 1, 2, 3, -1, 1)
    for index in range(120):
        value = mean + amplitude * Decimal(pattern[(index + phase) % len(pattern)])
        if lucky_period is not None and index == lucky_period:
            value += Decimal("0.25")
        values.append(value)
    return _observations(tuple(values))


def test_family_freezes_scope_and_replay_is_exact() -> None:
    repository = InMemoryResearchIntegrityRepository()
    family = _open_family(repository)
    assert _open_family(repository) == family
    with pytest.raises(ResearchIdempotencyConflict, match="RESEARCH_FAMILY_IDEMPOTENCY_CONFLICT"):
        OpenResearchExperimentFamily(repository).execute(
            name="Expanded after observing results",
            vertical="SECURITIES",
            research_family="CANONICAL_BASELINES",
            research_question="Changed question",
            target_label_identity="daily-total-return-v1",
            benchmark_identities=("CASH",),
            data_scope_identities=("fixture-daily-v1",),
            data_scope_hashes=(HASH,),
            universe_hash="b" * 64,
            period_start="2025-01-01",
            period_end="2025-06-30",
            cutoff_at=NOW,
            feature_schema_hashes=("c" * 64,),
            permitted_families=("MODEST",),
            parameter_space={"lookback": [5]},
            maximum_trials=3,
            maximum_feature_variants=1,
            maximum_model_variants=1,
            maximum_simulator_variants=1,
            evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
            evaluation_policy_payload={"psr": "0.95"},
            cost_execution_assumptions={"commission_bps": "1"},
            frequency=ResearchFrequency.DAILY,
            operator_action_id="changed",
            operator_reason="Not permitted",
            idempotency_key="family-one",
            created_at=NOW,
        )


def test_trial_ordinals_budget_failures_and_conflicting_replay_are_retained() -> None:
    repository = InMemoryResearchIntegrityRepository()
    family = _open_family(repository, maximum_trials=3)
    ledger = ResearchTrialLedger(repository)
    trials = []
    for index, state in enumerate(
        (
            ResearchTrialState.COMPLETED,
            ResearchTrialState.FAILED,
            ResearchTrialState.REJECTED_INTEGRITY,
        ),
        start=1,
    ):
        trial = ledger.reserve(
            experiment_id=family.experiment_id,
            source_type=ResearchTrialSourceType.SECURITIES_SIMULATION,
            source_identity=f"source-{index}",
            source_hash=content_digest({"source": index}),
            strategy_model_family="MODEST",
            configuration={"lookback": index},
            feature_set_hash="c" * 64,
            dataset_hash=HASH,
            simulator_identity="APEX_RESEARCH",
            simulator_version="1",
            execution_cost_policy_hash="d" * 64,
            random_seed=index,
            idempotency_key=f"trial-{index}",
            started_at=NOW + timedelta(seconds=index),
        )
        trials.append(
            ledger.finish(
                trial.trial_id,
                state=state,
                completed_at=NOW + timedelta(minutes=index),
                result_identity=(
                    f"result-{index}" if state is ResearchTrialState.COMPLETED else None
                ),
                result_hash=content_digest({"result": index})
                if state is ResearchTrialState.COMPLETED
                else None,
                failure_reason=None
                if state is ResearchTrialState.COMPLETED
                else "RUNTIME_UNAVAILABLE",
                integrity_blocked=state is ResearchTrialState.REJECTED_INTEGRITY,
            )
        )
    assert tuple(item.ordinal for item in repository.trials(family.experiment_id)) == (
        1,
        2,
        3,
    )
    assert {item.state for item in trials} == {
        ResearchTrialState.COMPLETED,
        ResearchTrialState.FAILED,
        ResearchTrialState.REJECTED_INTEGRITY,
    }
    replay = ledger.reserve(
        experiment_id=family.experiment_id,
        source_type=ResearchTrialSourceType.SECURITIES_SIMULATION,
        source_identity="source-1",
        source_hash=content_digest({"source": 1}),
        strategy_model_family="MODEST",
        configuration={"lookback": 1},
        feature_set_hash="c" * 64,
        dataset_hash=HASH,
        simulator_identity="APEX_RESEARCH",
        simulator_version="1",
        execution_cost_policy_hash="d" * 64,
        random_seed=1,
        idempotency_key="trial-1",
        started_at=NOW + timedelta(seconds=1),
    )
    assert replay == trials[0]
    with pytest.raises(ResearchIntegrityApplicationError, match="TRIAL_BUDGET_EXHAUSTED"):
        ledger.reserve(
            experiment_id=family.experiment_id,
            source_type=ResearchTrialSourceType.FIXTURE_VALIDATION,
            source_identity="source-4",
            source_hash="e" * 64,
            strategy_model_family="MODEST",
            configuration={"lookback": 4},
            feature_set_hash="c" * 64,
            dataset_hash=HASH,
            simulator_identity="FIXTURE",
            simulator_version="1",
            execution_cost_policy_hash="d" * 64,
            random_seed=4,
            idempotency_key="trial-4",
            started_at=NOW + timedelta(seconds=4),
        )


def test_safe_artifact_replay_and_hash_mismatch(tmp_path: Path) -> None:
    repository = InMemoryResearchIntegrityRepository()
    family = _open_family(repository)
    ledger = ResearchTrialLedger(repository)
    trial = ledger.reserve(
        experiment_id=family.experiment_id,
        source_type=ResearchTrialSourceType.FIXTURE_VALIDATION,
        source_identity="safe-artifact",
        source_hash=HASH,
        strategy_model_family="MODEST",
        configuration={"lookback": 5},
        feature_set_hash="c" * 64,
        dataset_hash=HASH,
        simulator_identity="FIXTURE",
        simulator_version="1",
        execution_cost_policy_hash="d" * 64,
        random_seed=1,
        idempotency_key="artifact-trial",
        started_at=NOW + timedelta(seconds=1),
    )
    ledger.finish(
        trial.trial_id,
        state=ResearchTrialState.COMPLETED,
        completed_at=NOW + timedelta(minutes=1),
        result_identity="safe-result",
        result_hash="f" * 64,
    )
    store = LocalResearchReturnArtifactStore(tmp_path / "managed")
    service = ResearchIntegrityService(repository, store)
    evidence = _series(mean=Decimal("0.0003"), amplitude=Decimal("0.0002"))
    metric = service.retain_return_evidence(
        trial_id=trial.trial_id,
        gross_observations=evidence,
        net_observations=evidence,
        frequency=ResearchFrequency.DAILY,
    )
    assert metric.observation_count == 120
    assert store.read(repository.artifacts(trial.trial_id)[1]) == evidence
    artifact = repository.artifacts(trial.trial_id)[1]
    target = store.root / artifact.relative_path
    target.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ResearchIntegrityApplicationError, match="RETURN_ARTIFACT_HASH_MISMATCH"):
        store.read(artifact)


def test_metric_psr_dsr_and_effective_trial_reference_cases() -> None:
    observations = _series(mean=Decimal("0.0005"), amplitude=Decimal("0.001"))
    metrics = normalize_return_metrics(
        gross_observations=observations,
        net_observations=observations,
        frequency=ResearchFrequency.DAILY,
    )
    assert metrics.observation_count == 120
    assert metrics.sharpe_ratio is not None and metrics.sharpe_ratio > 0
    assert metrics.skewness is not None
    assert metrics.excess_kurtosis is not None
    psr = probabilistic_sharpe_ratio(
        observed_sharpe=Decimal("1"),
        benchmark_sharpe=Decimal(),
        effective_sample_size=Decimal("100"),
        skewness=Decimal(),
        excess_kurtosis=Decimal(),
    )
    assert psr > Decimal("0.999")
    effective = estimate_effective_trial_count(
        {
            "same-a": tuple(item.value for item in observations),
            "same-b": tuple(item.value for item in observations),
            "opposite": tuple(-item.value for item in observations),
        }
    )
    assert Decimal(1) <= effective.effective_trial_count < Decimal(3)
    deflated = deflated_sharpe_ratio(
        best_sharpe=Decimal("1.2"),
        observed_trial_sharpes=(
            Decimal("0.1"),
            Decimal("0.2"),
            Decimal("0.3"),
            Decimal("1.2"),
        ),
        effective_trial_count=Decimal("3"),
        effective_sample_size=Decimal("100"),
        skewness=Decimal(),
        excess_kurtosis=Decimal(),
    )
    assert Decimal() <= deflated.deflated_sharpe_ratio <= Decimal(1)
    assert deflated.expected_maximum_sharpe > 0
    assert deflated.minimum_track_record_length is not None


def test_cscv_and_stability_detect_isolated_lucky_winner() -> None:
    trial_returns = {
        "stable-a": _series(mean=Decimal("0.0004"), amplitude=Decimal("0.001")),
        "stable-b": _series(mean=Decimal("0.00035"), amplitude=Decimal("0.001"), phase=1),
        "null": _series(mean=Decimal(), amplitude=Decimal("0.0012"), phase=2),
        "lucky": _series(
            mean=Decimal("-0.0001"),
            amplitude=Decimal("0.001"),
            phase=3,
            lucky_period=25,
        ),
    }
    cscv = cscv_probability_of_backtest_overfitting(trial_returns=trial_returns)
    assert cscv.evaluated_split_count == 20
    assert Decimal() <= cscv.probability_of_backtest_overfitting <= Decimal(1)
    stability = selection_stability(trial_returns=trial_returns)
    assert stability.original_winner == "lucky"
    assert stability.best_period_excluded["winner_changed"] is True


def test_evaluation_never_grants_strategy_or_capital_authority(tmp_path: Path) -> None:
    repository = InMemoryResearchIntegrityRepository()
    family = _open_family(repository)
    ledger = ResearchTrialLedger(repository)
    service = ResearchIntegrityService(
        repository, LocalResearchReturnArtifactStore(tmp_path / "managed")
    )
    for index, name in enumerate(("MODEST", "NULL", "LUCKY"), start=1):
        trial = ledger.reserve(
            experiment_id=family.experiment_id,
            source_type=ResearchTrialSourceType.FIXTURE_VALIDATION,
            source_identity=f"result-{index}",
            source_hash=content_digest({"source": index}),
            strategy_model_family=name,
            configuration={"variant": index},
            feature_set_hash="c" * 64,
            dataset_hash=HASH,
            simulator_identity="FIXTURE",
            simulator_version="1",
            execution_cost_policy_hash="d" * 64,
            random_seed=index,
            idempotency_key=f"evaluation-{index}",
            started_at=NOW + timedelta(seconds=index),
        )
        completed = ledger.finish(
            trial.trial_id,
            state=ResearchTrialState.COMPLETED,
            completed_at=NOW + timedelta(minutes=index),
            result_identity=f"result-{index}",
            result_hash=content_digest({"result": index}),
        )
        evidence = _series(
            mean=Decimal("0.0003") - Decimal(index - 1) * Decimal("0.0001"),
            amplitude=Decimal("0.001"),
            phase=index,
            lucky_period=10 if name == "LUCKY" else None,
        )
        service.retain_return_evidence(
            trial_id=completed.trial_id,
            gross_observations=evidence,
            net_observations=evidence,
            frequency=ResearchFrequency.DAILY,
        )
    evaluation = service.evaluate(
        experiment_id=family.experiment_id, cutoff_at=NOW + timedelta(days=1)
    )
    assert evaluation.recommendation in {
        ResearchRecommendation.REJECT_SELECTION_BIAS,
        ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW,
    }
    assert not hasattr(evaluation, "order_id")
    assert not hasattr(evaluation, "capital")
    extension = strategy_lab_research_integrity_extension(
        experiment=family,
        trial=completed,
        evaluation=evaluation,
    )
    expected_gate = (
        ()
        if evaluation.recommendation is ResearchRecommendation.ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW
        else ("RESEARCH_SELECTION_INTEGRITY_NOT_ELIGIBLE",)
    )
    assert research_integrity_candidate_rejection_reasons(evaluation) == expected_gate
    assert extension["authority_granted"] is False
    assert extension["eligible_for_shadow_review"] is (not expected_gate)


def test_cscv_rejects_incomparable_frequency_length() -> None:
    with pytest.raises(ResearchIntegrityError, match="CSCV_SERIES_INCOMPARABLE"):
        cscv_probability_of_backtest_overfitting(
            trial_returns={
                "a": _series(mean=Decimal(), amplitude=Decimal("0.001")),
                "b": _series(mean=Decimal(), amplitude=Decimal("0.001"))[:-1],
                "c": _series(mean=Decimal(), amplitude=Decimal("0.001"), phase=2),
            }
        )


def test_research_integrity_api_and_console_expose_no_trial_deletion() -> None:
    routes = {
        (route.path, method)
        for route in create_app().routes
        if route.path.startswith("/api/v1/research-integrity/")
        for method in route.methods
    }
    assert (
        "/api/v1/research-integrity/experiment-families",
        "POST",
    ) in routes
    assert (
        "/api/v1/research-integrity/experiment-families/{experiment_id}/trials",
        "GET",
    ) in routes
    assert not any(method == "DELETE" for _, method in routes)
    console = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "main.tsx").read_text(
        encoding="utf-8"
    )
    assert "TESTING MORE VARIANTS INCREASES THE CHANCE OF A FALSE WINNER" in console
    assert "RESEARCH INTEGRITY ELIGIBILITY DOES NOT GRANT TRADING AUTHORITY" in console
    assert "There is no deletion control or default failed-run filter." in console
