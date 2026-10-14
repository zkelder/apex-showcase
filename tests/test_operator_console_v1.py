"""Focused contracts for the consolidated, read-only Operator Console."""

from __future__ import annotations

from pathlib import Path

import pytest

from apex.application.operator_console_v1 import (
    OperatorConsoleFilterError,
    build_operator_allocation,
    build_operator_attention,
    build_operator_broad_market,
    build_operator_home,
    build_operator_learning,
    build_operator_live_readiness,
    build_operator_opportunities,
    build_operator_paper_operations,
    build_operator_paper_strategy_registry,
    build_operator_research_operations,
)
from apex.operator_api import app


def _shared() -> dict[str, object]:
    def row(identity: str, venue: str, state: str, rank: int | None) -> dict[str, object]:
        return {
            "projection": {
                "projection_id": identity,
                "venue": venue,
                "instrument_id": f"instrument-{identity}",
                "product_or_market_id": f"product-{identity}",
                "strategy_or_method_id": "method-v1",
                "expected_net_return": None if venue == "CRYPTO" else "0.02",
                "maximum_loss": "25",
                "required_hypothetical_capital": "25.5",
                "freshness_seconds": 15,
                "freshness_limit_seconds": 300,
                "source_rejection_reasons": ("SOURCE_BLOCKER",) if venue == "CRYPTO" else (),
                "source_warnings": ("VISIBLE_LIQUIDITY_IS_NOT_FILLABILITY",),
                "supporting_evidence_ids": (f"evidence-{identity}",),
                "source_details": (
                    ("best_bid", "0.48"),
                    ("best_ask", "0.50"),
                    ("midpoint", "0.49"),
                    ("spread_bps", "408.16326531"),
                    ("visible_ask_quote_depth", "100"),
                    ("return_7d", "-0.05"),
                    ("return_90d", "0.20"),
                    ("downside_probability", "0.25"),
                    ("guaranteed_settlement_payout", "5"),
                    ("conservative_net_gap", "1.10"),
                    ("yes_leg_1", "market=KXONE|ask=0.25"),
                ),
            },
            "eligibility": {"state": state, "reasons": (f"{state}_REASON",)},
            "priority": None if rank is None else {"rank": rank, "score": "0.5"},
            "explanation": {"text": f"retained {identity}"},
        }

    return {
        "opportunity_set": {"opportunity_set_id": "set-1", "manifest_hash": "a" * 64},
        "ranked_opportunities": (row("kalshi-1", "KALSHI", "ELIGIBLE", 1),),
        "rejected_opportunities": (row("crypto-1", "CRYPTO", "REJECTED", None),),
    }


def test_operator_inbox_filters_without_reordering_or_inventing_missing_values() -> None:
    cross_venue = {
        "decision": {
            "context_state": "MARKET_STRESS",
            "considered_projection_ids": ("kalshi-1", "crypto-1"),
            "excluded_projection_ids": ("crypto-1",),
        },
        "lines": ({"projection_id": "kalshi-1", "amount": "125"},),
        "paper_trial_proposals": (
            {"proposal_id": "proposal-1", "source_projection_id": "kalshi-1"},
        ),
    }
    value = build_operator_opportunities(
        _shared(),
        cross_venue=cross_venue,
        venue="kalshi",
        eligibility="eligible",
        allocation="allocated",
        paper_readiness="ready",
    )

    assert value["total"] == 1
    item = value["items"][0]  # type: ignore[index]
    assert item["projection_id"] == "kalshi-1"  # type: ignore[index]
    assert item["priority_rank"] == 1  # type: ignore[index]
    assert item["hypothetical_allocation"] == "125"  # type: ignore[index]
    assert item["paper_readiness"] == "READY"  # type: ignore[index]
    assert item["total_market_context"] == "MARKET_STRESS"  # type: ignore[index]
    assert item["live_eligible"] is False  # type: ignore[index]
    assert item["live_authority"] == "NONE"  # type: ignore[index]
    assert item["best_bid"] == "0.48"  # type: ignore[index]
    assert item["best_ask"] == "0.50"  # type: ignore[index]
    assert item["required_hypothetical_capital"] == "25.5"  # type: ignore[index]
    assert item["return_7d"] == "-0.05"  # type: ignore[index]
    assert item["return_90d"] == "0.20"  # type: ignore[index]
    assert item["downside_probability"] == "0.25"  # type: ignore[index]
    assert item["guaranteed_settlement_payout"] == "5"  # type: ignore[index]
    assert item["conservative_net_gap"] == "1.10"  # type: ignore[index]
    assert item["package_legs"] == (("yes_leg_1", "market=KXONE|ask=0.25"),)  # type: ignore[index]
    assert item["evidence_lineage"] == ("evidence-kalshi-1",)  # type: ignore[index]
    assert item["evidence_href"] == "/api/v1/shared-opportunities/projections/kalshi-1"  # type: ignore[index]

    crypto = build_operator_opportunities(_shared(), query="instrument-crypto")
    crypto_item = crypto["items"][0]  # type: ignore[index]
    assert crypto_item["expected_net_return"] is None  # type: ignore[index]


def test_operator_inbox_rejects_unbounded_or_unknown_filters() -> None:
    with pytest.raises(OperatorConsoleFilterError, match="VENUE_FILTER_INVALID"):
        build_operator_opportunities(_shared(), venue="ALPACA")
    with pytest.raises(OperatorConsoleFilterError, match="PAGINATION_INVALID"):
        build_operator_opportunities(_shared(), limit=101)
    with pytest.raises(OperatorConsoleFilterError, match="QUERY_TOO_LONG"):
        build_operator_opportunities(_shared(), query="x" * 161)
    with pytest.raises(OperatorConsoleFilterError, match="ALLOCATION_FILTER_INVALID"):
        build_operator_opportunities(_shared(), allocation="OPTIMIZED")
    with pytest.raises(OperatorConsoleFilterError, match="PAPER_READINESS_FILTER_INVALID"):
        build_operator_opportunities(_shared(), paper_readiness="AUTO_TRADE")


