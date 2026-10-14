"""Securities Research -> Shared Opportunity Recurring Bridge v1: domain-level
proofs.

Adapter-layer code (``adapters.shared_opportunity_sources.
RetainedSharedOpportunitySource.securities_projections``, the repository
queries it composes, and the full recurring-cycle wiring) is exercised
against real retained evidence in
``tests/persistence/test_postgres_securities_shared_opportunity_bridge.py``
instead -- matching this codebase's own established split (CRYPTO/KALSHI's
own producer logic is likewise never unit-tested outside the persistence
tier; see ``tests/persistence/test_postgres_crypto_market_pulse.py``).  This
file proves the pure domain/application-layer contract: eligibility, ranking
coexistence, counterfactual-ledger lineage, the follow-up domain builder, the
recurring venue-dispatch fix, and outcome-cohort honesty -- all from genuine
domain objects, none hand-waved.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.counterfactual_followup import (
    KalshiMarketRead,
    SecuritiesPriceLookup,
    SyncFollowUpObservations,
)
from apex.domain.counterfactual_classification import ReasonFamily
from apex.domain.counterfactual_followup import (
    CounterfactualFollowUpError,
    FollowUpBaselineKind,
    FollowUpHorizonKind,
    FollowUpObservationType,
    build_securities_structure_terminal_observation,
    determine_followup_due_state,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
    build_decision_from_eligibility,
)
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    CounterfactualOutcomeQualificationState,
    build_counterfactual_outcome_aggregates,
)
from apex.domain.options_structures import StructureType, structure_payoff_at
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    EligibilityState,
    PriorityTier,
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
    evaluate_eligibility,
    prioritize,
)
from tests.test_counterfactual_followup import (
    _FakeDecisionSource,
    _FakeRepository,
    _FakeSubjectSource,
)

NOW = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)  # a Thursday, XNYS close


# ---------------------------------------------------------------------------
# Fixture builders -- mirror tests/test_counterfactual_outcome.py's own
# established hand-constructed style for CounterfactualDecision/observation
# fixtures (there is no lighter-weight seam for the outcome layer; the ledger
# builder itself is proven separately, from a genuine EligibilityDecision, in
# TestCounterfactualLedgerLineage below).
# ---------------------------------------------------------------------------


def securities_projection(
    *,
    structure_id: str = "structure-1",
    comparable_value: str | None = None,
    comparable_value_identity: str | None = None,
    evidence_class: str = "GENUINE_PUBLIC",
    freshness_seconds: int = 300,
    freshness_limit_seconds: int = 345600,
    valid_until: datetime | None = None,
    maximum_loss: str | None = "500",
    required_hypothetical_capital: str | None = "500",
    costs_retained: bool = True,
    liquidity_state: str = "SUFFICIENT",
    source_authority: str = "NONE",
    generated_at: datetime = NOW,
    expected_holding_days: int | None = 14,
    source_details: tuple[tuple[str, str], ...] | None = None,
) -> SharedOpportunityProjection:
    if source_details is None:
        source_details = (
            ("underlying_canonical_security_id", "security-spy-stable"),
            ("structure_type", "CALL_DEBIT_VERTICAL"),
            ("expiration", "2026-09-04"),
            ("long_strike", "495"),
            ("short_strike", "505"),
            ("multiplier", "100"),
            ("quantity", "1"),
            ("initial_cash_flow", "-320.00"),
            ("underlying_reference_price_at_decision", "500"),
            ("source_chain_snapshot_id", "snapshot-1"),
        )
    return SharedOpportunityProjection.create(
        projection_id=f"shared-securities-structure-{structure_id}",
        venue=SharedVenue.SECURITIES,
        source_record_type="OptionStructureCandidate",
        source_record_id=structure_id,
        source_canonical_hash="a" * 64,
        strategy_or_method_id="SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1",
        instrument_id="security-spy-stable",
        product_or_market_id=structure_id,
        direction="CALL_DEBIT_VERTICAL",
        generated_at=generated_at,
        valid_until=valid_until if valid_until is not None else generated_at + timedelta(days=4),
        evidence_class=evidence_class,
        freshness_seconds=freshness_seconds,
        freshness_limit_seconds=freshness_limit_seconds,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value=comparable_value,
        comparable_value_identity=comparable_value_identity,
        uncertainty=None,
        maximum_loss=maximum_loss,
        modeled_fees="1.30",
        modeled_slippage="2.00",
        costs_retained=costs_retained,
        liquidity_state=liquidity_state,
        liquidity_limitations=(),
        required_hypothetical_capital=required_hypothetical_capital,
        maximum_sensible_research_allocation=required_hypothetical_capital,
        source_maximum_allocation_fraction=None,
        expected_holding_days=expected_holding_days,
        estimated_lockup_days=expected_holding_days,
        supporting_evidence_ids=(structure_id, "snapshot-1", "obs-long-1", "obs-short-1"),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority=source_authority,
        projection_limitations=("EXPECTED_VALUE_UNAVAILABLE_NO_PROBABILITY_MODEL",),
        source_details=source_details,
    )


def crypto_projection(*, projection_id: str = "shared-crypto-1") -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=projection_id,
        venue=SharedVenue.CRYPTO,
        source_record_type="CryptoResearchOpportunity",
        source_record_id="crypto-1",
        source_canonical_hash="b" * 64,
        strategy_or_method_id="CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",
        instrument_id="BTC-USD",
        product_or_market_id="BTC-USD",
        direction="LONG",
        generated_at=NOW,
        valid_until=NOW + timedelta(hours=6),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        expected_gross_return="0.02",
        expected_net_return="0.015",
        comparable_value="0.015",
        comparable_value_identity="STRICTLY_PRIOR_WALK_FORWARD_EXPECTED_NET_RETURN",
        uncertainty="0.05",
        maximum_loss="100",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="100",
        source_maximum_allocation_fraction=None,
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=("crypto-evidence-1",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


def kalshi_projection(*, projection_id: str = "shared-kalshi-1") -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=projection_id,
        venue=SharedVenue.KALSHI,
        source_record_type="ExhaustiveEventSetEvaluation",
        source_record_id="kalshi-1",
        source_canonical_hash="c" * 64,
        strategy_or_method_id="KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1",
        instrument_id="KALSHI-EVT",
        product_or_market_id="KALSHI-MKT",
        direction="BUY_YES",
        generated_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=30,
        freshness_limit_seconds=300,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value="0.03",
        comparable_value_identity="GUARANTEED_SETTLEMENT_NET_GAP_OVER_MODELED_COST",
        uncertainty=None,
        maximum_loss="50",
        modeled_fees="0.5",
        modeled_slippage=None,
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="50",
        maximum_sensible_research_allocation="50",
        source_maximum_allocation_fraction=None,
        expected_holding_days=3,
        estimated_lockup_days=3,
        supporting_evidence_ids=("kalshi-evidence-1",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )


# ===========================================================================
# A. SharedVenue.SECURITIES
# ===========================================================================


def test_shared_venue_has_exactly_three_members_including_securities() -> None:
    assert set(SharedVenue) == {SharedVenue.CRYPTO, SharedVenue.KALSHI, SharedVenue.SECURITIES}


# ===========================================================================
# B. Eligibility -- conservative, fails closed (requirements 5, 7, 8)
# ===========================================================================


class TestSecuritiesEligibility:
    def test_valid_securities_structure_reaches_eligible(self) -> None:
        decision = evaluate_eligibility(securities_projection(), evaluated_at=NOW)
        assert decision.state is EligibilityState.ELIGIBLE
        assert "ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY" in decision.reasons

    def test_missing_maximum_loss_blocks_rather_than_fabricates(self) -> None:
        decision = evaluate_eligibility(securities_projection(maximum_loss=None), evaluated_at=NOW)
        assert decision.state is EligibilityState.BLOCKED_MISSING_EVIDENCE
        assert "MAXIMUM_LOSS_MISSING" in decision.reasons

    def test_missing_required_capital_blocks(self) -> None:
        decision = evaluate_eligibility(
            securities_projection(required_hypothetical_capital=None), evaluated_at=NOW
        )
        assert decision.state is EligibilityState.BLOCKED_MISSING_EVIDENCE
        assert "REQUIRED_CAPITAL_MISSING" in decision.reasons

    def test_fixture_evidence_class_is_rejected_never_masquerades_as_forward(self) -> None:
        """Historical-vs-forward proof (requirement 7): synthetic evidence can
        never reach ELIGIBLE, exactly like every other venue."""

        decision = evaluate_eligibility(
            securities_projection(evidence_class="FIXTURE"), evaluated_at=NOW
        )
        assert decision.state is EligibilityState.REJECTED
        assert "EVIDENCE_CLASS_NOT_PERMITTED" in decision.reasons

    def test_historical_public_is_permitted_but_never_implies_forward_conviction(self) -> None:
        """HISTORICAL_PUBLIC is a genuinely permitted evidence class (same
        allowlist every venue shares) -- but nothing about reaching ELIGIBLE
        here upgrades it to GENUINE_PUBLIC or implies current forward
        qualification; the projection's own ``evidence_class`` field is the
        single source of truth and is never silently rewritten."""

        projection = securities_projection(evidence_class="HISTORICAL_PUBLIC")
        decision = evaluate_eligibility(projection, evaluated_at=NOW)
        assert decision.state is EligibilityState.ELIGIBLE
        assert projection.evidence_class == "HISTORICAL_PUBLIC"

    def test_stale_evidence_beyond_freshness_window_is_rejected(self) -> None:
        stale = securities_projection(freshness_seconds=400000, freshness_limit_seconds=345600)
        decision = evaluate_eligibility(stale, evaluated_at=NOW)
        assert decision.state is EligibilityState.REJECTED
        assert "EVIDENCE_STALE" in decision.reasons

    def test_expired_valid_until_is_expired_not_merely_rejected(self) -> None:
        expired = securities_projection(valid_until=NOW - timedelta(seconds=1))
        decision = evaluate_eligibility(expired, evaluated_at=NOW)
        assert decision.state is EligibilityState.EXPIRED

    def test_non_none_source_authority_is_rejected(self) -> None:
        """Authority proof (requirement 14): a securities producer could
        never smuggle execution authority through even if it tried -- the
        shared eligibility gate rejects it exactly like every other venue."""

        decision = evaluate_eligibility(
            securities_projection(source_authority="PAPER"), evaluated_at=NOW
        )
        assert decision.state is EligibilityState.REJECTED
        assert "SOURCE_AUTHORITY_NOT_RESEARCH_ONLY" in decision.reasons

    def test_paper_and_live_eligibility_always_false_for_securities(self) -> None:
        projection = securities_projection()
        assert projection.paper_eligible is False
        assert projection.live_eligible is False
        assert projection.paper_eligibility_reasons == ("PAPER_ELIGIBILITY_SCOPED_TO_CRYPTO_VENUE",)
        assert projection.authority == "NONE"
        assert projection.capital_authority == "NONE"
        assert projection.financial_authority == "NONE"


# ===========================================================================
# C. Ranking coexistence + RESEARCH_ONLY tiering (requirements 9, 10, 11, 15)
# ===========================================================================


class TestSecuritiesRankingCoexistence:
    def test_securities_is_research_only_when_no_genuine_magnitude(self) -> None:
        projection = securities_projection()
        decision = evaluate_eligibility(projection, evaluated_at=NOW)
        (result,) = prioritize(
            (projection,), (decision,), opportunity_set_id="securities-research-only-set"
        )
        assert result.tier is PriorityTier.RESEARCH_ONLY
        assert any(
            reason.startswith("NO_GENUINE_ECONOMIC_MAGNITUDE") for reason in result.tier_reasons
        )

    def test_mixed_venue_set_ranks_all_three_without_crashing(self) -> None:
        projections = (securities_projection(), crypto_projection(), kalshi_projection())
        decisions = tuple(evaluate_eligibility(item, evaluated_at=NOW) for item in projections)
        results = prioritize(projections, decisions, opportunity_set_id="mixed-venue-set")
        assert {item.projection_id for item in results} == {
            item.projection_id for item in projections
        }
        by_id = {item.projection_id: item for item in results}
        securities_result = by_id[projections[0].projection_id]
        crypto_result = by_id[projections[1].projection_id]
        kalshi_result = by_id[projections[2].projection_id]
        assert securities_result.tier is PriorityTier.RESEARCH_ONLY
        # CRYPTO/KALSHI both carry genuine magnitude and clear the floor.
        assert crypto_result.tier is PriorityTier.ACTIONABLE
        assert kalshi_result.tier is PriorityTier.ACTIONABLE

    def test_crypto_score_is_identical_with_or_without_a_securities_peer(self) -> None:
        """Requirement 15 (ranking-specific): adding a SECURITIES projection
        to the opportunity set must not perturb CRYPTO's own score -- no
        shared mutable state, no cross-venue percentile leakage (SECURITIES
        has no genuine magnitude to begin with, but this proves it even at
        the score-component level, not merely "didn't crash")."""

        crypto = crypto_projection()
        crypto_decision = evaluate_eligibility(crypto, evaluated_at=NOW)
        (alone,) = prioritize((crypto,), (crypto_decision,), opportunity_set_id="crypto-alone-set")

        securities = securities_projection()
        securities_decision = evaluate_eligibility(securities, evaluated_at=NOW)
        results = prioritize(
            (crypto, securities),
            (crypto_decision, securities_decision),
            opportunity_set_id="crypto-with-securities-peer-set",
        )
        with_peer = next(item for item in results if item.projection_id == crypto.projection_id)

        assert with_peer.score == alone.score
        assert with_peer.tier is alone.tier is PriorityTier.ACTIONABLE

    def test_build_opportunity_set_includes_securities_with_full_provenance(self) -> None:
        """Requirement 6: provenance survives projection through the whole
        opportunity-set build."""

        projection = securities_projection()
        opportunity_set, decisions, priorities, explanations = build_opportunity_set(
            opportunity_set_id="set-securities-1",
            created_at=NOW,
            projections=(projection,),
        )
        assert projection.projection_id in opportunity_set.projection_ids
        assert projection.projection_id in opportunity_set.ranked_projection_ids
        (decision,) = decisions
        assert decision.state is EligibilityState.ELIGIBLE
        (priority,) = priorities
        assert priority.tier is PriorityTier.RESEARCH_ONLY
        (explanation,) = explanations
        assert explanation.eligibility_state is EligibilityState.ELIGIBLE
        assert opportunity_set.authority == "NONE"
        assert decision.authority == "NONE"
        assert priority.authority == "NONE"


# ===========================================================================
# D. Counterfactual ledger lineage (requirement 12)
# ===========================================================================


class TestCounterfactualLedgerLineage:
    def test_eligible_securities_decision_retains_as_advanced(self) -> None:
        projection = securities_projection()
        eligibility = evaluate_eligibility(projection, evaluated_at=NOW)
        decision = build_decision_from_eligibility(
            decision=eligibility, projection=projection, opportunity_set_id="set-1"
        )
        assert decision.venue is SharedVenue.SECURITIES
        assert decision.disposition is DecisionDisposition.ADVANCED
        assert decision.origin is DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY
        assert decision.subject_projection_id == projection.projection_id
        assert decision.authority == "NONE"

    def test_rejected_securities_decision_is_retained_too_not_only_eligible(self) -> None:
        """'Both cohorts, always' -- a rejected SECURITIES candidate is
        retained exactly like a rejected CRYPTO/KALSHI one, never silently
        dropped (avoids survivorship bias in the ledger)."""

        projection = securities_projection(evidence_class="FIXTURE")
        eligibility = evaluate_eligibility(projection, evaluated_at=NOW)
        assert eligibility.state is EligibilityState.REJECTED
        decision = build_decision_from_eligibility(
            decision=eligibility, projection=projection, opportunity_set_id="set-1"
        )
        assert decision.disposition is DecisionDisposition.NOT_ADVANCED
        assert decision.venue is SharedVenue.SECURITIES


# ===========================================================================
# E. Follow-up domain builder (requirement 13, follow-up half)
# ===========================================================================


class TestSecuritiesFollowUpBuilder:
    def _decision(self) -> CounterfactualDecision:
        return CounterfactualDecision(
            decision_id="decision-securities-1",
            origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
            origin_decision_id="decision-securities-1-origin",
            origin_canonical_hash="a" * 64,
            origin_state="ELIGIBLE",
            subject_projection_id="shared-securities-structure-structure-1",
            subject_source_record_type="OptionStructureCandidate",
            subject_source_record_id="structure-1",
            venue=SharedVenue.SECURITIES,
            opportunity_set_id="set-1",
            mandate_id=None,
            mandate_lineage_id=None,
            mandate_version=None,
            mandate_hash=None,
            portfolio_snapshot_id=None,
            portfolio_snapshot_hash=None,
            evaluated_at=NOW,
            disposition=DecisionDisposition.ADVANCED,
            raw_reason_codes=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
            explanation="test",
            canonical_hash="b" * 64,
            authority="NONE",
        )

    def test_terminal_payoff_matches_the_reused_structure_payoff_function(self) -> None:
        decision = self._decision()
        observation = build_securities_structure_terminal_observation(
            decision=decision,
            horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
            horizon_days=14,
            target_time=NOW + timedelta(days=14),
            subject_instrument_id="security-spy-stable",
            subject_product_or_market_id="structure-1",
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_strike=Decimal("495"),
            short_strike=Decimal("505"),
            multiplier=Decimal("100"),
            quantity=1,
            initial_cash_flow=Decimal("-320.00"),
            capital_required=Decimal("320.00"),
            underlying_price_at_baseline=Decimal("500"),
            underlying_price_at_baseline_source_id="snapshot-baseline",
            underlying_price_at_target=Decimal("510"),
            underlying_price_at_target_source_id="snapshot-target",
            observed_at=NOW + timedelta(days=14),
            policy_version="FOLLOWUP_POLICY_V2",
        )
        expected_payoff = structure_payoff_at(
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_strike=Decimal("495"),
            short_strike=Decimal("505"),
            multiplier=Decimal("100"),
            quantity=1,
            initial_cash_flow=Decimal("-320.00"),
            underlying_price_at_expiration=Decimal("510"),
        )
        expected_return = expected_payoff.net_profit_loss / Decimal("320.00")
        assert observation.venue is SharedVenue.SECURITIES
        assert observation.observation_type is (
            FollowUpObservationType.SECURITIES_STRUCTURE_TERMINAL_PAYOFF
        )
        assert observation.baseline_kind is FollowUpBaselineKind.DECISION_TIME
        assert observation.subsequent_return == expected_return.quantize(
            observation.subsequent_return
        )
        assert observation.reference_price_at_baseline == Decimal("500")
        assert observation.reference_price_at_target == Decimal("510")
        assert observation.provenance is EvidenceClass.GENUINE_PUBLIC
        assert observation.market_status_at_target is None  # KALSHI-only field, unused
        assert observation.authority == "NONE"
        assert (
            "SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT"
            in (observation.data_limitations)
        )

    def test_idempotent_replay_reproduces_identical_id_and_hash(self) -> None:
        decision = self._decision()
        kwargs = dict(
            decision=decision,
            horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
            horizon_days=1,
            target_time=NOW + timedelta(days=1),
            subject_instrument_id="security-spy-stable",
            subject_product_or_market_id="structure-1",
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_strike=Decimal("495"),
            short_strike=Decimal("505"),
            multiplier=Decimal("100"),
            quantity=1,
            initial_cash_flow=Decimal("-320.00"),
            capital_required=Decimal("320.00"),
            underlying_price_at_baseline=Decimal("500"),
            underlying_price_at_baseline_source_id="snapshot-baseline",
            underlying_price_at_target=Decimal("495"),
            underlying_price_at_target_source_id="snapshot-target",
            observed_at=NOW + timedelta(days=1),
            policy_version="FOLLOWUP_POLICY_V2",
        )
        first = build_securities_structure_terminal_observation(**kwargs)
        second = build_securities_structure_terminal_observation(**kwargs)
        assert first.observation_id == second.observation_id
        assert first.canonical_hash == second.canonical_hash

    def test_nonpositive_reference_price_fails_closed(self) -> None:
        decision = self._decision()
        with pytest.raises(CounterfactualFollowUpError):
            build_securities_structure_terminal_observation(
                decision=decision,
                horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
                horizon_days=1,
                target_time=NOW + timedelta(days=1),
                subject_instrument_id="security-spy-stable",
                subject_product_or_market_id="structure-1",
                structure_type=StructureType.CALL_DEBIT_VERTICAL,
                long_strike=Decimal("495"),
                short_strike=Decimal("505"),
                multiplier=Decimal("100"),
                quantity=1,
                initial_cash_flow=Decimal("-320.00"),
                capital_required=Decimal("320.00"),
                underlying_price_at_baseline=Decimal("0"),
                underlying_price_at_baseline_source_id="snapshot-baseline",
                underlying_price_at_target=Decimal("495"),
                underlying_price_at_target_source_id="snapshot-target",
                observed_at=NOW + timedelta(days=1),
                policy_version="FOLLOWUP_POLICY_V2",
            )

    def test_nonpositive_capital_required_fails_closed(self) -> None:
        decision = self._decision()
        with pytest.raises(CounterfactualFollowUpError):
            build_securities_structure_terminal_observation(
                decision=decision,
                horizon_kind=FollowUpHorizonKind.SHORT_HORIZON,
                horizon_days=1,
                target_time=NOW + timedelta(days=1),
                subject_instrument_id="security-spy-stable",
                subject_product_or_market_id="structure-1",
                structure_type=StructureType.CALL_DEBIT_VERTICAL,
                long_strike=Decimal("495"),
                short_strike=Decimal("505"),
                multiplier=Decimal("100"),
                quantity=1,
                initial_cash_flow=Decimal("-320.00"),
                capital_required=Decimal("0"),
                underlying_price_at_baseline=Decimal("500"),
                underlying_price_at_baseline_source_id="snapshot-baseline",
                underlying_price_at_target=Decimal("495"),
                underlying_price_at_target_source_id="snapshot-target",
                observed_at=NOW + timedelta(days=1),
                policy_version="FOLLOWUP_POLICY_V2",
            )


def test_determine_followup_due_state_is_unchanged_and_venue_agnostic() -> None:
    """The due-state calculation SECURITIES reuses unmodified: proves
    NOT_YET_DUE / DUE / UNSUPPORTED all still work exactly as CRYPTO/KALSHI's
    own tests already prove -- this is a light confirmation the bridge did
    not need to (and did not) touch this function."""

    not_yet_due = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=14,
        as_of=NOW + timedelta(days=1),
        already_observed=False,
    )
    assert not_yet_due.state.value == "NOT_YET_DUE"

    due = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=14,
        as_of=NOW + timedelta(days=14),
        already_observed=False,
    )
    assert due.state.value == "DUE"
    assert due.target_time == NOW + timedelta(days=14)

    unsupported = determine_followup_due_state(
        horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
        horizon_anchor_at=NOW,
        expected_holding_days=None,
        as_of=NOW + timedelta(days=14),
        already_observed=False,
    )
    assert unsupported.state.value == "UNSUPPORTED"


# ===========================================================================
# F. Recurring venue-dispatch fix -- the concrete latent bug found and fixed
# while adding this venue (requirement 13/14, plus the "no financial/provider
# mutation is possible" proof).
# ===========================================================================


class _RaisingKalshiSource:
    """Proves the dispatch fix: if a SECURITIES candidate were ever
    misrouted here (the exact latent bug found and fixed while adding this
    venue), this test would fail loudly instead of silently mis-attributing
    evidence."""

    def read_market_status(self, **_: object) -> KalshiMarketRead:
        raise AssertionError("SECURITIES candidate was misrouted into the KALSHI market source")


class _FakeSecuritiesPrices:
    def __init__(self, lookup: SecuritiesPriceLookup | None) -> None:
        self._lookup = lookup

    def earliest_reference_price_at_or_after(
        self, *, underlying_canonical_security_id: str, at_or_after: datetime
    ) -> SecuritiesPriceLookup | None:
        return self._lookup


def _due_securities_decision() -> tuple[CounterfactualDecision, SharedOpportunityProjection]:
    projection = securities_projection(expected_holding_days=1, generated_at=NOW)
    decision = CounterfactualDecision(
        decision_id="decision-securities-due",
        origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
        origin_decision_id="decision-securities-due-origin",
        origin_canonical_hash="a" * 64,
        origin_state="ELIGIBLE",
        subject_projection_id=projection.projection_id,
        subject_source_record_type="OptionStructureCandidate",
        subject_source_record_id="structure-1",
        venue=SharedVenue.SECURITIES,
        opportunity_set_id="set-1",
        mandate_id=None,
        mandate_lineage_id=None,
        mandate_version=None,
        mandate_hash=None,
        portfolio_snapshot_id=None,
        portfolio_snapshot_hash=None,
        evaluated_at=NOW,
        disposition=DecisionDisposition.ADVANCED,
        raw_reason_codes=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
        explanation="test",
        canonical_hash="b" * 64,
        authority="NONE",
    )
    return decision, projection


class TestSecuritiesRecurringDispatch:
    def test_due_securities_candidate_never_reaches_the_kalshi_source(self) -> None:
        decision, projection = _due_securities_decision()
        repository = _FakeRepository()
        target_lookup = SecuritiesPriceLookup(
            underlying_reference_price=Decimal("510"),
            source_snapshot_id="snapshot-target",
            captured_at=NOW + timedelta(days=1),
        )
        result = SyncFollowUpObservations(
            _FakeDecisionSource((decision,)),
            _FakeSubjectSource({projection.projection_id: projection}),
            crypto_prices=object(),  # never touched for a SECURITIES-only run
            kalshi_source=_RaisingKalshiSource(),
            repository=repository,
            securities_prices=_FakeSecuritiesPrices(target_lookup),
        ).execute(as_of=NOW + timedelta(days=1), policy_version="FOLLOWUP_POLICY_V2")

        assert result.created_count >= 1
        saved = repository.observation(result.entries[0].observation_id)
        assert saved is not None
        assert saved.venue is SharedVenue.SECURITIES
        assert saved.authority == "NONE"

    def test_unconfigured_securities_source_fails_closed_never_crashes(self) -> None:
        """``securities_prices=None`` (every one of the ~39 pre-existing call
        sites this constructor already had) must never crash on a SECURITIES
        candidate -- it fails closed to BLOCKED_UNAVAILABLE, exactly as if
        the terminal price genuinely were not yet retained."""

        decision, projection = _due_securities_decision()
        repository = _FakeRepository()
        result = SyncFollowUpObservations(
            _FakeDecisionSource((decision,)),
            _FakeSubjectSource({projection.projection_id: projection}),
            crypto_prices=object(),
            kalshi_source=_RaisingKalshiSource(),
            repository=repository,
        ).execute(as_of=NOW + timedelta(days=1), policy_version="FOLLOWUP_POLICY_V2")

        assert result.created_count == 0
        assert result.blocked_count >= 1
        assert result.entries[0].reasons == ("SECURITIES_PRICE_SOURCE_NOT_CONFIGURED",)

    def test_malformed_source_details_fails_closed(self) -> None:
        """A projection missing the structure-reconstruction keys (e.g. one
        retained before this bridge, or a genuine upstream defect) must
        block, never crash and never fabricate a structure shape."""

        decision, projection = _due_securities_decision()
        broken_projection = replace(projection, source_details=())
        repository = _FakeRepository()
        result = SyncFollowUpObservations(
            _FakeDecisionSource((decision,)),
            _FakeSubjectSource({projection.projection_id: broken_projection}),
            crypto_prices=object(),
            kalshi_source=_RaisingKalshiSource(),
            repository=repository,
            securities_prices=_FakeSecuritiesPrices(None),
        ).execute(as_of=NOW + timedelta(days=1), policy_version="FOLLOWUP_POLICY_V2")

        assert result.created_count == 0
        assert result.blocked_count >= 1
        assert result.entries[0].reasons == ("SECURITIES_STRUCTURE_SOURCE_DETAILS_NOT_RESOLVABLE",)


# ===========================================================================
# G. Outcome-cohort honesty (requirement 13, outcome half)
# ===========================================================================


class TestSecuritiesOutcomeCohort:
    def test_securities_cohort_is_honestly_reduction_unavailable_not_crashed(self) -> None:
        """Confirms the exact live-crash risk found and closed while adding
        this venue: the *first* SECURITIES follow-up observation ever
        retained would, without migration 0104's widened
        ``ck_counterfactual_outcome_venue``, have failed a CHECK constraint
        the moment this honest, structurally-correct aggregate tried to
        persist. Here (pure domain, no DB) it proves the *computation* itself
        was already safe -- ``_decision_is_economically_reducible`` and
        ``_cohort_venue_is_in_scope_for_policy_version`` are membership
        tests, not exhaustive enum matches, so neither raises for an
        unrecognized venue."""

        from apex.domain.counterfactual_classification import (
            CounterfactualDecisionClassification,
        )

        decision = CounterfactualDecision(
            decision_id="decision-securities-outcome",
            origin=DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY,
            origin_decision_id="decision-securities-outcome-origin",
            origin_canonical_hash="a" * 64,
            origin_state="ELIGIBLE",
            subject_projection_id="projection-securities-outcome",
            subject_source_record_type="OptionStructureCandidate",
            subject_source_record_id="structure-1",
            venue=SharedVenue.SECURITIES,
            opportunity_set_id="set-1",
            mandate_id=None,
            mandate_lineage_id=None,
            mandate_version=None,
            mandate_hash=None,
            portfolio_snapshot_id=None,
            portfolio_snapshot_hash=None,
            evaluated_at=NOW,
            disposition=DecisionDisposition.ADVANCED,
            raw_reason_codes=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
            explanation="test",
            canonical_hash="b" * 64,
            authority="NONE",
        )
        classification = CounterfactualDecisionClassification(
            classification_id="decision-securities-outcome-classification",
            decision_id=decision.decision_id,
            decision_canonical_hash=decision.canonical_hash,
            classifier_version="REASON_CLASSIFIER_V2",
            classifier_policy_hash="c" * 64,
            reason_families=(ReasonFamily.RESEARCH_CONFIDENCE,),
            classified_at=NOW,
            canonical_hash="d" * 64,
            authority="NONE",
        )
        observation = build_securities_structure_terminal_observation(
            decision=decision,
            horizon_kind=FollowUpHorizonKind.STRATEGY_HORIZON,
            horizon_days=14,
            target_time=NOW + timedelta(days=14),
            subject_instrument_id="security-spy-stable",
            subject_product_or_market_id="structure-1",
            structure_type=StructureType.CALL_DEBIT_VERTICAL,
            long_strike=Decimal("495"),
            short_strike=Decimal("505"),
            multiplier=Decimal("100"),
            quantity=1,
            initial_cash_flow=Decimal("-320.00"),
            capital_required=Decimal("320.00"),
            underlying_price_at_baseline=Decimal("500"),
            underlying_price_at_baseline_source_id="snapshot-baseline",
            underlying_price_at_target=Decimal("510"),
            underlying_price_at_target_source_id="snapshot-target",
            observed_at=NOW + timedelta(days=14),
            policy_version="FOLLOWUP_POLICY_V2",
        )

        aggregates = build_counterfactual_outcome_aggregates(
            decisions=(decision,),
            classifications=(classification,),
            observations=(observation,),
            assessments=(),
            strategy_or_method_id_by_projection={
                decision.subject_projection_id: "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"
            },
            run_id="run-securities-outcome-1",
            generated_at=NOW,
            outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        )

        assert len(aggregates) == 1
        (aggregate,) = aggregates
        assert aggregate.venue is SharedVenue.SECURITIES
        assert (
            aggregate.qualification_state
            is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
        )
        assert aggregate.mean_subsequent_return is None
        assert aggregate.authority == "NONE"
