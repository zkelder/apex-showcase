"""Research-only Strategy Lab model adapter for retained securities bars."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, localcontext

from apex.application.securities import SecuritiesModelSignalBuilder
from apex.application.strategy_lab import StrategyLabArtifactStore
from apex.application.strategy_lab_shadow import (
    DeploymentState,
    StrategyLabForwardRow,
    StrategyLabModelDeployment,
    StrategyLabShadowModelLoader,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesError,
    SecuritiesSignal,
    SecuritiesSignalDirection,
    SecuritiesStrategyKind,
    SecuritiesStrategySpecification,
)
from apex.domain.values import content_digest, deterministic_id, parse_decimal

SECURITIES_STRATEGY_LAB_FEATURE_NAMES = (
    "one_session_return",
    "lookback_return",
    "volume_ratio",
    "close_to_range",
)
SECURITIES_STRATEGY_LAB_SCHEMA_HASH = content_digest(
    {
        "schema_id": "securities-forward-v1",
        "feature_names": SECURITIES_STRATEGY_LAB_FEATURE_NAMES,
        "missingness": "REJECT",
        "ordering": "EXACT",
    }
)


class StrategyLabSecuritiesSignalBuilder(SecuritiesModelSignalBuilder):
    """Verify one ACTIVE deployment, then emit retained research signals."""

    def __init__(
        self,
        *,
        deployment: StrategyLabModelDeployment,
        artifact_store: StrategyLabArtifactStore,
        model_loader: StrategyLabShadowModelLoader,
    ) -> None:
        self._deployment = deployment
        self._artifact_store = artifact_store
        self._model_loader = model_loader

    def build_signals(
        self,
        *,
        research_run_id: str,
        specification: SecuritiesStrategySpecification,
        bars: Sequence[SecuritiesBarEvidence],
        universe: Sequence[str],
        start_date: str,
        end_date: str,
        decision_cutoff: datetime,
    ) -> tuple[SecuritiesSignal, ...]:
        if specification.kind is not SecuritiesStrategyKind.STRATEGY_LAB_MODEL:
            raise SecuritiesError("Strategy Lab adapter requires a Strategy Lab specification")
        if self._deployment.state is not DeploymentState.ACTIVE:
            raise SecuritiesError("Strategy Lab deployment is not ACTIVE")
        if specification.strategy_lab_deployment_id != self._deployment.deployment_id:
            raise SecuritiesError("strategy specification is bound to a different deployment")
        if self._deployment.feature_schema_hash != SECURITIES_STRATEGY_LAB_SCHEMA_HASH:
            raise SecuritiesError("Strategy Lab securities feature schema hash does not match")
        payload = self._artifact_store.read_bytes(self._deployment.artifact)
        verified_model = self._model_loader.verify(payload=payload, deployment=self._deployment)
        grouped: dict[str, list[SecuritiesBarEvidence]] = defaultdict(list)
        for bar in bars:
            if (
                bar.instrument_id in universe
                and bar.interval is specification.interval
                and bar.adjustment_state is specification.adjustment_state
                and start_date <= bar.period_end.date().isoformat() <= end_date
                and bar.available_at <= decision_cutoff
            ):
                grouped[bar.instrument_id].append(bar)
        feature_rows: list[StrategyLabForwardRow] = []
        source_by_row: dict[str, tuple[SecuritiesBarEvidence, ...]] = {}
        for instrument_id in universe:
            instrument_bars = sorted(grouped[instrument_id], key=lambda item: item.period_end)
            for index in range(specification.lookback_sessions, len(instrument_bars)):
                source = tuple(instrument_bars[index - specification.lookback_sessions : index + 1])
                current = source[-1]
                previous = source[-2]
                volume_average = sum((item.volume for item in source[:-1]), Decimal(0)) / Decimal(
                    len(source) - 1
                )
                range_width = current.high - current.low
                with localcontext() as context:
                    context.prec = 28
                    values = (
                        _bounded((current.close - previous.close) / previous.close),
                        _bounded((current.close - source[0].close) / source[0].close),
                        _bounded(
                            current.volume / volume_average if volume_average > 0 else Decimal(0)
                        ),
                        _bounded(
                            (current.close - current.low) / range_width
                            if range_width > 0
                            else Decimal(0)
                        ),
                    )
                row_id = deterministic_id(
                    "securities_feature",
                    {
                        "deployment_id": self._deployment.deployment_id,
                        "instrument_id": instrument_id,
                        "feature_at": current.period_end,
                        "source_bar_ids": tuple(item.bar_id for item in source),
                    },
                )
                feature_rows.append(
                    StrategyLabForwardRow(
                        row_id=row_id,
                        entity_id=instrument_id,
                        grouping_id=current.period_end.date().isoformat(),
                        feature_at=current.period_end,
                        feature_values=values,
                        baseline_probability=Decimal("0.5"),
                        positive_outcome_identity="NEXT_BAR_POSITIVE_RETURN",
                        source_lineage_hash=content_digest(
                            tuple(item.content_hash for item in source)
                        ),
                        source_configuration_hash=specification.canonical_hash,
                    )
                )
                source_by_row[row_id] = source
        predictions = verified_model.predict(feature_rows)
        if len(predictions) != len(feature_rows):
            raise SecuritiesError("Strategy Lab prediction cardinality does not match features")
        signals: list[SecuritiesSignal] = []
        for row, (_raw, calibrated) in zip(feature_rows, predictions, strict=True):
            source = source_by_row[row.row_id]
            current = source[-1]
            direction = (
                SecuritiesSignalDirection.LONG
                if calibrated > Decimal("0.5")
                else SecuritiesSignalDirection.FLAT
            )
            weight = Decimal(1) if direction is SecuritiesSignalDirection.LONG else Decimal(0)
            signal_payload = {
                "research_run_id": research_run_id,
                "specification_id": specification.specification_id,
                "instrument_id": row.entity_id,
                "signal_at": row.feature_at,
                "direction": direction,
                "target_weight": weight,
                "source_bar_ids": tuple(item.bar_id for item in source),
                "model_deployment_id": self._deployment.deployment_id,
                "model_artifact_hash": self._deployment.artifact.sha256,
                "feature_row_hash": row.canonical_hash,
            }
            signals.append(
                SecuritiesSignal(
                    signal_id=deterministic_id("securities_signal", signal_payload),
                    research_run_id=research_run_id,
                    specification_id=specification.specification_id,
                    instrument_id=row.entity_id,
                    signal_at=row.feature_at,
                    direction=direction,
                    target_weight=weight,
                    reference_close=current.close,
                    feature_start=source[0].period_start,
                    feature_end=current.period_end,
                    source_bar_ids=tuple(item.bar_id for item in source),
                    reason_codes=("STRATEGY_LAB_PROBABILITY_ABOVE_HALF",)
                    if direction is SecuritiesSignalDirection.LONG
                    else ("STRATEGY_LAB_PROBABILITY_NOT_ABOVE_HALF",),
                    model_deployment_id=self._deployment.deployment_id,
                    model_artifact_hash=self._deployment.artifact.sha256,
                    canonical_hash=content_digest(signal_payload),
                )
            )
        return tuple(signals)


def _bounded(value: Decimal) -> Decimal:
    return parse_decimal(
        value.quantize(Decimal("0.00000001")),
        allow_negative=True,
    )
