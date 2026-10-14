"""Corporate Fundamental, Filing, and Event Intelligence v1: point-in-time
official SEC filing/fact evidence and its bounded derived research features.

This is research intelligence only. It is not stock picking, strategy
generation, or execution. It creates no Strategy, Opportunity, Order,
PAPER trial, or LIVE authority; every record here carries ``authority =
"NONE"``. Nothing in this module interprets a filing as bullish or bearish,
infers sentiment from filing text, or claims a causal relationship between
a fundamental fact and any subsequent price or return.

Target information flow
------------------------
stable security identity -> official filing/fact/event evidence -> point-in-
time corporate feature snapshot -> research dataset / replay / hypothesis
input.

Official source policy (milestone section 1)
----------------------------------------------
Two official, structured SEC EDGAR machine-readable sources back every fact
this module retains:

* ``GET https://data.sec.gov/submissions/CIK##########.json`` -- filing
  history: accession numbers, forms, filing dates, acceptance timestamps,
  report periods, primary documents, and 8-K item codes.
* ``GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` --
  structured XBRL company facts across every retained ``us-gaap``/``dei``
  concept, each entry already carrying its own reporting period, unit,
  accession, form, and filed date.

Both are parsed exclusively by ``apex.adapters.corporate_filings_source``;
this module never scrapes filing HTML and never invents a fact a genuine
SEC response did not report. ``apex.adapters.security_reference`` already
proved the ``www.sec.gov`` company-tickers identity path and its
``validate_sec_user_agent``/rate-limit conventions; the new ``data.sec.gov``
clients reuse that identical identifying-User-Agent policy rather than
inventing a second one.

Point-in-time availability (section 2)
-----------------------------------------
Every retained filing and fact carries *both* its own report/fiscal period
and the evidence Apex actually has of when it became publicly knowable:
SEC's ``acceptanceDateTime`` when the source filing is cross-referenced
(``CorporateFilingEvent.accepted_at``), or -- honestly, with an explicit
limitation flag rather than a silent substitution -- the coarser
``filingDate`` (midnight UTC) when acceptance evidence was not retained for
that accession. ``effective_available_at`` is the single point-in-time
anchor every replay/snapshot function in this module filters on. A fiscal
quarter ending June 30 is therefore never visible to a snapshot built
``as_of`` June 30 unless a filing reporting it was *also* accepted by then --
see ``build_corporate_fundamental_snapshot`` and the anti-lookahead proofs
in ``tests/test_corporate_fundamentals.py``.

Restatement/amendment behavior (section 6)
---------------------------------------------
Nothing here ever overwrites a previously retained fact or filing row --
every table this module's persistence layer defines is append-only. SEC's
``companyfacts`` endpoint itself already retains every historical filing's
report of a concept/period, so a restatement surfaces as a second
``FundamentalFact`` row for the identical ``(concept, fiscal_year,
fiscal_period, period_end)`` with a later ``accession_number`` and a
different ``value``. ``resolve_fact_as_of`` and ``revision_chain`` are pure,
read-time functions over that retained set: replay at time T sees only the
revision whose ``effective_available_at <= T``; later research calling the
identical function with a later ``as_of`` sees the restated value. Nothing
is mutated to make that true.

Bounded scope (sections 3, 4, 11, 16)
-----------------------------------------
* Filing forms retained as first-class ``CorporateFilingEvent`` rows are
  bounded to ``RETAINED_FILING_FORMS`` (10-K, 10-K/A, 10-Q, 10-Q/A, 8-K,
  8-K/A). Registration/prospectus forms (S-1 and similar) are not genuinely
  useful for this cohort of long-listed issuers and are deliberately
  excluded from v1; every other form SEC reports is still counted (never
  silently dropped) in ``CorporateDataQualityReport.forms_found``.
* The structured fundamental fact vocabulary is bounded to
  ``CONCEPT_DEFINITIONS`` (11 concepts) -- not an unbounded XBRL warehouse.
* Filing *text* is never fetched or analyzed; ``CorporateFilingEvent``
  retains exact accession/document lineage so a future, separately reviewed
  milestone can safely attach risk-factor diffs, language-change detection,
  or model-assisted summaries without this module changing at all.
* Earnings-release timing is only ever an 8-K Item 2.02 filing-evidence
  proxy (``derive_earnings_events``); this module never claims that is the
  exact earnings-release timestamp -- see ``EarningsEvent.limitation``.

Non-goals
---------
No opaque composite "quality score" exists anywhere below -- every derived
feature exposes its own required source facts, period comparison, and
methodology string, and every unavailable value carries an explicit reason
rather than a fabricated zero.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from apex.domain.research_cohort import ResolvedCohort
from apex.domain.security_identity import StableSecurityIdentity
from apex.domain.values import content_digest, deterministic_id, parse_decimal

# Used only as an unbounded "as of everything currently retained" cutoff for
# ``assess_issuer_quality`` (a current data-quality summary, not a
# historical point-in-time query) -- never used by any point-in-time
# snapshot/replay function, which always take a genuine caller-supplied
# ``as_of``.
_FAR_FUTURE_AS_OF = datetime(2100, 1, 1, tzinfo=UTC)

CORPORATE_FILING_EVENT_V1 = "CORPORATE_FILING_EVENT_V1"
CORPORATE_FUNDAMENTAL_FACT_V1 = "CORPORATE_FUNDAMENTAL_FACT_V1"
CORPORATE_FUNDAMENTAL_SNAPSHOT_V1 = "CORPORATE_FUNDAMENTAL_SNAPSHOT_V1"
CORPORATE_EVIDENCE_COHORT_PLAN_V1 = "CORPORATE_EVIDENCE_COHORT_PLAN_V1"
CORPORATE_EVIDENCE_CAPTURE_CHECKPOINT_V1 = "CORPORATE_EVIDENCE_CAPTURE_CHECKPOINT_V1"
CORPORATE_HISTORY_DATASET_V1 = "CORPORATE_HISTORY_DATASET_V1"
CORPORATE_DATA_QUALITY_REPORT_V1 = "CORPORATE_DATA_QUALITY_REPORT_V1"
EARNINGS_EVENT_V1 = "EARNINGS_EVENT_V1"
DERIVED_FUNDAMENTAL_FEATURE_V1 = "DERIVED_FUNDAMENTAL_FEATURE_V1"
FILING_EVENT_FEATURE_V1 = "FILING_EVENT_FEATURE_V1"
REPLAY_CORPORATE_EVIDENCE_ATTACHMENT_V1 = "REPLAY_CORPORATE_EVIDENCE_ATTACHMENT_V1"
LOOKBACK_WINDOW_V1 = "CORPORATE_LOOKBACK_WINDOW_V1"
CORPORATE_REPLAY_FEATURE_FRAME_V1 = "CORPORATE_REPLAY_FEATURE_FRAME_V1"
CONCEPT_VOCABULARY_VERSION = "CORPORATE_FUNDAMENTAL_CONCEPT_VOCABULARY_V1"
EARNINGS_TIMING_LIMITATION = "FILING_ACCEPTANCE_TIME_USED_AS_EARNINGS_PROXY_NOT_EXACT_RELEASE_TIME"
DERIVED_Q4_METHODOLOGY = (
    "DISCRETE_Q4_V1: fourth-quarter discrete value = full-fiscal-year duration fact minus the "
    "sum of the three preceding discrete quarterly duration facts, computed only when all four "
    "source facts share the identical concept, fiscal year, and unit and the three quarters'"
    " combined period exactly covers the year up to the FY period end. Source fact ids are"
    " retained on the derived value; nothing is subtracted without them."
)
RESEARCH_ONLY_WARNING = "CORPORATE FUNDAMENTAL EVIDENCE — RESEARCH ONLY, NOT A TRADING SIGNAL"

_ACCESSION_PATTERN = re.compile(r"\A\d{10}-\d{2}-\d{6}\Z")
_CIK_PATTERN = re.compile(r"\A\d{1,10}\Z")


class CorporateFundamentalsError(ValueError):
    """A corporate fundamental evidence invariant was violated."""


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CorporateFundamentalsError(code)
    return value


def _text(value: str, code: str, *, maximum: int = 2_000) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CorporateFundamentalsError(code)
    return normalized


def _cik(value: str, code: str) -> str:
    if _CIK_PATTERN.fullmatch(value) is None:
        raise CorporateFundamentalsError(code)
    return value


def _accession(value: str, code: str) -> str:
    if _ACCESSION_PATTERN.fullmatch(value) is None:
        raise CorporateFundamentalsError(code)
    return value


def _bounded_ratio(value: Decimal) -> Decimal:
    """Quantizes a computed ratio/growth Decimal to the system's own
    ``MAX_SCALE`` (8 fractional digits) -- identical pattern to
    ``domain.historical_replay._bounded_decimal``/``domain.securities.
    _bounded_decimal``. Division (unlike the ``parse_decimal``-validated
    inputs it operates on) can otherwise yield up to 28 significant digits,
    which ``canonical_json_bytes``/``content_digest`` would then reject --
    found via genuine SEC capture acceptance, not a theoretical concern."""
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def sec_cik_10digit(cik: str) -> str:
    """Zero-pad Apex's plain-numeric CIK representation (matching
    ``StableSecurityIdentity.cik``, itself sourced from SEC's unpadded
    ``cik_str``) to SEC's required 10-digit path segment. A pure formatting
    function used only at the HTTP-request boundary -- every domain-layer
    field keeps the unpadded form so it stays joinable with existing
    identity evidence without a second, divergent CIK representation."""
    if _CIK_PATTERN.fullmatch(cik) is None:
        raise CorporateFundamentalsError("CIK_FORMAT_INVALID")
    return cik.rjust(10, "0")


def corporate_fundamentals_warnings() -> tuple[str, ...]:
    return (
        RESEARCH_ONLY_WARNING,
        "FILING_ACCEPTANCE_TIME_USED_FOR_POINT_IN_TIME_VISIBILITY_FILING_DATE_ONLY_WHEN_ABSENT",
        EARNINGS_TIMING_LIMITATION,
        "BOUNDED_CONCEPT_VOCABULARY_NOT_EVERY_XBRL_FACT_IS_RETAINED",
        "DISCRETE_Q4_VALUES_ARE_DERIVED_SEE_DERIVED_Q4_METHODOLOGY",
        "NO_CAUSAL_OR_SENTIMENT_CLAIM_IS_MADE_FROM_ANY_FILING_OR_FACT",
    )


# ---------------------------------------------------------------------------
# Filing event model (section 3)
# ---------------------------------------------------------------------------


class FilingForm(StrEnum):
    FORM_10K = "10-K"
    FORM_10K_A = "10-K/A"
    FORM_10Q = "10-Q"
    FORM_10Q_A = "10-Q/A"
    FORM_8K = "8-K"
    FORM_8K_A = "8-K/A"


RETAINED_FILING_FORMS = frozenset(item.value for item in FilingForm)
_CORE_ANNUAL_QUARTERLY_FORMS = frozenset(
    {FilingForm.FORM_10K, FilingForm.FORM_10K_A, FilingForm.FORM_10Q, FilingForm.FORM_10Q_A}
)


class AmendmentState(StrEnum):
    ORIGINAL = "ORIGINAL"
    AMENDMENT = "AMENDMENT"


def amendment_state_for_form(form: FilingForm) -> AmendmentState:
    return AmendmentState.AMENDMENT if form.value.endswith("/A") else AmendmentState.ORIGINAL


def base_form_for(form: FilingForm) -> FilingForm:
    """The un-amended counterpart of an amendment form -- ``10-K/A`` ->
    ``10-K`` -- used to find what an amendment plausibly amends among a
    issuer's other retained filings."""
    if form.value.endswith("/A"):
        return FilingForm(form.value.removesuffix("/A"))
    return form


