"""PostgreSQL repository for Corporate Fundamental, Filing, and Event
Intelligence v1 (migration ``0088``).

Follows the identical ``payload``-column pattern already established for
this codebase's other durable evidence families: one canonical JSON
payload column (restored exactly via ``canonical_rehydrate.rehydrate``)
plus a small set of indexed columns for the query shapes the application
layer actually needs. Every table carries ``authority = 'NONE'`` and is
immutable at the database boundary (migration ``0088``'s triggers); every
``save_*`` method here is therefore insert-if-absent, never update, so
retrying an interrupted capture or replaying an identical dataset
materialization is naturally idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.corporate_fundamentals import (
    CorporateDataQualityReport,
    CorporateEvidenceCaptureCheckpoint,
    CorporateEvidenceCohortPlan,
    CorporateFilingEvent,
    CorporateHistoryDatasetSnapshot,
    FundamentalFact,
    IssuerCaptureStatus,
    ReplayCorporateEvidenceAttachment,
)
from apex.domain.values import canonical_json_bytes

CORPORATE_FUNDAMENTALS_TABLES = (
    "corporate_evidence_cohort_plan",
    "corporate_filing_event",
    "corporate_fundamental_fact",
    "corporate_evidence_capture_checkpoint",
    "corporate_data_quality_report",
    "corporate_history_dataset_snapshot",
    "replay_corporate_evidence_attachment",
)
CORPORATE_FUNDAMENTALS_IMMUTABLE_TABLES = CORPORATE_FUNDAMENTALS_TABLES

metadata = sa.MetaData()

corporate_evidence_cohort_plan = sa.Table(
    "corporate_evidence_cohort_plan",
    metadata,
    sa.Column("plan_id", sa.String(160), primary_key=True),
    sa.Column("cohort_policy_id", sa.String(160), nullable=False),
    sa.Column("cohort_policy_hash", sa.String(64), nullable=False),
    sa.Column("bounds_id", sa.String(160), nullable=False),
    sa.Column("member_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_filing_event = sa.Table(
    "corporate_filing_event",
    metadata,
    sa.Column("filing_event_id", sa.String(160), primary_key=True),
    sa.Column("stable_security_id", sa.String(160), nullable=False),
    sa.Column("cik", sa.String(10), nullable=False),
    sa.Column("accession_number", sa.String(20), nullable=False, unique=True),
    sa.Column("form", sa.String(10), nullable=False),
    sa.Column("amendment_state", sa.String(20), nullable=False),
    sa.Column("report_period", sa.String(10), nullable=True),
    sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("primary_document", sa.String(300), nullable=False),
    sa.Column("source_url", sa.Text(), nullable=False),
    sa.Column("file_number", sa.String(40), nullable=True),
    sa.Column("is_xbrl", sa.Boolean(), nullable=False),
    sa.Column("is_inline_xbrl", sa.Boolean(), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_fundamental_fact = sa.Table(
    "corporate_fundamental_fact",
    metadata,
    sa.Column("fact_id", sa.String(160), primary_key=True),
    sa.Column("stable_security_id", sa.String(160), nullable=False),
    sa.Column("cik", sa.String(10), nullable=False),
    sa.Column("concept", sa.String(40), nullable=False),
    sa.Column("source_taxonomy", sa.String(20), nullable=False),
    sa.Column("source_tag", sa.String(160), nullable=False),
    sa.Column("unit", sa.String(20), nullable=False),
    sa.Column("period_type", sa.String(10), nullable=False),
    sa.Column("fiscal_year", sa.Integer(), nullable=False),
    sa.Column("fiscal_period", sa.String(4), nullable=False),
    sa.Column("period_start", sa.String(10), nullable=True),
    sa.Column("period_end", sa.String(10), nullable=False),
    sa.Column("value", sa.Numeric(38, 8), nullable=False),
    sa.Column("accession_number", sa.String(20), nullable=False),
    sa.Column("form", sa.String(10), nullable=False),
    sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("frame", sa.String(40), nullable=True),
    sa.Column("is_ytd_derived", sa.Boolean(), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_evidence_capture_checkpoint = sa.Table(
    "corporate_evidence_capture_checkpoint",
    metadata,
    sa.Column("checkpoint_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(160),
        sa.ForeignKey("corporate_evidence_cohort_plan.plan_id"),
        nullable=False,
    ),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("issuers_attempted_count", sa.Integer(), nullable=False),
    sa.Column("issuers_captured_count", sa.Integer(), nullable=False),
    sa.Column("total_requests_this_run", sa.Integer(), nullable=False),
    sa.Column("total_bytes_this_run", sa.BigInteger(), nullable=False),
    sa.Column("runtime_seconds", sa.Numeric(12, 3), nullable=False),
    sa.Column("complete", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_data_quality_report = sa.Table(
    "corporate_data_quality_report",
    metadata,
    sa.Column("report_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(160),
        sa.ForeignKey("corporate_evidence_cohort_plan.plan_id"),
        nullable=False,
    ),
    sa.Column("issuers_requested", sa.Integer(), nullable=False),
    sa.Column("issuers_captured", sa.Integer(), nullable=False),
    sa.Column("point_in_time_completeness", sa.String(20), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_history_dataset_snapshot = sa.Table(
    "corporate_history_dataset_snapshot",
    metadata,
    sa.Column("dataset_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(160),
        sa.ForeignKey("corporate_evidence_cohort_plan.plan_id"),
        nullable=False,
    ),
    sa.Column("fact_count", sa.Integer(), nullable=False),
    sa.Column("filing_count", sa.Integer(), nullable=False),
    sa.Column(
        "quality_report_id",
        sa.String(160),
        sa.ForeignKey("corporate_data_quality_report.report_id"),
        nullable=False,
    ),
    sa.Column("completeness", sa.String(20), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

replay_corporate_evidence_attachment = sa.Table(
    "replay_corporate_evidence_attachment",
    metadata,
    sa.Column("attachment_id", sa.String(160), primary_key=True),
    sa.Column("replay_run_id", sa.String(160), nullable=False),
    sa.Column("frame_id", sa.String(160), nullable=False),
    sa.Column("stable_security_id", sa.String(160), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("snapshot_hash", sa.String(64), nullable=False),
    sa.Column("snapshot_quality", sa.String(30), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class CorporateFundamentalsPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CorporateFundamentalsPersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _decoded_with_defaults(row: RowMapping, defaults: dict[str, Any]) -> dict[str, Any]:
    """Identical to ``_decoded`` except any key absent from the stored
    payload is backfilled from ``defaults`` first. Exists solely so the
    small number of ``corporate_data_quality_report``/``corporate_history_
    dataset_snapshot`` rows persisted before the Corporate Evidence
    Completion and Replay Scaling v1 milestone added new fields to those
    two payload dataclasses keep reading back cleanly -- every row created
    from this milestone onward always carries every key directly and this
    path is a no-op for them. Never used to paper over a missing field on
    any *other* table; genuinely missing required evidence should still
    fail loudly via plain ``_decoded``."""
    value = _decoded(row)
    for key, default in defaults.items():
        value.setdefault(key, default)
    return value


# Backward-compatibility defaults for the pre-lookback-policy quality report
# and dataset snapshot rows retained from before this milestone -- obvious
# sentinel values, never mistaken for genuine measurements.
_QUALITY_REPORT_LEGACY_DEFAULTS: dict[str, Any] = {
    "issuers_not_yet_attempted": [],
    "issuers_attempted_zero_evidence": [],
    "quality_state_counts": [],
    "period_gap_count": 0,
    "unit_mismatch_count": 0,
}
_DATASET_SNAPSHOT_LEGACY_DEFAULTS: dict[str, Any] = {
    "lookback_years": 0,
    "lookback_cutoff_date": "UNKNOWN_PRE_LOOKBACK_POLICY",
    "lookback_retention_floor_date": "UNKNOWN_PRE_LOOKBACK_POLICY",
    "lookback_excluded_filing_count": 0,
    "lookback_excluded_fact_count": 0,
    "lookback_predecessor_only_fact_count": 0,
}


class PostgresCorporateFundamentalsRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- plan ---------------------------------------------------------------

    def save_plan(self, plan: CorporateEvidenceCohortPlan) -> CorporateEvidenceCohortPlan:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(corporate_evidence_cohort_plan.c.plan_id).where(
                        corporate_evidence_cohort_plan.c.plan_id == plan.plan_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        corporate_evidence_cohort_plan.insert().values(
                            plan_id=plan.plan_id,
                            cohort_policy_id=plan.cohort_policy_id,
                            cohort_policy_hash=plan.cohort_policy_hash,
                            bounds_id=plan.bounds.bounds_id,
                            member_count=len(plan.members),
                            created_at=plan.created_at,
                            payload=_payload(plan),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError("PLAN_PERSISTENCE_CONFLICT") from error
        return plan

    def plan_by_id(self, plan_id: str) -> CorporateEvidenceCohortPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_evidence_cohort_plan).where(
                        corporate_evidence_cohort_plan.c.plan_id == plan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return rehydrate(CorporateEvidenceCohortPlan, _decoded(row)) if row is not None else None

    def latest_plan(self) -> CorporateEvidenceCohortPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_evidence_cohort_plan)
                    .order_by(corporate_evidence_cohort_plan.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return rehydrate(CorporateEvidenceCohortPlan, _decoded(row)) if row is not None else None

    # -- filings --------------------------------------------------------------

    def save_filing(self, filing: CorporateFilingEvent) -> CorporateFilingEvent:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(corporate_filing_event.c.filing_event_id).where(
                        corporate_filing_event.c.filing_event_id == filing.filing_event_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        corporate_filing_event.insert().values(
                            filing_event_id=filing.filing_event_id,
                            stable_security_id=filing.stable_security_id,
                            cik=filing.cik,
                            accession_number=filing.accession_number,
                            form=filing.form.value,
                            amendment_state=filing.amendment_state.value,
                            report_period=filing.report_period,
                            filed_at=filing.filed_at,
                            accepted_at=filing.accepted_at,
                            primary_document=filing.primary_document,
                            source_url=filing.source_url,
                            file_number=filing.file_number,
                            is_xbrl=filing.is_xbrl,
                            is_inline_xbrl=filing.is_inline_xbrl,
                            payload=_payload(filing),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError("FILING_PERSISTENCE_CONFLICT") from error
        return filing

    def filings_for(self, stable_security_id: str) -> tuple[CorporateFilingEvent, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_filing_event)
                .where(corporate_filing_event.c.stable_security_id == stable_security_id)
                .order_by(
                    corporate_filing_event.c.filed_at, corporate_filing_event.c.filing_event_id
                )
            ).mappings()
            return tuple(rehydrate(CorporateFilingEvent, _decoded(row)) for row in rows)

    def filing_by_accession(self, accession_number: str) -> CorporateFilingEvent | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_filing_event).where(
                        corporate_filing_event.c.accession_number == accession_number
                    )
                )
                .mappings()
                .one_or_none()
            )
        return rehydrate(CorporateFilingEvent, _decoded(row)) if row is not None else None

    def all_filings_for_plan(self, plan_id: str) -> tuple[CorporateFilingEvent, ...]:
        plan = self.plan_by_id(plan_id)
        if plan is None:
            return ()
        security_ids = tuple(member.stable_security_id for member in plan.members)
        if not security_ids:
            return ()
        return self.filings_for_securities(security_ids)

    def filings_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[CorporateFilingEvent, ...]:
        """One bounded bulk query by stable security id -- the batched
        replay-attachment pass's read side (milestone: Corporate Evidence
        Completion and Replay Scaling v1, section 3): a caller loads every
        needed issuer's filings once, up front, into an in-memory index,
        instead of one ``filings_for`` round trip per (frame, security)
        pair."""
        ids = tuple(dict.fromkeys(stable_security_ids))
        if not ids:
            return ()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_filing_event)
                .where(corporate_filing_event.c.stable_security_id.in_(ids))
                .order_by(
                    corporate_filing_event.c.stable_security_id,
                    corporate_filing_event.c.filed_at,
                    corporate_filing_event.c.filing_event_id,
                )
            ).mappings()
            return tuple(rehydrate(CorporateFilingEvent, _decoded(row)) for row in rows)

    # -- facts ----------------------------------------------------------------

    def save_fact(self, fact: FundamentalFact) -> FundamentalFact:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(corporate_fundamental_fact.c.fact_id).where(
                        corporate_fundamental_fact.c.fact_id == fact.fact_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        corporate_fundamental_fact.insert().values(
                            fact_id=fact.fact_id,
                            stable_security_id=fact.stable_security_id,
                            cik=fact.cik,
                            concept=fact.concept.value,
                            source_taxonomy=fact.source_taxonomy,
                            source_tag=fact.source_tag,
                            unit=fact.unit.value,
                            period_type=fact.period_type.value,
                            fiscal_year=fact.fiscal_year,
                            fiscal_period=fact.fiscal_period.value,
                            period_start=fact.period_start,
                            period_end=fact.period_end,
                            value=fact.value,
                            accession_number=fact.accession_number,
                            form=fact.form,
                            filed_at=fact.filed_at,
                            accepted_at=fact.accepted_at,
                            frame=fact.frame,
                            is_ytd_derived=fact.is_ytd_derived,
                            payload=_payload(fact),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError("FACT_PERSISTENCE_CONFLICT") from error
        return fact

    def facts_for(self, stable_security_id: str) -> tuple[FundamentalFact, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_fundamental_fact)
                .where(corporate_fundamental_fact.c.stable_security_id == stable_security_id)
                .order_by(
                    corporate_fundamental_fact.c.period_end, corporate_fundamental_fact.c.fact_id
                )
            ).mappings()
            return tuple(rehydrate(FundamentalFact, _decoded(row)) for row in rows)

    def all_facts_for_plan(self, plan_id: str) -> tuple[FundamentalFact, ...]:
        plan = self.plan_by_id(plan_id)
        if plan is None:
            return ()
        security_ids = tuple(member.stable_security_id for member in plan.members)
        if not security_ids:
            return ()
        return self.facts_for_securities(security_ids)

    def facts_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[FundamentalFact, ...]:
        """Bulk counterpart to ``filings_for_securities`` -- see its
        docstring. Both lean on this table's existing ``ix_cff_security_
        concept_period``/``ix_cff_security_accepted`` indexes (migration
        0088, already leading with ``stable_security_id``), so no new index
        or migration is needed to make this bulk shape efficient."""
        ids = tuple(dict.fromkeys(stable_security_ids))
        if not ids:
            return ()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_fundamental_fact)
                .where(corporate_fundamental_fact.c.stable_security_id.in_(ids))
                .order_by(
                    corporate_fundamental_fact.c.stable_security_id,
                    corporate_fundamental_fact.c.period_end,
                    corporate_fundamental_fact.c.fact_id,
                )
            ).mappings()
            return tuple(rehydrate(FundamentalFact, _decoded(row)) for row in rows)

    # -- checkpoints ------------------------------------------------------------

    def save_checkpoint(
        self, checkpoint: CorporateEvidenceCaptureCheckpoint
    ) -> CorporateEvidenceCaptureCheckpoint:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(corporate_evidence_capture_checkpoint.c.checkpoint_id).where(
                        corporate_evidence_capture_checkpoint.c.checkpoint_id
                        == checkpoint.checkpoint_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        corporate_evidence_capture_checkpoint.insert().values(
                            checkpoint_id=checkpoint.checkpoint_id,
                            plan_id=checkpoint.plan_id,
                            run_id=checkpoint.run_id,
                            issuers_attempted_count=len(checkpoint.outcomes),
                            issuers_captured_count=sum(
                                1
                                for item in checkpoint.outcomes
                                if item.status is IssuerCaptureStatus.CAPTURED
                            ),
                            total_requests_this_run=checkpoint.total_requests_this_run,
                            total_bytes_this_run=checkpoint.total_bytes_this_run,
                            runtime_seconds=checkpoint.runtime_seconds,
                            complete=checkpoint.complete,
                            created_at=checkpoint.created_at,
                            payload=_payload(checkpoint),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError(
                "CHECKPOINT_PERSISTENCE_CONFLICT"
            ) from error
        return checkpoint

    def checkpoints_for_plan(self, plan_id: str) -> tuple[CorporateEvidenceCaptureCheckpoint, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_evidence_capture_checkpoint)
                .where(corporate_evidence_capture_checkpoint.c.plan_id == plan_id)
                .order_by(corporate_evidence_capture_checkpoint.c.created_at)
            ).mappings()
            return tuple(
                rehydrate(CorporateEvidenceCaptureCheckpoint, _decoded(row)) for row in rows
            )

    def latest_checkpoint(self, plan_id: str) -> CorporateEvidenceCaptureCheckpoint | None:
        checkpoints = self.checkpoints_for_plan(plan_id)
        return checkpoints[-1] if checkpoints else None

    def processed_ciks(self, plan_id: str) -> tuple[str, ...]:
        latest = self.latest_checkpoint(plan_id)
        return latest.cumulative_processed_ciks if latest is not None else ()

    # -- quality report -----------------------------------------------------

    def save_quality_report(self, report: CorporateDataQualityReport) -> CorporateDataQualityReport:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(corporate_data_quality_report.c.report_id).where(
                        corporate_data_quality_report.c.report_id == report.report_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        corporate_data_quality_report.insert().values(
                            report_id=report.report_id,
                            plan_id=report.plan_id,
                            issuers_requested=report.issuers_requested,
                            issuers_captured=report.issuers_captured,
                            point_in_time_completeness=report.point_in_time_completeness,
                            created_at=report.created_at,
                            payload=_payload(report),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError(
                "QUALITY_REPORT_PERSISTENCE_CONFLICT"
            ) from error
        return report

    def quality_report_by_id(self, report_id: str) -> CorporateDataQualityReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_data_quality_report).where(
                        corporate_data_quality_report.c.report_id == report_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        decoded = _decoded_with_defaults(row, _QUALITY_REPORT_LEGACY_DEFAULTS)
        return rehydrate(CorporateDataQualityReport, decoded)

    def latest_quality_report(self, plan_id: str) -> CorporateDataQualityReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_data_quality_report)
                    .where(corporate_data_quality_report.c.plan_id == plan_id)
                    .order_by(corporate_data_quality_report.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        decoded = _decoded_with_defaults(row, _QUALITY_REPORT_LEGACY_DEFAULTS)
        return rehydrate(CorporateDataQualityReport, decoded)

    # -- dataset snapshot ------------------------------------------------------

    def save_dataset_snapshot(
        self, dataset: CorporateHistoryDatasetSnapshot
    ) -> CorporateHistoryDatasetSnapshot:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(corporate_history_dataset_snapshot.c.dataset_id).where(
                        corporate_history_dataset_snapshot.c.dataset_id == dataset.dataset_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        corporate_history_dataset_snapshot.insert().values(
                            dataset_id=dataset.dataset_id,
                            plan_id=dataset.plan_id,
                            fact_count=len(dataset.fact_ids),
                            filing_count=len(dataset.filing_event_ids),
                            quality_report_id=dataset.quality_report_id,
                            completeness=dataset.completeness,
                            created_at=dataset.created_at,
                            payload=_payload(dataset),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError("DATASET_PERSISTENCE_CONFLICT") from error
        return dataset

    def dataset_snapshot_by_id(self, dataset_id: str) -> CorporateHistoryDatasetSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_history_dataset_snapshot).where(
                        corporate_history_dataset_snapshot.c.dataset_id == dataset_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        decoded = _decoded_with_defaults(row, _DATASET_SNAPSHOT_LEGACY_DEFAULTS)
        return rehydrate(CorporateHistoryDatasetSnapshot, decoded)

    def latest_dataset_snapshot(self, plan_id: str) -> CorporateHistoryDatasetSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_history_dataset_snapshot)
                    .where(corporate_history_dataset_snapshot.c.plan_id == plan_id)
                    .order_by(corporate_history_dataset_snapshot.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        decoded = _decoded_with_defaults(row, _DATASET_SNAPSHOT_LEGACY_DEFAULTS)
        return rehydrate(CorporateHistoryDatasetSnapshot, decoded)

    # -- replay attachment --------------------------------------------------

    def save_replay_attachment(
        self, attachment: ReplayCorporateEvidenceAttachment
    ) -> ReplayCorporateEvidenceAttachment:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(replay_corporate_evidence_attachment.c.attachment_id).where(
                        replay_corporate_evidence_attachment.c.attachment_id
                        == attachment.attachment_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        replay_corporate_evidence_attachment.insert().values(
                            attachment_id=attachment.attachment_id,
                            replay_run_id=attachment.replay_run_id,
                            frame_id=attachment.frame_id,
                            stable_security_id=attachment.stable_security_id,
                            as_of=attachment.as_of,
                            snapshot_hash=attachment.snapshot_hash,
                            snapshot_quality=attachment.snapshot_quality.value,
                            created_at=attachment.created_at,
                            payload=_payload(attachment),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise CorporateFundamentalsPersistenceError(
                "ATTACHMENT_PERSISTENCE_CONFLICT"
            ) from error
        return attachment

    def replay_attachment_by_id(
        self, attachment_id: str
    ) -> ReplayCorporateEvidenceAttachment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(replay_corporate_evidence_attachment).where(
                        replay_corporate_evidence_attachment.c.attachment_id == attachment_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return (
            rehydrate(ReplayCorporateEvidenceAttachment, _decoded(row)) if row is not None else None
        )

    def attachments_for_run(
        self, replay_run_id: str
    ) -> tuple[ReplayCorporateEvidenceAttachment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(replay_corporate_evidence_attachment)
                .where(replay_corporate_evidence_attachment.c.replay_run_id == replay_run_id)
                .order_by(replay_corporate_evidence_attachment.c.created_at)
            ).mappings()
            return tuple(
                rehydrate(ReplayCorporateEvidenceAttachment, _decoded(row)) for row in rows
            )

    def attachment_run_summary(self) -> tuple[tuple[str, int], ...]:
        """One bounded row per distinct replay run this repository has ever
        attached corporate evidence to, with its retained attachment count
        -- the workspace-level "replay-attachment performance" surface
        (per-run detail belongs to ``corporate-replay-attach``'s own
        output, not this summary)."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    replay_corporate_evidence_attachment.c.replay_run_id,
                    sa.func.count().label("attachment_count"),
                )
                .group_by(replay_corporate_evidence_attachment.c.replay_run_id)
                .order_by(replay_corporate_evidence_attachment.c.replay_run_id)
            )
            return tuple((row.replay_run_id, int(row.attachment_count)) for row in rows)

    _ATTACHMENT_INSERT_CHUNK_SIZE = 500

    def save_replay_attachments(
        self, attachments: Sequence[ReplayCorporateEvidenceAttachment]
    ) -> int:
        """Bulk insert-if-absent for the batched replay-attachment pass
        (milestone: Corporate Evidence Completion and Replay Scaling v1,
        section 3). Callers are expected to have already filtered out
        attachment keys retained by an earlier run (``attachments_for_
        run``); this still tolerates a genuine collision (e.g. a
        concurrent run) by falling back to the identical single-row
        insert-if-absent path used everywhere else in this repository, one
        chunk at a time, so a rare race never loses or duplicates a row.
        Never touches ``UPDATE``/``DELETE`` -- purely additive ``INSERT``,
        fully compatible with this table's immutability trigger."""
        items = list(attachments)
        if not items:
            return 0
        saved = 0
        for start in range(0, len(items), self._ATTACHMENT_INSERT_CHUNK_SIZE):
            chunk = items[start : start + self._ATTACHMENT_INSERT_CHUNK_SIZE]
            rows = [
                {
                    "attachment_id": item.attachment_id,
                    "replay_run_id": item.replay_run_id,
                    "frame_id": item.frame_id,
                    "stable_security_id": item.stable_security_id,
                    "as_of": item.as_of,
                    "snapshot_hash": item.snapshot_hash,
                    "snapshot_quality": item.snapshot_quality.value,
                    "created_at": item.created_at,
                    "payload": _payload(item),
                    "authority": "NONE",
                }
                for item in chunk
            ]
            try:
                with self._engine.begin() as connection:
                    connection.execute(replay_corporate_evidence_attachment.insert(), rows)
                saved += len(chunk)
            except IntegrityError:
                for item in chunk:
                    if self.replay_attachment_by_id(item.attachment_id) is None:
                        self.save_replay_attachment(item)
                        saved += 1
        return saved
