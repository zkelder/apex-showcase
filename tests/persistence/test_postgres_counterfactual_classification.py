"""PostgreSQL persistence proofs for Counterfactual Decision Classification &
Interpretation Versioning v1: idempotent classification sync, side-by-side
classifier-version coexistence without mutating either the decision or an
earlier version's classification, no double-counting of decision events,
tamper-detection, coverage diagnostics, and migration
``0095_reason_classification``'s historically-honest backfill of the
521-decision-shaped pre-existing data every real environment carries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.application.counterfactual_classification import SyncCounterfactualClassification
from apex.application.counterfactual_ledger import SyncCounterfactualLedgerFromSharedOpportunity
from apex.domain.counterfactual_classification import (
    REASON_CLASSIFIER_V1,
    REASON_CLASSIFIER_V1_POLICY_HASH,
    REASON_CLASSIFIER_V2,
    CounterfactualClassificationError,
    ReasonFamily,
)
from apex.domain.counterfactual_ledger import DecisionDisposition, decision_canonical_hash
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import canonical_json_bytes, content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _projection(identity: str, **overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": f"cfc-proj-{identity}",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "FixtureOpportunity",
        "source_record_id": f"cfc-source-{identity}",
        "source_canonical_hash": content_digest((identity,)),
        "strategy_or_method_id": "CFC_FIXTURE_V1",
        "instrument_id": f"{identity}-USD",
        "product_or_market_id": f"{identity}-USD",
        "direction": "HYPOTHETICAL_LONG_SPOT_RESEARCH",
        "generated_at": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(days=1),
        "evidence_class": "HISTORICAL_PUBLIC",
        "freshness_seconds": 60,
        "freshness_limit_seconds": 3600,
        "expected_gross_return": None,
        "expected_net_return": None,
        "comparable_value": "0.10",
        "comparable_value_identity": None,
        "uncertainty": "0.10",
        "maximum_loss": "50",
        "modeled_fees": "1",
        "modeled_slippage": "1",
        "costs_retained": True,
        "liquidity_state": "SUFFICIENT",
        "liquidity_limitations": (),
        "required_hypothetical_capital": "100",
        "maximum_sensible_research_allocation": "200",
        "source_maximum_allocation_fraction": "0.5",
        "expected_holding_days": 5,
        "estimated_lockup_days": 5,
        "supporting_evidence_ids": (f"cfc-evidence-{identity}",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _seed_opportunity_set(
    engine: Engine, *, set_id: str, projections: tuple[SharedOpportunityProjection, ...]
) -> str:
    from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository

    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=set_id, created_at=NOW, projections=projections
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"{set_id}-bundle",
        opportunity_set=opportunity_set,
        projections=projections,
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    return opportunity_set.opportunity_set_id


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


# --------------------------------------------------------------------------
# Sync: creation, idempotency, version coexistence
# --------------------------------------------------------------------------


def test_sync_classification_creates_one_row_per_decision(postgres_engine: Engine) -> None:
    eligible = _projection("sync-a-eligible")
    rejected = _projection("sync-a-rejected", liquidity_state="INSUFFICIENT")
    _seed_opportunity_set(
        postgres_engine, set_id="cfc-set-sync-a", projections=(eligible, rejected)
    )
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    ledger_result = SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    assert ledger_result.considered_count == 2

    result = SyncCounterfactualClassification(repository, repository).execute(
        classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    assert result.considered_count == 2
    assert result.newly_written_count == 2
    assert result.idempotent_replay_count == 0
    assert result.classifier_version == REASON_CLASSIFIER_V1
    assert _row_count(postgres_engine, "counterfactual_decision_classification") == 2
    for decision_id in ledger_result.decision_ids:
        classifications = repository.classifications_for_decision(decision_id)
        assert len(classifications) == 1
        assert classifications[0].classifier_version == REASON_CLASSIFIER_V1
        assert classifications[0].authority == "NONE"


def test_sync_classification_is_idempotent_on_replay(postgres_engine: Engine) -> None:
    projection = _projection("sync-b")
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-sync-b", projections=(projection,))
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    ledger_result = SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    decision_id = ledger_result.decision_ids[0]

    sync = SyncCounterfactualClassification(repository, repository)
    first = sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1)
    before = _row_count(postgres_engine, "counterfactual_decision_classification")
    second = sync.execute(
        classified_at=NOW + timedelta(hours=1), classifier_version=REASON_CLASSIFIER_V1
    )
    after = _row_count(postgres_engine, "counterfactual_decision_classification")

    assert after == before
    assert second.newly_written_count == 0
    assert second.idempotent_replay_count == first.considered_count
    assert set(first.classification_ids) == set(second.classification_ids)
    # replay must not even update classified_at on the retained row
    (classification,) = repository.classifications_for_decision(decision_id)
    assert classification.classified_at == NOW


def test_sync_classification_v2_coexists_with_v1_without_mutating_it_or_the_decision(
    postgres_engine: Engine,
) -> None:
    """Part 12/Part 9: classifying under a second version must add rows,
    never touch the first version's rows or the underlying decision."""

    projection = _projection("sync-c", source_rejection_reasons=("OUTSIDE_TOP_RANKED_BOUND",))
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-sync-c", projections=(projection,))
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    ledger_result = SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    decision_id = ledger_result.decision_ids[0]
    decision_before = repository.decision(decision_id)
    assert decision_before is not None

    sync = SyncCounterfactualClassification(repository, repository)
    sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1)
    v1_before = repository.classifications_for_decision(decision_id)
    assert len(v1_before) == 1

    sync.execute(classified_at=NOW + timedelta(hours=2), classifier_version=REASON_CLASSIFIER_V2)
    all_versions = repository.classifications_for_decision(decision_id)
    assert len(all_versions) == 2
    versions = {item.classifier_version for item in all_versions}
    assert versions == {REASON_CLASSIFIER_V1, REASON_CLASSIFIER_V2}

    v1_after = next(
        item for item in all_versions if item.classifier_version == REASON_CLASSIFIER_V1
    )
    v2_after = next(
        item for item in all_versions if item.classifier_version == REASON_CLASSIFIER_V2
    )
    # v1's own row is byte-identical to before v2 ever ran
    assert v1_after.classification_id == v1_before[0].classification_id
    assert v1_after.canonical_hash == v1_before[0].canonical_hash
    assert v1_after.classified_at == v1_before[0].classified_at
    # v1 and v2 disagree on this decision's family (the known real gap) --
    # without any identity conflict
    assert v1_after.reason_families == (ReasonFamily.OTHER_EXPLICIT,)
    assert v2_after.reason_families == (ReasonFamily.COMPETITIVE_SELECTION,)

    # the decision itself was never touched
    decision_after = repository.decision(decision_id)
    assert decision_after is not None
    assert decision_after.decision_id == decision_before.decision_id
    assert decision_after.canonical_hash == decision_before.canonical_hash
    assert decision_after.raw_reason_codes == decision_before.raw_reason_codes
    assert decision_after.disposition == decision_before.disposition
    assert _row_count(postgres_engine, "counterfactual_decision") == 1