def test_operator_inbox_accepts_securities_venue_filter_and_propagates_priority_tier() -> None:
    """SECURITIES Operator Console Backend Read-Model Closure v1: venue=SECURITIES
    must return real bounded rows rather than 400 (acceptance A), and the
    row's own retained PriorityResult.tier must survive to the Opportunity
    row exactly as retained (acceptance C). Tier is orthogonal to
    eligibility, PAPER readiness, and execution authority -- an ELIGIBLE,
    RESEARCH_ONLY, NOT_READY, NONE-authority row proves none of the four is
    inferred from another (acceptance D)."""

    shared: dict[str, object] = {
        "opportunity_set": {"opportunity_set_id": "set-2", "manifest_hash": "b" * 64},
        "ranked_opportunities": (
            {
                "projection": {
                    "projection_id": "sec-1",
                    "venue": "SECURITIES",
                    "instrument_id": "AAPL 2026-09-19 200/210 CALL SPREAD",
                    "product_or_market_id": "AAPL",
                    "strategy_or_method_id": "options-forward-v1",
                    "authority": "NONE",
                },
                "eligibility": {"state": "ELIGIBLE", "reasons": ()},
                "priority": {
                    "rank": 1,
                    "score": "0.31",
                    "components": (),
                    "tier": "RESEARCH_ONLY",
                    "tier_reasons": ("BELOW_OPPORTUNITY_COST_FLOOR",),
                },
                "explanation": {"text": "retained sec-1"},
            },
        ),
        "rejected_opportunities": (),
    }

    value = build_operator_opportunities(shared, venue="SECURITIES")
    assert value["total"] == 1
    item = value["items"][0]  # type: ignore[index]
    assert item["venue"] == "SECURITIES"  # type: ignore[index]
    assert item["eligibility"] == "ELIGIBLE"  # type: ignore[index]
    assert item["priority_tier"] == "RESEARCH_ONLY"  # type: ignore[index]
    assert item["priority_tier_reasons"] == ("BELOW_OPPORTUNITY_COST_FLOOR",)  # type: ignore[index]
    # No PAPER trial proposal supplied for sec-1 -- honestly NOT_READY, never
    # inferred from tier or eligibility.
    assert item["paper_readiness"] == "NOT_READY"  # type: ignore[index]
    assert item["authority"] == "NONE"  # type: ignore[index]
    assert item["live_authority"] == "NONE"  # type: ignore[index]

    # CRYPTO/KALSHI filtering is exactly unchanged by widening the allow-list
    # (acceptance B). kalshi-1/crypto-1 (from the shared `_shared()` fixture)
    # each carry no `tier` key in their stored priority payload at all --
    # proving a legacy retained row (or one with no PriorityResult, like
    # crypto-1's rank=None) reports tier honestly as `None`, never a
    # fabricated RESEARCH_ONLY default invented at this read-model layer.
    kalshi_only = build_operator_opportunities(_shared(), venue="KALSHI")
    assert kalshi_only["total"] == 1
    assert kalshi_only["items"][0]["venue"] == "KALSHI"  # type: ignore[index]
    assert kalshi_only["items"][0]["priority_tier"] is None  # type: ignore[index]

    crypto_only = build_operator_opportunities(_shared(), venue="CRYPTO")
    assert crypto_only["total"] == 1
    assert crypto_only["items"][0]["venue"] == "CRYPTO"  # type: ignore[index]
    assert crypto_only["items"][0]["priority_tier"] is None  # type: ignore[index]

    # Unknown/invalid venue values remain fail-closed (acceptance E): ALPACA
    # is a real PortfolioVenue member elsewhere in Apex but never a
    # SharedVenue member, so it must still 400, exactly as before this fix.
    with pytest.raises(OperatorConsoleFilterError, match="VENUE_FILTER_INVALID"):
        build_operator_opportunities(shared, venue="ALPACA")


def test_home_summary_uses_retained_operating_brief_values_exactly() -> None:
    operating = {
        "operating_brief": {
            "brief_id": "brief-1",
            "cycle_id": "cycle-1",
            "overall_system_state": "DEGRADED",
            "opportunity_counts": (("ELIGIBLE", 0), ("BLOCKED", 3), ("REJECTED", 2)),
            "unallocated_cash": "1000",
            "warnings": ("retained-warning",),
        },
        "latest_cycle_state": "COMPLETED",
        "operating_health": {"state": "ATTENTION", "operator_control": "ACTIVE"},
    }

    value = build_operator_home(
        operating=operating,
        attention=(),
        paper={
            "summary": {
                "internal_account_count": 2,
                "open_order_count": 1,
                "unresolved_reconciliation_count": 1,
            }
        },
        performance={
            "health": ({"state": "DEGRADED"}, {"state": "INSUFFICIENT_SAMPLE"}),
            "paper_evidence_counts": {"reconciliation_issues": 2},
        },
    )

    assert value["summary_source"] == "RETAINED_OPERATING_BRIEF"
    assert value["eligible_opportunities"] == 0
    assert value["blocked_opportunities"] == 3
    assert value["rejected_opportunities"] == 2
    assert value["unallocated_cash"] == "1000"
    assert value["paper_account_count"] == 2
    assert value["paper_open_order_count"] == 1
    assert value["reconciliation_issue_count"] == 3
    assert value["degraded_strategy_or_method_count"] == 1
    assert value["insufficient_sample_count"] == 1
    assert value["live_authority"] == "NONE"


def test_paper_console_keeps_internal_and_external_state_separate() -> None:
    value = build_operator_paper_operations(
        crypto_accounts=(),
        kalshi_accounts=(),
        external_instructions=(
            {
                "instruction": {
                    "instruction_id": "alpaca-instruction-1",
                    "campaign_id": "campaign-1",
                    "state": "READY",
                },
                "validated_fills": (),
                "reconciliations": ({"classification": "MATCHED"},),
            },
        ),
        crypto_capability=None,
        kalshi_capability={"primary_state": "PAPER_ONLY"},
    )

    assert value["internal_crypto"]["account_count"] == 0  # type: ignore[index]
    assert value["internal_kalshi"]["account_count"] == 0  # type: ignore[index]
    assert value["external_alpaca"]["instruction_count"] == 1  # type: ignore[index]
    assert value["external_alpaca"]["validated_fill_count"] == 0  # type: ignore[index]
    assert value["genuine_provider_writes_created"] == 0
    assert value["live_authority"] == "NONE"


def test_paper_console_projects_populated_lifecycle_without_merging_financial_state() -> None:
    value = build_operator_paper_operations(
        crypto_accounts=(
            {
                "account": {"account_id": "crypto-paper-1"},
                "account_state": {"cash": "900", "reserved_cash": "100"},
                "authorizations": ({"authorization_id": "auth-1"},),
                "previews": ({"preview_id": "preview-1", "rejection_reasons": ()},),
                "orders": (
                    {
                        "order_id": "order-1",
                        "product_id": "BTC-USD",
                        "state": "PARTIALLY_FILLED",
                        "target_base_quantity": "1",
                        "filled_base_quantity": "0.5",
                    },
                ),
                "fills": ({"fill_id": "fill-1"},),
                "positions": ({"position_id": "position-1", "state": "OPEN"},),
                "reconciliations": (
                    {
                        "reconciliation_id": "reconciliation-1",
                        "status": "BLOCKED",
                        "discrepancies": ("RESERVED_CASH_MISMATCH",),
                    },
                ),
            },
        ),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability={"primary_state": "PAPER_ONLY"},
        kalshi_capability=None,
    )

    crypto = value["internal_crypto"]  # type: ignore[assignment]
    account = crypto["accounts"][0]  # type: ignore[index]
    assert account["orders"][0]["state"] == "PARTIALLY_FILLED"  # type: ignore[index]
    assert account["positions"][0]["position_id"] == "position-1"  # type: ignore[index]
    assert value["summary"]["open_order_count"] == 1  # type: ignore[index]
    assert value["summary"]["unresolved_reconciliation_count"] == 1  # type: ignore[index]
    assert value["genuine_provider_writes_created"] == 0


