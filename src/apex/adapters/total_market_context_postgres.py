"""Immutable PostgreSQL evidence for Total Market context integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    HypotheticalAllocationProposal,
    OpportunityExplanation,
    PriorityResult,
    SharedOpportunityProjection,
    SharedOpportunitySet,
)
from apex.domain.total_market_context import (
    ApplicabilityState,
    ContextApplicability,
    ContextComponent,
    ContextComponentState,
    ContextComponentType,
    ContextEnrichedOpportunitySet,
    OpportunityContextAttachment,
    OverallContextState,
    TotalMarketContextError,
    TotalMarketContextSnapshot,
    UnavailableContextComponent,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()


def _evidence_table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
    )


component_table = _evidence_table(
    "total_market_context_component",
    "component_id",
    sa.Column("component_type", sa.String(50), nullable=False),
    sa.Column("source_observation_id", sa.String(300), nullable=False),
    sa.Column("source_batch_id", sa.String(160), nullable=False),
    sa.Column("observation_at", sa.DateTime(timezone=True), nullable=False),
    # Recurring Operating Health & Unattended Evidence Closure v1: a
    # component's own ``freshness_seconds`` is inherently as-of-relative
    # (``build_context_snapshot`` enforces ``freshness_seconds ==
    # snapshot.created_at - available_at``), so the identical source bar
    # genuinely produces a different, later, honestly-staler component each
    # time it is re-observed on a new as-of date -- the uniqueness guard
    # below must be scoped to that date, or a genuinely new (not
    # duplicate/conflicting) later observation of the same bar collides
    # with an earlier one. See migration 0093 for the concrete defect this
    # closes and its exact backfill.
    sa.Column("context_as_of", sa.Date(), nullable=False),
    sa.UniqueConstraint(
        "component_type",
        "source_observation_id",
        "context_as_of",
        name="uq_total_market_component_source_as_of",
    ),
)
snapshot_table = _evidence_table(
    "total_market_context_snapshot",
    "snapshot_id",
    sa.Column("source_batch_id", sa.String(160), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
)
enriched_set_table = _evidence_table(
    "context_enriched_opportunity_set",
    "enriched_set_id",
    sa.Column(
        "original_opportunity_set_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
        nullable=False,
    ),
    sa.Column(
        "context_snapshot_id",
        sa.String(300),
        sa.ForeignKey("total_market_context_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
)
attachment_table = _evidence_table(
    "opportunity_context_attachment",
    "attachment_id",
    sa.Column(
        "enriched_set_id",
        sa.String(300),
        sa.ForeignKey("context_enriched_opportunity_set.enriched_set_id"),
        nullable=False,
    ),
    sa.Column(
        "projection_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.Column(
        "context_snapshot_id",
        sa.String(300),
        sa.ForeignKey("total_market_context_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "enriched_set_id",
        "projection_id",
        name="uq_opportunity_context_enriched_projection",
    ),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise TotalMarketContextError("TOTAL_MARKET_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TotalMarketContextError("TOTAL_MARKET_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _component(row: RowMapping) -> ContextComponent:
    value = _data(row)
    return ContextComponent(
        component_id=str(value["component_id"]),
        version=int(value["version"]),
        component_type=ContextComponentType(value["component_type"]),
        source_observation_id=str(value["source_observation_id"]),
        provider=str(value["provider"]),
        subject=str(value["subject"]),
        observation_at=_time(value["observation_at"]),
        available_at=_time(value["available_at"]),
        ingested_at=_time(value["ingested_at"]),
        current_value=Decimal(str(value["current_value"])),
        prior_comparable_value=Decimal(str(value["prior_comparable_value"])),
        normalized_change=Decimal(str(value["normalized_change"])),
        freshness_seconds=int(value["freshness_seconds"]),
        freshness_limit_seconds=int(value["freshness_limit_seconds"]),
        quality_status=str(value["quality_status"]),
        evidence_class=str(value["evidence_class"]),
        limitations=_strings(value["limitations"]),
        interpretation_category=str(value["interpretation_category"]),
        state=ContextComponentState(value["state"]),
        contribution=int(value["contribution"]),
        classification_reason=str(value["classification_reason"]),
        supporting_evidence_ids=_strings(value["supporting_evidence_ids"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
        order_authority="NONE",
        financial_mutation_capability="NONE",
    )


def _snapshot(row: RowMapping) -> TotalMarketContextSnapshot:
    value = _data(row)
    return TotalMarketContextSnapshot(
        snapshot_id=str(value["snapshot_id"]),
        version=int(value["version"]),
        created_at=_time(value["created_at"]),
        source_batch_id=str(value["source_batch_id"]),
        component_ids=_strings(value["component_ids"]),
        source_evidence_ids=_strings(value["source_evidence_ids"]),
        source_timestamps=tuple(_time(item) for item in value["source_timestamps"]),
        source_availability_timestamps=tuple(
            _time(item) for item in value["source_availability_timestamps"]
        ),
        source_ingestion_timestamps=tuple(
            _time(item) for item in value["source_ingestion_timestamps"]
        ),
        freshness_limit_seconds=int(value["freshness_limit_seconds"]),
        unavailable_components=tuple(
            UnavailableContextComponent(
                ContextComponentType(item["component_type"]),
                str(item["reason"]),
            )
            for item in value["unavailable_components"]
        ),
        component_states=tuple((str(item[0]), str(item[1])) for item in value["component_states"]),
        overall_state=OverallContextState(value["overall_state"]),
        contribution_total=int(value["contribution_total"]),
        policy_id=str(value["policy_id"]),
        policy_hash=str(value["policy_hash"]),
        warnings=_strings(value["warnings"]),
        limitations=_strings(value["limitations"]),
        manifest_hash=str(value["manifest_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
        order_authority="NONE",
        financial_mutation_capability="NONE",
    )


def _attachment(row: RowMapping) -> OpportunityContextAttachment:
    value = _data(row)
    return OpportunityContextAttachment(
        attachment_id=str(value["attachment_id"]),
        version=int(value["version"]),
        projection_id=str(value["projection_id"]),
        context_snapshot_id=str(value["context_snapshot_id"]),
        attached_at=_time(value["attached_at"]),
        relevant_component_ids=_strings(value["relevant_component_ids"]),
        applicability=tuple(
            ContextApplicability(
                ContextComponentType(item["component_type"]),
                ApplicabilityState(item["state"]),
                None if item["component_id"] is None else str(item["component_id"]),
                str(item["reason"]),
            )
            for item in value["applicability"]
        ),
        contextual_observations=_strings(value["contextual_observations"]),
        limitations=_strings(value["limitations"]),
        explanation=str(value["explanation"]),
        policy_id=str(value["policy_id"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        opportunity_authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
        order_authority="NONE",
        financial_mutation_capability="NONE",
    )


def _enriched_set(row: RowMapping) -> ContextEnrichedOpportunitySet:
    value = _data(row)
    return ContextEnrichedOpportunitySet(
        enriched_set_id=str(value["enriched_set_id"]),
        version=int(value["version"]),
        original_opportunity_set_id=str(value["original_opportunity_set_id"]),
        original_opportunity_set_hash=str(value["original_opportunity_set_hash"]),
        original_allocation_id=(
            None
            if value["original_allocation_id"] is None
            else str(value["original_allocation_id"])
        ),
        original_allocation_hash=(
            None
            if value["original_allocation_hash"] is None
            else str(value["original_allocation_hash"])
        ),
        context_snapshot_id=str(value["context_snapshot_id"]),
        attachment_ids=_strings(value["attachment_ids"]),
        attachment_manifest_hash=str(value["attachment_manifest_hash"]),
        eligibility_hash_before=str(value["eligibility_hash_before"]),
        eligibility_hash_after=str(value["eligibility_hash_after"]),
        priority_hash_before=str(value["priority_hash_before"]),
        priority_hash_after=str(value["priority_hash_after"]),
        allocation_hash_before=(
            None
            if value["allocation_hash_before"] is None
            else str(value["allocation_hash_before"])
        ),
        allocation_hash_after=(
            None if value["allocation_hash_after"] is None else str(value["allocation_hash_after"])
        ),
        eligibility_unchanged=bool(value["eligibility_unchanged"]),
        priority_unchanged=bool(value["priority_unchanged"]),
        allocation_unchanged=bool(value["allocation_unchanged"]),
        created_at=_time(value["created_at"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
        order_authority="NONE",
        financial_mutation_capability="NONE",
    )


class PostgresTotalMarketContextRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._shared = PostgresSharedOpportunityRepository(engine)

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _existing_hash(
        connection: sa.Connection, table: sa.Table, identity: str, value: str
    ) -> str | None:
        return connection.scalar(
            sa.select(table.c.canonical_hash).where(table.c[identity] == value)
        )

    def context_component(self, component_id: str) -> ContextComponent | None:
        row = self._one(
            sa.select(component_table).where(component_table.c.component_id == component_id)
        )
        return None if row is None else _component(row)

    def context_snapshot(self, snapshot_id: str) -> TotalMarketContextSnapshot | None:
        row = self._one(
            sa.select(snapshot_table).where(snapshot_table.c.snapshot_id == snapshot_id)
        )
        return None if row is None else _snapshot(row)

    def context_snapshots(self) -> tuple[TotalMarketContextSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(snapshot_table).order_by(
                    snapshot_table.c.created_at, snapshot_table.c.snapshot_id
                )
            ).mappings()
            return tuple(_snapshot(row) for row in rows)

    def context_components(self, snapshot_id: str) -> tuple[ContextComponent, ...]:
        snapshot = self.context_snapshot(snapshot_id)
        if snapshot is None:
            return ()
        components = tuple(
            component
            for component_id in snapshot.component_ids
            if (component := self.context_component(component_id)) is not None
        )
        if len(components) != len(snapshot.component_ids):
            raise TotalMarketContextError("TOTAL_MARKET_SNAPSHOT_COMPONENT_LINEAGE_INCOMPLETE")
        return components

    def enriched_set(self, enriched_set_id: str) -> ContextEnrichedOpportunitySet | None:
        row = self._one(
            sa.select(enriched_set_table).where(
                enriched_set_table.c.enriched_set_id == enriched_set_id
            )
        )
        return None if row is None else _enriched_set(row)

    def enriched_sets(self) -> tuple[ContextEnrichedOpportunitySet, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(enriched_set_table).order_by(
                    enriched_set_table.c.created_at,
                    enriched_set_table.c.enriched_set_id,
                )
            ).mappings()
            return tuple(_enriched_set(row) for row in rows)

    def attachments(self, enriched_set_id: str) -> tuple[OpportunityContextAttachment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(attachment_table)
                .where(attachment_table.c.enriched_set_id == enriched_set_id)
                .order_by(attachment_table.c.projection_id)
            ).mappings()
            return tuple(_attachment(row) for row in rows)

    def shared_opportunity_set(self, opportunity_set_id: str) -> SharedOpportunitySet | None:
        return self._shared.opportunity_set(opportunity_set_id)

    def shared_projections(
        self, opportunity_set_id: str
    ) -> tuple[SharedOpportunityProjection, ...]:
        opportunity_set = self._shared.opportunity_set(opportunity_set_id)
        if opportunity_set is None:
            return ()
        return tuple(
            projection
            for projection_id in opportunity_set.projection_ids
            if (projection := self._shared.projection(projection_id)) is not None
        )

    def shared_eligibility(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]:
        return self._shared.eligibility_decisions(opportunity_set_id)

    def shared_priorities(self, opportunity_set_id: str) -> tuple[PriorityResult, ...]:
        return self._shared.priorities(opportunity_set_id)

    def shared_explanations(self, opportunity_set_id: str) -> tuple[OpportunityExplanation, ...]:
        return self._shared.explanations(opportunity_set_id)

    def shared_allocation(self, opportunity_set_id: str) -> HypotheticalAllocationProposal | None:
        return self._shared.allocation_for_set(opportunity_set_id)

    def save_context_snapshot(
        self,
        *,
        idempotency_key: str,
        snapshot: TotalMarketContextSnapshot,
        components: tuple[ContextComponent, ...],
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = self._existing_hash(
                    connection, snapshot_table, "snapshot_id", snapshot.snapshot_id
                )
                if existing is not None:
                    retained_key = connection.scalar(
                        sa.select(snapshot_table.c.idempotency_key).where(
                            snapshot_table.c.snapshot_id == snapshot.snapshot_id
                        )
                    )
                    if existing != snapshot.canonical_hash or retained_key != idempotency_key:
                        raise TotalMarketContextError("TOTAL_MARKET_SNAPSHOT_IDENTITY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(snapshot_table.c.snapshot_id).where(
                        snapshot_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise TotalMarketContextError("TOTAL_MARKET_SNAPSHOT_IDEMPOTENCY_CONFLICT")
                for component in components:
                    prior = self._existing_hash(
                        connection, component_table, "component_id", component.component_id
                    )
                    if prior is not None and prior != component.canonical_hash:
                        raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_IDENTITY_CONFLICT")
                    if prior is None:
                        connection.execute(
                            component_table.insert().values(
                                component_id=component.component_id,
                                component_type=component.component_type.value,
                                source_observation_id=component.source_observation_id,
                                source_batch_id=snapshot.source_batch_id,
                                observation_at=component.observation_at,
                                context_as_of=snapshot.created_at.date(),
                                payload=_payload(component),
                                canonical_hash=component.canonical_hash,
                                authority="NONE",
                            )
                        )
                connection.execute(
                    snapshot_table.insert().values(
                        snapshot_id=snapshot.snapshot_id,
                        source_batch_id=snapshot.source_batch_id,
                        created_at=snapshot.created_at,
                        manifest_hash=snapshot.manifest_hash,
                        idempotency_key=idempotency_key,
                        payload=_payload(snapshot),
                        canonical_hash=snapshot.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise TotalMarketContextError("TOTAL_MARKET_SNAPSHOT_PERSISTENCE_CONFLICT") from error

    def save_context_enriched_set(
        self,
        *,
        idempotency_key: str,
        enriched_set: ContextEnrichedOpportunitySet,
        attachments: tuple[OpportunityContextAttachment, ...],
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = self._existing_hash(
                    connection,
                    enriched_set_table,
                    "enriched_set_id",
                    enriched_set.enriched_set_id,
                )
                if existing is not None:
                    retained_key = connection.scalar(
                        sa.select(enriched_set_table.c.idempotency_key).where(
                            enriched_set_table.c.enriched_set_id == enriched_set.enriched_set_id
                        )
                    )
                    if existing != enriched_set.canonical_hash or retained_key != idempotency_key:
                        raise TotalMarketContextError("TOTAL_MARKET_ENRICHED_SET_IDENTITY_CONFLICT")
                    retained_attachments = tuple(
                        connection.execute(
                            sa.select(
                                attachment_table.c.attachment_id,
                                attachment_table.c.canonical_hash,
                            )
                            .where(
                                attachment_table.c.enriched_set_id == enriched_set.enriched_set_id
                            )
                            .order_by(attachment_table.c.attachment_id)
                        )
                    )
                    expected_attachments = tuple(
                        sorted(
                            (
                                attachment.attachment_id,
                                attachment.canonical_hash,
                            )
                            for attachment in attachments
                        )
                    )
                    if (
                        tuple(
                            (str(row.attachment_id), str(row.canonical_hash))
                            for row in retained_attachments
                        )
                        != expected_attachments
                    ):
                        raise TotalMarketContextError("TOTAL_MARKET_ATTACHMENT_REPLAY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(enriched_set_table.c.enriched_set_id).where(
                        enriched_set_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise TotalMarketContextError("TOTAL_MARKET_ENRICHED_IDEMPOTENCY_CONFLICT")
                connection.execute(
                    enriched_set_table.insert().values(
                        enriched_set_id=enriched_set.enriched_set_id,
                        original_opportunity_set_id=enriched_set.original_opportunity_set_id,
                        context_snapshot_id=enriched_set.context_snapshot_id,
                        created_at=enriched_set.created_at,
                        idempotency_key=idempotency_key,
                        payload=_payload(enriched_set),
                        canonical_hash=enriched_set.canonical_hash,
                        authority="NONE",
                    )
                )
                for attachment in attachments:
                    connection.execute(
                        attachment_table.insert().values(
                            attachment_id=attachment.attachment_id,
                            enriched_set_id=enriched_set.enriched_set_id,
                            projection_id=attachment.projection_id,
                            context_snapshot_id=attachment.context_snapshot_id,
                            payload=_payload(attachment),
                            canonical_hash=attachment.canonical_hash,
                            authority="NONE",
                        )
                    )
            return False
        except IntegrityError as error:
            raise TotalMarketContextError("TOTAL_MARKET_ENRICHED_PERSISTENCE_CONFLICT") from error

    def workspace(self) -> dict[str, object]:
        enriched_sets = self.enriched_sets()
        if not enriched_sets:
            return {
                "context_snapshot": None,
                "enriched_opportunity_set": None,
                "components": (),
                "attachments": (),
                "unavailable_components": tuple(item.value for item in ContextComponentType),
                "warnings": ("NO_TOTAL_MARKET_CONTEXT_RETAINED",),
                **_authority_payload(),
            }
        enriched = enriched_sets[-1]
        snapshot = self.context_snapshot(enriched.context_snapshot_id)
        if snapshot is None:
            raise TotalMarketContextError("TOTAL_MARKET_WORKSPACE_SNAPSHOT_MISSING")
        return {
            "context_snapshot": snapshot,
            "enriched_opportunity_set": enriched,
            "components": self.context_components(snapshot.snapshot_id),
            "attachments": self.attachments(enriched.enriched_set_id),
            "unavailable_components": snapshot.unavailable_components,
            "ranking_changed": not enriched.priority_unchanged,
            "allocation_changed": not enriched.allocation_unchanged,
            "eligibility_changed": not enriched.eligibility_unchanged,
            "warnings": snapshot.warnings,
            **_authority_payload(),
        }


def _authority_payload() -> dict[str, object]:
    return {
        "evidence_class": "RETAINED_CONTEXT",
        "context_authority": "NONE",
        "opportunity_authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "order_authority": "NONE",
        "financial_mutation_capability": "NONE",
    }
