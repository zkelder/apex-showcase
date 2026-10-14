"""Application services for Corporate Fundamental, Filing, and Event
Intelligence v1: plan registration, point-in-time snapshot/feature
reconstruction, dataset materialization, data-quality reporting, and the
additive Historical Replay attachment seam.

Every service here is adapter-free: real SEC network I/O lives in
``apex.adapters.corporate_evidence_capture``/``corporate_filings_source``,
never here. This module only orchestrates already-parsed domain evidence
through the repository protocol below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.corporate_fundamentals import (
    CorporateDataQualityReport,
    CorporateEvidenceCaptureBounds,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortPlan,
    CorporateEvidenceCohortResolution,
    CorporateFilingEvent,
    CorporateFundamentalSnapshot,
    CorporateHistoryDatasetSnapshot,
    CorporateReplayFeatureFrame,
    DerivedFundamentalFeature,
    EarningsEvent,
    FilingEventFeature,
    FundamentalConcept,
    FundamentalFact,
    IssuerQualityAssessment,
    ReplayCorporateEvidenceAttachment,
    assess_issuer_quality,
    build_corporate_fundamental_snapshot,
    build_corporate_replay_feature_frame,
    compute_derived_features,
    compute_filing_event_features,
    derive_earnings_events,
    resolve_corporate_evidence_cohort,
)
from apex.domain.historical_replay import ReplayFeatureFrame
from apex.domain.research_cohort import ResolvedCohort
from apex.domain.security_identity import StableSecurityIdentity


class CorporateFundamentalsApplicationError(RuntimeError):
    pass


class CorporateFundamentalsNotFound(CorporateFundamentalsApplicationError):
    pass


class CorporateFundamentalsRepository(Protocol):
    def save_plan(self, plan: CorporateEvidenceCohortPlan) -> CorporateEvidenceCohortPlan: ...
    def plan_by_id(self, plan_id: str) -> CorporateEvidenceCohortPlan | None: ...
    def latest_plan(self) -> CorporateEvidenceCohortPlan | None: ...

    def save_filing(self, filing: CorporateFilingEvent) -> CorporateFilingEvent: ...
    def filings_for(self, stable_security_id: str) -> tuple[CorporateFilingEvent, ...]: ...
    def filing_by_accession(self, accession_number: str) -> CorporateFilingEvent | None: ...
    def all_filings_for_plan(self, plan_id: str) -> tuple[CorporateFilingEvent, ...]: ...
    def filings_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[CorporateFilingEvent, ...]: ...

    def save_fact(self, fact: FundamentalFact) -> FundamentalFact: ...
    def facts_for(self, stable_security_id: str) -> tuple[FundamentalFact, ...]: ...
    def all_facts_for_plan(self, plan_id: str) -> tuple[FundamentalFact, ...]: ...
    def facts_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[FundamentalFact, ...]: ...

    def save_checkpoint(
        self, checkpoint: CorporateEvidenceCaptureCheckpoint
    ) -> CorporateEvidenceCaptureCheckpoint: ...
    def checkpoints_for_plan(
        self, plan_id: str
    ) -> tuple[CorporateEvidenceCaptureCheckpoint, ...]: ...
    def latest_checkpoint(self, plan_id: str) -> CorporateEvidenceCaptureCheckpoint | None: ...
    def processed_ciks(self, plan_id: str) -> tuple[str, ...]: ...

    def save_quality_report(
        self, report: CorporateDataQualityReport
    ) -> CorporateDataQualityReport: ...
    def quality_report_by_id(self, report_id: str) -> CorporateDataQualityReport | None: ...
    def latest_quality_report(self, plan_id: str) -> CorporateDataQualityReport | None: ...

    def save_dataset_snapshot(
        self, dataset: CorporateHistoryDatasetSnapshot
    ) -> CorporateHistoryDatasetSnapshot: ...
    def dataset_snapshot_by_id(self, dataset_id: str) -> CorporateHistoryDatasetSnapshot | None: ...
    def latest_dataset_snapshot(self, plan_id: str) -> CorporateHistoryDatasetSnapshot | None: ...

    def save_replay_attachment(
        self, attachment: ReplayCorporateEvidenceAttachment
    ) -> ReplayCorporateEvidenceAttachment: ...
    def replay_attachment_by_id(
        self, attachment_id: str
    ) -> ReplayCorporateEvidenceAttachment | None: ...
    def attachments_for_run(
        self, replay_run_id: str
    ) -> tuple[ReplayCorporateEvidenceAttachment, ...]: ...
    def attachment_run_summary(self) -> tuple[tuple[str, int], ...]: ...
    def save_replay_attachments(
        self, attachments: Sequence[ReplayCorporateEvidenceAttachment]
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RegisterCorporateEvidenceCohortPlanCommand:
    cohort_policy_id: str
    cohort_policy_hash: str
    resolved_cohort: ResolvedCohort
    stable_identities: tuple[StableSecurityIdentity, ...]
    bounds: CorporateEvidenceCaptureBounds
    created_at: datetime


class RegisterCorporateEvidenceCohortPlan:
    """Idempotent registration: an identical cohort/bounds always resolves
    to the identical content-addressed ``plan_id``, so re-registering the
    same declared policy replays the existing retained plan rather than
    creating a duplicate."""

    def __init__(self, *, repository: CorporateFundamentalsRepository) -> None:
        self._repository = repository

    def execute(
        self, command: RegisterCorporateEvidenceCohortPlanCommand
    ) -> tuple[CorporateEvidenceCohortPlan, CorporateEvidenceCohortResolution]:
        resolution = resolve_corporate_evidence_cohort(
            command.resolved_cohort, command.stable_identities
        )
        plan = CorporateEvidenceCohortPlan.create(
            cohort_policy_id=command.cohort_policy_id,
            cohort_policy_hash=command.cohort_policy_hash,
            resolution=resolution,
            bounds=command.bounds,
            created_at=command.created_at,
        )
        existing = self._repository.plan_by_id(plan.plan_id)
        saved = existing or self._repository.save_plan(plan)
        return saved, resolution


@dataclass(frozen=True, slots=True)
class BuildCorporateFundamentalSnapshotCommand:
    stable_security_id: str
    cik: str
    as_of: datetime


class BuildCorporateFundamentalSnapshotAsOf:
    """The one point-in-time reconstruction entry point every CLI/API/
    replay caller shares -- always re-derived from currently retained
    evidence, never itself persisted as a separate mutable cache."""

    def __init__(self, *, repository: CorporateFundamentalsRepository) -> None:
        self._repository = repository

    def execute(
        self, command: BuildCorporateFundamentalSnapshotCommand
    ) -> CorporateFundamentalSnapshot:
        filings = self._repository.filings_for(command.stable_security_id)
        facts = self._repository.facts_for(command.stable_security_id)
        return build_corporate_fundamental_snapshot(
            stable_security_id=command.stable_security_id,
            cik=command.cik,
            as_of=command.as_of,
            filings=filings,
            facts=facts,
        )


class ComputeDerivedFundamentalFeaturesAsOf:
    def __init__(self, *, repository: CorporateFundamentalsRepository) -> None:
        self._repository = repository

    def execute(
        self, command: BuildCorporateFundamentalSnapshotCommand
    ) -> tuple[CorporateFundamentalSnapshot, tuple[DerivedFundamentalFeature, ...]]:
        facts = self._repository.facts_for(command.stable_security_id)
        filings = self._repository.filings_for(command.stable_security_id)
        snapshot = build_corporate_fundamental_snapshot(
            stable_security_id=command.stable_security_id,
            cik=command.cik,
            as_of=command.as_of,
            filings=filings,
            facts=facts,
        )
        features = compute_derived_features(snapshot=snapshot, facts=facts)
        return snapshot, features


class ComputeFilingEventFeaturesAsOf:
    def __init__(self, *, repository: CorporateFundamentalsRepository) -> None:
        self._repository = repository

    def execute(
        self, *, stable_security_id: str, as_of: datetime, lookback_days: int = 30
    ) -> tuple[FilingEventFeature, ...]:
        filings = self._repository.filings_for(stable_security_id)
        return compute_filing_event_features(
            stable_security_id=stable_security_id,
            as_of=as_of,
            filings=filings,
            lookback_days=lookback_days,
        )


class ListEarningsEvents:
    """Read-only projection over already-retained filings -- see
    ``derive_earnings_events``'s explicit timing limitation. Never a
    separately persisted table."""

    def __init__(self, *, repository: CorporateFundamentalsRepository) -> None:
        self._repository = repository

    def execute(self, *, stable_security_id: str) -> tuple[EarningsEvent, ...]:
        filings = self._repository.filings_for(stable_security_id)
        return derive_earnings_events(filings)


@dataclass(frozen=True, slots=True)
class MaterializeCorporateHistoryDatasetCommand:
    plan_id: str
    created_at: datetime


class MaterializeCorporateHistoryDataset:
    """Binds one plan's currently retained filings/facts/checkpoints into
    one immutable dataset snapshot plus its quality report (section 13) --
    never a parallel backtest datastore, purely a content-addressed
    manifest over already-retained evidence."""

    def __init__(self, *, repository: CorporateFundamentalsRepository) -> None:
        self._repository = repository

    def execute(
        self, command: MaterializeCorporateHistoryDatasetCommand
    ) -> tuple[CorporateDataQualityReport, CorporateHistoryDatasetSnapshot]:
        plan = self._repository.plan_by_id(command.plan_id)
        if plan is None:
            raise CorporateFundamentalsNotFound("CORPORATE_EVIDENCE_PLAN_NOT_FOUND")
        checkpoints = self._repository.checkpoints_for_plan(command.plan_id)
        if not checkpoints:
            raise CorporateFundamentalsApplicationError("NO_RETAINED_CAPTURE_CHECKPOINT_FOR_PLAN")
        latest_checkpoint = checkpoints[-1]
        processed_ciks = self._repository.processed_ciks(command.plan_id)
        all_outcomes = tuple(
            outcome for checkpoint in checkpoints for outcome in checkpoint.outcomes
        )
        all_filings = self._repository.all_filings_for_plan(command.plan_id)
        all_facts = self._repository.all_facts_for_plan(command.plan_id)
        filings_by_security: dict[str, list[CorporateFilingEvent]] = {}
        for filing in all_filings:
            filings_by_security.setdefault(filing.stable_security_id, []).append(filing)
        facts_by_security: dict[str, list[FundamentalFact]] = {}
        for fact in all_facts:
            facts_by_security.setdefault(fact.stable_security_id, []).append(fact)
        assessments = tuple(
            assess_issuer_quality(
                stable_security_id=member.stable_security_id,
                symbol=member.symbol,
                cik=member.cik,
                filings=tuple(filings_by_security.get(member.stable_security_id, ())),
                facts=tuple(facts_by_security.get(member.stable_security_id, ())),
            )
            for member in plan.members
        )
        report = CorporateDataQualityReport.create(
            plan=plan,
            issuer_assessments=assessments,
            all_filings=all_filings,
            all_facts=all_facts,
            all_outcomes=all_outcomes,
            processed_ciks=processed_ciks,
            checkpoint_complete=latest_checkpoint.complete,
            created_at=command.created_at,
        )
        existing_report = self._repository.quality_report_by_id(report.report_id)
        saved_report = existing_report or self._repository.save_quality_report(report)

        # Every checkpoint's own capture receipts contribute lineage -- a
        # dataset rebuilt after several resumed capture runs must bind all
        # of them, not just the run that happened to complete last.
        source_receipt_hashes = tuple(
            sorted(
                {
                    source
                    for checkpoint in checkpoints
                    for item in checkpoint.outcomes
                    for source in item.source_receipt_ids
                }
            )
        )
        dataset = CorporateHistoryDatasetSnapshot.create(
            plan=plan,
            checkpoint_ids=tuple(item.checkpoint_id for item in checkpoints),
            filings=all_filings,
            facts=all_facts,
            quality_report=saved_report,
            source_receipt_hashes=source_receipt_hashes,
            created_at=command.created_at,
        )
        existing_dataset = self._repository.dataset_snapshot_by_id(dataset.dataset_id)
        saved_dataset = existing_dataset or self._repository.save_dataset_snapshot(dataset)
        return saved_report, saved_dataset


def resolve_cik_for_security(
    repository: CorporateFundamentalsRepository, stable_security_id: str
) -> str | None:
    """The CIK already carried by any retained filing/fact for this
    security -- never guessed, never a second identity source. ``None``
    when nothing has ever been captured for it yet, so a caller (CLI,
    replay attachment) can report that honestly instead of building a
    snapshot against a fabricated identity."""
    filings = repository.filings_for(stable_security_id)
    if filings:
        return filings[0].cik
    facts = repository.facts_for(stable_security_id)
    return facts[0].cik if facts else None


class ReplayFrameSource(Protocol):
    """The single narrow read seam this module needs from Historical
    Replay -- deliberately not the full ``HistoricalReplayRepository``
    protocol, so attaching corporate evidence never requires a caller to
    implement replay's entire surface. Any ``HistoricalReplayRepository``
    implementation already satisfies this structurally."""

    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]: ...


@dataclass(frozen=True, slots=True)
class AttachCorporateEvidenceToReplayFrameCommand:
    replay_run_id: str
    created_at: datetime
    # Bounded, not unlimited: comfortably covers the real 1,515-frame
    # genuine historical replay (as of this milestone) with headroom,
    # while still refusing to silently walk an unbounded frame table.
    frame_limit: int = 2_000


@dataclass(frozen=True, slots=True)
class ReplayAttachmentRunStats:
    """Bounded performance/coverage counters for one batched attachment
    pass (milestone: Corporate Evidence Completion and Replay Scaling v1,
    section 8) -- deliberately returned alongside, not instead of, the
    attachment tuple every existing caller already depends on."""

    frames_considered: int
    universe_pairs_considered: int
    pairs_already_attached: int
    pairs_newly_attached: int
    pairs_skipped_no_cik: int
    securities_indexed: int
    filings_loaded: int
    facts_loaded: int


class AttachCorporateEvidenceToReplayFrame:
    """Purely additive Historical Replay integration (section 14): reads
    already-persisted ``ReplayFeatureFrame`` rows for one run and, for each
    frame's universe member with any retained corporate evidence, attaches
    a point-in-time snapshot built at *that exact frame's* ``as_of`` --
    never the frame's own fields, and never a provider request. No field
    of ``ReplayFeatureFrame``/``HistoricalReplayEngine`` is read beyond
    ``frame_id``/``as_of``/``universe``, and none is written.

    Batched, index-once implementation (replay-scaling milestone, section
    3): the original per-``(frame, security)`` implementation issued up to
    six repository round trips *for every pair* (two inside ``resolve_cik_
    for_security``, two more re-fetching the identical filings/facts, one
    existence check, one insert) -- fine at the small scale it was first
    proven against, but O(frames x universe) round trips against the real
    1,515-frame replay times out. This version issues a small, constant
    number of bulk queries regardless of frame count -- two bulk reads
    (``filings_for_securities``/``facts_for_securities``) to build one
    in-memory index over exactly the securities appearing in these frames'
    universes, one bulk read of already-retained attachments for this run,
    and a chunked bulk insert of whatever is genuinely new -- then does
    every per-``(frame, security)`` computation in memory.

    Semantics are unchanged, proven by construction, not just by
    inspection: for every pair, this still calls the identical ``build_
    corporate_fundamental_snapshot``/``compute_derived_features``/
    ``ReplayCorporateEvidenceAttachment.create`` sequence with the
    identical arguments the slow path used (same ``as_of``, same *full,
    unfiltered* per-security filing/fact history so each frame's own
    point-in-time cutoff still does its own anti-lookahead filtering
    independently -- nothing is pre-filtered to any single ``as_of`` before
    being distributed across frames). A ``(replay_run_id, frame_id,
    stable_security_id)`` triple already retained from an earlier call is
    never recomputed -- attachment is defined to happen exactly once per
    triple (the migration's own unique index enforces this); the retained
    row is reused as-is. See ``tests/test_corporate_fundamentals_
    application.py`` for the slow-reference-vs-batched equivalence proofs.
    """

    def __init__(
        self,
        *,
        repository: CorporateFundamentalsRepository,
        frame_source: ReplayFrameSource,
    ) -> None:
        self._repository = repository
        self._frame_source = frame_source

    def execute(
        self, command: AttachCorporateEvidenceToReplayFrameCommand
    ) -> tuple[ReplayCorporateEvidenceAttachment, ...]:
        return self.execute_with_stats(command)[0]

    def execute_with_stats(
        self, command: AttachCorporateEvidenceToReplayFrameCommand
    ) -> tuple[tuple[ReplayCorporateEvidenceAttachment, ...], ReplayAttachmentRunStats]:
        frames = self._frame_source.list_frames(command.replay_run_id, limit=command.frame_limit)
        if not frames:
            return (), ReplayAttachmentRunStats(0, 0, 0, 0, 0, 0, 0, 0)

        security_ids = tuple(sorted({sid for frame in frames for sid in frame.universe}))
        filings = self._repository.filings_for_securities(security_ids)
        facts = self._repository.facts_for_securities(security_ids)

        filings_by_security: dict[str, list[CorporateFilingEvent]] = {}
        cik_by_security: dict[str, str] = {}
        for filing_item in filings:
            filings_by_security.setdefault(filing_item.stable_security_id, []).append(filing_item)
            cik_by_security.setdefault(filing_item.stable_security_id, filing_item.cik)
        facts_by_security: dict[str, list[FundamentalFact]] = {}
        for fact_item in facts:
            facts_by_security.setdefault(fact_item.stable_security_id, []).append(fact_item)
            cik_by_security.setdefault(fact_item.stable_security_id, fact_item.cik)

        existing_by_key = {
            (item.frame_id, item.stable_security_id): item
            for item in self._repository.attachments_for_run(command.replay_run_id)
        }

        ordered: list[ReplayCorporateEvidenceAttachment] = []
        to_insert: list[ReplayCorporateEvidenceAttachment] = []
        seen: set[tuple[str, str]] = set()
        pairs_considered = 0
        skipped_no_cik = 0
        for frame in frames:
            for stable_security_id in frame.universe:
                key = (frame.frame_id, stable_security_id)
                if key in seen:
                    continue
                seen.add(key)
                pairs_considered += 1
                existing = existing_by_key.get(key)
                if existing is not None:
                    ordered.append(existing)
                    continue
                cik = cik_by_security.get(stable_security_id)
                if cik is None:
                    skipped_no_cik += 1
                    continue
                issuer_filings = tuple(filings_by_security.get(stable_security_id, ()))
                issuer_facts = tuple(facts_by_security.get(stable_security_id, ()))
                snapshot = build_corporate_fundamental_snapshot(
                    stable_security_id=stable_security_id,
                    cik=cik,
                    as_of=frame.as_of,
                    filings=issuer_filings,
                    facts=issuer_facts,
                )
                features = compute_derived_features(snapshot=snapshot, facts=issuer_facts)
                attachment = ReplayCorporateEvidenceAttachment.create(
                    replay_run_id=command.replay_run_id,
                    frame_id=frame.frame_id,
                    snapshot=snapshot,
                    features=features,
                    created_at=command.created_at,
                )
                ordered.append(attachment)
                to_insert.append(attachment)

        if to_insert:
            self._repository.save_replay_attachments(to_insert)

        stats = ReplayAttachmentRunStats(
            frames_considered=len(frames),
            universe_pairs_considered=pairs_considered,
            pairs_already_attached=len(existing_by_key),
            pairs_newly_attached=len(to_insert),
            pairs_skipped_no_cik=skipped_no_cik,
            securities_indexed=len(security_ids),
            filings_loaded=len(filings),
            facts_loaded=len(facts),
        )
        return tuple(ordered), stats


class ReplayFrameByIdSource(Protocol):
    """A second, narrow read seam from Historical Replay -- one frame
    looked up by id, deliberately separate from ``ReplayFrameSource``'s
    list-by-run seam so a caller needing only one frame is not forced to
    implement both. Any ``HistoricalReplayRepository`` implementation
    already satisfies this structurally."""

    def frame_by_id(self, frame_id: str) -> ReplayFeatureFrame | None: ...


@dataclass(frozen=True, slots=True)
class BuildCorporateReplayFeatureFrameCommand:
    replay_run_id: str
    frame_id: str
    stable_security_id: str


class BuildCorporateReplayFeatureFrame:
    """``corporate-replay-feature-inspect`` (sections 5, 12): ties the
    bounded feature projection to one genuine, already-persisted replay
    frame's exact ``as_of`` rather than a free-form caller-supplied date --
    read-time recomputation only, exactly like ``compute_derived_
    features``/``compute_filing_event_features`` themselves; never
    persisted as its own row."""

    def __init__(
        self,
        *,
        repository: CorporateFundamentalsRepository,
        frame_source: ReplayFrameByIdSource,
    ) -> None:
        self._repository = repository
        self._frame_source = frame_source

    def execute(
        self, command: BuildCorporateReplayFeatureFrameCommand
    ) -> CorporateReplayFeatureFrame:
        frame = self._frame_source.frame_by_id(command.frame_id)
        if frame is None:
            raise CorporateFundamentalsNotFound("REPLAY_FRAME_NOT_FOUND")
        if frame.replay_run_id != command.replay_run_id:
            raise CorporateFundamentalsApplicationError("REPLAY_FRAME_RUN_MISMATCH")
        if command.stable_security_id not in frame.universe:
            raise CorporateFundamentalsApplicationError("SECURITY_NOT_IN_FRAME_UNIVERSE")
        cik = resolve_cik_for_security(self._repository, command.stable_security_id)
        if cik is None:
            raise CorporateFundamentalsNotFound("NO_RETAINED_CIK_EVIDENCE_FOR_SECURITY")
        filings = self._repository.filings_for(command.stable_security_id)
        facts = self._repository.facts_for(command.stable_security_id)
        snapshot = build_corporate_fundamental_snapshot(
            stable_security_id=command.stable_security_id,
            cik=cik,
            as_of=frame.as_of,
            filings=filings,
            facts=facts,
        )
        derived = compute_derived_features(snapshot=snapshot, facts=facts)
        filing_features = compute_filing_event_features(
            stable_security_id=command.stable_security_id, as_of=frame.as_of, filings=filings
        )
        return build_corporate_replay_feature_frame(
            snapshot=snapshot, derived_features=derived, filing_event_features=filing_features
        )


def issuer_quality_assessments_for_plan(
    *, repository: CorporateFundamentalsRepository, plan: CorporateEvidenceCohortPlan
) -> tuple[IssuerQualityAssessment, ...]:
    """Shared read-only helper (CLI/API use) recomputing exactly the same
    per-issuer assessments ``MaterializeCorporateHistoryDataset`` folds
    into a persisted quality report, without requiring one to exist yet."""
    filings_by_security: dict[str, list[CorporateFilingEvent]] = {}
    for filing in repository.all_filings_for_plan(plan.plan_id):
        filings_by_security.setdefault(filing.stable_security_id, []).append(filing)
    facts_by_security: dict[str, list[FundamentalFact]] = {}
    for fact in repository.all_facts_for_plan(plan.plan_id):
        facts_by_security.setdefault(fact.stable_security_id, []).append(fact)
    return tuple(
        assess_issuer_quality(
            stable_security_id=member.stable_security_id,
            symbol=member.symbol,
            cik=member.cik,
            filings=tuple(filings_by_security.get(member.stable_security_id, ())),
            facts=tuple(facts_by_security.get(member.stable_security_id, ())),
        )
        for member in plan.members
    )


def concept_values_over_time(
    facts: Sequence[FundamentalFact], *, concept: FundamentalConcept
) -> tuple[FundamentalFact, ...]:
    """Bounded, read-only convenience: every retained revision of one
    concept for one issuer, chronologically ordered -- the exact evidence
    ``corporate-facts inspect`` and ``revision_chain`` both draw from."""
    return tuple(
        sorted(
            (item for item in facts if item.concept is concept),
            key=lambda item: (item.period_end, item.effective_available_at),
        )
    )