@dataclass(frozen=True, slots=True)
class CorporateFilingEvent:
    """One retained SEC filing event for one issuer.

    ``accepted_at`` is SEC's ``acceptanceDateTime`` when the source
    ``submissions`` response carried it for this accession; when it did not
    (an older filing outside the retained window, or an accession only ever
    observed indirectly through ``companyfacts``), it is ``None`` and
    ``effective_available_at`` honestly falls back to the coarser
    ``filed_at`` with ``"ACCEPTANCE_TIME_UNAVAILABLE_USED_FILING_DATE"``
    recorded in ``limitations`` -- never silently treated as exact.
    """

    filing_event_id: str
    stable_security_id: str
    cik: str
    accession_number: str
    form: FilingForm
    amendment_state: AmendmentState
    report_period: str | None
    filed_at: datetime
    accepted_at: datetime | None
    primary_document: str
    source_url: str
    file_number: str | None
    items: tuple[str, ...]
    is_xbrl: bool
    is_inline_xbrl: bool
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @property
    def effective_available_at(self) -> datetime:
        return self.accepted_at if self.accepted_at is not None else self.filed_at

    @classmethod
    def create(
        cls,
        *,
        stable_security_id: str,
        cik: str,
        accession_number: str,
        form: FilingForm | str,
        report_period: str | None,
        filed_at: datetime,
        accepted_at: datetime | None,
        primary_document: str,
        file_number: str | None,
        items: tuple[str, ...] = (),
        is_xbrl: bool = False,
        is_inline_xbrl: bool = False,
    ) -> CorporateFilingEvent:
        if not stable_security_id:
            raise CorporateFundamentalsError("FILING_STABLE_SECURITY_ID_REQUIRED")
        cik_value = _cik(cik, "FILING_CIK_INVALID")
        accession = _accession(accession_number, "FILING_ACCESSION_INVALID")
        form_value = FilingForm(form)
        filed = _aware(filed_at, "FILING_FILED_AT_INVALID")
        accepted = _aware(accepted_at, "FILING_ACCEPTED_AT_INVALID") if accepted_at else None
        if accepted is not None and accepted < filed - timedelta(days=1):
            # A genuine sanity bound (section 19): acceptance meaningfully
            # before the filing date it is paired with is not a real SEC
            # relationship worth silently accepting.
            raise CorporateFundamentalsError("FILING_ACCEPTED_BEFORE_FILED")
        period = None
        if report_period:
            period = _text(report_period, "FILING_REPORT_PERIOD_INVALID", maximum=10)
        document = _text(primary_document, "FILING_PRIMARY_DOCUMENT_INVALID", maximum=300)
        accession_no_dashes = accession.replace("-", "")
        source_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik_value)}/"
            f"{accession_no_dashes}/{document}"
        )
        normalized_items = tuple(
            sorted({_text(item, "FILING_ITEM_INVALID", maximum=20) for item in items})
        )
        if normalized_items and form_value not in {FilingForm.FORM_8K, FilingForm.FORM_8K_A}:
            raise CorporateFundamentalsError("FILING_ITEMS_ONLY_VALID_FOR_8K")
        limitations = (
            () if accepted is not None else ("ACCEPTANCE_TIME_UNAVAILABLE_USED_FILING_DATE",)
        )
        payload = (
            CORPORATE_FILING_EVENT_V1,
            stable_security_id,
            cik_value,
            accession,
            form_value,
            period,
            filed,
            accepted,
            document,
            file_number,
            normalized_items,
            is_xbrl,
            is_inline_xbrl,
        )
        filing_event_id = deterministic_id("corporate_filing_event", payload)
        amendment = amendment_state_for_form(form_value)
        return cls(
            filing_event_id=filing_event_id,
            stable_security_id=stable_security_id,
            cik=cik_value,
            accession_number=accession,
            form=form_value,
            amendment_state=amendment,
            report_period=period,
            filed_at=filed,
            accepted_at=accepted,
            primary_document=document,
            source_url=source_url,
            file_number=file_number,
            items=normalized_items,
            is_xbrl=is_xbrl,
            is_inline_xbrl=is_inline_xbrl,
            limitations=limitations,
            canonical_hash=content_digest(payload),
        )


def visible_filings_as_of(
    filings: Sequence[CorporateFilingEvent], *, as_of: datetime
) -> tuple[CorporateFilingEvent, ...]:
    """Anti-lookahead filter (section 14): only filings this evidence proves
    were publicly available by ``as_of``."""
    _aware(as_of, "FILINGS_AS_OF_INVALID")
    return tuple(
        sorted(
            (item for item in filings if item.effective_available_at <= as_of),
            key=lambda item: (item.effective_available_at, item.accession_number),
        )
    )


# ---------------------------------------------------------------------------
# Earnings/event seam (section 10) -- deliberately distinct from filing events
# ---------------------------------------------------------------------------

EARNINGS_RESULTS_ITEM_CODE = "2.02"


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    """A derived, explicitly-limited earnings-related-event proxy.

    Investigated per milestone section 10: Apex's only retained filing
    evidence (SEC ``submissions``) gives no separate, precise earnings-
    announcement timestamp -- only the 8-K's own filed/accepted evidence.
    Item 2.02 ("Results of Operations and Financial Condition") is SEC's
    own documented convention for an earnings-release 8-K, so its presence
    is retained as a real, sourced event -- but ``limitation`` is always
    populated and this is never presented as an exact release time.
    """

    earnings_event_id: str
    stable_security_id: str
    source_filing_event_id: str
    accession_number: str
    item_code: str
    filed_at: datetime
    accepted_at: datetime | None
    limitation: str
    canonical_hash: str
    authority: str = "NONE"

    @property
    def effective_available_at(self) -> datetime:
        return self.accepted_at if self.accepted_at is not None else self.filed_at


def derive_earnings_events(
    filings: Sequence[CorporateFilingEvent],
) -> tuple[EarningsEvent, ...]:
    """Pure, recomputable-on-demand derivation -- never a separately
    persisted table (mirrors this codebase's existing convention of not
    storing a report exactly rebuildable from already-retained evidence)."""
    events: list[EarningsEvent] = []
    for filing in filings:
        if filing.form not in {FilingForm.FORM_8K, FilingForm.FORM_8K_A}:
            continue
        if EARNINGS_RESULTS_ITEM_CODE not in filing.items:
            continue
        payload = (
            EARNINGS_EVENT_V1,
            filing.stable_security_id,
            filing.filing_event_id,
            filing.accession_number,
            EARNINGS_RESULTS_ITEM_CODE,
            filing.filed_at,
            filing.accepted_at,
        )
        events.append(
            EarningsEvent(
                earnings_event_id=deterministic_id("corporate_earnings_event", payload),
                stable_security_id=filing.stable_security_id,
                source_filing_event_id=filing.filing_event_id,
                accession_number=filing.accession_number,
                item_code=EARNINGS_RESULTS_ITEM_CODE,
                filed_at=filing.filed_at,
                accepted_at=filing.accepted_at,
                limitation=EARNINGS_TIMING_LIMITATION,
                canonical_hash=content_digest(payload),
            )
        )
    return tuple(sorted(events, key=lambda item: item.effective_available_at))


# ---------------------------------------------------------------------------
# Structured fundamental facts: bounded vocabulary, units, period semantics
# (sections 4, 5)
# ---------------------------------------------------------------------------


class FundamentalConcept(StrEnum):
    REVENUE = "REVENUE"
    NET_INCOME = "NET_INCOME"
    OPERATING_INCOME = "OPERATING_INCOME"
    EPS_DILUTED = "EPS_DILUTED"
    TOTAL_ASSETS = "TOTAL_ASSETS"
    TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
    STOCKHOLDERS_EQUITY = "STOCKHOLDERS_EQUITY"
    CASH_AND_EQUIVALENTS = "CASH_AND_EQUIVALENTS"
    OPERATING_CASH_FLOW = "OPERATING_CASH_FLOW"
    CAPITAL_EXPENDITURES = "CAPITAL_EXPENDITURES"
    SHARES_OUTSTANDING = "SHARES_OUTSTANDING"


class FactUnit(StrEnum):
    USD = "USD"
    USD_PER_SHARE = "USD_PER_SHARE"
    SHARES = "SHARES"


class PeriodType(StrEnum):
    INSTANT = "INSTANT"
    DURATION = "DURATION"


class FiscalPeriod(StrEnum):
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"
    FY = "FY"


@dataclass(frozen=True, slots=True)
class ConceptDefinition:
    """One bounded canonical concept and the exact, priority-ordered
    ``(taxonomy, tag)`` pairs it may be sourced from. Every retained fact
    keeps its own matched ``source_taxonomy``/``source_tag`` (never just
    the canonical concept), satisfying "retain source taxonomy/concept
    identity" even though several raw tags can map to one canonical
    concept (real issuers genuinely use different tags across years, most
    commonly around the 2018 ASC 606 revenue-recognition transition)."""

    concept: FundamentalConcept
    unit: FactUnit
    period_type: PeriodType
    source_tags: tuple[tuple[str, str], ...]


CONCEPT_DEFINITIONS: tuple[ConceptDefinition, ...] = (
    ConceptDefinition(
        FundamentalConcept.REVENUE,
        FactUnit.USD,
        PeriodType.DURATION,
        (
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "Revenues"),
            ("us-gaap", "SalesRevenueNet"),
        ),
    ),
    ConceptDefinition(
        FundamentalConcept.NET_INCOME,
        FactUnit.USD,
        PeriodType.DURATION,
        (("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")),
    ),
    ConceptDefinition(
        FundamentalConcept.OPERATING_INCOME,
        FactUnit.USD,
        PeriodType.DURATION,
        (("us-gaap", "OperatingIncomeLoss"),),
    ),
    ConceptDefinition(
        FundamentalConcept.EPS_DILUTED,
        FactUnit.USD_PER_SHARE,
        PeriodType.DURATION,
        (("us-gaap", "EarningsPerShareDiluted"),),
    ),
    ConceptDefinition(
        FundamentalConcept.TOTAL_ASSETS,
        FactUnit.USD,
        PeriodType.INSTANT,
        (("us-gaap", "Assets"),),
    ),
    ConceptDefinition(
        FundamentalConcept.TOTAL_LIABILITIES,
        FactUnit.USD,
        PeriodType.INSTANT,
        (("us-gaap", "Liabilities"),),
    ),
    ConceptDefinition(
        FundamentalConcept.STOCKHOLDERS_EQUITY,
        FactUnit.USD,
        PeriodType.INSTANT,
        (
            ("us-gaap", "StockholdersEquity"),
            ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
        ),
    ),
    ConceptDefinition(
        FundamentalConcept.CASH_AND_EQUIVALENTS,
        FactUnit.USD,
        PeriodType.INSTANT,
        (
            ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
            (
                "us-gaap",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            ),
        ),
    ),
    ConceptDefinition(
        FundamentalConcept.OPERATING_CASH_FLOW,
        FactUnit.USD,
        PeriodType.DURATION,
        (
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ),
    ),
    ConceptDefinition(
        FundamentalConcept.CAPITAL_EXPENDITURES,
        FactUnit.USD,
        PeriodType.DURATION,
        (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),),
    ),
    ConceptDefinition(
        FundamentalConcept.SHARES_OUTSTANDING,
        FactUnit.SHARES,
        PeriodType.INSTANT,
        (
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
        ),
    ),
)
_DEFINITION_BY_CONCEPT: dict[FundamentalConcept, ConceptDefinition] = {
    item.concept: item for item in CONCEPT_DEFINITIONS
}
SEC_UNIT_KEY_BY_FACT_UNIT: Mapping[FactUnit, str] = {
    FactUnit.USD: "USD",
    FactUnit.USD_PER_SHARE: "USD/shares",
    FactUnit.SHARES: "shares",
}
_FISCAL_PERIOD_BY_SEC_FP: Mapping[str, FiscalPeriod] = {
    "Q1": FiscalPeriod.Q1,
    "Q2": FiscalPeriod.Q2,
    "Q3": FiscalPeriod.Q3,
    "Q4": FiscalPeriod.Q4,
    "FY": FiscalPeriod.FY,
}


def concept_definition(concept: FundamentalConcept) -> ConceptDefinition:
    return _DEFINITION_BY_CONCEPT[concept]


def fiscal_period_for_sec_fp(value: str) -> FiscalPeriod | None:
    """``None`` (never a guess) for any of SEC's less-common ``fp`` values
    (e.g. ``H1``, ``CY``) this bounded vocabulary does not map."""
    return _FISCAL_PERIOD_BY_SEC_FP.get(value)


@dataclass(frozen=True, slots=True)
class FundamentalFact:
    """One immutable, exactly-as-reported structured fundamental fact.

    Identity is content-addressed over every field including
    ``accession_number``, so a restated later filing of the identical
    concept/period produces a genuinely new, separately retained row rather
    than colliding with or overwriting the original -- see the module
    docstring's "Restatement/amendment behavior" section.
    """

    fact_id: str
    stable_security_id: str
    cik: str
    concept: FundamentalConcept
    source_taxonomy: str
    source_tag: str
    unit: FactUnit
    period_type: PeriodType
    fiscal_year: int
    fiscal_period: FiscalPeriod
    period_start: str | None
    period_end: str
    value: Decimal
    accession_number: str
    form: str
    filed_at: datetime
    accepted_at: datetime | None
    frame: str | None
    is_ytd_derived: bool
    derivation_note: str | None
    canonical_hash: str
    authority: str = "NONE"

    @property
    def effective_available_at(self) -> datetime:
        return self.accepted_at if self.accepted_at is not None else self.filed_at

    @classmethod
    def create(
        cls,
        *,
        stable_security_id: str,
        cik: str,
        concept: FundamentalConcept | str,
        source_taxonomy: str,
        source_tag: str,
        fiscal_year: int,
        fiscal_period: FiscalPeriod | str,
        period_start: str | None,
        period_end: str,
        value: Decimal,
        accession_number: str,
        form: str,
        filed_at: datetime,
        accepted_at: datetime | None,
        frame: str | None = None,
        is_ytd_derived: bool = False,
        derivation_note: str | None = None,
    ) -> FundamentalFact:
        if not stable_security_id:
            raise CorporateFundamentalsError("FACT_STABLE_SECURITY_ID_REQUIRED")
        cik_value = _cik(cik, "FACT_CIK_INVALID")
        concept_value = FundamentalConcept(concept)
        definition = concept_definition(concept_value)
        taxonomy = _text(source_taxonomy, "FACT_TAXONOMY_INVALID", maximum=20)
        tag = _text(source_tag, "FACT_TAG_INVALID", maximum=160)
        if (taxonomy, tag) not in definition.source_tags:
            raise CorporateFundamentalsError("FACT_SOURCE_TAG_NOT_IN_BOUNDED_VOCABULARY")
        fiscal_period_value = FiscalPeriod(fiscal_period)
        if not 1993 <= fiscal_year <= 2100:
            raise CorporateFundamentalsError("FACT_FISCAL_YEAR_OUT_OF_BOUNDS")
        end = _text(period_end, "FACT_PERIOD_END_INVALID", maximum=10)
        start = (
            _text(period_start, "FACT_PERIOD_START_INVALID", maximum=10) if period_start else None
        )
        if definition.period_type is PeriodType.INSTANT and start is not None:
            raise CorporateFundamentalsError("FACT_INSTANT_CONCEPT_MUST_NOT_HAVE_PERIOD_START")
        if definition.period_type is PeriodType.DURATION and start is None:
            raise CorporateFundamentalsError("FACT_DURATION_CONCEPT_REQUIRES_PERIOD_START")
        if start is not None and start > end:
            raise CorporateFundamentalsError("FACT_PERIOD_START_AFTER_END")
        parsed_value = parse_decimal(value, allow_negative=True)
        accession = _accession(accession_number, "FACT_ACCESSION_INVALID")
        form_value = _text(form, "FACT_FORM_INVALID", maximum=20)
        if form_value not in RETAINED_FILING_FORMS:
            raise CorporateFundamentalsError("FACT_FORM_NOT_IN_RETAINED_FORMS")
        filed = _aware(filed_at, "FACT_FILED_AT_INVALID")
        accepted = _aware(accepted_at, "FACT_ACCEPTED_AT_INVALID") if accepted_at else None
        if accepted is not None and accepted < filed - timedelta(days=1):
            # Mirrors CorporateFilingEvent.create's identical sanity bound
            # (section 19): genuine SEC evidence has shown a fact's own
            # "filed" date and its accession's cross-referenced acceptance
            # time can disagree by more than a day between SEC's
            # submissions and XBRL-frames endpoints. A caller should fall
            # back to filed-only evidence (``accepted_at=None``) rather
            # than pass a self-contradictory pair here -- see
            # ``corporate_filings_source.parse_sec_companyfacts``.
            raise CorporateFundamentalsError("FACT_ACCEPTED_BEFORE_FILED")
        note = (
            _text(derivation_note, "FACT_DERIVATION_NOTE_INVALID", maximum=1_000)
            if derivation_note
            else None
        )
        if is_ytd_derived and note is None:
            raise CorporateFundamentalsError("FACT_DERIVED_REQUIRES_DERIVATION_NOTE")
        frame_value = _text(frame, "FACT_FRAME_INVALID", maximum=40) if frame else None
        payload = (
            CORPORATE_FUNDAMENTAL_FACT_V1,
            stable_security_id,
            cik_value,
            concept_value,
            taxonomy,
            tag,
            definition.unit,
            definition.period_type,
            fiscal_year,
            fiscal_period_value,
            start,
            end,
            parsed_value,
            accession,
            form_value,
            filed,
            accepted,
            frame_value,
            is_ytd_derived,
            note,
        )
        return cls(
            fact_id=deterministic_id("corporate_fundamental_fact", payload),
            stable_security_id=stable_security_id,
            cik=cik_value,
            concept=concept_value,
            source_taxonomy=taxonomy,
            source_tag=tag,
            unit=definition.unit,
            period_type=definition.period_type,
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period_value,
            period_start=start,
            period_end=end,
            value=parsed_value,
            accession_number=accession,
            form=form_value,
            filed_at=filed,
            accepted_at=accepted,
            frame=frame_value,
            is_ytd_derived=is_ytd_derived,
            derivation_note=note,
            canonical_hash=content_digest(payload),
        )


