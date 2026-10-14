"""Immutable PostgreSQL repository for research-only ensemble run manifests."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import securities_ensemble_run
from apex.application.securities_ensemble import EnsembleApplicationError, SecuritiesEnsembleRun
from apex.application.strategy_lab import StrategyLabArtifact
from apex.domain.values import canonical_json_bytes


class PostgresEnsembleRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def run_by_idempotency(self, key: str) -> SecuritiesEnsembleRun | None:
        with self._engine.connect() as c:
            row = (
                c.execute(
                    sa.select(securities_ensemble_run).where(
                        securities_ensemble_run.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore(row)

    def save_run(self, run: SecuritiesEnsembleRun) -> SecuritiesEnsembleRun:
        prior = self.run_by_idempotency(run.idempotency_key)
        if prior:
            if prior.canonical_input_hash != run.canonical_input_hash:
                raise EnsembleApplicationError("ENSEMBLE_IDEMPOTENCY_CONFLICT")
            return prior
        artifacts = {
            name: asdict(getattr(run, f"{name}_artifact"))
            for name in (
                "components",
                "credibility",
                "ensemble",
                "decisions",
                "portfolio",
                "evaluation",
                "snapshot",
            )
        }
        try:
            with self._engine.begin() as c:
                c.execute(
                    securities_ensemble_run.insert().values(
                        run_id=run.run_id,
                        intelligence_run_id=run.intelligence_run_id,
                        artifacts=canonical_json_bytes(artifacts).decode(),
                        blockers=list(run.blockers),
                        idempotency_key=run.idempotency_key,
                        canonical_input_hash=run.canonical_input_hash,
                        canonical_result_hash=run.canonical_result_hash,
                        created_at=run.created_at,
                    )
                )
        except sa.exc.IntegrityError as error:
            raise EnsembleApplicationError("ENSEMBLE_IMMUTABLE") from error
        return run

    def runs(self) -> tuple[SecuritiesEnsembleRun, ...]:
        with self._engine.connect() as c:
            rows = (
                c.execute(
                    sa.select(securities_ensemble_run).order_by(
                        securities_ensemble_run.c.created_at
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_restore(x) for x in rows)


def _restore(row: RowMapping) -> SecuritiesEnsembleRun:
    values = cast(dict[str, object], json.loads(cast(str, row["artifacts"])))

    def artifact(name: str) -> StrategyLabArtifact:
        value = cast(dict[str, object], values[name])
        return StrategyLabArtifact(
            cast(str, value["relative_location"]),
            cast(str, value["sha256"]),
            int(cast(str | int, value["byte_size"])),
        )

    return SecuritiesEnsembleRun(
        cast(str, row["run_id"]),
        cast(str, row["intelligence_run_id"]),
        artifact("components"),
        artifact("credibility"),
        artifact("ensemble"),
        artifact("decisions"),
        artifact("portfolio"),
        artifact("evaluation"),
        artifact("snapshot"),
        tuple(cast(list[str], row["blockers"])),
        cast(str, row["canonical_input_hash"]),
        cast(str, row["canonical_result_hash"]),
        cast(str, row["idempotency_key"]),
        row["created_at"],
    )
