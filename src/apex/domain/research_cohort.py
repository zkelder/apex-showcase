"""Genuine multi-asset research cohort: a deterministic, versioned, pre-
declared security selection policy and its resolution against retained
stable-security identity evidence.

Why this module exists
-----------------------
``application.historical_market_data.CreateHistoricalCapturePlan`` already
resolves an *operator-supplied* symbol list against
``StableSecurityIdentitySource.latest_stable_identities()`` -- the identical
"declare an exact fixed list up front" ergonomics
``domain.real_securities_data.SecuritiesUniverseSnapshot.fixed`` already
uses. What is missing is the declared list itself: a cohort chosen
*deterministically* from a versioned research policy rather than an ad hoc
CLI argument, with its role/sector rationale retained alongside it so a
later reviewer can see *why* each security is present without re-deriving
it from memory.

Selection methodology (must be read before editing ``GENUINE_MULTI_ASSET_
COHORT_POLICY_V1``)
--------------------------------------------------------------------------
Every member below is selected by one of exactly two criteria, both fixed
*before* any price/return history is consulted:

1. Broad index/sector/rates/commodity ETFs are the standard, publicly
   documented State Street/BlackRock/iShares sector-SPDR and asset-class
   proxy set (the same eleven GICS sector SPDRs used industry-wide, plus
   the most commonly cited broad-index, Treasury-duration, credit, and
   commodity/currency proxy ETFs).
2. Single-name equities are selected by *index-membership plausibility and
   sector coverage* (large, long-listed, broadly held names spread across
   every GICS sector for the large-cap tranche; smaller, unremarkable,
   long-listed names for the smaller-cap tranche) -- never by having
   looked at any security's historical price, return, or volatility first.

No member was added, removed, or reordered because of how it performed
historically. ``canonical_hash`` is a pure function of the declared roster
below; changing the roster for any reason (including a symbol that no
longer resolves -- see ``resolve_cohort``) requires a new policy version,
never a silent in-place edit of this one.

Point-in-time listing/delisting evidence (milestone section 7)
--------------------------------------------------------------------------
Investigated, not assumed: Apex's only retained issuer-reference sources
(``adapters.security_reference.parse_nasdaq_symbol_directory`` over Nasdaq
Trader's ``nasdaqlisted.txt``/``otherlisted.txt``, and ``parse_sec_company_
tickers`` over SEC's ``company_tickers.json``) are both *current-day symbol
directory snapshots* -- point-in-time status (``EvidenceState``) and a
listing-status *code*, never a listing date, delisting date, symbol-change
date, or any historical status-change log. Apex ingests only today's
snapshot of each, never a retained daily archive of them. No trustworthy
historical listing/delisting membership source therefore exists yet in
Apex's retained evidence, and this module does not invent one --
``PointInTimeUniverseState.POINT_IN_TIME_UNIVERSE_INCOMPLETE`` (``domain.
historical_market_data``) stays the honest, carried-forward state.

This is not a theoretical gap: three real 2025 corporate events surfaced
it directly while resolving this exact cohort roster. JWN (Nordstrom, taken
private), PARA (Paramount, merged into Paramount Skydance), and X (US
Steel, acquired by Nippon Steel) were all originally considered for the
smaller-cap tranche below and all three failed to resolve against
``latest_stable_identities()`` -- the *current* snapshot has already moved
on, but nothing in retained evidence records *when* or *why*, so a
genuinely point-in-time historical universe as of, say, 2021 cannot be
reconstructed from what Apex retains today. They are absent from the
roster below for exactly that reason, not because of anything about their
returns.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from apex.domain.security_identity import StableSecurityIdentity
from apex.domain.values import content_digest, deterministic_id

RESEARCH_COHORT_V1 = "GENUINE_MULTI_ASSET_RESEARCH_COHORT_V1"


class ResearchCohortError(ValueError):
    """A cohort policy or resolution invariant was violated."""


class CohortRole(StrEnum):
    """Functional role tags. A member may carry more than one -- e.g. a
    single equity is commonly both ``LARGE_CAP_EQUITY`` and one style tilt."""

    BROAD_INDEX_ETF = "BROAD_INDEX_ETF"
    SECTOR_ETF = "SECTOR_ETF"
    RATES_BOND_ETF = "RATES_BOND_ETF"
    COMMODITY_ETF = "COMMODITY_ETF"
    LARGE_CAP_EQUITY = "LARGE_CAP_EQUITY"
    SMALL_CAP_EQUITY = "SMALL_CAP_EQUITY"
    GROWTH_TILT = "GROWTH_TILT"
    VALUE_TILT = "VALUE_TILT"
    DEFENSIVE_TILT = "DEFENSIVE_TILT"
    CYCLICAL_TILT = "CYCLICAL_TILT"


_ETF_ROLES = frozenset(
    {
        CohortRole.BROAD_INDEX_ETF,
        CohortRole.SECTOR_ETF,
        CohortRole.RATES_BOND_ETF,
        CohortRole.COMMODITY_ETF,
    }
)


class CohortSector(StrEnum):
    """A descriptive GICS-style sector/asset-class label -- explanatory
    grouping only, never an eligibility or performance claim."""

    BROAD_MARKET = "BROAD_MARKET"
    TECHNOLOGY = "TECHNOLOGY"
    FINANCIALS = "FINANCIALS"
    ENERGY = "ENERGY"
    HEALTH_CARE = "HEALTH_CARE"
    INDUSTRIALS = "INDUSTRIALS"
    CONSUMER_DISCRETIONARY = "CONSUMER_DISCRETIONARY"
    CONSUMER_STAPLES = "CONSUMER_STAPLES"
    UTILITIES = "UTILITIES"
    MATERIALS = "MATERIALS"
    REAL_ESTATE = "REAL_ESTATE"
    COMMUNICATION_SERVICES = "COMMUNICATION_SERVICES"
    RATES_AND_BONDS = "RATES_AND_BONDS"
    COMMODITIES = "COMMODITIES"


@dataclass(frozen=True, slots=True)
class CohortSecurityDeclaration:
    symbol: str
    sector: CohortSector
    roles: tuple[CohortRole, ...]
    note: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ResearchCohortError("COHORT_DECLARATION_SYMBOL_INVALID")
        if not self.roles:
            raise ResearchCohortError("COHORT_DECLARATION_REQUIRES_AT_LEAST_ONE_ROLE")
        if len(set(self.roles)) != len(self.roles):
            raise ResearchCohortError("COHORT_DECLARATION_DUPLICATE_ROLE")

    @property
    def is_etf(self) -> bool:
        return bool(_ETF_ROLES.intersection(self.roles))


@dataclass(frozen=True, slots=True)
class ResearchCohortPolicy:
    """A frozen, content-addressed cohort declaration. Two policies built
    from an identical version string, roster, methodology, and rationale
    always produce the identical ``policy_id``."""

    policy_id: str
    version: str
    members: tuple[CohortSecurityDeclaration, ...]
    selection_methodology: str
    rationale: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        version: str,
        members: Sequence[CohortSecurityDeclaration],
        selection_methodology: str,
        rationale: str,
    ) -> ResearchCohortPolicy:
        if not version:
            raise ResearchCohortError("COHORT_POLICY_VERSION_REQUIRED")
        if not selection_methodology or not rationale:
            raise ResearchCohortError("COHORT_POLICY_METHODOLOGY_AND_RATIONALE_REQUIRED")
        symbols = [item.symbol for item in members]
        if len(set(symbols)) != len(symbols):
            raise ResearchCohortError("COHORT_POLICY_DUPLICATE_SYMBOL")
        if not (1 <= len(members) <= 500):
            raise ResearchCohortError("COHORT_POLICY_MEMBER_COUNT_OUT_OF_BOUNDS")
        ordered = tuple(sorted(members, key=lambda item: item.symbol))
        payload = (
            RESEARCH_COHORT_V1,
            version,
            tuple(
                (item.symbol, item.sector.value, tuple(role.value for role in item.roles))
                for item in ordered
            ),
            selection_methodology,
            rationale,
        )
        return cls(
            policy_id=deterministic_id("research_cohort_policy", payload),
            version=version,
            members=ordered,
            selection_methodology=selection_methodology,
            rationale=rationale,
            canonical_hash=content_digest(payload),
        )

    def member_count_with_role(self, role: CohortRole) -> int:
        return sum(1 for item in self.members if role in item.roles)


@dataclass(frozen=True, slots=True)
class ResolvedCohortMember:
    stable_security_id: str
    symbol: str
    sector: CohortSector
    roles: tuple[CohortRole, ...]
    asset_class: str


@dataclass(frozen=True, slots=True)
class ResolvedCohort:
    policy_id: str
    policy_hash: str
    members: tuple[ResolvedCohortMember, ...]
    unresolved_symbols: tuple[str, ...]
    ambiguous_symbols: tuple[str, ...]

    @property
    def fully_resolved(self) -> bool:
        return not self.unresolved_symbols and not self.ambiguous_symbols


def resolve_cohort(
    policy: ResearchCohortPolicy, *, stable_identities: Sequence[StableSecurityIdentity]
) -> ResolvedCohort:
    """Deterministic resolution against retained stable-security identity
    evidence -- never against a live provider call, and never inferring a
    membership Apex has not actually retained.

    An unresolved or ambiguous symbol is reported, not silently dropped:
    the caller decides whether a partially-resolved cohort is usable for
    this run (mirrors ``CreateHistoricalCapturePlan.execute``'s own
    unresolved/ambiguous accounting, just without raising -- this function
    has no repository or plan side effect to guard)."""

    by_symbol: dict[str, list[StableSecurityIdentity]] = {}
    for identity in stable_identities:
        by_symbol.setdefault(identity.current_symbol, []).append(identity)
    resolved: list[ResolvedCohortMember] = []
    unresolved: list[str] = []
    ambiguous: list[str] = []
    for member in policy.members:
        candidates = by_symbol.get(member.symbol, [])
        if not candidates:
            unresolved.append(member.symbol)
            continue
        if len(candidates) > 1:
            ambiguous.append(member.symbol)
            continue
        resolved.append(
            ResolvedCohortMember(
                stable_security_id=candidates[0].stable_security_id,
                symbol=member.symbol,
                sector=member.sector,
                roles=member.roles,
                asset_class="ETF" if member.is_etf else "COMMON_STOCK",
            )
        )
    return ResolvedCohort(
        policy_id=policy.policy_id,
        policy_hash=policy.canonical_hash,
        members=tuple(resolved),
        unresolved_symbols=tuple(sorted(unresolved)),
        ambiguous_symbols=tuple(sorted(ambiguous)),
    )


def _m(
    symbol: str, sector: CohortSector, roles: tuple[CohortRole, ...], note: str
) -> CohortSecurityDeclaration:
    return CohortSecurityDeclaration(symbol=symbol, sector=sector, roles=roles, note=note)


_INDEX = (CohortRole.BROAD_INDEX_ETF,)
_SECTOR = (CohortRole.SECTOR_ETF,)
_BOND = (CohortRole.RATES_BOND_ETF,)
_CMDTY = (CohortRole.COMMODITY_ETF,)
_LARGE = (CohortRole.LARGE_CAP_EQUITY,)
_SMALL = (CohortRole.SMALL_CAP_EQUITY,)


def _large(*extra: CohortRole) -> tuple[CohortRole, ...]:
    return (CohortRole.LARGE_CAP_EQUITY, *extra)


def _small(*extra: CohortRole) -> tuple[CohortRole, ...]:
    return (CohortRole.SMALL_CAP_EQUITY, *extra)


_GROWTH = CohortRole.GROWTH_TILT
_VALUE = CohortRole.VALUE_TILT
_DEFENSIVE = CohortRole.DEFENSIVE_TILT
_CYCLICAL = CohortRole.CYCLICAL_TILT

_S = CohortSector

_DECLARED_MEMBERS: tuple[CohortSecurityDeclaration, ...] = (
    # -- Broad index ETFs (5) -------------------------------------------------
    _m("SPY", _S.BROAD_MARKET, _INDEX, "S&P 500 (SPDR)"),
    _m("QQQ", _S.BROAD_MARKET, _INDEX, "Nasdaq-100 (Invesco)"),
    _m("DIA", _S.BROAD_MARKET, _INDEX, "Dow Jones Industrial Average (SPDR)"),
    _m("IWM", _S.BROAD_MARKET, _INDEX, "Russell 2000 (iShares)"),
    _m("VTI", _S.BROAD_MARKET, _INDEX, "Total US market (Vanguard)"),
    # -- GICS sector SPDRs (11) ------------------------------------------------
    _m("XLK", _S.TECHNOLOGY, _SECTOR, "Technology sector SPDR"),
    _m("XLF", _S.FINANCIALS, _SECTOR, "Financials sector SPDR"),
    _m("XLE", _S.ENERGY, _SECTOR, "Energy sector SPDR"),
    _m("XLV", _S.HEALTH_CARE, _SECTOR, "Health Care sector SPDR"),
    _m("XLI", _S.INDUSTRIALS, _SECTOR, "Industrials sector SPDR"),
    _m("XLY", _S.CONSUMER_DISCRETIONARY, _SECTOR, "Consumer Discretionary sector SPDR"),
    _m("XLP", _S.CONSUMER_STAPLES, _SECTOR, "Consumer Staples sector SPDR"),
    _m("XLU", _S.UTILITIES, _SECTOR, "Utilities sector SPDR"),
    _m("XLB", _S.MATERIALS, _SECTOR, "Materials sector SPDR"),
    _m("XLRE", _S.REAL_ESTATE, _SECTOR, "Real Estate sector SPDR"),
    _m("XLC", _S.COMMUNICATION_SERVICES, _SECTOR, "Communication Services sector SPDR"),
    # -- Rates / bond proxies (6) -----------------------------------------------
    _m("TLT", _S.RATES_AND_BONDS, _BOND, "20+ year Treasury (iShares)"),
    _m("IEF", _S.RATES_AND_BONDS, _BOND, "7-10 year Treasury (iShares)"),
    _m("SHY", _S.RATES_AND_BONDS, _BOND, "1-3 year Treasury (iShares)"),
    _m("AGG", _S.RATES_AND_BONDS, _BOND, "Aggregate US bond market (iShares)"),
    _m("LQD", _S.RATES_AND_BONDS, _BOND, "Investment-grade corporate bond (iShares)"),
    _m("HYG", _S.RATES_AND_BONDS, _BOND, "High-yield corporate bond (iShares)"),
    # -- Commodity / inflation proxies (6) --------------------------------------
    _m("GLD", _S.COMMODITIES, _CMDTY, "Gold (SPDR)"),
    _m("SLV", _S.COMMODITIES, _CMDTY, "Silver (iShares)"),
    _m("USO", _S.COMMODITIES, _CMDTY, "Crude oil (United States Oil Fund)"),
    _m("DBC", _S.COMMODITIES, _CMDTY, "Broad commodity basket (Invesco)"),
    _m("UUP", _S.COMMODITIES, _CMDTY, "US Dollar Index bullish (Invesco)"),
    _m("TIP", _S.COMMODITIES, _CMDTY, "Treasury inflation-protected securities (iShares)"),
    # -- Large-cap technology (10) -----------------------------------------------
    _m("AAPL", _S.TECHNOLOGY, _large(_GROWTH), "Mega-cap technology"),
    _m("MSFT", _S.TECHNOLOGY, _large(_GROWTH), "Mega-cap technology"),
    _m("NVDA", _S.TECHNOLOGY, _large(_GROWTH), "Mega-cap technology"),
    _m("GOOGL", _S.COMMUNICATION_SERVICES, _large(_GROWTH), "Mega-cap internet/media"),
    _m("META", _S.COMMUNICATION_SERVICES, _large(_GROWTH), "Mega-cap internet/media"),
    _m("AVGO", _S.TECHNOLOGY, _large(_GROWTH), "Large-cap semiconductors"),
    _m("ORCL", _S.TECHNOLOGY, _large(), "Large-cap enterprise software"),
    _m("CSCO", _S.TECHNOLOGY, _large(_VALUE), "Large-cap networking hardware"),
    _m("ADBE", _S.TECHNOLOGY, _large(_GROWTH), "Large-cap software"),
    _m("CRM", _S.TECHNOLOGY, _large(_GROWTH), "Large-cap enterprise software"),
    # -- Large-cap consumer discretionary / communication (7) --------------------
    _m("AMZN", _S.CONSUMER_DISCRETIONARY, _large(_GROWTH, _CYCLICAL), "Mega-cap e-commerce"),
    _m("TSLA", _S.CONSUMER_DISCRETIONARY, _large(_GROWTH, _CYCLICAL), "Large-cap automotive"),
    _m("HD", _S.CONSUMER_DISCRETIONARY, _large(_CYCLICAL), "Large-cap home improvement retail"),
    _m("MCD", _S.CONSUMER_DISCRETIONARY, _large(_DEFENSIVE), "Large-cap restaurants"),
    _m("NFLX", _S.COMMUNICATION_SERVICES, _large(_GROWTH), "Large-cap streaming media"),
    _m("DIS", _S.COMMUNICATION_SERVICES, _large(_CYCLICAL), "Large-cap media/entertainment"),
    _m("NKE", _S.CONSUMER_DISCRETIONARY, _large(_CYCLICAL), "Large-cap apparel"),
    # -- Large-cap financials (7) --------------------------------------------------
    _m("JPM", _S.FINANCIALS, _large(_VALUE), "Large-cap money-center bank"),
    _m("BAC", _S.FINANCIALS, _large(_VALUE, _CYCLICAL), "Large-cap money-center bank"),
    _m("WFC", _S.FINANCIALS, _large(_VALUE, _CYCLICAL), "Large-cap money-center bank"),
    _m("GS", _S.FINANCIALS, _large(_CYCLICAL), "Large-cap investment bank"),
    _m("MS", _S.FINANCIALS, _large(_CYCLICAL), "Large-cap investment bank"),
    _m("V", _S.FINANCIALS, _large(_GROWTH), "Large-cap payments network"),
    _m("MA", _S.FINANCIALS, _large(_GROWTH), "Large-cap payments network"),
    # -- Large-cap health care (6) --------------------------------------------------
    _m("UNH", _S.HEALTH_CARE, _large(_DEFENSIVE), "Large-cap managed care"),
    _m("JNJ", _S.HEALTH_CARE, _large(_DEFENSIVE), "Large-cap pharmaceuticals/consumer health"),
    _m("LLY", _S.HEALTH_CARE, _large(_GROWTH), "Large-cap pharmaceuticals"),
    _m("PFE", _S.HEALTH_CARE, _large(_VALUE, _DEFENSIVE), "Large-cap pharmaceuticals"),
    _m("ABBV", _S.HEALTH_CARE, _large(_DEFENSIVE), "Large-cap pharmaceuticals"),
    _m("MRK", _S.HEALTH_CARE, _large(_VALUE, _DEFENSIVE), "Large-cap pharmaceuticals"),
    # -- Large-cap industrials (5) ---------------------------------------------------
    _m("BA", _S.INDUSTRIALS, _large(_CYCLICAL), "Large-cap aerospace"),
    _m("CAT", _S.INDUSTRIALS, _large(_CYCLICAL), "Large-cap heavy machinery"),
    _m("GE", _S.INDUSTRIALS, _large(_CYCLICAL), "Large-cap industrial conglomerate"),
    _m("HON", _S.INDUSTRIALS, _large(_CYCLICAL), "Large-cap industrial conglomerate"),
    _m("UPS", _S.INDUSTRIALS, _large(_CYCLICAL), "Large-cap logistics"),
    # -- Large-cap energy (3) -----------------------------------------------------------
    _m("XOM", _S.ENERGY, _large(_VALUE, _CYCLICAL), "Large-cap integrated oil"),
    _m("CVX", _S.ENERGY, _large(_VALUE, _CYCLICAL), "Large-cap integrated oil"),
    _m("COP", _S.ENERGY, _large(_VALUE, _CYCLICAL), "Large-cap exploration & production"),
    # -- Large-cap consumer staples (5) --------------------------------------------------
    _m("PG", _S.CONSUMER_STAPLES, _large(_DEFENSIVE), "Large-cap household products"),
    _m("KO", _S.CONSUMER_STAPLES, _large(_DEFENSIVE), "Large-cap beverages"),
    _m("PEP", _S.CONSUMER_STAPLES, _large(_DEFENSIVE), "Large-cap beverages/snacks"),
    _m("WMT", _S.CONSUMER_STAPLES, _large(_DEFENSIVE), "Large-cap discount retail"),
    _m("COST", _S.CONSUMER_STAPLES, _large(_DEFENSIVE), "Large-cap warehouse retail"),
    # -- Large-cap utilities (3) --------------------------------------------------------
    _m("NEE", _S.UTILITIES, _large(_DEFENSIVE), "Large-cap electric utility"),
    _m("DUK", _S.UTILITIES, _large(_DEFENSIVE), "Large-cap electric utility"),
    _m("SO", _S.UTILITIES, _large(_DEFENSIVE), "Large-cap electric utility"),
    # -- Large-cap materials (2) --------------------------------------------------------
    _m("LIN", _S.MATERIALS, _large(_CYCLICAL), "Large-cap industrial gases"),
    _m("APD", _S.MATERIALS, _large(_CYCLICAL), "Large-cap industrial gases"),
    # -- Large-cap real estate (2) ------------------------------------------------------
    _m("PLD", _S.REAL_ESTATE, _large(_CYCLICAL), "Large-cap industrial REIT"),
    _m("AMT", _S.REAL_ESTATE, _large(), "Large-cap communications infrastructure REIT"),
    # -- Smaller-cap exposure across sectors (15) ----------------------------------------
    _m("UNM", _S.FINANCIALS, _small(_VALUE), "Mid-cap life/disability insurance"),
    _m("AVT", _S.TECHNOLOGY, _small(_VALUE, _CYCLICAL), "Mid-cap technology distribution"),
    _m("HAS", _S.CONSUMER_DISCRETIONARY, _small(_CYCLICAL), "Mid-cap toys/entertainment"),
    _m("BEN", _S.FINANCIALS, _small(_VALUE), "Mid-cap asset management"),
    _m("CHRW", _S.INDUSTRIALS, _small(_CYCLICAL), "Mid-cap logistics/freight brokerage"),
    _m("XRAY", _S.HEALTH_CARE, _small(_VALUE), "Small-cap dental equipment"),
    _m("RPM", _S.MATERIALS, _small(_DEFENSIVE), "Mid-cap specialty coatings"),
    _m("OMCL", _S.HEALTH_CARE, _small(_GROWTH), "Small-cap health-care technology"),
    _m("DXC", _S.TECHNOLOGY, _small(_VALUE), "Mid-cap IT services"),
    _m("HRB", _S.CONSUMER_DISCRETIONARY, _small(_DEFENSIVE), "Small-cap tax-preparation services"),
    _m("JBLU", _S.INDUSTRIALS, _small(_CYCLICAL), "Small-cap airline"),
    _m("CLF", _S.MATERIALS, _small(_VALUE, _CYCLICAL), "Mid-cap steel"),
    _m("MOS", _S.MATERIALS, _small(_VALUE, _CYCLICAL), "Mid-cap fertilizer/agriculture"),
    _m("FMC", _S.MATERIALS, _small(_VALUE), "Mid-cap agricultural chemicals"),
    _m("AOS", _S.INDUSTRIALS, _small(_CYCLICAL), "Mid-cap water heating/treatment equipment"),
)

GENUINE_MULTI_ASSET_COHORT_POLICY_V1 = ResearchCohortPolicy.create(
    version="GENUINE_MULTI_ASSET_COHORT_V1",
    members=_DECLARED_MEMBERS,
    selection_methodology=(
        "STATIC_PREDECLARED_ROLE_AND_SECTOR_COVERAGE_NOT_PERFORMANCE_BASED_V1: broad "
        "index/sector/rates/commodity exposure uses the standard, publicly documented "
        "eleven-sector GICS SPDR set plus the most commonly cited broad-index, Treasury-"
        "duration, credit, and commodity/currency proxy ETFs; single-name equities are "
        "selected for large, long-listed, broadly held sector coverage (large-cap tranche) "
        "and ordinary, long-listed, smaller sector-spanning names (smaller-cap tranche); no "
        "member's historical price, return, or volatility was consulted before selection."
    ),
    rationale=(
        "Cohort v1 for the Genuine Multi-Asset Historical Evidence Bring-Up milestone: "
        "spans broad indexes, all eleven GICS sectors, rates/bond and commodity/inflation "
        "proxies, large-cap and smaller-cap equities, and growth/value/defensive/cyclical "
        "style tilts, so cross-market research has representative -- not exhaustive -- "
        "coverage of the genuine US equity/ETF market without attempting the full universe."
    ),
)
