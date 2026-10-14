"""Bounded fixture-only strategy definitions for the Autopilot demonstration.

They are never included in the normal production registry.  The module has no
provider, database, order, execution, position, ledger, or settlement import.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

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
    StrategyRegistry,
)
from apex.application.discover_public_markets import CapturedPublicMarketPage
from apex.application.strategy_shadow import ShadowPrediction, ShadowPredictionState
from apex.domain.autopilot import EvidenceTier, OpportunityEconomics, OpportunityOutcome
from apex.domain.decisions import OpportunityProposal, Side
from apex.domain.market import (
    ContractRuleVersion,
    InformationSetBuilder,
    Instrument,
    MarketSnapshot,
    SourceRecord,
)
from apex.domain.paper import PaperExecutionEvidence, PaperOrderbookLevel
from apex.domain.values import (
    ContentHash,
    InformationCutoff,
    Money,
    Price,
    Quantity,
    RiskFraction,
    content_digest,
    deterministic_id,
)


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _descriptor(
    strategy_id: str,
    *,
    failing: bool,
    authority: StrategyAuthorityState = StrategyAuthorityState.SHADOW,
    maximum_proposals: int | None = None,
) -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id=strategy_id,
        version="fixture-v1",
        display_name=("Autopilot fixture failure" if failing else "Autopilot fixture opportunity"),
        family=StrategyFamily.FIXTURE_ONLY,
        implementation_state=StrategyImplementationState.FIXTURE,
        authority_state=authority,
        evidence_state=StrategyEvidenceState.FIXTURE,
        last_evidence_decision=("FIXTURE_FAILURE" if failing else "FIXTURE_ONLY"),
        supported_instrument_class="BOUNDED_PREDICTION_MARKET",
        maximum_proposals_per_cycle=(
            maximum_proposals if maximum_proposals is not None else (3 if not failing else 0)
        ),
        configuration_hash=_hash({"strategy_id": strategy_id, "fixture": True, "failing": failing}),
    )


class FixtureAutopilotStrategy:
    descriptor = _descriptor("fixture-autopilot-opportunities", failing=False)

    def __init__(self, *, paper: bool = False) -> None:
        self._paper = paper
        self.descriptor = _descriptor(
            "fixture-paper-portfolio" if paper else "fixture-autopilot-opportunities",
            failing=False,
            authority=(StrategyAuthorityState.PAPER if paper else StrategyAuthorityState.SHADOW),
            maximum_proposals=4 if paper else 3,
        )

    @staticmethod
    def _opportunity(
        context: AutopilotStrategyContext,
        *,
        label: str,
        conservative_yes: str,
        payout_yes: str,
        total_cost: str,
        visible_quantity: str = "1",
        paper_ask: str | None = None,
        descriptor: StrategyDescriptor | None = None,
    ) -> ShadowOpportunity:
        strategy_descriptor = descriptor or FixtureAutopilotStrategy.descriptor
        decision_at = context.decision_at
        fixture_scope = f"{context.campaign_id}-{label}"
        instrument = Instrument(
            instrument_id=f"fixture-autopilot-{fixture_scope}",
            category="POLITICS",
            payout_unit=Decimal("1"),
            price_tick=Decimal("0.01"),
            quantity_increment=Decimal("1"),
            yes_settlement_value=Decimal("1"),
            no_settlement_value=Decimal("0"),
            void_settlement_value=Decimal("0"),
        )
        rule = ContractRuleVersion(
            rule_version_id=f"fixture-autopilot-rule-{fixture_scope}",
            exact_text=f"Fixture-only bounded rule {label}",
            resolution_source="fixture-autopilot-source",
            effective_at=decision_at - timedelta(days=1),
            close_at=decision_at + timedelta(minutes=30),
            determination_at=decision_at + timedelta(minutes=45),
            expected_settlement_at=decision_at + timedelta(hours=1),
            content_hash=_hash({"campaign_id": context.campaign_id, "rule": label}),
        )
        source_id = f"fixture-autopilot-market-{fixture_scope}"
        information_set = InformationSetBuilder.build(
            records=(
                SourceRecord(
                    source_id=source_id,
                    source_event_at=decision_at - timedelta(seconds=2),
                    available_at=decision_at - timedelta(seconds=1),
                    sequence=1,
                    schema_version="fixture-v1",
                    content_hash=_hash({"campaign_id": context.campaign_id, "market": label}),
                ),
            ),
            instrument=instrument,
            cutoff=InformationCutoff(decision_at - timedelta(seconds=1)),
            rule=rule,
            market=MarketSnapshot(
                yes_ask=Price("0.50", "0.01"),
                no_ask=Price("0.50", "0.01"),
                visible_yes_quantity=Quantity(visible_quantity, "1"),
            ),
        )
        proposal_id = deterministic_id(
            "proposal",
            {
                "campaign_id": context.campaign_id,
                "fixture_autopilot": label,
                "decision_at": decision_at,
            },
        )
        expected_payout = Decimal(conservative_yes) * Decimal(payout_yes)
        proposal = OpportunityProposal(
            proposal_id=proposal_id,
            instrument_id=instrument.instrument_id,
            rule_version_id=rule.rule_version_id,
            information_set_id=information_set.information_set_id,
            strategy_version=strategy_descriptor.version,
            side=Side.YES,
            executable_ask=Price("0.50", "0.01"),
            fee_model_version="fixture-autopilot-fee-v1",
            fee_per_unit=Money("0"),
            expected_net_value_per_unit=expected_payout - Decimal(total_cost),
            maximum_loss_per_unit=Money(total_cost),
            requested_risk_fraction=RiskFraction("1"),
            expires_at=decision_at + timedelta(minutes=5),
            fingerprint=content_digest(
                {
                    "campaign_id": context.campaign_id,
                    "fixture-autopilot-proposal": label,
                    "at": decision_at,
                }
            ),
        )
        conservative_no = Decimal("1") - Decimal(conservative_yes)
        economics = OpportunityEconomics(
            proposal_id=proposal_id,
            strategy_id=strategy_descriptor.strategy_id,
            strategy_version=strategy_descriptor.version,
            instrument_id=instrument.instrument_id,
            decision_at=decision_at,
            capital_release_at=decision_at + timedelta(minutes=15),
            outcomes=(
                OpportunityOutcome(
                    outcome_id="YES",
                    point_probability=conservative_yes,
                    conservative_probability=conservative_yes,
                    payout=payout_yes,
                ),
                OpportunityOutcome(
                    outcome_id="NO",
                    point_probability=conservative_no,
                    conservative_probability=conservative_no,
                    payout="0",
                ),
            ),
            acquisition_cost=total_cost,
            expected_fee="0",
            slippage_reserve="0",
            execution_uncertainty_reserve="0",
            visible_quantity=Quantity(visible_quantity, "1"),
            requested_risk_fraction=RiskFraction("1"),
            evidence_tier=EvidenceTier.STRONG,
            evidence_references=(f"fixture-evidence-{label}",),
            correlation_buckets=("fixture-macro",),
            configuration_version="fixture-v1",
            configuration_hash=_hash(
                {"campaign_id": context.campaign_id, "fixture-autopilot-economics": label}
            ),
        )
        evidence = None
        if paper_ask is not None:
            evidence = PaperExecutionEvidence(
                evidence_id=(f"fixture-paper-evidence-{fixture_scope}-{decision_at.isoformat()}"),
                provider="FIXTURE",
                provider_market_ticker=f"fixture-paper-market-{fixture_scope}",
                provider_series_ticker="fixture-paper-series",
                market_observation_source_id=source_id,
                instrument_id=instrument.instrument_id,
                rule_version_id=rule.rule_version_id,
                rule_content_hash=rule.content_hash,
                fee_model_version="fixture-autopilot-fee-v1",
                observed_at=decision_at - timedelta(seconds=1),
                available_at=decision_at,
                yes_asks=(PaperOrderbookLevel(Price(paper_ask, "0.01"), Quantity("1", "1")),),
                no_asks=(PaperOrderbookLevel(Price("0.50", "0.01"), Quantity("2", "1")),),
                yes_bids=(PaperOrderbookLevel(Price("0.49", "0.01"), Quantity("2", "1")),),
                no_bids=(PaperOrderbookLevel(Price("0.49", "0.01"), Quantity("2", "1")),),
                evidence_references=(f"fixture-evidence-{label}",),
                content_hash=_hash(
                    {
                        "campaign_id": context.campaign_id,
                        "paper-evidence": label,
                        "at": decision_at,
                    }
                ),
            )
        return ShadowOpportunity(
            proposal=proposal,
            economics=economics,
            information_set=information_set,
            market_observation_source_id=source_id,
            paper_execution_evidence=evidence,
        )

    def evaluate(self, context: AutopilotStrategyContext) -> StrategyInvocationResult:
        if self._paper:
            return StrategyInvocationResult(
                state=InvocationState.COMPLETED,
                opportunities=(
                    self._opportunity(
                        context,
                        label="paper-full",
                        conservative_yes="0.90",
                        payout_yes="1",
                        total_cost="0.50",
                        paper_ask="0.50",
                        descriptor=self.descriptor,
                    ),
                    self._opportunity(
                        context,
                        label="paper-partial",
                        conservative_yes="0.90",
                        payout_yes="1",
                        total_cost="0.50",
                        visible_quantity="2",
                        paper_ask="0.50",
                        descriptor=self.descriptor,
                    ),
                    self._opportunity(
                        context,
                        label="paper-unfilled",
                        conservative_yes="0.90",
                        payout_yes="1",
                        total_cost="0.50",
                        paper_ask="0.60",
                        descriptor=self.descriptor,
                    ),
                    self._opportunity(
                        context,
                        label="paper-overbudget",
                        conservative_yes="0.90",
                        payout_yes="1",
                        total_cost="300",
                        paper_ask="0.50",
                        descriptor=self.descriptor,
                    ),
                ),
                evidence_references=("fixture-paper-evidence",),
                diagnostics=(("source", "fixture-paper"),),
                reason_codes=("FIXTURE_PAPER_OPPORTUNITIES_EMITTED",),
            )
        return StrategyInvocationResult(
            state=InvocationState.COMPLETED,
            opportunities=(
                self._opportunity(
                    context,
                    label="positive",
                    conservative_yes="0.90",
                    payout_yes="200",
                    total_cost="100",
                ),
                self._opportunity(
                    context,
                    label="nonpositive",
                    conservative_yes="0.20",
                    payout_yes="200",
                    total_cost="100",
                ),
                self._opportunity(
                    context,
                    label="overbudget",
                    conservative_yes="0.90",
                    payout_yes="500",
                    total_cost="300",
                ),
            ),
            evidence_references=("fixture-autopilot-evidence",),
            diagnostics=(("source", "fixture"),),
            reason_codes=("FIXTURE_OPPORTUNITIES_EMITTED",),
        )


class FixtureFailingAutopilotStrategy:
    descriptor = _descriptor("fixture-autopilot-failure", failing=True)

    def __init__(self, *, paper: bool = False) -> None:
        self.descriptor = _descriptor(
            "fixture-paper-failure" if paper else "fixture-autopilot-failure",
            failing=True,
            authority=(StrategyAuthorityState.PAPER if paper else StrategyAuthorityState.SHADOW),
        )

    def evaluate(self, context: AutopilotStrategyContext) -> StrategyInvocationResult:
        _ = context
        raise RuntimeError("fixture strategy failure")


def fixture_autopilot_registry() -> StrategyRegistry:
    """Explicit demo-only registry; normal production registration excludes it."""

    return StrategyRegistry((FixtureAutopilotStrategy(), FixtureFailingAutopilotStrategy()))


def fixture_paper_portfolio_registry() -> StrategyRegistry:
    """Explicit demo-only PAPER registration; production remains unregistered."""

    return StrategyRegistry(
        (FixtureAutopilotStrategy(paper=True), FixtureFailingAutopilotStrategy(paper=True))
    )


GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID = "fixture-governed-evidence-paper-v1"


class FixtureGovernedEvidenceStrategy:
    """One unregistered fixture strategy used only by the governed trial proof.

    Its durable descriptor begins in SHADOW.  The Paper-cycle runner consults
    durable authority plus the campaign trial rather than treating this static
    fixture declaration as PAPER permission.
    """

    descriptor = _descriptor(GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID, failing=False)

    def evaluate(self, context: AutopilotStrategyContext) -> StrategyInvocationResult:
        return StrategyInvocationResult(
            state=InvocationState.COMPLETED,
            opportunities=(
                FixtureAutopilotStrategy._opportunity(
                    context,
                    label="governed-paper-open",
                    conservative_yes="0.90",
                    payout_yes="1",
                    total_cost="0.50",
                    paper_ask="0.50",
                    descriptor=self.descriptor,
                ),
                FixtureAutopilotStrategy._opportunity(
                    context,
                    label="governed-paper-loss",
                    conservative_yes="0.90",
                    payout_yes="1",
                    total_cost="0.50",
                    paper_ask="0.50",
                    descriptor=self.descriptor,
                ),
            ),
            evidence_references=("fixture-governed-evidence",),
            diagnostics=(("source", "fixture-governed-evidence"),),
            reason_codes=("FIXTURE_GOVERNED_PAPER_OPPORTUNITIES_EMITTED",),
        )


def fixture_governed_evidence_registry() -> StrategyRegistry:
    """Return the fixture-only registry; production registration excludes it."""

    return StrategyRegistry((FixtureGovernedEvidenceStrategy(),))


class EvidenceReviewFixtureMarketClient:
    """One bounded, generated Kalshi-shaped page for deterministic forward evidence."""

    def __init__(self, *, count: int, received_at: datetime) -> None:
        if not 100 <= count <= 300:
            raise ValueError("fixture evidence market count must be between 100 and 300")
        self._count = count
        self._received_at = received_at

    def fetch(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        exclude_multivariate: bool,
    ) -> CapturedPublicMarketPage:
        if (
            status != "open"
            or cursor is not None
            or page_size < self._count
            or not exclude_multivariate
            or not 1 <= timeout_seconds <= 60
        ):
            raise ValueError("fixture evidence discovery request is incompatible")
        markets = [
            {
                "ticker": f"KXFIXTURE-EVIDENCE-{ordinal:03d}",
                "event_ticker": f"KXFIXTURE-EVENT-{ordinal:03d}",
                "title": f"Fixture governed evidence market {ordinal}",
                "subtitle": "Deterministic forward-scoring fixture only",
                "status": "open",
                "open_time": "2026-01-01T00:00:00Z",
                "close_time": "2026-12-31T00:00:00Z",
                "latest_expiration_time": "2027-01-01T00:00:00Z",
                "yes_bid_dollars": "0.49",
                "yes_ask_dollars": "0.50",
                "no_bid_dollars": "0.50",
                "no_ask_dollars": "0.51",
                "yes_bid_size_fp": "10",
                "yes_ask_size_fp": "10",
                "volume_fp": "0",
                "volume_24h_fp": "0",
                "open_interest_fp": "0",
                "liquidity_dollars": "0",
                "rules_primary": f"Fixture governed evidence rule {ordinal}.",
                "rules_secondary": None,
            }
            for ordinal in range(self._count)
        ]
        document = {"cursor": None, "markets": markets}
        raw_payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return CapturedPublicMarketPage(
            endpoint_identity="KALSHI_GET_MARKETS",
            request_cursor=None,
            received_at=self._received_at,
            http_status=200,
            response_content_type="application/json",
            raw_payload=raw_payload,
            response_cursor=None,
            document=document,
        )


def fixture_governed_forward_prediction(
    *,
    campaign_id: str,
    market_snapshot_id: str,
    market_ticker: str,
    event_ticker: str,
    decision_at: datetime,
    ordinal: int,
) -> tuple[ShadowPrediction, str]:
    """Generate one forward forecast; scoring is deliberately left to production code."""

    outcome = "YES" if ordinal % 2 == 0 else "NO"
    correct_probability = (Decimal("0.55"), Decimal("0.70"), Decimal("0.90"))[ordinal % 3]
    conservative_probability = (
        Decimal("0.52"),
        Decimal("0.65"),
        Decimal("0.85"),
    )[ordinal % 3]

    def binary(probability: Decimal) -> tuple[tuple[str, Decimal], ...]:
        yes = probability if outcome == "YES" else Decimal(1) - probability
        return (("NO", Decimal(1) - yes), ("YES", yes))

    payload = {
        "strategy_id": GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID,
        "campaign_id": campaign_id,
        "market_snapshot_id": market_snapshot_id,
        "ordinal": ordinal,
        "decision_at": decision_at,
    }
    return (
        ShadowPrediction(
            prediction_id=deterministic_id("fixture_governed_forward_prediction", payload),
            strategy_id=GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID,
            strategy_version="fixture-v1",
            campaign_id=campaign_id,
            cycle_id=None,
            invocation_id=None,
            proposal_id=None,
            provider="KALSHI",
            instrument_id=market_ticker,
            event_id=event_ticker,
            decision_at=decision_at,
            resolution_at=decision_at + timedelta(days=1),
            point_probabilities=binary(correct_probability),
            conservative_probabilities=binary(conservative_probability),
            market_probabilities=(("NO", Decimal("0.50")), ("YES", Decimal("0.50"))),
            market_evidence_id=market_snapshot_id,
            expected_fee=Decimal(0),
            slippage_reserve=Decimal(0),
            execution_uncertainty_reserve=Decimal(0),
            evidence_tier=EvidenceTier.STRONG.value,
            evidence_references=(market_snapshot_id, f"fixture-governed-evidence-{ordinal}"),
            thesis_summary="Fixture-only forward forecast for governed PAPER proof.",
            correlation_buckets=(f"fixture-event-{ordinal}",),
            operator_note="No provider submission, execution, or LIVE authority.",
            model_configuration_version="fixture-v1",
            canonical_input_hash=content_digest(payload),
            canonical_prediction_hash=content_digest(
                {
                    "payload": payload,
                    "point": binary(correct_probability),
                    "conservative": binary(conservative_probability),
                }
            ),
            state=ShadowPredictionState.UNRESOLVED,
        ),
        outcome,
    )
