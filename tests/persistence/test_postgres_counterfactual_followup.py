"""PostgreSQL persistence proofs for Counterfactual Decision Follow-Up
Observation v1: idempotent save, tamper/conflict detection, foreign-key
integrity against ``counterfactual_decision``, append-only immutability,
CHECK-constraint enforcement of the venue/observation-type pairing, genuine
local retained-bar reads via ``PostgresCryptoUniverseRepository``, coverage
diagnostics, restart-safe end-to-end sync against the real repository, and
migration ``0096_counterfactual_followup``'s own downgrade guard.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
    evidence_quality_table,
    observation_table,
)
from apex.adapters.counterfactual_ledger_postgres import PostgresCounterfactualLedgerRepository
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.application.counterfactual_followup import (
    AssessFollowUpEvidenceQuality,
    CryptoPriceLookup,
    CryptoPriceSource,
    FollowUpScanPosition,
    FollowUpSourceUnavailable,
    KalshiMarketRead,
    PlanFollowUpObservations,
    SyncFollowUpObservations,
)
from apex.application.counterfactual_ledger import SyncCounterfactualLedgerFromSharedOpportunity
from apex.domain.counterfactual_followup import (
    CRYPTO_DAILY_CLOSE_LIMITATION,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    FOLLOWUP_POLICY_V3,
    CounterfactualFollowUpError,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    FollowUpObservationType,
    assess_followup_evidence_quality,
    build_crypto_price_movement_observation,
    followup_observation_canonical_hash,
    followup_observation_id,
)
from apex.domain.crypto_universe import CryptoDailyObservation, CryptoProductObservation
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


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _projection(identity: str, **overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": f"cff-proj-{identity}",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "FixtureOpportunity",
        "source_record_id": f"cff-source-{identity}",
        "source_canonical_hash": content_digest((identity,)),
        "strategy_or_method_id": "CFF_FIXTURE_V1",
        "instrument_id": "BTC",
        "product_or_market_id": "BTC-USD",
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
        "supporting_evidence_ids": (f"cff-evidence-{identity}",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _seed_decision(engine: Engine, *, identity: str, **projection_overrides: object) -> str:
    """Seed one real, retained ``CounterfactualDecision`` (via a genuine
    SharedOpportunityProjection/eligibility-set sync, exactly the production
    path) and return its ``decision_id``.

    Final validation gate (FOLLOW-UP STARVATION FIX, HEAD 9ceb508) found and
    fixed a real bug here: ``SyncCounterfactualLedgerFromSharedOpportunity.
    execute`` resyncs *every* retained ``SharedOpportunitySet`` on each call
    -- it has no ``evaluated_at``-scoped filter of its own; that parameter
    only stamps the returned result, exactly like its production
    recurring-cycle caller already relies on for idempotent full replay --
    so its own returned ``decision_ids`` accumulates every decision ever
    seeded in this database, not only this call's.  Returning ``decision_
    ids[0]`` therefore silently kept returning the *first* decision ever
    seeded once ``_seed_decision`` was called more than once against the
    same database -- proven the hard way: two tests that each seed several
    decisions specifically to distinguish them from each other (``test_
    sync_candidate_scan_limit_below_permanent_prefix_still_reaches_later_
    work_via_cursor`` and ``test_wrap_around_examines_true_oldest_
    candidates_within_a_single_cycle``) silently aliased every seeded
    decision to the same id before this fix, which read as the fix's own
    cursor never making progress.  Deriving this call's own decision_id
    directly via the same pure ``build_decision_from_eligibility`` the sync
    itself calls internally -- rather than trusting position ``[0]`` of its
    whole-database-accumulated result -- is exact and call-specific
    regardless of how many decisions already exist; ``decisions[0]`` below
    is unambiguous because ``build_opportunity_set`` is always called here
    with exactly one projection, so it always returns exactly one matching
    ``EligibilityDecision`` in the same position."""

    from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
    from apex.domain.counterfactual_ledger import build_decision_from_eligibility

    projection = _projection(identity, **projection_overrides)
    # ``created_at`` becomes the underlying ``EligibilityDecision.evaluated_at``
    # (see ``domain.shared_opportunity.build_opportunity_set``) -- anchor it to
    # ``DECISION_DAY``, not wall-clock ``NOW``, so every due-state computation
    # in this file is deterministic and independent of the real calendar date
    # the test happens to run on.
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"cff-set-{identity}", created_at=DECISION_DAY, projections=(projection,)
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"cff-set-{identity}-bundle",
        opportunity_set=opportunity_set,
        projections=(projection,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    ledger = PostgresCounterfactualLedgerRepository(engine)
    result = SyncCounterfactualLedgerFromSharedOpportunity(
        ledger, ledger.shared_opportunity
    ).execute(evaluated_at=DECISION_DAY)
    assert result.decision_ids
    return build_decision_from_eligibility(
        decision=decisions[0],
        projection=projection,
        opportunity_set_id=opportunity_set.opportunity_set_id,
    ).decision_id


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


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


# --------------------------------------------------------------------------
# save/read round trip, idempotency, conflict detection
# --------------------------------------------------------------------------


def test_save_and_read_round_trip_every_field(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="roundtrip")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    target = DECISION_DAY + timedelta(days=1)
    observation = build_crypto_price_movement_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=1,
        target_time=target,
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=DECISION_DAY,
        price_at_baseline=Decimal("100"),
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    replay = repository.save_observation(observation=observation)
    assert replay is False
    reloaded = repository.observation(observation.observation_id)
    assert reloaded == observation


def test_save_observation_is_idempotent(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="idempotent")
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
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    first = repository.save_observation(observation=observation)
    second = repository.save_observation(observation=observation)
    assert first is False
    assert second is True
    assert _row_count(postgres_engine, "decision_followup_observation") == 1


def test_save_observation_detects_conflict(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="conflict")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    kwargs = dict(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=1,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=DECISION_DAY,
        price_at_baseline=Decimal("100"),
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    original = build_crypto_price_movement_observation(**kwargs)  # type: ignore[arg-type]
    repository.save_observation(observation=original)
    conflicting = build_crypto_price_movement_observation(
        **{**kwargs, "price_at_target": Decimal("999")}  # type: ignore[arg-type]
    )
    assert original.observation_id == conflicting.observation_id
    assert original.canonical_hash != conflicting.canonical_hash
    with pytest.raises(CounterfactualFollowUpError):
        repository.save_observation(observation=conflicting)
    # the original, already-retained row must be untouched
    assert repository.observation(original.observation_id) == original


def test_save_observation_requires_existing_decision(postgres_engine: Engine) -> None:
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    fake_decision_id = "counterfactual_decision_does_not_exist"
    from apex.domain.counterfactual_ledger import CounterfactualDecision, DecisionDisposition

    fabricated = CounterfactualDecision(
        decision_id=fake_decision_id,
        origin="SHARED_OPPORTUNITY_ELIGIBILITY",  # type: ignore[arg-type]
        origin_decision_id="does-not-exist",
        origin_canonical_hash="a" * 64,
        origin_state="REJECTED",
        subject_projection_id="does-not-exist",
        subject_source_record_type="FixtureOpportunity",
        subject_source_record_id="does-not-exist",
        venue=SharedVenue.CRYPTO,
        opportunity_set_id=None,
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=DECISION_DAY,
        disposition=DecisionDisposition.NOT_ADVANCED,
        raw_reason_codes=("EVIDENCE_STALE",),
        explanation="fixture",
        canonical_hash="b" * 64,
        authority="NONE",
    )
    observation = build_crypto_price_movement_observation(
        decision=fabricated,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=1,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        baseline_time=DECISION_DAY,
        price_at_baseline=Decimal("100"),
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    with pytest.raises(CounterfactualFollowUpError):
        repository.save_observation(observation=observation)


# --------------------------------------------------------------------------
# Immutability / CHECK constraints
# --------------------------------------------------------------------------


def test_immutable_trigger_blocks_update(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="immutable")
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
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    repository.save_observation(observation=observation)
    with pytest.raises(Exception, match="immutable"), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.update()
            .where(observation_table.c.observation_id == observation.observation_id)
            .values(canonical_hash="f" * 64)
        )


def test_check_constraint_rejects_venue_observation_type_mismatch(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="mismatch")
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.insert().values(
                observation_id="decision_followup_observation_bogus",
                decision_id=decision_id,
                venue="CRYPTO",
                disposition="NOT_ADVANCED",
                policy_version="FOLLOWUP_POLICY_V1",
                horizon_kind="SHORT_HORIZON",
                baseline_kind="DECISION_TIME",
                observation_type="KALSHI_MARKET_STATUS_AND_PRICE",  # mismatched on purpose
                target_time=NOW,
                observed_at=NOW,
                payload="{}",
                canonical_hash="c" * 64,
                authority="NONE",
            )
        )


# --------------------------------------------------------------------------
# Migration 0097's own baseline_kind CHECK/UNIQUE constraints -- DB-level
# restatements of the domain identity formula, mirroring the venue/
# observation-type mismatch test above exactly.
# --------------------------------------------------------------------------


def test_check_constraint_rejects_invalid_baseline_kind(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="bad-baseline-kind")
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.insert().values(
                observation_id="decision_followup_observation_bogus_baseline",
                decision_id=decision_id,
                venue="CRYPTO",
                disposition="NOT_ADVANCED",
                policy_version="FOLLOWUP_POLICY_V2",
                horizon_kind="SHORT_HORIZON",
                baseline_kind="BOGUS_KIND",
                observation_type="CRYPTO_REFERENCE_PRICE_MOVEMENT",
                target_time=NOW,
                observed_at=NOW,
                payload="{}",
                canonical_hash="c" * 64,
                authority="NONE",
            )
        )


def test_check_constraint_rejects_kalshi_with_origin_evidence_baseline_kind(
    postgres_engine: Engine,
) -> None:
    """DB-level restatement of ``FollowUpBaselineKind``'s own domain
    contract: Apex retains no historical Kalshi quote archive to construct
    an ORIGIN_EVIDENCE reading from, so a KALSHI row can never legitimately
    carry it -- even a caller that bypassed the domain builders entirely
    could not insert one."""

    decision_id = _seed_decision(postgres_engine, identity="kalshi-origin-evidence-guard")
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.insert().values(
                observation_id="decision_followup_observation_kalshi_bogus",
                decision_id=decision_id,
                venue="KALSHI",
                disposition="NOT_ADVANCED",
                policy_version="FOLLOWUP_POLICY_V2",
                horizon_kind="SHORT_HORIZON",
                baseline_kind="ORIGIN_EVIDENCE",
                observation_type="KALSHI_MARKET_STATUS_AND_PRICE",
                target_time=NOW,
                observed_at=NOW,
                payload="{}",
                canonical_hash="c" * 64,
                authority="NONE",
            )
        )


def test_unique_constraint_permits_origin_evidence_and_decision_time_coexistence(
    postgres_engine: Engine,
) -> None:
    """``uq_decision_followup_identity`` widened to include ``baseline_kind``
    (migration 0097): a genuine ORIGIN_EVIDENCE row and a genuine
    DECISION_TIME row for the identical (decision, policy_version,
    horizon_kind) must coexist -- proved directly against real Postgres
    identity/conflict-detection, not just an in-memory fake."""

    decision_id = _seed_decision(postgres_engine, identity="baseline-kind-coexist")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    origin_evidence = _build(
        decision=decision,
        policy_version=FOLLOWUP_POLICY_V2,
        price_at_baseline=Decimal("90"),
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE,
    )
    decision_time = _build(
        decision=decision,
        policy_version=FOLLOWUP_POLICY_V2,
        price_at_baseline=Decimal("95"),
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    assert origin_evidence.observation_id != decision_time.observation_id  # type: ignore[attr-defined]
    assert repository.save_observation(observation=origin_evidence) is False  # type: ignore[arg-type]
    assert repository.save_observation(observation=decision_time) is False  # type: ignore[arg-type]
    history = repository.observations_for_decision(decision_id)
    assert len(history) == 2
    assert {item.baseline_kind for item in history} == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }


# --------------------------------------------------------------------------
# Genuine local retained-bar reads (Part 7/16: zero provider requests)
# --------------------------------------------------------------------------


def test_crypto_price_source_reads_real_retained_bars(postgres_engine: Engine) -> None:
    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    decision_bucket = DECISION_DAY
    target_bucket = DECISION_DAY + timedelta(days=1)
    observation = _crypto_observation(
        observation_id="cff-crypto-observation-reads",
        product_id="ETH-USD",
        candles=(
            CryptoDailyObservation.create(bucket_start=decision_bucket, close="2000", volume="10"),
            CryptoDailyObservation.create(bucket_start=target_bucket, close="2100", volume="12"),
        ),
        completed_at=target_bucket + timedelta(hours=1),
    )
    crypto_repository.save_observation(observation)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    at_decision = repository.crypto_prices.daily_close(
        product_id="ETH-USD", bucket_start=decision_bucket
    )
    at_target = repository.crypto_prices.daily_close(
        product_id="ETH-USD", bucket_start=target_bucket
    )
    missing = repository.crypto_prices.daily_close(
        product_id="ETH-USD", bucket_start=target_bucket + timedelta(days=100)
    )
    assert at_decision is not None and at_decision.close == Decimal("2000")
    assert at_target is not None and at_target.close == Decimal("2100")
    assert missing is None


def test_crypto_price_source_prefers_most_recently_completed_batch(postgres_engine: Engine) -> None:
    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY
    older = _crypto_observation(
        observation_id="cff-crypto-observation-older",
        product_id="SOL-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="10", volume="1"),),
        completed_at=bucket + timedelta(hours=1),
    )
    newer = _crypto_observation(
        observation_id="cff-crypto-observation-newer",
        product_id="SOL-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="11", volume="1"),),
        completed_at=bucket + timedelta(days=5),
    )
    crypto_repository.save_observation(older)
    crypto_repository.save_observation(newer)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.daily_close(product_id="SOL-USD", bucket_start=bucket)
    assert result is not None
    assert result.close == Decimal("11")
    assert result.source_observation_id == "cff-crypto-observation-newer"


# --------------------------------------------------------------------------
# Counterfactual Crypto Follow-Up Point-in-Time Semantics v2:
# ``latest_close_before`` against real retained bars -- the same real-DB
# rigor ``daily_close`` gets above, for the one new query V2's baseline
# resolution depends on.
# --------------------------------------------------------------------------


def test_latest_close_before_selects_prior_day_never_the_boundary_itself(
    postgres_engine: Engine,
) -> None:
    """The hindsight-leakage regression, proved against genuine Postgres
    storage/round-trip: a retained bucket that exists *at* the boundary
    (``before``) must never be selected -- only the latest one strictly
    before it."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    boundary = DECISION_DAY
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-latest-before-prior",
            product_id="ADA-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=boundary - timedelta(days=1), close="0.40", volume="1"
                ),
            ),
            # Genuinely available by ``boundary``: captured shortly after its
            # own (prior) day, not after ``boundary`` itself.
            completed_at=boundary - timedelta(hours=23),
        )
    )
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-latest-before-at-boundary",
            product_id="ADA-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=boundary, close="0.99", volume="1"),
            ),
            completed_at=boundary + timedelta(hours=1),
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(product_id="ADA-USD", before=boundary)
    assert result is not None
    assert result.close == Decimal("0.40")
    assert result.source_observation_id == "cff-latest-before-prior"


