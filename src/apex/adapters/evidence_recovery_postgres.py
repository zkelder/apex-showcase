"""Immutable PostgreSQL evidence for Historical Evidence Gap Recovery v1.

Three append-only tables: the recovery run, the coverage-gap event ledger, and
the evidence-vintage sidecar. Nothing here rewrites an existing evidence row;
authority is ``NONE`` at the database boundary.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.evidence_recovery import RetainedBar, RetainedInstrument
from apex.domain.evidence_recovery import (
    RECOVERABLE_FAMILIES,
    AcquisitionPath,
    BarValues,
    EvidenceCoverageGapEvent,
    EvidenceFamily,
    EvidenceRecoveryError,
    EvidenceRecoveryRun,
    EvidenceVintage,
    GapDisposition,
    RecoveryRunStatus,
    build_acquisition_path_correction,
    verify_evidence_gap_event,
    verify_evidence_recovery_run,
    verify_evidence_vintage,
)

metadata = sa.MetaData()

evidence_recovery_run_table = sa.Table(
    "evidence_recovery_run",
    metadata,
    sa.Column("run_id", sa.String(300), primary_key=True),
    sa.Column("evidence_family", sa.String(40), nullable=False),
    sa.Column("acquisition_path", sa.String(24), nullable=False),
    sa.Column("cycle_key", sa.String(300), nullable=False),
    sa.Column("scope_start", sa.String(40), nullable=False),
    sa.Column("scope_end", sa.String(40), nullable=False),
    sa.Column("gap_keys", sa.Text(), nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("request_ceiling", sa.Integer, nullable=False),
    sa.Column("provider_requests", sa.Integer, nullable=False),
    sa.Column("gaps_examined", sa.Integer, nullable=False),
    sa.Column("gaps_recovered", sa.Integer, nullable=False),
    sa.Column("gaps_partial", sa.Integer, nullable=False),
    sa.Column("gaps_irrecoverable", sa.Integer, nullable=False),
    sa.Column("gaps_conflicted", sa.Integer, nullable=False),
    sa.Column("reason_codes", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

evidence_coverage_gap_table = sa.Table(
    "evidence_coverage_gap",
    metadata,
    sa.Column("event_id", sa.String(300), primary_key=True),
    sa.Column("gap_key", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("evidence_family", sa.String(40), nullable=False),
    sa.Column("subject_id", sa.String(300), nullable=False),
    sa.Column("interval_start", sa.String(40), nullable=False),
    sa.Column("interval_end", sa.String(40), nullable=False),
    sa.Column("disposition", sa.String(24), nullable=False),
    sa.Column("reason_codes", sa.Text(), nullable=False),
    sa.Column(
        "recovery_run_id",
        sa.String(300),
        sa.ForeignKey("evidence_recovery_run.run_id"),
        nullable=True,
    ),
    sa.Column("recovered_evidence_ids", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    sa.UniqueConstraint("gap_key", "ordinal", name="uq_evidence_coverage_gap_key_ordinal"),
)

evidence_vintage_table = sa.Table(
    "evidence_vintage",
    metadata,
    sa.Column("vintage_id", sa.String(300), primary_key=True),
    sa.Column("evidence_family", sa.String(40), nullable=False),
    sa.Column("evidence_id", sa.String(300), nullable=False),
    sa.Column("market_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("acquisition_path", sa.String(24), nullable=False),
    sa.Column(
        "recovery_run_id",
        sa.String(300),
        sa.ForeignKey("evidence_recovery_run.run_id"),
        nullable=True,
    ),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    sa.UniqueConstraint(
        "evidence_family", "evidence_id", "acquisition_path", name="uq_evidence_vintage_identity"
    ),
)

EVIDENCE_RECOVERY_TABLES = (
    "evidence_recovery_run",
    "evidence_coverage_gap",
    "evidence_vintage",
)


def _time(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _run_from_row(row: RowMapping) -> EvidenceRecoveryRun:
    return verify_evidence_recovery_run(
        EvidenceRecoveryRun(
            run_id=row["run_id"],
            evidence_family=EvidenceFamily(row["evidence_family"]),
            acquisition_path=AcquisitionPath(row["acquisition_path"]),
            cycle_key=row["cycle_key"],
            scope_start=row["scope_start"],
            scope_end=row["scope_end"],
            gap_keys=tuple(json.loads(row["gap_keys"])),
            status=RecoveryRunStatus(row["status"]),
            request_ceiling=row["request_ceiling"],
            provider_requests=row["provider_requests"],
            gaps_examined=row["gaps_examined"],
            gaps_recovered=row["gaps_recovered"],
            gaps_partial=row["gaps_partial"],
            gaps_irrecoverable=row["gaps_irrecoverable"],
            gaps_conflicted=row["gaps_conflicted"],
            reason_codes=tuple(json.loads(row["reason_codes"])),
            canonical_hash=row["canonical_hash"],
            authority=row["authority"],
        )
    )


def _gap_from_row(row: RowMapping) -> EvidenceCoverageGapEvent:
    return verify_evidence_gap_event(
        EvidenceCoverageGapEvent(
            event_id=row["event_id"],
            gap_key=row["gap_key"],
            ordinal=row["ordinal"],
            evidence_family=EvidenceFamily(row["evidence_family"]),
            subject_id=row["subject_id"],
            interval_start=row["interval_start"],
            interval_end=row["interval_end"],
            disposition=GapDisposition(row["disposition"]),
            reason_codes=tuple(json.loads(row["reason_codes"])),
            recovery_run_id=row["recovery_run_id"],
            recovered_evidence_ids=tuple(json.loads(row["recovered_evidence_ids"])),
            canonical_hash=row["canonical_hash"],
            authority=row["authority"],
        )
    )


def _vintage_from_row(row: RowMapping) -> EvidenceVintage:
    return verify_evidence_vintage(
        EvidenceVintage(
            vintage_id=row["vintage_id"],
            evidence_family=EvidenceFamily(row["evidence_family"]),
            evidence_id=row["evidence_id"],
            market_time=_time(row["market_time"]),
            available_at=None if row["available_at"] is None else _time(row["available_at"]),
            acquired_at=_time(row["acquired_at"]),
            acquisition_path=AcquisitionPath(row["acquisition_path"]),
            recovery_run_id=row["recovery_run_id"],
            canonical_hash=row["canonical_hash"],
            authority=row["authority"],
        )
    )


def _write_run(connection: Connection, run: EvidenceRecoveryRun, recorded_at: datetime) -> bool:
    table = evidence_recovery_run_table
    existing = connection.execute(
        sa.select(table.c.canonical_hash).where(table.c.run_id == run.run_id)
    ).scalar_one_or_none()
    if existing is not None:
        if existing != run.canonical_hash:
            raise EvidenceRecoveryError("EVIDENCE_RECOVERY_RUN_IDENTITY_CONFLICT")
        return True
    connection.execute(
        table.insert().values(
            run_id=run.run_id,
            evidence_family=run.evidence_family.value,
            acquisition_path=run.acquisition_path.value,
            cycle_key=run.cycle_key,
            scope_start=run.scope_start,
            scope_end=run.scope_end,
            gap_keys=json.dumps(run.gap_keys),
            status=run.status.value,
            request_ceiling=run.request_ceiling,
            provider_requests=run.provider_requests,
            gaps_examined=run.gaps_examined,
            gaps_recovered=run.gaps_recovered,
            gaps_partial=run.gaps_partial,
            gaps_irrecoverable=run.gaps_irrecoverable,
            gaps_conflicted=run.gaps_conflicted,
            reason_codes=json.dumps(run.reason_codes),
            canonical_hash=run.canonical_hash,
            recorded_at=recorded_at,
            authority=run.authority,
        )
    )
    return False


def _write_gap_event(
    connection: Connection, event: EvidenceCoverageGapEvent, recorded_at: datetime
) -> bool:
    """Idempotent append. A different event at an occupied ordinal, or an
    illegal transition from the retained head, is refused."""

    verify_evidence_gap_event(event)
    table = evidence_coverage_gap_table
    rows = (
        connection.execute(
            sa.select(table).where(table.c.gap_key == event.gap_key).order_by(table.c.ordinal)
        )
        .mappings()
        .all()
    )
    history = tuple(_gap_from_row(row) for row in rows)
    for prior in history:
        if prior.ordinal == event.ordinal:
            if prior.canonical_hash != event.canonical_hash:
                raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_IDENTITY_CONFLICT")
            return True
    head = history[-1] if history else None
    if head is None:
        if event.ordinal != 1:
            raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_OUT_OF_SEQUENCE")
    else:
        if event.ordinal != head.ordinal + 1:
            raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_OUT_OF_SEQUENCE")
        # Re-derive the transition to prove it is legal.
        head.transition(
            disposition=event.disposition,
            reason_codes=event.reason_codes,
            recovery_run_id=event.recovery_run_id,
            recovered_evidence_ids=event.recovered_evidence_ids,
            interval_end=event.interval_end,
        )
    connection.execute(
        table.insert().values(
            event_id=event.event_id,
            gap_key=event.gap_key,
            ordinal=event.ordinal,
            evidence_family=event.evidence_family.value,
            subject_id=event.subject_id,
            interval_start=event.interval_start,
            interval_end=event.interval_end,
            disposition=event.disposition.value,
            reason_codes=json.dumps(event.reason_codes),
            recovery_run_id=event.recovery_run_id,
            recovered_evidence_ids=json.dumps(event.recovered_evidence_ids),
            canonical_hash=event.canonical_hash,
            recorded_at=recorded_at,
            authority=event.authority,
        )
    )
    return False


def _write_vintages(
    connection: Connection, vintages: tuple[EvidenceVintage, ...], recorded_at: datetime
) -> int:
    table = evidence_vintage_table
    fresh: list[dict[str, object]] = []
    for vintage in vintages:
        verify_evidence_vintage(vintage)
        if (
            vintage.acquisition_path is AcquisitionPath.SCHEDULED_LIVE
            and vintage.recovery_run_id is not None
        ):
            # Only a recovery run carries a run id, and recovery is never
            # scheduled-live capture. Legacy rows are corrected, not re-written.
            raise EvidenceRecoveryError("SCHEDULED_LIVE_VINTAGE_CANNOT_CARRY_RECOVERY_RUN")
        existing = connection.execute(
            sa.select(table.c.canonical_hash).where(table.c.vintage_id == vintage.vintage_id)
        ).scalar_one_or_none()
        if existing is not None:
            if existing != vintage.canonical_hash:
                raise EvidenceRecoveryError("EVIDENCE_VINTAGE_IDENTITY_CONFLICT")
            continue
        fresh.append(
            {
                "vintage_id": vintage.vintage_id,
                "evidence_family": vintage.evidence_family.value,
                "evidence_id": vintage.evidence_id,
                "market_time": vintage.market_time,
                "available_at": vintage.available_at,
                "acquired_at": vintage.acquired_at,
                "acquisition_path": vintage.acquisition_path.value,
                "recovery_run_id": vintage.recovery_run_id,
                "canonical_hash": vintage.canonical_hash,
                "recorded_at": recorded_at,
                "authority": vintage.authority,
            }
        )
    for start in range(0, len(fresh), 500):
        connection.execute(table.insert(), fresh[start : start + 500])
    return len(fresh)


class PostgresEvidenceRecoveryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- runs -----------------------------------------------------------

    def register_run(self, *, run: EvidenceRecoveryRun, recorded_at: datetime) -> bool:
        """Idempotent. ``True`` means an identical run was already retained."""

        verify_evidence_recovery_run(run)
        try:
            with self._engine.begin() as connection:
                return _write_run(connection, run, recorded_at)
        except IntegrityError as error:
            raise EvidenceRecoveryError("EVIDENCE_RECOVERY_RUN_PERSISTENCE_CONFLICT") from error

    def run(self, run_id: str) -> EvidenceRecoveryRun | None:
        table = evidence_recovery_run_table
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(table.c.run_id == run_id))
                .mappings()
                .first()
            )
        return None if row is None else _run_from_row(row)

    def runs(self) -> tuple[EvidenceRecoveryRun, ...]:
        table = evidence_recovery_run_table
        with self._engine.connect() as connection:
            rows = (
                connection.execute(sa.select(table).order_by(table.c.recorded_at, table.c.run_id))
                .mappings()
                .all()
            )
        return tuple(_run_from_row(row) for row in rows)

    # -- gaps -----------------------------------------------------------

    def register_gap_event(self, *, event: EvidenceCoverageGapEvent, recorded_at: datetime) -> bool:
        try:
            with self._engine.begin() as connection:
                return _write_gap_event(connection, event, recorded_at)
        except IntegrityError as error:
            raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_PERSISTENCE_CONFLICT") from error

    def gap_history(self, gap_key: str) -> tuple[EvidenceCoverageGapEvent, ...]:
        table = evidence_coverage_gap_table
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(table).where(table.c.gap_key == gap_key).order_by(table.c.ordinal)
                )
                .mappings()
                .all()
            )
        return tuple(_gap_from_row(row) for row in rows)

    def current_gaps(
        self, *, evidence_family: EvidenceFamily | None = None
    ) -> tuple[EvidenceCoverageGapEvent, ...]:
        """Head event of every gap, ordered deterministically by interval then subject."""

        table = evidence_coverage_gap_table
        latest = (
            sa.select(table.c.gap_key, sa.func.max(table.c.ordinal).label("ordinal"))
            .group_by(table.c.gap_key)
            .subquery()
        )
        statement = sa.select(table).join(
            latest,
            sa.and_(table.c.gap_key == latest.c.gap_key, table.c.ordinal == latest.c.ordinal),
        )
        if evidence_family is not None:
            statement = statement.where(table.c.evidence_family == evidence_family.value)
        statement = statement.order_by(table.c.interval_start, table.c.subject_id, table.c.gap_key)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_gap_from_row(row) for row in rows)

    def unresolved_gaps(
        self, *, evidence_family: EvidenceFamily
    ) -> tuple[EvidenceCoverageGapEvent, ...]:
        return tuple(
            item
            for item in self.current_gaps(evidence_family=evidence_family)
            if item.disposition in (GapDisposition.OPEN, GapDisposition.PARTIALLY_RECOVERED)
        )

    # -- vintages -------------------------------------------------------

    def register_vintages(
        self, *, vintages: tuple[EvidenceVintage, ...], recorded_at: datetime
    ) -> int:
        """Idempotent bulk append; returns the count newly written."""

        try:
            with self._engine.begin() as connection:
                return _write_vintages(connection, vintages, recorded_at)
        except IntegrityError as error:
            raise EvidenceRecoveryError("EVIDENCE_VINTAGE_PERSISTENCE_CONFLICT") from error

    def vintages_for(
        self, *, evidence_family: EvidenceFamily, evidence_id: str
    ) -> tuple[EvidenceVintage, ...]:
        table = evidence_vintage_table
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(table)
                    .where(table.c.evidence_family == evidence_family.value)
                    .where(table.c.evidence_id == evidence_id)
                    .order_by(table.c.acquired_at, table.c.vintage_id)
                )
                .mappings()
                .all()
            )
        return tuple(_vintage_from_row(row) for row in rows)

    def mislabelled_recovery_vintages(self) -> tuple[EvidenceVintage, ...]:
        """``SCHEDULED_LIVE`` rows tied to a ``RECOVERY_BACKFILL`` run whose
        evidence has no ``RECOVERY_BACKFILL`` vintage yet. Read-only."""

        table = evidence_vintage_table
        runs = evidence_recovery_run_table
        sibling = table.alias("sibling")
        statement = (
            sa.select(table)
            .join(runs, runs.c.run_id == table.c.recovery_run_id)
            .where(table.c.acquisition_path == AcquisitionPath.SCHEDULED_LIVE.value)
            .where(table.c.evidence_family.in_([item.value for item in RECOVERABLE_FAMILIES]))
            .where(runs.c.acquisition_path == AcquisitionPath.RECOVERY_BACKFILL.value)
            .where(
                ~sa.exists().where(
                    sa.and_(
                        sibling.c.evidence_family == table.c.evidence_family,
                        sibling.c.evidence_id == table.c.evidence_id,
                        sibling.c.acquisition_path == AcquisitionPath.RECOVERY_BACKFILL.value,
                    )
                )
            )
            .order_by(table.c.evidence_family, table.c.evidence_id)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_vintage_from_row(row) for row in rows)

    def correct_acquisition_paths(
        self, *, recorded_at: datetime, expected_count: int | None = None
    ) -> int:
        """Append the canonical ``RECOVERY_BACKFILL`` vintage for every
        mislabelled row; original rows are untouched. Deterministic and
        idempotent (a second call finds nothing). ``expected_count`` refuses to
        write unless exactly that many rows qualify."""

        pending = self.mislabelled_recovery_vintages()
        if expected_count is not None and len(pending) != expected_count:
            raise EvidenceRecoveryError("ACQUISITION_PATH_CORRECTION_COUNT_MISMATCH")
        corrections = tuple(build_acquisition_path_correction(item) for item in pending)
        return self.register_vintages(vintages=corrections, recorded_at=recorded_at)

    def vintage_count(self, *, evidence_family: EvidenceFamily | None = None) -> int:
        table = evidence_vintage_table
        statement = sa.select(sa.func.count()).select_from(table)
        if evidence_family is not None:
            statement = statement.where(table.c.evidence_family == evidence_family.value)
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    # -- atomic ---------------------------------------------------------

    def commit_recovery(
        self,
        *,
        run: EvidenceRecoveryRun,
        events: tuple[EvidenceCoverageGapEvent, ...],
        vintages: tuple[EvidenceVintage, ...],
        recorded_at: datetime,
    ) -> int:
        """One transaction for a run, every gap resolution it produced and its
        vintages: either all are retained or none (a crash can never leave a
        run without its resolutions, or resolutions without their run)."""

        verify_evidence_recovery_run(run)
        try:
            with self._engine.begin() as connection:
                _write_run(connection, run, recorded_at)
                for event in events:
                    _write_gap_event(connection, event, recorded_at)
                return _write_vintages(connection, vintages, recorded_at)
        except IntegrityError as error:
            raise EvidenceRecoveryError("EVIDENCE_RECOVERY_COMMIT_CONFLICT") from error


def evidence_ledger_present(engine: Engine) -> bool:
    """Whether migration 0118 has been applied (read-only previews run before it)."""

    with engine.connect() as connection:
        return bool(
            connection.execute(sa.text("SELECT to_regclass('evidence_coverage_gap')")).scalar()
        )


SECURITIES_RECOVERY_DATASET = "genuine-historical-alpaca-iex-raw-v1"


class PostgresSecuritiesCoverage:
    """Retained READY RAW daily bars of the cohort dataset, across batches."""

    def __init__(self, engine: Engine, *, dataset_name: str = SECURITIES_RECOVERY_DATASET) -> None:
        self._engine = engine
        self._dataset = dataset_name

    def instruments(self) -> tuple[RetainedInstrument, ...]:
        statement = sa.text(
            """
            SELECT DISTINCT i.instrument_id, i.symbol, i.asset_class
            FROM securities_instrument i
            JOIN securities_bar_evidence b ON b.instrument_id = i.instrument_id
            JOIN securities_market_data_batch m ON m.batch_id = b.batch_id
            WHERE m.dataset_name = :dataset AND m.state = 'READY'
            ORDER BY i.instrument_id
            """
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement, {"dataset": self._dataset}).all()
        return tuple(RetainedInstrument(row[0], row[1], row[2]) for row in rows)

    def bars(self, *, start: date, end: date) -> tuple[RetainedBar, ...]:
        statement = sa.text(
            """
            SELECT b.instrument_id, b.bar_id, b.batch_id,
                   (b.period_start AT TIME ZONE 'UTC')::date AS session_date,
                   b.available_at, b.open, b.high, b.low, b.close, b.volume
            FROM securities_bar_evidence b
            JOIN securities_market_data_batch m ON m.batch_id = b.batch_id
            WHERE m.dataset_name = :dataset AND m.state = 'READY'
              AND b."interval" = 'ONE_DAY' AND b.adjustment_state = 'RAW'
              AND (b.period_start AT TIME ZONE 'UTC')::date BETWEEN :start AND :end
            ORDER BY b.instrument_id, session_date, b.batch_id
            """
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                statement, {"dataset": self._dataset, "start": start, "end": end}
            ).all()
        return tuple(
            RetainedBar(
                instrument_id=row[0],
                bar_id=row[1],
                batch_id=row[2],
                session_date=row[3],
                available_at=row[4],
                values=BarValues(
                    open=Decimal(row[5]),
                    high=Decimal(row[6]),
                    low=Decimal(row[7]),
                    close=Decimal(row[8]),
                    volume=Decimal(row[9]),
                ),
            )
            for row in rows
        )


# --------------------------------------------------------------------------
# Live-only evidence: read retained timestamps only
# --------------------------------------------------------------------------


def retained_options_session_ids(engine: Engine) -> tuple[str, ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT DISTINCT session_id FROM options_forward_evidence_checkpoint")
        ).all()
    return tuple(sorted(row[0] for row in rows))


def retained_kalshi_refresh_dates(engine: Engine) -> tuple[date, ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT DISTINCT (as_of AT TIME ZONE 'UTC')::date FROM apex_kalshi_current_refresh"
            )
        ).all()
    return tuple(sorted(row[0] for row in rows))


def retained_live_observation_rounds(
    engine: Engine, *, since: datetime
) -> dict[str, tuple[datetime, ...]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT venue || ':' || provider, window_start FROM live_market_observation_round "
                "WHERE window_start >= :since ORDER BY window_start"
            ),
            {"since": since},
        ).all()
    grouped: dict[str, list[datetime]] = {}
    for subject, start in rows:
        grouped.setdefault(subject, []).append(start)
    return {key: tuple(value) for key, value in grouped.items()}


def retained_paper_closures(engine: Engine) -> tuple[tuple[str, datetime, datetime], ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT t.trial_id, t.horizon_target_at, c.closed_at "
                "FROM internal_strategy_paper_trial t "
                "JOIN internal_strategy_paper_trial_closure c ON c.trial_id = t.trial_id "
                "ORDER BY t.trial_id"
            )
        ).all()
    return tuple((row[0], row[1], row[2]) for row in rows)
