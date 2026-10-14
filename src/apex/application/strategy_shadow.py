"""Controlled strategy promotion and forward-only SHADOW prediction rules.

This module is deliberately an application boundary, not a trading strategy.
It validates immutable operator forecasts, exact authority transitions, and
forward scoring inputs.  Persistence, public-data retrieval, and rendering are
owned by their respective adapters.  Nothing here can create financial state.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import cast

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
from apex.domain.autopilot import EvidenceTier, OpportunityEconomics, OpportunityOutcome
from apex.domain.decisions import OpportunityProposal, Side
from apex.domain.market import (
    ContractRuleVersion,
    InformationSetBuilder,
    Instrument,
    MarketSnapshot,
    SourceRecord,
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
    parse_decimal,
)

OPERATOR_THESIS_STRATEGY_ID = "operator-thesis-v1"
MARKET_CONSENSUS_BASELINE_STRATEGY_ID = "market-consensus-baseline-v1"
SHADOW_SCORING_VERSION = "shadow-scoring-v1"
LOG_LOSS_FLOOR = Decimal("0.000000000000000000000000000001")
SCORE_STORAGE_QUANTUM = Decimal("0.00000001")


class StrategyAuthorityDecisionState(StrEnum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class ShadowPredictionState(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    RESOLVED = "RESOLVED"


class ShadowOutcomeEvaluationState(StrEnum):
    COMPLETED = "COMPLETED"


def _required_text(value: str, *, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueValidationError(f"{label} must be non-empty and at most {maximum} characters")
    return value


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _probabilities(
    values: tuple[tuple[str, Decimal | str], ...], *, label: str
) -> tuple[tuple[str, Decimal], ...]:
    if len(values) < 2 or len(values) > 32:
        raise ValueValidationError(f"{label} requires between two and 32 outcomes")
    parsed: list[tuple[str, Decimal]] = []
    for outcome_id, probability in values:
        parsed.append(
            (
                _required_text(outcome_id, label="outcome ID", maximum=120),
                parse_decimal(probability),
            )
        )
    ordered = tuple(sorted(parsed))
    if tuple(item[0] for item in ordered) != tuple(item[0] for item in parsed):
        raise ValueValidationError(f"{label} outcomes must use deterministic sorted order")
    if len({item[0] for item in ordered}) != len(ordered):
        raise ValueValidationError(f"{label} outcome IDs must be unique")
    if sum((item[1] for item in ordered), Decimal(0)) != Decimal(1):
        raise ValueValidationError(f"{label} probabilities must sum exactly to one")
    return ordered


def _matching_probabilities(
    point: tuple[tuple[str, Decimal | str], ...],
    conservative: tuple[tuple[str, Decimal | str], ...],
) -> tuple[tuple[tuple[str, Decimal], ...], tuple[tuple[str, Decimal], ...]]:
    parsed_point = _probabilities(point, label="point probability distribution")
    parsed_conservative = _probabilities(
        conservative, label="conservative probability distribution"
    )
    if tuple(item[0] for item in parsed_point) != tuple(item[0] for item in parsed_conservative):
        raise ValueValidationError("point and conservative outcome identities must match")
    return parsed_point, parsed_conservative


@dataclass(frozen=True, slots=True)
class StrategyAuthorityCommand:
    strategy_id: str
    requested_authority: StrategyAuthorityState
    operator_reason: str
    evidence_review_id: str
    idempotency_key: str
    confirmation_text: str | None
    requested_at: datetime
    paper_trial_authorization_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.strategy_id, label="strategy ID", maximum=160)
        _required_text(self.operator_reason, label="operator reason")
        _required_text(self.evidence_review_id, label="evidence review ID", maximum=160)
        _required_text(self.idempotency_key, label="idempotency key", maximum=160)
        _aware(self.requested_at, label="requested timestamp")
        if self.requested_authority is StrategyAuthorityState.PAPER:
            _required_text(
                self.paper_trial_authorization_id or "",
                label="paper trial authorization ID",
                maximum=160,
            )
        elif self.paper_trial_authorization_id is not None:
            raise ValueValidationError("paper trial authorization only applies to PAPER authority")
        if self.confirmation_text is not None and len(self.confirmation_text) > 160:
            raise ValueValidationError("confirmation text must be at most 160 characters")

    @property
    def request_hash(self) -> str:
        return content_digest(
            {
                "strategy_id": self.strategy_id,
                "requested_authority": self.requested_authority,
                "operator_reason": self.operator_reason,
                "evidence_review_id": self.evidence_review_id,
                "idempotency_key": self.idempotency_key,
                "confirmation_text": self.confirmation_text,
                "paper_trial_authorization_id": self.paper_trial_authorization_id,
            }
        )


def authority_transition_reasons(
    *,
    current: StrategyAuthorityState,
    requested: StrategyAuthorityState,
    evidence_state: StrategyEvidenceState,
    confirmation_text: str | None,
    paper_trial_authorization_id: str | None = None,
) -> tuple[str, ...]:
    """Return deterministic reasons; callers fail closed if the tuple is nonempty."""

    if requested is StrategyAuthorityState.PAPER:
        if current is not StrategyAuthorityState.SHADOW:
            return ("PAPER_PROMOTION_REQUIRES_SHADOW",)
        if not paper_trial_authorization_id:
            return ("PAPER_TRIAL_AUTHORIZATION_REQUIRED",)
        return ()
    if current is StrategyAuthorityState.REJECTED:
        return ("REJECTED_STRATEGY_CANNOT_REACTIVATE",)
    if current is StrategyAuthorityState.DISABLED:
        return ("DISABLED_STRATEGY_CANNOT_REACTIVATE",)
    if current is StrategyAuthorityState.RESEARCH and requested is StrategyAuthorityState.SHADOW:
        if evidence_state is StrategyEvidenceState.INCOMPLETE:
            return ("EVIDENCE_REVIEW_REQUIREMENTS_UNMET",)
        return ()
    if current is StrategyAuthorityState.SHADOW and requested in (
        StrategyAuthorityState.PAUSED,
        StrategyAuthorityState.REJECTED,
    ):
        if requested is StrategyAuthorityState.REJECTED and not confirmation_text:
            return ("REJECTION_CONFIRMATION_REQUIRED",)
        return ()
    if current is StrategyAuthorityState.PAUSED and requested is StrategyAuthorityState.SHADOW:
        return ()
    if current is StrategyAuthorityState.RESEARCH and requested is StrategyAuthorityState.REJECTED:
        if not confirmation_text:
            return ("REJECTION_CONFIRMATION_REQUIRED",)
        return ()
    if current is requested:
        return ("NO_AUTHORITY_STATE_CHANGE",)
    return ("AUTHORITY_TRANSITION_NOT_PERMITTED",)


@dataclass(frozen=True, slots=True)
class StrategyAuthorityDecision:
    decision_id: str
    strategy_id: str
    strategy_version: str
    previous_authority: StrategyAuthorityState
    requested_authority: StrategyAuthorityState
    resulting_authority: StrategyAuthorityState
    operator_action_id: str | None
    requested_at: datetime
    completed_at: datetime
    state: StrategyAuthorityDecisionState
    operator_reason: str
    evidence_review_id: str
    minimum_evidence_requirements: tuple[str, ...]
    reason_codes: tuple[str, ...]
    idempotency_key: str
    canonical_request_hash: str
    canonical_result_hash: str


@dataclass(frozen=True, slots=True)
class RetainedMarketEvidence:
    """The exact retained public market fields safe for a forward forecast."""

    snapshot_id: str
    provider: str
    provider_market_ticker: str
    provider_event_ticker: str | None
    observed_at: datetime
    available_at: datetime
    close_at: datetime
    rule_text: str
    rule_content_hash: ContentHash
    yes_bid: Decimal | None
    yes_ask: Decimal
    no_ask: Decimal
    visible_yes_quantity: Decimal | None
    fee_model_version: str | None
    expected_fee: Decimal

    def __post_init__(self) -> None:
        _required_text(self.snapshot_id, label="market snapshot ID", maximum=160)
        _required_text(self.provider, label="provider", maximum=30)
        _required_text(self.provider_market_ticker, label="provider market ticker", maximum=300)
        _required_text(self.rule_text, label="retained rule text", maximum=20_000)
        _aware(self.observed_at, label="market observed timestamp")
        _aware(self.available_at, label="market available timestamp")
        _aware(self.close_at, label="market close timestamp")
        if self.available_at.astimezone(UTC) < self.observed_at.astimezone(UTC):
            raise ValueValidationError("retained market availability cannot precede observation")
        if self.close_at.astimezone(UTC) <= self.available_at.astimezone(UTC):
            raise ValueValidationError("retained market must remain open at observation")
        for label, value in (("YES ask", self.yes_ask), ("NO ask", self.no_ask)):
            parsed = parse_decimal(value)
            if parsed > 1:
                raise ValueValidationError(f"retained {label} must be between zero and one")
        if self.yes_bid is not None:
            parsed_bid = parse_decimal(self.yes_bid)
            if parsed_bid > 1:
                raise ValueValidationError("retained YES bid must be between zero and one")
        if parse_decimal(self.expected_fee) < 0:
            raise ValueValidationError("retained expected fee cannot be negative")

    @property
    def market_probability(self) -> Decimal:
        if self.yes_bid is not None:
            return (parse_decimal(self.yes_bid) + parse_decimal(self.yes_ask)) / Decimal(2)
        return parse_decimal(self.yes_ask)


@dataclass(frozen=True, slots=True)
class OperatorThesisInput:
    campaign_id: str
    point_probabilities: tuple[tuple[str, Decimal | str], ...]
    conservative_probabilities: tuple[tuple[str, Decimal | str], ...]
    resolution_at: datetime
    requested_risk_fraction: RiskFraction | Decimal | str
    thesis_summary: str
    evidence_references: tuple[str, ...]
    correlation_buckets: tuple[str, ...]
    operator_note: str
    idempotency_key: str
    requested_at: datetime

    def __post_init__(self) -> None:
        _required_text(self.campaign_id, label="campaign ID", maximum=160)
        _matching_probabilities(self.point_probabilities, self.conservative_probabilities)
        resolution_at = _aware(self.resolution_at, label="resolution timestamp")
        requested_at = _aware(self.requested_at, label="requested timestamp")
        if resolution_at <= requested_at:
            raise ValueValidationError(
                "resolution timestamp must follow the thesis decision timestamp"
            )
        fraction = (
            self.requested_risk_fraction
            if isinstance(self.requested_risk_fraction, RiskFraction)
            else RiskFraction(self.requested_risk_fraction)
        )
        if fraction.value <= 0:
            raise ValueValidationError("requested risk fraction must be greater than zero")
        _required_text(self.thesis_summary, label="thesis summary", maximum=4_000)
        _required_text(self.idempotency_key, label="idempotency key", maximum=160)
        if len(self.operator_note) > 500:
            raise ValueValidationError("operator note must be at most 500 characters")
        if not 1 <= len(self.evidence_references) <= 32:
            raise ValueValidationError("thesis requires between one and 32 evidence references")
        if not 1 <= len(self.correlation_buckets) <= 8:
            raise ValueValidationError("thesis requires between one and eight correlation buckets")
        references = tuple(
            _required_text(value, label="evidence reference", maximum=300)
            for value in self.evidence_references
        )
        buckets = tuple(
            _required_text(value, label="correlation bucket", maximum=80)
            for value in self.correlation_buckets
        )
        if len(set(references)) != len(references):
            raise ValueValidationError("evidence references must be unique")
        if tuple(sorted(buckets)) != buckets or len(set(buckets)) != len(buckets):
            raise ValueValidationError("correlation buckets must be unique and sorted")
        object.__setattr__(self, "requested_risk_fraction", fraction)
        object.__setattr__(self, "resolution_at", resolution_at)
        object.__setattr__(self, "requested_at", requested_at)

    @property
    def request_hash(self) -> str:
        return content_digest(
            {
                "campaign_id": self.campaign_id,
                "point_probabilities": self.point_probabilities,
                "conservative_probabilities": self.conservative_probabilities,
                "resolution_at": self.resolution_at,
                "requested_risk_fraction": self.requested_risk_fraction,
                "thesis_summary": self.thesis_summary,
                "evidence_references": self.evidence_references,
                "correlation_buckets": self.correlation_buckets,
                "operator_note": self.operator_note,
                "idempotency_key": self.idempotency_key,
            }
        )


@dataclass(frozen=True, slots=True)
class ShadowPrediction:
    prediction_id: str
    strategy_id: str
    strategy_version: str
    campaign_id: str | None
    cycle_id: str | None
    invocation_id: str | None
    proposal_id: str | None
    provider: str
    instrument_id: str
    event_id: str | None
    decision_at: datetime
    resolution_at: datetime
    point_probabilities: tuple[tuple[str, Decimal], ...]
    conservative_probabilities: tuple[tuple[str, Decimal], ...]
    market_probabilities: tuple[tuple[str, Decimal], ...]
    market_evidence_id: str
    expected_fee: Decimal
    slippage_reserve: Decimal
    execution_uncertainty_reserve: Decimal
    evidence_tier: str
    evidence_references: tuple[str, ...]
    thesis_summary: str | None
    correlation_buckets: tuple[str, ...]
    operator_note: str | None
    model_configuration_version: str
    canonical_input_hash: str
    canonical_prediction_hash: str
    state: ShadowPredictionState

    def __post_init__(self) -> None:
        point, _conservative = _matching_probabilities(
            tuple((outcome, probability) for outcome, probability in self.point_probabilities),
            tuple(
                (outcome, probability) for outcome, probability in self.conservative_probabilities
            ),
        )
        market = _probabilities(
            tuple((outcome, probability) for outcome, probability in self.market_probabilities),
            label="market probability distribution",
        )
        if tuple(item[0] for item in point) != tuple(item[0] for item in market):
            raise ValueValidationError("market and strategy outcome identities must match")
        if _aware(self.resolution_at, label="resolution timestamp") <= _aware(
            self.decision_at, label="decision timestamp"
        ):
            raise ValueValidationError("prediction resolution must follow its decision")
        for label, value in (
            ("expected fee", self.expected_fee),
            ("slippage reserve", self.slippage_reserve),
            ("execution uncertainty reserve", self.execution_uncertainty_reserve),
        ):
            if parse_decimal(value) < 0:
                raise ValueValidationError(f"{label} cannot be negative")
        if self.thesis_summary is not None:
            _required_text(self.thesis_summary, label="thesis summary", maximum=4_000)
        if self.operator_note is not None and len(self.operator_note) > 500:
            raise ValueValidationError("operator note must be at most 500 characters")
        if len(self.correlation_buckets) > 8:
            raise ValueValidationError("prediction has too many correlation buckets")
        buckets = tuple(
            _required_text(value, label="correlation bucket", maximum=80)
            for value in self.correlation_buckets
        )
        if tuple(sorted(buckets)) != buckets or len(set(buckets)) != len(buckets):
            raise ValueValidationError(
                "prediction correlation buckets must be unique and use sorted order"
            )
        object.__setattr__(self, "correlation_buckets", buckets)


@dataclass(frozen=True, slots=True)
class ShadowOutcomeEvaluation:
    evaluation_id: str
    prediction_id: str
    settlement_fact_id: str
    evaluated_at: datetime
    resolved_outcome: str
    brier_score: Decimal
    log_loss: Decimal
    direction_correct: bool
    market_baseline_brier_score: Decimal
    market_baseline_log_loss: Decimal
    probability_advantage: Decimal
    evaluation_state: ShadowOutcomeEvaluationState
    reason_codes: tuple[str, ...]
    numerical_method: str
    numerical_precision: int
    canonical_input_hash: str
    canonical_result_hash: str


def binary_market_probabilities(yes_probability: Decimal | str) -> tuple[tuple[str, Decimal], ...]:
    yes = parse_decimal(yes_probability)
    if yes > 1:
        raise ValueValidationError("market probability must be between zero and one")
    return (("NO", Decimal(1) - yes), ("YES", yes))


def production_shadow_strategy_descriptors() -> tuple[StrategyDescriptor, ...]:
    """Normal-catalog strategies; every production entry remains SHADOW-only."""

    from apex.domain.market_dynamics import MarketDynamicsStrategyConfig
    from apex.domain.orderbook_microstructure import OrderbookMicrostructureStrategyConfig

    definitions = (
        (
            OPERATOR_THESIS_STRATEGY_ID,
            "Operator Thesis",
            StrategyFamily.OPERATOR_SUPPLIED,
            StrategyEvidenceState.INCOMPLETE,
            "OPERATOR_CONTROLLED_SHADOW",
            1,
        ),
        (
            MARKET_CONSENSUS_BASELINE_STRATEGY_ID,
            "Market Consensus Baseline",
            StrategyFamily.MARKET_STRUCTURE,
            StrategyEvidenceState.INCOMPLETE,
            "RETAINED_MARKET_BASELINE",
            100,
        ),
    )
    controlled = tuple(
        StrategyDescriptor(
            strategy_id=strategy_id,
            version="v1",
            display_name=display_name,
            family=family,
            implementation_state=StrategyImplementationState.IMPLEMENTED,
            authority_state=StrategyAuthorityState.SHADOW,
            evidence_state=evidence_state,
            last_evidence_decision=decision,
            supported_instrument_class="BOUNDED_PREDICTION_MARKET",
            maximum_proposals_per_cycle=maximum_proposals,
            configuration_hash=ContentHash(
                content_digest(
                    {
                        "strategy_id": strategy_id,
                        "version": "v1",
                        "authority": "SHADOW",
                        "configuration_version": SHADOW_SCORING_VERSION,
                    }
                )
            ),
        )
        for (
            strategy_id,
            display_name,
            family,
            evidence_state,
            decision,
            maximum_proposals,
        ) in definitions
    )
    configuration = OrderbookMicrostructureStrategyConfig()
    microstructure = StrategyDescriptor(
        strategy_id=configuration.strategy_id,
        version=configuration.strategy_version,
        display_name="Kalshi Orderbook Microstructure",
        family=StrategyFamily.MARKET_STRUCTURE,
        implementation_state=StrategyImplementationState.IMPLEMENTED,
        authority_state=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        last_evidence_decision="FORWARD_VALIDATION",
        supported_instrument_class="ACTIVE_BINARY_KALSHI_MARKET",
        maximum_proposals_per_cycle=configuration.maximum_markets_per_invocation,
        configuration_hash=configuration.canonical_hash,
    )
    dynamics_configuration = MarketDynamicsStrategyConfig()
    dynamics = StrategyDescriptor(
        strategy_id=dynamics_configuration.strategy_id,
        version=dynamics_configuration.strategy_version,
        display_name="Kalshi Market Dynamics",
        family=StrategyFamily.MARKET_DYNAMICS,
        implementation_state=StrategyImplementationState.IMPLEMENTED,
        authority_state=StrategyAuthorityState.SHADOW,
        evidence_state=StrategyEvidenceState.INCOMPLETE,
        last_evidence_decision="FORWARD_VALIDATION",
        supported_instrument_class="ACTIVE_BINARY_KALSHI_MARKET",
        maximum_proposals_per_cycle=dynamics_configuration.maximum_markets_per_invocation,
        configuration_hash=dynamics_configuration.canonical_hash,
    )
    return (*controlled, microstructure, dynamics)


def _operator_thesis_descriptor() -> StrategyDescriptor:
    return next(
        item
        for item in production_shadow_strategy_descriptors()
        if item.strategy_id == OPERATOR_THESIS_STRATEGY_ID
    )


def build_operator_thesis_opportunity(
    *,
    market: RetainedMarketEvidence,
    thesis: OperatorThesisInput,
) -> ShadowOpportunity | None:
    """Build one bounded YES-side shadow candidate from exact retained evidence.

    The operator supplies only probabilities and commentary.  The exact market
    terms, price, rule, fee evidence, and payout model are derived here from
    the retained market snapshot.
    """

    descriptor = _operator_thesis_descriptor()
    point, conservative = _matching_probabilities(
        thesis.point_probabilities, thesis.conservative_probabilities
    )
    if tuple(item[0] for item in point) != ("NO", "YES"):
        raise ValueValidationError(
            "Operator Thesis v1 supports the exact binary NO/YES outcome set"
        )
    decision_at = thesis.requested_at
    if decision_at >= market.close_at:
        raise ValueValidationError("retained market is already closed")
    if thesis.resolution_at < market.close_at:
        raise ValueValidationError(
            "thesis resolution timestamp cannot precede retained market close"
        )
    rule = ContractRuleVersion(
        rule_version_id=f"public-rule-{market.snapshot_id}",
        exact_text=market.rule_text,
        resolution_source=f"{market.provider}:retained-public-market",
        effective_at=market.observed_at - timedelta(days=1),
        close_at=market.close_at,
        determination_at=market.close_at,
        expected_settlement_at=thesis.resolution_at,
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
    visible = Quantity(
        max(Decimal(1), parse_decimal(market.visible_yes_quantity or Decimal(1))), Decimal(1)
    )
    information_set = InformationSetBuilder.build(
        records=(
            SourceRecord(
                source_id=market.snapshot_id,
                source_event_at=market.observed_at,
                available_at=market.available_at,
                sequence=1,
                schema_version="kalshi-public-retained-v1",
                content_hash=ContentHash(content_digest({"snapshot_id": market.snapshot_id})),
            ),
        ),
        instrument=instrument,
        cutoff=InformationCutoff(market.available_at),
        rule=rule,
        market=MarketSnapshot(
            yes_ask=Price(market.yes_ask, Decimal("0.01")),
            no_ask=Price(market.no_ask, Decimal("0.01")),
            visible_yes_quantity=visible,
        ),
    )
    total_cost = parse_decimal(market.yes_ask) + parse_decimal(market.expected_fee)
    conservative_yes = dict(conservative)["YES"]
    if conservative_yes - total_cost <= 0:
        return None
    proposal_payload = {
        "campaign_id": thesis.campaign_id,
        "strategy_id": descriptor.strategy_id,
        "market_snapshot_id": market.snapshot_id,
        "thesis_request_hash": thesis.request_hash,
    }
    proposal_id = deterministic_id("operator_thesis_proposal", proposal_payload)
    proposal = OpportunityProposal(
        proposal_id=proposal_id,
        instrument_id=instrument.instrument_id,
        rule_version_id=rule.rule_version_id,
        information_set_id=information_set.information_set_id,
        strategy_version=descriptor.version,
        side=Side.YES,
        executable_ask=Price(market.yes_ask, Decimal("0.01")),
        fee_model_version=market.fee_model_version or "retained-fee-unavailable-v1",
        fee_per_unit=Money(market.expected_fee),
        expected_net_value_per_unit=dict(point)["YES"] - total_cost,
        maximum_loss_per_unit=Money(total_cost),
        requested_risk_fraction=cast(RiskFraction, thesis.requested_risk_fraction),
        expires_at=min(thesis.resolution_at, market.close_at),
        fingerprint=content_digest(proposal_payload),
    )
    economics = OpportunityEconomics(
        proposal_id=proposal_id,
        strategy_id=descriptor.strategy_id,
        strategy_version=descriptor.version,
        instrument_id=instrument.instrument_id,
        decision_at=decision_at,
        capital_release_at=thesis.resolution_at,
        outcomes=tuple(
            OpportunityOutcome(
                outcome_id=outcome_id,
                point_probability=point_probability,
                conservative_probability=dict(conservative)[outcome_id],
                payout=Decimal(1) if outcome_id == "YES" else Decimal(0),
            )
            for outcome_id, point_probability in point
        ),
        acquisition_cost=market.yes_ask,
        expected_fee=market.expected_fee,
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
        visible_quantity=visible,
        requested_risk_fraction=thesis.requested_risk_fraction,
        evidence_tier=EvidenceTier.RESEARCH,
        evidence_references=(market.snapshot_id, *thesis.evidence_references),
        correlation_buckets=thesis.correlation_buckets,
        configuration_version="operator-thesis-shadow-v1",
        configuration_hash=ContentHash(content_digest(proposal_payload)),
    )
    return ShadowOpportunity(
        proposal=proposal,
        economics=economics,
        information_set=information_set,
        market_observation_source_id=market.snapshot_id,
    )


class OperatorThesisAutopilotStrategy:
    """One operator-supplied forecast evaluated only through the existing shadow path."""

    descriptor = _operator_thesis_descriptor()

    def __init__(self, opportunity: ShadowOpportunity | None, market_snapshot_id: str) -> None:
        self._opportunity = opportunity
        self._market_snapshot_id = market_snapshot_id

    def evaluate(self, context: AutopilotStrategyContext) -> StrategyInvocationResult:
        _ = context
        if self._opportunity is None:
            return StrategyInvocationResult(
                state=InvocationState.COMPLETED,
                opportunities=(),
                evidence_references=(self._market_snapshot_id,),
                diagnostics=(("conservative_value", "nonpositive"),),
                reason_codes=("NONPOSITIVE_CONSERVATIVE_NET_EXPECTED_VALUE",),
            )
        return StrategyInvocationResult(
            state=InvocationState.COMPLETED,
            opportunities=(self._opportunity,),
            evidence_references=(self._market_snapshot_id,),
            diagnostics=(("source", "operator-thesis"),),
            reason_codes=("OPERATOR_THESIS_EVALUATED",),
        )


def build_operator_thesis_prediction(
    *,
    market: RetainedMarketEvidence,
    thesis: OperatorThesisInput,
    cycle_id: str | None,
    invocation_id: str | None,
    proposal_id: str | None,
) -> ShadowPrediction:
    point, conservative = _matching_probabilities(
        thesis.point_probabilities, thesis.conservative_probabilities
    )
    if tuple(item[0] for item in point) != ("NO", "YES"):
        raise ValueValidationError(
            "Operator Thesis v1 supports the exact binary NO/YES outcome set"
        )
    payload = {
        "strategy_id": OPERATOR_THESIS_STRATEGY_ID,
        "market_snapshot_id": market.snapshot_id,
        "thesis_request_hash": thesis.request_hash,
        "cycle_id": cycle_id,
        "proposal_id": proposal_id,
    }
    return ShadowPrediction(
        prediction_id=deterministic_id("shadow_prediction", payload),
        strategy_id=OPERATOR_THESIS_STRATEGY_ID,
        strategy_version="v1",
        campaign_id=thesis.campaign_id,
        cycle_id=cycle_id,
        invocation_id=invocation_id,
        proposal_id=proposal_id,
        provider=market.provider,
        instrument_id=market.provider_market_ticker,
        event_id=market.provider_event_ticker,
        decision_at=thesis.requested_at,
        resolution_at=thesis.resolution_at,
        point_probabilities=point,
        conservative_probabilities=conservative,
        market_probabilities=binary_market_probabilities(market.market_probability),
        market_evidence_id=market.snapshot_id,
        expected_fee=market.expected_fee,
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
        evidence_tier=EvidenceTier.RESEARCH.value,
        evidence_references=(market.snapshot_id, *thesis.evidence_references),
        thesis_summary=thesis.thesis_summary,
        correlation_buckets=thesis.correlation_buckets,
        operator_note=thesis.operator_note or None,
        model_configuration_version="operator-thesis-shadow-v1",
        canonical_input_hash=content_digest(payload),
        canonical_prediction_hash=content_digest(
            {
                "payload": payload,
                "market": market,
                "point": point,
                "conservative": conservative,
                "market_probabilities": binary_market_probabilities(market.market_probability),
                "thesis_request_hash": thesis.request_hash,
                "decision_at": thesis.requested_at,
                "resolution_at": thesis.resolution_at,
            }
        ),
        state=ShadowPredictionState.UNRESOLVED,
    )


def build_market_consensus_baseline_prediction(
    *, market: RetainedMarketEvidence, campaign_id: str, decision_at: datetime
) -> ShadowPrediction:
    """Retain the exact market-price proxy as a forecast-only benchmark."""

    decided_at = _aware(decision_at, label="baseline decision timestamp")
    if decided_at >= market.close_at:
        raise ValueValidationError("retained market is already closed")
    probabilities = binary_market_probabilities(market.market_probability)
    payload = {
        "strategy_id": MARKET_CONSENSUS_BASELINE_STRATEGY_ID,
        "campaign_id": campaign_id,
        "market_snapshot_id": market.snapshot_id,
        "decision_at": decided_at,
    }
    return ShadowPrediction(
        prediction_id=deterministic_id("market_consensus_baseline_prediction", payload),
        strategy_id=MARKET_CONSENSUS_BASELINE_STRATEGY_ID,
        strategy_version="v1",
        campaign_id=campaign_id,
        cycle_id=None,
        invocation_id=None,
        proposal_id=None,
        provider=market.provider,
        instrument_id=market.provider_market_ticker,
        event_id=market.provider_event_ticker,
        decision_at=decided_at,
        resolution_at=market.close_at,
        point_probabilities=probabilities,
        conservative_probabilities=probabilities,
        market_probabilities=probabilities,
        market_evidence_id=market.snapshot_id,
        expected_fee=Decimal(0),
        slippage_reserve=Decimal(0),
        execution_uncertainty_reserve=Decimal(0),
        evidence_tier=EvidenceTier.RESEARCH.value,
        evidence_references=(market.snapshot_id,),
        thesis_summary=None,
        correlation_buckets=(),
        operator_note=None,
        model_configuration_version="market-consensus-baseline-v1",
        canonical_input_hash=content_digest(payload),
        canonical_prediction_hash=content_digest(
            {
                "payload": payload,
                "market": market,
                "probabilities": probabilities,
                "decision_at": decided_at,
                "resolution_at": market.close_at,
            }
        ),
        state=ShadowPredictionState.UNRESOLVED,
    )


def _log_loss(probability: Decimal) -> Decimal:
    clipped = min(max(probability, LOG_LOSS_FLOOR), Decimal(1) - LOG_LOSS_FLOOR)
    # Decimal.ln is deterministic at a fixed local precision.  The resulting
    # decimal is retained with its method/precision alongside the evaluation.
    with localcontext() as context:
        context.prec = 50
        return (-clipped.ln()).quantize(SCORE_STORAGE_QUANTUM)


def _stored_score(value: Decimal) -> Decimal:
    """Store derived metrics at Apex's canonical eight-decimal boundary."""

    return value.quantize(SCORE_STORAGE_QUANTUM)


