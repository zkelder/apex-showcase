"""Persistence regression coverage for "CryptoResearchConflict Root-Cause &
Autonomous Recovery v1".

Reproduces, against a real migrated Postgres database, the exact retained
production shape that made ``CRYPTO_MARKET_PULSE`` genuinely FAIL on
2026-08-16 with ``error_code=CryptoResearchConflict``: a
``RunCurrentCryptoStatefulMethod`` current-signal computation that selects
an asset to enter (``CryptoStatefulCurrentSignal.state == LONG``) and
therefore tries to persist a ``CryptoResearchOpportunity`` whose
``discovery_run_id`` names a ``crypto_stateful_current_signal`` row rather
than a ``crypto_discovery_run`` row. Before migration
``0101_counterfactual_outcome`` this insert always violated
``crypto_research_opportunity_discovery_run_id_fkey`` and surfaced as
``CryptoResearchConflict("CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT")``; after it,
the same insert succeeds because the lineage guard trigger accepts a
``discovery_run_id`` that resolves through *either* legitimate producer
table. See migration 0100's docstring for the full root-cause trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.crypto_stateful_research_postgres import (
    PostgresCryptoStatefulResearchRepository,
)
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.postgres import PostgresSettings, create_postgres_engine
from apex.adapters.research_operations_postgres import PostgresResearchOperationsRepository
from apex.application.crypto_research import CryptoResearchConflict
from apex.application.crypto_stateful_research import (
    CryptoStatefulExperimentExecutor,
    ImplementCryptoStatefulMethod,
    RefineAndApproveGeneratedCryptoProposal,
    RunCurrentCryptoStatefulMethod,
)
from apex.application.research_operations import CreateResearchProposal, RefreshResearchAgenda
from apex.domain.crypto_stateful_research import (
    CryptoExecutionCandle,
    CryptoExecutionHistory,
    CryptoState,
    CryptoStatefulExperimentRun,
    CryptoStatefulMethodSpecification,
)
from apex.domain.crypto_universe import (
    CryptoCurrentMarketEvidence,
    CryptoDailyObservation,
    CryptoProductObservation,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.research_operations import (
    ResearchAgendaSourceFact,
    ResearchFeasibilityState,
    ResearchProposal,
    ResearchProposalOrigin,
    ResearchVenue,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 8, 16, tzinfo=UTC)
HASH = "a" * 64


def _original_proposal() -> ResearchProposal:
    return ResearchProposal.create(
        proposal_id="lineage-rank-1-generated-crypto",
        version=1,
        agenda_item_id="agenda-lineage-negative-crypto",
        venue=ResearchVenue.CRYPTO,
        hypothesis="A stateful challenger may resolve negative current economics.",
        expected_mechanism="Stateful monthly transitions replace short-horizon ranking.",
        instruments=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
        proposed_family="CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1_MATERIAL_STATEFUL_CHALLENGER",
        definition_kind="CRYPTO_GENERIC_LONG_CASH",
        strategy_definition=(
            ("decision_cadence", "CALENDAR_MONTHLY"),
            ("execution", "NEXT_OPEN"),
            ("feature", "POINT_TO_POINT_RETURN"),
            ("state", "STATEFUL_LONG_CASH"),
        ),
        exact_change_from_prior=(
            "STATEFUL_MONTHLY_REGIME_TRANSITIONS_REPLACE_SHORT_HORIZON_REVERSAL_RANKING"
        ),
        prior_research_ids=(),
        required_data_ids=("negative-expected-economics",),
        experiment_configuration=(("scope", "RETAINED_DEVELOPMENT_ONLY"),),
        validation_windows=(("scope", "EXACT_RETAINED_OR_DISJOINT_SCOPE"),),
        cost_assumptions=(("cost_policy", "EXISTING_FIXED_COST_ASSUMPTIONS"),),
        success_criteria=("DECISION_EVIDENCE_IMPROVED",),
        failure_criteria=("ECONOMICS_OR_QUALITY_FAILED",),
        maximum_experiment_runs=1,
        origin=ResearchProposalOrigin.SYSTEM_HEURISTIC,
        actor="operator:generator",
        warnings=("RESEARCH_ONLY",),
        created_at=NOW - timedelta(days=14),
    )


def _backtest_history(
    product_id: str, asset_id: str, closes: tuple[Decimal, ...]
) -> CryptoExecutionHistory:
    """Mirrors ``tests/test_crypto_stateful_research.py``'s ``_history`` helper: only
    needs to be *valid* enough for ``run_stateful_crypto_experiment`` to produce a
    genuine ``CryptoStatefulExperimentRun`` -- this fixture's actual entry/exit
    decisions are irrelevant to the persistence defect under test, which lives
    entirely in the *current signal* -> opportunity path below."""

    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = tuple(
        CryptoExecutionCandle.create(
            bucket_start=start + timedelta(days=index), open_price=value, close_price=value
        )
        for index, value in enumerate(closes)
    )
    return CryptoExecutionHistory(
        history_id=f"lineage-history-{asset_id}",
        canonical_asset_id=asset_id,
        product_id=product_id,
        source_observation_id=f"lineage-obs-{asset_id}",
        source_observation_hash=HASH,
        interval_start=start,
        interval_end=start + timedelta(days=len(closes)),
        candles=candles,
        raw_evidence_hashes=(HASH,),
        request_count=1,
        captured_at=NOW,
        canonical_hash=HASH,
    )


def _backtest_series() -> tuple[tuple[Decimal, ...], tuple[Decimal, ...], tuple[Decimal, ...]]:
    first: list[Decimal] = []
    second: list[Decimal] = []
    third: list[Decimal] = []
    for index in range(145):
        base = Decimal("100") + Decimal(index) * Decimal("0.30")
        if 96 <= index <= 103:
            base -= Decimal(index - 95) * Decimal("1.20")
        if index >= 104:
            base += Decimal(index - 103) * Decimal("1.30") - Decimal("9.60")
        first.append(base)
        second.append(
            Decimal("80")
            + Decimal(index) * Decimal("0.10")
            + (Decimal("4") if index % 2 else Decimal("-4"))
        )
        third.append(
            Decimal("60")
            + Decimal(index) * Decimal("0.08")
            + (Decimal("6") if index % 3 else Decimal("-6"))
        )
    return tuple(first), tuple(second), tuple(third)


def _pullback_closes() -> tuple[Decimal, ...]:
    """91 daily closes: a smooth 84-day rise (+50%) then a sharp 7-day pullback
    (~-10%) -- deterministically forces ``current_stateful_signal`` to select
    this asset (``return_90d > 0``, ``return_7d <= -0.03``, low realized
    volatility relative to the two noisy, net-flat-or-down comparison assets
    below), exactly mirroring the real 2026-08-16 XLM entry."""

    values: list[Decimal] = []
    for index in range(84):
        values.append(Decimal("100") + Decimal(index) * Decimal("0.6"))
    peak = values[-1]
    for step in range(1, 8):
        values.append(peak - Decimal(step) * Decimal("2.2"))
    assert len(values) == 91
    return tuple(values)


def _noisy_flat_closes(offset: Decimal) -> tuple[Decimal, ...]:
    """91 daily closes with real day-to-day noise (so its volatility is not
    the smallest of the three, keeping the target's volatility at or below
    the group median) but a flat-to-negative 90-day trend, so this asset can
    never itself satisfy ``return_90d > 0`` and is never selected."""

    values: list[Decimal] = []
    for index in range(91):
        swing = Decimal("6") if index % 2 == 0 else Decimal("-6")
        values.append(Decimal("200") + offset - Decimal(index) * Decimal("0.35") + swing)
    return tuple(values)


def _observation(
    *, product_id: str, asset_id: str, start: datetime, closes: tuple[Decimal, ...]
) -> CryptoProductObservation:
    candles = tuple(
        CryptoDailyObservation.create(
            bucket_start=start + timedelta(days=index), close=value, volume=Decimal("1000000")
        )
        for index, value in enumerate(closes)
    )
    return CryptoProductObservation.create(
        observation_id=f"lineage-current-obs-{asset_id}",
        source_capture_id="lineage-current-capture",
        product_id=product_id,
        interval_start=start,
        interval_end=start + timedelta(days=len(closes)),
        evidence_class=EvidenceClass.FIXTURE,
        candles=candles,
        missing_buckets=0,
        duplicate_records=0,
        rejected_records=0,
        request_count=1,
        raw_evidence_hashes=(HASH,),
        source_contract_version="TEST_SOURCE_V1",
        parser_version="TEST_PARSER_V1",
        checkpoint_ordinal=1,
        completed_at=start + timedelta(days=len(closes)),
        canonical_asset_id=asset_id,
    )


def _market_evidence(*, product_id: str, observed_at: datetime) -> CryptoCurrentMarketEvidence:
    payload = (product_id, "lineage-checkpoint", observed_at, "ACTIVE/ONLINE")
    return CryptoCurrentMarketEvidence(
        product_id=product_id,
        checkpoint_id="lineage-checkpoint",
        observed_at=observed_at,
        product_status="ACTIVE/ONLINE",
        spread_bps=Decimal("5"),
        visible_ask_depth=Decimal("5000"),
        required_capital=Decimal("100"),
        maximum_loss=Decimal("100"),
        maximum_sensible_allocation=Decimal("5000"),
        evidence_complete=True,
        canonical_hash=content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class _StatefulFixture:
    """One real, FK-safe specification + experiment run, built exactly the way
    ``ImplementCryptoStatefulMethod``/``CryptoStatefulExperimentExecutor`` do in
    production. Shared by every test below that needs a genuine
    ``crypto_stateful_current_signal`` producer parent, since this setup (unlike
    the signal itself) never changes per-day -- mirrors the six retained
    2026-08-1{1..6} signals, which all share one ``experiment_run_id``."""

    operations: PostgresResearchOperationsRepository
    stateful: PostgresCryptoStatefulResearchRepository
    crypto: PostgresCryptoUniverseRepository
    specification: CryptoStatefulMethodSpecification
    experiment: CryptoStatefulExperimentRun


def _build_experiment(postgres_engine: sa.Engine) -> _StatefulFixture:
    operations = PostgresResearchOperationsRepository(postgres_engine)
    stateful = PostgresCryptoStatefulResearchRepository(postgres_engine)
    crypto = PostgresCryptoUniverseRepository(postgres_engine)

    RefreshResearchAgenda(operations).execute(
        facts=(
            ResearchAgendaSourceFact.create(
                fact_id="lineage-negative-crypto",
                fact_type="NEGATIVE_EXPECTED_ECONOMICS",
                venue=ResearchVenue.CRYPTO,
                market_scope=("CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",),
                state="BLOCKED",
                summary="No positive current source-eligible result.",
                evidence_ids=("negative-expected-economics",),
                evidence_hashes=(HASH,),
                observed_at=NOW - timedelta(days=14),
            ),
        )
    )
    original = _original_proposal()
    CreateResearchProposal(operations).execute(original)
    refined = RefineAndApproveGeneratedCryptoProposal(operations, stateful).execute(
        original_proposal_id=original.proposal_id,
        refined_proposal_id="lineage-refinement-v2",
        refinement_id="lineage-refinement-evidence-v1",
        dataset_id="lineage-retained-730-day-open-close-v1",
        retained_observation_ids=("obs-a", "obs-b", "obs-c"),
        current_evidence_ids=("current-pulse",),
        actor="operator:test",
        refined_at=NOW - timedelta(days=13),
        provider_request_limit=9,
    )
    histories = tuple(
        _backtest_history(product, asset, values)
        for product, asset, values in zip(
            ("AAA-USD", "BBB-USD", "CCC-USD"),
            ("AAA", "BBB", "CCC"),
            _backtest_series(),
            strict=True,
        )
    )
    specification, feasibility, _ = ImplementCryptoStatefulMethod(operations, stateful).execute(
        proposal_id=refined.proposal.proposal_id,
        specification_id="lineage-spec-v1",
        implementation_id="lineage-implementation-v1",
        implemented_at=NOW - timedelta(days=12),
        histories=histories,
    )
    assert feasibility.status is ResearchFeasibilityState.READY_FOR_REVIEW
    executor = CryptoStatefulExperimentExecutor(
        stateful, specification, histories, "lineage-dataset-v1", Decimal("0.01"), Decimal("-0.02")
    )
    executor.execute(
        proposal=refined.proposal,
        run_id="lineage-experiment-run-v1",
        executed_at=NOW - timedelta(days=11),
    )
    experiment = stateful.experiment_run("lineage-experiment-run-v1")
    assert experiment is not None
    return _StatefulFixture(
        operations=operations,
        stateful=stateful,
        crypto=crypto,
        specification=specification,
        experiment=experiment,
    )


@pytest.mark.postgres
def test_stateful_current_signal_opportunity_persists_after_lineage_widening(
    postgres_engine: sa.Engine,
) -> None:
    fixture = _build_experiment(postgres_engine)
    stateful, crypto = fixture.stateful, fixture.crypto
    specification, experiment = fixture.specification, fixture.experiment
    # Sanity check: no discovery run of any kind has been created by any of the
    # above -- the FK this migration widens genuinely cannot be satisfied by
    # the pre-migration single-table constraint.
    assert crypto.discovery_run("lineage-experiment-run-v1") is None

    # --- Build the CURRENT (as of "today") observation/market evidence: one
    # asset that genuinely satisfies every stateful entry precondition, and two
    # that cannot, exactly mirroring the real three-or-more-asset universe the
    # production method scores every day.
    start = NOW - timedelta(days=91)
    target = _observation(
        product_id="TST-USD", asset_id="TST", start=start, closes=_pullback_closes()
    )
    neutral_one = _observation(
        product_id="NTA-USD", asset_id="NTA", start=start, closes=_noisy_flat_closes(Decimal("0"))
    )
    neutral_two = _observation(
        product_id="NTB-USD", asset_id="NTB", start=start, closes=_noisy_flat_closes(Decimal("17"))
    )
    observations = (target, neutral_one, neutral_two)
    market = tuple(
        _market_evidence(product_id=item.product_id, observed_at=NOW) for item in observations
    )

    runner = RunCurrentCryptoStatefulMethod(stateful, crypto)

    # === D: exact reproduction of the 2026-08-16 shape, now fixed. ===
    # Before migration 0100 this ``execute`` call raised
    # ``CryptoResearchConflict("CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT")`` from
    # ``PostgresCryptoUniverseRepository.save_opportunities`` -> ``_insert``
    # wrapping the ``crypto_research_opportunity_discovery_run_id_fkey``
    # violation, every single time, deterministically -- this is not a flaky
    # or timing-dependent repro.
    day1_signal_id = "lineage-cycle-worker-20260816-crypto-stateful-pullback-rotation-v1"
    signal, opportunities, replay = runner.execute(
        signal_id=day1_signal_id,
        specification=specification,
        experiment=experiment,
        recommendation_id="lineage-recommendation-v1",
        source_pulse_id="lineage-pulse-v1",
        universe_snapshot_id="lineage-universe-snapshot-v1",
        observations=observations,
        current_market=market,
        generated_at=NOW,
    )
    assert not replay
    assert signal.state is CryptoState.LONG
    assert signal.selected_asset_id == "TST"
    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.discovery_run_id == day1_signal_id
    assert opportunity.canonical_asset_id == "TST"
    # The whole point: this identity resolves through the *signal* table, not
    # the discovery-run table the old single-table foreign key demanded.
    assert crypto.discovery_run(day1_signal_id) is None
    assert stateful.current_signal(day1_signal_id) is not None
    retained_opportunity = crypto.opportunity(opportunity.opportunity_id)
    assert retained_opportunity == opportunity

    # === A / E: exact replay is idempotent, not a conflict, and stable across
    # repeated recurring-cycle-style invocations (same signal_id + inputs). ===
    for _ in range(2):
        replayed_signal, replayed_opportunities, idempotent = runner.execute(
            signal_id=day1_signal_id,
            specification=specification,
            experiment=experiment,
            recommendation_id="lineage-recommendation-v1",
            source_pulse_id="lineage-pulse-v1",
            universe_snapshot_id="lineage-universe-snapshot-v1",
            observations=observations,
            current_market=market,
            generated_at=NOW,
        )
        assert idempotent
        assert replayed_signal == signal
        assert replayed_opportunities == opportunities
    assert (
        len(
            tuple(
                item for item in crypto.opportunities() if item.discovery_run_id == day1_signal_id
            )
        )
        == 1
    )

    # === B: same identity, materially different canonical content -> a
    # genuine conflict must still fail closed (the fix widens *which*
    # identities are accepted; it does not weaken *what counts as* a real
    # content conflict once an identity is accepted). ===
    mutated_payload = (
        opportunity.opportunity_id,
        opportunity.discovery_run_id,
        opportunity.product_id,
        "MUTATED_FOR_TEST",
    )
    mutated = opportunity.__class__(
        **{
            **{field: getattr(opportunity, field) for field in opportunity.__dataclass_fields__},
            "gross_research_score": opportunity.gross_research_score + Decimal("1"),
            "canonical_hash": content_digest(mutated_payload),
        }
    )
    assert mutated.opportunity_id == opportunity.opportunity_id
    assert mutated.canonical_hash != opportunity.canonical_hash
    with pytest.raises(CryptoResearchConflict, match="CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT"):
        crypto.save_opportunities((mutated,))

    # === C: a legitimate different day (a different, independently-valid
    # signal identity) coexists rather than conflicting, even though it
    # shares the same specification/experiment lineage and selects the same
    # asset -- exactly how 2026-08-17's cycle would behave if the pullback
    # condition were still active. ===
    day2_signal_id = "lineage-cycle-worker-20260817-crypto-stateful-pullback-rotation-v1"
    day2_signal, day2_opportunities, day2_replay = runner.execute(
        signal_id=day2_signal_id,
        specification=specification,
        experiment=experiment,
        recommendation_id="lineage-recommendation-v1",
        source_pulse_id="lineage-pulse-day2-v1",
        universe_snapshot_id="lineage-universe-snapshot-v1",
        observations=observations,
        current_market=tuple(
            _market_evidence(product_id=item.product_id, observed_at=NOW + timedelta(days=1))
            for item in observations
        ),
        generated_at=NOW + timedelta(days=1),
    )
    assert not day2_replay
    assert day2_signal.signal_id != day1_signal_id
    assert day2_opportunities[0].opportunity_id != opportunity.opportunity_id
    assert stateful.current_signal(day1_signal_id) is not None
    assert stateful.current_signal(day2_signal_id) is not None
    assert crypto.opportunity(opportunity.opportunity_id) is not None
    assert crypto.opportunity(day2_opportunities[0].opportunity_id) is not None

    # --- Integrity is not weakened: a discovery_run_id resolving through
    # *neither* legitimate producer table must still fail closed. ---
    dangling_payload = ("lineage-dangling-opportunity", "not-a-real-run-or-signal-id")
    dangling = opportunity.__class__(
        **{
            **{field: getattr(opportunity, field) for field in opportunity.__dataclass_fields__},
            "opportunity_id": "lineage-dangling-opportunity",
            "discovery_run_id": "definitely-not-retained-anywhere",
            "canonical_hash": content_digest(dangling_payload),
        }
    )
    with pytest.raises(CryptoResearchConflict, match="CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT"):
        crypto.save_opportunities((dangling,))
    assert crypto.opportunity("lineage-dangling-opportunity") is None

    # --- Migration 0100's own downgrade guard: with a genuinely
    # stateful-sourced opportunity retained (day1's), downgrading past it must
    # refuse rather than silently reintroduce a foreign key the retained data
    # can no longer satisfy. ---
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0100: retained crypto_research_opportunity rows exist",
        ):
            command.downgrade(_alembic_config(connection), "0099_followup_evidence_quality")
        connection.rollback()
        # Counterfactual Outcome Feedback v1's own migration 0101 sits above
        # 0100 and owns no retained evidence in this test (nothing here ever
        # syncs a counterfactual_outcome_aggregate row), so its own downgrade
        # step commits genuinely before 0100's guard is ever reached -- the
        # walk still lands on 0100, not head, exactly the same shape
        # documented in AGENTS.md's Counterfactual Decision Classification
        # changelog for the analogous 0094/0095 pair.
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0100_crypto_opportunity_lineage"
        )


def _insert_discovery_run_chain(connection: sa.Connection, *, suffix: str) -> str:
    """Raw-SQL minimal capture -> snapshot -> discovery_run chain: only needs
    to satisfy the tables' own constraints, not full domain realism -- used by
    the tests below, which exercise producer-table DB semantics (immutability,
    TRUNCATE, transaction visibility) directly rather than through the
    application layer the test above already covers end to end. Returns the
    new run_id."""
    h = HASH
    connection.execute(
        sa.text(
            "INSERT INTO crypto_public_product_capture "
            "(capture_id, evidence_class, payload, canonical_hash, authority) "
            "VALUES (:capture_id, 'FIXTURE', '{}', :hash, 'NONE')"
        ),
        {"capture_id": f"raw-capture-{suffix}", "hash": h},
    )
    connection.execute(
        sa.text(
            "INSERT INTO crypto_universe_snapshot "
            "(snapshot_id, source_capture_id, policy_id, manifest_hash, payload, "
            "canonical_hash, authority) "
            "VALUES (:snapshot_id, :capture_id, 'raw-policy', :manifest, '{}', :hash, 'NONE')"
        ),
        {
            "snapshot_id": f"raw-snapshot-{suffix}",
            "capture_id": f"raw-capture-{suffix}",
            "manifest": f"raw-manifest-{suffix}",
            "hash": h,
        },
    )
    run_id = f"raw-run-{suffix}"
    connection.execute(
        sa.text(
            "INSERT INTO crypto_discovery_run "
            "(run_id, universe_snapshot_id, method_id, result_hash, payload, "
            "canonical_hash, authority) "
            "VALUES (:run_id, :snapshot_id, 'raw-method', :hash, '{}', :hash, 'NONE')"
        ),
        {"run_id": run_id, "snapshot_id": f"raw-snapshot-{suffix}", "hash": h},
    )
    return run_id


def _insert_opportunity(
    connection: sa.Connection, *, opportunity_id: str, discovery_run_id: str
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO crypto_research_opportunity "
            "(opportunity_id, discovery_run_id, product_id, status, payload, "
            "canonical_hash, authority) "
            "VALUES (:opportunity_id, :discovery_run_id, 'raw-product', 'RESEARCH_ONLY', "
            "'{}', :hash, 'NONE')"
        ),
        {"opportunity_id": opportunity_id, "discovery_run_id": discovery_run_id, "hash": HASH},
    )


def _insert_signal(connection: sa.Connection, *, signal_id: str, experiment_run_id: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO crypto_stateful_current_signal "
            "(signal_id, method_id, experiment_run_id, generated_at, state, payload, "
            "canonical_hash, authority, live_authority) "
            "VALUES (:signal_id, 'raw-method', :experiment_run_id, :generated_at, 'CASH', "
            "'{}', :hash, 'NONE', 'NONE')"
        ),
        {
            "signal_id": signal_id,
            "experiment_run_id": experiment_run_id,
            "generated_at": NOW,
            "hash": HASH,
        },
    )


@pytest.mark.postgres
def test_lineage_producers_and_opportunity_reject_update_delete_and_untagged_truncate(
    postgres_engine: sa.Engine,
) -> None:
    """The CONCERN this proves: a ``BEFORE INSERT`` trigger alone only checks
    lineage at insert time; a real FK also blocks later deletion (and, less
    obviously, TRUNCATE) of a referenced parent. Migration 0100 drops
    ``crypto_research_opportunity``'s only FK, so this proves the replacement
    protection -- the pre-existing, untouched ``apex_immutable_*`` row trigger
    for UPDATE/DELETE, plus the new statement-level TRUNCATE guard this
    migration adds -- is at least as strong, for both producer tables and for
    the opportunity table itself. Without the TRUNCATE guard added by this
    migration, ``TRUNCATE crypto_discovery_run`` (single-table, untagged)
    provably succeeds post-0100 and strands any referencing opportunity --
    see this migration's docstring for the empirical before/after proof."""
    fixture = _build_experiment(postgres_engine)
    signal_id = "immutability-signal-v1"
    with postgres_engine.begin() as connection:
        _insert_signal(connection, signal_id=signal_id, experiment_run_id=fixture.experiment.run_id)
        run_id = _insert_discovery_run_chain(connection, suffix="immutability")
        _insert_opportunity(
            connection, opportunity_id="immutability-opportunity-run", discovery_run_id=run_id
        )
        _insert_opportunity(
            connection,
            opportunity_id="immutability-opportunity-signal",
            discovery_run_id=signal_id,
        )

    # --- UPDATE/DELETE: unchanged, pre-existing apex_immutable_* row
    # triggers, exercised directly against both producers and the opportunity
    # itself (only the opportunity's own UPDATE path was previously covered,
    # by test_postgres_crypto_universe.py). ---
    for table, id_column, id_value in (
        ("crypto_discovery_run", "run_id", run_id),
        ("crypto_stateful_current_signal", "signal_id", signal_id),
        ("crypto_research_opportunity", "opportunity_id", "immutability-opportunity-run"),
    ):
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(f"DELETE FROM {table} WHERE {id_column} = :id"), {"id": id_value}
            )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(f"UPDATE {table} SET authority = 'PAPER' WHERE {id_column} = :id"),
                {"id": id_value},
            )

    # --- TRUNCATE (single-table and CASCADE), untagged: must also fail
    # closed on both producer tables. ---
    for table in ("crypto_discovery_run", "crypto_stateful_current_signal"):
        for statement in (f"TRUNCATE TABLE {table}", f"TRUNCATE TABLE {table} CASCADE"):
            with (
                pytest.raises(sa.exc.DBAPIError, match="immutable"),
                postgres_engine.begin() as connection,
            ):
                connection.exec_driver_sql(statement)

    # --- Every row above -- and the opportunities referencing them -- must
    # have survived every rejected attempt completely untouched: no dangling
    # lineage was created by any of the failed operations. ---
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM crypto_discovery_run WHERE run_id = :id"),
                {"id": run_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM crypto_stateful_current_signal WHERE signal_id = :id"
                ),
                {"id": signal_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM crypto_research_opportunity WHERE opportunity_id IN "
                    "('immutability-opportunity-run', 'immutability-opportunity-signal')"
                )
            ).scalar_one()
            == 2
        )

    # --- The narrow, explicitly-tagged escape hatch the test suite's own
    # ``clean_database`` fixture relies on must still genuinely work exactly
    # as intended: opt-in, scoped to one transaction, both producers at once,
    # even combined with CASCADE. ---
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL apex.allow_producer_truncate = 'true'")
        connection.exec_driver_sql(
            "TRUNCATE TABLE crypto_discovery_run, crypto_stateful_current_signal CASCADE"
        )
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT count(*) FROM crypto_discovery_run")).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM crypto_stateful_current_signal")
            ).scalar_one()
            == 0
        )


