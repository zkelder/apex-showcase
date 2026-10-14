"""Immutable PostgreSQL evidence for deterministic cross-venue allocation."""

from __future__ import annotations

import json
import types
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast, get_args, get_origin, get_type_hints

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.crypto_paper_postgres import (
    account_table as crypto_account_table,
)
from apex.adapters.crypto_paper_postgres import (
    capability_table as crypto_capability_table,
)
from apex.adapters.kalshi_operating_postgres import (
    account_table as kalshi_account_table,
)
from apex.adapters.kalshi_operating_postgres import (
    capability_table as kalshi_capability_table,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.domain.cross_venue_allocation import (
    CROSS_VENUE_ALLOCATION_POLICY_V1,
    AllocationConstraintExplanation,
    CrossVenueAllocationDecision,
    CrossVenueAllocationError,
    CrossVenueAllocationLine,
    CrossVenueAllocationPolicy,
    PaperTrialProposal,
    VenuePaperReadiness,
)
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    PriorityResult,
    SharedOpportunityProjection,
    SharedOpportunitySet,
    SharedVenue,
)
from apex.domain.total_market_context import (
    ContextEnrichedOpportunitySet,
    TotalMarketContextSnapshot,
)
from apex.domain.values import canonical_json_bytes, parse_decimal

metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("live_authority", sa.String(16), nullable=False),
    )


