"""PostgreSQL persistence proofs for Counterfactual Follow-Up Recurring
Integration v1: the ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` operating-cycle task.

Every test here drives ``ExistingCapabilityOperatingTaskExecutor.execute``
directly for exactly this one task type against a real, freshly migrated
``apex_test`` database -- the same scoped-executor pattern the existing
``PORTFOLIO_SNAPSHOT_REFRESH``/``MANDATE_EVALUATION_REBUILD`` persistence
tests already use (see ``test_postgres_operating_cycle.py``). Kalshi is
always a local fake (``_FakeRecurringKalshiSource``/
``_FailingRecurringKalshiSource``, injected via the executor's
``followup_kalshi_source`` seam) -- zero genuine network requests anywhere
in this file, matching every other Kalshi-adjacent test in this suite.

Scheduling-level proofs (unconditionally DUE, ``dependency_types=()``, one
follow-up-task failure never blocking or being blocked by any other task,
including options) live in ``tests/test_operating_cycle.py`` -- pure
domain/application logic that does not need a real database. This file
proves the executor branch itself is correct against genuine retained
evidence: it reuses ``application.counterfactual_followup.
SyncFollowUpObservations`` completely unmodified, enforces its own bounded
per-cycle Kalshi request budget, carries a due backlog forward
deterministically, and replays idempotently on restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.kalshi_resolution_postgres import PostgresKalshiResolutionFactRepository
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import (
    COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED,
    COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE,
    COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION,
    ExistingCapabilityOperatingTaskExecutor,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_followup import (
    FollowUpSourceUnavailable,
    KalshiMarketRead,
    PlanFollowUpObservations,
)
from apex.application.counterfactual_ledger import SyncCounterfactualLedgerFromSharedOpportunity
from apex.application.operating_cycle import RegisterOperatingCyclePlan
from apex.domain.counterfactual_followup import (
    CRYPTO_DAILY_CLOSE_LIMITATION,
    CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V1,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpDueState,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
    build_crypto_price_movement_observation,
    current_followup_policy_version,
    followup_observation_canonical_hash,
    followup_observation_id,
    is_evidence_eligible_for_downstream_attribution,
)
from apex.domain.crypto_universe import CryptoDailyObservation, CryptoProductObservation
from apex.domain.kalshi_resolution import KalshiResolutionState
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskType,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
DECISION_DAY = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)


def _observation_count(engine: Engine, policy_version: str) -> int:
    """Retained observations for exactly one lane -- the quantity the
    per-lane daily ceiling governs."""

    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM decision_followup_observation "
                    "WHERE policy_version = :policy_version"
                ),
                {"policy_version": policy_version},
            ).scalar_one()
        )


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _projection(
    identity: str, *, venue: SharedVenue = SharedVenue.CRYPTO, **overrides: object
) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": f"cfr-proj-{identity}",
        "venue": venue,
        "source_record_type": "FixtureOpportunity",
        "source_record_id": f"cfr-source-{identity}",
        "source_canonical_hash": content_digest((identity,)),
        "strategy_or_method_id": "CFR_FIXTURE_V1",
        "instrument_id": "BTC" if venue is SharedVenue.CRYPTO else f"KALSHI-{identity}",
        "product_or_market_id": (
            "BTC-USD" if venue is SharedVenue.CRYPTO else f"KALSHI-{identity}-MKT"
        ),
        "direction": "HYPOTHETICAL_LONG_SPOT_RESEARCH",
        "generated_at": DECISION_DAY + timedelta(hours=12),
        "valid_until": DECISION_DAY + timedelta(days=60),
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
        "expected_holding_days": 30,
        "estimated_lockup_days": 30,
        "supporting_evidence_ids": (f"cfr-evidence-{identity}",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _seed_decision(
    engine: Engine, *, identity: str, venue: SharedVenue = SharedVenue.CRYPTO, **overrides: object
) -> str:
    """Seed one real, retained ``CounterfactualDecision`` via a genuine
    SharedOpportunityProjection/eligibility-set sync (the production path)
    and return its ``decision_id``. Resolves the id by ``subject_projection_
    id`` rather than trusting ``LedgerSyncResult.decision_ids[0]``, because
    that sync scans *every* retained opportunity set -- correct regardless
    of how many times this helper has already been called in the same
    test."""

    projection = _projection(identity, venue=venue, **overrides)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"cfr-set-{identity}", created_at=DECISION_DAY, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"cfr-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    ledger = PostgresCounterfactualLedgerRepository(engine)
    SyncCounterfactualLedgerFromSharedOpportunity(ledger, ledger.shared_opportunity).execute(
        evaluated_at=DECISION_DAY
    )
    matches = tuple(
        item
        for item in ledger.all_decisions()
        if item.subject_projection_id == projection.projection_id
    )
    assert len(matches) == 1
    return matches[0].decision_id


def _crypto_observation(
    *,
    observation_id: str,
    product_id: str,
    candles: tuple[CryptoDailyObservation, ...],
    completed_at: datetime,
    evidence_class: EvidenceClass = EvidenceClass.HISTORICAL_PUBLIC,
) -> CryptoProductObservation:
    return CryptoProductObservation.create(
        observation_id=observation_id,
        source_capture_id=f"{observation_id}-capture",
        product_id=product_id,
        interval_start=DECISION_DAY - timedelta(days=1),
        interval_end=DECISION_DAY + timedelta(days=61),
        evidence_class=evidence_class,
        candles=candles,
        missing_buckets=0,
        duplicate_records=0,
        rejected_records=0,
        request_count=1,
        raw_evidence_hashes=(content_digest((observation_id,)),),
        source_contract_version="TEST_CONTRACT_V1",
        parser_version="TEST_PARSER_V1",
        checkpoint_ordinal=1,
        completed_at=completed_at,
        canonical_asset_id=product_id.split("-")[0],
    )


def _seed_btc_bars(engine: Engine) -> None:
    """BTC-USD daily bars covering ``DECISION_DAY - 1`` through
    ``DECISION_DAY + 1`` -- enough for every CRYPTO fixture decision in this
    file, since they all share ``product_or_market_id="BTC-USD"`` and
    ``expected_holding_days=1`` (so SHORT_HORIZON and STRATEGY_HORIZON share
    one target bucket).

    ``DECISION_DAY - 1`` (close 95) exists specifically for Counterfactual
    Crypto Follow-Up Point-in-Time Semantics v2: every fixture decision's
    ``SharedOpportunityProjection.generated_at`` defaults to
    ``DECISION_DAY + 12h`` (see ``_projection`` above), so
    ``FOLLOWUP_POLICY_V2``'s own baseline query
    (``latest_close_before(before=completed_utc_day(projection.generated_at))``
    == "the latest retained close strictly before ``DECISION_DAY``") resolves
    to this bucket, not the ``DECISION_DAY`` bucket itself (close 100) --
    exactly the corrected, non-hindsight-leaking selection this milestone
    exists to prove even in the recurring-task path, not only in the
    dedicated point-in-time test files.  Its distinct price (95, vs.
    ``DECISION_DAY``'s 100) is deliberate: a test that silently regressed
    back to V1's semantics here would still create an observation (so
    ``created_count`` alone would not catch it) -- only asserting the actual
    stamped ``reference_price_at_baseline``/``policy_version`` does.  Its own
    ``completed_at`` is stamped shortly after *its own* day, genuinely
    available well before ``DECISION_DAY`` (Part: availability-as-of
    correctness -- a batch completed only after the boundary it is meant to
    answer for must never be selected, no matter how historical its
    ``bucket_start`` looks).

    ``DECISION_DAY`` (close 100) doubles as the corrected V2 *target* bucket
    too (Part: target-bucket-from-baseline-candle): with ``horizon_days ==
    1`` for both horizon kinds here, the target bucket is the baseline
    candle's own bucket_start (``DECISION_DAY - 1``) plus 1 day ==
    ``DECISION_DAY`` -- not ``DECISION_DAY + 1`` (a naive completed_utc_day
    (target_time) re-derivation would have picked that instead, silently
    comparing closes 2 calendar days apart rather than 1).  ``DECISION_DAY +
    1`` (close 105) is retained purely as a decoy proving that: a
    regression back to the naive re-derivation would still create an
    observation, but its ``reference_price_at_target`` would be 105, not
    100.

    Counterfactual Forward Outcome Temporal Integrity v1: these same three
    bars also exactly cover ``FOLLOWUP_POLICY_V3``'s two economic windows
    for this fixture, which is why no bar had to be added here.  Every
    fixture decision has ``evaluated_at == DECISION_DAY`` (already a
    daily-close boundary) and ``generated_at == DECISION_DAY + 12h``
    (intraday), so V3's ``W0 = first daily-close boundary at or after the
    anchor`` resolves to ``DECISION_DAY`` for ``DECISION_TIME`` and to
    ``DECISION_DAY + 1`` for ``ORIGIN_EVIDENCE`` -- two genuinely different
    windows, which is the whole point of retaining both baseline kinds:

      * ``DECISION_TIME``  : 95 (``DECISION_DAY - 1``) -> 100 (``DECISION_DAY``)
      * ``ORIGIN_EVIDENCE``: 100 (``DECISION_DAY``)    -> 105 (``DECISION_DAY + 1``)

    Note what changed and what did not.  Under ``FOLLOWUP_POLICY_V2`` *both*
    baseline kinds stamped ``reference_price_at_baseline = 95``, because V2
    floored each anchor to the start of its own UTC day and then walked
    back -- so the ``ORIGIN_EVIDENCE`` row's supposedly *forward* window
    actually began 12 hours *before* its own anchor.  Under V3 that row
    measures from ``DECISION_DAY + 1``, strictly after its anchor, which is
    exactly the pre-anchor-movement correction this milestone exists for.
    The decision-input reference (retained as disclosure only, never an
    endpoint of the return) is 95 for both kinds -- the latest close Apex
    could genuinely see at either anchor."""

    crypto_universe = PostgresCryptoUniverseRepository(engine)
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cfr-bar-prior-completed-day",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="95", volume="1"
                ),
            ),
            completed_at=DECISION_DAY - timedelta(hours=23),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cfr-bar-decision-day",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="100", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cfr-bar-target-day",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY + timedelta(days=1), close="105", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(days=1, hours=1),
        )
    )


def _due_task(cycle_id: str) -> DueTask:
    return DueTask.create(
        task_type=OperatingTaskType.COUNTERFACTUAL_FOLLOWUP_REFRESH,
        disposition=DueDisposition.DUE,
        reasons=("fixture-due",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="LOCAL_RETAINED_CRYPTO_READS_PLUS_BOUNDED_KALSHI_GETS_WHEN_DUE",
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


def _executor(engine: Engine, *, kalshi_source: object) -> ExistingCapabilityOperatingTaskExecutor:
    state_source = PostgresOperatingStateSource(engine, PostgresOperatingCycleRepository(engine))
    return ExistingCapabilityOperatingTaskExecutor(
        engine,
        state_source,
        followup_kalshi_source=kalshi_source,  # type: ignore[arg-type]
    )


class _FakeRecurringKalshiSource:
    """Deterministic, zero-network ``KalshiMarketSource``: every call reports
    a genuine-looking live (unsettled) market read at a fixed request cost.
    Implements the Protocol structurally -- never touches a real socket."""

    def __init__(self, *, request_cost: int = 1) -> None:
        self.call_count = 0
        self.request_cost = request_cost

    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        self.call_count += 1
        return KalshiMarketRead(
            market_status="ACTIVE",
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.45"),
            settlement_result=None,
            settlement_source=None,
            source_endpoint_identity="fake-kalshi-followup-recurring",
            request_count=self.request_cost,
        )


class _FailingRecurringKalshiSource:
    """Every genuine attempt fails after consuming exactly one request --
    proves failure accounting stays accurate without ever fabricating a
    result."""

    def __init__(self) -> None:
        self.call_count = 0

    def read_market_status(self, **_: object) -> KalshiMarketRead:
        self.call_count += 1
        raise FollowUpSourceUnavailable("FIXTURE_KALSHI_UNAVAILABLE", request_count=1)


# --------------------------------------------------------------------------
# Due -> genuine execution; crypto is zero-provider-request; not-yet-due
# stays cheap.
# --------------------------------------------------------------------------


def test_followup_refresh_task_becomes_due_and_crypto_completes_with_zero_provider_requests(
    postgres_engine: Engine,
) -> None:
    # Structural guard: "current" must genuinely mean the corrected version,
    # not have silently regressed to equal the permanently-frozen V1
    # constant.
    assert current_followup_policy_version(SharedVenue.CRYPTO) != FOLLOWUP_POLICY_V1
    _seed_btc_bars(postgres_engine)
    decision_id = _seed_decision(postgres_engine, identity="crypto-due", expected_holding_days=1)
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-due-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)  # both horizons due (holding_days=1)

    result = executor.execute(
        task=_due_task("cycle-followup-due"),
        cycle_id="cycle-followup-due",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    assert result.status.value == "COMPLETED"
    assert result.changed is True
    assert result.network_requests == 0  # CRYPTO-only: zero provider requests
    assert kalshi_source.call_count == 0
    # 2 horizon kinds x 2 baseline kinds (Part: pre-deployment staleness-
    # semantics audit) -- gap_days == 0 in this fixture (see
    # ``_seed_btc_bars``'s own docstring), so ORIGIN_EVIDENCE and
    # DECISION_TIME both resolve identically, all 4 genuinely due/buildable.
    assert len(result.output_identities) == 4
    assert "FOLLOWUP_CREATED:4" in result.warnings
    assert "FOLLOWUP_PROVIDER_REQUESTS:0" in result.warnings

    followup_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observations = followup_repository.observations_for_decision(decision_id)
    assert len(observations) == 4
    assert all(item.authority == "NONE" for item in observations)  # authority NONE
    # The recurring task must genuinely use the corrected *current* policy
    # version for this venue, not merely "create some observation".
    assert all(
        item.policy_version == current_followup_policy_version(SharedVenue.CRYPTO)
        for item in observations
    )
    assert {item.baseline_kind for item in observations} == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }
    # Counterfactual Forward Outcome Temporal Integrity v1: the two baseline
    # kinds now measure two genuinely different windows (see
    # ``_seed_btc_bars``'s own docstring for the full derivation) -- which is
    # precisely what FOLLOWUP_POLICY_V2 could not express, since it floored
    # both anchors to the same UTC day and stamped 95 for both.  Asserting
    # the actual stamped prices is what catches a regression back to that: a
    # regression would still create four observations.
    by_baseline = {item.baseline_kind: item for item in observations}
    decision_time = by_baseline[FollowUpBaselineKind.DECISION_TIME]
    origin_evidence = by_baseline[FollowUpBaselineKind.ORIGIN_EVIDENCE]
    assert decision_time.reference_price_at_baseline == Decimal("95")
    assert decision_time.reference_price_at_target == Decimal("100")
    assert origin_evidence.reference_price_at_baseline == Decimal("100")
    assert origin_evidence.reference_price_at_target == Decimal("105")
    assert decision_time.policy_anchor_at == DECISION_DAY
    assert origin_evidence.policy_anchor_at == DECISION_DAY + timedelta(hours=12)
    for item in observations:
        # No pre-anchor movement can enter the measured window -- verifiable
        # directly from the retained row, without re-deriving anything.
        assert item.policy_anchor_at is not None
        assert item.baseline_time >= item.policy_anchor_at
        assert item.target_time - item.baseline_time == timedelta(days=item.horizon_days)
        # Every endpoint lookup was bounded by the instant this cycle
        # declared at dispatch.
        assert item.measurement_as_of == as_of
        # The decision-input reference is retained as disclosure only.
        assert item.decision_input_reference_price == Decimal("95")
        assert item.decision_input_source_observation_id == "cfr-bar-prior-completed-day"
        assert item.decision_input_evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    assert _row_count(postgres_engine, "decision_followup_observation") == 4


def test_not_yet_due_pairs_stay_pending_at_zero_cost(postgres_engine: Engine) -> None:
    _seed_btc_bars(postgres_engine)
    _seed_decision(postgres_engine, identity="crypto-early", expected_holding_days=90)
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-not-due-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    # Before SHORT_HORIZON's own target (decision + 1 day) -- neither
    # horizon is due yet.
    as_of = DECISION_DAY + timedelta(hours=1)

    result = executor.execute(
        task=_due_task("cycle-followup-not-due"),
        cycle_id="cycle-followup-not-due",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    # Counterfactual Outcome Feedback v1, Phase 0: this branch now also
    # performs ledger + classification sync ahead of follow-up sync itself.
    # ``_seed_decision`` already ledger-syncs its fixture decision (a
    # pre-existing helper behavior, unrelated to this milestone) but never
    # classifies it -- so classification-sync genuinely does new work here
    # even though follow-up sync finds nothing due, correctly making this
    # cycle COMPLETED rather than a pure zero-cost REUSED no-op.
    assert result.status.value == "COMPLETED"
    assert result.changed is True
    assert result.network_requests == 0
    assert kalshi_source.call_count == 0
    # 2 horizon kinds x 2 baseline kinds, all not yet due.
    assert "FOLLOWUP_SKIPPED_NOT_YET_DUE:4" in result.warnings
    assert "CLASSIFICATION_NEWLY_WRITTEN:1" in result.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == 0


# --------------------------------------------------------------------------
# Kalshi request budget
# --------------------------------------------------------------------------


def test_kalshi_request_budget_is_enforced_and_reported(postgres_engine: Engine) -> None:
    decision_count = 11  # 22 pairs total; budget is 20 requests at cost 1 each
    for index in range(decision_count):
        _seed_decision(
            postgres_engine,
            identity=f"kalshi-budget-{index:02d}",
            venue=SharedVenue.KALSHI,
            expected_holding_days=1,
        )
    assert decision_count * 2 > COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE
    assert decision_count * 2 <= COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-kalshi-budget-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource(request_cost=1)
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-kalshi-budget"),
        cycle_id="cycle-followup-kalshi-budget",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    budget = COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE
    assert result.status.value == "COMPLETED"
    assert result.network_requests == budget  # never exceeds the fixed budget
    assert kalshi_source.call_count == budget
    assert f"FOLLOWUP_CREATED:{budget}" in result.warnings
    assert f"FOLLOWUP_BLOCKED_UNAVAILABLE:{decision_count * 2 - budget}" in result.warnings
    assert COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED in result.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == budget

    # A later cycle picks up exactly the backlog the budget deferred --
    # never dropped, never retried in an unbounded loop.
    second_kalshi_source = _FakeRecurringKalshiSource(request_cost=1)
    second_executor = _executor(postgres_engine, kalshi_source=second_kalshi_source)
    second = second_executor.execute(
        task=_due_task("cycle-followup-kalshi-budget-2"),
        cycle_id="cycle-followup-kalshi-budget-2",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    remaining = decision_count * 2 - budget
    assert second.status.value == "COMPLETED"
    assert second.network_requests == remaining
    assert f"FOLLOWUP_CREATED:{remaining}" in second.warnings
    assert COUNTERFACTUAL_FOLLOWUP_KALSHI_BUDGET_EXHAUSTED not in second.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == decision_count * 2


# --------------------------------------------------------------------------
# Kalshi Settlement & Resolution Evidence Maturation v1: the real,
# engine-backed ``PostgresKalshiResolutionFactRepository`` wiring, driven
# through the identical real executor every other test in this file uses --
# not just the in-memory orchestration proofs in
# ``tests/test_counterfactual_followup.py``.
# --------------------------------------------------------------------------


class _FinalizedRecurringKalshiSource:
    """Every genuine attempt reports one already-finalized single-market
    read for a fixed ticker -- deterministic, zero-network."""

    def __init__(self, *, market_ticker: str, result: str = "yes") -> None:
        self.call_count = 0
        self._market_ticker = market_ticker
        self._result = result

    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        self.call_count += 1
        return KalshiMarketRead(
            market_status="finalized",
            yes_bid=None,
            yes_ask=None,
            settlement_result=self._result,
            settlement_source="KALSHI_PUBLIC_RESULT_FIELD",
            source_endpoint_identity="fake-kalshi-followup-recurring",
            request_count=1,
            market_ticker=self._market_ticker,
            event_ticker=None,
            settlement_value_dollars=Decimal("1.0000") if self._result == "yes" else Decimal("0"),
            settlement_ts=DECISION_DAY + timedelta(days=1),
            close_time=DECISION_DAY,
        )


def test_kalshi_resolution_fact_is_persisted_through_the_real_executor(
    postgres_engine: Engine,
) -> None:
    # ``instrument_id``/``product_or_market_id`` set to the real ticker,
    # matching the genuine ``BinaryComplementEvaluation``-with-no-retained-
    # event-ticker shape (``adapters.counterfactual_followup_sources``'s own
    # module docstring: instrument_id is then "the market ticker" directly)
    # -- the shape ``_read_kalshi_with_resolution_gate``'s pre-live-call
    # cache check can actually key on before the first live read ever
    # resolves anything.
    decision_id = _seed_decision(
        postgres_engine,
        identity="kalshi-resolution-1",
        venue=SharedVenue.KALSHI,
        expected_holding_days=1,
        instrument_id="KX-RECURRING-1",
        product_or_market_id="KX-RECURRING-1",
    )
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-kalshi-resolution-plan-v1")
    kalshi_source = _FinalizedRecurringKalshiSource(market_ticker="KX-RECURRING-1")
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-kalshi-resolution"),
        cycle_id="cycle-followup-kalshi-resolution",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    assert result.status.value == "COMPLETED"
    followup_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    observations = followup_repository.observations_for_decision(decision_id)
    # KALSHI: DECISION_TIME baseline only, but both SHORT_HORIZON and
    # STRATEGY_HORIZON (expected_holding_days=1) are due by day 2.
    assert len(observations) == 2
    assert all(item.settlement_result == "YES" for item in observations)
    # The second horizon candidate's own read was answered entirely from
    # the first candidate's freshly-persisted resolution fact -- exactly
    # one genuine live request for both.
    assert kalshi_source.call_count == 1

    resolution_facts = PostgresKalshiResolutionFactRepository(postgres_engine)
    fact = resolution_facts.latest_fact(market_ticker="KX-RECURRING-1", as_of=as_of)
    assert fact is not None
    assert fact.resolution_state is KalshiResolutionState.FINALIZED
    assert fact.resolved_result == "YES"
    assert fact.settlement_value_dollars == Decimal("1.0000")
    assert _row_count(postgres_engine, "kalshi_resolution_fact") == 1


def test_kalshi_resolution_fact_is_reused_across_separate_executor_dispatches(
    postgres_engine: Engine,
) -> None:
    """The real cross-cycle durability proof: two decisions referencing the
    identical market, resolved via two entirely separate executor
    instances (mirroring two independent systemd-timer ticks, or a
    fresh-process restart) -- the second dispatch must reuse the first's
    durably retained resolution fact rather than issuing its own live
    request."""

    first_decision_id = _seed_decision(
        postgres_engine,
        identity="kalshi-resolution-shared-1",
        venue=SharedVenue.KALSHI,
        expected_holding_days=None,  # exactly one due candidate: SHORT_HORIZON
        instrument_id="KX-RECURRING-SHARED",
        product_or_market_id="KX-RECURRING-SHARED",
    )
    plan = _register_plan(
        postgres_engine, plan_id="postgres-followup-kalshi-resolution-reuse-plan-v1"
    )
    as_of = DECISION_DAY + timedelta(days=2)
    first_source = _FinalizedRecurringKalshiSource(market_ticker="KX-RECURRING-SHARED")
    first_executor = _executor(postgres_engine, kalshi_source=first_source)
    first_result = first_executor.execute(
        task=_due_task("cycle-followup-kalshi-resolution-reuse-1"),
        cycle_id="cycle-followup-kalshi-resolution-reuse-1",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert first_result.status.value == "COMPLETED"
    assert first_source.call_count == 1

    second_decision_id = _seed_decision(
        postgres_engine,
        identity="kalshi-resolution-shared-2",
        venue=SharedVenue.KALSHI,
        expected_holding_days=None,
        instrument_id="KX-RECURRING-SHARED",
        product_or_market_id="KX-RECURRING-SHARED",
    )
    second_source = _FinalizedRecurringKalshiSource(market_ticker="KX-RECURRING-SHARED")
    second_executor = _executor(postgres_engine, kalshi_source=second_source)
    second_result = second_executor.execute(
        task=_due_task("cycle-followup-kalshi-resolution-reuse-2"),
        cycle_id="cycle-followup-kalshi-resolution-reuse-2",
        plan=plan,
        as_of=as_of + timedelta(minutes=5),
        executed_at=as_of + timedelta(minutes=5),
    )
    assert second_result.status.value == "COMPLETED"
    # The second decision's own follow-up matured entirely from the
    # already-retained resolution fact -- the injected fake was never
    # called at all.
    assert second_source.call_count == 0
    assert second_result.network_requests == 0

    followup_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    first_observation = followup_repository.observations_for_decision(first_decision_id)[0]
    second_observation = followup_repository.observations_for_decision(second_decision_id)[0]
    assert first_observation.settlement_result == "YES"
    assert second_observation.settlement_result == "YES"
    # Exactly one resolution fact retained for the shared ticker -- reused,
    # never duplicated.
    assert _row_count(postgres_engine, "kalshi_resolution_fact") == 1


# --------------------------------------------------------------------------
# Backlog carries forward deterministically; final cycle is a genuine
# zero-cost no-op once fully drained.
# --------------------------------------------------------------------------


def test_backlog_carries_forward_deterministically_and_final_cycle_is_a_zero_cost_no_op(
    postgres_engine: Engine,
) -> None:
    _seed_btc_bars(postgres_engine)
    # 30 decisions x 2 horizon kinds x 2 baseline kinds (Part: pre-deployment
    # staleness-semantics audit -- CRYPTO gets both ORIGIN_EVIDENCE and
    # DECISION_TIME under the current policy) = 120 pairs; sample limit is
    # 25 per cycle.
    decision_count = 30
    for index in range(decision_count):
        _seed_decision(postgres_engine, identity=f"backlog-{index:03d}", expected_holding_days=1)
    total_pairs = decision_count * 4
    # Counterfactual Forward Outcome Temporal Integrity v1: every decision
    # here is CRYPTO, so exactly one of the current selection's two bounded
    # runs (FOLLOWUP_POLICY_V3) has any candidates at all -- the per-cycle
    # total is the sum across runs, but the number this fixture can actually
    # produce per cycle is one run's own bound.
    sample = COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    assert total_pairs > sample  # otherwise this test would not exercise carry-forward
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-backlog-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    all_created: set[str] = set()
    cycle_number = 0
    while len(all_created) < total_pairs:
        cycle_number += 1
        result = executor.execute(
            task=_due_task(f"cycle-followup-backlog-{cycle_number}"),
            cycle_id=f"cycle-followup-backlog-{cycle_number}",
            plan=plan,
            as_of=as_of,
            executed_at=as_of,
        )
        created_this_cycle = set(result.output_identities)
        expected_this_cycle = min(sample, total_pairs - len(all_created))
        assert len(created_this_cycle) == expected_this_cycle
        # Deterministic, oldest-due-first: never re-creates an id a prior
        # cycle already produced.
        assert created_this_cycle.isdisjoint(all_created)
        all_created |= created_this_cycle
        assert kalshi_source.call_count == 0  # every pair here is CRYPTO

    assert cycle_number > 1  # genuinely needed more than one cycle
    assert len(all_created) == total_pairs
    assert _row_count(postgres_engine, "decision_followup_observation") == total_pairs
    # Every pair drained across every cycle used the current (corrected)
    # policy version -- carry-forward/backlog draining is not something
    # FOLLOWUP_POLICY_V1 vs V2 changes, but this proves it genuinely stayed
    # on one explicit version throughout, never drifting mid-backlog.
    all_observations = PostgresCounterfactualFollowUpRepository(postgres_engine).observations()
    assert len(all_observations) == total_pairs
    assert all(
        item.policy_version == current_followup_policy_version(SharedVenue.CRYPTO)
        for item in all_observations
    )
    # Per baseline kind under FOLLOWUP_POLICY_V3 -- the two kinds now
    # measure two genuinely different windows (see ``_seed_btc_bars``).
    assert {
        (item.baseline_kind, item.reference_price_at_baseline, item.reference_price_at_target)
        for item in all_observations
    } == {
        (FollowUpBaselineKind.DECISION_TIME, Decimal("95"), Decimal("100")),
        (FollowUpBaselineKind.ORIGIN_EVIDENCE, Decimal("100"), Decimal("105")),
    }

    # One more cycle: genuinely nothing left due.
    cycle_number += 1
    final = executor.execute(
        task=_due_task(f"cycle-followup-backlog-{cycle_number}"),
        cycle_id=f"cycle-followup-backlog-{cycle_number}",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert final.status.value == "REUSED"
    assert final.changed is False
    assert final.network_requests == 0
    assert f"FOLLOWUP_SKIPPED_ALREADY_OBSERVED:{total_pairs}" in final.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == total_pairs


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Backlog Throughput v1: ``TaskExecution.awaits_
# intraday_continuation`` against the real, unmodified executor branch --
# proves the *new* signal this milestone adds, not ``SyncFollowUpObservations``
# itself (already proven above/pre-existing). Every call below deliberately
# reuses the identical ``cycle_id`` across rounds -- exactly the new
# invocation pattern ``application.operating_cycle.ExecuteOperatingCycle``
# now performs across same-UTC-day ticks (see ``TaskExecution.awaits_
# intraday_continuation``'s own docstring) -- rather than the distinct-
# cycle_id-per-round shape the cross-day carry-forward test above uses, so
# this is also the first proof that the executor branch itself (ledger
# sync/baseline-quote/classification sync composed alongside the follow-up
# sync proper) tolerates genuine repeat dispatch under one unchanged
# cycle_id without conflict.
# --------------------------------------------------------------------------


def test_awaits_intraday_continuation_signals_more_work_then_clears_once_drained(
    postgres_engine: Engine,
) -> None:
    _seed_btc_bars(postgres_engine)
    decision_count = 30
    for index in range(decision_count):
        _seed_decision(postgres_engine, identity=f"intraday-{index:03d}", expected_holding_days=1)
    total_pairs = decision_count * 4
    # CRYPTO-only fixture -- one of the selection's two runs does all the
    # work, so one round produces one run's own bound (see the carry-forward
    # test above for the same reasoning).
    sample = COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    assert total_pairs > sample * 2  # exercise at least two continuing rounds before draining
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-intraday-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)
    cycle_id = "cycle-followup-intraday-same-day"

    all_created: set[str] = set()
    round_number = 0
    saw_continuation = False
    while len(all_created) < total_pairs:
        round_number += 1
        result = executor.execute(
            task=_due_task(cycle_id),
            cycle_id=cycle_id,
            plan=plan,
            as_of=as_of,
            executed_at=as_of,
        )
        created_this_round = set(result.output_identities)
        expected_this_round = min(sample, total_pairs - len(all_created))
        assert len(created_this_round) == expected_this_round
        assert created_this_round.isdisjoint(all_created)
        all_created |= created_this_round
        if len(all_created) < total_pairs:
            # More genuinely due work remained right after this round's own
            # writes landed -- must signal continuation so the orchestration
            # layer dispatches another bounded round this same UTC day
            # instead of freezing today's result at this round's count.
            assert result.awaits_intraday_continuation is True
            assert "FOLLOWUP_AWAITS_INTRADAY_CONTINUATION" in result.warnings
            saw_continuation = True
        else:
            # This round's own writes exhausted the backlog -- genuinely
            # nothing left due, so continuation must not be signalled.
            assert result.awaits_intraday_continuation is False
            assert "FOLLOWUP_AWAITS_INTRADAY_CONTINUATION" not in result.warnings

    assert saw_continuation  # the test genuinely exercised >1 round
    assert round_number > 1
    assert len(all_created) == total_pairs
    assert _row_count(postgres_engine, "decision_followup_observation") == total_pairs
    assert kalshi_source.call_count == 0  # every pair here is CRYPTO

    # One more same-cycle_id call: genuinely nothing left due, so this is a
    # zero-cost no-op that also never signals continuation.
    round_number += 1
    final = executor.execute(
        task=_due_task(cycle_id),
        cycle_id=cycle_id,
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert final.status.value == "REUSED"
    assert final.changed is False
    assert final.awaits_intraday_continuation is False
    assert _row_count(postgres_engine, "decision_followup_observation") == total_pairs


def test_daily_ceiling_stops_intraday_continuation_even_when_more_is_genuinely_due(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apex.adapters import operating_cycle_tasks as task_module

    # CRYPTO-only fixture: one round produces one policy version's own bound.
    lowered_daily_ceiling = COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION - 1
    # Counterfactual Follow-Up Daily Ceiling Lane Scope v1: the ceiling the
    # enforcement path reads is now the per-lane one; ``..._PER_DAY`` is the
    # derived, reported total. This test's CRYPTO-only fixture exercises a
    # single lane, so lowering the per-lane bound reproduces exactly the
    # situation it has always asserted.
    monkeypatch.setattr(
        task_module,
        "COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION",
        lowered_daily_ceiling,
    )
    _seed_btc_bars(postgres_engine)
    decision_count = 30
    for index in range(decision_count):
        _seed_decision(postgres_engine, identity=f"cap-{index:03d}", expected_holding_days=1)
    total_pairs = decision_count * 4
    assert total_pairs > COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-cap-plan-v1")
    executor = _executor(postgres_engine, kalshi_source=_FakeRecurringKalshiSource())
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-cap-day"),
        cycle_id="cycle-followup-cap-day",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    # This round genuinely filled its own sample_limit -- real backlog
    # remains -- but its own writes already crossed the deliberately
    # lowered daily ceiling, so continuation must not be signalled even
    # though more is due: the named daily bound is a genuine, independent
    # ceiling, never bypassed just because a round itself stayed bounded.
    assert len(result.output_identities) == COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    assert result.awaits_intraday_continuation is False
    assert (
        f"FOLLOWUP_OBSERVATIONS_TODAY:{COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION}"
        in result.warnings
    )
    assert (
        f"FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION:{lowered_daily_ceiling}"
        in result.warnings
    )
    # The lane that genuinely finished its day is named, not merely absent.
    assert (
        f"FOLLOWUP_DAILY_CEILING_REACHED:{current_followup_policy_version(SharedVenue.CRYPTO)}"
        in result.warnings
    )
    assert "FOLLOWUP_AWAITS_INTRADAY_CONTINUATION" not in result.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == (
        COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    )


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Daily Ceiling Lane Scope v1
# --------------------------------------------------------------------------


def test_one_lane_reaching_its_daily_ceiling_does_not_stop_another_lanes_work(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this milestone exists for, at its root.

    CRYPTO runs under ``FOLLOWUP_POLICY_V3`` and KALSHI/SECURITIES under
    ``FOLLOWUP_POLICY_V2`` -- two independently-cursored lanes with
    independent per-round bounds. Before this milestone the *daily* bound was
    a single shared row count, so CRYPTO's rows and KALSHI's rows competed
    for one budget and whichever lane wrote faster ended the other lane's day
    with it. Here CRYPTO deliberately exhausts its own lane immediately; the
    KALSHI lane must be entirely unaffected -- still running, still creating,
    still asking for another round."""

    from apex.adapters import operating_cycle_tasks as task_module

    crypto_version = current_followup_policy_version(SharedVenue.CRYPTO)
    kalshi_version = current_followup_policy_version(SharedVenue.KALSHI)
    assert crypto_version != kalshi_version  # two genuine lanes, not one

    sample = COUNTERFACTUAL_FOLLOWUP_SAMPLE_LIMIT_PER_POLICY_VERSION
    # One round of CRYPTO alone already meets this ceiling, so the CRYPTO
    # lane is finished for the day after round 1 while genuine CRYPTO
    # backlog remains -- the exact shape that used to end KALSHI's day too.
    monkeypatch.setattr(
        task_module, "COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION", sample
    )

    _seed_btc_bars(postgres_engine)
    # CRYPTO: 30 decisions x 4 pairs = 120, far more than one round's bound,
    # so the CRYPTO lane genuinely saturates round 1 and still has backlog.
    for index in range(30):
        _seed_decision(
            postgres_engine, identity=f"lane-crypto-{index:03d}", expected_holding_days=1
        )
    # KALSHI: 13 decisions x 2 pairs = 26, deliberately just over the same
    # per-round attempt bound, so the KALSHI lane *also* saturates round 1
    # and genuinely still wants another one. Under-seeding here would make
    # this test turn on whether KALSHI happened to run dry, a different
    # question entirely from the cross-lane coupling under test.
    kalshi_decision_count = 13
    assert kalshi_decision_count * 2 > sample
    for index in range(kalshi_decision_count):
        _seed_decision(
            postgres_engine,
            identity=f"lane-kalshi-{index:03d}",
            venue=SharedVenue.KALSHI,
            expected_holding_days=1,
        )

    plan = _register_plan(postgres_engine, plan_id="postgres-followup-lane-scope-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    first = executor.execute(
        task=_due_task("cycle-followup-lane-scope"),
        cycle_id="cycle-followup-lane-scope",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    crypto_after_first = _observation_count(postgres_engine, crypto_version)
    kalshi_after_first = _observation_count(postgres_engine, kalshi_version)
    assert crypto_after_first == sample  # CRYPTO lane is now at its ceiling
    # KALSHI genuinely ran too, and is still under its own ceiling -- its
    # round-1 creations are capped by the independent Kalshi *request* budget,
    # which this milestone deliberately leaves exactly as it was.
    assert kalshi_after_first == COUNTERFACTUAL_FOLLOWUP_MAX_KALSHI_REQUESTS_PER_CYCLE
    assert kalshi_after_first < sample
    assert f"FOLLOWUP_DAILY_CEILING_REACHED:{crypto_version}" in first.warnings
    assert f"FOLLOWUP_DAILY_CEILING_REACHED:{kalshi_version}" not in first.warnings
    # KALSHI still has genuine backlog and budget, so the day continues --
    # this is the assertion that used to fail: the shared counter had
    # already crossed the ceiling on CRYPTO's rows alone.
    assert first.awaits_intraday_continuation is True

    second = executor.execute(
        task=_due_task("cycle-followup-lane-scope"),
        cycle_id="cycle-followup-lane-scope",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    crypto_after_second = _observation_count(postgres_engine, crypto_version)
    kalshi_after_second = _observation_count(postgres_engine, kalshi_version)
    # The exhausted lane is skipped outright -- not run and then discarded.
    assert crypto_after_second == crypto_after_first
    assert f"FOLLOWUP_POLICY_VERSION:{crypto_version}" not in second.warnings
    assert f"FOLLOWUP_POLICY_VERSION:{kalshi_version}" in second.warnings
    # ... while the unaffected lane keeps exactly the throughput it had.
    assert kalshi_after_second > kalshi_after_first
    # Both lanes are reported honestly every round, including the skipped one.
    assert f"FOLLOWUP_OBSERVATIONS_TODAY:{crypto_version}:{crypto_after_second}" in second.warnings
    assert f"FOLLOWUP_OBSERVATIONS_TODAY:{kalshi_version}:{kalshi_after_second}" in second.warnings


def test_an_exhausted_lane_consumes_no_provider_requests_and_stays_replay_stable(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane past its daily ceiling must cost nothing at all -- no provider
    request, no write -- and repeating the round must stay deterministic."""

    from apex.adapters import operating_cycle_tasks as task_module

    kalshi_version = current_followup_policy_version(SharedVenue.KALSHI)
    monkeypatch.setattr(
        task_module, "COUNTERFACTUAL_FOLLOWUP_MAX_OBSERVATIONS_PER_DAY_PER_POLICY_VERSION", 4
    )
    for index in range(8):
        _seed_decision(
            postgres_engine,
            identity=f"lane-exhaust-{index:03d}",
            venue=SharedVenue.KALSHI,
            expected_holding_days=1,
        )
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-lane-exhaust-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    executor.execute(
        task=_due_task("cycle-followup-lane-exhaust"),
        cycle_id="cycle-followup-lane-exhaust",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    rows_after_first = _row_count(postgres_engine, "decision_followup_observation")
    calls_after_first = kalshi_source.call_count
    assert rows_after_first >= 4  # lane reached its ceiling

    later = executor.execute(
        task=_due_task("cycle-followup-lane-exhaust"),
        cycle_id="cycle-followup-lane-exhaust",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert kalshi_source.call_count == calls_after_first  # zero further provider load
    assert _row_count(postgres_engine, "decision_followup_observation") == rows_after_first
    assert later.awaits_intraday_continuation is False
    assert f"FOLLOWUP_DAILY_CEILING_REACHED:{kalshi_version}" in later.warnings
    assert _observation_count(postgres_engine, kalshi_version) == rows_after_first

    # Replay determinism: a third identical round changes nothing again.
    repeat = executor.execute(
        task=_due_task("cycle-followup-lane-exhaust"),
        cycle_id="cycle-followup-lane-exhaust",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert kalshi_source.call_count == calls_after_first
    assert _row_count(postgres_engine, "decision_followup_observation") == rows_after_first
    assert repeat.awaits_intraday_continuation is False


# --------------------------------------------------------------------------
# Restart safety / already-observed reuse (crypto and Kalshi both)
# --------------------------------------------------------------------------


def test_repeated_executor_calls_are_restart_safe_and_reuse_already_observed_evidence(
    postgres_engine: Engine,
) -> None:
    _seed_btc_bars(postgres_engine)
    crypto_decision = _seed_decision(
        postgres_engine, identity="restart-crypto", expected_holding_days=1
    )
    kalshi_decision = _seed_decision(
        postgres_engine,
        identity="restart-kalshi",
        venue=SharedVenue.KALSHI,
        expected_holding_days=1,
    )
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-restart-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    first = executor.execute(
        task=_due_task("cycle-followup-restart-1"),
        cycle_id="cycle-followup-restart-1",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert first.status.value == "COMPLETED"
    assert first.changed is True
    # 1 CRYPTO decision x 2 horizons x 2 baseline kinds (Part: pre-deployment
    # staleness-semantics audit) + 1 KALSHI decision x 2 horizons x 1
    # baseline kind (KALSHI never gets ORIGIN_EVIDENCE) = 4 + 2 = 6.
    assert len(first.output_identities) == 6
    assert kalshi_source.call_count == 2  # exactly the 2 KALSHI horizon attempts
    assert first.network_requests == 2

    # "Restart": a brand-new executor instance (fresh repositories) against
    # the same database, same genuinely still-due window.
    second_kalshi_source = _FakeRecurringKalshiSource()
    second_executor = _executor(postgres_engine, kalshi_source=second_kalshi_source)
    second = second_executor.execute(
        task=_due_task("cycle-followup-restart-2"),
        cycle_id="cycle-followup-restart-2",
        plan=plan,
        as_of=as_of + timedelta(hours=1),
        executed_at=as_of + timedelta(hours=1),
    )
    assert second.status.value == "REUSED"
    assert second.changed is False
    assert second.network_requests == 0  # zero re-fetch of already-observed pairs
    assert second_kalshi_source.call_count == 0  # never even attempted
    assert "FOLLOWUP_SKIPPED_ALREADY_OBSERVED:6" in second.warnings

    assert _row_count(postgres_engine, "decision_followup_observation") == 6
    followup_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    crypto_observations = followup_repository.observations_for_decision(crypto_decision)
    kalshi_observations = followup_repository.observations_for_decision(kalshi_decision)
    assert len(crypto_observations) == 4
    assert len(kalshi_observations) == 2
    assert {item.baseline_kind for item in crypto_observations} == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }
    assert {item.baseline_kind for item in kalshi_observations} == {
        FollowUpBaselineKind.DECISION_TIME
    }
    # Each venue's observations are stamped under the explicit current
    # policy version *for that venue* (Counterfactual Forward Outcome
    # Temporal Integrity v1's per-venue selection): CRYPTO under
    # FOLLOWUP_POLICY_V3, KALSHI still under FOLLOWUP_POLICY_V2 -- whose own
    # read logic remains byte-for-byte unchanged (Part: Kalshi
    # non-regression).
    assert all(
        item.policy_version == current_followup_policy_version(SharedVenue.CRYPTO)
        for item in crypto_observations
    )
    assert all(
        item.policy_version == current_followup_policy_version(SharedVenue.KALSHI)
        for item in kalshi_observations
    )
    # Per baseline kind under FOLLOWUP_POLICY_V3: the two kinds measure two
    # genuinely different windows now (see ``_seed_btc_bars``).
    assert {
        (item.baseline_kind, item.reference_price_at_baseline) for item in crypto_observations
    } == {
        (FollowUpBaselineKind.DECISION_TIME, Decimal("95")),
        (FollowUpBaselineKind.ORIGIN_EVIDENCE, Decimal("100")),
    }


# --------------------------------------------------------------------------
# Provider failure: explicit, never silently absorbed; request accounting
# stays accurate even though nothing was persisted.
# --------------------------------------------------------------------------


def test_provider_failure_produces_explicit_blocked_outcome_with_accurate_request_accounting(
    postgres_engine: Engine,
) -> None:
    _seed_decision(
        postgres_engine, identity="always-fails", venue=SharedVenue.KALSHI, expected_holding_days=1
    )
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-failure-plan-v1")
    kalshi_source = _FailingRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-provider-failure"),
        cycle_id="cycle-followup-provider-failure",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    # The task itself never FAILS on a genuine per-pair source outage --
    # BLOCKED_UNAVAILABLE is an explicit, retained-nowhere-but-honestly-
    # reported outcome, not a task-level failure and not a fabricated
    # success. Counterfactual Outcome Feedback v1, Phase 0: as in the
    # not-yet-due test above, ``_seed_decision`` ledger-syncs but never
    # classifies its fixture decision, so this branch's own classification
    # sync genuinely does new work -- COMPLETED, not REUSED, even though
    # follow-up sync itself created zero observations.
    assert result.status.value == "COMPLETED"  # classification-sync did new work
    assert result.changed is True
    assert kalshi_source.call_count == 2  # both horizons genuinely attempted
    assert result.network_requests == 2  # accurate even though both failed
    assert "FOLLOWUP_BLOCKED_UNAVAILABLE:2" in result.warnings
    assert "FOLLOWUP_CREATED:0" in result.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == 0


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Starvation Fix v1 -- the genuine "Jul-30"
# availability-as-of shape against the real Postgres-backed
# ``CryptoPriceSource`` (``_CryptoFollowUpPriceSource.latest_close_before``),
# not a simplified in-memory fake: a retained candle *does* exist with a
# qualifying ``bucket_start`` strictly before the decision's own baseline
# boundary, but the batch that retains it was only captured
# (``completed_at``) strictly *after* that boundary -- the specific
# defect-shape ``latest_close_before`` exists to reject, distinct from (and
# a strict superset of) the simpler "nothing retained at all" shape already
# proven end-to-end in ``tests/test_counterfactual_followup.py``.  No future
# ingestion can ever change this: ``completed_at`` is a fixed historical
# fact the instant it is written, and any *new* batch saved from now on
# necessarily gets its own ``completed_at`` at or after "now", which is
# already past this boundary too.
# --------------------------------------------------------------------------


def test_jul30_availability_as_of_shape_is_permanent_for_both_baseline_and_horizon_kinds(
    postgres_engine: Engine,
) -> None:
    """Covers both ``FollowUpBaselineKind`` values (``ORIGIN_EVIDENCE``,
    ``DECISION_TIME`` -- ``gap_days == 0`` in this fixture, so they share
    the identical unsatisfiable boundary, exactly the real Jul-30 cohort's
    own shape) and both ``FollowUpHorizonKind`` values (``SHORT_HORIZON``,
    and ``STRATEGY_HORIZON`` -- the "second Aug-29 wave" shape: baseline
    permanence never depends on which horizon is being measured, so both
    are proven permanent by the identical mechanism, not by coincidence).
    A second, genuinely resolvable CRYPTO decision (a different product)
    proves the permanent one cannot starve later CRYPTO progress even
    within the same venue, the same cycle, and the same executor call."""

    permanent_decision_id = _seed_decision(
        postgres_engine, identity="jul30-permanent", expected_holding_days=3
    )
    resolvable_decision_id = _seed_decision(
        postgres_engine,
        identity="jul30-resolvable",
        expected_holding_days=3,
        instrument_id="ETH",
        product_or_market_id="ETH-USD",
    )
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    boundary = DECISION_DAY  # completed_utc_day of both evaluated_at and generated_at here
    # The one retained BTC-USD batch: a real, qualifying bucket_start
    # (strictly before ``boundary``) -- but captured strictly *after*
    # ``boundary``, so it must never be selected (Part: availability-as-of
    # correctness).  No batch retained anywhere else for BTC-USD, so this
    # is the *only* candidate the baseline lookup could ever consider.
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="jul30-permanent-too-late-batch",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=boundary - timedelta(days=1), close="95", volume="1"
                ),
            ),
            completed_at=boundary + timedelta(hours=1),
        )
    )
    # A second, independent CRYPTO product with genuinely, properly
    # available evidence -- baseline captured well before the boundary, and
    # both horizons' target buckets retained too.
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="jul30-resolvable-baseline",
            product_id="ETH-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=boundary - timedelta(days=1), close="50", volume="1"
                ),
            ),
            completed_at=boundary - timedelta(hours=1),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="jul30-resolvable-short-target",
            product_id="ETH-USD",
            candles=(CryptoDailyObservation.create(bucket_start=boundary, close="51", volume="1"),),
            completed_at=boundary + timedelta(hours=1),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="jul30-resolvable-strategy-target",
            product_id="ETH-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=boundary + timedelta(days=2), close="53", volume="1"
                ),
            ),
            completed_at=boundary + timedelta(days=2, hours=1),
        )
    )
    # Counterfactual Forward Outcome Temporal Integrity v1: the two bars
    # ``FOLLOWUP_POLICY_V3``'s ORIGIN_EVIDENCE window actually reads.  That
    # anchor is ``generated_at`` (``boundary + 12h``, intraday), so its
    # window starts at ``boundary + 1`` -- one day later than the
    # DECISION_TIME window, and therefore one bucket later on each end.
    # Under FOLLOWUP_POLICY_V2 both baseline kinds read the same two
    # buckets, because V2 floored both anchors to the same UTC day; needing
    # distinct bars here is a direct, visible consequence of V3 no longer
    # doing that.  Their prices are deliberately distinct from every bar
    # above so a regression reading the wrong bucket could not accidentally
    # produce a matching price.
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="jul30-resolvable-origin-short-target",
            product_id="ETH-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=boundary + timedelta(days=1), close="52", volume="1"
                ),
            ),
            completed_at=boundary + timedelta(days=1, hours=1),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="jul30-resolvable-origin-strategy-target",
            product_id="ETH-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=boundary + timedelta(days=3), close="54", volume="1"
                ),
            ),
            completed_at=boundary + timedelta(days=3, hours=1),
        )
    )

    plan = _register_plan(postgres_engine, plan_id="postgres-followup-jul30-permanent-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=10)  # both horizon kinds due for both decisions

    result = executor.execute(
        task=_due_task("cycle-followup-jul30-permanent"),
        cycle_id="cycle-followup-jul30-permanent",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    assert result.status.value == "COMPLETED"
    assert result.network_requests == 0  # every pair here is CRYPTO
    assert kalshi_source.call_count == 0
    # permanent decision: SHORT_HORIZON + STRATEGY_HORIZON, x ORIGIN_EVIDENCE
    # + DECISION_TIME -- all 4 pairs permanently unavailable, zero attempted.
    assert "FOLLOWUP_PERMANENTLY_UNAVAILABLE:4" in result.warnings
    # resolvable decision: the identical 4-pair shape, all genuinely created.
    assert "FOLLOWUP_CREATED:4" in result.warnings
    assert "FOLLOWUP_BLOCKED_UNAVAILABLE:0" in result.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == 4

    followup_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    # No fabricated row for the permanently-blocked decision, ever.
    assert followup_repository.observations_for_decision(permanent_decision_id) == ()
    resolvable_observations = followup_repository.observations_for_decision(resolvable_decision_id)
    assert len(resolvable_observations) == 4
    assert {item.baseline_kind for item in resolvable_observations} == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }
    assert {item.horizon_kind for item in resolvable_observations} == {
        FollowUpHorizonKind.SHORT_HORIZON,
        FollowUpHorizonKind.STRATEGY_HORIZON,
    }

    # The read-only, zero-provider-request PlanFollowUpObservations path
    # (the CLI's own ``followup-plan``/``status`` diagnostics) independently
    # classifies every one of the permanent decision's 4 pairs the same way
    # -- an operator must never see this reported as ordinary healthy DUE
    # backlog.
    plan_report = PlanFollowUpObservations(
        followup_repository.ledger, followup_repository.subjects, followup_repository.crypto_prices
    ).execute(
        as_of=as_of,
        already_observed={item.observation_id for item in followup_repository.observations()},
        policy_version=current_followup_policy_version(SharedVenue.CRYPTO),
        venues=(SharedVenue.CRYPTO,),
    )
    permanent_entries = [
        entry for entry in plan_report.entries if entry.decision_id == permanent_decision_id
    ]
    assert len(permanent_entries) == 4
    assert all(
        entry.state is FollowUpDueState.PERMANENTLY_UNAVAILABLE for entry in permanent_entries
    )
    # Counterfactual Forward Outcome Temporal Integrity v1: the permanence
    # *proof* is identical under V2 and V3 (a frozen, anchor-derived
    # boundary that no future capture can satisfy), but the missing fact is
    # structurally different -- V2's measurement baseline versus V3's
    # decision-input disclosure -- so the two deliberately never share a
    # reason string.  A diagnostic filtering on ``reasons`` alone must be
    # able to tell which one it is looking at.
    assert all(
        entry.reasons == (CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE,)
        for entry in permanent_entries
    )
    resolvable_entries = [
        entry for entry in plan_report.entries if entry.decision_id == resolvable_decision_id
    ]
    assert len(resolvable_entries) == 4
    assert all(entry.state is FollowUpDueState.OBSERVED for entry in resolvable_entries)

    # Next cycle: the identical permanent classification is honestly
    # re-derived (never durably retained, never magically resolved by mere
    # elapsed time), and costs zero attempt/provider budget again -- the
    # already-observed resolvable decision costs zero too, restart-safe.
    second_kalshi_source = _FakeRecurringKalshiSource()
    second_executor = _executor(postgres_engine, kalshi_source=second_kalshi_source)
    second = second_executor.execute(
        task=_due_task("cycle-followup-jul30-permanent-2"),
        cycle_id="cycle-followup-jul30-permanent-2",
        plan=plan,
        as_of=as_of + timedelta(hours=1),
        executed_at=as_of + timedelta(hours=1),
    )
    assert second.status.value == "REUSED"
    assert second.changed is False
    assert second.network_requests == 0
    assert "FOLLOWUP_PERMANENTLY_UNAVAILABLE:4" in second.warnings
    assert "FOLLOWUP_SKIPPED_ALREADY_OBSERVED:4" in second.warnings
    assert "FOLLOWUP_CREATED:0" in second.warnings
    assert _row_count(postgres_engine, "decision_followup_observation") == 4
    assert followup_repository.observations_for_decision(permanent_decision_id) == ()


# --------------------------------------------------------------------------
# Autonomy Audit v1: every newly-created (or otherwise still-unassessed)
# retained follow-up observation must eventually receive a deterministic
# evidence-quality assessment without any human CLI invocation. Proves the
# recurring ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` branch's own bounded,
# self-healing ``AssessFollowUpEvidenceQuality`` sweep (``adapters.
# operating_cycle_tasks._BoundedUnassessedFollowUpEvidenceSource``) end to
# end, through the identical executor every other test in this file drives.
# --------------------------------------------------------------------------


def test_new_clean_observation_is_automatically_assessed_eligible_in_the_same_cycle(
    postgres_engine: Engine,
) -> None:
    """Requirement A. One recurring-cycle call both creates a genuinely new
    CRYPTO observation (via the unmodified ``SyncFollowUpObservations``) and
    assesses it ``ELIGIBLE`` -- zero CLI/human involvement anywhere."""

    _seed_btc_bars(postgres_engine)
    _seed_decision(postgres_engine, identity="auto-eligible", expected_holding_days=1)
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-auto-eligible-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-auto-eligible"),
        cycle_id="cycle-followup-auto-eligible",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )

    assert result.status.value == "COMPLETED"
    assert result.changed is True
    assert result.output_identities  # sync genuinely created something
    assert f"FOLLOWUP_EVIDENCE_QUALITY_ASSESSED:{len(result.output_identities)}" in result.warnings
    assert "FOLLOWUP_EVIDENCE_QUALITY_INELIGIBLE:0" in result.warnings
    assert not any(
        warning.startswith("FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_SWEEP_FAILED")
        for warning in result.warnings
    )

    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    for observation_id in result.output_identities:
        assessment = repository.assessment(
            observation_id=observation_id,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        )
        assert assessment is not None
        assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
        assert is_evidence_eligible_for_downstream_attribution(assessment) is True


def test_contaminated_observation_already_retained_is_automatically_assessed_ineligible(
    postgres_engine: Engine,
) -> None:
    """Requirement B. Mirrors the confirmed DOGE-USD incident shape exactly
    (``DecisionFollowUpObservation`` constructed directly -- the guarded
    ``build_crypto_price_movement_observation`` now refuses this, exactly
    how the real contaminated row entered before this milestone's
    generation-time fix existed): a retained observation whose baseline
    evidence is ``EvidenceClass.FIXTURE`` is found and marked
    ``INELIGIBLE_EVIDENCE`` by the recurring cycle's own bounded sweep,
    automatically -- proving the sweep is not biased toward always finding
    evidence clean."""

    decision_id = _seed_decision(
        postgres_engine,
        identity="auto-ineligible",
        product_or_market_id="ZEC-USD",
        instrument_id="ZEC",
        expected_holding_days=None,
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    fixture_baseline_id = "cfr-auto-ineligible-fixture-baseline"
    genuine_target_id = "cfr-auto-ineligible-genuine-target"
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id=fixture_baseline_id,
            product_id="ZEC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY, close="189.504", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
            evidence_class=EvidenceClass.FIXTURE,
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id=genuine_target_id,
            product_id="ZEC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY + timedelta(days=1), close="0.07215", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(days=1, hours=1),
        )
    )
    baseline_time = DECISION_DAY
    target_time = DECISION_DAY + timedelta(days=1)
    observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    source_lineage = (fixture_baseline_id, genuine_target_id)
    canonical_hash = followup_observation_canonical_hash(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=decision.venue,
        disposition=decision.disposition,
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=1,
        decision_evaluated_at=decision.evaluated_at,
        target_time=target_time,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id="ZEC",
        subject_product_or_market_id="ZEC-USD",
        provenance=EvidenceClass.HISTORICAL_PUBLIC,
        source_lineage=source_lineage,
        data_limitations=(CRYPTO_DAILY_CLOSE_LIMITATION,),
        reference_price_at_baseline=Decimal("189.504"),
        reference_price_at_target=Decimal("0.07215"),
        subsequent_return=Decimal("-0.99961927"),
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=baseline_time,
    )
    contaminated = DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=decision.venue,
        disposition=decision.disposition,
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=baseline_time,
        target_time=target_time,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id="ZEC",
        subject_product_or_market_id="ZEC-USD",
        provenance=EvidenceClass.HISTORICAL_PUBLIC,
        baseline_evidence_class=None,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        evidence_eligibility_policy_version=None,
        observed_at=NOW,
        source_lineage=source_lineage,
        data_limitations=(CRYPTO_DAILY_CLOSE_LIMITATION,),
        reference_price_at_baseline=Decimal("189.504"),
        reference_price_at_target=Decimal("0.07215"),
        subsequent_return=Decimal("-0.99961927"),
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=canonical_hash,
        authority="NONE",
    )
    was_replay = repository.save_observation(observation=contaminated)
    assert was_replay is False

    # Pre-condition: genuinely unassessed, fail-closed.
    assert (
        repository.assessment(
            observation_id=observation_id,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        )
        is None
    )
    assert is_evidence_eligible_for_downstream_attribution(None) is False

    plan = _register_plan(postgres_engine, plan_id="postgres-followup-auto-ineligible-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-auto-ineligible"),
        cycle_id="cycle-followup-auto-ineligible",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert result.status.value != "FAILED"

    assessment = repository.assessment(
        observation_id=observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert assessment is not None
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (
        assessment.evidence_defect_type is FollowUpEvidenceDefectType.BASELINE_EVIDENCE_INELIGIBLE
    )
    assert is_evidence_eligible_for_downstream_attribution(assessment) is False

    # The retained observation itself: still byte/canonical-hash unchanged --
    # this fix never rewrites historical evidence, contaminated or not.
    after = repository.observation(observation_id)
    assert after is not None
    assert after.canonical_hash == canonical_hash
    assert after.reference_price_at_baseline == Decimal("189.504")


def test_crash_after_observation_commit_before_assessment_is_repaired_next_cycle(
    postgres_engine: Engine,
) -> None:
    """Requirement C. Simulates the exact crash window the preferred
    invariant names: an observation is durably committed (retained here
    directly, standing in for a prior cycle's own ``SyncFollowUpObservations``
    write) but nothing ever assesses it -- confirmed genuinely ``UNASSESSED``
    (fail-closed) first. The *next* natural cycle (a fresh executor call,
    exactly what the real recurring timer does every ~5 minutes) repairs the
    gap with no special-cased recovery logic: the bounded sweep queries
    retained state for "no assessment row yet", so it cannot distinguish
    this scenario from any other unassessed row."""

    _seed_btc_bars(postgres_engine)
    decision_id = _seed_decision(
        postgres_engine, identity="crash-recovery", expected_holding_days=1
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None

    observation = build_crypto_price_movement_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=1,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=DECISION_DAY,
        price_at_baseline=Decimal("100"),
        price_at_baseline_source_observation_id="cfr-bar-decision-day",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="cfr-bar-target-day",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
        policy_version=FOLLOWUP_POLICY_V1,
    )
    was_replay = repository.save_observation(observation=observation)
    assert was_replay is False

    # Pre-condition: the "crash" left this genuinely unassessed.
    assert (
        repository.assessment(
            observation_id=observation.observation_id,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        )
        is None
    )

    plan = _register_plan(postgres_engine, plan_id="postgres-followup-crash-recovery-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    # "Next natural cycle" -- the observation above was never touched by
    # this executor; this is its first call in this test.
    result = executor.execute(
        task=_due_task("cycle-followup-crash-recovery"),
        cycle_id="cycle-followup-crash-recovery",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert result.status.value != "FAILED"

    repaired = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert repaired is not None
    assert repaired.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
    assert repaired.observation_canonical_hash == observation.canonical_hash
    assert is_evidence_eligible_for_downstream_attribution(repaired) is True


def test_assessment_sweep_replay_is_idempotent_and_never_duplicates_or_conflicts(
    postgres_engine: Engine,
) -> None:
    """Requirement D. Three consecutive "natural cycles" (fresh executor
    instances, restart-safe, mirroring this file's own restart-safety test
    above) against an unchanging backlog: the assessment row count is
    stable after the first cycle, no conflict/failure warning ever appears,
    and every replay honestly reports zero new assessments created."""

    _seed_btc_bars(postgres_engine)
    _seed_decision(postgres_engine, identity="assess-replay", expected_holding_days=1)
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-assess-replay-plan-v1")
    as_of = DECISION_DAY + timedelta(days=2)

    first = _executor(postgres_engine, kalshi_source=_FakeRecurringKalshiSource()).execute(
        task=_due_task("cycle-followup-assess-replay-1"),
        cycle_id="cycle-followup-assess-replay-1",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert first.status.value == "COMPLETED"
    assessed_after_first = _row_count(postgres_engine, "followup_evidence_quality_assessment")
    assert assessed_after_first > 0
    assert f"FOLLOWUP_EVIDENCE_QUALITY_ASSESSED:{assessed_after_first}" in first.warnings

    for index in (2, 3):
        replay = _executor(postgres_engine, kalshi_source=_FakeRecurringKalshiSource()).execute(
            task=_due_task(f"cycle-followup-assess-replay-{index}"),
            cycle_id=f"cycle-followup-assess-replay-{index}",
            plan=plan,
            as_of=as_of + timedelta(hours=index),
            executed_at=as_of + timedelta(hours=index),
        )
        assert replay.status.value != "FAILED"
        assert "FOLLOWUP_EVIDENCE_QUALITY_ASSESSED:0" in replay.warnings
        assert not any(
            warning.startswith("FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_SWEEP_FAILED")
            for warning in replay.warnings
        )
        assert (
            _row_count(postgres_engine, "followup_evidence_quality_assessment")
            == assessed_after_first
        )


def test_automatic_assessment_adds_zero_provider_requests_across_both_venues(
    postgres_engine: Engine,
) -> None:
    """Requirement E. The assessment sweep is a pure local read
    (``PostgresCounterfactualFollowUpRepository.unassessed_observations``)
    plus a pure per-row function (``domain.counterfactual_followup.
    assess_followup_evidence_quality``) -- identical read path to the CLI's
    own ``followup-evidence-quality-assess``. Proven against both venues in
    one cycle: the Kalshi call count and the task's own reported
    ``network_requests`` after this cycle equal exactly what
    ``SyncFollowUpObservations`` alone would have cost (2, one per KALSHI
    horizon -- see this file's own restart-safety test), never more --
    CRYPTO assessment costs zero by construction, and KALSHI assessment
    never reads a source at all (Part: ``assess_followup_evidence_quality``
    is always ``ELIGIBLE`` for KALSHI without consulting either evidence
    class)."""

    _seed_btc_bars(postgres_engine)
    crypto_decision = _seed_decision(
        postgres_engine, identity="zero-cost-crypto", expected_holding_days=1
    )
    kalshi_decision = _seed_decision(
        postgres_engine,
        identity="zero-cost-kalshi",
        venue=SharedVenue.KALSHI,
        expected_holding_days=1,
    )
    plan = _register_plan(postgres_engine, plan_id="postgres-followup-zero-cost-plan-v1")
    kalshi_source = _FakeRecurringKalshiSource()
    executor = _executor(postgres_engine, kalshi_source=kalshi_source)
    as_of = DECISION_DAY + timedelta(days=2)

    result = executor.execute(
        task=_due_task("cycle-followup-zero-cost"),
        cycle_id="cycle-followup-zero-cost",
        plan=plan,
        as_of=as_of,
        executed_at=as_of,
    )
    assert result.status.value == "COMPLETED"
    assert kalshi_source.call_count == 2  # exactly sync's own 2 horizon attempts
    assert result.network_requests == 2  # assessment adds nothing to this count

    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    crypto_observations = repository.observations_for_decision(crypto_decision)
    kalshi_observations = repository.observations_for_decision(kalshi_decision)
    assert crypto_observations and kalshi_observations
    for item in (*crypto_observations, *kalshi_observations):
        assessment = repository.assessment(
            observation_id=item.observation_id,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        )
        assert assessment is not None
        assert assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE

    # Re-confirm after assessment ran: no further Kalshi cost was attributed
    # to it, even retroactively.
    assert kalshi_source.call_count == 2
