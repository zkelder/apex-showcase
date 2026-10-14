"""Strategy PAPER Trial Outcome Assessment v1 against real PostgreSQL.

The legacy scenario drives the production-shaped generation-1 trial
(``test_postgres_strategy_paper_trial_closure_readiness._ProductionShapedTrial``,
byte-identical to the retained production trial) through its real horizon
close, then runs the real recurring ``STRATEGY_TRIAL_OUTCOME_ASSESSMENT``
executor branch. The bound scenario drives a new, provenance-bound trial
through the real recurring dispatch (``_ClosedTrialOne``). Neither is a
manually simulated production close; tonight's natural close is a separate,
later read-only proof.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from test_postgres_strategy_paper_trial_closure_readiness import (
    CLOSE_DAY_AS_OF,
    CLOSE_FILL_TICK_AT,
    CLOSE_REPLAY_TICK_AT,
    PRODUCTION_BASE_QUANTITY,
    PRODUCTION_OPENING_ORDER_ID,
    PRODUCTION_TRIAL_ID,
    _expected_close_economics,
    _ProductionShapedTrial,
    _state,
)
from test_postgres_strategy_paper_trial_recurring import (
    DAY1,
    _bootstrap_universe_and_candles,
    _ClosedTrialOne,
    _Executors,
    _plan,
)

from apex.adapters.strategy_trial_outcome_assessment_postgres import (
    PostgresStrategyTrialOutcomeAssessmentRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    PostgresStrategyVersionProvenanceRepository,
)
from apex.application.strategy_trial_outcome_assessment import (
    StrategyTrialOutcomeAssessmentResearchError,
)
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingTaskStatus,
    OperatingTaskType,
)
from apex.domain.performance_attribution import PaperEvidenceClass
from apex.domain.strategy_paper_trial import closed_position_attribution_source_hash
from apex.domain.strategy_trial_outcome_assessment import (
    STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1,
    ExpectedReturnBasis,
    StrategyTrialOutcomeAssessment,
    StrategyTrialOutcomeStatus,
    TrialEconomicsState,
    _content_hash,
    strategy_trial_outcome_assessment_id,
)
from apex.domain.strategy_version_provenance import TrialProvenanceClass

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
TABLE = "strategy_trial_outcome_assessment"
R0116 = "0116_securities_followup_lag"
R0117 = "0117_trial_outcome_assessment"
R0118 = "0118_evidence_gap_recovery"


def _assessment_task(cycle_id: str) -> DueTask:
    return DueTask.create(
        task_type=OperatingTaskType.STRATEGY_TRIAL_OUTCOME_ASSESSMENT,
        disposition=DueDisposition.DUE,
        reasons=("STRATEGY_TRIAL_OUTCOME_ASSESSMENT_MANAGED_EVERY_CYCLE",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="LOCAL_RETAINED_READS_ONLY_NO_PROVIDER_REQUEST",
    )


def _assess(env: _Executors, *, as_of: datetime, executed_at: datetime):  # type: ignore[no-untyped-def]
    cycle_id = f"outcome-assessment-{as_of:%Y%m%d}"
    return env.executor.execute(
        task=_assessment_task(cycle_id),
        cycle_id=cycle_id,
        plan=_plan(created_at=as_of),
        as_of=as_of,
        executed_at=executed_at,
    )


def _rows(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {TABLE}")) or 0)  # nosec B608


def _config(connection: sa.Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _rehash(item: StrategyTrialOutcomeAssessment) -> StrategyTrialOutcomeAssessment:
    return replace(item, canonical_hash=_content_hash(item))


@pytest.fixture
def env(postgres_engine: Engine) -> _Executors:
    return _Executors(postgres_engine)


def _closed_production_trial(env: _Executors) -> _ProductionShapedTrial:
    driven = _ProductionShapedTrial(env)
    driven.submit_close()
    driven.fill_close()
    assert driven.closure() is not None
    return driven


class TestProductionShapedLegacyClose:
    def test_legacy_close_gets_exactly_one_honest_predates_assessment(
        self, env: _Executors
    ) -> None:
        """Requirements 1, 2, 4, 9 (the trial closed before the assessment
        task ever ran), 12 and 15."""

        driven = _closed_production_trial(env)
        closure = driven.closure()
        assert closure is not None
        expected = _expected_close_economics()
        assert _rows(env.engine) == 0

        first = _assess(
            env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT + timedelta(minutes=1)
        )
        assert first.status is OperatingTaskStatus.COMPLETED
        assert first.network_requests == 0
        assert "LIVE_AUTHORITY_NONE" in first.warnings
        assert "NO_PROVIDER_REQUEST" in first.warnings
        assert "STRATEGY_TRIAL_OUTCOME_ASSESSMENTS_WRITTEN:1" in first.warnings
        (assessment_id,) = first.output_identities
        assert assessment_id == strategy_trial_outcome_assessment_id(
            trial_id=PRODUCTION_TRIAL_ID, policy_version=STRATEGY_TRIAL_OUTCOME_ASSESSMENT_POLICY_V1
        )
        repository = PostgresStrategyTrialOutcomeAssessmentRepository(env.engine)
        assessment = repository.get(assessment_id=assessment_id)
        assert assessment is not None

        assert assessment.status is StrategyTrialOutcomeStatus.PREDATES_STRATEGY_PROVENANCE
        assert assessment.provenance_class is TrialProvenanceClass.PREDATES_STRATEGY_PROVENANCE
        assert assessment.trial_legacy_adopted is True
        assert assessment.generation == 1
        assert (
            assessment.trial_hash
            == driven.trial.canonical_hash
            == ("701cfaeea9939e9b1ae116d39249b0fc79d2399ef0e1bbc7529a663e5638e4a8")
        )
        # Never fabricated into a bound version, transition, or evaluation.
        assert assessment.definition_hash is None
        assert assessment.transition_id is None
        assert assessment.evaluation_id is None
        assert assessment.population_id is None and assessment.population_size == 0
        assert (
            PostgresStrategyVersionProvenanceRepository(env.engine).trial_provenance(
                trial_id=PRODUCTION_TRIAL_ID
            )
            is None
        )
        assert assessment.expected_return_basis is (
            ExpectedReturnBasis.NO_JUSTIFYING_EVALUATION_RETAINED
        )

        # Economics: independently derived, exact Decimals.
        assert assessment.closure_id == closure.closure_id
        assert assessment.closure_hash == closure.canonical_hash
        assert assessment.closed_at == closure.closed_at == CLOSE_FILL_TICK_AT
        assert assessment.economics_state is TrialEconomicsState.RECONCILED
        assert assessment.opening_base_quantity == PRODUCTION_BASE_QUANTITY
        assert assessment.opening_cost == Decimal("9.98")
        assert assessment.closing_base_quantity == PRODUCTION_BASE_QUANTITY
        assert assessment.closing_quote_quantity == expected["quote"]
        assert assessment.closing_fees == expected["fee"]
        assert assessment.closing_net_proceeds == expected["net_proceeds"]
        assert assessment.ledger_realized_pnl == expected["realized"] == closure.realized_paper_pnl
        assert assessment.cash_flow_pnl == expected["net_proceeds"] - Decimal("9.98")
        assert assessment.rounding_residual == (
            expected["net_proceeds"] - Decimal("9.98") - expected["realized"]
        )
        assert assessment.realized_return == (expected["realized"] / Decimal("9.98")).quantize(
            Decimal("0.00000001")
        )

        # Final attribution is the superseding POSITION_CLOSED rows.
        state = _state(env)
        orders = {item.order_id: item for item in state.orders}
        opening_final = env.performance.attribution_for_source(
            PRODUCTION_OPENING_ORDER_ID,
            closed_position_attribution_source_hash(
                orders[PRODUCTION_OPENING_ORDER_ID].canonical_hash
            ),
            PaperEvidenceClass.GENUINE_PAPER,
        )
        assert opening_final is not None
        assert assessment.opening_attribution_id == opening_final.attribution_id
        assert assessment.opening_attribution_hash == opening_final.canonical_hash
        assert assessment.closing_attribution_id is not None
        assert assessment.authority == "NONE"

        # Replay / duplicate closure delivery: nothing new, ever.
        driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        again = _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert again.status is OperatingTaskStatus.REUSED
        assert again.output_identities == ()
        assert _rows(env.engine) == 1
        assert repository.get(assessment_id=assessment_id) == assessment

    def test_assessment_waits_for_the_closure_and_ignores_the_open_trial(
        self, env: _Executors
    ) -> None:
        """Requirements 10 and 11: while the SELL is in flight (and after a
        submitted-but-unfilled close) nothing is assessed."""

        driven = _ProductionShapedTrial(env)
        opened = _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT)
        assert opened.output_identities == ()
        assert "STRATEGY_TRIAL_OUTCOME_ACTIVE_TRIALS:1" in opened.warnings
        assert opened.awaits_intraday_continuation is True
        driven.submit_close()
        in_flight = _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT)
        assert in_flight.output_identities == ()
        assert driven.closure() is None
        assert _rows(env.engine) == 0
        driven.fill_close()
        closed = _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert len(closed.output_identities) == 1

    def test_failing_assessment_never_breaks_the_close(
        self, env: _Executors, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement 13 through the real executor: the assessment write
        fails every time, yet the close fills, the closure is written, and
        reconciliation stays CLEAN."""

        driven = _ProductionShapedTrial(env)

        def broken(*_: object, **__: object) -> bool:
            raise StrategyTrialOutcomeAssessmentResearchError("INJECTED_ASSESSMENT_FAILURE")

        monkeypatch.setattr(PostgresStrategyTrialOutcomeAssessmentRepository, "register", broken)
        driven.submit_close()
        filled = driven.fill_close()
        assert "STRATEGY_PAPER_TRIALS_CLOSED:1" in filled.warnings
        assert "STRATEGY_PAPER_RECONCILIATION:CLEAN" in filled.warnings
        failing = _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        # Per-trial isolation: the task itself does not fail; the trial is
        # reported and retried on later ticks.
        assert failing.output_identities == ()
        assert (
            f"STRATEGY_TRIAL_OUTCOME_FAILED:{PRODUCTION_TRIAL_ID}:INJECTED_ASSESSMENT_FAILURE"
            in failing.warnings
        )
        assert failing.awaits_intraday_continuation is True
        assert driven.closure() is not None
        later = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert later.status is not OperatingTaskStatus.FAILED
        assert "STRATEGY_PAPER_RECONCILIATION:CLEAN" in later.warnings
        assert _rows(env.engine) == 0
        monkeypatch.undo()
        recovered = _assess(
            env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT + timedelta(minutes=5)
        )
        assert len(recovered.output_identities) == 1


