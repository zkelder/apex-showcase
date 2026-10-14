from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.shared_opportunity import (
    SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1,
    SHARED_OPPORTUNITY_PRIORITY_POLICY_V1,
    EligibilityState,
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
    propose_hypothetical_allocation,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 7, 30, tzinfo=UTC)


def projection(
    identity: str,
    *,
    venue: SharedVenue = SharedVenue.CRYPTO,
    comparable_value: str | None = "0.05",
    comparable_value_identity: str | None = "FIXTURE_COMPARABLE_MEASURE",
    maximum_loss: str | None = "10",
    capital: str | None = "100",
    fees: str | None = "1",
    slippage: str | None = "1",
    lockup: int | None = 30,
    holding_days: int | None = 30,
    uncertainty: str | None = "0.10",
    freshness: int = 60,
    liquidity: str = "SUFFICIENT",
    rejection_reasons: tuple[str, ...] = (),
    maximum_allocation: str | None = "400",
    warnings: tuple[str, ...] = ("FIXTURE_RESEARCH_ONLY",),
    authority: str = "NONE",
    evidence_ids: tuple[str, ...] | None = None,
    statistical_sample_size: int | None = None,
    strategy_or_method_id: str = "FIXTURE_METHOD_V1",
    expected_net_return: str | None = None,
    costs_retained: bool | None = None,
) -> SharedOpportunityProjection:
    """Ranking-v1 note: ``comparable_value_identity`` defaults to a tag that is deliberately
    *not* in ``GENUINE_ECONOMIC_MAGNITUDE_IDENTITIES`` (fixtures are proxy-magnitude by default,
    same as the real ``CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1`` source). Pass one of the identities
    from that allowlist (see ``shared_opportunity.py``) to build a genuine-EV fixture instead.
    ``holding_days`` (expected_holding_days, drives horizon normalization) and ``lockup``
    (estimated_lockup_days, drives the LOCKUP component) both default to 30 -- equal, like real
    retained projections -- but vary independently when a test needs to isolate one effect."""

    return SharedOpportunityProjection.create(
        projection_id=f"shared-{identity}",
        venue=venue,
        source_record_type="FixtureOpportunity",
        source_record_id=identity,
        source_canonical_hash=content_digest((identity, "source")),
        strategy_or_method_id=strategy_or_method_id,
        instrument_id=f"{identity}-instrument",
        product_or_market_id=f"{identity}-market",
        direction="HYPOTHETICAL_LONG_RESEARCH",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=60),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=freshness,
        freshness_limit_seconds=3600,
        expected_gross_return=None,
        expected_net_return=expected_net_return,
        comparable_value=comparable_value,
        comparable_value_identity=comparable_value_identity,
        uncertainty=uncertainty,
        maximum_loss=maximum_loss,
        modeled_fees=fees,
        modeled_slippage=slippage,
        costs_retained=(
            (fees is not None and slippage is not None)
            if costs_retained is None
            else costs_retained
        ),
        liquidity_state=liquidity,
        liquidity_limitations=("FIXTURE_DISPLAYED_LIQUIDITY_ONLY",),
        required_hypothetical_capital=capital,
        maximum_sensible_research_allocation=maximum_allocation,
        source_maximum_allocation_fraction=None,
        expected_holding_days=holding_days,
        estimated_lockup_days=lockup,
        supporting_evidence_ids=(
            (f"{identity}-evidence",) if evidence_ids is None else evidence_ids
        ),
        source_warnings=warnings,
        source_rejection_reasons=rejection_reasons,
        source_authority=authority,
        projection_limitations=("FIXTURE_ONLY",),
        source_details=(
            ("yes_side", "BUY_YES") if venue is SharedVenue.KALSHI else ("route", "USD"),
        ),
        statistical_sample_size=statistical_sample_size,
    )


