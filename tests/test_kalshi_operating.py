from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.kalshi_operating_acceptance import (
    run_kalshi_paper_fixture_acceptance,
)
from apex.application.kalshi_operating import (
    ApplyKalshiPaperFill,
    AuthorizeKalshiPaperOpportunity,
    CancelKalshiPaperOrder,
    CreateKalshiPaperAccount,
    ExpireKalshiPaperOrder,
    InMemoryKalshiOperatingRepository,
    PreviewKalshiPaperOrder,
    SettleKalshiPaperPosition,
    SubmitKalshiPaperOrder,
)
from apex.domain.kalshi_operating import (
    KALSHI_PAPER_ORDER_ELIGIBILITY_POLICY_V1,
    MAX_PAPER_ORDER_RISK,
    KalshiCapability,
    KalshiCapabilityState,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiMarketEligibilityState,
    KalshiOperatingError,
    KalshiPaperAuthorization,
    KalshiPaperEligibility,
    KalshiReconciliationStatus,
    KalshiResolutionFact,
    KalshiSide,
    reconcile_paper_state,
)

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def opportunity(
    *,
    side: KalshiSide = KalshiSide.YES,
    observed_at: datetime = NOW,
    book_complete: bool = True,
    visible_liquidity: int = 20,
) -> KalshiCurrentOpportunity:
    return KalshiCurrentOpportunity.create(
        opportunity_id=f"kalshi-test-{side.value.lower()}",
        refresh_id="kalshi-test-refresh",
        evaluation_id="kalshi-test-evaluation",
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_ticker="KXTEST-YESNO",
        event_ticker="KXTEST",
        rule_hash="a" * 64,
        side=side,
        price="0.45",
        visible_liquidity=visible_liquidity,
        close_at=NOW + timedelta(hours=2),
        settlement_at=NOW + timedelta(hours=3),
        evidence_observed_at=observed_at,
        as_of=NOW,
        supporting_evidence_ids=("market", "orderbook", "fee"),
        book_complete=book_complete,
    )


