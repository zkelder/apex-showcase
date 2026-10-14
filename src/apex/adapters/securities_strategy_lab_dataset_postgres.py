"""PostgreSQL metadata store for the genuine Securities dataset manifest."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import securities_daily_direction_dataset
from apex.application.securities_strategy_lab_dataset import (
    SecuritiesDailyDirectionDataset,
    SecuritiesDirectionDatasetError,
)
from apex.application.strategy_lab import StrategyLabArtifact, StrategyLabFold
from apex.domain.values import canonical_json_bytes


class PostgresSecuritiesDirectionDatasetRepository:
    """Immutable research metadata only; it has no financial dependencies."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def dataset_by_idempotency(self, key: str) -> SecuritiesDailyDirectionDataset | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_daily_direction_dataset).where(
                        securities_daily_direction_dataset.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore(row)

    def save_dataset(
        self, value: SecuritiesDailyDirectionDataset
    ) -> SecuritiesDailyDirectionDataset:
        prior = self.dataset_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise SecuritiesDirectionDatasetError("GENUINE_DATASET_IDEMPOTENCY_CONFLICT")
            return prior
        values = {
            "dataset_id": value.dataset_id,
            "strategy_lab_dataset_id": value.strategy_lab_dataset_id,
            "raw_batch_id": value.raw_batch_id,
            "raw_batch_hash": value.raw_batch_hash,
            "derived_batch_id": value.derived_batch_id,
            "derived_batch_hash": value.derived_batch_hash,
            "universe_id": value.universe_id,
            "universe_hash": value.universe_hash,
            "calendar_identity": value.calendar_identity,
            "calendar_version": value.calendar_version,
            "feature_schema_hash": value.feature_schema_hash,
            "label_definition_hash": value.label_definition_hash,
            "cutoff_at": value.cutoff_at,
            "start_date": date.fromisoformat(value.start_date),
            "end_date": date.fromisoformat(value.end_date),
            "row_count": value.row_count,
            "group_count": value.group_count,
            "instrument_count": value.instrument_count,
            "positive_label_count": value.positive_label_count,
            "negative_label_count": value.negative_label_count,
            "exclusion_counts": canonical_json_bytes(value.exclusion_counts).decode(),
            "artifacts": canonical_json_bytes(
                {
                    "feature_rows": asdict(value.feature_rows_artifact),
                    "labels": asdict(value.labels_artifact),
                    "manifest": asdict(value.manifest_artifact),
                    "folds": asdict(value.folds_artifact),
                    "holdout": asdict(value.holdout_artifact),
                    "returns": asdict(value.returns_artifact),
                }
            ).decode(),
            "fold_definitions": canonical_json_bytes(
                tuple(asdict(item) for item in value.folds)
            ).decode(),
            "holdout_group_ids": list(value.holdout_group_ids),
            "baseline_scorecard_ids": list(value.baseline_scorecard_ids),
            "baseline_scorecard_hashes": list(value.baseline_scorecard_hashes),
            "simulator_conformance_ids": list(value.simulator_conformance_ids),
            "simulator_conformance_hashes": list(value.simulator_conformance_hashes),
            "idempotency_key": value.idempotency_key,
            "canonical_input_hash": value.canonical_input_hash,
            "canonical_result_hash": value.canonical_result_hash,
            "created_at": value.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(securities_daily_direction_dataset.insert().values(**values))
        except sa.exc.IntegrityError as error:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_IMMUTABLE") from error
        return value

    def dataset(self, dataset_id: str) -> SecuritiesDailyDirectionDataset | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_daily_direction_dataset).where(
                        securities_daily_direction_dataset.c.dataset_id == dataset_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore(row)

    def datasets(self) -> tuple[SecuritiesDailyDirectionDataset, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_daily_direction_dataset).order_by(
                        securities_daily_direction_dataset.c.created_at,
                        securities_daily_direction_dataset.c.dataset_id,
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
        byte_size=cast(int, item["byte_size"]),
    )


def _restore(row: RowMapping) -> SecuritiesDailyDirectionDataset:
    artifacts = cast(dict[str, object], json.loads(cast(str, row["artifacts"])))
    folds = tuple(
        StrategyLabFold(
            ordinal=int(cast(str | int, item["ordinal"])),
            training_group_ids=tuple(cast(list[str], item["training_group_ids"])),
            validation_group_ids=tuple(cast(list[str], item["validation_group_ids"])),
            test_group_ids=tuple(cast(list[str], item["test_group_ids"])),
            canonical_hash=cast(str, item["canonical_hash"]),
        )
        for item in cast(list[dict[str, object]], json.loads(cast(str, row["fold_definitions"])))
    )
    return SecuritiesDailyDirectionDataset(
        dataset_id=cast(str, row["dataset_id"]),
        strategy_lab_dataset_id=cast(str, row["strategy_lab_dataset_id"]),
        raw_batch_id=cast(str, row["raw_batch_id"]),
        raw_batch_hash=cast(str, row["raw_batch_hash"]),
        derived_batch_id=cast(str, row["derived_batch_id"]),
        derived_batch_hash=cast(str, row["derived_batch_hash"]),
        universe_id=cast(str, row["universe_id"]),
        universe_hash=cast(str, row["universe_hash"]),
        calendar_identity=cast(str, row["calendar_identity"]),
        calendar_version=cast(str, row["calendar_version"]),
        feature_schema_hash=cast(str, row["feature_schema_hash"]),
        label_definition_hash=cast(str, row["label_definition_hash"]),
        cutoff_at=row["cutoff_at"],
        start_date=str(row["start_date"]),
        end_date=str(row["end_date"]),
        row_count=int(row["row_count"]),
        group_count=int(row["group_count"]),
        instrument_count=int(row["instrument_count"]),
        positive_label_count=int(row["positive_label_count"]),
        negative_label_count=int(row["negative_label_count"]),
        exclusion_counts=tuple(
            (cast(str, item[0]), int(cast(str | int, item[1])))
            for item in cast(list[list[object]], json.loads(cast(str, row["exclusion_counts"])))
        ),
        feature_rows_artifact=_artifact(artifacts, "feature_rows"),
        labels_artifact=_artifact(artifacts, "labels"),
        manifest_artifact=_artifact(artifacts, "manifest"),
        folds_artifact=_artifact(artifacts, "folds"),
        holdout_artifact=_artifact(artifacts, "holdout"),
        returns_artifact=_artifact(artifacts, "returns"),
        folds=folds,
        holdout_group_ids=tuple(cast(list[str], row["holdout_group_ids"])),
        baseline_scorecard_ids=tuple(cast(list[str], row["baseline_scorecard_ids"])),
        baseline_scorecard_hashes=tuple(cast(list[str], row["baseline_scorecard_hashes"])),
        simulator_conformance_ids=tuple(cast(list[str], row["simulator_conformance_ids"])),
        simulator_conformance_hashes=tuple(cast(list[str], row["simulator_conformance_hashes"])),
        idempotency_key=cast(str, row["idempotency_key"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
        created_at=row["created_at"],
    )
