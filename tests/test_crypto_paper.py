from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

import pytest

from apex.domain.crypto_paper import (
    CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1,
    CryptoPaperAccount,
    CryptoPaperAccountState,
    CryptoPaperAuthorization,
    CryptoPaperEligibility,
    CryptoPaperError,
    CryptoPaperFill,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperProviderContract,
    CryptoPaperReconciliation,
    PaperAssetBalance,
    PaperEligibilityState,
    PaperOrderState,
    PaperOrderType,
    PaperPricingSource,
    PaperReconciliationStatus,
    PaperReservationState,
    PaperSide,
    ProviderCapabilityState,
    RetainedPaperPriceEvidence,
    apply_retained_evidence,
    build_paper_preview,
    build_provider_contract,
    cancel_paper_order,
    construct_order_intent,
    estimate_open_position_mark,
    evaluate_paper_eligibility,
    expire_paper_order,
    inspect_paper_capability,
    open_trial_orders_by_strategy,
    reconcile_paper_state,
    submit_paper_order,
)
from apex.domain.crypto_universe import (
    CatalogPaginationStatus,
    CryptoUniverseSnapshot,
    PublicCryptoProduct,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import (
    EligibilityDecision,
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
)

NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def product() -> PublicCryptoProduct:
    return PublicCryptoProduct.create(
        product_id="BTC-USD",
        provider="COINBASE_ADVANCED_TRADE",
        base_asset="BTC",
        quote_asset="USD",
        market_type="SPOT",
        trading_status="ACTIVE",
        product_status="ONLINE",
        price_increment="0.01",
        quantity_increment="0.00000001",
        minimum_quantity="0.00000001",
        minimum_notional="1",
        public_volume_24h="1000000",
        captured_at=NOW - timedelta(hours=1),
        source_contract_version="COINBASE_PUBLIC_PRODUCTS_CURSOR_V1",
        raw_evidence_hash="a" * 64,
        parser_version="CRYPTO_PRODUCT_PARSER_V1",
        evidence_class=EvidenceClass.FIXTURE,
    )


def universe() -> CryptoUniverseSnapshot:
    return CryptoUniverseSnapshot(
        snapshot_id="fixture-universe",
        source_capture_id="fixture-capture",
        policy_id="CRYPTO_UNIVERSE_SELECTION_QUALITY_POLICY_V1",
        policy_version=1,
        considered_product_ids=("BTC-USD",),
        decisions=(),
        eligible_product_ids=("BTC-USD",),
        excluded_product_ids=(),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_limitations=(),
        manifest_hash="b" * 64,
        created_at=NOW - timedelta(hours=1),
        canonical_hash="c" * 64,
        catalog_pagination_status=CatalogPaginationStatus.COMPLETE,
        eligible_asset_ids=("BTC",),
        selected_routes=(("BTC", "BTC-USD"),),
    )


def projection(*, paper_eligible: bool = True) -> SharedOpportunityProjection:
    value = SharedOpportunityProjection.create(
        projection_id="fixture-shared-btc",
        venue=SharedVenue.CRYPTO,
        source_record_type="CRYPTO_PULSE_OPPORTUNITY",
        source_record_id="fixture-pulse-btc",
        source_canonical_hash="d" * 64,
        strategy_or_method_id="CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
        instrument_id="BTC",
        product_or_market_id="BTC-USD",
        direction="LONG",
        generated_at=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(days=1),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=86400,
        expected_gross_return="0.10",
        expected_net_return="0.08",
        comparable_value="0.08",
        comparable_value_identity="NET_RETURN",
        uncertainty="0.02",
        maximum_loss="25",
        modeled_fees="0.15",
        modeled_slippage="0.025",
        costs_retained=True,
        liquidity_state="LIMITED_PUBLIC_EVIDENCE",
        liquidity_limitations=("PUBLIC_VOLUME_NOT_EXECUTABLE_LIQUIDITY",),
        required_hypothetical_capital="25",
        maximum_sensible_research_allocation="25",
        source_maximum_allocation_fraction="0.025",
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=("fixture-open-before",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("FIXTURE_ISOLATED_ACCEPTANCE",),
        source_details=(("pulse_id", "fixture-pulse"),),
    )
    return replace(value, paper_eligible=paper_eligible)


def shared_decision(state: EligibilityState = EligibilityState.ELIGIBLE) -> EligibilityDecision:
    return EligibilityDecision(
        decision_id="fixture-shared-btc-eligibility-v1",
        projection_id="fixture-shared-btc",
        policy_id="SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1",
        evaluated_at=NOW,
        state=state,
        reasons=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
        canonical_hash="e" * 64,
    )


def prepared() -> tuple[
    CryptoPaperAccount,
    CryptoPaperAccountState,
    CryptoPaperProviderContract,
    CryptoPaperEligibility,
    CryptoPaperAuthorization,
]:
    account = CryptoPaperAccount.create(
        account_id="crypto-paper-fixture-account-v1",
        opening_cash="1000",
        created_at=NOW,
    )
    state = account_state = CryptoPaperAccountState.opening(
        account=account,
        event_id="account-created",
    )
    contract = build_provider_contract(
        contract_id="coinbase-btc-usd-paper-contract-v1",
        product=product(),
        source_capture_id="fixture-capture",
    )
    decision = evaluate_paper_eligibility(
        decision_id="fixture-paper-eligibility-v1",
        opportunity_set_id="fixture-shared-set",
        projection=projection(),
        shared_decision=shared_decision(),
        universe=universe(),
        contract=contract,
        account=account,
        account_state=state,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    authorization = CryptoPaperAuthorization.create(
        authorization_id="fixture-paper-authorization-v1",
        account_id=account.account_id,
        eligibility=decision,
        projection=projection(),
        maximum_quote_notional="25",
        maximum_base_size="0.001",
        allowed_side=PaperSide.BUY,
        allowed_order_type=PaperOrderType.MARKET,
        expires_at=NOW + timedelta(hours=1),
        actor="fixture-operator",
        reason="isolated deterministic PAPER acceptance",
        idempotency_key="fixture-paper-authorization-key",
        created_at=NOW,
    )
    return account, account_state, contract, decision, authorization


def test_capability_defaults_to_paper_only_without_credentials() -> None:
    contract = build_provider_contract(
        contract_id="coinbase-btc-usd-paper-contract-v1",
        product=product(),
        source_capture_id="fixture-capture",
    )
    capability = inspect_paper_capability(contract=contract, inspected_at=NOW)
    assert capability.primary_state is ProviderCapabilityState.PAPER_ONLY
    assert capability.states == (
        ProviderCapabilityState.PUBLIC_MARKET_DATA_AVAILABLE,
        ProviderCapabilityState.PRIVATE_CREDENTIALS_ABSENT,
        ProviderCapabilityState.PAPER_ONLY,
    )
    assert capability.provider_requests == 0
    assert not capability.credentials_requested
    assert capability.live_authority == "NONE"
    inactive = build_provider_contract(
        contract_id="coinbase-btc-usd-inactive-paper-contract-v1",
        product=replace(product(), trading_status="RESTRICTED"),
        source_capture_id="fixture-capture",
    )
    assert inactive.submission_capability is ProviderCapabilityState.UNAVAILABLE


def test_genuine_style_research_only_opportunity_fails_closed() -> None:
    account, state, contract, _, _ = prepared()
    decision = evaluate_paper_eligibility(
        decision_id="genuine-zero-submission-decision",
        opportunity_set_id="genuine-shared-set",
        projection=projection(paper_eligible=False),
        shared_decision=shared_decision(EligibilityState.EXPIRED),
        universe=universe(),
        contract=contract,
        account=account,
        account_state=state,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    assert decision.policy_id == CRYPTO_PAPER_ORDER_ELIGIBILITY_POLICY_V1
    assert decision.state is PaperEligibilityState.REJECTED
    assert "SOURCE_OPPORTUNITY_DOES_NOT_PERMIT_PAPER_CONSIDERATION" in decision.reasons


def test_considered_but_ineligible_universe_product_fails_closed() -> None:
    account, state, contract, _, _ = prepared()
    decision = evaluate_paper_eligibility(
        decision_id="considered-only-paper-eligibility",
        opportunity_set_id="fixture-shared-set",
        projection=projection(),
        shared_decision=shared_decision(),
        universe=replace(
            universe(),
            eligible_product_ids=(),
            excluded_product_ids=("BTC-USD",),
        ),
        contract=contract,
        account=account,
        account_state=state,
        operating_cycle_enabled=True,
        evaluated_at=NOW,
    )
    assert decision.state is PaperEligibilityState.REJECTED
    assert "PRODUCT_NOT_IN_CURRENT_ELIGIBLE_CRYPTO_UNIVERSE" in decision.reasons


def test_market_buy_submit_next_evidence_partial_fill_cancel_and_reconcile() -> None:
    account, state, contract, decision, authorization = prepared()
    assert decision.state is PaperEligibilityState.ELIGIBLE
    intent = construct_order_intent(
        intent_id="fixture-market-buy-intent-v1",
        authorization=authorization,
        contract=contract,
        account_state=state,
        side=PaperSide.BUY,
        order_type=PaperOrderType.MARKET,
        quote_size="25",
        base_size=None,
        limit_price=None,
        created_at=NOW + timedelta(seconds=1),
    )
    prior = RetainedPaperPriceEvidence.create(
        evidence_id="fixture-open-before",
        product_id="BTC-USD",
        observed_at=NOW - timedelta(minutes=1),
        ingested_at=NOW,
        price="50000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    preview = build_paper_preview(
        preview_id="fixture-market-buy-preview-v1",
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        evidence=prior,
        fee_bps="60",
        spread_bps="10",
        slippage_bps="10",
        created_at=NOW + timedelta(seconds=2),
    )
    assert not preview.rejection_reasons
    assert not preview.genuine_coinbase_preview
    assert preview.provider_requests == 0
    submission = submit_paper_order(
        order_id="fixture-market-buy-order-v1",
        reservation_id="fixture-market-buy-reservation-v1",
        preview=preview,
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        open_orders=(),
        submitted_at=NOW + timedelta(seconds=3),
        valid_until=NOW + timedelta(days=1, minutes=1),
        actor="fixture-operator",
    )
    assert submission.order.state is PaperOrderState.OPEN
    assert submission.account_state.reserved_cash > 0
    later = RetainedPaperPriceEvidence.create(
        evidence_id="fixture-open-after",
        product_id="BTC-USD",
        observed_at=NOW + timedelta(days=1),
        ingested_at=NOW + timedelta(days=1, seconds=1),
        price="50500",
        available_base_quantity="0.0002",
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    applied = apply_retained_evidence(
        fill_id="fixture-market-buy-fill-v1",
        order=submission.order,
        contract=contract,
        reservation=submission.reservation,
        account_state=submission.account_state,
        position=None,
        evidence=later,
        fee_bps="60",
        slippage_bps="10",
        actor="fixture-simulator",
        prior_fills=(),
    )
    assert applied is not None
    assert applied.order.state is PaperOrderState.PARTIALLY_FILLED
    assert applied.reservation.state is PaperReservationState.PARTIALLY_CONSUMED
    assert applied.account_state.cash < Decimal("1000")
    assert applied.account_state.balance("BTC").total == Decimal("0.0002")
    assert sum(
        item.amount for item in applied.journal.postings if item.direction == "DEBIT"
    ) == sum(item.amount for item in applied.journal.postings if item.direction == "CREDIT")
    canceled = cancel_paper_order(
        order=applied.order,
        reservation=applied.reservation,
        account_state=applied.account_state,
        canceled_at=NOW + timedelta(days=1, seconds=2),
        actor="fixture-operator",
        reason="exercise exact-ID cancellation",
    )
    assert canceled.order.state is PaperOrderState.CANCELED
    assert canceled.reservation.state is PaperReservationState.RELEASED
    assert canceled.account_state.reserved_cash == 0
    reconciliation = reconcile_paper_state(
        reconciliation_id="fixture-market-buy-reconciliation-v1",
        account=account,
        account_state=canceled.account_state,
        orders=(canceled.order,),
        reservations=(canceled.reservation,),
        fills=(applied.fill,),
        positions=(applied.position,),
        journals=(applied.journal,),
        evaluated_at=NOW + timedelta(days=1, seconds=3),
        order_id=canceled.order.order_id,
    )
    assert reconciliation.status is PaperReconciliationStatus.CLEAN
    assert not reconciliation.discrepancies


def test_no_same_candle_lookahead_and_limit_conditions() -> None:
    _, state, contract, _, authorization = prepared()
    authorization = replace(
        authorization,
        allowed_order_type=PaperOrderType.LIMIT,
        canonical_hash="f" * 64,
    )
    intent = construct_order_intent(
        intent_id="fixture-limit-buy-intent-v1",
        authorization=authorization,
        contract=contract,
        account_state=state,
        side=PaperSide.BUY,
        order_type=PaperOrderType.LIMIT,
        quote_size=None,
        base_size="0.0004",
        limit_price="50000",
        created_at=NOW + timedelta(seconds=1),
    )
    prior = RetainedPaperPriceEvidence.create(
        evidence_id="fixture-limit-before",
        product_id="BTC-USD",
        observed_at=NOW,
        ingested_at=NOW,
        price="49000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    preview = build_paper_preview(
        preview_id="fixture-limit-preview",
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        evidence=prior,
        fee_bps="60",
        spread_bps="10",
        slippage_bps="10",
        created_at=NOW + timedelta(seconds=2),
    )
    submission = submit_paper_order(
        order_id="fixture-limit-order",
        reservation_id="fixture-limit-reservation",
        preview=preview,
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        open_orders=(),
        submitted_at=NOW + timedelta(seconds=3),
        valid_until=NOW + timedelta(days=2),
        actor="fixture-operator",
    )
    with pytest.raises(CryptoPaperError, match="NEXT_EVIDENCE_REQUIRED"):
        apply_retained_evidence(
            fill_id="same-candle-must-not-fill",
            order=submission.order,
            contract=contract,
            reservation=submission.reservation,
            account_state=submission.account_state,
            position=None,
            evidence=prior,
            fee_bps="60",
            slippage_bps="10",
            actor="fixture-simulator",
            prior_fills=(),
        )
    above = RetainedPaperPriceEvidence.create(
        evidence_id="fixture-limit-above",
        product_id="BTC-USD",
        observed_at=NOW + timedelta(days=1),
        ingested_at=NOW + timedelta(days=1, seconds=1),
        price="51000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    assert (
        apply_retained_evidence(
            fill_id="above-limit-no-fill",
            order=submission.order,
            contract=contract,
            reservation=submission.reservation,
            account_state=submission.account_state,
            position=None,
            evidence=above,
            fee_bps="60",
            slippage_bps="10",
            actor="fixture-simulator",
            prior_fills=(),
        )
        is None
    )


@pytest.mark.parametrize(
    ("quote_size", "base_size", "limit_price", "reason"),
    [
        ("25.001", None, None, "QUOTE_INCREMENT"),
        ("0.5", None, None, "MINIMUM_NOTIONAL"),
    ],
)
def test_market_buy_rejects_invalid_rounding_and_minimums(
    quote_size: str,
    base_size: str | None,
    limit_price: str | None,
    reason: str,
) -> None:
    _, state, contract, _, authorization = prepared()
    with pytest.raises(CryptoPaperError, match=reason):
        construct_order_intent(
            intent_id=f"invalid-{reason.lower()}",
            authorization=authorization,
            contract=contract,
            account_state=state,
            side=PaperSide.BUY,
            order_type=PaperOrderType.MARKET,
            quote_size=quote_size,
            base_size=base_size,
            limit_price=limit_price,
            created_at=NOW + timedelta(seconds=1),
        )


def test_expired_authorization_and_stale_preview_fail_closed() -> None:
    _, state, contract, decision, _ = prepared()
    expired = CryptoPaperAuthorization.create(
        authorization_id="expired-authorization",
        account_id=state.account_id,
        eligibility=decision,
        projection=projection(),
        maximum_quote_notional="25",
        maximum_base_size="0.001",
        allowed_side=PaperSide.BUY,
        allowed_order_type=PaperOrderType.MARKET,
        expires_at=NOW + timedelta(seconds=1),
        actor="fixture-operator",
        reason="exercise authorization expiry",
        idempotency_key="expired-authorization-key",
        created_at=NOW,
    )
    with pytest.raises(CryptoPaperError, match="AUTHORIZATION_EXPIRED"):
        construct_order_intent(
            intent_id="expired-intent",
            authorization=expired,
            contract=contract,
            account_state=state,
            side=PaperSide.BUY,
            order_type=PaperOrderType.MARKET,
            quote_size="25",
            base_size=None,
            limit_price=None,
            created_at=NOW + timedelta(seconds=1),
        )

    _, _, _, _, authorization = prepared()
    intent = construct_order_intent(
        intent_id="stale-preview-intent",
        authorization=authorization,
        contract=contract,
        account_state=state,
        side=PaperSide.BUY,
        order_type=PaperOrderType.MARKET,
        quote_size="25",
        base_size=None,
        limit_price=None,
        created_at=NOW,
    )
    evidence = RetainedPaperPriceEvidence.create(
        evidence_id="stale-preview-evidence",
        product_id="BTC-USD",
        observed_at=NOW - timedelta(minutes=1),
        ingested_at=NOW,
        price="50000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    preview = build_paper_preview(
        preview_id="stale-preview",
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        evidence=evidence,
        fee_bps="60",
        spread_bps="10",
        slippage_bps="10",
        created_at=NOW,
    )
    with pytest.raises(CryptoPaperError, match="PREVIEW_NOT_SUBMITTABLE"):
        submit_paper_order(
            order_id="stale-preview-order",
            reservation_id="stale-preview-reservation",
            preview=preview,
            intent=intent,
            authorization=authorization,
            contract=contract,
            account_state=state,
            open_orders=(),
            submitted_at=preview.expires_at,
            valid_until=preview.expires_at + timedelta(hours=1),
            actor="fixture-operator",
        )


def test_cash_asset_and_authorization_caps_are_enforced() -> None:
    account, state, contract, decision, authorization = prepared()
    poor_account = CryptoPaperAccount.create(
        account_id="poor-paper-account",
        opening_cash="10",
        created_at=NOW,
    )
    poor_state = CryptoPaperAccountState.opening(
        account=poor_account,
        event_id="poor-account-created",
    )
    with pytest.raises(CryptoPaperError, match="CASH_INSUFFICIENT"):
        construct_order_intent(
            intent_id="insufficient-cash-intent",
            authorization=replace(authorization, account_id=poor_account.account_id),
            contract=contract,
            account_state=poor_state,
            side=PaperSide.BUY,
            order_type=PaperOrderType.MARKET,
            quote_size="25",
            base_size=None,
            limit_price=None,
            created_at=NOW + timedelta(seconds=1),
        )
    sell_authorization = CryptoPaperAuthorization.create(
        authorization_id="no-assets-sell-authorization",
        account_id=account.account_id,
        eligibility=decision,
        projection=projection(),
        maximum_quote_notional="25",
        maximum_base_size="0.001",
        allowed_side=PaperSide.SELL,
        allowed_order_type=PaperOrderType.MARKET,
        expires_at=NOW + timedelta(hours=1),
        actor="fixture-operator",
        reason="exercise no-short asset guard",
        idempotency_key="no-assets-sell-key",
        created_at=NOW,
    )
    with pytest.raises(CryptoPaperError, match="ASSET_BALANCE_INSUFFICIENT"):
        construct_order_intent(
            intent_id="insufficient-assets-intent",
            authorization=sell_authorization,
            contract=contract,
            account_state=state,
            side=PaperSide.SELL,
            order_type=PaperOrderType.MARKET,
            quote_size=None,
            base_size="0.0001",
            limit_price=None,
            created_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(CryptoPaperError, match="AUTHORIZATION_BOUND_INVALID"):
        CryptoPaperAuthorization.create(
            authorization_id="over-cap-authorization",
            account_id=account.account_id,
            eligibility=decision,
            projection=projection(),
            maximum_quote_notional="25.01",
            maximum_base_size=None,
            allowed_side=PaperSide.BUY,
            allowed_order_type=PaperOrderType.MARKET,
            expires_at=NOW + timedelta(hours=1),
            actor="fixture-operator",
            reason="must fail",
            idempotency_key="over-cap-key",
            created_at=NOW,
        )


def test_order_expiry_releases_unused_reservation() -> None:
    _, state, contract, _, authorization = prepared()
    intent = construct_order_intent(
        intent_id="expiry-intent",
        authorization=authorization,
        contract=contract,
        account_state=state,
        side=PaperSide.BUY,
        order_type=PaperOrderType.MARKET,
        quote_size="25",
        base_size=None,
        limit_price=None,
        created_at=NOW + timedelta(seconds=1),
    )
    evidence = RetainedPaperPriceEvidence.create(
        evidence_id="expiry-preview-evidence",
        product_id="BTC-USD",
        observed_at=NOW,
        ingested_at=NOW,
        price="50000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    preview = build_paper_preview(
        preview_id="expiry-preview",
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        evidence=evidence,
        fee_bps="60",
        spread_bps="10",
        slippage_bps="10",
        created_at=NOW + timedelta(seconds=2),
    )
    submission = submit_paper_order(
        order_id="expiry-order",
        reservation_id="expiry-reservation",
        preview=preview,
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        open_orders=(),
        submitted_at=NOW + timedelta(seconds=3),
        valid_until=NOW + timedelta(minutes=10),
        actor="fixture-operator",
    )
    with pytest.raises(CryptoPaperError, match="NOT_YET_EXPIRED"):
        expire_paper_order(
            order=submission.order,
            reservation=submission.reservation,
            account_state=submission.account_state,
            expired_at=NOW + timedelta(minutes=9),
            actor="fixture-operator",
        )
    expired = expire_paper_order(
        order=submission.order,
        reservation=submission.reservation,
        account_state=submission.account_state,
        expired_at=NOW + timedelta(minutes=10),
        actor="fixture-operator",
    )
    assert expired.order.state is PaperOrderState.EXPIRED
    assert expired.reservation.state is PaperReservationState.RELEASED
    assert expired.account_state.reserved_cash == 0


def test_isolated_acceptance_proves_idempotency_accounting_and_zero_provider_calls() -> None:
    from apex.adapters.crypto_paper_acceptance import (
        run_crypto_paper_fixture_acceptance,
    )

    result = run_crypto_paper_fixture_acceptance()
    assert result["submit_replay"] is True
    assert result["fill_replay"] is True
    assert result["duplicate_fill_rejected"] is True
    assert result["provider_requests"] == 0
    assert result["live_authority"] == "NONE"
    assert result["genuine_financial_mutation"] is False
    assert result["opening_cash"] == Decimal("1000")
    final_cash = result["final_cash"]
    assert isinstance(final_cash, Decimal)
    assert final_cash < Decimal("1000")
    reconciliation = result["reconciliation"]
    assert isinstance(reconciliation, CryptoPaperReconciliation)
    assert reconciliation.status is PaperReconciliationStatus.CLEAN


def test_reconciliation_reports_discrepancy_without_repair() -> None:
    account, state, _, _, _ = prepared()
    inconsistent = replace(state, reserved_cash=Decimal("1"))
    reconciliation = reconcile_paper_state(
        reconciliation_id="fixture-discrepancy",
        account=account,
        account_state=inconsistent,
        orders=(),
        reservations=(),
        fills=(),
        positions=(),
        journals=(),
        evaluated_at=NOW,
    )
    assert reconciliation.status is PaperReconciliationStatus.DISCREPANCY
    assert reconciliation.repairability == "NO_SILENT_REPAIR"
    assert reconciliation.discrepancies == ("RESERVED_CASH_MISMATCH",)


def test_market_sell_updates_position_and_realized_paper_pnl() -> None:
    account, opening, contract, decision, _ = prepared()
    state = replace(
        opening,
        cash=Decimal("900"),
        balances=(PaperAssetBalance("BTC", Decimal("0.001"), Decimal(0)),),
        canonical_hash="f" * 64,
    )
    position = CryptoPaperPosition(
        position_id=f"{account.account_id}-BTC-USD-position",
        account_id=account.account_id,
        product_id="BTC-USD",
        base_asset="BTC",
        base_quantity=Decimal("0.001"),
        average_cost=Decimal("40000"),
        cumulative_cost=Decimal("40"),
        cumulative_sale_proceeds=Decimal(0),
        realized_paper_pnl=Decimal(0),
        modeled_fees=Decimal(0),
        state="OPEN",
        version=1,
        canonical_hash="1" * 64,
    )
    authorization = CryptoPaperAuthorization.create(
        authorization_id="fixture-market-sell-authorization",
        account_id=account.account_id,
        eligibility=decision,
        projection=projection(),
        maximum_quote_notional="25",
        maximum_base_size="0.0004",
        allowed_side=PaperSide.SELL,
        allowed_order_type=PaperOrderType.MARKET,
        expires_at=NOW + timedelta(hours=1),
        actor="fixture-operator",
        reason="exercise long-only PAPER reduction",
        idempotency_key="fixture-market-sell-authorization-key",
        created_at=NOW,
    )
    intent = construct_order_intent(
        intent_id="fixture-market-sell-intent",
        authorization=authorization,
        contract=contract,
        account_state=state,
        side=PaperSide.SELL,
        order_type=PaperOrderType.MARKET,
        quote_size=None,
        base_size="0.0004",
        limit_price=None,
        created_at=NOW + timedelta(seconds=1),
    )
    before = RetainedPaperPriceEvidence.create(
        evidence_id="fixture-market-sell-before",
        product_id="BTC-USD",
        observed_at=NOW,
        ingested_at=NOW,
        price="50000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    preview = build_paper_preview(
        preview_id="fixture-market-sell-preview",
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        evidence=before,
        fee_bps="60",
        spread_bps="10",
        slippage_bps="10",
        created_at=NOW + timedelta(seconds=2),
    )
    assert not preview.rejection_reasons
    submission = submit_paper_order(
        order_id="fixture-market-sell-order",
        reservation_id="fixture-market-sell-reservation",
        preview=preview,
        intent=intent,
        authorization=authorization,
        contract=contract,
        account_state=state,
        open_orders=(),
        submitted_at=NOW + timedelta(seconds=3),
        valid_until=NOW + timedelta(days=2),
        actor="fixture-operator",
    )
    assert submission.account_state.balance("BTC").reserved == Decimal("0.0004")
    after = RetainedPaperPriceEvidence.create(
        evidence_id="fixture-market-sell-after",
        product_id="BTC-USD",
        observed_at=NOW + timedelta(days=1),
        ingested_at=NOW + timedelta(days=1, seconds=1),
        price="55000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    opening_fill = CryptoPaperFill(
        fill_id="fixture-market-sell-opening-fill",
        order_id="fixture-market-sell-opening-order",
        product_id="BTC-USD",
        side=PaperSide.BUY,
        price=Decimal("40000"),
        base_quantity=Decimal("0.001"),
        quote_quantity=Decimal("40"),
        fee=Decimal(0),
        slippage=Decimal(0),
        evidence_id="fixture-market-sell-opening-evidence",
        event_time=NOW - timedelta(days=1),
        ingestion_time=NOW - timedelta(days=1),
        canonical_hash="2" * 64,
    )
    applied = apply_retained_evidence(
        fill_id="fixture-market-sell-fill",
        order=submission.order,
        contract=contract,
        reservation=submission.reservation,
        account_state=submission.account_state,
        position=position,
        evidence=after,
        fee_bps="60",
        slippage_bps="10",
        actor="fixture-simulator",
        prior_fills=(opening_fill,),
    )
    assert applied is not None
    assert applied.order.state is PaperOrderState.FILLED
    assert applied.position.base_quantity == Decimal("0.0006")
    assert applied.position.realized_paper_pnl > 0
    assert applied.account_state.realized_paper_pnl == applied.position.realized_paper_pnl
    assert applied.account_state.balance("BTC").reserved == 0
    assert sum(
        item.amount for item in applied.journal.postings if item.direction == "DEBIT"
    ) == sum(item.amount for item in applied.journal.postings if item.direction == "CREDIT")


def test_exact_cli_read_only_api_and_empty_workspace_surfaces() -> None:
    from apex.application.crypto_paper import paper_workspace
    from apex.cli import parser
    from apex.operator_api import create_app

    parsed = parser().parse_args(
        [
            "crypto-paper-order-inspect",
            "--account-id",
            "exact-account",
            "--order-id",
            "exact-order",
        ]
    )
    assert parsed.account_id == "exact-account"
    assert parsed.order_id == "exact-order"
    routes = {
        (getattr(route, "path", ""), method)
        for route in create_app().routes
        for method in getattr(route, "methods", ())
    }
    assert (
        "/api/v1/crypto-paper/workspace/{account_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/crypto-paper/accounts/{account_id}/orders/{order_id}",
        "GET",
    ) in routes
    empty = paper_workspace(None)
    assert empty["account"] is None
    assert empty["provider_requests"] == 0
    assert empty["live_authority"] == "NONE"


def _open_position(**overrides: object) -> CryptoPaperPosition:
    values: dict[str, object] = {
        "position_id": "fixture-mark-position",
        "account_id": "fixture-mark-account",
        "product_id": "BTC-USD",
        "base_asset": "BTC",
        "base_quantity": Decimal("0.01"),
        "average_cost": Decimal("50000"),
        "cumulative_cost": Decimal("500"),
        "cumulative_sale_proceeds": Decimal(0),
        "realized_paper_pnl": Decimal(0),
        "modeled_fees": Decimal("0.30"),
        "state": "OPEN",
        "version": 1,
        "canonical_hash": "1" * 64,
    }
    values.update(overrides)
    return CryptoPaperPosition(**values)  # type: ignore[arg-type]


def test_estimate_open_position_mark_computes_market_value_and_unrealized_pnl() -> None:
    # PAPER Operations & Observability v1: mirrors
    # domain.securities_paper.mark_position's exact formula
    # (market_value - average_cost * quantity) without mutating the
    # Position or touching the balanced double-entry journal.
    marked_at = NOW + timedelta(minutes=5)
    mark = estimate_open_position_mark(
        position=_open_position(), mark_price=Decimal("55000"), marked_at=marked_at
    )
    assert mark.position_id == "fixture-mark-position"
    assert mark.product_id == "BTC-USD"
    assert mark.base_quantity == Decimal("0.01")
    assert mark.mark_price == Decimal("55000")
    assert mark.marked_at == marked_at
    assert mark.market_value == Decimal("550.00")
    assert mark.unrealized_paper_pnl == Decimal("50.00")
    assert mark.authority == "NONE"


def test_estimate_open_position_mark_is_negative_when_price_is_below_average_cost() -> None:
    mark = estimate_open_position_mark(
        position=_open_position(), mark_price=Decimal("40000"), marked_at=NOW
    )
    assert mark.market_value == Decimal("400.00")
    assert mark.unrealized_paper_pnl == Decimal("-100.00")


def test_estimate_open_position_mark_never_exceeds_the_platform_decimal_scale() -> None:
    # Regression: a raw Decimal product's scale is the SUM of its operands'
    # scales -- an 8-decimal price times an 8-decimal quantity can reach 16,
    # exceeding apex.domain.values.MAX_SCALE (8) and raising
    # ValueValidationError out of content_digest. Round, low-precision
    # fixture values (as in every other test in this file) never exercise
    # this; genuine Coinbase BTC-USD precision does -- caught only by this
    # milestone's own runtime acceptance against real retained state, not a
    # unit test, until this one was added.
    position = _open_position(
        base_quantity=Decimal("0.01234567"), average_cost=Decimal("49876.54321098")
    )
    mark = estimate_open_position_mark(
        position=position, mark_price=Decimal("56789.87654321"), marked_at=NOW
    )
    assert mark.market_value.as_tuple().exponent >= -8
    assert mark.unrealized_paper_pnl.as_tuple().exponent >= -8
    # Sanity: still numerically correct, not merely non-crashing. Matches
    # this file's own established floor-to-8-decimals convention (see
    # apply_retained_evidence's average_cost = _floor(...,
    # Decimal("0.00000001"))), not banker's-rounding quantize.
    expected_market_value = (Decimal("56789.87654321") * Decimal("0.01234567")).quantize(
        Decimal("0.00000001"), rounding=ROUND_FLOOR
    )
    expected_cost_basis = (Decimal("49876.54321098") * Decimal("0.01234567")).quantize(
        Decimal("0.00000001"), rounding=ROUND_FLOOR
    )
    assert mark.market_value == expected_market_value
    assert mark.unrealized_paper_pnl == expected_market_value - expected_cost_basis


def test_estimate_open_position_mark_rejects_a_closed_position() -> None:
    # An operator surface must never fabricate an unrealized figure for a
    # position that no longer exists as an open exposure.
    with pytest.raises(CryptoPaperError, match="CRYPTO_PAPER_POSITION_NOT_OPEN"):
        estimate_open_position_mark(
            position=_open_position(state="CLOSED"),
            mark_price=Decimal("55000"),
            marked_at=NOW,
        )


def test_estimate_open_position_mark_never_mutates_the_position_or_journal() -> None:
    # Pure, read-only, deterministic: identical inputs produce a bit-for-bit
    # identical mark, and the source Position (frozen) is untouched --
    # nothing here can ever become a second write path onto PAPER state.
    position = _open_position()
    first = estimate_open_position_mark(
        position=position, mark_price=Decimal("55000"), marked_at=NOW
    )
    second = estimate_open_position_mark(
        position=position, mark_price=Decimal("55000"), marked_at=NOW
    )
    assert first == second
    assert position == _open_position()


def _fixture_order(**overrides: object) -> CryptoPaperOrder:
    values: dict[str, object] = {
        "order_id": "fixture-order",
        "account_id": "fixture-mark-account",
        "intent_id": "fixture-intent",
        "preview_id": "fixture-preview",
        "authorization_id": "fixture-authorization",
        "product_id": "BTC-USD",
        "base_asset": "BTC",
        "quote_asset": "USD",
        "side": PaperSide.BUY,
        "order_type": PaperOrderType.MARKET,
        "target_base_quantity": Decimal("0.01"),
        "limit_price": None,
        "filled_base_quantity": Decimal("0.01"),
        "filled_quote_quantity": Decimal("500"),
        "cumulative_fees": Decimal("0.30"),
        "state": PaperOrderState.FILLED,
        "submitted_at": NOW,
        "valid_until": NOW + timedelta(minutes=5),
        "last_sequence": 2,
        "canonical_hash": "2" * 64,
    }
    values.update(overrides)
    return CryptoPaperOrder(**values)  # type: ignore[arg-type]


def _fixture_authorization(**overrides: object) -> CryptoPaperAuthorization:
    values: dict[str, object] = {
        "authorization_id": "fixture-authorization",
        "account_id": "fixture-mark-account",
        "opportunity_set_id": "fixture-set",
        "projection_id": "fixture-projection",
        "source_projection_hash": "3" * 64,
        "eligibility_decision_id": "fixture-decision",
        "eligibility_decision_hash": "4" * 64,
        "strategy_or_discovery_id": "strategy-buy-and-hold",
        "product_id": "BTC-USD",
        "maximum_quote_notional": Decimal("25"),
        "maximum_base_size": Decimal("0.01"),
        "allowed_side": PaperSide.BUY,
        "allowed_order_type": PaperOrderType.MARKET,
        "expires_at": NOW + timedelta(hours=1),
        "actor": "fixture-operator",
        "reason": "fixture",
        "idempotency_key": "fixture-authorization-key",
        "created_at": NOW,
        "canonical_hash": "5" * 64,
    }
    values.update(overrides)
    return CryptoPaperAuthorization(**values)  # type: ignore[arg-type]


def test_open_trial_orders_by_strategy_finds_a_filled_order_with_an_open_position() -> None:
    # Regression: an earlier version filtered by order.state in
    # {OPEN, PARTIALLY_FILLED}, which silently dropped every trial the
    # moment its market order filled -- even though the Position it
    # created was still genuinely OPEN. FILLED is the expected steady
    # state for a successful market order; it must still count.
    result = open_trial_orders_by_strategy(
        positions=(_open_position(),),
        orders=(_fixture_order(state=PaperOrderState.FILLED),),
        authorizations=(_fixture_authorization(),),
    )
    assert result == {"strategy-buy-and-hold": _fixture_order(state=PaperOrderState.FILLED)}


def test_open_trial_orders_by_strategy_finds_a_still_open_order_too() -> None:
    result = open_trial_orders_by_strategy(
        positions=(_open_position(),),
        orders=(_fixture_order(state=PaperOrderState.OPEN, filled_base_quantity=Decimal(0)),),
        authorizations=(_fixture_authorization(),),
    )
    assert "strategy-buy-and-hold" in result


def test_open_trial_orders_by_strategy_ignores_an_order_whose_position_is_closed() -> None:
    result = open_trial_orders_by_strategy(
        positions=(_open_position(state="CLOSED"),),
        orders=(_fixture_order(state=PaperOrderState.FILLED),),
        authorizations=(_fixture_authorization(),),
    )
    assert result == {}


def test_open_trial_orders_by_strategy_ignores_a_dead_order_even_with_a_matching_product() -> None:
    for dead_state in (PaperOrderState.REJECTED, PaperOrderState.CANCELED, PaperOrderState.EXPIRED):
        result = open_trial_orders_by_strategy(
            positions=(_open_position(),),
            orders=(_fixture_order(state=dead_state),),
            authorizations=(_fixture_authorization(),),
        )
        assert result == {}, dead_state


def test_open_trial_orders_by_strategy_ignores_an_order_with_no_resolvable_authorization() -> None:
    result = open_trial_orders_by_strategy(
        positions=(_open_position(),),
        orders=(_fixture_order(authorization_id="unresolvable-authorization"),),
        authorizations=(_fixture_authorization(),),
    )
    assert result == {}


def test_open_trial_orders_by_strategy_is_empty_with_no_open_positions_at_all() -> None:
    result = open_trial_orders_by_strategy(
        positions=(), orders=(_fixture_order(),), authorizations=()
    )
    assert result == {}