def test_decision_event_count_does_not_multiply_with_classifier_versions(
    postgres_engine: Engine,
) -> None:
    """Part 11: counting classification rows across two versions must never
    be mistaken for -- or reported as -- twice as many considered
    decisions."""

    eligible = _projection("count-a")
    rejected = _projection("count-b", liquidity_state="INSUFFICIENT")
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-count", projections=(eligible, rejected))
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    decision_event_count = _row_count(postgres_engine, "counterfactual_decision")
    assert decision_event_count == 2

    sync = SyncCounterfactualClassification(repository, repository)
    sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1)
    sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V2)

    assert _row_count(postgres_engine, "counterfactual_decision") == decision_event_count
    assert (
        _row_count(postgres_engine, "counterfactual_decision_classification")
        == decision_event_count * 2
    )

    coverage_v1 = repository.classification_coverage(classifier_version=REASON_CLASSIFIER_V1)
    coverage_v2 = repository.classification_coverage(classifier_version=REASON_CLASSIFIER_V2)
    assert coverage_v1["decision_event_count"] == decision_event_count
    assert coverage_v2["decision_event_count"] == decision_event_count
    assert coverage_v1["classified_decision_count"] == decision_event_count
    assert coverage_v2["classified_decision_count"] == decision_event_count
    assert coverage_v1["classification_complete"] is True


