"""PostgreSQL persistence proofs for Operator Console Follow-Up Performance
v1.

``GET /api/v1/operator-console/followups`` (``operator_api.py``) used to
call ``application.counterfactual_followup.PlanFollowUpObservations.
execute()`` against the real, unwrapped repositories -- measured against
the real retained production corpus (521 decisions, 1666 V2-policy
candidate pairs) at ~103 wall-clock seconds and 1178 SQL statements for one
GET, dominated by a CRYPTO baseline-availability check that re-fetched and
re-deserialized the entire retained ``crypto_product_observation`` table
once per currently-DUE CRYPTO candidate (measured: 648 times) with zero
caching.

The fix adds three small, request-scoped dependency wrappers in
``operator_api.py`` (``_PrefetchedDecisions``, ``_PrefetchedProjections``,
``_MemoizedCryptoUniverseObservations``) plus one batched read
(``PostgresSharedOpportunityRepository.projections_by_id``) and one
backward-compatible optional-prefetch parameter pair on
``PostgresCounterfactualFollowUpRepository.coverage()``. Every one of
these is purely additive: ``PlanFollowUpObservations`` itself,
``_CryptoFollowUpPriceSource.daily_close()``/``latest_close_before()``,
``SyncFollowUpObservations``, ``AssessFollowUpEvidenceQuality``, and the
recurring ``COUNTERFACTUAL_FOLLOWUP_REFRESH`` task branch in
``adapters.operating_cycle_tasks`` all receive zero line changes anywhere
in this milestone.

These tests exercise the real, unmodified ``PlanFollowUpObservations``/
``_CryptoFollowUpPriceSource`` classes throughout -- only the dependency
objects fed into them differ -- and prove:

  A. the wrapped construction never calls the one genuinely expensive
     dependency (``PostgresCryptoUniverseRepository.observations()``) more
     than once per instance, regardless of how many decisions/candidates
     exist (a deterministic call-COUNT bound, not a flaky wall-clock one);
  B/E. wrapped and unwrapped dependency construction produce byte-identical
     ``FollowUpPlan`` output -- same entries, same state_counts, same
     PERMANENTLY_UNAVAILABLE/DUE/NOT_YET_DUE classification;
  G. nothing this milestone added ever mutates retained evidence;
  H. the read-model behaves correctly with zero, sparse, and moderate
     multi-decision/multi-product state.

A genuine 521-decision reproduction is not something a persistence test
should reseed (far too slow, and it would just be restating the real
production database) -- that evidence lives in the Operator Console
Follow-Up Performance v1 investigation itself (bounded reproduction/
measurement scripts run directly against the real retained ``apex``
database, read-only, documented in that milestone's final report).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
    _CryptoFollowUpPriceSource,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.counterfactual_followup import (
    PlanFollowUpObservations,
    plan_current_followup_selection,
)
from apex.application.counterfactual_ledger import SyncCounterfactualLedgerFromSharedOpportunity
from apex.domain.crypto_universe import CryptoDailyObservation, CryptoProductObservation
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)
from apex.domain.values import content_digest
from apex.operator_api import (
    _MemoizedCryptoUniverseObservations,
    _PrefetchedDecisions,
    _PrefetchedProjections,
)

pytestmark = pytest.mark.postgres

DECISION_DAY = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)


def _projection(identity: str, **overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": f"opv-proj-{identity}",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "FixtureOpportunity",
        "source_record_id": f"opv-source-{identity}",
        "source_canonical_hash": content_digest((identity,)),
        "strategy_or_method_id": "OPV_FIXTURE_V1",
        "instrument_id": identity.upper(),
        "product_or_market_id": f"{identity.upper()}-USD",
        "direction": "HYPOTHETICAL_LONG_SPOT_RESEARCH",
        "generated_at": DECISION_DAY + timedelta(hours=12),
        "valid_until": DECISION_DAY + timedelta(days=200),
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
        "supporting_evidence_ids": (f"opv-evidence-{identity}",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _seed_decision(
    engine: Engine, *, identity: str, evaluated_at: datetime, **projection_overrides: object
) -> tuple[str, SharedOpportunityProjection]:
    """Seed one real, retained ``CounterfactualDecision`` via a genuine
    SharedOpportunityProjection/eligibility-set sync -- the production path
    -- and return ``(decision_id, projection)``. Mirrors ``tests.
    persistence.test_postgres_counterfactual_followup._seed_decision``,
    parametrized on ``product_or_market_id``/venue via
    ``projection_overrides`` so this file can seed several distinct CRYPTO
    products and a KALSHI decision."""

    from apex.domain.counterfactual_ledger import build_decision_from_eligibility

    projection = _projection(identity, **projection_overrides)
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"opv-set-{identity}", created_at=evaluated_at, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"opv-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    ledger = PostgresCounterfactualLedgerRepository(engine)
    result = SyncCounterfactualLedgerFromSharedOpportunity(
        ledger, ledger.shared_opportunity
    ).execute(evaluated_at=evaluated_at)
    assert result.decision_ids
    decision_id = build_decision_from_eligibility(
        decision=decisions[0],
        projection=projection,
        opportunity_set_id=opportunity_set.opportunity_set_id,
    ).decision_id
    return decision_id, projection


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
        interval_start=DECISION_DAY - timedelta(days=3),
        interval_end=DECISION_DAY + timedelta(days=200),
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


def _wrapped_plan(engine: Engine, *, decisions, as_of: datetime, already_observed: set[str]):
    """Exactly the construction ``operator_api.operator_console_followups``
    now uses -- real ``PlanFollowUpObservations``, real
    ``_CryptoFollowUpPriceSource``, only the three dependency objects
    swapped for request-scoped prefetch/memoizing wrappers."""

    shared_opportunity_repository = PostgresSharedOpportunityRepository(engine)
    crypto_universe_repository = PostgresCryptoUniverseRepository(engine)
    projections_by_id = shared_opportunity_repository.projections_by_id(
        {item.subject_projection_id for item in decisions}
    )
    # Counterfactual Forward Outcome Temporal Integrity v1: the route plans
    # the whole current per-venue *selection* now (one bounded read-only
    # plan per distinct version, each scoped to its own venues), not a
    # single version -- so this ground-truth comparison must too.  Planning
    # under one version alone would silently drop every venue that version
    # does not cover, which is exactly the hiding this helper exists to
    # catch.
    return plan_current_followup_selection(
        PlanFollowUpObservations(
            _PrefetchedDecisions(decisions),
            _PrefetchedProjections(projections_by_id),
            _CryptoFollowUpPriceSource(
                _MemoizedCryptoUniverseObservations(crypto_universe_repository)
            ),
        ),
        as_of=as_of,
        already_observed=already_observed,
    )


def _unwrapped_plan(engine: Engine, *, as_of: datetime, already_observed: set[str]):
    """The real, unmodified construction this route used before this
    milestone -- kept only as the ground truth this file's consistency
    tests compare the wrapped construction against."""

    repository = PostgresCounterfactualFollowUpRepository(engine)
    return plan_current_followup_selection(
        PlanFollowUpObservations(repository.ledger, repository.subjects, repository.crypto_prices),
        as_of=as_of,
        already_observed=already_observed,
    )


# --------------------------------------------------------------------------
# B/E: wrapped and unwrapped dependency construction are byte-identical.
# --------------------------------------------------------------------------


def test_wrapped_dependencies_produce_byte_identical_plan_to_the_real_unwrapped_repositories(
    postgres_engine: Engine,
) -> None:
    """Seeds a genuinely mixed backlog -- one CRYPTO decision with a
    retained, resolvable price history (DUE and resolvable), one CRYPTO
    decision with zero retained price history at all (DUE and provably
    PERMANENTLY_UNAVAILABLE), one CRYPTO decision evaluated too recently
    for its STRATEGY_HORIZON to be due yet (NOT_YET_DUE), and one KALSHI
    decision -- and asserts the wrapped-dependency
    ``PlanFollowUpObservations`` run reports the *exact* same
    ``FollowUpPlan`` (every entry, every state count) as the real,
    unwrapped repositories. This is the master proof that the performance
    fix changed nothing observable."""

    engine = postgres_engine
    crypto_universe = PostgresCryptoUniverseRepository(engine)

    resolvable_id, _ = _seed_decision(
        engine,
        identity="resolvable",
        evaluated_at=DECISION_DAY,
        product_or_market_id="RESOLV-USD",
        instrument_id="RESOLV",
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="opv-resolvable-bar",
            product_id="RESOLV-USD",
            # latest_close_before()'s boundary here is completed_utc_day
            # (decision.evaluated_at) == DECISION_DAY itself -- a candle
            # must be strictly BEFORE that (hindsight-leakage guard) and
            # its own batch must have completed_at <= boundary too, so
            # this is deliberately dated the day before, not DECISION_DAY.
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="100", volume="1"
                ),
            ),
            completed_at=DECISION_DAY - timedelta(hours=1),
        )
    )

    unavailable_id, _ = _seed_decision(
        engine,
        identity="unavailable",
        evaluated_at=DECISION_DAY,
        product_or_market_id="GONE-USD",
        instrument_id="GONE",
    )
    # Deliberately zero crypto observations retained for GONE-USD at all --
    # latest_close_before() must return None for every boundary, which
    # PlanFollowUpObservations reclassifies DUE -> PERMANENTLY_UNAVAILABLE.

    not_yet_due_id, _ = _seed_decision(
        engine,
        identity="notyetdue",
        evaluated_at=DECISION_DAY + timedelta(days=40),
        product_or_market_id="LATE-USD",
        instrument_id="LATE",
    )

    kalshi_id, _ = _seed_decision(
        engine,
        identity="kalshi",
        evaluated_at=DECISION_DAY,
        venue=SharedVenue.KALSHI,
        instrument_id="KALSHI-MKT",
        product_or_market_id="KALSHI-MKT-1",
    )

    ledger = PostgresCounterfactualLedgerRepository(engine)
    decisions = ledger.decisions()
    assert {item.decision_id for item in decisions} == {
        resolvable_id,
        unavailable_id,
        not_yet_due_id,
        kalshi_id,
    }

    as_of = DECISION_DAY + timedelta(days=45)
    wrapped = _wrapped_plan(engine, decisions=decisions, as_of=as_of, already_observed=set())
    unwrapped = _unwrapped_plan(engine, as_of=as_of, already_observed=set())

    assert wrapped == unwrapped
    assert wrapped.considered_decisions == 4
    assert wrapped.considered_pairs == unwrapped.considered_pairs

    state_by_decision: dict[str, set[str]] = {}
    for entry in wrapped.entries:
        state_by_decision.setdefault(entry.decision_id, set()).add(entry.state.value)

    assert "DUE" in state_by_decision[resolvable_id]
    assert "PERMANENTLY_UNAVAILABLE" in state_by_decision[unavailable_id]
    assert "NOT_YET_DUE" in state_by_decision[not_yet_due_id]
    assert state_by_decision[kalshi_id] <= {"DUE", "NOT_YET_DUE"}


# --------------------------------------------------------------------------
# A: deterministic, non-flaky call-count bound (never a wall-clock assertion).
# --------------------------------------------------------------------------


def test_memoized_crypto_universe_observations_fetches_the_real_repository_at_most_once(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several CRYPTO decisions, each producing a DUE-and-resolvable
    candidate under a *distinct* product, ``PlanFollowUpObservations``
    would (before this fix) call ``observations()`` once per candidate --
    proven elsewhere to cost ~0.1-0.15s and dominate the real endpoint's
    ~103s. This asserts the real, underlying
    ``PostgresCryptoUniverseRepository.observations()`` is invoked at most
    once through the wrapped construction, regardless of candidate count."""

    engine = postgres_engine
    crypto_universe = PostgresCryptoUniverseRepository(engine)
    product_count = 6
    for index in range(product_count):
        product_id = f"MANY{index}-USD"
        _seed_decision(
            engine,
            identity=f"many{index}",
            evaluated_at=DECISION_DAY,
            product_or_market_id=product_id,
            instrument_id=f"MANY{index}",
        )
        crypto_universe.save_observation(
            _crypto_observation(
                observation_id=f"opv-many-bar-{index}",
                product_id=product_id,
                candles=(
                    CryptoDailyObservation.create(
                        bucket_start=DECISION_DAY - timedelta(days=1), close="10", volume="1"
                    ),
                ),
                completed_at=DECISION_DAY - timedelta(hours=1),
            )
        )

    call_count = 0
    real_observations = PostgresCryptoUniverseRepository.observations

    def counting_observations(self: PostgresCryptoUniverseRepository):
        nonlocal call_count
        call_count += 1
        return real_observations(self)

    monkeypatch.setattr(PostgresCryptoUniverseRepository, "observations", counting_observations)

    ledger = PostgresCounterfactualLedgerRepository(engine)
    decisions = ledger.decisions()
    assert len(decisions) == product_count
    as_of = DECISION_DAY + timedelta(days=45)
    plan = _wrapped_plan(engine, decisions=decisions, as_of=as_of, already_observed=set())

    # Every seeded decision produced at least one DUE CRYPTO candidate that
    # required a crypto-availability check -- proving this is a genuine,
    # non-trivial N, not an N=0/1 case that would pass this bound trivially.
    due_crypto_candidates = sum(
        1 for entry in plan.entries if entry.state.value == "DUE" and entry.venue.value == "CRYPTO"
    )
    assert due_crypto_candidates >= product_count
    assert call_count <= 1, (
        f"expected the memoized wrapper to fetch the real repository at most "
        f"once regardless of {due_crypto_candidates} DUE CRYPTO candidates; "
        f"observed {call_count} real fetches"
    )


