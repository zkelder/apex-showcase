"""PostgreSQL proofs for Strategy Version Provenance v1 (migration 0115).

Every constraint below is exercised with raw SQL as well as through the
repository, so the guarantees hold even for a writer that bypasses the
domain layer.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.strategy_evaluation_history_postgres import (
    PostgresInternalStrategyEvaluationRepository,
)
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    STRATEGY_VERSION_PROVENANCE_TABLES,
    PostgresStrategyVersionProvenanceRepository,
)
from apex.application.strategy_evaluation_history import RecordInternalStrategyEvaluation
from apex.application.strategy_paper_trial_lifecycle import RecordStrategyPaperTrialOpened
from apex.application.strategy_version_provenance import (
    RecordEvaluationSubject,
    RecordProvenancedTrialOpened,
    RecordStrategyVersionTransition,
    StrategyVersionProvenanceResearchError,
    strategy_version_provenance_report,
)
from apex.domain.crypto_research import CryptoDailyCandle
from apex.domain.research_evidence import EvidenceClass
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks, simulate_strategy_backtest
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyLifecycleState,
    next_version,
)
from apex.domain.strategy_evaluation import evaluate_external_strategy, promote_to_paper_eligible
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialRecord,
    build_strategy_paper_trial_record,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionRef,
    StrategyVersionRegistry,
    StrategyVersionTransitionKind,
    build_strategy_paper_trial_provenance,
    build_strategy_version_record,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 2, tzinfo=UTC)
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CAPTURE_ID = "test-capture-strategy-version-provenance-postgres"
CATALOG = StrategyVersionRegistry.CATALOG_CODE
INTERNAL = StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION


def _candles(count: int) -> tuple[CryptoDailyCandle, ...]:
    return tuple(
        CryptoDailyCandle.create(
            capture_id=CAPTURE_ID,
            attempt_id=f"attempt-{offset}",
            evidence_class=EvidenceClass.FIXTURE,
            bucket_start=_EPOCH + timedelta(days=offset),
            open=str(100 + 5 * offset),
            high=str(100 + 5 * offset),
            low=str(100 + 5 * offset),
            close=str(100 + 5 * offset),
            volume="1",
            ingested_at=NOW,
            raw_response_hash="a" * 64,
        )
        for offset in range(count)
    )


def _edited(definition: StrategyDefinition) -> StrategyDefinition:
    return StrategyDefinition.create(
        source_kind=definition.source_kind,
        display_name="Buy and Hold BTC-USD (edited later)",
        venue=definition.venue,
        instrument_ids=definition.instrument_ids,
        horizon_kind=definition.horizon_kind,
        rule_kind=definition.rule_kind,
        rule_parameters=definition.rule_parameters,
        evidence_requirements=definition.evidence_requirements,
        provenance=definition.provenance,
        created_at=definition.created_at,
    )


def _chain(engine: Engine, definition: StrategyDefinition | None = None):  # type: ignore[no-untyped-def]
    """The recurring cycle's exact write order, against real PostgreSQL."""

    definition = definition or canonical_strategy_benchmarks(created_at=NOW)[0]
    provenance = PostgresStrategyVersionProvenanceRepository(engine)
    backtest = simulate_strategy_backtest(
        definition,
        _candles(35),
        evidence_class=EvidenceClass.FIXTURE,
        evidence_provenance=CAPTURE_ID,
    )
    evaluation = evaluate_external_strategy(definition, backtest)
    recorded = RecordInternalStrategyEvaluation(
        PostgresInternalStrategyEvaluationRepository(engine)
    ).execute(
        definition=definition,
        candidate=evaluation,
        cycle_id="cycle-2026-09-02",
        as_of=NOW,
        capture_id=CAPTURE_ID,
        evaluated_at=NOW,
    )
    RecordEvaluationSubject(provenance).execute(
        definition=definition,
        registry=CATALOG,
        evaluation_source=INTERNAL,
        evaluation_id=recorded.record.evaluation_id,
        evaluation_hash=recorded.record.canonical_hash,
        evaluated_strategy_id=recorded.record.strategy_id,
        evaluated_strategy_version=recorded.record.strategy_version,
    )
    promoted = promote_to_paper_eligible(definition, evaluation=evaluation, promoted_at=NOW)
    admission = RecordStrategyVersionTransition(provenance).execute(
        kind=StrategyVersionTransitionKind.CYCLE_GATE_ADMISSION,
        from_definition=definition,
        to_definition=promoted,
        evaluation_source=INTERNAL,
        evaluation_id=recorded.record.evaluation_id,
        evaluation=evaluation,
        registry=CATALOG,
    )
    return provenance, recorded, promoted, admission