def test_paper_console_crypto_account_surfaces_continuation_health_passed_through() -> None:
    # PAPER Continuation Health Signal v2 (cross-venue extension): identical
    # contract to build_operator_paper_strategy_registry's own
    # continuation_health parameter (see
    # test_paper_strategy_registry_continuation_health_passes_through_unmodified
    # below) -- this function never computes health itself, only surfaces
    # the caller's precomputed, already-jsonified value verbatim, keyed by
    # account_id.
    computed_health = {
        "state": "RECOVERED",
        "is_stale": False,
        "recovered_since_failure": True,
        "latest_continuation_at": "2026-09-01T14:25:23.303079Z",
        "latest_terminal_status": "FAILED",
        "latest_terminal_at": "2026-08-29T00:00:30.652625Z",
        "freshness_window_seconds": 600,
    }
    value = build_operator_paper_operations(
        crypto_accounts=({"account": {"account_id": "crypto-paper-1"}},),
        kalshi_accounts=({"account": {"account_id": "kalshi-paper-1"}},),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
        continuation_health={"crypto-paper-1": computed_health},
    )
    crypto_account = value["internal_crypto"]["accounts"][0]  # type: ignore[index]
    kalshi_account = value["internal_kalshi"]["accounts"][0]  # type: ignore[index]
    assert crypto_account["continuation_health"] == computed_health
    # An account absent from the mapping surfaces None, never a fabricated
    # or borrowed status from a different account/venue.
    assert kalshi_account["continuation_health"] is None


def test_paper_console_continuation_health_defaults_to_none_when_mapping_absent() -> None:
    value = build_operator_paper_operations(
        crypto_accounts=({"account": {"account_id": "crypto-paper-1"}},),
        kalshi_accounts=({"account": {"account_id": "kalshi-paper-1"}},),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
    )
    crypto_account = value["internal_crypto"]["accounts"][0]  # type: ignore[index]
    kalshi_account = value["internal_kalshi"]["accounts"][0]  # type: ignore[index]
    assert crypto_account["continuation_health"] is None
    assert kalshi_account["continuation_health"] is None


def test_paper_console_capability_gate_is_real_not_hardcoded() -> None:
    # PAPER Operations & Observability v1: the known misleading state this
    # milestone fixes -- the console must show the real, retained
    # PaperActivationEvent-folded gate, never a hardcoded value, and must
    # distinguish "no retained activation decision at all" from "disabled".
    enabled = build_operator_paper_operations(
        crypto_accounts=(),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
        paper_activation={
            "state": "ENABLED",
            "reason": "operator enabled crypto strategy PAPER",
            "actor": "operator:console",
            "occurred_at": "2026-08-27T00:00:00Z",
        },
    )
    assert enabled["capability_gate"]["state"] == "ENABLED"  # type: ignore[index]
    assert enabled["capability_gate"]["enabled"] is True  # type: ignore[index]
    assert enabled["capability_gate"]["empty_state"] is None  # type: ignore[index]

    disabled = build_operator_paper_operations(
        crypto_accounts=(),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
        paper_activation={"state": "DISABLED", "reason": "never enabled", "actor": "system"},
    )
    assert disabled["capability_gate"]["enabled"] is False  # type: ignore[index]

    never_evaluated = build_operator_paper_operations(
        crypto_accounts=(),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
    )
    assert never_evaluated["capability_gate"]["enabled"] is False  # type: ignore[index]
    assert (
        never_evaluated["capability_gate"]["empty_state"]  # type: ignore[index]
        == "NO_RETAINED_PAPER_ACTIVATION_DECISION"
    )


def test_paper_console_reports_real_cost_assumptions() -> None:
    value = build_operator_paper_operations(
        crypto_accounts=(),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
    )
    # Real governing constants (application.strategy_paper_trial), not
    # invented numbers -- Q10 ("what transaction-cost assumptions were
    # applied").
    assert value["cost_assumptions"]["fee_bps"] == 60  # type: ignore[index]
    assert value["cost_assumptions"]["spread_bps"] == 10  # type: ignore[index]
    assert value["cost_assumptions"]["slippage_bps"] == 10  # type: ignore[index]


def test_paper_console_surfaces_authorization_lineage_and_position_state_split() -> None:
    value = build_operator_paper_operations(
        crypto_accounts=(
            {
                "account": {"account_id": "crypto-paper-1"},
                "account_state": {"cash": "900", "reserved_cash": "0"},
                "authorizations": (
                    {
                        "authorization_id": "auth-1",
                        "strategy_or_discovery_id": "strategy-buy-and-hold",
                        "projection_id": "projection-1",
                    },
                ),
                "orders": (),
                "positions": (
                    {"position_id": "position-open", "state": "OPEN", "base_quantity": "0.01"},
                    {"position_id": "position-closed", "state": "CLOSED", "base_quantity": "0"},
                ),
                "operations": (
                    {"idempotency_key": "op-1", "operation_type": "PLACE_ORDER"},
                    {"idempotency_key": "op-2", "operation_type": "APPLY_EVIDENCE"},
                    {"idempotency_key": "op-3", "operation_type": "APPLY_EVIDENCE"},
                ),
                "reconciliations": (),
            },
        ),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
    )
    account = value["internal_crypto"]["accounts"][0]  # type: ignore[index]
    # Lineage: which strategy caused this PAPER action (Q11) -- an already
    # real, already-retained field this row previously dropped.
    assert account["authorizations"][0]["strategy_or_discovery_id"] == "strategy-buy-and-hold"
    assert dict(account["position_state_counts"]) == {"OPEN": 1, "CLOSED": 1}
    assert account["operations_summary"]["total"] == 3
    assert dict(account["operations_summary"]["by_type"]) == {
        "PLACE_ORDER": 1,
        "APPLY_EVIDENCE": 2,
    }
    # An OPEN position with no supplied mark shows an honest disposition,
    # never a fabricated number.
    open_position = next(
        item for item in account["positions"] if item["position_id"] == "position-open"
    )
    assert open_position["unrealized_paper_pnl"] is None
    assert open_position["mark_disposition"] == "NOT_EVALUATED"
    closed_position = next(
        item for item in account["positions"] if item["position_id"] == "position-closed"
    )
    assert closed_position["mark_disposition"] == "CLOSED_POSITION_NOT_MARKED"


