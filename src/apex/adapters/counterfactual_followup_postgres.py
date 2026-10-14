"""Immutable PostgreSQL evidence for Decision Follow-Up Observation v1.

Composes ``PostgresCounterfactualLedgerRepository`` (already-retained
``CounterfactualDecision`` rows and, via its own ``.shared_opportunity``, the
``SharedOpportunityProjection`` each decision is about) and
``PostgresCryptoUniverseRepository`` (already-retained ``CryptoProductObservation``
daily bars) to supply every read ``application.counterfactual_followup``'s
two services depend on, and persists one normalized
``DecisionFollowUpObservation`` row per genuinely observed
``(decision, policy version, horizon kind)``.  No provider client,
credential, or network capability lives here -- the one genuine network
capability this milestone needs (a bounded Kalshi public read) lives in the
sibling ``adapters.counterfactual_followup_sources`` module instead, kept
separate so this file's own concerns stay limited to persistence and local
retained-evidence composition.  No row this module writes ever carries an
``authority`` other than ``'NONE'``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.counterfactual_ledger_postgres import (
    PostgresCounterfactualLedgerRepository,
    _data,
    _payload,
)
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.counterfactual_followup import (
    CryptoPriceLookup,
    FollowUpScanPosition,
    SecuritiesPriceLookup,
)
from apex.domain.counterfactual_followup import (
    CounterfactualFollowUpError,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
    SecuritiesTerminalEvidenceTiming,
    SecuritiesTerminalTemporalStatus,
    is_followup_evidence_eligible,
    resolve_securities_terminal_evidence_timing,
    securities_terminal_snapshot_id,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision, DecisionDisposition
from apex.domain.options_research_operations import OptionsCaptureWindowPolicy
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities import SecuritiesCalendarSession
from apex.domain.shared_opportunity import SharedVenue

metadata = sa.MetaData()

observation_table = sa.Table(
    "decision_followup_observation",
    metadata,
    sa.Column("observation_id", sa.String(300), primary_key=True),
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("counterfactual_decision.decision_id"),
        nullable=False,
    ),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("disposition", sa.String(20), nullable=False),
    sa.Column("policy_version", sa.String(40), nullable=False),
    sa.Column("horizon_kind", sa.String(20), nullable=False),
    sa.Column("baseline_kind", sa.String(20), nullable=False),
    sa.Column("observation_type", sa.String(60), nullable=False),
    sa.Column("target_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    # Counterfactual Forward Outcome Temporal Integrity v1 (migration 0113).
    # Nullable by construction and permanently ``NULL`` for every
    # ``FOLLOWUP_POLICY_V1``/``V2`` row -- never backfilled.  Promoted to a
    # real column rather than living only inside ``payload`` (where the
    # five sibling V3 facts do live) because it is the one V3 fact the
    # database itself must be able to constrain: migration 0113's
    # biconditional ``ck_decision_followup_v3_measurement_as_of`` is what
    # makes "every V3 row has one, no other row may" a schema guarantee
    # rather than an application convention.
    sa.Column("measurement_as_of", sa.DateTime(timezone=True), nullable=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

# Counterfactual Follow-Up Long-Run Fairness v1.  Deliberately NOT one of
# ``DECISION_FOLLOWUP_TABLES`` and deliberately NOT append-only/immutable
# like every other table in this module (or, with the single narrow,
# heavily-guarded exception of ``securities_position_state``, almost every
# other table in this repository): it holds durable scan-position
# *bookkeeping* for ``SyncFollowUpObservations`` own bounded walk, not
# evidence -- no venue, no measured payload, no ``canonical_hash``, nothing
# an operator could mistake for a market observation or a judgment about
# one.  One row per ``policy_version``, genuinely overwritten every cycle
# (upsert semantics) -- see ``FollowUpScanPosition``/``FollowUpRepository.
# scan_position``/``.save_scan_position`` in ``application.
# counterfactual_followup`` for why this is safe: losing or rolling back
# this row is never a correctness hazard, only bounded, rare, wasted
# re-scanning the next time ``SyncFollowUpObservations.execute`` runs
# (every write it makes is already idempotent).
scan_cursor_table = sa.Table(
    "followup_scan_cursor",
    metadata,
    sa.Column("policy_version", sa.String(40), primary_key=True),
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("counterfactual_decision.decision_id"),
        nullable=False,
    ),
    sa.Column("horizon_kind", sa.String(20), nullable=False),
    sa.Column("baseline_kind", sa.String(20), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

DECISION_FOLLOWUP_TABLES = ("decision_followup_observation",)

# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1.  Immutable, append-only -- one row per ``(observation_id,
# assessment_policy_version)``, same as every other evidence table this
# module owns.  Deliberately a *separate* tuple from
# ``DECISION_FOLLOWUP_TABLES`` (mirrors ``followup_scan_cursor``'s own
# reason for being listed separately in ``adapters.postgres``, just for the
# opposite reason: that table is deliberately mutable, this one is
# deliberately immutable) so a reader of either tuple alone never has to
# guess which table it names.
evidence_quality_table = sa.Table(
    "followup_evidence_quality_assessment",
    metadata,
    sa.Column("assessment_id", sa.String(300), primary_key=True),
    sa.Column(
        "observation_id",
        sa.String(300),
        sa.ForeignKey("decision_followup_observation.observation_id"),
        nullable=False,
    ),
    sa.Column("assessment_policy_version", sa.String(60), nullable=False),
    sa.Column("verdict", sa.String(24), nullable=False),
    sa.Column("evidence_defect_type", sa.String(40), nullable=False),
    sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

FOLLOWUP_EVIDENCE_QUALITY_TABLES = ("followup_evidence_quality_assessment",)


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CounterfactualFollowUpError("COUNTERFACTUAL_FOLLOWUP_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _observation(row: RowMapping) -> DecisionFollowUpObservation:
    """Reconstruct a retained row.  ``baseline_kind``/``baseline_time`` and
    the renamed ``reference_price_at_baseline`` are backward-compatible with
    every row retained before this axis existed (Counterfactual Crypto
    Follow-Up Point-in-Time Semantics v2's own pre-deployment staleness-
    semantics audit): those rows' ``payload`` JSON genuinely lacks these
    keys (``reference_price_at_decision`` under its old name instead) --
    this never rewrites a single already-retained row (the immutability
    trigger would reject it regardless), it only widens how this function
    *reads* what is already there.  A row missing ``baseline_kind`` is,
    structurally, always ``DECISION_TIME`` (every ``FOLLOWUP_POLICY_V1`` row
    of either venue, and every KALSHI row of any policy version, always was
    -- see ``FollowUpBaselineKind``'s own docstring), and its
    ``baseline_time`` is always its own ``decision_evaluated_at``."""

    value = _data(row)
    baseline_kind = FollowUpBaselineKind(
        value.get("baseline_kind", FollowUpBaselineKind.DECISION_TIME)
    )
    decision_evaluated_at = _time(value["decision_evaluated_at"])
    baseline_time = (
        _time(value["baseline_time"]) if "baseline_time" in value else decision_evaluated_at
    )
    reference_price_at_baseline = _optional_decimal(
        value["reference_price_at_baseline"]
        if "reference_price_at_baseline" in value
        else value["reference_price_at_decision"]
    )
    return DecisionFollowUpObservation(
        observation_id=str(value["observation_id"]),
        decision_id=str(value["decision_id"]),
        decision_canonical_hash=str(value["decision_canonical_hash"]),
        venue=SharedVenue(value["venue"]),
        disposition=DecisionDisposition(value["disposition"]),
        policy_version=str(value["policy_version"]),
        horizon_kind=FollowUpHorizonKind(value["horizon_kind"]),
        baseline_kind=baseline_kind,
        horizon_days=int(value["horizon_days"]),
        decision_evaluated_at=decision_evaluated_at,
        baseline_time=baseline_time,
        target_time=_time(value["target_time"]),
        observation_type=FollowUpObservationType(value["observation_type"]),
        subject_instrument_id=str(value["subject_instrument_id"]),
        subject_product_or_market_id=str(value["subject_product_or_market_id"]),
        provenance=EvidenceClass(value["provenance"]),
        # CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
        # Remediation v1.  ``target_evidence_class`` is always recoverable
        # for a legacy row predating this axis -- it is, and always was,
        # exactly ``provenance`` (Part: "provenance... reflects only the
        # TARGET evidence class", now made explicit rather than implicit).
        # ``baseline_evidence_class`` has no honest fallback: guessing it
        # from ``provenance`` would be exactly the DOGE-USD incident's own
        # mistake (a target's evidence class standing in for the
        # baseline's) -- ``None`` here means "not captured at write time",
        # never "known eligible".  See ``resolve_observation_evidence_
        # lineage``/``FollowUpEvidenceQualityAssessment`` for the separate,
        # additive, retroactive determination.
        baseline_evidence_class=(
            EvidenceClass(value["baseline_evidence_class"])
            if value.get("baseline_evidence_class") is not None
            else None
        ),
        target_evidence_class=EvidenceClass(
            value.get("target_evidence_class", value["provenance"])
        ),
        # Generation-semantics versioning (Part: see ``domain.
        # counterfactual_followup``'s own dedicated section).  Missing key ->
        # ``None``, identical "never guessed after the fact" treatment as
        # ``baseline_evidence_class`` immediately above -- true of every row
        # retained before this concept existed, both venues.
        evidence_eligibility_policy_version=_optional_string(
            value.get("evidence_eligibility_policy_version")
        ),
        observed_at=_time(value["observed_at"]),
        source_lineage=_strings(value["source_lineage"]),
        data_limitations=_strings(value["data_limitations"]),
        reference_price_at_baseline=reference_price_at_baseline,
        reference_price_at_target=_optional_decimal(value["reference_price_at_target"]),
        subsequent_return=_optional_decimal(value["subsequent_return"]),
        market_status_at_target=_optional_string(value["market_status_at_target"]),
        yes_bid_at_target=_optional_decimal(value["yes_bid_at_target"]),
        yes_ask_at_target=_optional_decimal(value["yes_ask_at_target"]),
        settlement_result=_optional_string(value["settlement_result"]),
        settlement_source=_optional_string(value["settlement_source"]),
        # Counterfactual Forward Outcome Temporal Integrity v1.  Read from
        # ``payload`` with ``.get`` -- identical "missing key -> ``None``,
        # never guessed after the fact" treatment as ``baseline_evidence_
        # class``/``evidence_eligibility_policy_version`` above, and true of
        # every row retained before V3 existed.  ``measurement_as_of`` is
        # deliberately read from ``payload`` too, not from its own column:
        # the column exists so the *database* can constrain it, while the
        # payload stays the single reconstruction source for every field, so
        # a row can never read back with a hashed value and a column value
        # that disagree.
        measurement_as_of=(
            _time(value["measurement_as_of"])
            if value.get("measurement_as_of") is not None
            else None
        ),
        policy_anchor_at=(
            _time(value["policy_anchor_at"]) if value.get("policy_anchor_at") is not None else None
        ),
        decision_input_reference_price=_optional_decimal(
            value.get("decision_input_reference_price")
        ),
        decision_input_reference_bucket_start=(
            _time(value["decision_input_reference_bucket_start"])
            if value.get("decision_input_reference_bucket_start") is not None
            else None
        ),
        decision_input_source_observation_id=_optional_string(
            value.get("decision_input_source_observation_id")
        ),
        decision_input_evidence_class=(
            EvidenceClass(value["decision_input_evidence_class"])
            if value.get("decision_input_evidence_class") is not None
            else None
        ),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))


def _securities_terminal_timing(value: object) -> SecuritiesTerminalEvidenceTiming | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CounterfactualFollowUpError("FOLLOWUP_EVIDENCE_QUALITY_TIMING_PAYLOAD_INVALID")
    return SecuritiesTerminalEvidenceTiming(
        status=SecuritiesTerminalTemporalStatus(value["status"]),
        target_time=_time(value["target_time"]),
        terminal_snapshot_id=_optional_string(value["terminal_snapshot_id"]),
        terminal_captured_at=_optional_time(value["terminal_captured_at"]),
        calendar_identity=str(value["calendar_identity"]),
        calendar_version=_optional_string(value["calendar_version"]),
        capture_window_policy_id=str(value["capture_window_policy_id"]),
        eligible_session_date=_optional_string(value["eligible_session_date"]),
        eligible_window_open_at=_optional_time(value["eligible_window_open_at"]),
        eligible_window_close_at=_optional_time(value["eligible_window_close_at"]),
        lateness_seconds=_optional_int(value["lateness_seconds"]),
        seconds_beyond_permitted_window=_optional_int(value["seconds_beyond_permitted_window"]),
    )


def _evidence_quality_assessment(row: RowMapping) -> FollowUpEvidenceQualityAssessment:
    """Reconstruct a retained ``followup_evidence_quality_assessment`` row.
    ``securities_terminal_timing`` (Securities Follow-Up Temporal Integrity
    v1) is absent from every row retained before it existed and ``null`` on
    every V1 row written after -- both decode to ``None``."""

    value = _data(row)
    return FollowUpEvidenceQualityAssessment(
        assessment_id=str(value["assessment_id"]),
        observation_id=str(value["observation_id"]),
        observation_canonical_hash=str(value["observation_canonical_hash"]),
        assessment_policy_version=str(value["assessment_policy_version"]),
        baseline_evidence_class=(
            EvidenceClass(value["baseline_evidence_class"])
            if value["baseline_evidence_class"] is not None
            else None
        ),
        target_evidence_class=(
            EvidenceClass(value["target_evidence_class"])
            if value["target_evidence_class"] is not None
            else None
        ),
        verdict=FollowUpEvidenceQualityVerdict(value["verdict"]),
        reason_code=str(value["reason_code"]),
        evidence_defect_type=FollowUpEvidenceDefectType(value["evidence_defect_type"]),
        assessed_at=_time(value["assessed_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        securities_terminal_timing=_securities_terminal_timing(
            value.get("securities_terminal_timing")
        ),
    )


class PostgresCounterfactualFollowUpRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.ledger = PostgresCounterfactualLedgerRepository(engine)
        self.subjects = self.ledger.shared_opportunity
        self.crypto_prices = _CryptoFollowUpPriceSource(PostgresCryptoUniverseRepository(engine))
        # Securities Research -> Shared Opportunity Recurring Bridge v1.
        self.securities_prices = _SecuritiesFollowUpPriceSource(
            PostgresOptionsQuoteIntelligenceRepository(engine)
        )

    def observation(self, observation_id: str) -> DecisionFollowUpObservation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(observation_table).where(
                        observation_table.c.observation_id == observation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _observation(row)

    def save_observation(self, *, observation: DecisionFollowUpObservation) -> bool:
        """Persist one genuine follow-up observation.  Idempotent: replaying
        the identical ``observation_id``/``canonical_hash`` returns ``True``
        and writes nothing; a conflicting payload under the same
        ``observation_id`` raises rather than silently overwriting retained
        evidence (Part 10/18) -- see the domain module docstring for why
        this should not arise in normal operation (at most one genuine
        observation attempt is ever made per ``(decision, horizon)``)."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(observation_table.c.canonical_hash).where(
                        observation_table.c.observation_id == observation.observation_id
                    )
                )
                if existing is not None:
                    if existing != observation.canonical_hash:
                        raise CounterfactualFollowUpError(
                            "COUNTERFACTUAL_FOLLOWUP_OBSERVATION_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    observation_table.insert().values(
                        observation_id=observation.observation_id,
                        decision_id=observation.decision_id,
                        venue=observation.venue.value,
                        disposition=observation.disposition.value,
                        policy_version=observation.policy_version,
                        horizon_kind=observation.horizon_kind.value,
                        baseline_kind=observation.baseline_kind.value,
                        observation_type=observation.observation_type.value,
                        target_time=observation.target_time,
                        observed_at=observation.observed_at,
                        measurement_as_of=observation.measurement_as_of,
                        payload=_payload(observation),
                        canonical_hash=observation.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise CounterfactualFollowUpError(
                "COUNTERFACTUAL_FOLLOWUP_OBSERVATION_PERSISTENCE_CONFLICT"
            ) from error

    def scan_position(self, *, policy_version: str) -> FollowUpScanPosition | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        scan_cursor_table.c.decision_id,
                        scan_cursor_table.c.horizon_kind,
                        scan_cursor_table.c.baseline_kind,
                    ).where(scan_cursor_table.c.policy_version == policy_version)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return FollowUpScanPosition(
            decision_id=str(row["decision_id"]),
            horizon_kind=str(row["horizon_kind"]),
            baseline_kind=str(row["baseline_kind"]),
        )

    def save_scan_position(self, *, policy_version: str, position: FollowUpScanPosition) -> None:
        """Idempotent upsert -- last writer for a given ``policy_version``
        wins (Part: see ``FollowUpRepository.save_scan_position``'s own
        docstring for why this table is deliberately mutable, unlike every
        other table this module owns).  Plain update-then-insert-if-missed
        inside one transaction, mirroring this module's existing plain-core
        style rather than a dialect-specific ``ON CONFLICT`` construct --
        ``SyncFollowUpObservations.execute`` is never invoked concurrently
        for the same ``policy_version`` (the recurring operating cycle runs
        its tasks sequentially; see ``adapters.operating_cycle_tasks``), so
        the narrow update-miss-then-insert-collides race this would need to
        guard against does not arise in practice; a genuine collision still
        fails loudly via ``IntegrityError`` rather than corrupting state."""

        values = {
            "decision_id": position.decision_id,
            "horizon_kind": position.horizon_kind,
            "baseline_kind": position.baseline_kind,
            "updated_at": datetime.now(UTC),
        }
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    scan_cursor_table.update()
                    .where(scan_cursor_table.c.policy_version == policy_version)
                    .values(**values)
                )
                if result.rowcount == 0:
                    connection.execute(
                        scan_cursor_table.insert().values(
                            policy_version=policy_version, authority="NONE", **values
                        )
                    )
        except IntegrityError as error:
            raise CounterfactualFollowUpError(
                "COUNTERFACTUAL_FOLLOWUP_SCAN_CURSOR_PERSISTENCE_CONFLICT"
            ) from error

    def scan_cursor_diagnostics(self, *, policy_version: str) -> dict[str, Any] | None:
        """Operator-facing read of the identical ``followup_scan_cursor`` row
        ``scan_position`` reads, additionally including ``updated_at`` -- the
        last time a completed bounded scan actually advanced this policy
        version's cursor. ``FollowUpScanPosition`` itself deliberately omits
        that timestamp (Part: it is orchestration bookkeeping the write-path
        ``FollowUpRepository`` contract has no use for, not evidence); this
        method exists only so an operator can honestly answer "is the scan
        still moving" without widening that contract. Read-only -- a plain
        SELECT, never called by ``SyncFollowUpObservations`` or any other
        write path -- and safe to call as often as desired, exactly like
        ``coverage()``."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        scan_cursor_table.c.decision_id,
                        scan_cursor_table.c.horizon_kind,
                        scan_cursor_table.c.baseline_kind,
                        scan_cursor_table.c.updated_at,
                    ).where(scan_cursor_table.c.policy_version == policy_version)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return {
            # Counterfactual Forward Outcome Temporal Integrity v1: the
            # caller's own argument echoed back.  Now that the current
            # selection routes different venues to different policy
            # versions, an operator surface shows more than one of these
            # cursors at once, and a cursor with no version of its own would
            # be genuinely ambiguous about which scan it describes.
            "policy_version": policy_version,
            "decision_id": str(row["decision_id"]),
            "horizon_kind": str(row["horizon_kind"]),
            "baseline_kind": str(row["baseline_kind"]),
            "updated_at": row["updated_at"],
        }

    def observations(
        self,
        *,
        venue: SharedVenue | None = None,
        horizon_kind: FollowUpHorizonKind | None = None,
        baseline_kind: FollowUpBaselineKind | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        policy_version: str | None = None,
    ) -> tuple[DecisionFollowUpObservation, ...]:
        """Bounded read for operator diagnostics, optionally filtered by
        venue/horizon/baseline-kind/policy-version and/or an ``observed_at``
        half-open range.  Read-only; never mutates retained evidence.
        ``policy_version``/``baseline_kind`` both default to ``None`` (no
        filter, every retained value returned) rather than to any one value
        -- this is a historical-inspection read path, not a due/backlog
        computation, so it must never silently hide an already-retained
        version or baseline kind from an operator who did not ask for a
        filter.  Filtering on ``baseline_kind=DECISION_TIME`` also matches
        every row retained before this axis existed (Part: backward
        compatibility -- see ``_observation``), since that is what a
        missing ``baseline_kind`` column value always structurally was; the
        SQL column itself, however, is NOT NULL from migration 0097 onward
        (every already-retained row is backfilled), so this is a plain
        column comparison, not a NULL-handling special case."""

        clauses = []
        if venue is not None:
            clauses.append(observation_table.c.venue == venue.value)
        if horizon_kind is not None:
            clauses.append(observation_table.c.horizon_kind == horizon_kind.value)
        if baseline_kind is not None:
            clauses.append(observation_table.c.baseline_kind == baseline_kind.value)
        if policy_version is not None:
            clauses.append(observation_table.c.policy_version == policy_version)
        if start is not None:
            clauses.append(observation_table.c.observed_at >= start)
        if end is not None:
            clauses.append(observation_table.c.observed_at < end)
        statement = sa.select(observation_table).order_by(
            observation_table.c.observed_at, observation_table.c.observation_id
        )
        if clauses:
            statement = statement.where(sa.and_(*clauses))
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_observation(row) for row in rows)

    def observations_for_decision(
        self, decision_id: str
    ) -> tuple[DecisionFollowUpObservation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(observation_table)
                .where(observation_table.c.decision_id == decision_id)
                .order_by(observation_table.c.horizon_kind)
            ).mappings()
            return tuple(_observation(row) for row in rows)

    def coverage(
        self,
        *,
        decisions: tuple[CounterfactualDecision, ...] | None = None,
        observations: tuple[DecisionFollowUpObservation, ...] | None = None,
    ) -> dict[str, Any]:
        """How many retained decisions have at least one follow-up
        observation, broken down by venue/horizon/observation-type/
        provenance/policy-version -- lets an operator distinguish "not yet
        due" from "genuinely never attempted" from "attempted and
        retained".

        ``observation_count_by_policy_version`` is a historical, cross-
        version breakdown of already-retained rows (e.g. how many V1 rows
        exist vs. how many V2 rows exist) -- it is not, and must never be
        read as, a due/backlog figure: a decision counted once here under
        each of two policy versions is still one decision, not two (Part:
        "must not accidentally report decisions x policy as decisions").
        The active/current policy's own due/pending/observed backlog comes
        from ``application.counterfactual_followup.PlanFollowUpObservations``
        instead, called once with one explicit ``policy_version``.

        ``decisions``/``observations`` (Operator Console Follow-Up
        Performance v1) are each optional, purely additive prefetch
        parameters: when a caller already holds the identical bounded reads
        this method would otherwise perform itself (``self.ledger.
        all_decisions()`` / ``self.observations()``), passing them in avoids
        a redundant second fetch of the same rows within one request.
        Omitting either (every pre-existing caller: the CLI, the Decision
        Ledger overview, both persistence tests) preserves this method's
        original behavior exactly -- it fetches fresh, as always."""

        all_decisions = self.ledger.all_decisions() if decisions is None else decisions
        all_observations = self.observations() if observations is None else observations
        decisions_with_observation = {item.decision_id for item in all_observations}
        by_venue: dict[str, int] = {}
        by_horizon: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_provenance: dict[str, int] = {}
        by_disposition: dict[str, int] = {}
        by_policy_version: dict[str, int] = {}
        by_baseline_kind: dict[str, int] = {}
        for item in all_observations:
            by_venue[item.venue.value] = by_venue.get(item.venue.value, 0) + 1
            by_horizon[item.horizon_kind.value] = by_horizon.get(item.horizon_kind.value, 0) + 1
            by_type[item.observation_type.value] = by_type.get(item.observation_type.value, 0) + 1
            by_provenance[item.provenance.value] = by_provenance.get(item.provenance.value, 0) + 1
            by_disposition[item.disposition.value] = (
                by_disposition.get(item.disposition.value, 0) + 1
            )
            by_policy_version[item.policy_version] = (
                by_policy_version.get(item.policy_version, 0) + 1
            )
            by_baseline_kind[item.baseline_kind.value] = (
                by_baseline_kind.get(item.baseline_kind.value, 0) + 1
            )
        return {
            "decision_count": len(all_decisions),
            "decisions_with_at_least_one_observation": len(decisions_with_observation),
            "observation_count": len(all_observations),
            "observation_count_by_venue": by_venue,
            "observation_count_by_horizon_kind": by_horizon,
            "observation_count_by_observation_type": by_type,
            "observation_count_by_provenance": by_provenance,
            "observation_count_by_disposition": by_disposition,
            "observation_count_by_policy_version": by_policy_version,
            # Pre-deployment staleness-semantics audit: ORIGIN_EVIDENCE vs.
            # DECISION_TIME breakdown -- like observation_count_by_policy_
            # version, this is historical/cross-kind, never a due/backlog
            # figure (a decision followed up under both kinds is still one
            # decision).
            "observation_count_by_baseline_kind": by_baseline_kind,
            # Counterfactual Follow-Up Recurring Integration v1: the single
            # most recent genuine capture time across every retained
            # observation -- an honest "is this still moving" operator
            # signal now that observations can accumulate unattended rather
            # than only during a manually invoked ``followup-sync``.
            "most_recent_observed_at": (
                max(item.observed_at for item in all_observations) if all_observations else None
            ),
        }

    # ----------------------------------------------------------------
    # CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
    # Remediation v1 -- additive assessment persistence.  Never called by
    # ``save_observation``/``SyncFollowUpObservations``; a wholly separate,
    # explicitly-triggered write path (Part: mirrors ``counterfactual-
    # classification-sync``'s own CLI-triggered-only precedent -- see
    # ``cli.py``'s ``followup-evidence-quality-assess``).
    # ----------------------------------------------------------------

    def resolve_observation_evidence_lineage(
        self, observation: DecisionFollowUpObservation
    ) -> tuple[EvidenceClass | None, EvidenceClass | None, bool]:
        """Resolve ``(baseline_evidence_class, target_evidence_class,
        lineage_resolved)`` for one retained observation from ``source_
        lineage`` -- correct for every retained row regardless of whether
        it predates ``DecisionFollowUpObservation.baseline_evidence_class``
        as a concept (Part: this is *not* a shortcut that merely reads the
        observation's own, possibly-``None``, dataclass fields -- it
        independently re-derives both sides from retained ``crypto_product_
        observation`` batches, which is what makes retroactive assessment
        of a pre-remediation row possible at all).

        ``source_lineage`` is a deduplicated, order-preserving tuple built
        by ``domain.counterfactual_followup._lineage(baseline_id,
        target_id)`` (see its own docstring): exactly one element means
        both sides came from the identical retained batch (``baseline_id ==
        target_id``); exactly two means index 0 is always the baseline's
        source id and index 1 is always the target's, in that fixed order,
        because the builder always passes baseline before target.  KALSHI
        rows have a one-element ``source_lineage`` naming a provider
        endpoint identity, never a ``crypto_product_observation`` id -- this
        method is CRYPTO-only; callers must not invoke it for a KALSHI
        observation (see ``assess_followup_evidence_quality``, which never
        calls it for one)."""

        lineage = observation.source_lineage
        if len(lineage) == 2:
            baseline_id, target_id = lineage[0], lineage[1]
        elif len(lineage) == 1:
            baseline_id = target_id = lineage[0]
        else:
            return None, None, False
        baseline_class = self.crypto_prices.evidence_class_for_batch(
            source_observation_id=baseline_id
        )
        target_class = self.crypto_prices.evidence_class_for_batch(source_observation_id=target_id)
        resolved = baseline_class is not None and target_class is not None
        return baseline_class, target_class, resolved

    def assessment(
        self, *, observation_id: str, assessment_policy_version: str
    ) -> FollowUpEvidenceQualityAssessment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(evidence_quality_table).where(
                        sa.and_(
                            evidence_quality_table.c.observation_id == observation_id,
                            evidence_quality_table.c.assessment_policy_version
                            == assessment_policy_version,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _evidence_quality_assessment(row)

    def save_assessment(self, *, assessment: FollowUpEvidenceQualityAssessment) -> bool:
        """Persist one evidence-quality assessment idempotently -- identical
        contract to ``save_observation``: replaying the identical
        ``assessment_id``/``canonical_hash`` returns ``True`` and writes
        nothing; a conflicting payload under the same ``assessment_id``
        raises rather than silently overwriting retained evidence."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(evidence_quality_table.c.canonical_hash).where(
                        evidence_quality_table.c.assessment_id == assessment.assessment_id
                    )
                )
                if existing is not None:
                    if existing != assessment.canonical_hash:
                        raise CounterfactualFollowUpError(
                            "FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    evidence_quality_table.insert().values(
                        assessment_id=assessment.assessment_id,
                        observation_id=assessment.observation_id,
                        assessment_policy_version=assessment.assessment_policy_version,
                        verdict=assessment.verdict.value,
                        evidence_defect_type=assessment.evidence_defect_type.value,
                        assessed_at=assessment.assessed_at,
                        payload=_payload(assessment),
                        canonical_hash=assessment.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise CounterfactualFollowUpError(
                "FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_PERSISTENCE_CONFLICT"
            ) from error

    def assessments(
        self, *, assessment_policy_version: str | None = None
    ) -> tuple[FollowUpEvidenceQualityAssessment, ...]:
        """Bounded read for operator diagnostics/downstream eligibility
        checks -- every retained assessment, optionally filtered by
        assessment policy version.  Read-only; never mutates retained
        evidence."""

        statement = sa.select(evidence_quality_table).order_by(
            evidence_quality_table.c.assessed_at, evidence_quality_table.c.assessment_id
        )
        if assessment_policy_version is not None:
            statement = statement.where(
                evidence_quality_table.c.assessment_policy_version == assessment_policy_version
            )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_evidence_quality_assessment(row) for row in rows)

    def unassessed_observations(
        self,
        *,
        assessment_policy_version: str,
        limit: int,
        venue: SharedVenue | None = None,
    ) -> tuple[DecisionFollowUpObservation, ...]:
        """Autonomy Audit v1 (CRYPTO Follow-Up Evidence Eligibility &
        Contaminated Observation Remediation v1's own required follow-up):
        bounded, deterministic (``observed_at``, then ``observation_id``
        ascending -- identical ordering to ``observations()`` itself) read
        of every retained observation that has no ``followup_evidence_
        quality_assessment`` row yet for ``assessment_policy_version``.

        A plain ``NOT EXISTS`` anti-join against the exact identity pair
        (``observation_id``, ``assessment_policy_version``) ``evidence_
        quality_table``'s own ``uq_followup_evidence_quality_identity``
        constraint enforces.  This *is* the backlog position -- no separate
        cursor/bookmark table is needed (unlike ``followup_scan_cursor``)
        because "has an assessment row" is a permanent, monotonic fact once
        true (assessments are append-only, never deleted, never updated):
        successive bounded calls -- across cycles, across restarts, after a
        crash between an observation's own commit and its assessment's --
        structurally always make forward progress and never revisit an
        already-assessed row, with no special-cased recovery logic anywhere.

        Used only by the recurring ``COUNTERFACTUAL_FOLLOWUP_REFRESH``
        task's self-healing assessment sweep (Part: see ``adapters.
        operating_cycle_tasks._BoundedUnassessedFollowUpEvidenceSource``).
        The CLI's own ``followup-evidence-quality-assess`` path deliberately
        keeps assessing every retained observation unconditionally instead
        (Part: a full, explicit, one-time-triggerable census -- see
        ``application.counterfactual_followup.AssessFollowUpEvidenceQuality``'s
        own docstring) -- this narrower "only what's missing" method is not
        shared with it, and calling it changes nothing about how that CLI
        command behaves.

        ``venue`` (Securities Follow-Up Temporal Integrity v1) narrows the
        backlog to one venue -- the SECURITIES-only
        ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2`` lane must never pick up a
        CRYPTO/KALSHI row it defines no verdict for."""

        venue_clause = sa.true() if venue is None else observation_table.c.venue == venue.value
        statement = (
            sa.select(observation_table)
            .where(venue_clause)
            .where(
                ~sa.exists(
                    sa.select(1).where(
                        sa.and_(
                            evidence_quality_table.c.observation_id
                            == observation_table.c.observation_id,
                            evidence_quality_table.c.assessment_policy_version
                            == assessment_policy_version,
                        )
                    )
                )
            )
            .order_by(observation_table.c.observed_at, observation_table.c.observation_id)
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_observation(row) for row in rows)

    def resolve_securities_terminal_evidence_timing(
        self, observation: DecisionFollowUpObservation
    ) -> SecuritiesTerminalEvidenceTiming:
        """Securities Follow-Up Temporal Integrity v1.  Resolves the
        terminal snapshot's own retained ``captured_at`` (by exact id from
        ``source_lineage`` -- never re-selected) and the reviewed XNYS
        sessions bracketing ``target_time``, then delegates the verdict to
        the pure domain resolver.  Read-only; zero provider requests."""

        snapshot_id = securities_terminal_snapshot_id(observation)
        captured_at = (
            None
            if snapshot_id is None
            else self.securities_prices.snapshot_captured_at(snapshot_id=snapshot_id)
        )
        return resolve_securities_terminal_evidence_timing(
            target_time=observation.target_time,
            terminal_snapshot_id=snapshot_id,
            terminal_captured_at=captured_at,
            sessions=_xnys_sessions_bracketing(observation.target_time),
            window_policy=OptionsCaptureWindowPolicy.create(),
        )


