from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_baseline_quote_postgres import PostgresKalshiBaselineQuoteEvidenceSource
from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_public import KalshiPublicEventNormalizer, KalshiPublicMarketNormalizer
from apex.adapters.kalshi_threshold_fixtures import KalshiThresholdFixtureClient
from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    postgres_bounded_orderbook_capture_store,
    postgres_public_market_discovery_store,
    postgres_public_threshold_ladder_store,
)
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.application.capture_public_orderbook_session import CaptureBoundedPublicOrderbookSession
from apex.application.discover_public_markets import DiscoverPublicPredictionMarkets
from apex.application.scan_public_threshold_ladders import (
    PublicThresholdLadderConflictError,
    PublicThresholdLadderScanRequest,
    ScanPublicThresholdLadders,
)
from apex.domain.public_threshold import ThresholdEvaluationReasonCode, ThresholdEvaluationState

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
REVISION_0012 = "0012_orderbook_capture"
REVISION_0013 = "0013_threshold_ladder"
HEAD_REVISION = "0118_evidence_gap_recovery"
THRESHOLD_TABLES = {
    "threshold_ladder_scan",
    "threshold_market_relationship",
    "threshold_bundle_evaluation",
    "threshold_matched_slice",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _migrate(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.upgrade(_config(connection), revision)
        connection.commit()


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.downgrade(_config(connection), revision)
        connection.commit()


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _scan(engine: Engine, *, scan_id: str):
    clock = VirtualCaptureClock()
    client = KalshiThresholdFixtureClient(clock)
    discovery_store = postgres_public_market_discovery_store(engine)
    capture_store = postgres_bounded_orderbook_capture_store(engine)
    normalizer = KalshiPublicMarketNormalizer()
    return ScanPublicThresholdLadders(
        discovery=DiscoverPublicPredictionMarkets(
            client=client,
            normalizer=normalizer,
            store=discovery_store,
            clock=clock.wall_clock,
        ),
        capture=CaptureBoundedPublicOrderbookSession(
            client=client,
            store=capture_store,
            wall_clock=clock.wall_clock,
            monotonic_clock=clock.monotonic_clock,
            sleeper=clock.sleep,
            normalizer=normalizer,
        ),
        client=client,
        candidate_selector=lambda run_id, limit: capture_store.select_capture_candidates(
            ingestion_run_id=run_id, limit=limit
        ),
        store=postgres_public_threshold_ladder_store(engine),
        normalizer=normalizer,
        event_normalizer=KalshiPublicEventNormalizer(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
    ).scan(
        PublicThresholdLadderScanRequest(
            scan_id=scan_id,
            event_count=5,
            maximum_pages=3,
            market_limit=100,
            maximum_enrichments=20,
            maximum_markets_per_event=8,
            rounds=3,
            interval_seconds=10,
            maximum_depth=5,
            maximum_bundle_quantity="10",
            maximum_observation_skew_seconds=5,
            timeout_seconds=10,
        )
    )


def test_0013_schema_isolated_and_empty_downgrade_reupgrade(postgres_engine: Engine) -> None:
    assert THRESHOLD_TABLES.issubset(inspect(postgres_engine).get_table_names())
    financial_tables = {"campaign", "ledger_journal", "risk_decision", "order", "position"}
    for table_name in THRESHOLD_TABLES:
        assert not financial_tables.intersection(
            foreign_key["referred_table"]
            for foreign_key in inspect(postgres_engine).get_foreign_keys(table_name)
        )

    _downgrade(postgres_engine, REVISION_0012)
    try:
        assert _revision(postgres_engine) == REVISION_0012
        assert THRESHOLD_TABLES.isdisjoint(inspect(postgres_engine).get_table_names())
        _migrate(postgres_engine, "head")
        assert _revision(postgres_engine) == HEAD_REVISION
    finally:
        _migrate(postgres_engine, "head")


def test_threshold_evidence_is_idempotent_immutable_restart_safe_and_financially_isolated(
    postgres_engine: Engine,
) -> None:
    financial_tables = (
        "campaign",
        "ledger_journal",
        "risk_decision",
        "risk_reservation",
        "order",
        "execution_event",
        "position",
        "settlement_fact",
        "position_settlement",
    )
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in financial_tables
        }

    first = _scan(postgres_engine, scan_id="postgres-threshold-evidence")
    replay = _scan(postgres_engine, scan_id="postgres-threshold-evidence")
    restarted_engine = create_postgres_engine(PostgresSettings.from_env().for_test())
    try:
        restarted = _scan(restarted_engine, scan_id="postgres-threshold-evidence")
    finally:
        restarted_engine.dispose()

    assert first.scan.state.value == "COMPLETED"
    assert first.scan.relationship_count == 8
    assert first.scan.evaluation_count == 15
    assert first.scan.candidate_count == 3
    assert first.scan.persisting_candidate_count == 1
    assert replay.idempotent_replay
    assert restarted.idempotent_replay
    assert restarted.evaluations == first.evaluations
    assert restarted.ranking == first.ranking
    source = RetainedSharedOpportunitySource(postgres_engine)
    projections = source.kalshi_projections(
        evaluation_ids=(),
        as_of=datetime(2026, 7, 20, 14, 1, tzinfo=UTC),
    )
    related_projections = tuple(
        item
        for item in projections
        if item.strategy_or_method_id == "KALSHI_RELATED_MARKET_CONSISTENCY_V1"
    )
    exhaustive_projections = tuple(
        item
        for item in projections
        if item.strategy_or_method_id == "KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1"
    )
    assert len(related_projections) == 5
    assert all(
        item.strategy_or_method_id == "KALSHI_RELATED_MARKET_CONSISTENCY_V1"
        and item.direction == "BUY_BROADER_YES_AND_NARROWER_NO"
        and item.expected_gross_return is None
        and item.expected_net_return is None
        and not item.paper_eligible
        and not item.live_eligible
        and item.authority == "NONE"
        for item in related_projections
    )
    assert any(item.source_rejection_reasons == () for item in related_projections)
    assert 0 < len(exhaustive_projections) <= 100
    assert all(
        item.direction == "BUY_ONE_YES_IN_EVERY_EXHAUSTIVE_OUTCOME"
        and item.product_or_market_id == f"{item.instrument_id}:COMPLETE_SET"
        and item.expected_gross_return is None
        and item.expected_net_return is None
        and not item.paper_eligible
        and not item.live_eligible
        and item.authority == "NONE"
        for item in exhaustive_projections
    )
    library = source.strategy_library()
    kalshi_library = next(
        item for item in library if item["method_id"] == "KALSHI_RELATED_MARKET_CONSISTENCY_V1"
    )
    assert kalshi_library["current_record_id"] == first.scan.scan_id
    assert kalshi_library["eligible_result_count"] == 3
    assert kalshi_library["scan_breadth_summary"] == (
        "16 markets · 5 selected events · 8 relationships · 15 evaluations"
    )
    assert kalshi_library["scan_breadth"]["markets_considered"] == 16
    exhaustive_library = next(
        item
        for item in library
        if item["method_id"] == "KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1"
    )
    assert exhaustive_library["current_record_id"] == first.scan.scan_id
    assert exhaustive_library["result_count"] == len(exhaustive_projections)
    assert exhaustive_library["paper_ready_count"] == 0
    assert exhaustive_library["live_authority"] == "NONE"
    with pytest.raises(PublicThresholdLadderConflictError, match="configuration conflicts"):
        postgres_public_threshold_ladder_store(postgres_engine).report(
            PublicThresholdLadderScanRequest(
                scan_id="postgres-threshold-evidence",
                event_count=4,
            )
        )
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM threshold_ladder_scan")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM threshold_market_relationship")) == 8
        assert connection.scalar(sa.text("SELECT count(*) FROM threshold_bundle_evaluation")) == 15
        assert connection.scalar(sa.text("SELECT count(*) FROM threshold_matched_slice")) == 3
        assert connection.scalar(sa.text("SELECT count(*) FROM normalized_event_snapshot")) == 17
        assert connection.scalar(sa.text("SELECT count(*) FROM event_market_relationship")) == 17
        for table, count in before.items():
            assert connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) == count
    with (
        postgres_engine.begin() as connection,
        pytest.raises(sa.exc.DBAPIError, match="immutable Apex row"),
    ):
        connection.execute(sa.text("UPDATE threshold_bundle_evaluation SET matched_quantity = 99"))


