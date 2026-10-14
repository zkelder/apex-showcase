from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from apex.adapters.securities_synthetic import synthetic_securities_dataset
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.application.securities_strategy_lab_dataset import (
    BuildGenuineSecuritiesDailyDirectionDataset,
    InMemorySecuritiesDirectionDatasetRepository,
)
from apex.application.strategy_lab import InMemoryStrategyLabRepository, StrategyLabService
from apex.domain.real_securities_data import SecuritiesUniverseSnapshot
from apex.domain.securities import SecuritiesDataBatchState, SecuritiesMarketDataBatch
from apex.domain.values import content_digest


class _Evidence:
    def __init__(self, batch: SecuritiesMarketDataBatch, dataset: tuple[object, ...]) -> None:
        self.batch = batch
        self.value = dataset

    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch:
        assert batch_id in {"raw", "derived"}
        return self.batch

    def get_dataset(self, batch_id: str) -> tuple[object, ...]:
        assert batch_id in {"raw", "derived"}
        return self.value


def _batch() -> SecuritiesMarketDataBatch:
    timestamp = datetime(2026, 7, 28, tzinfo=UTC)
    return SecuritiesMarketDataBatch(
        batch_id="genuine-fixture-batch",
        dataset_name="genuine fixture",
        state=SecuritiesDataBatchState.READY,
        imported_at=timestamp,
        source_artifact="fixture.jsonl",
        source_sha256="a" * 64,
        source_byte_size=1,
        canonical_artifact="fixture.jsonl",
        canonical_sha256="a" * 64,
        canonical_byte_size=1,
        instrument_count=12,
        session_count=500,
        bar_count=18_000,
        corporate_action_count=4,
        first_period_start=datetime(2024, 1, 2, tzinfo=UTC),
        last_period_end=datetime(2026, 1, 1, tzinfo=UTC),
        idempotency_key="fixture",
        warnings=(),
        canonical_hash=content_digest({"genuine": "fixture"}),
    )


def test_genuine_daily_direction_dataset_is_grouped_and_has_no_lookahead(tmp_path: Path) -> None:
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    universe = SecuritiesUniverseSnapshot.fixed(
        display_name="fixture fixed universe",
        symbols=tuple(item.symbol for item in instruments),
        reason="fixed before acquisition",
        frozen_at=datetime(2024, 1, 1, tzinfo=UTC),
        operator_action_id="fixture",
        idempotency_key="fixture-universe",
    )
    service = StrategyLabService(
        InMemoryStrategyLabRepository(), LocalStrategyLabArtifactStore(tmp_path / "artifacts")
    )
    result = BuildGenuineSecuritiesDailyDirectionDataset(
        evidence=_Evidence(_batch(), (instruments, sessions, bars, actions)),  # type: ignore[arg-type]
        strategy_service=service,
        artifacts=LocalStrategyLabArtifactStore(tmp_path / "artifacts"),
        repository=InMemorySecuritiesDirectionDatasetRepository(),
    ).execute(
        raw_batch_id="raw",
        derived_batch_id="derived",
        universe=universe,
        cutoff_at=max(item.available_at for item in bars),
        idempotency_key="genuine-fixture-dataset",
    )

    assert result.row_count == 12 * (500 - 60 - 5)
    assert result.group_count == 435
    assert result.instrument_count == 12
    assert result.positive_label_count + result.negative_label_count == result.row_count
    assert dict(result.exclusion_counts) == {
        "FIVE_FUTURE_ELIGIBLE_SESSIONS_UNAVAILABLE": 60,
        "REQUIRED_LOOKBACK_UNAVAILABLE": 720,
    }
    assert len(result.holdout_group_ids) == 87
    assert len(result.folds) == 3
    assert all(
        set(fold.training_group_ids).isdisjoint(fold.validation_group_ids)
        and set(fold.training_group_ids).isdisjoint(fold.test_group_ids)
        and set(fold.validation_group_ids).isdisjoint(fold.test_group_ids)
        and set(fold.test_group_ids).isdisjoint(result.holdout_group_ids)
        for fold in result.folds
    )
    assert service._artifacts.exists(result.feature_rows_artifact)  # type: ignore[attr-defined]
    assert service._artifacts.exists(result.labels_artifact)  # type: ignore[attr-defined]