@pytest.mark.postgres
def test_producer_truncate_guc_opt_in_is_transaction_local(
    postgres_engine: sa.Engine, postgres_settings: PostgresSettings
) -> None:
    """Test-bypass hardening (final pre-merge security review): ``SET LOCAL
    apex.allow_producer_truncate = 'true'`` must only ever authorize the one
    transaction that set it -- it must never leak into a later transaction on
    the same session/connection, since a long-lived pooled or interactive
    session that once opted in for a legitimate reason (e.g. this very test
    suite's own ``clean_database`` fixture) must not stay silently authorized
    afterwards. Proven directly on a single checked-out connection reused
    across several sequential transactions (not a fresh connection per
    transaction, which would prove nothing about leakage).

    Empirically, ``current_setting('apex.allow_producer_truncate', true)``
    returns SQL NULL only on a session that has *never* referenced the custom
    placeholder GUC before; once a session has set it even once, Postgres
    keeps the placeholder variable "known" for the rest of that session and it
    reads back as an empty string (not NULL) after the transaction that set it
    ends. Both are correctly rejected by the guard's ``IS DISTINCT FROM
    'true'`` check -- this test proves the untagged case fails closed under
    both representations, and does not assume the imprecise "always reverts to
    NULL" framing.
    """
    # --- A genuinely pristine connection -- its own short-lived engine, never
    # touched by any other test or fixture -- starts with the GUC reading as
    # real SQL NULL. This cannot be shown on ``postgres_engine`` itself: this
    # file's own autouse ``clean_database`` fixture already issues ``SET
    # LOCAL`` on whatever connection it draws immediately before every single
    # test (including this one), so by the time a test body runs, its
    # checked-out pooled connection has, in practice, already been touched. ---
    fresh_engine = create_postgres_engine(postgres_settings)
    try:
        with fresh_engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT current_setting('apex.allow_producer_truncate', true)")
                ).scalar_one()
                is None
            )
    finally:
        fresh_engine.dispose()

    with postgres_engine.connect() as connection:
        # Realistic pooled state (see docstring): almost certainly already
        # touched by ``clean_database`` moments ago, so this may legitimately
        # read back NULL or ``''`` depending on pool history -- either is
        # correctly "not opted in". Explicitly scoped in its own ``begin()``
        # block (rather than a bare ``execute()``) so it does not leave an
        # implicit "autobegin" transaction open that would make the very next
        # explicit ``connection.begin()`` below raise.
        with connection.begin():
            baseline = connection.execute(
                sa.text("SELECT current_setting('apex.allow_producer_truncate', true)")
            ).scalar_one()
            assert baseline != "true"

        # --- COMMIT path: opt in, observe it applied, end the transaction with
        # COMMIT (without ever truncating), then confirm a fresh transaction on
        # this SAME connection no longer authorizes TRUNCATE. ---
        with connection.begin():
            connection.exec_driver_sql("SET LOCAL apex.allow_producer_truncate = 'true'")
            assert (
                connection.execute(
                    sa.text("SELECT current_setting('apex.allow_producer_truncate', true)")
                ).scalar_one()
                == "true"
            )
        with connection.begin():
            reverted = connection.execute(
                sa.text("SELECT current_setting('apex.allow_producer_truncate', true)")
            ).scalar_one()
            assert reverted != "true"
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            connection.begin(),
        ):
            connection.exec_driver_sql("TRUNCATE TABLE crypto_discovery_run")

        # --- ROLLBACK path: opt in, then explicitly roll back instead of
        # committing -- must reset identically to the COMMIT path above. ---
        with connection.begin() as transaction:
            connection.exec_driver_sql("SET LOCAL apex.allow_producer_truncate = 'true'")
            transaction.rollback()
        with connection.begin():
            reverted = connection.execute(
                sa.text("SELECT current_setting('apex.allow_producer_truncate', true)")
            ).scalar_one()
            assert reverted != "true"
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            connection.begin(),
        ):
            connection.exec_driver_sql("TRUNCATE TABLE crypto_discovery_run")

    # --- Nothing above ever genuinely truncated: the producer table (already
    # empty from the autouse ``clean_database`` fixture) still round-trips a
    # fresh insert/read normally, i.e. this connection's session state is not
    # left in some broken half-aborted condition by the rejected attempts. ---
    with postgres_engine.begin() as connection:
        run_id = _insert_discovery_run_chain(connection, suffix="guc-locality")
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM crypto_discovery_run WHERE run_id = :id"),
                {"id": run_id},
            ).scalar_one()
            == 1
        )


