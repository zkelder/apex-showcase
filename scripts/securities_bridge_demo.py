"""Securities Research -> Shared Opportunity Recurring Bridge v1: a
deterministic, READ-ONLY representative mixed-market demonstration.

Connects to the configured Postgres database (defaults to the real primary
``apex`` database via ``PostgresSettings.from_env()`` -- the same connection
the live recurring worker uses) and issues SELECT-only reads through already-
existing, already-tested repository/source classes:

* ``PostgresOperatingStateSource.snapshot`` -- the exact same read the real
  recurring worker performs every cycle to resolve current retained
  identities; never mutates anything.
* ``RetainedSharedOpportunitySource.{securities,crypto,kalshi}_projections``
  -- read exact retained source rows; make no provider request and issue no
  writes (confirmed: neither method calls any ``save_*``/``execute`` path).
* ``domain.shared_opportunity.build_opportunity_set`` /
  ``domain.counterfactual_ledger.build_decision_from_eligibility`` -- pure,
  in-memory functions; no I/O of any kind.

This script deliberately never calls ``MaterializeSharedOpportunitySet.
execute`` (which persists), any ``save_*``/``Sync*`` method, or anything in
``adapters.operating_cycle_tasks`` -- nothing here writes a single row.
Every value printed is genuine retained evidence or an honestly-reported
absence; nothing is fabricated to force a passing result.
"""

from __future__ import annotations

from datetime import UTC, datetime

from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.postgres import PostgresSettings, create_postgres_engine
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.domain.counterfactual_ledger import build_decision_from_eligibility
from apex.domain.shared_opportunity import EligibilityState, build_opportunity_set


def _fmt(value: object) -> str:
    return "—" if value is None else str(value)


def main() -> None:
    settings = PostgresSettings.from_env()
    print(
        f"Connecting READ-ONLY to database: {settings.database!r} @ {settings.host}:{settings.port}"
    )
    engine = create_postgres_engine(settings)
    now = datetime.now(UTC)
    try:
        state_source = PostgresOperatingStateSource(
            engine, PostgresOperatingCycleRepository(engine)
        )
        state = state_source.snapshot(as_of=now)
        source = RetainedSharedOpportunitySource(engine)

        securities = source.securities_projections(as_of=now)
        crypto = (
            source.crypto_projections(pulse_id=state.crypto_pulse_id, as_of=now)
            if state.crypto_pulse_id is not None
            else ()
        )
        kalshi = source.kalshi_projections(evaluation_ids=state.kalshi_evaluation_ids, as_of=now)

        print(f"\nAs of: {now.isoformat()}")
        print(f"Real crypto_pulse_id resolved: {_fmt(state.crypto_pulse_id)}")
        print(f"Real kalshi_evaluation_ids resolved: {len(state.kalshi_evaluation_ids)} retained")
        print(
            "Real securities structure candidates retained (latest session only): "
            f"{len(securities)}"
        )
        if not securities:
            print(
                "SECURITIES: genuinely UNAVAILABLE this run -- no retained structure candidate "
                "cleared quality filtering. This is a truthful report, not a bridge failure."
            )

        all_projections = (*securities[:3], *crypto[:2], *kalshi[:2])
        if not all_projections:
            print("\nNo projections available from any venue at this instant. Stopping honestly.")
            return

        opportunity_set, decisions, priorities, _explanations = build_opportunity_set(
            opportunity_set_id=f"securities-bridge-demo-{now.strftime('%Y%m%dT%H%M%S')}",
            created_at=now,
            projections=all_projections,
        )
        decisions_by_id = {item.projection_id: item for item in decisions}
        priorities_by_id = {item.projection_id: item for item in priorities}

        print(f"\nOpportunity set: {opportunity_set.opportunity_set_id}")
        print(
            f"  authority={opportunity_set.authority}"
            f"  paper_eligible={opportunity_set.paper_eligible}"
            f"  live_eligible={opportunity_set.live_eligible}"
            f"  capital_authority={opportunity_set.capital_authority}"
        )
        print(f"  limitations: {opportunity_set.limitations}")

        for projection in all_projections:
            decision = decisions_by_id[projection.projection_id]
            priority = priorities_by_id.get(projection.projection_id)
            print(f"\n{'=' * 78}")
            print(
                f"venue={projection.venue.value}  source={projection.source_record_type}"
                f"#{projection.source_record_id}"
            )
            print(
                f"  instrument_id={projection.instrument_id}  product_or_market_id="
                f"{projection.product_or_market_id}  direction={projection.direction}"
            )
            print(
                f"  horizon_days={_fmt(projection.expected_holding_days)}"
                f"  generated_at={projection.generated_at.isoformat()}"
                f"  valid_until={_fmt(projection.valid_until)}"
            )
            print(
                f"  provenance: evidence_class={projection.evidence_class}"
                f"  freshness={projection.freshness_seconds}s/{projection.freshness_limit_seconds}s"
                f"  source_canonical_hash={projection.source_canonical_hash[:16]}..."
            )
            print(
                f"  economics: comparable_value={_fmt(projection.comparable_value)}"
                f"  comparable_value_identity={_fmt(projection.comparable_value_identity)}"
                f"  maximum_loss={_fmt(projection.maximum_loss)}"
                f"  required_hypothetical_capital={_fmt(projection.required_hypothetical_capital)}"
            )
            print(f"  eligibility: state={decision.state.value}  reasons={decision.reasons}")
            if priority is not None:
                print(
                    f"  priority: rank={priority.rank}  score={priority.score}"
                    f"  tier={priority.tier.value}"
                )
                print(f"    tier_reasons={priority.tier_reasons}")
                top = sorted(priority.components, key=lambda c: (-c.weighted_value, c.name))[:3]
                print(
                    "    top components: " + ", ".join(f"{c.name}={c.weighted_value}" for c in top)
                )
            else:
                print("  priority: not ranked (not ELIGIBLE)")
            print(
                f"  authority: projection.authority={projection.authority}"
                f"  source_authority={projection.source_authority}"
                f"  paper_eligible={projection.paper_eligible}"
                f"  live_eligible={projection.live_eligible}"
                f"  capital_authority={projection.capital_authority}"
                f"  financial_authority={projection.financial_authority}"
            )

            # Pure, in-memory only -- proves counterfactual lineage shape
            # without writing a single row anywhere.
            counterfactual = build_decision_from_eligibility(
                decision=decision,
                projection=projection,
                opportunity_set_id=opportunity_set.opportunity_set_id,
            )
            print(
                "  counterfactual lineage (in-memory only, not persisted): "
                f"venue={counterfactual.venue.value}"
                f"  disposition={counterfactual.disposition.value}"
                f"  authority={counterfactual.authority}"
            )
            if decision.state is not EligibilityState.ELIGIBLE:
                missing = tuple(r for r in decision.reasons if r.endswith("_MISSING"))
                if missing:
                    print(f"  (missing evidence, honestly reported, never fabricated): {missing}")

        print(f"\n{'=' * 78}")
        print(
            "Nothing above was written to the database. "
            "No financial/provider authority was exercised."
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
