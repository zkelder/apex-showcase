"""Application-layer repository contracts, in-memory reference
implementations, and orchestration services for External Strategy
Evaluation v1.

Three deliberately small, explicit services, mirroring the "one action, one
purpose" idiom already used throughout Apex (e.g.
``application.kalshi_resolution``): ``RegisterExternalStrategy`` (parse +
persist a declarative specification, never evaluates), ``EvaluateExternalStrategy``
(fetch retained evidence, run the EXISTING deterministic backtest/evaluation/
benchmark-comparison machinery unchanged, persist the resulting immutable
record -- never promotes), and ``PromoteExternalStrategy`` (the one explicit,
operator-invoked action that can move a definition from ``RESEARCH_ONLY`` to
``PAPER_ELIGIBLE`` -- never called by the other two, never called
automatically by anything in this module).

None of these three services constructs, imports, or calls
``application.strategy_paper_trial.ExecuteStrategyPaperTrial`` -- the only
code in this codebase that can place a PAPER order (see
``domain.external_strategy``'s module docstring). Reaching
``PAPER_ELIGIBLE`` through ``PromoteExternalStrategy`` changes a definition's
own ``lifecycle_state`` and nothing else: it does not authorize, register,
or make this strategy visible to the separate, live
``STRATEGY_PAPER_TRIAL_EXECUTION`` recurring-cycle task, whose own candidate
set (``domain.strategy_benchmark.canonical_strategy_benchmarks``) is a fixed,
hardcoded catalog this module never reads from or writes to.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.application.crypto_research import CryptoResearchRepository
from apex.application.strategy_version_provenance import (
    RecordEvaluationSubject,
    RecordStrategyVersionTransition,
    StrategyVersionProvenanceRepository,
    StrategyVersionProvenanceResearchError,
)
from apex.domain.external_strategy import (
    EXTERNAL_STRATEGY_WARNING,
    ExternalStrategyEvaluationError,
    ExternalStrategyEvaluationRecord,
    build_external_strategy_evaluation_record,
    ensure_capture_eligible,
    point_in_time_candle_window,
    strategy_definition_from_specification,
)
from apex.domain.strategy_benchmark import (
    StrategyBenchmarkError,
    canonical_strategy_benchmarks,
    simulate_strategy_backtest,
)
from apex.domain.strategy_definition import StrategyDefinition
from apex.domain.strategy_evaluation import (
    StrategyEvaluationError,
    compare_strategy_evaluations,
    evaluate_external_strategy,
    promote_to_paper_eligible,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionProvenanceError,
    StrategyVersionRegistry,
    StrategyVersionTransitionKind,
)

__all__ = [
    "EXTERNAL_STRATEGY_WARNING",
    "EvaluateExternalStrategy",
    "EvaluateExternalStrategyResult",
    "ExternalStrategyDefinitionRepository",
    "ExternalStrategyEvaluationRepository",
    "ExternalStrategyResearchError",
    "InMemoryExternalStrategyDefinitionRepository",
    "InMemoryExternalStrategyEvaluationRepository",
    "PromoteExternalStrategy",
    "PromoteExternalStrategyResult",
    "RegisterExternalStrategy",
    "RegisterExternalStrategyResult",
    "external_strategy_detail",
    "external_strategy_registry_snapshot",
]


class ExternalStrategyResearchError(ValueError):
    """A register/evaluate/promote action could not honestly proceed
    (unknown strategy/version/evaluation, a strategy/evaluation pairing that
    does not belong together, ...). Fails closed."""


class ExternalStrategyDefinitionRepository(Protocol):
    def register(self, *, definition: StrategyDefinition) -> bool:
        """Persist one immutable, versioned definition idempotently.
        Returns ``True`` on idempotent replay of an already-identical
        retained row (same ``strategy_id``+``version``, same
        ``canonical_hash``), ``False`` on a genuine new write. Raises
        ``ExternalStrategyResearchError`` if the same ``strategy_id``+
        ``version`` already exists with a different ``canonical_hash`` --
        e.g. a re-registration whose ``display_name``/``provenance``/
        ``evidence_requirements`` genuinely changed (see ``domain.
        external_strategy``'s own test coverage: ``strategy_id`` alone does
        not capture that content)."""

    def get(self, *, strategy_id: str, version: int) -> StrategyDefinition | None:
        """One exact, immutable version. ``None`` if not retained."""

    def versions(self, *, strategy_id: str) -> tuple[StrategyDefinition, ...]:
        """Every retained version of one strategy, oldest first -- full
        lineage for audit/reproducibility."""

    def latest_version(self, *, strategy_id: str) -> StrategyDefinition | None:
        """The highest-``version`` retained row for ``strategy_id``. ``None``
        if no version is retained at all."""

    def all_root_strategies(self) -> tuple[StrategyDefinition, ...]:
        """The LATEST retained version of every distinct retained
        ``strategy_id`` -- the operator-registry listing shape: one row per
        strategy, never one row per version."""


class ExternalStrategyEvaluationRepository(Protocol):
    def register(self, *, record: ExternalStrategyEvaluationRecord) -> bool:
        """Persist one immutable evaluation record idempotently -- same
        idempotent-replay/identity-conflict contract as
        ``ExternalStrategyDefinitionRepository.register``, keyed by
        ``evaluation_id``."""

    def get(self, *, evaluation_id: str) -> ExternalStrategyEvaluationRecord | None: ...

    def for_strategy(self, *, strategy_id: str) -> tuple[ExternalStrategyEvaluationRecord, ...]:
        """Every retained evaluation across every version of one strategy,
        most-recently-``evaluated_at`` first."""

    def latest_for_strategy_version(
        self, *, strategy_id: str, version: int
    ) -> ExternalStrategyEvaluationRecord | None:
        """The most-recently-``evaluated_at`` retained evaluation for one
        exact (``strategy_id``, ``version``). ``None`` if none is
        retained."""


class InMemoryExternalStrategyDefinitionRepository:
    """Reference/test implementation."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], StrategyDefinition] = {}

    def register(self, *, definition: StrategyDefinition) -> bool:
        key = (definition.strategy_id, definition.version)
        existing = self._rows.get(key)
        if existing is not None:
            if existing.canonical_hash != definition.canonical_hash:
                raise ExternalStrategyResearchError(
                    "EXTERNAL_STRATEGY_DEFINITION_IDENTITY_CONFLICT"
                )
            return True
        self._rows[key] = definition
        return False

    def get(self, *, strategy_id: str, version: int) -> StrategyDefinition | None:
        return self._rows.get((strategy_id, version))

    def versions(self, *, strategy_id: str) -> tuple[StrategyDefinition, ...]:
        return tuple(
            sorted(
                (item for item in self._rows.values() if item.strategy_id == strategy_id),
                key=lambda item: item.version,
            )
        )

    def latest_version(self, *, strategy_id: str) -> StrategyDefinition | None:
        versions = self.versions(strategy_id=strategy_id)
        return versions[-1] if versions else None

    def all_root_strategies(self) -> tuple[StrategyDefinition, ...]:
        latest_by_strategy: dict[str, StrategyDefinition] = {}
        for item in self._rows.values():
            current = latest_by_strategy.get(item.strategy_id)
            if current is None or item.version > current.version:
                latest_by_strategy[item.strategy_id] = item
        return tuple(
            sorted(
                latest_by_strategy.values(), key=lambda item: (item.created_at, item.strategy_id)
            )
        )