def _trial(
    promoted: StrategyDefinition, *, legacy_adopted: bool = False
) -> StrategyPaperTrialRecord:
    return build_strategy_paper_trial_record(
        strategy_id=promoted.strategy_id,
        strategy_version=promoted.version,
        instrument_id=promoted.instrument_ids[0],
        account_id="genuine-strategy-crypto-paper-account",
        generation=1,
        opened_at=NOW,
        opening_authorization_id="gen-1-authorization",
        opening_order_id="gen-1-order",
        opening_realized_pnl_baseline=Decimal("0"),
        legacy_adopted=legacy_adopted,
    )


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())  # nosec B608
            for table in (*STRATEGY_VERSION_PROVENANCE_TABLES, "internal_strategy_paper_trial")
        }


class TestRepository:
    def test_full_chain_round_trips_and_replays_with_zero_new_rows(
        self, postgres_engine: Engine
    ) -> None:
        provenance, recorded, promoted, admission = _chain(postgres_engine)
        trial = _trial(promoted)
        opened = RecordProvenancedTrialOpened(provenance).execute(
            trial=trial, transition_id=admission.transition.transition_id
        )
        assert opened.idempotent_replay is False
        before = _counts(postgres_engine)
        assert before == {
            "strategy_version": 2,
            "strategy_evaluation_subject": 1,
            "strategy_version_transition": 1,
            "strategy_paper_trial_provenance": 1,
            "internal_strategy_paper_trial": 1,
        }

        # Replay the entire chain (a later same-day tick): zero new rows,
        # byte-identical provenance.
        _p, _r, _pr, replayed_admission = _chain(postgres_engine)
        assert replayed_admission.idempotent_replay is True
        assert replayed_admission.transition == admission.transition
        replay = RecordProvenancedTrialOpened(provenance).execute(
            trial=trial, transition_id=admission.transition.transition_id
        )
        assert replay.idempotent_replay is True
        assert _counts(postgres_engine) == before

        assert provenance.transition(transition_id=admission.transition.transition_id) == (
            admission.transition
        )
        assert provenance.version(ref=admission.version.ref) == admission.version
        bound = provenance.trial_provenance(trial_id=trial.trial_id)
        assert bound == opened.provenance
        assert PostgresStrategyPaperTrialRepository(postgres_engine).get(
            trial_id=trial.trial_id
        ) == (trial)

        report = strategy_version_provenance_report(
            provenance=provenance,
            strategy_id=promoted.strategy_id,
            trials=PostgresStrategyPaperTrialRepository(postgres_engine).for_strategy(
                strategy_id=promoted.strategy_id
            ),
        )
        trials = report["trials"]
        assert isinstance(trials, list)
        assert trials[0]["provenance_class"] == "BOUND"
        assert trials[0]["justifying_evaluation_id"] == recorded.record.evaluation_id
        assert (
            trials[0]["evaluated_definition_hash"] == admission.transition.from_ref.definition_hash
        )
        assert report["warnings"] == []

    def test_composite_write_is_atomic(self, postgres_engine: Engine) -> None:
        """A provenance row that the database refuses (transition absent)
        leaves no trial row behind either."""

        provenance, _recorded, promoted, admission = _chain(postgres_engine)
        trial = _trial(promoted)
        bound = build_strategy_paper_trial_provenance(trial=trial, transition=admission.transition)
        forged = dataclasses.replace(
            bound,
            transition_id="strategy_version_transition_never_retained",
        )
        with pytest.raises(StrategyVersionProvenanceResearchError):
            provenance.record_trial_with_provenance(trial=trial, provenance=forged)
        assert _counts(postgres_engine)["internal_strategy_paper_trial"] == 0
        assert _counts(postgres_engine)["strategy_paper_trial_provenance"] == 0

    def test_legacy_trial_stays_insertable_without_provenance(
        self, postgres_engine: Engine
    ) -> None:
        """Requirement 9: the constraint trigger exempts legacy-adopted rows,
        so the production generation-1 trial's adoption path is unaffected."""

        promoted = _chain(postgres_engine)[2]
        legacy = _trial(promoted, legacy_adopted=True)
        RecordStrategyPaperTrialOpened(
            PostgresStrategyPaperTrialRepository(postgres_engine)
        ).execute(record=legacy)
        report = strategy_version_provenance_report(
            provenance=PostgresStrategyVersionProvenanceRepository(postgres_engine),
            strategy_id=promoted.strategy_id,
            trials=(legacy,),
        )
        trials = report["trials"]
        assert isinstance(trials, list)
        assert trials[0]["provenance_class"] == "PREDATES_STRATEGY_PROVENANCE"


