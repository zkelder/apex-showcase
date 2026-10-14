"""Governed SHADOW-only conversion of retained microstructure features.

The service accepts only immutable public evidence.  It creates forward
predictions and optional ``ShadowOpportunity`` values for the existing
Autopilot assessment path; it has no financial repository dependency.
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
from apex.domain.orderbook_microstructure import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    MicrostructureFeatureSnapshot,
    MicrostructureProbability,
    OrderbookMicrostructureStrategyConfig,
    derive_microstructure_probability,
)
from apex.domain.public_complement import FEE_MODEL_IDENTITY, ROUNDING_IDENTITY, SeriesFeeSnapshot
from apex.domain.published_fee_schedule import (
    FeeLiquidityRole,
    PublishedFeeScheduleApplicability,
    reconcile_kalshi_published_fee_schedule,
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


def orderbook_microstructure_strategy_descriptor(
    configuration: OrderbookMicrostructureStrategyConfig | None = None,
) -> StrategyDescriptor:
    config = configuration or OrderbookMicrostructureStrategyConfig()
    return StrategyDescriptor(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        display_name="Kalshi Orderbook Microstructure",
        family=StrategyFamily.MARKET_STRUCTURE,
        implementation_state=StrategyImplementationState.IMPLEMENTED,
        authority_state=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        last_evidence_decision="FORWARD_VALIDATION",
        supported_instrument_class="ACTIVE_BINARY_KALSHI_MARKET",
        maximum_proposals_per_cycle=config.maximum_markets_per_invocation,
        configuration_hash=config.canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class MicrostructureShadowEvaluation:
    feature: MicrostructureFeatureSnapshot
    probability: MicrostructureProbability | None
    prediction: ShadowPrediction | None
    opportunity: ShadowOpportunity | None
    reason_codes: tuple[str, ...]


class MicrostructureShadowWriter(Protocol):
    """The narrow non-financial persistence port used by the strategy service."""

    def persist_microstructure_feature(self, feature: MicrostructureFeatureSnapshot) -> bool: ...

    def persist_prediction(self, prediction: ShadowPrediction) -> bool: ...

    def link_microstructure_prediction(
        self, *, prediction: ShadowPrediction, feature_snapshot_id: str
    ) -> bool: ...


def _rule_and_information(
    *,
    market: RetainedMarketEvidence,
    feature: MicrostructureFeatureSnapshot,
    decision_at: datetime,
    resolution_at: datetime,
) -> tuple[ContractRuleVersion, Instrument, Quantity, InformationSet]:
    if feature.best_ask is None:
        raise ValueValidationError("valid microstructure evidence requires a held YES ask")
    rule = ContractRuleVersion(
        rule_version_id=f"public-rule-{market.snapshot_id}",
        exact_text=market.rule_text,
        resolution_source=f"{market.provider}:retained-public-market",
        effective_at=market.observed_at - timedelta(days=1),
        close_at=market.close_at,
        determination_at=market.close_at,
        expected_settlement_at=resolution_at,
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
    visible = Quantity(max(Decimal(1), feature.visible_ask_quantity or Decimal(1)), Decimal(1))
    information = InformationSetBuilder.build(
        records=(
            SourceRecord(
                source_id=market.snapshot_id,
                source_event_at=market.observed_at,
                available_at=market.available_at,
                sequence=1,
                schema_version="kalshi-retained-orderbook-microstructure-v1",
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
            yes_ask=Price(feature.best_ask, Decimal("0.01")),
            no_ask=Price(Decimal(1) - (feature.best_bid or Decimal(0)), Decimal("0.01")),
            visible_yes_quantity=visible,
        ),
    )
    return rule, instrument, visible, information


def _modeled_fee(
    *,
    price: Decimal,
    configuration: OrderbookMicrostructureStrategyConfig,
    fee_evidence: SeriesFeeSnapshot | None,
    applicability: PublishedFeeScheduleApplicability | None,
) -> Decimal:
    """Use legacy proxy arithmetic for v1 and the published trade fee for v2."""

    if not configuration.uses_published_fee_schedule:
        # Preserve the original v1 result exactly for existing retained evidence.
        return (Decimal("0.07") * price * (Decimal(1) - price)).quantize(Decimal("0.00000001"))
    schedule = configuration.published_fee_schedule
    if (
        schedule is None
        or fee_evidence is None
        or fee_evidence.fee_multiplier is None
        or applicability is None
        or not applicability.is_exact_taker_compatible
    ):
        raise ValueValidationError("published fee evidence is incompatible")
    # Both immediate-cross legs consume displayed liquidity, so neither may use
    # the documented maker coefficient.
    return schedule.trade_fee(
        role=FeeLiquidityRole.TAKER,
        multiplier=fee_evidence.fee_multiplier,
        contract_count=Decimal(1),
        price=price,
    )


def _published_fee_applicability(
    *,
    fee_evidence: SeriesFeeSnapshot | None,
    configuration: OrderbookMicrostructureStrategyConfig,
    series_ticker: str,
    decision_at: datetime,
) -> PublishedFeeScheduleApplicability | None:
    if fee_evidence is None or fee_evidence.series_ticker != series_ticker:
        return None
    schedule = configuration.published_fee_schedule
    if schedule is None:
        return None
    return reconcile_kalshi_published_fee_schedule(
        provider_fee_type=fee_evidence.fee_type,
        series_ticker=fee_evidence.series_ticker,
        fee_multiplier=fee_evidence.fee_multiplier,
        fee_effective_at=fee_evidence.effective_at,
        decision_at=decision_at,
        fee_model_identity=fee_evidence.fee_model_identity,
        taker_coefficient=fee_evidence.taker_coefficient,
        maker_coefficient=fee_evidence.maker_coefficient,
        retained_rounding_identity=fee_evidence.rounding_identity,
        schedule_identity=fee_evidence.schedule_identity,
        schedule_effective_date=fee_evidence.schedule_effective_date,
        expected_fee_model_identity=FEE_MODEL_IDENTITY,
        expected_retained_rounding_identity=ROUNDING_IDENTITY,
        schedule=schedule,
    )


def evaluate_retained_microstructure(
    *,
    campaign_id: str,
    market: RetainedMarketEvidence,
    feature: MicrostructureFeatureSnapshot,
    configuration: OrderbookMicrostructureStrategyConfig,
    decision_at: datetime,
    fee_evidence: SeriesFeeSnapshot | None = None,
    cycle_id: str | None = None,
    invocation_id: str | None = None,
) -> MicrostructureShadowEvaluation:
    """Create one forward prediction, and an opportunity only after all costs."""

    if not feature.valid:
        return MicrostructureShadowEvaluation(
            feature=feature,
            probability=None,
            prediction=None,
            opportunity=None,
            reason_codes=feature.reason_codes,
        )
    if decision_at >= market.close_at or market.available_at > decision_at:
        return MicrostructureShadowEvaluation(
            feature=feature,
            probability=None,
            prediction=None,
            opportunity=None,
            reason_codes=("MARKET_EVIDENCE_NOT_CURRENT_AT_DECISION",),
        )
    if configuration.uses_published_fee_schedule:
        if feature.series_ticker is None:
            return MicrostructureShadowEvaluation(
                feature=feature,
                probability=None,
                prediction=None,
                opportunity=None,
                reason_codes=("PUBLISHED_FEE_EVIDENCE_INCOMPATIBLE",),
            )
        applicability = _published_fee_applicability(
            fee_evidence=fee_evidence,
            configuration=configuration,
            series_ticker=feature.series_ticker,
            decision_at=decision_at,
        )
    else:
        applicability = None
    if configuration.uses_published_fee_schedule and (
        applicability is None or not applicability.is_exact_taker_compatible
    ):
        return MicrostructureShadowEvaluation(
            feature=feature,
            probability=None,
            prediction=None,
            opportunity=None,
            reason_codes=("PUBLISHED_FEE_EVIDENCE_INCOMPATIBLE",),
        )
    probability = derive_microstructure_probability(feature=feature, configuration=configuration)
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
    if configuration.uses_published_fee_schedule:
        assert fee_evidence is not None
        assert applicability is not None
        payload["published_fee_evidence"] = {
            "fee_snapshot_id": fee_evidence.fee_snapshot_id,
            "source_hash": fee_evidence.source_hash.value,
            "series_ticker": fee_evidence.series_ticker,
            "fee_type": fee_evidence.fee_type,
            "fee_multiplier": fee_evidence.fee_multiplier,
            "effective_at": fee_evidence.effective_at,
            "applicability": applicability.canonical_payload,
        }
    prediction = ShadowPrediction(
        prediction_id=deterministic_id("microstructure_shadow_prediction", payload),
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
        evidence_references=(
            market.snapshot_id,
            feature.feature_snapshot_id,
            feature.first_orderbook_snapshot_id or "",
            feature.latest_orderbook_snapshot_id or "",
        )
        + (
            (fee_evidence.fee_snapshot_id, fee_evidence.source_hash.value)
            if configuration.uses_published_fee_schedule and fee_evidence is not None
            else ()
        ),
        thesis_summary="Fixed public-orderbook microstructure adjustment; forward validation only.",
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
    assert feature.best_ask is not None and feature.best_bid is not None
    yes_cost = (
        feature.best_ask
        + _modeled_fee(
            price=feature.best_ask,
            configuration=configuration,
            fee_evidence=fee_evidence,
            applicability=applicability,
        )
        + configuration.slippage_reserve
        + configuration.execution_uncertainty_reserve
    )
    no_ask = Decimal(1) - feature.best_bid
    no_cost = (
        no_ask
        + _modeled_fee(
            price=no_ask,
            configuration=configuration,
            fee_evidence=fee_evidence,
            applicability=applicability,
        )
        + configuration.slippage_reserve
        + configuration.execution_uncertainty_reserve
    )
    advantages = (
        (Side.YES, dict(conservative)["YES"] - yes_cost, feature.best_ask, yes_cost),
        (Side.NO, dict(conservative)["NO"] - no_cost, no_ask, no_cost),
    )
    side, advantage, ask, total_cost = max(advantages, key=lambda item: (item[1], item[0].value))
    if advantage < configuration.minimum_conservative_advantage:
        return MicrostructureShadowEvaluation(
            feature,
            probability,
            prediction,
            None,
            ("NO_PROPOSAL_CONSERVATIVE_ADVANTAGE_BELOW_THRESHOLD",),
        )
    rule, instrument, visible, information = _rule_and_information(
        market=market, feature=feature, decision_at=decision_at, resolution_at=market.close_at
    )
    fee = _modeled_fee(
        price=ask,
        configuration=configuration,
        fee_evidence=fee_evidence,
        applicability=applicability,
    )
    proposal_id = deterministic_id(
        "microstructure_shadow_proposal", {**payload, "side": side.value}
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
    return MicrostructureShadowEvaluation(
        feature,
        probability,
        prediction,
        ShadowOpportunity(proposal, economics, information, market.snapshot_id),
        ("SHADOW_OPPORTUNITY_QUALIFIED",),
    )


class OrderbookMicrostructureAutopilotStrategy:
    """Small adapter used by the existing bounded SHADOW cycle only."""

    descriptor = orderbook_microstructure_strategy_descriptor()

    def __init__(self, evaluations: tuple[MicrostructureShadowEvaluation, ...]) -> None:
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
            reason_codes=reasons or ("NO_MICROSTRUCTURE_MARKETS_SELECTED",),
        )


class RunOrderbookMicrostructureShadowStrategy:
    """Retain bounded evaluated evidence, then hand optional proposals to Autopilot.

    The caller supplies only a frozen retained market and feature snapshot. This
    service cannot inspect Campaign, Ledger, Order, Position, or settlement
    repositories and performs no allocation itself.
    """

    def __init__(self, writer: MicrostructureShadowWriter) -> None:
        self._writer = writer

    def run(
        self,
        *,
        campaign_id: str,
        market: RetainedMarketEvidence,
        feature: MicrostructureFeatureSnapshot,
        configuration: OrderbookMicrostructureStrategyConfig,
        decision_at: datetime,
        fee_evidence: SeriesFeeSnapshot | None = None,
        cycle_id: str | None = None,
        invocation_id: str | None = None,
        expected_configuration_hash: ContentHash | None = None,
        expected_configuration_version: str | None = None,
    ) -> MicrostructureShadowEvaluation:
        if expected_configuration_hash is not None and (
            configuration.canonical_hash != expected_configuration_hash
            or feature.configuration_hash != expected_configuration_hash
        ):
            return MicrostructureShadowEvaluation(
                feature, None, None, None, ("PLAN_STRATEGY_CONFIGURATION_MISMATCH",)
            )
        if (
            expected_configuration_version is not None
            and configuration.configuration_version != expected_configuration_version
        ):
            return MicrostructureShadowEvaluation(
                feature, None, None, None, ("CONFIGURATION_VERSION_MISMATCH",)
            )
        evaluation = evaluate_retained_microstructure(
            campaign_id=campaign_id,
            market=market,
            feature=feature,
            configuration=configuration,
            decision_at=decision_at,
            fee_evidence=fee_evidence,
            cycle_id=cycle_id,
            invocation_id=invocation_id,
        )
        self._writer.persist_microstructure_feature(feature)
        if evaluation.prediction is not None:
            self._writer.persist_prediction(evaluation.prediction)
            self._writer.link_microstructure_prediction(
                prediction=evaluation.prediction,
                feature_snapshot_id=feature.feature_snapshot_id,
            )
        return evaluation
