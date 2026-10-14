"""Deterministic, provider-free Kalshi operating and PAPER acceptance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.application.kalshi_operating import (
    ApplyKalshiPaperFill,
    AuthorizeKalshiPaperOpportunity,
    CancelKalshiPaperOrder,
    CreateKalshiPaperAccount,
    InMemoryKalshiOperatingRepository,
    KalshiOperatingRepository,
    PreviewKalshiPaperOrder,
    ReconcileKalshiPaperAccount,
    RetainKalshiCapability,
    SettleKalshiPaperPosition,
    SubmitKalshiPaperOrder,
)
from apex.domain.kalshi_operating import (
    KalshiCapability,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiPaperAuthorization,
    KalshiPaperEligibility,
    KalshiReconciliationStatus,
    KalshiResolutionFact,
    KalshiSide,
)

ACCEPTANCE_AT = datetime(2026, 7, 30, 16, 0, tzinfo=UTC)
ACCEPTANCE_ACCOUNT_ID = "apex-kalshi-paper-fixture-v1"


def _opportunity(*, suffix: str, side: KalshiSide) -> KalshiCurrentOpportunity:
    return KalshiCurrentOpportunity.create(
        opportunity_id=f"apex-kalshi-paper-fixture-opportunity-{suffix}",
        refresh_id="apex-kalshi-fixture-refresh-v1",
        evaluation_id=f"apex-kalshi-fixture-evaluation-{suffix}",
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_ticker=f"APEX-KALSHI-FIXTURE-{suffix.upper()}",
        event_ticker="APEX-KALSHI-FIXTURE-EVENT",
        rule_hash="a" * 64,
        side=side,
        price=Decimal("0.45"),
        visible_liquidity=100,
        close_at=ACCEPTANCE_AT + timedelta(hours=2),
        settlement_at=ACCEPTANCE_AT + timedelta(hours=3),
        evidence_observed_at=ACCEPTANCE_AT,
        as_of=ACCEPTANCE_AT,
        supporting_evidence_ids=(
            f"fixture-market-{suffix}",
            f"fixture-orderbook-{suffix}",
            f"fixture-fee-{suffix}",
        ),
    )


def run_kalshi_paper_fixture_acceptance(
    repository: KalshiOperatingRepository | None = None,
) -> dict[str, object]:
    repository = repository or InMemoryKalshiOperatingRepository()
    capability = KalshiCapability.inspect(
        capability_id="apex-kalshi-fixture-capability-v1",
        inspected_at=ACCEPTANCE_AT,
        public_discovery=True,
        public_orderbook=True,
        public_rules=True,
    )
    RetainKalshiCapability(repository).execute(
        capability=capability,
        idempotency_key="apex-kalshi-fixture-capability-key-v1",
    )
    account_result = CreateKalshiPaperAccount(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opening_cash=Decimal("1000"),
        created_at=ACCEPTANCE_AT,
        idempotency_key="apex-kalshi-fixture-account-key-v1",
    )
    account_replay = CreateKalshiPaperAccount(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opening_cash=Decimal("1000"),
        created_at=ACCEPTANCE_AT,
        idempotency_key="apex-kalshi-fixture-account-key-v1",
    )
    first = _opportunity(suffix="first", side=KalshiSide.YES)
    eligibility = KalshiPaperEligibility.evaluate(
        decision_id="apex-kalshi-fixture-eligibility-first",
        opportunity=first,
        account=account_result.value.account,
        available_cash=account_result.value.available_cash,
        shared_eligible=True,
        operating_cycle_enabled=True,
        evaluated_at=ACCEPTANCE_AT,
    )
    authorization = KalshiPaperAuthorization.create(
        authorization_id="apex-kalshi-fixture-authorization-first",
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opportunity=first,
        eligibility=eligibility,
        maximum_contracts=10,
        maximum_dollar_risk=Decimal("25"),
        expires_at=ACCEPTANCE_AT + timedelta(hours=1),
        actor="fixture-operator",
        reason="bounded deterministic PAPER acceptance",
        idempotency_key="apex-kalshi-fixture-authorize-first-key",
        created_at=ACCEPTANCE_AT,
    )
    AuthorizeKalshiPaperOpportunity(repository).execute(authorization=authorization)
    preview = PreviewKalshiPaperOrder(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opportunity=first,
        preview_id="apex-kalshi-fixture-preview-first",
        authorization_id=authorization.authorization_id,
        limit_price=Decimal("0.45"),
        contracts=10,
        available_liquidity=100,
        evidence_id="fixture-pre-submission-orderbook-first",
        evidence_observed_at=ACCEPTANCE_AT,
        created_at=ACCEPTANCE_AT + timedelta(seconds=1),
        idempotency_key="apex-kalshi-fixture-preview-first-key",
    )
    order = SubmitKalshiPaperOrder(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id="apex-kalshi-fixture-order-first",
        preview_id=preview.value.preview_id,
        submitted_at=ACCEPTANCE_AT + timedelta(seconds=2),
        idempotency_key="apex-kalshi-fixture-submit-first-key",
    )
    fill = ApplyKalshiPaperFill(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=str(order.value.order_id),
        fill_id="apex-kalshi-fixture-fill-first",
        contracts=10,
        price=Decimal("0.44"),
        evidence_id="fixture-later-orderbook-first",
        evidence_observed_at=ACCEPTANCE_AT + timedelta(seconds=3),
        ingested_at=ACCEPTANCE_AT + timedelta(seconds=4),
        idempotency_key="apex-kalshi-fixture-fill-first-key",
    )
    fill_replay = ApplyKalshiPaperFill(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=str(order.value.order_id),
        fill_id="apex-kalshi-fixture-fill-first",
        contracts=10,
        price=Decimal("0.44"),
        evidence_id="fixture-later-orderbook-first",
        evidence_observed_at=ACCEPTANCE_AT + timedelta(seconds=3),
        ingested_at=ACCEPTANCE_AT + timedelta(seconds=4),
        idempotency_key="apex-kalshi-fixture-fill-first-key",
    )

    second = _opportunity(suffix="second", side=KalshiSide.NO)
    current = repository.state(ACCEPTANCE_ACCOUNT_ID)
    assert current is not None
    second_eligibility = KalshiPaperEligibility.evaluate(
        decision_id="apex-kalshi-fixture-eligibility-second",
        opportunity=second,
        account=current.account,
        available_cash=current.available_cash,
        shared_eligible=True,
        operating_cycle_enabled=True,
        evaluated_at=ACCEPTANCE_AT,
    )
    second_authorization = KalshiPaperAuthorization.create(
        authorization_id="apex-kalshi-fixture-authorization-second",
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opportunity=second,
        eligibility=second_eligibility,
        maximum_contracts=5,
        maximum_dollar_risk=Decimal("25"),
        expires_at=ACCEPTANCE_AT + timedelta(hours=1),
        actor="fixture-operator",
        reason="prove reservation release on cancellation",
        idempotency_key="apex-kalshi-fixture-authorize-second-key",
        created_at=ACCEPTANCE_AT,
    )
    AuthorizeKalshiPaperOpportunity(repository).execute(authorization=second_authorization)
    second_preview = PreviewKalshiPaperOrder(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opportunity=second,
        preview_id="apex-kalshi-fixture-preview-second",
        authorization_id=second_authorization.authorization_id,
        limit_price=Decimal("0.45"),
        contracts=5,
        available_liquidity=100,
        evidence_id="fixture-pre-submission-orderbook-second",
        evidence_observed_at=ACCEPTANCE_AT,
        created_at=ACCEPTANCE_AT + timedelta(seconds=5),
        idempotency_key="apex-kalshi-fixture-preview-second-key",
    )
    second_order = SubmitKalshiPaperOrder(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id="apex-kalshi-fixture-order-second",
        preview_id=second_preview.value.preview_id,
        submitted_at=ACCEPTANCE_AT + timedelta(seconds=6),
        idempotency_key="apex-kalshi-fixture-submit-second-key",
    )
    canceled = CancelKalshiPaperOrder(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=str(second_order.value.order_id),
        canceled_at=ACCEPTANCE_AT + timedelta(seconds=7),
        idempotency_key="apex-kalshi-fixture-cancel-second-key",
    )

    state = repository.state(ACCEPTANCE_ACCOUNT_ID)
    assert state is not None and len(state.positions) == 1
    fact = KalshiResolutionFact.create(
        resolution_fact_id="apex-kalshi-fixture-resolution-first",
        market_ticker=first.market_ticker,
        event_ticker=first.event_ticker,
        resolution=KalshiSide.YES,
        resolution_source="FIXTURE_EXACT_RESOLUTION",
        rule_hash=first.rule_hash,
        settled_at=ACCEPTANCE_AT + timedelta(hours=3),
        retained_at=ACCEPTANCE_AT + timedelta(hours=3, seconds=1),
        evidence_class=KalshiEvidenceClass.FIXTURE,
    )
    settlement = SettleKalshiPaperPosition(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        position_id=state.positions[0].position_id,
        fact=fact,
        settlement_id="apex-kalshi-fixture-settlement-first",
        idempotency_key="apex-kalshi-fixture-settlement-first-key",
    )
    settlement_replay = SettleKalshiPaperPosition(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        position_id=state.positions[0].position_id,
        fact=fact,
        settlement_id="apex-kalshi-fixture-settlement-first",
        idempotency_key="apex-kalshi-fixture-settlement-first-key",
    )
    reconciliation = ReconcileKalshiPaperAccount(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        reconciliation_id="apex-kalshi-fixture-reconciliation-v1",
        reconciled_at=ACCEPTANCE_AT + timedelta(hours=3, seconds=2),
        idempotency_key="apex-kalshi-fixture-reconciliation-key-v1",
    )
    final_state = repository.state(ACCEPTANCE_ACCOUNT_ID)
    assert final_state is not None
    assert reconciliation.value.status is KalshiReconciliationStatus.CLEAN
    filled_order = next(
        item for item in final_state.orders if item.order_id == "apex-kalshi-fixture-order-first"
    )
    return {
        "capability": capability,
        "account": final_state.account,
        "authorization": authorization,
        "preview": preview.value,
        "filled_order": filled_order,
        "fill": fill.value,
        "canceled_order": canceled.value,
        "resolution": fact,
        "settlement": settlement.value,
        "reconciliation": reconciliation.value,
        "cash": final_state.cash,
        "reserved_cash": final_state.reserved_cash,
        "modeled_fees": final_state.modeled_fees,
        "realized_paper_pnl": final_state.realized_paper_pnl,
        "positions": final_state.positions,
        "journals": final_state.journals,
        "replay": {
            "account": account_replay.idempotent_replay,
            "fill": fill_replay.idempotent_replay,
            "settlement": settlement_replay.idempotent_replay,
            "state_hash": final_state.canonical_hash,
        },
        "provider_requests": 0,
        "provider_writes": 0,
        "genuine_financial_mutations": 0,
        "live_authority": "NONE",
    }
