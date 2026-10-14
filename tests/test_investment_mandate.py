"""Domain proofs for Investment Mandate v1: durable/versioned mandate
construction, and deterministic, fail-closed evaluation of a retained
Opportunity against mandate + normalized portfolio truth. No database, no
network -- mirrors the existing offline ``tests/test_total_market_context.py``
and ``tests/test_cross_venue_allocation.py`` pattern.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import scripts.dev as dev
from apex.domain.investment_mandate import (
    AutonomyRequirement,
    InvestmentMandate,
    InvestmentMandateError,
    MandateDecisionState,
    evaluate_mandate,
)
from apex.domain.portfolio_state import (
    EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
    AccountKind,
    PortfolioAccountBalance,
    PortfolioPosition,
    PortfolioVenue,
    PositionExposure,
    UnavailablePortfolioSource,
    build_portfolio_snapshot,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import RiskFraction
from scripts.dev import parser

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)


def _mandate(**overrides: object) -> InvestmentMandate:
    values: dict[str, object] = {
        "version": 1,
        "previous_mandate_id": None,
        "effective_at": YESTERDAY,
        "created_at": YESTERDAY,
        "total_capital_allocated": "1000",
        "permitted_venues": ("COINBASE",),
        "permitted_market_families": ("CRYPTO_SPOT",),
        "disallowed_market_families": (),
        "maximum_aggregate_exposure_fraction": "0.5",
        "maximum_per_instrument_fraction": "0.5",
        "maximum_venue_concentration_fraction": "0.5",
        "maximum_market_family_concentration_fraction": "0.5",
        "maximum_capital_lockup_days": 30,
    }
    values.update(overrides)
    return InvestmentMandate.create(**values)  # type: ignore[arg-type]


def _opportunity(**overrides: object) -> SharedOpportunityProjection:
    values: dict[str, object] = {
        "projection_id": "proj-1",
        "venue": SharedVenue.CRYPTO,
        "source_record_type": "Test",
        "source_record_id": "src-1",
        "source_canonical_hash": "d" * 64,
        "strategy_or_method_id": "TEST_METHOD",
        "instrument_id": "BTC-USD",
        "product_or_market_id": "BTC-USD",
        "direction": "HYPOTHETICAL_LONG_SPOT_RESEARCH",
        "generated_at": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(hours=1),
        "evidence_class": "GENUINE_PUBLIC",
        "freshness_seconds": 300,
        "freshness_limit_seconds": 3600,
        "expected_gross_return": "0.02",
        "expected_net_return": "0.01",
        "comparable_value": "0.4",
        "comparable_value_identity": None,
        "uncertainty": "0.3",
        "maximum_loss": "50",
        "modeled_fees": "1",
        "modeled_slippage": "1",
        "costs_retained": True,
        "liquidity_state": "SUFFICIENT",
        "liquidity_limitations": (),
        "required_hypothetical_capital": "100",
        "maximum_sensible_research_allocation": "200",
        "source_maximum_allocation_fraction": "0.5",
        "expected_holding_days": 5,
        "estimated_lockup_days": 5,
        "supporting_evidence_ids": ("ev-1",),
        "source_warnings": (),
        "source_rejection_reasons": (),
        "source_authority": "NONE",
        "projection_limitations": (),
        "source_details": (),
    }
    values.update(overrides)
    return SharedOpportunityProjection.create(**values)  # type: ignore[arg-type]


def _empty_snapshot(**overrides: object):
    values: dict[str, object] = {
        "snapshot_id": "snap-1",
        "as_of": NOW,
        "created_at": NOW,
        "account_balances": (),
        "positions": (),
        "unavailable_sources": (),
    }
    values.update(overrides)
    return build_portfolio_snapshot(**values)  # type: ignore[arg-type]


def _position(**overrides: object) -> PortfolioPosition:
    values: dict[str, object] = {
        "venue": PortfolioVenue.COINBASE,
        "account_kind": AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        "account_id": "acct-1",
        "instrument_id": "BTC-USD",
        "market_family": "CRYPTO_SPOT",
        "exposure": PositionExposure.LONG,
        "raw_side": "SPOT_LONG",
        "quantity": "1",
        "cost_basis": "100",
        "opened_at": NOW,
        "as_of": NOW,
        "captured_at": NOW,
        "evidence_class": EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        "source_record_type": "Test",
        "source_record_id": "src-1",
        "source_canonical_hash": "b" * 64,
    }
    values.update(overrides)
    return PortfolioPosition.create(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# InvestmentMandate construction
# --------------------------------------------------------------------------


def test_mandate_is_deterministic_given_identical_inputs() -> None:
    first = _mandate()
    second = _mandate()
    assert first.canonical_hash == second.canonical_hash
    assert first.mandate_id == second.mandate_id


def test_mandate_initial_version_must_have_no_predecessor() -> None:
    with pytest.raises(InvestmentMandateError):
        _mandate(version=1, previous_mandate_id="some-id")


def test_mandate_revision_requires_predecessor() -> None:
    with pytest.raises(InvestmentMandateError):
        _mandate(version=2, previous_mandate_id=None)


def test_mandate_cannot_be_backdated_before_its_own_creation() -> None:
    with pytest.raises(InvestmentMandateError):
        _mandate(effective_at=YESTERDAY - timedelta(days=10), created_at=YESTERDAY)


def test_mandate_rejects_market_family_permitted_and_disallowed_conflict() -> None:
    with pytest.raises(InvestmentMandateError):
        _mandate(
            permitted_market_families=("CRYPTO_SPOT",),
            disallowed_market_families=("CRYPTO_SPOT",),
        )


def test_mandate_rejects_unknown_venue_and_market_family() -> None:
    with pytest.raises(InvestmentMandateError):
        _mandate(permitted_venues=("NOT_A_REAL_VENUE",))
    with pytest.raises(InvestmentMandateError):
        _mandate(permitted_market_families=("NOT_A_REAL_FAMILY",))


def test_mandate_rejects_undefined_risk_options_without_options_permitted() -> None:
    with pytest.raises(InvestmentMandateError):
        _mandate(options_permitted=False, options_defined_risk_only=False)


def test_mandate_accepts_risk_fraction_instances_directly() -> None:
    mandate = _mandate(maximum_aggregate_exposure_fraction=RiskFraction("0.25"))
    assert mandate.maximum_aggregate_exposure_fraction.value == Decimal("0.25")


def test_default_v1_mandate_permits_nothing_and_allocates_zero_capital() -> None:
    default = InvestmentMandate.default_v1(effective_at=YESTERDAY)
    assert default.total_capital_allocated == Decimal(0)
    assert default.permitted_venues == ()
    assert default.permitted_market_families == ()
    assert default.maximum_aggregate_exposure_fraction.value == Decimal(0)


def test_mandate_revision_lineage_chains_by_previous_mandate_id() -> None:
    v1 = _mandate(version=1, previous_mandate_id=None)
    v2 = _mandate(version=2, previous_mandate_id=v1.mandate_id, effective_at=NOW, created_at=NOW)
    assert v2.previous_mandate_id == v1.mandate_id
    assert v2.mandate_id != v1.mandate_id


# --------------------------------------------------------------------------
# evaluate_mandate: ALLOWED / CONSTRAINED / REJECTED
# --------------------------------------------------------------------------


def test_evaluate_allows_when_cash_and_limits_cover_required_capital() -> None:
    mandate = _mandate()
    # Give the account real cash so the opportunity's $100 requirement is fundable.
    balance = PortfolioAccountBalance.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id="acct-1",
        cash="500",
        reserved_cash="0",
        as_of=NOW,
        captured_at=NOW,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="Test",
        source_record_id="src-1",
        source_canonical_hash="a" * 64,
    )
    funded_snapshot = _empty_snapshot(account_balances=(balance,))
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=funded_snapshot,
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.ALLOWED
    assert result.permitted_allocation_amount == Decimal("100")
    assert result.reason_codes == ()
    assert not result.unknown_facts


def test_evaluate_rejects_when_no_cash_is_genuinely_known() -> None:
    mandate = _mandate()
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    # Zero accounts is a known $0, not an unknown -- correctly REJECTED for
    # insufficient (not unknown) cash.
    assert result.state is MandateDecisionState.REJECTED
    assert "MANDATE_HEADROOM_EXHAUSTED" in result.reason_codes


def test_evaluate_rejects_when_venue_not_permitted() -> None:
    mandate = _mandate(
        permitted_venues=("KALSHI",), permitted_market_families=("KALSHI_EVENT_CONTRACT",)
    )
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "VENUE_NOT_PERMITTED_BY_MANDATE" in result.reason_codes


def test_evaluate_securities_venue_rejects_honestly_never_crashes() -> None:
    """Securities Research -> Shared Opportunity Recurring Bridge v1
    regression: ``_venue_for_shared_venue``/``_market_family_for_shared_
    venue`` previously raised ``InvestmentMandateError`` for any venue other
    than CRYPTO/KALSHI, uncaught by ``evaluate_mandate``'s own caller
    (``adapters.operating_cycle_tasks``'s ``MANDATE_EVALUATION_REBUILD``
    branch has no try/except around this call) -- confirmed reachable
    against real retained production evidence the moment a SECURITIES
    opportunity first became ELIGIBLE. Fixed by mapping SECURITIES to the
    already-defined ``PortfolioVenue.ALPACA`` / ``MARKET_FAMILY_US_EQUITY_
    OPTION`` (the real Alpaca-backed evidence source and market family this
    bridge's only producer actually has) -- granting no new authority: the
    default mandate here permits neither, so this must land on a genuine,
    non-crashing REJECTED, exactly like an unpermitted CRYPTO/KALSHI
    opportunity already does."""

    mandate = _mandate()  # permits only COINBASE / CRYPTO_SPOT
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(
            venue=SharedVenue.SECURITIES,
            source_record_type="OptionStructureCandidate",
            instrument_id="security-spy-stable",
            product_or_market_id="options_structure_1",
            direction="CALL_DEBIT_VERTICAL",
            comparable_value=None,
        ),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert result.venue is PortfolioVenue.ALPACA
    assert result.market_family == "US_EQUITY_OPTION"
    assert "VENUE_NOT_PERMITTED_BY_MANDATE" in result.reason_codes
    assert "MARKET_FAMILY_NOT_PERMITTED_BY_MANDATE" in result.reason_codes
    assert result.authority == "NONE"


def test_evaluate_rejects_when_market_family_explicitly_disallowed() -> None:
    mandate = _mandate(
        permitted_market_families=(),
        disallowed_market_families=("CRYPTO_SPOT",),
    )
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "MARKET_FAMILY_EXPLICITLY_DISALLOWED_BY_MANDATE" in result.reason_codes


def test_evaluate_rejects_when_lockup_exceeds_mandate_maximum() -> None:
    mandate = _mandate(maximum_capital_lockup_days=1)
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(estimated_lockup_days=5),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "CAPITAL_LOCKUP_EXCEEDS_MANDATE_MAXIMUM" in result.reason_codes


def test_evaluate_rejects_when_liquidity_below_mandate_minimum() -> None:
    mandate = _mandate()
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(liquidity_state="INSUFFICIENT"),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "LIQUIDITY_BELOW_MANDATE_MINIMUM" in result.reason_codes


def test_evaluate_rejects_short_exposure_unless_permitted() -> None:
    mandate = _mandate(short_exposure_permitted=False)
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(direction="HYPOTHETICAL_SHORT_SPOT_RESEARCH"),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "SHORT_EXPOSURE_NOT_PERMITTED_BY_MANDATE" in result.reason_codes

    permissive = _mandate(short_exposure_permitted=True)
    allowed_short = evaluate_mandate(
        mandate=permissive,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(direction="HYPOTHETICAL_SHORT_SPOT_RESEARCH"),
        evaluated_at=NOW,
    )
    assert "SHORT_EXPOSURE_NOT_PERMITTED_BY_MANDATE" not in allowed_short.reason_codes


def test_evaluate_rejects_incomplete_opportunity_economics() -> None:
    mandate = _mandate()
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(costs_retained=False),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "OPPORTUNITY_ECONOMICS_INCOMPLETE_FOR_MANDATE_EVALUATION" in result.reason_codes
    assert "OPPORTUNITY_REQUIRED_CAPITAL_OR_MAXIMUM_LOSS_OR_COSTS" in result.unknown_facts


def test_evaluate_fails_closed_when_mandate_not_yet_effective() -> None:
    mandate = _mandate(effective_at=NOW + timedelta(days=1), created_at=NOW)
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "MANDATE_NOT_YET_EFFECTIVE" in result.reason_codes


def test_evaluate_fails_closed_on_stale_portfolio_snapshot() -> None:
    mandate = _mandate()
    stale_snapshot = _empty_snapshot(
        snapshot_id="snap-stale",
        as_of=NOW - timedelta(days=2),
        created_at=NOW - timedelta(days=2),
    )
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=stale_snapshot,
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "PORTFOLIO_SNAPSHOT_STALE" in result.reason_codes


def test_evaluate_fails_closed_on_unknown_existing_exposure_for_the_same_instrument() -> None:
    mandate = _mandate()
    unknown_position = _position(cost_basis=None, mark_value=None)
    snapshot = _empty_snapshot(positions=(unknown_position,))
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=snapshot,
        positions=(unknown_position,),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "EXISTING_INSTRUMENT_EXPOSURE_UNKNOWN" in result.reason_codes
    assert "INSTRUMENT_EXISTING_EXPOSURE" in result.unknown_facts


def test_evaluate_never_silently_treats_unknown_as_allowed() -> None:
    """A comprehensive fail-closed sweep: every unknown-safety-relevant-fact
    scenario above resolves to REJECTED, never ALLOWED."""
    mandate = _mandate()
    scenarios = (
        _empty_snapshot(),  # zero known cash
        _empty_snapshot(
            unavailable_sources=(
                UnavailablePortfolioSource(
                    PortfolioVenue.COINBASE,
                    AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
                    "CREDENTIALS_ABSENT",
                ),
            ),
        ),
    )
    for snapshot in scenarios:
        result = evaluate_mandate(
            mandate=mandate,
            portfolio=snapshot,
            positions=(),
            opportunity=_opportunity(),
            evaluated_at=NOW,
        )
        assert result.state is not MandateDecisionState.ALLOWED


# --------------------------------------------------------------------------
# Concentration boundary behavior
# --------------------------------------------------------------------------


def test_evaluate_constrains_allocation_exactly_at_the_concentration_boundary() -> None:
    """Existing exposure of 150 against a 25% x $1000 = $250 instrument cap
    leaves exactly $100 headroom -- exactly enough for a $100 requirement:
    ALLOWED, not CONSTRAINED."""
    mandate = _mandate(maximum_per_instrument_fraction="0.25")
    balance = PortfolioAccountBalance.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id="acct-1",
        cash="1000",
        reserved_cash="0",
        as_of=NOW,
        captured_at=NOW,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="Test",
        source_record_id="src-1",
        source_canonical_hash="a" * 64,
    )
    existing = _position(cost_basis="150")
    snapshot = _empty_snapshot(account_balances=(balance,), positions=(existing,))
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=snapshot,
        positions=(existing,),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.ALLOWED
    assert result.permitted_allocation_amount == Decimal("100")


def test_evaluate_rejects_when_concentration_headroom_is_fully_exhausted() -> None:
    mandate = _mandate(maximum_per_instrument_fraction="0.25")
    balance = PortfolioAccountBalance.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id="acct-1",
        cash="1000",
        reserved_cash="0",
        as_of=NOW,
        captured_at=NOW,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="Test",
        source_record_id="src-1",
        source_canonical_hash="a" * 64,
    )
    existing = _position(cost_basis="250")  # exactly at the $250 cap already
    snapshot = _empty_snapshot(account_balances=(balance,), positions=(existing,))
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=snapshot,
        positions=(existing,),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.REJECTED
    assert "MANDATE_HEADROOM_EXHAUSTED" in result.reason_codes


def test_evaluate_binding_constraint_is_the_tightest_cap() -> None:
    mandate = _mandate(
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_aggregate_exposure_fraction="0.02",
    )
    balance = PortfolioAccountBalance.create(
        venue=PortfolioVenue.COINBASE,
        account_kind=AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        account_id="acct-1",
        cash="1000",
        reserved_cash="0",
        as_of=NOW,
        captured_at=NOW,
        evidence_class=EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        source_record_type="Test",
        source_record_id="src-1",
        source_canonical_hash="a" * 64,
    )
    snapshot = _empty_snapshot(account_balances=(balance,))
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=snapshot,
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.state is MandateDecisionState.CONSTRAINED
    assert result.permitted_allocation_amount == Decimal("20")  # 1000 * 0.02
    binding = {check.name for check in result.constraints_evaluated if check.binding}
    assert "AGGREGATE_EXPOSURE" in binding
    assert "PER_INSTRUMENT_CONCENTRATION" not in binding


# --------------------------------------------------------------------------
# Determinism / replay / lineage
# --------------------------------------------------------------------------


def test_evaluate_is_deterministic_and_replay_stable() -> None:
    mandate = _mandate()
    snapshot = _empty_snapshot()
    opportunity = _opportunity()
    first = evaluate_mandate(
        mandate=mandate, portfolio=snapshot, positions=(), opportunity=opportunity, evaluated_at=NOW
    )
    second = evaluate_mandate(
        mandate=mandate, portfolio=snapshot, positions=(), opportunity=opportunity, evaluated_at=NOW
    )
    assert first.canonical_hash == second.canonical_hash
    assert first.evaluation_id == second.evaluation_id


def test_evaluate_rejects_incomplete_position_lineage() -> None:
    mandate = _mandate()
    unknown_position = _position()
    snapshot = _empty_snapshot(positions=(unknown_position,))
    with pytest.raises(InvestmentMandateError):
        evaluate_mandate(
            mandate=mandate,
            portfolio=snapshot,
            positions=(),  # missing the referenced position
            opportunity=_opportunity(),
            evaluated_at=NOW,
        )


def test_autonomy_requirement_is_recorded_but_grants_no_execution_path() -> None:
    mandate = _mandate(autonomy_requirement=AutonomyRequirement.AUTONOMOUS_WITHIN_LIMITS)
    assert mandate.autonomy_requirement is AutonomyRequirement.AUTONOMOUS_WITHIN_LIMITS
    # The evaluation result carries no order/reservation/execution authority
    # regardless of the mandate's declared autonomy preference.
    result = evaluate_mandate(
        mandate=mandate,
        portfolio=_empty_snapshot(),
        positions=(),
        opportunity=_opportunity(),
        evaluated_at=NOW,
    )
    assert result.authority == "NONE"
    assert "NOT_AN_ORDER" in result.warnings


# --------------------------------------------------------------------------
# CLI registration
# --------------------------------------------------------------------------


def test_investment_mandate_commands_parse_and_do_not_collide() -> None:
    create_args = parser().parse_args(
        ["investment-mandate-create", "--effective-at", "2026-08-10T00:00:00Z"]
    )
    assert create_args.command == "investment-mandate-create"
    inspect_args = parser().parse_args(["investment-mandate-inspect"])
    assert inspect_args.lineage_id is None
    evaluate_args = parser().parse_args(
        ["investment-mandate-evaluate", "--opportunity-id", "proj-1"]
    )
    assert evaluate_args.opportunity_id == "proj-1"

    source = inspect.getsource(dev.main)
    dispatch_index = source.index(
        'if arguments.command.startswith(("portfolio-snapshot-", "investment-mandate-")):'
    )
    preceding = source[:dispatch_index]
    assert 'startswith("investment-mandate' not in preceding
    assert '"investment-mandate-create"' not in preceding
    assert '"investment-mandate-evaluate"' not in preceding