def visible_facts_as_of(
    facts: Sequence[FundamentalFact], *, as_of: datetime
) -> tuple[FundamentalFact, ...]:
    """Anti-lookahead filter: only facts whose ``effective_available_at`` is
    no later than ``as_of``. Nothing else in this module may read a fact
    without going through this (or an equivalent inline filter)."""
    _aware(as_of, "FACTS_AS_OF_INVALID")
    return tuple(item for item in facts if item.effective_available_at <= as_of)


def facts_for_period(
    facts: Sequence[FundamentalFact],
    *,
    concept: FundamentalConcept,
    fiscal_year: int,
    fiscal_period: FiscalPeriod,
) -> tuple[FundamentalFact, ...]:
    return tuple(
        item
        for item in facts
        if item.concept is concept
        and item.fiscal_year == fiscal_year
        and item.fiscal_period is fiscal_period
    )


def _fact_precedence_key(fact: FundamentalFact) -> tuple[datetime, int, int, str, str]:
    """Corporate Research Panel Determinism v1: the single canonical,
    total-order precedence key every competing-fact selection in this
    module resolves through (``_choose_fact``, ``latest_fact``, and
    ``revision_chain`` alike) -- fixed once at this lowest layer so every
    derived feature and every research panel built on top inherits it
    automatically, rather than each caller re-implementing its own
    tie-break. Every component is a retained, content-derived field --
    never database row order, Python set/dict iteration order, or object
    identity -- so ``max()``/``sorted()`` against this key return the
    identical result regardless of ``candidates``' own input order.

    Discovered via genuine regression evidence, not assumed: repeated
    real-data queries proved 31,433 ``(security, concept, period_end)``
    groups among retained facts share an identical ``(effective_
    available_at, accession_number)`` pair across two or more distinct
    facts -- the previous key -- and that the previous ``max()`` over such
    a group silently returned whichever candidate happened to come first
    in caller-supplied order (itself downstream of an incompletely
    ordered SQL fetch). Most to least significant below:

    1. ``effective_available_at`` -- point-in-time truth (section 6): the
       latest publicly visible revision always wins; an older restated
       value is never preferred once a newer one is visible.
    2. Canonical concept/tag priority -- ``ConceptDefinition.source_tags``
       is itself already declared priority-ordered (see its own
       docstring). A fact tagged with an earlier-listed, higher-priority
       ``(taxonomy, tag)`` pair wins a genuine same-timestamp tie -- the
       observed real-data case of one accession tagging the identical
       canonical concept under two compatible XBRL elements (e.g.
       ``NetIncomeLoss`` vs ``ProfitLoss``, ``Revenues`` vs
       ``SalesRevenueNet``). Negated below so a lower (better) rank sorts
       as a larger key.
    3. Discrete-period preference -- among facts that still tie after (1)
       and (2) (identical tag, identical accession, identical
       availability), SEC XBRL commonly tags *both* a discrete quarter and
       a cumulative year-to-date duration under the identical
       concept/tag/fiscal-period/accession (also observed directly in
       retained evidence); every YoY/margin comparison this module
       computes assumes the discrete window, so the shorter covered span
       wins. Instant facts have no ``period_start`` and are unaffected
       (span is always 0 on both sides). Negated below so a shorter
       (better) span sorts as a larger key.
    4. ``accession_number`` -- SEC's own deterministic accession identity,
       lexicographically, as a tie-break with genuine real-world meaning.
    5. ``fact_id`` -- this module's own content hash. Unreachable in
       practice (it would require two facts identical in tag, accession,
       period, and value) but guarantees a true total order with zero
       remaining ties under any circumstance.
    """
    definition = concept_definition(fact.concept)
    tag_rank = definition.source_tags.index((fact.source_taxonomy, fact.source_tag))
    span_days = (
        (date.fromisoformat(fact.period_end) - date.fromisoformat(fact.period_start)).days
        if fact.period_start is not None
        else 0
    )
    return (fact.effective_available_at, -tag_rank, -span_days, fact.accession_number, fact.fact_id)


def _choose_fact(candidates: Sequence[FundamentalFact]) -> FundamentalFact | None:
    """Among candidates already known to be visible as of some cutoff: the
    most recently *reported period*, and within that period, the single
    fact ``_fact_precedence_key`` ranks highest -- never a value from a
    period earlier than what is genuinely visible, never an older
    restated value once a newer one has become visible, and -- because
    ``_fact_precedence_key`` is a true total order -- never dependent on
    ``candidates``' own iteration order."""
    if not candidates:
        return None
    best_period_end = max(item.period_end for item in candidates)
    same_period = [item for item in candidates if item.period_end == best_period_end]
    return max(same_period, key=_fact_precedence_key)


def resolve_fact_as_of(
    facts: Sequence[FundamentalFact], *, as_of: datetime
) -> FundamentalFact | None:
    """The single fact value point-in-time replay at ``as_of`` would see
    among ``facts`` (typically pre-filtered to one concept/period): the
    latest revision whose evidence was visible by then, or ``None`` if
    nothing was visible yet."""
    return _choose_fact(visible_facts_as_of(facts, as_of=as_of))


def latest_fact(facts: Sequence[FundamentalFact]) -> FundamentalFact | None:
    """The current (unbounded ``as_of``) latest revision -- "later research
    may see the restated value" (section 6). Uses the identical total-
    order ``_fact_precedence_key`` as ``_choose_fact``."""
    if not facts:
        return None
    return max(facts, key=_fact_precedence_key)


@dataclass(frozen=True, slots=True)
class FactRevision:
    """One entry in a purely derived, read-time revision chain -- never a
    stored mutation. ``supersedes_fact_id`` is the chronologically
    preceding candidate, inferred from retained evidence alone."""

    fact_id: str
    accession_number: str
    effective_available_at: datetime
    value: Decimal
    is_latest: bool
    supersedes_fact_id: str | None


def revision_chain(facts: Sequence[FundamentalFact]) -> tuple[FactRevision, ...]:
    """Explains, for one concept/period's full retained history, exactly
    which filing superseded which -- "replacement/revision relationship
    where inferable" (section 6). Facts spanning more than one concept or
    period are still accepted but chained purely chronologically (ties at
    the identical ``effective_available_at`` broken by the same
    deterministic ``_fact_precedence_key`` ``_choose_fact`` uses); callers
    normally pre-filter with ``facts_for_period``."""
    ordered = sorted(facts, key=_fact_precedence_key)
    chain: list[FactRevision] = []
    for index, fact in enumerate(ordered):
        chain.append(
            FactRevision(
                fact_id=fact.fact_id,
                accession_number=fact.accession_number,
                effective_available_at=fact.effective_available_at,
                value=fact.value,
                is_latest=index == len(ordered) - 1,
                supersedes_fact_id=ordered[index - 1].fact_id if index > 0 else None,
            )
        )
    return tuple(chain)


def count_ambiguous_fact_groups(facts: Sequence[FundamentalFact]) -> int:
    """Operator diagnostic (Corporate Research Panel Determinism v1,
    section 15): counts how many ``(stable_security_id, concept,
    period_end)`` groups among ``facts`` contain two or more distinct
    facts sharing the identical ``effective_available_at`` -- the exact
    shape ``_fact_precedence_key`` must tie-break beyond point-in-time
    availability (multiple compatible XBRL tags, or a discrete-quarter
    value alongside a year-to-date value, reported under one accession).
    A positive count is expected and healthy -- it means genuine
    ambiguity exists in the source evidence and was resolved
    deterministically, not that anything is wrong; this exists so an
    operator can see the deterministic-selection load a given retained
    dataset carries, not to drive any decision by itself."""
    seen_by_group: dict[tuple[str, FundamentalConcept, str], set[datetime]] = {}
    ambiguous_groups: set[tuple[str, FundamentalConcept, str]] = set()
    for fact in facts:
        key = (fact.stable_security_id, fact.concept, fact.period_end)
        seen = seen_by_group.setdefault(key, set())
        if fact.effective_available_at in seen:
            ambiguous_groups.add(key)
        seen.add(fact.effective_available_at)
    return len(ambiguous_groups)


def derive_q4_discrete_fact(
    *,
    fiscal_year_fact: FundamentalFact,
    q1_fact: FundamentalFact,
    q2_fact: FundamentalFact,
    q3_fact: FundamentalFact,
) -> FundamentalFact | None:
    """Discrete Q4 = FY - (Q1+Q2+Q3), with exact source-fact provenance
    (section 5). Returns ``None`` -- never a guess -- unless every input
    genuinely lines up: identical concept/unit/taxonomy family, fiscal
    year, ``FY``/``Q1``/``Q2``/``Q3`` periods respectively, duration type,
    and Q3's period end immediately precedes the day before the FY period
    end is impossible to verify from period bounds alone, so this checks
    what retained evidence *can* prove: matching concept/fiscal_year/unit
    and the expected fiscal_period on each input.
    """
    inputs = (fiscal_year_fact, q1_fact, q2_fact, q3_fact)
    expected_periods = (FiscalPeriod.FY, FiscalPeriod.Q1, FiscalPeriod.Q2, FiscalPeriod.Q3)
    if any(item.period_type is not PeriodType.DURATION for item in inputs):
        return None
    if len({item.concept for item in inputs}) != 1 or len({item.unit for item in inputs}) != 1:
        return None
    if len({item.fiscal_year for item in inputs}) != 1:
        return None
    if tuple(item.fiscal_period for item in inputs) != expected_periods:
        return None
    fy, q1, q2, q3 = inputs
    derived_value = fy.value - (q1.value + q2.value + q3.value)
    source_ids = ",".join(item.fact_id for item in inputs)
    payload = (
        CORPORATE_FUNDAMENTAL_FACT_V1,
        "DERIVED_Q4",
        fy.stable_security_id,
        fy.cik,
        fy.concept,
        fy.unit,
        fy.fiscal_year,
        q3.period_end,
        fy.period_end,
        derived_value,
        source_ids,
    )
    return FundamentalFact(
        fact_id=deterministic_id("corporate_fundamental_fact_derived", payload),
        stable_security_id=fy.stable_security_id,
        cik=fy.cik,
        concept=fy.concept,
        source_taxonomy=fy.source_taxonomy,
        source_tag=fy.source_tag,
        unit=fy.unit,
        period_type=PeriodType.DURATION,
        fiscal_year=fy.fiscal_year,
        fiscal_period=FiscalPeriod.Q4,
        period_start=q3.period_end,
        period_end=fy.period_end,
        value=derived_value,
        accession_number=fy.accession_number,
        form=fy.form,
        filed_at=fy.filed_at,
        accepted_at=fy.accepted_at,
        frame=None,
        is_ytd_derived=True,
        derivation_note=f"{DERIVED_Q4_METHODOLOGY} source_fact_ids={source_ids}",
        canonical_hash=content_digest(payload),
    )


# ---------------------------------------------------------------------------
# Historical sanity checks / anomaly flagging (section 19) -- flag, never
# silently correct source data.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FactAnomaly:
    fact_id: str
    concept: FundamentalConcept
    reason: str


