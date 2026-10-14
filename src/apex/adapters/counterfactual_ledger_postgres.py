"""Immutable PostgreSQL evidence for the Counterfactual Decision Ledger v1
and for Counterfactual Decision Classification & Interpretation Versioning
v1.

Composes ``PostgresSharedOpportunityRepository`` (retained Opportunities,
eligibility decisions) and ``PostgresInvestmentMandateRepository`` (retained
mandate evaluations) to read the two origins the ledger integrates, and
persists one normalized ``CounterfactualDecision`` row per already-retained
origin decision.  Separately, it persists ``CounterfactualDecisionClassification``
rows -- each an independent, versioned analytical interpretation of one
already-retained ``CounterfactualDecision``'s ``raw_reason_codes`` -- keyed
by ``(decision_id, classifier_version)``, never mutating or replacing the
decision it classifies.  No provider client, credential, or network
capability lives here, and no row this module writes ever carries an
``authority`` other than ``'NONE'``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.domain.counterfactual_classification import (
    CounterfactualClassificationError,
    CounterfactualDecisionClassification,
    ReasonFamily,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    CounterfactualLedgerError,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.investment_mandate import MandateEvaluation
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    SharedOpportunityProjection,
    SharedOpportunitySet,
    SharedVenue,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

decision_table = sa.Table(
    "counterfactual_decision",
    metadata,
    sa.Column("decision_id", sa.String(300), primary_key=True),
    sa.Column("origin", sa.String(40), nullable=False),
    sa.Column("origin_decision_id", sa.String(300), nullable=False),
    sa.Column(
        "subject_projection_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.Column(
        "opportunity_set_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_set.opportunity_set_id"),
        nullable=True,
    ),
    sa.Column(
        "mandate_id", sa.String(300), sa.ForeignKey("investment_mandate.mandate_id"), nullable=True
    ),
    sa.Column(
        "portfolio_snapshot_id",
        sa.String(300),
        sa.ForeignKey("portfolio_snapshot.snapshot_id"),
        nullable=True,
    ),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("disposition", sa.String(20), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

classification_table = sa.Table(
    "counterfactual_decision_classification",
    metadata,
    sa.Column("classification_id", sa.String(300), primary_key=True),
    sa.Column(
        "decision_id",
        sa.String(300),
        sa.ForeignKey("counterfactual_decision.decision_id"),
        nullable=False,
    ),
    sa.Column("classifier_version", sa.String(40), nullable=False),
    sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

COUNTERFACTUAL_LEDGER_TABLES = ("counterfactual_decision",)
COUNTERFACTUAL_CLASSIFICATION_TABLES = ("counterfactual_decision_classification",)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CounterfactualLedgerError("COUNTERFACTUAL_LEDGER_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CounterfactualLedgerError("COUNTERFACTUAL_LEDGER_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _decision(row: RowMapping) -> CounterfactualDecision:
    value = _data(row)
    return CounterfactualDecision(
        decision_id=str(value["decision_id"]),
        origin=DecisionOrigin(value["origin"]),
        origin_decision_id=str(value["origin_decision_id"]),
        origin_canonical_hash=str(value["origin_canonical_hash"]),
        origin_state=str(value["origin_state"]),
        subject_projection_id=str(value["subject_projection_id"]),
        subject_source_record_type=str(value["subject_source_record_type"]),
        subject_source_record_id=str(value["subject_source_record_id"]),
        venue=SharedVenue(value["venue"]),
        opportunity_set_id=(
            None if value["opportunity_set_id"] is None else str(value["opportunity_set_id"])
        ),
        mandate_id=(None if value["mandate_id"] is None else str(value["mandate_id"])),
        mandate_lineage_id=(
            None if value["mandate_lineage_id"] is None else str(value["mandate_lineage_id"])
        ),
        mandate_version=(
            None if value["mandate_version"] is None else int(value["mandate_version"])
        ),
        mandate_hash=(None if value["mandate_hash"] is None else str(value["mandate_hash"])),
        portfolio_snapshot_id=(
            None if value["portfolio_snapshot_id"] is None else str(value["portfolio_snapshot_id"])
        ),
        portfolio_snapshot_hash=(
            None
            if value["portfolio_snapshot_hash"] is None
            else str(value["portfolio_snapshot_hash"])
        ),
        evaluated_at=_time(value["evaluated_at"]),
        disposition=DecisionDisposition(value["disposition"]),
        raw_reason_codes=_strings(value["raw_reason_codes"]),
        explanation=str(value["explanation"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _classification(row: RowMapping) -> CounterfactualDecisionClassification:
    value = _data(row)
    return CounterfactualDecisionClassification(
        classification_id=str(value["classification_id"]),
        decision_id=str(value["decision_id"]),
        decision_canonical_hash=str(value["decision_canonical_hash"]),
        classifier_version=str(value["classifier_version"]),
        classifier_policy_hash=str(value["classifier_policy_hash"]),
        reason_families=tuple(ReasonFamily(item) for item in _strings(value["reason_families"])),
        classified_at=_time(value["classified_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresCounterfactualLedgerRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.shared_opportunity = PostgresSharedOpportunityRepository(engine)
        self.investment_mandate = PostgresInvestmentMandateRepository(engine)

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def decision(self, decision_id: str) -> CounterfactualDecision | None:
        row = self._one(
            sa.select(decision_table).where(decision_table.c.decision_id == decision_id)
        )
        return None if row is None else _decision(row)

    def decisions_for_subject(
        self, subject_projection_id: str
    ) -> tuple[CounterfactualDecision, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(decision_table)
                .where(decision_table.c.subject_projection_id == subject_projection_id)
                .order_by(decision_table.c.evaluated_at, decision_table.c.decision_id)
            ).mappings()
            return tuple(_decision(row) for row in rows)

    def decisions(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        origin: DecisionOrigin | None = None,
        venue: SharedVenue | None = None,
        disposition: DecisionDisposition | None = None,
    ) -> tuple[CounterfactualDecision, ...]:
        """Bounded read for operator diagnostics: every retained decision,
        optionally filtered by an ``evaluated_at`` half-open range and/or
        origin/venue/disposition.  Read-only; never mutates retained
        evidence."""

        clauses = []
        if start is not None:
            clauses.append(decision_table.c.evaluated_at >= start)
        if end is not None:
            clauses.append(decision_table.c.evaluated_at < end)
        if origin is not None:
            clauses.append(decision_table.c.origin == origin.value)
        if venue is not None:
            clauses.append(decision_table.c.venue == venue.value)
        if disposition is not None:
            clauses.append(decision_table.c.disposition == disposition.value)
        statement = sa.select(decision_table).order_by(
            decision_table.c.evaluated_at, decision_table.c.decision_id
        )
        if clauses:
            statement = statement.where(sa.and_(*clauses))
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_decision(row) for row in rows)

    def all_decisions(self) -> tuple[CounterfactualDecision, ...]:
        """Every retained decision, unfiltered -- the narrow read surface
        ``application.counterfactual_classification.SyncCounterfactualClassification``
        depends on.  A thin, explicit alias for ``decisions()`` called with
        no filters, kept separate so that dependency is obvious from the
        call site rather than implicit in all-optional keyword defaults."""

        return self.decisions()

    def save_decision(self, *, decision: CounterfactualDecision) -> bool:
        """Persist one normalized decision.  Idempotent: replaying the
        identical ``decision_id``/``canonical_hash`` returns ``True`` and
        writes nothing; a conflicting payload under the same
        ``decision_id`` raises rather than silently overwriting retained
        truth (``decision_id`` is a pure function of the origin decision's
        own id+hash, so this can only happen if the origin itself was
        mutated outside its own immutability guarantee)."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(decision_table.c.canonical_hash).where(
                        decision_table.c.decision_id == decision.decision_id
                    )
                )
                if existing is not None:
                    if existing != decision.canonical_hash:
                        raise CounterfactualLedgerError("COUNTERFACTUAL_DECISION_IDENTITY_CONFLICT")
                    return True
                connection.execute(
                    decision_table.insert().values(
                        decision_id=decision.decision_id,
                        origin=decision.origin.value,
                        origin_decision_id=decision.origin_decision_id,
                        subject_projection_id=decision.subject_projection_id,
                        opportunity_set_id=decision.opportunity_set_id,
                        mandate_id=decision.mandate_id,
                        portfolio_snapshot_id=decision.portfolio_snapshot_id,
                        venue=decision.venue.value,
                        evaluated_at=decision.evaluated_at,
                        disposition=decision.disposition.value,
                        payload=_payload(decision),
                        canonical_hash=decision.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise CounterfactualLedgerError(
                "COUNTERFACTUAL_DECISION_PERSISTENCE_CONFLICT"
            ) from error

    def coverage(self) -> dict[str, object]:
        """How much of the two integrated origins has been synced into the
        ledger so far -- lets an operator distinguish "genuinely zero
        decisions exist yet" from "decisions exist upstream but have not
        been synced," rather than silently presenting partial coverage as
        complete."""

        opportunity_sets = self.shared_opportunity.opportunity_sets()
        upstream_eligibility_count = sum(
            len(self.shared_opportunity.eligibility_decisions(item.opportunity_set_id))
            for item in opportunity_sets
        )
        upstream_mandate_count = len(self.investment_mandate.all_evaluations())
        with self._engine.connect() as connection:
            ledger_eligibility_count = connection.scalar(
                sa.select(sa.func.count())
                .select_from(decision_table)
                .where(
                    decision_table.c.origin == DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY.value
                )
            )
            ledger_mandate_count = connection.scalar(
                sa.select(sa.func.count())
                .select_from(decision_table)
                .where(
                    decision_table.c.origin == DecisionOrigin.INVESTMENT_MANDATE_EVALUATION.value
                )
            )
        return {
            "opportunity_set_count": len(opportunity_sets),
            "upstream_eligibility_decision_count": upstream_eligibility_count,
            "ledger_eligibility_decision_count": int(ledger_eligibility_count or 0),
            "upstream_mandate_evaluation_count": upstream_mandate_count,
            "ledger_mandate_evaluation_count": int(ledger_mandate_count or 0),
            "eligibility_coverage_complete": (
                upstream_eligibility_count == int(ledger_eligibility_count or 0)
            ),
            "mandate_coverage_complete": (upstream_mandate_count == int(ledger_mandate_count or 0)),
        }

    # ----------------------------------------------------------------
    # Counterfactual Decision Classification & Interpretation Versioning v1
    # ----------------------------------------------------------------

    def classification(self, classification_id: str) -> CounterfactualDecisionClassification | None:
        row = self._one(
            sa.select(classification_table).where(
                classification_table.c.classification_id == classification_id
            )
        )
        return None if row is None else _classification(row)

    def classifications_for_decision(
        self, decision_id: str
    ) -> tuple[CounterfactualDecisionClassification, ...]:
        """Every retained classifier version's interpretation of one
        decision, oldest-classified first -- lets an operator see "what did
        v1 say, what does v2 say" side by side without either row ever
        having been mutated."""

        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(classification_table)
                .where(classification_table.c.decision_id == decision_id)
                .order_by(
                    classification_table.c.classified_at, classification_table.c.classification_id
                )
            ).mappings()
            return tuple(_classification(row) for row in rows)

    def classifications(
        self,
        *,
        classifier_version: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[CounterfactualDecisionClassification, ...]:
        """Bounded read for operator diagnostics, optionally filtered by
        classifier version and/or a ``classified_at`` half-open range.
        Read-only; never mutates retained evidence."""

        clauses = []
        if classifier_version is not None:
            clauses.append(classification_table.c.classifier_version == classifier_version)
        if start is not None:
            clauses.append(classification_table.c.classified_at >= start)
        if end is not None:
            clauses.append(classification_table.c.classified_at < end)
        statement = sa.select(classification_table).order_by(
            classification_table.c.classified_at, classification_table.c.classification_id
        )
        if clauses:
            statement = statement.where(sa.and_(*clauses))
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_classification(row) for row in rows)

    def save_classification(self, *, classification: CounterfactualDecisionClassification) -> bool:
        """Persist one classification.  Idempotent: replaying the identical
        ``classification_id``/``canonical_hash`` returns ``True`` and
        writes nothing; a conflicting payload under the same
        ``classification_id`` raises rather than silently overwriting
        retained analysis (``classification_id`` is a pure function of
        ``(decision_id, classifier_version)``, so this can only happen if a
        classifier version's own mapping table changed without minting a
        new version string -- see ``domain.counterfactual_classification``).
        Mirrors ``save_decision`` exactly."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(classification_table.c.canonical_hash).where(
                        classification_table.c.classification_id == classification.classification_id
                    )
                )
                if existing is not None:
                    if existing != classification.canonical_hash:
                        raise CounterfactualClassificationError(
                            "COUNTERFACTUAL_CLASSIFICATION_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    classification_table.insert().values(
                        classification_id=classification.classification_id,
                        decision_id=classification.decision_id,
                        classifier_version=classification.classifier_version,
                        classified_at=classification.classified_at,
                        payload=_payload(classification),
                        canonical_hash=classification.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise CounterfactualClassificationError(
                "COUNTERFACTUAL_CLASSIFICATION_PERSISTENCE_CONFLICT"
            ) from error

    def classification_coverage(self, *, classifier_version: str) -> dict[str, object]:
        """How much of the ledger's own already-retained decisions have been
        classified under one classifier version -- the classification-layer
        analogue of ``coverage()``.  ``decision_event_count`` and
        ``classified_decision_count`` are always decision counts (never
        summed across classifier versions); a decision classified under
        both v1 and v2 still counts once in each, never twice in either
        (Part 11: classification changes must not change the count of
        underlying considered decisions)."""

        with self._engine.connect() as connection:
            decision_event_count = connection.scalar(
                sa.select(sa.func.count()).select_from(decision_table)
            )
            classified_decision_count = connection.scalar(
                sa.select(sa.func.count(sa.distinct(classification_table.c.decision_id))).where(
                    classification_table.c.classifier_version == classifier_version
                )
            )
        decision_event_count = int(decision_event_count or 0)
        classified_decision_count = int(classified_decision_count or 0)
        return {
            "classifier_version": classifier_version,
            "decision_event_count": decision_event_count,
            "classified_decision_count": classified_decision_count,
            "unclassified_decision_count": decision_event_count - classified_decision_count,
            "classification_complete": decision_event_count == classified_decision_count,
        }


class BatchedLedgerEvidenceSource:
    """Read-only, request-scoped adapter implementing both
    ``application.counterfactual_ledger.SharedOpportunityEvidenceSource``
    and ``...MandateEvidenceSource`` by pre-batching every
    ``SharedOpportunityProjection`` a full ledger rescan will need into one
    bulk round trip, instead of the per-eligibility-decision /
    per-mandate-evaluation point lookups
    ``SyncCounterfactualLedgerFromSharedOpportunity`` /
    ``...FromMandateEvaluations`` call by default through their narrow
    Protocol seam (``projection(projection_id)`` / ``opportunity
    (projection_id)``, one row at a time).

    Added for Counterfactual Outcome Feedback v1's Phase 0: wiring
    ``counterfactual-ledger-sync``'s CLI-only full rescan into a task that
    the recurring operating cycle runs on a five-minute cadence turns what
    was an acceptable one-off human wait (hundreds of individual round
    trips) into repeated ambient per-cycle cost -- the same class of defect
    Operator Console Follow-Up Performance v1 already found and fixed for
    ``/followups`` (521 individual ``projection()`` calls, ~103s) via
    ``PostgresSharedOpportunityRepository.projections_by_id()``. This
    adapter reuses that exact bulk method rather than reproducing the fix
    independently: ``opportunity_sets()``/``eligibility_decisions()``/
    ``all_evaluations()`` are already single-round-trip reads (confirmed by
    inspection, not assumed), so the only genuine fan-out was the
    per-projection lookup -- collecting every referenced ``projection_id``
    up front and resolving them in one ``WHERE ... IN (...)`` query bounds
    per-cycle cost by the number of retained opportunity *sets*
    (~1/day) rather than the number of retained *decisions* (~30/day and
    growing with total history), a materially healthier growth shape for a
    task that runs every cycle indefinitely.

    Neither integrated sync service is modified: this class only changes
    which source object backs their existing, unmodified Protocol seam.
    Read-only; performs no writes and mutates no retained evidence."""

    def __init__(self, ledger_repository: PostgresCounterfactualLedgerRepository) -> None:
        shared_opportunity = ledger_repository.shared_opportunity
        investment_mandate = ledger_repository.investment_mandate
        self._opportunity_sets: tuple[SharedOpportunitySet, ...] = (
            shared_opportunity.opportunity_sets()
        )
        self._eligibility_by_set: dict[str, tuple[EligibilityDecision, ...]] = {
            item.opportunity_set_id: shared_opportunity.eligibility_decisions(
                item.opportunity_set_id
            )
            for item in self._opportunity_sets
        }
        self._evaluations: tuple[MandateEvaluation, ...] = investment_mandate.all_evaluations()
        needed_projection_ids: set[str] = {
            decision.projection_id
            for decisions in self._eligibility_by_set.values()
            for decision in decisions
        }
        needed_projection_ids.update(evaluation.opportunity_id for evaluation in self._evaluations)
        self._projections: dict[str, SharedOpportunityProjection] = (
            shared_opportunity.projections_by_id(needed_projection_ids)
        )

    # -- SharedOpportunityEvidenceSource --------------------------------

    def opportunity_sets(self) -> tuple[SharedOpportunitySet, ...]:
        return self._opportunity_sets

    def eligibility_decisions(self, opportunity_set_id: str) -> tuple[EligibilityDecision, ...]:
        return self._eligibility_by_set.get(opportunity_set_id, ())

    def projection(self, projection_id: str) -> SharedOpportunityProjection | None:
        return self._projections.get(projection_id)

    # -- MandateEvidenceSource -------------------------------------------

    def all_evaluations(self) -> tuple[MandateEvaluation, ...]:
        return self._evaluations

    def opportunity(self, projection_id: str) -> SharedOpportunityProjection | None:
        return self._projections.get(projection_id)
