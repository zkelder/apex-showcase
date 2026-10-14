"""Idempotent orchestration for defined-risk options structure economics.

Every service here is additive research-evidence orchestration only: no
Order, Strategy, Shared Opportunity, PAPER order, or LIVE authority is ever
created, and every persisted fact keeps ``authority = "NONE"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from apex.domain.options_quote_intelligence import OptionQuoteObservation, OptionsChainSnapshot
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
    OptionStructureCandidate,
    StructurePricingPolicy,
    StructureResearchFeatures,
    StructureResearchSnapshot,
    generate_structure_candidates,
)


class OptionsStructuresApplicationError(RuntimeError):
    pass


class OptionsStructuresRepository(Protocol):
    def save_fee_policy(self, policy: OptionsFeePolicy) -> OptionsFeePolicy: ...

    def fee_policy_by_id(self, policy_id: str) -> OptionsFeePolicy | None: ...

    def latest_fee_policy(self) -> OptionsFeePolicy | None: ...

    def save_slippage_policy(self, policy: OptionsSlippagePolicy) -> OptionsSlippagePolicy: ...

    def slippage_policy_by_id(self, policy_id: str) -> OptionsSlippagePolicy | None: ...

    def latest_slippage_policy(self) -> OptionsSlippagePolicy | None: ...

    def save_pricing_policy(self, policy: StructurePricingPolicy) -> StructurePricingPolicy: ...

    def pricing_policy_by_id(self, policy_id: str) -> StructurePricingPolicy | None: ...

    def latest_pricing_policy(self) -> StructurePricingPolicy | None: ...

    def save_structure_policy(
        self, policy: OptionsStructureResearchPolicy
    ) -> OptionsStructureResearchPolicy: ...

    def structure_policy_by_id(self, policy_id: str) -> OptionsStructureResearchPolicy | None: ...

    def latest_structure_policy(self) -> OptionsStructureResearchPolicy | None: ...

    def save_structures(
        self, structures: tuple[OptionStructureCandidate, ...]
    ) -> tuple[OptionStructureCandidate, ...]: ...

    def structure_by_id(self, structure_id: str) -> OptionStructureCandidate | None: ...

    def structures_by_ids(
        self, structure_ids: tuple[str, ...]
    ) -> tuple[OptionStructureCandidate, ...]: ...

    def structures_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionStructureCandidate, ...]: ...

    def save_research_snapshot(
        self, snapshot: StructureResearchSnapshot
    ) -> StructureResearchSnapshot: ...

    def research_snapshot_by_id(
        self, research_snapshot_id: str
    ) -> StructureResearchSnapshot | None: ...

    def latest_research_snapshot_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> StructureResearchSnapshot | None: ...

    def research_snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[StructureResearchSnapshot, ...]: ...

    def save_research_features(
        self, features: tuple[StructureResearchFeatures, ...]
    ) -> tuple[StructureResearchFeatures, ...]: ...

    def research_features_by_structure(
        self, structure_id: str
    ) -> StructureResearchFeatures | None: ...


class CreateOptionsFeePolicy:
    """Content-addressed: creating the identical fee policy twice returns
    the already-retained row rather than a duplicate."""

    def __init__(self, repository: OptionsStructuresRepository) -> None:
        self._repository = repository

    def execute(self, *, policy: OptionsFeePolicy) -> OptionsFeePolicy:
        existing = self._repository.fee_policy_by_id(policy.policy_id)
        if existing is not None:
            return existing
        return self._repository.save_fee_policy(policy)


class CreateOptionsSlippagePolicy:
    def __init__(self, repository: OptionsStructuresRepository) -> None:
        self._repository = repository

    def execute(self, *, policy: OptionsSlippagePolicy) -> OptionsSlippagePolicy:
        existing = self._repository.slippage_policy_by_id(policy.policy_id)
        if existing is not None:
            return existing
        return self._repository.save_slippage_policy(policy)


class CreateStructurePricingPolicy:
    def __init__(self, repository: OptionsStructuresRepository) -> None:
        self._repository = repository

    def execute(self, *, policy: StructurePricingPolicy) -> StructurePricingPolicy:
        existing = self._repository.pricing_policy_by_id(policy.policy_id)
        if existing is not None:
            return existing
        return self._repository.save_pricing_policy(policy)


class CreateOptionsStructureResearchPolicy:
    def __init__(self, repository: OptionsStructuresRepository) -> None:
        self._repository = repository

    def execute(self, *, policy: OptionsStructureResearchPolicy) -> OptionsStructureResearchPolicy:
        existing = self._repository.structure_policy_by_id(policy.policy_id)
        if existing is not None:
            return existing
        return self._repository.save_structure_policy(policy)


@dataclass(frozen=True, slots=True)
class BuildStructuresForUnderlyingCommand:
    structure_policy: OptionsStructureResearchPolicy
    fee_policy: OptionsFeePolicy
    slippage_policy: OptionsSlippagePolicy
    snapshot: OptionsChainSnapshot
    observations: tuple[OptionQuoteObservation, ...]
    as_of: date
    evaluated_at: datetime
    quantity: int = 1


@dataclass(frozen=True, slots=True)
class UnderlyingStructureBuildOutcome:
    research_snapshot: StructureResearchSnapshot
    structures: tuple[OptionStructureCandidate, ...]


class BuildStructuresForUnderlying:
    """Runs bounded, deterministic candidate generation for one already-
    retained chain snapshot and idempotently persists the result: identical
    evidence/policy replay resolves to the identical retained research
    snapshot and structure rows, never a duplicate."""

    def __init__(self, repository: OptionsStructuresRepository) -> None:
        self._repository = repository

    def execute(
        self, command: BuildStructuresForUnderlyingCommand
    ) -> UnderlyingStructureBuildOutcome:
        result = generate_structure_candidates(
            snapshot=command.snapshot,
            observations=command.observations,
            structure_policy=command.structure_policy,
            fee_policy=command.fee_policy,
            slippage_policy=command.slippage_policy,
            as_of=command.as_of,
            evaluated_at=command.evaluated_at,
            quantity=command.quantity,
        )
        research_snapshot = StructureResearchSnapshot.create(
            structure_policy=command.structure_policy,
            snapshot=command.snapshot,
            result=result,
            evaluated_at=command.evaluated_at,
        )
        existing = self._repository.research_snapshot_by_id(research_snapshot.research_snapshot_id)
        if existing is not None:
            structures = self._repository.structures_by_ids(existing.retained_structure_ids)
            return UnderlyingStructureBuildOutcome(existing, structures)
        if result.evaluated:
            self._repository.save_structures(result.evaluated)
            features = tuple(
                StructureResearchFeatures.from_candidate(candidate)
                for candidate in result.evaluated
            )
            self._repository.save_research_features(features)
        self._repository.save_research_snapshot(research_snapshot)
        return UnderlyingStructureBuildOutcome(research_snapshot, result.evaluated)


@dataclass(frozen=True, slots=True)
class BuildStructuresAcrossCohortCommand:
    structure_policy: OptionsStructureResearchPolicy
    fee_policy: OptionsFeePolicy
    slippage_policy: OptionsSlippagePolicy
    members: tuple[tuple[OptionsChainSnapshot, tuple[OptionQuoteObservation, ...]], ...]
    as_of: date
    evaluated_at: datetime
    quantity: int = 1


@dataclass(frozen=True, slots=True)
class CohortStructureBuildOutcome:
    research_snapshots: tuple[StructureResearchSnapshot, ...]
    structures: tuple[OptionStructureCandidate, ...]
    underlyings_processed: int
    underlyings_skipped_by_total_cap: int
    total_structures_cap_reached: bool


class BuildStructuresAcrossCohort:
    """Runs ``BuildStructuresForUnderlying`` once per already-retained
    member snapshot, stopping *before* starting a further underlying once
    ``structure_policy.maximum_total_structures`` is already met by
    fully-processed underlyings (milestone section 18/30: bounded, never
    producing an unbounded number of structure rows). Each processed
    underlying's own row count is still bounded independently by
    ``maximum_structures_per_underlying``, so the retained total can
    exceed the declared cap by at most one underlying's worth -- a
    documented soft bound, not a hard mid-underlying truncation that would
    leave a partially-persisted, inconsistent research snapshot."""

    def __init__(self, repository: OptionsStructuresRepository) -> None:
        self._repository = repository
        self._per_underlying = BuildStructuresForUnderlying(repository)

    def execute(self, command: BuildStructuresAcrossCohortCommand) -> CohortStructureBuildOutcome:
        snapshots: list[StructureResearchSnapshot] = []
        structures: list[OptionStructureCandidate] = []
        skipped = 0
        cap_reached = False
        for snapshot, observations in command.members:
            if len(structures) >= command.structure_policy.maximum_total_structures:
                cap_reached = True
                skipped += 1
                continue
            outcome = self._per_underlying.execute(
                BuildStructuresForUnderlyingCommand(
                    structure_policy=command.structure_policy,
                    fee_policy=command.fee_policy,
                    slippage_policy=command.slippage_policy,
                    snapshot=snapshot,
                    observations=observations,
                    as_of=command.as_of,
                    evaluated_at=command.evaluated_at,
                    quantity=command.quantity,
                )
            )
            snapshots.append(outcome.research_snapshot)
            structures.extend(outcome.structures)
        return CohortStructureBuildOutcome(
            research_snapshots=tuple(snapshots),
            structures=tuple(structures),
            underlyings_processed=len(snapshots),
            underlyings_skipped_by_total_cap=skipped,
            total_structures_cap_reached=cap_reached,
        )


class InMemoryOptionsStructuresRepository:
    def __init__(self) -> None:
        self._fee_policies: dict[str, OptionsFeePolicy] = {}
        self._slippage_policies: dict[str, OptionsSlippagePolicy] = {}
        self._pricing_policies: dict[str, StructurePricingPolicy] = {}
        self._structure_policies: dict[str, OptionsStructureResearchPolicy] = {}
        self._structures: dict[str, OptionStructureCandidate] = {}
        self._research_snapshots: dict[str, StructureResearchSnapshot] = {}
        self._research_features: dict[str, StructureResearchFeatures] = {}

    def save_fee_policy(self, policy: OptionsFeePolicy) -> OptionsFeePolicy:
        self._fee_policies[policy.policy_id] = policy
        return policy

    def fee_policy_by_id(self, policy_id: str) -> OptionsFeePolicy | None:
        return self._fee_policies.get(policy_id)

    def latest_fee_policy(self) -> OptionsFeePolicy | None:
        if not self._fee_policies:
            return None
        return next(iter(reversed(self._fee_policies.values())))

    def save_slippage_policy(self, policy: OptionsSlippagePolicy) -> OptionsSlippagePolicy:
        self._slippage_policies[policy.policy_id] = policy
        return policy

    def slippage_policy_by_id(self, policy_id: str) -> OptionsSlippagePolicy | None:
        return self._slippage_policies.get(policy_id)

    def latest_slippage_policy(self) -> OptionsSlippagePolicy | None:
        return (
            next(iter(reversed(self._slippage_policies.values())), None)
            if self._slippage_policies
            else None
        )

    def save_pricing_policy(self, policy: StructurePricingPolicy) -> StructurePricingPolicy:
        self._pricing_policies[policy.policy_id] = policy
        return policy

    def pricing_policy_by_id(self, policy_id: str) -> StructurePricingPolicy | None:
        return self._pricing_policies.get(policy_id)

    def latest_pricing_policy(self) -> StructurePricingPolicy | None:
        return (
            next(iter(reversed(self._pricing_policies.values())), None)
            if self._pricing_policies
            else None
        )

    def save_structure_policy(
        self, policy: OptionsStructureResearchPolicy
    ) -> OptionsStructureResearchPolicy:
        self._structure_policies[policy.policy_id] = policy
        return policy

    def structure_policy_by_id(self, policy_id: str) -> OptionsStructureResearchPolicy | None:
        return self._structure_policies.get(policy_id)

    def latest_structure_policy(self) -> OptionsStructureResearchPolicy | None:
        return (
            max(self._structure_policies.values(), key=lambda item: item.created_at)
            if self._structure_policies
            else None
        )

    def save_structures(
        self, structures: tuple[OptionStructureCandidate, ...]
    ) -> tuple[OptionStructureCandidate, ...]:
        for structure in structures:
            self._structures.setdefault(structure.structure_id, structure)
        return structures

    def structure_by_id(self, structure_id: str) -> OptionStructureCandidate | None:
        return self._structures.get(structure_id)

    def structures_by_ids(
        self, structure_ids: tuple[str, ...]
    ) -> tuple[OptionStructureCandidate, ...]:
        return tuple(self._structures[item] for item in structure_ids if item in self._structures)

    def structures_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionStructureCandidate, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._structures.values()
                    if item.underlying_canonical_security_id == underlying_canonical_security_id
                ),
                key=lambda item: item.structure_id,
            )
        )

    def save_research_snapshot(
        self, snapshot: StructureResearchSnapshot
    ) -> StructureResearchSnapshot:
        self._research_snapshots.setdefault(snapshot.research_snapshot_id, snapshot)
        return snapshot

    def research_snapshot_by_id(
        self, research_snapshot_id: str
    ) -> StructureResearchSnapshot | None:
        return self._research_snapshots.get(research_snapshot_id)

    def latest_research_snapshot_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> StructureResearchSnapshot | None:
        matches = self.research_snapshots_for_underlying(underlying_canonical_security_id)
        return matches[-1] if matches else None

    def research_snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[StructureResearchSnapshot, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._research_snapshots.values()
                    if item.underlying_canonical_security_id == underlying_canonical_security_id
                ),
                key=lambda item: item.evaluated_at,
            )
        )

    def save_research_features(
        self, features: tuple[StructureResearchFeatures, ...]
    ) -> tuple[StructureResearchFeatures, ...]:
        for item in features:
            self._research_features.setdefault(item.feature_id, item)
        return features

    def research_features_by_structure(self, structure_id: str) -> StructureResearchFeatures | None:
        return next(
            (
                item
                for item in self._research_features.values()
                if item.structure_id == structure_id
            ),
            None,
        )
