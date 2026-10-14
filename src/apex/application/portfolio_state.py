"""Idempotent orchestration for normalized portfolio-state snapshots.

Composes the existing, already-retained per-vertical PAPER account/position
evidence (Crypto Spot PAPER, Kalshi PAPER, Securities internal PAPER) and,
optionally, already-retrieved genuine External PAPER (Alpaca) broker
evidence, into one provider-neutral ``PortfolioSnapshot`` via the pure
domain builder.  This module performs no provider I/O itself: crypto/
Kalshi/Securities evidence is read from already-persisted repositories, and
External PAPER evidence -- if any -- must be supplied by the caller (e.g.
an operator-invoked CLI command that separately performed the existing
read-only ``AlpacaPaperBroker.account()``/``.positions()`` calls).  Nothing
here creates a reservation, order, PAPER submission, or LIVE authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from apex.domain.campaign import CampaignSnapshot
from apex.domain.crypto_paper import CryptoPaperState
from apex.domain.external_paper import ExternalPaperBrokerEvidence
from apex.domain.kalshi_operating import KalshiPaperState, KalshiPositionState
from apex.domain.portfolio_state import (
    AccountKind,
    PortfolioAccountBalance,
    PortfolioPosition,
    PortfolioSnapshot,
    PortfolioVenue,
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


class PortfolioStateSource(Protocol):
    def crypto_paper_state(self, account_id: str) -> CryptoPaperState | None: ...
    def kalshi_paper_state(self, account_id: str) -> KalshiPaperState | None: ...
    def securities_paper_positions(
        self, campaign_id: str
    ) -> tuple[SecuritiesPositionState, ...]: ...
    def securities_campaign_snapshot(self, campaign_id: str) -> CampaignSnapshot | None: ...


class PortfolioStateRepository(Protocol):
    def portfolio_snapshot(self, snapshot_id: str) -> PortfolioSnapshot | None: ...
    def portfolio_account_balances(
        self, snapshot_id: str
    ) -> tuple[PortfolioAccountBalance, ...]: ...
    def portfolio_positions(self, snapshot_id: str) -> tuple[PortfolioPosition, ...]: ...
    def save_portfolio_snapshot(
        self,
        *,
        idempotency_key: str,
        snapshot: PortfolioSnapshot,
        balances: tuple[PortfolioAccountBalance, ...],
        positions: tuple[PortfolioPosition, ...],
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExternalPaperAccountEvidence:
    """Caller-supplied, already-retrieved genuine Alpaca broker evidence.

    This service never performs a provider read itself -- the caller is
    responsible for having already exercised the existing, separately
    governed ``AlpacaPaperBroker.account()``/``.positions()`` read-only
    paths (or for re-supplying previously retained evidence rows) before
    building a snapshot that should include External PAPER truth.
    """

    account_id: str
    account_evidence: ExternalPaperBrokerEvidence | None = None
    position_evidence: tuple[ExternalPaperBrokerEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class PortfolioSnapshotResult:
    snapshot: PortfolioSnapshot
    account_balances: tuple[PortfolioAccountBalance, ...]
    positions: tuple[PortfolioPosition, ...]
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class PortfolioSourceSelection:
    """Which already-retained accounts/campaigns to compose into a snapshot.

    Nothing here is auto-discovered across verticals: the caller states
    exactly which accounts/campaigns to include, so a missing selection is
    an explicit, honest omission rather than a silent, possibly-incomplete
    scan.
    """

    crypto_account_ids: tuple[str, ...] = ()
    kalshi_account_ids: tuple[str, ...] = ()
    securities_campaign_ids: tuple[str, ...] = ()
    external_paper_accounts: tuple[ExternalPaperAccountEvidence, ...] = field(default_factory=tuple)


class BuildPortfolioSnapshot:
    def __init__(self, repository: PortfolioStateRepository, source: PortfolioStateSource) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        snapshot_id: str,
        as_of: datetime,
        created_at: datetime,
        idempotency_key: str,
        selection: PortfolioSourceSelection,
    ) -> PortfolioSnapshotResult:
        balances: list[PortfolioAccountBalance] = []
        positions: list[PortfolioPosition] = []
        unavailable: list[UnavailablePortfolioSource] = []

        for account_id in selection.crypto_account_ids:
            state = self._source.crypto_paper_state(account_id)
            if state is None:
                unavailable.append(
                    UnavailablePortfolioSource(
                        PortfolioVenue.COINBASE,
                        AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
                        f"CRYPTO_PAPER_ACCOUNT_NOT_FOUND:{account_id}",
                    )
                )
                continue
            balances.append(
                balance_from_crypto_paper(
                    account=state.account,
                    state=state.account_state,
                    as_of=as_of,
                    captured_at=created_at,
                )
            )
            for position in state.positions:
                if position.state != "OPEN":
                    continue
                positions.append(
                    position_from_crypto_paper(
                        position=position, as_of=as_of, captured_at=created_at
                    )
                )

        for account_id in selection.kalshi_account_ids:
            kalshi_state = self._source.kalshi_paper_state(account_id)
            if kalshi_state is None:
                unavailable.append(
                    UnavailablePortfolioSource(
                        PortfolioVenue.KALSHI,
                        AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
                        f"KALSHI_PAPER_ACCOUNT_NOT_FOUND:{account_id}",
                    )
                )
                continue
            balances.append(
                balance_from_kalshi_paper(state=kalshi_state, as_of=as_of, captured_at=created_at)
            )
            for kalshi_position in kalshi_state.positions:
                if kalshi_position.state is not KalshiPositionState.OPEN:
                    continue
                positions.append(
                    position_from_kalshi_paper(
                        position=kalshi_position,
                        account_id=kalshi_state.account.account_id,
                        as_of=as_of,
                        captured_at=created_at,
                    )
                )

        for campaign_id in selection.securities_campaign_ids:
            campaign_snapshot = self._source.securities_campaign_snapshot(campaign_id)
            if campaign_snapshot is None:
                unavailable.append(
                    UnavailablePortfolioSource(
                        PortfolioVenue.APEX_MANAGED_SECURITIES_DATA,
                        AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
                        f"SECURITIES_CAMPAIGN_NOT_FOUND:{campaign_id}",
                    )
                )
                continue
            balances.append(
                balance_from_securities_campaign(
                    campaign_id=campaign_id,
                    snapshot=campaign_snapshot,
                    as_of=as_of,
                    captured_at=created_at,
                )
            )
            for securities_position in self._source.securities_paper_positions(campaign_id):
                if securities_position.state is SecuritiesPositionStatus.CLOSED:
                    continue
                positions.append(
                    position_from_securities_paper(
                        position=securities_position, as_of=as_of, captured_at=created_at
                    )
                )

        for entry in selection.external_paper_accounts:
            if entry.account_evidence is None:
                unavailable.append(
                    UnavailablePortfolioSource(
                        PortfolioVenue.ALPACA,
                        AccountKind.PROVIDER_PAPER_BROKER,
                        f"EXTERNAL_PAPER_ACCOUNT_EVIDENCE_NOT_SUPPLIED:{entry.account_id}",
                    )
                )
            else:
                balance = balance_from_external_paper_evidence(
                    entry.account_evidence,
                    account_id=entry.account_id,
                    as_of=as_of,
                    captured_at=created_at,
                )
                if balance is None:
                    unavailable.append(
                        UnavailablePortfolioSource(
                            PortfolioVenue.ALPACA,
                            AccountKind.PROVIDER_PAPER_BROKER,
                            f"EXTERNAL_PAPER_ACCOUNT_EVIDENCE_UNPARSEABLE:{entry.account_id}",
                        )
                    )
                else:
                    balances.append(balance)
            for position_evidence in entry.position_evidence:
                mapped_position = position_from_external_paper_evidence(
                    position_evidence,
                    account_id=entry.account_id,
                    as_of=as_of,
                    captured_at=created_at,
                )
                if mapped_position is not None:
                    positions.append(mapped_position)

        snapshot = build_portfolio_snapshot(
            snapshot_id=snapshot_id,
            as_of=as_of,
            created_at=created_at,
            account_balances=tuple(balances),
            positions=tuple(positions),
            unavailable_sources=tuple(unavailable),
        )
        replay = self._repository.save_portfolio_snapshot(
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            balances=tuple(balances),
            positions=tuple(positions),
        )
        return PortfolioSnapshotResult(snapshot, tuple(balances), tuple(positions), replay)