policy_table = _table("cross_venue_allocation_policy", "policy_id")
decision_table = _table(
    "cross_venue_allocation_decision",
    "decision_id",
    sa.Column(
        "policy_id",
        sa.String(300),
        sa.ForeignKey("cross_venue_allocation_policy.policy_id"),
        nullable=False,
    ),
    sa.Column(
        "enriched_set_id",
        sa.String(300),
        sa.ForeignKey("context_enriched_opportunity_set.enriched_set_id"),
        nullable=False,
    ),
    sa.Column(
        "opportunity_set_id",
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
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.UniqueConstraint("enriched_set_id", name="uq_cross_venue_decision_enriched_set"),
)
line_table = _table(
    "cross_venue_allocation_line",
    "line_id",
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("cross_venue_allocation_decision.decision_id"),
        nullable=False,
    ),
    sa.Column(
        "projection_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.Column("amount", sa.Numeric(38, 8), nullable=False),
    sa.UniqueConstraint("decision_id", "projection_id", name="uq_cross_venue_line_projection"),
)
explanation_table = _table(
    "cross_venue_constraint_explanation",
    "explanation_id",
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("cross_venue_allocation_decision.decision_id"),
        nullable=False,
    ),
    sa.Column(
        "projection_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.UniqueConstraint(
        "decision_id", "projection_id", name="uq_cross_venue_explanation_projection"
    ),
)
proposal_table = _table(
    "cross_venue_paper_trial_proposal",
    "proposal_id",
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("cross_venue_allocation_decision.decision_id"),
        nullable=False,
    ),
    sa.Column(
        "line_id",
        sa.String(300),
        sa.ForeignKey("cross_venue_allocation_line.line_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CrossVenueAllocationError("CROSS_VENUE_RETAINED_PAYLOAD_INVALID")
    return value


def _restore(type_hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(type_hint)
    arguments = get_args(type_hint)
    if origin is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(_restore(arguments[0], item) for item in value)
        return tuple(_restore(hint, item) for hint, item in zip(arguments, value, strict=True))
    if origin in {types.UnionType, getattr(types, "UnionType", object)}:
        possible = tuple(item for item in arguments if item is not type(None))
        return _restore(possible[0], value) if possible else None
    if type_hint is Decimal:
        return parse_decimal(str(value), allow_negative=True)
    if type_hint is datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    if isinstance(type_hint, type) and issubclass(type_hint, Enum):
        return type_hint(value)
    if isinstance(type_hint, type) and is_dataclass(type_hint):
        hints = get_type_hints(type_hint)
        return type_hint(
            **{
                field.name: _restore(hints[field.name], value[field.name])
                for field in fields(type_hint)
            }
        )
    return value


def _object[T](row: RowMapping, result_type: type[T]) -> T:
    return cast(T, _restore(result_type, _data(row)))


class PostgresCrossVenueAllocationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def policy(self, policy_id: str) -> CrossVenueAllocationPolicy | None:
        row = self._one(sa.select(policy_table).where(policy_table.c.policy_id == policy_id))
        return None if row is None else _object(row, CrossVenueAllocationPolicy)

    def decision(self, decision_id: str) -> CrossVenueAllocationDecision | None:
        row = self._one(
            sa.select(decision_table).where(decision_table.c.decision_id == decision_id)
        )
        return None if row is None else _object(row, CrossVenueAllocationDecision)

    def decisions(self) -> tuple[CrossVenueAllocationDecision, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(decision_table).order_by(
                    decision_table.c.decided_at, decision_table.c.decision_id
                )
            ).mappings()
            return tuple(_object(row, CrossVenueAllocationDecision) for row in rows)

    def decision_for_enriched_set(
        self, enriched_set_id: str
    ) -> CrossVenueAllocationDecision | None:
        row = self._one(
            sa.select(decision_table).where(decision_table.c.enriched_set_id == enriched_set_id)
        )
        return None if row is None else _object(row, CrossVenueAllocationDecision)

    def lines(self, decision_id: str) -> tuple[CrossVenueAllocationLine, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(line_table)
                .where(line_table.c.decision_id == decision_id)
                .order_by(line_table.c.line_id)
            ).mappings()
            return tuple(_object(row, CrossVenueAllocationLine) for row in rows)

    def explanations(self, decision_id: str) -> tuple[AllocationConstraintExplanation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(explanation_table)
                .where(explanation_table.c.decision_id == decision_id)
                .order_by(explanation_table.c.projection_id)
            ).mappings()
            return tuple(_object(row, AllocationConstraintExplanation) for row in rows)

    def proposals(self, decision_id: str) -> tuple[PaperTrialProposal, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(proposal_table)
                .where(proposal_table.c.decision_id == decision_id)
                .order_by(proposal_table.c.proposal_id)
            ).mappings()
            return tuple(_object(row, PaperTrialProposal) for row in rows)

    def proposal(self, proposal_id: str) -> PaperTrialProposal | None:
        row = self._one(
            sa.select(proposal_table).where(proposal_table.c.proposal_id == proposal_id)
        )
        return None if row is None else _object(row, PaperTrialProposal)

    def save_bundle(
        self,
        *,
        idempotency_key: str,
        policy: CrossVenueAllocationPolicy,
        decision: CrossVenueAllocationDecision,
        lines: tuple[CrossVenueAllocationLine, ...],
        explanations: tuple[AllocationConstraintExplanation, ...],
        proposals: tuple[PaperTrialProposal, ...],
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(decision_table.c.canonical_hash).where(
                        decision_table.c.decision_id == decision.decision_id
                    )
                )
                if existing is not None:
                    key = connection.scalar(
                        sa.select(decision_table.c.idempotency_key).where(
                            decision_table.c.decision_id == decision.decision_id
                        )
                    )
                    if existing != decision.canonical_hash or key != idempotency_key:
                        raise CrossVenueAllocationError("CROSS_VENUE_DECISION_IDENTITY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(decision_table.c.decision_id).where(
                        decision_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise CrossVenueAllocationError("CROSS_VENUE_IDEMPOTENCY_CONFLICT")
                retained_policy = connection.scalar(
                    sa.select(policy_table.c.canonical_hash).where(
                        policy_table.c.policy_id == policy.policy_id
                    )
                )
                if retained_policy is None:
                    connection.execute(
                        policy_table.insert().values(
                            policy_id=policy.policy_id,
                            payload=_payload(policy),
                            canonical_hash=policy.canonical_hash,
                            authority="NONE",
                            live_authority="NONE",
                        )
                    )
                elif retained_policy != policy.canonical_hash:
                    raise CrossVenueAllocationError("CROSS_VENUE_POLICY_IDENTITY_CONFLICT")
                connection.execute(
                    decision_table.insert().values(
                        decision_id=decision.decision_id,
                        policy_id=decision.policy_id,
                        enriched_set_id=decision.enriched_set_id,
                        opportunity_set_id=decision.opportunity_set_id,
                        context_snapshot_id=decision.context_snapshot_id,
                        decided_at=decision.decided_at,
                        idempotency_key=idempotency_key,
                        payload=_payload(decision),
                        canonical_hash=decision.canonical_hash,
                        authority="NONE",
                        live_authority="NONE",
                    )
                )
                for line in lines:
                    connection.execute(
                        line_table.insert().values(
                            line_id=line.line_id,
                            decision_id=line.decision_id,
                            projection_id=line.projection_id,
                            amount=line.amount,
                            payload=_payload(line),
                            canonical_hash=line.canonical_hash,
                            authority="NONE",
                            live_authority="NONE",
                        )
                    )
                for explanation in explanations:
                    connection.execute(
                        explanation_table.insert().values(
                            explanation_id=explanation.explanation_id,
                            decision_id=explanation.decision_id,
                            projection_id=explanation.projection_id,
                            payload=_payload(explanation),
                            canonical_hash=explanation.canonical_hash,
                            authority="NONE",
                            live_authority="NONE",
                        )
                    )
                for proposal in proposals:
                    connection.execute(
                        proposal_table.insert().values(
                            proposal_id=proposal.proposal_id,
                            decision_id=proposal.decision_id,
                            line_id=proposal.allocation_line_id,
                            venue=proposal.venue.value,
                            expires_at=proposal.expires_at,
                            payload=_payload(proposal),
                            canonical_hash=proposal.canonical_hash,
                            authority="NONE",
                            live_authority="NONE",
                        )
                    )
            return False
        except IntegrityError as error:
            raise CrossVenueAllocationError("CROSS_VENUE_PERSISTENCE_CONFLICT") from error

    def workspace(self) -> dict[str, object]:
        decisions = self.decisions()
        if not decisions:
            return {
                "policy": CrossVenueAllocationPolicy.v1(),
                "decision": None,
                "lines": (),
                "explanations": (),
                "paper_trial_proposals": (),
                "warnings": ("NO_CROSS_VENUE_ALLOCATION_RETAINED",),
                "authority": "NONE",
                "live_authority": "NONE",
            }
        decision = decisions[-1]
        return {
            "policy": self.policy(CROSS_VENUE_ALLOCATION_POLICY_V1),
            "decision": decision,
            "lines": self.lines(decision.decision_id),
            "explanations": self.explanations(decision.decision_id),
            "paper_trial_proposals": self.proposals(decision.decision_id),
            "authority": "NONE",
            "paper_authorization_created": False,
            "orders_created": 0,
            "live_authority": "NONE",
        }


class PostgresCrossVenueAllocationSource:
    """Read exact retained source evidence and PAPER readiness without mutation."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._context = PostgresTotalMarketContextRepository(engine)

    def enriched_set(self, enriched_set_id: str) -> ContextEnrichedOpportunitySet | None:
        return self._context.enriched_set(enriched_set_id)

    def context_snapshot(self, snapshot_id: str) -> TotalMarketContextSnapshot | None:
        return self._context.context_snapshot(snapshot_id)

    def opportunity_set(self, opportunity_set_id: str) -> SharedOpportunitySet | None:
        return self._shared.opportunity_set(opportunity_set_id)

    def projections(self, opportunity_set_id: str) -> tuple[SharedOpportunityProjection, ...]:
        value = self._shared.opportunity_set(opportunity_set_id)
        if value is None:
            return ()
        return tuple(
            item
            for identity in value.projection_ids
            if (item := self._shared.projection(identity)) is not None
        )

    def eligibility(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]:
        return self._shared.eligibility_decisions(opportunity_set_id)

    def priorities(self, opportunity_set_id: str) -> tuple[PriorityResult, ...]:
        return self._shared.priorities(opportunity_set_id)

    def paper_readiness(self) -> tuple[VenuePaperReadiness, ...]:
        boundaries: list[VenuePaperReadiness] = []
        with self._engine.connect() as connection:
            for venue, capability_table, account_table in (
                (SharedVenue.CRYPTO, crypto_capability_table, crypto_account_table),
                (SharedVenue.KALSHI, kalshi_capability_table, kalshi_account_table),
            ):
                capability = (
                    connection.execute(
                        sa.select(capability_table)
                        .order_by(
                            capability_table.c.inspected_at.desc(),
                            capability_table.c.capability_id.desc(),
                        )
                        .limit(1)
                    )
                    .mappings()
                    .one_or_none()
                )
                account = (
                    connection.execute(
                        sa.select(account_table)
                        .order_by(
                            account_table.c.created_at.desc(), account_table.c.account_id.desc()
                        )
                        .limit(1)
                    )
                    .mappings()
                    .one_or_none()
                )
                reasons: list[str] = []
                if capability is None:
                    reasons.append("PAPER_CAPABILITY_NOT_RETAINED")
                else:
                    capability_payload = json.loads(str(capability["payload"]))
                    if (
                        not isinstance(capability_payload, dict)
                        or capability_payload.get("primary_state") != "PAPER_ONLY"
                        or capability["authority"] != "PAPER_ONLY"
                        or capability["live_authority"] != "NONE"
                    ):
                        reasons.append("PAPER_CAPABILITY_INCOMPATIBLE")
                if account is None:
                    reasons.append("PAPER_ACCOUNT_NOT_RETAINED")
                else:
                    account_payload = json.loads(str(account["payload"]))
                    if (
                        not isinstance(account_payload, dict)
                        or account_payload.get("status") != "ACTIVE"
                        or account["authority"] != "PAPER_ONLY"
                        or account["live_authority"] != "NONE"
                    ):
                        reasons.append("PAPER_ACCOUNT_INCOMPATIBLE")
                boundaries.append(
                    VenuePaperReadiness(
                        venue=venue,
                        available=not reasons,
                        capability_id=None
                        if capability is None
                        else str(capability["capability_id"]),
                        account_id=None if account is None else str(account["account_id"]),
                        reason_codes=tuple(reasons),
                    )
                )
        return tuple(boundaries)
