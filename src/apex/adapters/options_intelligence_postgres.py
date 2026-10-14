"""PostgreSQL repository for durable options-intelligence reference evidence.

This is the durable boundary for what ``options_intelligence`` owns: honest
provider capability evidence, provider-neutral option contract *reference*
records (strike/expiration/type identity only — never a quote, Greek, or
implied-volatility observation), the optionable-underlying decisions derived
from that reference evidence, and any retained defined-risk SHADOW proposal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.options_intelligence import (
    DefinedRiskOptionsShadowProposal,
    OptionableUnderlyingDecision,
    OptionContract,
    OptionProviderCapabilities,
)
from apex.domain.values import canonical_json_bytes

OPTIONS_INTELLIGENCE_TABLES = (
    "options_intelligence_capability",
    "options_intelligence_contract",
    "options_intelligence_optionable_underlying",
    "options_intelligence_shadow_proposal",
)
# Bounds every chunked query issued by save_contracts() against the genuine
# official options-directory scale (a single capture can retain well over a
# million contracts): keeps per-statement bound-parameter counts well under
# the driver's limit while keeping round-trip count small.
_CONTRACT_BATCH_SIZE = 2_000

metadata = sa.MetaData()

options_intelligence_capability = sa.Table(
    "options_intelligence_capability",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("provider", sa.String(80), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_intelligence_contract = sa.Table(
    "options_intelligence_contract",
    metadata,
    sa.Column("canonical_contract_id", sa.String(160), primary_key=True),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("option_type", sa.String(8), nullable=False),
    sa.Column("expiration", sa.String(10), nullable=False),
    sa.Column("source_provider", sa.String(80), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_intelligence_optionable_underlying = sa.Table(
    "options_intelligence_optionable_underlying",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("canonical_security_id", sa.String(160), nullable=False),
    sa.Column("optionable", sa.Boolean(), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_intelligence_shadow_proposal = sa.Table(
    "options_intelligence_shadow_proposal",
    metadata,
    sa.Column("idempotency_key", sa.String(160), primary_key=True),
    sa.Column("proposal_id", sa.String(160), nullable=False),
    sa.Column("discovery_run_id", sa.String(160), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class OptionsIntelligencePersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise OptionsIntelligencePersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _capability(row: RowMapping) -> OptionProviderCapabilities:
    return rehydrate(OptionProviderCapabilities, _decoded(row))


def _contract(row: RowMapping) -> OptionContract:
    return rehydrate(OptionContract, _decoded(row))


def _optionable(row: RowMapping) -> OptionableUnderlyingDecision:
    return rehydrate(OptionableUnderlyingDecision, _decoded(row))


def _proposal(row: RowMapping) -> DefinedRiskOptionsShadowProposal:
    return rehydrate(DefinedRiskOptionsShadowProposal, _decoded(row))


class PostgresOptionsIntelligenceRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def proposal_by_key(self, key: str) -> DefinedRiskOptionsShadowProposal | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_intelligence_shadow_proposal).where(
                        options_intelligence_shadow_proposal.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _proposal(row) if row is not None else None

    def save_proposal(
        self, key: str, proposal: DefinedRiskOptionsShadowProposal
    ) -> DefinedRiskOptionsShadowProposal:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    options_intelligence_shadow_proposal.insert().values(
                        idempotency_key=key,
                        proposal_id=proposal.proposal_id,
                        discovery_run_id=proposal.discovery_run_id,
                        recorded_at=datetime.now(tz=UTC),
                        canonical_hash=proposal.canonical_hash,
                        payload=_payload(proposal),
                        authority="NONE",
                    )
                )
        except IntegrityError as error:
            raise OptionsIntelligencePersistenceError("PROPOSAL_PERSISTENCE_CONFLICT") from error
        return proposal

    def save_capability(
        self, capability: OptionProviderCapabilities, *, recorded_at: datetime
    ) -> OptionProviderCapabilities:
        with self._engine.begin() as connection:
            exists = connection.execute(
                sa.select(options_intelligence_capability.c.canonical_hash).where(
                    options_intelligence_capability.c.canonical_hash == capability.canonical_hash
                )
            ).scalar_one_or_none()
            if exists is None:
                connection.execute(
                    options_intelligence_capability.insert().values(
                        canonical_hash=capability.canonical_hash,
                        provider=capability.provider,
                        recorded_at=recorded_at,
                        payload=_payload(capability),
                        authority="NONE",
                    )
                )
        return capability

    def capabilities(self, provider: str | None = None) -> tuple[OptionProviderCapabilities, ...]:
        statement = sa.select(options_intelligence_capability).order_by(
            options_intelligence_capability.c.recorded_at
        )
        if provider is not None:
            statement = statement.where(options_intelligence_capability.c.provider == provider)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_capability(row) for row in rows)

    def save_contracts(
        self, contracts: tuple[OptionContract, ...], *, recorded_at: datetime
    ) -> tuple[OptionContract, ...]:
        """Idempotently persist a bounded capture's contracts at genuine scale.

        A single official Nasdaq options-directory capture can retain over a
        million contracts -- one ``IN (...)`` clause or one ``VALUES`` list
        over the whole set would exceed the driver's bound-parameter limit,
        so both the existence check and the insert are chunked.  Contracts
        are also deduplicated by ``canonical_contract_id`` within this one
        call (not just against what is already durably retained): the
        official source can legitimately repeat an identical series row,
        and a duplicate insert attempt within the same transaction must
        never violate the primary key.
        """
        if not contracts:
            return contracts
        with self._engine.begin() as connection:
            all_ids = tuple({item.canonical_contract_id for item in contracts})
            existing: set[str] = set()
            for start in range(0, len(all_ids), _CONTRACT_BATCH_SIZE):
                batch_ids = all_ids[start : start + _CONTRACT_BATCH_SIZE]
                existing.update(
                    connection.execute(
                        sa.select(options_intelligence_contract.c.canonical_contract_id).where(
                            options_intelligence_contract.c.canonical_contract_id.in_(batch_ids)
                        )
                    ).scalars()
                )
            pending: dict[str, dict[str, object]] = {}
            for contract in contracts:
                if contract.canonical_contract_id in existing:
                    continue
                pending.setdefault(
                    contract.canonical_contract_id,
                    {
                        "canonical_contract_id": contract.canonical_contract_id,
                        "underlying_canonical_security_id": (
                            contract.underlying_canonical_security_id
                        ),
                        "option_type": contract.option_type.value,
                        "expiration": contract.expiration,
                        "source_provider": contract.source_provider,
                        "recorded_at": recorded_at,
                        "canonical_hash": contract.canonical_hash,
                        "payload": _payload(contract),
                        "authority": "NONE",
                    },
                )
            rows = list(pending.values())
            for start in range(0, len(rows), _CONTRACT_BATCH_SIZE):
                connection.execute(
                    options_intelligence_contract.insert(),
                    rows[start : start + _CONTRACT_BATCH_SIZE],
                )
        return contracts

    def contracts_for_underlying(self, canonical_security_id: str) -> tuple[OptionContract, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_intelligence_contract)
                .where(
                    options_intelligence_contract.c.underlying_canonical_security_id
                    == canonical_security_id
                )
                .order_by(
                    options_intelligence_contract.c.expiration,
                    options_intelligence_contract.c.canonical_contract_id,
                )
            ).mappings()
            return tuple(_contract(row) for row in rows)

    def contracts(self) -> tuple[OptionContract, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_intelligence_contract).order_by(
                    options_intelligence_contract.c.underlying_canonical_security_id,
                    options_intelligence_contract.c.expiration,
                    options_intelligence_contract.c.canonical_contract_id,
                )
            ).mappings()
            return tuple(_contract(row) for row in rows)

    def contract_count(self) -> int:
        """A genuine official capture can retain well over a million
        contracts; callers that only need the count (API/Console summaries)
        must never pay for deserializing every retained row to get it.
        """
        with self._engine.connect() as connection:
            return int(
                connection.scalar(
                    sa.select(sa.func.count()).select_from(options_intelligence_contract)
                )
                or 0
            )

    def save_optionable_underlyings(
        self, decisions: tuple[OptionableUnderlyingDecision, ...], *, recorded_at: datetime
    ) -> tuple[OptionableUnderlyingDecision, ...]:
        if not decisions:
            return decisions
        with self._engine.begin() as connection:
            existing = set(
                connection.execute(
                    sa.select(options_intelligence_optionable_underlying.c.canonical_hash).where(
                        options_intelligence_optionable_underlying.c.canonical_hash.in_(
                            {item.canonical_hash for item in decisions}
                        )
                    )
                ).scalars()
            )
            for decision in decisions:
                if decision.canonical_hash in existing:
                    continue
                connection.execute(
                    options_intelligence_optionable_underlying.insert().values(
                        canonical_hash=decision.canonical_hash,
                        canonical_security_id=decision.canonical_security_id,
                        optionable=decision.optionable,
                        recorded_at=recorded_at,
                        payload=_payload(decision),
                        authority="NONE",
                    )
                )
        return decisions

    def optionable_underlying(
        self, canonical_security_id: str
    ) -> OptionableUnderlyingDecision | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_intelligence_optionable_underlying)
                    .where(
                        options_intelligence_optionable_underlying.c.canonical_security_id
                        == canonical_security_id
                    )
                    .order_by(options_intelligence_optionable_underlying.c.recorded_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _optionable(row) if row is not None else None

    def optionable_underlyings(self) -> tuple[OptionableUnderlyingDecision, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_intelligence_optionable_underlying).order_by(
                    options_intelligence_optionable_underlying.c.recorded_at.desc()
                )
            ).mappings()
            seen: set[str] = set()
            results = []
            for row in rows:
                decision = _optionable(row)
                if decision.canonical_security_id in seen:
                    continue
                seen.add(decision.canonical_security_id)
                results.append(decision)
            return tuple(sorted(results, key=lambda item: item.canonical_security_id))
