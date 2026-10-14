"""Orchestrates one genuine, bounded, checkpointed SEC corporate-evidence
capture run (Corporate Fundamental, Filing, and Event Intelligence v1).

Composes ``SecXbrlFilingsClient`` (real network I/O) with the pure domain
capture-plan/checkpoint machinery (``domain.corporate_fundamentals``) and
the durable repository -- mirroring exactly how ``adapters.
options_research_capture.AlpacaOptionsResearchEvidenceCapture`` composes a
provider-specific client with already-reviewed application/domain services.
No Order, Strategy, Opportunity, PAPER, or LIVE authority is ever created
here; every persisted fact keeps ``authority = "NONE"``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from apex.adapters.corporate_filings_source import (
    SecFilingsSourceCapabilityUnavailable,
    SecFilingsSourceError,
    SecXbrlFilingsClient,
    parse_sec_companyfacts,
    parse_sec_submissions,
    parse_sec_submissions_page,
)
from apex.application.corporate_fundamentals import CorporateFundamentalsRepository
from apex.domain.corporate_fundamentals import (
    UNIT_MISMATCH_REASON_PREFIX,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortMember,
    CorporateEvidenceCohortPlan,
    IssuerCaptureOutcome,
    IssuerCaptureStatus,
    pending_members,
    select_run_batch,
)
from apex.domain.values import deterministic_id


class CorporateEvidenceCaptureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CorporateEvidenceCaptureCommand:
    plan: CorporateEvidenceCohortPlan
    now: datetime
    user_agent: str


class SecCorporateEvidenceCapture:
    """Runs one bounded batch of the plan's still-pending issuers: for each,
    fetches ``submissions`` (plus up to ``MAX_ADDITIONAL_SUBMISSION_PAGES``
    bounded overflow pages), then ``companyfacts``, persists every parsed
    filing/fact (idempotently -- the repository de-duplicates by content-
    addressed id), and folds every issuer's outcome into one retained
    checkpoint. Stops issuing new requests once the plan's bounds are
    exhausted; an issuer skipped for that reason is retained honestly as
    ``SKIPPED_BUDGET_EXHAUSTED``, never silently dropped.
    """

    def __init__(
        self,
        *,
        repository: CorporateFundamentalsRepository,
        client: SecXbrlFilingsClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._client = client or SecXbrlFilingsClient()
        self._sleep = sleep

    def run(self, command: CorporateEvidenceCaptureCommand) -> CorporateEvidenceCaptureCheckpoint:
        plan = command.plan
        bounds = plan.bounds
        previously_processed = self._repository.processed_ciks(plan.plan_id)
        pending = pending_members(plan, processed_ciks=previously_processed)
        batch = select_run_batch(pending, bounds=bounds)

        run_id = deterministic_id(
            "corporate_evidence_capture_run", (plan.plan_id, command.now.isoformat())
        )
        outcomes: list[IssuerCaptureOutcome] = []
        total_requests = 0
        total_bytes = 0
        started = time.monotonic()

        for member in batch:
            elapsed = time.monotonic() - started
            if (
                total_requests >= bounds.max_requests_per_run
                or total_bytes >= bounds.max_bytes_per_run
                or elapsed >= bounds.max_runtime_seconds
            ):
                outcomes.append(
                    IssuerCaptureOutcome(
                        stable_security_id=member.stable_security_id,
                        cik=member.cik,
                        status=IssuerCaptureStatus.SKIPPED_BUDGET_EXHAUSTED,
                        filings_retained=0,
                        facts_retained=0,
                        submissions_bytes=0,
                        companyfacts_bytes=0,
                        request_count=0,
                        reasons=("RUN_BUDGET_EXHAUSTED",),
                        source_receipt_ids=(),
                    )
                )
                continue
            outcome = self._capture_issuer(
                member,
                min_request_interval_seconds=float(bounds.min_request_interval_seconds),
                user_agent=command.user_agent,
            )
            outcomes.append(outcome)
            total_requests += outcome.request_count
            total_bytes += outcome.submissions_bytes + outcome.companyfacts_bytes
            self._sleep(float(bounds.min_request_interval_seconds))

        runtime_seconds = round(time.monotonic() - started, 3)
        checkpoint = CorporateEvidenceCaptureCheckpoint.create(
            plan=plan,
            run_id=run_id,
            outcomes=tuple(outcomes),
            previously_processed_ciks=previously_processed,
            total_requests_this_run=total_requests,
            total_bytes_this_run=total_bytes,
            runtime_seconds=Decimal(str(runtime_seconds)),
            created_at=command.now,
        )
        return self._repository.save_checkpoint(checkpoint)

    def _capture_issuer(
        self,
        member: CorporateEvidenceCohortMember,
        *,
        min_request_interval_seconds: float,
        user_agent: str,
    ) -> IssuerCaptureOutcome:
        requests = 0
        submissions_bytes = 0
        companyfacts_bytes = 0
        receipts: list[str] = []
        reasons: list[str] = []

        try:
            submissions_capture = self._client.fetch_submissions(
                cik=member.cik, user_agent=user_agent
            )
        except (SecFilingsSourceCapabilityUnavailable, SecFilingsSourceError) as error:
            return IssuerCaptureOutcome(
                stable_security_id=member.stable_security_id,
                cik=member.cik,
                status=IssuerCaptureStatus.FAILED,
                filings_retained=0,
                facts_retained=0,
                submissions_bytes=0,
                companyfacts_bytes=0,
                request_count=1,
                reasons=(f"SUBMISSIONS_UNAVAILABLE:{type(error).__name__}",),
                source_receipt_ids=(),
            )
        requests += 1
        submissions_bytes += submissions_capture.byte_count
        receipts.append(submissions_capture.raw_sha256)
        parsed_submissions = parse_sec_submissions(
            payload=submissions_capture.body,
            stable_security_id=member.stable_security_id,
            cik=member.cik,
        )
        all_filings = list(parsed_submissions.filings)
        accepted_at_by_accession = dict(parsed_submissions.accepted_at_by_accession)
        if parsed_submissions.malformed_entry_count:
            reasons.append(f"SUBMISSIONS_MALFORMED_ROWS:{parsed_submissions.malformed_entry_count}")

        for file_name in parsed_submissions.additional_page_files:
            self._sleep(min_request_interval_seconds)
            try:
                page_capture = self._client.fetch_submissions_page(
                    file_name=file_name, user_agent=user_agent
                )
            except (SecFilingsSourceCapabilityUnavailable, SecFilingsSourceError):
                reasons.append(f"SUBMISSIONS_PAGE_UNAVAILABLE:{file_name}")
                continue
            requests += 1
            submissions_bytes += page_capture.byte_count
            receipts.append(page_capture.raw_sha256)
            page_result = parse_sec_submissions_page(
                payload=page_capture.body,
                stable_security_id=member.stable_security_id,
                cik=member.cik,
            )
            all_filings.extend(page_result.filings)
            accepted_at_by_accession.update(page_result.accepted_at_by_accession)

        saved_filings = tuple(self._repository.save_filing(item) for item in all_filings)

        self._sleep(min_request_interval_seconds)
        try:
            companyfacts_capture = self._client.fetch_companyfacts(
                cik=member.cik, user_agent=user_agent
            )
        except (SecFilingsSourceCapabilityUnavailable, SecFilingsSourceError) as error:
            reasons.append(f"COMPANYFACTS_UNAVAILABLE:{type(error).__name__}")
            return IssuerCaptureOutcome(
                stable_security_id=member.stable_security_id,
                cik=member.cik,
                status=IssuerCaptureStatus.PARTIAL,
                filings_retained=len(saved_filings),
                facts_retained=0,
                submissions_bytes=submissions_bytes,
                companyfacts_bytes=0,
                request_count=requests,
                reasons=tuple(reasons),
                source_receipt_ids=tuple(receipts),
            )
        requests += 1
        companyfacts_bytes += companyfacts_capture.byte_count
        receipts.append(companyfacts_capture.raw_sha256)
        parsed_facts = parse_sec_companyfacts(
            payload=companyfacts_capture.body,
            stable_security_id=member.stable_security_id,
            cik=member.cik,
            accepted_at_by_accession=accepted_at_by_accession,
        )
        if parsed_facts.malformed_entry_count:
            reasons.append(f"COMPANYFACTS_MALFORMED_ENTRIES:{parsed_facts.malformed_entry_count}")
        if parsed_facts.unmapped_fiscal_period_count:
            reasons.append(
                f"COMPANYFACTS_UNMAPPED_FISCAL_PERIODS:{parsed_facts.unmapped_fiscal_period_count}"
            )
        if parsed_facts.alternate_unit_entry_count:
            reasons.append(
                f"{UNIT_MISMATCH_REASON_PREFIX}{parsed_facts.alternate_unit_entry_count}"
            )
        saved_facts = tuple(self._repository.save_fact(item) for item in parsed_facts.facts)

        return IssuerCaptureOutcome(
            stable_security_id=member.stable_security_id,
            cik=member.cik,
            status=IssuerCaptureStatus.CAPTURED,
            filings_retained=len(saved_filings),
            facts_retained=len(saved_facts),
            submissions_bytes=submissions_bytes,
            companyfacts_bytes=companyfacts_bytes,
            request_count=requests,
            reasons=tuple(reasons),
            source_receipt_ids=tuple(receipts),
        )
