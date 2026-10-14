"""Repeatable Strategy PAPER Evidence v1: tests for
``application.strategy_paper_trial_lifecycle`` -- idempotent-replay/
identity-conflict behavior of the two record services, against the
in-memory reference repositories. No database, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from apex.application.strategy_paper_trial_lifecycle import (
    InMemoryStrategyPaperTrialClosureRepository,
    InMemoryStrategyPaperTrialRepository,
    RecordStrategyPaperTrialClosed,
    RecordStrategyPaperTrialOpened,
    StrategyPaperTrialLifecycleResearchError,
    strategy_paper_trial_lineage,
)
from apex.domain.strategy_paper_trial_evidence_coverage import (
    TrialEvidenceCoverage,
    evaluate_trial_evidence_coverage,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    STRATEGY_PAPER_TRIAL_HORIZON_DAYS,
    StrategyPaperTrialRecord,
    build_strategy_paper_trial_closure_record,
    build_strategy_paper_trial_record,
)

NOW = datetime(2026, 9, 3, tzinfo=UTC)
STRATEGY_ID = "buy-and-hold-btc-usd"
INSTRUMENT_ID = "BTC-USD"
ACCOUNT_ID = "genuine-strategy-crypto-paper-account"


def _coverage(*, window_start: datetime, window_end: datetime) -> TrialEvidenceCoverage:
    """See tests/test_strategy_paper_trial_lifecycle.py's identical helper
    -- a representative coverage summary computed through the real
    evaluate_trial_evidence_coverage rather than a hand-rolled literal."""

    return evaluate_trial_evidence_coverage(
        instrument_id=INSTRUMENT_ID,
        window_start=window_start,
        window_end=window_end,
        observations=(),
    )


def _trial(*, generation: int, opened_at: datetime = NOW) -> StrategyPaperTrialRecord:
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
    )


class TestRecordStrategyPaperTrialOpened:
    def test_first_registration_is_a_genuine_write(self) -> None:
        repo = InMemoryStrategyPaperTrialRepository()
        result = RecordStrategyPaperTrialOpened(repo).execute(record=_trial(generation=1))
        assert result.idempotent_replay is False
        assert repo.get(trial_id=result.record.trial_id) == result.record

    def test_replaying_the_identical_record_is_idempotent(self) -> None:
        repo = InMemoryStrategyPaperTrialRepository()
        trial = _trial(generation=1)
        RecordStrategyPaperTrialOpened(repo).execute(record=trial)
        second = RecordStrategyPaperTrialOpened(repo).execute(record=trial)
        assert second.idempotent_replay is True
        assert repo.for_slot(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID
        ) == (trial,)

    def test_same_trial_id_different_content_is_a_hard_conflict(self) -> None:
        repo = InMemoryStrategyPaperTrialRepository()
        trial = _trial(generation=1)
        RecordStrategyPaperTrialOpened(repo).execute(record=trial)
        conflicting = build_strategy_paper_trial_record(
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
            account_id=ACCOUNT_ID,
            generation=1,
            opened_at=NOW,
            opening_authorization_id="a-completely-different-authorization",
            opening_order_id="a-completely-different-order",
            opening_realized_pnl_baseline=Decimal("0"),
        )
        assert conflicting.trial_id == trial.trial_id
        with pytest.raises(StrategyPaperTrialLifecycleResearchError):
            RecordStrategyPaperTrialOpened(repo).execute(record=conflicting)

    def test_for_slot_orders_by_generation_ascending(self) -> None:
        repo = InMemoryStrategyPaperTrialRepository()
        gen2 = _trial(generation=2, opened_at=NOW + timedelta(days=40))
        gen1 = _trial(generation=1, opened_at=NOW)
        # Registered out of order -- for_slot must still return them sorted.
        RecordStrategyPaperTrialOpened(repo).execute(record=gen2)
        RecordStrategyPaperTrialOpened(repo).execute(record=gen1)
        ordered = repo.for_slot(
            strategy_id=STRATEGY_ID, strategy_version=1, instrument_id=INSTRUMENT_ID
        )
        assert [item.generation for item in ordered] == [1, 2]


class TestRecordStrategyPaperTrialClosed:
    def _closed_pair(
        self,
    ) -> tuple[
        InMemoryStrategyPaperTrialRepository,
        InMemoryStrategyPaperTrialClosureRepository,
        StrategyPaperTrialRecord,
    ]:
        trials = InMemoryStrategyPaperTrialRepository()
        closures = InMemoryStrategyPaperTrialClosureRepository()
        trial = _trial(generation=1)
        RecordStrategyPaperTrialOpened(trials).execute(record=trial)
        return trials, closures, trial

    def test_closing_an_unknown_trial_fails_closed(self) -> None:
        trials = InMemoryStrategyPaperTrialRepository()
        closures = InMemoryStrategyPaperTrialClosureRepository()
        phantom_trial = _trial(generation=1)
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=phantom_trial,
            closed_at=closed_at,
            closing_authorization_id="close-a",
            closing_order_id="close-o",
            realized_paper_pnl=Decimal("1"),
            coverage=_coverage(window_start=phantom_trial.opened_at, window_end=closed_at),
        )
        with pytest.raises(StrategyPaperTrialLifecycleResearchError):
            RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(record=closure)

    def test_first_close_is_a_genuine_write(self) -> None:
        trials, closures, trial = self._closed_pair()
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-a",
            closing_order_id="close-o",
            realized_paper_pnl=Decimal("1.5"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        result = RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(
            record=closure
        )
        assert result.idempotent_replay is False
        assert closures.for_trial(trial_id=trial.trial_id) == closure

    def test_replaying_the_identical_closure_is_idempotent(self) -> None:
        trials, closures, trial = self._closed_pair()
        closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=closed_at,
            closing_authorization_id="close-a",
            closing_order_id="close-o",
            realized_paper_pnl=Decimal("1.5"),
            coverage=_coverage(window_start=trial.opened_at, window_end=closed_at),
        )
        service = RecordStrategyPaperTrialClosed(trials=trials, closures=closures)
        first = service.execute(record=closure)
        second = service.execute(record=closure)
        assert first.idempotent_replay is False
        assert second.idempotent_replay is True

    def test_a_terminal_trial_cannot_be_closed_twice_with_different_content(self) -> None:
        trials, closures, trial = self._closed_pair()
        service = RecordStrategyPaperTrialClosed(trials=trials, closures=closures)
        first_closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        first_closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=first_closed_at,
            closing_authorization_id="close-a",
            closing_order_id="close-o",
            realized_paper_pnl=Decimal("1.5"),
            coverage=_coverage(window_start=trial.opened_at, window_end=first_closed_at),
        )
        service.execute(record=first_closure)
        second_closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS + 1)
        second_closure = build_strategy_paper_trial_closure_record(
            trial=trial,
            closed_at=second_closed_at,
            closing_authorization_id="close-a-again",
            closing_order_id="close-o-again",
            realized_paper_pnl=Decimal("9.99"),
            coverage=_coverage(window_start=trial.opened_at, window_end=second_closed_at),
        )
        with pytest.raises(StrategyPaperTrialLifecycleResearchError):
            service.execute(record=second_closure)
        # The FIRST closure remains the one and only retained fact.
        assert closures.for_trial(trial_id=trial.trial_id) == first_closure


class TestStrategyPaperTrialLineage:
    def test_no_trials_yields_empty_lineage(self) -> None:
        trials = InMemoryStrategyPaperTrialRepository()
        closures = InMemoryStrategyPaperTrialClosureRepository()
        lineage = strategy_paper_trial_lineage(
            trials=trials,
            closures=closures,
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
        )
        assert lineage["trial_count"] == 0
        assert lineage["active_trial"] is None
        assert lineage["latest_closure"] is None

    def test_active_trial_is_the_one_with_no_closure(self) -> None:
        trials = InMemoryStrategyPaperTrialRepository()
        closures = InMemoryStrategyPaperTrialClosureRepository()
        gen1 = _trial(generation=1)
        RecordStrategyPaperTrialOpened(trials).execute(record=gen1)
        gen1_closed_at = NOW + timedelta(days=STRATEGY_PAPER_TRIAL_HORIZON_DAYS)
        closure = build_strategy_paper_trial_closure_record(
            trial=gen1,
            closed_at=gen1_closed_at,
            closing_authorization_id="close-a",
            closing_order_id="close-o",
            realized_paper_pnl=Decimal("1"),
            coverage=_coverage(window_start=gen1.opened_at, window_end=gen1_closed_at),
        )
        RecordStrategyPaperTrialClosed(trials=trials, closures=closures).execute(record=closure)
        gen2 = _trial(generation=2, opened_at=closure.next_eligible_at)
        RecordStrategyPaperTrialOpened(trials).execute(record=gen2)

        lineage = strategy_paper_trial_lineage(
            trials=trials,
            closures=closures,
            strategy_id=STRATEGY_ID,
            strategy_version=1,
            instrument_id=INSTRUMENT_ID,
        )
        assert lineage["trial_count"] == 2
        assert lineage["active_trial"] == gen2
        assert lineage["latest_closure"] == closure
        generations = cast("tuple[dict[str, Any], ...]", lineage["generations"])
        assert [item["trial"].generation for item in generations] == [1, 2]
        assert generations[0]["closure"] == closure
        assert generations[1]["closure"] is None
