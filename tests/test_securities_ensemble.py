from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from apex.application.securities_ensemble import (
    BuildSecuritiesEnsemble,
    EnsembleApplicationError,
    InMemoryEnsembleRepository,
)
from apex.application.securities_strategy_intelligence import (
    BuildGenuineSecuritiesStrategyIntelligence,
    InMemoryStrategyIntelligenceRepository,
)
from tests.test_securities_strategy_intelligence import _dataset


def test_ensemble_retains_blocked_evidence_cash_and_replay(tmp_path: Path) -> None:
    dataset, evidence, artifacts = _dataset(tmp_path)
    intelligence = BuildGenuineSecuritiesStrategyIntelligence(
        evidence=evidence,
        artifacts=artifacts,
        repository=InMemoryStrategyIntelligenceRepository(),  # type: ignore[arg-type]
    ).execute(dataset=dataset, idempotency_key="intelligence")  # type: ignore[arg-type]
    service = BuildSecuritiesEnsemble(artifacts=artifacts, repository=InMemoryEnsembleRepository())

    run = service.execute(intelligence=intelligence, idempotency_key="ensemble")
    assert artifacts.exists(run.components_artifact)
    assert artifacts.exists(run.snapshot_artifact)
    assert run.blockers == ("MATERIAL_SIMULATOR_MISMATCH", "PBO_ABOVE_MAXIMUM")
    assert service.execute(intelligence=intelligence, idempotency_key="ensemble") == run
    with pytest.raises(EnsembleApplicationError, match="IDEMPOTENCY_CONFLICT"):
        service.execute(
            intelligence=replace(intelligence, canonical_result_hash="b" * 64),
            idempotency_key="ensemble",
        )