def test_latest_close_before_skips_a_multi_day_gap_to_the_true_latest_bucket(
    postgres_engine: Engine,
) -> None:
    """Regression for the provider-lag edge case: if the most recent
    retained bucket is more than one day before the boundary (a real,
    if rare, possibility the pulse's own freshness tolerance permits), V2
    must still find it -- never a hardcoded "boundary minus one day" that
    would silently miss it."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    # boundary is 2 days after the only retained candle (still within
    # ``_crypto_observation``'s fixed [DECISION_DAY-1, DECISION_DAY+61)
    # interval) -- a genuine multi-day gap, not the ideal one-day-back case.
    boundary = DECISION_DAY + timedelta(days=1)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-latest-before-gap",
            product_id="XLM-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="0.12", volume="1"
                ),
            ),
            # Genuinely available well before ``boundary`` (2 days later) --
            # captured shortly after its own day, exactly like a real
            # provider-lag batch would have been.
            completed_at=DECISION_DAY - timedelta(hours=23),
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(product_id="XLM-USD", before=boundary)
    assert result is not None
    assert result.close == Decimal("0.12")


def test_latest_close_before_prefers_most_recently_completed_batch_on_tie(
    postgres_engine: Engine,
) -> None:
    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY - timedelta(days=1)
    older = _crypto_observation(
        observation_id="cff-latest-before-tie-older",
        product_id="LINK-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="8", volume="1"),),
        completed_at=bucket + timedelta(hours=1),
    )
    newer = _crypto_observation(
        observation_id="cff-latest-before-tie-newer",
        product_id="LINK-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="9", volume="1"),),
        # Later than ``older``'s own ``completed_at`` (still resolves the
        # tie in its favor) but still genuinely available by ``before`` --
        # a batch completed *after* ``before`` (even with an old
        # ``bucket_start``) must never win a tie it is not even eligible
        # for (Part: availability-as-of correctness).
        completed_at=bucket + timedelta(hours=2),
    )
    crypto_repository.save_observation(older)
    crypto_repository.save_observation(newer)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(
        product_id="LINK-USD", before=DECISION_DAY
    )
    assert result is not None
    assert result.close == Decimal("9")
    assert result.source_observation_id == "cff-latest-before-tie-newer"


def test_latest_close_before_returns_none_when_nothing_retained_strictly_before(
    postgres_engine: Engine,
) -> None:
    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-latest-before-none",
            product_id="DOGE-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="0.07", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(
        product_id="DOGE-USD", before=DECISION_DAY
    )
    assert result is None


# --------------------------------------------------------------------------
# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1 -- real-Postgres proofs against ``_CryptoFollowUpPriceSource
# .daily_close``/``.latest_close_before``.  Regression letters A/B/C below
# refer to the milestone's own lettered regression-test requirements; this
# is the *exact* shape of the confirmed DOGE-USD incident (a genuine
# ``HISTORICAL_PUBLIC`` batch, a synthetic ``FIXTURE`` "quality repair"
# batch covering the identical bucket, the fixture completed later).
# --------------------------------------------------------------------------


def test_daily_close_genuine_beats_later_completed_fixture_on_tie(
    postgres_engine: Engine,
) -> None:
    """Regression A (``daily_close``): a later-inserted/later-completed
    ``FIXTURE`` batch must never beat an earlier, genuinely eligible
    ``HISTORICAL_PUBLIC`` batch covering the identical bucket."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY
    genuine = _crypto_observation(
        observation_id="cff-eligibility-daily-genuine",
        product_id="ZEC-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="467.04", volume="1"),),
        completed_at=bucket + timedelta(hours=1),
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    fixture = _crypto_observation(
        observation_id="cff-eligibility-daily-fixture",
        product_id="ZEC-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="999.99", volume="1"),),
        # Completed strictly later than ``genuine`` -- exactly the ordering
        # that let a fixture win under the pre-fix "latest completed_at
        # wins, no eligibility precondition" tie-break.
        completed_at=bucket + timedelta(hours=5),
        evidence_class=EvidenceClass.FIXTURE,
    )
    crypto_repository.save_observation(genuine)
    crypto_repository.save_observation(fixture)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.daily_close(product_id="ZEC-USD", bucket_start=bucket)
    assert result is not None
    assert result.close == Decimal("467.04")
    assert result.source_observation_id == "cff-eligibility-daily-genuine"
    assert result.evidence_class is EvidenceClass.HISTORICAL_PUBLIC


def test_latest_close_before_genuine_beats_later_completed_fixture_on_tie(
    postgres_engine: Engine,
) -> None:
    """Regression A (``latest_close_before``) -- the exact confirmed
    DOGE-USD shape: a genuine ``HISTORICAL_PUBLIC`` batch completed at
    T, a synthetic ``FIXTURE`` "quality repair" batch covering the
    identical bucket completed 4m38s later, both otherwise genuinely
    point-in-time-eligible (``bucket_start < before`` and ``completed_at <=
    before``)."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY - timedelta(days=1)
    boundary = DECISION_DAY
    genuine = _crypto_observation(
        observation_id="cff-eligibility-latest-genuine",
        product_id="DOGE-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="0.07196", volume="1"),),
        completed_at=bucket + timedelta(hours=1, minutes=44),
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    fixture = _crypto_observation(
        observation_id="cff-eligibility-latest-fixture",
        product_id="DOGE-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="189.504", volume="1"),),
        completed_at=bucket + timedelta(hours=1, minutes=48, seconds=38),
        evidence_class=EvidenceClass.FIXTURE,
    )
    crypto_repository.save_observation(genuine)
    crypto_repository.save_observation(fixture)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(product_id="DOGE-USD", before=boundary)
    assert result is not None
    assert result.close == Decimal("0.07196")
    assert result.source_observation_id == "cff-eligibility-latest-genuine"
    assert result.evidence_class is EvidenceClass.HISTORICAL_PUBLIC


def test_daily_close_returns_none_when_only_fixture_retained(postgres_engine: Engine) -> None:
    """Regression B (``daily_close``): fails closed -- no genuine candidate
    exists, so no candidate at all, never a silent fixture fallback."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-eligibility-daily-only-fixture",
            product_id="PEPE-USD",
            candles=(CryptoDailyObservation.create(bucket_start=bucket, close="1", volume="1"),),
            completed_at=bucket + timedelta(hours=1),
            evidence_class=EvidenceClass.FIXTURE,
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.daily_close(product_id="PEPE-USD", bucket_start=bucket)
    assert result is None


def test_latest_close_before_returns_none_when_only_fixture_retained(
    postgres_engine: Engine,
) -> None:
    """Regression B (``latest_close_before``): identical fail-closed proof
    for the baseline-side lookup -- the one ``FOLLOWUP_POLICY_V2`` actually
    depends on."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY - timedelta(days=1)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-eligibility-latest-only-fixture",
            product_id="APT-USD",
            candles=(CryptoDailyObservation.create(bucket_start=bucket, close="1", volume="1"),),
            completed_at=bucket + timedelta(hours=1),
            evidence_class=EvidenceClass.FIXTURE,
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(product_id="APT-USD", before=DECISION_DAY)
    assert result is None


def test_latest_close_before_still_selects_eligible_historical_public_with_no_fixture_present(
    postgres_engine: Engine,
) -> None:
    """Regression C: the fix must not become overzealous -- genuinely
    retained ``HISTORICAL_PUBLIC`` evidence, legitimately available by the
    anchor, remains usable exactly as before when no ineligible evidence is
    even present."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY - timedelta(days=1)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-eligibility-latest-clean-historical",
            product_id="ICP-USD",
            candles=(CryptoDailyObservation.create(bucket_start=bucket, close="5.5", volume="1"),),
            completed_at=bucket + timedelta(hours=1),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.latest_close_before(product_id="ICP-USD", before=DECISION_DAY)
    assert result is not None
    assert result.close == Decimal("5.5")
    assert result.evidence_class is EvidenceClass.HISTORICAL_PUBLIC


def test_daily_close_ranks_among_eligible_candidates_only_ignoring_a_later_fixture(
    postgres_engine: Engine,
) -> None:
    """Combines A and C: with two eligible ``HISTORICAL_PUBLIC`` batches and
    one, later-completed, ``FIXTURE`` batch all covering the same bucket,
    the winner must be the most-recently-completed *eligible* one -- the
    fixture must be excluded from ranking entirely, not merely lose a fair
    tie-break against it."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY
    oldest_genuine = _crypto_observation(
        observation_id="cff-eligibility-rank-oldest-genuine",
        product_id="SUI-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="1.10", volume="1"),),
        completed_at=bucket + timedelta(hours=1),
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    newest_genuine = _crypto_observation(
        observation_id="cff-eligibility-rank-newest-genuine",
        product_id="SUI-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="1.12", volume="1"),),
        completed_at=bucket + timedelta(hours=2),
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    latest_fixture = _crypto_observation(
        observation_id="cff-eligibility-rank-latest-fixture",
        product_id="SUI-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="9.99", volume="1"),),
        completed_at=bucket + timedelta(hours=3),
        evidence_class=EvidenceClass.FIXTURE,
    )
    crypto_repository.save_observation(oldest_genuine)
    crypto_repository.save_observation(newest_genuine)
    crypto_repository.save_observation(latest_fixture)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    result = repository.crypto_prices.daily_close(product_id="SUI-USD", bucket_start=bucket)
    assert result is not None
    assert result.close == Decimal("1.12")
    assert result.source_observation_id == "cff-eligibility-rank-newest-genuine"


# --------------------------------------------------------------------------
# End-to-end sync against the real repository (crypto path is fully local;
# Kalshi source is faked to avoid a real network call in this test)
# --------------------------------------------------------------------------


class _FakeKalshiSource:
    def __init__(self) -> None:
        self.call_count = 0

    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        self.call_count += 1
        raise FollowUpSourceUnavailable("NOT_EXERCISED_IN_THIS_TEST")


def test_end_to_end_sync_is_restart_safe_against_real_repository(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="e2e", expected_holding_days=30)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    for offset, price in ((0, "100"), (1, "101"), (30, "150")):
        crypto_universe.save_observation(
            _crypto_observation(
                observation_id=f"cff-e2e-bar-{offset}",
                product_id="BTC-USD",
                candles=(
                    CryptoDailyObservation.create(
                        bucket_start=DECISION_DAY + timedelta(days=offset), close=price, volume="1"
                    ),
                ),
                completed_at=DECISION_DAY + timedelta(days=offset, hours=1),
            )
        )
    as_of = DECISION_DAY + timedelta(days=31)
    plan = PlanFollowUpObservations(
        repository.ledger, repository.subjects, repository.crypto_prices
    ).execute(as_of=as_of, already_observed=set())
    assert dict(plan.state_counts).get("DUE", 0) >= 2

    kalshi_source = _FakeKalshiSource()
    first = SyncFollowUpObservations(
        repository.ledger, repository.subjects, repository.crypto_prices, kalshi_source, repository
    ).execute(as_of=as_of, sample_limit=10)
    assert first.created_count == 2  # SHORT_HORIZON + STRATEGY_HORIZON
    assert kalshi_source.call_count == 0

    # "restart": a brand-new repository/service instance against the same DB
    restarted_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    second = SyncFollowUpObservations(
        restarted_repository.ledger,
        restarted_repository.subjects,
        restarted_repository.crypto_prices,
        _FakeKalshiSource(),
        restarted_repository,
    ).execute(as_of=as_of + timedelta(hours=1), sample_limit=10)
    assert second.created_count == 0
    assert second.skipped_already_observed_count == 2
    assert _row_count(postgres_engine, "decision_followup_observation") == 2

    history = restarted_repository.observations_for_decision(decision_id)
    assert len(history) == 2
    assert {item.horizon_kind for item in history} == {
        FollowUpHorizonKind.SHORT_HORIZON,
        FollowUpHorizonKind.STRATEGY_HORIZON,
    }


def test_coverage_reports_counts_by_venue_and_horizon(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="coverage")
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
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    repository.save_observation(observation=observation)
    coverage = repository.coverage()
    assert coverage["observation_count"] >= 1
    assert coverage["observation_count_by_venue"]["CRYPTO"] >= 1
    assert coverage["observation_count_by_horizon_kind"]["SHORT_HORIZON"] >= 1
    assert coverage["observation_count_by_provenance"]["HISTORICAL_PUBLIC"] >= 1
    # whatever this fixture's genuine eligibility disposition turns out to be
    # (it is not the point of this test -- the point is that coverage()
    # attributes the observation to it at all, matching Part 3's disposition-
    # agnostic coverage).
    assert coverage["observation_count_by_disposition"][decision.disposition.value] >= 1


def test_coverage_accepts_prefetched_decisions_and_observations_and_matches_default_fetch(
    postgres_engine: Engine,
) -> None:
    """Operator Console Follow-Up Performance v1: ``coverage()``'s two new
    optional keyword parameters are purely additive -- calling it with an
    already-fetched ``decisions``/``observations`` tuple must report
    exactly the same breakdown as the original, argument-free call that
    fetches both fresh, and every pre-existing caller (CLI, this file's own
    other tests, the Decision Ledger overview) that never passes them keeps
    its original behavior unchanged."""

    decision_id = _seed_decision(postgres_engine, identity="coverage-prefetch")
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
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    repository.save_observation(observation=observation)

    default_coverage = repository.coverage()
    prefetched_decisions = repository.ledger.all_decisions()
    prefetched_observations = repository.observations()
    prefetched_coverage = repository.coverage(
        decisions=prefetched_decisions, observations=prefetched_observations
    )
    assert prefetched_coverage == default_coverage

    # The parameters are genuinely used, not silently ignored: an explicitly
    # empty prefetch must report an empty breakdown, not fall back to a
    # fresh fetch.
    empty_coverage = repository.coverage(decisions=(), observations=())
    assert empty_coverage["decision_count"] == 0
    assert empty_coverage["observation_count"] == 0
    assert empty_coverage["most_recent_observed_at"] is None
    assert empty_coverage != default_coverage


# --------------------------------------------------------------------------
# Counterfactual Crypto Follow-Up Point-in-Time Semantics v2: real-Postgres
# V1/V2 coexistence, filtering, coverage, and the corrected end-to-end sync.
# --------------------------------------------------------------------------


def _build(
    *,
    decision: object,
    policy_version: str,
    price_at_baseline: Decimal,
    baseline_kind: FollowUpBaselineKind = FollowUpBaselineKind.DECISION_TIME,
) -> object:
    return build_crypto_price_movement_observation(
        decision=decision,  # type: ignore[arg-type]
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=baseline_kind,
        horizon_days=1,
        baseline_time=DECISION_DAY,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        price_at_baseline=price_at_baseline,
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
        policy_version=policy_version,
    )


def test_v1_and_v2_observations_coexist_for_same_decision_real_postgres(
    postgres_engine: Engine,
) -> None:
    """Decision D/V1/SHORT_HORIZON and Decision D/V2/SHORT_HORIZON: proved,
    against real Postgres identity/conflict-detection (not just an in-memory
    fake), to be separate immutable rows that both reference the same
    stable decision -- no identity conflict, neither shadows the other."""

    decision_id = _seed_decision(postgres_engine, identity="v1-v2-coexist")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    v1 = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V1, price_at_baseline=Decimal("100")
    )
    v2 = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V2, price_at_baseline=Decimal("95")
    )
    assert v1.observation_id != v2.observation_id  # type: ignore[attr-defined]
    assert repository.save_observation(observation=v1) is False  # type: ignore[arg-type]
    assert repository.save_observation(observation=v2) is False  # type: ignore[arg-type]

    reloaded_v1 = repository.observation(v1.observation_id)  # type: ignore[attr-defined]
    reloaded_v2 = repository.observation(v2.observation_id)  # type: ignore[attr-defined]
    assert reloaded_v1 == v1
    assert reloaded_v2 == v2
    assert reloaded_v1.decision_id == reloaded_v2.decision_id == decision_id  # type: ignore[union-attr]

    history = repository.observations_for_decision(decision_id)
    assert len(history) == 2
    assert {item.policy_version for item in history} == {FOLLOWUP_POLICY_V1, FOLLOWUP_POLICY_V2}
    assert _row_count(postgres_engine, "decision_followup_observation") == 2


def test_observations_can_be_filtered_by_policy_version(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="policy-filter")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    v1 = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V1, price_at_baseline=Decimal("100")
    )
    v2 = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V2, price_at_baseline=Decimal("95")
    )
    repository.save_observation(observation=v1)  # type: ignore[arg-type]
    repository.save_observation(observation=v2)  # type: ignore[arg-type]

    only_v1 = repository.observations(policy_version=FOLLOWUP_POLICY_V1)
    only_v2 = repository.observations(policy_version=FOLLOWUP_POLICY_V2)
    unfiltered = repository.observations()
    assert {item.observation_id for item in only_v1} == {v1.observation_id}  # type: ignore[attr-defined]
    assert {item.observation_id for item in only_v2} == {v2.observation_id}  # type: ignore[attr-defined]
    assert len(unfiltered) >= 2


def test_coverage_reports_counts_by_policy_version(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="policy-coverage")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    v1 = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V1, price_at_baseline=Decimal("100")
    )
    v2 = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V2, price_at_baseline=Decimal("95")
    )
    repository.save_observation(observation=v1)  # type: ignore[arg-type]
    repository.save_observation(observation=v2)  # type: ignore[arg-type]
    coverage = repository.coverage()
    by_policy = coverage["observation_count_by_policy_version"]
    assert by_policy[FOLLOWUP_POLICY_V1] >= 1
    assert by_policy[FOLLOWUP_POLICY_V2] >= 1
    # This breakdown is historical/cross-version -- decision_count must stay
    # the true decision count, never inflated by how many policy versions
    # have observed it.
    assert coverage["decision_count"] == len(repository.ledger.all_decisions())


def test_end_to_end_sync_v2_uses_corrected_baseline_against_real_retained_bars(
    postgres_engine: Engine,
) -> None:
    """Full, real-Postgres, end-to-end proof of the hindsight-leakage fix:
    seeds a genuine decision (whose ``SharedOpportunityProjection.
    generated_at`` defaults to ``DECISION_DAY + 12h``, so
    ``completed_utc_day(projection.generated_at) == DECISION_DAY``), retains
    a bar at ``DECISION_DAY`` (what FOLLOWUP_POLICY_V1 would have wrongly
    selected) and a distinct one at ``DECISION_DAY - 1`` (the correct
    FOLLOWUP_POLICY_V2 baseline), then runs the real
    ``SyncFollowUpObservations`` orchestration against the real repository
    end to end and asserts the *persisted* rows reflect the corrected
    value -- not merely what the pure builder computes in isolation.

    ``completed_utc_day(decision.evaluated_at) == completed_utc_day(
    projection.generated_at) == DECISION_DAY`` here (gap_days == 0 -- both
    stamped the same fixture day), so ``ORIGIN_EVIDENCE`` and
    ``DECISION_TIME`` resolve identically: 2 horizon kinds x 2 baseline
    kinds = 4 genuinely distinct retained rows (Part: pre-deployment
    staleness-semantics audit), not 2."""

    decision_id = _seed_decision(postgres_engine, identity="v2-e2e", expected_holding_days=1)
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cff-v2-e2e-prior-day",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="90", volume="1"
                ),
            ),
            # Genuinely available by DECISION_DAY (the baseline boundary
            # here, gap_days == 0): captured shortly after its own (prior)
            # day, not after the boundary itself (Part: availability-as-of
            # correctness).
            completed_at=DECISION_DAY - timedelta(hours=23),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cff-v2-e2e-decision-day",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="999", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
        )
    )
    # This is also -- with expected_holding_days=1, so STRATEGY_HORIZON ==
    # SHORT_HORIZON == 1 day here -- exactly the corrected V2 *target*
    # bucket (Part: target-bucket-from-baseline-candle: the baseline
    # candle's own bucket_start, DECISION_DAY - 1, plus horizon_days (1) ==
    # DECISION_DAY).  ``daily_close`` has no availability-as-of constraint
    # of its own (Part: not a hindsight-leakage concern for a forward-
    # looking target read), so this bar's ``completed_at`` above does not
    # need to precede any boundary to be used as the target price.
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cff-v2-e2e-target-day",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY + timedelta(days=1), close="95", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(days=1, hours=1),
        )
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    kalshi_source = _FakeKalshiSource()

    result = SyncFollowUpObservations(
        repository.ledger, repository.subjects, repository.crypto_prices, kalshi_source, repository
    ).execute(
        as_of=DECISION_DAY + timedelta(days=2),
        sample_limit=10,
        policy_version=FOLLOWUP_POLICY_V2,
    )
    assert result.created_count == 4  # 2 horizon kinds x 2 baseline kinds
    assert kalshi_source.call_count == 0

    history = repository.observations_for_decision(decision_id)
    assert len(history) == 4
    assert all(item.policy_version == FOLLOWUP_POLICY_V2 for item in history)
    # Never 999 (the DECISION_DAY bucket a same-day-anchored formula would
    # have picked) -- always 90 (the prior completed day relative to the
    # subject's own generation), for every baseline kind.
    assert all(item.reference_price_at_baseline == Decimal("90") for item in history)
    assert all(item.reference_price_at_baseline != Decimal("999") for item in history)
    # Target-bucket-from-baseline-candle, proved end to end: with
    # horizon_days == 1 for both horizon kinds here, the correct target
    # bucket is the baseline candle's own bucket_start (DECISION_DAY - 1)
    # plus 1 day == DECISION_DAY -- the "decision-day" bar (999), never the
    # "target-day" bar (95, DECISION_DAY + 1) a naive completed_utc_day(
    # target_time) re-derivation would have picked instead (which would
    # have silently compared closes 2 calendar days apart, not 1).
    assert all(item.reference_price_at_target == Decimal("999") for item in history)
    assert all(item.reference_price_at_target != Decimal("95") for item in history)
    assert {item.baseline_kind for item in history} == {
        FollowUpBaselineKind.ORIGIN_EVIDENCE,
        FollowUpBaselineKind.DECISION_TIME,
    }
    assert len({item.observation_id for item in history}) == 4  # all genuinely distinct


# --------------------------------------------------------------------------
# Migration 0096's own downgrade guard
# --------------------------------------------------------------------------


def test_0096_downgrade_succeeds_when_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "decision_followup_observation") == 0
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0095_reason_classification")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0095_reason_classification"
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0096_counterfactual_followup")
        connection.commit()


def test_0096_downgrade_blocks_with_retained_followup_evidence(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="downgrade-guard")
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
        price_at_baseline_source_observation_id="crypto-obs-a",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="crypto-obs-b",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    repository.save_observation(observation=observation)

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0096 with retained follow-up observation evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0095_reason_classification")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0096_counterfactual_followup"
    assert _row_count(postgres_engine, "decision_followup_observation") == 1


# --------------------------------------------------------------------------
# Migration 0097's own downgrade guard (Counterfactual Crypto Follow-Up
# Point-in-Time Semantics v2): symmetric with 0096's above, scoped to
# exactly what 0097 widened -- a retained non-V1 row blocks; V1-only (or
# empty) does not.
# --------------------------------------------------------------------------


def test_0097_downgrade_succeeds_when_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "decision_followup_observation") == 0
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0096_counterfactual_followup")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0096_counterfactual_followup"
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0097_followup_policy_v2")
        connection.commit()


def test_0097_downgrade_succeeds_with_only_v1_evidence_retained(postgres_engine: Engine) -> None:
    """The narrower constraint 0097's downgrade restores still accepts
    FOLLOWUP_POLICY_V1 -- a database that has not yet used V2 at all
    downgrades cleanly, exactly like any other additive migration."""

    decision_id = _seed_decision(postgres_engine, identity="downgrade-v1-only")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    observation = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V1, price_at_baseline=Decimal("100")
    )
    repository.save_observation(observation=observation)  # type: ignore[arg-type]

    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0096_counterfactual_followup")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0096_counterfactual_followup"
    assert _row_count(postgres_engine, "decision_followup_observation") == 1

    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0097_followup_policy_v2")
        connection.commit()


def test_0097_downgrade_blocks_with_retained_v2_evidence(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="downgrade-v2-guard")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    observation = _build(
        decision=decision, policy_version=FOLLOWUP_POLICY_V2, price_at_baseline=Decimal("95")
    )
    repository.save_observation(observation=observation)  # type: ignore[arg-type]

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0097 with retained non-V1 follow-up policy evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0096_counterfactual_followup")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0097_followup_policy_v2"


# --------------------------------------------------------------------------
# Migration 0098's own downgrade (Counterfactual Follow-Up Long-Run
# Fairness v1): unlike 0096/0097 above, no "refuse if evidence would be
# lost" guard is needed or present -- ``followup_scan_cursor`` holds no
# evidence, only a resume pointer that is always safe to lose (see the
# migration's own docstring) -- so a downgrade succeeds unconditionally,
# even with a genuinely retained cursor row.
# --------------------------------------------------------------------------


def test_0098_downgrade_succeeds_with_a_retained_cursor(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="downgrade-cursor")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    repository.save_scan_position(
        policy_version=FOLLOWUP_POLICY_V2,
        position=FollowUpScanPosition(
            decision_id=decision_id,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON.value,
            baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
        ),
    )
    assert _row_count(postgres_engine, "followup_scan_cursor") == 1

    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0097_followup_policy_v2")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        table_names = set(sa.inspect(connection).get_table_names())
    assert current_revision == "0097_followup_policy_v2"
    assert "followup_scan_cursor" not in table_names

    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0098_followup_scan_cursor")
        connection.commit()


# --------------------------------------------------------------------------
# Counterfactual Follow-Up Long-Run Fairness v1: the durable scan cursor,
# proven directly against real Postgres persistence -- basic round trip,
# restart safety, and (the property that actually matters) genuine bounded
# progress toward a later-satisfiable pair across repeated, restart-safe
# cycles when candidate_scan_limit is deliberately smaller than the
# oldest-sorting permanently-blocked prefix.
# --------------------------------------------------------------------------


def test_scan_position_round_trips_and_survives_a_fresh_repository_instance(
    postgres_engine: Engine,
) -> None:
    """``FollowUpScanPosition`` is durable orchestration bookkeeping, not
    evidence -- proven directly against real Postgres: absent before any
    write, upserted idempotently (a second save overwrites, never
    duplicates or conflicts), scoped independently per ``policy_version``,
    and -- the actually load-bearing property for unattended operation --
    read back correctly by a brand-new repository instance against the same
    database, exactly the "restart" shape every other restart-safety proof
    in this file already covers for genuine observations."""

    decision_id = _seed_decision(postgres_engine, identity="cursor-basic")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) is None

    first_position = FollowUpScanPosition(
        decision_id=decision_id,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
    )
    repository.save_scan_position(policy_version=FOLLOWUP_POLICY_V2, position=first_position)
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) == first_position
    # A different policy version's cursor is completely independent -- an ad
    # hoc FOLLOWUP_POLICY_V1 sync must never advance or be blocked by the
    # recurring cycle's own FOLLOWUP_POLICY_V2 cursor, or vice versa.
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V1) is None

    # Upsert: a second save for the same policy_version overwrites in place.
    second_position = FollowUpScanPosition(
        decision_id=decision_id,
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.ORIGIN_EVIDENCE.value,
    )
    repository.save_scan_position(policy_version=FOLLOWUP_POLICY_V2, position=second_position)
    assert _row_count(postgres_engine, "followup_scan_cursor") == 1
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) == second_position

    # "Restart": a brand-new repository instance against the same database.
    restarted = PostgresCounterfactualFollowUpRepository(postgres_engine)
    assert restarted.scan_position(policy_version=FOLLOWUP_POLICY_V2) == second_position


def test_scan_cursor_diagnostics_reads_the_identical_row_plus_updated_at(
    postgres_engine: Engine,
) -> None:
    """The Operator Console's own read-only diagnostic surface: the exact
    same ``followup_scan_cursor`` row ``scan_position`` reads, additionally
    carrying ``updated_at`` -- proven, against real Postgres, to (a) report
    an honest ``None`` before any write, (b) match ``scan_position`` on
    every field the two share after a save, and (c) never write anything
    itself (a second read is byte-for-byte identical, and the write-path
    ``scan_position``/``save_scan_position`` pair remain completely
    unaffected by calling it)."""

    decision_id = _seed_decision(postgres_engine, identity="cursor-diagnostics")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    assert repository.scan_cursor_diagnostics(policy_version=FOLLOWUP_POLICY_V2) is None

    position = FollowUpScanPosition(
        decision_id=decision_id,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
    )
    before_save = datetime.now(UTC)
    repository.save_scan_position(policy_version=FOLLOWUP_POLICY_V2, position=position)
    after_save = datetime.now(UTC)

    diagnostics = repository.scan_cursor_diagnostics(policy_version=FOLLOWUP_POLICY_V2)
    assert diagnostics is not None
    assert diagnostics["decision_id"] == position.decision_id
    assert diagnostics["horizon_kind"] == position.horizon_kind
    assert diagnostics["baseline_kind"] == position.baseline_kind
    assert before_save <= diagnostics["updated_at"] <= after_save

    # A different, never-written policy version stays genuinely absent.
    assert repository.scan_cursor_diagnostics(policy_version=FOLLOWUP_POLICY_V1) is None

    # Read-only: calling it never mutates the row `scan_position` reads.
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) == position
    assert _row_count(postgres_engine, "followup_scan_cursor") == 1
    repeat = repository.scan_cursor_diagnostics(policy_version=FOLLOWUP_POLICY_V2)
    assert repeat == diagnostics


def test_sync_candidate_scan_limit_below_permanent_prefix_still_reaches_later_work_via_cursor(
    postgres_engine: Engine,
) -> None:
    """The required long-run fairness invariant, proven end-to-end against
    real Postgres persistence (not only the in-memory fakes in ``tests/
    test_counterfactual_followup.py``): five permanently-blocked CRYPTO
    decisions (20 candidates, zero retained BTC-USD evidence at all) sort
    before one genuinely resolvable CRYPTO decision (a different product,
    ETH-USD) -- with ``candidate_scan_limit=4`` (deliberately smaller than
    the 20-candidate permanent prefix), reaching the resolvable decision
    requires several cycles.  Each cycle here uses a brand-new repository
    instance (a genuine "restart" every time, the least favorable case for
    an in-memory-only cursor) to prove the durable cursor -- not merely an
    in-process variable -- is what makes this converge."""

    permanent_ids = tuple(
        _seed_decision(
            postgres_engine, identity=f"cursor-perm-{index:02d}", expected_holding_days=1
        )
        for index in range(5)
    )
    resolvable_id = _seed_decision(
        postgres_engine,
        identity="cursor-resolvable",
        expected_holding_days=1,
        instrument_id="ETH",
        product_or_market_id="ETH-USD",
    )
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cursor-eth-baseline",
            product_id="ETH-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="50", volume="1"
                ),
            ),
            completed_at=DECISION_DAY - timedelta(hours=1),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="cursor-eth-target",
            product_id="ETH-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="53", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
        )
    )
    # No BTC-USD evidence retained anywhere -- every permanent decision's
    # baseline lookup returns None unconditionally.

    total_candidates = 5 * 4 + 4  # 5 permanent decisions + 1 resolvable, x4 pairs each
    candidate_scan_limit = 4  # == every decision's own candidate width here
    assert candidate_scan_limit < 5 * 4  # smaller than the permanent prefix alone
    as_of = DECISION_DAY + timedelta(days=2)

    resolvable_reached_at_cycle: int | None = None
    for cycle in range(1, 16):
        cycle_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
        result = SyncFollowUpObservations(
            cycle_repository.ledger,
            cycle_repository.subjects,
            cycle_repository.crypto_prices,
            _FakeKalshiSource(),
            cycle_repository,
        ).execute(
            as_of=as_of + timedelta(hours=cycle),
            sample_limit=100,
            candidate_scan_limit=candidate_scan_limit,
            policy_version=FOLLOWUP_POLICY_V2,
        )
        assert result.candidates_examined == candidate_scan_limit
        # Wait for *every* one of the resolvable decision's 4 pairs, not
        # merely the first -- candidate_scan_limit need not always align to
        # a decision boundary in general, so stopping on the first sighting
        # could break out mid-decision and assert a count that can never
        # complete (nothing loops back to finish it).
        if len(cycle_repository.observations_for_decision(resolvable_id)) == 4:
            resolvable_reached_at_cycle = cycle
            break

    assert resolvable_reached_at_cycle is not None, (
        "resolvable decision was never fully reached across 15 restart-safe cycles"
    )
    # One full lap over every candidate takes at most
    # ceil(total_candidates / candidate_scan_limit) cycles -- reaching the
    # resolvable decision can never take longer than that.
    assert resolvable_reached_at_cycle <= math.ceil(total_candidates / candidate_scan_limit)

    final_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    resolvable_observations = final_repository.observations_for_decision(resolvable_id)
    assert len(resolvable_observations) == 4
    assert all(
        item.reference_price_at_baseline == Decimal("50") for item in resolvable_observations
    )
    assert all(item.reference_price_at_target == Decimal("53") for item in resolvable_observations)
    # No permanently-blocked decision ever produced a fabricated row, across
    # every restart-safe cycle it took to reach the resolvable one.
    for permanent_id in permanent_ids:
        assert final_repository.observations_for_decision(permanent_id) == ()
    assert _row_count(postgres_engine, "decision_followup_observation") == 4


# --------------------------------------------------------------------------
# Final validation gate (FOLLOW-UP STARVATION FIX, HEAD 9ceb508): cursor
# atomicity.  ``save_observation`` and ``save_scan_position`` are each their
# own independent, already-committed transaction (see both methods on
# ``PostgresCounterfactualFollowUpRepository``) -- never one shared unit of
# work.  The test below proves directly, against real Postgres and through
# the real ``SyncFollowUpObservations.execute`` loop (not merely a
# repository-level analogy), that this split cannot let a process crash
# silently skip legitimate work forever.
# --------------------------------------------------------------------------


class _CrashAfterNCryptoPriceSource:
    """Delegates every call to a real ``CryptoPriceSource``, except that the
    Nth ``latest_close_before`` call raises a plain, uncaught ``RuntimeError``
    instead of returning -- faithfully simulating a genuine process crash
    partway through ``SyncFollowUpObservations.execute``'s candidate loop,
    not merely an analogous direct-repository write.  Every candidate the
    loop examined *before* the raise already had its own ``save_observation``
    call genuinely committed for real (each is its own independent,
    already-committed ``self._engine.begin()`` transaction), while
    ``execute`` never reaches its own single, final ``save_scan_position``
    call -- exactly the state a real crash in this exact window would leave
    behind."""

    def __init__(self, real: CryptoPriceSource, *, crash_on_call: int) -> None:
        self._real = real
        self._crash_on_call = crash_on_call
        self.calls = 0

    def daily_close(self, *, product_id: str, bucket_start: datetime) -> CryptoPriceLookup | None:
        return self._real.daily_close(product_id=product_id, bucket_start=bucket_start)

    def latest_close_before(self, *, product_id: str, before: datetime) -> CryptoPriceLookup | None:
        self.calls += 1
        if self.calls == self._crash_on_call:
            raise RuntimeError("SIMULATED_PROCESS_CRASH_MID_CYCLE")
        return self._real.latest_close_before(product_id=product_id, before=before)


def test_process_crash_after_observation_commit_before_cursor_save_loses_no_work(
    postgres_engine: Engine,
) -> None:
    """Cursor atomicity, proven directly against real Postgres rather than
    only argued from reading the source.

    One CRYPTO decision, four ``FOLLOWUP_POLICY_V2`` candidates (2 horizon
    kinds x 2 baseline kinds, ``_all_candidates``' own deterministic order),
    all genuinely ``DUE`` and resolvable.  A wrapped ``CryptoPriceSource``
    raises a plain (non-``FollowUpSourceUnavailable``) ``RuntimeError`` on
    the 3rd ``latest_close_before`` call -- after the first two candidates'
    ``save_observation`` calls have already, genuinely, independently
    committed, and *before* ``execute`` can ever reach its own single,
    final ``save_scan_position`` call.  This is not an analogy for a crash;
    it is one, exercised through the real ``execute`` loop.

    Required properties, each asserted directly:
    * the two already-committed observations survive the crash untouched;
    * the scan cursor is *not* partially advanced to reflect them -- it
      stays exactly ``None``, precisely as if this cycle had never run;
    * a subsequent, fresh cycle (a genuine "restart": brand-new repository
      instance, real un-wrapped price source) resumes at the true oldest
      candidate (``_rotate_from_cursor``'s own ``cursor is None`` branch),
      correctly recognizes the two already-retained observations as
      ``OBSERVED`` (no duplicate row, no ``IntegrityError``), and -- the
      property that actually matters -- still reaches and genuinely
      creates the two remaining candidates that were never examined before
      the crash.  No legitimate work is silently skipped forever."""

    decision_id = _seed_decision(
        postgres_engine, identity="crash-atomicity", expected_holding_days=1
    )
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="crash-atomicity-baseline",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY - timedelta(days=1), close="100", volume="1"
                ),
            ),
            completed_at=DECISION_DAY - timedelta(hours=1),
        )
    )
    crypto_universe.save_observation(
        _crypto_observation(
            observation_id="crash-atomicity-target",
            product_id="BTC-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="105", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
        )
    )
    as_of = DECISION_DAY + timedelta(days=2)
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) is None

    crashing_prices = _CrashAfterNCryptoPriceSource(repository.crypto_prices, crash_on_call=3)
    with pytest.raises(RuntimeError, match="SIMULATED_PROCESS_CRASH_MID_CYCLE"):
        SyncFollowUpObservations(
            repository.ledger,
            repository.subjects,
            crashing_prices,
            _FakeKalshiSource(),
            repository,
        ).execute(as_of=as_of, sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)

    # The first two candidates (both SHORT_HORIZON, one per baseline kind --
    # see `_all_candidates`'s own deterministic order) were genuinely built
    # and committed for real before the third call raised.
    assert crashing_prices.calls == 3
    assert _row_count(postgres_engine, "decision_followup_observation") == 2
    history_after_crash = repository.observations_for_decision(decision_id)
    assert len(history_after_crash) == 2
    assert {item.horizon_kind for item in history_after_crash} == {
        FollowUpHorizonKind.SHORT_HORIZON
    }

    # The crash happened *before* `execute` ever reached its own single,
    # final `save_scan_position` call -- the cursor must be untouched, not
    # partially advanced to reflect the two candidates that were examined.
    assert repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) is None

    # Recovery: a fresh cycle (a genuine "restart" -- brand-new repository
    # instance, real un-wrapped price source) over the same, unmoved cursor.
    recovered_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    recovery = SyncFollowUpObservations(
        recovered_repository.ledger,
        recovered_repository.subjects,
        recovered_repository.crypto_prices,
        _FakeKalshiSource(),
        recovered_repository,
    ).execute(as_of=as_of + timedelta(hours=1), sample_limit=10, policy_version=FOLLOWUP_POLICY_V2)

    assert recovery.skipped_already_observed_count == 2
    assert recovery.created_count == 2
    assert recovery.genuine_attempts == 2
    assert _row_count(postgres_engine, "decision_followup_observation") == 4
    history_after_recovery = recovered_repository.observations_for_decision(decision_id)
    assert len(history_after_recovery) == 4
    assert {item.horizon_kind for item in history_after_recovery} == {
        FollowUpHorizonKind.SHORT_HORIZON,
        FollowUpHorizonKind.STRATEGY_HORIZON,
    }

    # This completed cycle *does* reach its own final `save_scan_position`
    # call -- the cursor now genuinely advances, to the true last candidate
    # in `_all_candidates`'s own deterministic order.
    assert recovered_repository.scan_position(
        policy_version=FOLLOWUP_POLICY_V2
    ) == FollowUpScanPosition(
        decision_id=decision_id,
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON.value,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME.value,
    )


def test_wrap_around_examines_true_oldest_candidates_within_a_single_cycle(
    postgres_engine: Engine,
) -> None:
    """Wrap-around, proven as a genuine single-cycle event -- not merely as
    an emergent property visible only after enough cycles.  Two CRYPTO
    decisions (8 ``FOLLOWUP_POLICY_V2`` candidates total, all genuinely
    resolvable).  Cycle 1 examines the first 6 in oldest-first order
    (``candidate_scan_limit=6``), leaving the cursor pointing at the 6th.
    Cycle 2 (a genuine "restart": brand-new repository instance,
    ``candidate_scan_limit=4``) must then examine, within that *same*
    bounded call, both the 2 never-before-seen candidates at the physical
    *end* of the oldest-first order and the 2 candidates at the physical
    *start* -- ``_rotate_from_cursor``'s own ``candidates[resume_index:] +
    candidates[:resume_index]`` concatenation, genuinely exercised across
    the seam in one call, not only the "finished exactly on the boundary,
    start over next cycle" case a scan limit dividing the total evenly
    would otherwise only ever reach (see
    ``test_sync_candidate_scan_limit_below_permanent_prefix_still_reaches_
    later_work_via_cursor`` above, whose own 24-candidate/limit-4 shape
    divides evenly and therefore never needs to)."""

    decision_a = _seed_decision(postgres_engine, identity="wrap-a", expected_holding_days=1)
    decision_b = _seed_decision(
        postgres_engine,
        identity="wrap-b",
        expected_holding_days=1,
        instrument_id="ETH",
        product_or_market_id="ETH-USD",
    )
    crypto_universe = PostgresCryptoUniverseRepository(postgres_engine)
    for product_id in ("BTC-USD", "ETH-USD"):
        crypto_universe.save_observation(
            _crypto_observation(
                observation_id=f"wrap-{product_id}-baseline",
                product_id=product_id,
                candles=(
                    CryptoDailyObservation.create(
                        bucket_start=DECISION_DAY - timedelta(days=1), close="100", volume="1"
                    ),
                ),
                completed_at=DECISION_DAY - timedelta(hours=1),
            )
        )
        crypto_universe.save_observation(
            _crypto_observation(
                observation_id=f"wrap-{product_id}-target",
                product_id=product_id,
                candles=(
                    CryptoDailyObservation.create(
                        bucket_start=DECISION_DAY, close="105", volume="1"
                    ),
                ),
                completed_at=DECISION_DAY + timedelta(hours=1),
            )
        )
    as_of = DECISION_DAY + timedelta(days=2)

    first_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    first = SyncFollowUpObservations(
        first_repository.ledger,
        first_repository.subjects,
        first_repository.crypto_prices,
        _FakeKalshiSource(),
        first_repository,
    ).execute(
        as_of=as_of, sample_limit=100, candidate_scan_limit=6, policy_version=FOLLOWUP_POLICY_V2
    )
    assert first.candidates_examined == 6
    assert first.created_count == 6
    assert _row_count(postgres_engine, "decision_followup_observation") == 6

    second_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    second = SyncFollowUpObservations(
        second_repository.ledger,
        second_repository.subjects,
        second_repository.crypto_prices,
        _FakeKalshiSource(),
        second_repository,
    ).execute(
        as_of=as_of + timedelta(hours=1),
        sample_limit=100,
        candidate_scan_limit=4,
        policy_version=FOLLOWUP_POLICY_V2,
    )
    # The defining wrap-around signature: this single bounded cycle reaches
    # 2 candidates it has never seen before (the true tail) *and* 2 it
    # already fully processed last cycle (the true head, wrapped back to)
    # -- never only one or the other, which is exactly what a plain
    # "start over" (no resume) or "stop at the end" (no wrap) bug would
    # instead produce.
    assert second.candidates_examined == 4
    assert second.created_count == 2
    assert second.skipped_already_observed_count == 2
    assert second.genuine_attempts == 2

    # Zero duplicates, zero omissions: exactly the 8 true candidates are
    # retained, no more, no fewer, after the wrap.
    assert _row_count(postgres_engine, "decision_followup_observation") == 8
    final_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    assert len(final_repository.observations_for_decision(decision_a)) == 4
    assert len(final_repository.observations_for_decision(decision_b)) == 4
    # The cursor keeps advancing -- it never resets merely because it
    # wrapped physically within the candidate array.
    assert final_repository.scan_position(policy_version=FOLLOWUP_POLICY_V2) is not None


class _SuccessfulFakeKalshiSource:
    """Configurable-success Kalshi fake -- keyed by ``instrument_id``,
    mirroring ``tests/test_counterfactual_followup.py``'s own in-memory
    ``_later_satisfiable_kalshi`` fixture shape exactly, so a genuine,
    zero-network KALSHI observation can actually be created against real
    Postgres (``_FakeKalshiSource`` above always raises -- deliberately,
    for every other test in this file that only cares about CRYPTO)."""

    def __init__(self, reads: dict[str, KalshiMarketRead]) -> None:
        self._reads = reads
        self.call_count = 0

    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        self.call_count += 1
        return self._reads[instrument_id]


def test_scan_limit_below_permanent_prefix_still_reaches_kalshi_via_cursor(
    postgres_engine: Engine,
) -> None:
    """The same required long-run fairness invariant as ``test_sync_
    candidate_scan_limit_below_permanent_prefix_still_reaches_later_work_
    via_cursor`` above, proven again with a genuinely satisfiable *KALSHI*
    decision past the blocked prefix instead of CRYPTO.  ``_rotate_from_
    cursor`` and the durable cursor itself never inspect venue at all --
    only ``execute``'s own per-candidate branch, reached *after* rotation
    has already selected which candidates to examine, does -- so this is
    not a redundant repeat of the CRYPTO case: it is the direct
    persistence-layer proof that the venue-blind cursor mechanism
    genuinely reaches KALSHI work too, not merely an inference from it."""

    permanent_ids = tuple(
        _seed_decision(
            postgres_engine, identity=f"cursor-kalshi-perm-{index:02d}", expected_holding_days=1
        )
        for index in range(5)
    )
    resolvable_id = _seed_decision(
        postgres_engine,
        identity="cursor-kalshi-resolvable",
        venue=SharedVenue.KALSHI,
        instrument_id="KX-CURSOR-RESOLVABLE",
        product_or_market_id="KX-CURSOR-RESOLVABLE:COMPLETE_SET",
        expected_holding_days=None,
    )
    # No BTC-USD evidence retained anywhere -- every permanent decision's
    # baseline lookup returns None unconditionally, exactly as above.
    as_of = DECISION_DAY + timedelta(days=2)
    kalshi_reads = {
        "KX-CURSOR-RESOLVABLE": KalshiMarketRead(
            market_status="open",
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.45"),
            settlement_result=None,
            settlement_source=None,
            source_endpoint_identity="kalshi-public-event-followup-v1",
            request_count=1,
        )
    }

    # 5 permanent CRYPTO decisions x 4 candidates + 1 KALSHI decision x 2
    # candidates (KALSHI only ever gets DECISION_TIME -- see
    # ``_baseline_kinds_for``; its STRATEGY_HORIZON candidate is
    # UNSUPPORTED here since no ``expected_holding_days`` is declared, but
    # still costs one ``candidates_examined`` unit like every outcome).
    total_candidates = 5 * 4 + 2
    candidate_scan_limit = 4
    assert candidate_scan_limit < 5 * 4

    resolvable_reached_at_cycle: int | None = None
    for cycle in range(1, 16):
        cycle_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
        result = SyncFollowUpObservations(
            cycle_repository.ledger,
            cycle_repository.subjects,
            cycle_repository.crypto_prices,
            _SuccessfulFakeKalshiSource(kalshi_reads),
            cycle_repository,
        ).execute(
            as_of=as_of + timedelta(hours=cycle),
            sample_limit=100,
            candidate_scan_limit=candidate_scan_limit,
            policy_version=FOLLOWUP_POLICY_V2,
        )
        assert result.candidates_examined == candidate_scan_limit
        if len(cycle_repository.observations_for_decision(resolvable_id)) == 1:
            resolvable_reached_at_cycle = cycle
            break

    assert resolvable_reached_at_cycle is not None, (
        "resolvable KALSHI decision was never reached across 15 restart-safe cycles"
    )
    assert resolvable_reached_at_cycle <= math.ceil(total_candidates / candidate_scan_limit)

    final_repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    resolvable_observations = final_repository.observations_for_decision(resolvable_id)
    assert len(resolvable_observations) == 1
    assert resolvable_observations[0].horizon_kind == FollowUpHorizonKind.SHORT_HORIZON
    # No permanently-blocked decision ever produced a fabricated row.
    for permanent_id in permanent_ids:
        assert final_repository.observations_for_decision(permanent_id) == ()


# --------------------------------------------------------------------------
# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1 -- additive assessment persistence, real ``source_lineage``
# lineage resolution, and the confirmed-incident-shaped regression proof
# (F): the existing contaminated row stays byte/canonical-hash unchanged,
# the additive assessment marks it ``INELIGIBLE_EVIDENCE``, and downstream
# eligibility (``assessments()``) surfaces the exclusion.
# --------------------------------------------------------------------------


def _seed_observation(
    engine: Engine, *, identity: str, product_id: str = "BTC-USD", instrument_id: str = "BTC"
) -> tuple[PostgresCounterfactualFollowUpRepository, DecisionFollowUpObservation]:
    """Seed one real, retained decision, one real, retained (eligible) pair
    of ``crypto_product_observation`` batches, and one real, retained
    (eligible) CRYPTO follow-up observation referencing them via ``source_
    lineage`` -- what every new observation looks like after this
    milestone, genuinely resolvable end-to-end (not merely a builder call
    over unretained source-observation ids), so ``AssessFollowUpEvidence
    Quality``'s own real ``resolve_observation_evidence_lineage`` join
    succeeds against it exactly as it would in production."""

    decision_id = _seed_decision(
        engine, identity=identity, product_or_market_id=product_id, instrument_id=instrument_id
    )
    repository = PostgresCounterfactualFollowUpRepository(engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    crypto_repository = PostgresCryptoUniverseRepository(engine)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id=f"cff-{identity}-baseline-batch",
            product_id=product_id,
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="100", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id=f"cff-{identity}-target-batch",
            product_id=product_id,
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY + timedelta(days=1), close="110", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(days=1, hours=1),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    observation = build_crypto_price_movement_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        baseline_time=DECISION_DAY,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id=instrument_id,
        subject_product_or_market_id=product_id,
        price_at_baseline=Decimal("100"),
        price_at_baseline_source_observation_id=f"cff-{identity}-baseline-batch",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("110"),
        price_at_target_source_observation_id=f"cff-{identity}-target-batch",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
        policy_version=FOLLOWUP_POLICY_V1,
    )
    was_replay = repository.save_observation(observation=observation)
    assert was_replay is False
    return repository, observation


# --------------------------------------------------------------------------
# Autonomy Audit v1: ``unassessed_observations`` -- the bounded backlog
# query the recurring cycle's own self-healing assessment sweep relies on
# (``adapters.operating_cycle_tasks._BoundedUnassessedFollowUpEvidenceSource``).
# --------------------------------------------------------------------------


def test_unassessed_observations_returns_only_missing_bounded_and_deterministic(
    postgres_engine: Engine,
) -> None:
    """Proves the query is exactly "every retained observation with no
    assessment row yet for this policy version" -- excludes an already-
    assessed row, respects ``limit``, orders deterministically (``observed_
    at``, then ``observation_id`` -- identical ordering to ``observations()``
    itself), and drains to empty once the whole backlog is assessed (Part:
    this query *is* the backlog position; no separate cursor is needed)."""

    repository, first = _seed_observation(postgres_engine, identity="unassessed-a")
    _, second = _seed_observation(postgres_engine, identity="unassessed-b")
    _, third = _seed_observation(postgres_engine, identity="unassessed-c")

    repository.save_assessment(
        assessment=assess_followup_evidence_quality(
            observation=first,
            baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
            lineage_resolved=True,
            assessed_at=NOW,
        )
    )

    unassessed = repository.unassessed_observations(
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1, limit=10
    )
    assert {item.observation_id for item in unassessed} == {
        second.observation_id,
        third.observation_id,
    }
    # observed_at ties (both NOW) -- tiebreak deterministically by
    # observation_id, identical ordering to ``observations()``.
    assert [item.observation_id for item in unassessed] == sorted(
        item.observation_id for item in unassessed
    )

    bounded = repository.unassessed_observations(
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1, limit=1
    )
    assert len(bounded) == 1
    assert bounded[0].observation_id == unassessed[0].observation_id

    # A different (hypothetical future) policy version sees the identical
    # backlog as fully unassessed -- ``first``'s assessment is scoped to
    # ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1`` only, never treated as
    # covering any other version.
    assert (
        len(
            repository.unassessed_observations(
                assessment_policy_version="SOME_FUTURE_POLICY_V2", limit=10
            )
        )
        == 3
    )

    # Assessing the remaining backlog drains this query to empty.
    for item in (second, third):
        repository.save_assessment(
            assessment=assess_followup_evidence_quality(
                observation=item,
                baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
                target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
                lineage_resolved=True,
                assessed_at=NOW,
            )
        )
    assert (
        repository.unassessed_observations(
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1, limit=10
        )
        == ()
    )


def test_evidence_quality_assessment_save_and_read_round_trip(postgres_engine: Engine) -> None:
    repository, observation = _seed_observation(postgres_engine, identity="assess-roundtrip")
    assessment = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    was_replay = repository.save_assessment(assessment=assessment)
    assert was_replay is False
    fetched = repository.assessment(
        observation_id=observation.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert fetched == assessment
    assert fetched.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE


def test_evidence_quality_assessment_is_idempotent(postgres_engine: Engine) -> None:
    repository, observation = _seed_observation(postgres_engine, identity="assess-idempotent")
    assessment = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    first = repository.save_assessment(assessment=assessment)
    second = repository.save_assessment(assessment=assessment)
    assert first is False
    assert second is True
    assert _row_count(postgres_engine, "followup_evidence_quality_assessment") == 1


def test_evidence_quality_assessment_detects_conflict(postgres_engine: Engine) -> None:
    repository, observation = _seed_observation(postgres_engine, identity="assess-conflict")
    original = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    repository.save_assessment(assessment=original)
    conflicting = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.FIXTURE,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert original.assessment_id == conflicting.assessment_id
    assert original.canonical_hash != conflicting.canonical_hash
    with pytest.raises(CounterfactualFollowUpError):
        repository.save_assessment(assessment=conflicting)
    assert (
        repository.assessment(
            observation_id=observation.observation_id,
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        )
        == original
    )


def test_evidence_quality_assessment_immutable_trigger_blocks_update(
    postgres_engine: Engine,
) -> None:
    repository, observation = _seed_observation(postgres_engine, identity="assess-immutable")
    assessment = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    repository.save_assessment(assessment=assessment)
    with pytest.raises(Exception, match="immutable"), postgres_engine.begin() as connection:
        connection.execute(
            evidence_quality_table.update()
            .where(evidence_quality_table.c.assessment_id == assessment.assessment_id)
            .values(canonical_hash="f" * 64)
        )


def test_evidence_quality_assessment_observation_hash_invalid_persists_through_real_postgres(
    postgres_engine: Engine,
) -> None:
    """Section 6 (final-review addition): a tampered/corrupted observation's
    assessment -- ``verdict=INELIGIBLE_EVIDENCE``, ``evidence_defect_type=
    OBSERVATION_HASH_INVALID`` -- must genuinely round-trip through real
    Postgres, proving migration 0099's own ``ck_followup_evidence_quality_
    defect_type`` CHECK constraint accepts this value (not merely that
    Python's closed ``FollowUpEvidenceDefectType`` enum does)."""

    repository, observation = _seed_observation(postgres_engine, identity="assess-hash-invalid")
    tampered = replace(observation, canonical_hash="f" * 64)
    assessment = assess_followup_evidence_quality(
        observation=tampered,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert assessment.evidence_defect_type is FollowUpEvidenceDefectType.OBSERVATION_HASH_INVALID
    was_replay = repository.save_assessment(assessment=assessment)
    assert was_replay is False
    fetched = repository.assessment(
        observation_id=tampered.observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert fetched == assessment


def test_resolve_observation_evidence_lineage_two_distinct_batches(
    postgres_engine: Engine,
) -> None:
    """A row whose baseline and target came from two different retained
    batches -- ``source_lineage`` has two elements, index 0 is always the
    baseline's own source id (Part: the builder always passes baseline
    before target to ``domain.counterfactual_followup._lineage``)."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-lineage-two-baseline",
            product_id="LINK-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="1", volume="1"),
            ),
            completed_at=DECISION_DAY + timedelta(hours=1),
            evidence_class=EvidenceClass.FIXTURE,
        )
    )
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-lineage-two-target",
            product_id="LINK-USD",
            candles=(
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY + timedelta(days=1), close="1.1", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(days=1, hours=1),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    decision_id = _seed_decision(
        postgres_engine,
        identity="lineage-two",
        product_or_market_id="LINK-USD",
        instrument_id="LINK",
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    observation = build_crypto_price_movement_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        baseline_time=DECISION_DAY,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="LINK",
        subject_product_or_market_id="LINK-USD",
        price_at_baseline=Decimal("1"),
        price_at_baseline_source_observation_id="cff-lineage-two-baseline",
        # Deliberately eligible so the *builder* accepts it -- what this
        # test is proving is that ``resolve_observation_evidence_lineage``
        # independently re-derives the true, retained-batch evidence class
        # (FIXTURE) rather than trusting this parameter.
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("1.1"),
        price_at_target_source_observation_id="cff-lineage-two-target",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    assert observation.source_lineage == ("cff-lineage-two-baseline", "cff-lineage-two-target")
    baseline_class, target_class, resolved = repository.resolve_observation_evidence_lineage(
        observation
    )
    assert resolved is True
    assert baseline_class is EvidenceClass.FIXTURE
    assert target_class is EvidenceClass.HISTORICAL_PUBLIC


def test_resolve_observation_evidence_lineage_single_shared_batch(
    postgres_engine: Engine,
) -> None:
    """A one-element ``source_lineage`` means baseline and target came from
    the identical retained batch -- both sides resolve to that batch's own
    evidence class."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    crypto_repository.save_observation(
        _crypto_observation(
            observation_id="cff-lineage-shared-batch",
            product_id="XRP-USD",
            candles=(
                CryptoDailyObservation.create(bucket_start=DECISION_DAY, close="0.5", volume="1"),
                CryptoDailyObservation.create(
                    bucket_start=DECISION_DAY + timedelta(days=1), close="0.52", volume="1"
                ),
            ),
            completed_at=DECISION_DAY + timedelta(days=1, hours=1),
            evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        )
    )
    decision_id = _seed_decision(
        postgres_engine,
        identity="lineage-shared",
        product_or_market_id="XRP-USD",
        instrument_id="XRP",
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    observation = build_crypto_price_movement_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        baseline_time=DECISION_DAY,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="XRP",
        subject_product_or_market_id="XRP-USD",
        price_at_baseline=Decimal("0.5"),
        price_at_baseline_source_observation_id="cff-lineage-shared-batch",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("0.52"),
        price_at_target_source_observation_id="cff-lineage-shared-batch",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    assert observation.source_lineage == ("cff-lineage-shared-batch",)
    baseline_class, target_class, resolved = repository.resolve_observation_evidence_lineage(
        observation
    )
    assert resolved is True
    assert baseline_class is EvidenceClass.HISTORICAL_PUBLIC
    assert target_class is EvidenceClass.HISTORICAL_PUBLIC


def test_resolve_observation_evidence_lineage_unresolvable_when_batch_missing(
    postgres_engine: Engine,
) -> None:
    """A ``source_lineage`` id that no longer resolves to any retained
    batch (or never did) fails closed: ``resolved=False``, never a guess."""

    decision_id = _seed_decision(postgres_engine, identity="lineage-missing")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    observation = build_crypto_price_movement_observation(
        decision=decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        baseline_time=DECISION_DAY,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        price_at_baseline=Decimal("100"),
        price_at_baseline_source_observation_id="cff-lineage-never-retained-batch",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("105"),
        price_at_target_source_observation_id="cff-lineage-also-never-retained-batch",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=NOW,
    )
    _baseline_class, _target_class, resolved = repository.resolve_observation_evidence_lineage(
        observation
    )
    assert resolved is False


def test_assess_follow_up_evidence_quality_marks_contaminated_row_ineligible_without_mutating_it(
    postgres_engine: Engine,
) -> None:
    """Regression F -- the confirmed DOGE-USD incident shape, reproduced at
    test scale.  Constructed by calling ``DecisionFollowUpObservation``'s
    dataclass constructor directly (never through the now-guarded builder,
    which would refuse it) -- exactly how the real contaminated row entered
    this repository before this milestone's own fix existed.  Proves:
    the retained observation stays byte/canonical-hash unchanged; the
    additive assessment marks it ``INELIGIBLE_EVIDENCE`` with the correct
    defect type; a second assessment run is idempotent; downstream
    eligibility (``assessments()``) surfaces the exclusion."""

    crypto_repository = PostgresCryptoUniverseRepository(postgres_engine)
    bucket = DECISION_DAY
    fixture_baseline_batch = _crypto_observation(
        observation_id="cff-contaminated-fixture-baseline",
        product_id="ZEC-USD",
        candles=(CryptoDailyObservation.create(bucket_start=bucket, close="189.504", volume="1"),),
        completed_at=bucket + timedelta(hours=1),
        evidence_class=EvidenceClass.FIXTURE,
    )
    genuine_target_batch = _crypto_observation(
        observation_id="cff-contaminated-genuine-target",
        product_id="ZEC-USD",
        candles=(
            CryptoDailyObservation.create(
                bucket_start=bucket + timedelta(days=1), close="0.07215", volume="1"
            ),
        ),
        completed_at=bucket + timedelta(days=1, hours=1),
        evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )
    crypto_repository.save_observation(fixture_baseline_batch)
    crypto_repository.save_observation(genuine_target_batch)

    decision_id = _seed_decision(
        postgres_engine,
        identity="contaminated",
        product_or_market_id="ZEC-USD",
        instrument_id="ZEC",
    )
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None

    baseline_time = bucket
    target_time = bucket + timedelta(days=1)
    observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=FOLLOWUP_POLICY_V1,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
    )
    source_lineage = (fixture_baseline_batch.observation_id, genuine_target_batch.observation_id)
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
        # Matches the real incident exactly: top-level provenance reflects
        # only the (genuine) target read, silently hiding the fixture
        # baseline -- the defect this milestone's lineage fields fix for
        # every row built from now on.
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
        # Exactly what a pre-remediation row reads back as: never guessed.
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
    original_hash = repository.observation(observation_id).canonical_hash  # type: ignore[union-attr]
    assert original_hash == canonical_hash

    result = AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=NOW, assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
    )
    assert result.ineligible_count >= 1
    assert result.created_count >= 1

    assessment = repository.assessment(
        observation_id=observation_id,
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    assert assessment is not None
    assert assessment.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    assert (
        assessment.evidence_defect_type is FollowUpEvidenceDefectType.BASELINE_EVIDENCE_INELIGIBLE
    )
    assert assessment.baseline_evidence_class is EvidenceClass.FIXTURE
    assert assessment.target_evidence_class is EvidenceClass.HISTORICAL_PUBLIC

    # The retained observation itself: byte/canonical-hash unchanged.
    after = repository.observation(observation_id)
    assert after is not None
    assert after.canonical_hash == original_hash
    assert after.reference_price_at_baseline == Decimal("189.504")
    assert after.provenance is EvidenceClass.HISTORICAL_PUBLIC  # unchanged, still misleading alone

    # Downstream eligibility surfaces the exclusion.
    ineligible_ids = {
        item.observation_id
        for item in repository.assessments(
            assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1
        )
        if item.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
    }
    assert observation_id in ineligible_ids

    # Re-running the assessment is idempotent -- never a second write, never
    # a different verdict.
    replay = AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=NOW + timedelta(hours=1),
        assessment_policy_version=FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    )
    replayed_entry = next(
        entry for entry in replay.entries if entry.observation_id == observation_id
    )
    assert replayed_entry.outcome == "REUSED_IDEMPOTENT_REPLAY"
    assert replayed_entry.verdict is FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE


def test_assess_follow_up_evidence_quality_end_to_end_mixed_eligible_and_kalshi(
    postgres_engine: Engine,
) -> None:
    """Broader integration proof: a genuinely eligible CRYPTO observation
    and a genuine KALSHI observation are both assessed -- every retained
    observation gets exactly one assessment, KALSHI is always ELIGIBLE, and
    a second full run is entirely idempotent replay."""

    repository, _seeded_observation = _seed_observation(
        postgres_engine, identity="mixed-crypto", product_id="ADA-USD", instrument_id="ADA"
    )
    kalshi_decision_id = _seed_decision(
        postgres_engine,
        identity="mixed-kalshi",
        venue=SharedVenue.KALSHI,
        instrument_id="KX-EVENT",
        product_or_market_id="KX-EVENT:COMPLETE_SET",
        expected_holding_days=None,
    )
    kalshi_decision = repository.ledger.decision(kalshi_decision_id)
    assert kalshi_decision is not None
    from apex.domain.counterfactual_followup import build_kalshi_market_status_observation

    kalshi_observation = build_kalshi_market_status_observation(
        decision=kalshi_decision,
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        horizon_days=1,
        target_time=DECISION_DAY + timedelta(days=1),
        subject_instrument_id="KX-EVENT",
        subject_product_or_market_id="KX-EVENT:COMPLETE_SET",
        market_status="open",
        yes_bid=Decimal("0.4"),
        yes_ask=Decimal("0.6"),
        settlement_result=None,
        settlement_source=None,
        source_endpoint_identity="https://api.elections.kalshi.com/trade-api/v2/markets/KX-EVENT",
        observed_at=NOW,
    )
    repository.save_observation(observation=kalshi_observation)

    result = AssessFollowUpEvidenceQuality(repository, repository).execute(assessed_at=NOW)
    assert result.considered_observations == 2
    assert result.eligible_count == 2
    assert result.ineligible_count == 0
    assert result.created_count == 2

    replay = AssessFollowUpEvidenceQuality(repository, repository).execute(
        assessed_at=NOW + timedelta(hours=1)
    )
    assert replay.considered_observations == 2
    assert replay.created_count == 0
    assert replay.idempotent_replay_count == 2


# --------------------------------------------------------------------------
# Migration 0099's own downgrade guard
# --------------------------------------------------------------------------


def test_0099_downgrade_succeeds_when_empty(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0098_followup_scan_cursor")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        table_names = set(sa.inspect(connection).get_table_names())
    assert current_revision == "0098_followup_scan_cursor"
    assert "followup_evidence_quality_assessment" not in table_names
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0099_followup_evidence_quality")
        connection.commit()


def test_0099_downgrade_blocks_with_retained_assessment_evidence(
    postgres_engine: Engine,
) -> None:
    repository, observation = _seed_observation(postgres_engine, identity="downgrade-0099")
    assessment = assess_followup_evidence_quality(
        observation=observation,
        baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        lineage_resolved=True,
        assessed_at=NOW,
    )
    repository.save_assessment(assessment=assessment)
    assert _row_count(postgres_engine, "followup_evidence_quality_assessment") == 1
    with (
        pytest.raises(Exception, match="cannot downgrade 0099"),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0098_followup_scan_cursor")
        connection.commit()


# --------------------------------------------------------------------------
# Counterfactual Forward Outcome Temporal Integrity v1 -- migration 0113's
# own database-level guarantees.  These are the halves of the milestone's
# acceptance matrix that only a real schema can prove; the domain/application
# halves live in ``tests/test_counterfactual_followup.py``.
# --------------------------------------------------------------------------

V3_ANCHOR = datetime(2026, 7, 3, 3, 3, tzinfo=UTC)
V3_WINDOW_START = datetime(2026, 7, 4, tzinfo=UTC)
V3_WINDOW_END = datetime(2026, 7, 5, tzinfo=UTC)
V3_MEASUREMENT_AS_OF = datetime(2026, 7, 6, tzinfo=UTC)


def _build_v3(decision: object) -> object:
    return build_crypto_price_movement_observation(
        decision=decision,  # type: ignore[arg-type]
        horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
        baseline_kind=FollowUpBaselineKind.DECISION_TIME,
        horizon_days=1,
        baseline_time=V3_WINDOW_START,
        target_time=V3_WINDOW_END,
        subject_instrument_id="BTC",
        subject_product_or_market_id="BTC-USD",
        price_at_baseline=Decimal("90"),
        price_at_baseline_source_observation_id="crypto-obs-window-start",
        price_at_baseline_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        price_at_target=Decimal("99"),
        price_at_target_source_observation_id="crypto-obs-window-end",
        price_at_target_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
        observed_at=V3_MEASUREMENT_AS_OF,
        policy_version=FOLLOWUP_POLICY_V3,
        measurement_as_of=V3_MEASUREMENT_AS_OF,
        policy_anchor_at=V3_ANCHOR,
        decision_input_reference_price=Decimal("80"),
        decision_input_reference_bucket_start=datetime(2026, 7, 2, tzinfo=UTC),
        decision_input_source_observation_id="crypto-obs-decision-input",
        decision_input_evidence_class=EvidenceClass.HISTORICAL_PUBLIC,
    )


def test_v3_observation_round_trips_every_new_fact_through_real_postgres(
    postgres_engine: Engine,
) -> None:
    """The six V3 facts survive a genuine write/read cycle.  Five of them
    live only in the immutable ``payload``; ``measurement_as_of`` is
    additionally a real column so the database can constrain it, and both
    representations must agree -- a row that hashed one instant and stored
    another in its column would be exactly the kind of "two sources of one
    fact" this repository refuses to ship."""

    decision_id = _seed_decision(postgres_engine, identity="v3-round-trip")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    observation = _build_v3(decision)
    assert repository.save_observation(observation=observation) is False  # type: ignore[arg-type]

    retained = repository.observation(observation.observation_id)  # type: ignore[attr-defined]
    assert retained is not None
    assert retained == observation
    assert retained.measurement_as_of == V3_MEASUREMENT_AS_OF
    assert retained.policy_anchor_at == V3_ANCHOR
    assert retained.decision_input_reference_price == Decimal("80")
    assert retained.decision_input_reference_bucket_start == datetime(2026, 7, 2, tzinfo=UTC)
    assert retained.decision_input_source_observation_id == "crypto-obs-decision-input"
    assert retained.decision_input_evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    # The column and the hashed payload agree.
    with postgres_engine.connect() as connection:
        column_value = connection.scalar(
            sa.select(observation_table.c.measurement_as_of).where(
                observation_table.c.observation_id == observation.observation_id  # type: ignore[attr-defined]
            )
        )
    assert column_value is not None
    assert column_value.astimezone(UTC) == V3_MEASUREMENT_AS_OF
    # Idempotent replay of the identical measurement writes nothing.
    assert repository.save_observation(observation=observation) is True  # type: ignore[arg-type]
    assert _row_count(postgres_engine, "decision_followup_observation") == 1


def test_v3_row_without_measurement_as_of_is_rejected_by_the_database(
    postgres_engine: Engine,
) -> None:
    """A11 (database half).  ``ck_decision_followup_v3_measurement_as_of``.
    The domain builder already refuses this; the constraint means no future
    write path can ever reintroduce it."""

    decision_id = _seed_decision(postgres_engine, identity="v3-missing-measurement")
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.insert().values(
                observation_id="decision_followup_observation_v3_no_measurement",
                decision_id=decision_id,
                venue="CRYPTO",
                disposition="NOT_ADVANCED",
                policy_version="FOLLOWUP_POLICY_V3",
                horizon_kind="SHORT_HORIZON",
                baseline_kind="DECISION_TIME",
                observation_type="CRYPTO_REFERENCE_PRICE_MOVEMENT",
                target_time=V3_WINDOW_END,
                observed_at=V3_MEASUREMENT_AS_OF,
                measurement_as_of=None,  # the violation
                payload="{}",
                canonical_hash="c" * 64,
                authority="NONE",
            )
        )


@pytest.mark.parametrize("policy_version", ["FOLLOWUP_POLICY_V1", "FOLLOWUP_POLICY_V2"])
def test_legacy_row_with_a_measurement_as_of_is_rejected_by_the_database(
    postgres_engine: Engine, policy_version: str
) -> None:
    """A12 (database half).  The constraint is a *biconditional*, so a
    ``FOLLOWUP_POLICY_V1``/``V2`` row can never acquire a measurement instant
    it never measured under -- which would both misrepresent that row and
    break the canonical-hash block invariant."""

    decision_id = _seed_decision(postgres_engine, identity=f"legacy-{policy_version.lower()}")
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.insert().values(
                observation_id=f"decision_followup_observation_legacy_{policy_version}",
                decision_id=decision_id,
                venue="CRYPTO",
                disposition="NOT_ADVANCED",
                policy_version=policy_version,
                horizon_kind="SHORT_HORIZON",
                baseline_kind="DECISION_TIME",
                observation_type="CRYPTO_REFERENCE_PRICE_MOVEMENT",
                target_time=V3_WINDOW_END,
                observed_at=V3_MEASUREMENT_AS_OF,
                measurement_as_of=V3_MEASUREMENT_AS_OF,  # the violation
                payload="{}",
                canonical_hash="c" * 64,
                authority="NONE",
            )
        )


def test_every_already_retained_legacy_row_keeps_a_null_measurement_as_of(
    postgres_engine: Engine,
) -> None:
    """A12.  Migration 0113 performs no backfill, so a genuine V1 and a
    genuine V2 row written through the real repository both read back with
    ``measurement_as_of IS NULL`` -- permanently, and without the migration
    ever having disabled the append-only immutability trigger."""

    decision_id = _seed_decision(postgres_engine, identity="legacy-null-measurement")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    for policy_version in (FOLLOWUP_POLICY_V1, FOLLOWUP_POLICY_V2):
        repository.save_observation(  # type: ignore[arg-type]
            observation=_build(
                decision=decision,
                policy_version=policy_version,
                price_at_baseline=Decimal("100"),
            )
        )
    with postgres_engine.connect() as connection:
        null_count = connection.scalar(
            sa.text(
                "SELECT count(*) FROM decision_followup_observation WHERE measurement_as_of IS NULL"
            )
        )
    assert null_count == 2
    assert all(item.measurement_as_of is None for item in repository.observations())


def test_v3_row_for_a_non_crypto_venue_is_rejected_by_the_database(
    postgres_engine: Engine,
) -> None:
    """A13 (database half).  ``ck_decision_followup_v3_crypto_only``.
    Restates, at the schema, what ``followup_policy_version_supports_venue``
    and both non-CRYPTO builders already enforce -- deliberately in more than
    one place, exactly as ``ck_decision_followup_kalshi_baseline_kind``
    already is."""

    decision_id = _seed_decision(postgres_engine, identity="v3-kalshi-venue")
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            observation_table.insert().values(
                observation_id="decision_followup_observation_v3_kalshi",
                decision_id=decision_id,
                venue="KALSHI",  # the violation
                disposition="NOT_ADVANCED",
                policy_version="FOLLOWUP_POLICY_V3",
                horizon_kind="SHORT_HORIZON",
                baseline_kind="DECISION_TIME",
                observation_type="KALSHI_MARKET_STATUS_AND_PRICE",
                target_time=V3_WINDOW_END,
                observed_at=V3_MEASUREMENT_AS_OF,
                measurement_as_of=V3_MEASUREMENT_AS_OF,
                payload="{}",
                canonical_hash="c" * 64,
                authority="NONE",
            )
        )


def test_securities_structure_outcome_policy_constraint_is_deliberately_not_widened(
    postgres_engine: Engine,
) -> None:
    """A22.  SECURITIES stays on ``FOLLOWUP_POLICY_V2`` and V3 is CRYPTO-only,
    so no SECURITIES structure outcome can ever carry V3.  Migration 0113
    therefore leaves ``ck_securities_structure_outcome_followup_policy_
    version`` alone -- asserted positively here, because a future migration
    widening it "for consistency" would quietly assert a capability that does
    not exist."""

    with postgres_engine.connect() as connection:
        securities_clause = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_securities_structure_outcome_followup_policy_version'"
            )
        )
        outcome_clause = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_counterfactual_outcome_followup_policy_version'"
            )
        )
        observation_clause = connection.scalar(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_decision_followup_policy_version'"
            )
        )
    assert securities_clause is not None
    assert "FOLLOWUP_POLICY_V3" not in securities_clause
    # The two that genuinely had to widen, did.
    assert outcome_clause is not None and "FOLLOWUP_POLICY_V3" in outcome_clause
    assert observation_clause is not None and "FOLLOWUP_POLICY_V3" in observation_clause


