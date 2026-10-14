"""PostgreSQL persistence proofs for Kalshi Decision-Linked Baseline Quote
Evidence v1: migration 0102, ``PostgresKalshiBaselineQuoteRepository``/
``PostgresKalshiBaselineQuoteEvidenceSource``, ``SyncKalshiBaselineQuoteEvidence``,
and the real recurring-cycle wiring (``COUNTERFACTUAL_FOLLOWUP_REFRESH``
capture sub-step and ``COUNTERFACTUAL_OUTCOME_REFRESH``'s additive V2 pass)
against a real, freshly migrated ``apex_test`` database. Zero genuine
network requests anywhere in this file -- every Kalshi evaluation is
persisted through the real, existing ``ScanPublicBinaryComplements`` +
``postgres_public_binary_complement_store`` path against a synthetic
in-process fixture client, exactly like ``test_postgres_binary_complements.py``
already does."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import (
    BatchedLedgerEvidenceSource,
    PostgresCounterfactualLedgerRepository,
)
from apex.adapters.counterfactual_outcome_postgres import PostgresCounterfactualOutcomeRepository
from apex.adapters.kalshi_baseline_quote_postgres import (
    PostgresKalshiBaselineQuoteEvidenceSource,
)
from apex.adapters.kalshi_complement_fixtures import KalshiFixtureBinaryComplementClient
from apex.adapters.kalshi_public import (
    KALSHI_MARKETS_ENDPOINT_IDENTITY,
    KalshiPublicMarketNormalizer,
)
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import ExistingCapabilityOperatingTaskExecutor
from apex.adapters.postgres import (
    postgres_public_binary_complement_store,
    postgres_public_market_discovery_store,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_classification import SyncCounterfactualClassification
from apex.application.counterfactual_followup import FollowUpSourceUnavailable, KalshiMarketRead
from apex.application.counterfactual_ledger import SyncCounterfactualLedger
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    DiscoverPublicPredictionMarkets,
    PublicMarketScanRequest,
)
from apex.application.kalshi_baseline_quote import SyncKalshiBaselineQuoteEvidence
from apex.application.operating_cycle import RegisterOperatingCyclePlan
from apex.application.scan_public_binary_complements import (
    BinaryComplementScanRequest,
    ScanPublicBinaryComplements,
)
from apex.domain.counterfactual_classification import CURRENT_CLASSIFIER_VERSION
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
)
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_POLICY_V2,
    CounterfactualOutcomeQualificationState,
)
from apex.domain.kalshi_baseline_quote import (
    BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    KALSHI_BASELINE_QUOTE_POLICY_V2,
    KalshiBaselineQuoteError,
    KalshiQuoteKind,
)
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskType,
)
from apex.domain.public_complement import ComplementState
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

ROOT_HEAD_REVISION = "0118_evidence_gap_recovery"
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
DISCOVERY_CLOCK = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


class _OneMarketClient:
    def fetch(self, **kwargs: object) -> CapturedPublicMarketPage:
        if kwargs.get("status") != "open" or kwargs.get("cursor") is not None:
            raise ValueError("synthetic market request is malformed")
        market = {
            "ticker": "COMPLEMENT-MARKET",
            "event_ticker": "COMPLEMENT-EVENT",
            "series_ticker": "COMPLEMENT-SERIES",
            "status": "open",
            "open_time": "2026-07-20T11:00:00Z",
            "close_time": "2026-07-20T13:00:00Z",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.55",
            "no_bid_dollars": "0.45",
            "no_ask_dollars": "0.55",
            "yes_bid_size_fp": "10.00",
            "yes_ask_size_fp": "10.00",
            "rules_primary": "rule-v1",
        }
        document = {"markets": [market], "cursor": None}
        raw_payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return CapturedPublicMarketPage(
            endpoint_identity=KALSHI_MARKETS_ENDPOINT_IDENTITY,
            request_cursor=None,
            received_at=DISCOVERY_CLOCK,
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw_payload,
            response_cursor=None,
            document=document,
        )


def _persist_real_binary_complement_evaluation(
    engine: Engine, *, run_key: str, scenario: str = "positive"
) -> dict[str, object]:
    """Persists one genuine, immutable ``binary_complement_evaluation`` (+
    its ``binary_complement_leg`` rows, and its already-immutable
    ``market_orderbook_snapshot``/``market_orderbook_level`` rows) through
    the real, unmodified production adapters -- exactly the evidence a
    genuine KALSHI_MARKET_REFRESH cycle would have already committed.
    ``scenario`` defaults to the original ``"positive"`` (CANDIDATE) shape;
    Kalshi Baseline Quote Capture & Economic Maturation v1's own tests pass
    ``"non_candidate"``/``"one_sided"``/``"empty"`` to exercise the new
    market-quote-observation (V2) path against the identical real fixture
    shapes ``domain.kalshi_baseline_quote``'s own docstring already reasons
    about. Returns the row's own ``evaluation_id``/``provider_market_ticker``/
    ``canonical_evaluation_hash``/``orderbook_snapshot_id`` for building a
    matching ``SharedOpportunityProjection`` afterward."""

    discovery_run_id = (
        DiscoverPublicPredictionMarkets(
            client=_OneMarketClient(),
            normalizer=KalshiPublicMarketNormalizer(),
            store=postgres_public_market_discovery_store(engine),
            clock=lambda: DISCOVERY_CLOCK,
        )
        .discover(PublicMarketScanRequest(run_key=run_key, maximum_pages=1))
        .ingestion_run_id
    )
    report = ScanPublicBinaryComplements(
        client=KalshiFixtureBinaryComplementClient(scenario=scenario),
        store=postgres_public_binary_complement_store(engine),
        clock=lambda: DISCOVERY_CLOCK,
    ).scan(
        BinaryComplementScanRequest(
            market_discovery_run_id=discovery_run_id,
            market_limit=1,
            maximum_depth=5,
            maximum_bundle_quantity="10",
        )
    )
    evaluation = report.evaluations[0]
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT canonical_evaluation_hash, provider_market_ticker, state, "
                    "matched_quantity, total_modeled_acquisition_cost, gross_settlement_value, "
                    "orderbook_snapshot_id "
                    "FROM binary_complement_evaluation WHERE evaluation_id = :evaluation_id"
                ),
                {"evaluation_id": evaluation.evaluation_id},
            )
            .mappings()
            .one()
        )
    return {
        "evaluation_id": evaluation.evaluation_id,
        "canonical_evaluation_hash": str(row["canonical_evaluation_hash"]),
        "provider_market_ticker": str(row["provider_market_ticker"]),
        "state": str(row["state"]),
        "matched_quantity": row["matched_quantity"],
        "total_modeled_acquisition_cost": row["total_modeled_acquisition_cost"],
        "gross_settlement_value": row["gross_settlement_value"],
        "orderbook_snapshot_id": str(row["orderbook_snapshot_id"]),
    }


def _kalshi_projection(
    evaluation_info: dict[str, object], *, identity: str
) -> SharedOpportunityProjection:
    ticker = str(evaluation_info["provider_market_ticker"])
    return SharedOpportunityProjection.create(
        projection_id=f"kbq-proj-{identity}",
        venue=SharedVenue.KALSHI,
        source_record_type=BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
        source_record_id=str(evaluation_info["evaluation_id"]),
        source_canonical_hash=str(evaluation_info["canonical_evaluation_hash"]),
        strategy_or_method_id="public-binary-complement-v1",
        instrument_id=ticker,
        product_or_market_id=ticker,
        direction="BUY_YES_AND_NO_COMPLEMENT",
        generated_at=NOW - timedelta(minutes=5),
        valid_until=NOW + timedelta(hours=1),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=300,
        freshness_limit_seconds=3600,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value="0.024",
        comparable_value_identity="CONSERVATIVE_NET_LOWER_BOUND_OVER_MODELED_COST",
        uncertainty=None,
        # Deliberately INSUFFICIENT liquidity: forces a genuine REJECTED
        # EligibilityDecision (this milestone's evidence-capture path does
        # not require ELIGIBLE -- CounterfactualDecision is built from
        # either disposition; mirrors test_postgres_counterfactual_outcome_
        # recurring.py's own established fixture pattern).
        maximum_loss="10",
        modeled_fees="0.35",
        modeled_slippage=None,
        costs_retained=True,
        liquidity_state="INSUFFICIENT",
        liquidity_limitations=("FIXTURE_LIQUIDITY_LIMITATION",),
        required_hypothetical_capital="10",
        maximum_sensible_research_allocation="10",
        source_maximum_allocation_fraction=None,
        expected_holding_days=3,
        estimated_lockup_days=3,
        supporting_evidence_ids=(f"kbq-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


def _seed_kalshi_projection_and_eligibility(
    engine: Engine,
    *,
    evaluation_info: dict[str, object],
    identity: str,
    created_at: datetime = NOW,
) -> SharedOpportunityProjection:
    """``created_at`` defaults to the module ``NOW`` (every existing call
    site's unchanged behavior). Kalshi Baseline Temporal Semantics & Sweep
    Isolation v1's own regression test passes an earlier value deliberately
    -- ``build_opportunity_set`` copies it verbatim onto every
    ``EligibilityDecision.evaluated_at`` it produces (mirroring the real
    ``operating-cycle-worker``'s own coarse, day-floor ``as_of``), so this
    is the one lever needed to reproduce "decision rebuilt before its own
    referenced evaluation's real, later capture instant" without touching
    the evaluation/snapshot fixture clock at all."""

    projection = _kalshi_projection(evaluation_info, identity=identity)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"kbq-set-{identity}", created_at=created_at, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"kbq-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    return projection


class _NoKalshiFollowUpSource:
    def read_market_status(self, **_: object) -> KalshiMarketRead:
        raise FollowUpSourceUnavailable("FIXTURE_KALSHI_FOLLOWUP_NEVER_CALLED", request_count=0)


def _executor(engine: Engine) -> ExistingCapabilityOperatingTaskExecutor:
    state_source = PostgresOperatingStateSource(engine, PostgresOperatingCycleRepository(engine))
    return ExistingCapabilityOperatingTaskExecutor(
        engine, state_source, followup_kalshi_source=_NoKalshiFollowUpSource()
    )


def _due_task(cycle_id: str, task_type: OperatingTaskType) -> DueTask:
    return DueTask.create(
        task_type=task_type,
        disposition=DueDisposition.DUE,
        reasons=("fixture-due",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="fixture",
    )


def _register_plan(engine: Engine, *, plan_id: str) -> OperatingCyclePlan:
    repository = PostgresOperatingCycleRepository(engine)
    plan, _ = RegisterOperatingCyclePlan(repository).execute(
        plan_id=plan_id,
        crypto_pulse_plan_id="retained-pulse-plan",
        actor="persistence-test",
        created_at=NOW,
        idempotency_key=f"{plan_id}-key",
    )
    return plan


# ---------------------------------------------------------------------------
# Migration 0102 shape and downgrade guard
# ---------------------------------------------------------------------------


def test_migration_creates_the_table_and_it_is_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0


def test_migration_head_is_0102(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == ROOT_HEAD_REVISION


def test_immutable_trigger_blocks_update_and_delete(postgres_engine: Engine) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-immutable"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="immutable"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-immutable")
    executor = _executor(postgres_engine)
    executor.execute(
        task=_due_task("cycle-immutable", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-immutable",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.connect() as connection,
    ):
        connection.execute(sa.text("UPDATE kalshi_baseline_quote SET run_id = 'tampered'"))
        connection.commit()
    with (
        pytest.raises(Exception, match="immutable Apex row cannot be changed"),
        postgres_engine.connect() as connection,
    ):
        connection.execute(sa.text("DELETE FROM kalshi_baseline_quote"))
        connection.commit()
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_downgrade_refuses_with_retained_evidence(postgres_engine: Engine) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-downgrade-guard"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="downgrade-guard"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-downgrade-guard")
    _executor(postgres_engine).execute(
        task=_due_task("cycle-downgrade-guard", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-downgrade-guard",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0102 with retained kalshi-baseline-quote evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        # From head (0103), a multi-step downgrade to 0101 first steps
        # 0103 -> 0102 cleanly (no V2 outcome evidence in this fixture),
        # then genuinely blocks at 0102 -> 0101 -- the guard under test.
        command.downgrade(_alembic_config(connection), "0101_counterfactual_outcome")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0102_kalshi_baseline_quote"
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    # Restore head for any later test in this session-scoped fixture.
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), ROOT_HEAD_REVISION)
        connection.commit()


def test_downgrade_and_reupgrade_round_trip_when_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0101_counterfactual_outcome")
        connection.commit()
    with postgres_engine.connect() as connection:
        exists = connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'kalshi_baseline_quote')"
            )
        )
    assert exists is False
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), ROOT_HEAD_REVISION)
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == ROOT_HEAD_REVISION
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0


def test_0103_widens_the_outcome_policy_constraint_to_accept_v2(postgres_engine: Engine) -> None:
    """The real DB-level constraint (not merely the domain's Python
    validation) genuinely accepts both policy versions after 0103, and a
    third, unrelated value is still genuinely rejected."""

    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO counterfactual_outcome_aggregate "
                "(aggregate_id, cohort_id, outcome_policy_version, run_id, venue, "
                "strategy_or_method_id, reason_family, horizon_kind, baseline_kind, "
                "followup_policy_version, classifier_version, evidence_quality_policy_version, "
                "qualification_state, total_observations_considered, mature_observation_count, "
                "eligible_observation_count, generated_at, payload, canonical_hash, authority) "
                "VALUES (:aggregate_id, 'cohort-0103', :policy, 'run-0103', 'KALSHI', "
                "'method-0103', 'ECONOMICS', 'SHORT_HORIZON', 'DECISION_TIME', "
                "'FOLLOWUP_POLICY_V2', 'REASON_CLASSIFIER_V2', "
                "'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1', 'INSUFFICIENT_SAMPLE', 0, 0, 0, "
                "now(), '{}', :hash, 'NONE')"
            ),
            {
                "aggregate_id": "agg-0103-v2",
                "policy": "COUNTERFACTUAL_OUTCOME_POLICY_V2",
                "hash": "a" * 64,
            },
        )
    assert _row_count(postgres_engine, "counterfactual_outcome_aggregate") == 1

    with (
        pytest.raises(Exception, match="ck_counterfactual_outcome_policy_version"),
        postgres_engine.connect() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO counterfactual_outcome_aggregate "
                "(aggregate_id, cohort_id, outcome_policy_version, run_id, venue, "
                "strategy_or_method_id, reason_family, horizon_kind, baseline_kind, "
                "followup_policy_version, classifier_version, "
                "evidence_quality_policy_version, qualification_state, "
                "total_observations_considered, mature_observation_count, "
                "eligible_observation_count, generated_at, payload, canonical_hash, "
                "authority) "
                "VALUES ('agg-0103-bad', 'cohort-0103-bad', 'NOT_A_REAL_POLICY_VERSION', "
                "'run-0103-bad', 'KALSHI', 'method-0103', 'ECONOMICS', 'SHORT_HORIZON', "
                "'DECISION_TIME', 'FOLLOWUP_POLICY_V2', 'REASON_CLASSIFIER_V2', "
                "'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1', 'INSUFFICIENT_SAMPLE', 0, 0, 0, "
                "now(), '{}', :hash, 'NONE')"
            ),
            {"hash": "b" * 64},
        )
        connection.commit()


def test_0103_downgrade_refuses_with_retained_v2_evidence(postgres_engine: Engine) -> None:
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO counterfactual_outcome_aggregate "
                "(aggregate_id, cohort_id, outcome_policy_version, run_id, venue, "
                "strategy_or_method_id, reason_family, horizon_kind, baseline_kind, "
                "followup_policy_version, classifier_version, evidence_quality_policy_version, "
                "qualification_state, total_observations_considered, mature_observation_count, "
                "eligible_observation_count, generated_at, payload, canonical_hash, authority) "
                "VALUES ('agg-0103-guard', 'cohort-0103-guard', "
                "'COUNTERFACTUAL_OUTCOME_POLICY_V2', 'run-0103-guard', 'KALSHI', "
                "'method-0103', 'ECONOMICS', 'SHORT_HORIZON', 'DECISION_TIME', "
                "'FOLLOWUP_POLICY_V2', 'REASON_CLASSIFIER_V2', "
                "'FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1', 'INSUFFICIENT_SAMPLE', 0, 0, 0, "
                "now(), '{}', :hash, 'NONE')"
            ),
            {"hash": "c" * 64},
        )
    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0103 with retained COUNTERFACTUAL_OUTCOME_POLICY_V2 evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0102_kalshi_baseline_quote")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # Deliberately not ``ROOT_HEAD_REVISION`` (Securities Research -> Shared
    # Opportunity Recurring Bridge v1): ``command.downgrade`` above is a
    # multi-step walk from head down to "0102_kalshi_baseline_quote", and
    # migration 0104 sits directly above 0103 with its own, unrelated
    # downgrade guard (retained SECURITIES-venue evidence -- none exists in
    # this test's fixture) that completes and commits cleanly *before*
    # 0103's own guard is ever reached. The resting point after 0103's own
    # guard fires is therefore always "one step below whatever the true
    # head is", not head itself -- true before this migration existed (when
    # 0103 coincidentally *was* head) and still true now.
    assert current_revision == "0103_counterfactual_outcome_v2"


# ---------------------------------------------------------------------------
# PostgresKalshiBaselineQuoteRepository / PostgresKalshiBaselineQuoteEvidenceSource
# ---------------------------------------------------------------------------


def test_source_finds_pending_kalshi_decision_and_reads_real_evaluation_evidence(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-source-read"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="source-read"
    )
    # No operating-cycle plan needed -- this test drives ledger sync and
    # the baseline-quote source directly, never through the executor.
    # Ledger sync only (do not run the baseline-quote capture sub-step
    # itself here -- exercised separately below); calling the same
    # COUNTERFACTUAL_FOLLOWUP_REFRESH branch is the only way ledger sync
    # runs, so this test intentionally lets baseline-quote capture happen
    # too and asserts the *source* would have found it as pending beforehand
    # by re-deriving from a hand-rolled ledger-sync-only call instead.
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)
    decision = ledger.all_decisions()[0]
    assert decision.venue is SharedVenue.KALSHI
    assert decision.subject_source_record_type == BINARY_COMPLEMENT_SOURCE_RECORD_TYPE

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    pending = source.pending_kalshi_decisions(limit=10)
    assert pending == (decision,)

    evidence = source.binary_complement_evidence((decision.subject_source_record_id,))
    assert decision.subject_source_record_id in evidence
    evaluation, legs = evidence[decision.subject_source_record_id]
    assert evaluation.evaluation_id == decision.subject_source_record_id
    assert len(legs) > 0
    assert evaluation.matched_quantity == evaluation_info["matched_quantity"]


def test_sync_captures_a_genuine_baseline_quote_and_no_longer_reports_it_pending(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-sync-capture"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="sync-capture"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)
    decision = ledger.all_decisions()[0]

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    result = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="sync-capture-run", generated_at=NOW
    )
    assert result.considered_count == 1
    assert result.captured_count == 1
    assert result.idempotent_replay_count == 0
    assert result.no_candidate_evaluation_count == 0
    assert result.source_evaluation_not_found_count == 0
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    quote = source.quotes.quote_for_decision(decision.decision_id)
    assert quote is not None
    assert quote.decision_id == decision.decision_id
    assert quote.matched_quantity == evaluation_info["matched_quantity"]
    assert quote.total_modeled_acquisition_cost == evaluation_info["total_modeled_acquisition_cost"]
    assert quote.modeled_settlement_value == evaluation_info["gross_settlement_value"]
    assert quote.authority == "NONE"

    # No longer pending -- the bounded, self-healing sweep is queried again.
    assert source.pending_kalshi_decisions(limit=10) == ()


def test_repeated_sync_is_idempotent_and_creates_no_duplicate_row(postgres_engine: Engine) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-idempotent"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="idempotent"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    first = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="idempotent-run-1", generated_at=NOW
    )
    assert first.captured_count == 1
    second = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="idempotent-run-2", generated_at=NOW + timedelta(hours=3)
    )
    # The decision is no longer pending at all (already captured), so the
    # second sweep considers zero candidates -- distinct from, but equally
    # valid proof of idempotency as, a same-candidate replay.
    assert second.considered_count == 0
    assert second.captured_count == 0
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    # Direct repository-level replay proof: saving the identical quote
    # object again (as if a crash resumed mid-sweep before the "already
    # captured" state was visible) is a true idempotent replay, not a
    # duplicate or a conflict.
    quote = source.quotes.all_quotes()[0]
    assert source.quotes.save_quote(quote=quote) is True
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_conflicting_payload_under_the_same_quote_id_raises(postgres_engine: Engine) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-conflict"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="conflict"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="conflict-run", generated_at=NOW
    )
    quote = source.quotes.all_quotes()[0]
    tampered = dataclasses.replace(quote, canonical_hash="f" * 64)
    with pytest.raises(KalshiBaselineQuoteError, match="KALSHI_BASELINE_QUOTE_IDENTITY_CONFLICT"):
        source.quotes.save_quote(quote=tampered)


# ---------------------------------------------------------------------------
# Recurring worker integration: COUNTERFACTUAL_FOLLOWUP_REFRESH capture
# sub-step, replay safety, and no N+1/no provider request.
# ---------------------------------------------------------------------------


def test_recurring_task_captures_baseline_quote_automatically(postgres_engine: Engine) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-recurring"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="recurring"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-recurring")
    executor = _executor(postgres_engine)

    result = executor.execute(
        task=_due_task("cycle-recurring", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-recurring",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}
    assert result.network_requests == 0
    assert "KALSHI_BASELINE_QUOTE_CAPTURED:1" in result.warnings
    assert "KALSHI_BASELINE_QUOTE_CONSIDERED:1" in result.warnings
    assert not any(
        warning.startswith("KALSHI_BASELINE_QUOTE_SYNC_FAILED") for warning in result.warnings
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_worker_replay_creates_no_duplicate_baseline_quote_evidence(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-replay"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="replay"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-replay")
    executor = _executor(postgres_engine)

    task = _due_task("cycle-replay", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
    first = executor.execute(
        task=task, cycle_id="cycle-replay", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    # Genuine crash-resume replay: identical cycle_id/task, a fresh executor
    # instance (a new process would construct one), a later wall-clock
    # instant.
    second = ExistingCapabilityOperatingTaskExecutor(
        postgres_engine,
        PostgresOperatingStateSource(
            postgres_engine, PostgresOperatingCycleRepository(postgres_engine)
        ),
        followup_kalshi_source=_NoKalshiFollowUpSource(),
    ).execute(
        task=task,
        cycle_id="cycle-replay",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=7),
    )
    assert second.status.value in {"COMPLETED", "REUSED"}
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1
    assert first.status.value in {"COMPLETED", "REUSED"}

    # A later, genuinely new cycle for the identical (already-captured)
    # decision must also stay a pure no-op replay -- not a second row.
    third = executor.execute(
        task=_due_task("cycle-replay-2", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-replay-2",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(days=1),
    )
    assert "KALSHI_BASELINE_QUOTE_CAPTURED:0" in third.warnings
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_unsupported_source_record_type_never_produces_a_row(postgres_engine: Engine) -> None:
    """A KALSHI decision sourced from an evaluator with no persisted table
    (simulated here by a fixture projection claiming an unsupported
    ``source_record_type``) is honestly counted and never crashes the sweep
    -- absence of a row is the correct, permanent "unavailable" fact."""

    projection = SharedOpportunityProjection.create(
        projection_id="kbq-proj-unsupported",
        venue=SharedVenue.KALSHI,
        source_record_type="ExhaustiveEventSetEvaluation",
        source_record_id="exhaustive-eval-1",
        source_canonical_hash="a" * 64,
        strategy_or_method_id="KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1",
        instrument_id="KALSHI-EVT",
        product_or_market_id="KALSHI-EVT:COMPLETE_SET",
        direction="BUY_COMPLETE_SET",
        generated_at=NOW - timedelta(minutes=5),
        valid_until=NOW + timedelta(hours=1),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=300,
        freshness_limit_seconds=3600,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value=None,
        comparable_value_identity=None,
        uncertainty=None,
        maximum_loss=None,
        modeled_fees=None,
        modeled_slippage=None,
        costs_retained=True,
        liquidity_state="INSUFFICIENT",
        liquidity_limitations=("FIXTURE_LIQUIDITY_LIMITATION",),
        required_hypothetical_capital=None,
        maximum_sensible_research_allocation=None,
        source_maximum_allocation_fraction=None,
        expected_holding_days=None,
        estimated_lockup_days=None,
        supporting_evidence_ids=("kbq-evidence-unsupported",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id="kbq-set-unsupported", created_at=NOW, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(postgres_engine).save_opportunity_bundle(
        idempotency_key="kbq-set-unsupported-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-unsupported")
    result = _executor(postgres_engine).execute(
        task=_due_task("cycle-unsupported", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-unsupported",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}
    assert not any(
        warning.startswith("KALSHI_BASELINE_QUOTE_SYNC_FAILED") for warning in result.warnings
    )
    assert "KALSHI_BASELINE_QUOTE_UNSUPPORTED_SOURCE_TYPE:1" in result.warnings
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0


# ---------------------------------------------------------------------------
# COUNTERFACTUAL_OUTCOME_REFRESH: additive V2 pass, end to end
# ---------------------------------------------------------------------------


def test_outcome_refresh_v2_pass_qualifies_once_settlement_and_baseline_quote_both_exist(
    postgres_engine: Engine,
) -> None:
    """Genuine end-to-end proof: real binary-complement evaluation -> real
    baseline quote capture -> real settled follow-up observation -> V2
    aggregate reaches QUALIFIED, entirely through the real recurring-cycle
    executor, while V1's own aggregate for the identical cohort (same run,
    same call) stays VENUE_ECONOMIC_REDUCTION_UNAVAILABLE, unmodified."""

    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-qualify"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-qualify"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-v2-qualify")
    executor = _executor(postgres_engine)

    # Cycle 1: ledger sync + baseline-quote capture (no settlement yet).
    executor.execute(
        task=_due_task("cycle-v2-qualify", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-v2-qualify",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    decision = PostgresCounterfactualLedgerRepository(postgres_engine).all_decisions()[0]
    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    # Directly persist one genuine, ELIGIBLE, cleanly-settled Kalshi
    # follow-up observation for this exact decision (bypasses the network-
    # bound SyncFollowUpObservations machinery already proven exhaustively
    # in test_postgres_counterfactual_followup_recurring.py -- this file's
    # purpose is proving the *baseline-quote + outcome-v2* wiring).
    observation_id = "kbq-v2-qualify-obs"
    observation = DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        disposition=decision.disposition,
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=3,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=decision.evaluated_at,
        target_time=decision.evaluated_at + timedelta(days=3),
        observation_type=FollowUpObservationType.KALSHI_MARKET_STATUS_AND_PRICE,
        subject_instrument_id=str(evaluation_info["provider_market_ticker"]),
        subject_product_or_market_id=str(evaluation_info["provider_market_ticker"]),
        provenance=EvidenceClass.GENUINE_PUBLIC,
        baseline_evidence_class=None,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version=None,
        observed_at=decision.evaluated_at + timedelta(days=3, minutes=1),
        source_lineage=("kbq-v2-qualify-source",),
        data_limitations=(),
        reference_price_at_baseline=None,
        reference_price_at_target=None,
        subsequent_return=None,
        market_status_at_target="finalized",
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result="YES",
        settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=content_digest((observation_id, decision.decision_id, "v2-qualify-fixture")),
        authority="NONE",
    )
    followup.save_observation(observation=observation)
    followup.save_assessment(
        assessment=FollowUpEvidenceQualityAssessment(
            assessment_id=f"{observation_id}-assessment",
            observation_id=observation_id,
            observation_canonical_hash=observation.canonical_hash,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
            baseline_evidence_class=observation.baseline_evidence_class,
            target_evidence_class=observation.target_evidence_class,
            verdict=FollowUpEvidenceQualityVerdict.ELIGIBLE,
            reason_code="FIXTURE_ELIGIBLE",
            evidence_defect_type=FollowUpEvidenceDefectType.NONE,
            assessed_at=NOW,
            canonical_hash=content_digest((observation_id, "assessment-fixture")),
            authority="NONE",
        )
    )
    SyncCounterfactualClassification(followup.ledger, followup.ledger).execute(
        classified_at=NOW, classifier_version=CURRENT_CLASSIFIER_VERSION
    )

    result = executor.execute(
        task=_due_task("cycle-v2-qualify-2", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH),
        cycle_id="cycle-v2-qualify-2",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}
    assert not any(
        warning.startswith("COUNTERFACTUAL_OUTCOME_V2_REFRESH_FAILED")
        for warning in result.warnings
    )

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    v1_aggregates = outcome_repository.aggregates_for_run(
        "cycle-v2-qualify-2-counterfactual-outcome"
    )
    v2_aggregates = outcome_repository.aggregates_for_run(
        "cycle-v2-qualify-2-counterfactual-outcome-v2"
    )
    assert len(v1_aggregates) == 1
    assert len(v2_aggregates) == 1
    assert (
        v1_aggregates[0].qualification_state
        is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
    )
    # n=1 << COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES (30): an honest
    # INSUFFICIENT_SAMPLE, not a fabricated QUALIFIED result from one
    # observation -- proving the minimum-sample floor still applies
    # identically under V2.
    assert (
        v2_aggregates[0].qualification_state
        is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert v2_aggregates[0].eligible_observation_count == 1
    assert v2_aggregates[0].outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V2


# ---------------------------------------------------------------------------
# Kalshi Baseline Quote Capture & Economic Maturation v1: migration 0108
# shape/downgrade-guard proofs.
# ---------------------------------------------------------------------------


def test_migration_0108_adds_quote_kind_and_widens_policy_version(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'kalshi_baseline_quote'"
                )
            )
        }
        assert "quote_kind" in columns
        policy_definition = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_kalshi_baseline_quote_policy_version'"
            )
        )
    assert policy_definition is not None
    assert "KALSHI_BASELINE_QUOTE_POLICY_V1" in policy_definition
    assert "KALSHI_BASELINE_QUOTE_POLICY_V2" in policy_definition


def test_quote_kind_check_constraint_genuinely_rejects_an_unrecognized_value(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-bad-kind"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-bad-kind"
    )
    decision = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(decision)
    SyncCounterfactualLedger(decision, batched, batched).execute(evaluated_at=NOW)
    retained_decision = decision.all_decisions()[0]
    # An unrecognized ``quote_kind`` value unavoidably also disagrees with
    # every ``policy_version`` (no valid pairing exists for it), so this
    # insert genuinely violates *both* ``ck_kalshi_baseline_quote_quote_kind``
    # and ``ck_kalshi_baseline_quote_kind_matches_policy_version``
    # simultaneously -- which one Postgres reports first is an
    # implementation detail, not something this test needs to pin down. The
    # substring "quote_kind" appears in both constraint names, so either
    # failure mode is genuine proof the database rejects this row.
    with (
        pytest.raises(Exception, match="quote_kind"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO kalshi_baseline_quote "
                "(quote_id, decision_id, policy_version, venue, subject_source_record_type, "
                "decision_evaluated_at, captured_at, run_id, payload, canonical_hash, "
                "authority, quote_kind) "
                "VALUES (:quote_id, :decision_id, 'KALSHI_BASELINE_QUOTE_POLICY_V1', 'KALSHI', "
                "'BinaryComplementEvaluation', now(), now(), 'run-bad-kind', '{}', :hash, "
                "'NONE', 'NOT_A_REAL_KIND')"
            ),
            {
                "quote_id": "kbq-bad-kind",
                "decision_id": retained_decision.decision_id,
                "hash": "a" * 64,
            },
        )


def test_kind_must_match_policy_version_constraint_rejects_a_mismatched_pair(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-mismatched-pair"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-mismatched-pair"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)
    retained_decision = ledger.all_decisions()[0]
    with (
        pytest.raises(Exception, match="ck_kalshi_baseline_quote_kind_matches_policy_version"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO kalshi_baseline_quote "
                "(quote_id, decision_id, policy_version, venue, subject_source_record_type, "
                "decision_evaluated_at, captured_at, run_id, payload, canonical_hash, "
                "authority, quote_kind) "
                "VALUES (:quote_id, :decision_id, 'KALSHI_BASELINE_QUOTE_POLICY_V2', 'KALSHI', "
                "'BinaryComplementEvaluation', now(), now(), 'run-mismatch', '{}', :hash, "
                "'NONE', 'MATCHED_TRADE_EXECUTION')"
            ),
            {
                "quote_id": "kbq-mismatch",
                "decision_id": retained_decision.decision_id,
                "hash": "b" * 64,
            },
        )


def test_downgrade_0108_refuses_with_retained_market_quote_observation_evidence(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-downgrade-guard"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-downgrade-guard"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)
    retained_decision = ledger.all_decisions()[0]
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO kalshi_baseline_quote "
                "(quote_id, decision_id, policy_version, venue, subject_source_record_type, "
                "decision_evaluated_at, captured_at, run_id, payload, canonical_hash, "
                "authority, quote_kind) "
                "VALUES (:quote_id, :decision_id, 'KALSHI_BASELINE_QUOTE_POLICY_V2', 'KALSHI', "
                "'BinaryComplementEvaluation', now(), now(), 'run-guard', '{}', :hash, "
                "'NONE', 'MARKET_QUOTE_OBSERVATION')"
            ),
            {
                "quote_id": "kbq-v2-guard-row",
                "decision_id": retained_decision.decision_id,
                "hash": "c" * 64,
            },
        )
    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0108 with retained MARKET_QUOTE_OBSERVATION evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0107_kalshi_resolution_fact")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # NOT ROOT_HEAD_REVISION once later migrations stack on top of 0108
    # (0109 External Strategy Evaluation v1, 0110 Strategy Evaluation
    # History & Promotion Lineage v1): alembic runs each intermediate
    # revision's downgrade() as its own committed step, so a downgrade
    # command issued from a head above 0108 genuinely, successfully
    # downgrades every step down to 0108 before ever reaching 0108's own
    # downgrade() (the one that raises here) -- only THAT step's own DDL
    # rolls back. The resting point is therefore always 0108 itself, a
    # permanent fact about this migration's own guard, never "whatever
    # head happens to be current" -- proven empirically via a real
    # 0110-on-top run, not assumed (see the identical, sibling fix in
    # test_postgres_external_strategy_research.py for 0109's own guard).
    assert current_revision == "0108_kalshi_baseline_v2"
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_downgrade_0108_steps_down_cleanly_when_only_matched_trade_rows_retained(
    postgres_engine: Engine,
) -> None:
    """The guard's exception condition must be correctly permissive for
    every V1 row this table could ever have retained before this
    migration existed -- proved directly against a genuine, real, captured
    ``MATCHED_TRADE_EXECUTION`` row, not merely asserted."""

    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v1-downgrade-clean"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v1-downgrade-clean"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-v1-downgrade-clean")
    _executor(postgres_engine).execute(
        task=_due_task(
            "cycle-v1-downgrade-clean", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH
        ),
        cycle_id="cycle-v1-downgrade-clean",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0107_kalshi_resolution_fact")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0107_kalshi_resolution_fact"
    with postgres_engine.connect() as connection:
        exists = connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'kalshi_baseline_quote' AND column_name = 'quote_kind')"
            )
        )
    assert exists is False
    # Restore head for any later test sharing this session-scoped fixture.
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), ROOT_HEAD_REVISION)
        connection.commit()
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


# ---------------------------------------------------------------------------
# Kalshi Baseline Quote Capture & Economic Maturation v1: market-quote-
# observation (V2) capture, idempotency, fail-closed, and recurring-cycle
# wiring -- against real, non-CANDIDATE evaluations produced by the actual
# production scanner (never hand-forged).
# ---------------------------------------------------------------------------


def test_sync_captures_a_genuine_market_quote_observation_for_non_candidate_evaluation(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-capture", scenario="non_candidate"
    )
    assert evaluation_info["state"] == ComplementState.NO_CANDIDATE.value
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-capture"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)
    decision = ledger.all_decisions()[0]

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    result = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-capture-run", generated_at=NOW
    )
    assert result.considered_count == 1
    assert result.no_candidate_evaluation_count == 1
    assert result.matched_trade_captured_count == 0
    assert result.market_quote_captured_count == 1
    assert result.captured_count == 1
    assert result.insufficient_market_quote_evidence_count == 0
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    quote = source.quotes.quote_for_decision(decision.decision_id)
    assert quote is not None
    assert quote.policy_version == KALSHI_BASELINE_QUOTE_POLICY_V2
    assert quote.quote_kind == KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value
    assert quote.authority == "NONE"
    assert len(quote.legs) == 1
    leg = quote.legs[0]
    assert leg.orderbook_snapshot_id == evaluation_info["orderbook_snapshot_id"]
    # The fixture's "non_candidate" orderbook is symmetric (0.50/0.50 on
    # both sides) -- a genuine, honest, non-arbitrage-able two-sided quote.
    assert leg.yes_bid == Decimal("0.50")
    assert leg.no_bid == Decimal("0.50")

    # No longer pending -- the bounded, self-healing sweep is queried again.
    assert source.pending_kalshi_decisions(limit=10) == ()


def test_sync_captures_market_quote_when_evaluation_captured_after_coarse_decision_time(
    postgres_engine: Engine,
) -> None:
    """Kalshi Baseline Temporal Semantics & Sweep Isolation v1's core
    regression, reproduced against a real Postgres round-trip and the real,
    unmodified scan pipeline -- the production shape byte-for-byte: the
    referenced evaluation's own real capture instant (``DISCOVERY_CLOCK``,
    2026-07-20) falls AFTER the decision's own coarse ``evaluated_at``
    (deliberately seeded well before it here), exactly as a
    ``SHARED_OPPORTUNITY_REBUILD`` day-boundary ``as_of`` frozen before a
    dependent evaluation/capture task actually ran would produce. Before
    this milestone's fix, the domain constructor raised
    ``ValueValidationError`` for every decision shaped like this one (the
    production audit found 184 of 500) and the unguarded sweep loop turned
    that into zero captured rows; this must now succeed."""

    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-temporal-regression", scenario="non_candidate"
    )
    assert evaluation_info["state"] == ComplementState.NO_CANDIDATE.value
    coarse_decision_evaluated_at = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    # The regression's own precondition: the decision's coarse floor must
    # genuinely predate the evidence's real capture instant, or this test
    # would not exercise the bug at all.
    assert coarse_decision_evaluated_at < DISCOVERY_CLOCK
    _seed_kalshi_projection_and_eligibility(
        postgres_engine,
        evaluation_info=evaluation_info,
        identity="v2-temporal-regression",
        created_at=coarse_decision_evaluated_at,
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)
    decision = ledger.all_decisions()[0]
    assert decision.evaluated_at == coarse_decision_evaluated_at

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    # ``generated_at`` is the sweep's own real "now" -- genuinely after
    # both the coarse decision floor and the evaluation's real capture
    # instant, exactly like the real recurring cycle's own ``executed_at``.
    result = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-temporal-regression-run", generated_at=NOW
    )

    assert result.market_quote_captured_count == 1
    assert result.matched_trade_validation_rejected_count == 0
    assert result.market_quote_validation_rejected_count == 0
    assert result.captured_count == 1
    assert result.rejection_reason_counts == ()
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    quote = source.quotes.quote_for_decision(decision.decision_id)
    assert quote is not None
    assert quote.policy_version == KALSHI_BASELINE_QUOTE_POLICY_V2
    leg = quote.legs[0]
    # The exact relationship that used to be rejected, now proven accepted.
    assert leg.quote_observed_at > quote.decision_evaluated_at
    assert leg.quote_observed_at <= quote.captured_at


def test_sync_fails_closed_on_one_sided_orderbook_and_produces_no_row(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-one-sided", scenario="one_sided"
    )
    assert evaluation_info["state"] == ComplementState.INELIGIBLE.value
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-one-sided"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    result = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-one-sided-run", generated_at=NOW
    )
    assert result.considered_count == 1
    assert result.no_candidate_evaluation_count == 1
    assert result.market_quote_captured_count == 0
    assert result.insufficient_market_quote_evidence_count == 1
    assert result.captured_count == 0
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0
    # Permanently unavailable, honestly: the referenced evaluation is
    # immutable, so a later sweep re-derives the identical, still-negative
    # result -- never dropped, never retried differently.
    second = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-one-sided-run-2", generated_at=NOW + timedelta(hours=1)
    )
    assert second.considered_count == 1
    assert second.insufficient_market_quote_evidence_count == 1
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0


def test_sync_fails_closed_on_empty_orderbook_and_produces_no_row(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-empty", scenario="empty"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-empty"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    result = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-empty-run", generated_at=NOW
    )
    assert result.insufficient_market_quote_evidence_count == 1
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 0


def test_market_quote_observation_sync_is_idempotent_and_creates_no_duplicate_row(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-idempotent", scenario="non_candidate"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-idempotent"
    )
    ledger = PostgresCounterfactualLedgerRepository(postgres_engine)
    batched = BatchedLedgerEvidenceSource(ledger)
    SyncCounterfactualLedger(ledger, batched, batched).execute(evaluated_at=NOW)

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    first = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-idempotent-run-1", generated_at=NOW
    )
    assert first.market_quote_captured_count == 1
    second = SyncKalshiBaselineQuoteEvidence(source, source.quotes).execute(
        run_id="v2-idempotent-run-2", generated_at=NOW + timedelta(hours=3)
    )
    assert second.considered_count == 0
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    # Direct repository-level replay proof, mirroring the V1 proof above:
    # saving the identical quote object again (a genuine crash-resume
    # replay) is idempotent, never a duplicate or conflict.
    quote = source.quotes.all_quotes()[0]
    assert source.quotes.save_quote(quote=quote) is True
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_recurring_task_captures_market_quote_observation_automatically(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-recurring", scenario="non_candidate"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-recurring"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-v2-recurring")
    executor = _executor(postgres_engine)

    result = executor.execute(
        task=_due_task("cycle-v2-recurring", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-v2-recurring",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}
    assert result.network_requests == 0
    assert "KALSHI_BASELINE_QUOTE_CAPTURED:1" in result.warnings
    assert "KALSHI_BASELINE_QUOTE_NO_CANDIDATE_EVALUATION:1" in result.warnings
    assert not any(
        warning.startswith("KALSHI_BASELINE_QUOTE_SYNC_FAILED") for warning in result.warnings
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1
    row = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine).quotes.all_quotes()[0]
    assert row.quote_kind == KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value


def test_market_quote_observation_survives_worker_replay_after_restart(
    postgres_engine: Engine,
) -> None:
    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-replay", scenario="non_candidate"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-replay"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-v2-replay")
    task = _due_task("cycle-v2-replay", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH)
    _executor(postgres_engine).execute(
        task=task, cycle_id="cycle-v2-replay", plan=plan, as_of=NOW, executed_at=NOW
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1

    # Genuine crash-resume replay: a fresh executor instance (a new process
    # would construct one), a later wall-clock instant.
    second = ExistingCapabilityOperatingTaskExecutor(
        postgres_engine,
        PostgresOperatingStateSource(
            postgres_engine, PostgresOperatingCycleRepository(postgres_engine)
        ),
        followup_kalshi_source=_NoKalshiFollowUpSource(),
    ).execute(
        task=task,
        cycle_id="cycle-v2-replay",
        plan=plan,
        as_of=NOW,
        executed_at=NOW + timedelta(minutes=7),
    )
    assert second.status.value in {"COMPLETED", "REUSED"}
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1


def test_outcome_refresh_v2_never_consumes_a_market_quote_observation_row(
    postgres_engine: Engine,
) -> None:
    """The one conflation this whole milestone must never make, proved
    directly: a ``MARKET_QUOTE_OBSERVATION`` row -- even with a clean
    settled follow-up for the identical decision -- must never reach
    ``COUNTERFACTUAL_OUTCOME_POLICY_V2``'s reduction. No capital was ever
    modeled as deployed against a plain quote; there is no return to
    compute, and this proves the read-side filter
    (``PostgresKalshiBaselineQuoteRepository.matched_trade_quotes()``) keeps
    it that way end to end, through the real recurring-cycle executor."""

    evaluation_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-v2-no-maturation", scenario="non_candidate"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=evaluation_info, identity="v2-no-maturation"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-v2-no-maturation")
    executor = _executor(postgres_engine)

    executor.execute(
        task=_due_task("cycle-v2-no-maturation", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-v2-no-maturation",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 1
    quote = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine).quotes.all_quotes()[0]
    assert quote.quote_kind == KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value

    decision = PostgresCounterfactualLedgerRepository(postgres_engine).all_decisions()[0]
    followup = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observation_id = "kbq-v2-no-maturation-obs"
    observation = DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.KALSHI,
        disposition=decision.disposition,
        policy_version=FOLLOWUP_POLICY_V2,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=3,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=decision.evaluated_at,
        target_time=decision.evaluated_at + timedelta(days=3),
        observation_type=FollowUpObservationType.KALSHI_MARKET_STATUS_AND_PRICE,
        subject_instrument_id=str(evaluation_info["provider_market_ticker"]),
        subject_product_or_market_id=str(evaluation_info["provider_market_ticker"]),
        provenance=EvidenceClass.GENUINE_PUBLIC,
        baseline_evidence_class=None,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version=None,
        observed_at=decision.evaluated_at + timedelta(days=3, minutes=1),
        source_lineage=("kbq-v2-no-maturation-source",),
        data_limitations=(),
        reference_price_at_baseline=None,
        reference_price_at_target=None,
        subsequent_return=None,
        market_status_at_target="finalized",
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result="YES",
        settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=content_digest(
            (observation_id, decision.decision_id, "v2-no-maturation-fixture")
        ),
        authority="NONE",
    )
    followup.save_observation(observation=observation)
    followup.save_assessment(
        assessment=FollowUpEvidenceQualityAssessment(
            assessment_id=f"{observation_id}-assessment",
            observation_id=observation_id,
            observation_canonical_hash=observation.canonical_hash,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
            baseline_evidence_class=observation.baseline_evidence_class,
            target_evidence_class=observation.target_evidence_class,
            verdict=FollowUpEvidenceQualityVerdict.ELIGIBLE,
            reason_code="FIXTURE_ELIGIBLE",
            evidence_defect_type=FollowUpEvidenceDefectType.NONE,
            assessed_at=NOW,
            canonical_hash=content_digest((observation_id, "assessment-fixture")),
            authority="NONE",
        )
    )
    SyncCounterfactualClassification(followup.ledger, followup.ledger).execute(
        classified_at=NOW, classifier_version=CURRENT_CLASSIFIER_VERSION
    )

    result = executor.execute(
        task=_due_task(
            "cycle-v2-no-maturation-2", OperatingTaskType.COUNTERFACTUAL_OUTCOME_REFRESH
        ),
        cycle_id="cycle-v2-no-maturation-2",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert result.status.value in {"COMPLETED", "REUSED"}

    outcome_repository = PostgresCounterfactualOutcomeRepository(postgres_engine)
    v2_aggregates = outcome_repository.aggregates_for_run(
        "cycle-v2-no-maturation-2-counterfactual-outcome-v2"
    )
    assert len(v2_aggregates) == 1
    aggregate = v2_aggregates[0]
    # ``BinaryComplementEvaluation`` is a structurally-reducible source type
    # under V2 (``_decision_is_economically_reducible`` -- see
    # ``domain.counterfactual_outcome``) regardless of which *specific*
    # evaluation a decision references, so this cohort is never
    # ``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` (that state is reserved for
    # source types that can *never* reduce, e.g. ``ThresholdBundleEvaluation``
    # -- see the sibling method-isolation test below). The real, precise
    # proof that this ``MARKET_QUOTE_OBSERVATION`` row was never consumed is
    # sharper than the top-line qualification label: the observation is
    # genuinely evidence-eligible (``eligible_observation_count == 1``,
    # identical to the sibling V1/matched-trade test above) yet contributes
    # *zero* returns -- ``positive/negative/zero_outcome_count`` all stay 0
    # -- because ``baseline_quote_by_decision_id`` (sourced from
    # ``matched_trade_quotes()``, never raw ``all_quotes()``) never contains
    # this decision's id at all. The sibling test's identical fixture shape,
    # differing only in baseline kind, produces exactly one non-zero
    # outcome count instead -- proving the distinction is this row's kind,
    # not some other difference between the two fixtures.
    assert (
        aggregate.qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
    )
    assert aggregate.eligible_observation_count == 1
    assert (
        aggregate.positive_outcome_count
        + aggregate.negative_outcome_count
        + aggregate.zero_outcome_count
        == 0
    )


# ---------------------------------------------------------------------------
# Kalshi Baseline Quote Capture & Economic Maturation v1: operator
# observability (census).
# ---------------------------------------------------------------------------


def test_census_reports_quote_kind_breakdown_and_latest_capture_time(
    postgres_engine: Engine,
) -> None:
    matched_trade_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-census-matched"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=matched_trade_info, identity="census-matched"
    )
    market_quote_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-census-quote", scenario="non_candidate"
    )
    _seed_kalshi_projection_and_eligibility(
        postgres_engine, evaluation_info=market_quote_info, identity="census-quote"
    )
    plan = _register_plan(postgres_engine, plan_id="kbq-plan-census")
    _executor(postgres_engine).execute(
        task=_due_task("cycle-census", OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH),
        cycle_id="cycle-census",
        plan=plan,
        as_of=NOW,
        executed_at=NOW,
    )
    assert _row_count(postgres_engine, "kalshi_baseline_quote") == 2

    census = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine).quotes.census()
    assert census["total_quote_rows"] == 2
    assert census["matched_trade_execution_rows"] == 1
    assert census["market_quote_observation_rows"] == 1
    assert census["by_quote_kind"] == {
        KalshiQuoteKind.MATCHED_TRADE_EXECUTION.value: 1,
        KalshiQuoteKind.MARKET_QUOTE_OBSERVATION.value: 1,
    }
    assert census["latest_captured_at"] is not None


def test_orderbook_top_of_book_distinguishes_two_sided_one_sided_and_missing(
    postgres_engine: Engine,
) -> None:
    two_sided_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-tob-two-sided", scenario="non_candidate"
    )
    one_sided_info = _persist_real_binary_complement_evaluation(
        postgres_engine, run_key="kbq-tob-one-sided", scenario="one_sided"
    )
    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    result = source.orderbook_top_of_book(
        (
            str(two_sided_info["orderbook_snapshot_id"]),
            str(one_sided_info["orderbook_snapshot_id"]),
            "a-snapshot-id-that-was-never-captured",
        )
    )
    two_sided = result[str(two_sided_info["orderbook_snapshot_id"])]
    assert two_sided.is_two_sided is True
    assert two_sided.yes_bid == Decimal("0.50")
    assert two_sided.no_bid == Decimal("0.50")

    one_sided = result[str(one_sided_info["orderbook_snapshot_id"])]
    assert one_sided.is_two_sided is False
    assert one_sided.yes_bid == Decimal("0.53")
    assert one_sided.no_bid is None

    assert "a-snapshot-id-that-was-never-captured" not in result
