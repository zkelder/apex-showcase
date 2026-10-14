"""Bounded, unauthenticated SEC EDGAR ``data.sec.gov`` capture clients and
parsers for Corporate Fundamental, Filing, and Event Intelligence v1.

Two official, structured endpoints only -- never filing HTML:

* ``GET https://data.sec.gov/submissions/CIK##########.json`` -- one
  issuer's filing history (forms, accession numbers, filing/acceptance
  timestamps, report periods, primary documents, 8-K item codes), plus
  (for prolific filers) bounded pagination into older ``filings.files``
  pages.
* ``GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` --
  every retained ``us-gaap``/``dei`` structured fact for that issuer; this
  module extracts only the bounded ``CONCEPT_DEFINITIONS`` vocabulary from
  it, never an unbounded warehouse of every tag present.

Both reuse ``apex.adapters.security_reference.validate_sec_user_agent`` --
the identical identifying-User-Agent policy already reviewed for
``www.sec.gov/files/company_tickers.json`` -- rather than inventing a
second SEC access convention. Parsing is pure and independently testable
against fixture-shaped payloads; genuine capture can only target the
allowlisted ``data.sec.gov`` host and the two documented path shapes.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from apex.adapters.security_reference import validate_sec_user_agent
from apex.domain.corporate_fundamentals import (
    CONCEPT_DEFINITIONS,
    RETAINED_FILING_FORMS,
    SEC_UNIT_KEY_BY_FACT_UNIT,
    CorporateFilingEvent,
    CorporateFundamentalsError,
    FundamentalFact,
    fiscal_period_for_sec_fp,
    sec_cik_10digit,
)

SEC_XBRL_DATA_HOST = "data.sec.gov"
SEC_SUBMISSIONS_PATH_PREFIX = "/submissions/"
SEC_COMPANYFACTS_PATH_PREFIX = "/api/xbrl/companyfacts/"
MAX_SUBMISSIONS_BYTES = 8_000_000
MAX_COMPANYFACTS_BYTES = 20_000_000
MAX_TIMEOUT_SECONDS = 30
MAX_ADDITIONAL_SUBMISSION_PAGES = 2


class SecFilingsSourceError(RuntimeError):
    pass


class SecFilingsSourceCapabilityUnavailable(SecFilingsSourceError):
    pass


@dataclass(frozen=True, slots=True)
class SecXbrlHttpResponse:
    status: int
    body: bytes


class SecXbrlTransport(Protocol):
    def get(
        self,
        *,
        host: str,
        path: str,
        user_agent: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> SecXbrlHttpResponse: ...


class HttpsSecXbrlTransport:
    """TLS-verified, one-request transport honoring SEC's identifying UA
    policy, allowlisted to exactly the two ``data.sec.gov`` path shapes
    this module ever requests."""

    def get(
        self,
        *,
        host: str,
        path: str,
        user_agent: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> SecXbrlHttpResponse:
        if host != SEC_XBRL_DATA_HOST or not (
            path.startswith(SEC_SUBMISSIONS_PATH_PREFIX)
            or path.startswith(SEC_COMPANYFACTS_PATH_PREFIX)
        ):
            raise SecFilingsSourceError("SEC_XBRL_SOURCE_NOT_ALLOWLISTED")
        connection = http.client.HTTPSConnection(
            host, port=443, timeout=timeout_seconds, context=ssl.create_default_context()
        )
        try:
            connection.request(
                "GET", path, headers={"Accept": "application/json", "User-Agent": user_agent}
            )
            response = connection.getresponse()
            declared_length = response.getheader("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > maximum_response_bytes:
                        raise SecFilingsSourceError("SEC_XBRL_RESPONSE_TOO_LARGE")
                except ValueError as error:
                    raise SecFilingsSourceError("SEC_XBRL_CONTENT_LENGTH_INVALID") from error
            body = response.read(maximum_response_bytes + 1)
            if len(body) > maximum_response_bytes:
                raise SecFilingsSourceError("SEC_XBRL_RESPONSE_TOO_LARGE")
            return SecXbrlHttpResponse(response.status, body)
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            raise SecFilingsSourceCapabilityUnavailable(
                f"SEC_XBRL_SOURCE_UNAVAILABLE:{type(error).__name__}"
            ) from error
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class SecXbrlCapture:
    """One bounded, genuinely timed HTTP GET's raw evidence: exactly what a
    persistence/quality layer needs to prove how many bytes were read and
    how long the request took, without re-deriving it from the response."""

    body: bytes
    byte_count: int
    raw_sha256: str
    request_seconds: Decimal


class SecXbrlFilingsClient:
    """Genuine bounded capture over the two allowlisted ``data.sec.gov``
    endpoints. Every method performs exactly one HTTP GET; looping across
    issuers or pages is the caller's (orchestrator's) responsibility, so
    the caller -- not this client -- owns rate limiting and run budgets.
    """

    def __init__(self, transport: SecXbrlTransport | None = None) -> None:
        self._transport = transport or HttpsSecXbrlTransport()

    def _get(
        self, *, path: str, user_agent: str, timeout_seconds: int, maximum_response_bytes: int
    ) -> SecXbrlCapture:
        validated_agent = validate_sec_user_agent(user_agent)
        if not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise SecFilingsSourceError("SEC_XBRL_TIMEOUT_BOUNDS_INVALID")
        started = time.monotonic()
        response = self._transport.get(
            host=SEC_XBRL_DATA_HOST,
            path=path,
            user_agent=validated_agent,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise SecFilingsSourceCapabilityUnavailable("SEC_XBRL_CAPTURE_RUNTIME_EXCEEDED")
        if response.status != 200:
            raise SecFilingsSourceCapabilityUnavailable(f"SEC_XBRL_SOURCE_HTTP_{response.status}")
        if not response.body or len(response.body) > maximum_response_bytes:
            raise SecFilingsSourceError("SEC_XBRL_RESPONSE_SIZE_INVALID")
        return SecXbrlCapture(
            body=response.body,
            byte_count=len(response.body),
            raw_sha256=hashlib.sha256(response.body).hexdigest(),
            request_seconds=Decimal(str(round(elapsed, 6))),
        )

    def fetch_submissions(
        self,
        *,
        cik: str,
        user_agent: str,
        timeout_seconds: int = 20,
        maximum_response_bytes: int = MAX_SUBMISSIONS_BYTES,
    ) -> SecXbrlCapture:
        path = f"{SEC_SUBMISSIONS_PATH_PREFIX}CIK{sec_cik_10digit(cik)}.json"
        return self._get(
            path=path,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )

    def fetch_submissions_page(
        self,
        *,
        file_name: str,
        user_agent: str,
        timeout_seconds: int = 20,
        maximum_response_bytes: int = MAX_SUBMISSIONS_BYTES,
    ) -> SecXbrlCapture:
        if "/" in file_name or not file_name.endswith(".json"):
            raise SecFilingsSourceError("SEC_XBRL_SUBMISSIONS_PAGE_NAME_INVALID")
        path = f"{SEC_SUBMISSIONS_PATH_PREFIX}{file_name}"
        return self._get(
            path=path,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )

    def fetch_companyfacts(
        self,
        *,
        cik: str,
        user_agent: str,
        timeout_seconds: int = 30,
        maximum_response_bytes: int = MAX_COMPANYFACTS_BYTES,
    ) -> SecXbrlCapture:
        path = f"{SEC_COMPANYFACTS_PATH_PREFIX}CIK{sec_cik_10digit(cik)}.json"
        return self._get(
            path=path,
            user_agent=user_agent,
            timeout_seconds=timeout_seconds,
            maximum_response_bytes=maximum_response_bytes,
        )


# ---------------------------------------------------------------------------
# Submissions parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmissionsParseResult:
    filings: tuple[CorporateFilingEvent, ...]
    accepted_at_by_accession: dict[str, datetime]
    additional_page_files: tuple[str, ...]
    forms_observed: tuple[tuple[str, int], ...]
    malformed_entry_count: int


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sec_columns(block: dict[str, object], count: int) -> dict[str, list[object]]:
    """Every SEC ``filings.recent``/paginated-page column is a parallel
    array of exactly the row count; a length mismatch is a genuinely
    malformed response, not a row-level concern -- fail closed rather than
    guess at a zip alignment."""
    names = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "form",
        "items",
        "primaryDocument",
        "fileNumber",
        "isXBRL",
        "isInlineXBRL",
    )
    columns: dict[str, list[object]] = {}
    for name in names:
        values = block.get(name, [])
        if not isinstance(values, list) or len(values) != count:
            raise SecFilingsSourceError(f"SEC_SUBMISSIONS_COLUMN_LENGTH_MISMATCH:{name}")
        columns[name] = values
    return columns


def _parse_recent_block(
    block: dict[str, object], *, stable_security_id: str, cik: str
) -> tuple[list[CorporateFilingEvent], dict[str, datetime], dict[str, int], int]:
    accession_numbers = block.get("accessionNumber", [])
    if not isinstance(accession_numbers, list):
        raise SecFilingsSourceError("SEC_SUBMISSIONS_ACCESSION_COLUMN_MISSING")
    columns = _sec_columns(block, len(accession_numbers))
    filings: list[CorporateFilingEvent] = []
    accepted_at_by_accession: dict[str, datetime] = {}
    forms_observed: dict[str, int] = {}
    malformed = 0
    for index in range(len(accession_numbers)):
        accession = columns["accessionNumber"][index]
        form_raw = columns["form"][index]
        if isinstance(form_raw, str) and form_raw:
            forms_observed[form_raw] = forms_observed.get(form_raw, 0) + 1
        if not isinstance(accession, str) or not isinstance(form_raw, str):
            malformed += 1
            continue
        filing_date = columns["filingDate"][index]
        acceptance_raw = columns["acceptanceDateTime"][index]
        accepted_at: datetime | None = None
        if isinstance(acceptance_raw, str) and acceptance_raw:
            try:
                accepted_at = datetime.fromisoformat(acceptance_raw.replace("Z", "+00:00"))
            except ValueError:
                accepted_at = None
        if accepted_at is not None:
            accepted_at_by_accession[accession] = accepted_at
        if form_raw not in RETAINED_FILING_FORMS:
            continue
        if not isinstance(filing_date, str) or not filing_date:
            malformed += 1
            continue
        try:
            filed_at = datetime.fromisoformat(filing_date).replace(tzinfo=UTC)
        except ValueError:
            malformed += 1
            continue
        report_date = columns["reportDate"][index]
        primary_document = columns["primaryDocument"][index]
        file_number = columns["fileNumber"][index]
        items_raw = columns["items"][index]
        items = (
            tuple(sorted({item.strip() for item in items_raw.split(",") if item.strip()}))
            if isinstance(items_raw, str)
            else ()
        )
        is_xbrl = bool(columns["isXBRL"][index])
        is_inline_xbrl = bool(columns["isInlineXBRL"][index])
        if not isinstance(primary_document, str) or not primary_document:
            malformed += 1
            continue
        try:
            filing = CorporateFilingEvent.create(
                stable_security_id=stable_security_id,
                cik=cik,
                accession_number=accession,
                form=form_raw,
                report_period=report_date if isinstance(report_date, str) and report_date else None,
                filed_at=filed_at,
                accepted_at=accepted_at,
                primary_document=primary_document,
                file_number=(file_number if isinstance(file_number, str) and file_number else None),
                items=items,
                is_xbrl=is_xbrl,
                is_inline_xbrl=is_inline_xbrl,
            )
        except CorporateFundamentalsError:
            malformed += 1
            continue
        filings.append(filing)
    return filings, accepted_at_by_accession, forms_observed, malformed


def parse_sec_submissions(
    *, payload: bytes, stable_security_id: str, cik: str
) -> SubmissionsParseResult:
    if not payload or len(payload) > MAX_SUBMISSIONS_BYTES:
        raise SecFilingsSourceError("SEC_SUBMISSIONS_RESPONSE_SIZE_INVALID")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecFilingsSourceError("SEC_SUBMISSIONS_JSON_INVALID") from error
    if not isinstance(decoded, dict):
        raise SecFilingsSourceError("SEC_SUBMISSIONS_OBJECT_REQUIRED")
    reported_cik = decoded.get("cik")
    if reported_cik is not None and str(reported_cik).lstrip("0") != cik.lstrip("0"):
        raise SecFilingsSourceError("SEC_SUBMISSIONS_CIK_MISMATCH")
    filings_block = decoded.get("filings")
    if not isinstance(filings_block, dict):
        raise SecFilingsSourceError("SEC_SUBMISSIONS_FILINGS_BLOCK_MISSING")
    recent = filings_block.get("recent")
    if not isinstance(recent, dict):
        raise SecFilingsSourceError("SEC_SUBMISSIONS_RECENT_BLOCK_MISSING")
    filings, accepted_map, forms_observed, malformed = _parse_recent_block(
        recent, stable_security_id=stable_security_id, cik=cik
    )
    additional_pages: list[str] = []
    for entry in filings_block.get("files", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            additional_pages.append(entry["name"])
    return SubmissionsParseResult(
        filings=tuple(sorted(filings, key=lambda item: item.accession_number)),
        accepted_at_by_accession=accepted_map,
        additional_page_files=tuple(additional_pages[:MAX_ADDITIONAL_SUBMISSION_PAGES]),
        forms_observed=tuple(sorted(forms_observed.items())),
        malformed_entry_count=malformed,
    )


def parse_sec_submissions_page(
    *, payload: bytes, stable_security_id: str, cik: str
) -> SubmissionsParseResult:
    """Parses one bounded ``filings.files[]`` overflow page -- the identical
    parallel-array shape as ``filings.recent`` but at the JSON top level,
    with no further nested pagination of its own."""
    if not payload or len(payload) > MAX_SUBMISSIONS_BYTES:
        raise SecFilingsSourceError("SEC_SUBMISSIONS_PAGE_RESPONSE_SIZE_INVALID")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecFilingsSourceError("SEC_SUBMISSIONS_PAGE_JSON_INVALID") from error
    if not isinstance(decoded, dict):
        raise SecFilingsSourceError("SEC_SUBMISSIONS_PAGE_OBJECT_REQUIRED")
    filings, accepted_map, forms_observed, malformed = _parse_recent_block(
        decoded, stable_security_id=stable_security_id, cik=cik
    )
    return SubmissionsParseResult(
        filings=tuple(sorted(filings, key=lambda item: item.accession_number)),
        accepted_at_by_accession=accepted_map,
        additional_page_files=(),
        forms_observed=tuple(sorted(forms_observed.items())),
        malformed_entry_count=malformed,
    )


# ---------------------------------------------------------------------------
# Company facts parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanyFactsParseResult:
    facts: tuple[FundamentalFact, ...]
    forms_observed: tuple[tuple[str, int], ...]
    unmapped_fiscal_period_count: int
    malformed_entry_count: int
    alternate_unit_entry_count: int


def _trusted_accepted_at(accepted_at: datetime | None, *, filed_at: datetime) -> datetime | None:
    """Genuine SEC evidence has shown a companyfacts entry's own ``filed``
    date and its accession's ``submissions``-sourced acceptance time can
    disagree by more than a day (two independent SEC backend systems for
    the identical accession). Rather than reject an otherwise perfectly
    good fact value over a cross-endpoint timestamp quirk, fall back to
    filed-only point-in-time evidence -- the identical honest degradation
    already used when no acceptance time was ever retained at all -- and
    let ``FundamentalFact.create``'s own invariant stay a hard, un-bypassed
    gate against any genuinely self-contradictory direct input.
    """
    if accepted_at is None or accepted_at >= filed_at - timedelta(days=1):
        return accepted_at
    return None


def parse_sec_companyfacts(
    *,
    payload: bytes,
    stable_security_id: str,
    cik: str,
    accepted_at_by_accession: dict[str, datetime],
) -> CompanyFactsParseResult:
    if not payload or len(payload) > MAX_COMPANYFACTS_BYTES:
        raise SecFilingsSourceError("SEC_COMPANYFACTS_RESPONSE_SIZE_INVALID")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecFilingsSourceError("SEC_COMPANYFACTS_JSON_INVALID") from error
    if not isinstance(decoded, dict):
        raise SecFilingsSourceError("SEC_COMPANYFACTS_OBJECT_REQUIRED")
    reported_cik = decoded.get("cik")
    if reported_cik is not None and str(reported_cik).lstrip("0") != cik.lstrip("0"):
        raise SecFilingsSourceError("SEC_COMPANYFACTS_CIK_MISMATCH")
    # An issuer genuinely without XBRL company facts (e.g. many funds/ETFs)
    # returns no usable "facts" block -- an honest empty result, not an error.
    facts_block = decoded.get("facts")
    if not isinstance(facts_block, dict):
        facts_block = {}

    results: list[FundamentalFact] = []
    forms_observed: dict[str, int] = {}
    unmapped_fp = 0
    malformed = 0
    alternate_unit_entries = 0
    seen_fact_ids: set[str] = set()
    for definition in CONCEPT_DEFINITIONS:
        for taxonomy, tag in definition.source_tags:
            taxonomy_block = facts_block.get(taxonomy)
            if not isinstance(taxonomy_block, dict):
                continue
            concept_block = taxonomy_block.get(tag)
            if not isinstance(concept_block, dict):
                continue
            units_block = concept_block.get("units")
            if not isinstance(units_block, dict):
                continue
            unit_key = SEC_UNIT_KEY_BY_FACT_UNIT[definition.unit]
            # A retained concept/tag genuinely reported under some *other*
            # unit key too (e.g. a one-off "USD-per-share" entry inside an
            # otherwise "USD" revenue block) is real evidence worth
            # counting, even though this module's bounded vocabulary never
            # retains that differently-unit-typed entry itself (section
            # 17's "unit mismatches").
            for other_key, other_entries in units_block.items():
                if other_key != unit_key and isinstance(other_entries, list):
                    alternate_unit_entries += len(other_entries)
            entries = units_block.get(unit_key)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    malformed += 1
                    continue
                form_raw = entry.get("form")
                if isinstance(form_raw, str) and form_raw:
                    forms_observed[form_raw] = forms_observed.get(form_raw, 0) + 1
                if not isinstance(form_raw, str) or form_raw not in RETAINED_FILING_FORMS:
                    continue
                fp_raw = entry.get("fp")
                fiscal_period = (
                    fiscal_period_for_sec_fp(fp_raw) if isinstance(fp_raw, str) else None
                )
                if fiscal_period is None:
                    unmapped_fp += 1
                    continue
                fiscal_year = _int_or_none(entry.get("fy"))
                accession = entry.get("accn")
                period_end = entry.get("end")
                period_start = entry.get("start")
                raw_value = entry.get("val")
                filed_raw = entry.get("filed")
                frame = entry.get("frame")
                if (
                    fiscal_year is None
                    or not isinstance(accession, str)
                    or not isinstance(period_end, str)
                    or not isinstance(filed_raw, str)
                    or not isinstance(raw_value, (int, float))
                    or isinstance(raw_value, bool)
                ):
                    malformed += 1
                    continue
                try:
                    filed_at = datetime.fromisoformat(filed_raw).replace(tzinfo=UTC)
                    fact = FundamentalFact.create(
                        stable_security_id=stable_security_id,
                        cik=cik,
                        concept=definition.concept,
                        source_taxonomy=taxonomy,
                        source_tag=tag,
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        period_start=period_start if isinstance(period_start, str) else None,
                        period_end=period_end,
                        value=Decimal(str(raw_value)),
                        accession_number=accession,
                        form=form_raw,
                        filed_at=filed_at,
                        accepted_at=_trusted_accepted_at(
                            accepted_at_by_accession.get(accession), filed_at=filed_at
                        ),
                        frame=frame if isinstance(frame, str) and frame else None,
                    )
                except (CorporateFundamentalsError, InvalidOperation, ValueError):
                    malformed += 1
                    continue
                if fact.fact_id in seen_fact_ids:
                    continue
                seen_fact_ids.add(fact.fact_id)
                results.append(fact)
    return CompanyFactsParseResult(
        facts=tuple(sorted(results, key=lambda item: item.fact_id)),
        forms_observed=tuple(sorted(forms_observed.items())),
        unmapped_fiscal_period_count=unmapped_fp,
        malformed_entry_count=malformed,
        alternate_unit_entry_count=alternate_unit_entries,
    )