def score_prediction(
    *,
    prediction: ShadowPrediction,
    settlement_fact_id: str,
    resolved_outcome: str,
    evaluated_at: datetime,
) -> ShadowOutcomeEvaluation:
    """Score one immutable forward forecast against a retained outcome identity."""

    outcome = _required_text(resolved_outcome, label="resolved outcome", maximum=120)
    probabilities = dict(prediction.point_probabilities)
    baseline = dict(prediction.market_probabilities)
    if outcome not in probabilities:
        raise ValueValidationError("settlement outcome is not in the prediction outcome set")
    brier = sum(
        ((probability - (Decimal(1) if outcome_id == outcome else Decimal(0))) ** 2)
        for outcome_id, probability in prediction.point_probabilities
    ) / Decimal(len(prediction.point_probabilities))
    baseline_brier = sum(
        ((probability - (Decimal(1) if outcome_id == outcome else Decimal(0))) ** 2)
        for outcome_id, probability in prediction.market_probabilities
    ) / Decimal(len(prediction.market_probabilities))
    direction = (
        max(prediction.point_probabilities, key=lambda item: (item[1], item[0]))[0] == outcome
    )
    input_payload = {
        "prediction_id": prediction.prediction_id,
        "settlement_fact_id": settlement_fact_id,
        "resolved_outcome": outcome,
        "evaluated_at": _aware(evaluated_at, label="evaluation timestamp"),
        "method": SHADOW_SCORING_VERSION,
        "precision": 50,
    }
    result_payload = {
        "brier_score": _stored_score(brier),
        "log_loss": _log_loss(probabilities[outcome]),
        "market_baseline_brier_score": _stored_score(baseline_brier),
        "market_baseline_log_loss": _log_loss(baseline[outcome]),
        "probability_advantage": _stored_score(probabilities[outcome] - baseline[outcome]),
    }
    return ShadowOutcomeEvaluation(
        evaluation_id=content_digest({"shadow_evaluation": input_payload})[:40],
        prediction_id=prediction.prediction_id,
        settlement_fact_id=_required_text(
            settlement_fact_id, label="settlement fact ID", maximum=160
        ),
        evaluated_at=_aware(evaluated_at, label="evaluation timestamp"),
        resolved_outcome=outcome,
        brier_score=result_payload["brier_score"],
        log_loss=result_payload["log_loss"],
        direction_correct=direction,
        market_baseline_brier_score=result_payload["market_baseline_brier_score"],
        market_baseline_log_loss=result_payload["market_baseline_log_loss"],
        probability_advantage=result_payload["probability_advantage"],
        evaluation_state=ShadowOutcomeEvaluationState.COMPLETED,
        reason_codes=("SETTLEMENT_FACT_EVALUATED",),
        numerical_method="Decimal.ln",
        numerical_precision=50,
        canonical_input_hash=content_digest(input_payload),
        canonical_result_hash=content_digest(result_payload),
    )


