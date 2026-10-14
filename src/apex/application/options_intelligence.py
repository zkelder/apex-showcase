"""Idempotent orchestration for observation-only options intelligence."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from apex.domain.options_intelligence import (
    DefinedRiskOptionCandidate,
    DefinedRiskOptionsShadowProposal,
    OptionableUnderlyingDecision,
    OptionChainSnapshot,
    OptionContract,
    OptionProviderCapabilities,
    create_options_shadow_proposal,
    derive_optionable_underlyings,
)


class OptionsIntelligenceApplicationError(RuntimeError):
    pass


class OptionsIntelligenceRepository(Protocol):
    def proposal_by_key(self, key: str) -> DefinedRiskOptionsShadowProposal | None: ...

    def save_proposal(
        self, key: str, proposal: DefinedRiskOptionsShadowProposal
    ) -> DefinedRiskOptionsShadowProposal: ...

    def save_capability(
        self, capability: OptionProviderCapabilities, *, recorded_at: datetime
    ) -> OptionProviderCapabilities: ...

    def capabilities(
        self, provider: str | None = None
    ) -> tuple[OptionProviderCapabilities, ...]: ...

    def save_contracts(
        self, contracts: tuple[OptionContract, ...], *, recorded_at: datetime
    ) -> tuple[OptionContract, ...]: ...

    def contracts_for_underlying(
        self, canonical_security_id: str
    ) -> tuple[OptionContract, ...]: ...

    def contracts(self) -> tuple[OptionContract, ...]: ...

    def contract_count(self) -> int: ...

    def save_optionable_underlyings(
        self, decisions: tuple[OptionableUnderlyingDecision, ...], *, recorded_at: datetime
    ) -> tuple[OptionableUnderlyingDecision, ...]: ...

    def optionable_underlying(
        self, canonical_security_id: str
    ) -> OptionableUnderlyingDecision | None: ...

    def optionable_underlyings(self) -> tuple[OptionableUnderlyingDecision, ...]: ...


class CreateDefinedRiskOptionsShadowProposal:
    def __init__(self, repository: OptionsIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        discovery_run_id: str,
        chains: tuple[OptionChainSnapshot, ...],
        candidates: tuple[DefinedRiskOptionCandidate, ...],
        calculation_policy_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> DefinedRiskOptionsShadowProposal:
        candidate = create_options_shadow_proposal(
            discovery_run_id=discovery_run_id,
            chains=chains,
            candidates=candidates,
            calculation_policy_ids=calculation_policy_ids,
        )
        prior = self._repository.proposal_by_key(idempotency_key)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise OptionsIntelligenceApplicationError("OPTIONS_SHADOW_IDEMPOTENCY_CONFLICT")
            return prior
        return self._repository.save_proposal(idempotency_key, candidate)


class RecordOptionProviderCapability:
    def __init__(self, repository: OptionsIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self, *, capability: OptionProviderCapabilities, recorded_at: datetime
    ) -> OptionProviderCapabilities:
        return self._repository.save_capability(capability, recorded_at=recorded_at)


class RecordOptionContracts:
    def __init__(self, repository: OptionsIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self, *, contracts: tuple[OptionContract, ...], recorded_at: datetime
    ) -> tuple[OptionContract, ...]:
        return self._repository.save_contracts(contracts, recorded_at=recorded_at)


class RecordOptionableUnderlyings:
    """Derive and durably record optionable-underlying decisions.

    Bounded by an already-resolved security universe; grouping is a pure
    domain computation over retained contract-reference evidence only.
    """

    def __init__(self, repository: OptionsIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        universe_security_ids: tuple[str, ...],
        contracts: tuple[OptionContract, ...],
        source_provider: str,
        as_of: str,
        recorded_at: datetime,
    ) -> tuple[OptionableUnderlyingDecision, ...]:
        decisions = derive_optionable_underlyings(
            universe_security_ids=universe_security_ids,
            contracts=contracts,
            source_provider=source_provider,
            as_of=as_of,
        )
        return self._repository.save_optionable_underlyings(decisions, recorded_at=recorded_at)


class InMemoryOptionsIntelligenceRepository:
    def __init__(self) -> None:
        self._proposals: dict[str, DefinedRiskOptionsShadowProposal] = {}
        self._capabilities: dict[str, OptionProviderCapabilities] = {}
        self._contracts: dict[str, OptionContract] = {}
        self._optionable: dict[str, OptionableUnderlyingDecision] = {}

    def proposal_by_key(self, key: str) -> DefinedRiskOptionsShadowProposal | None:
        return self._proposals.get(key)

    def save_proposal(
        self, key: str, proposal: DefinedRiskOptionsShadowProposal
    ) -> DefinedRiskOptionsShadowProposal:
        self._proposals[key] = proposal
        return proposal

    def save_capability(
        self, capability: OptionProviderCapabilities, *, recorded_at: datetime
    ) -> OptionProviderCapabilities:
        self._capabilities[capability.canonical_hash] = capability
        return capability

    def capabilities(self, provider: str | None = None) -> tuple[OptionProviderCapabilities, ...]:
        values: Iterable[OptionProviderCapabilities] = self._capabilities.values()
        if provider is not None:
            values = (item for item in values if item.provider == provider)
        return tuple(sorted(values, key=lambda item: item.canonical_hash))

    def save_contracts(
        self, contracts: tuple[OptionContract, ...], *, recorded_at: datetime
    ) -> tuple[OptionContract, ...]:
        for contract in contracts:
            self._contracts[contract.canonical_contract_id] = contract
        return contracts

    def contracts_for_underlying(self, canonical_security_id: str) -> tuple[OptionContract, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._contracts.values()
                    if item.underlying_canonical_security_id == canonical_security_id
                ),
                key=lambda item: item.canonical_contract_id,
            )
        )

    def contracts(self) -> tuple[OptionContract, ...]:
        return tuple(sorted(self._contracts.values(), key=lambda item: item.canonical_contract_id))

    def contract_count(self) -> int:
        return len(self._contracts)

    def save_optionable_underlyings(
        self, decisions: tuple[OptionableUnderlyingDecision, ...], *, recorded_at: datetime
    ) -> tuple[OptionableUnderlyingDecision, ...]:
        for decision in decisions:
            self._optionable[decision.canonical_security_id] = decision
        return decisions

    def optionable_underlying(
        self, canonical_security_id: str
    ) -> OptionableUnderlyingDecision | None:
        return self._optionable.get(canonical_security_id)

    def optionable_underlyings(self) -> tuple[OptionableUnderlyingDecision, ...]:
        return tuple(sorted(self._optionable.values(), key=lambda item: item.canonical_security_id))
