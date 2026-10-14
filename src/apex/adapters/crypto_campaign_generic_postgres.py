"""PostgreSQL catalog for bounded generic crypto candidate definitions and plans."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_campaign_candidate_postgres import (
    PostgresCryptoCampaignCandidateRepository,
)
from apex.adapters.crypto_research_postgres import _data, _payload
from apex.application.crypto_research import CryptoResearchConflict
from apex.domain.crypto_campaign_generic import (
    CompiledCandidatePlan,
    GenericCandidateDefinition,
    compile_candidate_definition,
)

metadata = sa.MetaData()

generic_definition_table = sa.Table(
    "crypto_campaign_candidate_definition",
    metadata,
    sa.Column("definition_id", sa.String(160), primary_key=True),
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("candidate_id", sa.String(160), nullable=False),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
    sa.Column("schema_version", sa.String(80), nullable=False),
    sa.Column("semantics_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

generic_plan_table = sa.Table(
    "crypto_campaign_compiled_plan",
    metadata,
    sa.Column("plan_id", sa.String(160), primary_key=True),
    sa.Column("definition_id", sa.String(160), nullable=False),
    sa.Column("compiler_id", sa.String(80), nullable=False),
    sa.Column("compiler_version", sa.Integer(), nullable=False),
    sa.Column("semantics_hash", sa.String(64), nullable=False),
    sa.Column("plan_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


def _strip_computed(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_computed(item)
            for key, item in value.items()
            if key not in {"canonical_hash", "semantics_hash", "authority"}
        }
    if isinstance(value, list):
        return [_strip_computed(item) for item in value]
    return value


def _definition(row: RowMapping) -> GenericCandidateDefinition:
    payload = _strip_computed(_data(row))
    definition = GenericCandidateDefinition.from_mapping(payload)
    if definition.canonical_hash != str(row["canonical_hash"]) or definition.semantics_hash != str(
        row["semantics_hash"]
    ):
        raise CryptoResearchConflict("GENERIC_DEFINITION_PERSISTED_HASH_INVALID")
    return definition


class PostgresGenericCryptoCampaignRepository(PostgresCryptoCampaignCandidateRepository):
    """Existing campaign evidence plus the generic definition/plan catalog."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def generic_definition(self, definition_id: str) -> GenericCandidateDefinition | None:
        row = self._one(
            sa.select(generic_definition_table).where(
                generic_definition_table.c.definition_id == definition_id
            )
        )
        return _definition(row) if row is not None else None

    def save_generic_definition(
        self, definition: GenericCandidateDefinition
    ) -> GenericCandidateDefinition:
        prior = self.generic_definition(definition.definition_id)
        if prior is not None:
            if prior.canonical_hash != definition.canonical_hash:
                raise CryptoResearchConflict("GENERIC_DEFINITION_PERSISTENCE_CONFLICT")
            return prior
        self._insert(
            generic_definition_table,
            {
                "definition_id": definition.definition_id,
                "campaign_id": definition.campaign_id,
                "candidate_id": definition.candidate_id,
                "hypothesis_version_id": definition.hypothesis_version_id,
                "strategy_specification_id": definition.strategy_specification_id,
                "schema_version": definition.schema_version.value,
                "semantics_hash": definition.semantics_hash,
                "payload": _payload(definition),
                "canonical_hash": definition.canonical_hash,
                "authority": "NONE",
            },
            "GENERIC_DEFINITION_PERSISTENCE_CONFLICT",
        )
        return definition

    def generic_definitions(self) -> tuple[GenericCandidateDefinition, ...]:
        return tuple(
            _definition(row)
            for row in self._rows(
                sa.select(generic_definition_table).order_by(
                    generic_definition_table.c.definition_id
                )
            )
        )

    def generic_plan(self, plan_id: str) -> CompiledCandidatePlan | None:
        row = self._one(
            sa.select(generic_plan_table).where(generic_plan_table.c.plan_id == plan_id)
        )
        if row is None:
            return None
        definition = self.generic_definition(str(row["definition_id"]))
        if definition is None:
            raise CryptoResearchConflict("GENERIC_PLAN_DEFINITION_NOT_FOUND")
        plan = compile_candidate_definition(definition)
        if (
            plan.plan_id != str(row["plan_id"])
            or plan.plan_hash != str(row["plan_hash"])
            or plan.canonical_hash != str(row["canonical_hash"])
        ):
            raise CryptoResearchConflict("GENERIC_PLAN_PERSISTED_HASH_INVALID")
        return plan

    def save_generic_plan(self, plan: CompiledCandidatePlan) -> CompiledCandidatePlan:
        if self.generic_definition(plan.definition_id) is None:
            raise CryptoResearchConflict("GENERIC_PLAN_DEFINITION_NOT_FOUND")
        prior = self.generic_plan(plan.plan_id)
        if prior is not None:
            if prior.canonical_hash != plan.canonical_hash:
                raise CryptoResearchConflict("GENERIC_PLAN_PERSISTENCE_CONFLICT")
            return prior
        self._insert(
            generic_plan_table,
            {
                "plan_id": plan.plan_id,
                "definition_id": plan.definition_id,
                "compiler_id": plan.compiler_id,
                "compiler_version": plan.compiler_version,
                "semantics_hash": plan.semantics_hash,
                "plan_hash": plan.plan_hash,
                "payload": _payload(plan),
                "canonical_hash": plan.canonical_hash,
                "authority": "NONE",
            },
            "GENERIC_PLAN_PERSISTENCE_CONFLICT",
        )
        return plan

    def generic_plans(self) -> tuple[CompiledCandidatePlan, ...]:
        rows = self._rows(
            sa.select(generic_plan_table.c.plan_id).order_by(generic_plan_table.c.plan_id)
        )
        result: list[CompiledCandidatePlan] = []
        for row in rows:
            value = self.generic_plan(str(row["plan_id"]))
            if value is not None:
                result.append(value)
        return tuple(result)

    def retained_candidate_comparison(self, candidate_id: str) -> object | None:
        """Compatibility resolver kept below generic API/application surfaces."""

        return self.candidate_comparisons(candidate_id) or self.breakout_comparisons(candidate_id)

    def retained_candidate_protocol(self, protocol_id: str) -> object | None:
        return self.candidate_protocol(protocol_id) or self.breakout_protocol(protocol_id)

    def retained_candidate_run(self, run_id: str) -> object | None:
        return self.candidate_run(run_id) or self.breakout_run(run_id)

    def retained_candidate_outcome_for_run(self, run_id: str) -> object | None:
        return self.candidate_outcome_for_run(run_id) or self.breakout_outcome_for_run(run_id)

    def retained_candidate_dossier(self, dossier_id: str) -> object | None:
        return self.candidate_dossier(dossier_id) or self.breakout_dossier(dossier_id)

    def retained_candidate_nomination_for_dossier(self, dossier_id: str) -> object | None:
        return self.candidate_nomination_for_dossier(
            dossier_id
        ) or self.breakout_nomination_for_dossier(dossier_id)