class InMemoryExternalStrategyEvaluationRepository:
    """Reference/test implementation."""

    def __init__(self) -> None:
        self._rows: dict[str, ExternalStrategyEvaluationRecord] = {}

    def register(self, *, record: ExternalStrategyEvaluationRecord) -> bool:
        existing = self._rows.get(record.evaluation_id)
        if existing is not None:
            if existing.canonical_hash != record.canonical_hash:
                raise ExternalStrategyResearchError(
                    "EXTERNAL_STRATEGY_EVALUATION_IDENTITY_CONFLICT"
                )
            return True
        self._rows[record.evaluation_id] = record
        return False

    def get(self, *, evaluation_id: str) -> ExternalStrategyEvaluationRecord | None:
        return self._rows.get(evaluation_id)

    def for_strategy(self, *, strategy_id: str) -> tuple[ExternalStrategyEvaluationRecord, ...]:
        return tuple(
            sorted(
                (item for item in self._rows.values() if item.strategy_id == strategy_id),
                key=lambda item: (item.evaluated_at, item.evaluation_id),
                reverse=True,
            )
        )

    def latest_for_strategy_version(
        self, *, strategy_id: str, version: int
    ) -> ExternalStrategyEvaluationRecord | None:
        candidates = [
            item
            for item in self._rows.values()
            if item.strategy_id == strategy_id and item.strategy_version == version
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.evaluated_at, item.evaluation_id))
        return candidates[-1]