def initialized() -> tuple[
    InMemoryKalshiOperatingRepository,
    KalshiCurrentOpportunity,
    KalshiPaperAuthorization,
]:
    repository = InMemoryKalshiOperatingRepository()
    account = (
        CreateKalshiPaperAccount(repository)
        .execute(
            account_id="kalshi-test-account",
            opening_cash="1000",
            created_at=NOW,
            idempotency_key="account-key",
        )
        .value
    )
    source = opportunity()
    eligibility = KalshiPaperEligibility.evaluate(
        decision_id="kalshi-test-eligibility",
        opportunity=source,
        account=account.account,
        available_cash=account.available_cash,
        shared_eligible=True,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    authorization = KalshiPaperAuthorization.create(
        authorization_id="kalshi-test-authorization",
        account_id=account.account.account_id,
        opportunity=source,
        eligibility=eligibility,
        maximum_contracts=10,
        maximum_dollar_risk=MAX_PAPER_ORDER_RISK,
        expires_at=NOW + timedelta(hours=1),
        actor="test-operator",
        reason="exact test approval",
        idempotency_key="authorize-key",
        created_at=NOW,
    )
    AuthorizeKalshiPaperOpportunity(repository).execute(authorization=authorization)
    return repository, source, authorization


def test_capability_defaults_to_paper_only_without_credentials() -> None:
    capability = KalshiCapability.inspect(
        capability_id="kalshi-test-capability",
        inspected_at=NOW,
        public_discovery=True,
        public_orderbook=True,
        public_rules=True,
    )
    assert capability.primary_state is KalshiCapabilityState.PAPER_ONLY
    assert capability.public_refresh_available
    assert KalshiCapabilityState.PRIVATE_CREDENTIALS_ABSENT in capability.states
    assert (
        capability.authenticated_account_visibility
        is KalshiCapabilityState.PRIVATE_CREDENTIALS_ABSENT
    )
    assert capability.paper_capability is KalshiCapabilityState.PAPER_ONLY
    assert capability.live_capability is KalshiCapabilityState.UNAVAILABLE
    assert capability.credentials_requested is False
    assert capability.provider_requests == 0
    assert capability.live_authority == "NONE"


def test_market_policy_fails_closed_for_stale_or_incomplete_evidence() -> None:
    stale = opportunity(observed_at=NOW - timedelta(seconds=301))
    incomplete = opportunity(book_complete=False, visible_liquidity=0)
    assert stale.eligibility is KalshiMarketEligibilityState.REJECTED
    assert "EVIDENCE_STALE_OR_UNAVAILABLE" in stale.rejection_reasons
    assert incomplete.eligibility is KalshiMarketEligibilityState.REJECTED
    assert {
        "ORDERBOOK_MISSING_SIDE",
        "VISIBLE_LIQUIDITY_INSUFFICIENT",
    }.issubset(incomplete.rejection_reasons)


def test_paper_eligibility_requires_shared_cycle_account_and_exact_policy() -> None:
    source = opportunity()
    decision = KalshiPaperEligibility.evaluate(
        decision_id="rejected-eligibility",
        opportunity=source,
        account=None,
        available_cash="0",
        shared_eligible=False,
        operating_cycle_enabled=False,
        evaluated_at=NOW,
    )
    assert decision.policy_id == KALSHI_PAPER_ORDER_ELIGIBILITY_POLICY_V1
    assert decision.eligible is False
    assert {
        "PAPER_ACCOUNT_UNAVAILABLE",
        "PAPER_CASH_UNAVAILABLE",
        "SHARED_OPPORTUNITY_NOT_ELIGIBLE",
        "OPERATING_CYCLE_NOT_ENABLED",
    }.issubset(decision.reasons)


def test_paper_eligibility_requires_cash_for_one_exact_contract() -> None:
    repository = InMemoryKalshiOperatingRepository()
    state = (
        CreateKalshiPaperAccount(repository)
        .execute(
            account_id="low-cash-account",
            opening_cash="0.40",
            created_at=NOW,
            idempotency_key="low-cash-account-key",
        )
        .value
    )
    decision = KalshiPaperEligibility.evaluate(
        decision_id="low-cash-eligibility",
        opportunity=opportunity(),
        account=state.account,
        available_cash=state.available_cash,
        shared_eligible=True,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    assert not decision.eligible
    assert "PAPER_CASH_UNAVAILABLE" in decision.reasons


def test_authorization_rejects_more_than_25_dollars() -> None:
    repository = InMemoryKalshiOperatingRepository()
    state = (
        CreateKalshiPaperAccount(repository)
        .execute(
            account_id="risk-account",
            opening_cash="1000",
            created_at=NOW,
            idempotency_key="risk-account-key",
        )
        .value
    )
    source = opportunity()
    eligibility = KalshiPaperEligibility.evaluate(
        decision_id="risk-eligibility",
        opportunity=source,
        account=state.account,
        available_cash=state.available_cash,
        shared_eligible=True,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    with pytest.raises(KalshiOperatingError, match="KALSHI_PAPER_ORDER_RISK_LIMIT_EXCEEDED"):
        KalshiPaperAuthorization.create(
            authorization_id="too-large",
            account_id=state.account.account_id,
            opportunity=source,
            eligibility=eligibility,
            maximum_contracts=100,
            maximum_dollar_risk="25.01",
            expires_at=NOW + timedelta(hours=1),
            actor="operator",
            reason="must fail",
            idempotency_key="too-large-key",
            created_at=NOW,
        )


def test_authorization_is_bound_to_exact_opportunity_hash_and_account() -> None:
    repository = InMemoryKalshiOperatingRepository()
    first = (
        CreateKalshiPaperAccount(repository)
        .execute(
            account_id="binding-account-one",
            opening_cash="1000",
            created_at=NOW,
            idempotency_key="binding-account-one-key",
        )
        .value
    )
    CreateKalshiPaperAccount(repository).execute(
        account_id="binding-account-two",
        opening_cash="1000",
        created_at=NOW,
        idempotency_key="binding-account-two-key",
    )
    source = opportunity()
    eligibility = KalshiPaperEligibility.evaluate(
        decision_id="binding-eligibility",
        opportunity=source,
        account=first.account,
        available_cash=first.available_cash,
        shared_eligible=True,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    inputs = {
        "authorization_id": "binding-authorization",
        "account_id": "binding-account-two",
        "opportunity": source,
        "eligibility": eligibility,
        "maximum_contracts": 1,
        "maximum_dollar_risk": Decimal("1"),
        "expires_at": NOW + timedelta(hours=1),
        "actor": "operator",
        "reason": "must remain exact",
        "idempotency_key": "binding-authorization-key",
        "created_at": NOW,
    }
    with pytest.raises(KalshiOperatingError, match="KALSHI_PAPER_OPPORTUNITY_NOT_ELIGIBLE"):
        KalshiPaperAuthorization.create(**inputs)
    with pytest.raises(KalshiOperatingError, match="KALSHI_PAPER_OPPORTUNITY_NOT_ELIGIBLE"):
        KalshiPaperAuthorization.create(
            **{
                **inputs,
                "account_id": first.account.account_id,
                "opportunity": replace(source, canonical_hash="b" * 64),
            }
        )


def test_preview_submission_requires_cash_and_fill_requires_later_evidence() -> None:
    repository, source, authorization = initialized()
    preview = (
        PreviewKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            opportunity=source,
            preview_id="kalshi-test-preview",
            authorization_id=authorization.authorization_id,
            limit_price="0.45",
            contracts=10,
            available_liquidity=20,
            evidence_id="pre-submission-book",
            evidence_observed_at=NOW,
            created_at=NOW + timedelta(seconds=1),
            idempotency_key="preview-key",
        )
        .value
    )
    assert preview.accepted
    assert preview.gross_cost == Decimal("4.50")
    assert preview.modeled_fees == Decimal("0.18")
    order = (
        SubmitKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            order_id="kalshi-test-order",
            preview_id=preview.preview_id,
            submitted_at=NOW + timedelta(seconds=2),
            idempotency_key="submit-key",
        )
        .value
    )
    with pytest.raises(KalshiOperatingError, match="KALSHI_PAPER_PRE_SUBMISSION_FILL_FORBIDDEN"):
        ApplyKalshiPaperFill(repository).execute(
            account_id="kalshi-test-account",
            order_id=order.order_id,
            fill_id="invalid-fill",
            contracts=1,
            price="0.44",
            evidence_id="old-book",
            evidence_observed_at=NOW + timedelta(seconds=2),
            ingested_at=NOW + timedelta(seconds=3),
            idempotency_key="invalid-fill-key",
        )


def test_duplicate_fill_replays_exactly_and_changed_key_cannot_overfill() -> None:
    repository, source, authorization = initialized()
    preview = (
        PreviewKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            opportunity=source,
            preview_id="fill-preview",
            authorization_id=authorization.authorization_id,
            limit_price="0.45",
            contracts=2,
            available_liquidity=20,
            evidence_id="preview-book",
            evidence_observed_at=NOW,
            created_at=NOW + timedelta(seconds=1),
            idempotency_key="fill-preview-key",
        )
        .value
    )
    order = (
        SubmitKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            order_id="fill-order",
            preview_id=preview.preview_id,
            submitted_at=NOW + timedelta(seconds=2),
            idempotency_key="fill-submit-key",
        )
        .value
    )
    service = ApplyKalshiPaperFill(repository)
    first = service.execute(
        account_id="kalshi-test-account",
        order_id=order.order_id,
        fill_id="fill-exact",
        contracts=2,
        price="0.44",
        evidence_id="later-book",
        evidence_observed_at=NOW + timedelta(seconds=3),
        ingested_at=NOW + timedelta(seconds=4),
        idempotency_key="fill-exact-key",
    )
    replay = service.execute(
        account_id="kalshi-test-account",
        order_id=order.order_id,
        fill_id="fill-exact",
        contracts=2,
        price="0.44",
        evidence_id="later-book",
        evidence_observed_at=NOW + timedelta(seconds=3),
        ingested_at=NOW + timedelta(seconds=4),
        idempotency_key="fill-exact-key",
    )
    assert replay.idempotent_replay
    assert replay.value == first.value
    submit_replay = SubmitKalshiPaperOrder(repository).execute(
        account_id="kalshi-test-account",
        order_id=order.order_id,
        preview_id=preview.preview_id,
        submitted_at=NOW + timedelta(seconds=2),
        idempotency_key="fill-submit-key",
    )
    assert submit_replay.idempotent_replay
    assert submit_replay.value.state.value == "FILLED"
    with pytest.raises(KalshiOperatingError, match="KALSHI_PAPER_ORDER_NOT_FILLABLE"):
        service.execute(
            account_id="kalshi-test-account",
            order_id=order.order_id,
            fill_id="second-fill",
            contracts=1,
            price="0.44",
            evidence_id="another-book",
            evidence_observed_at=NOW + timedelta(seconds=5),
            ingested_at=NOW + timedelta(seconds=6),
            idempotency_key="second-fill-key",
        )


