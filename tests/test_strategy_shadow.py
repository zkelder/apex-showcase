from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.autopilot import StrategyAuthorityState, StrategyEvidenceState
from apex.application.strategy_shadow import (
    OPERATOR_THESIS_STRATEGY_ID,
    OperatorThesisInput,
    RetainedMarketEvidence,
    StrategyAuthorityCommand,
    authority_transition_reasons,
    build_market_consensus_baseline_prediction,
    build_operator_thesis_opportunity,
    build_operator_thesis_prediction,
    score_prediction,
    strategy_scorecard,
)
from apex.domain.values import ContentHash, ValueValidationError, content_digest

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _market() -> RetainedMarketEvidence:
    rule_text = (
        "A retained public binary contract resolves YES exactly when the stated event occurs."
    )
    return RetainedMarketEvidence(
        snapshot_id="retained-market-1",
        provider="KALSHI",
        provider_market_ticker="KXTEST-26JUL23",
        provider_event_ticker="KXTEST-26JUL23-EVENT",
        observed_at=NOW - timedelta(seconds=2),
        available_at=NOW - timedelta(seconds=1),
        close_at=NOW + timedelta(hours=1),
        rule_text=rule_text,
        rule_content_hash=ContentHash(content_digest({"rule": rule_text})),
        yes_bid=Decimal("0.54"),
        yes_ask=Decimal("0.56"),
        no_ask=Decimal("0.46"),
        visible_yes_quantity=Decimal("10"),
        fee_model_version=None,
        expected_fee=Decimal("0"),
    )


def _thesis(*, conservative_yes: str = "0.70") -> OperatorThesisInput:
    return OperatorThesisInput(
        campaign_id="shadow-campaign",
        point_probabilities=(("NO", "0.20"), ("YES", "0.80")),
        conservative_probabilities=(
            ("NO", str(Decimal(1) - Decimal(conservative_yes))),
            ("YES", conservative_yes),
        ),
        resolution_at=NOW + timedelta(hours=2),
        requested_risk_fraction="0.05",
        thesis_summary="Retained public evidence supports a bounded forward forecast.",
        evidence_references=("operator-note-1",),
        correlation_buckets=("macro",),
        operator_note="No payout, fee, or execution price was supplied by the operator.",
        idempotency_key=f"thesis-{conservative_yes}",
        requested_at=NOW,
    )


def test_operator_thesis_uses_authoritative_market_terms_for_positive_value() -> None:
    market = _market()
    opportunity = build_operator_thesis_opportunity(market=market, thesis=_thesis())

    assert opportunity is not None
    assert opportunity.proposal.instrument_id == market.provider_market_ticker
    assert opportunity.proposal.executable_ask.value == Decimal("0.56")
    assert opportunity.economics.expected_fee == Decimal(0)
    assert opportunity.economics.conservative_net_expected_value == Decimal("0.14")
    assert (
        build_operator_thesis_opportunity(market=market, thesis=_thesis(conservative_yes="0.56"))
        is None
    )


def test_forward_prediction_is_immutable_input_and_scores_only_a_matching_outcome_identity() -> (
    None
):
    prediction = build_operator_thesis_prediction(
        market=_market(),
        thesis=_thesis(),
        cycle_id="cycle-1",
        invocation_id="invocation-1",
        proposal_id="proposal-1",
    )

    evaluation = score_prediction(
        prediction=prediction,
        settlement_fact_id="retained-settlement-1",
        resolved_outcome="YES",
        evaluated_at=NOW + timedelta(hours=3),
    )

    assert evaluation.brier_score == Decimal("0.04")
    assert evaluation.market_baseline_brier_score == Decimal("0.2025")
    assert evaluation.probability_advantage == Decimal("0.25")
    assert evaluation.direction_correct is True
    assert prediction.thesis_summary == _thesis().thesis_summary
    assert prediction.correlation_buckets == ("macro",)
    assert prediction.operator_note == _thesis().operator_note
    with pytest.raises(ValueValidationError, match="not in the prediction outcome set"):
        score_prediction(
            prediction=prediction,
            settlement_fact_id="retained-settlement-2",
            resolved_outcome="VOID",
            evaluated_at=NOW + timedelta(hours=3),
        )


def test_baseline_creates_no_proposal_and_scorecard_groups_correlated_event_rows() -> None:
    market = _market()
    baseline = build_market_consensus_baseline_prediction(
        market=market, campaign_id="shadow-campaign", decision_at=NOW
    )
    assert baseline.strategy_id != OPERATOR_THESIS_STRATEGY_ID
    assert baseline.proposal_id is None

    thesis = build_operator_thesis_prediction(
        market=market,
        thesis=_thesis(),
        cycle_id="cycle-1",
        invocation_id="invocation-1",
        proposal_id="proposal-1",
    )
    evaluation = score_prediction(
        prediction=thesis,
        settlement_fact_id="retained-settlement-1",
        resolved_outcome="YES",
        evaluated_at=NOW + timedelta(hours=3),
    )
    scorecard = strategy_scorecard(
        strategy_id=OPERATOR_THESIS_STRATEGY_ID,
        predictions=(thesis, thesis),
        evaluations=(evaluation,),
    )
    assert scorecard["resolved_predictions"] == 1
    assert scorecard["distinct_events"] == 1
    assert scorecard["integrity_warnings"] == ()


def test_prediction_correlation_buckets_and_operator_note_use_bounded_canonical_terms() -> None:
    baseline = build_market_consensus_baseline_prediction(
        market=_market(), campaign_id="shadow-campaign", decision_at=NOW
    )

    with pytest.raises(ValueValidationError, match="unique and use sorted order"):
        replace(baseline, correlation_buckets=("macro", "macro"))
    with pytest.raises(ValueValidationError, match="correlation bucket must be non-empty"):
        replace(baseline, correlation_buckets=("",))
    with pytest.raises(ValueValidationError, match="correlation bucket must be non-empty"):
        replace(baseline, correlation_buckets=("x" * 81,))
    with pytest.raises(ValueValidationError, match="operator note must be at most 500"):
        replace(baseline, operator_note="x" * 501)


def test_authority_rules_are_fail_closed_and_paper_requires_a_trial() -> None:
    assert (
        authority_transition_reasons(
            current=StrategyAuthorityState.SHADOW,
            requested=StrategyAuthorityState.PAUSED,
            evidence_state=StrategyEvidenceState.INCOMPLETE,
            confirmation_text=None,
        )
        == ()
    )
    assert authority_transition_reasons(
        current=StrategyAuthorityState.RESEARCH,
        requested=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        confirmation_text=None,
    ) == ("EVIDENCE_REVIEW_REQUIREMENTS_UNMET",)
    assert authority_transition_reasons(
        current=StrategyAuthorityState.REJECTED,
        requested=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        confirmation_text=None,
    ) == ("REJECTED_STRATEGY_CANNOT_REACTIVATE",)
    with pytest.raises(ValueValidationError, match="paper trial authorization ID"):
        StrategyAuthorityCommand(
            strategy_id=OPERATOR_THESIS_STRATEGY_ID,
            requested_authority=StrategyAuthorityState.PAPER,
            operator_reason="not permitted",
            evidence_review_id="review-1",
            idempotency_key="authority-1",
            confirmation_text=None,
            requested_at=NOW,
        )
    assert authority_transition_reasons(
        current=StrategyAuthorityState.SHADOW,
        requested=StrategyAuthorityState.PAPER,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        confirmation_text=None,
    ) == ("PAPER_TRIAL_AUTHORIZATION_REQUIRED",)
