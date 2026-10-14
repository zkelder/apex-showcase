"""Application services for immutable current-listed security universes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from apex.domain.security_reference import (
    CurrentEquityUniversePolicy,
    CurrentEquityUniverseSnapshot,
    ReconciledSecurity,
    create_current_equity_universe_snapshot,
)


class SecurityReferenceApplicationError(RuntimeError):
    pass


class SecurityReferenceRepository(Protocol):
    def universe_by_key(self, idempotency_key: str) -> CurrentEquityUniverseSnapshot | None: ...

    def save_universe(
        self, idempotency_key: str, snapshot: CurrentEquityUniverseSnapshot
    ) -> CurrentEquityUniverseSnapshot: ...

    def universe(self, universe_id: str) -> CurrentEquityUniverseSnapshot | None: ...

    def universes(self) -> tuple[CurrentEquityUniverseSnapshot, ...]: ...

    def save_identities(
        self, securities: tuple[ReconciledSecurity, ...], *, recorded_at: datetime
    ) -> tuple[ReconciledSecurity, ...]: ...

    def identity(self, canonical_hash: str) -> ReconciledSecurity | None: ...

    def identities_by_symbol(self, current_symbol: str) -> tuple[ReconciledSecurity, ...]: ...

    def identities_requiring_review(self) -> tuple[ReconciledSecurity, ...]: ...

    def identities(self) -> tuple[ReconciledSecurity, ...]: ...


class CreateCurrentEquityUniverse:
    def __init__(self, repository: SecurityReferenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        securities: tuple[ReconciledSecurity, ...],
        policy: CurrentEquityUniversePolicy,
        source_receipt_ids: tuple[str, ...],
        as_of_date: date,
        created_at: datetime,
        idempotency_key: str,
    ) -> CurrentEquityUniverseSnapshot:
        candidate = create_current_equity_universe_snapshot(
            securities=securities,
            policy=policy,
            source_receipt_ids=source_receipt_ids,
            as_of_date=as_of_date,
            created_at=created_at,
        )
        prior = self._repository.universe_by_key(idempotency_key)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise SecurityReferenceApplicationError("BROAD_UNIVERSE_IDEMPOTENCY_CONFLICT")
            return prior
        # Resolved identity and ambiguity evidence is durable on its own terms:
        # every universe build re-asserts (idempotently) the exact reconciled
        # identities it was computed from, independent of universe policy.
        self._repository.save_identities(securities, recorded_at=created_at)
        return self._repository.save_universe(idempotency_key, candidate)


class InMemorySecurityReferenceRepository:
    def __init__(self) -> None:
        self._universes: dict[str, CurrentEquityUniverseSnapshot] = {}
        self._identities: dict[str, ReconciledSecurity] = {}

    def universe_by_key(self, idempotency_key: str) -> CurrentEquityUniverseSnapshot | None:
        return self._universes.get(idempotency_key)

    def save_universe(
        self, idempotency_key: str, snapshot: CurrentEquityUniverseSnapshot
    ) -> CurrentEquityUniverseSnapshot:
        self._universes[idempotency_key] = snapshot
        return snapshot

    def universe(self, universe_id: str) -> CurrentEquityUniverseSnapshot | None:
        return next(
            (item for item in self._universes.values() if item.universe_id == universe_id), None
        )

    def universes(self) -> tuple[CurrentEquityUniverseSnapshot, ...]:
        return tuple(sorted(self._universes.values(), key=lambda item: item.universe_id))

    def save_identities(
        self, securities: tuple[ReconciledSecurity, ...], *, recorded_at: datetime
    ) -> tuple[ReconciledSecurity, ...]:
        for security in securities:
            self._identities[security.canonical_hash] = security
        return securities

    def identity(self, canonical_hash: str) -> ReconciledSecurity | None:
        return self._identities.get(canonical_hash)

    def identities_by_symbol(self, current_symbol: str) -> tuple[ReconciledSecurity, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._identities.values()
                    if item.current_symbol == current_symbol
                ),
                key=lambda item: item.canonical_hash,
            )
        )

    def identities_requiring_review(self) -> tuple[ReconciledSecurity, ...]:
        return tuple(
            sorted(
                (item for item in self._identities.values() if item.requires_review),
                key=lambda item: item.canonical_hash,
            )
        )

    def identities(self) -> tuple[ReconciledSecurity, ...]:
        return tuple(sorted(self._identities.values(), key=lambda item: item.canonical_hash))