class TestProvenanceBoundClose:
    def test_bound_trial_persists_exact_lineage_and_insufficient_sample(
        self, env: _Executors
    ) -> None:
        """Requirements 3 and 5, through the real recurring dispatch."""

        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        driven = _ClosedTrialOne(env, _plan(created_at=DAY1))
        closure = driven.closure
        result = _assess(
            env, as_of=closure.closed_at, executed_at=closure.closed_at + timedelta(minutes=1)
        )
        (assessment_id,) = result.output_identities
        assessment = PostgresStrategyTrialOutcomeAssessmentRepository(env.engine).get(
            assessment_id=assessment_id
        )
        assert assessment is not None
        provenance_repository = PostgresStrategyVersionProvenanceRepository(env.engine)
        provenance = provenance_repository.trial_provenance(trial_id=driven.trial_1.trial_id)
        assert provenance is not None
        transition = provenance_repository.transition(transition_id=provenance.transition_id)
        assert transition is not None
        evaluation = env.internal_strategy_evaluations.get(evaluation_id=transition.evaluation_id)
        assert evaluation is not None

        assert assessment.provenance_class is TrialProvenanceClass.BOUND
        assert assessment.status is StrategyTrialOutcomeStatus.INSUFFICIENT_SAMPLE
        assert assessment.population_size == 1
        assert assessment.population_mean_realized_return is None
        assert assessment.definition_hash == provenance.version_ref.definition_hash
        assert assessment.provenance_hash == provenance.canonical_hash
        assert assessment.transition_id == transition.transition_id
        assert assessment.transition_hash == transition.canonical_hash
        assert assessment.evaluation_id == evaluation.evaluation_id
        assert assessment.evaluation_hash == evaluation.canonical_hash
        assert assessment.evaluated_version == evaluation.strategy_version == 1
        assert assessment.evaluation_net_return == evaluation.candidate.net_return
        assert assessment.evaluation_as_of == evaluation.as_of
        assert assessment.expected_return_basis is (
            ExpectedReturnBasis.EVALUATION_WINDOW_NET_RETURN_NOT_HORIZON_MATCHED
        )
        assert assessment.economics_state is TrialEconomicsState.RECONCILED
        assert assessment.ledger_realized_pnl == closure.realized_paper_pnl
        assert assessment.realized_return is not None and assessment.realized_return > 0
        # The trial row and every provenance row are untouched.
        assert env.strategy_paper_trials.get(trial_id=driven.trial_1.trial_id) == driven.trial_1