# The first XNYS session at or after any instant is at most a few calendar
# days away (the longest closure run is a holiday weekend); ten days either
# side brackets it with a wide margin.  Cached per target date: the reviewed
# calendar is deterministic for a pinned library version, and one sweep
# typically assesses many rows sharing a target date.
_XNYS_BRACKET_DAYS = 10
_xnys_bracket_cache: dict[str, tuple[SecuritiesCalendarSession, ...]] = {}


def _xnys_sessions_bracketing(target_time: datetime) -> tuple[SecuritiesCalendarSession, ...]:
    key = target_time.astimezone(UTC).date().isoformat()
    cached = _xnys_bracket_cache.get(key)
    if cached is None:
        anchor = target_time.astimezone(UTC)
        cached = ReviewedXNYSCalendar(
            start_date=(anchor - timedelta(days=_XNYS_BRACKET_DAYS)).date().isoformat(),
            end_date=(anchor + timedelta(days=_XNYS_BRACKET_DAYS)).date().isoformat(),
        ).sessions
        _xnys_bracket_cache[key] = cached
    return cached


class _CryptoFollowUpPriceSource:
    """Implements ``application.counterfactual_followup.CryptoPriceSource``
    by searching already-retained ``CryptoProductObservation`` batches for an
    exact UTC-day bucket.  Zero provider requests -- purely local reads
    (Part 7/16); if the current retained observation volume grows large
    enough that a full per-product-id scan stops being cheap, a targeted
    product-scoped repository query is the natural next efficiency step, not
    needed at today's volume (see the milestone's genuine acceptance
    report)."""

    def __init__(self, crypto_universe: PostgresCryptoUniverseRepository) -> None:
        self._crypto_universe = crypto_universe

    def daily_close(self, *, product_id: str, bucket_start: datetime) -> CryptoPriceLookup | None:
        candidates: list[tuple[datetime, CryptoPriceLookup]] = []
        for observation in self._crypto_universe.observations():
            if observation.product_id != product_id:
                continue
            # CRYPTO Follow-Up Evidence Eligibility & Contaminated
            # Observation Remediation v1: an ineligible batch (confirmed
            # live: a ``FIXTURE`` "quality repair" batch) must never even
            # become a ranking candidate -- eligibility is checked *before*
            # the tie-break below, so a later-inserted ineligible batch can
            # never beat an earlier eligible one no matter how its
            # ``completed_at`` compares.  See ``domain.counterfactual_
            # followup.FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES`` for the
            # fail-closed allowlist and its full incident trace.
            if not is_followup_evidence_eligible(observation.evidence_class):
                continue
            for candle in observation.candles:
                if candle.bucket_start != bucket_start:
                    continue
                candidates.append(
                    (
                        observation.completed_at,
                        CryptoPriceLookup(
                            bucket_start=candle.bucket_start,
                            close=candle.close,
                            source_observation_id=observation.observation_id,
                            evidence_class=observation.evidence_class,
                        ),
                    )
                )
        if not candidates:
            return None
        # Prefer the most-recently-completed retained batch covering this
        # bucket if more than one does (overlapping capture windows) --
        # "latest retained evidence wins", the same idiom used elsewhere for
        # overlapping capture coverage.  No ``completed_at``-vs-``before``
        # availability constraint here (contrast ``latest_close_before``
        # below): every caller of ``daily_close`` reads a forward-looking
        # target price or V1's own frozen baseline, neither of which is a
        # hindsight-leakage concern (Part: "not a hindsight-leakage concern
        # -- this is the observation's own forward-looking measurement, not
        # a baseline", ``SyncFollowUpObservations._build_crypto``).
        return max(candidates, key=lambda item: item[0])[1]

    def daily_close_as_of(
        self, *, product_id: str, bucket_start: datetime, measurement_as_of: datetime
    ) -> CryptoPriceLookup | None:
        """Counterfactual Forward Outcome Temporal Integrity v1 --
        ``FOLLOWUP_POLICY_V3``'s only endpoint lookup, for *both* the
        window-start and window-end buckets.  See ``application.
        counterfactual_followup.CryptoPriceSource.daily_close_as_of`` for
        the full contract this implements.

        Same full local scan and general shape as its two siblings above --
        deliberately not a materially different query, so the same "not
        needed at today's volume, obvious next efficiency step if that
        changes" judgment applies equally to all three.

        The three predicates are applied in a deliberate order, all of them
        *before* any ranking: product, then evidence eligibility, then the
        availability bound, then exact bucket.  Eligibility before ranking
        is the exact boundary the confirmed DOGE-USD contamination incident
        crossed (see ``domain.counterfactual_followup.FOLLOWUP_ELIGIBLE_
        EVIDENCE_CLASSES``); the availability bound before ranking is what
        makes this method reproducible -- a batch that completed after
        ``measurement_as_of`` is not a late-losing candidate, it is not a
        candidate at all, so a later-arriving historical batch can never
        change what an earlier declared measurement resolves to.

        Ranking is then the explicit *total* order
        ``(completed_at, observation_id)``.  ``daily_close`` above ranks by
        ``completed_at`` alone, which leaves two same-``completed_at``
        batches resolving by scan order; that is frozen V1/V2 behavior and
        is left exactly as it is, but V3 must not inherit it."""

        candidates: list[tuple[datetime, str, CryptoPriceLookup]] = []
        for observation in self._crypto_universe.observations():
            if observation.product_id != product_id:
                continue
            if not is_followup_evidence_eligible(observation.evidence_class):
                continue
            if observation.completed_at > measurement_as_of:
                continue
            for candle in observation.candles:
                if candle.bucket_start != bucket_start:
                    continue
                candidates.append(
                    (
                        observation.completed_at,
                        observation.observation_id,
                        CryptoPriceLookup(
                            bucket_start=candle.bucket_start,
                            close=candle.close,
                            source_observation_id=observation.observation_id,
                            evidence_class=observation.evidence_class,
                        ),
                    )
                )
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def latest_close_before(self, *, product_id: str, before: datetime) -> CryptoPriceLookup | None:
        """Counterfactual Crypto Follow-Up Point-in-Time Semantics v2's own
        baseline resolution for ``reference_price_at_decision`` (see
        ``domain.counterfactual_followup.FOLLOWUP_POLICY_V2``).  Same full
        local scan and "latest retained batch wins" tie-break as
        ``daily_close`` above -- deliberately not a materially different
        query shape, just a different selection predicate
        (``bucket_start < before`` and take the maximum, instead of
        ``bucket_start == bucket_start``) -- so the same "not needed at
        today's volume, obvious next efficiency step if that changes"
        judgment applies equally to both.

        Unlike ``daily_close``, a candidate here must also have been
        *genuinely available* to Apex by ``before`` -- not merely carry an
        economically-historical ``bucket_start`` (Part: availability-as-of
        correctness; found by this exact milestone's own pre-deployment
        audit, before FOLLOWUP_POLICY_V2 had ever been synced against the
        live database).  A retained ``CryptoProductObservation`` batch's
        ``bucket_start`` values describe *which trading day* a candle is
        about; they say nothing about *when Apex captured it* -- a
        backfilled or recaptured batch (``completed_at`` after ``before``)
        can carry candles whose ``bucket_start`` is arbitrarily far in the
        economic past.  Selecting one of those merely because its
        ``bucket_start`` predates ``before`` would be exactly the same
        hindsight leak the ``bucket_start < before`` predicate exists to
        prevent, just routed through ingestion time instead of economic
        time -- so a candle is only a candidate here when *both*
        ``candle.bucket_start < before`` *and* ``observation.completed_at
        <= before`` hold; ``bucket_start`` alone never implies
        availability.

        CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
        Remediation v1: a candidate must *also* be ``is_followup_evidence_
        eligible`` -- checked before the point-in-time predicates above are
        even evaluated, so an ineligible batch is never a ranking candidate
        regardless of its ``completed_at``/``bucket_start``.  This is
        exactly the boundary the confirmed DOGE-USD incident crossed: a
        genuine ``HISTORICAL_PUBLIC`` batch completed at 01:44:13Z, a
        synthetic ``FIXTURE`` "quality repair" batch covering the identical
        bucket completed 4m38s later at 01:48:51Z, and the old "latest
        completed_at wins" tie-break -- run with no eligibility
        precondition at all -- let the fixture win.  See ``domain.
        counterfactual_followup.FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES`` for the
        fail-closed allowlist."""

        candidates: list[tuple[datetime, datetime, CryptoPriceLookup]] = []
        for observation in self._crypto_universe.observations():
            if observation.product_id != product_id:
                continue
            if observation.completed_at > before:
                continue
            if not is_followup_evidence_eligible(observation.evidence_class):
                continue
            for candle in observation.candles:
                if candle.bucket_start >= before:
                    continue
                candidates.append(
                    (
                        candle.bucket_start,
                        observation.completed_at,
                        CryptoPriceLookup(
                            bucket_start=candle.bucket_start,
                            close=candle.close,
                            source_observation_id=observation.observation_id,
                            evidence_class=observation.evidence_class,
                        ),
                    )
                )
        if not candidates:
            return None
        # Latest qualifying bucket wins; a tie on bucket_start (more than one
        # retained batch captured the same historical day, both genuinely
        # available by ``before``) is broken the same way daily_close breaks
        # it -- most-recently-completed batch.
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[-1][2]

    def evidence_class_for_batch(self, *, source_observation_id: str) -> EvidenceClass | None:
        """CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
        Remediation v1.  Resolves a retained ``DecisionFollowUpObservation.
        source_lineage`` entry (a ``crypto_product_observation.
        observation_id``) back to that batch's own retained ``evidence_
        class`` -- the one join retroactive assessment needs and the
        ``daily_close``/``latest_close_before`` scans above do not, since
        they already carry ``evidence_class`` on every ``CryptoPriceLookup``
        they return.  Returns ``None`` when no retained batch has this id
        (Part: "observations whose lineage cannot be proven from retained
        state" -- fails closed at the caller, never guessed here)."""

        for observation in self._crypto_universe.observations():
            if observation.observation_id == source_observation_id:
                return observation.evidence_class
        return None


