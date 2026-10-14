"""PostgreSQL persistence proofs for the Rejected-Opportunity / Counterfactual
Decision Ledger v1: deterministic round trip, idempotent replay, changed-
evidence versioning, point-in-time mandate/portfolio lineage retention,
queryable accepted/rejected/constrained cohorts without survivor bias, and
bounded-filter/coverage diagnostics.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_ledger import (
    SyncCounterfactualLedgerFromMandateEvaluations,
    SyncCounterfactualLedgerFromSharedOpportunity,
)
from apex.application.investment_mandate import (
    CreateInvestmentMandate,
    EvaluateOpportunityAgainstMandate,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualLedgerError,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.portfolio_state import (
    EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
    AccountKind,
    PortfolioAccountBalance,
    PortfolioVenue,
    build_portfolio_snapshot,
)
from apex.domain.shared_opportunity import (
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _projection(identity: str, **overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": f"cfl-proj-{identity}",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "FixtureOpportunity",
        "source_record_id": f"cfl-source-{identity}",
        "source_canonical_hash": content_digest((identity,)),
        "strategy_or_method_id": "CFL_FIXTURE_V1",
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
        "supporting_evidence_ids": (f"cfl-evidence-{identity}",),
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


def _seed_full_cohort(engine: Engine, *, tag: str) -> tuple[str, dict[str, str]]:
    """One eligible, one economically/liquidity-rejected, one expired, and
    one evidence-blocked projection in a single opportunity set -- so a sync
    must retain all four dispositions, never just the negative ones."""

    eligible = _projection(f"{tag}-eligible")
    rejected = _projection(f"{tag}-rejected", liquidity_state="INSUFFICIENT")
    expired = _projection(f"{tag}-expired", valid_until=NOW - timedelta(days=1))
    blocked = _projection(f"{tag}-blocked", costs_retained=False)
    set_id = _seed_opportunity_set(
        engine,
        set_id=f"cfl-set-{tag}",
        projections=(eligible, rejected, expired, blocked),
    )
    return set_id, {
        "eligible": eligible.projection_id,
        "rejected": rejected.projection_id,
        "expired": expired.projection_id,
        "blocked": blocked.projection_id,
    }


def _seed_mandate_evaluation(
    engine: Engine, *, tag: str, opportunity: SharedOpportunityProjection
) -> None:
    mandate_repository = PostgresInvestmentMandateRepository(engine)
    CreateInvestmentMandate(mandate_repository).execute(
        idempotency_key=f"cfl-mandate-{tag}",
        lineage_id=f"cfl-lineage-{tag}",
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="1000",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
    )
    # Real cash evidence, not just a policy limit -- ``evaluate_mandate``
    # derives available headroom from the portfolio snapshot's own known
    # balances, never from the mandate's ``total_capital_allocated`` alone.
    balance = PortfolioAccountBalance.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id=f"cfl-account-{tag}",
        cash="1000",
        reserved_cash="0",
        as_of=NOW,
        captured_at=NOW,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="Test",
        source_record_id=f"cfl-balance-{tag}",
        source_canonical_hash=content_digest((tag, "balance")),
    )
    snapshot = build_portfolio_snapshot(
        snapshot_id=f"cfl-snapshot-{tag}",
        as_of=NOW,
        created_at=NOW,
        account_balances=(balance,),
        positions=(),
        unavailable_sources=(),
    )
    PostgresPortfolioSnapshotRepository(engine).save_portfolio_snapshot(
        idempotency_key=f"cfl-snapshot-{tag}-key",
        snapshot=snapshot,
        balances=(balance,),
        positions=(),
    )
    EvaluateOpportunityAgainstMandate(mandate_repository).execute(
        idempotency_key=f"cfl-evaluation-{tag}",
        opportunity_id=opportunity.projection_id,
        evaluated_at=NOW,
        mandate_lineage_id=f"cfl-lineage-{tag}",
        portfolio_snapshot_id=snapshot.snapshot_id,
    )


# --------------------------------------------------------------------------
# Shared Opportunity eligibility origin
# --------------------------------------------------------------------------


def test_sync_from_shared_opportunity_retains_every_disposition_without_survivor_bias(
    postgres_engine: Engine,
) -> None:
    _seed_full_cohort(postgres_engine, tag="cohort-a")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    source = repository.shared_opportunity
    result = SyncCounterfactualLedgerFromSharedOpportunity(repository, source).execute(
        evaluated_at=NOW
    )
    assert result.considered_count == 4
    assert result.newly_written_count == 4
    assert result.idempotent_replay_count == 0

    decisions = repository.decisions(origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY)
    by_state = {
        item.origin_state: item for item in decisions if item.decision_id in result.decision_ids
    }
    assert by_state[EligibilityState.ELIGIBLE.value].disposition is DecisionDisposition.ADVANCED
    assert by_state[EligibilityState.REJECTED.value].disposition is DecisionDisposition.NOT_ADVANCED
    assert by_state[EligibilityState.EXPIRED.value].disposition is DecisionDisposition.NOT_ADVANCED
    assert (
        by_state[EligibilityState.BLOCKED_MISSING_EVIDENCE.value].disposition
        is DecisionDisposition.NOT_ADVANCED
    )
    for item in decisions:
        assert item.authority == "NONE"


def test_sync_from_shared_opportunity_is_idempotent_on_replay(postgres_engine: Engine) -> None:
    _seed_full_cohort(postgres_engine, tag="cohort-b")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    source = repository.shared_opportunity
    sync = SyncCounterfactualLedgerFromSharedOpportunity(repository, source)
    first = sync.execute(evaluated_at=NOW)
    before_count = _row_count(postgres_engine, "counterfactual_decision")
    second = sync.execute(evaluated_at=NOW)
    after_count = _row_count(postgres_engine, "counterfactual_decision")

    assert after_count == before_count
    assert second.newly_written_count == 0
    assert second.idempotent_replay_count == first.considered_count
    assert set(first.decision_ids) == set(second.decision_ids)


def test_changed_projection_evidence_produces_new_ledger_row_not_a_mutation(
    postgres_engine: Engine,
) -> None:
    """Re-evaluating a differently-priced/staled Opportunity (a distinct
    ``SharedOpportunityProjection``/``EligibilityDecision`` pair, as the real
    pipeline produces on every fresh discovery run -- a fresh discovery run
    mints both a new ``projection_id`` and a new ``source_record_id``
    together, so two independent projections is the realistic shape, not an
    in-place edit of one) must append a new, independent ledger row rather
    than altering the first one."""

    stale = _projection("cohort-c-liquid", liquidity_state="SUFFICIENT")
    fresh = _projection(
        "cohort-c-illiquid",
        liquidity_state="INSUFFICIENT",  # materially different economics
    )
    set_one = _seed_opportunity_set(
        postgres_engine, set_id="cfl-set-cohort-c-1", projections=(stale,)
    )
    set_two = _seed_opportunity_set(
        postgres_engine, set_id="cfl-set-cohort-c-2", projections=(fresh,)
    )
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    sync = SyncCounterfactualLedgerFromSharedOpportunity(repository, repository.shared_opportunity)
    result = sync.execute(evaluated_at=NOW)
    ids_for_set = {
        set_one: [
            d for d in result.decision_ids if repository.decision(d).opportunity_set_id == set_one
        ],
        set_two: [
            d for d in result.decision_ids if repository.decision(d).opportunity_set_id == set_two
        ],
    }
    assert len(ids_for_set[set_one]) == 1
    assert len(ids_for_set[set_two]) == 1
    assert ids_for_set[set_one][0] != ids_for_set[set_two][0]
    first_decision = repository.decision(ids_for_set[set_one][0])
    second_decision = repository.decision(ids_for_set[set_two][0])
    assert first_decision.disposition is DecisionDisposition.ADVANCED
    assert second_decision.disposition is DecisionDisposition.NOT_ADVANCED
    # both remain independently retained -- the earlier fact was never erased
    assert _row_count(postgres_engine, "counterfactual_decision") >= 2


def test_bounded_filters_by_date_venue_and_disposition(postgres_engine: Engine) -> None:
    _seed_full_cohort(postgres_engine, tag="cohort-d")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)

    in_range = repository.decisions(
        start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1), venue=SharedVenue.CRYPTO
    )
    assert len(in_range) >= 4
    out_of_range = repository.decisions(start=NOW + timedelta(days=1), end=NOW + timedelta(days=2))
    assert not any(
        item.subject_projection_id.startswith("cfl-proj-cohort-d") for item in out_of_range
    )

    advanced_only = repository.decisions(disposition=DecisionDisposition.ADVANCED)
    not_advanced_only = repository.decisions(disposition=DecisionDisposition.NOT_ADVANCED)
    assert all(item.disposition is DecisionDisposition.ADVANCED for item in advanced_only)
    assert all(item.disposition is DecisionDisposition.NOT_ADVANCED for item in not_advanced_only)


def test_decisions_for_subject_returns_only_that_subjects_rows(postgres_engine: Engine) -> None:
    _set_id, ids = _seed_full_cohort(postgres_engine, tag="cohort-e")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    subject_rows = repository.decisions_for_subject(ids["rejected"])
    assert len(subject_rows) == 1
    assert subject_rows[0].subject_projection_id == ids["rejected"]
    assert subject_rows[0].disposition is DecisionDisposition.NOT_ADVANCED


def test_conflicting_payload_under_same_decision_id_is_rejected(postgres_engine: Engine) -> None:
    _seed_full_cohort(postgres_engine, tag="cohort-f")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    result = SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    original = repository.decision(result.decision_ids[0])
    assert original is not None
    from dataclasses import replace

    tampered = replace(original, canonical_hash="f" * 64)
    with pytest.raises(CounterfactualLedgerError):
        repository.save_decision(decision=tampered)


# --------------------------------------------------------------------------
# Investment Mandate origin: point-in-time lineage
# --------------------------------------------------------------------------


def test_sync_from_mandate_evaluation_retains_mandate_and_portfolio_lineage(
    postgres_engine: Engine,
) -> None:
    opportunity = _projection("cohort-g")
    _seed_opportunity_set(postgres_engine, set_id="cfl-set-cohort-g", projections=(opportunity,))
    _seed_mandate_evaluation(postgres_engine, tag="cohort-g", opportunity=opportunity)

    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    result = SyncCounterfactualLedgerFromMandateEvaluations(
        repository, repository.investment_mandate
    ).execute(evaluated_at=NOW)
    assert result.considered_count == 1
    decision = repository.decision(result.decision_ids[0])
    assert decision is not None
    assert decision.origin is DecisionOrigin.INVESTMENT_MANDATE_EVALUATION
    assert decision.mandate_lineage_id == "cfl-lineage-cohort-g"
    assert decision.mandate_version == 1
    assert decision.mandate_id is not None
    assert decision.mandate_hash is not None
    assert decision.portfolio_snapshot_id == "cfl-snapshot-cohort-g"
    assert decision.portfolio_snapshot_hash is not None
    assert decision.disposition is DecisionDisposition.ADVANCED  # permissive fixture mandate
    assert decision.raw_reason_codes == ()


def test_sync_from_mandate_evaluation_is_idempotent_on_replay(postgres_engine: Engine) -> None:
    opportunity = _projection("cohort-h")
    _seed_opportunity_set(postgres_engine, set_id="cfl-set-cohort-h", projections=(opportunity,))
    _seed_mandate_evaluation(postgres_engine, tag="cohort-h", opportunity=opportunity)

    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    sync = SyncCounterfactualLedgerFromMandateEvaluations(repository, repository.investment_mandate)
    first = sync.execute(evaluated_at=NOW)
    before = _row_count(postgres_engine, "counterfactual_decision")
    second = sync.execute(evaluated_at=NOW)
    after = _row_count(postgres_engine, "counterfactual_decision")
    assert after == before
    assert second.idempotent_replay_count == first.considered_count
    assert second.newly_written_count == 0


# --------------------------------------------------------------------------
# Coverage diagnostics
# --------------------------------------------------------------------------


def test_coverage_reports_incomplete_before_sync_and_complete_after(
    postgres_engine: Engine,
) -> None:
    _seed_full_cohort(postgres_engine, tag="cohort-i")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)

    before = repository.coverage()
    assert before["upstream_eligibility_decision_count"] >= 4
    assert (
        before["upstream_eligibility_decision_count"] > before["ledger_eligibility_decision_count"]
    )
    assert before["eligibility_coverage_complete"] is False

    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    after = repository.coverage()
    assert (
        after["upstream_eligibility_decision_count"] == after["ledger_eligibility_decision_count"]
    )
    assert after["eligibility_coverage_complete"] is True


# --------------------------------------------------------------------------
# Migration downgrade guard (append-only enforcement)
# --------------------------------------------------------------------------


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_retained_decision_blocks_0094_downgrade(postgres_engine: Engine) -> None:
    """A retained ``counterfactual_decision`` row must block downgrading
    past 0094 -- the guard must fire before any DDL runs, leaving the
    schema, the retained row, and the current revision untouched."""

    _seed_full_cohort(postgres_engine, tag="cohort-downgrade-guard")
    repository = PostgresCounterfactualLedgerRepository(postgres_engine)
    SyncCounterfactualLedgerFromSharedOpportunity(
        repository, repository.shared_opportunity
    ).execute(evaluated_at=NOW)
    assert _row_count(postgres_engine, "counterfactual_decision") >= 1

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0118_evidence_gap_recovery"

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0094 with retained counterfactual-ledger evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0093_tmc_component_as_of")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # migrations 0096 (Counterfactual Decision Follow-Up Observation v1) and
    # 0095 (Counterfactual Decision Classification & Interpretation
    # Versioning v1) both sit above 0094 and own no retained evidence here
    # (this test never syncs any classification or follow-up observation),
    # so each one's own single empty-table downgrade step commits genuinely
    # before 0094's guard is reached -- exactly the same shape documented in
    # test_postgres_total_market_context.py's test_downgrade_0093_blocks_
    # with_retained_component_evidence for the 0093/0094 pair. The walk
    # still lands on 0094, not head, regardless of how many empty
    # migrations now sit above it.
    assert current_revision == "0094_counterfactual_ledger"
    assert _row_count(postgres_engine, "counterfactual_decision") >= 1