def test_paper_console_unrealized_pnl_is_passed_through_never_recomputed() -> None:
    # The projector must never re-derive a financial total from already-
    # jsonified (str) evidence -- every unrealized figure below is supplied
    # pre-computed by the caller, exactly like realized_paper_pnl already is.
    value = build_operator_paper_operations(
        crypto_accounts=(
            {
                "account": {"account_id": "crypto-paper-1"},
                "account_state": {"cash": "900"},
                "positions": (
                    {"position_id": "position-open", "state": "OPEN", "base_quantity": "0.01"},
                    {"position_id": "position-stale", "state": "OPEN", "base_quantity": "0.01"},
                ),
            },
        ),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
        position_marks={
            "position-open": {
                "unrealized_paper_pnl": "50.00",
                "mark_price": "55000",
                "marked_at": "2026-08-30T00:05:00Z",
            },
            "position-stale": {"disposition": "STALE", "reasons": ["LIVE_OBSERVATION_STALE"]},
        },
        unrealized_totals={"crypto-paper-1": "50.00"},
    )
    account = value["internal_crypto"]["accounts"][0]  # type: ignore[index]
    assert account["unrealized_paper_pnl"] == "50.00"
    marked = next(item for item in account["positions"] if item["position_id"] == "position-open")
    assert marked["unrealized_paper_pnl"] == "50.00"
    assert marked["mark_price"] == "55000"
    assert marked["mark_disposition"] == "SATISFIED"
    stale = next(item for item in account["positions"] if item["position_id"] == "position-stale")
    assert stale["unrealized_paper_pnl"] is None
    assert stale["mark_disposition"] == "STALE"
    # One of the two OPEN positions genuinely got a SATISFIED mark this
    # request; the coverage figure must reflect exactly that, distinctly
    # from "no open positions at all" (F -- stale market data is a visible,
    # distinct degraded state, not silently indistinguishable from idle).
    assert account["open_position_mark_coverage"] == {"open_count": 2, "marked_count": 1}


def test_paper_console_reconciliation_reflects_the_latest_evaluation_only() -> None:
    # An old, already-resolved discrepancy must not still read as currently
    # unreconciled once a later, clean reconciliation exists (Q13).
    value = build_operator_paper_operations(
        crypto_accounts=(
            {
                "account": {"account_id": "crypto-paper-1"},
                "account_state": {"cash": "900"},
                "reconciliations": (
                    {
                        "reconciliation_id": "reconciliation-old",
                        "status": "DISCREPANCY",
                        "discrepancies": ("RESERVED_CASH_MISMATCH",),
                        "evaluated_at": "2026-08-29T00:00:00Z",
                    },
                    {
                        "reconciliation_id": "reconciliation-new",
                        "status": "CLEAN",
                        "discrepancies": (),
                        "evaluated_at": "2026-08-30T00:00:00Z",
                    },
                ),
            },
        ),
        kalshi_accounts=(),
        external_instructions=(),
        crypto_capability=None,
        kalshi_capability=None,
    )
    account = value["internal_crypto"]["accounts"][0]  # type: ignore[index]
    assert account["latest_reconciliation"]["reconciliation_id"] == "reconciliation-new"
    assert account["has_unresolved_reconciliation"] is False


def _strategy_candidate(strategy_id: str) -> dict[str, object]:
    return {
        "strategy_id": strategy_id,
        "display_name": f"Fixture strategy {strategy_id}",
        "version": 1,
        "source_kind": "EXTERNAL",
        "venue": "CRYPTO",
        "instrument_ids": ("BTC-USD",),
        "rule_kind": "MOVING_AVERAGE_TREND",
        "horizon_kind": "SWING",
    }


def test_paper_strategy_registry_shows_no_evaluation_yet_when_no_cycle_has_run() -> None:
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"), _strategy_candidate("strategy-b")),
        latest_task_result=None,
    )
    assert value["empty_state"] == "NO_RETAINED_STRATEGY_PAPER_CYCLE_YET"
    for row in value["strategies"]:  # type: ignore[union-attr]
        assert row["lifecycle_state"] is None
        assert row["evaluation_empty_state"] == "NOT_YET_EVALUATED_THIS_TASK_HISTORY"
        assert row["eligible_this_cycle"] is False


def test_paper_strategy_registry_has_evaluation_history_false_when_history_param_omitted() -> None:
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),), latest_task_result=None
    )
    row = value["strategies"][0]  # type: ignore[index]
    assert row["has_evaluation_history"] is False
    assert row["latest_durable_evaluation"] is None
    assert row["durable_current_lifecycle_state"] is None
    assert row["recent_evaluations"] == ()
    assert row["promotion_count"] == 0


def test_paper_strategy_registry_has_evaluation_history_false_for_a_zero_evaluation_strategy() -> (
    None
):
    """Regression: a strategy_id genuinely present in ``internal_evaluation_
    history`` (the caller always includes every candidate) but with ZERO
    retained evaluations must still show ``has_evaluation_history=False`` --
    the per-strategy history dict itself is never empty (it always carries
    ``evaluations``/``latest_evaluation``/... keys, defaulted), so this must
    be derived from whether a real evaluation exists, never from truthiness
    of the wrapping dict alone."""

    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),),
        latest_task_result=None,
        internal_evaluation_history={
            "strategy-a": {
                "strategy_id": "strategy-a",
                "evaluations": (),
                "latest_evaluation": None,
                "promotions": (),
                "latest_promotion": None,
                "current_lifecycle_state": "RESEARCH_ONLY",
                "authority": "NONE",
                "warning": "...",
                "freshness": "NEVER_EVALUATED",
            }
        },
    )
    row = value["strategies"][0]  # type: ignore[index]
    assert row["has_evaluation_history"] is False
    assert row["latest_durable_evaluation"] is None
    assert row["durable_current_lifecycle_state"] == "RESEARCH_ONLY"
    assert row["evaluation_freshness"] == "NEVER_EVALUATED"
    assert row["recent_evaluations"] == ()
    assert row["promotion_count"] == 0
    assert row["latest_promotion"] is None


def test_paper_strategy_registry_durable_history_can_honestly_differ_from_this_cycle() -> None:
    """Part 14: current operational lifecycle_state (this cycle's live
    dispatch) and durable_current_lifecycle_state (retained promotion
    history) may legitimately disagree -- proven directly, never merged."""

    evaluation = {
        "evaluation_id": "eval-1",
        "evaluated_at": "2026-09-02T00:05:00Z",
        "as_of": "2026-09-02T00:00:00Z",
        "qualification": "WOULD_QUALIFY_FOR_PAPER_ELIGIBLE",
        "sample_size": 35,
        "net_return": "0.10",
    }
    promotion = {
        "promotion_id": "promo-1",
        "promoted_at": "2026-09-01T00:00:00Z",
        "actor": "operator:test",
    }
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),),
        # This cycle's own live dispatch never even considered the
        # strategy (e.g. a stale/absent terminal result) -- lifecycle_state
        # reads None -- while a durable promotion from an EARLIER real
        # dispatch is still genuinely retained and must keep showing.
        latest_task_result=None,
        internal_evaluation_history={
            "strategy-a": {
                "strategy_id": "strategy-a",
                "evaluations": (evaluation,),
                "latest_evaluation": evaluation,
                "promotions": (promotion,),
                "latest_promotion": promotion,
                "current_lifecycle_state": "PAPER_ELIGIBLE",
                "authority": "NONE",
                "warning": "...",
                "freshness": "FRESH",
            }
        },
    )
    row = value["strategies"][0]  # type: ignore[index]
    assert row["lifecycle_state"] is None  # this cycle's own live view
    assert row["durable_current_lifecycle_state"] == "PAPER_ELIGIBLE"  # durable view
    assert row["has_evaluation_history"] is True
    assert row["latest_durable_evaluation"] == evaluation
    assert row["evaluation_freshness"] == "FRESH"
    assert row["recent_evaluations"] == (evaluation,)
    assert row["promotion_count"] == 1
    assert row["latest_promotion"] == promotion