def detect_fact_anomalies(facts: Sequence[FundamentalFact]) -> tuple[FactAnomaly, ...]:
    anomalies: list[FactAnomaly] = []
    for item in facts:
        if (
            item.concept
            in {
                FundamentalConcept.REVENUE,
                FundamentalConcept.TOTAL_ASSETS,
                FundamentalConcept.SHARES_OUTSTANDING,
            }
            and item.value < 0
        ):
            anomalies.append(FactAnomaly(item.fact_id, item.concept, "NEGATIVE_VALUE_UNEXPECTED"))
    by_period: dict[tuple[str, FundamentalConcept, int, FiscalPeriod], list[FundamentalFact]] = {}
    for item in facts:
        by_period.setdefault(
            (item.stable_security_id, item.concept, item.fiscal_year, item.fiscal_period), []
        ).append(item)
    for group in by_period.values():
        if len(group) < 2:
            continue
        distinct_values = {item.value for item in group}
        if len(distinct_values) > 1:
            latest = latest_fact(group)
            if latest is not None:
                anomalies.append(
                    FactAnomaly(
                        latest.fact_id,
                        latest.concept,
                        "RESTATEMENT_VALUE_DIFFERS_FROM_PRIOR_FILING",
                    )
                )
    return tuple(anomalies)


def assert_filing_dates_ordered(filings: Sequence[CorporateFilingEvent]) -> tuple[str, ...]:
    """Returns accession numbers whose ``filed_at`` precedes an
    earlier-in-sequence filing's ``filed_at`` for the identical issuer --
    should always be empty for genuine SEC evidence; flagged, not raised,
    consistent with "flag anomalies rather than correcting source data."""
    by_issuer: dict[str, list[CorporateFilingEvent]] = {}
    for item in filings:
        by_issuer.setdefault(item.stable_security_id, []).append(item)
    out_of_order: list[str] = []
    for members in by_issuer.values():
        ordered_by_accession = sorted(members, key=lambda item: item.accession_number)
        last_filed: datetime | None = None
        for item in ordered_by_accession:
            if last_filed is not None and item.filed_at < last_filed:
                out_of_order.append(item.accession_number)
            last_filed = item.filed_at
    return tuple(sorted(out_of_order))


# ---------------------------------------------------------------------------
# Fundamental snapshot (section 7)
# ---------------------------------------------------------------------------