def test_projection_preserves_missing_values_and_venue_semantics() -> None:
    crypto = projection("crypto", comparable_value="-0.05", maximum_loss=None, capital=None)
    kalshi = projection("kalshi", venue=SharedVenue.KALSHI)
    assert crypto.maximum_loss is None
    assert crypto.comparable_value == Decimal("-0.05")
    assert crypto.required_hypothetical_capital is None
    assert crypto.source_details == (("route", "USD"),)
    assert kalshi.source_details == (("yes_side", "BUY_YES"),)
    assert crypto.authority == kalshi.authority == "NONE"
    assert not crypto.paper_eligible and not kalshi.live_eligible


def test_eligibility_retains_every_missing_and_source_rejection_reason() -> None:
    blocked = projection(
        "blocked",
        maximum_loss=None,
        capital=None,
        fees=None,
        slippage=None,
        liquidity="UNAVAILABLE",
        rejection_reasons=("SOURCE_POLICY_FAILED",),
    )
    _, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id="missing-set", created_at=NOW, projections=(blocked,)
    )
    decision = decisions[0]
    assert decision.policy_id == SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1
    assert decision.state is EligibilityState.BLOCKED_MISSING_EVIDENCE
    assert {
        "EXPECTED_COSTS_MISSING",
        "MAXIMUM_LOSS_MISSING",
        "REQUIRED_CAPITAL_MISSING",
        "LIQUIDITY_INSUFFICIENT",
        "SOURCE_POLICY_REJECTED",
        "SOURCE:SOURCE_POLICY_FAILED",
    }.issubset(decision.reasons)
    assert priorities == ()
    assert "no hypothetical allocation" in explanations[0].text


def test_expiration_and_staleness_fail_closed() -> None:
    expired = replace(
        projection("expired"),
        valid_until=NOW,
        canonical_hash=content_digest(("expired",)),
    )
    stale = projection("stale", freshness=3601)
    _, decisions, _, _ = build_opportunity_set(
        opportunity_set_id="time-set", created_at=NOW, projections=(expired, stale)
    )
    by_id = {item.projection_id: item for item in decisions}
    assert by_id["shared-expired"].state is EligibilityState.EXPIRED
    assert by_id["shared-stale"].state is EligibilityState.REJECTED
    assert "EVIDENCE_STALE" in by_id["shared-stale"].reasons


def test_priority_is_decimal_deterministic_and_ties_use_exact_source_identity() -> None:
    second = projection("b", venue=SharedVenue.KALSHI)
    first = projection("a", venue=SharedVenue.CRYPTO)
    opportunity_set, decisions, priorities, _ = build_opportunity_set(
        opportunity_set_id="tie-set",
        created_at=NOW,
        projections=(second, first),
    )
    assert all(item.state is EligibilityState.ELIGIBLE for item in decisions)
    assert priorities[0].projection_id == "shared-a"
    assert priorities[1].projection_id == "shared-b"
    assert priorities[0].score == priorities[1].score
    assert opportunity_set.tie_groups == (("shared-a", "shared-b"),)
    assert priorities[0].policy_id == SHARED_OPPORTUNITY_PRIORITY_POLICY_V1
    assert sum((item.weight for item in priorities[0].components), Decimal(0)) == Decimal(1)


def test_cost_and_lockup_change_priority_without_return_only_ranking() -> None:
    efficient = projection("efficient", fees="0.25", slippage="0.25", lockup=2)
    costly = projection("costly", comparable_value="0.06", fees="10", slippage="10", lockup=300)
    _, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="cost-set", created_at=NOW, projections=(costly, efficient)
    )
    assert priorities[0].projection_id == efficient.projection_id


def test_zero_eligible_preserves_all_hypothetical_cash() -> None:
    rejected = projection("rejected", rejection_reasons=("SOURCE_REJECTED",))
    opportunity_set, _, priorities, _ = build_opportunity_set(
        opportunity_set_id="zero-set", created_at=NOW, projections=(rejected,)
    )
    proposal = propose_hypothetical_allocation(
        proposal_id="zero-allocation",
        opportunity_set=opportunity_set,
        projections=(rejected,),
        priorities=priorities,
    )
    assert proposal.allocations == ()
    assert proposal.unallocated_cash == Decimal("1000")
    assert not proposal.reservation and not proposal.order
    assert proposal.financial_authority == "NONE"