@dataclass(frozen=True, slots=True)
class RegisterExternalStrategyResult:
    definition: StrategyDefinition
    idempotent_replay: bool


class RegisterExternalStrategy:
    """Validate one declarative specification and persist it as a genuine,
    immutable, version-1 ``RESEARCH_ONLY`` ``StrategyDefinition``. Never
    evaluates, never promotes."""

    def __init__(self, definitions: ExternalStrategyDefinitionRepository) -> None:
        self._definitions = definitions

    def execute(
        self, *, specification: Mapping[str, object], created_at: datetime
    ) -> RegisterExternalStrategyResult:
        definition = strategy_definition_from_specification(specification, created_at=created_at)
        idempotent_replay = self._definitions.register(definition=definition)
        return RegisterExternalStrategyResult(
            definition=definition, idempotent_replay=idempotent_replay
        )


@dataclass(frozen=True, slots=True)
class EvaluateExternalStrategyResult:
    record: ExternalStrategyEvaluationRecord
    idempotent_replay: bool
    compared_benchmark_strategy_ids: tuple[str, ...]
    skipped_benchmark_strategy_ids: tuple[str, ...]


class EvaluateExternalStrategy:
    """Fetch retained candle evidence bounded to ``as_of``, run the
    unmodified ``domain.strategy_benchmark``/``domain.strategy_evaluation``
    machinery, compare against every canonical benchmark
    (``domain.strategy_benchmark.canonical_strategy_benchmarks`` --
    Benchmark Discipline, Part 9: always all four, deterministically, never
    hand-picked after seeing a result), and persist the resulting immutable
    ``ExternalStrategyEvaluationRecord``. Never promotes -- see
    ``PromoteExternalStrategy``."""

    def __init__(
        self,
        *,
        definitions: ExternalStrategyDefinitionRepository,
        evaluations: ExternalStrategyEvaluationRepository,
        candles: CryptoResearchRepository,
    ) -> None:
        self._definitions = definitions
        self._evaluations = evaluations
        self._candles = candles

    def execute(
        self,
        *,
        strategy_id: str,
        version: int,
        capture_id: str,
        as_of: datetime,
        evaluated_at: datetime,
    ) -> EvaluateExternalStrategyResult:
        definition = self._definitions.get(strategy_id=strategy_id, version=version)
        if definition is None:
            raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_DEFINITION_NOT_FOUND")
        if len(definition.instrument_ids) != 1:
            # Structurally unreachable through strategy_definition_from_
            # specification (which always registers exactly one instrument
            # id) -- defense in depth for a definition reached any other
            # way. simulate_strategy_backtest has no cross-sectional
            # simulator; see domain.external_strategy's module docstring.
            raise ExternalStrategyEvaluationError(
                "EXTERNAL_STRATEGY_EVALUATION_REQUIRES_EXACTLY_ONE_INSTRUMENT"
            )
        ensure_capture_eligible(capture_id)
        instrument_id = definition.instrument_ids[0]

        raw_candles = self._candles.candles(capture_id)
        if not raw_candles:
            raise ExternalStrategyEvaluationError(
                "EXTERNAL_STRATEGY_CAPTURE_HAS_NO_RETAINED_CANDLES"
            )
        window = point_in_time_candle_window(raw_candles, instrument_id=instrument_id, as_of=as_of)
        if not window:
            raise ExternalStrategyEvaluationError(
                "EXTERNAL_STRATEGY_NO_CANDLES_AVAILABLE_AT_OR_BEFORE_AS_OF"
            )
        evidence_classes = {item.evidence_class for item in window}
        if len(evidence_classes) > 1:
            raise ExternalStrategyEvaluationError(
                "EXTERNAL_STRATEGY_MIXED_EVIDENCE_CLASS_IN_CAPTURE"
            )
        evidence_class = window[0].evidence_class

        try:
            candidate_backtest = simulate_strategy_backtest(
                definition, window, evidence_class=evidence_class, evidence_provenance=capture_id
            )
        except StrategyBenchmarkError as error:
            raise ExternalStrategyEvaluationError(
                f"EXTERNAL_STRATEGY_BACKTEST_FAILED:{error}"
            ) from error
        candidate = evaluate_external_strategy(definition, candidate_backtest)

        comparisons = []
        compared_ids: list[str] = []
        skipped_ids: list[str] = []
        for benchmark_definition in canonical_strategy_benchmarks(created_at=evaluated_at):
            try:
                benchmark_backtest = simulate_strategy_backtest(
                    benchmark_definition,
                    window,
                    evidence_class=evidence_class,
                    evidence_provenance=capture_id,
                )
            except StrategyBenchmarkError:
                # Honestly skip -- e.g. the window has fewer than the
                # 50/200-day benchmark's own 201-day requirement. Never a
                # hard failure of the whole evaluation, and never silently
                # substituted with a weaker benchmark (Part 9): the set
                # considered is still deterministic from ``canonical_
                # strategy_benchmarks`` alone, this candidate's own window
                # length is simply too short for that one member.
                skipped_ids.append(benchmark_definition.strategy_id)
                continue
            benchmark_evaluation = evaluate_external_strategy(
                benchmark_definition, benchmark_backtest
            )
            comparisons.append(
                compare_strategy_evaluations(
                    candidate, benchmark_evaluation, compared_at=evaluated_at
                )
            )
            compared_ids.append(benchmark_definition.strategy_id)

        record = build_external_strategy_evaluation_record(
            strategy_id=definition.strategy_id,
            strategy_version=definition.version,
            as_of=as_of,
            capture_id=capture_id,
            evaluated_at=evaluated_at,
            candidate=candidate,
            benchmark_comparisons=tuple(comparisons),
        )
        idempotent_replay = self._evaluations.register(record=record)
        return EvaluateExternalStrategyResult(
            record=record,
            idempotent_replay=idempotent_replay,
            compared_benchmark_strategy_ids=tuple(compared_ids),
            skipped_benchmark_strategy_ids=tuple(skipped_ids),
        )


