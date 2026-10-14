"""Domain proofs for Normalized Portfolio State v1: provider-neutral
account-balance/position normalization, conservative aggregate rollups,
and the pure per-vertical mapping functions. No database, no network --
mirrors the existing offline ``tests/test_total_market_context.py`` and
``tests/test_shared_opportunity.py`` pattern.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import scripts.dev as dev
from apex.domain.campaign import Campaign, CampaignPolicy, CampaignSnapshot
from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperAccountState,
    CryptoPaperPosition,
)
from apex.domain.external_paper import ExternalPaperBrokerEvidence
from apex.domain.kalshi_operating import (
    KalshiPaperAccount,
    KalshiPaperPosition,
    KalshiPaperState,
    KalshiPositionState,
    KalshiSide,
)
from apex.domain.portfolio_state import (
    EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
    EVIDENCE_CLASS_GENUINE_RETAINED_PROVIDER_EVIDENCE,
    AccountKind,
    PortfolioAccountBalance,
    PortfolioCompleteness,
    PortfolioPosition,
    PortfolioStateError,
    PortfolioVenue,
    PositionExposure,
    UnavailablePortfolioSource,
    balance_from_crypto_paper,
    balance_from_external_paper_evidence,
    balance_from_kalshi_paper,
    balance_from_securities_campaign,
    build_portfolio_snapshot,
    position_from_crypto_paper,
    position_from_external_paper_evidence,
    position_from_kalshi_paper,
    position_from_securities_paper,
)
from apex.domain.securities_paper import SecuritiesPositionState, SecuritiesPositionStatus
from apex.domain.values import ContentHash, Money, Quantity
from scripts.dev import parser

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _balance(**overrides: object) -> PortfolioAccountBalance:
    values: dict[str, object] = {
        "venue": PortfolioVenue.COINBASE,
        "account_kind": AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        "account_id": "acct-1",
        "cash": "500",
        "reserved_cash": "0",
        "as_of": NOW,
        "captured_at": NOW,
        "evidence_class": EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        "source_record_type": "Test",
        "source_record_id": "src-1",
        "source_canonical_hash": "a" * 64,
    }
    values.update(overrides)
    return PortfolioAccountBalance.create(**values)  # type: ignore[arg-type]


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
# PortfolioAccountBalance
# --------------------------------------------------------------------------


def test_balance_computes_available_cash_only_when_both_known() -> None:
    known = _balance(cash="500", reserved_cash="100")
    assert known.available_cash == Decimal("400")
    assert "available_cash" not in known.unavailable_fields

    unknown_reserved = _balance(cash="500", reserved_cash=None)
    assert unknown_reserved.available_cash is None
    assert "available_cash" in unknown_reserved.unavailable_fields
    assert "reserved_cash" in unknown_reserved.unavailable_fields
    assert "cash" not in unknown_reserved.unavailable_fields


def test_balance_lists_every_unknown_field_explicitly() -> None:
    balance = _balance(
        cash=None, reserved_cash=None, buying_power=None, equity=None, realized_pnl=None
    )
    assert set(balance.unavailable_fields) == {
        "cash",
        "reserved_cash",
        "available_cash",
        "buying_power",
        "equity",
        "realized_pnl",
    }


def test_balance_rejects_unknown_evidence_class() -> None:
    with pytest.raises(PortfolioStateError):
        _balance(evidence_class="NOT_A_REAL_EVIDENCE_CLASS")


def test_balance_is_deterministic_given_identical_inputs() -> None:
    first = _balance()
    second = _balance()
    assert first.canonical_hash == second.canonical_hash
    assert first.balance_id == second.balance_id


# --------------------------------------------------------------------------
# PortfolioPosition
# --------------------------------------------------------------------------


def test_position_exposure_value_prefers_mark_over_cost_basis() -> None:
    with_mark = _position(cost_basis="100", mark_value="120")
    assert with_mark.exposure_value == Decimal("120")
    assert with_mark.exposure_value_source == "MARK"

    cost_only = _position(cost_basis="100", mark_value=None)
    assert cost_only.exposure_value == Decimal("100")
    assert cost_only.exposure_value_source == "COST_BASIS"

    unknown = _position(cost_basis=None, mark_value=None)
    assert unknown.exposure_value is None
    assert unknown.exposure_value_source is None
    assert "exposure_value" in unknown.unavailable_fields


def test_position_rejects_opened_after_as_of() -> None:
    with pytest.raises(PortfolioStateError):
        _position(as_of=NOW, opened_at=NOW + timedelta(days=1))


def test_position_rejects_captured_before_as_of() -> None:
    with pytest.raises(PortfolioStateError):
        _position(as_of=NOW, captured_at=NOW - timedelta(seconds=1))


# --------------------------------------------------------------------------
# build_portfolio_snapshot
# --------------------------------------------------------------------------


def test_snapshot_is_deterministic_across_repeated_builds() -> None:
    balance = _balance()
    position = _position()
    first = build_portfolio_snapshot(
        snapshot_id="snap-1",
        as_of=NOW,
        created_at=NOW,
        account_balances=(balance,),
        positions=(position,),
        unavailable_sources=(),
    )
    second = build_portfolio_snapshot(
        snapshot_id="snap-1",
        as_of=NOW,
        created_at=NOW,
        account_balances=(balance,),
        positions=(position,),
        unavailable_sources=(),
    )
    assert first.canonical_hash == second.canonical_hash


def test_snapshot_completeness_reflects_unavailable_sources() -> None:
    complete = build_portfolio_snapshot(
        snapshot_id="snap-complete",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    assert complete.completeness is PortfolioCompleteness.COMPLETE
    assert complete.total_known_cash == Decimal(0)

    unavailable = (
        UnavailablePortfolioSource(
            PortfolioVenue.ALPACA, AccountKind.PROVIDER_PAPER_BROKER, "CREDENTIALS_ABSENT"
        ),
    )
    partial = build_portfolio_snapshot(
        snapshot_id="snap-partial",
        as_of=NOW,
        created_at=NOW,
        account_balances=(_balance(),),
        positions=(),
        unavailable_sources=unavailable,
    )
    assert partial.completeness is PortfolioCompleteness.PARTIAL_UNKNOWN_SOURCES
    assert partial.total_known_cash is None  # fail-closed: an unqueried source could hold more

    all_unknown = build_portfolio_snapshot(
        snapshot_id="snap-unknown",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=unavailable,
    )
    assert all_unknown.completeness is PortfolioCompleteness.ALL_SOURCES_UNKNOWN


def test_snapshot_total_known_position_value_requires_every_position_known() -> None:
    known = _position(cost_basis="100", mark_value=None)
    unknown = _position(
        account_id="acct-2",
        instrument_id="ETH-USD",
        cost_basis=None,
        mark_value=None,
        source_record_id="src-2",
    )
    snapshot = build_portfolio_snapshot(
        snapshot_id="snap-mixed",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(known, unknown),
        unavailable_sources=(),
    )
    assert snapshot.total_known_position_value is None
    assert unknown.position_id in snapshot.positions_with_unknown_exposure_value
    assert known.position_id not in snapshot.positions_with_unknown_exposure_value


def test_snapshot_exposure_rollups_sum_only_known_positions() -> None:
    btc = _position(cost_basis="300")
    eth = _position(
        account_id="acct-1",
        instrument_id="ETH-USD",
        cost_basis="200",
        source_record_id="src-2",
    )
    kalshi = _position(
        venue=PortfolioVenue.KALSHI,
        instrument_id="FOO-BAR",
        market_family="KALSHI_EVENT_CONTRACT",
        cost_basis="50",
        source_record_id="src-3",
    )
    snapshot = build_portfolio_snapshot(
        snapshot_id="snap-rollup",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(btc, eth, kalshi),
        unavailable_sources=(),
    )
    assert dict(snapshot.exposure_by_instrument) == {
        "BTC-USD": Decimal("300"),
        "ETH-USD": Decimal("200"),
        "FOO-BAR": Decimal("50"),
    }
    assert dict(snapshot.exposure_by_venue) == {"COINBASE": Decimal("500"), "KALSHI": Decimal("50")}
    assert dict(snapshot.exposure_by_market_family) == {
        "CRYPTO_SPOT": Decimal("500"),
        "KALSHI_EVENT_CONTRACT": Decimal("50"),
    }
    assert snapshot.total_known_portfolio_value == Decimal("550")


def test_snapshot_rejects_evidence_captured_after_snapshot_as_of() -> None:
    future_balance = _balance(as_of=NOW + timedelta(hours=1), captured_at=NOW + timedelta(hours=1))
    with pytest.raises(PortfolioStateError):
        build_portfolio_snapshot(
            snapshot_id="snap-future",
            as_of=NOW,
            created_at=NOW + timedelta(hours=1),
            account_balances=(future_balance,),
            positions=(),
            unavailable_sources=(),
        )


def test_snapshot_rejects_duplicate_balance_or_position_ids() -> None:
    balance = _balance()
    with pytest.raises(PortfolioStateError):
        build_portfolio_snapshot(
            snapshot_id="snap-dup",
            as_of=NOW,
            created_at=NOW,
            account_balances=(balance, balance),
            positions=(),
            unavailable_sources=(),
        )


# --------------------------------------------------------------------------
# Per-vertical pure mapping functions
# --------------------------------------------------------------------------


def test_crypto_paper_mapping_preserves_cash_and_cost_basis() -> None:
    account = CryptoPaperAccount.create(account_id="crypto-1", opening_cash="1000", created_at=NOW)
    state = CryptoPaperAccountState.opening(account=account, event_id="evt-1")
    balance = balance_from_crypto_paper(account=account, state=state, as_of=NOW, captured_at=NOW)
    assert balance.venue is PortfolioVenue.COINBASE
    assert balance.cash == Decimal("1000")
    assert balance.evidence_class == EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER

    position = CryptoPaperPosition(
        position_id="pos-1",
        account_id="crypto-1",
        product_id="BTC-USD",
        base_asset="BTC",
        base_quantity=Decimal("0.5"),
        average_cost=Decimal("50000"),
        cumulative_cost=Decimal("25000"),
        cumulative_sale_proceeds=Decimal("0"),
        realized_paper_pnl=Decimal("10"),
        modeled_fees=Decimal("1"),
        state="OPEN",
        version=1,
        canonical_hash="c" * 64,
    )
    mapped = position_from_crypto_paper(position=position, as_of=NOW, captured_at=NOW)
    assert mapped.instrument_id == "BTC-USD"
    assert mapped.cost_basis == Decimal("25000")
    assert mapped.realized_pnl == Decimal("10")
    assert mapped.market_family == "CRYPTO_SPOT"


def test_kalshi_paper_mapping_preserves_raw_side_and_contracts() -> None:
    account = KalshiPaperAccount.create(account_id="kalshi-1", opening_cash="500", created_at=NOW)
    state = KalshiPaperState.opening(account)
    balance = balance_from_kalshi_paper(state=state, as_of=NOW, captured_at=NOW)
    assert balance.venue is PortfolioVenue.KALSHI
    assert balance.cash == Decimal("500")

    position = KalshiPaperPosition(
        position_id="kpos-1",
        market_ticker="FOO-BAR",
        event_ticker="FOO",
        rule_hash="d" * 64,
        side=KalshiSide.NO,
        contracts=25,
        cost_basis=Decimal("60"),
        fees=Decimal("2"),
        state=KalshiPositionState.OPEN,
        opened_at=NOW,
        settled_at=None,
        canonical_hash="e" * 64,
    )
    mapped = position_from_kalshi_paper(
        position=position, account_id="kalshi-1", as_of=NOW, captured_at=NOW
    )
    assert mapped.raw_side == "NO"
    assert mapped.exposure is PositionExposure.LONG  # holding NO is long-the-NO-contract, not short
    assert mapped.quantity == Decimal("25")
    assert mapped.market_family == "KALSHI_EVENT_CONTRACT"


def test_securities_campaign_mapping_uses_ledger_equity_and_reservations() -> None:
    policy = CampaignPolicy(
        policy_version_id="pv1",
        profile="test",
        requested_risk_fraction="0.1",
        max_trade_loss_fraction="0.1",
        max_open_loss_fraction="0.1",
        min_synthetic_edge="0.01",
        max_spread="0.05",
        max_capital_lockup_seconds=3600,
        content_hash="f" * 64,
    )
    campaign = Campaign(
        campaign_id="camp-1",
        contribution=Money("1000"),
        authorized_loss=Money("100"),
        policy=policy,
    )
    snapshot = CampaignSnapshot(
        campaign=campaign,
        ledger_equity=Money("900"),
        settled_cash=Money("800"),
        active_reservations=Money("50"),
    )
    balance = balance_from_securities_campaign(
        campaign_id="camp-1", snapshot=snapshot, as_of=NOW, captured_at=NOW
    )
    assert balance.venue is PortfolioVenue.APEX_MANAGED_SECURITIES_DATA
    assert balance.cash == Decimal("800")
    assert balance.reserved_cash == Decimal("50")
    assert balance.equity == Decimal("900")

    position_state = SecuritiesPositionState(
        position_id="spos-1",
        run_id="run-1",
        campaign_id="camp-1",
        instrument_id="AAPL",
        current_symbol="AAPL",
        open_quantity=Quantity("10", "1"),
        average_cost=Decimal("150"),
        cumulative_purchase_cost=Money("1500"),
        cumulative_sale_proceeds=Money("0"),
        realized_pnl=Decimal("0"),
        unrealized_pnl=Decimal("50"),
        last_mark_price=Decimal("155"),
        last_mark_at=NOW,
        state=SecuritiesPositionStatus.OPEN,
        originating_fill_id="fill-1",
        row_version=1,
        canonical_hash=ContentHash("0" * 64),
    )
    mapped = position_from_securities_paper(position=position_state, as_of=NOW, captured_at=NOW)
    assert mapped.venue is PortfolioVenue.APEX_MANAGED_SECURITIES_DATA
    assert mapped.cost_basis == Decimal("1500")
    assert mapped.mark_value == Decimal("1550")
    assert mapped.unrealized_pnl == Decimal("50")
    assert mapped.market_family == "US_EQUITY"


def test_external_paper_balance_mapping_returns_none_when_no_relevant_field_present() -> None:
    evidence = ExternalPaperBrokerEvidence.create(
        instruction_id=None,
        endpoint_identity="PAPER:GET:/v2/account",
        observation_type="ACCOUNT_SNAPSHOT",
        observed_at=NOW,
        broker_reported_at=None,
        request_id="req-1",
        sanitized_payload={"status": "ACTIVE"},
    )
    assert (
        balance_from_external_paper_evidence(
            evidence, account_id="alpaca-1", as_of=NOW, captured_at=NOW
        )
        is None
    )


def test_external_paper_balance_mapping_parses_genuine_fields() -> None:
    evidence = ExternalPaperBrokerEvidence.create(
        instruction_id=None,
        endpoint_identity="PAPER:GET:/v2/account",
        observation_type="ACCOUNT_SNAPSHOT",
        observed_at=NOW,
        broker_reported_at=None,
        request_id="req-1",
        sanitized_payload={"cash": "123.45", "buying_power": "246.90", "status": "ACTIVE"},
    )
    balance = balance_from_external_paper_evidence(
        evidence, account_id="alpaca-1", as_of=NOW, captured_at=NOW
    )
    assert balance is not None
    assert balance.venue is PortfolioVenue.ALPACA
    assert balance.account_kind is AccountKind.PROVIDER_PAPER_BROKER
    assert balance.cash == Decimal("123.45")
    assert balance.evidence_class == EVIDENCE_CLASS_GENUINE_RETAINED_PROVIDER_EVIDENCE
    assert balance.equity is None
    assert "equity" in balance.unavailable_fields


def test_external_paper_position_mapping_returns_none_without_symbol() -> None:
    evidence = ExternalPaperBrokerEvidence.create(
        instruction_id=None,
        endpoint_identity="PAPER:GET:/v2/positions",
        observation_type="POSITION_SNAPSHOT_ITEM",
        observed_at=NOW,
        broker_reported_at=None,
        request_id="req-2",
        sanitized_payload={"qty": "10"},
    )
    assert (
        position_from_external_paper_evidence(
            evidence, account_id="alpaca-1", as_of=NOW, captured_at=NOW
        )
        is None
    )


def test_external_paper_position_mapping_detects_short_side() -> None:
    evidence = ExternalPaperBrokerEvidence.create(
        instruction_id=None,
        endpoint_identity="PAPER:GET:/v2/positions",
        observation_type="POSITION_SNAPSHOT_ITEM",
        observed_at=NOW,
        broker_reported_at=None,
        request_id="req-3",
        sanitized_payload={"symbol": "AAPL", "qty": "-10", "side": "short", "market_value": "1750"},
    )
    position = position_from_external_paper_evidence(
        evidence, account_id="alpaca-1", as_of=NOW, captured_at=NOW
    )
    assert position is not None
    assert position.exposure is PositionExposure.SHORT
    assert position.quantity == Decimal("10")  # magnitude only; sign is carried by `exposure`


# --------------------------------------------------------------------------
# CLI registration -- mirrors tests/test_total_market_context.py's own
# final check.
# --------------------------------------------------------------------------


def test_portfolio_snapshot_commands_parse_and_do_not_collide() -> None:
    build_args = parser().parse_args(["portfolio-snapshot-build"])
    assert build_args.command == "portfolio-snapshot-build"
    inspect_args = parser().parse_args(["portfolio-snapshot-inspect", "--snapshot-id", "snap-1"])
    assert inspect_args.snapshot_id == "snap-1"

    source = inspect.getsource(dev.main)
    dispatch_index = source.index(
        'if arguments.command.startswith(("portfolio-snapshot-", "investment-mandate-")):'
    )
    preceding = source[:dispatch_index]
    assert 'startswith("portfolio-snapshot' not in preceding
    assert '"portfolio-snapshot-build"' not in preceding
    assert '"portfolio-snapshot-inspect"' not in preceding