def test_paper_strategy_registry_distinguishes_open_trial_from_pre_promotion_rejection() -> None:
    value = build_operator_paper_strategy_registry(
        candidates=(
            _strategy_candidate("strategy-open-trial"),
            _strategy_candidate("strategy-never-promoted"),
            _strategy_candidate("strategy-promoted-not-selected"),
        ),
        latest_task_result={
            "status": "COMPLETED",
            "completed_at": "2026-08-30T00:05:00Z",
            "warning_codes": (
                "STRATEGY_PAPER_CANDIDATES_CONSIDERED:3",
                "STRATEGY_PAPER_CANDIDATES_PROMOTED:2",
                "STRATEGY_PAPER_TRIALS_CREATED:0",
                "STRATEGY_PAPER_RECONCILIATION:CLEAN",
                "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:SATISFIED",
                "STRATEGY_PAPER_REJECTED:strategy-never-promoted:INSUFFICIENT_RETAINED_CANDLE_HISTORY",
                "STRATEGY_PAPER_REJECTED:strategy-promoted-not-selected:"
                "NOT_SELECTED_THIS_CYCLE_RANK_OR_SAME_INSTRUMENT_CAPACITY",
            ),
        },
        open_trial_order_by_strategy_id={
            "strategy-open-trial": {
                "order_id": "order-1",
                "account_id": "genuine-strategy-crypto-paper-account",
            },
        },
    )
    rows = {row["strategy_id"]: row for row in value["strategies"]}  # type: ignore[union-attr]

    assert rows["strategy-open-trial"]["lifecycle_state"] == "PAPER_ELIGIBLE"
    assert rows["strategy-open-trial"]["has_open_trial"] is True
    assert rows["strategy-open-trial"]["eligible_this_cycle"] is True
    assert rows["strategy-open-trial"]["open_order_id"] == "order-1"
    assert rows["strategy-open-trial"]["open_order_evidence_href"] == (
        "/api/v1/crypto-paper/accounts/genuine-strategy-crypto-paper-account/orders/order-1"
    )
    assert rows["strategy-never-promoted"]["open_order_evidence_href"] is None

    # Never reached the RESEARCH_ONLY -> PAPER_ELIGIBLE evidence gate this
    # cycle (Q3/Q5 -- lifecycle state AND why).
    assert rows["strategy-never-promoted"]["lifecycle_state"] == "RESEARCH_ONLY"
    assert rows["strategy-never-promoted"]["blocking_reasons"] == (
        "INSUFFICIENT_RETAINED_CANDLE_HISTORY",
    )
    assert rows["strategy-never-promoted"]["eligible_this_cycle"] is False

    # WAS promoted (reached PAPER_ELIGIBLE) but lost this cycle's single-
    # slot ranking -- a materially different state from never-promoted,
    # and the read model must not conflate them.
    assert rows["strategy-promoted-not-selected"]["lifecycle_state"] == "PAPER_ELIGIBLE"
    assert rows["strategy-promoted-not-selected"]["eligible_this_cycle"] is False
    assert rows["strategy-promoted-not-selected"]["blocking_reasons"] == (
        "NOT_SELECTED_THIS_CYCLE_RANK_OR_SAME_INSTRUMENT_CAPACITY",
    )

    assert value["candidates_considered"] == "3"
    assert value["candidates_promoted"] == "2"
    assert value["reconciliation_status"] == "CLEAN"
    assert value["empty_state"] is None


def test_paper_strategy_registry_distinguishes_broken_task_from_nothing_qualifies() -> None:
    # Q15: "if nothing is trading, is that because the system is broken or
    # because no strategy currently qualifies". A FAILED task never reaches
    # its own return statement, so it has no STRATEGY_PAPER_* warning codes
    # at all -- every strategy must show "not yet evaluated", not a false
    # "ineligible".
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),),
        latest_task_result={
            "status": "FAILED",
            "completed_at": "2026-08-30T00:05:00Z",
            "warning_codes": (),
            "error_code": "STRATEGY_PAPER_TRIAL_UNEXPECTED_ERROR",
        },
    )
    assert value["latest_task_status"] == "FAILED"
    row = value["strategies"][0]  # type: ignore[index]
    assert row["lifecycle_state"] is None
    assert row["blocking_reasons"] == ()


def test_paper_strategy_registry_account_last_reconciled_at_is_a_real_cross_check() -> None:
    # Regression: STRATEGY_PAPER_TRIAL_EXECUTION reports
    # awaits_intraday_continuation=True on every successful dispatch, so
    # ExecuteOperatingCycle deliberately never persists a terminal
    # OperatingTaskResult while it keeps succeeding -- only a genuine
    # exception forces one. A task that FAILED three days ago and has been
    # succeeding silently every cycle since would show that stale failure
    # here as "latest_task_status" with no contradiction in this function's
    # own fields alone. account_last_reconciled_at is the real, independent
    # freshness cross-check the caller supplies (from the account's own
    # Reconciliation history, refreshed on every real dispatch) -- proven
    # here to survive being passed straight through, unmodified, alongside
    # an old FAILED terminal result, exactly the real production shape this
    # milestone's own runtime acceptance against retained state uncovered.
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),),
        latest_task_result={
            "status": "FAILED",
            "completed_at": "2026-08-29T00:00:30Z",
            "warning_codes": (),
            "error_code": "LiveMarketObservationError",
        },
        account_last_reconciled_at="2026-08-31T23:15:00Z",
    )
    assert value["latest_task_status"] == "FAILED"
    assert value["latest_task_completed_at"] == "2026-08-29T00:00:30Z"
    assert value["account_last_reconciled_at"] == "2026-08-31T23:15:00Z"


def test_paper_strategy_registry_account_last_reconciled_at_defaults_to_none() -> None:
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),), latest_task_result=None
    )
    assert value["account_last_reconciled_at"] is None


