"""PostgreSQL persistence for immutable crypto strategy evidence dossiers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_research_postgres import (
    PostgresCryptoResearchRepository,
    _configuration,
    _data,
    _metric,
    _payload,
    _signal,
    _strings,
    _time,
)
from apex.domain.crypto_dossier import (
    CryptoDossierConclusion,
    CryptoDossierConclusionType,
    CryptoEvidenceDossier,
    CryptoReplicationOutcome,
    CryptoReplicationProtocol,
    CryptoReplicationRun,
    CryptoStrategyLifecycle,
    CryptoStrategySpecification,
)
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
)

metadata = sa.MetaData()


def _table(name: str, *columns: sa.Column[Any], unique: str | None = None) -> sa.Table:
    items: list[Any] = list(columns)
    if unique is not None:
        items.append(sa.UniqueConstraint(unique))
    return sa.Table(name, metadata, *items)


crypto_strategy_specification = _table(
    "crypto_strategy_specification",
    sa.Column("specification_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("original_run_id", sa.String(160), nullable=False),
    sa.Column("original_outcome_id", sa.String(160), nullable=False),
    sa.Column("semantics_hash", sa.String(64), nullable=False),
    sa.Column("parent_specification_id", sa.String(160)),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("change_rationale", sa.String(500), nullable=False),
    sa.Column("lifecycle_state", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="semantics_hash",
)
crypto_replication_protocol = _table(
    "crypto_replication_protocol",
    sa.Column("protocol_id", sa.String(160), primary_key=True),
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
    sa.Column("protocol_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_replication_run = _table(
    "crypto_replication_run",
    sa.Column("run_id", sa.String(160), primary_key=True),
    sa.Column("protocol_id", sa.String(160), nullable=False),
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("configuration_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_replication_outcome = _table(
    "crypto_replication_outcome",
    sa.Column("outcome_id", sa.String(160), primary_key=True),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("decision", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="run_id",
)
crypto_evidence_dossier = _table(
    "crypto_evidence_dossier",
    sa.Column("dossier_id", sa.String(160), primary_key=True),
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
    sa.Column("replication_run_id", sa.String(160), nullable=False),
    sa.Column("membership_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="membership_hash",
)
crypto_dossier_conclusion = _table(
    "crypto_dossier_conclusion",
    sa.Column("conclusion_id", sa.String(160), primary_key=True),
    sa.Column("dossier_id", sa.String(160), nullable=False),
    sa.Column("conclusion", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="dossier_id",
)


def _pairs(value: object) -> tuple[tuple[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError("CRYPTO_DOSSIER_PAIRS_INVALID")
    result: list[tuple[str, object]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            continue
        key = str(item[0])
        member = item[1]
        if key in {"fee_bps_per_side", "slippage_bps_per_side"}:
            member = Decimal(str(member))
        elif key in {"baselines", "success_thresholds"} and isinstance(member, list):
            member = tuple(str(element) for element in member)
        result.append((key, member))
    return tuple(result)


def _strategy(row: RowMapping) -> CryptoStrategySpecification:
    value = _data(row)
    return CryptoStrategySpecification(
        specification_id=str(value["specification_id"]),
        strategy_family=str(value["strategy_family"]),
        version=int(value["version"]),
        parent_specification_id=(
            str(value["parent_specification_id"])
            if value["parent_specification_id"] is not None
            else None
        ),
        change_rationale=str(value["change_rationale"]),
        description=str(value["description"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        original_run_id=str(value["original_run_id"]),
        original_outcome_id=str(value["original_outcome_id"]),
        semantics=_pairs(value["semantics"]),
        semantics_hash=str(value["semantics_hash"]),
        created_at=_time(value["created_at"]),
        lifecycle_state=CryptoStrategyLifecycle(value["lifecycle_state"]),
        known_limitations=_strings(value["known_limitations"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _protocol(row: RowMapping) -> CryptoReplicationProtocol:
    value = _data(row)
    return CryptoReplicationProtocol(
        protocol_id=str(value["protocol_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        original_run_id=str(value["original_run_id"]),
        original_outcome_id=str(value["original_outcome_id"]),
        data_start=_time(value["data_start"]),
        data_end=_time(value["data_end"]),
        warmup_start=_time(value["warmup_start"]),
        warmup_end=_time(value["warmup_end"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
        disjoint_reason=str(value["disjoint_reason"]),
        cost_model=str(value["cost_model"]),
        validation_policy=str(value["validation_policy"]),
        baseline_definitions=_strings(value["baseline_definitions"]),
        executor_identity=str(value["executor_identity"]),
        conclusion_policy=str(value["conclusion_policy"]),
        protocol_hash=str(value["protocol_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _replication_run(row: RowMapping) -> CryptoReplicationRun:
    value = _data(row)
    signals = value["signals"]
    metrics = value["metrics"]
    if not isinstance(signals, list) or not isinstance(metrics, list):
        raise ValueError("CRYPTO_REPLICATION_RESULT_INVALID")
    return CryptoReplicationRun(
        run_id=str(value["run_id"]),
        protocol_id=str(value["protocol_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        snapshot_id=str(value["snapshot_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        configuration=_configuration(value["configuration"]),
        configuration_hash=str(value["configuration_hash"]),
        code_revision=str(value["code_revision"]),
        deterministic_seed=int(value["deterministic_seed"]),
        executed_at=_time(value["executed_at"]),
        state=ExperimentState(value["state"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        signals=tuple(_signal(item) for item in signals if isinstance(item, dict)),
        metrics=tuple(_metric(item) for item in metrics if isinstance(item, dict)),
        warnings=_strings(value["warnings"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _replication_outcome(row: RowMapping) -> CryptoReplicationOutcome:
    value = _data(row)
    return CryptoReplicationOutcome(
        outcome_id=str(value["outcome_id"]),
        run_id=str(value["run_id"]),
        decision=ExperimentOutcomeDecision(value["decision"]),
        reason=str(value["reason"]),
        decided_at=_time(value["decided_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _dossier(row: RowMapping) -> CryptoEvidenceDossier:
    value = _data(row)
    return CryptoEvidenceDossier(
        dossier_id=str(value["dossier_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        original_snapshot_id=str(value["original_snapshot_id"]),
        original_run_id=str(value["original_run_id"]),
        original_result_hash=str(value["original_result_hash"]),
        original_outcome_id=str(value["original_outcome_id"]),
        replication_protocol_id=str(value["replication_protocol_id"]),
        replication_snapshot_id=str(value["replication_snapshot_id"]),
        replication_run_id=str(value["replication_run_id"]),
        replication_result_hash=str(value["replication_result_hash"]),
        replication_outcome_id=str(value["replication_outcome_id"]),
        shared_semantics_hash=str(value["shared_semantics_hash"]),
        complete_windows=int(value["complete_windows"]),
        passing_windows=int(value["passing_windows"]),
        failing_windows=int(value["failing_windows"]),
        median_conservative_net_return=Decimal(str(value["median_conservative_net_return"])),
        worst_maximum_drawdown=Decimal(str(value["worst_maximum_drawdown"])),
        consistency_status=str(value["consistency_status"]),
        warnings=_strings(value["warnings"]),
        created_at=_time(value["created_at"]),
        membership_hash=str(value["membership_hash"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _conclusion(row: RowMapping) -> CryptoDossierConclusion:
    value = _data(row)
    return CryptoDossierConclusion(
        conclusion_id=str(value["conclusion_id"]),
        dossier_id=str(value["dossier_id"]),
        conclusion=CryptoDossierConclusionType(value["conclusion"]),
        reason=str(value["reason"]),
        policy_version=str(value["policy_version"]),
        decided_at=_time(value["decided_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
    )


class PostgresCryptoDossierRepository(PostgresCryptoResearchRepository):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def strategy_specification(self, specification_id: str) -> CryptoStrategySpecification | None:
        row = self._one(
            sa.select(crypto_strategy_specification).where(
                crypto_strategy_specification.c.specification_id == specification_id
            )
        )
        return _strategy(row) if row is not None else None

    def strategy_by_semantics(self, semantics_hash: str) -> CryptoStrategySpecification | None:
        row = self._one(
            sa.select(crypto_strategy_specification).where(
                crypto_strategy_specification.c.semantics_hash == semantics_hash
            )
        )
        return _strategy(row) if row is not None else None

    def save_strategy_specification(
        self, value: CryptoStrategySpecification
    ) -> CryptoStrategySpecification:
        self._insert(
            crypto_strategy_specification,
            {
                "specification_id": value.specification_id,
                "hypothesis_version_id": value.hypothesis_version_id,
                "original_run_id": value.original_run_id,
                "original_outcome_id": value.original_outcome_id,
                "semantics_hash": value.semantics_hash,
                "parent_specification_id": value.parent_specification_id,
                "version": value.version,
                "change_rationale": value.change_rationale,
                "lifecycle_state": value.lifecycle_state.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_STRATEGY_SPECIFICATION_PERSISTENCE_CONFLICT",
        )
        return value

    def strategy_specifications(self) -> tuple[CryptoStrategySpecification, ...]:
        return tuple(
            _strategy(row)
            for row in self._rows(
                sa.select(crypto_strategy_specification).order_by(
                    crypto_strategy_specification.c.specification_id
                )
            )
        )

    def replication_protocol(self, protocol_id: str) -> CryptoReplicationProtocol | None:
        row = self._one(
            sa.select(crypto_replication_protocol).where(
                crypto_replication_protocol.c.protocol_id == protocol_id
            )
        )
        return _protocol(row) if row is not None else None

    def save_replication_protocol(
        self, value: CryptoReplicationProtocol
    ) -> CryptoReplicationProtocol:
        self._insert(
            crypto_replication_protocol,
            {
                "protocol_id": value.protocol_id,
                "strategy_specification_id": value.strategy_specification_id,
                "protocol_hash": value.protocol_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_REPLICATION_PROTOCOL_PERSISTENCE_CONFLICT",
        )
        return value

    def replication_protocols(self) -> tuple[CryptoReplicationProtocol, ...]:
        return tuple(
            _protocol(row)
            for row in self._rows(
                sa.select(crypto_replication_protocol).order_by(
                    crypto_replication_protocol.c.protocol_id
                )
            )
        )

    def replication_run(self, run_id: str) -> CryptoReplicationRun | None:
        row = self._one(
            sa.select(crypto_replication_run).where(crypto_replication_run.c.run_id == run_id)
        )
        return _replication_run(row) if row is not None else None

    def save_replication_run(self, value: CryptoReplicationRun) -> CryptoReplicationRun:
        self._insert(
            crypto_replication_run,
            {
                "run_id": value.run_id,
                "protocol_id": value.protocol_id,
                "strategy_specification_id": value.strategy_specification_id,
                "snapshot_id": value.snapshot_id,
                "result_hash": value.result_hash,
                "configuration_hash": value.configuration_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_REPLICATION_RUN_PERSISTENCE_CONFLICT",
        )
        return value

    def replication_runs(self) -> tuple[CryptoReplicationRun, ...]:
        return tuple(
            _replication_run(row)
            for row in self._rows(
                sa.select(crypto_replication_run).order_by(crypto_replication_run.c.run_id)
            )
        )

    def replication_outcome(self, outcome_id: str) -> CryptoReplicationOutcome | None:
        row = self._one(
            sa.select(crypto_replication_outcome).where(
                crypto_replication_outcome.c.outcome_id == outcome_id
            )
        )
        return _replication_outcome(row) if row is not None else None

    def replication_outcome_for_run(self, run_id: str) -> CryptoReplicationOutcome | None:
        row = self._one(
            sa.select(crypto_replication_outcome).where(
                crypto_replication_outcome.c.run_id == run_id
            )
        )
        return _replication_outcome(row) if row is not None else None

    def save_replication_outcome(self, value: CryptoReplicationOutcome) -> CryptoReplicationOutcome:
        self._insert(
            crypto_replication_outcome,
            {
                "outcome_id": value.outcome_id,
                "run_id": value.run_id,
                "decision": value.decision.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "paper_eligible": False,
                "live_eligible": False,
                "authority": "NONE",
            },
            "CRYPTO_REPLICATION_OUTCOME_PERSISTENCE_CONFLICT",
        )
        return value

    def replication_outcomes(self) -> tuple[CryptoReplicationOutcome, ...]:
        return tuple(
            _replication_outcome(row)
            for row in self._rows(
                sa.select(crypto_replication_outcome).order_by(
                    crypto_replication_outcome.c.outcome_id
                )
            )
        )

    def dossier(self, dossier_id: str) -> CryptoEvidenceDossier | None:
        row = self._one(
            sa.select(crypto_evidence_dossier).where(
                crypto_evidence_dossier.c.dossier_id == dossier_id
            )
        )
        return _dossier(row) if row is not None else None

    def save_dossier(self, value: CryptoEvidenceDossier) -> CryptoEvidenceDossier:
        self._insert(
            crypto_evidence_dossier,
            {
                "dossier_id": value.dossier_id,
                "strategy_specification_id": value.strategy_specification_id,
                "replication_run_id": value.replication_run_id,
                "membership_hash": value.membership_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_DOSSIER_PERSISTENCE_CONFLICT",
        )
        return value

    def dossiers(self) -> tuple[CryptoEvidenceDossier, ...]:
        return tuple(
            _dossier(row)
            for row in self._rows(
                sa.select(crypto_evidence_dossier).order_by(crypto_evidence_dossier.c.dossier_id)
            )
        )

    def dossier_conclusion(self, conclusion_id: str) -> CryptoDossierConclusion | None:
        row = self._one(
            sa.select(crypto_dossier_conclusion).where(
                crypto_dossier_conclusion.c.conclusion_id == conclusion_id
            )
        )
        return _conclusion(row) if row is not None else None

    def dossier_conclusion_for_dossier(self, dossier_id: str) -> CryptoDossierConclusion | None:
        row = self._one(
            sa.select(crypto_dossier_conclusion).where(
                crypto_dossier_conclusion.c.dossier_id == dossier_id
            )
        )
        return _conclusion(row) if row is not None else None

    def save_dossier_conclusion(self, value: CryptoDossierConclusion) -> CryptoDossierConclusion:
        self._insert(
            crypto_dossier_conclusion,
            {
                "conclusion_id": value.conclusion_id,
                "dossier_id": value.dossier_id,
                "conclusion": value.conclusion.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "paper_eligible": False,
                "live_eligible": False,
                "authority": "NONE",
            },
            "CRYPTO_DOSSIER_CONCLUSION_PERSISTENCE_CONFLICT",
        )
        return value

    def dossier_conclusions(self) -> tuple[CryptoDossierConclusion, ...]:
        return tuple(
            _conclusion(row)
            for row in self._rows(
                sa.select(crypto_dossier_conclusion).order_by(
                    crypto_dossier_conclusion.c.conclusion_id
                )
            )
        )


__all__ = ["PostgresCryptoDossierRepository", "metadata"]
