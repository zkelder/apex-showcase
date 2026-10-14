"""Isolated deterministic proof for shared PAPER attribution and health."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.performance_attribution import (
    AttributionSourceSnapshot,
    PaperEvidenceClass,
    PaperVenue,
    assess_strategy_health,
    materialize_attribution,
    summarize_performance,
)
from apex.domain.values import content_digest


def _source(
    identity: str,
    *,
    venue: PaperVenue = PaperVenue.CRYPTO_INTERNAL_PAPER,
    method: str = "FIXTURE_CRYPTO_METHOD_V1",
    expected: str = "0.05",
    realized: str | None = "0.04",
    intended: str = "10",
    filled: str = "10",
    estimated_fees: str = "0.10",
    realized_fees: str = "0.10",
    estimated_slippage: str = "0.10",
    realized_slippage: str = "0.10",
    reconciliation: str = "RECONCILED",
    issues: tuple[str, ...] = (),
    settled: bool = True,
) -> AttributionSourceSnapshot:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=int(identity.rsplit("-", 1)[-1]))
    source = AttributionSourceSnapshot(
        source_id=identity,
        source_hash="0" * 64,
        venue=venue,
        evidence_class=PaperEvidenceClass.FIXTURE_PAPER,
        strategy_or_method_id=method,
        strategy_or_method_version="1",
        instrument_id="BTC-USD" if venue is PaperVenue.CRYPTO_INTERNAL_PAPER else "KX-FIXTURE",
        side="BUY" if venue is PaperVenue.CRYPTO_INTERNAL_PAPER else "YES",
        source_opportunity_id=f"{identity}-opportunity",
        shared_projection_id=f"{identity}-shared",
        allocation_decision_id=f"{identity}-allocation",
        paper_trial_proposal_id=f"{identity}-proposal",
        authorization_id=f"{identity}-authorization",
        order_id=f"{identity}-order",
        reservation_id=f"{identity}-reservation",
        reservation_original_amount=Decimal("100"),
        reservation_current_amount=Decimal(0) if settled else Decimal("60"),
        reservation_consumed_amount=Decimal("100") if settled else Decimal("40"),
        fill_ids=(f"{identity}-fill",) if Decimal(filled) > 0 else (),
        position_id=f"{identity}-position" if Decimal(filled) > 0 else None,
        settlement_id=f"{identity}-settlement" if settled else None,
        resolution_fact_id=f"{identity}-resolution"
        if settled and venue is PaperVenue.KALSHI_INTERNAL_PAPER
        else None,
        context_at_consideration_id=f"{identity}-context",
        context_at_execution_id=f"{identity}-execution-context",
        consideration_context="MIXED",
        execution_context="RISK_CAUTIONARY",
        intended_quantity=Decimal(intended),
        filled_quantity=Decimal(filled),
        intended_notional=Decimal("100"),
        filled_notional=Decimal("100") * Decimal(filled) / Decimal(intended),
        expected_gross_return=Decimal(expected)
        + Decimal(estimated_fees)
        + Decimal(estimated_slippage),
        realized_gross_return=None
        if realized is None
        else Decimal(realized) + Decimal(realized_fees) + Decimal(realized_slippage),
        expected_net_return=Decimal(expected),
        realized_net_return=None if realized is None else Decimal(realized),
        estimated_fees=Decimal(estimated_fees),
        realized_fees=Decimal(realized_fees),
        estimated_slippage=Decimal(estimated_slippage),
        realized_slippage=Decimal(realized_slippage),
        modeled_maximum_loss=Decimal("25"),
        realized_loss=None if realized is None else max(Decimal(0), -Decimal(realized)),
        open_exposure=Decimal(0) if settled else Decimal("40"),
        unrealized_paper_pnl=None if settled else Decimal("0.02"),
        expected_lockup_seconds=86400,
        actual_lockup_seconds=90000,
        considered_at=start,
        first_fill_at=start + timedelta(seconds=30),
        completed_at=start + timedelta(hours=25) if settled else None,
        expires_at=start + timedelta(days=2),
        position_state="SETTLED" if settled else "OPEN",
        reconciliation_status=reconciliation,
        reconciliation_issues=issues,
        opportunity_expiration_behavior="FILLED"
        if Decimal(filled) == Decimal(intended)
        else "PARTIAL_FILL_CANCELED",
        limitations=("ISOLATED_DETERMINISTIC_FIXTURE",),
    )
    return replace(source, source_hash=content_digest(source))


def run_performance_attribution_acceptance() -> dict[str, object]:
    sources = (
        _source("crypto-profit-1"),
        _source("crypto-profit-2", realized="0.03"),
        _source("crypto-loss-3", realized="-0.02"),
        _source("crypto-partial-4", realized=None, filled="4", settled=False),
        _source(
            "crypto-cost-degraded-5",
            realized="-0.04",
            realized_fees="0.25",
            realized_slippage="0.30",
        ),
        _source(
            "kalshi-settlement-6",
            venue=PaperVenue.KALSHI_INTERNAL_PAPER,
            method="FIXTURE_KALSHI_COMPLEMENT_V1",
            realized="0.08",
        ),
        _source(
            "crypto-reconciliation-7",
            method="FIXTURE_RECONCILIATION_METHOD_V1",
            realized=None,
            reconciliation="BLOCKED",
            issues=("JOURNAL_MISMATCH",),
            settled=False,
        ),
    )
    records = tuple(
        materialize_attribution(
            attribution_id=f"acceptance-{source.source_id}",
            source=source,
            materialized_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        for source in sources
    )
    attributions = tuple(item[0] for item in records)
    crypto = tuple(
        item for item in attributions if item.strategy_or_method_id == "FIXTURE_CRYPTO_METHOD_V1"
    )
    transitions = tuple(
        assess_strategy_health(
            health_id=f"fixture-crypto-health-{count}",
            attributions=crypto[:count],
            as_of=datetime(2026, 2, count, tzinfo=UTC),
        ).state.value
        for count in (1, 3, 5)
    )
    blocked = assess_strategy_health(
        health_id="fixture-reconciliation-health",
        attributions=(attributions[-1],),
        as_of=datetime(2026, 2, 8, tzinfo=UTC),
    )
    summary = summarize_performance(
        summary_id="fixture-combined-summary",
        scope_type="PORTFOLIO",
        scope_id="FIXTURE_COMBINED_PAPER_RESEARCH_PORTFOLIO",
        attributions=attributions,
        evidence_class=PaperEvidenceClass.FIXTURE_PAPER,
        as_of=datetime(2026, 2, 8, tzinfo=UTC),
    )
    return {
        "attribution_count": len(attributions),
        "component_categories": tuple(
            sorted(
                {component.category.value for _, components in records for component in components}
            )
        ),
        "profitable_crypto_net_return": attributions[0].realized_net_return,
        "losing_crypto_net_return": attributions[2].realized_net_return,
        "partial_fill_rate": attributions[3].fill_rate,
        "kalshi_settlement_id": attributions[5].settlement_id,
        "reconciliation_health": blocked.state.value,
        "health_transitions": transitions,
        "combined_summary": summary,
        "evidence_class": PaperEvidenceClass.FIXTURE_PAPER.value,
        "persisted_to_genuine_workspace": False,
        "provider_requests": 0,
        "broker_requests": 0,
        "financial_mutations": 0,
        "live_authority": "NONE",
    }