def test_partial_fill_and_expiry_release_only_unused_reservation() -> None:
    repository, source, authorization = initialized()
    preview = (
        PreviewKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            opportunity=source,
            preview_id="partial-preview",
            authorization_id=authorization.authorization_id,
            limit_price="0.45",
            contracts=10,
            available_liquidity=20,
            evidence_id="partial-preview-book",
            evidence_observed_at=NOW,
            created_at=NOW + timedelta(seconds=1),
            idempotency_key="partial-preview-key",
        )
        .value
    )
    order = (
        SubmitKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            order_id="partial-order",
            preview_id=preview.preview_id,
            submitted_at=NOW + timedelta(seconds=2),
            idempotency_key="partial-submit-key",
        )
        .value
    )
    ApplyKalshiPaperFill(repository).execute(
        account_id="kalshi-test-account",
        order_id=order.order_id,
        fill_id="partial-fill",
        contracts=4,
        price="0.44",
        evidence_id="partial-later-book",
        evidence_observed_at=NOW + timedelta(seconds=3),
        ingested_at=NOW + timedelta(seconds=4),
        idempotency_key="partial-fill-key",
    )
    partial = repository.state("kalshi-test-account")
    assert partial is not None
    assert partial.reserved_cash > 0
    assert partial.positions[0].contracts == 4
    assert partial.open_risk == partial.positions[0].cost_basis + partial.reserved_cash
    canceled = CancelKalshiPaperOrder(repository).execute(
        account_id="kalshi-test-account",
        order_id=order.order_id,
        canceled_at=NOW + timedelta(seconds=5),
        idempotency_key="partial-cancel-key",
    )
    final = repository.state("kalshi-test-account")
    assert canceled.value.state.value == "CANCELED"
    assert final is not None and final.reserved_cash == 0
    assert final.open_risk == final.positions[0].cost_basis
    with pytest.raises(KalshiOperatingError, match="KALSHI_PAPER_ORDER_NOT_EXPIRABLE"):
        ExpireKalshiPaperOrder(repository).execute(
            account_id="kalshi-test-account",
            order_id=order.order_id,
            expired_at=authorization.expires_at,
            idempotency_key="partial-expire-key",
        )


