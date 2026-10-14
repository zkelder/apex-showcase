from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.shared_opportunity_postgres import (
    PostgresSharedOpportunityRepository,
)
from apex.application.shared_opportunity import (
    MaterializeSharedOpportunitySet,
    ProposeSharedResearchAllocation,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest

NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _projection(identity: str, venue: SharedVenue) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"postgres-shared-{identity}",
        venue=venue,
        source_record_type="FixtureOpportunity",
        source_record_id=f"source-{identity}",
        source_canonical_hash=content_digest((identity,)),
        strategy_or_method_id="SHARED_FIXTURE_V1",
        instrument_id=identity,
        product_or_market_id=f"{identity}-market",
        direction="HYPOTHETICAL_RESEARCH",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=10),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=10,
        freshness_limit_seconds=300,
        expected_gross_return=None,
        expected_net_return=None,
        comparable_value="0.10" if venue is SharedVenue.CRYPTO else "0.05",
        comparable_value_identity="FIXTURE_MEASURE",
        uncertainty="0.10",
        maximum_loss="10",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=("FIXTURE",),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="300",
        source_maximum_allocation_fraction=None,
        expected_holding_days=7,
        estimated_lockup_days=7,
        supporting_evidence_ids=(f"evidence-{identity}",),
        source_warnings=("FIXTURE_ONLY",),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=("FIXTURE_ONLY",),
        source_details=(("venue_semantics", venue.value),),
    )


class _Source:
    crypto = (_projection("crypto", SharedVenue.CRYPTO),)
    kalshi = (_projection("kalshi", SharedVenue.KALSHI),)

    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        assert pulse_id == "exact-pulse"
        assert as_of == NOW
        return self.crypto

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        assert evaluation_ids == ("exact-evaluation",)
        assert as_of == NOW
        return self.kalshi

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        return ()


@pytest.mark.postgres
def test_shared_opportunity_is_immutable_idempotent_authority_free_and_guarded(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresSharedOpportunityRepository(postgres_engine)
    before = {}
    with postgres_engine.connect() as connection:
        for table in (
            '"order"',
            "position",
            "risk_reservation",
            "execution_event",
            "ledger_journal",
            "crypto_research_campaign",
            "crypto_holdout_event",
        ):
            before[table] = int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)

    service = MaterializeSharedOpportunitySet(repository, _Source())
    first = service.execute(
        opportunity_set_id="postgres-shared-set",
        crypto_pulse_id="exact-pulse",
        kalshi_evaluation_ids=("exact-evaluation",),
        as_of=NOW,
        idempotency_key="postgres-shared-set-key",
    )
    replay = service.execute(
        opportunity_set_id="postgres-shared-set",
        crypto_pulse_id="exact-pulse",
        kalshi_evaluation_ids=("exact-evaluation",),
        as_of=NOW,
        idempotency_key="postgres-shared-set-key",
    )
    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert replay.opportunity_set == first.opportunity_set
    assert repository.opportunity_set("postgres-shared-set") == first.opportunity_set
    assert repository.priorities("postgres-shared-set") == first.priorities
    assert repository.eligibility_decisions("postgres-shared-set") == first.decisions
    assert repository.explanations("postgres-shared-set") == first.explanations

    allocation_service = ProposeSharedResearchAllocation(repository)
    allocation = allocation_service.execute(
        proposal_id="postgres-shared-allocation",
        opportunity_set_id="postgres-shared-set",
        idempotency_key="postgres-shared-allocation-key",
    )
    allocation_replay = allocation_service.execute(
        proposal_id="postgres-shared-allocation",
        opportunity_set_id="postgres-shared-set",
        idempotency_key="postgres-shared-allocation-key",
    )
    assert not allocation.idempotent_replay
    assert allocation_replay.idempotent_replay
    assert allocation.proposal.hypothetical_budget == Decimal("1000")
    assert allocation.proposal.authority == "NONE"
    assert not allocation.proposal.reservation
    assert not allocation.proposal.order

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE shared_opportunity_projection SET authority = 'PAPER' "
                "WHERE projection_id = 'postgres-shared-crypto'"
            )
        )

    with postgres_engine.connect() as connection:
        for table, count in before.items():
            assert int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0) == count
        with pytest.raises(
            Exception,
            match="cannot downgrade 0058 with retained shared Opportunity evidence",
        ):
            command.downgrade(_config(connection), "0057_crypto_market_pulse")
        connection.rollback()
        # This long a walk (head all the way down to 0058's guard) never
        # observably commits any intermediate step on this connection: the
        # final ``connection.rollback()`` reverts the whole attempt back to
        # head, not to some step just above 0058 -- proved directly by the
        # migration log emitted during this test (every step from head down
        # to 0058 logs as "Running downgrade", none of it survives the
        # rollback). Verified empirically after adding migration 0095
        # (Counterfactual Decision Classification & Interpretation
        # Versioning v1) directly above 0094.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


