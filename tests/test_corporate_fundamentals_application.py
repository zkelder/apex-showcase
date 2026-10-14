"""Application-service proofs for Corporate Fundamental, Filing, and Event
Intelligence v1, against an in-memory fake repository. No database, no
server, no network.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.corporate_fundamentals import (
    AttachCorporateEvidenceToReplayFrame,
    AttachCorporateEvidenceToReplayFrameCommand,
    BuildCorporateFundamentalSnapshotAsOf,
    BuildCorporateFundamentalSnapshotCommand,
    BuildCorporateReplayFeatureFrame,
    BuildCorporateReplayFeatureFrameCommand,
    ComputeDerivedFundamentalFeaturesAsOf,
    CorporateFundamentalsApplicationError,
    CorporateFundamentalsNotFound,
    MaterializeCorporateHistoryDataset,
    MaterializeCorporateHistoryDatasetCommand,
    RegisterCorporateEvidenceCohortPlan,
    RegisterCorporateEvidenceCohortPlanCommand,
    resolve_cik_for_security,
)
from apex.domain.corporate_fundamentals import (
    CorporateDataQualityReport,
    CorporateEvidenceCaptureBounds,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortPlan,
    CorporateFilingEvent,
    CorporateHistoryDatasetSnapshot,
    FilingForm,
    FiscalPeriod,
    FundamentalConcept,
    FundamentalFact,
    IssuerCaptureOutcome,
    IssuerCaptureStatus,
    ReplayCorporateEvidenceAttachment,
    build_corporate_fundamental_snapshot,
    compute_derived_features,
)
from apex.domain.historical_replay import ReplayFeatureFrame
from apex.domain.research_cohort import (
    CohortRole,
    CohortSector,
    ResolvedCohort,
    ResolvedCohortMember,
)
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity

UTC_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class InMemoryCorporateFundamentalsRepository:
    """Deliberately minimal in-memory double for
    ``CorporateFundamentalsRepository`` -- exercises the exact same
    application-service contracts a Postgres-backed run would."""

    def __init__(self) -> None:
        self._plans: dict[str, CorporateEvidenceCohortPlan] = {}
        self._filings: dict[str, CorporateFilingEvent] = {}
        self._facts: dict[str, FundamentalFact] = {}
        self._checkpoints: dict[str, list[CorporateEvidenceCaptureCheckpoint]] = {}
        self._quality_reports: dict[str, CorporateDataQualityReport] = {}
        self._datasets: dict[str, CorporateHistoryDatasetSnapshot] = {}
        self._attachments: dict[str, ReplayCorporateEvidenceAttachment] = {}

    def save_plan(self, plan: CorporateEvidenceCohortPlan) -> CorporateEvidenceCohortPlan:
        self._plans.setdefault(plan.plan_id, plan)
        return self._plans[plan.plan_id]

    def plan_by_id(self, plan_id: str) -> CorporateEvidenceCohortPlan | None:
        return self._plans.get(plan_id)

    def latest_plan(self) -> CorporateEvidenceCohortPlan | None:
        ordered = sorted(self._plans.values(), key=lambda item: item.created_at)
        return ordered[-1] if ordered else None

    def save_filing(self, filing: CorporateFilingEvent) -> CorporateFilingEvent:
        self._filings.setdefault(filing.filing_event_id, filing)
        return self._filings[filing.filing_event_id]

    def filings_for(self, stable_security_id: str) -> tuple[CorporateFilingEvent, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._filings.values()
                    if item.stable_security_id == stable_security_id
                ),
                key=lambda item: item.filed_at,
            )
        )

    def filing_by_accession(self, accession_number: str) -> CorporateFilingEvent | None:
        return next(
            (item for item in self._filings.values() if item.accession_number == accession_number),
            None,
        )

    def all_filings_for_plan(self, plan_id: str) -> tuple[CorporateFilingEvent, ...]:
        plan = self._plans.get(plan_id)
        if plan is None:
            return ()
        security_ids = {member.stable_security_id for member in plan.members}
        return tuple(
            item for item in self._filings.values() if item.stable_security_id in security_ids
        )

    def filings_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[CorporateFilingEvent, ...]:
        ids = set(stable_security_ids)
        return tuple(
            sorted(
                (item for item in self._filings.values() if item.stable_security_id in ids),
                key=lambda item: item.filed_at,
            )
        )

    def save_fact(self, fact: FundamentalFact) -> FundamentalFact:
        self._facts.setdefault(fact.fact_id, fact)
        return self._facts[fact.fact_id]

    def facts_for(self, stable_security_id: str) -> tuple[FundamentalFact, ...]:
        return tuple(
            item for item in self._facts.values() if item.stable_security_id == stable_security_id
        )

    def all_facts_for_plan(self, plan_id: str) -> tuple[FundamentalFact, ...]:
        plan = self._plans.get(plan_id)
        if plan is None:
            return ()
        security_ids = {member.stable_security_id for member in plan.members}
        return tuple(
            item for item in self._facts.values() if item.stable_security_id in security_ids
        )

    def facts_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[FundamentalFact, ...]:
        ids = set(stable_security_ids)
        return tuple(item for item in self._facts.values() if item.stable_security_id in ids)

    def save_checkpoint(
        self, checkpoint: CorporateEvidenceCaptureCheckpoint
    ) -> CorporateEvidenceCaptureCheckpoint:
        bucket = self._checkpoints.setdefault(checkpoint.plan_id, [])
        if not any(item.checkpoint_id == checkpoint.checkpoint_id for item in bucket):
            bucket.append(checkpoint)
        return checkpoint

    def checkpoints_for_plan(self, plan_id: str) -> tuple[CorporateEvidenceCaptureCheckpoint, ...]:
        return tuple(self._checkpoints.get(plan_id, ()))

    def latest_checkpoint(self, plan_id: str) -> CorporateEvidenceCaptureCheckpoint | None:
        checkpoints = self._checkpoints.get(plan_id, [])
        return checkpoints[-1] if checkpoints else None

    def processed_ciks(self, plan_id: str) -> tuple[str, ...]:
        latest = self.latest_checkpoint(plan_id)
        return latest.cumulative_processed_ciks if latest is not None else ()

    def save_quality_report(self, report: CorporateDataQualityReport) -> CorporateDataQualityReport:
        self._quality_reports.setdefault(report.report_id, report)
        return self._quality_reports[report.report_id]

    def quality_report_by_id(self, report_id: str) -> CorporateDataQualityReport | None:
        return self._quality_reports.get(report_id)

    def latest_quality_report(self, plan_id: str) -> CorporateDataQualityReport | None:
        matches = sorted(
            (item for item in self._quality_reports.values() if item.plan_id == plan_id),
            key=lambda item: item.created_at,
        )
        return matches[-1] if matches else None

    def save_dataset_snapshot(
        self, dataset: CorporateHistoryDatasetSnapshot
    ) -> CorporateHistoryDatasetSnapshot:
        self._datasets.setdefault(dataset.dataset_id, dataset)
        return self._datasets[dataset.dataset_id]

    def dataset_snapshot_by_id(self, dataset_id: str) -> CorporateHistoryDatasetSnapshot | None:
        return self._datasets.get(dataset_id)

    def latest_dataset_snapshot(self, plan_id: str) -> CorporateHistoryDatasetSnapshot | None:
        matches = sorted(
            (item for item in self._datasets.values() if item.plan_id == plan_id),
            key=lambda item: item.created_at,
        )
        return matches[-1] if matches else None

    def save_replay_attachment(
        self, attachment: ReplayCorporateEvidenceAttachment
    ) -> ReplayCorporateEvidenceAttachment:
        self._attachments.setdefault(attachment.attachment_id, attachment)
        return self._attachments[attachment.attachment_id]

    def replay_attachment_by_id(
        self, attachment_id: str
    ) -> ReplayCorporateEvidenceAttachment | None:
        return self._attachments.get(attachment_id)

    def attachments_for_run(
        self, replay_run_id: str
    ) -> tuple[ReplayCorporateEvidenceAttachment, ...]:
        return tuple(
            item for item in self._attachments.values() if item.replay_run_id == replay_run_id
        )

    def attachment_run_summary(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for item in self._attachments.values():
            counts[item.replay_run_id] = counts.get(item.replay_run_id, 0) + 1
        return tuple(sorted(counts.items()))

    def save_replay_attachments(
        self, attachments: Sequence[ReplayCorporateEvidenceAttachment]
    ) -> int:
        saved = 0
        for item in attachments:
            self.save_replay_attachment(item)
            saved += 1
        return saved


def _identity(stable_security_id: str, symbol: str, cik: str | None) -> StableSecurityIdentity:
    return StableSecurityIdentity(
        stable_security_id=stable_security_id,
        identity_basis=IdentityBasis.CIK_AND_SYMBOL if cik else IdentityBasis.SYMBOL_ONLY,
        cik=cik,
        normalized_symbol=symbol,
        current_symbol=symbol,
        source_identity_hash=f"hash-{symbol}",
        source_canonical_security_id=f"canonical-{symbol}",
        review_required=False,
        canonical_hash=f"hash-{symbol}",
    )


def _resolved_cohort() -> ResolvedCohort:
    return ResolvedCohort(
        policy_id="policy-1",
        policy_hash="a" * 64,
        members=(
            ResolvedCohortMember(
                stable_security_id="sec-aapl",
                symbol="AAPL",
                sector=CohortSector.TECHNOLOGY,
                roles=(CohortRole.LARGE_CAP_EQUITY,),
                asset_class="COMMON_STOCK",
            ),
        ),
        unresolved_symbols=(),
        ambiguous_symbols=(),
    )


def test_register_plan_is_idempotent_across_repeated_invocations() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    service = RegisterCorporateEvidenceCohortPlan(repository=repository)
    command = RegisterCorporateEvidenceCohortPlanCommand(
        cohort_policy_id="policy-1",
        cohort_policy_hash="a" * 64,
        resolved_cohort=_resolved_cohort(),
        stable_identities=(_identity("sec-aapl", "AAPL", "320193"),),
        bounds=CorporateEvidenceCaptureBounds.create(),
        created_at=UTC_NOW,
    )
    plan_a, resolution_a = service.execute(command)
    plan_b, resolution_b = service.execute(command)
    assert plan_a.plan_id == plan_b.plan_id
    assert len(repository._plans) == 1
    assert resolution_a.members == resolution_b.members


def test_build_snapshot_as_of_reads_only_from_retained_evidence() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    fact = FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.FY,
        period_start="2023-10-01",
        period_end="2024-09-28",
        value=Decimal("391035000000"),
        accession_number="0000320193-24-000123",
        form="10-K",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    repository.save_fact(fact)
    snapshot = BuildCorporateFundamentalSnapshotAsOf(repository=repository).execute(
        BuildCorporateFundamentalSnapshotCommand(
            stable_security_id="sec-aapl", cik="320193", as_of=datetime(2024, 11, 2, tzinfo=UTC)
        )
    )
    assert snapshot.value_for(FundamentalConcept.REVENUE).value == Decimal("391035000000")


def test_compute_derived_features_as_of_returns_snapshot_and_features_together() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    repository.save_fact(
        FundamentalFact.create(
            stable_security_id="sec-aapl",
            cik="320193",
            concept=FundamentalConcept.REVENUE,
            source_taxonomy="us-gaap",
            source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            fiscal_year=2024,
            fiscal_period=FiscalPeriod.FY,
            period_start="2023-10-01",
            period_end="2024-09-28",
            value=Decimal("391035000000"),
            accession_number="0000320193-24-000123",
            form="10-K",
            filed_at=datetime(2024, 11, 1, tzinfo=UTC),
            accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        )
    )
    snapshot, features = ComputeDerivedFundamentalFeaturesAsOf(repository=repository).execute(
        BuildCorporateFundamentalSnapshotCommand(
            stable_security_id="sec-aapl", cik="320193", as_of=datetime(2024, 11, 2, tzinfo=UTC)
        )
    )
    assert snapshot.stable_security_id == "sec-aapl"
    assert len(features) > 0


def test_resolve_cik_for_security_prefers_filings_then_facts_then_none() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    assert resolve_cik_for_security(repository, "sec-unknown") is None
    repository.save_fact(
        FundamentalFact.create(
            stable_security_id="sec-aapl",
            cik="320193",
            concept=FundamentalConcept.REVENUE,
            source_taxonomy="us-gaap",
            source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            fiscal_year=2024,
            fiscal_period=FiscalPeriod.FY,
            period_start="2023-10-01",
            period_end="2024-09-28",
            value=Decimal("1"),
            accession_number="0000320193-24-000123",
            form="10-K",
            filed_at=datetime(2024, 11, 1, tzinfo=UTC),
            accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        )
    )
    assert resolve_cik_for_security(repository, "sec-aapl") == "320193"


def test_materialize_dataset_requires_a_registered_plan_and_a_checkpoint() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    service = MaterializeCorporateHistoryDataset(repository=repository)
    with pytest.raises(CorporateFundamentalsNotFound):
        service.execute(
            MaterializeCorporateHistoryDatasetCommand(plan_id="missing", created_at=UTC_NOW)
        )

    plan, _resolution = RegisterCorporateEvidenceCohortPlan(repository=repository).execute(
        RegisterCorporateEvidenceCohortPlanCommand(
            cohort_policy_id="policy-1",
            cohort_policy_hash="a" * 64,
            resolved_cohort=_resolved_cohort(),
            stable_identities=(_identity("sec-aapl", "AAPL", "320193"),),
            bounds=CorporateEvidenceCaptureBounds.create(),
            created_at=UTC_NOW,
        )
    )
    with pytest.raises(CorporateFundamentalsApplicationError):
        service.execute(
            MaterializeCorporateHistoryDatasetCommand(plan_id=plan.plan_id, created_at=UTC_NOW)
        )


def test_materialize_dataset_binds_checkpoints_filings_facts_and_quality() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    plan, _resolution = RegisterCorporateEvidenceCohortPlan(repository=repository).execute(
        RegisterCorporateEvidenceCohortPlanCommand(
            cohort_policy_id="policy-1",
            cohort_policy_hash="a" * 64,
            resolved_cohort=_resolved_cohort(),
            stable_identities=(_identity("sec-aapl", "AAPL", "320193"),),
            bounds=CorporateEvidenceCaptureBounds.create(),
            created_at=UTC_NOW,
        )
    )
    filing = CorporateFilingEvent.create(
        stable_security_id="sec-aapl",
        cik="320193",
        accession_number="0000320193-24-000123",
        form=FilingForm.FORM_10K,
        report_period="2024-09-28",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        primary_document="doc.htm",
        file_number="001-36743",
    )
    repository.save_filing(filing)
    outcome = IssuerCaptureOutcome(
        "sec-aapl", "320193", IssuerCaptureStatus.CAPTURED, 1, 0, 100, 100, 2, (), ("hash-1",)
    )
    checkpoint = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=(outcome,),
        previously_processed_ciks=(),
        total_requests_this_run=2,
        total_bytes_this_run=200,
        runtime_seconds=Decimal("1.0"),
        created_at=UTC_NOW,
    )
    repository.save_checkpoint(checkpoint)

    report, dataset = MaterializeCorporateHistoryDataset(repository=repository).execute(
        MaterializeCorporateHistoryDatasetCommand(plan_id=plan.plan_id, created_at=UTC_NOW)
    )
    assert dataset.plan_id == plan.plan_id
    assert filing.filing_event_id in dataset.filing_event_ids
    assert dataset.quality_report_id == report.report_id
    assert dataset.completeness == "COMPLETE"

    # Idempotent replay: identical retained evidence resolves to the
    # identical dataset id, never a duplicate.
    report_again, dataset_again = MaterializeCorporateHistoryDataset(repository=repository).execute(
        MaterializeCorporateHistoryDatasetCommand(plan_id=plan.plan_id, created_at=UTC_NOW)
    )
    assert dataset_again.dataset_id == dataset.dataset_id
    assert report_again.report_id == report.report_id


def test_attach_corporate_evidence_to_replay_frame_is_purely_additive() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    repository.save_fact(
        FundamentalFact.create(
            stable_security_id="sec-aapl",
            cik="320193",
            concept=FundamentalConcept.REVENUE,
            source_taxonomy="us-gaap",
            source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            fiscal_year=2024,
            fiscal_period=FiscalPeriod.FY,
            period_start="2023-10-01",
            period_end="2024-09-28",
            value=Decimal("391035000000"),
            accession_number="0000320193-24-000123",
            form="10-K",
            filed_at=datetime(2024, 11, 1, tzinfo=UTC),
            accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        )
    )

    class _FrameSource:
        def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]:
            return (_frame_with_universe(replay_run_id, ("sec-aapl", "sec-no-evidence")),)

    attachments = AttachCorporateEvidenceToReplayFrame(
        repository=repository, frame_source=_FrameSource()
    ).execute(
        AttachCorporateEvidenceToReplayFrameCommand(replay_run_id="replay-1", created_at=UTC_NOW)
    )
    assert len(attachments) == 1
    assert attachments[0].stable_security_id == "sec-aapl"
    assert attachments[0].replay_run_id == "replay-1"

    # Replaying the identical run is idempotent -- no duplicate rows.
    attachments_again = AttachCorporateEvidenceToReplayFrame(
        repository=repository, frame_source=_FrameSource()
    ).execute(
        AttachCorporateEvidenceToReplayFrameCommand(replay_run_id="replay-1", created_at=UTC_NOW)
    )
    assert attachments_again[0].attachment_id == attachments[0].attachment_id
    assert len(repository._attachments) == 1


def _frame_with_universe(replay_run_id: str, universe: tuple[str, ...]) -> ReplayFeatureFrame:
    return _frame(
        replay_run_id, "frame-" + replay_run_id, datetime(2024, 11, 2, tzinfo=UTC), universe
    )


def _frame(
    replay_run_id: str, frame_id: str, as_of: datetime, universe: tuple[str, ...]
) -> ReplayFeatureFrame:
    from apex.domain.historical_replay import BreadthSnapshot, MarketRegimeSnapshot

    return ReplayFeatureFrame(
        frame_id=frame_id,
        replay_run_id=replay_run_id,
        dataset_snapshot_id="dataset-1",
        as_of=as_of,
        session_date=as_of.date().isoformat(),
        feature_policy_id="policy-1",
        feature_policy_hash="a" * 64,
        universe=universe,
        securities=(),
        breadth=BreadthSnapshot(
            universe_size=len(universe),
            evaluable_count=0,
            advancing_count=0,
            declining_count=0,
            unchanged_count=0,
            fraction_advancing=None,
            fraction_above_moving_average=None,
            reasons=(),
        ),
        regime=MarketRegimeSnapshot(rule_version="v1", classifications=()),
        excluded_evidence_count=0,
        excluded_evidence_reasons=(),
        point_in_time_limitations=(),
        data_quality_flags=(),
        created_at=UTC_NOW,
        canonical_hash="a" * 64,
    )


class _StaticFrameSource:
    def __init__(self, frames: tuple[ReplayFeatureFrame, ...]) -> None:
        self._frames = frames

    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]:
        return tuple(item for item in self._frames if item.replay_run_id == replay_run_id)[:limit]

    def frame_by_id(self, frame_id: str) -> ReplayFeatureFrame | None:
        return next((item for item in self._frames if item.frame_id == frame_id), None)


# --------------------------------------------------------------------------
# Batch-attachment equivalence (Corporate Evidence Completion and Replay
# Scaling v1, section 3/4): a reference implementation reproducing the
# original per-``(frame, security)`` algorithm byte-for-byte, proven equal
# to the batched ``AttachCorporateEvidenceToReplayFrame``.
# --------------------------------------------------------------------------


def _slow_reference_attach(
    *,
    repository: InMemoryCorporateFundamentalsRepository,
    frame_source: _StaticFrameSource,
    command: AttachCorporateEvidenceToReplayFrameCommand,
) -> tuple[ReplayCorporateEvidenceAttachment, ...]:
    """Reproduces the pre-batching implementation exactly: one repository
    round trip per (frame, security) pair, no bulk index. Exists only to
    prove the batched implementation is a genuine equivalence, never a
    behavior change."""
    frames = frame_source.list_frames(command.replay_run_id, limit=command.frame_limit)
    attachments: list[ReplayCorporateEvidenceAttachment] = []
    for frame in frames:
        for stable_security_id in frame.universe:
            cik = resolve_cik_for_security(repository, stable_security_id)
            if cik is None:
                continue
            filings = repository.filings_for(stable_security_id)
            facts = repository.facts_for(stable_security_id)
            snapshot = build_corporate_fundamental_snapshot(
                stable_security_id=stable_security_id,
                cik=cik,
                as_of=frame.as_of,
                filings=filings,
                facts=facts,
            )
            features = compute_derived_features(snapshot=snapshot, facts=facts)
            attachment = ReplayCorporateEvidenceAttachment.create(
                replay_run_id=command.replay_run_id,
                frame_id=frame.frame_id,
                snapshot=snapshot,
                features=features,
                created_at=command.created_at,
            )
            existing = repository.replay_attachment_by_id(attachment.attachment_id)
            saved = existing or repository.save_replay_attachment(attachment)
            attachments.append(saved)
    return tuple(attachments)


def _attachment_key(item: ReplayCorporateEvidenceAttachment) -> tuple[object, ...]:
    return (
        item.frame_id,
        item.stable_security_id,
        item.as_of,
        item.snapshot_hash,
        item.snapshot_quality,
        item.fact_lineage,
        item.filing_lineage,
        item.feature_hashes,
    )


def _msft_filing() -> CorporateFilingEvent:
    return CorporateFilingEvent.create(
        stable_security_id="sec-msft",
        cik="789019",
        accession_number="0000789019-24-000001",
        form=FilingForm.FORM_10K,
        report_period="2024-06-30",
        filed_at=datetime(2024, 9, 30, tzinfo=UTC),
        accepted_at=datetime(2024, 10, 1, tzinfo=UTC),
        primary_document="doc.htm",
        file_number="001-99999",
    )


def _msft_fact() -> FundamentalFact:
    return FundamentalFact.create(
        stable_security_id="sec-msft",
        cik="789019",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.FY,
        period_start="2023-07-01",
        period_end="2024-06-30",
        value=Decimal("245000000000"),
        accession_number="0000789019-24-000001",
        form="10-K",
        filed_at=datetime(2024, 9, 30, tzinfo=UTC),
        accepted_at=datetime(2024, 10, 1, tzinfo=UTC),
    )


def _aapl_filing() -> CorporateFilingEvent:
    return CorporateFilingEvent.create(
        stable_security_id="sec-aapl",
        cik="320193",
        accession_number="0000320193-24-000123",
        form=FilingForm.FORM_10K,
        report_period="2024-09-28",
        filed_at=datetime(2024, 10, 31, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        primary_document="doc.htm",
        file_number="001-36743",
    )


def _aapl_fact(*, accession: str, value: str, accepted_at: datetime) -> FundamentalFact:
    return FundamentalFact.create(
        stable_security_id="sec-aapl",
        cik="320193",
        concept=FundamentalConcept.REVENUE,
        source_taxonomy="us-gaap",
        source_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        fiscal_year=2024,
        fiscal_period=FiscalPeriod.FY,
        period_start="2023-10-01",
        period_end="2024-09-28",
        value=Decimal(value),
        accession_number=accession,
        form="10-K",
        filed_at=accepted_at - timedelta(days=1),
        accepted_at=accepted_at,
    )


def _seed_equivalence_fixture() -> tuple[
    InMemoryCorporateFundamentalsRepository, FundamentalFact, FundamentalFact
]:
    """Two evidenced issuers (AAPL with an original fact plus a later
    restatement of the identical period, MSFT with one fact) -- returned
    alongside the exact original/restated fact objects so tests can assert
    point-in-time lineage without re-deriving content-addressed ids."""
    repository = InMemoryCorporateFundamentalsRepository()
    repository.save_filing(_aapl_filing())
    original = _aapl_fact(
        accession="0000320193-24-000123",
        value="391035000000",
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
    )
    restated = _aapl_fact(
        accession="0000320193-24-000456",
        value="400000000000",
        accepted_at=datetime(2024, 12, 1, tzinfo=UTC),
    )
    repository.save_fact(original)
    repository.save_fact(restated)
    repository.save_filing(_msft_filing())
    repository.save_fact(_msft_fact())
    return repository, original, restated


def _equivalence_frames(replay_run_id: str) -> tuple[ReplayFeatureFrame, ...]:
    universe = ("sec-aapl", "sec-msft", "sec-none")
    return (
        _frame(replay_run_id, "frame-1", datetime(2024, 10, 15, tzinfo=UTC), universe),
        _frame(replay_run_id, "frame-2", datetime(2024, 11, 15, tzinfo=UTC), universe),
        _frame(replay_run_id, "frame-3", datetime(2024, 12, 15, tzinfo=UTC), universe),
    )


def test_batched_attach_matches_slow_reference_small_fixture() -> None:
    replay_run_id = "replay-equiv-small"
    frame_source = _StaticFrameSource(_equivalence_frames(replay_run_id))
    command = AttachCorporateEvidenceToReplayFrameCommand(
        replay_run_id=replay_run_id, created_at=UTC_NOW
    )

    slow_repository, _original, _restated = _seed_equivalence_fixture()
    slow_attachments = _slow_reference_attach(
        repository=slow_repository, frame_source=frame_source, command=command
    )

    batched_repository, _original2, _restated2 = _seed_equivalence_fixture()
    batched_attachments = AttachCorporateEvidenceToReplayFrame(
        repository=batched_repository, frame_source=frame_source
    ).execute(command)

    assert len(slow_attachments) == len(batched_attachments) == 6  # 2 evidenced x 3 frames
    assert {_attachment_key(item) for item in slow_attachments} == {
        _attachment_key(item) for item in batched_attachments
    }
    assert all(item.stable_security_id != "sec-none" for item in batched_attachments)


def test_batched_attach_no_lookahead_and_restatement_timing() -> None:
    replay_run_id = "replay-timing"
    repository, original, restated = _seed_equivalence_fixture()
    frame_source = _StaticFrameSource(_equivalence_frames(replay_run_id))
    attachments = AttachCorporateEvidenceToReplayFrame(
        repository=repository, frame_source=frame_source
    ).execute(
        AttachCorporateEvidenceToReplayFrameCommand(replay_run_id=replay_run_id, created_at=UTC_NOW)
    )
    by_key = {(item.frame_id, item.stable_security_id): item for item in attachments}

    # frame-1 (2024-10-15): before AAPL's earliest fact (accepted
    # 2024-11-01) is visible -- no lookahead into the future.
    assert by_key[("frame-1", "sec-aapl")].fact_lineage == ()

    # frame-2 (2024-11-15): only the *original* accession is visible.
    assert by_key[("frame-2", "sec-aapl")].fact_lineage == (original.fact_id,)

    # frame-3 (2024-12-15): the restatement has since become visible and
    # supersedes the original -- a genuinely different lineage/snapshot.
    assert by_key[("frame-3", "sec-aapl")].fact_lineage == (restated.fact_id,)
    assert (
        by_key[("frame-2", "sec-aapl")].snapshot_hash
        != by_key[("frame-3", "sec-aapl")].snapshot_hash
    )

    # MSFT's single fact is visible at every one of these frames.
    for frame_id in ("frame-1", "frame-2", "frame-3"):
        assert by_key[(frame_id, "sec-msft")].fact_lineage != ()


def test_batched_attach_matches_slow_reference_larger_frame_count() -> None:
    replay_run_id = "replay-equiv-large"
    universe = ("sec-aapl", "sec-msft", "sec-none")
    frames = tuple(
        _frame(
            replay_run_id,
            f"frame-{index}",
            datetime(2024, 9, 1, tzinfo=UTC) + timedelta(days=index),
            universe,
        )
        for index in range(60)
    )
    frame_source = _StaticFrameSource(frames)
    command = AttachCorporateEvidenceToReplayFrameCommand(
        replay_run_id=replay_run_id, created_at=UTC_NOW, frame_limit=200
    )

    slow_repository, _o1, _r1 = _seed_equivalence_fixture()
    slow_attachments = _slow_reference_attach(
        repository=slow_repository, frame_source=frame_source, command=command
    )
    batched_repository, _o2, _r2 = _seed_equivalence_fixture()
    batched_attachments = AttachCorporateEvidenceToReplayFrame(
        repository=batched_repository, frame_source=frame_source
    ).execute(command)

    assert len(slow_attachments) == len(batched_attachments) == 120  # 2 evidenced x 60 frames
    assert {_attachment_key(item) for item in slow_attachments} == {
        _attachment_key(item) for item in batched_attachments
    }


def test_batched_attach_skips_missing_issuer_without_error() -> None:
    replay_run_id = "replay-missing-issuer"
    repository, _original, _restated = _seed_equivalence_fixture()
    frame_source = _StaticFrameSource(
        (_frame(replay_run_id, "frame-1", datetime(2024, 12, 15, tzinfo=UTC), ("sec-none",)),)
    )
    attachments = AttachCorporateEvidenceToReplayFrame(
        repository=repository, frame_source=frame_source
    ).execute(
        AttachCorporateEvidenceToReplayFrameCommand(replay_run_id=replay_run_id, created_at=UTC_NOW)
    )
    assert attachments == ()


def test_batched_attach_reuses_already_attached_pairs_without_recomputing() -> None:
    replay_run_id = "replay-reuse"
    repository, _original, _restated = _seed_equivalence_fixture()
    frame_source = _StaticFrameSource(
        (_frame(replay_run_id, "frame-1", datetime(2024, 11, 15, tzinfo=UTC), ("sec-aapl",)),)
    )
    command = AttachCorporateEvidenceToReplayFrameCommand(
        replay_run_id=replay_run_id, created_at=UTC_NOW
    )
    service = AttachCorporateEvidenceToReplayFrame(repository=repository, frame_source=frame_source)
    first_pass, first_stats = service.execute_with_stats(command)
    assert first_stats.pairs_newly_attached == 1
    assert first_stats.pairs_already_attached == 0

    # Re-running the identical (replay_run_id, frame_id) against the same
    # repository is a genuine duplicate-replay: every pair is already
    # retained, nothing new is inserted, and the identical row is returned.
    second_pass, second_stats = service.execute_with_stats(command)
    assert second_stats.pairs_newly_attached == 0
    assert second_stats.pairs_already_attached == 1
    assert second_pass[0].attachment_id == first_pass[0].attachment_id
    assert len(repository._attachments) == 1


# --------------------------------------------------------------------------
# Corporate replay feature frame service (section 5, 12)
# --------------------------------------------------------------------------


def test_build_corporate_replay_feature_frame_ties_to_a_genuine_replay_frame() -> None:
    repository, _original, _restated = _seed_equivalence_fixture()
    frame_source = _StaticFrameSource(
        (
            _frame(
                "replay-feat",
                "frame-1",
                datetime(2024, 11, 15, tzinfo=UTC),
                ("sec-aapl", "sec-msft"),
            ),
        )
    )
    frame_feature = BuildCorporateReplayFeatureFrame(
        repository=repository, frame_source=frame_source
    ).execute(
        BuildCorporateReplayFeatureFrameCommand(
            replay_run_id="replay-feat", frame_id="frame-1", stable_security_id="sec-aapl"
        )
    )
    assert frame_feature.stable_security_id == "sec-aapl"
    assert frame_feature.as_of == datetime(2024, 11, 15, tzinfo=UTC)
    # A lone fact with no retained prior year: YoY growth is honestly
    # unavailable, never fabricated -- and it is named in missing_fields.
    assert frame_feature.revenue_growth_yoy.unavailable_reason is not None
    assert "revenue_growth_yoy" in frame_feature.missing_fields


def test_build_corporate_replay_feature_frame_rejects_security_outside_frame_universe() -> None:
    repository, _original, _restated = _seed_equivalence_fixture()
    frame_source = _StaticFrameSource(
        (_frame("replay-feat", "frame-1", datetime(2024, 11, 15, tzinfo=UTC), ("sec-msft",)),)
    )
    with pytest.raises(CorporateFundamentalsApplicationError):
        BuildCorporateReplayFeatureFrame(repository=repository, frame_source=frame_source).execute(
            BuildCorporateReplayFeatureFrameCommand(
                replay_run_id="replay-feat", frame_id="frame-1", stable_security_id="sec-aapl"
            )
        )


def test_build_corporate_replay_feature_frame_requires_a_genuine_retained_frame() -> None:
    repository, _original, _restated = _seed_equivalence_fixture()
    frame_source = _StaticFrameSource(())
    with pytest.raises(CorporateFundamentalsNotFound):
        BuildCorporateReplayFeatureFrame(repository=repository, frame_source=frame_source).execute(
            BuildCorporateReplayFeatureFrameCommand(
                replay_run_id="replay-feat", frame_id="missing-frame", stable_security_id="sec-aapl"
            )
        )


# --------------------------------------------------------------------------
# Dataset rematerialization: a new snapshot after more evidence is captured
# never mutates the earlier, narrower one.
# --------------------------------------------------------------------------


def test_materialize_dataset_produces_a_new_snapshot_after_more_evidence_is_captured() -> None:
    repository = InMemoryCorporateFundamentalsRepository()
    plan, _resolution = RegisterCorporateEvidenceCohortPlan(repository=repository).execute(
        RegisterCorporateEvidenceCohortPlanCommand(
            cohort_policy_id="policy-1",
            cohort_policy_hash="a" * 64,
            resolved_cohort=_resolved_cohort(),
            stable_identities=(_identity("sec-aapl", "AAPL", "320193"),),
            bounds=CorporateEvidenceCaptureBounds.create(),
            created_at=UTC_NOW,
        )
    )
    filing = CorporateFilingEvent.create(
        stable_security_id="sec-aapl",
        cik="320193",
        accession_number="0000320193-24-000123",
        form=FilingForm.FORM_10K,
        report_period="2024-09-28",
        filed_at=datetime(2024, 11, 1, tzinfo=UTC),
        accepted_at=datetime(2024, 11, 1, tzinfo=UTC),
        primary_document="doc.htm",
        file_number="001-36743",
    )
    repository.save_filing(filing)
    checkpoint1 = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-1",
        outcomes=(
            IssuerCaptureOutcome(
                "sec-aapl",
                "320193",
                IssuerCaptureStatus.CAPTURED,
                1,
                0,
                100,
                100,
                2,
                (),
                ("hash-1",),
            ),
        ),
        previously_processed_ciks=(),
        total_requests_this_run=2,
        total_bytes_this_run=200,
        runtime_seconds=Decimal("1.0"),
        created_at=UTC_NOW,
    )
    repository.save_checkpoint(checkpoint1)

    report_a, dataset_a = MaterializeCorporateHistoryDataset(repository=repository).execute(
        MaterializeCorporateHistoryDatasetCommand(plan_id=plan.plan_id, created_at=UTC_NOW)
    )
    assert dataset_a.filing_event_ids == (filing.filing_event_id,)

    # More evidence is captured in a second, later checkpoint...
    second_filing = CorporateFilingEvent.create(
        stable_security_id="sec-aapl",
        cik="320193",
        accession_number="0000320193-25-000001",
        form=FilingForm.FORM_10Q,
        report_period="2024-12-28",
        filed_at=datetime(2025, 1, 30, tzinfo=UTC),
        accepted_at=datetime(2025, 1, 30, tzinfo=UTC),
        primary_document="doc2.htm",
        file_number="001-36743",
    )
    repository.save_filing(second_filing)
    checkpoint2 = CorporateEvidenceCaptureCheckpoint.create(
        plan=plan,
        run_id="run-2",
        outcomes=(
            IssuerCaptureOutcome(
                "sec-aapl",
                "320193",
                IssuerCaptureStatus.CAPTURED,
                1,
                0,
                100,
                100,
                2,
                (),
                ("hash-2",),
            ),
        ),
        previously_processed_ciks=checkpoint1.cumulative_processed_ciks,
        total_requests_this_run=2,
        total_bytes_this_run=200,
        runtime_seconds=Decimal("1.0"),
        created_at=UTC_NOW,
    )
    repository.save_checkpoint(checkpoint2)

    later = UTC_NOW + timedelta(days=1)
    report_b, dataset_b = MaterializeCorporateHistoryDataset(repository=repository).execute(
        MaterializeCorporateHistoryDatasetCommand(plan_id=plan.plan_id, created_at=later)
    )
    assert dataset_b.dataset_id != dataset_a.dataset_id
    assert second_filing.filing_event_id in dataset_b.filing_event_ids
    assert dataset_b.checkpoint_ids == tuple(
        sorted((checkpoint1.checkpoint_id, checkpoint2.checkpoint_id))
    )
    assert report_b.report_id != report_a.report_id

    # The earlier, narrower snapshot is never mutated -- still retrievable
    # by its own id with its own original evidence set, unaffected by the
    # later capture run.
    reloaded_a = repository.dataset_snapshot_by_id(dataset_a.dataset_id)
    assert reloaded_a is not None
    assert reloaded_a.filing_event_ids == (filing.filing_event_id,)
    assert second_filing.filing_event_id not in reloaded_a.filing_event_ids