class IssuerDataQuality(StrEnum):
    USABLE = "USABLE"
    USABLE_WITH_WARNINGS = "USABLE_WITH_WARNINGS"
    INSUFFICIENT = "INSUFFICIENT"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class SnapshotQualityPolicy:
    policy_id: str
    version: str
    core_concepts: tuple[FundamentalConcept, ...]
    usable_minimum_core_concepts: int
    warnings_minimum_core_concepts: int
    canonical_hash: str

    @classmethod
    def create(cls) -> SnapshotQualityPolicy:
        core = (
            FundamentalConcept.REVENUE,
            FundamentalConcept.NET_INCOME,
            FundamentalConcept.TOTAL_ASSETS,
            FundamentalConcept.TOTAL_LIABILITIES,
            FundamentalConcept.STOCKHOLDERS_EQUITY,
            FundamentalConcept.OPERATING_CASH_FLOW,
        )
        payload = ("SNAPSHOT_QUALITY_POLICY_V1", "V1", core, 5, 2)
        return cls(
            policy_id=deterministic_id("corporate_snapshot_quality_policy", payload),
            version="V1",
            core_concepts=core,
            usable_minimum_core_concepts=5,
            warnings_minimum_core_concepts=2,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class SnapshotFactValue:
    concept: FundamentalConcept
    value: Decimal | None
    unit: FactUnit
    fact_id: str | None
    fiscal_year: int | None
    fiscal_period: FiscalPeriod | None
    period_end: str | None
    accession_number: str | None
    filed_at: datetime | None
    accepted_at: datetime | None
    is_ytd_derived: bool
    missing_reason: str | None

    def __post_init__(self) -> None:
        if (self.value is None) != (self.missing_reason is not None):
            raise CorporateFundamentalsError("SNAPSHOT_FACT_VALUE_MISSING_REASON_INCONSISTENT")


@dataclass(frozen=True, slots=True)
class CorporateFundamentalSnapshot:
    snapshot_id: str
    stable_security_id: str
    cik: str
    as_of: datetime
    fields: tuple[SnapshotFactValue, ...]
    filing_lineage: tuple[str, ...]
    fact_lineage: tuple[str, ...]
    most_recent_filing_accepted_at: datetime | None
    freshness_days: int | None
    quality: IssuerDataQuality
    quality_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    def value_for(self, concept: FundamentalConcept) -> SnapshotFactValue:
        for field in self.fields:
            if field.concept is concept:
                return field
        raise CorporateFundamentalsError("SNAPSHOT_CONCEPT_NOT_PRESENT")


def build_corporate_fundamental_snapshot(
    *,
    stable_security_id: str,
    cik: str,
    as_of: datetime,
    filings: Sequence[CorporateFilingEvent],
    facts: Sequence[FundamentalFact],
    quality_policy: SnapshotQualityPolicy | None = None,
) -> CorporateFundamentalSnapshot:
    """The single deterministic point-in-time reconstruction function this
    entire milestone is built around. Never fills a missing fact with zero
    (section 7); every field is present for every one of the 11 bounded
    concepts, either with a value and full lineage or an explicit
    ``missing_reason``.
    """
    _aware(as_of, "SNAPSHOT_AS_OF_INVALID")
    policy = quality_policy or SnapshotQualityPolicy.create()
    visible_filings = visible_filings_as_of(filings, as_of=as_of)
    visible_facts = visible_facts_as_of(facts, as_of=as_of)

    fields: list[SnapshotFactValue] = []
    for definition in CONCEPT_DEFINITIONS:
        candidates = [item for item in visible_facts if item.concept is definition.concept]
        chosen = _choose_fact(candidates)
        if chosen is None:
            fields.append(
                SnapshotFactValue(
                    concept=definition.concept,
                    value=None,
                    unit=definition.unit,
                    fact_id=None,
                    fiscal_year=None,
                    fiscal_period=None,
                    period_end=None,
                    accession_number=None,
                    filed_at=None,
                    accepted_at=None,
                    is_ytd_derived=False,
                    missing_reason="NO_VISIBLE_FACT_FOR_CONCEPT",
                )
            )
            continue
        fields.append(
            SnapshotFactValue(
                concept=definition.concept,
                value=chosen.value,
                unit=chosen.unit,
                fact_id=chosen.fact_id,
                fiscal_year=chosen.fiscal_year,
                fiscal_period=chosen.fiscal_period,
                period_end=chosen.period_end,
                accession_number=chosen.accession_number,
                filed_at=chosen.filed_at,
                accepted_at=chosen.accepted_at,
                is_ytd_derived=chosen.is_ytd_derived,
                missing_reason=None,
            )
        )

    fact_lineage = tuple(sorted({field.fact_id for field in fields if field.fact_id is not None}))
    filing_lineage = tuple(
        sorted({field.accession_number for field in fields if field.accession_number is not None})
    )
    most_recent = max((item.effective_available_at for item in visible_filings), default=None)
    freshness_days = (as_of.date() - most_recent.date()).days if most_recent is not None else None

    core_present = sum(
        1 for field in fields if field.concept in policy.core_concepts and field.value is not None
    )
    has_core_filing = any(item.form in _CORE_ANNUAL_QUARTERLY_FORMS for item in visible_filings)
    reasons: list[str] = []
    if not visible_filings and not visible_facts:
        quality = IssuerDataQuality.REJECTED
        reasons.append("NO_FILINGS_OR_FACTS_VISIBLE_AS_OF_DATE")
    elif core_present >= policy.usable_minimum_core_concepts and has_core_filing:
        quality = IssuerDataQuality.USABLE
    elif core_present >= policy.warnings_minimum_core_concepts:
        quality = IssuerDataQuality.USABLE_WITH_WARNINGS
        reasons.append(f"ONLY_{core_present}_OF_{len(policy.core_concepts)}_CORE_CONCEPTS_VISIBLE")
        if not has_core_filing:
            reasons.append("NO_10K_OR_10Q_FILING_VISIBLE")
    else:
        quality = IssuerDataQuality.INSUFFICIENT
        reasons.append(f"ONLY_{core_present}_OF_{len(policy.core_concepts)}_CORE_CONCEPTS_VISIBLE")

    payload = (
        CORPORATE_FUNDAMENTAL_SNAPSHOT_V1,
        stable_security_id,
        cik,
        as_of,
        tuple(
            (field.concept, str(field.value) if field.value is not None else None, field.fact_id)
            for field in fields
        ),
        quality,
    )
    return CorporateFundamentalSnapshot(
        snapshot_id=deterministic_id("corporate_fundamental_snapshot", payload),
        stable_security_id=stable_security_id,
        cik=cik,
        as_of=as_of,
        fields=tuple(fields),
        filing_lineage=filing_lineage,
        fact_lineage=fact_lineage,
        most_recent_filing_accepted_at=most_recent,
        freshness_days=freshness_days,
        quality=quality,
        quality_reasons=tuple(reasons),
        limitations=corporate_fundamentals_warnings(),
        canonical_hash=content_digest(payload),
    )


# ---------------------------------------------------------------------------
# Derived fundamental features (section 8) -- transparent, bounded, no
# opaque composite score.
# ---------------------------------------------------------------------------


class DerivedFeatureType(StrEnum):
    YOY_REVENUE_GROWTH = "YOY_REVENUE_GROWTH"
    YOY_NET_INCOME_GROWTH = "YOY_NET_INCOME_GROWTH"
    YOY_OPERATING_CASH_FLOW_GROWTH = "YOY_OPERATING_CASH_FLOW_GROWTH"
    OPERATING_MARGIN = "OPERATING_MARGIN"
    OPERATING_MARGIN_CHANGE_YOY = "OPERATING_MARGIN_CHANGE_YOY"
    NET_MARGIN = "NET_MARGIN"
    LEVERAGE_LIABILITIES_TO_ASSETS = "LEVERAGE_LIABILITIES_TO_ASSETS"
    CASH_TO_ASSETS = "CASH_TO_ASSETS"
    SHARE_COUNT_CHANGE_YOY = "SHARE_COUNT_CHANGE_YOY"


@dataclass(frozen=True, slots=True)
class DerivedFundamentalFeature:
    feature_id: str
    feature_type: DerivedFeatureType
    stable_security_id: str
    as_of: datetime
    value: Decimal | None
    required_source_fact_ids: tuple[str, ...]
    period_comparison: str
    methodology: str
    unavailable_reason: str | None
    canonical_hash: str
    authority: str = "NONE"

    def __post_init__(self) -> None:
        if (self.value is None) != (self.unavailable_reason is not None):
            raise CorporateFundamentalsError("DERIVED_FEATURE_UNAVAILABLE_REASON_INCONSISTENT")


def _feature(
    *,
    feature_type: DerivedFeatureType,
    stable_security_id: str,
    as_of: datetime,
    value: Decimal | None,
    source_fact_ids: tuple[str, ...],
    period_comparison: str,
    methodology: str,
    unavailable_reason: str | None,
) -> DerivedFundamentalFeature:
    payload = (
        DERIVED_FUNDAMENTAL_FEATURE_V1,
        feature_type,
        stable_security_id,
        as_of,
        str(value) if value is not None else None,
        source_fact_ids,
        period_comparison,
        unavailable_reason,
    )
    return DerivedFundamentalFeature(
        feature_id=deterministic_id("corporate_derived_feature", payload),
        feature_type=feature_type,
        stable_security_id=stable_security_id,
        as_of=as_of,
        value=value,
        required_source_fact_ids=source_fact_ids,
        period_comparison=period_comparison,
        methodology=methodology,
        unavailable_reason=unavailable_reason,
        canonical_hash=content_digest(payload),
    )


def _growth(
    *,
    feature_type: DerivedFeatureType,
    stable_security_id: str,
    as_of: datetime,
    current: SnapshotFactValue,
    prior: SnapshotFactValue | None,
) -> DerivedFundamentalFeature:
    comparison = (
        f"{current.fiscal_period.value if current.fiscal_period is not None else '?'} FY"
        f"{current.fiscal_year} vs prior-year same period"
    )
    methodology = (
        "(current - prior) / abs(prior); requires the identical fiscal period one year earlier"
    )
    if current.value is None:
        return _feature(
            feature_type=feature_type,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=None,
            source_fact_ids=(),
            period_comparison=comparison,
            methodology=methodology,
            unavailable_reason="CURRENT_PERIOD_FACT_UNAVAILABLE",
        )
    if prior is None or prior.value is None:
        return _feature(
            feature_type=feature_type,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=None,
            source_fact_ids=(current.fact_id,) if current.fact_id else (),
            period_comparison=comparison,
            methodology=methodology,
            unavailable_reason="PRIOR_YEAR_SAME_PERIOD_FACT_UNAVAILABLE",
        )
    if prior.value == 0:
        return _feature(
            feature_type=feature_type,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=None,
            source_fact_ids=tuple(
                fact_id for fact_id in (current.fact_id, prior.fact_id) if fact_id
            ),
            period_comparison=comparison,
            methodology=methodology,
            unavailable_reason="PRIOR_VALUE_ZERO_GROWTH_UNDEFINED",
        )
    value = _bounded_ratio((current.value - prior.value) / abs(prior.value))
    return _feature(
        feature_type=feature_type,
        stable_security_id=stable_security_id,
        as_of=as_of,
        value=value,
        source_fact_ids=tuple(fact_id for fact_id in (current.fact_id, prior.fact_id) if fact_id),
        period_comparison=comparison,
        methodology=methodology,
        unavailable_reason=None,
    )


def _ratio(
    *,
    feature_type: DerivedFeatureType,
    stable_security_id: str,
    as_of: datetime,
    numerator: SnapshotFactValue,
    denominator: SnapshotFactValue,
    methodology: str,
) -> DerivedFundamentalFeature:
    comparison = f"as of {numerator.period_end or '?'}"
    if numerator.value is None or denominator.value is None:
        return _feature(
            feature_type=feature_type,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=None,
            source_fact_ids=tuple(
                fact_id for fact_id in (numerator.fact_id, denominator.fact_id) if fact_id
            ),
            period_comparison=comparison,
            methodology=methodology,
            unavailable_reason="REQUIRED_FACT_UNAVAILABLE",
        )
    if denominator.value == 0:
        return _feature(
            feature_type=feature_type,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=None,
            source_fact_ids=(numerator.fact_id or "", denominator.fact_id or ""),
            period_comparison=comparison,
            methodology=methodology,
            unavailable_reason="DENOMINATOR_ZERO",
        )
    return _feature(
        feature_type=feature_type,
        stable_security_id=stable_security_id,
        as_of=as_of,
        value=_bounded_ratio(numerator.value / denominator.value),
        source_fact_ids=(numerator.fact_id or "", denominator.fact_id or ""),
        period_comparison=comparison,
        methodology=methodology,
        unavailable_reason=None,
    )


def _prior_year_value(
    *, as_of_visible_facts: Sequence[FundamentalFact], current: SnapshotFactValue
) -> SnapshotFactValue | None:
    """``as_of_visible_facts`` must already be point-in-time filtered
    (``visible_facts_as_of``) -- named accordingly so a caller cannot pass
    the raw, unfiltered retained set by mistake the way an unqualified
    ``facts`` parameter invites."""
    if current.fiscal_year is None or current.fiscal_period is None:
        return None
    candidates = facts_for_period(
        as_of_visible_facts,
        concept=current.concept,
        fiscal_year=current.fiscal_year - 1,
        fiscal_period=current.fiscal_period,
    )
    chosen = _choose_fact(candidates)
    if chosen is None:
        return None
    return SnapshotFactValue(
        concept=chosen.concept,
        value=chosen.value,
        unit=chosen.unit,
        fact_id=chosen.fact_id,
        fiscal_year=chosen.fiscal_year,
        fiscal_period=chosen.fiscal_period,
        period_end=chosen.period_end,
        accession_number=chosen.accession_number,
        filed_at=chosen.filed_at,
        accepted_at=chosen.accepted_at,
        is_ytd_derived=chosen.is_ytd_derived,
        missing_reason=None,
    )


def compute_derived_features(
    *,
    snapshot: CorporateFundamentalSnapshot,
    facts: Sequence[FundamentalFact],
) -> tuple[DerivedFundamentalFeature, ...]:
    """Every feature below states its own required source facts, period
    comparison, and methodology (section 8) -- no opaque composite score.

    ``facts`` may be the full retained set for the issuer, filtered or not:
    this function re-derives its own ``snapshot.as_of``-visible subset
    internally (mirroring ``build_corporate_fundamental_snapshot`` and
    ``ReplayCorporateEvidenceAttachment.create``'s defense-in-depth
    anti-lookahead re-checks) rather than trusting a caller-supplied
    "already visible" set -- a prior-year lookup is exactly where a caller
    passing the raw retained set by mistake would otherwise silently leak a
    future restatement into a growth calculation.
    """

    as_of_visible = visible_facts_as_of(facts, as_of=snapshot.as_of)
    revenue = snapshot.value_for(FundamentalConcept.REVENUE)
    net_income = snapshot.value_for(FundamentalConcept.NET_INCOME)
    operating_income = snapshot.value_for(FundamentalConcept.OPERATING_INCOME)
    operating_cash_flow = snapshot.value_for(FundamentalConcept.OPERATING_CASH_FLOW)
    total_assets = snapshot.value_for(FundamentalConcept.TOTAL_ASSETS)
    total_liabilities = snapshot.value_for(FundamentalConcept.TOTAL_LIABILITIES)
    cash = snapshot.value_for(FundamentalConcept.CASH_AND_EQUIVALENTS)
    shares = snapshot.value_for(FundamentalConcept.SHARES_OUTSTANDING)

    prior_revenue = _prior_year_value(as_of_visible_facts=as_of_visible, current=revenue)
    prior_net_income = _prior_year_value(as_of_visible_facts=as_of_visible, current=net_income)
    prior_operating_cash_flow = _prior_year_value(
        as_of_visible_facts=as_of_visible, current=operating_cash_flow
    )
    prior_operating_income = _prior_year_value(
        as_of_visible_facts=as_of_visible, current=operating_income
    )
    prior_shares = _prior_year_value(as_of_visible_facts=as_of_visible, current=shares)

    features = [
        _growth(
            feature_type=DerivedFeatureType.YOY_REVENUE_GROWTH,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            current=revenue,
            prior=prior_revenue,
        ),
        _growth(
            feature_type=DerivedFeatureType.YOY_NET_INCOME_GROWTH,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            current=net_income,
            prior=prior_net_income,
        ),
        _growth(
            feature_type=DerivedFeatureType.YOY_OPERATING_CASH_FLOW_GROWTH,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            current=operating_cash_flow,
            prior=prior_operating_cash_flow,
        ),
        _ratio(
            feature_type=DerivedFeatureType.OPERATING_MARGIN,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            numerator=operating_income,
            denominator=revenue,
            methodology="operating_income / revenue, same reporting period",
        ),
        _ratio(
            feature_type=DerivedFeatureType.NET_MARGIN,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            numerator=net_income,
            denominator=revenue,
            methodology="net_income / revenue, same reporting period",
        ),
        _ratio(
            feature_type=DerivedFeatureType.LEVERAGE_LIABILITIES_TO_ASSETS,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            numerator=total_liabilities,
            denominator=total_assets,
            methodology="total_liabilities / total_assets, same balance-sheet date",
        ),
        _ratio(
            feature_type=DerivedFeatureType.CASH_TO_ASSETS,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            numerator=cash,
            denominator=total_assets,
            methodology="cash_and_equivalents / total_assets, same balance-sheet date",
        ),
        _growth(
            feature_type=DerivedFeatureType.SHARE_COUNT_CHANGE_YOY,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            current=shares,
            prior=prior_shares,
        ),
    ]

    # Operating-margin change YoY needs both periods' revenue and operating
    # income simultaneously, so it is computed inline rather than through
    # the single-ratio/single-growth helpers above.
    comparison = f"FY{operating_income.fiscal_year or '?'} vs prior year"
    methodology = "operating_income/revenue this period minus the identical ratio one year earlier"
    if (
        operating_income.value is None
        or revenue.value is None
        or revenue.value == 0
        or prior_operating_income is None
        or prior_operating_income.value is None
        or prior_revenue is None
        or prior_revenue.value is None
        or prior_revenue.value == 0
    ):
        reason = (
            "REQUIRED_FACT_UNAVAILABLE"
            if operating_income.value is None or revenue.value is None
            else "PRIOR_YEAR_MARGIN_UNAVAILABLE"
            if prior_operating_income is None
            or prior_operating_income.value is None
            or prior_revenue is None
            or prior_revenue.value is None
            else "DENOMINATOR_ZERO"
        )
        features.append(
            _feature(
                feature_type=DerivedFeatureType.OPERATING_MARGIN_CHANGE_YOY,
                stable_security_id=snapshot.stable_security_id,
                as_of=snapshot.as_of,
                value=None,
                source_fact_ids=tuple(
                    fact_id
                    for fact_id in (
                        operating_income.fact_id,
                        revenue.fact_id,
                        prior_operating_income.fact_id if prior_operating_income else None,
                        prior_revenue.fact_id if prior_revenue else None,
                    )
                    if fact_id
                ),
                period_comparison=comparison,
                methodology=methodology,
                unavailable_reason=reason,
            )
        )
    else:
        current_margin = _bounded_ratio(operating_income.value / revenue.value)
        prior_margin = _bounded_ratio(prior_operating_income.value / prior_revenue.value)
        features.append(
            _feature(
                feature_type=DerivedFeatureType.OPERATING_MARGIN_CHANGE_YOY,
                stable_security_id=snapshot.stable_security_id,
                as_of=snapshot.as_of,
                value=current_margin - prior_margin,
                source_fact_ids=(
                    operating_income.fact_id or "",
                    revenue.fact_id or "",
                    prior_operating_income.fact_id or "",
                    prior_revenue.fact_id or "",
                ),
                period_comparison=comparison,
                methodology=methodology,
                unavailable_reason=None,
            )
        )
    return tuple(features)


# ---------------------------------------------------------------------------
# Filing-event features (section 9)
# ---------------------------------------------------------------------------


class FilingEventFeatureType(StrEnum):
    DAYS_SINCE_LAST_10Q_OR_10K = "DAYS_SINCE_LAST_10Q_OR_10K"
    NEW_FILING_INDICATOR = "NEW_FILING_INDICATOR"
    RECENT_8K_INDICATOR = "RECENT_8K_INDICATOR"
    AMENDMENT_INDICATOR = "AMENDMENT_INDICATOR"
    FILING_FREQUENCY = "FILING_FREQUENCY"
    DAYS_FISCAL_PERIOD_END_TO_FILING = "DAYS_FISCAL_PERIOD_END_TO_FILING"


@dataclass(frozen=True, slots=True)
class FilingEventFeature:
    feature_id: str
    feature_type: FilingEventFeatureType
    stable_security_id: str
    as_of: datetime
    value: Decimal | None
    lookback_days: int | None
    supporting_accession_numbers: tuple[str, ...]
    methodology: str
    unavailable_reason: str | None
    canonical_hash: str
    authority: str = "NONE"


def _filing_event_feature(
    *,
    feature_type: FilingEventFeatureType,
    stable_security_id: str,
    as_of: datetime,
    value: Decimal | None,
    lookback_days: int | None,
    accession_numbers: tuple[str, ...],
    methodology: str,
    unavailable_reason: str | None,
) -> FilingEventFeature:
    payload = (
        FILING_EVENT_FEATURE_V1,
        feature_type,
        stable_security_id,
        as_of,
        str(value) if value is not None else None,
        lookback_days,
        accession_numbers,
        unavailable_reason,
    )
    return FilingEventFeature(
        feature_id=deterministic_id("corporate_filing_event_feature", payload),
        feature_type=feature_type,
        stable_security_id=stable_security_id,
        as_of=as_of,
        value=value,
        lookback_days=lookback_days,
        supporting_accession_numbers=accession_numbers,
        methodology=methodology,
        unavailable_reason=unavailable_reason,
        canonical_hash=content_digest(payload),
    )


def compute_filing_event_features(
    *,
    stable_security_id: str,
    as_of: datetime,
    filings: Sequence[CorporateFilingEvent],
    lookback_days: int = 30,
) -> tuple[FilingEventFeature, ...]:
    visible = visible_filings_as_of(filings, as_of=as_of)
    window_start = as_of - timedelta(days=lookback_days)
    within_window = tuple(item for item in visible if item.effective_available_at >= window_start)
    features: list[FilingEventFeature] = []

    core = tuple(item for item in visible if item.form in _CORE_ANNUAL_QUARTERLY_FORMS)
    if core:
        last = max(core, key=lambda item: item.effective_available_at)
        days = (as_of.date() - last.effective_available_at.date()).days
        features.append(
            _filing_event_feature(
                feature_type=FilingEventFeatureType.DAYS_SINCE_LAST_10Q_OR_10K,
                stable_security_id=stable_security_id,
                as_of=as_of,
                value=Decimal(days),
                lookback_days=None,
                accession_numbers=(last.accession_number,),
                methodology="calendar days between as_of and the most recently visible 10-K/10-Q",
                unavailable_reason=None,
            )
        )
    else:
        features.append(
            _filing_event_feature(
                feature_type=FilingEventFeatureType.DAYS_SINCE_LAST_10Q_OR_10K,
                stable_security_id=stable_security_id,
                as_of=as_of,
                value=None,
                lookback_days=None,
                accession_numbers=(),
                methodology="calendar days between as_of and the most recently visible 10-K/10-Q",
                unavailable_reason="NO_10K_OR_10Q_VISIBLE",
            )
        )

    features.append(
        _filing_event_feature(
            feature_type=FilingEventFeatureType.NEW_FILING_INDICATOR,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=Decimal(1) if within_window else Decimal(0),
            lookback_days=lookback_days,
            accession_numbers=tuple(sorted(item.accession_number for item in within_window)),
            methodology=(
                f"1 if any filing became visible within the trailing {lookback_days} days, else 0"
            ),
            unavailable_reason=None,
        )
    )

    recent_8k = tuple(
        item for item in within_window if item.form in {FilingForm.FORM_8K, FilingForm.FORM_8K_A}
    )
    features.append(
        _filing_event_feature(
            feature_type=FilingEventFeatureType.RECENT_8K_INDICATOR,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=Decimal(1) if recent_8k else Decimal(0),
            lookback_days=lookback_days,
            accession_numbers=tuple(sorted(item.accession_number for item in recent_8k)),
            methodology=(
                f"1 if any 8-K became visible within the trailing {lookback_days} days, else 0"
            ),
            unavailable_reason=None,
        )
    )

    recent_amendment = tuple(
        item for item in within_window if item.amendment_state is AmendmentState.AMENDMENT
    )
    features.append(
        _filing_event_feature(
            feature_type=FilingEventFeatureType.AMENDMENT_INDICATOR,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=Decimal(1) if recent_amendment else Decimal(0),
            lookback_days=lookback_days,
            accession_numbers=tuple(sorted(item.accession_number for item in recent_amendment)),
            methodology=(
                f"1 if any amendment (form ending '/A') became visible within the trailing "
                f"{lookback_days} days, else 0"
            ),
            unavailable_reason=None,
        )
    )

    features.append(
        _filing_event_feature(
            feature_type=FilingEventFeatureType.FILING_FREQUENCY,
            stable_security_id=stable_security_id,
            as_of=as_of,
            value=Decimal(len(within_window)),
            lookback_days=lookback_days,
            accession_numbers=tuple(sorted(item.accession_number for item in within_window)),
            methodology=f"count of filings visible within the trailing {lookback_days} days",
            unavailable_reason=None,
        )
    )

    with_period = tuple(item for item in core if item.report_period)
    if with_period:
        last_with_period = max(with_period, key=lambda item: item.effective_available_at)
        report_period = last_with_period.report_period
        assert report_period is not None  # narrowed by the `with_period` filter above
        days_to_filing = (
            last_with_period.effective_available_at.date()
            - datetime.fromisoformat(report_period).date()
        ).days
        features.append(
            _filing_event_feature(
                feature_type=FilingEventFeatureType.DAYS_FISCAL_PERIOD_END_TO_FILING,
                stable_security_id=stable_security_id,
                as_of=as_of,
                value=Decimal(days_to_filing),
                lookback_days=None,
                accession_numbers=(last_with_period.accession_number,),
                methodology=(
                    "calendar days from the filing's own report period end to its filing evidence"
                ),
                unavailable_reason=None,
            )
        )
    else:
        features.append(
            _filing_event_feature(
                feature_type=FilingEventFeatureType.DAYS_FISCAL_PERIOD_END_TO_FILING,
                stable_security_id=stable_security_id,
                as_of=as_of,
                value=None,
                lookback_days=None,
                accession_numbers=(),
                methodology=(
                    "calendar days from the filing's own report period end to its filing evidence"
                ),
                unavailable_reason="NO_10K_OR_10Q_WITH_REPORT_PERIOD_VISIBLE",
            )
        )
    return tuple(features)


# ---------------------------------------------------------------------------
# Corporate replay feature frame (milestone: Corporate Evidence Completion
# and Replay Scaling v1, section 5) -- a bounded, named *selection* of
# already-computed derived/filing-event features, never a new value type
# and never an opaque composite score.
# ---------------------------------------------------------------------------

_DERIVED_FEATURE_SLOTS: tuple[tuple[str, DerivedFeatureType], ...] = (
    ("revenue_growth_yoy", DerivedFeatureType.YOY_REVENUE_GROWTH),
    ("net_income_growth_yoy", DerivedFeatureType.YOY_NET_INCOME_GROWTH),
    ("operating_margin", DerivedFeatureType.OPERATING_MARGIN),
    ("operating_margin_change_yoy", DerivedFeatureType.OPERATING_MARGIN_CHANGE_YOY),
    ("operating_cash_flow_growth_yoy", DerivedFeatureType.YOY_OPERATING_CASH_FLOW_GROWTH),
    ("leverage_liabilities_to_assets", DerivedFeatureType.LEVERAGE_LIABILITIES_TO_ASSETS),
    ("cash_to_assets", DerivedFeatureType.CASH_TO_ASSETS),
    ("share_count_change_yoy", DerivedFeatureType.SHARE_COUNT_CHANGE_YOY),
)
_FILING_FEATURE_SLOTS: tuple[tuple[str, FilingEventFeatureType], ...] = (
    ("latest_10q_or_10k_age_days", FilingEventFeatureType.DAYS_SINCE_LAST_10Q_OR_10K),
    ("recent_8k_indicator", FilingEventFeatureType.RECENT_8K_INDICATOR),
)


def _find_derived(
    features: Sequence[DerivedFundamentalFeature], feature_type: DerivedFeatureType
) -> DerivedFundamentalFeature:
    for item in features:
        if item.feature_type is feature_type:
            return item
    raise CorporateFundamentalsError("REPLAY_FEATURE_FRAME_FEATURE_TYPE_NOT_PRESENT")


def _find_filing_feature(
    features: Sequence[FilingEventFeature], feature_type: FilingEventFeatureType
) -> FilingEventFeature:
    for item in features:
        if item.feature_type is feature_type:
            return item
    raise CorporateFundamentalsError("REPLAY_FEATURE_FRAME_FEATURE_TYPE_NOT_PRESENT")


@dataclass(frozen=True, slots=True)
class CorporateReplayFeatureFrame:
    """One bounded, point-in-time corporate feature projection per security
    per replay frame. A named *selection* of exactly the derived/filing-
    event features cross-market research needs -- every field is one of
    the already-reviewed, independently transparent ``DerivedFundamental
    Feature``/``FilingEventFeature`` objects (own value, required source
    fact ids, methodology, ``unavailable_reason``); nothing here
    recomputes a feature or invents a value its inputs did not already
    carry. ``missing_fields`` names exactly which of the ten bounded slots
    below are currently unavailable (see each field's own
    ``unavailable_reason`` for why) -- there is deliberately no single
    composite "quality" or "score" number anywhere on this dataclass;
    ``snapshot_quality`` is only ever the pre-existing, already-reviewed
    ``IssuerDataQuality`` classification, never a new one.
    """

    frame_feature_id: str
    stable_security_id: str
    as_of: datetime
    snapshot_id: str
    snapshot_quality: IssuerDataQuality
    freshness_days: int | None
    latest_10q_or_10k_age_days: FilingEventFeature
    recent_8k_indicator: FilingEventFeature
    revenue_growth_yoy: DerivedFundamentalFeature
    net_income_growth_yoy: DerivedFundamentalFeature
    operating_margin: DerivedFundamentalFeature
    operating_margin_change_yoy: DerivedFundamentalFeature
    operating_cash_flow_growth_yoy: DerivedFundamentalFeature
    leverage_liabilities_to_assets: DerivedFundamentalFeature
    cash_to_assets: DerivedFundamentalFeature
    share_count_change_yoy: DerivedFundamentalFeature
    missing_fields: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def build_corporate_replay_feature_frame(
    *,
    snapshot: CorporateFundamentalSnapshot,
    derived_features: Sequence[DerivedFundamentalFeature],
    filing_event_features: Sequence[FilingEventFeature],
) -> CorporateReplayFeatureFrame:
    """Composes an already-computed ``snapshot``/``derived_features``
    (``compute_derived_features``)/``filing_event_features`` (``compute_
    filing_event_features``) triple -- all required to share the identical
    ``as_of`` -- into one bounded, named projection. Pure composition only:
    never recomputes a feature, never fetches evidence, never invents a
    value its inputs did not already carry."""
    if any(item.as_of != snapshot.as_of for item in derived_features):
        raise CorporateFundamentalsError("REPLAY_FEATURE_FRAME_AS_OF_MISMATCH")
    if any(item.as_of != snapshot.as_of for item in filing_event_features):
        raise CorporateFundamentalsError("REPLAY_FEATURE_FRAME_AS_OF_MISMATCH")

    derived_by_slot = {
        name: _find_derived(derived_features, feature_type)
        for name, feature_type in _DERIVED_FEATURE_SLOTS
    }
    filing_by_slot = {
        name: _find_filing_feature(filing_event_features, feature_type)
        for name, feature_type in _FILING_FEATURE_SLOTS
    }
    all_slots: dict[str, DerivedFundamentalFeature | FilingEventFeature] = {
        **derived_by_slot,
        **filing_by_slot,
    }
    missing = tuple(sorted(name for name, feature in all_slots.items() if feature.value is None))
    payload = (
        CORPORATE_REPLAY_FEATURE_FRAME_V1,
        snapshot.snapshot_id,
        snapshot.stable_security_id,
        snapshot.as_of,
        tuple(feature.canonical_hash for _, feature in sorted(filing_by_slot.items())),
        tuple(feature.canonical_hash for _, feature in sorted(derived_by_slot.items())),
    )
    return CorporateReplayFeatureFrame(
        frame_feature_id=deterministic_id("corporate_replay_feature_frame", payload),
        stable_security_id=snapshot.stable_security_id,
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        snapshot_quality=snapshot.quality,
        freshness_days=snapshot.freshness_days,
        latest_10q_or_10k_age_days=filing_by_slot["latest_10q_or_10k_age_days"],
        recent_8k_indicator=filing_by_slot["recent_8k_indicator"],
        revenue_growth_yoy=derived_by_slot["revenue_growth_yoy"],
        net_income_growth_yoy=derived_by_slot["net_income_growth_yoy"],
        operating_margin=derived_by_slot["operating_margin"],
        operating_margin_change_yoy=derived_by_slot["operating_margin_change_yoy"],
        operating_cash_flow_growth_yoy=derived_by_slot["operating_cash_flow_growth_yoy"],
        leverage_liabilities_to_assets=derived_by_slot["leverage_liabilities_to_assets"],
        cash_to_assets=derived_by_slot["cash_to_assets"],
        share_count_change_yoy=derived_by_slot["share_count_change_yoy"],
        missing_fields=missing,
        canonical_hash=content_digest(payload),
    )


# ---------------------------------------------------------------------------
# Genuine bounded cohort / CIK resolution (sections 11, 20)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorporateEvidenceCohortMember:
    stable_security_id: str
    symbol: str
    cik: str
    sector: str
    asset_class: str


@dataclass(frozen=True, slots=True)
class CorporateEvidenceCohortResolution:
    members: tuple[CorporateEvidenceCohortMember, ...]
    excluded_missing_cik: tuple[str, ...]
    excluded_unresolved: tuple[str, ...]


def resolve_corporate_evidence_cohort(
    resolved_cohort: ResolvedCohort, stable_identities: Sequence[StableSecurityIdentity]
) -> CorporateEvidenceCohortResolution:
    """Anchors on stable security identity, never on a current ticker alone
    (section 20): every member is looked up by its already-resolved
    ``stable_security_id``, and a member without retained CIK evidence is
    reported, not silently skipped or guessed at. Does not pre-judge asset
    class (an ETF's genuine SEC filing/fact coverage, if any, is discovered
    -- not assumed absent -- by capture and reported honestly through
    ``CorporateDataQualityReport``)."""
    by_id = {item.stable_security_id: item for item in stable_identities}
    members: list[CorporateEvidenceCohortMember] = []
    missing_cik: list[str] = []
    unresolved: list[str] = []
    for candidate in resolved_cohort.members:
        identity = by_id.get(candidate.stable_security_id)
        if identity is None:
            unresolved.append(candidate.symbol)
            continue
        if not identity.cik:
            missing_cik.append(candidate.symbol)
            continue
        members.append(
            CorporateEvidenceCohortMember(
                stable_security_id=candidate.stable_security_id,
                symbol=candidate.symbol,
                cik=identity.cik,
                sector=candidate.sector.value,
                asset_class=candidate.asset_class,
            )
        )
    return CorporateEvidenceCohortResolution(
        members=tuple(sorted(members, key=lambda item: item.symbol)),
        excluded_missing_cik=tuple(sorted(missing_cik)),
        excluded_unresolved=tuple(sorted(unresolved)),
    )


# ---------------------------------------------------------------------------
# Bounded capture plan / checkpoint (sections 11, 12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorporateEvidenceCaptureBounds:
    bounds_id: str
    max_issuers_per_run: int
    max_requests_per_run: int
    max_bytes_per_run: int
    max_runtime_seconds: int
    min_request_interval_seconds: Decimal
    lookback_years: int
    concept_vocabulary_version: str
    retained_forms: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        max_issuers_per_run: int = 25,
        max_requests_per_run: int = 80,
        max_bytes_per_run: int = 200_000_000,
        max_runtime_seconds: int = 300,
        min_request_interval_seconds: Decimal = Decimal("0.35"),
        lookback_years: int = 6,
    ) -> CorporateEvidenceCaptureBounds:
        if not (1 <= max_issuers_per_run <= 200):
            raise CorporateFundamentalsError("CAPTURE_BOUNDS_ISSUERS_INVALID")
        if not (1 <= max_requests_per_run <= 2_000):
            raise CorporateFundamentalsError("CAPTURE_BOUNDS_REQUESTS_INVALID")
        if not (1 <= max_bytes_per_run <= 1_000_000_000):
            raise CorporateFundamentalsError("CAPTURE_BOUNDS_BYTES_INVALID")
        if not (1 <= max_runtime_seconds <= 3_600):
            raise CorporateFundamentalsError("CAPTURE_BOUNDS_RUNTIME_INVALID")
        interval = parse_decimal(min_request_interval_seconds, allow_zero=False)
        if interval < Decimal("0.1"):
            raise CorporateFundamentalsError("CAPTURE_BOUNDS_RATE_LIMIT_TOO_AGGRESSIVE")
        if not (1 <= lookback_years <= 30):
            raise CorporateFundamentalsError("CAPTURE_BOUNDS_LOOKBACK_INVALID")
        forms = tuple(sorted(RETAINED_FILING_FORMS))
        payload = (
            max_issuers_per_run,
            max_requests_per_run,
            max_bytes_per_run,
            max_runtime_seconds,
            interval,
            lookback_years,
            CONCEPT_VOCABULARY_VERSION,
            forms,
        )
        return cls(
            bounds_id=deterministic_id("corporate_evidence_capture_bounds", payload),
            max_issuers_per_run=max_issuers_per_run,
            max_requests_per_run=max_requests_per_run,
            max_bytes_per_run=max_bytes_per_run,
            max_runtime_seconds=max_runtime_seconds,
            min_request_interval_seconds=interval,
            lookback_years=lookback_years,
            concept_vocabulary_version=CONCEPT_VOCABULARY_VERSION,
            retained_forms=forms,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class CorporateEvidenceCohortPlan:
    plan_id: str
    cohort_policy_id: str
    cohort_policy_hash: str
    members: tuple[CorporateEvidenceCohortMember, ...]
    excluded_missing_cik: tuple[str, ...]
    excluded_unresolved: tuple[str, ...]
    bounds: CorporateEvidenceCaptureBounds
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        cohort_policy_id: str,
        cohort_policy_hash: str,
        resolution: CorporateEvidenceCohortResolution,
        bounds: CorporateEvidenceCaptureBounds,
        created_at: datetime,
    ) -> CorporateEvidenceCohortPlan:
        if not resolution.members:
            raise CorporateFundamentalsError("PLAN_REQUIRES_AT_LEAST_ONE_RESOLVED_MEMBER")
        created = _aware(created_at, "PLAN_CREATED_AT_INVALID")
        payload = (
            CORPORATE_EVIDENCE_COHORT_PLAN_V1,
            cohort_policy_id,
            cohort_policy_hash,
            tuple((item.stable_security_id, item.cik, item.symbol) for item in resolution.members),
            resolution.excluded_missing_cik,
            resolution.excluded_unresolved,
            bounds.bounds_id,
        )
        return cls(
            plan_id=deterministic_id("corporate_evidence_cohort_plan", payload),
            cohort_policy_id=cohort_policy_id,
            cohort_policy_hash=cohort_policy_hash,
            members=resolution.members,
            excluded_missing_cik=resolution.excluded_missing_cik,
            excluded_unresolved=resolution.excluded_unresolved,
            bounds=bounds,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


class IssuerCaptureStatus(StrEnum):
    CAPTURED = "CAPTURED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED_BUDGET_EXHAUSTED = "SKIPPED_BUDGET_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class IssuerCaptureOutcome:
    stable_security_id: str
    cik: str
    status: IssuerCaptureStatus
    filings_retained: int
    facts_retained: int
    submissions_bytes: int
    companyfacts_bytes: int
    request_count: int
    reasons: tuple[str, ...]
    source_receipt_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorporateEvidenceCaptureCheckpoint:
    checkpoint_id: str
    plan_id: str
    run_id: str
    outcomes: tuple[IssuerCaptureOutcome, ...]
    cumulative_processed_ciks: tuple[str, ...]
    total_requests_this_run: int
    total_bytes_this_run: int
    runtime_seconds: Decimal
    complete: bool
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        plan: CorporateEvidenceCohortPlan,
        run_id: str,
        outcomes: tuple[IssuerCaptureOutcome, ...],
        previously_processed_ciks: tuple[str, ...],
        total_requests_this_run: int,
        total_bytes_this_run: int,
        runtime_seconds: Decimal,
        created_at: datetime,
    ) -> CorporateEvidenceCaptureCheckpoint:
        if not run_id:
            raise CorporateFundamentalsError("CHECKPOINT_RUN_ID_REQUIRED")
        if total_requests_this_run < 0 or total_bytes_this_run < 0:
            raise CorporateFundamentalsError("CHECKPOINT_COUNTS_INVALID")
        created = _aware(created_at, "CHECKPOINT_CREATED_AT_INVALID")
        newly_attempted = tuple(
            item.cik
            for item in outcomes
            if item.status is not IssuerCaptureStatus.SKIPPED_BUDGET_EXHAUSTED
        )
        cumulative = tuple(sorted(set(previously_processed_ciks) | set(newly_attempted)))
        complete = set(member.cik for member in plan.members) <= set(cumulative)
        payload = (
            CORPORATE_EVIDENCE_CAPTURE_CHECKPOINT_V1,
            plan.plan_id,
            run_id,
            tuple(
                (
                    item.stable_security_id,
                    item.status,
                    item.filings_retained,
                    item.facts_retained,
                    item.request_count,
                )
                for item in outcomes
            ),
            cumulative,
            total_requests_this_run,
            total_bytes_this_run,
            str(runtime_seconds),
            complete,
        )
        return cls(
            checkpoint_id=deterministic_id("corporate_evidence_capture_checkpoint", payload),
            plan_id=plan.plan_id,
            run_id=run_id,
            outcomes=outcomes,
            cumulative_processed_ciks=cumulative,
            total_requests_this_run=total_requests_this_run,
            total_bytes_this_run=total_bytes_this_run,
            runtime_seconds=runtime_seconds,
            complete=complete,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def pending_members(
    plan: CorporateEvidenceCohortPlan, *, processed_ciks: Sequence[str]
) -> tuple[CorporateEvidenceCohortMember, ...]:
    processed = set(processed_ciks)
    return tuple(member for member in plan.members if member.cik not in processed)


def select_run_batch(
    pending: Sequence[CorporateEvidenceCohortMember], *, bounds: CorporateEvidenceCaptureBounds
) -> tuple[CorporateEvidenceCohortMember, ...]:
    return tuple(pending[: bounds.max_issuers_per_run])


# ---------------------------------------------------------------------------
# Period-gap / parse-anomaly aggregation helpers -- feed the data-quality
# report (section 17); pure functions over already-retained evidence.
# ---------------------------------------------------------------------------

_PERIOD_GAP_THRESHOLD_DAYS = 130


@dataclass(frozen=True, slots=True)
class FilingPeriodGap:
    """One flagged missing core (10-K/10-Q, either amendment state) report
    period for one issuer -- ordinary quarterly cadence is ~90-95 days
    apart, so a gap exceeding ``_PERIOD_GAP_THRESHOLD_DAYS`` between two
    *retained* report periods is genuinely more than filing-date jitter.
    Flags only; never fills, estimates, or otherwise invents the missing
    period (mirrors ``detect_fact_anomalies``/``assert_filing_dates_
    ordered``'s identical "flag, don't correct source data" convention)."""

    stable_security_id: str
    previous_report_period: str
    next_report_period: str
    gap_days: int


def detect_filing_period_gaps(
    filings: Sequence[CorporateFilingEvent],
) -> tuple[FilingPeriodGap, ...]:
    by_issuer: dict[str, set[str]] = {}
    for item in filings:
        if not item.report_period:
            continue
        if base_form_for(item.form) not in _CORE_ANNUAL_QUARTERLY_FORMS:
            continue
        by_issuer.setdefault(item.stable_security_id, set()).add(item.report_period)
    gaps: list[FilingPeriodGap] = []
    for stable_security_id, periods in by_issuer.items():
        ordered = sorted(periods)
        for previous, following in pairwise(ordered):
            gap_days = (date.fromisoformat(following) - date.fromisoformat(previous)).days
            if gap_days > _PERIOD_GAP_THRESHOLD_DAYS:
                gaps.append(FilingPeriodGap(stable_security_id, previous, following, gap_days))
    return tuple(
        sorted(gaps, key=lambda item: (item.stable_security_id, item.previous_report_period))
    )


def _extract_reason_total(reasons: Sequence[str], *, prefix: str) -> int:
    """Sums the trailing integer off every ``reasons`` entry starting with
    ``prefix`` -- the identical ``"REASON_CODE:count"`` convention this
    module's capture path already writes (e.g. ``SUBMISSIONS_MALFORMED_
    ROWS:3``, ``COMPANYFACTS_UNMAPPED_FISCAL_PERIODS:161``); a genuinely
    malformed suffix is skipped, never mis-parsed into a wrong count."""
    total = 0
    for reason in reasons:
        if reason.startswith(prefix):
            suffix = reason[len(prefix) :]
            if suffix.isdigit():
                total += int(suffix)
    return total


# ---------------------------------------------------------------------------
# Data quality report (section 17)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssuerQualityAssessment:
    stable_security_id: str
    symbol: str
    cik: str
    quality: IssuerDataQuality
    reasons: tuple[str, ...]
    filings_retained_count: int
    facts_retained_count: int
    forms_present: tuple[str, ...]
    concept_coverage_count: int
    has_10k: bool
    has_10q: bool
    has_8k: bool


def assess_issuer_quality(
    *,
    stable_security_id: str,
    symbol: str,
    cik: str,
    filings: Sequence[CorporateFilingEvent],
    facts: Sequence[FundamentalFact],
) -> IssuerQualityAssessment:
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id=stable_security_id,
        cik=cik,
        as_of=_FAR_FUTURE_AS_OF,
        filings=filings,
        facts=facts,
    )
    forms_present = tuple(sorted({item.form.value for item in filings}))
    concept_coverage = sum(1 for field in snapshot.fields if field.value is not None)
    return IssuerQualityAssessment(
        stable_security_id=stable_security_id,
        symbol=symbol,
        cik=cik,
        quality=snapshot.quality,
        reasons=snapshot.quality_reasons,
        filings_retained_count=len(filings),
        facts_retained_count=len(facts),
        forms_present=forms_present,
        concept_coverage_count=concept_coverage,
        has_10k=any(item.form in {FilingForm.FORM_10K, FilingForm.FORM_10K_A} for item in filings),
        has_10q=any(item.form in {FilingForm.FORM_10Q, FilingForm.FORM_10Q_A} for item in filings),
        has_8k=any(item.form in {FilingForm.FORM_8K, FilingForm.FORM_8K_A} for item in filings),
    )


