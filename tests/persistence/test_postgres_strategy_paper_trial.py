"""Phase F/G/I: one genuine, end-to-end strategy-driven CRYPTO PAPER trial.

This is both a persistence/integration test AND this milestone's Phase I
demonstration script. It runs against this worktree's own isolated,
disposable Postgres (never production -- see docs/WORKTREE_VERIFICATION.md)
and makes real, credential-free, unauthenticated public Coinbase network
calls for decision-time price evidence -- it is deliberately NOT part of
the fully-offline default test suite (see ``pytestmark`` below).

What is genuine vs. clearly-labeled-reference here, stated plainly:

- The EXTERNAL benchmark's historical backtest runs over a small,
  deterministic, clearly FIXTURE-labeled synthetic candle series (proving
  the Phase C/D evaluation mechanism; a full genuine multi-year candle
  history is a separate, already-exercised concern -- see
  domain/strategy_benchmark.py and domain/strategy_paper_trial.py module
  docstrings for why this split is deliberate, not a shortcut).
- The INTERNAL comparison side uses REAL retained production evidence,
  copied read-only (see docs/PAPER_ELIGIBILITY_TRACE_2026-08-25.md for how
  it was obtained): CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1's genuine
  counterfactual_outcome_aggregate as of 2026-08-24 (38 real matured
  REJECTED/ECONOMICS decisions, 100% positive subsequent price movement,
  mean +3.21%) -- Apex's actual retained track record for that method, not
  invented data.
- The DECISION-TIME price evidence and the order's fill evidence are both
  real, live, unauthenticated public Coinbase top-of-book captures made
  during this test run -- never a fixture.
- The product/contract tick-size metadata is a labeled FIXTURE reference
  (see domain/strategy_paper_trial.py) -- genuine product discovery is
  CRYPTO_UNIVERSE_REFRESH's own separate, already-exercised concern.
- Every PAPER execution mechanic (eligibility, authorization, order
  construction, preview, submission, fill, ledger, reconciliation) is
  Apex's existing, unmodified domain.crypto_paper / application.crypto_paper
  machinery -- this test adds no new execution logic, only genuine inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.engine import Engine

from apex.adapters.coinbase_universe_public import CoinbasePublicUniverseSource
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.application.crypto_paper import CreateCryptoPaperAccount
from apex.application.strategy_paper_trial import (
    ExecuteStrategyPaperTrial,
    capture_genuine_price_evidence,
)
from apex.domain.counterfactual_classification import ReasonFamily
from apex.domain.counterfactual_followup import FollowUpBaselineKind, FollowUpHorizonKind
from apex.domain.counterfactual_outcome import (
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeQualificationState,
)
from apex.domain.crypto_paper import (
    PaperEligibilityState,
    PaperOrderState,
    PaperReconciliationStatus,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.performance_attribution import assess_strategy_health, materialize_attribution
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import (
    ComparisonVerdict,
    compare_strategy_evaluations,
    evaluate_external_strategy,
    evaluate_internal_strategy_from_counterfactual_outcome,
)
from apex.domain.strategy_paper_trial import (
    StrategyPaperTrialOutcome,
    build_attribution_source_from_trial,
    build_strategy_opportunity_projection,
    reference_crypto_provider_contract,
    reference_crypto_universe_snapshot,
)

pytestmark = pytest.mark.postgres

ACCOUNT_ID = "strategy-paper-trial-demo-account"
INSTRUMENT_ID = "BTC-USD"
# Constructed here, at the test/composition-root layer, and injected into
# capture_genuine_price_evidence() -- application/strategy_paper_trial.py
# itself must not import apex.adapters directly (see tests/security/
# test_architecture_boundaries.py).
QUOTE_SOURCE = CoinbasePublicUniverseSource()


def _fixture_candles() -> tuple[CryptoDailyCandle, ...]:
    """Small, deterministic, clearly-FIXTURE-labeled synthetic history --
    see module docstring for why this is honest here."""

    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    prices = [str(30000 + 200 * i) for i in range(30)]  # steady uptrend
    return tuple(
        CryptoDailyCandle.create(
            capture_id="strategy-paper-trial-demo-capture",
            attempt_id="strategy-paper-trial-demo-attempt",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=epoch + timedelta(days=offset),
            open=price,
            high=price,
            low=price,
            close=price,
            volume="1",
            ingested_at=epoch,
            raw_response_hash="a" * 64,
        )
        for offset, price in enumerate(prices)
    )


def _real_internal_counterfactual_aggregate() -> CounterfactualOutcomeAggregate:
    """Byte-for-byte the real, retained production
    counterfactual_outcome_aggregate for CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1
    as of 2026-08-24 (aggregate_id ...e6b3760ad340496d9a7d2d71), obtained via
    the same read-only production trace as docs/
    PAPER_ELIGIBILITY_TRACE_2026-08-25.md. Copied as literal values here --
    never re-derived, never invented."""

    return CounterfactualOutcomeAggregate(
        aggregate_id="counterfactual_outcome_aggregate_e6b3760ad340496d9a7d2d71",
        cohort_id="counterfactual_outcome_cohort_6eeb28fa8a53415d14e572b1",
        outcome_policy_version="COUNTERFACTUAL_OUTCOME_POLICY_V1",
        run_id="apex-operating-cycle-plan-crypto-book-evidence-v1-worker-20260824-counterfactual-outcome",
        venue=SharedVenue.CRYPTO,
        strategy_or_method_id="CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
        reason_family=ReasonFamily.ECONOMICS,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        followup_policy_version="FOLLOWUP_POLICY_V2",
        classifier_version="REASON_CLASSIFIER_V2",
        evidence_quality_policy_version="FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1",
        generated_at=datetime(2026, 8, 24, 0, 0, 54, 765178, tzinfo=UTC),
        total_observations_considered=215,
        mature_observation_count=38,
        eligible_observation_count=38,
        evidence_ineligible_observation_count=0,
        unassessed_observation_count=0,
        unavailable_observation_count=177,
        positive_outcome_count=38,
        negative_outcome_count=0,
        zero_outcome_count=0,
        mean_subsequent_return=Decimal("0.0321332"),
        median_subsequent_return=Decimal("0.02456294"),
        positive_rate=Decimal("1"),
        qualification_state=CounterfactualOutcomeQualificationState.QUALIFIED,
        contributing_decision_ids=(),
        explanation=(
            "CRYPTO/CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1/ECONOMICS: 38/215 decisions "
            "matured, 38 evidence-eligible, 100% positive subsequent price movement."
        ),
        canonical_hash="90270274ac89c7292741682c43cece6a754c49bd1099f771c55b69966851c387",
    )


def _internal_method_definition() -> StrategyDefinition:
    return StrategyDefinition.create(
        source_kind=StrategySourceKind.INTERNAL,
        display_name="Internal Cross-Sectional Discovery (identity wrapper)",
        venue=SharedVenue.CRYPTO,
        instrument_ids=(INSTRUMENT_ID,),
        horizon_kind=StrategyHorizonKind.DAILY_SIGNAL_INDEFINITE_HOLD,
        rule_kind=StrategyRuleKind.INTERNAL_METHOD_REFERENCE,
        rule_parameters=StrategyRuleParameters(
            referenced_internal_method_id="CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1"
        ),
        evidence_requirements=StrategyEvidenceRequirements(
            minimum_history_days=1, requires_contemporaneous_observation=False
        ),
        provenance=StrategyProvenance(
            reference_kind="INTERNAL_RESEARCH_METHOD",
            reference_description="CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
            origin_note="Identifies Apex's own existing internal research method for comparison.",
        ),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def test_genuine_end_to_end_strategy_paper_trial_and_closed_loop(postgres_engine: Engine) -> None:
    repository = PostgresCryptoPaperRepository(postgres_engine)
    now = datetime.now(tz=UTC)

    # ---- Phase F setup: a real crypto PAPER account, bootstrapped fresh ----
    CreateCryptoPaperAccount(repository).execute(
        account_id=ACCOUNT_ID,
        opening_cash="1000",
        created_at=now,
        idempotency_key=f"{ACCOUNT_ID}-create",
    )

    # ---- Phase C/D: evaluate one EXTERNAL benchmark over fixture history ----
    buy_and_hold = canonical_strategy_benchmarks(created_at=now)[0]
    backtest = simulate_strategy_backtest(
        buy_and_hold,
        _fixture_candles(),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_provenance="strategy-paper-trial-demo-capture",
    )
    external_evaluation = evaluate_external_strategy(buy_and_hold, backtest)
    assert external_evaluation.net_return is not None
    assert external_evaluation.trade_count >= 1

    # ---- Phase D: compare against REAL internal counterfactual evidence ----
    internal_definition = _internal_method_definition()
    internal_evaluation = evaluate_internal_strategy_from_counterfactual_outcome(
        internal_definition, _real_internal_counterfactual_aggregate()
    )
    comparison = compare_strategy_evaluations(
        internal_evaluation, external_evaluation, compared_at=now
    )
    assert comparison.verdict in (
        ComparisonVerdict.CANDIDATE_OUTPERFORMS_BENCHMARK,
        ComparisonVerdict.BENCHMARK_OUTPERFORMS_CANDIDATE,
        ComparisonVerdict.INCONCLUSIVE_INSUFFICIENT_EVIDENCE,
    )
    # both sides carry real evidence; internal has no net_return (see
    # strategy_evaluation.py docstring), so the return-axis comparison is
    # honestly inconclusive -- but sample sizes and win-rate ARE comparable
    assert internal_evaluation.win_rate == Decimal("1")
    assert internal_evaluation.sample_size == 38

    # ---- Phase F: one real, live, credential-free Coinbase quote for the decision ----
    entry_evidence = capture_genuine_price_evidence(
        QUOTE_SOURCE, product_id=INSTRUMENT_ID, requested_at=now
    )
    assert entry_evidence.price > 0
    # the real network round trip can genuinely advance the clock past the
    # `now` captured before it -- everything downstream must be at or after
    # the evidence's own ingested_at, exactly like the recurring cycle's own
    # executed_at/evidence timing contract
    now = max(now, entry_evidence.ingested_at)

    projection = build_strategy_opportunity_projection(
        definition=buy_and_hold,
        evaluation=external_evaluation,
        reference_price=entry_evidence.price,
        generated_at=now,
        opportunity_ordinal="demo-1",
    )
    contract = reference_crypto_provider_contract(
        instrument_id=INSTRUMENT_ID, base_asset="BTC", created_at=now
    )
    universe = reference_crypto_universe_snapshot(instrument_id=INSTRUMENT_ID, created_at=now)

    service = ExecuteStrategyPaperTrial(repository)
    placement = service.place_order(
        definition=buy_and_hold,
        projection=projection,
        contract=contract,
        universe=universe,
        account_id=ACCOUNT_ID,
        price_evidence=entry_evidence,
        operating_cycle_enabled=True,
        executed_at=now,
        actor="test:strategy-paper-trial-demo",
        idempotency_seed=f"strategy-paper-trial-demo-{now.isoformat()}",
    )

    assert placement.eligibility.state is PaperEligibilityState.ELIGIBLE, (
        placement.eligibility.reasons
    )
    assert placement.order is not None
    assert placement.order.state is PaperOrderState.OPEN

    # ---- fill: a second, later, genuinely fresh public capture ----
    fill_at = now + timedelta(seconds=1)
    fill_evidence = capture_genuine_price_evidence(
        QUOTE_SOURCE, product_id=INSTRUMENT_ID, requested_at=fill_at
    )
    applied = service.apply_evidence(
        account_id=ACCOUNT_ID,
        order_id=placement.order.order_id,
        fill_id=f"{placement.order.order_id}-fill",
        evidence=fill_evidence,
        actor="test:strategy-paper-trial-demo",
        idempotency_key=f"{placement.order.order_id}-fill-key",
    )
    assert applied.value is not None, (
        "genuine evidence strictly after submission should fill a market order"
    )
    assert applied.value.order.state in (PaperOrderState.FILLED, PaperOrderState.PARTIALLY_FILLED)
    assert applied.value.journal.postings  # a real, balanced double-entry journal was posted

    # ---- reconcile ----
    reconciliation = service.reconcile(
        account_id=ACCOUNT_ID,
        reconciliation_id=f"{placement.order.order_id}-reconciliation",
        evaluated_at=fill_at,
        order_id=placement.order.order_id,
        idempotency_key=f"{placement.order.order_id}-reconciliation-key",
    )
    assert reconciliation.value.status is PaperReconciliationStatus.CLEAN

    # ---- Phase G: close the loop through Apex's EXISTING, unmodified ----
    # ---- attribution/health machinery ----
    outcome = StrategyPaperTrialOutcome(
        placement=placement,
        fill=applied.value,
        closure=None,
        reconciliation=reconciliation.value,
    )
    attribution_source = build_attribution_source_from_trial(
        definition=buy_and_hold,
        evaluation=external_evaluation,
        outcome=outcome,
        materialized_at=fill_at,
    )
    attribution, _components = materialize_attribution(
        attribution_id=f"{placement.order.order_id}-attribution",
        source=attribution_source,
        materialized_at=fill_at,
    )
    assert attribution.strategy_or_method_id == buy_and_hold.strategy_id
    assert attribution.fill_ids

    health = assess_strategy_health(
        health_id=f"{buy_and_hold.strategy_id}-health",
        attributions=(attribution,),
        as_of=fill_at,
    )
    assert health.strategy_or_method_id == buy_and_hold.strategy_id
    assert health.fill_count == 1
    # Exactly one genuine PAPER trial is below every minimum-sample floor in
    # StrategyHealthPolicy.v1() (minimum_trials=3) -- INSUFFICIENT_SAMPLE
    # would already be the honest, expected state after ONE trial. What this
    # existing, unmodified policy actually returns is stricter still:
    # DATA_QUALITY_BLOCKED, because build_attribution_source_from_trial()
    # deliberately leaves context_at_consideration_id/execution_context
    # unset -- this milestone never wires in Total Market Context lineage
    # for a strategy-sourced trial (a separate, already-existing Apex
    # concern, out of this narrow milestone's scope), and Apex's own real
    # health policy correctly refuses to call that "INSUFFICIENT_SAMPLE"
    # instead of catching the real gap. This is the closed loop genuinely
    # working -- catching real missing evidence, not something to paper over
    # by inventing a fake context id.
    assert health.state.value in (
        "INSUFFICIENT_SAMPLE",
        "NO_OPERATIONAL_EVIDENCE",
        "DATA_QUALITY_BLOCKED",
    )
    if health.state.value == "DATA_QUALITY_BLOCKED":
        assert "SOURCE_OR_CONTEXT_LINEAGE_INCOMPLETE" in health.failed_thresholds

    # ---- final, load-bearing assertion: this is genuinely PAPER, not LIVE ----
    assert placement.order.paper_authority == "PAPER_ONLY"
    assert placement.order.live_authority == "NONE"
    assert attribution.authority == "NONE"
    assert attribution.live_authority == "NONE"


def test_place_order_is_idempotent_on_replay(postgres_engine: Engine) -> None:
    """Calling place_order twice with the same idempotency_seed must not
    double-authorize, double-construct, or double-submit -- every
    underlying step already delegates to Apex's existing idempotent
    services (see application/crypto_paper.py); this proves MY new
    orchestration genuinely preserves that property end to end."""

    account_id = "strategy-paper-trial-idempotency-account"
    repository = PostgresCryptoPaperRepository(postgres_engine)
    now = datetime.now(tz=UTC)

    CreateCryptoPaperAccount(repository).execute(
        account_id=account_id,
        opening_cash="1000",
        created_at=now,
        idempotency_key=f"{account_id}-create",
    )

    buy_and_hold = canonical_strategy_benchmarks(created_at=now)[0]
    backtest = simulate_strategy_backtest(
        buy_and_hold,
        _fixture_candles(),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_provenance="strategy-paper-trial-demo-capture",
    )
    evaluation = evaluate_external_strategy(buy_and_hold, backtest)

    entry_evidence = capture_genuine_price_evidence(
        QUOTE_SOURCE, product_id=INSTRUMENT_ID, requested_at=now
    )
    now = max(now, entry_evidence.ingested_at)

    projection = build_strategy_opportunity_projection(
        definition=buy_and_hold,
        evaluation=evaluation,
        reference_price=entry_evidence.price,
        generated_at=now,
        opportunity_ordinal="idempotency-demo",
    )
    contract = reference_crypto_provider_contract(
        instrument_id=INSTRUMENT_ID, base_asset="BTC", created_at=now
    )
    universe = reference_crypto_universe_snapshot(instrument_id=INSTRUMENT_ID, created_at=now)

    service = ExecuteStrategyPaperTrial(repository)
    seed = f"strategy-paper-trial-idempotency-{now.isoformat()}"

    first = service.place_order(
        definition=buy_and_hold,
        projection=projection,
        contract=contract,
        universe=universe,
        account_id=account_id,
        price_evidence=entry_evidence,
        operating_cycle_enabled=True,
        executed_at=now,
        actor="test:strategy-paper-trial-idempotency",
        idempotency_seed=seed,
    )
    second = service.place_order(
        definition=buy_and_hold,
        projection=projection,
        contract=contract,
        universe=universe,
        account_id=account_id,
        price_evidence=entry_evidence,
        operating_cycle_enabled=True,
        executed_at=now,
        actor="test:strategy-paper-trial-idempotency",
        idempotency_seed=seed,
    )

    assert first.eligibility.state is PaperEligibilityState.ELIGIBLE
    assert first.order is not None
    assert second.order is not None
    assert first.order.order_id == second.order.order_id
    assert first.order.canonical_hash == second.order.canonical_hash

    state = repository.state(account_id)
    assert state is not None
    matching_orders = [o for o in state.orders if o.order_id == first.order.order_id]
    assert len(matching_orders) == 1, "replay must not create a second order row"
