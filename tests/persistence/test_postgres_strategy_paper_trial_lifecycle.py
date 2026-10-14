"""PostgreSQL persistence proofs for Repeatable Strategy PAPER Evidence v1:
migration ``0111``, ``PostgresStrategyPaperTrialRepository``,
``PostgresStrategyPaperTrialClosureRepository``. No network; every input is
a directly-built domain fixture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialClosureRepository,
    PostgresStrategyPaperTrialRepository,
)
from apex.application.strategy_paper_trial_lifecycle import (
    RecordStrategyPaperTrialClosed,
    RecordStrategyPaperTrialOpened,
    StrategyPaperTrialLifecycleResearchError,
)
from apex.domain.strategy_paper_trial_evidence_coverage import (
    TrialEvidenceCoverage,
    evaluate_trial_evidence_coverage,
    is_material_coverage_gap,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    STRATEGY_PAPER_TRIAL_HORIZON_DAYS,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
    build_strategy_paper_trial_closure_record,
    build_strategy_paper_trial_record,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
STRATEGY_ID = "buy-and-hold-btc-usd"
INSTRUMENT_ID = "BTC-USD"
ACCOUNT_ID = "genuine-strategy-crypto-paper-account"


def _coverage(*, window_start: datetime, window_end: datetime) -> TrialEvidenceCoverage:
    """See tests/test_strategy_paper_trial_lifecycle.py's identical helper
    -- a representative coverage summary computed through the real
    evaluate_trial_evidence_coverage rather than a hand-rolled literal.
    This module's own focus is the Postgres persistence round-trip of the
    coverage_* columns, not coverage-computation behavior itself (that has
    its own dedicated test module, tests/
    test_strategy_paper_trial_evidence_coverage.py)."""

    return evaluate_trial_evidence_coverage(
        instrument_id=INSTRUMENT_ID,
        window_start=window_start,
        window_end=window_end,
        observations=(),
    )


def _trial(*, generation: int, opened_at: datetime = NOW) -> StrategyPaperTrialRecord:
    # Strategy Version Provenance v1 (migration 0115): the bare trial
    # repository can store only legacy-adopted rows; a non-legacy trial must
    # be opened together with its provenance binding (see
    # test_postgres_strategy_version_provenance.py and
    # test_bare_non_legacy_trial_insert_is_rejected_at_commit below). These
    # 0111 round-trip/closure proofs are otherwise unchanged.
    return build_strategy_paper_trial_record(
        strategy_id=STRATEGY_ID,
        strategy_version=1,
        instrument_id=INSTRUMENT_ID,
        account_id=ACCOUNT_ID,
        generation=generation,
        opened_at=opened_at,
        opening_authorization_id=f"gen-{generation}-authorization",
        opening_order_id=f"gen-{generation}-order",
        opening_realized_pnl_baseline=Decimal("0"),
        legacy_adopted=True,
    )


class TestTrialRepository:
    def test_bare_non_legacy_trial_insert_is_rejected_at_commit(
        self, postgres_engine: Engine
    ) -> None:
        repository = PostgresStrategyPaperTrialRepository(postgres_engine)
        non_legacy = build_strategy_paper_trial_record(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            generation=1,
            opened_at=NOW,
            opening_authorization_id="gen-1-authorization",
            opening_order_id="gen-1-order",
            opening_realized_pnl_baseline=Decimal("0"),
        )
        with pytest.raises(StrategyPaperTrialLifecycleResearchError):
            RecordStrategyPaperTrialOpened(repository).execute(record=non_legacy)
        assert repository.get(trial_id=non_legacy.trial_id) is None

    def test_register_persists_and_round_trips(self, postgres_engine: Engine) -> None:
        repository = PostgresStrategyPaperTrialRepository(postgres_engine)
        trial = _trial(generation=1)
        result = RecordStrategyPaperTrialOpened(repository).execute(record=trial)
        assert result.idempotent_replay is False
        retained = repository.get(trial_id=trial.trial_id)
        assert retained == trial

    def test_replay_is_idempotent(self, postgres_engine: Engine) -> None:
        repository = PostgresStrategyPaperTrialRepository(postgres_engine)
        trial = _trial(generation=1)
        RecordStrategyPaperTrialOpened(repository).execute(record=trial)
        second = RecordStrategyPaperTrialOpened(repository).execute(record=trial)
        assert second.idempotent_replay is True
        with postgres_engine.connect() as connection:
            count = connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM internal_strategy_paper_trial WHERE trial_id = :trial_id"
                ),
                {"trial_id": trial.trial_id},
            ).scalar_one()
        assert count == 1

    def test_same_trial_id_different_content_conflicts(self, postgres_engine: Engine) -> None:
        repository = PostgresStrategyPaperTrialRepository(postgres_engine)
        trial = _trial(generation=1)
        RecordStrategyPaperTrialOpened(repository).execute(record=trial)
        conflicting = build_strategy_paper_trial_record(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            generation=1,
            opened_at=NOW,
            opening_authorization_id="a-different-authorization",
            opening_order_id="a-different-order",
            opening_realized_pnl_baseline=Decimal("0"),
        )
        assert conflicting.trial_id == trial.trial_id
        with pytest.raises(StrategyPaperTrialLifecycleResearchError):
            RecordStrategyPaperTrialOpened(repository).execute(record=conflicting)

    def test_repeated_generations_persist_distinctly_and_lineage_is_queryable(
        self, postgres_engine: Engine
    ) -> None:
        repository = PostgresStrategyPaperTrialRepository(postgres_engine)
        gen1 = _trial(generation=1, opened_at=NOW)
        gen2 = _trial(generation=2, opened_at=NOW + timedelta(days=40))
        gen3 = _trial(generation=3, opened_at=NOW + timedelta(days=80))
        for trial in (gen1, gen2, gen3):
            RecordStrategyPaperTrialOpened(repository).execute(record=trial)
        history = repository.for_slot(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID
        )
        assert [item.generation for item in history] == [1, 2, 3]
        assert [item.trial_id for item in history] == [gen1.trial_id, gen2.trial_id, gen3.trial_id]

    def test_the_deployment_guards_required_schema_revision_matches_this_migration(self) -> None:
        # Mechanical companion to tests/test_schema_compatibility.py's own
        # broader assertion -- confirms specifically that landing migration
        # 0111 without also bumping REQUIRED_SCHEMA_REVISION would be
        # caught, closing the exact incident class d7ff231 fixed.
        from apex.domain.schema_compatibility import REQUIRED_SCHEMA_REVISION

        assert REQUIRED_SCHEMA_REVISION == "0118_evidence_gap_recovery"


class TestClosureRepository:
    def _opened_trial(
        self, postgres_engine: Engine, *, generation: int = 1
    ) -> tuple[PostgresStrategyPaperTrialRepository, StrategyPaperTrialRecord]:
        trials = PostgresStrategyPaperTrialRepository(postgres_engine)
        trial = _trial(generation=generation)
        RecordStrategyPaperTrialOpened(trials).execute(record=trial)
        return trials, trial

    def test_register_persists_and_round_trips(self, postgres_engine: Engine) -> None:
        trials, trial = self._opened_trial(postgres_engine)
        closures = PostgresStrategyPaperTrialClosureRepository(postgres_engine)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1.23456789"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        result = RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(
            record=closure
        )
        assert result.idempotent_replay is False
        retained = closures.for_trial(trial_id=trial.trial_id)
        assert retained == closure
        assert retained is not None
        assert retained.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT

    def test_coverage_fields_round_trip_with_genuine_nonzero_values(
        self, postgres_engine: Engine
    ) -> None:
        # Distinct from the round-trip test above (which uses the
        # degenerate zero-observation coverage helper every other test in
        # this file uses): proves REAL, non-trivial coverage values --
        # including a non-round largest_gap_seconds that is_material_
        # coverage_gap would derive as material -- survive a genuine
        # Postgres write+read round trip byte-for-byte, not just a
        # None/zero shape.
        trials, trial = self._opened_trial(postgres_engine)
        closures = PostgresStrategyPaperTrialClosureRepository(postgres_engine)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        coverage = evaluate_trial_evidence_coverage(
            instrument_id=INSTRUMENT_ID,
            window_start=trial.opened_at,
            window_end=closed_at,
            observations=(),
        )
        # zero observations over a 30-day window
        assert is_material_coverage_gap(coverage.largest_gap_seconds) is True
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1.23456789"),
            coverage=coverage,
        )
        RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(record=closure)
        retained = closures.for_trial(trial_id=trial.trial_id)
        assert retained is not None
        assert retained.coverage_window_duration_seconds == coverage.window_duration_seconds
        assert retained.coverage_nominal_days == coverage.nominal_days
        assert retained.coverage_covered_days == coverage.covered_days == 0
        assert retained.coverage_qualifying_observation_count == 0
        assert retained.coverage_largest_gap_seconds == coverage.largest_gap_seconds
        assert is_material_coverage_gap(retained.coverage_largest_gap_seconds) is True
        assert retained.coverage_policy_version == coverage.policy_version
        assert retained == closure

    def test_replay_is_idempotent(self, postgres_engine: Engine) -> None:
        trials, trial = self._opened_trial(postgres_engine)
        closures = PostgresStrategyPaperTrialClosureRepository(postgres_engine)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1.5"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        service = RecordStrategyPaperTrialClosed(trials=trials, closures=closures)
        service.execute(record=closure)
        second = service.execute(record=closure)
        assert second.idempotent_replay is True
        with postgres_engine.connect() as connection:
            count = connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM internal_strategy_paper_trial_closure "
                    "WHERE trial_id = :trial_id"
                ),
                {"trial_id": trial.trial_id},
            ).scalar_one()
        assert count == 1

    def test_db_level_unique_constraint_refuses_a_second_closure_row_for_one_trial(
        self, postgres_engine: Engine
    ) -> None:
        # The load-bearing DB-enforced proof behind "a terminal trial
        # cannot be closed twice" -- bypasses the application-layer guard
        # entirely (a raw second INSERT with a genuinely distinct
        # closure_id but the SAME trial_id) to prove the UNIQUE(trial_id)
        # constraint from migration 0111 itself is what ultimately refuses
        # it, not merely the cooperating application check.
        trials, trial = self._opened_trial(postgres_engine)
        closures = PostgresStrategyPaperTrialClosureRepository(postgres_engine)
        first_closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        first = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=first_closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1"),
            coverage=_coverage(window_start=trial.opened_at, window_end=first_closed_at),
        )
        RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(record=first)
        with postgres_engine.begin() as connection, pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO internal_strategy_paper_trial_closure "
                    "(closure_id, trial_id, strategy_id, instrument_id, closed_at, "
                    "terminal_reason, closing_authorization_id, closing_order_id, "
                    "realized_paper_pnl, next_eligible_at, canonical_hash, authority) "
                    "VALUES (:closure_id, :trial_id, :strategy_id, :instrument_id, :closed_at, "
                    "'TRIAL_HORIZON_EXIT', 'x', 'y', 0, :closed_at, :hash, 'NONE')"
                ),
                {
                    "closure_id": f"{trial.trial_id}-a-second-distinct-closure-id",
                    "trial_id": trial.trial_id,
                    "strategy_id": trial.strategy_id,
                    "instrument_id": trial.instrument_id,
                    "closed_at": NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS + 1),
                    "hash": "b" * 64,
                },
            )

    def test_closing_an_unregistered_trial_fails_closed(self, postgres_engine: Engine) -> None:
        trials = PostgresStrategyPaperTrialRepository(postgres_engine)
        closures = PostgresStrategyPaperTrialClosureRepository(postgres_engine)
        phantom_trial = _trial(generation=1)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=phantom_trial,
            closed_at=closed_at,
            closing_authorization_id="a",
            closing_order_id="o",
            realized_paper_pnl=Decimal("1"),
            coverage=_coverage(window_start=phantom_trial.opened_at, window_end=closed_at),
        )
        with pytest.raises(StrategyPaperTrialLifecycleResearchError):
            RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(record=closure)

    def test_immutability_trigger_refuses_an_update(self, postgres_engine: Engine) -> None:
        # Both new tables carry the same BEFORE UPDATE OR DELETE trigger
        # every recent Apex evidence table carries (migration 0111's own
        # immutable_trigger_ddl calls) -- proven directly here rather than
        # only trusted from the migration source.
        trials, trial = self._opened_trial(postgres_engine)
        closures = PostgresStrategyPaperTrialClosureRepository(postgres_engine)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-authorization",
            closing_order_id="close-order",
            realized_paper_pnl=Decimal("1"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(record=closure)
        with (
            postgres_engine.begin() as connection,
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
        ):
            connection.execute(
                sa.text(
                    "UPDATE internal_strategy_paper_trial_closure "
                    "SET realized_paper_pnl = 999 WHERE closure_id = :closure_id"
                ),
                {"closure_id": closure.closure_id},
            )