def test_paper_strategy_registry_continuation_health_passes_through_unmodified() -> None:
    # PAPER Continuation Health Signal v1: the actual RECOVERED/FAILED/etc.
    # classification logic lives entirely in
    # domain.operating_cycle.compute_paper_continuation_health (see
    # tests/test_operating_cycle.py for its full state-machine coverage) --
    # the caller (operator_api.py) computes it once, already-jsonified, and
    # this function's own job is only to surface it verbatim alongside the
    # raw fields it supersedes for "is it healthy now", exactly like
    # account_last_reconciled_at above.
    computed_health = {
        "state": "RECOVERED",
        "is_stale": False,
        "recovered_since_failure": True,
        "latest_continuation_at": "2026-09-01T14:25:23.303079Z",
        "latest_terminal_status": "FAILED",
        "latest_terminal_at": "2026-08-29T00:00:30.652625Z",
        "freshness_window_seconds": 600,
    }
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),),
        latest_task_result={
            "status": "FAILED",
            "completed_at": "2026-08-29T00:00:30.652625Z",
            "warning_codes": (),
            "error_code": "LiveMarketObservationError",
        },
        account_last_reconciled_at="2026-09-01T14:25:23.303079Z",
        continuation_health=computed_health,
    )
    # The raw, historical fields are kept exactly as before -- never
    # removed, never reinterpreted -- so the FAILED row stays fully visible.
    assert value["latest_task_status"] == "FAILED"
    assert value["latest_task_completed_at"] == "2026-08-29T00:00:30.652625Z"
    assert value["account_last_reconciled_at"] == "2026-09-01T14:25:23.303079Z"
    # continuation_health is the new, authoritative "is it healthy right
    # now" answer, surfaced alongside them unmodified.
    assert value["continuation_health"] == computed_health


def test_paper_strategy_registry_continuation_health_defaults_to_none() -> None:
    value = build_operator_paper_strategy_registry(
        candidates=(_strategy_candidate("strategy-a"),), latest_task_result=None
    )
    assert value["continuation_health"] is None


def test_attention_is_deterministic_and_reconciliation_precedes_information() -> None:
    paper = build_operator_paper_operations(
        crypto_accounts=(),
        kalshi_accounts=(),
        external_instructions=(
            {
                "instruction": {"instruction_id": "external-1"},
                "validated_fills": (),
                "reconciliations": ({"classification": "UNKNOWN_ACTIVITY"},),
            },
        ),
        crypto_capability=None,
        kalshi_capability=None,
    )
    performance = {"health": ({"health_id": "health-1", "state": "INSUFFICIENT_SAMPLE"},)}

    first = build_operator_attention(
        operating={}, shared=_shared(), performance=performance, paper=paper
    )
    second = build_operator_attention(
        operating={}, shared=_shared(), performance=performance, paper=paper
    )

    assert first == second
    assert first[0]["category"] == "PAPER_RECONCILIATION"
    assert any(item["status"] == "WAITING" for item in first)
    assert all("source_identity" in item for item in first)
    assert all("available_operator_action" in item for item in first)


def test_allocation_and_learning_are_read_only_exact_evidence_projections() -> None:
    allocation = build_operator_allocation(
        {
            "decision": {
                "decision_id": "decision-1",
                "retained_cash": "1000",
                "venue_totals": (("CRYPTO", "0"), ("KALSHI", "0")),
            },
            "lines": (),
            "paper_trial_proposals": (),
        }
    )
    learning = build_operator_learning(
        {
            "attributions": (),
            "health": ({"health_id": "health-1", "state": "INSUFFICIENT_SAMPLE"},),
        }
    )

    assert allocation["orders_created"] == 0
    assert allocation["live_authority"] == "NONE"
    assert learning["empty_state"] == "INSUFFICIENT_SAMPLE"
    assert learning["authority"] == "NONE"


def test_operator_console_adds_get_only_routes_and_readiness_only_live_surface() -> None:
    expected = {
        "/api/v1/operator-console/home",
        "/api/v1/operator-console/attention",
        "/api/v1/operator-console/opportunities",
        "/api/v1/operator-console/strategy-library",
        "/api/v1/operator-console/allocation",
        "/api/v1/operator-console/paper",
        "/api/v1/operator-console/learning",
        "/api/v1/operator-console/live-readiness",
    }
    methods = {
        route.path: getattr(route, "methods", set())
        for route in app.routes
        if route.path in expected
    }

    assert methods.keys() == expected
    assert all(value == {"GET"} for value in methods.values())
    assert {path for path in methods if "live" in path.lower()} == {
        "/api/v1/operator-console/live-readiness"
    }


def test_broad_market_console_route_exists_and_is_get_only() -> None:
    routes = {route.path: getattr(route, "methods", set()) for route in app.routes}
    assert routes["/api/v1/operator-console/broad-market"] == {"GET"}


def test_broad_market_projection_reports_empty_state_and_never_touches_opportunities() -> None:
    empty = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": None,
            "as_of_date": None,
            "total_considered": 0,
            "research_eligible_count": 0,
            "identities_requiring_review": 0,
            "underlyings_considered": 0,
            "optionable_underlying_count": 0,
            "execution_capability_known_count": 0,
            "execution_capability_unknown_count": 0,
            "alpaca_credentials_available": False,
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert empty["empty_state"] == "NO_RETAINED_BROAD_MARKET_UNIVERSE"
    assert empty["universe_href"] is None
    assert empty["authority"] == "NONE"
    assert "opportunity_id" not in empty
    assert "eligibility" not in empty
    assert "priority" not in empty
    assert "allocation" not in empty

    populated = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-2026-07-29",
            "as_of_date": "2026-07-29",
            "total_considered": 4,
            "research_eligible_count": 1,
            "identities_requiring_review": 0,
            "underlyings_considered": 2,
            "optionable_underlying_count": 1,
            "execution_capability_known_count": 0,
            "execution_capability_unknown_count": 4,
            "alpaca_credentials_available": False,
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert populated["empty_state"] is None
    assert populated["universe_href"] == "/api/v1/broad-equity/universe/universe-2026-07-29"
    assert populated["excluded_count"] == 3
    assert populated["research_eligible_count"] == 1
    assert populated["execution_capability_known_count"] == 0
    assert populated["execution_capability_unknown_count"] == 4
    assert populated["alpaca_credentials_available"] is False
    assert populated["authority"] == "NONE"


def _source_state(capture_id: str, retained: int) -> dict[str, object]:
    return {
        "capture_id": capture_id,
        "status": "COMPLETED",
        "retained_observations": retained,
        "observation_at": "2026-08-06T00:00:00+00:00",
    }


