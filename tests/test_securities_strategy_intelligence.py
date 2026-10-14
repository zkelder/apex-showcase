from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.securities_synthetic import synthetic_securities_dataset
from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
from apex.application.securities_strategy_intelligence import (
    BuildGenuineSecuritiesStrategyIntelligence,
    InMemoryStrategyIntelligenceRepository,
    StrategyIntelligenceApplicationError,
)
from apex.application.securities_strategy_lab_dataset import (
    BuildGenuineSecuritiesDailyDirectionDataset,
    InMemorySecuritiesDirectionDatasetRepository,
)
from apex.application.strategy_lab import InMemoryStrategyLabRepository, StrategyLabService
from apex.domain.real_securities_data import SecuritiesUniverseSnapshot
from apex.domain.securities import SecuritiesDataBatchState, SecuritiesMarketDataBatch
from apex.domain.strategy_intelligence import (
    MarketRegime,
    MarketRegimePolicy,
)
from apex.domain.values import content_digest


class _Evidence:
    def __init__(self, batch: SecuritiesMarketDataBatch, dataset: tuple[object, ...]) -> None:
        self.batch = batch
        self.value = dataset

    def get_batch(self, _batch_id: str) -> SecuritiesMarketDataBatch:
        return self.batch

    def get_dataset(self, _batch_id: str) -> tuple[object, ...]:
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


def _dataset(tmp_path: Path) -> tuple[object, _Evidence, LocalStrategyLabArtifactStore]:
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    evidence = _Evidence(_batch(), (instruments, sessions, bars, actions))
    artifacts = LocalStrategyLabArtifactStore(tmp_path / "artifacts")
    universe = SecuritiesUniverseSnapshot.fixed(
        display_name="fixture fixed universe",
        symbols=tuple(item.symbol for item in instruments),
        reason="fixed before acquisition",
        frozen_at=datetime(2024, 1, 1, tzinfo=UTC),
        operator_action_id="fixture",
        idempotency_key="fixture-universe",
    )
    result = BuildGenuineSecuritiesDailyDirectionDataset(
        evidence=evidence,
        strategy_service=StrategyLabService(InMemoryStrategyLabRepository(), artifacts),
        artifacts=artifacts,
        repository=InMemorySecuritiesDirectionDatasetRepository(),
    ).execute(
        raw_batch_id="raw",
        derived_batch_id="derived",
        universe=universe,
        cutoff_at=max(item.available_at for item in bars),
        idempotency_key="dataset",
    )
    return result, evidence, artifacts


def test_regime_policy_is_exclusive_and_deterministic() -> None:
    policy = MarketRegimePolicy()
    measurements = {
        "broad_trend_60": Decimal("0.05"),
        "breadth_20": Decimal("0.8"),
        "volatility_20": Decimal("0.01"),
        "drawdown_60": Decimal("-0.01"),
        "average_correlation": Decimal("0.2"),
        "correlation_change": Decimal("0"),
        "return_dispersion": Decimal("0.01"),
    }
    first = policy.classify(session_id="s", measurements=measurements, source_lineage_hash="a" * 64)
    assert first.regime is MarketRegime.TRENDING_RISK_ON
    assert first == policy.classify(
        session_id="s", measurements=measurements, source_lineage_hash="a" * 64
    )


def test_genuine_intelligence_retains_research_only_ranking_and_replay(tmp_path: Path) -> None:
    dataset, evidence, artifacts = _dataset(tmp_path)
    repository = InMemoryStrategyIntelligenceRepository()
    service = BuildGenuineSecuritiesStrategyIntelligence(
        evidence=evidence, artifacts=artifacts, repository=repository
    )  # type: ignore[arg-type]
    run = service.execute(dataset=dataset, idempotency_key="intelligence")  # type: ignore[arg-type]

    assert artifacts.exists(run.features_artifact)
    assert artifacts.exists(run.latest_snapshot_artifact)
    assert run.blocking_reasons == ("MATERIAL_SIMULATOR_MISMATCH", "PBO_ABOVE_MAXIMUM")
    assert service.execute(dataset=dataset, idempotency_key="intelligence") == run  # type: ignore[arg-type]
    with pytest.raises(StrategyIntelligenceApplicationError, match="IDEMPOTENCY_CONFLICT"):
        service.execute(
            dataset=dataset, idempotency_key="intelligence", blocking_reasons=("DIFFERENT",)
        )  # type: ignore[arg-type]
