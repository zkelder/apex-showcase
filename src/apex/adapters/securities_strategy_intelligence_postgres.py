"""PostgreSQL manifest repository for research-only strategy intelligence."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import securities_strategy_intelligence_run
from apex.application.securities_strategy_intelligence import (
    SecuritiesStrategyIntelligenceRun,
    StrategyIntelligenceApplicationError,
)
from apex.application.strategy_lab import StrategyLabArtifact
from apex.domain.values import canonical_json_bytes


class PostgresStrategyIntelligenceRepository:
    """Stores immutable lineage metadata only; no financial table is touched."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def run_by_idempotency(self, key: str) -> SecuritiesStrategyIntelligenceRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_strategy_intelligence_run).where(
                        securities_strategy_intelligence_run.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore(row)

    def save_run(self, run: SecuritiesStrategyIntelligenceRun) -> SecuritiesStrategyIntelligenceRun:
        prior = self.run_by_idempotency(run.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != run.canonical_input_hash:
                raise StrategyIntelligenceApplicationError(
                    "STRATEGY_INTELLIGENCE_IDEMPOTENCY_CONFLICT"
                )
            return prior
        values = {
            "run_id": run.run_id,
            "dataset_id": run.dataset_id,
            "raw_batch_id": run.raw_batch_id,
            "derived_batch_id": run.derived_batch_id,
            "feature_policy_id": run.feature_policy_id,
            "regime_policy_hash": run.regime_policy_hash,
            "outcome_policy_hash": run.outcome_policy_hash,
            "action_policy_hash": run.action_policy_hash,
            "ranking_policy_hash": run.ranking_policy_hash,
            "model_integrity_state": run.model_integrity_state,
            "simulator_conformance_state": run.simulator_conformance_state,
            "blocking_reasons": list(run.blocking_reasons),
            "artifacts": canonical_json_bytes(
                {
                    "features": asdict(run.features_artifact),
                    "regimes": asdict(run.regimes_artifact),
                    "forecasts": asdict(run.forecasts_artifact),
                    "outcomes": asdict(run.outcomes_artifact),
                    "decisions": asdict(run.decisions_artifact),
                    "ranking": asdict(run.ranking_artifact),
                    "evaluation": asdict(run.evaluation_artifact),
                    "latest_snapshot": asdict(run.latest_snapshot_artifact),
                }
            ).decode(),
            "idempotency_key": run.idempotency_key,
            "canonical_input_hash": run.canonical_input_hash,
            "canonical_result_hash": run.canonical_result_hash,
            "created_at": run.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(securities_strategy_intelligence_run.insert().values(**values))
        except sa.exc.IntegrityError as error:
            raise StrategyIntelligenceApplicationError("STRATEGY_INTELLIGENCE_IMMUTABLE") from error
        return run

    def runs(self) -> tuple[SecuritiesStrategyIntelligenceRun, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_strategy_intelligence_run).order_by(
                        securities_strategy_intelligence_run.c.created_at,
                        securities_strategy_intelligence_run.c.run_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_restore(row) for row in rows)


def _artifact(values: dict[str, object], name: str) -> StrategyLabArtifact:
    item = cast(dict[str, object], values[name])
    return StrategyLabArtifact(
        relative_location=cast(str, item["relative_location"]),
        sha256=cast(str, item["sha256"]),
        byte_size=int(cast(str | int, item["byte_size"])),
    )


def _restore(row: RowMapping) -> SecuritiesStrategyIntelligenceRun:
    artifacts = cast(dict[str, object], json.loads(cast(str, row["artifacts"])))
    return SecuritiesStrategyIntelligenceRun(
        run_id=cast(str, row["run_id"]),
        dataset_id=cast(str, row["dataset_id"]),
        raw_batch_id=cast(str, row["raw_batch_id"]),
        derived_batch_id=cast(str, row["derived_batch_id"]),
        feature_policy_id=cast(str, row["feature_policy_id"]),
        regime_policy_hash=cast(str, row["regime_policy_hash"]),
        outcome_policy_hash=cast(str, row["outcome_policy_hash"]),
        action_policy_hash=cast(str, row["action_policy_hash"]),
        ranking_policy_hash=cast(str, row["ranking_policy_hash"]),
        model_integrity_state=cast(str, row["model_integrity_state"]),
        simulator_conformance_state=cast(str, row["simulator_conformance_state"]),
        blocking_reasons=tuple(cast(list[str], row["blocking_reasons"])),
        features_artifact=_artifact(artifacts, "features"),
        regimes_artifact=_artifact(artifacts, "regimes"),
        forecasts_artifact=_artifact(artifacts, "forecasts"),
        outcomes_artifact=_artifact(artifacts, "outcomes"),
        decisions_artifact=_artifact(artifacts, "decisions"),
        ranking_artifact=_artifact(artifacts, "ranking"),
        evaluation_artifact=_artifact(artifacts, "evaluation"),
        latest_snapshot_artifact=_artifact(artifacts, "latest_snapshot"),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        created_at=row["created_at"],
    )
