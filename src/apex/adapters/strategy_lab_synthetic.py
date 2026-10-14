"""Deterministic, provider-free evidence used by the Strategy Lab demo only."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.application.strategy_lab import (
    CalibrationMethod,
    GroupedWalkForwardPolicy,
    InMemoryStrategyLabRepository,
    ModelFamily,
    StrategyLabError,
    StrategyLabFeatureAdapter,
    StrategyLabFeatureDefinition,
    StrategyLabFeatureSchema,
    StrategyLabLabelDefinition,
    StrategyLabRow,
    StrategyLabService,
)
from apex.domain.values import content_digest


class DeterministicStrategyLabDemoAdapter(StrategyLabFeatureAdapter):
    """Eighty event groups with three retained-looking rows each."""

    adapter_id = "synthetic-event-market-v1"
    adapter_version = "1"
    schema = StrategyLabFeatureSchema(
        schema_id="synthetic-event-market-schema-v1",
        vertical="EVENT_MARKETS",
        research_family="SYNTHETIC_PROBABILITY_RESEARCH",
        version="V1",
        features=(
            StrategyLabFeatureDefinition(
                "market_consensus_probability", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
            StrategyLabFeatureDefinition("spread", "DECIMAL", False, "IDENTITY", "DECISION_TIME"),
            StrategyLabFeatureDefinition(
                "visible_liquidity", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
            StrategyLabFeatureDefinition(
                "fee_adjusted_execution_cost", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    label_definition = StrategyLabLabelDefinition(
        label_id="synthetic-event-market-label-v1",
        vertical="EVENT_MARKETS",
        research_family="SYNTHETIC_PROBABILITY_RESEARCH",
        version="V1",
        target_type="BINARY_PROBABILITY",
        positive_semantics="deterministically resolved synthetic positive outcome",
        negative_semantics="deterministically resolved synthetic negative outcome",
        resolution_rule="delayed retained synthetic resolution",
        feature_cutoff_rule="feature time",
        availability_rule="delayed label availability time",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    def source_identity(self, *, cutoff_at: datetime, source_scope: str | None = None) -> str:
        return content_digest({"demo": "strategy-lab", "cutoff": cutoff_at, "scope": source_scope})

    def rows(
        self, *, cutoff_at: datetime, source_scope: str | None = None
    ) -> tuple[StrategyLabRow, ...]:
        base = datetime(2026, 2, 1, tzinfo=UTC)
        configuration_hash = content_digest({"demo_configuration": "strategy-lab-v1"})
        rows: list[StrategyLabRow] = []
        for group in range(80):
            for ordinal in range(3):
                feature_at = base + timedelta(hours=(group * 3) + ordinal)
                if feature_at > cutoff_at:
                    continue
                raw_signal = ((group * 29 + ordinal * 17 + 7) % 97 + 2) / 100
                label = int(raw_signal >= 0.5)
                # The benchmark is a deliberately uninformative decision-time
                # consensus; the retained spread feature carries the signal.
                consensus = Decimal("0.5")
                rows.append(
                    StrategyLabRow(
                        row_id=f"synthetic-row-{group:03d}-{ordinal}",
                        entity_id=f"synthetic-market-{group:03d}-{ordinal}",
                        grouping_id=f"synthetic-event-{group:03d}",
                        feature_at=feature_at,
                        label_available_at=feature_at + timedelta(hours=36),
                        feature_values=(
                            consensus,
                            Decimal(str(raw_signal)),
                            Decimal(100 + group),
                            Decimal("0.02") + Decimal(ordinal) / Decimal("1000"),
                        ),
                        label=label,
                        baseline_probability=consensus,
                        entry_price=consensus,
                        fee_cost=Decimal("0.02"),
                        source_lineage_hash=content_digest({"row": group, "ordinal": ordinal}),
                        source_configuration_hash=configuration_hash,
                    )
                )
        return tuple(rows)


def run_strategy_lab_demo(*, artifact_root: Path | None = None) -> dict[str, object]:
    """Run two real model families through the normal research workflow."""

    adapter = DeterministicStrategyLabDemoAdapter()
    repository = InMemoryStrategyLabRepository()
    artifacts = LocalStrategyLabArtifactStore(artifact_root)
    service = StrategyLabService(repository, artifacts)
    cutoff = datetime(2026, 3, 1, tzinfo=UTC)
    dataset = service.build_dataset(
        adapter=adapter,
        name="Deterministic Strategy Lab Demonstration",
        cutoff_at=cutoff,
        idempotency_key="strategy-lab-demo-dataset-v1",
    )
    replay = service.build_dataset(
        adapter=adapter,
        name="Deterministic Strategy Lab Demonstration",
        cutoff_at=cutoff,
        idempotency_key="strategy-lab-demo-dataset-v1",
    )
    conflict_rejected = False
    try:
        service.build_dataset(
            adapter=adapter,
            name="Conflicting Demonstration",
            cutoff_at=cutoff,
            idempotency_key="strategy-lab-demo-dataset-v1",
        )
    except StrategyLabError:
        conflict_rejected = True
    policy = GroupedWalkForwardPolicy(
        fold_count=3,
        minimum_training_rows=24,
        minimum_validation_rows=12,
        minimum_test_rows=12,
        minimum_groups_per_partition=4,
    )
    logistic = service.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.LOGISTIC_REGRESSION,
        hyperparameters={"C": 1.0, "penalty": "l2", "max_iter": 500},
        calibration=CalibrationMethod.PLATT,
        seed=42,
        idempotency_key="strategy-lab-demo-logistic-v1",
        policy=policy,
    )
    logistic_evaluation, logistic_candidate = service.evaluate_candidate(
        training_run_id=logistic.training_run_id,
        policy=policy,
    )
    boosting = service.train_model(
        dataset_id=dataset.dataset_id,
        family=ModelFamily.HISTOGRAM_GRADIENT_BOOSTING,
        hyperparameters={"max_iter": 1, "min_samples_leaf": 128, "max_leaf_nodes": 2},
        calibration=CalibrationMethod.NONE,
        seed=42,
        idempotency_key="strategy-lab-demo-boosting-v1",
        policy=policy,
    )
    boosting_evaluation, boosting_candidate = service.evaluate_candidate(
        training_run_id=boosting.training_run_id,
        policy=policy,
    )
    artifact_mismatch_rejected = False
    assert logistic.model_artifact is not None
    try:
        artifacts.read_bytes(replace(logistic.model_artifact, sha256="0" * 64))
    except StrategyLabError:
        artifact_mismatch_rejected = True
    except Exception:  # The adapter's typed rejection is intentionally opaque to callers.
        artifact_mismatch_rejected = True
    return {
        "warning": "RESEARCH MODEL — NO STRATEGY OR CAPITAL AUTHORITY",
        "dataset_rows": dataset.row_count,
        "dataset_groups": dataset.grouping_count,
        "dataset_artifact_sha256": (
            dataset.artifact.sha256 if dataset.artifact is not None else ""
        ),
        "dataset_idempotent_replay": replay.dataset_id == dataset.dataset_id,
        "dataset_conflict_rejected": conflict_rejected,
        "logistic_candidate_state": logistic_candidate.state.value,
        "logistic_rejection_reasons": logistic_candidate.rejection_reasons,
        "logistic_brier_difference": str(logistic_evaluation.brier_difference),
        "boosting_candidate_state": boosting_candidate.state.value,
        "boosting_rejection_reasons": boosting_candidate.rejection_reasons,
        "boosting_brier_difference": str(boosting_evaluation.brier_difference),
        "artifact_mismatch_rejected": artifact_mismatch_rejected,
        "financial_mutation_count": 0,
        "strategy_authority_count": 0,
    }