def test_broad_market_projection_reports_listing_source_coverage() -> None:
    """The operator must be able to tell whether U.S. listing coverage is
    Nasdaq-only, other-listed-only, complete, or not yet captured -- see
    _broad_market_reference_summary's listing_source_coverage field."""
    nasdaq_only = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-nasdaq-only",
            "total_considered": 3,
            "research_eligible_count": 1,
            "nasdaq_listed_source": _source_state("broad-equity-nasdaq-2026-08-06", 5579),
            "nasdaq_other_listed_source": {
                "capture_id": None,
                "status": None,
                "retained_observations": None,
                "observation_at": None,
            },
            "sec_company_tickers_source": _source_state("broad-equity-sec-2026-08-06", 10398),
            "listing_source_coverage": "NASDAQ_ONLY",
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert nasdaq_only["listing_source_coverage"] == "NASDAQ_ONLY"
    assert nasdaq_only["nasdaq_listed_source"]["retained_observations"] == 5579
    assert nasdaq_only["nasdaq_other_listed_source"]["capture_id"] is None
    assert nasdaq_only["sec_company_tickers_source"]["capture_id"] == "broad-equity-sec-2026-08-06"

    complete = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-complete",
            "total_considered": 10,
            "research_eligible_count": 4,
            "nasdaq_listed_source": _source_state("broad-equity-nasdaq-2026-08-06", 5579),
            "nasdaq_other_listed_source": _source_state(
                "broad-equity-nasdaq-other-2026-08-06", 6200
            ),
            "sec_company_tickers_source": _source_state("broad-equity-sec-2026-08-06", 10398),
            "listing_source_coverage": "NASDAQ_AND_OTHER_LISTED_COMPLETE",
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert complete["listing_source_coverage"] == "NASDAQ_AND_OTHER_LISTED_COMPLETE"
    assert complete["nasdaq_other_listed_source"]["retained_observations"] == 6200
    assert complete["authority"] == "NONE"


def test_security_identity_routes_exist_and_are_get_only() -> None:
    routes = {route.path: getattr(route, "methods", set()) for route in app.routes}
    assert routes["/api/v1/security-identity/resolve/{identity_id}"] == {"GET"}
    assert routes["/api/v1/security-identity/ambiguous"] == {"GET"}


def test_broad_market_projection_reports_stable_identity_fields_and_defaults_empty() -> None:
    """Stable-identity/options-relink fields must round-trip through the
    Console projection when present, and stay honestly empty/zero -- never
    raise -- for a summary predating this milestone's commands ever running.
    """
    without_identity_evidence = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-2026-08-06",
            "total_considered": 4,
            "research_eligible_count": 1,
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert without_identity_evidence["stable_identity_count"] == 0
    assert without_identity_evidence["identity_resolution_count"] == 0
    assert without_identity_evidence["latest_option_linkage_run_id"] is None
    assert without_identity_evidence["legacy_vs_stable_optionable_underlying_comparison"] is None

    with_identity_evidence = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-2026-08-06",
            "total_considered": 16398,
            "research_eligible_count": 4989,
            "stable_identity_count": 16398,
            "identity_resolution_count": 19245,
            "identity_resolution_state_counts": (
                ("ALIAS", 2789),
                ("AMBIGUOUS_REQUIRES_REVIEW", 58),
                ("CURRENT", 16398),
            ),
            "latest_option_linkage_run_id": "options-stable-relink-2026-08-06-v1",
            "option_linkage_distinct_raw_symbols": 4196,
            "option_linkage_linked_count": 4192,
            "option_linkage_unlinked_count": 1,
            "option_linkage_ambiguous_count": 3,
            "legacy_vs_stable_optionable_underlying_comparison": {
                "legacy_optionable_underlying_count": 0,
                "stable_linked_underlying_count": 4192,
            },
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert with_identity_evidence["stable_identity_count"] == 16398
    assert with_identity_evidence["identity_resolution_state_counts"] == (
        ("ALIAS", 2789),
        ("AMBIGUOUS_REQUIRES_REVIEW", 58),
        ("CURRENT", 16398),
    )
    assert with_identity_evidence["latest_option_linkage_run_id"] == (
        "options-stable-relink-2026-08-06-v1"
    )
    assert with_identity_evidence["option_linkage_linked_count"] == 4192
    assert with_identity_evidence["legacy_vs_stable_optionable_underlying_comparison"] == {
        "legacy_optionable_underlying_count": 0,
        "stable_linked_underlying_count": 4192,
    }
    assert with_identity_evidence["authority"] == "NONE"
    assert "opportunity_id" not in with_identity_evidence


def test_broad_market_projection_reports_historical_data_fields_and_defaults_empty() -> None:
    """Historical Market Data Foundation v1 fields must round-trip through the
    Console projection when present, and stay honestly empty/zero -- never
    raise -- for a summary predating any capture plan ever running. The
    point-in-time-universe/survivorship states must always be surfaced
    alongside the counts, never as a bare number on its own.
    """
    without_historical_evidence = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-2026-08-06",
            "total_considered": 4,
            "research_eligible_count": 1,
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert without_historical_evidence["historical_capture_plan_count"] == 0
    assert without_historical_evidence["historical_dataset_snapshot_count"] == 0
    assert without_historical_evidence["historical_latest_snapshot_id"] is None
    assert without_historical_evidence["historical_latest_snapshot_href"] is None
    assert without_historical_evidence["historical_data_warnings"] == ()

    with_historical_evidence = build_operator_broad_market(
        {
            "reference_contract": "BROAD_MARKET_REFERENCE_SUMMARY_V1",
            "universe_id": "universe-2026-08-06",
            "total_considered": 16398,
            "research_eligible_count": 4989,
            "historical_capture_plan_count": 1,
            "historical_dataset_snapshot_count": 1,
            "historical_quality_report_count": 1,
            "historical_latest_snapshot_id": "historical_dataset_snapshot_abc123",
            "historical_latest_snapshot_completeness_state": "PARTIAL",
            "historical_latest_snapshot_point_in_time_universe_state": (
                "POINT_IN_TIME_UNIVERSE_INCOMPLETE"
            ),
            "historical_latest_snapshot_survivorship_state": "SURVIVORSHIP_LIMITED",
            "historical_latest_snapshot_observation_count": 0,
            "historical_data_warnings": (
                "HISTORICAL MARKET DATA IS RESEARCH EVIDENCE, NOT A TRADING EDGE OR "
                "EXECUTION AUTHORITY",
            ),
            "warning": "REFERENCE SUMMARY ONLY",
            "authority": "NONE",
        }
    )
    assert with_historical_evidence["historical_capture_plan_count"] == 1
    assert with_historical_evidence["historical_latest_snapshot_id"] == (
        "historical_dataset_snapshot_abc123"
    )
    assert with_historical_evidence["historical_latest_snapshot_href"] == (
        "/api/v1/securities/historical-data/snapshots/historical_dataset_snapshot_abc123"
    )
    assert with_historical_evidence["historical_latest_snapshot_point_in_time_universe_state"] == (
        "POINT_IN_TIME_UNIVERSE_INCOMPLETE"
    )
    assert with_historical_evidence["historical_latest_snapshot_survivorship_state"] == (
        "SURVIVORSHIP_LIMITED"
    )
    assert with_historical_evidence["historical_data_warnings"] != ()
    assert with_historical_evidence["authority"] == "NONE"


