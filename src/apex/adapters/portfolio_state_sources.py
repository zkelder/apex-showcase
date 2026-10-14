"""Read-only composition of existing PAPER repositories into ``PortfolioStateSource``.

Implements ``application.portfolio_state.PortfolioStateSource`` by
composing the existing Crypto Spot PAPER, Kalshi PAPER, and Securities
internal PAPER repositories.  Kept separate from
``adapters.portfolio_state_postgres`` (which owns the new normalized
tables and is imported by ``adapters.postgres`` for table registration)
because some of these older per-vertical repositories themselves import
from ``adapters.postgres`` -- composing them in the same module that
``adapters.postgres`` imports would create a circular import.  No
provider client, credential, or network capability lives here; every read
is against already-persisted evidence.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.securities_paper_postgres import (
    PostgresSecuritiesPaperCampaignBoundary,
    PostgresSecuritiesPaperRepository,
)
from apex.application.securities_paper import SecuritiesPaperApplicationError
from apex.domain.campaign import CampaignSnapshot
from apex.domain.crypto_paper import CryptoPaperState
from apex.domain.kalshi_operating import KalshiPaperState
from apex.domain.securities_paper import SecuritiesPositionState


class PostgresPortfolioStateSource:
    """Implements ``application.portfolio_state.PortfolioStateSource``."""

    def __init__(self, engine: Engine) -> None:
        self._crypto = PostgresCryptoPaperRepository(engine)
        self._kalshi = PostgresKalshiOperatingRepository(engine)
        self._securities = PostgresSecuritiesPaperRepository(engine)
        self._securities_campaign = PostgresSecuritiesPaperCampaignBoundary(engine)

    def crypto_paper_state(self, account_id: str) -> CryptoPaperState | None:
        return self._crypto.state(account_id)

    def kalshi_paper_state(self, account_id: str) -> KalshiPaperState | None:
        return self._kalshi.state(account_id)

    def securities_paper_positions(self, campaign_id: str) -> tuple[SecuritiesPositionState, ...]:
        return self._securities.positions(campaign_id)

    def securities_campaign_snapshot(self, campaign_id: str) -> CampaignSnapshot | None:
        try:
            return self._securities_campaign.snapshot(campaign_id)
        except SecuritiesPaperApplicationError:
            return None
