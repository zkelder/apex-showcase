"""Current-listed U.S. security reference evidence and immutable universe policy.

This module is deliberately provider-neutral and research-only.  Current
reference evidence is not historical membership evidence and grants no order,
position, reservation, ledger, PAPER, or LIVE authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from apex.domain.values import content_digest, deterministic_id

APEX_SECURITY_REFERENCE_V1 = "APEX_SECURITY_REFERENCE_V1"
APEX_SECURITY_REFERENCE_RECONCILIATION_V1 = "APEX_SECURITY_REFERENCE_RECONCILIATION_V1"
CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1 = "CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1"
APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1 = "APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1"
CURRENT_LISTED_WARNING = "CURRENT-LISTED UNIVERSE — NOT HISTORICAL POINT-IN-TIME MEMBERSHIP"


class SecurityReferenceError(ValueError):
    """Reference evidence violated a canonical invariant."""


class SecurityType(StrEnum):
    COMMON_STOCK = "COMMON_STOCK"
    REIT_COMMON = "REIT_COMMON"
    ADR = "ADR"
    ETF = "ETF"
    ETN = "ETN"
    MUTUAL_FUND = "MUTUAL_FUND"
    PREFERRED = "PREFERRED"
    WARRANT = "WARRANT"
    RIGHT = "RIGHT"
    UNIT = "UNIT"
    WHEN_ISSUED = "WHEN_ISSUED"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class EvidenceState(StrEnum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class ExecutionCapabilityState(StrEnum):
    """Provider (Alpaca) execution-capability evidence, kept strictly separate
    from research-universe eligibility.  Absent credentials or absent
    provider evidence must never read as, or silently become, a positive
    execution capability -- and must never exclude an otherwise valid
    security from the research universe.
    """

    ALPACA_CAPABILITY_NOT_OBSERVED = "ALPACA_CAPABILITY_NOT_OBSERVED"
    NOT_CONFIRMED_ALPACA_TRADABLE = "NOT_CONFIRMED_ALPACA_TRADABLE"
    ALPACA_TRADABLE = "ALPACA_TRADABLE"
    LEGACY_EXECUTION_CAPABILITY_UNKNOWN = "LEGACY_EXECUTION_CAPABILITY_UNKNOWN"
    """Reserved for schema-compatibility rehydration of universe snapshots
    retained before this research/execution separation existed at all.
    ``determine_execution_capability`` never produces this value -- it is
    honest about a stronger fact than "not observed": the retained payload
    predates the concept, so no per-security Alpaca evidence was ever
    captured for it, positive or negative.
    """


def determine_execution_capability(
    security: ReconciledSecurity,
) -> tuple[ExecutionCapabilityState, tuple[str, ...]]:
    """Derive provider execution capability from retained Alpaca evidence only.

    This is intentionally independent of ``CurrentEquityUniversePolicy``: no
    research-eligibility decision may ever depend on it, and this function
    never consults research-side fields (security type, listing status,
    ambiguity).  ``EvidenceState.UNKNOWN`` covers both "credentials were
    absent so Alpaca was never queried" and "Alpaca was queried but this
    security was absent from its asset master" -- both are honestly
    "not observed," never inferred as tradable or not tradable.
    """
    if security.tradable is EvidenceState.YES:
        return ExecutionCapabilityState.ALPACA_TRADABLE, ()
    if security.tradable is EvidenceState.NO:
        return (
            ExecutionCapabilityState.NOT_CONFIRMED_ALPACA_TRADABLE,
            ("ALPACA_EVIDENCE_REPORTS_NOT_TRADABLE",),
        )
    return (
        ExecutionCapabilityState.ALPACA_CAPABILITY_NOT_OBSERVED,
        ("NO_ALPACA_EVIDENCE_OBSERVED",),
    )


class ReconciliationClassification(StrEnum):
    CONFIRMED = "CONFIRMED"
    EXPLAINED_DIFFERENCE = "EXPLAINED_DIFFERENCE"
    SYMBOL_CONFLICT = "SYMBOL_CONFLICT"
    TYPE_CONFLICT = "TYPE_CONFLICT"
    LISTING_CONFLICT = "LISTING_CONFLICT"
    PROVIDER_ONLY = "PROVIDER_ONLY"
    INCOMPLETE_IDENTITY = "INCOMPLETE_IDENTITY"
    INACTIVE = "INACTIVE"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    receipt_id: str
    provider: str
    source_timestamp: datetime
    received_at: datetime
    payload_sha256: str
    byte_count: int
    source_locator: str
    limitations: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        source_timestamp: datetime,
        received_at: datetime,
        payload_sha256: str,
        byte_count: int,
        source_locator: str,
        limitations: tuple[str, ...] = (),
    ) -> SourceReceipt:
        if (
            not provider
            or not source_locator
            or byte_count < 0
            or len(payload_sha256) != 64
            or source_timestamp.tzinfo is None
            or received_at.tzinfo is None
        ):
            raise SecurityReferenceError("SOURCE_RECEIPT_INVALID")
        payload = (
            provider,
            source_timestamp,
            received_at,
            payload_sha256,
            byte_count,
            source_locator,
            tuple(sorted(limitations)),
        )
        return cls(
            deterministic_id("security_source_receipt", payload),
            provider,
            source_timestamp,
            received_at,
            payload_sha256,
            byte_count,
            source_locator,
            tuple(sorted(limitations)),
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class SecurityReference:
    canonical_security_id: str
    current_symbol: str
    provider_symbol: str
    issuer_name: str
    listing_exchange: str
    listing_category: str
    security_type: SecurityType
    common_stock: EvidenceState
    etf: EvidenceState
    adr: EvidenceState
    reit: EvidenceState
    test_issue: EvidenceState
    listing_status: str
    round_lot_size: int | None
    tradable: EvidenceState
    shortable: EvidenceState
    marginable: EvidenceState
    active: EvidenceState
    cik: str | None
    source_provider: str
    source_receipt_id: str
    source_timestamp: datetime
    source_payload_hash: str
    provider_disagreements: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_identity_hash: str

    @classmethod
    def create(
        cls,
        *,
        current_symbol: str,
        provider_symbol: str,
        issuer_name: str,
        listing_exchange: str,
        listing_category: str,
        security_type: SecurityType | str,
        common_stock: EvidenceState | str,
        etf: EvidenceState | str,
        adr: EvidenceState | str,
        reit: EvidenceState | str,
        test_issue: EvidenceState | str,
        listing_status: str,
        round_lot_size: int | None,
        tradable: EvidenceState | str,
        shortable: EvidenceState | str,
        marginable: EvidenceState | str,
        active: EvidenceState | str,
        cik: str | None,
        source_provider: str,
        source_receipt_id: str,
        source_timestamp: datetime,
        source_payload_hash: str,
        provider_disagreements: tuple[str, ...] = (),
        limitations: tuple[str, ...] = (),
    ) -> SecurityReference:
        symbol = current_symbol.strip().upper()
        provider_value = provider_symbol.strip().upper()
        normalized_cik = cik.zfill(10) if cik else None
        if (
            not symbol
            or not provider_value
            or not issuer_name.strip()
            or not source_provider
            or not source_receipt_id
            or source_timestamp.tzinfo is None
            or len(source_payload_hash) != 64
            or (round_lot_size is not None and round_lot_size <= 0)
            or (normalized_cik is not None and not normalized_cik.isdigit())
        ):
            raise SecurityReferenceError("SECURITY_REFERENCE_INVALID")
        identity = (
            APEX_SECURITY_REFERENCE_V1,
            symbol,
            issuer_name.strip(),
            listing_exchange.strip().upper(),
            SecurityType(security_type),
            normalized_cik,
        )
        canonical_identity_hash = content_digest(identity)
        return cls(
            deterministic_id("apex_security", identity),
            symbol,
            provider_value,
            issuer_name.strip(),
            listing_exchange.strip().upper(),
            listing_category.strip().upper(),
            SecurityType(security_type),
            EvidenceState(common_stock),
            EvidenceState(etf),
            EvidenceState(adr),
            EvidenceState(reit),
            EvidenceState(test_issue),
            listing_status.strip().upper(),
            round_lot_size,
            EvidenceState(tradable),
            EvidenceState(shortable),
            EvidenceState(marginable),
            EvidenceState(active),
            normalized_cik,
            source_provider,
            source_receipt_id,
            source_timestamp,
            source_payload_hash,
            tuple(sorted(provider_disagreements)),
            tuple(sorted(limitations)),
            canonical_identity_hash,
        )


@dataclass(frozen=True, slots=True)
class StandardizedCompanyFact:
    canonical_security_id: str
    taxonomy: str
    fact_name: str
    unit: str
    value: str
    period_start: str | None
    period_end: str
    filed_at: str
    form: str
    accession: str
    source_receipt_id: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        canonical_security_id: str,
        taxonomy: str,
        fact_name: str,
        unit: str,
        value: str,
        period_start: str | None,
        period_end: str,
        filed_at: str,
        form: str,
        accession: str,
        source_receipt_id: str,
    ) -> StandardizedCompanyFact:
        if not all(
            (
                canonical_security_id,
                taxonomy,
                fact_name,
                unit,
                value,
                period_end,
                filed_at,
                form,
                accession,
                source_receipt_id,
            )
        ):
            raise SecurityReferenceError("STANDARDIZED_COMPANY_FACT_INVALID")
        payload = (
            canonical_security_id,
            taxonomy,
            fact_name,
            unit,
            value,
            period_start,
            period_end,
            filed_at,
            form,
            accession,
            source_receipt_id,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class ReconciledSecurity:
    canonical_security_id: str
    current_symbol: str
    issuer_name: str
    listing_exchange: str
    security_type: SecurityType
    common_stock: EvidenceState
    etf: EvidenceState
    adr: EvidenceState
    reit: EvidenceState
    test_issue: EvidenceState
    listing_status: str
    tradable: EvidenceState
    active: EvidenceState
    cik: str | None
    source_record_ids: tuple[str, ...]
    classifications: tuple[ReconciliationClassification, ...]
    disagreements: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str

    @property
    def requires_review(self) -> bool:
        blocked = {
            ReconciliationClassification.SYMBOL_CONFLICT,
            ReconciliationClassification.TYPE_CONFLICT,
            ReconciliationClassification.LISTING_CONFLICT,
            ReconciliationClassification.MANUAL_REVIEW_REQUIRED,
        }
        return bool(blocked.intersection(self.classifications))


def reconcile_security_references(
    records: tuple[SecurityReference, ...],
) -> tuple[ReconciledSecurity, ...]:
    """Reconcile exact symbols while retaining every provider record."""
    grouped: dict[str, list[SecurityReference]] = {}
    for record in records:
        grouped.setdefault(record.current_symbol, []).append(record)
    reconciled: list[ReconciledSecurity] = []
    for symbol, members in sorted(grouped.items()):
        all_known_types = {
            member.security_type
            for member in members
            if member.security_type is not SecurityType.UNKNOWN
        }
        authoritative_types = {
            member.security_type
            for member in members
            if member.security_type is not SecurityType.UNKNOWN
            and member.source_provider in {"NASDAQ_TRADER", "OPERATOR_OVERRIDE"}
        }
        types = authoritative_types or all_known_types
        exchanges = {member.listing_exchange for member in members if member.listing_exchange}
        ciks = {member.cik for member in members if member.cik}
        active_states = {member.active for member in members}
        classifications: set[ReconciliationClassification] = set()
        disagreements: list[str] = []
        providers = {member.source_provider for member in members}
        if len(providers) == 1:
            classifications.add(ReconciliationClassification.PROVIDER_ONLY)
        else:
            classifications.add(ReconciliationClassification.CONFIRMED)
        if len(types) > 1:
            classifications.add(ReconciliationClassification.TYPE_CONFLICT)
            disagreements.append("SECURITY_TYPE_DISAGREEMENT")
        if len(exchanges) > 1:
            classifications.add(ReconciliationClassification.LISTING_CONFLICT)
            disagreements.append("LISTING_EXCHANGE_DISAGREEMENT")
        if len(ciks) > 1:
            classifications.add(ReconciliationClassification.MANUAL_REVIEW_REQUIRED)
            disagreements.append("CIK_DISAGREEMENT")
        if not ciks:
            classifications.add(ReconciliationClassification.INCOMPLETE_IDENTITY)
        if active_states == {EvidenceState.NO}:
            classifications.add(ReconciliationClassification.INACTIVE)
        preferred = sorted(
            members,
            key=lambda item: (
                item.source_provider != "NASDAQ_TRADER",
                item.source_provider != "SEC_EDGAR",
                item.source_provider,
            ),
        )[0]
        payload = (
            APEX_SECURITY_REFERENCE_RECONCILIATION_V1,
            symbol,
            tuple(sorted(member.canonical_identity_hash for member in members)),
            tuple(sorted(classification.value for classification in classifications)),
            tuple(sorted(disagreements)),
        )
        reconciled.append(
            ReconciledSecurity(
                preferred.canonical_security_id,
                symbol,
                preferred.issuer_name,
                preferred.listing_exchange,
                next(iter(types)) if len(types) == 1 else SecurityType.UNKNOWN,
                preferred.common_stock,
                preferred.etf,
                preferred.adr,
                preferred.reit,
                preferred.test_issue,
                preferred.listing_status,
                _consensus_state(tuple(member.tradable for member in members)),
                _consensus_state(tuple(member.active for member in members)),
                next(iter(ciks)) if len(ciks) == 1 else None,
                tuple(sorted(member.canonical_security_id for member in members)),
                tuple(sorted(classifications, key=lambda item: item.value)),
                tuple(sorted(disagreements)),
                tuple(sorted({value for member in members for value in member.limitations})),
                content_digest(payload),
            )
        )
    return tuple(reconciled)


def _consensus_state(states: tuple[EvidenceState, ...]) -> EvidenceState:
    known = {state for state in states if state is not EvidenceState.UNKNOWN}
    return known.pop() if len(known) == 1 else EvidenceState.UNKNOWN


@dataclass(frozen=True, slots=True)
class CurrentEquityUniversePolicy:
    """Research-universe inclusion policy.

    This policy governs research eligibility only, from retained
    official/reference evidence (type, listing status, identity, freshness).
    It deliberately carries no Alpaca or other provider-execution field: see
    ``determine_execution_capability`` for that entirely separate decision,
    which this policy never consults and can never be widened to consult.
    """

    policy_id: str
    eligible_types: tuple[SecurityType, ...]
    allow_adrs: bool
    adverse_listing_states: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def initial(cls) -> CurrentEquityUniversePolicy:
        payload = (
            CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1,
            (SecurityType.COMMON_STOCK, SecurityType.REIT_COMMON),
            False,
            ("BANKRUPT", "DEFICIENT", "DELINQUENT"),
        )
        return cls(
            CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1,
            payload[1],
            payload[2],
            payload[3],
            content_digest(payload),
        )

    def research_exclusion_reasons(self, security: ReconciledSecurity) -> tuple[str, ...]:
        """Reasons this security is excluded from the research universe.

        Determined solely from retained official/reference evidence: listed
        status, security type, listing exchange/category, identity
        ambiguity, and freshness.  Provider execution evidence (Alpaca or
        any other) is never consulted here.
        """
        reasons: list[str] = []
        if security.security_type not in self.eligible_types:
            reasons.append(f"SECURITY_TYPE_{security.security_type.value}_EXCLUDED")
        if security.adr is EvidenceState.YES and not self.allow_adrs:
            reasons.append("ADR_REVIEW_ONLY")
        if security.test_issue is not EvidenceState.NO:
            reasons.append("TEST_OR_UNKNOWN_TEST_ISSUE")
        if security.active is not EvidenceState.YES:
            reasons.append("NOT_CONFIRMED_ACTIVE")
        if security.requires_review:
            reasons.append("UNRESOLVED_IDENTITY_CONFLICT")
        if any(status in security.listing_status for status in self.adverse_listing_states):
            reasons.append("ADVERSE_LISTING_STATUS")
        return tuple(sorted(set(reasons)))


@dataclass(frozen=True, slots=True)
class UniverseMemberDecision:
    """One security's research-eligibility and execution-capability decisions.

    The two decisions are computed independently and never influence each
    other: ``research_eligible`` never depends on ``execution_capability``,
    and a security lacking observed execution capability remains fully
    research-eligible when the retained official evidence otherwise
    qualifies it.
    """

    canonical_security_id: str
    current_symbol: str
    research_eligible: bool
    research_reasons: tuple[str, ...]
    execution_capability: ExecutionCapabilityState
    execution_reasons: tuple[str, ...]
    security_hash: str


@dataclass(frozen=True, slots=True)
class CurrentEquityUniverseSnapshot:
    universe_id: str
    policy_id: str
    policy_hash: str
    as_of_date: str
    created_at: datetime
    source_receipt_ids: tuple[str, ...]
    total_considered: int
    security_type_counts: tuple[tuple[str, int], ...]
    research_eligible_count: int
    research_exclusion_counts: tuple[tuple[str, int], ...]
    execution_capability_counts: tuple[tuple[str, int], ...]
    execution_capability_known_count: int
    execution_capability_unknown_count: int
    decisions: tuple[UniverseMemberDecision, ...]
    provider_disagreements: tuple[str, ...]
    warning: str
    canonical_hash: str


def create_current_equity_universe_snapshot(
    *,
    securities: tuple[ReconciledSecurity, ...],
    policy: CurrentEquityUniversePolicy,
    source_receipt_ids: tuple[str, ...],
    as_of_date: date,
    created_at: datetime,
) -> CurrentEquityUniverseSnapshot:
    if created_at.tzinfo is None:
        raise SecurityReferenceError("UNIVERSE_TIMESTAMP_MUST_BE_AWARE")
    type_counts: dict[str, int] = {}
    research_exclusion_counts: dict[str, int] = {}
    execution_capability_counts: dict[str, int] = {}
    decisions: list[UniverseMemberDecision] = []
    disagreements: set[str] = set()
    seen_ids: set[str] = set()
    for security in sorted(securities, key=lambda item: item.canonical_security_id):
        if security.canonical_security_id in seen_ids:
            raise SecurityReferenceError("DUPLICATE_CANONICAL_SECURITY")
        seen_ids.add(security.canonical_security_id)
        type_counts[security.security_type.value] = (
            type_counts.get(security.security_type.value, 0) + 1
        )
        research_reasons = policy.research_exclusion_reasons(security)
        for reason in research_reasons:
            research_exclusion_counts[reason] = research_exclusion_counts.get(reason, 0) + 1
        execution_state, execution_reasons = determine_execution_capability(security)
        execution_capability_counts[execution_state.value] = (
            execution_capability_counts.get(execution_state.value, 0) + 1
        )
        disagreements.update(security.disagreements)
        decisions.append(
            UniverseMemberDecision(
                security.canonical_security_id,
                security.current_symbol,
                not research_reasons,
                research_reasons,
                execution_state,
                execution_reasons,
                security.canonical_hash,
            )
        )
    execution_unknown = execution_capability_counts.get(
        ExecutionCapabilityState.ALPACA_CAPABILITY_NOT_OBSERVED.value, 0
    )
    payload = (
        APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1,
        policy.canonical_hash,
        as_of_date.isoformat(),
        created_at,
        tuple(sorted(source_receipt_ids)),
        tuple(decisions),
        tuple(sorted(disagreements)),
        CURRENT_LISTED_WARNING,
    )
    canonical_hash = content_digest(payload)
    return CurrentEquityUniverseSnapshot(
        deterministic_id("broad_equity_universe", payload),
        policy.policy_id,
        policy.canonical_hash,
        as_of_date.isoformat(),
        created_at,
        tuple(sorted(source_receipt_ids)),
        len(decisions),
        tuple(sorted(type_counts.items())),
        sum(decision.research_eligible for decision in decisions),
        tuple(sorted(research_exclusion_counts.items())),
        tuple(sorted(execution_capability_counts.items())),
        len(decisions) - execution_unknown,
        execution_unknown,
        tuple(decisions),
        tuple(sorted(disagreements)),
        CURRENT_LISTED_WARNING,
        canonical_hash,
    )
