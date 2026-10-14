"""PAPER Operations & Observability v1: regression coverage for the known
misleading Operator Console state where ``strategy_library()`` hardcoded
``paper_eligible: False`` / ``paper_ready_count: 0`` for every method
regardless of the real, already-computed
``SharedOpportunityProjection.paper_eligible`` field. Proves both the
positive (genuinely eligible) and negative (genuinely ineligible) cases, and
the honest idle/empty state when no shared Opportunity set is retained at
all -- a fix that merely flipped the hardcode to always-True would fail the
negative and empty-state assertions below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.application.shared_opportunity import MaterializeSharedOpportunitySet
from apex.domain.crypto_universe import (
    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _projection(
    *, identity: str, method_id: str, liquidity_state: str
) -> SharedOpportunityProjection:
    """Mirrors tests/persistence/test_postgres_shared_opportunity.py's own
    ``_projection`` fixture shape exactly; ``liquidity_state`` is the one
    field varied here because ``domain.shared_opportunity.
    _crypto_paper_readiness`` gates on it directly (``LIQUIDITY_NOT_
    SUFFICIENT`` unless exactly "SUFFICIENT") -- every other input is
    already genuinely PAPER-permitting (HISTORICAL_PUBLIC evidence, fresh,
    costs retained, real fee/slippage/loss/capital evidence, NONE source
    authority, no disqualifying warnings)."""

    return SharedOpportunityProjection.create(
        projection_id=f"paper-eligibility-{identity}",
        venue=SharedVenue.CRYPTO,
        source_record_type="FixtureOpportunity",
        source_record_id=f"source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id=method_id,
        instrument_id="BTC-USD",
        product_or_market_id="BTC-USD",
        direction="HYPOTHETICAL_RESEARCH",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=10),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=10,
        freshness_limit_seconds=300,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value="0.10",
        comparable_value_identity="FIXTURE_MEASURE",
        uncertainty="0.10",
        maximum_loss="10",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state=liquidity_state,
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="300",
        source_maximum_allocation_fraction=None,
        expected_holding_days=7,
        estimated_lockup_days=7,
        supporting_evidence_ids=(f"evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("FIXTURE_ONLY",),
        source_details=(("venue_semantics", "CRYPTO"),),
    )


class _Source:
    def __init__(self, crypto: tuple[SharedOpportunityProjection, ...]) -> None:
        self._crypto = crypto

    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return self._crypto

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return ()

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        return ()


@pytest.mark.postgres
def test_strategy_library_paper_eligible_reflects_real_shared_opportunity_state(
    postgres_engine: sa.Engine,
) -> None:
    eligible = _projection(
        identity="eligible",
        method_id=CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
        liquidity_state="SUFFICIENT",
    )
    ineligible = _projection(
        identity="ineligible",
        method_id=CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
        liquidity_state="INSUFFICIENT",
    )
    # Prove the fixture itself -- not just the read-model -- genuinely
    # exercises both sides of the real domain gate.
    assert eligible.paper_eligible is True
    assert ineligible.paper_eligible is False

    shared_repository = PostgresSharedOpportunityRepository(postgres_engine)
    MaterializeSharedOpportunitySet(shared_repository, _Source((eligible, ineligible))).execute(
        opportunity_set_id="paper-eligibility-set",
        crypto_pulse_id="paper-eligibility-pulse",
        kalshi_evaluation_ids=(),
        as_of=NOW,
        idempotency_key="paper-eligibility-set-key",
    )

    library = {
        item["method_id"]: item
        for item in RetainedSharedOpportunitySource(postgres_engine).strategy_library()
    }

    # This is the exact known-misleading pair PAPER Operations &
    # Observability v1 fixes: both fields were hardcoded False/0 for every
    # method regardless of real retained state.
    assert library[CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1]["paper_eligible"] is True
    assert library[CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1]["paper_ready_count"] == 1
    assert library[CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1]["paper_eligible"] is False
    assert library[CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1]["paper_ready_count"] == 0


@pytest.mark.postgres
def test_strategy_library_paper_eligible_honestly_idle_with_no_retained_shared_set(
    postgres_engine: sa.Engine,
) -> None:
    """A genuinely empty state (no MaterializeSharedOpportunitySet ever run)
    must show False/0 for every method -- not crash, and not silently keep
    an old hardcoded True by accident."""

    library = RetainedSharedOpportunitySource(postgres_engine).strategy_library()
    assert library
    for item in library:
        assert item["paper_eligible"] is False, item["method_id"]
        assert item["paper_ready_count"] == 0, item["method_id"]