UNIT_MISMATCH_REASON_PREFIX = "COMPANYFACTS_ALTERNATE_UNIT_ENTRIES:"


@dataclass(frozen=True, slots=True)
class CorporateDataQualityReport:
    report_id: str
    plan_id: str
    issuers_requested: int
    issuers_captured: int
    issuers_missing_cik: tuple[str, ...]
    issuers_not_yet_attempted: tuple[str, ...]
    issuers_attempted_zero_evidence: tuple[str, ...]
    forms_found: tuple[tuple[str, int], ...]
    concept_coverage: tuple[tuple[str, int], ...]
    amendment_count: int
    restatement_incidence_count: int
    quality_state_counts: tuple[tuple[str, int], ...]
    period_gap_count: int
    unit_mismatch_count: int
    point_in_time_completeness: str
    issuer_assessments: tuple[IssuerQualityAssessment, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        plan: CorporateEvidenceCohortPlan,
        issuer_assessments: tuple[IssuerQualityAssessment, ...],
        all_filings: Sequence[CorporateFilingEvent],
        all_facts: Sequence[FundamentalFact],
        all_outcomes: Sequence[IssuerCaptureOutcome] = (),
        processed_ciks: Sequence[str] = (),
        checkpoint_complete: bool,
        created_at: datetime,
    ) -> CorporateDataQualityReport:
        created = _aware(created_at, "QUALITY_REPORT_CREATED_AT_INVALID")
        form_counts: dict[str, int] = {}
        for filing in all_filings:
            form_counts[filing.form.value] = form_counts.get(filing.form.value, 0) + 1
        concept_counts: dict[str, int] = {}
        issuers_by_concept: dict[str, set[str]] = {}
        for fact in all_facts:
            issuers_by_concept.setdefault(fact.concept.value, set()).add(fact.stable_security_id)
        for concept, issuers in issuers_by_concept.items():
            concept_counts[concept] = len(issuers)
        restatement_groups: dict[tuple[str, str, int, str], set[str]] = {}
        for fact in all_facts:
            key = (
                fact.stable_security_id,
                fact.concept.value,
                fact.fiscal_year,
                fact.fiscal_period.value,
            )
            restatement_groups.setdefault(key, set()).add(fact.accession_number)
        restatement_incidence = sum(
            1 for accessions in restatement_groups.values() if len(accessions) > 1
        )
        amendment_count = sum(
            1 for filing in all_filings if filing.amendment_state is AmendmentState.AMENDMENT
        )
        issuers_captured = sum(
            1
            for item in issuer_assessments
            if item.filings_retained_count or item.facts_retained_count
        )
        # Three-way, non-overlapping partition of "why doesn't this issuer
        # have a usable snapshot yet" (section 17): ``issuers_missing_cik``
        # (below) is resolved entirely from ``plan.excluded_missing_cik`` --
        # symbols never even reaching ``plan.members`` because no CIK was
        # ever retained for them, i.e. deliberately out of scope, not a
        # capture failure. Of the CIK-resolved ``plan.members`` that
        # remain: a member whose CIK the checkpoint machinery has never
        # attempted is "capture incomplete"; one it *has* attempted but
        # retained zero filings and zero facts for (e.g. a genuine ETF
        # structure with no XBRL company-facts) is "evidence genuinely
        # unavailable" -- never conflated with either of the other two.
        processed = set(processed_ciks)
        not_yet_attempted = tuple(
            sorted(member.symbol for member in plan.members if member.cik not in processed)
        )
        attempted_zero_evidence = tuple(
            sorted(
                item.symbol
                for item in issuer_assessments
                if item.cik in processed
                and not item.filings_retained_count
                and not item.facts_retained_count
            )
        )
        quality_counts: dict[str, int] = {}
        for item in issuer_assessments:
            quality_counts[item.quality.value] = quality_counts.get(item.quality.value, 0) + 1
        period_gaps = detect_filing_period_gaps(all_filings)
        unit_mismatch_total = _extract_reason_total(
            tuple(reason for outcome in all_outcomes for reason in outcome.reasons),
            prefix=UNIT_MISMATCH_REASON_PREFIX,
        )
        payload = (
            CORPORATE_DATA_QUALITY_REPORT_V1,
            plan.plan_id,
            len(plan.members),
            issuers_captured,
            plan.excluded_missing_cik,
            not_yet_attempted,
            attempted_zero_evidence,
            tuple(sorted(form_counts.items())),
            tuple(sorted(concept_counts.items())),
            amendment_count,
            restatement_incidence,
            tuple(sorted(quality_counts.items())),
            len(period_gaps),
            unit_mismatch_total,
            checkpoint_complete,
        )
        return cls(
            report_id=deterministic_id("corporate_data_quality_report", payload),
            plan_id=plan.plan_id,
            issuers_requested=len(plan.members),
            issuers_captured=issuers_captured,
            issuers_missing_cik=plan.excluded_missing_cik,
            issuers_not_yet_attempted=not_yet_attempted,
            issuers_attempted_zero_evidence=attempted_zero_evidence,
            forms_found=tuple(sorted(form_counts.items())),
            concept_coverage=tuple(sorted(concept_counts.items())),
            amendment_count=amendment_count,
            restatement_incidence_count=restatement_incidence,
            quality_state_counts=tuple(sorted(quality_counts.items())),
            period_gap_count=len(period_gaps),
            unit_mismatch_count=unit_mismatch_total,
            point_in_time_completeness="COMPLETE" if checkpoint_complete else "PARTIAL",
            issuer_assessments=issuer_assessments,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ---------------------------------------------------------------------------
# Lookback policy enforcement (milestone: "Corporate Evidence Completion and
# Replay Scaling v1", section 2) -- enforced *only* at the dataset-snapshot
# projection layer below, never at capture/save time and never by deleting
# a retained row. ``CorporateEvidenceCaptureBounds.lookback_years`` was
# already declared (v1) but never consulted anywhere; this is what makes it
# active. Every filing/fact this module ever retains stays retained
# forever, exactly as captured -- immutable, append-only, unbounded by
# design, so raw evidence keeps serving as an honest historical record and
# a re-run with a wider (or narrower) future policy never has to re-fetch
# anything from SEC. What *is* bounded is the served research artifact: the
# ``CorporateHistoryDatasetSnapshot`` a replay/hypothesis actually reads
# from includes only evidence inside the declared lookback window.
# ---------------------------------------------------------------------------

#: Exactly one further year of evidence is retained below the nominal
#: cutoff so a fact reporting the oldest in-window fiscal period can still
#: resolve its prior-year-same-period comparison
#: (``_prior_year_value``/``compute_derived_features``) -- "except where an
#: older fact is genuinely required to derive a boundary-period
#: comparison." Never more than one year: that would silently widen the
#: declared bound for every other purpose.
LOOKBACK_PREDECESSOR_YEARS = 1


def _shift_years(value: date, *, years: int) -> date:
    """Calendar-year subtraction with the standard Feb-29 -> Feb-28
    fallback for a target year without a leap day."""
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


@dataclass(frozen=True, slots=True)
class LookbackWindow:
    """The declared ``lookback_years`` bound resolved against one concrete
    ``reference_date`` -- always the dataset snapshot's own ``created_at``
    date (never "today" at read time), so replaying an identical
    materialization later stays deterministic.

    Two dates, two different jobs:

    * ``cutoff_date`` -- the *nominal* bounded-dataset horizon: what a
      caller asking "how far back does this declared-bounded dataset
      reach" should be told. ``reference_date`` minus ``lookback_years``
      calendar years.
    * ``retention_floor_date`` -- the *actual* inclusion floor the dataset
      projection filters evidence against: ``cutoff_date`` minus
      ``LOOKBACK_PREDECESSOR_YEARS`` further year. A fact/filing whose
      fiscal period falls in ``[retention_floor_date, cutoff_date)`` is
      retained in the bounded snapshot *only* to serve as a YoY
      predecessor-period comparator for the oldest in-window period -- see
      ``LookbackPolicyResult.predecessor_only_fact_ids``.
    """

    lookback_years: int
    reference_date: str
    cutoff_date: str
    retention_floor_date: str
    canonical_hash: str

    @classmethod
    def create(cls, *, lookback_years: int, reference_date: date) -> LookbackWindow:
        if not 1 <= lookback_years <= 30:
            raise CorporateFundamentalsError("LOOKBACK_WINDOW_YEARS_INVALID")
        cutoff = _shift_years(reference_date, years=lookback_years)
        floor = _shift_years(cutoff, years=LOOKBACK_PREDECESSOR_YEARS)
        payload = (
            LOOKBACK_WINDOW_V1,
            lookback_years,
            reference_date.isoformat(),
            cutoff.isoformat(),
            floor.isoformat(),
        )
        return cls(
            lookback_years=lookback_years,
            reference_date=reference_date.isoformat(),
            cutoff_date=cutoff.isoformat(),
            retention_floor_date=floor.isoformat(),
            canonical_hash=content_digest(payload),
        )


def _fact_period_date(fact: FundamentalFact) -> date:
    return date.fromisoformat(fact.period_end)


def _filing_horizon_date(filing: CorporateFilingEvent) -> date:
    """The filing's own *report period* when retained, else its filed
    date -- deliberately never ``accepted_at``/``filed_at``-as-visibility.
    Point-in-time *visibility* (when a filing genuinely became knowable) is
    a completely separate question already answered everywhere else in
    this module by ``effective_available_at``; this function instead
    answers "which fiscal horizon does this filing belong to," the
    question the lookback policy actually bounds."""
    if filing.report_period:
        return date.fromisoformat(filing.report_period)
    return filing.filed_at.date()


@dataclass(frozen=True, slots=True)
class LookbackPolicyResult:
    window: LookbackWindow
    included_filings: tuple[CorporateFilingEvent, ...]
    included_facts: tuple[FundamentalFact, ...]
    predecessor_only_fact_ids: tuple[str, ...]
    excluded_filing_count: int
    excluded_fact_count: int
    canonical_hash: str


def apply_lookback_policy(
    *,
    filings: Sequence[CorporateFilingEvent],
    facts: Sequence[FundamentalFact],
    window: LookbackWindow,
) -> LookbackPolicyResult:
    """Pure, read-side filter over already-retained evidence -- never
    deletes, mutates, or re-fetches a row.

    Fiscal-period versus filing-date filtering: facts are filtered by their
    own ``period_end`` (the fiscal period they *report*, via
    ``_fact_period_date``) and filings by ``report_period`` when retained
    else ``filed_at`` (via ``_filing_horizon_date``) -- never by
    ``accepted_at``/point-in-time visibility, which is a different concern
    this function leaves completely untouched. The lookback policy bounds
    how far back in *economic history* the served dataset reaches, not how
    recently something was filed or became visible.

    Amendment/restatement behavior around the boundary: an amendment
    (``10-K/A``/``10-Q/A``) or a restated fact is included on the identical
    rule as its original -- by the period it *reports*, never by its own
    (necessarily later) filing/acceptance date. A restatement of an
    in-window period stays in-window no matter how recently it was filed;
    a restatement of an out-of-window period stays excluded even if filed
    yesterday. Point-in-time *visibility* of whichever revisions end up
    included is entirely unaffected -- ``resolve_fact_as_of``/
    ``revision_chain`` run identically over whatever subset a caller passes
    them.
    """
    floor = date.fromisoformat(window.retention_floor_date)
    cutoff = date.fromisoformat(window.cutoff_date)
    included_filings = tuple(item for item in filings if _filing_horizon_date(item) >= floor)
    included_facts: list[FundamentalFact] = []
    predecessor_only: list[str] = []
    for item in facts:
        period = _fact_period_date(item)
        if period < floor:
            continue
        included_facts.append(item)
        if period < cutoff:
            predecessor_only.append(item.fact_id)
    payload = (
        window.canonical_hash,
        tuple(sorted(item.filing_event_id for item in included_filings)),
        tuple(sorted(item.fact_id for item in included_facts)),
    )
    return LookbackPolicyResult(
        window=window,
        included_filings=included_filings,
        included_facts=tuple(included_facts),
        predecessor_only_fact_ids=tuple(sorted(predecessor_only)),
        excluded_filing_count=len(filings) - len(included_filings),
        excluded_fact_count=len(facts) - len(included_facts),
        canonical_hash=content_digest(payload),
    )


# ---------------------------------------------------------------------------
# Fundamental historical dataset snapshot (section 13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorporateHistoryDatasetSnapshot:
    dataset_id: str
    plan_id: str
    checkpoint_ids: tuple[str, ...]
    stable_security_ids: tuple[str, ...]
    filing_event_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]
    concept_vocabulary_version: str
    earliest_report_period: str | None
    latest_report_period: str | None
    earliest_filed_at: datetime | None
    latest_filed_at: datetime | None
    quality_report_id: str
    completeness: str
    source_receipt_hashes: tuple[str, ...]
    lookback_years: int
    lookback_cutoff_date: str
    lookback_retention_floor_date: str
    lookback_excluded_filing_count: int
    lookback_excluded_fact_count: int
    lookback_predecessor_only_fact_count: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        plan: CorporateEvidenceCohortPlan,
        checkpoint_ids: tuple[str, ...],
        filings: Sequence[CorporateFilingEvent],
        facts: Sequence[FundamentalFact],
        quality_report: CorporateDataQualityReport,
        source_receipt_hashes: tuple[str, ...],
        created_at: datetime,
        lookback_years: int | None = None,
    ) -> CorporateHistoryDatasetSnapshot:
        if not checkpoint_ids:
            raise CorporateFundamentalsError("DATASET_REQUIRES_AT_LEAST_ONE_CHECKPOINT")
        created = _aware(created_at, "DATASET_CREATED_AT_INVALID")
        window = LookbackWindow.create(
            lookback_years=lookback_years or plan.bounds.lookback_years,
            reference_date=created.date(),
        )
        bounded = apply_lookback_policy(filings=filings, facts=facts, window=window)
        filings = bounded.included_filings
        facts = bounded.included_facts
        stable_ids = tuple(
            sorted(
                {item.stable_security_id for item in filings}
                | {item.stable_security_id for item in facts}
            )
        )
        filing_ids = tuple(sorted({item.filing_event_id for item in filings}))
        fact_ids = tuple(sorted({item.fact_id for item in facts}))
        report_periods = sorted({item.report_period for item in filings if item.report_period})
        filed_ats = sorted(item.filed_at for item in filings) if filings else []
        payload = (
            CORPORATE_HISTORY_DATASET_V1,
            plan.plan_id,
            tuple(sorted(checkpoint_ids)),
            stable_ids,
            filing_ids,
            fact_ids,
            CONCEPT_VOCABULARY_VERSION,
            quality_report.report_id,
            quality_report.point_in_time_completeness,
            tuple(sorted(source_receipt_hashes)),
            window.canonical_hash,
        )
        return cls(
            dataset_id=deterministic_id("corporate_history_dataset_snapshot", payload),
            plan_id=plan.plan_id,
            checkpoint_ids=tuple(sorted(checkpoint_ids)),
            stable_security_ids=stable_ids,
            filing_event_ids=filing_ids,
            fact_ids=fact_ids,
            concept_vocabulary_version=CONCEPT_VOCABULARY_VERSION,
            earliest_report_period=report_periods[0] if report_periods else None,
            latest_report_period=report_periods[-1] if report_periods else None,
            earliest_filed_at=filed_ats[0] if filed_ats else None,
            latest_filed_at=filed_ats[-1] if filed_ats else None,
            quality_report_id=quality_report.report_id,
            completeness=quality_report.point_in_time_completeness,
            source_receipt_hashes=tuple(sorted(source_receipt_hashes)),
            lookback_years=window.lookback_years,
            lookback_cutoff_date=window.cutoff_date,
            lookback_retention_floor_date=window.retention_floor_date,
            lookback_excluded_filing_count=bounded.excluded_filing_count,
            lookback_excluded_fact_count=bounded.excluded_fact_count,
            lookback_predecessor_only_fact_count=len(bounded.predecessor_only_fact_ids),
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ---------------------------------------------------------------------------
# Historical Replay integration seam (sections 14, 15) -- purely additive:
# no field of ``ReplayFeatureFrame``/``HistoricalReplayEngine`` is read or
# changed by this module.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayCorporateEvidenceAttachment:
    attachment_id: str
    replay_run_id: str
    frame_id: str
    stable_security_id: str
    as_of: datetime
    snapshot_hash: str
    snapshot_quality: IssuerDataQuality
    fact_lineage: tuple[str, ...]
    filing_lineage: tuple[str, ...]
    feature_hashes: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        replay_run_id: str,
        frame_id: str,
        snapshot: CorporateFundamentalSnapshot,
        features: Sequence[DerivedFundamentalFeature],
        created_at: datetime,
    ) -> ReplayCorporateEvidenceAttachment:
        created = _aware(created_at, "ATTACHMENT_CREATED_AT_INVALID")
        # Defense in depth: the snapshot builder already filters to
        # ``effective_available_at <= as_of``; re-assert it here so a wiring
        # mistake upstream cannot silently attach lookahead evidence to a
        # replay frame.
        for field in snapshot.fields:
            if field.accepted_at is not None and field.accepted_at > snapshot.as_of:
                raise CorporateFundamentalsError("ATTACHMENT_FACT_ACCEPTED_AFTER_AS_OF")
            if (
                field.accepted_at is None
                and field.filed_at is not None
                and field.filed_at > snapshot.as_of
            ):
                raise CorporateFundamentalsError("ATTACHMENT_FACT_FILED_AFTER_AS_OF")
        feature_hashes = tuple(sorted(item.canonical_hash for item in features))
        payload = (
            REPLAY_CORPORATE_EVIDENCE_ATTACHMENT_V1,
            replay_run_id,
            frame_id,
            snapshot.stable_security_id,
            snapshot.as_of,
            snapshot.canonical_hash,
            snapshot.quality,
            snapshot.fact_lineage,
            snapshot.filing_lineage,
            feature_hashes,
        )
        return cls(
            attachment_id=deterministic_id("replay_corporate_evidence_attachment", payload),
            replay_run_id=replay_run_id,
            frame_id=frame_id,
            stable_security_id=snapshot.stable_security_id,
            as_of=snapshot.as_of,
            snapshot_hash=snapshot.canonical_hash,
            snapshot_quality=snapshot.quality,
            fact_lineage=snapshot.fact_lineage,
            filing_lineage=snapshot.filing_lineage,
            feature_hashes=feature_hashes,
            created_at=created,
            canonical_hash=content_digest(payload),
        )