def test_resolution_must_match_market_event_and_rule_lineage() -> None:
    repository, source, authorization = initialized()
    preview = (
        PreviewKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            opportunity=source,
            preview_id="settle-preview",
            authorization_id=authorization.authorization_id,
            limit_price="0.45",
            contracts=1,
            available_liquidity=20,
            evidence_id="settle-preview-book",
            evidence_observed_at=NOW,
            created_at=NOW + timedelta(seconds=1),
            idempotency_key="settle-preview-key",
        )
        .value
    )
    order = (
        SubmitKalshiPaperOrder(repository)
        .execute(
            account_id="kalshi-test-account",
            order_id="settle-order",
            preview_id=preview.preview_id,
            submitted_at=NOW + timedelta(seconds=2),
            idempotency_key="settle-submit-key",
        )
        .value
    )
    ApplyKalshiPaperFill(repository).execute(
        account_id="kalshi-test-account",
        order_id=order.order_id,
        fill_id="settle-fill",
        contracts=1,
        price="0.44",
        evidence_id="settle-later-book",
        evidence_observed_at=NOW + timedelta(seconds=3),
        ingested_at=NOW + timedelta(seconds=4),
        idempotency_key="settle-fill-key",
    )
    state = repository.state("kalshi-test-account")
    assert state is not None
    fact = KalshiResolutionFact.create(
        resolution_fact_id="wrong-rule-fact",
        market_ticker=source.market_ticker,
        event_ticker=source.event_ticker,
        resolution=KalshiSide.YES,
        resolution_source="FIXTURE_EXACT_RESOLUTION",
        rule_hash="b" * 64,
        settled_at=NOW + timedelta(hours=3),
        retained_at=NOW + timedelta(hours=3, seconds=1),
        evidence_class=KalshiEvidenceClass.FIXTURE,
    )
    with pytest.raises(KalshiOperatingError, match="KALSHI_RESOLUTION_LINEAGE_MISMATCH"):
        SettleKalshiPaperPosition(repository).execute(
            account_id="kalshi-test-account",
            position_id=state.positions[0].position_id,
            fact=fact,
            settlement_id="wrong-rule-settlement",
            idempotency_key="wrong-rule-settlement-key",
        )
    with pytest.raises(
        KalshiOperatingError,
        match="KALSHI_RESOLUTION_RETAINED_BEFORE_SETTLEMENT",
    ):
        KalshiResolutionFact.create(
            resolution_fact_id="invalid-retention-order",
            market_ticker=source.market_ticker,
            event_ticker=source.event_ticker,
            resolution=KalshiSide.YES,
            resolution_source="FIXTURE_EXACT_RESOLUTION",
            rule_hash=source.rule_hash,
            settled_at=NOW + timedelta(hours=3),
            retained_at=NOW + timedelta(hours=2),
            evidence_class=KalshiEvidenceClass.FIXTURE,
        )