# --------------------------------------------------------------------------
# Migration 0113's own downgrade guard -- symmetric with 0096's and 0097's
# above, scoped to exactly what 0113 widened.
# --------------------------------------------------------------------------


def test_0113_downgrade_succeeds_when_empty(postgres_engine: Engine) -> None:
    assert _row_count(postgres_engine, "decision_followup_observation") == 0
    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0112_trial_evidence_coverage")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0112_trial_evidence_coverage"
    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0118_evidence_gap_recovery")
        connection.commit()


def test_0113_downgrade_succeeds_with_only_pre_v3_evidence_retained(
    postgres_engine: Engine,
) -> None:
    """The narrower constraints 0113's downgrade restores still accept every
    ``FOLLOWUP_POLICY_V1``/``V2`` row, and dropping a column that is ``NULL``
    on all of them loses nothing -- a database that has not yet used V3
    downgrades cleanly, exactly like any other additive migration."""

    decision_id = _seed_decision(postgres_engine, identity="downgrade-pre-v3")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    repository.save_observation(  # type: ignore[arg-type]
        observation=_build(
            decision=decision,
            policy_version=FOLLOWUP_POLICY_V2,
            price_at_baseline=Decimal("100"),
        )
    )

    with postgres_engine.connect() as connection:
        command.downgrade(_alembic_config(connection), "0112_trial_evidence_coverage")
        connection.commit()
    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0112_trial_evidence_coverage"
    assert _row_count(postgres_engine, "decision_followup_observation") == 1

    with postgres_engine.connect() as connection:
        command.upgrade(_alembic_config(connection), "0118_evidence_gap_recovery")
        connection.commit()


def test_0113_downgrade_blocks_with_retained_v3_evidence(postgres_engine: Engine) -> None:
    decision_id = _seed_decision(postgres_engine, identity="downgrade-v3-guard")
    repository = PostgresCounterfactualFollowUpRepository(postgres_engine)
    decision = repository.ledger.decision(decision_id)
    assert decision is not None
    repository.save_observation(observation=_build_v3(decision))  # type: ignore[arg-type]

    with (
        pytest.raises(
            Exception,
            match="cannot downgrade 0113 with retained FOLLOWUP_POLICY_V3 follow-up evidence",
        ),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_alembic_config(connection), "0112_trial_evidence_coverage")
        connection.commit()

    with postgres_engine.connect() as connection:
        current_revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert current_revision == "0113_followup_policy_v3"
    assert _row_count(postgres_engine, "decision_followup_observation") == 1
