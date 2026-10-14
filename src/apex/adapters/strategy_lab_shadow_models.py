"""Trusted, managed Strategy Lab artifact verification and inference adapter."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast

import numpy as np
from joblib import load as joblib_load  # type: ignore[import-untyped]

from apex.application.strategy_lab import ModelFamily, StrategyLabError, runtime_identity
from apex.application.strategy_lab_shadow import (
    StrategyLabForwardRow,
    StrategyLabModelDeployment,
)
from apex.domain.values import parse_decimal


def _decimal(value: float) -> Decimal:
    return parse_decimal(format(Decimal(str(value)).quantize(Decimal("0.00000001")), "f"))


class VerifiedStrategyLabModel:
    """Loads only bytes verified by the managed artifact store upstream."""

    def __init__(self, *, payload: bytes, deployment: StrategyLabModelDeployment) -> None:
        self._family = deployment.model_family
        self._feature_names: tuple[str, ...]
        self._model: Any
        self._calibrator: Any | None = None
        if deployment.artifact.relative_location.endswith(".json"):
            value = json.loads(payload.decode("utf-8"))
            if (
                value.get("artifact_type") != "APEX_STRATEGY_LAB_MODEL_V1"
                or value.get("model_family") != deployment.model_family.value
            ):
                raise StrategyLabError("UNTRUSTED_MODEL_ARTIFACT")
            self._verify_metadata(value, deployment)
            self._feature_names = tuple(value["feature_names"])
            self._model = (
                tuple(float(x) for x in value["coefficients"]),
                float(value["intercept"]),
            )
            parameters = value.get("calibration_parameters")
            self._calibrator = (
                None
                if parameters is None
                else (float(parameters["coefficients"][0]), float(parameters["intercept"]))
            )
        else:
            value = joblib_load(io.BytesIO(payload))
            if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
                raise StrategyLabError("UNTRUSTED_MODEL_ARTIFACT")
            self._verify_metadata(value["metadata"], deployment)
            self._feature_names = tuple(value["metadata"]["feature_names"])
            self._model = value.get("model")
            self._calibrator = value.get("calibrator")
            if self._model is None:
                raise StrategyLabError("UNTRUSTED_MODEL_ARTIFACT")

    def _verify_metadata(
        self, value: dict[str, Any], deployment: StrategyLabModelDeployment
    ) -> None:
        feature_names = cast(Sequence[str], deployment.canonical_payload["feature_names"])
        if tuple(value.get("feature_names", ())) != tuple(feature_names):
            raise StrategyLabError("FEATURE_SCHEMA_MISMATCH")
        runtime = value.get("runtime")
        if not isinstance(runtime, dict) or runtime != runtime_identity():
            raise StrategyLabError("UNSUPPORTED_MODEL_RUNTIME")

    def predict(self, rows: Sequence[StrategyLabForwardRow]) -> tuple[tuple[Decimal, Decimal], ...]:
        if any(
            len(row.feature_values) != len(self._feature_names)
            or any(value is None for value in row.feature_values)
            for row in rows
        ):
            raise StrategyLabError("FORWARD_FEATURE_SCHEMA_MISMATCH")
        matrix = np.asarray(
            [[float(cast(Decimal, value)) for value in row.feature_values] for row in rows],
            dtype=np.float64,
        )
        if self._family is ModelFamily.LOGISTIC_REGRESSION:
            coefficients, intercept = self._model
            raw = 1.0 / (1.0 + np.exp(-(matrix @ np.asarray(coefficients) + intercept)))
            calibrated = (
                raw
                if self._calibrator is None
                else 1.0 / (1.0 + np.exp(-(raw * self._calibrator[0] + self._calibrator[1])))
            )
        else:
            raw = self._model.predict_proba(matrix)[:, 1]
            calibrated = raw if self._calibrator is None else self._calibrator.predict(raw)
        return tuple(
            (_decimal(float(raw_value)), _decimal(float(calibrated_value)))
            for raw_value, calibrated_value in zip(raw, calibrated, strict=True)
        )


class StrategyLabManagedModelLoader:
    """Adapter implementation of the application's trusted-loader port."""

    def verify(
        self, *, payload: bytes, deployment: StrategyLabModelDeployment
    ) -> VerifiedStrategyLabModel:
        return VerifiedStrategyLabModel(payload=payload, deployment=deployment)
