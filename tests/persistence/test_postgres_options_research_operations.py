"""PostgreSQL persistence proofs for migration 0087: the retained
options-forward-evidence checkpoint round-trips, replays idempotently,
enforces immutability, and blocks a destructive downgrade while evidence is
retained -- built strictly on top of the already-retained genuine
quote-chain plan evidence from migration 0085.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_research_operations_postgres import (
    OPTIONS_RESEARCH_OPERATIONS_TABLES,
    PostgresOptionsResearchOperationsRepository,
)
from apex.application.options_research_operations import RecordOptionsForwardEvidenceCheckpoint
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
)
from apex.domain.options_research_operations import (
    OptionsCaptureWindowPolicy,
    OptionsForwardEvidenceCheckpoint,
)
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "0118_evidence_gap_recovery"
NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
WINDOW_OPEN = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
WINDOW_CLOSE = datetime(2026, 8, 10, 20, 15, tzinfo=UTC)


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _plan() -> OptionChainCapturePlan:
    members = tuple(
        ResolvedCohortMember(
            stable_security_id=f"security-{symbol.lower()}-stable",
            symbol=symbol,
            sector=CohortSector.BROAD_MARKET,
            roles=(CohortRole.BROAD_INDEX_ETF,),
            asset_class="ETF",
        )
        for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS
    )
    return OptionChainCapturePlan.create(
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        members=members,
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=NOW,
    )


def _seed_plan(postgres_engine: Engine) -> OptionChainCapturePlan:
    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    return quote_repository.save_plan(_plan())


def _checkpoint(
    *,
    plan_id: str,
    session_id: str = "2026-08-10",
    underlyings_captured_count: int = 19,
    underlyings_requested_count: int = 19,
    created_at: datetime = NOW,
) -> OptionsForwardEvidenceCheckpoint:
    policy = OptionsCaptureWindowPolicy.create()
    return OptionsForwardEvidenceCheckpoint.create(
        plan_id=plan_id,
        session_id=session_id,
        capture_window_policy_id=policy.policy_id,
        window_open_at=WINDOW_OPEN,
        window_close_at=WINDOW_CLOSE,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        feed="indicative",
        feed_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
        underlying_symbols_captured=DEFAULT_OPTIONS_COHORT_SYMBOLS[:underlyings_captured_count],
        underlyings_requested_count=underlyings_requested_count,
        underlyings_captured_count=underlyings_captured_count,
        fresh_snapshot_ids=tuple(f"snap-{i}" for i in range(underlyings_captured_count)),
        stale_snapshot_count=0,
        quote_quality_counts=(("OBSERVED", 300), ("STALE", 0)),
        chain_coverage_quality_counts=(("GOOD_COVERAGE", underlyings_captured_count),),
        cap_constrained_underlyings=(),
        structure_snapshot_ids=tuple(f"struct-snap-{i}" for i in range(underlyings_captured_count)),
        structures_evaluated=40,
        structures_retained=20,
        fee_policy_id="fee-policy-1",
        fee_evidence_status="EVIDENCED",
        opra_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
        open_interest_limitation="OPEN_INTEREST_UNAVAILABLE",
        iv_history_minimum_required=20,
        iv_history_ready_underlying_count=0,
        forward_history_underlying_count=underlyings_captured_count,
        created_at=created_at,
    )


def test_clean_database_is_at_current_head_with_options_forward_evidence_table(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        OPTIONS_RESEARCH_OPERATIONS_TABLES
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )


def test_checkpoint_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    checkpoint = _checkpoint(plan_id=plan.plan_id)
    RecordOptionsForwardEvidenceCheckpoint(repository).execute(checkpoint=checkpoint)
    RecordOptionsForwardEvidenceCheckpoint(repository).execute(checkpoint=checkpoint)  # idempotent
    fresh = PostgresOptionsResearchOperationsRepository(postgres_engine)
    assert fresh.checkpoint_by_id(checkpoint.checkpoint_id) == checkpoint
    assert fresh.latest_checkpoint(plan.plan_id) == checkpoint
    assert fresh.latest_checkpoint_for_session(plan_id=plan.plan_id, session_id="2026-08-10") == (
        checkpoint
    )
    assert fresh.checkpoints_for_session(plan_id=plan.plan_id, session_id="2026-08-10") == (
        checkpoint,
    )
    assert fresh.checkpoints_for_plan(plan.plan_id) == (checkpoint,)


def test_completed_session_ids_requires_full_underlying_coverage(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    partial = _checkpoint(
        plan_id=plan.plan_id,
        session_id="2026-08-07",
        underlyings_captured_count=10,
        underlyings_requested_count=19,
        created_at=NOW - timedelta(days=3),
    )
    complete = _checkpoint(
        plan_id=plan.plan_id,
        session_id="2026-08-10",
        underlyings_captured_count=19,
        underlyings_requested_count=19,
    )
    repository.save_checkpoint(partial)
    repository.save_checkpoint(complete)
    assert repository.completed_session_ids(plan.plan_id) == frozenset({"2026-08-10"})


def test_latest_checkpoint_for_session_picks_the_most_recently_created(
    postgres_engine: Engine,
) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    first = _checkpoint(
        plan_id=plan.plan_id,
        underlyings_captured_count=10,
        created_at=NOW,
    )
    second = _checkpoint(
        plan_id=plan.plan_id,
        underlyings_captured_count=19,
        created_at=NOW + timedelta(minutes=5),
    )
    repository.save_checkpoint(first)
    repository.save_checkpoint(second)
    latest = repository.latest_checkpoint_for_session(plan_id=plan.plan_id, session_id="2026-08-10")
    assert latest == second
    assert latest is not None
    assert latest.underlyings_captured_count == 19


def test_checkpoint_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    checkpoint = repository.save_checkpoint(_checkpoint(plan_id=plan.plan_id))
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE options_forward_evidence_checkpoint SET structures_retained = 999 "
                "WHERE checkpoint_id = :id"
            ),
            {"id": checkpoint.checkpoint_id},
        )


def test_checkpoint_immutable_trigger_rejects_delete(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    checkpoint = repository.save_checkpoint(_checkpoint(plan_id=plan.plan_id))
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text("DELETE FROM options_forward_evidence_checkpoint WHERE checkpoint_id = :id"),
            {"id": checkpoint.checkpoint_id},
        )


def test_authority_is_always_none(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    checkpoint = repository.save_checkpoint(_checkpoint(plan_id=plan.plan_id))
    assert checkpoint.authority == "NONE"
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT authority FROM options_forward_evidence_checkpoint "
                    "WHERE checkpoint_id = :id"
                ),
                {"id": checkpoint.checkpoint_id},
            )
            == "NONE"
        )


# --------------------------------------------------------------------------
# Migration downgrade protection
# --------------------------------------------------------------------------


def test_retained_evidence_blocks_destructive_0087_downgrade(postgres_engine: Engine) -> None:
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    repository.save_checkpoint(_checkpoint(plan_id=plan.plan_id))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0087 with retained options forward-evidence checkpoints",
        ):
            command.downgrade(_config(connection), "0086_defined_risk_options_eco")
        connection.rollback()
    # Migration 0088 now sits on top of this one and is empty in this
    # test's scenario (nothing here ever touches its tables), so alembic's
    # multi-revision downgrade genuinely completes 0088's own downgrade
    # step (no retained evidence, no guard trip) *before* reaching 0087's
    # guard, which then blocks further progress -- each migration's own
    # downgrade step commits independently, so the rollback above undoes
    # only the failed 0087 step, not the already-completed 0088 one. The
    # database is therefore genuinely left at 0087, not at HEAD_REVISION;
    # asserting that explicitly (rather than "unchanged") is what actually
    # happened, confirmed against the real database -- mirroring the
    # identical, already-reviewed pattern in
    # test_postgres_options_quote_intelligence.py's own
    # test_retained_evidence_blocks_destructive_0085_downgrade.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0087_options_forward_evidence"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0087_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        OPTIONS_RESEARCH_OPERATIONS_TABLES
    )
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0086_defined_risk_options_eco")
    assert set(OPTIONS_RESEARCH_OPERATIONS_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        OPTIONS_RESEARCH_OPERATIONS_TABLES
    )


# --------------------------------------------------------------------------
# Coinbase/Kalshi independence
# --------------------------------------------------------------------------


def test_options_research_operations_never_touches_crypto_or_kalshi_tables(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in ("apex_crypto_paper_account", "apex_kalshi_paper_account")
            if table in sa.inspect(postgres_engine).get_table_names()
        }
    plan = _seed_plan(postgres_engine)
    repository = PostgresOptionsResearchOperationsRepository(postgres_engine)
    repository.save_checkpoint(_checkpoint(plan_id=plan.plan_id))
    with postgres_engine.connect() as connection:
        after = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) for table in before
        }
    assert before == after