def test_disposition_unaffected_by_classifier_mapping(postgres_engine: Engine) -> None:
    projection = _projection("disposition-a", liquidity_state="INSUFFICIENT")
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-disposition", projections=(projection,))
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    ledger_result = SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    decision_id = ledger_result.decision_ids[0]
    before = repository.decision(decision_id)
    assert before is not None
    assert before.disposition is DecisionDisposition.NOT_ADVANCED

    sync = SyncCounterfactualClassification(repository, repository)
    sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1)
    sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V2)

    after = repository.decision(decision_id)
    assert after is not None
    assert after.disposition is DecisionDisposition.NOT_ADVANCED


# --------------------------------------------------------------------------
# Tamper-detection / conflict semantics
# --------------------------------------------------------------------------


def test_conflicting_classification_payload_under_same_classification_id_is_rejected(
    postgres_engine: Engine,
) -> None:
    projection = _projection("conflict-a")
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-conflict", projections=(projection,))
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    SyncCounterfactualClassification(repository, repository).execute(
        classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    decision_id = repository.all_decisions()[0].decision_id
    (original,) = repository.classifications_for_decision(decision_id)

    from dataclasses import replace

    tampered = replace(original, canonical_hash="f" * 64)
    with pytest.raises(CounterfactualClassificationError):
        repository.save_classification(classification=tampered)


# --------------------------------------------------------------------------
# Future-outcome FK-target stability
# --------------------------------------------------------------------------


def test_future_outcome_fk_target_stable_across_classifier_versions(
    postgres_engine: Engine,
) -> None:
    """A future ``counterfactual_outcome`` row would foreign-key to
    ``decision_id`` -- prove it (and the decision's own ``canonical_hash``)
    is bit-for-bit unchanged across classifying the same decision under
    every known classifier version."""

    projection = _projection("outcome-fk-a")
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-outcome-fk", projections=(projection,))
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    ledger_result = SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    decision_id = ledger_result.decision_ids[0]
    before = repository.decision(decision_id)
    assert before is not None

    sync = SyncCounterfactualClassification(repository, repository)
    sync.execute(classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1)
    mid = repository.decision(decision_id)
    sync.execute(classified_at=NOW + timedelta(days=1), classifier_version=REASON_CLASSIFIER_V2)
    after = repository.decision(decision_id)

    assert before is not None and mid is not None and after is not None
    assert before.decision_id == mid.decision_id == after.decision_id
    assert before.canonical_hash == mid.canonical_hash == after.canonical_hash


# --------------------------------------------------------------------------
# Migration 0095: historically-honest backfill of pre-existing rows
# --------------------------------------------------------------------------


def test_0095_migration_backfills_v1_classification_and_corrects_canonical_hash(
    postgres_engine: Engine,
) -> None:
    """Simulates exactly what this migration does against every real
    pre-existing environment: a ``counterfactual_decision`` row already
    shaped precisely like Counterfactual Decision Ledger v1 produced it
    (``reason_families`` folded into both ``payload`` and
    ``canonical_hash``) is migrated 0094 -> 0095 and must come out with its
    ``canonical_hash`` corrected (dropping ``reason_families`` from the
    formula), every other field byte-identical, and its pre-existing
    ``reason_families`` explicitly identified as classifier v1's own
    retained output -- never silently dropped, never recomputed from
    scratch."""

    projection = _projection("migration-fixture")
    _seed_opportunity_set(postgres_engine, set_id="cfc-set-migration", projections=(projection,))

    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0094_counterfactual_ledger")
        connection.commit()

    decision_id = "cfc-migration-fixture-decision"
    old_reason_families = ["ECONOMICS", "OTHER_EXPLICIT"]
    raw_reason_codes = ["SOURCE_POLICY_REJECTED", "SOURCE:CONSERVATIVE_NET_GAP_NOT_POSITIVE"]
    evaluated_at_text = "2026-08-01T00:00:00Z"
    old_value = {
        "decision_id": decision_id,
        "origin": "SHARED_OPPORTUNITY_ELIGIBILITY",
        "origin_decision_id": "cfc-migration-fixture-origin",
        "origin_canonical_hash": "a" * 64,
        "origin_state": "REJECTED",
        "subject_projection_id": projection.projection_id,
        "subject_source_record_type": projection.source_record_type,
        "subject_source_record_id": projection.source_record_id,
        "venue": "CRYPTO",
        "opportunity_set_id": None,
        "mandate_id": None,
        "mandate_lineage_id": None,
        "mandate_version": None,
        "mandate_hash": None,
        "portfolio_snapshot_id": None,
        "portfolio_snapshot_hash": None,
        "evaluated_at": evaluated_at_text,
        "disposition": "NOT_ADVANCED",
        "raw_reason_codes": raw_reason_codes,
        "reason_families": old_reason_families,
        "explanation": "pre-migration fixture row",
        "canonical_hash": "",
        "authority": "NONE",
    }
    # The pre-fix formula this milestone's migration corrects: the same
    # 22-element positional tuple ``decision_canonical_hash`` uses today,
    # but with ``reason_families`` still folded in immediately before
    # ``explanation`` -- exactly how the original (now-removed) inline
    # builder code shaped it.
    old_payload_tuple = (
        "COUNTERFACTUAL_DECISION_V1",
        old_value["decision_id"],
        old_value["origin"],
        old_value["origin_decision_id"],
        old_value["origin_canonical_hash"],
        old_value["origin_state"],
        old_value["subject_projection_id"],
        old_value["subject_source_record_type"],
        old_value["subject_source_record_id"],
        old_value["venue"],
        old_value["opportunity_set_id"],
        old_value["mandate_id"],
        old_value["mandate_lineage_id"],
        old_value["mandate_version"],
        old_value["mandate_hash"],
        old_value["portfolio_snapshot_id"],
        old_value["portfolio_snapshot_hash"],
        old_value["evaluated_at"],
        old_value["disposition"],
        tuple(old_value["raw_reason_codes"]),
        tuple(old_reason_families),
        old_value["explanation"],
        "NONE",
    )
    old_hash = content_digest(old_payload_tuple)
    old_value["canonical_hash"] = old_hash

    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO counterfactual_decision (decision_id, origin, origin_decision_id, "
                "subject_projection_id, opportunity_set_id, mandate_id, portfolio_snapshot_id, "
                "venue, evaluated_at, disposition, payload, canonical_hash, authority) VALUES "
                "(:decision_id, :origin, :origin_decision_id, :subject_projection_id, NULL, NULL, "
                "NULL, :venue, :evaluated_at, :disposition, :payload, :canonical_hash, 'NONE')"
            ),
            {
                "decision_id": decision_id,
                "origin": old_value["origin"],
                "origin_decision_id": old_value["origin_decision_id"],
                "subject_projection_id": projection.projection_id,
                "venue": "CRYPTO",
                "evaluated_at": datetime.fromisoformat(evaluated_at_text.replace("Z", "+00:00")),
                "disposition": "NOT_ADVANCED",
                "payload": canonical_json_bytes(old_value).decode(),
                "canonical_hash": old_hash,
            },
        )

    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "head")
        connection.commit()

    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    decision = repository.decision(decision_id)
    assert decision is not None
    # untouched, byte-for-byte
    assert decision.decision_id == decision_id
    assert decision.origin_decision_id == old_value["origin_decision_id"]
    assert decision.origin_canonical_hash == old_value["origin_canonical_hash"]
    assert decision.raw_reason_codes == tuple(raw_reason_codes)
    assert decision.explanation == old_value["explanation"]
    assert decision.disposition is DecisionDisposition.NOT_ADVANCED
    # corrected: the old formula (reason_families folded in) no longer matches
    assert decision.canonical_hash != old_hash

    expected_new_hash = decision_canonical_hash(
        decision_id=decision.decision_id,
        origin=decision.origin,
        origin_decision_id=decision.origin_decision_id,
        origin_canonical_hash=decision.origin_canonical_hash,
        origin_state=decision.origin_state,
        subject_projection_id=decision.subject_projection_id,
        subject_source_record_type=decision.subject_source_record_type,
        subject_source_record_id=decision.subject_source_record_id,
        venue=decision.venue,
        opportunity_set_id=decision.opportunity_set_id,
        mandate_id=decision.mandate_id,
        mandate_lineage_id=decision.mandate_lineage_id,
        mandate_version=decision.mandate_version,
        mandate_hash=decision.mandate_hash,
        portfolio_snapshot_id=decision.portfolio_snapshot_id,
        portfolio_snapshot_hash=decision.portfolio_snapshot_hash,
        evaluated_at=decision.evaluated_at,
        disposition=decision.disposition,
        raw_reason_codes=decision.raw_reason_codes,
        explanation=decision.explanation,
    )
    assert decision.canonical_hash == expected_new_hash

    classifications = repository.classifications_for_decision(decision_id)
    assert len(classifications) == 1
    backfilled = classifications[0]
    assert backfilled.classifier_version == REASON_CLASSIFIER_V1
    assert backfilled.decision_id == decision_id
    assert backfilled.decision_canonical_hash == decision.canonical_hash
    assert sorted(item.value for item in backfilled.reason_families) == sorted(old_reason_families)
    assert backfilled.classifier_policy_hash == REASON_CLASSIFIER_V1_POLICY_HASH
    assert backfilled.authority == "NONE"

    # the migration-corrected row now resyncs/replays as a pure no-op
    replay = repository.save_decision(decision=decision)
    assert replay is True
    assert _row_count(postgres_engine, "counterfactual_decision") == 1
    assert _row_count(postgres_engine, "counterfactual_decision_classification") == 1


def test_0095_downgrade_blocks_with_retained_classification_evidence(
    postgres_engine: Engine,
) -> None:
    """Migration 0095's own downgrade guard, mirroring 0094's."""

    projection = _projection("downgrade-guard-a")
    _seed_opportunity_set(
        postgres_engine, set_id="cfc-set-downgrade-guard", projections=(projection,)
    )
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    SyncCounterfactualClassification(repository, repository).execute(
        classified_at=NOW, classifier_version=REASON_CLASSIFIER_V1
    )
    assert _row_count(postgres_engine, "counterfactual_decision_classification") >= 1

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0118_evidence_gap_recovery"

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0095 with retained counterfactual-classification evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0094_counterfactual_ledger")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # Downgrading straight to 0094 now passes through 0096 first: 0096's own
    # downgrade guard finds no retained decision_followup_observation rows in
    # this test (only counterfactual_decision/_classification evidence was
    # ever seeded here) and so succeeds, landing at 0095 -- where 0095's own
    # guard then genuinely blocks on the retained classification evidence
    # above, exactly as intended.  The chain stops at 0095, not at whatever
    # today's actual head is.
    assert current_revision == "0095_reason_classification"
    assert _row_count(postgres_engine, "counterfactual_decision_classification") >= 1
