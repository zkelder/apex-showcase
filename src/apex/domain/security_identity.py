"""Stable, source-preference-invariant security identity and resolution lineage.

Why this module exists
-----------------------
``security_reference.py``'s ``ReconciledSecurity.canonical_security_id`` is
``preferred.canonical_security_id`` -- a content hash of the single source
record ``reconcile_security_references`` currently prefers for a symbol
(``(symbol, issuer_name, listing_exchange, security_type, cik)``, all taken
from *that one record*). Every one of those fields except ``symbol`` can
differ between an SEC-sourced and a Nasdaq-sourced record for the identical
security, and preference itself is ordering, not identity: Nasdaq Trader
(``nasdaqlisted.txt``/``otherlisted.txt``) always outranks SEC EDGAR whenever
both exist. Genuine evidence proved this is not a theoretical risk: adding
``otherlisted.txt`` to reconciliation changed ``canonical_security_id`` for
2,789 of 11,657 previously-existing symbols purely because each one's first
Nasdaq Trader-sourced record (from ``otherlisted.txt``) out-ranked its
previously-preferred SEC EDGAR record -- while the *reconciled consensus*
CIK (computed across every retained source, not just the preferred one)
changed for zero of them. The issuer never changed; only the selected
evidence source did.

The identity contract
----------------------
A stable Apex security identity, ``stable_security_id``, is::

    deterministic_id("apex_stable_security", (
        STABLE_SECURITY_IDENTITY_V1,
        reconciled_security.cik,               # consensus CIK, or None
        normalized_symbol_key(reconciled_security.current_symbol),
    ))

Both inputs come from ``ReconciledSecurity`` -- never from any one member
``SecurityReference`` -- specifically because both are already invariant to
which single source record reconciliation prefers:

* ``ReconciledSecurity.cik`` is the *consensus* CIK across every retained
  source for the symbol (``reconcile_security_references``), not the
  preferred record's own value. It does not move when preference does.
* ``current_symbol`` is reconciliation's own grouping key, identical
  regardless of which member is preferred by construction.
* ``normalized_symbol_key`` additionally collapses pure punctuation-
  representation variance (``BRK.A`` / ``BRK-A`` / ``BRK A``) using the
  identical, already-reviewed normalization options-underlying linkage uses
  (``normalize_option_underlying_symbol``), so a source that spells the same
  security's symbol with a different separator does not mint a new identity.

Deliberately excluded: issuer/company name text, listing exchange, security
type, listing/category status, round lot, tradability/shortability/
marginability, active status, and every research-eligibility or execution-
capability decision -- all of it descriptive, mutable, or provider-priority-
dependent, none of it durable security identity. A CIK is an issuer
identifier, not a security identifier (one issuer can list several classes/
securities), so it is only ever used *paired with* the normalized symbol,
never alone; a symbol is not assumed globally or historically unique either,
so it is only trusted alone when no CIK evidence exists at all for that
symbol (``IdentityBasis.SYMBOL_ONLY`` -- a genuine, documented limitation:
absent a captured point-in-time history, Apex cannot yet distinguish a
symbol-only identity being renamed from the same symbol being genuinely
reused later by an unrelated issuer; see ``detect_stable_identity_supersession``).

This key is a pure function of already-source-preference-invariant fields,
so it is automatically stable across every scenario
``CurrentEquityUniversePolicy``/reconciliation already varies independently
of identity: source arrival order, source priority, richer listing metadata,
descriptive name changes, Alpaca execution-capability evidence, and research-
eligibility decisions. See ``tests/test_security_identity.py`` for the
deterministic proof of each.

Resolution lineage (what changes it, and how the old ID stays honest)
-----------------------------------------------------------------------
Because the key still depends on ``(cik, normalized_symbol)``, a security's
identity is only expected to move for a *genuine* identity event: a symbol
change/rename (detected with confidence only when the *same* CIK anchors
both the vanished and the new key -- ``SUPERSEDED``), or CIK evidence newly
covering a previously CIK-less symbol (``SUPERSEDED``, same symbol both
sides). Anything reconciliation cannot resolve with confidence -- zero or
multiple plausible predecessors, or two real but *different* CIKs sharing
one symbol (a possible genuine ticker reuse by an unrelated issuer) -- is
recorded ``AMBIGUOUS_REQUIRES_REVIEW`` and never silently merged or split.
``IdentityResolution`` is the one append-only ledger fact type answering,
for any ID Apex has ever produced (legacy *and* stable): what is the current
stable identity, is this mapping automatic or review-required, and is an old
identity superseded, aliased, equivalent, ambiguous, or genuinely distinct.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from apex.domain.options_intelligence import (
    OptionContract,
    OptionUnderlyingLinkIndex,
    UnderlyingLinkOutcome,
    normalize_option_underlying_symbol,
)
from apex.domain.security_reference import ReconciledSecurity
from apex.domain.values import content_digest, deterministic_id

STABLE_SECURITY_IDENTITY_V1 = "STABLE_SECURITY_IDENTITY_V1"
SECURITY_IDENTITY_RESOLUTION_V1 = "SECURITY_IDENTITY_RESOLUTION_V1"
OPTION_UNDERLYING_STABLE_LINKAGE_V1 = "OPTION_UNDERLYING_STABLE_LINKAGE_V1"


class SecurityIdentityError(ValueError):
    """Stable-identity evidence violated a canonical invariant."""


class IdentityBasis(StrEnum):
    CIK_AND_SYMBOL = "CIK_AND_SYMBOL"
    SYMBOL_ONLY = "SYMBOL_ONLY"


class IdentityResolutionState(StrEnum):
    CURRENT = "CURRENT"
    ALIAS = "ALIAS"
    SUPERSEDED = "SUPERSEDED"
    EQUIVALENT = "EQUIVALENT"
    AMBIGUOUS_REQUIRES_REVIEW = "AMBIGUOUS_REQUIRES_REVIEW"
    DISTINCT_SECURITY = "DISTINCT_SECURITY"


_UNRESOLVED_STATES = frozenset(
    {IdentityResolutionState.AMBIGUOUS_REQUIRES_REVIEW, IdentityResolutionState.DISTINCT_SECURITY}
)


class OldIdentityKind(StrEnum):
    LEGACY_CANONICAL_SECURITY_ID = "LEGACY_CANONICAL_SECURITY_ID"
    STABLE_SECURITY_ID = "STABLE_SECURITY_ID"


def normalized_symbol_key(symbol: str) -> str:
    """The canonical punctuation-collapsed symbol key this layer keys on.

    A thin, deliberate reuse of options_intelligence's own underlying-symbol
    normalization: the identical punctuation variance (``BRK.A`` vs
    ``BRK-A`` vs ``BRK A``) that already had to be handled for options-
    underlying linkage applies identically to security identity, and both
    layers must agree on exactly one definition of "the same normalized
    symbol" -- otherwise a raw option row could resolve to a different
    identity than the security-identity layer itself would assign the same
    string.
    """
    return normalize_option_underlying_symbol(symbol)


def compute_stable_security_id(*, cik: str | None, current_symbol: str) -> str:
    """The pure identity-key function this module is built around.

    ``cik`` must already be a reconciled *consensus* CIK
    (``ReconciledSecurity.cik``), never one source record's own value --
    the consensus is computed across every retained source for the symbol
    and is therefore already invariant to which single record reconciliation
    happens to currently prefer.
    """
    key = normalized_symbol_key(current_symbol)
    if not key:
        raise SecurityIdentityError("STABLE_IDENTITY_SYMBOL_REQUIRED")
    return deterministic_id("apex_stable_security", (STABLE_SECURITY_IDENTITY_V1, cik, key))


@dataclass(frozen=True, slots=True)
class StableSecurityIdentity:
    """One stable identity as derived from one retained ``ReconciledSecurity``.

    Content-addressed by every field, so re-deriving the identical identity
    from the identical reconciled evidence always reproduces the identical
    row -- a prerequisite for idempotent, replay-safe persistence.
    """

    stable_security_id: str
    identity_basis: IdentityBasis
    cik: str | None
    normalized_symbol: str
    current_symbol: str
    source_identity_hash: str
    source_canonical_security_id: str
    review_required: bool
    canonical_hash: str

    @classmethod
    def derive(cls, security: ReconciledSecurity) -> StableSecurityIdentity:
        stable_id = compute_stable_security_id(
            cik=security.cik, current_symbol=security.current_symbol
        )
        basis = IdentityBasis.CIK_AND_SYMBOL if security.cik else IdentityBasis.SYMBOL_ONLY
        key = normalized_symbol_key(security.current_symbol)
        payload = (
            STABLE_SECURITY_IDENTITY_V1,
            stable_id,
            basis,
            security.cik,
            key,
            security.current_symbol,
            security.canonical_hash,
            security.canonical_security_id,
            security.requires_review,
        )
        return cls(
            stable_id,
            basis,
            security.cik,
            key,
            security.current_symbol,
            security.canonical_hash,
            security.canonical_security_id,
            security.requires_review,
            content_digest(payload),
        )


def apply_stable_identity(
    security: ReconciledSecurity, stable: StableSecurityIdentity
) -> ReconciledSecurity:
    """Swap a reconciled security's legacy ``canonical_security_id`` for its
    stable identity, so the existing, entirely unmodified universe/policy
    machinery in ``security_reference.py`` can build a fresh, stable-identity
    projection without any change to its own code.

    Safe because ``ReconciledSecurity.canonical_hash`` -- the field
    ``save_identities``/``create_current_equity_universe_snapshot`` actually
    de-duplicate and address evidence by -- never included
    ``canonical_security_id`` in its own payload; substituting it changes
    nothing about how already-retained evidence is addressed or replayed, it
    only changes which ID string downstream universe/options consumers see.
    """
    if stable.source_canonical_security_id != security.canonical_security_id:
        raise SecurityIdentityError("STABLE_IDENTITY_SOURCE_MISMATCH")
    return replace(security, canonical_security_id=stable.stable_security_id)


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """One append-only resolution fact: what an old identity (legacy or
    stable) resolves to, how confidently, and why.

    ``old_identity_id`` is heterogeneous by design (a legacy
    ``canonical_security_id`` or an earlier ``stable_security_id``);
    ``old_identity_kind`` disambiguates which namespace it came from. An
    ``AMBIGUOUS_REQUIRES_REVIEW`` or ``DISTINCT_SECURITY`` row is never
    ``automatic`` -- asserting unresolved ambiguity, or asserting two
    identities are genuinely distinct, is never a confident mechanical
    default in this design.
    """

    old_identity_id: str
    old_identity_kind: OldIdentityKind
    resolved_stable_security_id: str | None
    resolution_state: IdentityResolutionState
    automatic: bool
    resolution_method: str
    evidence: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        old_identity_id: str,
        old_identity_kind: OldIdentityKind | str,
        resolved_stable_security_id: str | None,
        resolution_state: IdentityResolutionState | str,
        automatic: bool,
        resolution_method: str,
        evidence: tuple[str, ...],
    ) -> IdentityResolution:
        kind = OldIdentityKind(old_identity_kind)
        state = IdentityResolutionState(resolution_state)
        if not old_identity_id or not resolution_method or not evidence:
            raise SecurityIdentityError("IDENTITY_RESOLUTION_INVALID")
        if state in _UNRESOLVED_STATES and automatic:
            raise SecurityIdentityError("IDENTITY_RESOLUTION_CANNOT_AUTO_RESOLVE_AMBIGUITY")
        payload = (
            SECURITY_IDENTITY_RESOLUTION_V1,
            old_identity_id,
            kind,
            resolved_stable_security_id,
            state,
            automatic,
            resolution_method,
            tuple(sorted(evidence)),
        )
        return cls(
            old_identity_id,
            kind,
            resolved_stable_security_id,
            state,
            automatic,
            resolution_method,
            tuple(sorted(evidence)),
            content_digest(payload),
        )


def resolve_legacy_security(
    security: ReconciledSecurity, *, is_current: bool
) -> tuple[StableSecurityIdentity, IdentityResolution]:
    """Map one retained (historical or latest) ``ReconciledSecurity`` row --
    from any universe build ever recorded, not only the latest -- to its
    stable identity, purely and deterministically from that row's own
    already-retained fields. No candidate matching is required here: unlike
    cross-run supersession, a legacy row already carries the exact
    ``(cik, current_symbol)`` this module's key is built from.

    ``is_current`` records whether the caller has established this exact row
    is the one the *latest* universe build currently returns for its symbol
    (``CURRENT``) versus an older, still-retained row for the same symbol
    that an earlier universe build returned before source preference changed
    which record won (``ALIAS``) -- both resolve to the identical stable
    identity, proving an old ``canonical_security_id`` from any retained
    build always resolves. A row already flagged ``requires_review`` by
    reconciliation's own existing conflict detection is never asserted
    ``CURRENT``/``ALIAS`` with confidence; it is reported
    ``AMBIGUOUS_REQUIRES_REVIEW`` instead, reusing that existing signal
    rather than inventing a parallel one.
    """
    stable = StableSecurityIdentity.derive(security)
    if security.requires_review:
        state = IdentityResolutionState.AMBIGUOUS_REQUIRES_REVIEW
        automatic = False
    else:
        state = IdentityResolutionState.CURRENT if is_current else IdentityResolutionState.ALIAS
        automatic = True
    resolution = IdentityResolution.create(
        old_identity_id=security.canonical_security_id,
        old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
        resolved_stable_security_id=stable.stable_security_id,
        resolution_state=state,
        automatic=automatic,
        resolution_method=(
            "DIRECT_RECONCILED_CIK_AND_SYMBOL" if security.cik else "DIRECT_RECONCILED_SYMBOL_ONLY"
        ),
        evidence=(security.canonical_hash,),
    )
    return stable, resolution


def detect_stable_identity_supersession(
    *,
    current: tuple[StableSecurityIdentity, ...],
    prior: tuple[StableSecurityIdentity, ...],
) -> tuple[IdentityResolution, ...]:
    """Compare freshly derived stable identities against the identities a
    previous projection run established, and explain every prior stable id
    the current run no longer reproduces.

    ``prior`` must already be the latest known ``StableSecurityIdentity`` per
    distinct ``stable_security_id`` (a persistence-layer/read-time concern --
    this pure function has no timestamp to resolve "latest" from). Candidate
    predecessors for a "new" current identity are prior identities that are
    themselves missing from the current run's own set *and* share either the
    exact same CIK or the exact same normalized symbol with the candidate.
    A CIK match with a changed symbol is a confident, automatic rename (CIK
    is a durable SEC-assigned issuer anchor); a same-symbol match with a
    newly-present or previously-absent CIK is a confident, automatic
    enrichment. Anything else -- zero candidates (nothing to supersede, a
    genuinely new identity), more than one candidate, or a same-symbol match
    where both sides carry a real but *different* CIK (a possible genuine
    ticker reuse by an unrelated issuer) -- is reported
    ``AMBIGUOUS_REQUIRES_REVIEW`` and never auto-resolved.
    """
    current_ids = {item.stable_security_id for item in current}
    prior_ids = {item.stable_security_id for item in prior}
    vanished = tuple(item for item in prior if item.stable_security_id not in current_ids)
    new_arrivals = tuple(item for item in current if item.stable_security_id not in prior_ids)
    resolutions: list[IdentityResolution] = []
    for new in sorted(new_arrivals, key=lambda item: item.stable_security_id):
        cik_matches = tuple(
            old
            for old in vanished
            if old.cik is not None and new.cik is not None and old.cik == new.cik
        )
        symbol_matches = tuple(
            old for old in vanished if old.normalized_symbol == new.normalized_symbol
        )
        by_id: dict[str, StableSecurityIdentity] = {}
        for old in (*cik_matches, *symbol_matches):
            by_id[old.stable_security_id] = old
        candidates = tuple(sorted(by_id.values(), key=lambda item: item.stable_security_id))
        if not candidates:
            continue  # genuinely new identity: nothing to supersede
        if len(candidates) > 1:
            resolutions.extend(
                _ambiguous(old, new, "MULTIPLE_CANDIDATE_PREDECESSORS") for old in candidates
            )
            continue
        old = candidates[0]
        same_symbol = old.normalized_symbol == new.normalized_symbol
        conflicting_cik = (
            same_symbol and old.cik is not None and new.cik is not None and old.cik != new.cik
        )
        if conflicting_cik:
            resolutions.append(_ambiguous(old, new, "CONFLICTING_CIK_SAME_SYMBOL"))
            continue
        method = "SYMBOL_MATCH_CIK_ENRICHED" if same_symbol else "CIK_MATCH_SYMBOL_CHANGED"
        resolutions.append(
            IdentityResolution.create(
                old_identity_id=old.stable_security_id,
                old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
                resolved_stable_security_id=new.stable_security_id,
                resolution_state=IdentityResolutionState.SUPERSEDED,
                automatic=True,
                resolution_method=method,
                evidence=(old.canonical_hash, new.canonical_hash),
            )
        )
    return tuple(resolutions)


def _ambiguous(
    old: StableSecurityIdentity, new: StableSecurityIdentity, reason: str
) -> IdentityResolution:
    return IdentityResolution.create(
        old_identity_id=old.stable_security_id,
        old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
        resolved_stable_security_id=new.stable_security_id,
        resolution_state=IdentityResolutionState.AMBIGUOUS_REQUIRES_REVIEW,
        automatic=False,
        resolution_method=reason,
        evidence=(old.canonical_hash, new.canonical_hash),
    )


def detect_symbol_punctuation_collisions(
    current: tuple[StableSecurityIdentity, ...],
) -> tuple[IdentityResolution, ...]:
    """Cross-reference distinct current-run stable identities that share the
    identical normalized symbol only because CIK presence differs between
    them -- for example an SEC-sourced ``BRK-A`` reconciled separately from
    a Nasdaq-sourced ``BRK.A`` for what looks like the same underlying share
    class, spelled with different punctuation by each source.

    This is a pre-existing fragmentation in ``reconcile_security_references``
    itself (it groups by *exact* ``current_symbol`` text, so two differently
    punctuated spellings of the same security never become one
    ``ReconciledSecurity`` to begin with), not something this module
    introduces. Both stable identities remain fully independent and
    ``CURRENT``; this only records an advisory, mutual
    ``AMBIGUOUS_REQUIRES_REVIEW`` cross-reference so an operator can see the
    possible duplication. Per-symbol identity is never merged on a
    ticker-string match alone.
    """
    by_normalized: dict[str, list[StableSecurityIdentity]] = {}
    for item in current:
        by_normalized.setdefault(item.normalized_symbol, []).append(item)
    resolutions: list[IdentityResolution] = []
    for group in by_normalized.values():
        distinct_ids = sorted({item.stable_security_id for item in group})
        if len(distinct_ids) < 2:
            continue
        by_id = {item.stable_security_id: item for item in group}
        for stable_id in distinct_ids:
            other_ids = tuple(value for value in distinct_ids if value != stable_id)
            item = by_id[stable_id]
            resolutions.append(
                IdentityResolution.create(
                    old_identity_id=stable_id,
                    old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
                    resolved_stable_security_id=None,
                    resolution_state=IdentityResolutionState.AMBIGUOUS_REQUIRES_REVIEW,
                    automatic=False,
                    resolution_method="NORMALIZED_SYMBOL_SHARED_ACROSS_DISTINCT_CIK_PRESENCE",
                    evidence=(item.canonical_hash, *other_ids),
                )
            )
    return tuple(resolutions)


# --- Options-underlying relink against the stable identity space ----------


@dataclass(frozen=True, slots=True)
class RawUnderlyingEvidence:
    """One distinct raw options-directory underlying symbol string, plus
    already-retained evidence about it: how many currently-retained
    contracts carry it, and -- when the original capture already linked it
    -- the exact underlying id that original linkage recorded. Built once
    from already-retained ``OptionContract`` rows, never from a new capture.
    """

    raw_underlying_symbol: str
    contract_count: int
    historical_underlying_security_id: str | None


def raw_underlying_symbol_from_contract(contract: OptionContract) -> str | None:
    """Recover the exact raw options-directory "Underlying Symbol" text a
    retained contract was linked from, without touching its immutable
    payload or re-fetching anything.

    ``parse_nasdaq_options_directory`` (``adapters/options_reference.py``)
    always builds ``provider_contract_id`` as exactly
    ``root|option_type|expiration|strike|underlying`` -- the fifth, final
    ``|``-delimited field is always the raw underlying text the *original*
    linkage decision used. This only decodes an already-retained, already
    domain-level field; it performs no I/O.
    """
    parts = contract.provider_contract_id.split("|")
    if len(parts) != 5:
        return None
    return parts[-1]


def collect_raw_underlying_evidence(
    contracts: tuple[OptionContract, ...],
) -> tuple[RawUnderlyingEvidence, ...]:
    """Group already-retained contracts by their raw underlying symbol.

    Fails closed (rather than guessing) if the same raw symbol was somehow
    originally linked to more than one distinct historical underlying id --
    genuine retained evidence shows this never happens (every one of 3,752
    distinct raw symbols across 1,422,302 retained contracts maps to exactly
    one historical underlying id), so this is a defensive invariant, not an
    expected path.
    """
    grouped: dict[str, list[OptionContract]] = {}
    unparseable = 0
    for contract in contracts:
        raw = raw_underlying_symbol_from_contract(contract)
        if raw is None:
            unparseable += 1
            continue
        grouped.setdefault(raw, []).append(contract)
    if unparseable:
        raise SecurityIdentityError(f"OPTION_CONTRACT_PROVIDER_ID_UNPARSEABLE_COUNT_{unparseable}")
    evidence: list[RawUnderlyingEvidence] = []
    for raw, members in grouped.items():
        underlying_ids = {item.underlying_canonical_security_id for item in members}
        if len(underlying_ids) > 1:
            raise SecurityIdentityError(f"RAW_UNDERLYING_SYMBOL_MULTIPLE_HISTORICAL_IDS:{raw}")
        evidence.append(RawUnderlyingEvidence(raw, len(members), next(iter(underlying_ids))))
    return tuple(sorted(evidence, key=lambda item: item.raw_underlying_symbol))


@dataclass(frozen=True, slots=True)
class OptionUnderlyingStableLinkage:
    """One raw options-underlying symbol's relink decision against the
    current stable-identity space, for one bounded linkage run.

    Retained once per distinct raw symbol (mirroring
    ``OptionUnderlyingLinkRejection``'s existing convention), never once per
    contract, so this stays bounded by distinct-underlying-symbol count
    (thousands) rather than total retained contract count (millions).
    """

    raw_underlying_symbol: str
    outcome: UnderlyingLinkOutcome
    resolved_stable_security_id: str | None
    candidate_stable_security_ids: tuple[str, ...]
    historical_underlying_security_id: str | None
    reasons: tuple[str, ...]
    contract_count: int
    linkage_run_id: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        raw_underlying_symbol: str,
        outcome: UnderlyingLinkOutcome | str,
        resolved_stable_security_id: str | None,
        candidate_stable_security_ids: tuple[str, ...],
        historical_underlying_security_id: str | None,
        reasons: tuple[str, ...],
        contract_count: int,
        linkage_run_id: str,
    ) -> OptionUnderlyingStableLinkage:
        outcome_value = UnderlyingLinkOutcome(outcome)
        linked = outcome_value is UnderlyingLinkOutcome.LINKED
        if (
            not raw_underlying_symbol
            or not linkage_run_id
            or contract_count < 0
            or not reasons
            or (linked and not resolved_stable_security_id)
            or (not linked and resolved_stable_security_id is not None)
        ):
            raise SecurityIdentityError("OPTION_UNDERLYING_STABLE_LINKAGE_INVALID")
        candidates = tuple(sorted(candidate_stable_security_ids))
        payload = (
            OPTION_UNDERLYING_STABLE_LINKAGE_V1,
            raw_underlying_symbol,
            outcome_value,
            resolved_stable_security_id,
            candidates,
            historical_underlying_security_id,
            tuple(sorted(reasons)),
            contract_count,
        )
        return cls(
            raw_underlying_symbol,
            outcome_value,
            resolved_stable_security_id,
            candidates,
            historical_underlying_security_id,
            tuple(sorted(reasons)),
            contract_count,
            linkage_run_id,
            content_digest(payload),
        )


def relink_option_underlyings_to_stable_identity(
    *,
    raw_evidence: tuple[RawUnderlyingEvidence, ...],
    previously_rejected: tuple[tuple[str, int], ...],
    stable_symbol_index: dict[str, str],
    linkage_run_id: str,
) -> tuple[OptionUnderlyingStableLinkage, ...]:
    """Relink already-retained raw options-underlying evidence against the
    current stable-identity symbol index.

    Consumes only already-retained genuine evidence -- ``raw_evidence`` from
    currently-retained ``OptionContract`` rows (``collect_raw_underlying_evidence``)
    and ``previously_rejected`` (raw symbol, row count) pairs recovered from
    the original capture's retained ``OptionUnderlyingLinkRejection``
    observations -- and issues no new provider request. Reuses
    ``OptionUnderlyingLinkIndex`` completely unmodified, fed the current
    stable-id symbol map (``current_symbol -> stable_security_id``) instead
    of the legacy one: exact match is always tried first, falling back to
    normalized-punctuation matching only when it resolves to exactly one
    candidate -- exactly as already reviewed for the legacy options-linkage
    path.
    """
    if not linkage_run_id:
        raise SecurityIdentityError("LINKAGE_RUN_ID_REQUIRED")
    link_index = OptionUnderlyingLinkIndex.build(stable_symbol_index)
    linkages: list[OptionUnderlyingStableLinkage] = []
    seen_symbols: set[str] = set()
    for item in raw_evidence:
        seen_symbols.add(item.raw_underlying_symbol)
        link = link_index.resolve(item.raw_underlying_symbol)
        linkages.append(
            OptionUnderlyingStableLinkage.create(
                raw_underlying_symbol=item.raw_underlying_symbol,
                outcome=link.outcome,
                resolved_stable_security_id=link.canonical_security_id,
                candidate_stable_security_ids=link.candidate_security_ids,
                historical_underlying_security_id=item.historical_underlying_security_id,
                reasons=link.reasons,
                contract_count=item.contract_count,
                linkage_run_id=linkage_run_id,
            )
        )
    for raw_symbol, row_count in previously_rejected:
        if raw_symbol in seen_symbols:
            continue
        seen_symbols.add(raw_symbol)
        link = link_index.resolve(raw_symbol)
        linkages.append(
            OptionUnderlyingStableLinkage.create(
                raw_underlying_symbol=raw_symbol,
                outcome=link.outcome,
                resolved_stable_security_id=link.canonical_security_id,
                candidate_stable_security_ids=link.candidate_security_ids,
                historical_underlying_security_id=None,
                reasons=link.reasons,
                contract_count=row_count,
                linkage_run_id=linkage_run_id,
            )
        )
    return tuple(sorted(linkages, key=lambda item: item.raw_underlying_symbol))