def test_allocation_enforces_opportunity_and_venue_caps_and_excludes_rejected() -> None:
    crypto_a = projection("crypto-a", comparable_value="0.09")
    crypto_b = projection("crypto-b", comparable_value="0.08")
    crypto_c = projection("crypto-c", comparable_value="0.07")
    kalshi = projection("kalshi-a", venue=SharedVenue.KALSHI, comparable_value="0.06")
    rejected = projection(
        "kalshi-rejected",
        venue=SharedVenue.KALSHI,
        comparable_value="0.20",
        rejection_reasons=("SOURCE_REJECTED",),
    )
    projections = (crypto_a, crypto_b, crypto_c, kalshi, rejected)
    opportunity_set, _, priorities, explanations = build_opportunity_set(
        opportunity_set_id="allocation-set", created_at=NOW, projections=projections
    )
    proposal = propose_hypothetical_allocation(
        proposal_id="allocation-proposal",
        opportunity_set=opportunity_set,
        projections=projections,
        priorities=priorities,
    )
    assert all(item.amount <= Decimal("250") for item in proposal.allocations)
    assert dict(proposal.venue_totals)["CRYPTO"] == Decimal("500")
    assert all(item.projection_id != rejected.projection_id for item in proposal.allocations)
    assert proposal.unallocated_cash == Decimal("250")
    assert all(item.canonical_hash for item in explanations)


def test_replay_inputs_produce_exact_set_and_explanation_hashes() -> None:
    values = (projection("one"), projection("two", venue=SharedVenue.KALSHI))
    first = build_opportunity_set(
        opportunity_set_id="replay-set", created_at=NOW, projections=values
    )
    replay = build_opportunity_set(
        opportunity_set_id="replay-set", created_at=NOW, projections=values
    )
    assert first[0].canonical_hash == replay[0].canonical_hash
    assert tuple(item.canonical_hash for item in first[3]) == tuple(
        item.canonical_hash for item in replay[3]
    )