class _SecuritiesFollowUpPriceSource:
    """Securities Research -> Shared Opportunity Recurring Bridge v1.
    Implements ``application.counterfactual_followup.
    SecuritiesUnderlyingPriceSource`` over already-retained
    ``domain.options_quote_intelligence.OptionsChainSnapshot`` evidence.
    Zero provider requests -- a single indexed query per lookup (Part 7/16:
    this bridge never triggers new options capture; that stays
    ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``'s own job, exactly like CRYPTO's
    analogous boundary with ``CRYPTO_UNIVERSE_REFRESH``)."""

    def __init__(self, options_quotes: PostgresOptionsQuoteIntelligenceRepository) -> None:
        self._options_quotes = options_quotes
        self._captured_at_cache: dict[str, datetime] = {}

    def earliest_reference_price_at_or_after(
        self, *, underlying_canonical_security_id: str, at_or_after: datetime
    ) -> SecuritiesPriceLookup | None:
        snapshot = self._options_quotes.earliest_snapshot_for_underlying_at_or_after(
            underlying_canonical_security_id, at_or_after
        )
        if snapshot is None:
            return None
        return SecuritiesPriceLookup(
            underlying_reference_price=snapshot.underlying_reference_price,
            source_snapshot_id=snapshot.snapshot_id,
            captured_at=snapshot.captured_at,
        )

    def snapshot_captured_at(self, *, snapshot_id: str) -> datetime | None:
        """The exact retained ``captured_at`` of one snapshot by id (column
        read only -- never the heavy payload), or ``None`` if no such
        snapshot is retained.  Securities Follow-Up Temporal Integrity v1."""

        cached = self._captured_at_cache.get(snapshot_id)
        if cached is not None:
            return cached
        captured_at = self._options_quotes.snapshot_captured_at(snapshot_id)
        if captured_at is not None:
            self._captured_at_cache[snapshot_id] = captured_at
        return captured_at
