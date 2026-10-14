"""Durable immutable retention for research-only Portfolio Intelligence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import apex_portfolio_intelligence_run
from apex.application.portfolio_intelligence import PortfolioIntelligenceError
from apex.domain.portfolio_intelligence import (
    AuthorityCeiling,
    NonExecutablePortfolioInstruction,
    OpportunityMaturity,
    OpportunitySource,
    PortfolioDependency,
    PortfolioIntelligenceRun,
    PortfolioOpportunity,
)
from apex.domain.values import canonical_json_bytes, parse_decimal


class PostgresPortfolioIntelligenceRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def run_by_key(self, key: str) -> PortfolioIntelligenceRun | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.select(apex_portfolio_intelligence_run.c.payload).where(
                    apex_portfolio_intelligence_run.c.idempotency_key == key
                )
            ).one_or_none()
        return None if row is None else _run(json.loads(cast(str, row[0])))

    def save_run(self, key: str, run: PortfolioIntelligenceRun) -> PortfolioIntelligenceRun:
        old = self.run_by_key(key)
        if old is not None:
            if old.canonical_hash != run.canonical_hash:
                raise PortfolioIntelligenceError("PORTFOLIO_INTELLIGENCE_IDEMPOTENCY_CONFLICT")
            return old
        with self.engine.begin() as connection:
            connection.execute(
                apex_portfolio_intelligence_run.insert().values(
                    run_id=run.run_id,
                    idempotency_key=key,
                    payload=canonical_json_bytes(run).decode(),
                    canonical_hash=run.canonical_hash,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return run

    def runs(self) -> tuple[PortfolioIntelligenceRun, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.select(apex_portfolio_intelligence_run.c.payload).order_by(
                    apex_portfolio_intelligence_run.c.created_at
                )
            ).all()
        return tuple(_run(json.loads(cast(str, row[0]))) for row in rows)


def _opportunity(value: dict[str, object]) -> PortfolioOpportunity:
    return PortfolioOpportunity(
        opportunity_id=cast(str, value["opportunity_id"]),
        source=OpportunitySource(cast(str, value["source"])),
        source_id=cast(str, value["source_id"]),
        instrument_id=cast(str, value["instrument_id"]),
        expected_net_return=parse_decimal(
            cast(str, value["expected_net_return"]), allow_negative=True
        ),
        positive_probability=parse_decimal(cast(str, value["positive_probability"])),
        downside=parse_decimal(cast(str, value["downside"]), allow_negative=True),
        uncertainty=parse_decimal(cast(str, value["uncertainty"])),
        liquidity=parse_decimal(cast(str, value["liquidity"]), allow_negative=True),
        maturity=OpportunityMaturity(cast(str, value["maturity"])),
        authority_ceiling=AuthorityCeiling(cast(str, value["authority_ceiling"])),
        blockers=tuple(cast(list[str], value["blockers"])),
        canonical_hash=cast(str, value["canonical_hash"]),
    )


def _run(value: dict[str, object]) -> PortfolioIntelligenceRun:
    return PortfolioIntelligenceRun(
        run_id=cast(str, value["run_id"]),
        opportunities=tuple(
            _opportunity(cast(dict[str, object], item))
            for item in cast(list[object], value["opportunities"])
        ),
        desired_allocations=tuple(
            (cast(str, item[0]), Decimal(cast(str, item[1])))
            for item in cast(list[list[object]], value["desired_allocations"])
        ),
        permitted_allocations=tuple(
            (cast(str, item[0]), Decimal(cast(str, item[1])))
            for item in cast(list[list[object]], value["permitted_allocations"])
        ),
        canonical_hash=cast(str, value["canonical_hash"]),
        dependencies=tuple(
            PortfolioDependency(
                dependency_id=cast(str, item["dependency_id"]),
                left_opportunity_id=cast(str, item["left_opportunity_id"]),
                right_opportunity_id=cast(str, item["right_opportunity_id"]),
                dependency_penalty=parse_decimal(cast(str, item["dependency_penalty"])),
                reason_code=cast(str, item["reason_code"]),
                canonical_hash=cast(str, item["canonical_hash"]),
            )
            for item in cast(list[dict[str, object]], value.get("dependencies", []))
        ),
        instructions=tuple(
            NonExecutablePortfolioInstruction(
                instruction_id=cast(str, item["instruction_id"]),
                opportunity_id=cast(str, item["opportunity_id"]),
                desired_allocation=parse_decimal(cast(str, item["desired_allocation"])),
                permitted_allocation=parse_decimal(cast(str, item["permitted_allocation"])),
                maturity=OpportunityMaturity(cast(str, item["maturity"])),
                authority_ceiling=AuthorityCeiling(cast(str, item["authority_ceiling"])),
                blockers=tuple(cast(list[str], item["blockers"])),
                state=cast(str, item["state"]),
                executable=bool(item["executable"]),
                canonical_hash=cast(str, item["canonical_hash"]),
            )
            for item in cast(list[dict[str, object]], value.get("instructions", []))
        ),
    )