def test_exact_id_cli_and_read_only_api_routes_are_registered() -> None:
    from apex.cli import parser
    from apex.operator_api import create_app

    parsed = parser().parse_args(
        ["shared-projection-inspect", "--projection-id", "exact-projection"]
    )
    assert parsed.projection_id == "exact-projection"
    mutation = parser().parse_args(
        [
            "shared-opportunity-materialize",
            "--opportunity-set-id",
            "exact-set",
            "--crypto-pulse-id",
            "exact-pulse",
            "--kalshi-evaluation-id",
            "exact-kalshi",
            "--as-of",
            NOW.isoformat(),
            "--idempotency-key",
            "exact-key",
        ]
    )
    assert mutation.opportunity_set_id == "exact-set"
    assert mutation.idempotency_key == "exact-key"
    routes = {
        (route.path, method)
        for route in create_app().routes
        for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/shared-opportunities/workspace", "GET") in routes
    assert (
        "/api/v1/shared-opportunities/projections/{projection_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/shared-opportunities/sets/{opportunity_set_id}",
        "GET",
    ) in routes
    assert (
        "/api/v1/shared-opportunities/allocations/{proposal_id}",
        "GET",
    ) in routes


def test_crypto_projection_becomes_paper_eligible_when_all_evidence_qualifies() -> None:
    """The canonical crypto PAPER-readiness computation (routed through
    SharedOpportunityProjection.create itself, not duplicated per call site)
    marks a genuinely well-evidenced crypto Opportunity paper_eligible=True."""

    value = projection("ready")
    assert value.paper_eligible is True
    assert value.paper_eligibility_reasons == ("PAPER_ELIGIBLE_UNDER_CANONICAL_SHARED_POLICY",)


def test_kalshi_projection_is_always_scoped_out_of_crypto_paper_eligibility() -> None:
    """Kalshi keeps gating PAPER consideration through its own
    KalshiPaperEligibility/EligibilityDecision path (see domain/kalshi_operating.py),
    which never reads this field -- it must always compute False here, with a
    single unambiguous reason, and never be incorrectly forced through the
    crypto-specific rules below."""

    value = projection("kalshi-scope", venue=SharedVenue.KALSHI)
    assert value.paper_eligible is False
    assert value.paper_eligibility_reasons == ("PAPER_ELIGIBILITY_SCOPED_TO_CRYPTO_VENUE",)


def test_crypto_projection_rejects_stale_evidence() -> None:
    value = projection("stale", freshness=3601)
    assert value.paper_eligible is False
    assert "EVIDENCE_STALE" in value.paper_eligibility_reasons


def test_crypto_projection_rejects_missing_fee_or_slippage_evidence() -> None:
    no_fees = projection("no-fees", fees=None)
    assert no_fees.paper_eligible is False
    assert "FEE_EVIDENCE_MISSING" in no_fees.paper_eligibility_reasons
    no_slippage = projection("no-slippage", slippage=None)
    assert no_slippage.paper_eligible is False
    assert "SLIPPAGE_EVIDENCE_MISSING" in no_slippage.paper_eligibility_reasons


def test_crypto_projection_rejects_missing_maximum_loss() -> None:
    value = projection("no-max-loss", maximum_loss=None)
    assert value.paper_eligible is False
    assert "MAXIMUM_LOSS_MISSING" in value.paper_eligibility_reasons


def test_crypto_projection_rejects_invalid_or_missing_required_capital() -> None:
    missing = projection("no-capital", capital=None)
    assert missing.paper_eligible is False
    assert "REQUIRED_CAPITAL_INVALID_OR_MISSING" in missing.paper_eligibility_reasons
    zero = projection("zero-capital", capital="0")
    assert zero.paper_eligible is False
    assert "REQUIRED_CAPITAL_INVALID_OR_MISSING" in zero.paper_eligibility_reasons


def test_crypto_projection_rejects_insufficient_liquidity() -> None:
    value = projection("thin", liquidity="INSUFFICIENT")
    assert value.paper_eligible is False
    assert "LIQUIDITY_NOT_SUFFICIENT" in value.paper_eligibility_reasons


def test_crypto_projection_rejects_source_policy_rejection() -> None:
    value = projection("policy-rejected", rejection_reasons=("SOURCE_POLICY_FAILED",))
    assert value.paper_eligible is False
    assert "SOURCE_POLICY_REJECTED" in value.paper_eligibility_reasons


def test_crypto_projection_rejects_disqualifying_warning_and_non_research_authority() -> None:
    warned = projection("disqualifying-warning", warnings=("LIQUIDITY_INSUFFICIENT",))
    assert warned.paper_eligible is False
    assert "DISQUALIFYING_WARNING_PRESENT" in warned.paper_eligibility_reasons
    authorized = projection("non-research-authority", authority="PAPER_ONLY")
    assert authorized.paper_eligible is False
    assert "SOURCE_AUTHORITY_NOT_RESEARCH_ONLY" in authorized.paper_eligibility_reasons


def test_crypto_projection_rejects_incomplete_evidence_lineage() -> None:
    value = projection("no-lineage", evidence_ids=())
    assert value.paper_eligible is False
    assert "EVIDENCE_LINEAGE_INCOMPLETE" in value.paper_eligibility_reasons


def test_crypto_paper_eligibility_reasons_never_inflate_source_details() -> None:
    """Regression guard: routing paper_eligible through construction must never
    start silently appending to the caller-supplied source_details tuple."""

    value = projection("crypto", comparable_value="-0.05", maximum_loss=None, capital=None)
    assert value.source_details == (("route", "USD"),)