def test_projections_by_id_prefetch_replaces_the_per_decision_projection_n_plus_one(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With several decisions, the route's own new call pattern
    (``projections_by_id`` once, feeding ``_PrefetchedProjections``) must
    call the real per-id ``PostgresSharedOpportunityRepository.
    projection()`` zero times -- every lookup is answered from the one
    batched prefetch instead."""

    engine = postgres_engine
    decision_count = 5
    for index in range(decision_count):
        _seed_decision(
            engine,
            identity=f"proj{index}",
            evaluated_at=DECISION_DAY,
            product_or_market_id=f"PROJ{index}-USD",
            instrument_id=f"PROJ{index}",
        )

    per_id_call_count = 0
    real_projection = PostgresSharedOpportunityRepository.projection

    def counting_projection(self: PostgresSharedOpportunityRepository, projection_id: str):
        nonlocal per_id_call_count
        per_id_call_count += 1
        return real_projection(self, projection_id)

    monkeypatch.setattr(PostgresSharedOpportunityRepository, "projection", counting_projection)

    ledger = PostgresCounterfactualLedgerRepository(engine)
    decisions = ledger.decisions()
    assert len(decisions) == decision_count
    plan = _wrapped_plan(
        engine, decisions=decisions, as_of=DECISION_DAY + timedelta(days=45), already_observed=set()
    )

    assert plan.considered_decisions == decision_count
    assert per_id_call_count == 0


# --------------------------------------------------------------------------
# H: correctness with no observations, sparse state, and moderate scale.
# --------------------------------------------------------------------------


def test_wrapped_plan_handles_zero_decisions(postgres_engine: Engine) -> None:
    engine = postgres_engine
    plan = _wrapped_plan(engine, decisions=(), as_of=DECISION_DAY, already_observed=set())
    assert plan.considered_decisions == 0
    assert plan.considered_pairs == 0
    assert plan.entries == ()
    assert plan.state_counts == ()


def test_wrapped_plan_handles_sparse_mixed_state(postgres_engine: Engine) -> None:
    engine = postgres_engine
    kalshi_id, _ = _seed_decision(
        engine,
        identity="sparse-kalshi",
        evaluated_at=DECISION_DAY,
        venue=SharedVenue.KALSHI,
        instrument_id="SPARSE-MKT",
        product_or_market_id="SPARSE-MKT-1",
    )
    crypto_id, _ = _seed_decision(
        engine,
        identity="sparse-crypto",
        evaluated_at=DECISION_DAY,
        product_or_market_id="SPARSE-USD",
        instrument_id="SPARSE",
    )
    ledger = PostgresCounterfactualLedgerRepository(engine)
    decisions = ledger.decisions()
    assert {item.decision_id for item in decisions} == {kalshi_id, crypto_id}
    plan = _wrapped_plan(
        engine, decisions=decisions, as_of=DECISION_DAY + timedelta(days=45), already_observed=set()
    )
    assert plan.considered_decisions == 2
    assert plan.considered_pairs > 0
    venues = {entry.venue.value for entry in plan.entries}
    assert venues == {"CRYPTO", "KALSHI"}


def test_wrapped_plan_matches_unwrapped_at_moderate_multi_decision_multi_product_scale(
    postgres_engine: Engine,
) -> None:
    """A bounded proxy for the real 521-decision/100-product corpus (which
    a persistence test cannot reasonably reseed): ~18 decisions across
    CRYPTO (several distinct products, some resolvable, some genuinely
    unavailable) and KALSHI, proving the byte-identical-output guarantee
    still holds as scale grows, not only at the trivial single-decision
    case above."""

    engine = postgres_engine
    crypto_universe = PostgresCryptoUniverseRepository(engine)
    decision_count = 18
    for index in range(decision_count):
        if index % 3 == 0:
            _seed_decision(
                engine,
                identity=f"scale-kalshi{index}",
                evaluated_at=DECISION_DAY,
                venue=SharedVenue.KALSHI,
                instrument_id=f"SCALEMKT{index}",
                product_or_market_id=f"SCALEMKT{index}-1",
            )
            continue
        product_id = f"SCALE{index}-USD"
        _seed_decision(
            engine,
            identity=f"scale{index}",
            evaluated_at=DECISION_DAY,
            product_or_market_id=product_id,
            instrument_id=f"SCALE{index}",
        )
        if index % 2 == 0:
            crypto_universe.save_observation(
                _crypto_observation(
                    observation_id=f"opv-scale-bar-{index}",
                    product_id=product_id,
                    candles=(
                        CryptoDailyObservation.create(
                            bucket_start=DECISION_DAY - timedelta(days=1), close="10", volume="1"
                        ),
                    ),
                    completed_at=DECISION_DAY - timedelta(hours=1),
                )
            )
        # else: zero retained bars for this product -> provably PERMANENTLY_UNAVAILABLE

    ledger = PostgresCounterfactualLedgerRepository(engine)
    decisions = ledger.decisions()
    assert len(decisions) == decision_count
    as_of = DECISION_DAY + timedelta(days=45)
    wrapped = _wrapped_plan(engine, decisions=decisions, as_of=as_of, already_observed=set())
    unwrapped = _unwrapped_plan(engine, as_of=as_of, already_observed=set())

    assert wrapped == unwrapped
    assert wrapped.considered_decisions == decision_count
    state_counts = dict(wrapped.state_counts)
    assert state_counts.get("PERMANENTLY_UNAVAILABLE", 0) > 0
    assert state_counts.get("DUE", 0) > 0


# --------------------------------------------------------------------------
# G: nothing this milestone's new read path added can mutate retained
# evidence.
# --------------------------------------------------------------------------


def test_no_write_reachable_through_the_new_read_model_dependencies(
    postgres_engine: Engine,
) -> None:
    engine = postgres_engine
    crypto_universe = PostgresCryptoUniverseRepository(engine)
    decision_id, _ = _seed_decision(
        engine,
        identity="writecheck",
        evaluated_at=DECISION_DAY,
        product_or_market_id="WRITECHECK-USD",
        instrument_id="WRITECHECK",
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="opv-writecheck-bar",
            product_id="WRITECHECK-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="10", volume="1"
                ),
            ),
            completed_at=DECISION_DAY - timedelta(hours=1),
        )
    )

    tables = (
        "counterfactual_decision",
        "decision_followup_observation",
        "followup_evidence_quality_assessment",
        "followup_scan_cursor",
        "crypto_product_observation",
        "shared_opportunity_projection",
    )
    with engine.connect() as connection:
        before = {
            table: connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in tables
        }

    ledger = PostgresCounterfactualLedgerRepository(engine)
    followup_repository = PostgresCounterfactualFollowUpRepository(engine)
    decisions = ledger.decisions()
    observations = followup_repository.observations()
    as_of = DECISION_DAY + timedelta(days=45)
    plan = _wrapped_plan(engine, decisions=decisions, as_of=as_of, already_observed=set())
    coverage = followup_repository.coverage(decisions=decisions, observations=observations)

    assert decision_id in {item.decision_id for item in decisions}
    assert plan.considered_decisions == 1
    assert coverage["decision_count"] == 1

    with engine.connect() as connection:
        after = {
            table: connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in tables
        }
    assert after == before