@pytest.mark.postgres
def test_crypto_paper_eligibility_round_trips_exactly_through_postgres(
    postgres_engine: sa.Engine,
) -> None:
    """The canonical crypto paper_eligible computation must be reproduced
    identically on reload -- never silently revert to a hardcoded default --
    while a Kalshi projection must round-trip as paper_eligible=False, since
    Kalshi never reads this field."""

    repository = PostgresSharedOpportunityRepository(postgres_engine)
    eligible = _projection("eligible-round-trip", SharedVenue.CRYPTO)
    kalshi = _projection("kalshi-round-trip", SharedVenue.KALSHI)
    assert eligible.paper_eligible is True
    assert eligible.paper_eligibility_reasons == ("PAPER_ELIGIBLE_UNDER_CANONICAL_SHARED_POLICY",)
    assert kalshi.paper_eligible is False

    class _RoundTripSource:
        def crypto_projections(
            self, *, pulse_id: str, as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return (eligible,)

        def kalshi_projections(
            self, *, evaluation_ids: tuple[str, ...], as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return (kalshi,)

        def securities_projections(
            self, *, as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return ()

    MaterializeSharedOpportunitySet(repository, _RoundTripSource()).execute(
        opportunity_set_id="postgres-shared-round-trip-set",
        crypto_pulse_id="exact-pulse",
        kalshi_evaluation_ids=("exact-evaluation",),
        as_of=NOW,
        idempotency_key="postgres-shared-round-trip-key",
    )

    reloaded_eligible = repository.projection(eligible.projection_id)
    reloaded_kalshi = repository.projection(kalshi.projection_id)
    assert reloaded_eligible is not None
    assert reloaded_kalshi is not None
    assert reloaded_eligible.paper_eligible is True
    assert reloaded_eligible.paper_eligibility_reasons == eligible.paper_eligibility_reasons
    assert reloaded_kalshi.paper_eligible is False
    assert reloaded_kalshi.paper_eligibility_reasons == kalshi.paper_eligibility_reasons


# --------------------------------------------------------------------------
# Operator Console Follow-Up Performance v1: ``projections_by_id`` -- one
# batched ``WHERE ... IN (...)`` read replacing the one-``projection()``-
# call-per-decision N+1 the ``/followups`` route previously drove through
# ``PlanFollowUpObservations`` (measured: 521 individual round trips at
# current production scale). ``projection()`` itself is unchanged.
# --------------------------------------------------------------------------


@pytest.mark.postgres
def test_projections_by_id_returns_a_batched_map_matching_individual_lookups(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresSharedOpportunityRepository(postgres_engine)
    eligible = _projection("batched-a", SharedVenue.CRYPTO)
    kalshi = _projection("batched-b", SharedVenue.KALSHI)

    class _BatchSource:
        def crypto_projections(
            self, *, pulse_id: str, as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return (eligible,)

        def kalshi_projections(
            self, *, evaluation_ids: tuple[str, ...], as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return (kalshi,)

        def securities_projections(
            self, *, as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return ()

    MaterializeSharedOpportunitySet(repository, _BatchSource()).execute(
        opportunity_set_id="postgres-shared-batch-set",
        crypto_pulse_id="exact-pulse",
        kalshi_evaluation_ids=("exact-evaluation",),
        as_of=NOW,
        idempotency_key="postgres-shared-batch-key",
    )

    batched = repository.projections_by_id({eligible.projection_id, kalshi.projection_id})
    assert batched == {
        eligible.projection_id: repository.projection(eligible.projection_id),
        kalshi.projection_id: repository.projection(kalshi.projection_id),
    }
    assert batched[eligible.projection_id] is not None


@pytest.mark.postgres
def test_projections_by_id_omits_unknown_ids_and_handles_empty_input(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresSharedOpportunityRepository(postgres_engine)
    eligible = _projection("batched-known", SharedVenue.CRYPTO)

    class _KnownSource:
        def crypto_projections(
            self, *, pulse_id: str, as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return (eligible,)

        def kalshi_projections(
            self, *, evaluation_ids: tuple[str, ...], as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return ()

        def securities_projections(
            self, *, as_of: datetime
        ) -> tuple[SharedOpportunityProjection, ...]:
            return ()

    MaterializeSharedOpportunitySet(repository, _KnownSource()).execute(
        opportunity_set_id="postgres-shared-known-set",
        crypto_pulse_id="exact-pulse",
        kalshi_evaluation_ids=(),
        as_of=NOW,
        idempotency_key="postgres-shared-known-key",
    )

    assert repository.projections_by_id(()) == {}
    result = repository.projections_by_id({eligible.projection_id, "unknown-projection-id"})
    assert set(result) == {eligible.projection_id}
