"""Idempotent orchestration for stable security identity and options relink.

Composes the pure ``domain.security_identity`` functions with durable
repositories. Nothing here grants Order, Position, reservation, journal,
PAPER, or LIVE authority; every persisted row carries ``authority = NONE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from apex.domain.options_intelligence import OptionContract
from apex.domain.security_identity import (
    IdentityResolution,
    OptionUnderlyingStableLinkage,
    StableSecurityIdentity,
    apply_stable_identity,
    collect_raw_underlying_evidence,
    detect_stable_identity_supersession,
    detect_symbol_punctuation_collisions,
    relink_option_underlyings_to_stable_identity,
    resolve_legacy_security,
)
from apex.domain.security_reference import (
    CurrentEquityUniversePolicy,
    CurrentEquityUniverseSnapshot,
    ReconciledSecurity,
    create_current_equity_universe_snapshot,
)


class SecurityIdentityApplicationError(RuntimeError):
    pass


class SecurityIdentityRepository(Protocol):
    def save_stable_identities(
        self, identities: tuple[StableSecurityIdentity, ...], *, recorded_at: datetime
    ) -> tuple[StableSecurityIdentity, ...]: ...

    def stable_identity(self, canonical_hash: str) -> StableSecurityIdentity | None: ...

    def stable_identities_by_stable_id(
        self, stable_security_id: str
    ) -> tuple[StableSecurityIdentity, ...]: ...

    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]: ...

    def stable_identity_count(self) -> int: ...

    def save_resolutions(
        self, resolutions: tuple[IdentityResolution, ...], *, recorded_at: datetime
    ) -> tuple[IdentityResolution, ...]: ...

    def resolutions_for_old_id(self, old_identity_id: str) -> tuple[IdentityResolution, ...]: ...

    def resolutions_to_stable_id(
        self, stable_security_id: str
    ) -> tuple[IdentityResolution, ...]: ...

    def ambiguous_resolutions(self, *, limit: int = 200) -> tuple[IdentityResolution, ...]: ...

    def resolution_state_counts(self) -> tuple[tuple[str, int], ...]: ...

    def resolution_count(self) -> int: ...

    def save_option_linkages(
        self, linkages: tuple[OptionUnderlyingStableLinkage, ...], *, recorded_at: datetime
    ) -> tuple[OptionUnderlyingStableLinkage, ...]: ...

    def option_linkages_for_run(
        self, linkage_run_id: str
    ) -> tuple[OptionUnderlyingStableLinkage, ...]: ...

    def latest_option_linkage_run_id(self) -> str | None: ...

    def option_linkage_run_ids(self) -> tuple[str, ...]: ...

    def option_linkage(
        self, raw_underlying_symbol: str
    ) -> OptionUnderlyingStableLinkage | None: ...


class SecurityReferenceRepositoryLike(Protocol):
    """The narrow slice of ``SecurityReferenceRepository`` this module uses."""

    def universe_by_key(self, idempotency_key: str) -> CurrentEquityUniverseSnapshot | None: ...

    def save_universe(
        self, idempotency_key: str, snapshot: CurrentEquityUniverseSnapshot
    ) -> CurrentEquityUniverseSnapshot: ...

    def save_identities(
        self, securities: tuple[ReconciledSecurity, ...], *, recorded_at: datetime
    ) -> tuple[ReconciledSecurity, ...]: ...

    def identities(self) -> tuple[ReconciledSecurity, ...]: ...


@dataclass(frozen=True, slots=True)
class StableIdentityProjectionResult:
    universe: CurrentEquityUniverseSnapshot
    identities: tuple[StableSecurityIdentity, ...]
    resolutions: tuple[IdentityResolution, ...]
    raw_source_record_count: int
    prior_legacy_identity_count: int
    stable_identity_count: int
    superseded_count: int
    ambiguous_count: int
    review_required_count: int


class BuildStableIdentityProjection:
    """Derive stable identities for the latest reconciled security set,
    detect resolution lineage against previously known stable identities,
    and build a fresh, stable-identity-bearing broad-equity universe using
    the existing, entirely unmodified ``create_current_equity_universe_snapshot``.

    Calls ``security_repository.save_identities`` with the *original*,
    un-swapped ``securities`` -- never the stable-id-bearing projection --
    exactly as ``CreateCurrentEquityUniverse`` itself does. This is required,
    not merely reused for consistency: ``security_stable_identity.source_identity_hash``
    carries a foreign key onto ``security_reference_identity.canonical_hash``
    (deliberately, so a stable identity can never reference reconciled
    identity evidence Apex never actually retained), so that row must exist
    before a stable identity derived from it can be saved. Because
    ``ReconciledSecurity.canonical_hash`` never includes
    ``canonical_security_id`` in its own payload (see
    ``apply_stable_identity``), this call is content-identical to, and safely
    idempotent against, whatever a prior legacy ``broad-equity-universe-create``
    already persisted for the identical reconciled evidence -- it can never
    plant a stable id under the legacy table's own (legacy-only)
    ``canonical_security_id`` column.
    """

    def __init__(
        self,
        *,
        security_repository: SecurityReferenceRepositoryLike,
        identity_repository: SecurityIdentityRepository,
    ) -> None:
        self._security_repository = security_repository
        self._identity_repository = identity_repository

    def execute(
        self,
        *,
        securities: tuple[ReconciledSecurity, ...],
        policy: CurrentEquityUniversePolicy,
        source_receipt_ids: tuple[str, ...],
        raw_source_record_count: int,
        as_of_date: date,
        created_at: datetime,
        idempotency_key: str,
    ) -> StableIdentityProjectionResult:
        self._security_repository.save_identities(securities, recorded_at=created_at)
        stable_identities = tuple(StableSecurityIdentity.derive(item) for item in securities)
        prior = self._identity_repository.latest_stable_identities()
        supersession = detect_stable_identity_supersession(current=stable_identities, prior=prior)
        punctuation = detect_symbol_punctuation_collisions(stable_identities)
        resolutions = supersession + punctuation

        projected_securities = tuple(
            apply_stable_identity(security, stable)
            for security, stable in zip(securities, stable_identities, strict=True)
        )
        candidate = create_current_equity_universe_snapshot(
            securities=projected_securities,
            policy=policy,
            source_receipt_ids=source_receipt_ids,
            as_of_date=as_of_date,
            created_at=created_at,
        )
        prior_snapshot = self._security_repository.universe_by_key(idempotency_key)
        if prior_snapshot is not None:
            if prior_snapshot.canonical_hash != candidate.canonical_hash:
                raise SecurityIdentityApplicationError("STABLE_UNIVERSE_IDEMPOTENCY_CONFLICT")
            universe = prior_snapshot
        else:
            universe = self._security_repository.save_universe(idempotency_key, candidate)

        self._identity_repository.save_stable_identities(stable_identities, recorded_at=created_at)
        self._identity_repository.save_resolutions(resolutions, recorded_at=created_at)

        superseded_count = sum(
            1 for item in resolutions if item.resolution_state.value == "SUPERSEDED"
        )
        ambiguous_count = sum(
            1 for item in resolutions if item.resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW"
        )
        review_required_count = sum(1 for item in stable_identities if item.review_required)
        return StableIdentityProjectionResult(
            universe=universe,
            identities=stable_identities,
            resolutions=resolutions,
            raw_source_record_count=raw_source_record_count,
            prior_legacy_identity_count=len(securities),
            stable_identity_count=len({item.stable_security_id for item in stable_identities}),
            superseded_count=superseded_count,
            ambiguous_count=ambiguous_count,
            review_required_count=review_required_count,
        )


@dataclass(frozen=True, slots=True)
class LegacyResolutionResult:
    legacy_identity_count: int
    current_count: int
    alias_count: int
    ambiguous_count: int
    resolutions: tuple[IdentityResolution, ...]


class ResolveLegacyIdentities:
    """Sweep every retained legacy ``ReconciledSecurity`` row -- from every
    universe build ever recorded, not only the latest -- and durably resolve
    each to its stable identity. This is the concrete backward-compatibility
    guarantee: any ``canonical_security_id`` Apex has ever produced, from any
    retained universe build, remains resolvable to its current stable
    identity (or, if reconciliation already flagged it ambiguous, remains
    honestly ``AMBIGUOUS_REQUIRES_REVIEW`` rather than being force-resolved).
    """

    def __init__(
        self,
        *,
        security_repository: SecurityReferenceRepositoryLike,
        identity_repository: SecurityIdentityRepository,
    ) -> None:
        self._security_repository = security_repository
        self._identity_repository = identity_repository

    def execute(
        self,
        *,
        latest_securities: tuple[ReconciledSecurity, ...],
        recorded_at: datetime,
    ) -> LegacyResolutionResult:
        all_legacy = self._security_repository.identities()
        # Identified by canonical_hash (the pre-stable-swap reconciliation
        # envelope hash), never by canonical_security_id: latest_securities
        # is the exact pre-swap reconciled set BuildStableIdentityProjection
        # was given, so comparing that -- not the stable-id-bearing universe
        # it produced -- is what tells a legacy row from an older build
        # (ALIAS) apart from the row the latest build actually used (CURRENT).
        current_hashes = {item.canonical_hash for item in latest_securities}
        stable_identities: list[StableSecurityIdentity] = []
        resolutions: list[IdentityResolution] = []
        for security in all_legacy:
            is_current = security.canonical_hash in current_hashes
            stable, resolution = resolve_legacy_security(security, is_current=is_current)
            stable_identities.append(stable)
            resolutions.append(resolution)
        self._identity_repository.save_stable_identities(
            tuple(stable_identities), recorded_at=recorded_at
        )
        self._identity_repository.save_resolutions(tuple(resolutions), recorded_at=recorded_at)
        current_count = sum(1 for item in resolutions if item.resolution_state.value == "CURRENT")
        alias_count = sum(1 for item in resolutions if item.resolution_state.value == "ALIAS")
        ambiguous_count = sum(
            1 for item in resolutions if item.resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW"
        )
        return LegacyResolutionResult(
            legacy_identity_count=len(all_legacy),
            current_count=current_count,
            alias_count=alias_count,
            ambiguous_count=ambiguous_count,
            resolutions=tuple(resolutions),
        )


class RelinkOptionContracts:
    """Relink already-retained option contracts (plus the original
    capture's already-retained rejection ledger) against the current
    stable-identity symbol index. Issues no new provider request and never
    rewrites the immutable retained ``OptionContract``/rejection rows."""

    def __init__(self, *, identity_repository: SecurityIdentityRepository) -> None:
        self._identity_repository = identity_repository

    def execute(
        self,
        *,
        contracts: tuple[OptionContract, ...],
        previously_rejected: tuple[tuple[str, int], ...],
        stable_symbol_index: dict[str, str],
        linkage_run_id: str,
        recorded_at: datetime,
    ) -> tuple[OptionUnderlyingStableLinkage, ...]:
        raw_evidence = collect_raw_underlying_evidence(contracts)
        linkages = relink_option_underlyings_to_stable_identity(
            raw_evidence=raw_evidence,
            previously_rejected=previously_rejected,
            stable_symbol_index=stable_symbol_index,
            linkage_run_id=linkage_run_id,
        )
        return self._identity_repository.save_option_linkages(linkages, recorded_at=recorded_at)


class InMemorySecurityIdentityRepository:
    def __init__(self) -> None:
        self._identities: dict[str, StableSecurityIdentity] = {}
        self._resolutions: dict[str, IdentityResolution] = {}
        self._linkages: dict[str, OptionUnderlyingStableLinkage] = {}
        self._identity_order: list[str] = []
        self._linkage_order: list[str] = []

    def save_stable_identities(
        self, identities: tuple[StableSecurityIdentity, ...], *, recorded_at: datetime
    ) -> tuple[StableSecurityIdentity, ...]:
        del recorded_at
        for item in identities:
            if item.canonical_hash not in self._identities:
                self._identity_order.append(item.canonical_hash)
            self._identities[item.canonical_hash] = item
        return identities

    def stable_identity(self, canonical_hash: str) -> StableSecurityIdentity | None:
        return self._identities.get(canonical_hash)

    def stable_identities_by_stable_id(
        self, stable_security_id: str
    ) -> tuple[StableSecurityIdentity, ...]:
        return tuple(
            self._identities[h]
            for h in self._identity_order
            if self._identities[h].stable_security_id == stable_security_id
        )

    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]:
        latest: dict[str, StableSecurityIdentity] = {}
        for h in self._identity_order:
            item = self._identities[h]
            latest[item.stable_security_id] = item
        return tuple(sorted(latest.values(), key=lambda item: item.stable_security_id))

    def stable_identity_count(self) -> int:
        return len({item.stable_security_id for item in self._identities.values()})

    def save_resolutions(
        self, resolutions: tuple[IdentityResolution, ...], *, recorded_at: datetime
    ) -> tuple[IdentityResolution, ...]:
        del recorded_at
        for item in resolutions:
            self._resolutions[item.canonical_hash] = item
        return resolutions

    def resolutions_for_old_id(self, old_identity_id: str) -> tuple[IdentityResolution, ...]:
        return tuple(
            sorted(
                (r for r in self._resolutions.values() if r.old_identity_id == old_identity_id),
                key=lambda item: item.canonical_hash,
            )
        )

    def resolutions_to_stable_id(self, stable_security_id: str) -> tuple[IdentityResolution, ...]:
        return tuple(
            sorted(
                (
                    r
                    for r in self._resolutions.values()
                    if r.resolved_stable_security_id == stable_security_id
                ),
                key=lambda item: item.canonical_hash,
            )
        )

    def ambiguous_resolutions(self, *, limit: int = 200) -> tuple[IdentityResolution, ...]:
        values = tuple(
            sorted(
                (
                    r
                    for r in self._resolutions.values()
                    if r.resolution_state.value
                    in ("AMBIGUOUS_REQUIRES_REVIEW", "DISTINCT_SECURITY")
                ),
                key=lambda item: item.canonical_hash,
            )
        )
        return values[:limit]

    def resolution_state_counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for item in self._resolutions.values():
            counts[item.resolution_state.value] = counts.get(item.resolution_state.value, 0) + 1
        return tuple(sorted(counts.items()))

    def resolution_count(self) -> int:
        return len(self._resolutions)

    def save_option_linkages(
        self, linkages: tuple[OptionUnderlyingStableLinkage, ...], *, recorded_at: datetime
    ) -> tuple[OptionUnderlyingStableLinkage, ...]:
        del recorded_at
        for item in linkages:
            if item.canonical_hash not in self._linkages:
                self._linkage_order.append(item.canonical_hash)
            self._linkages[item.canonical_hash] = item
        return linkages

    def option_linkages_for_run(
        self, linkage_run_id: str
    ) -> tuple[OptionUnderlyingStableLinkage, ...]:
        return tuple(
            sorted(
                (item for item in self._linkages.values() if item.linkage_run_id == linkage_run_id),
                key=lambda item: item.raw_underlying_symbol,
            )
        )

    def latest_option_linkage_run_id(self) -> str | None:
        return (
            self._linkages[self._linkage_order[-1]].linkage_run_id if self._linkage_order else None
        )

    def option_linkage_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.linkage_run_id for item in self._linkages.values()}))

    def option_linkage(self, raw_underlying_symbol: str) -> OptionUnderlyingStableLinkage | None:
        for h in reversed(self._linkage_order):
            item = self._linkages[h]
            if item.raw_underlying_symbol == raw_underlying_symbol:
                return item
        return None
