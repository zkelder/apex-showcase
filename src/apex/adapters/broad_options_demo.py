"""Deterministic production-service demos for broad equity and options research."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from apex.adapters.options_reference import (
    alpaca_options_capability,
    occ_public_reference_capability,
    parse_alpaca_option_contracts,
    parse_alpaca_option_snapshot,
)
from apex.adapters.security_reference import (
    NASDAQ_LISTED_PATH,
    parse_alpaca_assets,
    parse_nasdaq_symbol_directory,
    parse_sec_company_tickers,
)
from apex.application.broad_equity import (
    CreateBroadEquityShadowPlan,
    InMemoryBroadEquityRepository,
    RunBroadEquityDiscovery,
)
from apex.application.options_intelligence import (
    CreateDefinedRiskOptionsShadowProposal,
    InMemoryOptionsIntelligenceRepository,
)
from apex.application.security_reference import (
    CreateCurrentEquityUniverse,
    InMemorySecurityReferenceRepository,
    SecurityReferenceApplicationError,
)
from apex.domain.broad_equity import (
    BroadDataPolicy,
    assess_security_data_quality,
    calculate_broad_equity_features,
)
from apex.domain.options_intelligence import (
    OptionsStrategyType,
    compare_stock_and_option_expression,
    create_option_chain_snapshot,
    evaluate_defined_risk_options,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)
from apex.domain.security_reference import (
    CurrentEquityUniversePolicy,
    reconcile_security_references,
)

DEMO_NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)


def run_broad_equity_universe_demo() -> dict[str, object]:
    nasdaq_payload = (
        b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|"
        b"NextShares\n"
        b"ACME|Acme Corporation Common Stock|Q|N|N|100|N|N\n"
        b"REIT|Example REIT Common Shares|Q|N|N|100|N|N\n"
        b"FUND|Example Index ETF|G|N|N|100|Y|N\n"
        b"TEST|Test Security Common Stock|Q|Y|N|100|N|N\n"
        b"WARN|Warning Corp Common Stock|Q|N|D|100|N|N\n"
        b"File Creation Time: 0729202612:00|||||||\n"
    )
    nasdaq = parse_nasdaq_symbol_directory(
        payload=nasdaq_payload,
        source_path=NASDAQ_LISTED_PATH,
        received_at=DEMO_NOW,
    )
    sec_payload = json.dumps(
        {
            "0": {"cik_str": 1001, "ticker": "ACME", "title": "Acme Corporation"},
            "1": {"cik_str": 1002, "ticker": "REIT", "title": "Example REIT"},
        },
        separators=(",", ":"),
    ).encode()
    sec = parse_sec_company_tickers(
        payload=sec_payload,
        received_at=DEMO_NOW,
        source_timestamp=DEMO_NOW,
    )
    alpaca_payload = json.dumps(
        [
            {
                "symbol": symbol,
                "name": name,
                "class": "us_equity",
                "exchange": "NASDAQ",
                "status": "active",
                "tradable": True,
                "shortable": True,
                "marginable": True,
            }
            for symbol, name in (
                ("ACME", "Acme Corporation"),
                ("REIT", "Example REIT"),
                ("FUND", "Example Index ETF"),
                ("TEST", "Test Security"),
                ("WARN", "Warning Corp"),
            )
        ],
        separators=(",", ":"),
    ).encode()
    alpaca = parse_alpaca_assets(
        payload=alpaca_payload,
        received_at=DEMO_NOW,
        source_timestamp=DEMO_NOW,
    )
    reconciled = reconcile_security_references((*nasdaq.records, *sec.records, *alpaca.records))
    service = CreateCurrentEquityUniverse(InMemorySecurityReferenceRepository())
    policy = CurrentEquityUniversePolicy.initial()
    receipt_ids = (
        nasdaq.receipt.receipt_id,
        sec.receipt.receipt_id,
        alpaca.receipt.receipt_id,
    )
    snapshot = service.execute(
        securities=reconciled,
        policy=policy,
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=DEMO_NOW,
        idempotency_key="broad-universe-demo-v1",
    )
    replay = service.execute(
        securities=reconciled,
        policy=policy,
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=DEMO_NOW,
        idempotency_key="broad-universe-demo-v1",
    )
    conflict_proven = False
    try:
        service.execute(
            securities=reconciled,
            policy=policy,
            source_receipt_ids=receipt_ids,
            as_of_date=date(2026, 7, 29),
            created_at=DEMO_NOW + timedelta(seconds=1),
            idempotency_key="broad-universe-demo-v1",
        )
    except SecurityReferenceApplicationError:
        conflict_proven = True
    return {
        "contracts": (
            "APEX_SECURITY_REFERENCE_V1",
            "APEX_SECURITY_REFERENCE_RECONCILIATION_V1",
            "APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1",
        ),
        "source_providers": ("NASDAQ_TRADER", "SEC_EDGAR", "ALPACA"),
        "source_receipts": receipt_ids,
        "security_master_count": len(reconciled),
        "provider_disagreements": tuple(
            value for item in reconciled for value in item.disagreements
        ),
        "total_considered": snapshot.total_considered,
        "eligible_count": snapshot.research_eligible_count,
        "eligible_symbols": tuple(
            item.current_symbol for item in snapshot.decisions if item.research_eligible
        ),
        "exclusion_counts": dict(snapshot.research_exclusion_counts),
        "execution_capability_counts": dict(snapshot.execution_capability_counts),
        "execution_capability_known_count": snapshot.execution_capability_known_count,
        "execution_capability_unknown_count": snapshot.execution_capability_unknown_count,
        "warning": snapshot.warning,
        "replay_same_hash": replay.canonical_hash == snapshot.canonical_hash,
        "changed_same_key_conflict": conflict_proven,
        "broker_writes": 0,
        "financial_mutations": 0,
        "authority": "RESEARCH_ONLY",
    }


def _bars(instrument_id: str, *, drift_tenths: int) -> tuple[SecuritiesBarEvidence, ...]:
    values: list[SecuritiesBarEvidence] = []
    for index in range(130):
        start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        close = Decimal("50") + Decimal(index * drift_tenths) / Decimal(10)
        values.append(
            SecuritiesBarEvidence(
                instrument_id=instrument_id,
                interval=SecuritiesBarInterval.ONE_DAY,
                adjustment_state=SecuritiesAdjustmentState.SPLIT_ADJUSTED,
                period_start=start,
                period_end=start + timedelta(hours=6),
                available_at=start + timedelta(hours=7),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume="1000000",
                source_identity="alpaca-provider-shaped-fixture",
            )
        )
    return tuple(values)


def run_broad_equity_discovery_demo() -> dict[str, object]:
    sessions = tuple(
        (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).date().isoformat()
        for index in range(130)
    )
    policy = BroadDataPolicy.create(
        minimum_completed_sessions=120,
        preferred_sessions=130,
        minimum_median_dollar_volume="1000000",
    )
    qualities = []
    features = []
    for security_id, drift in (
        ("security-acme", 2),
        ("security-reit", 1),
        ("security-beta", -1),
    ):
        bars = _bars(security_id, drift_tenths=drift)
        quality = assess_security_data_quality(
            canonical_security_id=security_id,
            bars=bars,
            expected_session_dates=sessions,
            policy=policy,
        )
        qualities.append(quality)
        features.append(
            calculate_broad_equity_features(
                canonical_security_id=security_id,
                bars=bars,
                quality=quality,
            )
        )
    repository = InMemoryBroadEquityRepository()
    run = RunBroadEquityDiscovery(repository).execute(
        universe_id="broad-equity-universe-demo",
        data_capture_id="broad-equity-capture-demo",
        features=tuple(features),
        qualities=tuple(qualities),
        idempotency_key="broad-discovery-demo-v1",
    )
    shadow = CreateBroadEquityShadowPlan(repository).execute(
        discovery_run=run,
        policy_hashes=(policy.canonical_hash,),
        top_n=3,
        decision_at=DEMO_NOW,
        forward_horizon_sessions=20,
        data_cutoff=DEMO_NOW - timedelta(minutes=1),
        idempotency_key="broad-shadow-demo-v1",
    )
    return {
        "run_id": run.run_id,
        "regime": run.regime.regime,
        "ranked_candidates": tuple(
            {
                "rank": item.rank,
                "security_id": item.canonical_security_id,
                "score": item.score,
                "uncertainty": item.uncertainty,
            }
            for item in run.opportunities
        ),
        "data_quality": tuple(
            (item.canonical_security_id, item.readiness, item.provider_coverage)
            for item in qualities
        ),
        "hypothetical_allocations": run.portfolio.allocations,
        "cash_weight": run.portfolio.cash_weight,
        "authority_permitted_weights": tuple(
            item.authority_permitted_weight for item in run.portfolio.allocations
        ),
        "shadow_plan_id": shadow.plan_id,
        "shadow_state": shadow.state,
        "authority": shadow.authority,
        "warnings": run.warnings,
        "orders": 0,
        "positions": 0,
        "reservations": 0,
        "journals": 0,
    }


def run_options_intelligence_demo() -> dict[str, object]:
    expiration = "2026-09-18"
    payload = json.dumps(
        {
            "option_contracts": [
                {
                    "id": f"id-{kind}-{strike}",
                    "symbol": f"ACME{kind}{strike}",
                    "underlying_symbol": "ACME",
                    "type": kind,
                    "strike_price": str(strike),
                    "expiration_date": expiration,
                    "size": "100",
                    "status": "active",
                }
                for kind in ("call", "put")
                for strike in (95, 100, 105)
            ]
        },
        separators=(",", ":"),
    ).encode()
    reference = parse_alpaca_option_contracts(
        payload=payload,
        received_at=DEMO_NOW,
        source_timestamp=DEMO_NOW,
        underlying_ids={"ACME": "security-acme"},
    )
    evidence = []
    for contract in reference.contracts:
        distance = abs(contract.strike - 100)
        evidence.append(
            parse_alpaca_option_snapshot(
                contract=contract,
                payload={
                    "latestQuote": {
                        "bp": str(Decimal("4.8") - distance / 2),
                        "ap": str(Decimal("5") - distance / 2),
                    },
                    "latestTrade": {"p": "4.9"},
                    "dailyVolume": "500",
                    "openInterest": "1000",
                    "impliedVolatility": "0.25",
                    "greeks": {
                        "delta": ("0.5" if contract.option_type.value == "CALL" else "-0.5"),
                        "gamma": "0.03",
                        "theta": "-0.04",
                        "vega": "0.12",
                    },
                },
                quote_timestamp=DEMO_NOW,
                staleness_seconds=5,
                source_receipt_id=reference.receipt.receipt_id,
            )
        )
    chain = create_option_chain_snapshot(
        underlying_canonical_security_id="security-acme",
        captured_at=DEMO_NOW,
        underlying_price="100",
        expiration_start=date(2026, 8, 1),
        expiration_end=date(2026, 10, 1),
        maximum_strike_distance="0.1",
        maximum_contract_count=20,
        market_evidence=tuple(evidence),
        provider="ALPACA_OPTIONS",
        source_receipt_ids=(reference.receipt.receipt_id,),
    )
    candidates = evaluate_defined_risk_options(
        chain=chain,
        as_of_date=date(2026, 7, 29),
    )
    comparisons = tuple(
        compare_stock_and_option_expression(
            underlying_canonical_security_id="security-acme",
            stock_price="100",
            expected_holding_sessions=20,
            candidate=candidate,
        )
        for candidate in candidates[:4]
    )
    proposal = CreateDefinedRiskOptionsShadowProposal(
        InMemoryOptionsIntelligenceRepository()
    ).execute(
        discovery_run_id="broad-discovery-demo-v1",
        chains=(chain,),
        candidates=candidates[:4],
        calculation_policy_ids=("payoff-v1", "liquidity-v1"),
        idempotency_key="options-demo-v1",
    )
    return {
        "underlying_lineage": chain.underlying_canonical_security_id,
        "chain_id": chain.chain_id,
        "contract_count": len(chain.contracts),
        "bounded_contract_maximum": 20,
        "strategies": tuple(sorted({item.strategy_type for item in candidates})),
        "defined_risk_candidates": tuple(
            {
                "rank": item.rank,
                "strategy": item.strategy_type,
                "legs": item.legs,
                "premium_or_debit": item.premium_or_debit,
                "maximum_loss": item.maximum_loss,
                "maximum_gain": item.maximum_gain,
                "break_even": item.break_even,
                "blockers": item.blockers,
            }
            for item in candidates
        ),
        "stock_option_comparisons": comparisons,
        "alpaca_capability": alpaca_options_capability(),
        "occ_capability": occ_public_reference_capability(checked_at=DEMO_NOW),
        "shadow_proposal_id": proposal.proposal_id,
        "authority": proposal.authority,
        "warnings": proposal.warnings,
        "option_orders": 0,
        "paper_authority": 0,
        "live_authority": 0,
        "strategy_families_proven": tuple(strategy.value for strategy in OptionsStrategyType),
    }