@dataclass(frozen=True, slots=True)
class PromoteExternalStrategyResult:
    definition: StrategyDefinition
    idempotent_replay: bool


class PromoteExternalStrategy:
    """The one explicit, separate, operator-invoked action that can move a
    ``RESEARCH_ONLY`` external strategy to ``PAPER_ELIGIBLE``. Calls the
    real, unmodified ``strategy_evaluation.promote_to_paper_eligible`` gate
    -- never a looser or duplicated copy of it (see ``domain.
    external_strategy.classify_qualification``, which only ever reports
    what this gate WOULD do, and never calls it). Never invoked by
    ``RegisterExternalStrategy``/``EvaluateExternalStrategy``, and never
    invoked automatically by anything else in this codebase."""

    def __init__(
        self,
        *,
        definitions: ExternalStrategyDefinitionRepository,
        evaluations: ExternalStrategyEvaluationRepository,
        provenance: StrategyVersionProvenanceRepository,
    ) -> None:
        self._definitions = definitions
        self._evaluations = evaluations
        self._provenance = provenance

    def execute(
        self, *, strategy_id: str, version: int, evaluation_id: str, promoted_at: datetime
    ) -> PromoteExternalStrategyResult:
        definition = self._definitions.get(strategy_id=strategy_id, version=version)
        if definition is None:
            raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_DEFINITION_NOT_FOUND")
        record = self._evaluations.get(evaluation_id=evaluation_id)
        if record is None:
            raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_EVALUATION_NOT_FOUND")
        if record.strategy_id != strategy_id or record.strategy_version != version:
            raise ExternalStrategyResearchError(
                "EXTERNAL_STRATEGY_EVALUATION_DOES_NOT_BELONG_TO_THIS_STRATEGY_VERSION"
            )
        try:
            promoted = promote_to_paper_eligible(
                definition, evaluation=record.candidate, promoted_at=promoted_at
            )
        except StrategyEvaluationError as error:
            raise ExternalStrategyResearchError(
                f"EXTERNAL_STRATEGY_PROMOTION_REJECTED:{error}"
            ) from error
        # Strategy Version Provenance v1: retain the exact evaluated artifact,
        # its binding to this evaluation, and the justified transition BEFORE
        # the promoted version is registered, so no promoted artifact can
        # exist without its justification. ``definition`` comes from the
        # immutable registry, which holds exactly one artifact per
        # (strategy_id, version) and conflicts on any other hash. The record
        # checked above was evaluated at that exact key, so the binding is a
        # retained fact, not an inference from current state.
        try:
            RecordEvaluationSubject(self._provenance).execute(
                definition=definition,
                registry=StrategyVersionRegistry.EXTERNAL_OPERATOR,
                evaluation_source=StrategyEvaluationSource.EXTERNAL_STRATEGY_EVALUATION,
                evaluation_id=record.evaluation_id,
                evaluation_hash=record.canonical_hash,
                evaluated_strategy_id=record.strategy_id,
                evaluated_strategy_version=record.strategy_version,
            )
            RecordStrategyVersionTransition(self._provenance).execute(
                kind=StrategyVersionTransitionKind.EXTERNAL_OPERATOR_PROMOTION,
                from_definition=definition,
                to_definition=promoted,
                evaluation_source=StrategyEvaluationSource.EXTERNAL_STRATEGY_EVALUATION,
                evaluation_id=record.evaluation_id,
                evaluation=record.candidate,
                registry=StrategyVersionRegistry.EXTERNAL_OPERATOR,
            )
        except (StrategyVersionProvenanceError, StrategyVersionProvenanceResearchError) as error:
            raise ExternalStrategyResearchError(
                f"EXTERNAL_STRATEGY_PROMOTION_PROVENANCE_REJECTED:{error}"
            ) from error
        idempotent_replay = self._definitions.register(definition=promoted)
        return PromoteExternalStrategyResult(
            definition=promoted, idempotent_replay=idempotent_replay
        )