class TestDatabaseEnforcement:
    def _legacy(self, env: _Executors) -> StrategyTrialOutcomeAssessment:
        _closed_production_trial(env)
        _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        (assessment,) = PostgresStrategyTrialOutcomeAssessmentRepository(env.engine).assessments()
        return assessment

    def test_changed_content_under_the_same_identity_conflicts(self, env: _Executors) -> None:
        """Requirement 8."""

        assessment = self._legacy(env)
        repository = PostgresStrategyTrialOutcomeAssessmentRepository(env.engine)
        assert repository.register(assessment=assessment, assessed_at=CLOSE_REPLAY_TICK_AT)
        changed = _rehash(replace(assessment, realized_return=Decimal("0.5")))
        with pytest.raises(StrategyTrialOutcomeAssessmentResearchError, match="IDENTITY_CONFLICT"):
            repository.register(assessment=changed, assessed_at=CLOSE_REPLAY_TICK_AT)
        assert repository.assessments() == (assessment,)

    def test_rows_are_immutable(self, env: _Executors) -> None:
        self._legacy(env)
        for statement in (
            f"UPDATE {TABLE} SET status = 'ASSESSED'",  # nosec B608
            f"DELETE FROM {TABLE}",  # nosec B608
        ):
            with pytest.raises(sa.exc.DBAPIError), env.engine.begin() as connection:
                connection.execute(sa.text(statement))
        assert _rows(env.engine) == 1

    def test_legacy_row_can_never_carry_a_binding(self, env: _Executors) -> None:
        """Requirement 2 at the database boundary."""

        assessment = self._legacy(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("ALTER TABLE strategy_trial_outcome_assessment DISABLE TRIGGER USER")
            )
        try:
            for column, value in (
                ("transition_id", "'fabricated'"),
                ("status", "'INSUFFICIENT_SAMPLE'"),
                ("provenance_class", "'BOUND'"),
                ("authority", "'PAPER'"),
                ("population_mean_realized_return", "0.1"),
            ):
                with pytest.raises(sa.exc.IntegrityError), env.engine.begin() as connection:
                    connection.execute(
                        sa.text(f"UPDATE {TABLE} SET {column} = {value}")  # nosec B608
                    )
        finally:
            with env.engine.begin() as connection:
                connection.execute(
                    sa.text("ALTER TABLE strategy_trial_outcome_assessment ENABLE TRIGGER USER")
                )
        assert PostgresStrategyTrialOutcomeAssessmentRepository(env.engine).assessments() == (
            assessment,
        )

    def test_a_row_must_name_its_trials_own_closure(self, env: _Executors) -> None:
        assessment = self._legacy(env)
        with env.engine.begin() as connection:
            connection.execute(sa.text(f"ALTER TABLE {TABLE} DISABLE TRIGGER USER"))
        try:
            with pytest.raises(sa.exc.IntegrityError), env.engine.begin() as connection:
                connection.execute(
                    sa.text(f"UPDATE {TABLE} SET closure_hash = :h"),  # nosec B608
                    {"h": "0" * 64},
                )
            with pytest.raises(sa.exc.IntegrityError), env.engine.begin() as connection:
                connection.execute(
                    sa.text(f"UPDATE {TABLE} SET opening_attribution_hash = :h"),  # nosec B608
                    {"h": "0" * 64},
                )
        finally:
            with env.engine.begin() as connection:
                connection.execute(sa.text(f"ALTER TABLE {TABLE} ENABLE TRIGGER USER"))
        assert assessment.closure_hash != "0" * 64