@pytest.mark.postgres
def test_opportunity_lineage_resolves_against_producer_inserted_earlier_in_same_transaction(
    postgres_engine: sa.Engine,
) -> None:
    """Section 3.A/3.B of the referential-integrity audit: the lineage
    guard's ``SELECT`` must see a producer row this same transaction already
    wrote, even before that transaction has committed -- ordinary Postgres
    "read your own writes" semantics (true at every isolation level, since it
    concerns a transaction's own uncommitted work, not another transaction's
    commit visibility), proven directly rather than assumed, because the
    guard is a ``BEFORE INSERT`` row trigger evaluated against whatever the
    current transaction's own snapshot sees at statement time."""
    fixture = _build_experiment(postgres_engine)

    # --- A: crypto_stateful_current_signal inserted, then its opportunity,
    # both later in the SAME, still-open transaction. ---
    signal_id = "same-tx-signal-v1"
    with postgres_engine.begin() as connection:
        _insert_signal(connection, signal_id=signal_id, experiment_run_id=fixture.experiment.run_id)
        _insert_opportunity(
            connection, opportunity_id="same-tx-opportunity-signal", discovery_run_id=signal_id
        )
        # Still uncommitted here -- the insert above already had to pass the
        # trigger's SELECT without raising, so this is confirming the row
        # genuinely landed pre-commit, not re-testing the guard itself.
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM crypto_research_opportunity WHERE opportunity_id = :id"
                ),
                {"id": "same-tx-opportunity-signal"},
            ).scalar_one()
            == 1
        )
    with postgres_engine.connect() as verify:
        assert (
            verify.execute(
                sa.text(
                    "SELECT count(*) FROM crypto_research_opportunity WHERE opportunity_id = :id"
                ),
                {"id": "same-tx-opportunity-signal"},
            ).scalar_one()
            == 1
        )

    # --- B: crypto_discovery_run inserted, then its opportunity, both later
    # in the SAME, still-open transaction. ---
    with postgres_engine.begin() as connection:
        run_id = _insert_discovery_run_chain(connection, suffix="same-tx")
        _insert_opportunity(
            connection, opportunity_id="same-tx-opportunity-run", discovery_run_id=run_id
        )
    with postgres_engine.connect() as verify:
        assert (
            verify.execute(
                sa.text(
                    "SELECT count(*) FROM crypto_research_opportunity WHERE opportunity_id = :id"
                ),
                {"id": "same-tx-opportunity-run"},
            ).scalar_one()
            == 1
        )