def test_live_readiness_console_redacts_reference_locator_and_retains_blockers() -> None:
    value = build_operator_live_readiness(
        {
            "credential_references": (
                {
                    "reference_id": "reference-1",
                    "version": 1,
                    "provider": "COINBASE",
                    "account_environment": "LIVE",
                    "account_purpose": "READ_ONLY",
                    "required_permissions": ("PRIVATE_VIEW",),
                    "secret_backend": "ENVIRONMENT_REFERENCE",  # pragma: allowlist secret
                    "backend_reference_id": "must-not-reach-console",
                    "validation_status": "UNVALIDATED",
                    "canonical_hash": "a" * 64,
                },
            ),
            "current_execution_control": {"state": "LIVE_BLOCKED"},
            "current_assessment": {
                "recommendation": "LIVE_NOT_READY",
                "unresolved_blockers": ("PRIVATE_ACCOUNT_CAPABILITY_MISSING",),
            },
            "safe_controls": ("INSPECT_PROVIDER_CAPABILITY_READ_ONLY",),
            "prohibited_controls": ("GENUINE_ORDER_SUBMISSION",),
        },
        operational={
            "deployment_profile": {"profile_id": "APEX_LOCAL_OPERATIONAL_PROFILE_V1"},
            "runner": {"heartbeat_id": "runner-heartbeat-1", "status": "RUNNING"},
            "restore_verification": {"verification_id": "restore-1", "status": "VERIFIED"},
            "operational_health": {"health_id": "health-1", "state": "HEALTHY"},
            "alert_deliveries": ({"alert_id": "alert-1", "status": "DELIVERED"},),
            "kill_switch_drill": {"drill_id": "drill-1", "final_state": "LIVE_BLOCKED"},
            "safe_controls": ("TRIGGER_EXACT_BACKUP_VERIFICATION",),
        },
        kalshi_read_only={
            "provider": "KALSHI",
            "latest_approval": None,
            "latest_attempt": {
                "status": "BLOCKED",
                "blocked_reason": "PRIVATE_CREDENTIALS_ABSENT",
            },
            "provider_request_count": 0,
            "provider_write_count": 0,
            "authority": "NONE",
        },
    )
    assert value["readiness_recommendation"] == "LIVE_NOT_READY"
    assert value["live_authority"] == "NONE"
    assert "must-not-reach-console" not in str(value)
    assert value["kalshi_private_read_only"] == {
        "provider": "KALSHI",
        "latest_approval": None,
        "latest_attempt": {
            "status": "BLOCKED",
            "blocked_reason": "PRIVATE_CREDENTIALS_ABSENT",
        },
        "provider_request_count": 0,
        "provider_write_count": 0,
        "authority": "NONE",
    }
    assert value["credential_references"][0]["secret_values_retained"] is False  # type: ignore[index]
    assert value["deployment_profile"]["profile_id"] == "APEX_LOCAL_OPERATIONAL_PROFILE_V1"  # type: ignore[index]
    assert value["operating_runner"]["status"] == "RUNNING"  # type: ignore[index]
    assert "TRIGGER_EXACT_BACKUP_VERIFICATION" in value["safe_controls"]


def test_consolidated_frontend_keeps_authority_and_confirmation_boundaries_visible() -> None:
    # Operator Console Research GUI v1 extracted the shared presentation
    # primitives (including the EvidenceButton "Inspect evidence" control)
    # into a sibling consoleKit.tsx so every new page can reuse them
    # unchanged -- this check now spans both files rather than assuming
    # everything still lives in one, but every one of its assertions still
    # binds against the same real, shipped frontend source.
    frontend_src = Path(__file__).parents[1] / "frontend" / "src"
    source = (frontend_src / "operatorConsole.tsx").read_text() + (
        frontend_src / "consoleKit.tsx"
    ).read_text()

    assert "SUMMARY SOURCE: RETAINED_OPERATING_BRIEF" in source
    assert "RESEARCH PRIORITIES ONLY · NOT ORDERS" in source
    assert "Current strategy library" in source
    assert "KALSHI_RELATED_MARKET_CONSISTENCY_V1" not in source
    assert "NO BULK ACTIONS" in source
    assert "chooses no Opportunity automatically" in source
    assert "broker records are evidence" in source.lower()
    assert "LIVE AUTHORITY NONE" in source
    assert "Live Readiness" in source
    assert "DEFAULT LIVE_NOT_READY" in source
    assert "NO GENUINE PROVIDER WRITE" in source
    assert "Operational reliability" in source
    assert "Restore verification" in source
    assert "Coinbase read-only account capability" in source
    assert "READ-ONLY EVIDENCE — NOT APEX CASH OR TRADING CAPITAL" in source
    assert "Safe exact-ID actions:" in source
    assert "submit · cancel · replace · transfer · withdraw · live arming" in source
    assert "GENUINE_ORDER_SUBMISSION" not in source
    assert "Inspect evidence" in source


def test_research_operations_console_projects_empty_and_populated_states() -> None:
    empty = build_operator_research_operations({})
    assert empty["agenda_count"] == 0
    assert empty["automatic_approval"] is False
    assert empty["automatic_experiment_execution"] is False
    assert empty["live_authority"] == "NONE"

    value = build_operator_research_operations(
        {
            "agenda_items": (
                {
                    "agenda_item_id": "agenda-1",
                    "problem_statement": "retained blocker",
                    "authority": "NONE",
                },
            ),
            "proposals": (
                {
                    "proposal": {
                        "proposal_id": "proposal-1",
                        "canonical_hash": "a" * 64,
                        "version": 1,
                        "agenda_item_id": "agenda-1",
                        "venue": "CRYPTO",
                        "origin": "AI_ASSISTED",
                    },
                    "state": "READY_FOR_REVIEW",
                    "duplicate_decision": {
                        "classification": "MATERIALLY_DIFFERENT_CHALLENGER",
                        "canonical_hash": "b" * 64,
                        "approval_permitted": True,
                    },
                    "budget": {"maximum_runs": 1, "remaining_runs": 1},
                    "recommendations": (),
                },
            ),
        }
    )

    assert value["agenda_count"] == 1
    assert value["awaiting_review_count"] == 1
    proposal = value["proposals"][0]  # type: ignore[index]
    assert proposal["proposal_hash"] == "a" * 64  # type: ignore[index]
    assert proposal["duplicate_decision_hash"] == "b" * 64  # type: ignore[index]
    assert proposal["paper_authorization_created"] is False  # type: ignore[index]
    assert (  # type: ignore[index]
        proposal["evidence_href"] == "/api/v1/research-operations/proposals/proposal-1"
    )

    api_source = Path("src/apex/operator_api.py").read_text(encoding="utf-8")
    ui_source = Path("frontend/src/operatorConsole.tsx").read_text(encoding="utf-8")
    assert "/api/v1/operator-console/research-operations" in api_source
    assert "/api/v1/research-operations/proposals/{proposal_id}/run" in api_source
    assert "/api/v1/research-operations/generator-runs" in api_source
    assert "/api/v1/research-operations/review-queue" in api_source
    assert "/api/v1/research-operations/experiment-queue/{queue_id}/execute" in api_source
    assert "Research Operations" in ui_source
    assert "NO AUTOMATIC APPROVAL" in ui_source