class TestMigration:
    def test_downgrade_refuses_while_an_assessment_is_retained(self, env: _Executors) -> None:
        """Requirement 18."""

        _closed_production_trial(env)
        _assess(env, as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        env.engine.dispose()
        with env.engine.connect() as connection:
            with pytest.raises(Exception, match="cannot downgrade 0117"):
                command.downgrade(_config(connection), R0116)
            connection.rollback()
        with env.engine.connect() as connection:
            revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == R0117
        assert _rows(env.engine) == 1

    def test_empty_round_trip_restores_the_schema(self, postgres_engine: Engine) -> None:
        def schema() -> tuple[object, ...]:
            with postgres_engine.connect() as connection:
                return tuple(
                    connection.execute(
                        sa.text(
                            "SELECT conrelid::regclass::text, conname, "
                            "pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conname LIKE '%outcome%' ORDER BY 1, 2"
                        )
                    ).all()
                )

        at_head = schema()
        assert any(
            "uq_shared_performance_attribution_outcome_target" in str(row) for row in at_head
        )
        postgres_engine.dispose()
        with postgres_engine.connect() as connection:
            command.downgrade(_config(connection), R0116)
            connection.commit()
        with postgres_engine.connect() as connection:
            assert connection.scalar(sa.text(f"SELECT to_regclass('{TABLE}')")) is None
        assert not any("outcome_target" in str(row) for row in schema())
        with postgres_engine.connect() as connection:
            command.upgrade(_config(connection), "head")
            connection.commit()
        assert schema() == at_head
        with postgres_engine.connect() as connection:
            revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        assert revision == R0118