class TestDatabaseEnforcement:
    def test_artifact_must_hash_to_definition_hash(self, postgres_engine: Engine) -> None:
        provenance, *_ = _chain(postgres_engine)
        root = canonical_strategy_benchmarks(created_at=NOW)[0]
        record = provenance.version(ref=StrategyVersionRef.of(root, registry=CATALOG))
        assert record is not None
        tampered = record.artifact.decode().replace("Buy and Hold", "Sell and Hold")
        with (
            pytest.raises(sa.exc.IntegrityError, match="ck_strategy_version_artifact_hash"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO strategy_version (registry, strategy_id, version, "
                    "definition_hash, lifecycle_state, artifact, canonical_hash, "
                    "recorded_at, authority) VALUES ('EXTERNAL_OPERATOR', :sid, 1, :hash, "
                    "'RESEARCH_ONLY', :artifact, :chash, now(), 'NONE')"
                ),
                {
                    "sid": root.strategy_id,
                    "hash": root.canonical_hash,
                    "artifact": tampered,
                    "chash": "0" * 64,
                },
            )

    def test_transition_cannot_bind_an_evaluation_of_another_artifact(
        self, postgres_engine: Engine
    ) -> None:
        """Requirement 4 at the database boundary. B and B's own v2 are both
        validly retained; a raw transition claiming that A's evaluation
        justified B -> B-v2 is refused, specifically by the composite foreign
        key onto strategy_evaluation_subject."""

        provenance, recorded, _promoted, _admission = _chain(postgres_engine)
        root = canonical_strategy_benchmarks(created_at=NOW)[0]
        other = _edited(root)
        other_promoted = next_version(
            other, lifecycle_state=StrategyLifecycleState.PAPER_ELIGIBLE, created_at=NOW
        )
        provenance.record_version(
            record=build_strategy_version_record(definition=other, registry=CATALOG, parent=None)
        )
        provenance.record_version(
            record=build_strategy_version_record(
                definition=other_promoted, registry=CATALOG, parent=other
            )
        )
        with (
            pytest.raises(
                sa.exc.IntegrityError, match="fk_strategy_version_transition_evaluated_artifact"
            ),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO strategy_version_transition (transition_id, kind, registry, "
                    "strategy_id, from_version, from_definition_hash, to_version, "
                    "to_definition_hash, evaluation_source, evaluation_id, evaluation_hash, "
                    "gate_policy_version, canonical_hash, recorded_at, authority) VALUES "
                    "('forged', 'CYCLE_GATE_ADMISSION', 'CATALOG_CODE', :sid, 1, :from_hash, "
                    "2, :to_hash, 'INTERNAL_STRATEGY_EVALUATION', :eid, :ehash, 'X', :c, "
                    "now(), 'NONE')"
                ),
                {
                    "sid": root.strategy_id,
                    "from_hash": other.canonical_hash,
                    "to_hash": other_promoted.canonical_hash,
                    "eid": recorded.record.evaluation_id,
                    "ehash": recorded.record.canonical_hash,
                    "c": "0" * 64,
                },
            )

    def test_subject_must_agree_with_the_retained_evaluation_row(
        self, postgres_engine: Engine
    ) -> None:
        """A subject row whose evaluation hash (or strategy/version) differs
        from the retained evaluation row is refused by the composite FK."""

        _chain(postgres_engine)
        root = canonical_strategy_benchmarks(created_at=NOW)[0]
        backtest = simulate_strategy_backtest(
            root, _candles(35), evidence_class=EvidenceClass.FIXTURE, evidence_provenance=CAPTURE_ID
        )
        later = RecordInternalStrategyEvaluation(
            PostgresInternalStrategyEvaluationRepository(postgres_engine)
        ).execute(
            definition=root,
            candidate=evaluate_external_strategy(root, backtest),
            cycle_id="cycle-2026-09-03",
            as_of=NOW + timedelta(days=1),
            capture_id=CAPTURE_ID,
            evaluated_at=NOW + timedelta(days=1),
        )
        with (
            pytest.raises(sa.exc.IntegrityError, match="fk_strategy_evaluation_subject_internal"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO strategy_evaluation_subject (evaluation_source, "
                    "evaluation_id, internal_evaluation_id, evaluation_hash, registry, "
                    "strategy_id, version, definition_hash, canonical_hash, recorded_at, "
                    "authority) VALUES ('INTERNAL_STRATEGY_EVALUATION', :eid, :eid, "
                    ":wrong_hash, 'CATALOG_CODE', :sid, 1, :dhash, :c, now(), 'NONE')"
                ),
                {
                    "eid": later.record.evaluation_id,
                    "wrong_hash": "f" * 64,
                    "sid": root.strategy_id,
                    "dhash": root.canonical_hash,
                    "c": "0" * 64,
                },
            )

    def test_bare_non_legacy_trial_is_rejected_at_commit(self, postgres_engine: Engine) -> None:
        promoted = _chain(postgres_engine)[2]
        trial = _trial(promoted)
        with (
            pytest.raises(sa.exc.IntegrityError, match="PROVENANCE_REQUIRED"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO internal_strategy_paper_trial (trial_id, strategy_id, "
                    "strategy_version, instrument_id, account_id, generation, opened_at, "
                    "opening_authorization_id, opening_order_id, "
                    "opening_realized_pnl_baseline, horizon_days, horizon_target_at, "
                    "cooldown_days, policy_version, legacy_adopted, canonical_hash, authority) "
                    "VALUES (:tid, :sid, 2, 'BTC-USD', 'acct', 1, now(), 'a', 'o', 0, 30, "
                    "now(), 1, 'P', false, :h, 'NONE')"
                ),
                {"tid": trial.trial_id, "sid": trial.strategy_id, "h": trial.canonical_hash},
            )
        assert _counts(postgres_engine)["internal_strategy_paper_trial"] == 0

    def test_legacy_trial_can_never_receive_a_provenance_row(self, postgres_engine: Engine) -> None:
        provenance, _recorded, promoted, admission = _chain(postgres_engine)
        legacy = _trial(promoted, legacy_adopted=True)
        RecordStrategyPaperTrialOpened(
            PostgresStrategyPaperTrialRepository(postgres_engine)
        ).execute(record=legacy)
        with (
            pytest.raises(sa.exc.IntegrityError, match="not_legacy"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO strategy_paper_trial_provenance (trial_id, strategy_id, "
                    "strategy_version, trial_legacy_adopted, trial_canonical_hash, "
                    "transition_id, registry, definition_hash, canonical_hash, recorded_at, "
                    "authority) VALUES (:tid, :sid, 2, true, :th, :xid, 'CATALOG_CODE', "
                    ":dh, :c, now(), 'NONE')"
                ),
                {
                    "tid": legacy.trial_id,
                    "sid": legacy.strategy_id,
                    "th": legacy.canonical_hash,
                    "xid": admission.transition.transition_id,
                    "dh": admission.transition.to_ref.definition_hash,
                    "c": "0" * 64,
                },
            )
        assert provenance.trial_provenance(trial_id=legacy.trial_id) is None

    @pytest.mark.parametrize("table", STRATEGY_VERSION_PROVENANCE_TABLES)
    def test_every_table_is_immutable(self, postgres_engine: Engine, table: str) -> None:
        provenance, _recorded, promoted, admission = _chain(postgres_engine)
        RecordProvenancedTrialOpened(provenance).execute(
            trial=_trial(promoted), transition_id=admission.transition.transition_id
        )
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(sa.text(f"UPDATE {table} SET authority = 'NONE'"))  # nosec B608
        with (
            pytest.raises(sa.exc.DBAPIError, match="immutable"),
            postgres_engine.begin() as connection,
        ):
            connection.execute(sa.text(f"DELETE FROM {table}"))  # nosec B608


def test_downgrade_refuses_while_provenance_is_retained(postgres_engine: Engine) -> None:
    _chain(postgres_engine)
    with postgres_engine.connect() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        with pytest.raises(Exception, match="cannot downgrade 0115"):
            command.downgrade(config, "0114_outcome_specificity_v3")
        connection.rollback()


def test_downgrade_and_upgrade_round_trip_when_empty(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.downgrade(config, "0114_outcome_specificity_v3")
        connection.commit()
        tables = set(sa.inspect(connection).get_table_names())
        assert not tables & set(STRATEGY_VERSION_PROVENANCE_TABLES)
        command.upgrade(config, "head")
        connection.commit()
        assert set(STRATEGY_VERSION_PROVENANCE_TABLES) <= set(
            sa.inspect(connection).get_table_names()
        )
