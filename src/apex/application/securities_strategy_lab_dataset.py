"""Immutable, no-lookahead Securities daily-direction dataset construction.

This research-only boundary joins retained RAW bars to a separately retained
total-return outcome batch.  It deliberately has no campaign, Order, Position,
ledger, provider, broker, PAPER, or LIVE dependency.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from itertools import pairwise
from typing import Protocol

from apex.application.strategy_lab import (
    GroupedWalkForwardPolicy,
    StrategyLabArtifact,
    StrategyLabArtifactStore,
    StrategyLabFeatureAdapter,
    StrategyLabFeatureDefinition,
    StrategyLabFeatureSchema,
    StrategyLabFold,
    StrategyLabLabelDefinition,
    StrategyLabRow,
    StrategyLabService,
)
from apex.domain.real_securities_data import (
    SECURITIES_DAILY_DIRECTION_DATASET_V1,
    SecuritiesUniverseSnapshot,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id

FEATURE_POLICY_ID = "SECURITIES_DAILY_DIRECTION_RAW_FEATURES_V1"
LABEL_POLICY_ID = "SECURITIES_DAILY_DIRECTION_TOTAL_RETURN_LABELS_V1"
FOLD_POLICY_ID = "SECURITIES_DAILY_DIRECTION_GROUPED_HOLDOUT_V1"
HOLDOUT_FRACTION = Decimal("0.20")
_QUANTUM = Decimal("0.00000001")
_SCHEMA_CREATED_AT = datetime(2026, 7, 28, tzinfo=UTC)


class SecuritiesDirectionDatasetError(RuntimeError):
    """A genuine-evidence dataset cannot be constructed safely."""


class SecuritiesDatasetEvidencePort(Protocol):
    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch: ...

    def get_dataset(
        self, batch_id: str
    ) -> tuple[
        tuple[SecuritiesInstrument, ...],
        tuple[SecuritiesCalendarSession, ...],
        tuple[SecuritiesBarEvidence, ...],
        tuple[object, ...],
    ]: ...


class SecuritiesDirectionDatasetRepository(Protocol):
    def dataset_by_idempotency(self, key: str) -> SecuritiesDailyDirectionDataset | None: ...

    def save_dataset(
        self, value: SecuritiesDailyDirectionDataset
    ) -> SecuritiesDailyDirectionDataset: ...

    def dataset(self, dataset_id: str) -> SecuritiesDailyDirectionDataset | None: ...

    def datasets(self) -> tuple[SecuritiesDailyDirectionDataset, ...]: ...


class InMemorySecuritiesDirectionDatasetRepository:
    """Test-only immutable metadata store with no financial state."""

    def __init__(self) -> None:
        self._items: dict[str, SecuritiesDailyDirectionDataset] = {}

    def dataset_by_idempotency(self, key: str) -> SecuritiesDailyDirectionDataset | None:
        return next((item for item in self._items.values() if item.idempotency_key == key), None)

    def save_dataset(
        self, value: SecuritiesDailyDirectionDataset
    ) -> SecuritiesDailyDirectionDataset:
        prior = self._items.get(value.dataset_id)
        if prior is not None and prior.canonical_result_hash != value.canonical_result_hash:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_IMMUTABLE")
        self._items[value.dataset_id] = value
        return value

    def dataset(self, dataset_id: str) -> SecuritiesDailyDirectionDataset | None:
        return self._items.get(dataset_id)

    def datasets(self) -> tuple[SecuritiesDailyDirectionDataset, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: item.created_at))


@dataclass(frozen=True, slots=True)
class SecuritiesDailyDirectionFeatureRow:
    row_id: str
    instrument_id: str
    decision_session_id: str
    decision_session_date: str
    availability_at: datetime
    raw_bar_ids: tuple[str, ...]
    raw_bar_hashes: tuple[str, ...]
    values: tuple[Decimal, ...]
    calculation_policy_id: str
    source_lineage_hash: str
    canonical_row_hash: str

    def artifact_payload(self, feature_names: Sequence[str]) -> dict[str, object]:
        return {
            "availability_at": self.availability_at,
            "calculation_policy_id": self.calculation_policy_id,
            "canonical_row_hash": self.canonical_row_hash,
            "decision_session_date": self.decision_session_date,
            "decision_session_id": self.decision_session_id,
            "feature_values": dict(zip(feature_names, self.values, strict=True)),
            "instrument_id": self.instrument_id,
            "raw_bar_hashes": self.raw_bar_hashes,
            "raw_bar_ids": self.raw_bar_ids,
            "row_id": self.row_id,
            "source_lineage_hash": self.source_lineage_hash,
        }


@dataclass(frozen=True, slots=True)
class SecuritiesDailyDirectionLabelRow:
    label_id: str
    instrument_id: str
    decision_session_id: str
    decision_session_date: str
    outcome_session_id: str
    outcome_session_date: str
    derived_bar_ids: tuple[str, str]
    derived_bar_hashes: tuple[str, str]
    realized_total_return: Decimal
    label: int
    availability_at: datetime
    derivation_policy_hash: str
    canonical_label_hash: str

    def artifact_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SecuritiesDailyDirectionDataset:
    dataset_id: str
    strategy_lab_dataset_id: str
    raw_batch_id: str
    raw_batch_hash: str
    derived_batch_id: str
    derived_batch_hash: str
    universe_id: str
    universe_hash: str
    calendar_identity: str
    calendar_version: str
    feature_schema_hash: str
    label_definition_hash: str
    cutoff_at: datetime
    start_date: str
    end_date: str
    row_count: int
    group_count: int
    instrument_count: int
    positive_label_count: int
    negative_label_count: int
    exclusion_counts: tuple[tuple[str, int], ...]
    feature_rows_artifact: StrategyLabArtifact
    labels_artifact: StrategyLabArtifact
    manifest_artifact: StrategyLabArtifact
    folds_artifact: StrategyLabArtifact
    holdout_artifact: StrategyLabArtifact
    returns_artifact: StrategyLabArtifact
    folds: tuple[StrategyLabFold, ...]
    holdout_group_ids: tuple[str, ...]
    baseline_scorecard_ids: tuple[str, ...]
    baseline_scorecard_hashes: tuple[str, ...]
    simulator_conformance_ids: tuple[str, ...]
    simulator_conformance_hashes: tuple[str, ...]
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str
    created_at: datetime


def securities_daily_direction_schema() -> StrategyLabFeatureSchema:
    names = (
        "raw_return_1_session",
        "raw_return_5_session",
        "raw_return_20_session",
        "raw_return_60_session",
        "raw_volatility_20_session",
        "raw_volatility_60_session",
        "raw_drawdown_20_session",
        "raw_market_relative_return_20_session",
        "raw_cross_sectional_return_rank_20_session",
        "session_weekday",
        "session_month",
        "session_day_of_month",
        "session_is_early_close",
        "raw_volume_positive",
    )
    return StrategyLabFeatureSchema(
        schema_id=SECURITIES_DAILY_DIRECTION_DATASET_V1,
        vertical="SECURITIES",
        research_family="DAILY_DIRECTION",
        version="V1",
        features=tuple(
            StrategyLabFeatureDefinition(
                name=name,
                data_type="DECIMAL",
                nullable=False,
                normalization_identity="NONE_DECLARED_RAW_DECIMAL_V1",
                source_availability_requirement=(
                    "RAW_DAILY_AVAILABLE_AT_LE_DECISION_CUTOFF_V1"
                    if name.startswith("raw_")
                    else "RETAINED_XNYS_SESSION_EVIDENCE_V1"
                ),
            )
            for name in names
        ),
        created_at=_SCHEMA_CREATED_AT,
    )


def securities_daily_direction_label() -> StrategyLabLabelDefinition:
    return StrategyLabLabelDefinition(
        label_id="SECURITIES_DAILY_DIRECTION_TOTAL_RETURN_5_SESSION_V1",
        vertical="SECURITIES",
        research_family="DAILY_DIRECTION",
        version="V1",
        target_type="BINARY_PROBABILITY",
        positive_semantics="APEX_DERIVED_TOTAL_RETURN_OVER_NEXT_FIVE_ELIGIBLE_XNYS_SESSIONS_GT_ZERO",
        negative_semantics="APEX_DERIVED_TOTAL_RETURN_OVER_NEXT_FIVE_ELIGIBLE_XNYS_SESSIONS_LE_ZERO",
        resolution_rule="TOTAL_RETURN_CLOSE_T_PLUS_5_DIV_TOTAL_RETURN_CLOSE_T_MINUS_ONE",
        feature_cutoff_rule="RAW_EVIDENCE_AVAILABLE_AT_OR_BEFORE_DECISION_CUTOFF_ONLY",
        availability_rule="LABEL_AVAILABLE_AT_OUTCOME_DERIVED_BAR_AVAILABILITY",
        created_at=_SCHEMA_CREATED_AT,
    )


class GenuineSecuritiesDailyDirectionAdapter(StrategyLabFeatureAdapter):
    """A deterministic adapter over one RAW and one derived evidence batch."""

    adapter_id = "genuine-securities-daily-direction-v1"
    adapter_version = "1"
    schema = securities_daily_direction_schema()
    label_definition = securities_daily_direction_label()

    def __init__(
        self,
        *,
        raw_batch: SecuritiesMarketDataBatch,
        derived_batch: SecuritiesMarketDataBatch,
        universe: SecuritiesUniverseSnapshot,
        raw: tuple[
            tuple[SecuritiesInstrument, ...],
            tuple[SecuritiesCalendarSession, ...],
            tuple[SecuritiesBarEvidence, ...],
        ],
        derived: tuple[
            tuple[SecuritiesInstrument, ...],
            tuple[SecuritiesCalendarSession, ...],
            tuple[SecuritiesBarEvidence, ...],
        ],
    ) -> None:
        self._raw_batch = raw_batch
        self._derived_batch = derived_batch
        self._universe = universe
        self._raw = raw
        self._derived = derived
        self._built: (
            tuple[
                tuple[StrategyLabRow, ...],
                tuple[SecuritiesDailyDirectionFeatureRow, ...],
                tuple[SecuritiesDailyDirectionLabelRow, ...],
                Counter[str],
            ]
            | None
        ) = None

    def source_identity(self, *, cutoff_at: datetime, source_scope: str | None = None) -> str:
        if source_scope is not None:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_SOURCE_SCOPE_UNSUPPORTED")
        return content_digest(
            {
                "raw_batch": self._raw_batch.canonical_hash,
                "derived_batch": self._derived_batch.canonical_hash,
                "universe": self._universe.canonical_result_hash,
                "calendar": self._raw[1][0].content_hash if self._raw[1] else None,
                "feature_policy": FEATURE_POLICY_ID,
                "label_policy": LABEL_POLICY_ID,
                "cutoff_at": cutoff_at,
            }
        )

    def rows(
        self, *, cutoff_at: datetime, source_scope: str | None = None
    ) -> tuple[StrategyLabRow, ...]:
        if source_scope is not None:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_SOURCE_SCOPE_UNSUPPORTED")
        if cutoff_at.tzinfo is None:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_CUTOFF_TIMEZONE_REQUIRED")
        if self._built is None:
            self._built = self._build(cutoff_at=cutoff_at)
        return self._built[0]

    @property
    def detailed_rows(self) -> tuple[SecuritiesDailyDirectionFeatureRow, ...]:
        if self._built is None:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_ROWS_NOT_BUILT")
        return self._built[1]

    @property
    def labels(self) -> tuple[SecuritiesDailyDirectionLabelRow, ...]:
        if self._built is None:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_ROWS_NOT_BUILT")
        return self._built[2]

    @property
    def exclusions(self) -> Counter[str]:
        if self._built is None:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_ROWS_NOT_BUILT")
        return self._built[3]

    def _build(
        self, *, cutoff_at: datetime
    ) -> tuple[
        tuple[StrategyLabRow, ...],
        tuple[SecuritiesDailyDirectionFeatureRow, ...],
        tuple[SecuritiesDailyDirectionLabelRow, ...],
        Counter[str],
    ]:
        instruments, sessions, raw_bars = self._raw
        derived_instruments, derived_sessions, derived_bars = self._derived
        self._validate_scope(
            instruments, sessions, raw_bars, derived_instruments, derived_sessions, derived_bars
        )
        ordered_sessions = tuple(
            sorted(sessions, key=lambda item: (item.opens_at, item.session_id))
        )
        raw_by_key = _exact_bars(raw_bars, SecuritiesAdjustmentState.RAW)
        derived_by_key = _exact_bars(derived_bars, SecuritiesAdjustmentState.TOTAL_RETURN_ADJUSTED)
        instrument_ids = tuple(
            item.instrument_id for item in sorted(instruments, key=lambda item: item.instrument_id)
        )
        rows: list[StrategyLabRow] = []
        features: list[SecuritiesDailyDirectionFeatureRow] = []
        labels: list[SecuritiesDailyDirectionLabelRow] = []
        exclusions: Counter[str] = Counter()
        for ordinal, session in enumerate(ordered_sessions):
            if ordinal < 60:
                exclusions["REQUIRED_LOOKBACK_UNAVAILABLE"] += len(instrument_ids)
                continue
            if ordinal + 5 >= len(ordered_sessions):
                exclusions["FIVE_FUTURE_ELIGIBLE_SESSIONS_UNAVAILABLE"] += len(instrument_ids)
                continue
            window = ordered_sessions[ordinal - 60 : ordinal + 1]
            outcome = ordered_sessions[ordinal + 5]
            raw_window = {
                instrument_id: tuple(
                    raw_by_key.get((instrument_id, item.session_date)) for item in window
                )
                for instrument_id in instrument_ids
            }
            if any(any(item is None for item in series) for series in raw_window.values()):
                exclusions["REQUIRED_UNIVERSE_COVERAGE_INCOMPLETE"] += len(instrument_ids)
                continue
            if any(
                (instrument_id, session.session_date) not in derived_by_key
                or (instrument_id, outcome.session_date) not in derived_by_key
                for instrument_id in instrument_ids
            ):
                exclusions["DERIVED_LABEL_SOURCE_UNAVAILABLE"] += len(instrument_ids)
                continue
            closes = {
                instrument_id: tuple(
                    item.close for item in raw_window[instrument_id] if item is not None
                )
                for instrument_id in instrument_ids
            }
            returns20 = {
                instrument_id: _return(closes[instrument_id][-1], closes[instrument_id][-21])
                for instrument_id in instrument_ids
            }
            ranks = _ranks(returns20)
            market_return = _mean(returns20.values())
            for instrument_id in instrument_ids:
                source_bars = tuple(item for item in raw_window[instrument_id] if item is not None)
                availability = max(item.available_at for item in source_bars)
                if availability.astimezone(UTC) > cutoff_at.astimezone(UTC):
                    exclusions["FEATURE_EVIDENCE_UNAVAILABLE_AT_CUTOFF"] += 1
                    continue
                current_derived = derived_by_key[(instrument_id, session.session_date)]
                outcome_derived = derived_by_key[(instrument_id, outcome.session_date)]
                if current_derived.available_at >= outcome_derived.available_at:
                    exclusions["LABEL_AVAILABILITY_NOT_AFTER_DECISION"] += 1
                    continue
                values = (
                    _return(closes[instrument_id][-1], closes[instrument_id][-2]),
                    _return(closes[instrument_id][-1], closes[instrument_id][-6]),
                    returns20[instrument_id],
                    _return(closes[instrument_id][-1], closes[instrument_id][0]),
                    _volatility(closes[instrument_id][-21:]),
                    _volatility(closes[instrument_id]),
                    _drawdown(closes[instrument_id][-20:]),
                    _q(returns20[instrument_id] - market_return),
                    ranks[instrument_id],
                    Decimal(session.opens_at.weekday()),
                    Decimal(session.opens_at.month),
                    Decimal(session.opens_at.day),
                    Decimal(1 if session.is_early_close else 0),
                    Decimal(1 if source_bars[-1].volume > 0 else 0),
                )
                lineage = content_digest(
                    {
                        "raw_batch_hash": self._raw_batch.canonical_hash,
                        "universe_hash": self._universe.canonical_result_hash,
                        "session_hash": session.content_hash,
                        "raw_bar_hashes": tuple(item.content_hash for item in source_bars),
                        "policy": FEATURE_POLICY_ID,
                    }
                )
                feature_payload = {
                    "instrument_id": instrument_id,
                    "decision_session_id": session.session_id,
                    "availability_at": availability,
                    "raw_bar_ids": tuple(item.bar_id for item in source_bars),
                    "values": values,
                    "policy": FEATURE_POLICY_ID,
                    "lineage": lineage,
                }
                row_id = deterministic_id("securities_daily_direction_feature", feature_payload)
                feature = SecuritiesDailyDirectionFeatureRow(
                    row_id=row_id,
                    instrument_id=instrument_id,
                    decision_session_id=session.session_id,
                    decision_session_date=session.session_date,
                    availability_at=availability,
                    raw_bar_ids=tuple(item.bar_id for item in source_bars),
                    raw_bar_hashes=tuple(item.content_hash for item in source_bars),
                    values=values,
                    calculation_policy_id=FEATURE_POLICY_ID,
                    source_lineage_hash=lineage,
                    canonical_row_hash=content_digest(feature_payload),
                )
                realized = _return(outcome_derived.close, current_derived.close)
                label_payload = {
                    "instrument_id": instrument_id,
                    "decision_session_id": session.session_id,
                    "outcome_session_id": outcome.session_id,
                    "derived_bar_hashes": (
                        current_derived.content_hash,
                        outcome_derived.content_hash,
                    ),
                    "realized_total_return": realized,
                    "policy_hash": self._derived_batch.canonical_hash,
                }
                label = SecuritiesDailyDirectionLabelRow(
                    label_id=deterministic_id("securities_daily_direction_label", label_payload),
                    instrument_id=instrument_id,
                    decision_session_id=session.session_id,
                    decision_session_date=session.session_date,
                    outcome_session_id=outcome.session_id,
                    outcome_session_date=outcome.session_date,
                    derived_bar_ids=(current_derived.bar_id, outcome_derived.bar_id),
                    derived_bar_hashes=(current_derived.content_hash, outcome_derived.content_hash),
                    realized_total_return=realized,
                    label=1 if realized > 0 else 0,
                    availability_at=outcome_derived.available_at,
                    derivation_policy_hash=self._derived_batch.canonical_hash,
                    canonical_label_hash=content_digest(label_payload),
                )
                labels.append(label)
                features.append(feature)
                rows.append(
                    StrategyLabRow(
                        row_id=row_id,
                        entity_id=instrument_id,
                        grouping_id=session.session_id,
                        feature_at=availability,
                        label_available_at=label.availability_at,
                        feature_values=values,
                        label=label.label,
                        baseline_probability=None,
                        entry_price=None,
                        fee_cost=None,
                        source_lineage_hash=lineage,
                        source_configuration_hash=self.source_identity(cutoff_at=cutoff_at),
                    )
                )
        if not rows:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_NO_ELIGIBLE_ROWS")
        return tuple(rows), tuple(features), tuple(labels), exclusions

    def _validate_scope(
        self,
        instruments: Sequence[SecuritiesInstrument],
        sessions: Sequence[SecuritiesCalendarSession],
        raw_bars: Sequence[SecuritiesBarEvidence],
        derived_instruments: Sequence[SecuritiesInstrument],
        derived_sessions: Sequence[SecuritiesCalendarSession],
        derived_bars: Sequence[SecuritiesBarEvidence],
    ) -> None:
        if self._raw_batch.state.value != "READY" or self._derived_batch.state.value != "READY":
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_READY_BATCHES_REQUIRED")
        if not sessions or not instruments:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_RAW_SCOPE_EMPTY")
        if {item.symbol for item in instruments} != set(self._universe.symbols):
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_FIXED_UNIVERSE_MISMATCH")
        if {item.instrument_id for item in instruments} != {
            item.instrument_id for item in derived_instruments
        }:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_DERIVED_INSTRUMENT_MISMATCH")
        raw_calendar = {(item.calendar_identity, item.calendar_version) for item in sessions}
        derived_calendar = {
            (item.calendar_identity, item.calendar_version) for item in derived_sessions
        }
        if len(raw_calendar) != 1 or raw_calendar != derived_calendar:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_EXACT_XNYS_CALENDAR_REQUIRED")
        if any(item.calendar_identity != "XNYS" for item in sessions):
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_XNYS_REQUIRED")
        if any(
            item.interval is not SecuritiesBarInterval.ONE_DAY
            for item in (*raw_bars, *derived_bars)
        ):
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_DAILY_BARS_REQUIRED")


def _exact_bars(
    bars: Sequence[SecuritiesBarEvidence], adjustment: SecuritiesAdjustmentState
) -> dict[tuple[str, str], SecuritiesBarEvidence]:
    values: dict[tuple[str, str], SecuritiesBarEvidence] = {}
    for bar in bars:
        if bar.adjustment_state is not adjustment:
            continue
        key = (bar.instrument_id, bar.period_end.date().isoformat())
        if key in values:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_AMBIGUOUS_INSTRUMENT_SESSION")
        values[key] = bar
    return values


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM)


def _return(current: Decimal, prior: Decimal) -> Decimal:
    if prior <= 0:
        raise SecuritiesDirectionDatasetError("GENUINE_DATASET_NONPOSITIVE_RAW_CLOSE")
    return _q(current / prior - Decimal(1))


def _mean(values: Iterable[Decimal]) -> Decimal:
    values = tuple(values)
    if not values:
        raise SecuritiesDirectionDatasetError("GENUINE_DATASET_MEAN_REQUIRES_VALUES")
    return _q(sum(values, Decimal()) / Decimal(len(values)))


def _volatility(closes: Sequence[Decimal]) -> Decimal:
    returns = tuple(_return(current, prior) for prior, current in pairwise(closes))
    mean = _mean(returns)
    with localcontext() as context:
        context.prec = 48
        variance = sum(((value - mean) ** 2 for value in returns), Decimal()) / Decimal(
            len(returns)
        )
        return _q(variance.sqrt())


def _drawdown(closes: Sequence[Decimal]) -> Decimal:
    peak = max(closes)
    return _q(closes[-1] / peak - Decimal(1))


def _ranks(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    result: dict[str, Decimal] = {}
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = _q(Decimal(index + end - 1) / Decimal(2 * max(1, len(ordered) - 1)))
        for instrument_id, _value in ordered[index:end]:
            result[instrument_id] = rank
        index = end
    return result


class BuildGenuineSecuritiesDailyDirectionDataset:
    """Write one immutable genuine dataset and its inspection artifacts."""

    def __init__(
        self,
        *,
        evidence: SecuritiesDatasetEvidencePort,
        strategy_service: StrategyLabService,
        artifacts: StrategyLabArtifactStore,
        repository: SecuritiesDirectionDatasetRepository,
    ) -> None:
        self._evidence = evidence
        self._strategy_service = strategy_service
        self._artifacts = artifacts
        self._repository = repository

    def execute(
        self,
        *,
        raw_batch_id: str,
        derived_batch_id: str,
        universe: SecuritiesUniverseSnapshot,
        cutoff_at: datetime,
        idempotency_key: str,
        baseline_scorecards: Sequence[object] = (),
        simulator_reports: Sequence[object] = (),
    ) -> SecuritiesDailyDirectionDataset:
        if cutoff_at.tzinfo is None or not idempotency_key:
            raise SecuritiesDirectionDatasetError("GENUINE_DATASET_INPUT_INVALID")
        raw_batch = self._evidence.get_batch(raw_batch_id)
        derived_batch = self._evidence.get_batch(derived_batch_id)
        input_payload = {
            "policy": SECURITIES_DAILY_DIRECTION_DATASET_V1,
            "raw_batch": raw_batch.canonical_hash,
            "derived_batch": derived_batch.canonical_hash,
            "universe": universe.canonical_result_hash,
            "cutoff_at": cutoff_at,
            "feature_schema": securities_daily_direction_schema().canonical_hash,
            "label": securities_daily_direction_label().canonical_hash,
            "baseline_scorecard_hashes": tuple(_hash_of(item) for item in baseline_scorecards),
            "simulator_report_hashes": tuple(_hash_of(item) for item in simulator_reports),
            "key": idempotency_key,
        }
        input_hash = content_digest(input_payload)
        prior = self._repository.dataset_by_idempotency(idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise SecuritiesDirectionDatasetError("GENUINE_DATASET_IDEMPOTENCY_CONFLICT")
            return prior
        raw_instruments, raw_sessions, raw_bars, _raw_actions = self._evidence.get_dataset(
            raw_batch_id
        )
        derived_instruments, derived_sessions, derived_bars, _derived_actions = (
            self._evidence.get_dataset(derived_batch_id)
        )
        adapter = GenuineSecuritiesDailyDirectionAdapter(
            raw_batch=raw_batch,
            derived_batch=derived_batch,
            universe=universe,
            raw=(raw_instruments, raw_sessions, raw_bars),
            derived=(derived_instruments, derived_sessions, derived_bars),
        )
        snapshot = self._strategy_service.build_dataset(
            adapter=adapter,
            name="Genuine Securities Daily Direction Dataset v0.1",
            cutoff_at=cutoff_at,
            idempotency_key=idempotency_key,
        )
        rows = adapter.rows(cutoff_at=cutoff_at)
        features = adapter.detailed_rows
        labels = adapter.labels
        ordered_groups = tuple(
            session.session_id
            for session in sorted(raw_sessions, key=lambda item: (item.opens_at, item.session_id))
            if any(row.grouping_id == session.session_id for row in rows)
        )
        holdout_count = max(4, int(Decimal(len(ordered_groups)) * HOLDOUT_FRACTION))
        holdout_group_ids = ordered_groups[-holdout_count:]
        fold_rows = tuple(row for row in rows if row.grouping_id not in set(holdout_group_ids))
        folds = GroupedWalkForwardPolicy().build_folds(fold_rows)
        if len(folds) != 3:
            raise SecuritiesDirectionDatasetError(
                "GENUINE_DATASET_INSUFFICIENT_WALK_FORWARD_GROUPS"
            )
        base = content_digest({"dataset": snapshot.dataset_id, "input": input_hash})
        feature_artifact = _write_jsonl(
            self._artifacts,
            f"genuine-securities/{base}/feature-rows.jsonl",
            (item.artifact_payload(adapter.schema.feature_names) for item in features),
        )
        labels_artifact = _write_jsonl(
            self._artifacts,
            f"genuine-securities/{base}/labels.jsonl",
            (item.artifact_payload() for item in labels),
        )
        folds_artifact = _write_json(
            self._artifacts,
            f"genuine-securities/{base}/folds.json",
            {
                "policy": FOLD_POLICY_ID,
                "calibration_evidence": "VALIDATION_PARTITION_ONLY_V1",
                "folds": tuple(asdict(item) for item in folds),
            },
        )
        holdout_artifact = _write_json(
            self._artifacts,
            f"genuine-securities/{base}/holdout.json",
            {"policy": FOLD_POLICY_ID, "untouched_group_ids": holdout_group_ids},
        )
        returns_artifact = _write_jsonl(
            self._artifacts,
            f"genuine-securities/{base}/returns.jsonl",
            (
                {
                    "instrument_id": item.instrument_id,
                    "decision_session_id": item.decision_session_id,
                    "outcome_session_id": item.outcome_session_id,
                    "realized_total_return": item.realized_total_return,
                    "label_hash": item.canonical_label_hash,
                }
                for item in labels
            ),
        )
        manifest = {
            "dataset_policy": SECURITIES_DAILY_DIRECTION_DATASET_V1,
            "strategy_lab_dataset_id": snapshot.dataset_id,
            "raw_batch": {"id": raw_batch.batch_id, "hash": raw_batch.canonical_hash},
            "derived_batch": {"id": derived_batch.batch_id, "hash": derived_batch.canonical_hash},
            "universe": {"id": universe.universe_id, "hash": universe.canonical_result_hash},
            "calendar": {
                "identity": raw_sessions[0].calendar_identity,
                "version": raw_sessions[0].calendar_version,
            },
            "cutoff_at": cutoff_at,
            "rows": len(rows),
            "groups": len(ordered_groups),
            "instruments": len({row.entity_id for row in rows}),
            "label_balance": {
                "positive": sum(row.label for row in rows),
                "negative": sum(1 - row.label for row in rows),
            },
            "exclusions": tuple(sorted(adapter.exclusions.items())),
            "feature_schema_hash": adapter.schema.canonical_hash,
            "label_definition_hash": adapter.label_definition.canonical_hash,
            "baseline_scorecard_ids": tuple(
                _id_of(item, "scorecard_id") for item in baseline_scorecards
            ),
            "simulator_conformance_ids": tuple(
                _id_of(item, "report_id") for item in simulator_reports
            ),
            "artifacts": {
                "feature_rows": asdict(feature_artifact),
                "labels": asdict(labels_artifact),
                "folds": asdict(folds_artifact),
                "holdout": asdict(holdout_artifact),
                "returns": asdict(returns_artifact),
            },
            "no_lookahead": (
                "RAW_FEATURES_AVAILABLE_BY_DECISION_ONLY; "
                "DERIVED_TOTAL_RETURN_LABELS_AFTER_OUTCOME_ONLY"
            ),
        }
        manifest_artifact = _write_json(
            self._artifacts, f"genuine-securities/{base}/manifest.json", manifest
        )
        result_hash = content_digest({"input": input_hash, "manifest": manifest_artifact.sha256})
        dataset = SecuritiesDailyDirectionDataset(
            dataset_id=deterministic_id("securities_daily_direction_dataset", input_payload),
            strategy_lab_dataset_id=snapshot.dataset_id,
            raw_batch_id=raw_batch.batch_id,
            raw_batch_hash=raw_batch.canonical_hash,
            derived_batch_id=derived_batch.batch_id,
            derived_batch_hash=derived_batch.canonical_hash,
            universe_id=universe.universe_id,
            universe_hash=universe.canonical_result_hash,
            calendar_identity=raw_sessions[0].calendar_identity,
            calendar_version=raw_sessions[0].calendar_version,
            feature_schema_hash=adapter.schema.canonical_hash,
            label_definition_hash=adapter.label_definition.canonical_hash,
            cutoff_at=cutoff_at,
            start_date=min(item.decision_session_date for item in features),
            end_date=max(item.decision_session_date for item in features),
            row_count=len(rows),
            group_count=len(ordered_groups),
            instrument_count=len({row.entity_id for row in rows}),
            positive_label_count=sum(row.label for row in rows),
            negative_label_count=sum(1 - row.label for row in rows),
            exclusion_counts=tuple(sorted(adapter.exclusions.items())),
            feature_rows_artifact=feature_artifact,
            labels_artifact=labels_artifact,
            manifest_artifact=manifest_artifact,
            folds_artifact=folds_artifact,
            holdout_artifact=holdout_artifact,
            returns_artifact=returns_artifact,
            folds=folds,
            holdout_group_ids=holdout_group_ids,
            baseline_scorecard_ids=tuple(
                _id_of(item, "scorecard_id") for item in baseline_scorecards
            ),
            baseline_scorecard_hashes=tuple(_hash_of(item) for item in baseline_scorecards),
            simulator_conformance_ids=tuple(
                _id_of(item, "report_id") for item in simulator_reports
            ),
            simulator_conformance_hashes=tuple(_hash_of(item) for item in simulator_reports),
            idempotency_key=idempotency_key,
            canonical_input_hash=input_hash,
            canonical_result_hash=result_hash,
            created_at=datetime.now(tz=UTC),
        )
        return self._repository.save_dataset(dataset)


def _write_json(
    store: StrategyLabArtifactStore, location: str, value: object
) -> StrategyLabArtifact:
    return store.write_bytes(location, canonical_json_bytes(value))


def _write_jsonl(
    store: StrategyLabArtifactStore, location: str, values: Iterable[object]
) -> StrategyLabArtifact:
    return store.write_bytes(
        location,
        b"".join(canonical_json_bytes(value) + b"\n" for value in values),
    )


def _id_of(value: object, name: str) -> str:
    result = getattr(value, name, None)
    if not isinstance(result, str):
        raise SecuritiesDirectionDatasetError("GENUINE_DATASET_LINK_ID_INVALID")
    return result


def _hash_of(value: object) -> str:
    result = getattr(value, "canonical_result_hash", None)
    if not isinstance(result, str) or len(result) != 64:
        raise SecuritiesDirectionDatasetError("GENUINE_DATASET_LINK_HASH_INVALID")
    return result
