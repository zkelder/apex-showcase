"""Orchestration-level proofs for controlled cross-market hypothesis
research, against in-memory fakes and a genuine (non-network) fixture
panel -- no database, no provider request.

Covers: panel construction (point-in-time gating, cross-market
alignment, missing input), deterministic proposal generation, the full
governed methodology integration (holdout, walk-forward, multiple-
testing, overlap handling, robustness), the knowledge base (supported/
weak/rejected, conclusion history, contradiction, follow-up lineage),
and statistical honesty (negative controls, panel dependence, no causal
wording surfaced by the pipeline's own retained text).
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.application.cross_market_research import (
    V1_AGENDA_HYPOTHESIS_SPECS,
    AdvanceAgendaRun,
    BuildCrossMarketPanel,
    InMemoryCrossMarketResearchRepository,
    RegisterAgendaRun,
    _latest_knowledge_entries,
    build_agenda_timeline,
    generate_v1_agenda_hypotheses,
)
from apex.application.research_methodology import InMemoryResearchMethodologyRepository
from apex.domain.cross_market_research import (
    AgendaRunStatus,
    FeatureFamily,
    FeatureId,
    HypothesisGenerationMethod,
    HypothesisTemplateId,
    KnowledgeStatus,
    MarketId,
    OutcomeFamily,
    ResearchAgenda,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

NOW = datetime(2026, 8, 6, tzinfo=UTC)
SESSION_COUNT = 900


def _session_dates() -> list[date]:
    dates: list[date] = []
    cursor = date(2020, 7, 27)
    while len(dates) < SESSION_COUNT:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    return dates


def _fake_bars(instrument_id: str, base_price: float, seed: int, dates: list[date]) -> list[object]:
    rng = random.Random(seed)
    price = Decimal(str(base_price))
    bars = []
    for day in dates:
        drift = Decimal(str(1 + rng.uniform(-0.012, 0.012)))
        price = (price * drift).quantize(Decimal("0.01"))
        if price <= 0:
            price = Decimal("1.00")
        period_end = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=21)
        bars.append(
            SimpleNamespace(
                instrument_id=instrument_id,
                period_end=period_end,
                available_at=period_end + timedelta(hours=1),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(1000),
                source_identity="fixture",
            )
        )
    return bars


@pytest.fixture(scope="module")
def panel_inputs() -> dict[str, object]:
    dates = _session_dates()
    symbols = sorted({member.symbol for member in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members})
    instrument_of = {symbol: f"INSTR-{symbol}" for symbol in symbols}
    bars_by_batch: dict[str, list[object]] = {"batch-1": []}
    for index, symbol in enumerate(symbols):
        bars_by_batch["batch-1"].extend(
            _fake_bars(instrument_of[symbol], 50 + (hash(symbol) % 200), seed=index, dates=dates)
        )

    plan = SimpleNamespace(
        plan_id="plan-1",
        universe=tuple(
            SimpleNamespace(symbol_at_capture=symbol, instrument_id=instrument_of[symbol])
            for symbol in symbols
        ),
    )
    snapshot = SimpleNamespace(
        plan_id="plan-1",
        source_batch_ids=("batch-1",),
        earliest_observation_date=dates[0].isoformat(),
        latest_observation_date=dates[-1].isoformat(),
    )

    class FakeHistorical:
        def get_plan(self, plan_id: str) -> object:
            return plan if plan_id == "plan-1" else None

        def snapshot_by_id(self, snapshot_id: str) -> object:
            return snapshot if snapshot_id == "snap-1" else None

    class FakeEvidence:
        def get_dataset(self, batch_id: str) -> tuple:
            return ((), (), tuple(bars_by_batch[batch_id]), ())

    dataset = SimpleNamespace(
        crypto_capture_ids=("capture-1",),
        common_overlap_start=dates[0].isoformat(),
        common_overlap_end=dates[-1].isoformat(),
    )

    class FakeGenuine:
        def cross_market_dataset_by_id(self, dataset_id: str) -> object:
            return dataset if dataset_id == "cmd-1" else None

    def fake_candles() -> list[object]:
        rng = random.Random(999)
        price = Decimal("30000")
        out = []
        for day in dates:
            drift = Decimal(str(1 + rng.uniform(-0.035, 0.036)))
            price = (price * drift).quantize(Decimal("0.01"))
            out.append(
                SimpleNamespace(
                    product_id="BTC-USD",
                    bucket_start=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=Decimal(10),
                    provider="fixture",
                )
            )
        return out

    class FakeCrypto:
        def candles(self, capture_id: str) -> tuple:
            return tuple(fake_candles())

    return {
        "historical_repository": FakeHistorical(),
        "evidence": FakeEvidence(),
        "genuine_repository": FakeGenuine(),
        "crypto_repository": FakeCrypto(),
        "dates": dates,
    }


@pytest.fixture(scope="module")
def panel(panel_inputs: dict[str, object]):
    builder = BuildCrossMarketPanel(
        historical_repository=panel_inputs["historical_repository"],  # type: ignore[arg-type]
        evidence=panel_inputs["evidence"],  # type: ignore[arg-type]
        genuine_repository=panel_inputs["genuine_repository"],  # type: ignore[arg-type]
        crypto_repository=panel_inputs["crypto_repository"],  # type: ignore[arg-type]
    )
    return builder.execute(
        equity_snapshot_id="snap-1", cross_market_dataset_id="cmd-1", horizons=(5, 20, 60)
    )


@pytest.fixture(scope="module")
def agenda() -> ResearchAgenda:
    return ResearchAgenda.create(
        version="V1_TEST",
        objective="Bounded controlled cross-market hypothesis research (test fixture).",
        allowed_markets=tuple(MarketId),
        allowed_feature_families=tuple(FeatureFamily),
        allowed_outcome_families=tuple(OutcomeFamily),
        allowed_horizons_sessions=(5, 20, 60),
        allowed_templates=tuple(HypothesisTemplateId),
        hypothesis_generation_method=HypothesisGenerationMethod.DETERMINISTIC_TEMPLATE_EXPANSION_V1,
        maximum_hypothesis_count=len(V1_AGENDA_HYPOTHESIS_SPECS),
        maximum_configurations_per_hypothesis=1,
        maximum_total_trials=600,
        train_validation_holdout_policy="TRAIN<VALIDATION<HOLDOUT(locked)",
        multiple_testing_family_policy="PARTITIONED_BY_HYPOTHESIS_FAMILY_V1",
        robustness_requirements=(
            "SUBPERIOD_SPLIT",
            "REGIME_SPLIT",
            "LEAVE_ONE_PERIOD_OUT",
            "MISSING_DATA_SENSITIVITY",
            "COST_STRESS",
        ),
        dataset_snapshot_id="historical_dataset_snapshot_fixture",
        replay_run_id="historical_replay_run_fixture",
        cross_market_dataset_id="cmd-1",
        cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
        registered_at=NOW,
    )


# --------------------------------------------------------------------------
# Panel construction: point-in-time gating, cross-market alignment,
# missing input
# --------------------------------------------------------------------------


def test_panel_covers_the_full_declared_overlap_window(panel, panel_inputs) -> None:
    assert len(panel.session_dates) == SESSION_COUNT
    assert panel.session_dates[0] == panel_inputs["dates"][0].isoformat()
    assert panel.session_dates[-1] == panel_inputs["dates"][-1].isoformat()


def test_panel_forward_outcome_is_incomplete_near_the_end_of_history(panel) -> None:
    """Point-in-time honesty for outcomes: an outcome that would need
    evidence past the retained history's end must be marked incomplete,
    never fabricated."""

    horizon = 20
    outcomes = panel.outcomes_by_horizon[horizon]
    assert outcomes[-1].complete is False
    assert outcomes[-1].outcomes == ()
    assert outcomes[0].complete is True


def test_panel_feature_lookup_returns_none_for_a_feature_with_no_value_yet(panel) -> None:
    early = panel.observations[0]
    # a 252-session return cannot exist yet at the very first observation
    assert early.feature(FeatureId.EQUITY_RETURN_252) is None


def test_panel_regime_snapshot_is_present_for_every_observation(panel) -> None:
    assert all(obs.regime is not None for obs in panel.observations)
    assert all(len(obs.regime.classifications) == 10 for obs in panel.observations)


def test_panel_builder_fails_closed_when_the_broad_equity_benchmark_is_missing(
    panel_inputs,
) -> None:
    class MissingSpyEvidence:
        def get_dataset(self, batch_id: str) -> tuple:
            _instr, _sess, bars, _actions = panel_inputs["evidence"].get_dataset(batch_id)
            filtered = tuple(bar for bar in bars if bar.instrument_id != "INSTR-SPY")
            return (), (), filtered, ()

    builder = BuildCrossMarketPanel(
        historical_repository=panel_inputs["historical_repository"],  # type: ignore[arg-type]
        evidence=MissingSpyEvidence(),  # type: ignore[arg-type]
        genuine_repository=panel_inputs["genuine_repository"],  # type: ignore[arg-type]
        crypto_repository=panel_inputs["crypto_repository"],  # type: ignore[arg-type]
    )
    with pytest.raises(Exception, match="SPY"):
        builder.execute(
            equity_snapshot_id="snap-1", cross_market_dataset_id="cmd-1", horizons=(20,)
        )


# --------------------------------------------------------------------------
# Deterministic proposal generation
# --------------------------------------------------------------------------


def test_generate_v1_agenda_hypotheses_is_deterministic_and_bounded(agenda: ResearchAgenda) -> None:
    first = generate_v1_agenda_hypotheses(agenda=agenda, created_at=NOW)
    second = generate_v1_agenda_hypotheses(agenda=agenda, created_at=NOW)
    assert [item.hypothesis_id for item in first] == [item.hypothesis_id for item in second]
    assert len(first) == len(V1_AGENDA_HYPOTHESIS_SPECS)
    assert len(first) <= agenda.maximum_hypothesis_count
    families = {item.family for item in first}
    assert len(families) == 5
    negative_controls = [item for item in first if item.is_negative_control]
    assert len(negative_controls) >= 5
    kinds = {item.negative_control_kind for item in negative_controls}
    assert kinds == {
        "SHUFFLED_FEATURE",
        "LAG_DISPLACED_FEATURE",
        "ECONOMICALLY_UNRELATED_PAIR",
        "RANDOM_DATE_BASELINE",
        "PERSISTENCE_BASELINE",
        "UNCONDITIONAL_OUTCOME",
    }


# --------------------------------------------------------------------------
# Full governed methodology integration + knowledge base + contradictions +
# follow-ups + quality report, run once per module (expensive) and asserted
# on from several angles.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def completed_run(agenda: ResearchAgenda, panel):
    hypotheses = generate_v1_agenda_hypotheses(agenda=agenda, created_at=NOW)
    train_end = panel.session_dates[540]
    validation_end = panel.session_dates[720]
    timeline = build_agenda_timeline(
        panel=panel,
        agenda=agenda,
        train_end=train_end,
        validation_end=validation_end,
        created_at=NOW,
    )
    repository = InMemoryCrossMarketResearchRepository()
    methodology_repository = InMemoryResearchMethodologyRepository()
    repository.save_agenda(agenda)
    for hypothesis in hypotheses:
        repository.save_hypothesis(hypothesis)
    RegisterAgendaRun(repository=repository).execute(
        agenda=agenda, hypotheses=hypotheses, created_at=NOW
    )
    advancer = AdvanceAgendaRun(
        repository=repository,
        methodology_repository=methodology_repository,
        timeline=timeline,
        panel_warnings=panel.warnings,
    )
    run = None
    for _ in range(10):
        run = advancer.execute(agenda=agenda, panel=panel, maximum_hypotheses=6, created_at=NOW)
        if run.status is AgendaRunStatus.COMPLETE:
            break
    assert run is not None and run.status is AgendaRunStatus.COMPLETE
    return SimpleNamespace(
        agenda=agenda,
        hypotheses=hypotheses,
        run=run,
        repository=repository,
        methodology_repository=methodology_repository,
        timeline=timeline,
    )


def test_agenda_run_processes_every_hypothesis_exactly_once(completed_run) -> None:
    assert completed_run.run.cursor_ordinal == len(completed_run.hypotheses)
    assert len(completed_run.run.completed_hypothesis_ids) == len(completed_run.hypotheses)
    assert len(set(completed_run.run.completed_hypothesis_ids)) == len(completed_run.hypotheses)


def test_every_hypothesis_has_a_retained_execution_with_a_governance_policy(completed_run) -> None:
    executions = completed_run.repository.executions_for_agenda(completed_run.agenda.agenda_id)
    assert len(executions) == len(completed_run.hypotheses)
    for execution in executions:
        policy = completed_run.methodology_repository.governance_policy_by_id(
            execution.governance_policy_id
        )
        assert policy is not None
        assert policy.authority == "NONE"


def test_holdout_is_exposed_before_any_holdout_trial_and_governance_stays_locked(
    completed_run,
) -> None:
    executions = completed_run.repository.executions_for_agenda(completed_run.agenda.agenda_id)
    with_holdout = [item for item in executions if item.holdout_trial_id is not None]
    assert with_holdout, "at least one hypothesis should reach a holdout trial in this fixture"
    for execution in with_holdout:
        policy = completed_run.methodology_repository.governance_policy_by_id(
            execution.governance_policy_id
        )
        assert policy is not None
        exposures = completed_run.methodology_repository.holdout_exposures_for_policy(
            policy.governance_policy_id
        )
        assert exposures, "holdout trial exists without a retained exposure event"
        holdout_trial = completed_run.methodology_repository.trial_by_id(execution.holdout_trial_id)
        assert holdout_trial is not None
        assert all(exposure.exposed_at <= holdout_trial.created_at for exposure in exposures)


def test_walk_forward_folds_reuse_the_shared_fold_windows(completed_run) -> None:
    executions = completed_run.repository.executions_for_agenda(completed_run.agenda.agenda_id)
    fold_count = len(completed_run.timeline.fold_windows)
    for execution in executions:
        assert len(execution.walk_forward_trial_ids) <= fold_count


def test_overlap_handling_reports_a_conservative_effective_sample_not_zero(completed_run) -> None:
    """The panel-sample trial path must not silently report zero
    effective sample for a genuinely large sample (see ``record_panel_
    sample_trial``'s docstring for why the naive grouped-trial path
    cannot be reused unmodified here)."""

    executions = completed_run.repository.executions_for_agenda(completed_run.agenda.agenda_id)
    checked_any = False
    for execution in executions:
        if execution.validation_trial_id is None:
            continue
        trial = completed_run.methodology_repository.trial_by_id(execution.validation_trial_id)
        assert trial is not None
        if trial.diagnostics.base.sample_count > 20:
            checked_any = True
            assert trial.diagnostics.overlap.effective_sample_count > 0
            assert trial.diagnostics.overlap.raw_sample_count == trial.diagnostics.base.sample_count
    assert checked_any


def test_robustness_checks_ran_for_hypotheses_with_a_reference_trial(completed_run) -> None:
    executions = completed_run.repository.executions_for_agenda(completed_run.agenda.agenda_id)
    with_robustness = [item for item in executions if item.robustness_run_id is not None]
    assert with_robustness
    for execution in with_robustness:
        run = completed_run.methodology_repository.robustness_run_by_id(execution.robustness_run_id)
        assert run is not None
        assert run.summary.checks_run >= 5


def test_multiple_testing_correction_ran_per_family(completed_run) -> None:
    entries = completed_run.repository.list_knowledge_entries()
    corrected = [item for item in entries if "MULTIPLE_TESTING_CORRECTED" in item.evidence_strength]
    assert len(corrected) == len(completed_run.hypotheses)


def test_knowledge_base_retains_both_the_raw_and_corrected_revision(completed_run) -> None:
    """Section 16/17: multiple-testing correction must never overwrite the
    initial (pre-correction) knowledge-base entry -- both revisions stay
    retrievable, so the agenda can show how quickly findings disappear
    under stricter methodology."""

    for hypothesis in completed_run.hypotheses:
        history = completed_run.repository.knowledge_entries_for_hypothesis(
            hypothesis.hypothesis_id
        )
        assert len(history) == 2
        assert history[0].revision == 1
        assert history[1].revision == 2
        assert history[1].supersedes_entry_id == history[0].entry_id


def test_knowledge_status_distribution_uses_the_retained_vocabulary_only(completed_run) -> None:
    entries = completed_run.repository.list_knowledge_entries()
    latest = _latest_knowledge_entries(entries)
    statuses = {entry.status for entry in latest.values()}
    assert statuses <= set(KnowledgeStatus)


def test_quality_report_is_built_once_and_accounts_for_every_hypothesis(completed_run) -> None:
    report = completed_run.repository.quality_report_by_agenda(completed_run.agenda.agenda_id)
    assert report is not None
    assert report.hypotheses_proposed == len(completed_run.hypotheses)
    assert sum(count for _label, count in report.conclusion_distribution) == len(
        completed_run.hypotheses
    )
    assert "not independent samples" in report.panel_dependence_note.lower()


def test_negative_controls_are_retained_and_counted_in_the_quality_report(completed_run) -> None:
    negative_controls = [item for item in completed_run.hypotheses if item.is_negative_control]
    assert len(negative_controls) >= 5
    report = completed_run.repository.quality_report_by_agenda(completed_run.agenda.agenda_id)
    assert report is not None
    assert str(len(negative_controls)) in report.negative_control_behavior_note


def test_follow_up_proposals_link_back_to_a_real_parent_hypothesis(completed_run) -> None:
    follow_ups = completed_run.repository.list_follow_ups()
    hypothesis_ids = {item.hypothesis_id for item in completed_run.hypotheses}
    for follow_up in follow_ups:
        assert follow_up.parent_hypothesis_id in hypothesis_ids
        assert follow_up.requires_new_agenda is True


def test_contradictions_if_any_reference_real_knowledge_entries(completed_run) -> None:
    contradictions = completed_run.repository.list_contradictions()
    entry_ids = {item.entry_id for item in completed_run.repository.list_knowledge_entries()}
    for contradiction in contradictions:
        assert contradiction.first_entry_id in entry_ids
        assert contradiction.second_entry_id in entry_ids
        assert contradiction.first_entry_id != contradiction.second_entry_id


def test_resuming_a_complete_agenda_run_is_a_safe_no_op(completed_run) -> None:
    advancer = AdvanceAgendaRun(
        repository=completed_run.repository,
        methodology_repository=completed_run.methodology_repository,
        timeline=completed_run.timeline,
        panel_warnings=(),
    )
    before = completed_run.repository.list_knowledge_entries()
    run = advancer.execute(
        agenda=completed_run.agenda,
        panel=None,
        maximum_hypotheses=5,
        created_at=NOW,  # type: ignore[arg-type]
    )
    after = completed_run.repository.list_knowledge_entries()
    assert run.status is AgendaRunStatus.COMPLETE
    assert len(before) == len(after)


def test_no_strategy_opportunity_paper_or_live_authority_anywhere_in_retained_evidence(
    completed_run,
) -> None:
    executions = completed_run.repository.executions_for_agenda(completed_run.agenda.agenda_id)
    entries = completed_run.repository.list_knowledge_entries()
    assert all(item.authority == "NONE" for item in executions)
    assert all(item.authority == "NONE" for item in entries)
    assert completed_run.agenda.authority == "NONE"