def strategy_scorecard(
    *,
    strategy_id: str,
    predictions: Iterable[ShadowPrediction],
    evaluations: Iterable[ShadowOutcomeEvaluation],
) -> dict[str, object]:
    """Aggregate forward-only scoring without treating rows as independent trades."""

    rows = tuple(item for item in predictions if item.strategy_id == strategy_id)
    by_prediction = {item.prediction_id: item for item in rows}
    resolved = tuple(item for item in evaluations if item.prediction_id in by_prediction)
    unresolved = len(rows) - len(resolved)
    events = {
        by_prediction[item.prediction_id].event_id
        for item in resolved
        if by_prediction[item.prediction_id].event_id
    }
    buckets: dict[str, list[tuple[Decimal, int]]] = defaultdict(list)
    for evaluation in resolved:
        probability = dict(by_prediction[evaluation.prediction_id].point_probabilities)[
            evaluation.resolved_outcome
        ]
        bucket = min(4, int(probability * Decimal(5)))
        buckets[f"{bucket / 5:.1f}-{(bucket + 1) / 5:.1f}"].append(
            (probability, 1 if evaluation.direction_correct else 0)
        )
    count = len(resolved)
    return {
        "strategy_id": strategy_id,
        "predictions_created": len(rows),
        "unresolved_predictions": unresolved,
        "resolved_predictions": count,
        "distinct_events": len(events),
        "probability_coverage": Decimal(count) / Decimal(len(rows)) if rows else Decimal(0),
        "brier_score": sum((item.brier_score for item in resolved), Decimal(0)) / count
        if count
        else None,
        "log_loss": sum((item.log_loss for item in resolved), Decimal(0)) / count
        if count
        else None,
        "direction_accuracy": (
            Decimal(sum(item.direction_correct for item in resolved)) / Decimal(count)
            if count
            else None
        ),
        "market_baseline_brier_score": (
            sum((item.market_baseline_brier_score for item in resolved), Decimal(0)) / count
            if count
            else None
        ),
        "market_baseline_log_loss": (
            sum((item.market_baseline_log_loss for item in resolved), Decimal(0)) / count
            if count
            else None
        ),
        "score_difference": (
            sum(
                (item.market_baseline_brier_score - item.brier_score for item in resolved),
                Decimal(0),
            )
            / count
            if count
            else None
        ),
        "calibration_buckets": tuple(
            {
                "bucket": bucket,
                "prediction_count": len(values),
                "mean_probability": sum((item[0] for item in values), Decimal(0)) / len(values),
                "direction_accuracy": Decimal(sum(item[1] for item in values)) / len(values),
            }
            for bucket, values in sorted(buckets.items())
        ),
        "integrity_warnings": (("NO_RESOLVED_PREDICTIONS",) if not count else ())
        + (("CORRELATED_ROWS_GROUPED_BY_EVENT",) if count > len(events) and events else ()),
    }