def test_fixture_acceptance_settles_releases_and_reconciles_without_provider() -> None:
    report = run_kalshi_paper_fixture_acceptance()
    reconciliation = report["reconciliation"]
    assert reconciliation.status is KalshiReconciliationStatus.CLEAN
    assert report["cash"] == Decimal("1005.42")
    assert report["reserved_cash"] == Decimal("0.00")
    assert report["modeled_fees"] == Decimal("0.18")
    assert report["realized_paper_pnl"] == Decimal("5.42")
    assert report["filled_order"].state.value == "SETTLED"
    assert report["filled_order"].filled_contracts == 10
    assert report["provider_requests"] == 0
    assert report["provider_writes"] == 0
    assert report["genuine_financial_mutations"] == 0
    assert report["live_authority"] == "NONE"
    assert all(report["replay"].values())


def test_reconciliation_reports_discrepancy_without_repairing_state() -> None:
    repository = InMemoryKalshiOperatingRepository()
    state = (
        CreateKalshiPaperAccount(repository)
        .execute(
            account_id="reconcile-account",
            opening_cash="1000",
            created_at=NOW,
            idempotency_key="reconcile-account-key",
        )
        .value
    )
    corrupt = replace(state, cash=Decimal("999"), canonical_hash="").sealed()
    result = reconcile_paper_state(
        state=corrupt,
        reconciliation_id="discrepancy",
        reconciled_at=NOW,
    )
    assert result.status is KalshiReconciliationStatus.DISCREPANCY
    assert result.issues == ("CASH_MISMATCH",)
    assert corrupt.cash == Decimal("999")


def test_exact_cli_and_read_only_api_surfaces_have_no_live_control() -> None:
    from apex.cli import parser
    from apex.operator_api import create_app

    command_names = {
        name
        for action in parser()._actions
        if action.dest == "command" and action.choices is not None
        for name in action.choices
    }
    assert {
        "kalshi-operating-capability-inspect",
        "kalshi-operating-refresh",
        "kalshi-operating-refresh-replay",
        "kalshi-operating-market-inspect",
        "kalshi-operating-rule-inspect",
        "kalshi-operating-orderbook-inspect",
        "kalshi-operating-opportunity-inspect",
        "kalshi-paper-account-inspect",
        "kalshi-paper-authorization-create",
        "kalshi-paper-preview",
        "kalshi-paper-submit",
        "kalshi-paper-apply-fill",
        "kalshi-paper-cancel",
        "kalshi-paper-expire",
        "kalshi-paper-settle",
        "kalshi-paper-reconcile",
    }.issubset(command_names)
    assert not any("live" in name for name in command_names if name.startswith("kalshi-"))
    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in create_app().routes
    }
    assert (
        "/api/v1/kalshi-operating/workspace/{account_id}",
        ("GET",),
    ) in routes
    assert (
        "/api/v1/kalshi-operating/refreshes/{refresh_id}",
        ("GET",),
    ) in routes
    assert not any(
        methods != ("GET",) and path.startswith("/api/v1/kalshi") for path, methods in routes
    )