def external_strategy_registry_snapshot(
    *,
    definitions: ExternalStrategyDefinitionRepository,
    evaluations: ExternalStrategyEvaluationRepository,
) -> dict[str, object]:
    """One row per distinct registered external strategy (latest version),
    with its latest evaluation summary if one exists -- the Operator
    Console/CLI listing shape. Read-only, descriptive; never mutates
    anything (Part 19: distinguishes source, version, latest evaluation
    time, qualification/lifecycle state -- never a strategy marketplace)."""

    rows = []
    for definition in definitions.all_root_strategies():
        latest_evaluations = evaluations.for_strategy(strategy_id=definition.strategy_id)
        latest = latest_evaluations[0] if latest_evaluations else None
        rows.append(
            {
                "strategy_id": definition.strategy_id,
                "version": definition.version,
                "display_name": definition.display_name,
                "source_kind": definition.source_kind.value,
                "venue": definition.venue.value,
                "instrument_ids": definition.instrument_ids,
                "rule_kind": definition.rule_kind.value,
                "lifecycle_state": definition.lifecycle_state.value,
                "provenance": definition.provenance,
                "created_at": definition.created_at,
                "latest_evaluation": None if latest is None else _evaluation_summary(latest),
            }
        )
    return {
        "strategies": tuple(rows),
        "total": len(rows),
        "authority": "NONE",
        "paper_authority": "NONE",
        "live_authority": "NONE",
        "warning": EXTERNAL_STRATEGY_WARNING,
    }


def external_strategy_detail(
    *,
    definitions: ExternalStrategyDefinitionRepository,
    evaluations: ExternalStrategyEvaluationRepository,
    strategy_id: str,
) -> dict[str, object]:
    """Full retained lineage for one strategy: every version, every
    evaluation across every version. ``None`` fields where nothing is
    retained yet -- never fabricated."""

    versions = definitions.versions(strategy_id=strategy_id)
    strategy_evaluations = evaluations.for_strategy(strategy_id=strategy_id)
    return {
        "strategy_id": strategy_id,
        "versions": versions,
        "latest_version": versions[-1] if versions else None,
        "evaluations": strategy_evaluations,
        "latest_evaluation": strategy_evaluations[0] if strategy_evaluations else None,
        "authority": "NONE",
        "paper_authority": "NONE",
        "live_authority": "NONE",
        "warning": EXTERNAL_STRATEGY_WARNING,
    }


def _evaluation_summary(record: ExternalStrategyEvaluationRecord) -> dict[str, object]:
    return {
        "evaluation_id": record.evaluation_id,
        "evaluated_at": record.evaluated_at,
        "as_of": record.as_of,
        "capture_id": record.capture_id,
        "evaluation_policy_version": record.evaluation_policy_version,
        "sample_size": record.candidate.sample_size,
        "net_return": record.candidate.net_return,
        "gross_return": record.candidate.gross_return,
        "maximum_drawdown": record.candidate.maximum_drawdown,
        "qualification": record.qualification.value,
        "benchmark_comparisons": record.benchmark_comparisons,
    }
