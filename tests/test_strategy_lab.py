from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.adapters.strategy_lab_event_markets import _probabilities, _required_decimal
from apex.adapters.strategy_lab_synthetic import (
    DeterministicStrategyLabDemoAdapter,
    run_strategy_lab_demo,
)
from apex.application.strategy_lab import (
    DatasetState,
    GroupedWalkForwardPolicy,
    InMemoryStrategyLabRepository,
    ModelFamily,
    StrategyLabArtifact,
    StrategyLabArtifactError,
    StrategyLabError,
    StrategyLabService,
    bounded_hyperparameters,
    validate_dataset_rows,
)
from apex.domain.values import ValueValidationError


def test_artifact_store_rejects_escape_and_hash_mismatch(tmp_path: Path) -> None:
    store = LocalStrategyLabArtifactStore(tmp_path / "artifacts")
    with pytest.raises(StrategyLabArtifactError):
        store.write_bytes("../escape.json", b"x")
    artifact = store.write_bytes("datasets/safe.jsonl", b'{"safe":true}\n')
    with pytest.raises(StrategyLabArtifactError):
        store.write_bytes("datasets/safe.jsonl", b"other")
    assert store.write_bytes("datasets/safe.jsonl", b'{"safe":true}\n') == artifact
    with pytest.raises(StrategyLabArtifactError):
        store.read_bytes(
            StrategyLabArtifact(artifact.relative_location, "0" * 64, artifact.byte_size)
        )


def test_rows_are_deterministic_and_grouped_walk_forward_never_leaks() -> None:
    adapter = DeterministicStrategyLabDemoAdapter()
    rows = adapter.rows(cutoff_at=datetime(2026, 3, 1, tzinfo=UTC))
    assert len(rows) == 240
    assert not validate_dataset_rows(
        rows, schema=adapter.schema, cutoff_at=datetime(2026, 3, 1, tzinfo=UTC)
    )
    folds = GroupedWalkForwardPolicy().build_folds(rows)
    assert len(folds) == 3
    for fold in folds:
        assert not (
            set(fold.training_group_ids) & set(fold.validation_group_ids)
            | set(fold.training_group_ids) & set(fold.test_group_ids)
            | set(fold.validation_group_ids) & set(fold.test_group_ids)
        )


def test_invalid_lookahead_is_rejected_before_dataset_build() -> None:
    row = DeterministicStrategyLabDemoAdapter().rows(cutoff_at=datetime(2026, 3, 1, tzinfo=UTC))[0]
    with pytest.raises(ValueValidationError, match="precede label availability"):
        type(row)(
            **{
                **{name: getattr(row, name) for name in row.__dataclass_fields__},
                "feature_at": row.label_available_at + timedelta(seconds=1),
            }
        )


def test_failed_dataset_build_is_retained_as_invalid(tmp_path: Path) -> None:
    base = DeterministicStrategyLabDemoAdapter()

    class EmptyAdapter:
        adapter_id = "empty-v1"
        adapter_version = "1"
        schema = base.schema
        label_definition = base.label_definition

        def source_identity(self, *, cutoff_at: datetime, source_scope: str | None = None) -> str:
            return base.source_identity(cutoff_at=cutoff_at, source_scope=source_scope)

        def rows(
            self, *, cutoff_at: datetime, source_scope: str | None = None
        ) -> tuple[object, ...]:
            return ()

    repository = InMemoryStrategyLabRepository()
    service = StrategyLabService(repository, LocalStrategyLabArtifactStore(tmp_path / "artifacts"))
    with pytest.raises(StrategyLabError, match="DATASET_EMPTY"):
        service.build_dataset(
            adapter=EmptyAdapter(),  # type: ignore[arg-type]
            name="empty",
            cutoff_at=datetime(2026, 3, 1, tzinfo=UTC),
            idempotency_key="empty-key",
        )
    retained = repository.dataset_by_idempotency("empty-v1", "empty-key")
    assert retained is not None
    assert retained.state is DatasetState.INVALID
    assert retained.failure_code == "DATASET_EMPTY"


def test_model_hyperparameters_are_bounded_and_canonical() -> None:
    assert bounded_hyperparameters(ModelFamily.LOGISTIC_REGRESSION, {"C": 1.0}) == {"C": "1"}
    with pytest.raises(StrategyLabError):
        bounded_hyperparameters(ModelFamily.HISTOGRAM_GRADIENT_BOOSTING, {"max_iter": 301})
    with pytest.raises(StrategyLabError):
        bounded_hyperparameters(ModelFamily.LOGISTIC_REGRESSION, {"unknown": "x"})


def test_event_market_probability_payload_accepts_retained_ordered_shape() -> None:
    assert _probabilities(
        '[{"outcome_id":"NO","probability":"0.41"},{"outcome_id":"YES","probability":"0.59"}]'
    ) == {
        "NO": Decimal("0.41"),
        "YES": Decimal("0.59"),
    }
    assert _required_decimal(Decimal("0E-18")) == Decimal(0)


def test_demo_trains_real_models_without_strategy_or_financial_authority(tmp_path: Path) -> None:
    result = run_strategy_lab_demo(artifact_root=tmp_path / "artifacts")
    assert result["dataset_rows"] == 240
    assert result["dataset_groups"] == 80
    assert result["dataset_idempotent_replay"] is True
    assert result["dataset_conflict_rejected"] is True
    assert result["logistic_candidate_state"] == "ELIGIBLE_FOR_SHADOW_REVIEW"
    assert result["boosting_candidate_state"] == "REJECTED"
    assert result["artifact_mismatch_rejected"] is True
    assert result["strategy_authority_count"] == 0
    assert result["financial_mutation_count"] == 0
