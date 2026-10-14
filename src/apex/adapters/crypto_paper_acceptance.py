"""Isolated deterministic acceptance for internal Crypto Spot PAPER operations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from apex.application.crypto_paper import (
    ApplyCryptoPaperMarketEvidence,
    AuthorizeCryptoPaperOpportunity,
    CancelCryptoPaperOrder,
    ConstructCryptoPaperOrderIntent,
    CreateCryptoPaperAccount,
    CryptoPaperRepository,
    InMemoryCryptoPaperRepository,
    PreviewCryptoPaperOrder,
    ReconcileCryptoPaperAccount,
    RegisterCryptoPaperProviderContract,
    SubmitCryptoPaperOrder,
    paper_workspace,
)
from apex.domain.crypto_paper import (
    CryptoPaperAuthorization,
    CryptoPaperEligibilityScan,
    CryptoPaperError,
    PaperOrderType,
    PaperPricingSource,
    PaperSide,
    RetainedPaperPriceEvidence,
    build_provider_contract,
    evaluate_paper_eligibility,
    inspect_paper_capability,
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

ACCEPTANCE_TIME = datetime(2026, 7, 30, 12, tzinfo=UTC)
ACCEPTANCE_ACCOUNT_ID = "apex-crypto-paper-fixture-v1"
ACCEPTANCE_AUTHORIZATION_ID = "apex-crypto-paper-fixture-buy-auth-v1"
ACCEPTANCE_ORDER_ID = "apex-crypto-paper-fixture-buy-order-v1"
ACCEPTANCE_FILL_ID = "apex-crypto-paper-fixture-buy-fill-v1"
ACCEPTANCE_CANCEL_ORDER_ID = "apex-crypto-paper-fixture-cancel-order-v1"


def _product() -> PublicCryptoProduct:
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
        captured_at=ACCEPTANCE_TIME - timedelta(hours=1),
        source_contract_version="COINBASE_PUBLIC_PRODUCTS_CURSOR_V1",
        raw_evidence_hash="a" * 64,
        parser_version="CRYPTO_PRODUCT_PARSER_V1",
        evidence_class=EvidenceClass.FIXTURE,
    )


def _universe() -> CryptoUniverseSnapshot:
    return CryptoUniverseSnapshot(
        snapshot_id="apex-crypto-paper-fixture-universe-v1",
        source_capture_id="apex-crypto-paper-fixture-capture-v1",
        policy_id="CRYPTO_UNIVERSE_SELECTION_QUALITY_POLICY_V1",
        policy_version=1,
        considered_product_ids=("BTC-USD",),
        decisions=(),
        eligible_product_ids=("BTC-USD",),
        excluded_product_ids=(),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_limitations=("ISOLATED_CRYPTO_PAPER_ACCEPTANCE",),
        manifest_hash="b" * 64,
        created_at=ACCEPTANCE_TIME - timedelta(hours=1),
        canonical_hash="c" * 64,
        catalog_pagination_status=CatalogPaginationStatus.COMPLETE,
        eligible_asset_ids=("BTC",),
        selected_routes=(("BTC", "BTC-USD"),),
    )


def _projection() -> SharedOpportunityProjection:
    projection = SharedOpportunityProjection.create(
        projection_id="apex-crypto-paper-fixture-projection-v1",
        venue=SharedVenue.CRYPTO,
        source_record_type="CRYPTO_PULSE_OPPORTUNITY",
        source_record_id="apex-crypto-paper-fixture-pulse-opportunity-v1",
        source_canonical_hash="d" * 64,
        strategy_or_method_id="CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
        instrument_id="BTC",
        product_or_market_id="BTC-USD",
        direction="LONG",
        generated_at=ACCEPTANCE_TIME - timedelta(minutes=1),
        valid_until=ACCEPTANCE_TIME + timedelta(days=2),
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
        liquidity_limitations=("FIXTURE_VISIBLE_QUANTITY_ONLY",),
        required_hypothetical_capital="25",
        maximum_sensible_research_allocation="25",
        source_maximum_allocation_fraction="0.025",
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=("apex-crypto-paper-fixture-open-before",),
        source_warnings=("ISOLATED_FIXTURE_NOT_GENUINE_MARKET_EVIDENCE",),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("ISOLATED_CRYPTO_PAPER_ACCEPTANCE",),
        source_details=(("pulse_id", "apex-crypto-paper-fixture-pulse-v1"),),
    )
    return replace(projection, paper_eligible=True)


def _shared_decision() -> EligibilityDecision:
    return EligibilityDecision(
        decision_id="apex-crypto-paper-fixture-shared-eligibility-v1",
        projection_id="apex-crypto-paper-fixture-projection-v1",
        policy_id="SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1",
        evaluated_at=ACCEPTANCE_TIME,
        state=EligibilityState.ELIGIBLE,
        reasons=("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY",),
        canonical_hash="e" * 64,
    )


def run_crypto_paper_fixture_acceptance(
    repository: CryptoPaperRepository | None = None,
) -> dict[str, object]:
    selected_repository = repository or InMemoryCryptoPaperRepository()
    repository = selected_repository
    contract = build_provider_contract(
        contract_id="apex-crypto-paper-fixture-btc-usd-contract-v1",
        product=_product(),
        source_capture_id="apex-crypto-paper-fixture-capture-v1",
    )
    RegisterCryptoPaperProviderContract(repository).execute(
        contract=contract,
        idempotency_key="fixture-contract-key",
    )
    account_result = CreateCryptoPaperAccount(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        opening_cash="1000",
        created_at=ACCEPTANCE_TIME,
        idempotency_key="fixture-account-key",
    )
    projection = _projection()
    eligibility = evaluate_paper_eligibility(
        decision_id="apex-crypto-paper-fixture-eligibility-v1",
        opportunity_set_id="apex-crypto-paper-fixture-set-v1",
        projection=projection,
        shared_decision=_shared_decision(),
        universe=_universe(),
        contract=contract,
        account=account_result.value.account,
        account_state=account_result.value.account_state,
        operating_cycle_enabled=True,
        evaluated_at=ACCEPTANCE_TIME,
    )
    scan = CryptoPaperEligibilityScan.create(
        scan_id="apex-crypto-paper-fixture-scan-v1",
        opportunity_set_id=eligibility.opportunity_set_id,
        decisions=(eligibility,),
        evaluated_at=ACCEPTANCE_TIME,
    )
    repository.save_scan(
        scan=scan,
        decisions=(eligibility,),
        idempotency_key="fixture-scan-key",
    )
    authorization = CryptoPaperAuthorization.create(
        authorization_id=ACCEPTANCE_AUTHORIZATION_ID,
        account_id=ACCEPTANCE_ACCOUNT_ID,
        eligibility=eligibility,
        projection=projection,
        maximum_quote_notional="25",
        maximum_base_size="0.001",
        allowed_side=PaperSide.BUY,
        allowed_order_type=PaperOrderType.MARKET,
        expires_at=ACCEPTANCE_TIME + timedelta(hours=2),
        actor="fixture-operator",
        reason="isolated deterministic Crypto Spot PAPER acceptance",
        idempotency_key="fixture-buy-authorization-key",
        created_at=ACCEPTANCE_TIME,
    )
    AuthorizeCryptoPaperOpportunity(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        authorization=authorization,
    )
    intent = (
        ConstructCryptoPaperOrderIntent(repository)
        .execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            intent_id="apex-crypto-paper-fixture-buy-intent-v1",
            authorization_id=authorization.authorization_id,
            contract_id=contract.contract_id,
            side=PaperSide.BUY,
            order_type=PaperOrderType.MARKET,
            quote_size="25",
            base_size=None,
            limit_price=None,
            created_at=ACCEPTANCE_TIME + timedelta(seconds=1),
            idempotency_key="fixture-buy-intent-key",
        )
        .value
    )
    before = RetainedPaperPriceEvidence.create(
        evidence_id="apex-crypto-paper-fixture-open-before",
        product_id="BTC-USD",
        observed_at=ACCEPTANCE_TIME - timedelta(minutes=1),
        ingested_at=ACCEPTANCE_TIME,
        price="50000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    preview = (
        PreviewCryptoPaperOrder(repository)
        .execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            preview_id="apex-crypto-paper-fixture-buy-preview-v1",
            intent_id=intent.intent_id,
            evidence=before,
            fee_bps="60",
            spread_bps="10",
            slippage_bps="10",
            created_at=ACCEPTANCE_TIME + timedelta(seconds=2),
            idempotency_key="fixture-buy-preview-key",
        )
        .value
    )
    submission_service = SubmitCryptoPaperOrder(repository)
    submission = submission_service.execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=ACCEPTANCE_ORDER_ID,
        reservation_id="apex-crypto-paper-fixture-buy-reservation-v1",
        preview_id=preview.preview_id,
        submitted_at=ACCEPTANCE_TIME + timedelta(seconds=3),
        valid_until=ACCEPTANCE_TIME + timedelta(days=1, minutes=1),
        actor="fixture-operator",
        idempotency_key="fixture-buy-submit-key",
    ).value
    after = RetainedPaperPriceEvidence.create(
        evidence_id="apex-crypto-paper-fixture-open-after",
        product_id="BTC-USD",
        observed_at=ACCEPTANCE_TIME + timedelta(days=1),
        ingested_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=1),
        price="50000",
        available_base_quantity=None,
        source=PaperPricingSource.FIXTURE_RETAINED_OPEN,
        evidence_class="FIXTURE",
    )
    fill_service = ApplyCryptoPaperMarketEvidence(repository)
    fill = fill_service.execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=ACCEPTANCE_ORDER_ID,
        fill_id=ACCEPTANCE_FILL_ID,
        evidence=after,
        fee_bps="60",
        slippage_bps="10",
        actor="fixture-simulator",
        idempotency_key="fixture-buy-fill-key",
    ).value
    if fill is None:
        raise RuntimeError("fixture market order did not fill")

    cancel_authorization = CryptoPaperAuthorization.create(
        authorization_id="apex-crypto-paper-fixture-cancel-auth-v1",
        account_id=ACCEPTANCE_ACCOUNT_ID,
        eligibility=eligibility,
        projection=projection,
        maximum_quote_notional="20",
        maximum_base_size="0.0004",
        allowed_side=PaperSide.BUY,
        allowed_order_type=PaperOrderType.LIMIT,
        expires_at=ACCEPTANCE_TIME + timedelta(days=2),
        actor="fixture-operator",
        reason="exercise exact-ID PAPER cancellation and reservation release",
        idempotency_key="fixture-cancel-authorization-key",
        created_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=2),
    )
    AuthorizeCryptoPaperOpportunity(repository).execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        authorization=cancel_authorization,
    )
    cancel_intent = (
        ConstructCryptoPaperOrderIntent(repository)
        .execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            intent_id="apex-crypto-paper-fixture-cancel-intent-v1",
            authorization_id=cancel_authorization.authorization_id,
            contract_id=contract.contract_id,
            side=PaperSide.BUY,
            order_type=PaperOrderType.LIMIT,
            quote_size=None,
            base_size="0.0004",
            limit_price="48000",
            created_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=3),
            idempotency_key="fixture-cancel-intent-key",
        )
        .value
    )
    cancel_preview = (
        PreviewCryptoPaperOrder(repository)
        .execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            preview_id="apex-crypto-paper-fixture-cancel-preview-v1",
            intent_id=cancel_intent.intent_id,
            evidence=after,
            fee_bps="60",
            spread_bps="10",
            slippage_bps="10",
            created_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=4),
            idempotency_key="fixture-cancel-preview-key",
        )
        .value
    )
    cancel_submission = submission_service.execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=ACCEPTANCE_CANCEL_ORDER_ID,
        reservation_id="apex-crypto-paper-fixture-cancel-reservation-v1",
        preview_id=cancel_preview.preview_id,
        submitted_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=5),
        valid_until=ACCEPTANCE_TIME + timedelta(days=2),
        actor="fixture-operator",
        idempotency_key="fixture-cancel-submit-key",
    ).value
    canceled = (
        CancelCryptoPaperOrder(repository)
        .execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            order_id=ACCEPTANCE_CANCEL_ORDER_ID,
            canceled_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=6),
            actor="fixture-operator",
            reason="bounded acceptance cancellation",
            idempotency_key="fixture-cancel-key",
        )
        .value
    )
    reconciliation = (
        ReconcileCryptoPaperAccount(repository)
        .execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            reconciliation_id="apex-crypto-paper-fixture-reconciliation-v1",
            evaluated_at=ACCEPTANCE_TIME + timedelta(days=1, seconds=7),
            order_id=None,
            idempotency_key="fixture-reconciliation-key",
        )
        .value
    )
    submit_replay = submission_service.execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=ACCEPTANCE_ORDER_ID,
        reservation_id=submission.reservation.reservation_id,
        preview_id=preview.preview_id,
        submitted_at=ACCEPTANCE_TIME + timedelta(seconds=3),
        valid_until=ACCEPTANCE_TIME + timedelta(days=1, minutes=1),
        actor="fixture-operator",
        idempotency_key="fixture-buy-submit-key",
    )
    fill_replay = fill_service.execute(
        account_id=ACCEPTANCE_ACCOUNT_ID,
        order_id=ACCEPTANCE_ORDER_ID,
        fill_id=ACCEPTANCE_FILL_ID,
        evidence=after,
        fee_bps="60",
        slippage_bps="10",
        actor="fixture-simulator",
        idempotency_key="fixture-buy-fill-key",
    )
    duplicate_fill_rejected = False
    try:
        fill_service.execute(
            account_id=ACCEPTANCE_ACCOUNT_ID,
            order_id=ACCEPTANCE_ORDER_ID,
            fill_id=ACCEPTANCE_FILL_ID,
            evidence=after,
            fee_bps="60",
            slippage_bps="10",
            actor="fixture-simulator",
            idempotency_key="fixture-duplicate-fill-key",
        )
    except CryptoPaperError as error:
        duplicate_fill_rejected = str(error) == "CRYPTO_PAPER_DUPLICATE_FILL"
    final_state = repository.state(ACCEPTANCE_ACCOUNT_ID)
    if final_state is None:
        raise RuntimeError("fixture PAPER account state was not retained")
    return {
        "acceptance": "ISOLATED_DETERMINISTIC_CRYPTO_SPOT_PAPER_V1",
        "provider_contract": contract,
        "capability": inspect_paper_capability(
            contract=contract,
            inspected_at=ACCEPTANCE_TIME,
        ),
        "eligibility_scan": scan,
        "authorization": authorization,
        "provider_request_projection": intent.provider_request,
        "preview": preview,
        "submission": submission,
        "fill": fill,
        "cancel_submission": cancel_submission,
        "cancellation": canceled,
        "reconciliation": reconciliation,
        "opening_cash": account_result.value.account.opening_cash,
        "final_cash": final_state.account_state.cash,
        "final_balances": final_state.account_state.balances,
        "positions": final_state.positions,
        "modeled_fees": final_state.account_state.modeled_fees,
        "realized_paper_pnl": final_state.account_state.realized_paper_pnl,
        "workspace": paper_workspace(final_state),
        "submit_replay": submit_replay.idempotent_replay,
        "fill_replay": fill_replay.idempotent_replay,
        "duplicate_fill_rejected": duplicate_fill_rejected,
        "final_state_hash": final_state.canonical_hash,
        "provider_requests": 0,
        "genuine_financial_mutation": False,
        "paper_authority": "PAPER_ONLY",
        "live_authority": "NONE",
    }
