"""Governed SHADOW-only conversion of retained temporal price features.

The strategy accepts immutable market snapshots only.  It cannot reach an
orderbook-pressure signal, a provider client, or a financial repository.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from apex.application.autopilot import (
    AutopilotStrategyContext,
    InvocationState,
    ShadowOpportunity,
    StrategyAuthorityState,
    StrategyDescriptor,
    StrategyEvidenceState,
    StrategyFamily,
    StrategyImplementationState,
    StrategyInvocationResult,
)
from apex.application.strategy_shadow import (
    RetainedMarketEvidence,
    ShadowPrediction,
    ShadowPredictionState,
    binary_market_probabilities,
)
from apex.domain.autopilot import EvidenceTier, OpportunityEconomics, OpportunityOutcome
from apex.domain.decisions import OpportunityProposal, Side
from apex.domain.market import (
    ContractRuleVersion,
    InformationSet,
    InformationSetBuilder,
    Instrument,
    MarketSnapshot,
    SourceRecord,
)
from apex.domain.market_dynamics import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    MarketDynamicsFeatureSnapshot,
    MarketDynamicsProbability,
    MarketDynamicsStrategyConfig,
    derive_market_dynamics_probability,
)
from apex.domain.values import (
    ContentHash,
    InformationCutoff,
    Money,
    Price,
    Quantity,
    RiskFraction,
    ValueValidationError,
    content_digest,
    deterministic_id,
)


def market_dynamics_strategy_descriptor(
    configuration: MarketDynamicsStrategyConfig | None = None,
) -> StrategyDescriptor:
    config = configuration or MarketDynamicsStrategyConfig()
    return StrategyDescriptor(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        display_name="Kalshi Market Dynamics",
        family=StrategyFamily.MARKET_DYNAMICS,
        implementation_state=StrategyImplementationState.IMPLEMENTED,
        authority_state=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        last_evidence_decision="FORWARD_VALIDATION",
        supported_instrument_class="ACTIVE_BINARY_KALSHI_MARKET",
        maximum_proposals_per_cycle=config.maximum_markets_per_invocation,
        configuration_hash=config.canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class MarketDynamicsShadowEvaluation:
    feature: MarketDynamicsFeatureSnapshot
    probability: MarketDynamicsProbability | None
    prediction: ShadowPrediction | None
    opportunity: ShadowOpportunity | None
    reason_codes: tuple[str, ...]


class MarketDynamicsShadowWriter(Protocol):
    """Narrow immutable output port; no financial state is reachable here."""

    def persist_market_dynamics_feature(self, feature: MarketDynamicsFeatureSnapshot) -> bool: ...

    def persist_prediction(self, prediction: ShadowPrediction) -> bool: ...

    def link_market_dynamics_prediction(
        self, *, prediction: ShadowPrediction, feature_snapshot_id: str
    ) -> bool: ...


def _rule_and_information(
    *, market: RetainedMarketEvidence, feature: MarketDynamicsFeatureSnapshot, decision_at: datetime
) -> tuple[ContractRuleVersion, Instrument, Quantity, InformationSet]:
    if market.yes_ask is None:
        raise ValueValidationError("valid market dynamics evidence requires a retained YES ask")
    rule = ContractRuleVersion(
        rule_version_id=f"public-rule-{market.snapshot_id}",
        exact_text=market.rule_text,
        resolution_source=f"{market.provider}:retained-public-market",
        effective_at=market.observed_at - timedelta(days=1),
        close_at=market.close_at,
        determination_at=market.close_at,
        expected_settlement_at=market.close_at,
        content_hash=market.rule_content_hash,
    )
    instrument = Instrument(
        instrument_id=market.provider_market_ticker,
        category="PREDICTION_MARKET",
        payout_unit=Decimal(1),
        price_tick=Decimal("0.01"),
        quantity_increment=Decimal(1),
        yes_settlement_value=Decimal(1),
        no_settlement_value=Decimal(0),
        void_settlement_value=Decimal(0),
    )
    visible = Quantity(max(Decimal(1), market.visible_yes_quantity or Decimal(1)), Decimal(1))
    information = InformationSetBuilder.build(
        records=(
            SourceRecord(
                source_id=market.snapshot_id,
                source_event_at=market.observed_at,
                available_at=market.available_at,
                sequence=1,
                schema_version="kalshi-retained-market-dynamics-v1",
                content_hash=ContentHash(
                    content_digest(
                        {
                            "market_snapshot_id": market.snapshot_id,
                            "feature_snapshot_id": feature.feature_snapshot_id,
                        }
                    )
                ),
            ),
        ),
        instrument=instrument,
        cutoff=InformationCutoff(decision_at),
        rule=rule,
        market=MarketSnapshot(
            yes_ask=Price(market.yes_ask, Decimal("0.01")),
            no_ask=Price(market.no_ask, Decimal("0.01")),
            visible_yes_quantity=visible,
        ),
    )
    return rule, instrument, visible, information


def _modeled_fee(price: Decimal) -> Decimal:
    """Conservative public fee proxy; no execution claim or provider call."""
    return (Decimal("0.07") * price * (Decimal(1) - price)).quantize(Decimal("0.00000001"))


def evaluate_retained_market_dynamics(
    *,
    campaign_id: str,
    market: RetainedMarketEvidence,
    feature: MarketDynamicsFeatureSnapshot,
    configuration: MarketDynamicsStrategyConfig,
    decision_at: datetime,
    cycle_id: str | None = None,
    invocation_id: str | None = None,
) -> MarketDynamicsShadowEvaluation:
    """Create one prediction for every valid feature; proposal remains optional."""
    if not feature.valid:
        return MarketDynamicsShadowEvaluation(feature, None, None, None, feature.reason_codes)
    if decision_at >= market.close_at or market.available_at > decision_at:
        return MarketDynamicsShadowEvaluation(
            feature, None, None, None, ("MARKET_EVIDENCE_NOT_CURRENT_AT_DECISION",)
        )
    probability = derive_market_dynamics_probability(feature=feature, configuration=configuration)
    point = binary_market_probabilities(probability.point_yes_probability)
    conservative = binary_market_probabilities(probability.conservative_yes_probability)
    baseline = binary_market_probabilities(probability.market_yes_probability)
    payload = {
        "campaign_id": campaign_id,
        "strategy_id": STRATEGY_ID,
        "feature_snapshot_id": feature.feature_snapshot_id,
        "configuration_hash": configuration.canonical_hash.value,
        "decision_at": decision_at,
        "cycle_id": cycle_id,
    }
    prediction = ShadowPrediction(
        prediction_id=deterministic_id("market_dynamics_shadow_prediction", payload),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        campaign_id=campaign_id,
        cycle_id=cycle_id,
        invocation_id=invocation_id,
        proposal_id=None,
        provider=market.provider,
        instrument_id=market.provider_market_ticker,
        event_id=market.provider_event_ticker,
        decision_at=decision_at,
        resolution_at=market.close_at,
        point_probabilities=point,
        conservative_probabilities=conservative,
        market_probabilities=baseline,
        market_evidence_id=market.snapshot_id,
        expected_fee=Decimal(0),
        slippage_reserve=configuration.slippage_reserve,
        execution_uncertainty_reserve=configuration.execution_uncertainty_reserve,
        evidence_tier=EvidenceTier.RESEARCH.value,
        evidence_references=tuple(
            dict.fromkeys(
                item
                for item in (
                    market.snapshot_id,
                    feature.feature_snapshot_id,
                    feature.first_market_snapshot_id,
                    feature.latest_market_snapshot_id,
                )
                if item
            )
        ),
        thesis_summary="Fixed temporal market-price dynamics adjustment; forward validation only.",
        correlation_buckets=(market.provider_event_ticker or market.provider_market_ticker,),
        operator_note="SHADOW only; no provider submission, order, position, or financial state.",
        model_configuration_version=configuration.configuration_version,
        canonical_input_hash=content_digest(payload),
        canonical_prediction_hash=content_digest(
            {
                "payload": payload,
                "feature_hash": feature.canonical_feature_hash.value,
                "probability": probability,
            }
        ),
        state=ShadowPredictionState.UNRESOLVED,
    )
    yes_cost = (
        market.yes_ask
        + _modeled_fee(market.yes_ask)
        + configuration.slippage_reserve
        + configuration.execution_uncertainty_reserve
    )
    no_cost = (
        market.no_ask
        + _modeled_fee(market.no_ask)
        + configuration.slippage_reserve
        + configuration.execution_uncertainty_reserve
    )
    side, advantage, ask, total_cost = max(
        (
            (Side.YES, dict(conservative)["YES"] - yes_cost, market.yes_ask, yes_cost),
            (Side.NO, dict(conservative)["NO"] - no_cost, market.no_ask, no_cost),
        ),
        key=lambda item: (item[1], item[0].value),
    )
    if advantage < configuration.minimum_conservative_advantage:
        return MarketDynamicsShadowEvaluation(
            feature,
            probability,
            prediction,
            None,
            ("NO_PROPOSAL_CONSERVATIVE_ADVANTAGE_BELOW_THRESHOLD",),
        )
    rule, instrument, visible, information = _rule_and_information(
        market=market, feature=feature, decision_at=decision_at
    )
    fee = _modeled_fee(ask)
    proposal_id = deterministic_id(
        "market_dynamics_shadow_proposal", {**payload, "side": side.value}
    )
    proposal = OpportunityProposal(
        proposal_id=proposal_id,
        instrument_id=instrument.instrument_id,
        rule_version_id=rule.rule_version_id,
        information_set_id=information.information_set_id,
        strategy_version=STRATEGY_VERSION,
        side=side,
        executable_ask=Price(ask, Decimal("0.01")),
        fee_model_version=configuration.expected_fee_model,
        fee_per_unit=Money(fee),
        expected_net_value_per_unit=dict(point)[side.value] - total_cost,
        maximum_loss_per_unit=Money(total_cost),
        requested_risk_fraction=RiskFraction("0.01"),
        expires_at=market.close_at,
        fingerprint=content_digest({**payload, "side": side.value}),
    )
    economics = OpportunityEconomics(
        proposal_id=proposal_id,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        instrument_id=instrument.instrument_id,
        decision_at=decision_at,
        capital_release_at=market.close_at,
        outcomes=tuple(
            OpportunityOutcome(
                outcome_id=outcome_id,
                point_probability=point_value,
                conservative_probability=dict(conservative)[outcome_id],
                payout=Decimal(1) if outcome_id == side.value else Decimal(0),
            )
            for outcome_id, point_value in point
        ),
        acquisition_cost=ask,
        expected_fee=fee,
        slippage_reserve=configuration.slippage_reserve,
        execution_uncertainty_reserve=configuration.execution_uncertainty_reserve,
        visible_quantity=visible,
        requested_risk_fraction=RiskFraction("0.01"),
        evidence_tier=EvidenceTier.RESEARCH,
        evidence_references=prediction.evidence_references,
        correlation_buckets=prediction.correlation_buckets,
        configuration_version=configuration.configuration_version,
        configuration_hash=configuration.canonical_hash,
    )
    prediction = replace(prediction, proposal_id=proposal_id)
    return MarketDynamicsShadowEvaluation(
        feature,
        probability,
        prediction,
        ShadowOpportunity(proposal, economics, information, market.snapshot_id),
        ("SHADOW_OPPORTUNITY_QUALIFIED",),
    )


class MarketDynamicsAutopilotStrategy:
    descriptor = market_dynamics_strategy_descriptor()

    def __init__(self, evaluations: tuple[MarketDynamicsShadowEvaluation, ...]) -> None:
        self._evaluations = evaluations

    def evaluate(self, context: AutopilotStrategyContext) -> StrategyInvocationResult:
        _ = context
        opportunities = tuple(
            item.opportunity for item in self._evaluations if item.opportunity is not None
        )
        reasons = tuple(
            sorted({reason for item in self._evaluations for reason in item.reason_codes})
        )
        return StrategyInvocationResult(
            state=InvocationState.COMPLETED,
            opportunities=opportunities,
            evidence_references=tuple(
                sorted({item.feature.feature_snapshot_id for item in self._evaluations})
            ),
            diagnostics=(
                ("evaluated_market_count", str(len(self._evaluations))),
                (
                    "prediction_count",
                    str(sum(item.prediction is not None for item in self._evaluations)),
                ),
                ("proposal_count", str(len(opportunities))),
            ),
            reason_codes=reasons or ("NO_MARKET_DYNAMICS_MARKETS_SELECTED",),
        )


class RunMarketDynamicsShadowStrategy:
    """Persist immutable feature/prediction evidence only; allocation stays in Autopilot."""

    def __init__(self, writer: MarketDynamicsShadowWriter) -> None:
        self._writer = writer

    def run(
        self,
        *,
        campaign_id: str,
        market: RetainedMarketEvidence,
        feature: MarketDynamicsFeatureSnapshot,
        configuration: MarketDynamicsStrategyConfig,
        decision_at: datetime,
        cycle_id: str | None = None,
        invocation_id: str | None = None,
        expected_configuration_hash: ContentHash | None = None,
        expected_configuration_version: str | None = None,
    ) -> MarketDynamicsShadowEvaluation:
        if expected_configuration_hash is not None and (
            configuration.canonical_hash != expected_configuration_hash
            or feature.configuration_hash != expected_configuration_hash
        ):
            return MarketDynamicsShadowEvaluation(
                feature, None, None, None, ("PLAN_STRATEGY_CONFIGURATION_MISMATCH",)
            )
        if (
            expected_configuration_version is not None
            and configuration.configuration_version != expected_configuration_version
        ):
            return MarketDynamicsShadowEvaluation(
                feature, None, None, None, ("CONFIGURATION_VERSION_MISMATCH",)
            )
        evaluation = evaluate_retained_market_dynamics(
            campaign_id=campaign_id,
            market=market,
            feature=feature,
            configuration=configuration,
            decision_at=decision_at,
            cycle_id=cycle_id,
            invocation_id=invocation_id,
        )
        self._writer.persist_market_dynamics_feature(feature)
        if evaluation.prediction is not None:
            self._writer.persist_prediction(evaluation.prediction)
            self._writer.link_market_dynamics_prediction(
                prediction=evaluation.prediction, feature_snapshot_id=feature.feature_snapshot_id
            )
        return evaluation