@pytest.mark.postgres
def test_uncommitted_producer_in_a_different_transaction_does_not_satisfy_lineage(
    postgres_engine: sa.Engine,
) -> None:
    """Section 3.C of the referential-integrity audit: two independent
    connections/sessions. Connection A inserts a new discovery_run but does
    not commit. Connection B -- concurrently, in its own transaction -- must
    NOT see A's uncommitted row: its own opportunity insert referencing that
    same run_id must fail closed exactly as if the row never existed (plain
    ``SELECT``s under READ COMMITTED never block on, or see, another
    transaction's uncommitted writes). Only after A commits does the
    identical insert, retried in a fresh transaction, succeed -- proving this
    is genuinely a commit-visibility guarantee and not a permanent identity
    problem, and that no race lets an uncommitted, possibly-to-be-rolled-back
    producer create a false valid lineage."""
    run_id = "cross-tx-run-v1"
    with postgres_engine.connect() as connection_a, postgres_engine.connect() as connection_b:
        connection_a.execute(
            sa.text(
                "INSERT INTO crypto_public_product_capture "
                "(capture_id, evidence_class, payload, canonical_hash, authority) "
                "VALUES ('cross-tx-capture', 'FIXTURE', '{}', :hash, 'NONE')"
            ),
            {"hash": HASH},
        )
        connection_a.execute(
            sa.text(
                "INSERT INTO crypto_universe_snapshot "
                "(snapshot_id, source_capture_id, policy_id, manifest_hash, payload, "
                "canonical_hash, authority) "
                "VALUES ('cross-tx-snapshot', 'cross-tx-capture', 'raw-policy', "
                "'cross-tx-manifest', '{}', :hash, 'NONE')"
            ),
            {"hash": HASH},
        )
        connection_a.execute(
            sa.text(
                "INSERT INTO crypto_discovery_run "
                "(run_id, universe_snapshot_id, method_id, result_hash, payload, "
                "canonical_hash, authority) "
                "VALUES (:run_id, 'cross-tx-snapshot', 'raw-method', :hash, '{}', :hash, 'NONE')"
            ),
            {"run_id": run_id, "hash": HASH},
        )
        # Deliberately not committed yet: connection_a's transaction is still
        # open, and this run_id is not visible to any other session.

        with pytest.raises(sa.exc.DBAPIError, match="discovery_run_id must reference an existing"):
            _insert_opportunity(
                connection_b, opportunity_id="cross-tx-opportunity", discovery_run_id=run_id
            )
        # Required: Postgres aborts the whole transaction after any statement
        # error ("current transaction is aborted"); connection_b cannot be
        # used again until this is resolved.
        connection_b.rollback()

        connection_a.commit()

        # Now that A's producer row is genuinely durable, the identical
        # insert -- retried in a brand-new transaction on B -- must succeed.
        _insert_opportunity(
            connection_b, opportunity_id="cross-tx-opportunity", discovery_run_id=run_id
        )
        connection_b.commit()

    with postgres_engine.connect() as verify:
        assert (
            verify.execute(
                sa.text(
                    "SELECT discovery_run_id FROM crypto_research_opportunity "
                    "WHERE opportunity_id = 'cross-tx-opportunity'"
                )
            ).scalar_one()
            == run_id
        )