def test_retained_threshold_evidence_blocks_0013_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    _downgrade(postgres_engine, REVISION_0013)
    try:
        _scan(postgres_engine, scan_id="postgres-threshold-downgrade")
        with postgres_engine.connect() as connection:
            tables_before = set(inspect(postgres_engine).get_table_names())
            evidence_before = {
                table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                for table in (*sorted(THRESHOLD_TABLES), "ledger_journal", "ledger_posting")
            }

        with pytest.raises(Exception, match="retained threshold ladder evidence"):
            _downgrade(postgres_engine, REVISION_0012)

        assert _revision(postgres_engine) == REVISION_0013
        assert set(inspect(postgres_engine).get_table_names()) == tables_before
        with postgres_engine.connect() as connection:
            evidence_after = {
                table: connection.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                for table in evidence_before
            }
        assert evidence_after == evidence_before
    finally:
        _migrate(postgres_engine, "head")


def test_threshold_bundle_evidence_rehydrates_a_genuinely_persisted_non_candidate_row(
    postgres_engine: Engine,
) -> None:
    """Reproduces the live recurring-worker crash on the exact retained-data
    shape: a genuinely-scanned, genuinely-persisted non-``CANDIDATE``
    ``threshold_bundle_evaluation`` row (156/169 of the retained rows on the
    live database are ``NO_CANDIDATE``, 13/169 ``INELIGIBLE``, 0 ``CANDIDATE``
    as of 2026-08-19) must rehydrate through
    ``PostgresKalshiBaselineQuoteEvidenceSource.threshold_bundle_evidence`` --
    the exact call ``SyncKalshiBaselineQuoteEvidence.execute`` makes every
    recurring cycle for every still-pending KALSHI ``ThresholdBundleEvaluation``
    decision -- without raising.

    Before the fix this reproduced the live
    ``AttributeError: 'str' object has no attribute 'value'``: Postgres
    returns the ``reason_codes`` ``ARRAY(Text)`` column as plain ``str``
    values, but ``ThresholdBundleEvaluation.__post_init__`` calls
    ``_evaluation_reasons``, which sorts by ``value.value`` -- a real
    ``ThresholdEvaluationReasonCode`` member is required, not a bare string.
    The row is produced by the real, unmodified ``ScanPublicThresholdLadders``
    pipeline (same helper as the idempotency test above), never a
    hand-crafted INSERT, so the reason codes are written exactly as the live
    worker writes them."""
    report = _scan(postgres_engine, scan_id="postgres-threshold-baseline-quote-hydration")
    non_candidate = next(
        evaluation
        for evaluation in report.evaluations
        if evaluation.state is not ThresholdEvaluationState.CANDIDATE
    )
    assert non_candidate.reason_codes
    assert all(
        isinstance(code, ThresholdEvaluationReasonCode) for code in non_candidate.reason_codes
    )

    source = PostgresKalshiBaselineQuoteEvidenceSource(postgres_engine)
    hydrated = source.threshold_bundle_evidence((non_candidate.evaluation_id,))

    evaluation, _slices, broader_ticker, narrower_ticker = hydrated[non_candidate.evaluation_id]
    assert evaluation.evaluation_id == non_candidate.evaluation_id
    assert evaluation.state is non_candidate.state
    assert evaluation.reason_codes == non_candidate.reason_codes
    assert all(isinstance(code, ThresholdEvaluationReasonCode) for code in evaluation.reason_codes)
    assert broader_ticker and narrower_ticker
