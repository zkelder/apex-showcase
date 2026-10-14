"""Immutable PostgreSQL evidence for the shared Opportunity operating layer."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.shared_opportunity import (
    EligibilityDecision,
    EligibilityState,
    HypotheticalAllocation,
    HypotheticalAllocationProposal,
    OpportunityExplanation,
    PriorityComponent,
    PriorityResult,
    PriorityTier,
    SharedOpportunityError,
    SharedOpportunityProjection,
    SharedOpportunitySet,
    SharedVenue,
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


projection_table = _evidence_table(
    "shared_opportunity_projection",
    "projection_id",
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("source_record_type", sa.String(160), nullable=False),
    sa.Column("source_record_id", sa.String(300), nullable=False),
    sa.Column("source_canonical_hash", sa.String(64), nullable=False),
    sa.UniqueConstraint(
        "source_record_type",
        "source_record_id",
        name="uq_shared_opportunity_source_identity",
    ),
)
set_table = _evidence_table(
    "shared_opportunity_set",
    "opportunity_set_id",
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("eligibility_payload", sa.Text(), nullable=False),
)
priority_table = _evidence_table(
    "shared_opportunity_priority",
    "priority_id",
    sa.Column(
        "opportunity_set_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
        nullable=False,
    ),
    sa.Column(
        "projection_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.Column("rank", sa.Integer(), nullable=False),
    sa.UniqueConstraint(
        "opportunity_set_id", "projection_id", name="uq_shared_priority_set_projection"
    ),
    sa.UniqueConstraint("opportunity_set_id", "rank", name="uq_shared_priority_set_rank"),
)
explanation_table = _evidence_table(
    "shared_opportunity_explanation",
    "explanation_id",
    sa.Column(
        "opportunity_set_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
        nullable=False,
    ),
    sa.Column(
        "projection_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "opportunity_set_id",
        "projection_id",
        name="uq_shared_explanation_set_projection",
    ),
)
allocation_table = _evidence_table(
    "shared_research_allocation_proposal",
    "proposal_id",
    sa.Column(
        "opportunity_set_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
        nullable=False,
    ),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise SharedOpportunityError("SHARED_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SharedOpportunityError("SHARED_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise SharedOpportunityError("SHARED_RETAINED_PAIRS_INVALID")
    return tuple((str(item[0]), str(item[1])) for item in value)


def _projection(row: RowMapping) -> SharedOpportunityProjection:
    value = _data(row)
    return SharedOpportunityProjection(
        projection_id=str(value["projection_id"]),
        version=int(value["version"]),
        venue=SharedVenue(value["venue"]),
        source_record_type=str(value["source_record_type"]),
        source_record_id=str(value["source_record_id"]),
        source_canonical_hash=str(value["source_canonical_hash"]),
        strategy_or_method_id=str(value["strategy_or_method_id"]),
        instrument_id=str(value["instrument_id"]),
        product_or_market_id=str(value["product_or_market_id"]),
        direction=str(value["direction"]),
        generated_at=_time(value["generated_at"]),
        valid_until=_optional_time(value["valid_until"]),
        evidence_class=str(value["evidence_class"]),
        freshness_seconds=int(value["freshness_seconds"]),
        freshness_limit_seconds=int(value["freshness_limit_seconds"]),
        expected_gross_return=_optional_decimal(value["expected_gross_return"]),
        expected_net_return=_optional_decimal(value["expected_net_return"]),
        comparable_value=_optional_decimal(value["comparable_value"]),
        comparable_value_identity=(
            None
            if value["comparable_value_identity"] is None
            else str(value["comparable_value_identity"])
        ),
        uncertainty=_optional_decimal(value["uncertainty"]),
        maximum_loss=_optional_decimal(value["maximum_loss"]),
        modeled_fees=_optional_decimal(value["modeled_fees"]),
        modeled_slippage=_optional_decimal(value["modeled_slippage"]),
        costs_retained=bool(value["costs_retained"]),
        liquidity_state=str(value["liquidity_state"]),
        liquidity_limitations=_strings(value["liquidity_limitations"]),
        required_hypothetical_capital=_optional_decimal(value["required_hypothetical_capital"]),
        maximum_sensible_research_allocation=_optional_decimal(
            value["maximum_sensible_research_allocation"]
        ),
        source_maximum_allocation_fraction=_optional_decimal(
            value["source_maximum_allocation_fraction"]
        ),
        expected_holding_days=(
            None if value["expected_holding_days"] is None else int(value["expected_holding_days"])
        ),
        estimated_lockup_days=(
            None if value["estimated_lockup_days"] is None else int(value["estimated_lockup_days"])
        ),
        supporting_evidence_ids=_strings(value["supporting_evidence_ids"]),
        source_warnings=_strings(value["source_warnings"]),
        source_rejection_reasons=_strings(value["source_rejection_reasons"]),
        source_authority=str(value["source_authority"]),
        projection_limitations=_strings(value["projection_limitations"]),
        source_details=_pairs(value["source_details"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        research_only=True,
        paper_eligible=bool(value.get("paper_eligible", False)),
        live_eligible=False,
        capital_authority="NONE",
        financial_authority="NONE",
        paper_eligibility_reasons=_strings(value.get("paper_eligibility_reasons", [])),
        statistical_sample_size=(
            None
            if value.get("statistical_sample_size") is None
            else int(value["statistical_sample_size"])
        ),
    )


def _decision(value: dict[str, Any]) -> EligibilityDecision:
    return EligibilityDecision(
        decision_id=str(value["decision_id"]),
        projection_id=str(value["projection_id"]),
        policy_id=str(value["policy_id"]),
        evaluated_at=_time(value["evaluated_at"]),
        state=EligibilityState(value["state"]),
        reasons=_strings(value["reasons"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _component(value: dict[str, Any]) -> PriorityComponent:
    return PriorityComponent(
        name=str(value["name"]),
        raw_value=_optional_decimal(value["raw_value"]),
        normalized_value=Decimal(str(value["normalized_value"])),
        weight=Decimal(str(value["weight"])),
        weighted_value=Decimal(str(value["weighted_value"])),
        missing_behavior=(
            None if value["missing_behavior"] is None else str(value["missing_behavior"])
        ),
        explanation=str(value["explanation"]),
    )


def _priority(row: RowMapping) -> PriorityResult:
    value = _data(row)
    components = value["components"]
    if not isinstance(components, list):
        raise SharedOpportunityError("SHARED_PRIORITY_COMPONENTS_INVALID")
    return PriorityResult(
        priority_id=str(value["priority_id"]),
        projection_id=str(value["projection_id"]),
        policy_id=str(value["policy_id"]),
        score=Decimal(str(value["score"])),
        rank=int(value["rank"]),
        tie_break_key=tuple(str(item) for item in value["tie_break_key"]),  # type: ignore[arg-type]
        components=tuple(_component(item) for item in components),
        explanation=str(value["explanation"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        # Retained before Evidence-Weighted Cross-Venue Opportunity Ranking v1 -> no tier was
        # ever computed; default to the conservative RESEARCH_ONLY reading, never ACTIONABLE.
        tier=PriorityTier(value.get("tier", PriorityTier.RESEARCH_ONLY.value)),
        tier_reasons=_strings(value.get("tier_reasons", [])),
    )


def _opportunity_set(row: RowMapping) -> SharedOpportunitySet:
    value = _data(row)
    return SharedOpportunitySet(
        opportunity_set_id=str(value["opportunity_set_id"]),
        created_at=_time(value["created_at"]),
        projection_ids=_strings(value["projection_ids"]),
        eligibility_decision_ids=_strings(value["eligibility_decision_ids"]),
        priority_result_ids=_strings(value["priority_result_ids"]),
        ranked_projection_ids=_strings(value["ranked_projection_ids"]),
        tie_groups=tuple(tuple(str(item) for item in group) for group in value["tie_groups"]),
        expired_projection_ids=_strings(value["expired_projection_ids"]),
        unavailable_projection_ids=_strings(value["unavailable_projection_ids"]),
        manifest_hash=str(value["manifest_hash"]),
        policy_ids=tuple(str(item) for item in value["policy_ids"]),  # type: ignore[arg-type]
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
    )


def _explanation(row: RowMapping) -> OpportunityExplanation:
    value = _data(row)
    return OpportunityExplanation(
        explanation_id=str(value["explanation_id"]),
        opportunity_set_id=str(value["opportunity_set_id"]),
        projection_id=str(value["projection_id"]),
        eligibility_state=EligibilityState(value["eligibility_state"]),
        text=str(value["text"]),
        limiting_missing_evidence=_strings(value["limiting_missing_evidence"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _allocation(row: RowMapping) -> HypotheticalAllocationProposal:
    value = _data(row)
    allocations = tuple(
        HypotheticalAllocation(
            projection_id=str(item["projection_id"]),
            venue=SharedVenue(item["venue"]),
            amount=Decimal(str(item["amount"])),
            modeled_downside=Decimal(str(item["modeled_downside"])),
            modeled_costs=Decimal(str(item["modeled_costs"])),
            estimated_lockup_days=(
                None
                if item["estimated_lockup_days"] is None
                else int(item["estimated_lockup_days"])
            ),
            explanation=str(item["explanation"]),
        )
        for item in value["allocations"]
    )
    return HypotheticalAllocationProposal(
        proposal_id=str(value["proposal_id"]),
        opportunity_set_id=str(value["opportunity_set_id"]),
        priority_policy_id=str(value["priority_policy_id"]),
        hypothetical_budget=Decimal(str(value["hypothetical_budget"])),
        allocations=allocations,
        unallocated_cash=Decimal(str(value["unallocated_cash"])),
        venue_totals=tuple((str(item[0]), Decimal(str(item[1]))) for item in value["venue_totals"]),
        modeled_downside=Decimal(str(value["modeled_downside"])),
        modeled_costs=Decimal(str(value["modeled_costs"])),
        estimated_lockup_days=(
            None if value["estimated_lockup_days"] is None else int(value["estimated_lockup_days"])
        ),
        warnings=_strings(value["warnings"]),
        rejection_reasons=_strings(value["rejection_reasons"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        account_capital=False,
        reservation=False,
        order=False,
        paper_eligible=False,
        live_eligible=False,
        financial_authority="NONE",
    )


class PostgresSharedOpportunityRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def projection(self, projection_id: str) -> SharedOpportunityProjection | None:
        row = self._one(
            sa.select(projection_table).where(projection_table.c.projection_id == projection_id)
        )
        return None if row is None else _projection(row)

    def projections_by_id(
        self, projection_ids: Collection[str]
    ) -> dict[str, SharedOpportunityProjection]:
        """Bounded, batched alternative to calling ``projection()`` once per
        id -- one ``WHERE projection_id IN (...)`` round trip instead of len
        (``projection_ids``) individual indexed lookups.  Read-only; never
        mutates retained evidence.  Added for the Operator Console's
        ``/followups`` route (Operator Console Follow-Up Performance v1),
        which previously called ``projection()`` once per retained decision
        (521 individual round trips at current production scale) -- any
        other caller wanting many known ids at once may reuse this rather
        than looping ``projection()`` itself.  An id with no retained
        projection is simply absent from the result (never a ``None``
        entry), so ``result.get(projection_id)`` is the correct way to read
        it back, matching ``projection()``'s own "not retained" contract."""

        if not projection_ids:
            return {}
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(projection_table).where(
                    projection_table.c.projection_id.in_(projection_ids)
                )
            ).mappings()
            return {row["projection_id"]: _projection(row) for row in rows}

    def opportunity_set(self, opportunity_set_id: str) -> SharedOpportunitySet | None:
        row = self._one(
            sa.select(set_table).where(set_table.c.opportunity_set_id == opportunity_set_id)
        )
        return None if row is None else _opportunity_set(row)

    def opportunity_sets(self) -> tuple[SharedOpportunitySet, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(set_table).order_by(
                    set_table.c.created_at, set_table.c.opportunity_set_id
                )
            ).mappings()
            return tuple(_opportunity_set(row) for row in rows)

    def eligibility_decisions(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]:
        row = self._one(
            sa.select(set_table.c.eligibility_payload).where(
                set_table.c.opportunity_set_id == opportunity_set_id
            )
        )
        if row is None:
            return ()
        payload = json.loads(str(row["eligibility_payload"]))
        if not isinstance(payload, list):
            raise SharedOpportunityError("SHARED_ELIGIBILITY_PAYLOAD_INVALID")
        return tuple(_decision(item) for item in payload)

    def priorities(self, opportunity_set_id: str) -> tuple[PriorityResult, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(priority_table)
                .where(priority_table.c.opportunity_set_id == opportunity_set_id)
                .order_by(priority_table.c.rank)
            ).mappings()
            return tuple(_priority(row) for row in rows)

    def explanations(self, opportunity_set_id: str) -> tuple[OpportunityExplanation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(explanation_table)
                .where(explanation_table.c.opportunity_set_id == opportunity_set_id)
                .order_by(explanation_table.c.projection_id)
            ).mappings()
            return tuple(_explanation(row) for row in rows)

    def allocation(self, proposal_id: str) -> HypotheticalAllocationProposal | None:
        row = self._one(
            sa.select(allocation_table).where(allocation_table.c.proposal_id == proposal_id)
        )
        return None if row is None else _allocation(row)

    def allocation_for_set(self, opportunity_set_id: str) -> HypotheticalAllocationProposal | None:
        row = self._one(
            sa.select(allocation_table).where(
                allocation_table.c.opportunity_set_id == opportunity_set_id
            )
        )
        return None if row is None else _allocation(row)

    @staticmethod
    def _existing_hash(
        connection: sa.Connection, table: sa.Table, identity: str, value: str
    ) -> str | None:
        return connection.scalar(
            sa.select(table.c.canonical_hash).where(table.c[identity] == value)
        )

    def save_opportunity_bundle(
        self,
        *,
        idempotency_key: str,
        opportunity_set: SharedOpportunitySet,
        projections: tuple[SharedOpportunityProjection, ...],
        decisions: tuple[EligibilityDecision, ...],
        priorities: tuple[PriorityResult, ...],
        explanations: tuple[OpportunityExplanation, ...],
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = self._existing_hash(
                    connection,
                    set_table,
                    "opportunity_set_id",
                    opportunity_set.opportunity_set_id,
                )
                if existing is not None:
                    retained_key = connection.scalar(
                        sa.select(set_table.c.idempotency_key).where(
                            set_table.c.opportunity_set_id == opportunity_set.opportunity_set_id
                        )
                    )
                    if (
                        existing != opportunity_set.canonical_hash
                        or retained_key != idempotency_key
                    ):
                        raise SharedOpportunityError("SHARED_OPPORTUNITY_SET_IDENTITY_CONFLICT")
                    return True
                key_hash = connection.scalar(
                    sa.select(set_table.c.canonical_hash).where(
                        set_table.c.idempotency_key == idempotency_key
                    )
                )
                if key_hash is not None:
                    raise SharedOpportunityError("SHARED_OPPORTUNITY_IDEMPOTENCY_CONFLICT")
                for item in projections:
                    prior = self._existing_hash(
                        connection, projection_table, "projection_id", item.projection_id
                    )
                    if prior is not None and prior != item.canonical_hash:
                        raise SharedOpportunityError(
                            "SHARED_OPPORTUNITY_PROJECTION_IDENTITY_CONFLICT"
                        )
                    if prior is None:
                        connection.execute(
                            projection_table.insert().values(
                                projection_id=item.projection_id,
                                venue=item.venue.value,
                                source_record_type=item.source_record_type,
                                source_record_id=item.source_record_id,
                                source_canonical_hash=item.source_canonical_hash,
                                payload=_payload(item),
                                canonical_hash=item.canonical_hash,
                                authority="NONE",
                            )
                        )
                connection.execute(
                    set_table.insert().values(
                        opportunity_set_id=opportunity_set.opportunity_set_id,
                        created_at=opportunity_set.created_at,
                        manifest_hash=opportunity_set.manifest_hash,
                        idempotency_key=idempotency_key,
                        eligibility_payload=_payload(decisions),
                        payload=_payload(opportunity_set),
                        canonical_hash=opportunity_set.canonical_hash,
                        authority="NONE",
                    )
                )
                for priority in priorities:
                    connection.execute(
                        priority_table.insert().values(
                            priority_id=priority.priority_id,
                            opportunity_set_id=opportunity_set.opportunity_set_id,
                            projection_id=priority.projection_id,
                            rank=priority.rank,
                            payload=_payload(priority),
                            canonical_hash=priority.canonical_hash,
                            authority="NONE",
                        )
                    )
                for explanation in explanations:
                    connection.execute(
                        explanation_table.insert().values(
                            explanation_id=explanation.explanation_id,
                            opportunity_set_id=opportunity_set.opportunity_set_id,
                            projection_id=explanation.projection_id,
                            payload=_payload(explanation),
                            canonical_hash=explanation.canonical_hash,
                            authority="NONE",
                        )
                    )
            return False
        except IntegrityError as error:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_PERSISTENCE_CONFLICT") from error

    def save_allocation(
        self, *, idempotency_key: str, proposal: HypotheticalAllocationProposal
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                prior = self._existing_hash(
                    connection, allocation_table, "proposal_id", proposal.proposal_id
                )
                if prior is not None:
                    retained_key = connection.scalar(
                        sa.select(allocation_table.c.idempotency_key).where(
                            allocation_table.c.proposal_id == proposal.proposal_id
                        )
                    )
                    if prior != proposal.canonical_hash or retained_key != idempotency_key:
                        raise SharedOpportunityError("SHARED_ALLOCATION_IDENTITY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(allocation_table.c.proposal_id).where(
                        allocation_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise SharedOpportunityError("SHARED_ALLOCATION_IDEMPOTENCY_CONFLICT")
                connection.execute(
                    allocation_table.insert().values(
                        proposal_id=proposal.proposal_id,
                        opportunity_set_id=proposal.opportunity_set_id,
                        idempotency_key=idempotency_key,
                        payload=_payload(proposal),
                        canonical_hash=proposal.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise SharedOpportunityError("SHARED_ALLOCATION_PERSISTENCE_CONFLICT") from error

    def workspace(self) -> dict[str, object]:
        sets = self.opportunity_sets()
        if not sets:
            return {
                "opportunity_set": None,
                "crypto_count": 0,
                "kalshi_count": 0,
                "eligible_count": 0,
                "rejected_count": 0,
                "ranked_opportunities": (),
                "rejected_opportunities": (),
                "allocation": None,
                "warnings": ("NO_SHARED_OPPORTUNITY_SET_RETAINED",),
                "authority": "NONE",
                "financial_authority": "NONE",
            }
        current = sets[-1]
        projections = tuple(
            value
            for projection_id in current.projection_ids
            if (value := self.projection(projection_id)) is not None
        )
        decisions = self.eligibility_decisions(current.opportunity_set_id)
        decision_by_id = {item.projection_id: item for item in decisions}
        priority_by_id = {
            item.projection_id: item for item in self.priorities(current.opportunity_set_id)
        }
        explanation_by_id = {
            item.projection_id: item for item in self.explanations(current.opportunity_set_id)
        }
        rows = tuple(
            {
                "projection": item,
                "eligibility": decision_by_id[item.projection_id],
                "priority": priority_by_id.get(item.projection_id),
                "explanation": explanation_by_id[item.projection_id],
            }
            for item in projections
        )
        return {
            "opportunity_set": current,
            "crypto_count": sum(item.venue is SharedVenue.CRYPTO for item in projections),
            "kalshi_count": sum(item.venue is SharedVenue.KALSHI for item in projections),
            "eligible_count": sum(item.state is EligibilityState.ELIGIBLE for item in decisions),
            "rejected_count": sum(
                item.state is not EligibilityState.ELIGIBLE for item in decisions
            ),
            "ranked_opportunities": tuple(
                sorted(
                    (row for row in rows if row["priority"] is not None),
                    key=lambda row: row["priority"].rank,  # type: ignore[union-attr]
                )
            ),
            "rejected_opportunities": tuple(row for row in rows if row["priority"] is None),
            "allocation": self.allocation_for_set(current.opportunity_set_id),
            "warnings": current.limitations,
            "authority": "NONE",
            "financial_authority": "NONE",
            "paper_eligible": False,
            "live_eligible": False,
            "total_market_context": None,
        }