@pytest.mark.postgres
def test_lineage_guard_is_closed_to_a_third_real_but_unaccepted_identity(
    postgres_engine: sa.Engine,
) -> None:
    """Section 4 of the referential-integrity audit: the guard's accepted
    producer set is exactly {crypto_discovery_run, crypto_stateful_current_signal}
    -- not "any id that currently exists somewhere in the schema". Proven with
    a REAL, currently-existing row's id taken from a third, unrelated table
    (crypto_universe_snapshot, itself a legitimate evidence table one step
    removed in the same lineage chain) rather than an obviously-fake string:
    even a genuine, live identity must still fail closed if it does not
    belong to one of the two explicitly accepted producer tables. A future
    third producer type must be explicitly added to the guard function by a
    new migration; it cannot be satisfied incidentally."""
    with postgres_engine.begin() as connection:
        _insert_discovery_run_chain(connection, suffix="closed-set")
    snapshot_id = "raw-snapshot-closed-set"
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM crypto_universe_snapshot WHERE snapshot_id = :id"),
                {"id": snapshot_id},
            ).scalar_one()
            == 1
        )
    with (
        pytest.raises(sa.exc.DBAPIError, match="discovery_run_id must reference an existing"),
        postgres_engine.begin() as connection,
    ):
        _insert_opportunity(
            connection, opportunity_id="closed-set-opportunity", discovery_run_id=snapshot_id
        )
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM crypto_research_opportunity WHERE opportunity_id = :id"
                ),
                {"id": "closed-set-opportunity"},
            ).scalar_one()
            == 0
        )


def _alembic_config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config
