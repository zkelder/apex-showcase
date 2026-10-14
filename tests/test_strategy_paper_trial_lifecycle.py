"""Repeatable Strategy PAPER Evidence v1: pure, offline tests for
``domain.strategy_paper_trial_lifecycle`` -- trial identity, horizon
evaluation, retrial eligibility, generation computation, legacy adoption,
and closure-record construction. No database, no network -- every input is
a directly-constructed fixture or a literal datetime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.crypto_paper import (
    CryptoPaperAuthorization,
    CryptoPaperEligibility,
    CryptoPaperOrder,
    PaperEligibilityState,
    PaperOrderState,
    PaperOrderType,
    PaperSide,
)
from apex.domain.live_market_observation import (
    LiveMarketObservation,
    LiveObservationQuality,
    LiveObservationType,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
)
from apex.domain.strategy_paper_trial_evidence_coverage import (
    TrialEvidenceCoverage,
    evaluate_trial_evidence_coverage,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    STRATEGY_PAPER_TRIAL_COOLDOWN_DAYS,
    STRATEGY_PAPER_TRIAL_HORIZON_DAYS,
    StrategyPaperTrialLifecycleError,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
    TrialHorizonState,
    TrialRetrialEligibility,
    adopt_legacy_trial,
    build_strategy_paper_trial_closure_record,
    build_strategy_paper_trial_record,
    build_unfilled_expiry_closure_record,
    evaluate_retrial_eligibility,
    evaluate_trial_horizon,
    next_trial_generation,
    strategy_paper_trial_id,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 9, 3, tzinfo=UTC)
STRATEGY_ID = "buy-and-hold-btc-usd"
INSTRUMENT_ID = "BTC-USD"
ACCOUNT_ID = "genuine-strategy-crypto-paper-account"


def _observation(*, captured_at: datetime) -> LiveMarketObservation:
    return LiveMarketObservation.create(
        round_id="test-round",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_id=INSTRUMENT_ID,
        observation_type=LiveObservationType.QUOTE,
        captured_at=captured_at,
        bid_price=Decimal("100"),
        ask_price=Decimal("100.01"),
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=LiveObservationQuality.PASSED,
    )


def _coverage(*, window_start: datetime, window_end: datetime) -> TrialEvidenceCoverage:
    """A representative coverage summary for tests that don't care about
    its exact content -- computed through the real ``evaluate_trial_
    evidence_coverage`` from zero retained observations (the degenerate
    "entire window uncovered" case) rather than a hand-rolled literal that
    could silently drift from what the real function actually produces.
    Dedicated coverage-computation behavior has its own test module,
    ``tests/test_strategy_paper_trial_evidence_coverage.py``."""

    return evaluate_trial_evidence_coverage(
        instrument_id=INSTRUMENT_ID,
        window_start=window_start,
        window_end=window_end,
        observations=(),
    )


def _trial(*, generation: int = 1, opened_at: datetime = NOW) -> StrategyPaperTrialRecord:
    return build_strategy_paper_trial_record(
        strategy_id=STRATEGY_ID,
        strategy_version=1,
        instrument_id=INSTRUMENT_ID,
        account_id=ACCOUNT_ID,
        generation=generation,
        opened_at=opened_at,
        opening_authorization_id=f"trial-{generation}-authorization",
        opening_order_id=f"trial-{generation}-order",
        opening_realized_pnl_baseline=Decimal("0"),
    )


class TestTrialIdentity:
    def test_deterministic_across_repeated_calls(self) -> None:
        first = strategy_paper_trial_id(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID, generation=1
        )
        second = strategy_paper_trial_id(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID, generation=1
        )
        assert first == second

    def test_distinct_across_generations(self) -> None:
        gen1 = strategy_paper_trial_id(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID, generation=1
        )
        gen2 = strategy_paper_trial_id(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID, generation=2
        )
        assert gen1 != gen2

    def test_distinct_from_strategy_id_instrument_id_and_account_id_alone(self) -> None:
        trial_id = strategy_paper_trial_id(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID, generation=1
        )
        assert trial_id not in (STRATEGY_ID, INSTRUMENT_ID, ACCOUNT_ID)

    def test_build_rejects_nonpositive_generation(self) -> None:
        with pytest.raises(StrategyPaperTrialLifecycleError):
            build_strategy_paper_trial_record(
                strategy_id=STRATEGY_ID,
                strategy_version=1,
                instrument_id=INSTRUMENT_ID,
                account_id=ACCOUNT_ID,
                generation=0,
                opened_at=NOW,
                opening_authorization_id="a",
                opening_order_id="o",
                opening_realized_pnl_baseline=Decimal("0"),
            )

    def test_horizon_target_is_opened_at_plus_horizon_days(self) -> None:
        trial = _trial()
        assert trial.horizon_target_at == NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)

    def test_replay_with_identical_inputs_reproduces_identical_hash(self) -> None:
        first = _trial()
        second = _trial()
        assert first.trial_id == second.trial_id
        assert first.canonical_hash == second.canonical_hash

    def test_different_realized_pnl_baseline_changes_the_hash(self) -> None:
        # opening_realized_pnl_baseline is folded into identity so a
        # genuine content discrepancy at the same trial_id is detectable
        # as a hard conflict, never silently smoothed over as a replay.
        first = build_strategy_paper_trial_record(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            generation=1,
            opened_at=NOW,
            opening_authorization_id="a",
            opening_order_id="o",
            opening_realized_pnl_baseline=Decimal("0"),
        )
        second = build_strategy_paper_trial_record(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            generation=1,
            opened_at=NOW,
            opening_authorization_id="a",
            opening_order_id="o",
            opening_realized_pnl_baseline=Decimal("5"),
        )
        assert first.trial_id == second.trial_id
        assert first.canonical_hash != second.canonical_hash


class TestTrialHorizon:
    def test_before_horizon(self) -> None:
        trial = _trial(opened_at=NOW)
        as_of = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS - 1)
        assert evaluate_trial_horizon(trial=trial, as_of=as_of) is TrialHorizonState.BEFORE_HORIZON

    def test_exactly_at_horizon_is_due(self) -> None:
        trial = _trial(opened_at=NOW)
        as_of = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        assert (
            evaluate_trial_horizon(trial=trial, as_of=as_of)
            is TrialHorizonState.AT_OR_AFTER_HORIZON
        )

    def test_well_after_horizon_is_due(self) -> None:
        trial = _trial(opened_at=NOW)
        as_of = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS + 90)
        assert (
            evaluate_trial_horizon(trial=trial, as_of=as_of)
            is TrialHorizonState.AT_OR_AFTER_HORIZON
        )


class TestRetrialEligibility:
    def test_no_prior_trial(self) -> None:
        result = evaluate_retrial_eligibility(latest_trial=None, latest_closure=None, as_of=NOW)
        assert result is TrialRetrialEligibility.NO_PRIOR_TRIAL

    def test_active_trial_blocks_retrial(self) -> None:
        trial = _trial()
        result = evaluate_retrial_eligibility(latest_trial=trial, latest_closure=None, as_of=NOW)
        assert result is TrialRetrialEligibility.ACTIVE_TRIAL_BLOCKS_RETRIAL

    def test_cooldown_not_yet_elapsed(self) -> None:
        trial = _trial(opened_at=NOW)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1.23"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        as_of = closure.closed_at + timedelta(hours=1)
        result = evaluate_retrial_eligibility(
            latest_trial=trial, latest_closure=closure, as_of=as_of
        )
        assert result is TrialRetrialEligibility.COOLDOWN_NOT_YET_ELAPSED

    def test_eligible_for_next_generation_once_cooldown_elapses(self) -> None:
        trial = _trial(opened_at=NOW)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1.23"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        as_of = closure.next_eligible_at
        result = evaluate_retrial_eligibility(
            latest_trial=trial, latest_closure=closure, as_of=as_of
        )
        assert result is TrialRetrialEligibility.ELIGIBLE_FOR_NEXT_GENERATION

    def test_closure_for_a_different_trial_id_is_treated_as_still_active(self) -> None:
        # Defensive: a caller-supplied closure that does not actually
        # belong to latest_trial must never be mistaken for its own.
        trial = _trial(generation=2)
        other_trial = _trial(generation=1)
        stale_closure = build_strategy_paper_trial_closure_record(
            trial=other_trial,
            closed_at=NOW,
            closing_authorization_id="x",
            closing_order_id="y",
            realized_paper_pnl=None,
            coverage=_coverage(window_start=other_trial.opened_at, window_end=NOW),
        )
        result = evaluate_retrial_eligibility(
            latest_trial=trial, latest_closure=stale_closure, as_of=NOW + timedelta(days=365)
        )
        assert result is TrialRetrialEligibility.ACTIVE_TRIAL_BLOCKS_RETRIAL


class TestNextTrialGeneration:
    def test_no_trials_yields_generation_one(self) -> None:
        assert next_trial_generation(trials=()) == 1

    def test_one_terminal_trial_yields_generation_two(self) -> None:
        assert next_trial_generation(trials=(_trial(generation=1),)) == 2

    def test_uses_max_not_count(self) -> None:
        # Robust even against a hypothetical gap, not just len()+1.
        assert next_trial_generation(trials=(_trial(generation=1), _trial(generation=5))) == 6


class TestClosureRecords:
    def test_closure_rejects_closing_before_opening(self) -> None:
        trial = _trial(opened_at=NOW)
        with pytest.raises(StrategyPaperTrialLifecycleError):
            build_strategy_paper_trial_closure_record(
                trial=trial,
                closed_at=NOW - timedelta(days=1),
                closing_authorization_id="a",
                closing_order_id="o",
                realized_paper_pnl=None,
                coverage=_coverage(window_start=trial.opened_at, window_end=NOW),
            )

    def test_next_eligible_at_is_closed_at_plus_cooldown_days(self) -> None:
        trial = _trial(opened_at=NOW)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("2"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        assert closure.next_eligible_at == closed_at + timedelta(
            days=STRATEGY_PAPER_TRIAL_COOLDOWN_DAYS
        )

    def test_default_terminal_reason_is_horizon_exit(self) -> None:
        trial = _trial()
        closed_at = trial.opened_at + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("2"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        assert closure.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT

    def test_unfilled_expiry_closure_self_references_opening_ids_with_no_pnl(self) -> None:
        trial = _trial()
        closed_at = trial.opened_at + timedelta(hours=4)
        closure = build_unfilled_expiry_closure_record(
            trial=trial,
            closed_at=closed_at,
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        assert (
            closure.terminal_reason
            is StrategyPaperTrialTerminalReason.OPENING_ORDER_EXPIRED_UNFILLED
        )
        assert closure.closing_authorization_id == trial.opening_authorization_id
        assert closure.closing_order_id == trial.opening_order_id
        assert closure.realized_paper_pnl is None

    def test_closure_replay_is_idempotent_by_hash(self) -> None:
        trial = _trial()
        closed_at = trial.opened_at + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        coverage = _coverage(window_start=trial.opened_at, window_end=closed_at)
        first = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("2"),
            coverage=coverage,
        )
        # Recomputing coverage independently (mirroring a genuine replay,
        # which re-fetches and re-summarizes retained observations rather
        # than reusing the first call's in-memory value) must still
        # reproduce the identical value -- evaluate_trial_evidence_coverage
        # is pure and deterministic.
        second = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("2"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        assert first.closure_id == second.closure_id
        assert first.canonical_hash == second.canonical_hash

    def test_different_coverage_changes_the_hash(self) -> None:
        # Coverage must be load-bearing on identity -- a genuinely
        # different coverage summary for the same closure_id is exactly
        # the kind of conflict RecordStrategyPaperTrialClosed is designed
        # to reject, never silently accept as if it were a pure replay.
        trial = _trial()
        closed_at = trial.opened_at + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        empty_coverage = _coverage(window_start=trial.opened_at, window_end=closed_at)
        dense_coverage = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=trial.opened_at,
            window_end=closed_at,
            observations=(_observation(captured_at=trial.opened_at),),
        )
        assert empty_coverage != dense_coverage
        first = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("2"),
            coverage=empty_coverage,
        )
        second = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("2"),
            coverage=dense_coverage,
        )
        assert first.closure_id == second.closure_id
        assert first.canonical_hash != second.canonical_hash


def _legacy_authorization(*, authorization_id: str, strategy_id: str) -> CryptoPaperAuthorization:
    projection = SharedOpportunityProjection.create(
        projection_id="legacy-projection",
        venue=SharedVenue.CRYPTO,
        source_record_type="StrategyDefinition",
        source_record_id=strategy_id,
        source_canonical_hash="a" * 64,
        strategy_or_method_id=strategy_id,
        instrument_id=INSTRUMENT_ID,
        product_or_market_id=INSTRUMENT_ID,
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=NOW - timedelta(days=4),
        valid_until=NOW,
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=0,
        freshness_limit_seconds=1800,
        expected_gross_return=Decimal("0.01"),
        expected_net_return=Decimal("0.01"),
        comparable_value=None,
        comparable_value_identity=None,
        uncertainty=None,
        maximum_loss=Decimal("10"),
        modeled_fees=Decimal("0"),
        modeled_slippage=Decimal("0"),
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital=Decimal("10"),
        maximum_sensible_research_allocation=Decimal("10"),
        source_maximum_allocation_fraction=Decimal("1"),
        expected_holding_days=None,
        estimated_lockup_days=None,
        supporting_evidence_ids=("evidence",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
        statistical_sample_size=30,
    )
    eligibility = CryptoPaperEligibility(
        decision_id="legacy-eligibility",
        opportunity_set_id="legacy-set",
        projection_id=projection.projection_id,
        policy_id="TEST_POLICY_V1",
        evaluated_at=NOW - timedelta(days=4),
        state=PaperEligibilityState.ELIGIBLE,
        reasons=("ELIGIBLE_FOR_EXACT_OPERATOR_AUTHORIZED_PAPER_CONSIDERATION",),
        product_contract_id="legacy-contract",
        account_id=ACCOUNT_ID,
        canonical_hash=content_digest(("legacy-eligibility",)),
    )
    return CryptoPaperAuthorization.create(
        authorization_id=authorization_id,
        account_id=ACCOUNT_ID,
        eligibility=eligibility,
        projection=projection,
        maximum_quote_notional=Decimal("10"),
        maximum_base_size=None,
        allowed_side=PaperSide.BUY,
        allowed_order_type=PaperOrderType.MARKET,
        expires_at=NOW - timedelta(days=3),
        actor="system:test",
        reason=f"strategy-paper-trial:{strategy_id}",
        idempotency_key=f"{authorization_id}-key",
        created_at=NOW - timedelta(days=4),
    )


def _legacy_order(*, authorization: CryptoPaperAuthorization) -> CryptoPaperOrder:
    values = (
        f"{authorization.authorization_id}-order",
        ACCOUNT_ID,
        f"{authorization.authorization_id}-intent",
        f"{authorization.authorization_id}-preview",
        authorization.authorization_id,
        INSTRUMENT_ID,
        "BTC",
        "USD",
        PaperSide.BUY,
        PaperOrderType.MARKET,
        Decimal("0.00012687"),
        None,
        Decimal("0.00012687"),
        Decimal("10"),
        Decimal("0.06"),
        PaperOrderState.FILLED,
        NOW - timedelta(days=4),
        NOW - timedelta(days=4) + timedelta(hours=4),
        1,
        "PAPER_ONLY",
        "NONE",
    )
    return CryptoPaperOrder(*values[:-2], content_digest(values), *values[-2:])


class TestLegacyAdoption:
    def test_fixture_order_is_genuinely_well_formed(self) -> None:
        # Guards the hand-rolled positional CryptoPaperOrder construction
        # in _legacy_order() above -- a slicing mistake there would leave
        # canonical_hash/last_sequence/paper_authority silently populated
        # with the wrong values without raising (dataclasses do not
        # runtime-check field types), so assert on them explicitly here
        # rather than trusting the other tests' narrower assertions alone.
        authorization = _legacy_authorization(
            authorization_id="legacy-authorization", strategy_id=STRATEGY_ID
        )
        order = _legacy_order(authorization=authorization)
        assert order.last_sequence == 1
        assert order.paper_authority == "PAPER_ONLY"
        assert order.live_authority == "NONE"
        assert len(order.canonical_hash) == 64

    def test_adopts_generation_one_from_retained_facts(self) -> None:
        authorization = _legacy_authorization(
            authorization_id="legacy-authorization", strategy_id=STRATEGY_ID
        )
        order = _legacy_order(authorization=authorization)
        record = adopt_legacy_trial(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            authorization=authorization,
            order=order,
            opening_realized_pnl_baseline=Decimal("0"),
        )
        assert record.generation == 1
        assert record.legacy_adopted is True
        assert record.opened_at == order.submitted_at
        assert record.opening_authorization_id == authorization.authorization_id
        assert record.opening_order_id == order.order_id

    def test_rejects_authorization_belonging_to_a_different_strategy(self) -> None:
        authorization = _legacy_authorization(
            authorization_id="legacy-authorization", strategy_id="a-different-strategy"
        )
        order = _legacy_order(authorization=authorization)
        with pytest.raises(StrategyPaperTrialLifecycleError):
            adopt_legacy_trial(
                strategy_id=STRATEGY_ID,
                strategy_version=1,
                instrument_id=INSTRUMENT_ID,
                account_id=ACCOUNT_ID,
                authorization=authorization,
                order=order,
                opening_realized_pnl_baseline=Decimal("0"),
            )

    def test_rejects_order_referencing_a_different_authorization(self) -> None:
        authorization = _legacy_authorization(
            authorization_id="legacy-authorization", strategy_id=STRATEGY_ID
        )
        mismatched_authorization = _legacy_authorization(
            authorization_id="other-authorization", strategy_id=STRATEGY_ID
        )
        order = _legacy_order(authorization=mismatched_authorization)
        with pytest.raises(StrategyPaperTrialLifecycleError):
            adopt_legacy_trial(
                strategy_id=STRATEGY_ID,
                strategy_version=1,
                instrument_id=INSTRUMENT_ID,
                account_id=ACCOUNT_ID,
                authorization=authorization,
                order=order,
                opening_realized_pnl_baseline=Decimal("0"),
            )

    def test_no_live_authority_anywhere_on_adopted_or_built_records(self) -> None:
        authorization = _legacy_authorization(
            authorization_id="legacy-authorization", strategy_id=STRATEGY_ID
        )
        order = _legacy_order(authorization=authorization)
        record = adopt_legacy_trial(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            authorization=authorization,
            order=order,
            opening_realized_pnl_baseline=Decimal("0"),
        )
        assert record.authority == "NONE"
        closed_at = record.opened_at + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=record,
            closed_at=closed_at,
            closing_authorization_id="close-a",
            closing_order_id="close-o",
            realized_paper_pnl=Decimal("1"),
            coverage=_coverage(window_start=record.opened_at, window_end=closed_at),
        )
        assert closure.authority == "NONE"
