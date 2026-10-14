"""Adapter-free orchestration for Cross-Market Research v3: Fundamental and
Market Evidence Integration with Forward Options Seam.

Composes, entirely unmodified:

* ``application.corporate_fundamentals`` (bulk ``filings_for_securities``/
  ``facts_for_securities``, ``latest_dataset_snapshot``) for the retained
  corporate cohort's genuine point-in-time filings and facts.
* ``application.historical_market_data``/``application.real_securities_data``
  for the retained equity/ETF cohort's genuine daily bars -- the identical
  ports ``application.cross_market_research.BuildCrossMarketPanel`` already
  uses.
* ``application.genuine_multi_asset_history`` for the retained
  ``CrossMarketDataset`` binding (market-context lineage only).
* ``domain.research_methodology`` for the governed conclusion/promotion
  taxonomy and robustness-summary rule; ``domain.corporate_market_research``
  for every genuinely new corporate-evidence primitive.

Why this module does *not* reuse ``application.research_methodology``'s own
``RecordResearchTrial``/``RunRobustnessChecks`` command classes: both are
built around ``domain.historical_replay.HistoricalResearchEvaluation``
sourced from the persisted ``historical_replay_evaluation`` table, whose
own ``UNIQUE (replay_run_id, frame_id, method_id)`` constraint allows only
one evaluation per (frame, method) -- exactly the shape a *single broad-
market time series* needs, and exactly what a *cross-sectional panel of
many issuers sharing one frame* cannot use. Every governed *type*
(``ResearchGovernancePolicy``, ``ResearchTrial``, ``RobustnessCheckRun``,
``ResearchTrialFamily``, ``ResearchConclusion``, ``PromotionEvaluation``)
and its *persisted table* (migration ``0079``, entirely unmodified) is
still reused directly; only the orchestration that decides what evidence
feeds those types is new, because the underlying evidence shape is new.

No service here has Strategy, Opportunity, PAPER, LIVE, or provider-write
authority; every retained record stays ``authority = "NONE"``.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from datetime import date as _date
from decimal import Decimal, localcontext
from typing import ClassVar, Protocol, cast

from apex.domain.corporate_fundamentals import (
    CorporateFilingEvent,
    CorporateHistoryDatasetSnapshot,
    FilingForm,
    FundamentalFact,
    build_corporate_fundamental_snapshot,
    compute_derived_features,
    compute_filing_event_features,
    visible_filings_as_of,
)
from apex.domain.corporate_market_research import (
    ALLOWED_HORIZONS_SESSIONS,
    APPROXIMATE_US_EQUITY_CLOSE_UTC_HOUR,
    OPTIONS_SEAM_CURRENT_DESCRIPTIVE_ONLY,
    OPTIONS_SEAM_STRUCTURALLY_READY,
    CorporateContributionEvidence,
    CorporateFeatureId,
    CorporateFeatureObservation,
    CorporateFilingEventObservation,
    CorporateHypothesisFamily,
    CorporateMarketResearchAgenda,
    CorporateOutcomeId,
    CorporateOutcomeObservation,
    CorporatePanelRow,
    GeneratedCorporateHypothesis,
    IncrementalContributionResult,
    OptionsForwardSeamSnapshot,
    clustered_bootstrap_means,
    compute_panel_dependence_diagnostics,
    corporate_market_research_warnings,
    pearson_correlation,
    sector_relative_return,
)
from apex.domain.cross_market_research import (
    AgendaQualityReport,
    AgendaRun,
    AgendaRunStatus,
    ConditionRule,
    DatedValue,
    DiscoveryStatus,
    FollowUpKind,
    FollowUpProposal,
    HypothesisExecutionRecord,
    HypothesisTemplateId,
    KnowledgeStatus,
    MarketId,
    ProposalSource,
    ResearchAgenda,
    ResearchContradiction,
    ResearchKnowledgeEntry,
    StabilityReport,
    SubperiodResult,
    classify_bond_risk_relationship,
    derive_stability_state,
    fraction_positive,
    rolling_correlation_series,
    rolling_returns,
    rolling_volatility_series,
    to_dated_closes,
)
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalDatasetSnapshot,
    observation_view_from_securities_bar,
)
from apex.domain.historical_replay import (
    ReplayFeatureFrame,
    ReplayRun,
    ResearchMethodId,
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
    classify_breadth_strength,
    classify_risk_tone,
    classify_trend_strength,
    classify_volatility_regime,
)
from apex.domain.research_calibration import (
    EvidenceProfile,
    classify_practical_significance,
    derive_evidence_grade,
)
from apex.domain.research_calibration_v2 import (
    ConditioningShape,
    DesignClassification,
    MatchedCalibrationResult,
    ResearchDesign,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1
from apex.domain.research_methodology import (
    BenchmarkComparison,
    BenchmarkKind,
    BootstrapConfidenceInterval,
    ConfigurationOrigin,
    EffectSize,
    GovernanceIntent,
    HoldoutExposureEvent,
    MultipleTestingCorrectionMethod,
    PromotionEvaluation,
    PromotionPolicy,
    ResearchConclusion,
    ResearchConclusionState,
    ResearchConfiguration,
    ResearchGovernancePolicy,
    ResearchPhase,
    ResearchTrial,
    ResearchTrialFamily,
    RobustnessCheckResult,
    RobustnessCheckRun,
    RobustnessCheckType,
    SampleQualityState,
    StandardErrorEstimate,
    StatisticalDiagnosticsV2,
    TrialSelectionMethod,
    TrialTestResult,
    bound_statistical_decimal,
    compare_to_benchmark,
    derive_conclusion_state,
    guard_governed_holdout_access,
    resolve_minimum_sample_requirement,
    two_sided_z_test_p_value,
)
from apex.domain.securities import SecuritiesBarEvidence
from apex.domain.values import content_digest, deterministic_id

EQUITY_SESSIONS_PER_YEAR = 252
CORPORATE_MARKET_METHOD_ID = ResearchMethodId.CORPORATE_MARKET_CONTROLLED_HYPOTHESIS_V1
BROAD_MARKET_SYMBOL = "SPY"
RATES_PROXY_SYMBOL = "TLT"
DEFAULT_BOOTSTRAP_SEED = 20260809
MINIMUM_MONTHLY_DATES = 24


class CorporateMarketResearchApplicationError(RuntimeError):
    pass


class CorporateMarketResearchNotFound(CorporateMarketResearchApplicationError):
    pass


# ==========================================================================
# Collaborator protocols -- the narrow slice of each existing repository
# this module reads. Satisfied structurally by the same repositories every
# other research milestone already uses.
# ==========================================================================


class CorporateFundamentalsCollaborator(Protocol):
    def latest_dataset_snapshot(self, plan_id: str) -> CorporateHistoryDatasetSnapshot | None: ...
    def dataset_snapshot_by_id(self, dataset_id: str) -> CorporateHistoryDatasetSnapshot | None: ...
    def filings_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[CorporateFilingEvent, ...]: ...
    def facts_for_securities(
        self, stable_security_ids: Sequence[str]
    ) -> tuple[FundamentalFact, ...]: ...


class HistoricalMarketDataCollaborator(Protocol):
    def get_plan(self, plan_id: str) -> HistoricalCapturePlan | None: ...
    def snapshot_by_id(self, snapshot_id: str) -> HistoricalDatasetSnapshot | None: ...


class SecuritiesEvidenceCollaborator(Protocol):
    def get_dataset(
        self, batch_id: str
    ) -> tuple[object, object, tuple[SecuritiesBarEvidence, ...], object]: ...


class HistoricalReplayCollaborator(Protocol):
    def get_run(self, replay_run_id: str) -> ReplayRun | None: ...
    def list_frames(self, replay_run_id: str, *, limit: int) -> tuple[ReplayFeatureFrame, ...]: ...


class CorporatePanelBuilderCollaborator(Protocol):
    """The narrow slice of ``BuildCorporatePanel`` that ``AdvanceCorporate
    AgendaRun`` depends on -- a Protocol rather than the concrete class so
    tests can substitute a pre-built synthetic ``CorporatePanel`` without
    faking all four of ``BuildCorporatePanel``'s own real-data
    collaborators (structurally satisfied by ``BuildCorporatePanel``
    itself, unchanged)."""

    def execute(
        self,
        *,
        corporate_dataset_id: str,
        equity_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
    ) -> CorporatePanel: ...


class ResearchMethodologyCollaborator(Protocol):
    """The narrow slice of ``ResearchMethodologyRepository`` this module
    writes to directly -- reused unmodified (migration ``0079``)."""

    def governance_policy_by_id(
        self, governance_policy_id: str
    ) -> ResearchGovernancePolicy | None: ...
    def save_governance_policy(
        self, policy: ResearchGovernancePolicy
    ) -> ResearchGovernancePolicy: ...
    def save_holdout_exposure(self, event: HoldoutExposureEvent) -> HoldoutExposureEvent: ...
    def trial_by_id(self, trial_id: str) -> ResearchTrial | None: ...
    def save_trial(self, trial: ResearchTrial) -> ResearchTrial: ...
    def trial_family_by_id(self, trial_family_id: str) -> ResearchTrialFamily | None: ...
    def save_trial_family(self, family: ResearchTrialFamily) -> ResearchTrialFamily: ...
    def robustness_run_by_id(self, robustness_run_id: str) -> RobustnessCheckRun | None: ...
    def save_robustness_run(self, run: RobustnessCheckRun) -> RobustnessCheckRun: ...
    def conclusion_by_id(self, conclusion_id: str) -> ResearchConclusion | None: ...
    def save_conclusion(self, conclusion: ResearchConclusion) -> ResearchConclusion: ...
    def save_promotion_evaluation(self, evaluation: PromotionEvaluation) -> PromotionEvaluation: ...


class CorporateMarketResearchRepository(Protocol):
    def agenda_by_id(self, agenda_id: str) -> CorporateMarketResearchAgenda | None: ...
    def save_agenda(
        self, agenda: CorporateMarketResearchAgenda
    ) -> CorporateMarketResearchAgenda: ...
    def list_agendas(self) -> tuple[CorporateMarketResearchAgenda, ...]: ...

    def hypothesis_by_id(self, hypothesis_id: str) -> GeneratedCorporateHypothesis | None: ...
    def save_hypothesis(
        self, hypothesis: GeneratedCorporateHypothesis
    ) -> GeneratedCorporateHypothesis: ...
    def hypotheses_for_agenda(self, agenda_id: str) -> tuple[GeneratedCorporateHypothesis, ...]: ...

    def agenda_run_by_agenda(self, agenda_id: str) -> AgendaRun | None: ...
    def agenda_run_by_id(self, agenda_run_id: str) -> AgendaRun | None: ...
    def save_agenda_run(self, run: AgendaRun) -> AgendaRun: ...

    def execution_by_hypothesis(self, hypothesis_id: str) -> HypothesisExecutionRecord | None: ...
    def save_execution(self, record: HypothesisExecutionRecord) -> HypothesisExecutionRecord: ...
    def executions_for_agenda(self, agenda_id: str) -> tuple[HypothesisExecutionRecord, ...]: ...

    def knowledge_entries_for_hypothesis(
        self, hypothesis_id: str
    ) -> tuple[ResearchKnowledgeEntry, ...]: ...
    def save_knowledge_entry(self, entry: ResearchKnowledgeEntry) -> ResearchKnowledgeEntry: ...
    def list_knowledge_entries(self) -> tuple[ResearchKnowledgeEntry, ...]: ...

    def save_contradiction(self, item: ResearchContradiction) -> ResearchContradiction: ...
    def list_contradictions(self) -> tuple[ResearchContradiction, ...]: ...

    def save_follow_up(self, item: FollowUpProposal) -> FollowUpProposal: ...
    def list_follow_ups(self) -> tuple[FollowUpProposal, ...]: ...

    def quality_report_by_agenda(self, agenda_id: str) -> AgendaQualityReport | None: ...
    def save_quality_report(self, report: AgendaQualityReport) -> AgendaQualityReport: ...


class OptionsForwardCollaborator(Protocol):
    """The narrow, read-only slice of options evidence the forward seam
    (section 23) may describe -- never used to build a training sample."""

    def latest_checkpoint(self, plan_id: str) -> object | None: ...


# ==========================================================================
# Calendar / alignment helpers (shared by every panel-building step)
# ==========================================================================


def _end_of_day(session_date: str) -> datetime:
    return datetime.combine(_date.fromisoformat(session_date), time(23, 59, 59), tzinfo=UTC)


class TradingCalendar:
    """The shared session-date axis every corporate panel row and outcome
    window is measured against -- the real, retained historical-replay
    run's own session dates, never a synthetic calendar."""

    def __init__(self, dates: Sequence[str]) -> None:
        self.dates: tuple[str, ...] = tuple(sorted(set(dates)))
        self._index: dict[str, int] = {date: i for i, date in enumerate(self.dates)}

    def index_of(self, session_date: str) -> int | None:
        return self._index.get(session_date)

    def session_after(self, *, on_or_after: str) -> str | None:
        for date in self.dates:
            if date >= on_or_after:
                return date
        return None

    def forward(self, *, from_date: str, sessions: int) -> str | None:
        index = self.index_of(from_date)
        if index is None:
            return None
        target = index + sessions
        if target >= len(self.dates):
            return None
        return self.dates[target]

    def monthly_cadence(self) -> tuple[str, ...]:
        """One date per (year, month) -- the first retained session of
        each calendar month. Corporate fundamentals change at most a
        handful of times per year (quarterly filings, occasional 8-Ks);
        sampling the full daily replay calendar for a bounded 12-24-
        hypothesis agenda would recompute an unchanged snapshot ~20x more
        often than needed, so this deliberate, disclosed cadence keeps the
        shared feature matrix bounded (section 30) while still spanning
        the entire retained lookback window at ~72 dates over six years."""

        kept: list[str] = []
        last_month: tuple[int, int] | None = None
        for date_str in self.dates:
            parsed = _date.fromisoformat(date_str)
            month_key = (parsed.year, parsed.month)
            if month_key != last_month:
                kept.append(date_str)
                last_month = month_key
        return tuple(kept)


def align_to_calendar(
    series: Sequence[DatedValue], *, calendar: TradingCalendar
) -> tuple[Decimal, ...] | None:
    """Forward-fills ``series`` onto ``calendar``'s exact date axis --
    identical technique to ``application.cross_market_research.
    BuildCrossMarketPanel``'s own ``_aligned`` helper. Returns ``None``
    (never a fabricated leading value) when the series has no observation
    on or before the calendar's first date."""

    by_date = {item.session_date: item.value for item in series}
    values: list[Decimal] = []
    last: Decimal | None = None
    for date in calendar.dates:
        value = by_date.get(date, last)
        if value is None:
            return None
        values.append(value)
        last = value
    return tuple(values)


def window_drawdown(
    values: Sequence[Decimal], *, start_index: int, end_index: int
) -> Decimal | None:
    """Maximum peak-to-trough drawdown strictly *within* one bounded
    forward window ``[start_index, end_index]`` -- deliberately distinct
    from ``cross_market_research.rolling_drawdown_series``, which measures
    drawdown from the start of the entire series, not a bounded outcome
    window."""

    if start_index < 0 or end_index >= len(values) or end_index <= start_index:
        return None
    window = values[start_index : end_index + 1]
    with localcontext() as context:
        context.prec = 28
        peak = window[0]
        worst = Decimal(0)
        for value in window:
            peak = max(peak, value)
            if peak > 0:
                worst = max(worst, (peak - value) / peak)
    return worst.quantize(Decimal("0.00000001"))


def split_train_validation_holdout(
    dates: Sequence[str], *, replay_run_id: str
) -> ResearchPeriodPolicy | None:
    """The one, agenda-wide, predeclared 70/15/15 train/validation/locked-
    holdout split (milestone item 1: "locked train/validation/holdout
    structure") -- computed once from the monthly-cadence date axis and
    reused identically by every hypothesis's own governance policy.
    ``None`` when there are too few monthly dates to split meaningfully."""

    ordered = sorted(set(dates))
    if len(ordered) < MINIMUM_MONTHLY_DATES:
        return None
    train_cut = int(len(ordered) * Decimal("0.70"))
    validation_cut = int(len(ordered) * Decimal("0.85"))
    train_cut = max(1, min(train_cut, len(ordered) - 2))
    validation_cut = max(train_cut + 1, min(validation_cut, len(ordered) - 1))
    train_end = ordered[train_cut - 1]
    validation_start = ordered[train_cut]
    validation_end = ordered[validation_cut - 1]
    holdout_start = ordered[validation_cut]
    return ResearchPeriodPolicy.create(
        replay_run_id=replay_run_id,
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, ordered[0], train_end, False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, validation_start, validation_end, False
            ),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, holdout_start, ordered[-1], True),
        ),
        minimum_sample_count=resolve_minimum_sample_requirement(
            phase=ResearchPhase.VALIDATION
        ).minimum_raw_sample,
        created_at=_end_of_day(ordered[-1]),
    )


# ==========================================================================
# Section 30: the shared corporate + market panel, built once per agenda
# run and reused by every hypothesis.
# ==========================================================================


class CorporateContextDimension:
    """String labels for the four market-context conditioning dimensions
    this agenda actually uses (section 4) -- deliberately a small, fixed
    subset of ``cross_market_research.CrossMarketRegimeDimension``, not
    every dimension that module defines (section 4: "must not become an
    excuse to perform another large parameter search")."""

    EQUITY_TREND = "EQUITY_TREND"
    EQUITY_VOLATILITY = "EQUITY_VOLATILITY"
    EQUITY_RISK_TONE = "EQUITY_RISK_TONE"
    BOND_RISK_RELATIONSHIP = "BOND_RISK_RELATIONSHIP"


@dataclass(frozen=True, slots=True)
class CorporatePanel:
    calendar: TradingCalendar
    monthly_dates: tuple[str, ...]
    corporate_dataset_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    usable_security_ids: tuple[str, ...]
    sector_by_security: tuple[tuple[str, str], ...]
    features_by_security: dict[str, tuple[CorporateFeatureObservation, ...]]
    aligned_closes_by_security: dict[str, tuple[Decimal, ...]]
    context_labels: dict[str, dict[str, str]]
    filing_events_by_security: dict[str, tuple[CorporateFilingEventObservation, ...]]
    excluded_low_quality_security_ids: tuple[str, ...]
    warnings: tuple[str, ...]

    def fingerprint(self) -> str:
        """Corporate Research Panel Determinism v1, section 12: a canonical,
        order-independent content fingerprint over every genuinely
        research-relevant field this panel carries. Identical retained
        evidence + identical as-of + identical policy must reproduce this
        identical fingerprint; any drift in fact selection, feature
        values, missingness, or membership changes it. Computed on demand
        from already-existing fields -- reuses this codebase's one
        existing ``content_digest`` convention rather than inventing a
        parallel identity system, and needs no new persisted field or
        migration (a bounded operator diagnostic, section 15)."""
        feature_rows = tuple(
            sorted(
                (
                    security,
                    observation.session_date,
                    feature_id.value,
                    value,
                    tuple(sorted(observation.fact_lineage)),
                )
                for security, observations in self.features_by_security.items()
                for observation in observations
                for feature_id, value in observation.values
            )
        )
        filing_event_rows = tuple(
            sorted(
                (security, item.filing_event_id, item.first_outcome_session_date)
                for security, items in self.filing_events_by_security.items()
                for item in items
            )
        )
        payload = (
            "CORPORATE_PANEL_FINGERPRINT_V1",
            self.corporate_dataset_id,
            self.replay_run_id,
            self.cross_market_dataset_id,
            self.usable_security_ids,
            self.excluded_low_quality_security_ids,
            self.sector_by_security,
            feature_rows,
            filing_event_rows,
            self.warnings,
        )
        return content_digest(payload)

    def sector_of(self, stable_security_id: str) -> str | None:
        for key, value in self.sector_by_security:
            if key == stable_security_id:
                return value
        return None

    def feature_observation(
        self, stable_security_id: str, session_date: str
    ) -> CorporateFeatureObservation | None:
        for item in self.features_by_security.get(stable_security_id, ()):
            if item.session_date == session_date:
                return item
        return None

    def context_label(self, dimension: str, session_date: str) -> str | None:
        return self.context_labels.get(dimension, {}).get(session_date)

    def outcome(
        self, *, stable_security_id: str, session_date: str, horizon_sessions: int
    ) -> CorporateOutcomeObservation | None:
        closes = self.aligned_closes_by_security.get(stable_security_id)
        if closes is None:
            return None
        index = self.calendar.index_of(session_date)
        if index is None:
            return None
        broad_closes = self.aligned_closes_by_security.get(BROAD_MARKET_SYMBOL, ())
        security_return = window_return(
            closes, start_index=index, horizon_sessions=horizon_sessions
        )
        volatility = window_volatility(closes, start_index=index, horizon_sessions=horizon_sessions)
        drawdown = window_drawdown(closes, start_index=index, end_index=index + horizon_sessions)
        broad_return = (
            window_return(broad_closes, start_index=index, horizon_sessions=horizon_sessions)
            if broad_closes
            else None
        )
        benchmark_relative = (
            None
            if security_return is None or broad_return is None
            else (security_return - broad_return).quantize(Decimal("0.00000001"))
        )
        sector = self.sector_of(stable_security_id)
        sector_return = self._sector_basket_return(
            sector, session_date=session_date, horizon_sessions=horizon_sessions
        )
        sector_relative = sector_relative_return(
            security_return=security_return, sector_basket_return=sector_return
        )
        values = (
            (CorporateOutcomeId.SUBSEQUENT_RETURN, security_return),
            (CorporateOutcomeId.SUBSEQUENT_REALIZED_VOLATILITY, volatility),
            (CorporateOutcomeId.DRAWDOWN, drawdown),
            (CorporateOutcomeId.BENCHMARK_RELATIVE_RETURN, benchmark_relative),
            (CorporateOutcomeId.SECTOR_RELATIVE_RETURN, sector_relative),
        )
        return CorporateOutcomeObservation(
            stable_security_id=stable_security_id,
            session_date=session_date,
            horizon_sessions=horizon_sessions,
            values=values,
            complete=security_return is not None,
        )

    def _sector_basket_return(
        self, sector: str | None, *, session_date: str, horizon_sessions: int
    ) -> Decimal | None:
        if sector is None:
            return None
        index = self.calendar.index_of(session_date)
        if index is None:
            return None
        members = [key for key, value in self.sector_by_security if value == sector]
        returns: list[Decimal] = []
        for member in members:
            closes = self.aligned_closes_by_security.get(member)
            if closes is None:
                continue
            value = window_return(closes, start_index=index, horizon_sessions=horizon_sessions)
            if value is not None:
                returns.append(value)
        if not returns:
            return None
        with localcontext() as context:
            context.prec = 28
            return sum(returns, Decimal(0)) / Decimal(len(returns))


def window_return(
    closes: Sequence[Decimal], *, start_index: int, horizon_sessions: int
) -> Decimal | None:
    target = start_index + horizon_sessions
    if start_index < 0 or target >= len(closes):
        return None
    base = closes[start_index]
    if base == 0:
        return None
    with localcontext() as context:
        context.prec = 28
        return ((closes[target] - base) / base).quantize(Decimal("0.00000001"))


def window_volatility(
    closes: Sequence[Decimal], *, start_index: int, horizon_sessions: int
) -> Decimal | None:
    target = start_index + horizon_sessions
    if start_index < 0 or target >= len(closes) or horizon_sessions < 2:
        return None
    window = closes[start_index : target + 1]
    with localcontext() as context:
        context.prec = 28
        daily = [
            (window[i] - window[i - 1]) / window[i - 1]
            for i in range(1, len(window))
            if window[i - 1] != 0
        ]
        if len(daily) < 2:
            return None
        mean = sum(daily, Decimal(0)) / Decimal(len(daily))
        variance = sum(((value - mean) ** 2 for value in daily), Decimal(0)) / Decimal(len(daily))
        annualized = variance.sqrt() * Decimal(EQUITY_SESSIONS_PER_YEAR).sqrt()
    return annualized.quantize(Decimal("0.00000001"))


# ==========================================================================
# Corporate feature observation: reuses the exact retained point-in-time
# projection (``build_corporate_fundamental_snapshot`` / ``compute_derived_
# features`` / ``compute_filing_event_features``) for ten of twelve
# fields, verbatim; ``AMENDMENT_INDICATOR``/``FILING_LAG_FROM_PERIOD_END_
# DAYS`` are derived here, directly from the identical already-visible
# ``CorporateFilingEvent`` evidence, without changing
# ``domain.corporate_fundamentals`` at all.
# ==========================================================================

_AMENDMENT_FORMS = frozenset({FilingForm.FORM_10K_A, FilingForm.FORM_10Q_A, FilingForm.FORM_8K_A})
_CORE_FORMS = frozenset({FilingForm.FORM_10K, FilingForm.FORM_10Q})
_AMENDMENT_LOOKBACK_DAYS = 30


def _amendment_indicator(filings: Sequence[CorporateFilingEvent], *, as_of: datetime) -> Decimal:
    visible = visible_filings_as_of(filings, as_of=as_of)
    window_start = as_of - timedelta(days=_AMENDMENT_LOOKBACK_DAYS)
    recent_amendment = any(
        item.form in _AMENDMENT_FORMS and item.effective_available_at >= window_start
        for item in visible
    )
    return Decimal(1) if recent_amendment else Decimal(0)


def _filing_lag_days(filings: Sequence[CorporateFilingEvent], *, as_of: datetime) -> Decimal | None:
    visible = tuple(
        item
        for item in visible_filings_as_of(filings, as_of=as_of)
        if item.form in _CORE_FORMS and item.report_period is not None
    )
    if not visible:
        return None
    latest = max(visible, key=lambda item: item.effective_available_at)
    assert latest.report_period is not None
    try:
        period_end = _date.fromisoformat(latest.report_period)
    except ValueError:
        return None
    lag_days = (latest.effective_available_at.date() - period_end).days
    if lag_days < 0:
        return None
    return Decimal(lag_days)


def build_feature_observation(
    *,
    stable_security_id: str,
    cik: str,
    session_date: str,
    filings: Sequence[CorporateFilingEvent],
    facts: Sequence[FundamentalFact],
) -> CorporateFeatureObservation | None:
    as_of = _end_of_day(session_date)
    snapshot = build_corporate_fundamental_snapshot(
        stable_security_id=stable_security_id, cik=cik, as_of=as_of, filings=filings, facts=facts
    )
    if snapshot.quality.value in ("INSUFFICIENT", "REJECTED"):
        return None
    derived = compute_derived_features(snapshot=snapshot, facts=facts)
    filing_features = compute_filing_event_features(
        stable_security_id=stable_security_id, as_of=as_of, filings=filings
    )
    derived_by_type = {item.feature_type.value: item.value for item in derived}
    filing_by_type = {item.feature_type.value: item.value for item in filing_features}
    values: tuple[tuple[CorporateFeatureId, Decimal | None], ...] = (
        (CorporateFeatureId.REVENUE_GROWTH_YOY, derived_by_type.get("YOY_REVENUE_GROWTH")),
        (CorporateFeatureId.NET_INCOME_GROWTH_YOY, derived_by_type.get("YOY_NET_INCOME_GROWTH")),
        (
            CorporateFeatureId.OPERATING_CASH_FLOW_GROWTH_YOY,
            derived_by_type.get("YOY_OPERATING_CASH_FLOW_GROWTH"),
        ),
        (CorporateFeatureId.OPERATING_MARGIN, derived_by_type.get("OPERATING_MARGIN")),
        (
            CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
            derived_by_type.get("OPERATING_MARGIN_CHANGE_YOY"),
        ),
        (
            CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
            derived_by_type.get("LEVERAGE_LIABILITIES_TO_ASSETS"),
        ),
        (CorporateFeatureId.CASH_TO_ASSETS, derived_by_type.get("CASH_TO_ASSETS")),
        (
            CorporateFeatureId.SHARE_COUNT_CHANGE_YOY,
            derived_by_type.get("SHARE_COUNT_CHANGE_YOY"),
        ),
        (
            CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
            filing_by_type.get("DAYS_SINCE_LAST_10Q_OR_10K"),
        ),
        (CorporateFeatureId.RECENT_8K_INDICATOR, filing_by_type.get("RECENT_8K_INDICATOR")),
        (CorporateFeatureId.AMENDMENT_INDICATOR, _amendment_indicator(filings, as_of=as_of)),
        (
            CorporateFeatureId.FILING_LAG_FROM_PERIOD_END_DAYS,
            _filing_lag_days(filings, as_of=as_of),
        ),
    )
    missing = tuple(sorted(key.value for key, value in values if value is None))
    return CorporateFeatureObservation(
        stable_security_id=stable_security_id,
        session_date=session_date,
        as_of=as_of,
        values=values,
        snapshot_quality=snapshot.quality.value,
        missing_fields=missing,
        fact_lineage=snapshot.fact_lineage,
    )


# ==========================================================================
# Filing-event observations (section 12): event time = retained SEC
# availability/acceptance evidence; outcome windows begin only after
# event availability; an after-close event shifts to the next applicable
# market session.
# ==========================================================================


def build_filing_event_observations(
    *, stable_security_id: str, filings: Sequence[CorporateFilingEvent], calendar: TradingCalendar
) -> tuple[CorporateFilingEventObservation, ...]:
    observations: list[CorporateFilingEventObservation] = []
    for filing in filings:
        if filing.form not in (
            FilingForm.FORM_10K,
            FilingForm.FORM_10Q,
            FilingForm.FORM_10K_A,
            FilingForm.FORM_10Q_A,
            FilingForm.FORM_8K,
            FilingForm.FORM_8K_A,
        ):
            continue
        available = filing.effective_available_at
        event_date = available.date().isoformat()
        after_close = available.hour >= APPROXIMATE_US_EQUITY_CLOSE_UTC_HOUR
        candidate_date = calendar.session_after(
            on_or_after=event_date if not after_close else _next_calendar_day(event_date)
        )
        if candidate_date is None:
            continue
        shifted = after_close or candidate_date != event_date
        is_earnings_proxy = "2.02" in filing.items
        observations.append(
            CorporateFilingEventObservation(
                stable_security_id=stable_security_id,
                filing_event_id=filing.filing_event_id,
                form=filing.form.value,
                is_earnings_proxy=is_earnings_proxy,
                is_amendment=filing.form in _AMENDMENT_FORMS,
                event_available_at=available,
                event_session_date=event_date,
                first_outcome_session_date=candidate_date,
                shifted_to_next_session=shifted,
            )
        )
    return tuple(sorted(observations, key=lambda item: item.event_available_at))


def _next_calendar_day(session_date: str) -> str:
    return (_date.fromisoformat(session_date) + timedelta(days=1)).isoformat()


# ==========================================================================
# BuildCorporatePanel: the single shared feature/outcome/context matrix,
# built once per agenda run (section 30) and reused by every hypothesis.
# ==========================================================================


class BuildCorporatePanel:
    def __init__(
        self,
        *,
        corporate_repository: CorporateFundamentalsCollaborator,
        historical_repository: HistoricalMarketDataCollaborator,
        evidence: SecuritiesEvidenceCollaborator,
        replay_repository: HistoricalReplayCollaborator,
    ) -> None:
        self._corporate = corporate_repository
        self._historical = historical_repository
        self._evidence = evidence
        self._replay = replay_repository

    def execute(
        self,
        *,
        corporate_dataset_id: str,
        equity_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
    ) -> CorporatePanel:
        dataset = self._corporate.dataset_snapshot_by_id(corporate_dataset_id)
        if dataset is None:
            raise CorporateMarketResearchNotFound("CORPORATE_DATASET_SNAPSHOT_NOT_FOUND")
        replay_run = self._replay.get_run(replay_run_id)
        if replay_run is None:
            raise CorporateMarketResearchNotFound("HISTORICAL_REPLAY_RUN_NOT_FOUND")
        frames = self._replay.list_frames(replay_run_id, limit=5_000)
        if not frames:
            raise CorporateMarketResearchApplicationError("REPLAY_RUN_HAS_NO_RETAINED_FRAMES")
        calendar = TradingCalendar(sorted({frame.session_date for frame in frames}))
        monthly_dates = calendar.monthly_cadence()
        if len(monthly_dates) < MINIMUM_MONTHLY_DATES:
            raise CorporateMarketResearchApplicationError(
                "INSUFFICIENT_MONTHLY_CADENCE_DATES_FOR_A_LOCKED_TRAIN_VALIDATION_HOLDOUT_SPLIT"
            )

        equity_snapshot = self._historical.snapshot_by_id(equity_snapshot_id)
        if equity_snapshot is None:
            raise CorporateMarketResearchNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        plan = self._historical.get_plan(equity_snapshot.plan_id)
        if plan is None:
            raise CorporateMarketResearchNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")

        bars_by_instrument: dict[str, list[SecuritiesBarEvidence]] = {}
        for batch_id in equity_snapshot.source_batch_ids:
            _instruments, _sessions, bars, _actions = self._evidence.get_dataset(batch_id)
            for bar in bars:
                bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)

        aligned_closes_by_security: dict[str, tuple[Decimal, ...]] = {}
        symbol_by_security: dict[str, str] = {}
        for member in plan.universe:
            raw_bars = sorted(
                bars_by_instrument.get(member.instrument_id, ()), key=lambda item: item.period_end
            )
            views = tuple(observation_view_from_securities_bar(bar) for bar in raw_bars)
            series = to_dated_closes(views)
            aligned = align_to_calendar(series, calendar=calendar)
            if aligned is not None:
                aligned_closes_by_security[member.stable_security_id] = aligned
                aligned_closes_by_security[member.symbol_at_capture] = aligned
            symbol_by_security[member.stable_security_id] = member.symbol_at_capture

        if BROAD_MARKET_SYMBOL not in aligned_closes_by_security:
            raise CorporateMarketResearchApplicationError("BROAD_EQUITY_BENCHMARK_SPY_NOT_RESOLVED")

        sector_by_symbol = {
            decl.symbol: decl.sector.value for decl in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members
        }
        roles_by_symbol = {
            decl.symbol: decl.roles for decl in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members
        }
        etf_symbols = frozenset(
            decl.symbol for decl in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members if decl.is_etf
        )

        context_labels = self._build_context_labels(
            calendar=calendar,
            monthly_dates=monthly_dates,
            aligned_closes_by_security=aligned_closes_by_security,
            roles_by_symbol=roles_by_symbol,
            etf_symbols=etf_symbols,
        )

        candidate_ids = tuple(
            sid for sid in dataset.stable_security_ids if sid in aligned_closes_by_security
        )
        all_filings = self._corporate.filings_for_securities(candidate_ids)
        all_facts = self._corporate.facts_for_securities(candidate_ids)
        filings_by_security: dict[str, list[CorporateFilingEvent]] = {}
        for filing_item in all_filings:
            filings_by_security.setdefault(filing_item.stable_security_id, []).append(filing_item)
        facts_by_security: dict[str, list[FundamentalFact]] = {}
        for fact_item in all_facts:
            facts_by_security.setdefault(fact_item.stable_security_id, []).append(fact_item)
        cik_by_security = {item.stable_security_id: item.cik for item in all_filings}
        for fact_item in all_facts:
            cik_by_security.setdefault(fact_item.stable_security_id, fact_item.cik)

        usable_ids: list[str] = []
        excluded_ids: list[str] = []
        features_by_security: dict[str, tuple[CorporateFeatureObservation, ...]] = {}
        filing_events_by_security: dict[str, tuple[CorporateFilingEventObservation, ...]] = {}
        for stable_security_id in candidate_ids:
            cik = cik_by_security.get(stable_security_id)
            filings = tuple(filings_by_security.get(stable_security_id, ()))
            facts = tuple(facts_by_security.get(stable_security_id, ()))
            if cik is None or (not filings and not facts):
                excluded_ids.append(stable_security_id)
                continue
            observations = tuple(
                observation
                for date_str in monthly_dates
                if (
                    observation := build_feature_observation(
                        stable_security_id=stable_security_id,
                        cik=cik,
                        session_date=date_str,
                        filings=filings,
                        facts=facts,
                    )
                )
                is not None
            )
            if len(observations) < MINIMUM_MONTHLY_DATES // 2:
                excluded_ids.append(stable_security_id)
                continue
            usable_ids.append(stable_security_id)
            features_by_security[stable_security_id] = observations
            filing_events_by_security[stable_security_id] = build_filing_event_observations(
                stable_security_id=stable_security_id, filings=filings, calendar=calendar
            )

        sector_by_security = tuple(
            (sid, sector_by_symbol[symbol_by_security[sid]])
            for sid in usable_ids
            if symbol_by_security.get(sid) in sector_by_symbol
        )

        warnings = corporate_market_research_warnings()
        if excluded_ids:
            warnings = (
                *warnings,
                f"EXCLUDED_{len(excluded_ids)}_CANDIDATE_SECURITIES_INSUFFICIENT_EVIDENCE",
            )
        return CorporatePanel(
            calendar=calendar,
            monthly_dates=monthly_dates,
            corporate_dataset_id=corporate_dataset_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            usable_security_ids=tuple(sorted(usable_ids)),
            sector_by_security=sector_by_security,
            features_by_security=features_by_security,
            aligned_closes_by_security=aligned_closes_by_security,
            context_labels=context_labels,
            filing_events_by_security=filing_events_by_security,
            excluded_low_quality_security_ids=tuple(sorted(excluded_ids)),
            warnings=warnings,
        )

    @staticmethod
    def _build_context_labels(
        *,
        calendar: TradingCalendar,
        monthly_dates: tuple[str, ...],
        aligned_closes_by_security: dict[str, tuple[Decimal, ...]],
        roles_by_symbol: Mapping[str, tuple[object, ...]],
        etf_symbols: frozenset[str],
    ) -> dict[str, dict[str, str]]:
        spy = aligned_closes_by_security[BROAD_MARKET_SYMBOL]
        tlt = aligned_closes_by_security.get(RATES_PROXY_SYMBOL)
        equity_return_20 = rolling_returns(spy, window_sessions=20)
        equity_volatility_20 = rolling_volatility_series(
            spy, window_sessions=20, sessions_per_year=EQUITY_SESSIONS_PER_YEAR
        )
        rates_return_20 = rolling_returns(tlt, window_sessions=20) if tlt else None
        rates_correlation_60 = (
            rolling_correlation_series(tlt, spy, window_sessions=60) if tlt else None
        )
        equity_symbols = tuple(
            symbol
            for symbol in roles_by_symbol
            if symbol not in etf_symbols and symbol in aligned_closes_by_security
        )
        equity_returns_1 = {
            symbol: rolling_returns(aligned_closes_by_security[symbol], window_sessions=1)
            for symbol in equity_symbols
        }

        labels: dict[str, dict[str, str]] = {
            CorporateContextDimension.EQUITY_TREND: {},
            CorporateContextDimension.EQUITY_VOLATILITY: {},
            CorporateContextDimension.EQUITY_RISK_TONE: {},
            CorporateContextDimension.BOND_RISK_RELATIONSHIP: {},
        }
        for date_str in monthly_dates:
            index = calendar.index_of(date_str)
            if index is None:
                continue
            trend = classify_trend_strength(equity_return_20[index])
            volatility = classify_volatility_regime(equity_volatility_20[index])
            advancing = [
                value
                for series in equity_returns_1.values()
                if (value := series[index]) is not None
            ]
            breadth = classify_breadth_strength(fraction_positive(advancing))
            tone = classify_risk_tone(breadth=breadth, volatility=volatility, trend=trend)
            bond = classify_bond_risk_relationship(
                rates_proxy_return_20=(rates_return_20[index] if rates_return_20 else None),
                rates_equity_correlation_60=(
                    rates_correlation_60[index] if rates_correlation_60 else None
                ),
            )
            labels[CorporateContextDimension.EQUITY_TREND][date_str] = trend.value
            labels[CorporateContextDimension.EQUITY_VOLATILITY][date_str] = volatility.value
            labels[CorporateContextDimension.EQUITY_RISK_TONE][date_str] = tone.value
            labels[CorporateContextDimension.BOND_RISK_RELATIONSHIP][date_str] = bond.value
        return labels


# ==========================================================================
# Sample construction: every research design (sections 5, 9) reduces to a
# ``HypothesisSample`` -- a treatment population and a benchmark
# population, both restricted to one governed period's date range. This is
# the one place template-specific mechanics live; every downstream
# statistic (diagnostics, robustness, conclusion) is template-agnostic.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class HypothesisSample:
    treatment: tuple[CorporatePanelRow, ...]
    benchmark: tuple[CorporatePanelRow, ...]
    benchmark_kind: BenchmarkKind


EMPTY_SAMPLE = HypothesisSample((), (), BenchmarkKind.EQUAL_WEIGHT_COHORT)


def _dates_in_range(dates: Sequence[str], *, start: str, end: str) -> tuple[str, ...]:
    return tuple(item for item in dates if start <= item <= end)


def _feature_value(
    panel: CorporatePanel, *, security: str, date: str, feature_id: CorporateFeatureId
) -> Decimal | None:
    observation = panel.feature_observation(security, date)
    return None if observation is None else observation.feature(feature_id)


def _outcome_value(
    panel: CorporatePanel,
    *,
    security: str,
    date: str,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
) -> Decimal | None:
    observation = panel.outcome(
        stable_security_id=security, session_date=date, horizon_sessions=horizon_sessions
    )
    return None if observation is None else observation.outcome(outcome_id)


def _train_median_threshold(
    panel: CorporatePanel, *, dates: Sequence[str], feature_id: CorporateFeatureId
) -> Decimal | None:
    """The train-period-only median threshold every ``ABOVE_TRAIN_MEDIAN``
    split uses (section 5's ``ConditionRule``, reused from
    ``domain.cross_market_research``): derived *only* from the declared
    TRAIN period, then applied identically and without re-derivation to
    validation/holdout -- the same fixed-a-priori-threshold discipline
    ``cross_market_research.ConditionRule`` itself documents."""

    values: list[Decimal] = []
    for security in panel.usable_security_ids:
        for date in dates:
            value = _feature_value(panel, security=security, date=date, feature_id=feature_id)
            if value is not None:
                values.append(value)
    return _median_decimal(values) if len(values) >= 4 else None


def _median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 28
        return (ordered[middle - 1] + ordered[middle]) / 2


def _cross_sectional_rows(
    panel: CorporatePanel,
    *,
    dates: Sequence[str],
    feature_id: CorporateFeatureId,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    threshold: Decimal,
    feature_security_override: dict[tuple[str, str], str] | None = None,
    feature_date_override: dict[tuple[str, str], str] | None = None,
) -> HypothesisSample:
    """The shared cross-sectional/change-based row builder. ``feature_
    security_override``/``feature_date_override`` let negative controls
    (section 9) redirect *which* security's or *which date's* feature
    value is read while the outcome is always read for the genuine
    ``(security, date)`` pair -- the deliberate misalignment that makes a
    control a control."""

    benchmark: list[CorporatePanelRow] = []
    treatment: list[CorporatePanelRow] = []
    for security in panel.usable_security_ids:
        for date in dates:
            feature_security = (
                feature_security_override.get((security, date), security)
                if feature_security_override
                else security
            )
            feature_date = (
                feature_date_override.get((security, date), date) if feature_date_override else date
            )
            feature_value = _feature_value(
                panel, security=feature_security, date=feature_date, feature_id=feature_id
            )
            outcome_value = _outcome_value(
                panel,
                security=security,
                date=date,
                outcome_id=outcome_id,
                horizon_sessions=horizon_sessions,
            )
            if feature_value is None or outcome_value is None:
                continue
            row = CorporatePanelRow(
                stable_security_id=security, session_date=date, value=outcome_value
            )
            benchmark.append(row)
            if feature_value >= threshold:
                treatment.append(row)
    return HypothesisSample(
        treatment=tuple(treatment),
        benchmark=tuple(benchmark),
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
    )


def _randomized_issuer_rows(
    panel: CorporatePanel,
    *,
    dates: Sequence[str],
    feature_id: CorporateFeatureId,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    threshold: Decimal,
    seed: int,
) -> HypothesisSample:
    """Negative control: shuffles the security->feature-value mapping
    *within each date* (deterministic per-date seed) so the feature-outcome
    link is randomized while the date/market structure -- which securities
    and dates genuinely exist -- is preserved exactly."""

    benchmark: list[CorporatePanelRow] = []
    treatment: list[CorporatePanelRow] = []
    for date_ordinal, date in enumerate(dates):
        securities = list(panel.usable_security_ids)
        features = [
            _feature_value(panel, security=s, date=date, feature_id=feature_id) for s in securities
        ]
        generator = random.Random(seed + date_ordinal)
        shuffled = list(features)
        generator.shuffle(shuffled)
        for security, feature_value in zip(securities, shuffled, strict=True):
            outcome_value = _outcome_value(
                panel,
                security=security,
                date=date,
                outcome_id=outcome_id,
                horizon_sessions=horizon_sessions,
            )
            if feature_value is None or outcome_value is None:
                continue
            row = CorporatePanelRow(
                stable_security_id=security, session_date=date, value=outcome_value
            )
            benchmark.append(row)
            if feature_value >= threshold:
                treatment.append(row)
    return HypothesisSample(
        treatment=tuple(treatment),
        benchmark=tuple(benchmark),
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
    )


def _random_matched_dates(
    *, universe: Sequence[tuple[str, str]], count: int, seed: int
) -> tuple[tuple[str, str], ...]:
    if not universe or count <= 0:
        return ()
    generator = random.Random(seed)
    pool = list(universe)
    generator.shuffle(pool)
    return tuple(pool[: min(count, len(pool))])


def _filing_event_rows(
    panel: CorporatePanel,
    *,
    hypothesis: GeneratedCorporateHypothesis,
    dates: Sequence[str],
    randomize_event_dates: bool,
    seed: int,
) -> HypothesisSample:
    date_range = frozenset(dates)
    events: list[tuple[str, str]] = []
    for security, observations in panel.filing_events_by_security.items():
        for observation in observations:
            if observation.first_outcome_session_date not in date_range:
                continue
            if hypothesis.negative_control_kind is None:
                is_earnings = hypothesis.feature_id is CorporateFeatureId.RECENT_8K_INDICATOR
                is_amendment = hypothesis.feature_id is CorporateFeatureId.AMENDMENT_INDICATOR
                if is_earnings and not observation.is_earnings_proxy:
                    continue
                if is_amendment and not observation.is_amendment:
                    continue
                if not is_earnings and not is_amendment and observation.is_amendment:
                    continue
            events.append((security, observation.first_outcome_session_date))
    universe = tuple((security, date) for security in panel.usable_security_ids for date in dates)
    if randomize_event_dates:
        events = list(
            _random_matched_dates(universe=universe, count=max(len(events), 20), seed=seed)
        )
    treatment: list[CorporatePanelRow] = []
    for security, date in events:
        outcome_value = _outcome_value(
            panel,
            security=security,
            date=date,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
        )
        if outcome_value is not None:
            treatment.append(
                CorporatePanelRow(
                    stable_security_id=security, session_date=date, value=outcome_value
                )
            )
    matched = _random_matched_dates(universe=universe, count=len(treatment) or 20, seed=seed + 1)
    benchmark: list[CorporatePanelRow] = []
    for security, date in matched:
        outcome_value = _outcome_value(
            panel,
            security=security,
            date=date,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
        )
        if outcome_value is not None:
            benchmark.append(
                CorporatePanelRow(
                    stable_security_id=security, session_date=date, value=outcome_value
                )
            )
    return HypothesisSample(
        treatment=tuple(treatment),
        benchmark=tuple(benchmark),
        benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE,
    )


NEGATIVE_CONTROL_DISPLACEMENT_STEPS = 3  # ~1 quarter at monthly cadence
NEGATIVE_CONTROL_SEED = 20260809


def build_hypothesis_sample(
    hypothesis: GeneratedCorporateHypothesis,
    *,
    panel: CorporatePanel,
    period_policy: ResearchPeriodPolicy,
    role: ResearchPeriodRole,
) -> HypothesisSample | None:
    """The one dispatcher every research design (sections 5, 9) routes
    through. Every branch reduces to the same ``HypothesisSample`` shape
    so every downstream statistic is template-agnostic."""

    period = next((item for item in period_policy.periods if item.role is role), None)
    if period is None:
        return None
    dates = _dates_in_range(panel.monthly_dates, start=period.start_date, end=period.end_date)
    train_period = next(
        item for item in period_policy.periods if item.role is ResearchPeriodRole.TRAIN
    )
    train_dates = _dates_in_range(
        panel.monthly_dates, start=train_period.start_date, end=train_period.end_date
    )

    if hypothesis.template_id == "FILING_EVENT_V1":
        filing_dates = _dates_in_range(
            panel.calendar.dates, start=period.start_date, end=period.end_date
        )
        return _filing_event_rows(
            panel,
            hypothesis=hypothesis,
            dates=filing_dates,
            randomize_event_dates=(
                hypothesis.negative_control_kind == "RANDOM_FILING_EVENT_DATE_MATCHED_CALENDAR"
            ),
            seed=NEGATIVE_CONTROL_SEED,
        )

    restricted_dates = dates
    restricted_train_dates = train_dates
    if hypothesis.secondary_context_dimension is not None:
        label = hypothesis.secondary_condition_label
        restricted_dates = tuple(
            d
            for d in dates
            if panel.context_label(hypothesis.secondary_context_dimension, d) == label
        )
        restricted_train_dates = tuple(
            d
            for d in train_dates
            if panel.context_label(hypothesis.secondary_context_dimension, d) == label
        )

    threshold = _train_median_threshold(
        panel, dates=restricted_train_dates, feature_id=hypothesis.feature_id
    )
    if threshold is None:
        return EMPTY_SAMPLE

    if hypothesis.negative_control_kind == "RANDOMIZED_ISSUER_ASSIGNMENT":
        return _randomized_issuer_rows(
            panel,
            dates=restricted_dates,
            feature_id=hypothesis.feature_id,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
            threshold=threshold,
            seed=NEGATIVE_CONTROL_SEED,
        )
    if hypothesis.negative_control_kind == "TEMPORALLY_DISPLACED_CORPORATE_FACT":
        overrides = {
            (security, date): _displaced_date(panel, date)
            for security in panel.usable_security_ids
            for date in restricted_dates
        }
        return _cross_sectional_rows(
            panel,
            dates=restricted_dates,
            feature_id=hypothesis.feature_id,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
            threshold=threshold,
            feature_date_override=overrides,
        )
    if hypothesis.negative_control_kind == "MISMATCHED_ISSUER_FUNDAMENTALS":
        rotation = _issuer_rotation(panel.usable_security_ids)
        overrides = {
            (security, date): rotation[security]
            for security in panel.usable_security_ids
            for date in restricted_dates
        }
        return _cross_sectional_rows(
            panel,
            dates=restricted_dates,
            feature_id=hypothesis.feature_id,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
            threshold=threshold,
            feature_security_override=overrides,
        )
    return _cross_sectional_rows(
        panel,
        dates=restricted_dates,
        feature_id=hypothesis.feature_id,
        outcome_id=hypothesis.outcome_id,
        horizon_sessions=hypothesis.horizon_sessions,
        threshold=threshold,
    )


def _displaced_date(panel: CorporatePanel, date: str) -> str:
    index = panel.monthly_dates.index(date) if date in panel.monthly_dates else None
    if index is None or index - NEGATIVE_CONTROL_DISPLACEMENT_STEPS < 0:
        return date
    return panel.monthly_dates[index - NEGATIVE_CONTROL_DISPLACEMENT_STEPS]


def _issuer_rotation(security_ids: Sequence[str]) -> dict[str, str]:
    ordered = sorted(security_ids)
    if len(ordered) < 2:
        return {sid: sid for sid in ordered}
    return {sid: ordered[(i + 1) % len(ordered)] for i, sid in enumerate(ordered)}


# ==========================================================================
# Statistical diagnostics, benchmark comparison, and trial construction --
# reuses every generic piece of ``domain.research_methodology`` (
# ``ReplayStatisticalSummary`` via ``summarize_values``, ``EffectSize``,
# ``StandardErrorEstimate``, ``BootstrapConfidenceInterval``,
# ``BenchmarkComparison``, ``ResearchTrial``) and supplies only the
# panel-dependence-aware overlap diagnostic those functions do not compute
# themselves (section 15).
# ==========================================================================


def _effect_size_for_panel_sample(
    sample_values: Sequence[Decimal], benchmark_values: Sequence[Decimal]
) -> EffectSize:
    """A drop-in, panel-safe replacement for ``domain.research_methodology.
    compute_effect_size``: identical Cohen's-d computation, but with
    ``is_return_series=False`` on both of its internal ``summarize_values``
    calls, for the same reason ``build_corporate_diagnostics`` passes it
    below -- ``sample_values``/``benchmark_values`` here are pooled
    cross-sectional/panel observations, not a chronological return path
    (exact precedent: ``cross_market_research._effect_size_for_panel_
    sample``)."""
    from apex.domain.historical_replay import SummaryKind, summarize_values

    if len(sample_values) < 2 or len(benchmark_values) < 2:
        return EffectSize(cohens_d=None)
    sample_summary = summarize_values(
        tuple(sample_values),
        kind=SummaryKind.EXPLORATORY,
        group_label="effect_size_sample",
        is_return_series=False,
    )
    benchmark_summary = summarize_values(
        tuple(benchmark_values),
        kind=SummaryKind.EXPLORATORY,
        group_label="effect_size_benchmark",
        is_return_series=False,
    )
    if (
        sample_summary.stdev is None
        or benchmark_summary.stdev is None
        or sample_summary.mean is None
        or benchmark_summary.mean is None
    ):
        return EffectSize(cohens_d=None)
    n1, n2 = len(sample_values), len(benchmark_values)
    with localcontext() as context:
        context.prec = 28
        pooled_variance = (
            Decimal(n1 - 1) * sample_summary.stdev**2 + Decimal(n2 - 1) * benchmark_summary.stdev**2
        ) / Decimal(n1 + n2 - 2)
        if pooled_variance <= 0:
            return EffectSize(cohens_d=None)
        cohens_d = (sample_summary.mean - benchmark_summary.mean) / pooled_variance.sqrt()
    return EffectSize(cohens_d=bound_statistical_decimal(cohens_d))


def build_corporate_diagnostics(
    sample: HypothesisSample, *, bootstrap_seed: int | None = None
) -> StatisticalDiagnosticsV2:
    from apex.domain.historical_replay import SummaryKind, summarize_values

    values = tuple(row.value for row in sample.treatment)
    # ``sample.treatment`` is a pooled cross-sectional/panel sample (many
    # securities and/or dates contributing one independent observation
    # each), never one chronological return path -- compounding it via
    # ``_cumulative_return`` would be both meaningless and, for the larger
    # panels this module builds, numerically unbounded (see
    # ``cross_market_research._effect_size_for_panel_sample``, the same
    # precedent for the same reason).
    base = summarize_values(
        values,
        kind=SummaryKind.EXPLORATORY,
        group_label="corporate_trial",
        is_return_series=False,
    )
    panel_overlap = compute_panel_dependence_diagnostics(sample.treatment)
    notes: list[str] = []
    standard_error: StandardErrorEstimate | None = None
    if base.stdev is not None and panel_overlap.effective_sample_count >= 2:
        with localcontext() as context:
            context.prec = 28
            sample_sqrt = panel_overlap.effective_sample_count.sqrt()
        standard_error = StandardErrorEstimate(
            value=bound_statistical_decimal(base.stdev / sample_sqrt),
            method_id="IID_NORMAL_APPROXIMATION_V1",
        )
    benchmark_values = tuple(row.value for row in sample.benchmark)
    effect_size = (
        _effect_size_for_panel_sample(values, benchmark_values) if benchmark_values else None
    )
    bootstrap: BootstrapConfidenceInterval | None = None
    if bootstrap_seed is not None:
        means = clustered_bootstrap_means(sample.treatment, seed=bootstrap_seed)
        if means:
            ordered = sorted(means)
            tail = max(0, int(len(ordered) * Decimal("0.025")))
            bootstrap = BootstrapConfidenceInterval(
                lower=bound_statistical_decimal(ordered[tail]),
                upper=bound_statistical_decimal(ordered[len(ordered) - 1 - tail]),
                resample_count=len(ordered),
                seed=bootstrap_seed,
                method_id="SEEDED_PERCENTILE_BOOTSTRAP_V1",
            )
    if not panel_overlap.inference_available:
        notes.append(panel_overlap.note)
    return StatisticalDiagnosticsV2(
        base=base,
        overlap=panel_overlap,
        standard_error=standard_error,
        effect_size=effect_size,
        bootstrap_confidence_interval=bootstrap,
        grouped=(),
        inference_available=panel_overlap.inference_available,
        methodology_notes=tuple(notes),
    )


def build_corporate_trial(
    hypothesis: GeneratedCorporateHypothesis,
    sample: HypothesisSample,
    *,
    dataset_snapshot_id: str,
    replay_run_id: str,
    phase: ResearchPhase,
    governance_policy: ResearchGovernancePolicy | None,
    created_at: datetime,
    bootstrap_seed: int | None = None,
) -> ResearchTrial | None:
    if not sample.treatment:
        return None
    diagnostics = build_corporate_diagnostics(sample, bootstrap_seed=bootstrap_seed)
    benchmark: BenchmarkComparison | None = None
    if sample.benchmark and diagnostics.base.mean is not None:
        benchmark_values = tuple(row.value for row in sample.benchmark)
        benchmark = compare_to_benchmark(
            sample_mean=diagnostics.base.mean,
            benchmark_kind=sample.benchmark_kind,
            benchmark_values=benchmark_values,
        )
    configuration = ResearchConfiguration.create(
        method_id=CORPORATE_MARKET_METHOD_ID,
        method_version=hypothesis.template_version,
        parameter_values=(
            ("feature_id", hypothesis.feature_id.value),
            ("outcome_id", hypothesis.outcome_id.value),
            ("horizon_sessions", str(hypothesis.horizon_sessions)),
            ("template_id", hypothesis.template_id),
        ),
        origin=ConfigurationOrigin.FIXED_A_PRIORI,
        candidates_examined_count=1,
    )
    dated_ids = tuple(
        sorted(
            {
                (
                    row.session_date,
                    deterministic_id(
                        "corporate_market_panel_row",
                        (hypothesis.hypothesis_id, row.stable_security_id, row.session_date, phase),
                    ),
                )
                for row in sample.treatment
            }
        )
    )
    return ResearchTrial.create(
        hypothesis_version_id=hypothesis.hypothesis_id,
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        phase=phase,
        market_scope=(MarketId.US_EQUITY.value,),
        configuration=configuration,
        dated_evaluation_ids=dated_ids,
        diagnostics=diagnostics,
        benchmark=benchmark,
        created_at=created_at,
        governance_policy=governance_policy,
    )


def trial_p_value(trial: ResearchTrial) -> Decimal | None:
    if trial.benchmark is None or trial.diagnostics.base.stdev is None:
        return None
    return two_sided_z_test_p_value(
        sample_mean=trial.benchmark.absolute_result,
        benchmark_mean=trial.benchmark.benchmark_mean,
        sample_stdev=trial.diagnostics.base.stdev,
        effective_sample_count=trial.diagnostics.overlap.effective_sample_count,
    )


def _beats_benchmark(trial: ResearchTrial | None) -> bool | None:
    if trial is None or trial.benchmark is None:
        return None
    return trial.benchmark.beats_benchmark


# ==========================================================================
# Robustness (section 19): reuses ``RobustnessCheckType``/``RobustnessCheckResult``/
# ``RobustnessCheckRun``/``summarize_robustness`` unmodified. Two check
# types (``WINSORIZED_SENSITIVITY``, ``MEDIAN_VS_MEAN_COMPARISON``) were
# added to that shared enum for this milestone; the other three reuse
# existing members with a corporate-specific ``variant_label``. Computed
# only over TRAIN+VALIDATION -- the locked HOLDOUT period is never
# perturbed by a development-track diagnostic.
# ==========================================================================


def _rows_by(sample: HypothesisSample, key: str) -> dict[str, list[CorporatePanelRow]]:
    grouped: dict[str, list[CorporatePanelRow]] = {}
    for row in sample.treatment:
        grouped.setdefault(
            row.stable_security_id if key == "security" else row.session_date, []
        ).append(row)
    return grouped


def _mean_of(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 28
        return sum(values, Decimal(0)) / Decimal(len(values))


def _direction_consistent(base_mean: Decimal, candidate_mean: Decimal | None) -> bool | None:
    if candidate_mean is None or base_mean == 0:
        return None
    return (candidate_mean > 0) == (base_mean > 0)


def _leave_one_group_out(
    sample: HypothesisSample, *, panel: CorporatePanel, base_mean: Decimal, by: str
) -> RobustnessCheckResult:
    check_type = RobustnessCheckType.ASSET_COHORT_EXCLUSION
    if by == "security":
        grouped = _rows_by(sample, "security")
    else:
        grouped = {}
        for row in sample.treatment:
            sector = panel.sector_of(row.stable_security_id) or "UNKNOWN"
            grouped.setdefault(sector, []).append(row)
    if not grouped:
        return RobustnessCheckResult(check_type, f"leave_one_{by}_out", 0, None, None, "NO_GROUPS")
    largest_key = max(grouped, key=lambda key: len(grouped[key]))
    excluded = frozenset(grouped[largest_key])
    remaining = tuple(row for row in sample.treatment if row not in excluded)
    mean = _mean_of(tuple(row.value for row in remaining))
    return RobustnessCheckResult(
        check_type=check_type,
        variant_label=f"leave_one_{by}_out_excluding_{largest_key}",
        sample_count=len(remaining),
        result_mean=bound_statistical_decimal(mean) if mean is not None else None,
        direction_consistent_with_base=_direction_consistent(base_mean, mean),
        note="OK" if remaining else "INSUFFICIENT_DATA_AFTER_EXCLUSION",
    )


def _leave_one_period_out(sample: HypothesisSample, *, base_mean: Decimal) -> RobustnessCheckResult:
    grouped = _rows_by(sample, "date")
    if not grouped:
        return RobustnessCheckResult(
            RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
            "leave_one_period_out",
            0,
            None,
            None,
            "NO_DATES",
        )
    largest_date = max(grouped, key=lambda key: len(grouped[key]))
    remaining = tuple(row for row in sample.treatment if row.session_date != largest_date)
    mean = _mean_of(tuple(row.value for row in remaining))
    return RobustnessCheckResult(
        check_type=RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
        variant_label=f"leave_one_period_out_excluding_{largest_date}",
        sample_count=len(remaining),
        result_mean=bound_statistical_decimal(mean) if mean is not None else None,
        direction_consistent_with_base=_direction_consistent(base_mean, mean),
        note="OK" if remaining else "INSUFFICIENT_DATA_AFTER_EXCLUSION",
    )


def _winsorized_sensitivity(
    sample: HypothesisSample, *, base_mean: Decimal
) -> RobustnessCheckResult:
    values = sorted(row.value for row in sample.treatment)
    if len(values) < 5:
        return RobustnessCheckResult(
            RobustnessCheckType.WINSORIZED_SENSITIVITY,
            "winsorized_5_95",
            len(values),
            None,
            None,
            "INSUFFICIENT_SAMPLE_FOR_WINSORIZATION",
        )
    lower_index = max(0, int(len(values) * Decimal("0.05")))
    upper_index = min(len(values) - 1, len(values) - 1 - int(len(values) * Decimal("0.05")))
    floor, ceiling = values[lower_index], values[upper_index]
    winsorized = tuple(min(max(value, floor), ceiling) for value in values)
    mean = _mean_of(winsorized)
    return RobustnessCheckResult(
        check_type=RobustnessCheckType.WINSORIZED_SENSITIVITY,
        variant_label="winsorized_5_95",
        sample_count=len(winsorized),
        result_mean=bound_statistical_decimal(mean) if mean is not None else None,
        direction_consistent_with_base=_direction_consistent(base_mean, mean),
        note="OK",
    )


def _median_vs_mean(sample: HypothesisSample, *, base_mean: Decimal) -> RobustnessCheckResult:
    values = tuple(row.value for row in sample.treatment)
    median = _median_decimal(values)
    return RobustnessCheckResult(
        check_type=RobustnessCheckType.MEDIAN_VS_MEAN_COMPARISON,
        variant_label="median_vs_mean",
        sample_count=len(values),
        result_mean=bound_statistical_decimal(median) if median is not None else None,
        direction_consistent_with_base=_direction_consistent(base_mean, median),
        note="OK" if median is not None else "NO_SAMPLE",
    )


def compute_robustness(
    sample: HypothesisSample, *, panel: CorporatePanel, base_mean: Decimal
) -> tuple[RobustnessCheckResult, ...]:
    return (
        _leave_one_group_out(sample, panel=panel, base_mean=base_mean, by="security"),
        _leave_one_group_out(sample, panel=panel, base_mean=base_mean, by="sector"),
        _leave_one_period_out(sample, base_mean=base_mean),
        _winsorized_sensitivity(sample, base_mean=base_mean),
        _median_vs_mean(sample, base_mean=base_mean),
    )


# ==========================================================================
# Time stability (section 17): reuses ``StabilityReport``/``SubperiodResult``/
# ``derive_stability_state`` from ``domain.cross_market_research`` verbatim.
# ==========================================================================


def compute_stability(sample: HypothesisSample) -> StabilityReport | None:
    dates = sorted({row.session_date for row in sample.treatment})
    if len(dates) < 6:
        return None
    third = max(1, len(dates) // 3)
    bounds = (
        dates[0],
        dates[third - 1],
        dates[third],
        dates[2 * third - 1],
        dates[2 * third],
        dates[-1],
    )
    labels = ("EARLY", "MIDDLE", "LATE")
    subperiods = []
    for index, label in enumerate(labels):
        start, end = bounds[index * 2], bounds[index * 2 + 1]
        rows = tuple(row for row in sample.treatment if start <= row.session_date <= end)
        mean = _mean_of(tuple(row.value for row in rows))
        subperiods.append(
            SubperiodResult(
                label=label,
                start_date=start,
                end_date=end,
                sample_count=len(rows),
                mean=bound_statistical_decimal(mean) if mean is not None else None,
                is_high_stress_evidence=False,
            )
        )
    state = derive_stability_state(subperiods)
    return StabilityReport(
        subperiods=tuple(subperiods),
        recent_holdout=None,
        state=state,
        evidence_note="EARLY_MIDDLE_LATE_TERCILE_SPLIT_OF_THE_HYPOTHESIS_OWN_VALIDATION_SAMPLE",
    )


# ==========================================================================
# RunSingleCorporateHypothesis: governance policy + exploratory/validation/
# locked-holdout trials + robustness + stability for one hypothesis.
# Deliberately does *not* yet build the trial family, conclusion, promotion
# evaluation, or the final ``HypothesisExecutionRecord`` -- those require
# every sibling hypothesis in the same family to have already run
# (section 20), so ``FinalizeCorporateAgenda`` builds them afterward.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class HypothesisRunOutcome:
    hypothesis: GeneratedCorporateHypothesis
    governance_policy: ResearchGovernancePolicy
    exploratory_trial: ResearchTrial | None
    validation_trial: ResearchTrial | None
    holdout_trial: ResearchTrial | None
    robustness_run: RobustnessCheckRun | None
    stability: StabilityReport | None
    validation_sample: HypothesisSample
    holdout_sample: HypothesisSample


def reconstruct_hypothesis_outcome(
    execution: HypothesisExecutionRecord,
    hypothesis: GeneratedCorporateHypothesis,
    methodology: ResearchMethodologyCollaborator,
) -> HypothesisRunOutcome | None:
    """Rebuilds a reporting-only ``HypothesisRunOutcome`` from already-
    persisted evidence (used by ``corporate-market-quality-inspect`` and
    similar read-only surfaces) -- never recomputes a trial, never touches
    a provider, and never re-derives ``validation_sample``/``holdout_sample``
    (both left ``EMPTY_SAMPLE``: every reporting consumer of a
    reconstructed outcome reads trial/robustness/stability evidence only,
    never the raw sample rows)."""

    governance_policy = methodology.governance_policy_by_id(execution.governance_policy_id)
    if governance_policy is None:
        return None
    return HypothesisRunOutcome(
        hypothesis=hypothesis,
        governance_policy=governance_policy,
        exploratory_trial=(
            methodology.trial_by_id(execution.exploratory_trial_id)
            if execution.exploratory_trial_id
            else None
        ),
        validation_trial=(
            methodology.trial_by_id(execution.validation_trial_id)
            if execution.validation_trial_id
            else None
        ),
        holdout_trial=(
            methodology.trial_by_id(execution.holdout_trial_id)
            if execution.holdout_trial_id
            else None
        ),
        robustness_run=(
            methodology.robustness_run_by_id(execution.robustness_run_id)
            if execution.robustness_run_id
            else None
        ),
        stability=execution.stability,
        validation_sample=EMPTY_SAMPLE,
        holdout_sample=EMPTY_SAMPLE,
    )


class RunSingleCorporateHypothesis:
    def __init__(
        self,
        *,
        methodology: ResearchMethodologyCollaborator,
        panel: CorporatePanel,
        period_policy: ResearchPeriodPolicy,
    ) -> None:
        self._methodology = methodology
        self._panel = panel
        self._period_policy = period_policy

    def execute(
        self, hypothesis: GeneratedCorporateHypothesis, *, created_at: datetime
    ) -> HypothesisRunOutcome:
        policy = ResearchGovernancePolicy.create(
            hypothesis_version_id=hypothesis.hypothesis_id,
            dataset_snapshot_id=self._panel.corporate_dataset_id,
            replay_run_id=self._panel.replay_run_id,
            intent=GovernanceIntent.PROMOTION_TRACK,
            period_policy=self._period_policy,
            revision=1,
            supersedes_governance_policy_id=None,
            declared_at=created_at,
        )
        existing_policy = self._methodology.governance_policy_by_id(policy.governance_policy_id)
        policy = (
            existing_policy
            if existing_policy is not None
            else self._methodology.save_governance_policy(policy)
        )

        train_sample = build_hypothesis_sample(
            hypothesis,
            panel=self._panel,
            period_policy=self._period_policy,
            role=ResearchPeriodRole.TRAIN,
        )
        validation_sample = build_hypothesis_sample(
            hypothesis,
            panel=self._panel,
            period_policy=self._period_policy,
            role=ResearchPeriodRole.VALIDATION,
        )
        guard_governed_holdout_access(policy, purpose="EVALUATION_ONLY")
        holdout_sample = build_hypothesis_sample(
            hypothesis,
            panel=self._panel,
            period_policy=self._period_policy,
            role=ResearchPeriodRole.HOLDOUT,
        )
        exploratory_sample = HypothesisSample(
            treatment=(train_sample.treatment if train_sample else ())
            + (validation_sample.treatment if validation_sample else ()),
            benchmark=(train_sample.benchmark if train_sample else ())
            + (validation_sample.benchmark if validation_sample else ()),
            benchmark_kind=(
                validation_sample.benchmark_kind
                if validation_sample
                else BenchmarkKind.EQUAL_WEIGHT_COHORT
            ),
        )

        exploratory_trial = self._save_trial(
            build_corporate_trial(
                hypothesis,
                exploratory_sample,
                dataset_snapshot_id=self._panel.corporate_dataset_id,
                replay_run_id=self._panel.replay_run_id,
                phase=ResearchPhase.EXPLORATORY,
                governance_policy=None,
                created_at=created_at,
            )
        )
        validation_trial = self._save_trial(
            build_corporate_trial(
                hypothesis,
                validation_sample or EMPTY_SAMPLE,
                dataset_snapshot_id=self._panel.corporate_dataset_id,
                replay_run_id=self._panel.replay_run_id,
                phase=ResearchPhase.VALIDATION,
                governance_policy=policy,
                created_at=created_at,
                bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
            )
        )
        holdout_trial = self._save_trial(
            build_corporate_trial(
                hypothesis,
                holdout_sample or EMPTY_SAMPLE,
                dataset_snapshot_id=self._panel.corporate_dataset_id,
                replay_run_id=self._panel.replay_run_id,
                phase=ResearchPhase.LOCKED_HOLDOUT,
                governance_policy=policy,
                created_at=created_at,
            )
        )
        if holdout_trial is not None:
            exposure = HoldoutExposureEvent.create(
                governance_policy=policy,
                exposed_at=created_at,
                actor="system:corporate_market_research_agenda_run",
                purpose="EVALUATION_ONLY",
                consumed_trial_id=holdout_trial.trial_id,
            )
            self._methodology.save_holdout_exposure(exposure)

        robustness_run: RobustnessCheckRun | None = None
        if validation_trial is not None and validation_trial.diagnostics.base.mean is not None:
            validation_or_empty = validation_sample or EMPTY_SAMPLE
            dev_sample = HypothesisSample(
                treatment=(train_sample.treatment if train_sample else ())
                + validation_or_empty.treatment,
                benchmark=(train_sample.benchmark if train_sample else ())
                + validation_or_empty.benchmark,
                benchmark_kind=validation_or_empty.benchmark_kind,
            )
            results = compute_robustness(
                dev_sample, panel=self._panel, base_mean=validation_trial.diagnostics.base.mean
            )
            candidate_run = RobustnessCheckRun.create(
                base_trial_id=validation_trial.trial_id, results=results, created_at=created_at
            )
            existing_run = self._methodology.robustness_run_by_id(candidate_run.robustness_run_id)
            robustness_run = (
                existing_run
                if existing_run is not None
                else self._methodology.save_robustness_run(candidate_run)
            )

        stability = compute_stability(validation_sample) if validation_sample else None
        return HypothesisRunOutcome(
            hypothesis=hypothesis,
            governance_policy=policy,
            exploratory_trial=exploratory_trial,
            validation_trial=validation_trial,
            holdout_trial=holdout_trial,
            robustness_run=robustness_run,
            stability=stability,
            validation_sample=validation_sample or EMPTY_SAMPLE,
            holdout_sample=holdout_sample or EMPTY_SAMPLE,
        )

    def _save_trial(self, trial: ResearchTrial | None) -> ResearchTrial | None:
        if trial is None:
            return None
        existing = self._methodology.trial_by_id(trial.trial_id)
        return existing if existing is not None else self._methodology.save_trial(trial)


# ==========================================================================
# FinalizeCorporateAgenda: family-level multiple-testing correction
# (section 20), per-hypothesis conclusion/promotion/execution-record,
# knowledge base, contradictions, follow-ups, and the agenda-level
# quality report -- run once, after every hypothesis has an outcome.
# ==========================================================================

CORPORATE_PROMOTION_POLICY = PromotionPolicy(
    policy_id="CORPORATE_MARKET_RESEARCH_V3_PROMOTION_POLICY", require_holdout_consumed=True
)


def _family_test_result(outcome: HypothesisRunOutcome) -> TrialTestResult | None:
    if outcome.validation_trial is None:
        return None
    return TrialTestResult(
        trial_id=outcome.validation_trial.trial_id,
        label=outcome.hypothesis.hypothesis_id,
        p_value=trial_p_value(outcome.validation_trial),
    )


def build_trial_families(
    outcomes: Sequence[HypothesisRunOutcome], *, panel: CorporatePanel, created_at: datetime
) -> dict[CorporateHypothesisFamily, ResearchTrialFamily]:
    families: dict[CorporateHypothesisFamily, ResearchTrialFamily] = {}
    by_family: dict[CorporateHypothesisFamily, list[HypothesisRunOutcome]] = {}
    for outcome in outcomes:
        by_family.setdefault(outcome.hypothesis.family, []).append(outcome)
    for family, members in by_family.items():
        raw_results = tuple(
            result for outcome in members if (result := _family_test_result(outcome)) is not None
        )
        if not raw_results:
            continue
        outcome_ids = {outcome.hypothesis.outcome_id for outcome in members}
        families[family] = ResearchTrialFamily.create(
            hypothesis_id=f"corporate_market_family:{family.value}",
            candidate_count=len(members),
            asset_count=max(1, len(panel.usable_security_ids)),
            outcome_definition_count=max(1, len(outcome_ids)),
            regime_slice_count=1,
            selection_method=TrialSelectionMethod.PREDECLARED_SINGLE,
            raw_results=raw_results,
            correction_method=MultipleTestingCorrectionMethod.BENJAMINI_HOCHBERG_FDR,
            created_at=created_at,
        )
    return families


def _corrected_significance(
    family: ResearchTrialFamily | None, *, trial_id: str | None
) -> bool | None:
    if family is None or trial_id is None:
        return None
    for result in family.corrected_results:
        if result.trial_id == trial_id:
            return result.rejected_null
    return None


class FinalizeCorporateAgenda:
    def __init__(self, *, repository: CorporateMarketResearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        agenda: CorporateMarketResearchAgenda,
        outcomes: Sequence[HypothesisRunOutcome],
        panel: CorporatePanel,
        methodology: ResearchMethodologyCollaborator,
        created_at: datetime,
    ) -> AgendaQualityReport:
        families = build_trial_families(outcomes, panel=panel, created_at=created_at)
        saved_families: dict[CorporateHypothesisFamily, ResearchTrialFamily] = {}
        for family, candidate in families.items():
            existing = methodology.trial_family_by_id(candidate.trial_family_id)
            saved_families[family] = (
                existing if existing is not None else methodology.save_trial_family(candidate)
            )

        conclusion_states: dict[str, int] = {}
        for outcome in outcomes:
            trial_family = saved_families.get(outcome.hypothesis.family)
            validation_trial_id = (
                outcome.validation_trial.trial_id if outcome.validation_trial else None
            )
            corrected = _corrected_significance(trial_family, trial_id=validation_trial_id)
            requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.VALIDATION)
            effective = (
                outcome.validation_trial.diagnostics.overlap.effective_sample_count
                if outcome.validation_trial is not None
                else Decimal(0)
            )
            sample_quality = (
                SampleQualityState.SUFFICIENT
                if outcome.validation_trial is not None
                and outcome.validation_trial.diagnostics.overlap.inference_available
                and effective >= requirement.minimum_effective_sample
                else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
            )
            robustness_summary = outcome.robustness_run.summary if outcome.robustness_run else None
            conclusion_state = derive_conclusion_state(
                sample_quality=sample_quality,
                robustness_summary=robustness_summary,
                validation_beats_benchmark=_beats_benchmark(outcome.validation_trial),
                holdout_beats_benchmark=_beats_benchmark(outcome.holdout_trial),
                multiple_testing_corrected_significant=corrected,
            )
            limitations = [
                "SURVIVORSHIP_LIMITED",
                "POINT_IN_TIME_UNIVERSE_INCOMPLETE",
                "PANEL_OBSERVATIONS_SHARE_MARKET_DATES_NOT_INDEPENDENT_SAMPLES",
            ]
            if outcome.hypothesis.template_id == "FILING_EVENT_V1":
                limitations.append(
                    "FILING_ACCEPTANCE_TIME_USED_AS_EARNINGS_PROXY_NOT_EXACT_RELEASE_TIME"
                )
            conclusion = ResearchConclusion.create(
                hypothesis_version_id=outcome.hypothesis.hypothesis_id,
                dataset_snapshot_id=panel.corporate_dataset_id,
                replay_run_id=panel.replay_run_id,
                phase=(
                    ResearchPhase.LOCKED_HOLDOUT
                    if outcome.holdout_trial is not None
                    else ResearchPhase.VALIDATION
                ),
                training_trial_id=(
                    outcome.exploratory_trial.trial_id if outcome.exploratory_trial else None
                ),
                validation_trial_id=(
                    outcome.validation_trial.trial_id if outcome.validation_trial else None
                ),
                holdout_trial_id=outcome.holdout_trial.trial_id if outcome.holdout_trial else None,
                walk_forward_run_id=None,
                walk_forward_fold_count=0,
                robustness_run_id=(
                    outcome.robustness_run.robustness_run_id if outcome.robustness_run else None
                ),
                trial_family_id=trial_family.trial_family_id if trial_family is not None else None,
                sample_quality_state=sample_quality,
                point_in_time_limitations=tuple(limitations),
                benchmark_kind=(
                    outcome.validation_trial.benchmark.benchmark_kind
                    if outcome.validation_trial is not None and outcome.validation_trial.benchmark
                    else None
                ),
                beats_benchmark=_beats_benchmark(outcome.validation_trial),
                conclusion_state=conclusion_state,
                rationale=outcome.hypothesis.rationale,
                blockers=(),
                created_at=created_at,
            )
            existing_conclusion = methodology.conclusion_by_id(conclusion.conclusion_id)
            saved_conclusion = (
                existing_conclusion
                if existing_conclusion is not None
                else methodology.save_conclusion(conclusion)
            )
            conclusion_states[saved_conclusion.conclusion_state.value] = (
                conclusion_states.get(saved_conclusion.conclusion_state.value, 0) + 1
            )

            promotion = PromotionEvaluation.create(
                conclusion=saved_conclusion,
                policy=CORPORATE_PROMOTION_POLICY,
                holdout_exposed=outcome.holdout_trial is not None,
                robustness_completed=outcome.robustness_run is not None,
                lookahead_violation=False,
                created_at=created_at,
            )
            methodology.save_promotion_evaluation(promotion)

            record = HypothesisExecutionRecord.create(
                hypothesis_id=outcome.hypothesis.hypothesis_id,
                agenda_id=agenda.agenda_id,
                governance_policy_id=outcome.governance_policy.governance_policy_id,
                exploratory_trial_id=(
                    outcome.exploratory_trial.trial_id if outcome.exploratory_trial else None
                ),
                validation_trial_id=(
                    outcome.validation_trial.trial_id if outcome.validation_trial else None
                ),
                holdout_trial_id=outcome.holdout_trial.trial_id if outcome.holdout_trial else None,
                walk_forward_trial_ids=(),
                robustness_run_id=(
                    outcome.robustness_run.robustness_run_id if outcome.robustness_run else None
                ),
                trial_family_id=trial_family.trial_family_id if trial_family is not None else None,
                conclusion_id=saved_conclusion.conclusion_id,
                promotion_evaluation_id=promotion.promotion_evaluation_id,
                stability=outcome.stability,
                evidence_shape="CORPORATE_MARKET_PANEL_V3",
                blockers=(),
                created_at=created_at,
            )
            existing_record = self._repository.execution_by_hypothesis(
                outcome.hypothesis.hypothesis_id
            )
            if existing_record is None:
                self._repository.save_execution(record)
                self._save_knowledge_entry(outcome, saved_conclusion, created_at=created_at)

        self._propose_follow_ups(outcomes, methodology=methodology, created_at=created_at)
        self._detect_contradictions(outcomes, created_at=created_at)

        trials_run = sum(
            1
            for outcome in outcomes
            for trial in (
                outcome.exploratory_trial,
                outcome.validation_trial,
                outcome.holdout_trial,
            )
            if trial is not None
        )
        report = self._build_quality_report(
            agenda=agenda,
            outcomes=outcomes,
            panel=panel,
            conclusion_states=conclusion_states,
            trials_run=trials_run,
            created_at=created_at,
        )
        existing_report = self._repository.quality_report_by_agenda(agenda.agenda_id)
        if existing_report is not None:
            return existing_report
        return self._repository.save_quality_report(report)

    _CONCLUSION_TO_KNOWLEDGE: ClassVar[dict[ResearchConclusionState, KnowledgeStatus]] = {
        ResearchConclusionState.SUPPORTED: KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY,
        ResearchConclusionState.WEAK_SUPPORT: KnowledgeStatus.WEAK,
        ResearchConclusionState.REJECTED: KnowledgeStatus.REJECTED,
        ResearchConclusionState.HOLDOUT_FAILED: KnowledgeStatus.REJECTED,
        ResearchConclusionState.ROBUSTNESS_FAILED: KnowledgeStatus.REJECTED,
        ResearchConclusionState.INCONCLUSIVE: KnowledgeStatus.ACTIVE_RESEARCH,
        ResearchConclusionState.INSUFFICIENT_EVIDENCE: KnowledgeStatus.ACTIVE_RESEARCH,
    }

    def _save_knowledge_entry(
        self,
        outcome: HypothesisRunOutcome,
        conclusion: ResearchConclusion,
        *,
        created_at: datetime,
    ) -> ResearchKnowledgeEntry:
        status = self._CONCLUSION_TO_KNOWLEDGE[conclusion.conclusion_state]
        regime_dependence = (
            outcome.stability.state.value
            if outcome.stability is not None
            else "INSUFFICIENT_EVIDENCE"
        )
        shape = (
            "FILING_EVENT_STUDY"
            if outcome.hypothesis.template_id == "FILING_EVENT_V1"
            else "CROSS_SECTIONAL_PANEL"
        )
        entry = ResearchKnowledgeEntry.create(
            hypothesis_id=outcome.hypothesis.hypothesis_id,
            conclusion_id=conclusion.conclusion_id,
            status=status,
            evidence_strength=(
                f"validation_beats_benchmark={_beats_benchmark(outcome.validation_trial)} "
                f"holdout_beats_benchmark={_beats_benchmark(outcome.holdout_trial)}"
            ),
            phase_reached=conclusion.phase.value,
            markets=(MarketId.US_EQUITY,),
            regime_dependence=regime_dependence,
            benchmark_kind=(
                conclusion.benchmark_kind.value if conclusion.benchmark_kind else "NONE"
            ),
            holdout_result=str(_beats_benchmark(outcome.holdout_trial)),
            robustness_state=(
                "PASSED"
                if outcome.robustness_run is not None and outcome.robustness_run.summary.passed
                else "NOT_PASSED_OR_UNAVAILABLE"
            ),
            cross_sectional_or_time_series=shape,
            limitations=conclusion.point_in_time_limitations,
            revision=1,
            supersedes_entry_id=None,
            created_at=created_at,
        )
        existing = self._repository.knowledge_entries_for_hypothesis(
            outcome.hypothesis.hypothesis_id
        )
        for item in existing:
            if item.entry_id == entry.entry_id:
                return item
        return self._repository.save_knowledge_entry(entry)

    def _propose_follow_ups(
        self,
        outcomes: Sequence[HypothesisRunOutcome],
        *,
        methodology: ResearchMethodologyCollaborator,
        created_at: datetime,
    ) -> tuple[FollowUpProposal, ...]:
        proposals: list[FollowUpProposal] = []
        for outcome in outcomes:
            entries = self._repository.knowledge_entries_for_hypothesis(
                outcome.hypothesis.hypothesis_id
            )
            latest = entries[-1] if entries else None
            if latest is None:
                continue
            kind: FollowUpKind | None = None
            if latest.status in (KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY, KnowledgeStatus.WEAK):
                kind = (
                    FollowUpKind.SPLIT_BY_REGIME
                    if outcome.hypothesis.secondary_context_dimension is None
                    else FollowUpKind.ALTERNATE_HORIZON
                )
            elif outcome.robustness_run is not None and not outcome.robustness_run.summary.passed:
                kind = FollowUpKind.INVESTIGATE_ROBUSTNESS_FAILURE
            if kind is None:
                continue
            proposal = FollowUpProposal.create(
                parent_conclusion_id=latest.conclusion_id,
                parent_hypothesis_id=outcome.hypothesis.hypothesis_id,
                kind=kind,
                proposed_template_id=HypothesisTemplateId(outcome.hypothesis.template_id),
                description=(
                    f"Bounded follow-up for {outcome.hypothesis.feature_id.value} / "
                    f"{outcome.hypothesis.outcome_id.value}: {kind.value}."
                ),
                created_at=created_at,
            )
            existing = self._repository.list_follow_ups()
            if any(item.follow_up_id == proposal.follow_up_id for item in existing):
                proposals.append(proposal)
                continue
            proposals.append(self._repository.save_follow_up(proposal))
        return tuple(proposals)

    def _detect_contradictions(
        self, outcomes: Sequence[HypothesisRunOutcome], *, created_at: datetime
    ) -> tuple[ResearchContradiction, ...]:
        contradictions: list[ResearchContradiction] = []
        by_feature: dict[CorporateFeatureId, list[HypothesisRunOutcome]] = {}
        for outcome in outcomes:
            if outcome.hypothesis.is_negative_control:
                continue
            by_feature.setdefault(outcome.hypothesis.feature_id, []).append(outcome)
        for _feature_id, members in by_feature.items():
            for first, second in _pairwise(members):
                first_direction = _beats_benchmark(first.validation_trial)
                second_direction = _beats_benchmark(second.validation_trial)
                if first_direction is None or second_direction is None:
                    continue
                if first_direction == second_direction:
                    continue
                first_entries = self._repository.knowledge_entries_for_hypothesis(
                    first.hypothesis.hypothesis_id
                )
                second_entries = self._repository.knowledge_entries_for_hypothesis(
                    second.hypothesis.hypothesis_id
                )
                if not first_entries or not second_entries:
                    continue
                contradiction = ResearchContradiction.create(
                    first_entry_id=first_entries[-1].entry_id,
                    second_entry_id=second_entries[-1].entry_id,
                    shared_evidence=(first.hypothesis.feature_id.value,),
                    first_holds_when=f"template={first.hypothesis.template_id}",
                    second_holds_when=f"template={second.hypothesis.template_id}",
                    description=(
                        f"{first.hypothesis.feature_id.value} shows opposite benchmark-relative "
                        "direction across two retained corporate hypotheses using it."
                    ),
                    created_at=created_at,
                )
                existing = self._repository.list_contradictions()
                already_saved = any(
                    item.contradiction_id == contradiction.contradiction_id for item in existing
                )
                if already_saved:
                    contradictions.append(contradiction)
                    continue
                contradictions.append(self._repository.save_contradiction(contradiction))
        return tuple(contradictions)

    @staticmethod
    def _build_quality_report(
        *,
        agenda: CorporateMarketResearchAgenda,
        outcomes: Sequence[HypothesisRunOutcome],
        panel: CorporatePanel,
        conclusion_states: dict[str, int],
        trials_run: int,
        created_at: datetime,
    ) -> AgendaQualityReport:
        return AgendaQualityReport.create(
            agenda_id=agenda.agenda_id,
            dataset_limitations=(
                "SURVIVORSHIP_LIMITED",
                "POINT_IN_TIME_UNIVERSE_INCOMPLETE",
                f"EXCLUDED_LOW_QUALITY_SECURITIES:{len(panel.excluded_low_quality_security_ids)}",
                "FILING_ACCEPTANCE_TIME_USED_AS_EARNINGS_PROXY_NOT_EXACT_RELEASE_TIME",
                "MONTHLY_FEATURE_CADENCE_NOT_DAILY",
            ),
            provider_coverage_limitations=(
                "SIX_YEAR_BOUNDED_CORPORATE_LOOKBACK",
                "OPTIONS_FORWARD_EVIDENCE_STRUCTURAL_SEAM_ONLY_NEVER_HISTORICAL",
            ),
            panel_dependence_note=(
                "Every cross-sectional/conditional/interaction sample clusters many securities on "
                "shared market dates; effective sample uses the distinct-date count via "
                "compute_panel_dependence_diagnostics, never the raw (security, date) row count."
            ),
            multiple_testing_burden_note=(
                f"{len(build_trial_families(outcomes, panel=panel, created_at=created_at))} "
                "family-level Benjamini-Hochberg corrections applied across "
                f"{len(outcomes)} hypotheses (genuine and negative-control)."
            ),
            negative_control_behavior_note=(
                f"{sum(1 for o in outcomes if o.hypothesis.is_negative_control)} negative-control "
                "hypotheses retained across five required kinds; see calibration classification "
                "for matched-stratum sufficiency per kind."
            ),
            holdout_integrity_note=(
                "Locked HOLDOUT trials were only ever read through guard_governed_holdout_access "
                "with purpose=EVALUATION_ONLY; robustness checks used TRAIN+VALIDATION only."
            ),
            conclusion_distribution=tuple(sorted(conclusion_states.items())),
            hypotheses_proposed=len(outcomes),
            trials_run=trials_run,
            created_at=created_at,
        )


def _pairwise(
    items: Sequence[HypothesisRunOutcome],
) -> tuple[tuple[HypothesisRunOutcome, HypothesisRunOutcome], ...]:
    return tuple((items[i], items[j]) for i in range(len(items)) for j in range(i + 1, len(items)))


# ==========================================================================
# The bounded, predeclared v3 hypothesis catalog (sections 1, 2, 9): 20
# genuine hypotheses (within the required 12-24 range) spanning all five
# templates, plus 17 negative controls spanning all five required kinds.
# Registered *before* any result is viewed; never widened after seeing
# results (section 1's own instruction).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class _Spec:
    template_id: str
    family: CorporateHypothesisFamily
    feature_id: CorporateFeatureId
    outcome_id: CorporateOutcomeId
    horizon_sessions: int
    rationale: str
    benchmark_kind: BenchmarkKind = BenchmarkKind.EQUAL_WEIGHT_COHORT
    secondary_context_dimension: str | None = None
    secondary_condition_label: str | None = None
    is_negative_control: bool = False
    negative_control_kind: str | None = None
    control_parameters: tuple[tuple[str, str], ...] = ()


_CROSS_SECTIONAL_LEVEL: tuple[_Spec, ...] = (
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.REVENUE_GROWTH_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Companies with above-median point-in-time revenue growth have different subsequent "
        "20-session returns than peers with below-median growth.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.REVENUE_GROWTH_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        60,
        "Same relationship as the 20-session revenue-growth hypothesis, at a 60-session horizon.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.NET_INCOME_GROWTH_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Companies with above-median point-in-time net-income growth have different subsequent "
        "returns than peers with below-median growth.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.OPERATING_MARGIN,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Companies with above-median operating margin have different subsequent returns than "
        "peers with below-median margin.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.OPERATING_MARGIN,
        CorporateOutcomeId.BENCHMARK_RELATIVE_RETURN,
        60,
        "Above-median operating margin is associated with a different subsequent broad-market-"
        "relative return at a 60-session horizon.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Companies with above-median leverage (liabilities/assets) have different subsequent "
        "returns than peers with below-median leverage.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.CASH_TO_ASSETS,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Companies with above-median cash/assets have different subsequent returns than peers "
        "with below-median cash/assets.",
    ),
    _Spec(
        "CROSS_SECTIONAL_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_LEVEL,
        CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
        CorporateOutcomeId.SUBSEQUENT_REALIZED_VOLATILITY,
        20,
        "Companies with a longer time since their last 10-Q/10-K have different subsequent "
        "realized volatility than peers with fresher filings.",
    ),
)

_CROSS_SECTIONAL_CHANGE: tuple[_Spec, ...] = (
    _Spec(
        "CORPORATE_CHANGE_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
        CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Improving (above-median change) operating margin is associated with different "
        "subsequent returns than deteriorating operating margin.",
    ),
    _Spec(
        "CORPORATE_CHANGE_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
        CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        60,
        "Same operating-margin-change relationship at a 60-session horizon.",
    ),
    _Spec(
        "CORPORATE_CHANGE_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
        CorporateFeatureId.SHARE_COUNT_CHANGE_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Above-median share-count change (dilution) is associated with different subsequent "
        "returns than below-median change (buybacks).",
    ),
    _Spec(
        "CORPORATE_CHANGE_V1",
        CorporateHypothesisFamily.CROSS_SECTIONAL_CHANGE,
        CorporateFeatureId.OPERATING_CASH_FLOW_GROWTH_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Improving operating-cash-flow growth is associated with different subsequent returns "
        "than deteriorating operating-cash-flow growth.",
    ),
)

_FILING_EVENT: tuple[_Spec, ...] = (
    _Spec(
        "FILING_EVENT_V1",
        CorporateHypothesisFamily.FILING_EVENT,
        CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        5,
        "Market behavior in the 5 sessions after a 10-Q/10-K becomes available differs from an "
        "unconditional random-date matched baseline.",
        benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE,
    ),
    _Spec(
        "FILING_EVENT_V1",
        CorporateHypothesisFamily.FILING_EVENT,
        CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
        CorporateOutcomeId.SUBSEQUENT_REALIZED_VOLATILITY,
        5,
        "Realized volatility in the 5 sessions after a 10-Q/10-K becomes available differs from "
        "a random-date matched baseline.",
        benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE,
    ),
    _Spec(
        "FILING_EVENT_V1",
        CorporateHypothesisFamily.FILING_EVENT,
        CorporateFeatureId.RECENT_8K_INDICATOR,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        5,
        "Market behavior in the 5 sessions after an Item 2.02 (results-of-operations) 8-K "
        "filing/earnings-proxy event differs from a random-date matched baseline. This is a "
        "filing/earnings-proxy event, not an exact earnings-release timestamp.",
        benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE,
    ),
    _Spec(
        "FILING_EVENT_V1",
        CorporateHypothesisFamily.FILING_EVENT,
        CorporateFeatureId.AMENDMENT_INDICATOR,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        5,
        "Market behavior in the 5 sessions after a 10-K/A or 10-Q/A amendment filing becomes "
        "available differs from a random-date matched baseline.",
        benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE,
    ),
)

_CONDITIONAL: tuple[_Spec, ...] = (
    _Spec(
        "CONDITIONAL_RELATIONSHIP_V1",
        CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME,
        CorporateFeatureId.REVENUE_GROWTH_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Above-median revenue growth behaves differently when the broad equity market is in an "
        "uptrend than the unconditional relationship suggests.",
        secondary_context_dimension=CorporateContextDimension.EQUITY_TREND,
        secondary_condition_label="UPTREND",
    ),
    _Spec(
        "CONDITIONAL_RELATIONSHIP_V1",
        CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME,
        CorporateFeatureId.OPERATING_MARGIN_CHANGE_YOY,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "Improving operating margin behaves differently when broad equity volatility is elevated "
        "than the unconditional relationship suggests.",
        secondary_context_dimension=CorporateContextDimension.EQUITY_VOLATILITY,
        secondary_condition_label="ELEVATED_VOLATILITY",
    ),
)

_INTERACTION: tuple[_Spec, ...] = (
    _Spec(
        "INTERACTION_V1",
        CorporateHypothesisFamily.INTERACTION_FUNDAMENTAL_MARKET,
        CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "The relationship between leverage and subsequent return changes when the rates proxy is "
        "a headwind for equities (RATE_HEADWIND) versus when it is not.",
        secondary_context_dimension=CorporateContextDimension.BOND_RISK_RELATIONSHIP,
        secondary_condition_label="RATE_HEADWIND",
    ),
    _Spec(
        "INTERACTION_V1",
        CorporateHypothesisFamily.INTERACTION_FUNDAMENTAL_MARKET,
        CorporateFeatureId.CASH_TO_ASSETS,
        CorporateOutcomeId.SUBSEQUENT_RETURN,
        20,
        "The relationship between cash/assets and subsequent return changes when broad-market "
        "risk tone is RISK_OFF versus when it is not.",
        secondary_context_dimension=CorporateContextDimension.EQUITY_RISK_TONE,
        secondary_condition_label="RISK_OFF",
    ),
)

_CROSS_SECTIONAL_STRATUM_FEATURES: tuple[CorporateFeatureId, ...] = (
    CorporateFeatureId.REVENUE_GROWTH_YOY,
    CorporateFeatureId.NET_INCOME_GROWTH_YOY,
    CorporateFeatureId.OPERATING_MARGIN,
    CorporateFeatureId.LEVERAGE_LIABILITIES_TO_ASSETS,
    CorporateFeatureId.CASH_TO_ASSETS,
)


def _negative_control_specs() -> tuple[_Spec, ...]:
    specs: list[_Spec] = []
    for feature in _CROSS_SECTIONAL_STRATUM_FEATURES:
        specs.append(
            _Spec(
                "CROSS_SECTIONAL_V1",
                CorporateHypothesisFamily.NEGATIVE_CONTROL,
                feature,
                CorporateOutcomeId.SUBSEQUENT_RETURN,
                20,
                f"Negative control: {feature.value} evaluated "
                f"{NEGATIVE_CONTROL_DISPLACEMENT_STEPS} "
                "monthly steps earlier than its genuine as_of, paired with the outcome at the "
                "genuine as_of -- a deliberately temporally displaced, non-genuine pairing.",
                is_negative_control=True,
                negative_control_kind="TEMPORALLY_DISPLACED_CORPORATE_FACT",
                control_parameters=(
                    ("displacement_steps", str(NEGATIVE_CONTROL_DISPLACEMENT_STEPS)),
                ),
            )
        )
        specs.append(
            _Spec(
                "CROSS_SECTIONAL_V1",
                CorporateHypothesisFamily.NEGATIVE_CONTROL,
                feature,
                CorporateOutcomeId.SUBSEQUENT_RETURN,
                20,
                f"Negative control: {feature.value} read from a different (rotated) issuer than "
                "the one whose outcome is measured -- an economically nonsensical pairing.",
                is_negative_control=True,
                negative_control_kind="MISMATCHED_ISSUER_FUNDAMENTALS",
                control_parameters=(("rotation", "next_issuer_by_sorted_id"),),
            )
        )
        specs.append(
            _Spec(
                "CROSS_SECTIONAL_V1",
                CorporateHypothesisFamily.NEGATIVE_CONTROL,
                feature,
                CorporateOutcomeId.SUBSEQUENT_RETURN,
                20,
                f"Negative control: {feature.value} values are shuffled across issuers within "
                "each date before being paired with each issuer's own genuine outcome.",
                is_negative_control=True,
                negative_control_kind="RANDOMIZED_ISSUER_ASSIGNMENT",
                control_parameters=(("seed", str(NEGATIVE_CONTROL_SEED)),),
            )
        )
    specs.append(
        _Spec(
            "CROSS_SECTIONAL_V1",
            CorporateHypothesisFamily.NEGATIVE_CONTROL,
            CorporateFeatureId.FILING_LAG_FROM_PERIOD_END_DAYS,
            CorporateOutcomeId.SUBSEQUENT_RETURN,
            20,
            "Negative control: filing lag from fiscal period end is an administrative timing "
            "artifact with no plausible economic transmission channel to subsequent return.",
            is_negative_control=True,
            negative_control_kind="UNRELATED_CORPORATE_CONCEPT_OUTCOME_PAIRING",
        )
    )
    specs.append(
        _Spec(
            "FILING_EVENT_V1",
            CorporateHypothesisFamily.NEGATIVE_CONTROL,
            CorporateFeatureId.DAYS_SINCE_10Q_OR_10K,
            CorporateOutcomeId.SUBSEQUENT_RETURN,
            5,
            "Negative control: random dates with matched calendar characteristics, not genuine "
            "filing-availability events, used as the 'event' sample.",
            benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE,
            is_negative_control=True,
            negative_control_kind="RANDOM_FILING_EVENT_DATE_MATCHED_CALENDAR",
            control_parameters=(("seed", str(NEGATIVE_CONTROL_SEED)),),
        )
    )
    return tuple(specs)


def build_v3_agenda_hypothesis_specs() -> tuple[_Spec, ...]:
    return (
        _CROSS_SECTIONAL_LEVEL
        + _CROSS_SECTIONAL_CHANGE
        + _FILING_EVENT
        + _CONDITIONAL
        + _INTERACTION
        + _negative_control_specs()
    )


def build_v3_agenda_hypotheses(
    agenda: CorporateMarketResearchAgenda, *, created_at: datetime
) -> tuple[GeneratedCorporateHypothesis, ...]:
    hypotheses = []
    for ordinal, spec in enumerate(build_v3_agenda_hypothesis_specs()):
        hypotheses.append(
            GeneratedCorporateHypothesis.create(
                agenda=agenda,
                template_id=spec.template_id,
                template_version="V1",
                family=spec.family,
                feature_id=spec.feature_id,
                condition_rule=(
                    ConditionRule.LABEL_EQUALS
                    if spec.template_id == "FILING_EVENT_V1"
                    else ConditionRule.ABOVE_TRAIN_MEDIAN
                ),
                condition_label=(
                    "FILING_EVENT_OCCURRED" if spec.template_id == "FILING_EVENT_V1" else None
                ),
                secondary_context_dimension=spec.secondary_context_dimension,
                secondary_condition_label=spec.secondary_condition_label,
                outcome_id=spec.outcome_id,
                horizon_sessions=spec.horizon_sessions,
                benchmark_kind=spec.benchmark_kind,
                rationale=spec.rationale,
                proposal_source=ProposalSource.DETERMINISTIC_RULE,
                is_negative_control=spec.is_negative_control,
                negative_control_kind=spec.negative_control_kind,
                control_parameters=spec.control_parameters,
                discovery_status=(
                    DiscoveryStatus.NEGATIVE_CONTROL
                    if spec.is_negative_control
                    else DiscoveryStatus.PREDECLARED
                ),
                ordinal=ordinal,
                created_at=created_at,
            )
        )
    return tuple(hypotheses)


ALLOWED_V3_TEMPLATES: tuple[str, ...] = (
    "CROSS_SECTIONAL_V1",
    "CORPORATE_CHANGE_V1",
    "FILING_EVENT_V1",
    "CONDITIONAL_RELATIONSHIP_V1",
    "INTERACTION_V1",
)
TRAIN_VALIDATION_HOLDOUT_POLICY_TEXT = (
    "Fixed 70/15/15 split of the monthly-cadence corporate-dataset date axis: TRAIN (unlocked, "
    "used only to derive each feature's fixed ABOVE_TRAIN_MEDIAN threshold), VALIDATION "
    "(unlocked, the primary trial and family-correction sample), HOLDOUT (locked, read only "
    "through guard_governed_holdout_access with purpose=EVALUATION_ONLY)."
)
MULTIPLE_TESTING_FAMILY_POLICY_TEXT = (
    "Benjamini-Hochberg FDR at 10%, applied separately within each of the six "
    "CorporateHypothesisFamily partitions (five genuine + one negative-control family) -- never "
    "pooled across families."
)
ROBUSTNESS_REQUIREMENTS: tuple[str, ...] = (
    "ASSET_COHORT_EXCLUSION:leave_one_issuer_out",
    "ASSET_COHORT_EXCLUSION:leave_one_sector_out",
    "LEAVE_ONE_PERIOD_OUT",
    "WINSORIZED_SENSITIVITY",
    "MEDIAN_VS_MEAN_COMPARISON",
)


@dataclass(frozen=True, slots=True)
class RegisterCorporateMarketResearchAgendaCommand:
    corporate_dataset_id: str
    equity_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    cohort_policy_id: str
    lookback_years: int
    registered_at: datetime


class RegisterCorporateMarketResearchAgenda:
    def __init__(self, *, repository: CorporateMarketResearchRepository) -> None:
        self._repository = repository

    def execute(
        self, command: RegisterCorporateMarketResearchAgendaCommand
    ) -> CorporateMarketResearchAgenda:
        agenda = CorporateMarketResearchAgenda.create(
            version="CORPORATE_MARKET_RESEARCH_V3",
            objective=(
                "Determine whether point-in-time corporate fundamental and filing evidence adds "
                "reproducible research value beyond price/market context alone. Controlled "
                "hypothesis research; not strategy generation; not execution; authority NONE."
            ),
            corporate_dataset_id=command.corporate_dataset_id,
            equity_snapshot_id=command.equity_snapshot_id,
            replay_run_id=command.replay_run_id,
            cross_market_dataset_id=command.cross_market_dataset_id,
            cohort_policy_id=command.cohort_policy_id,
            allowed_feature_ids=tuple(CorporateFeatureId),
            allowed_outcome_ids=tuple(CorporateOutcomeId),
            allowed_horizons_sessions=ALLOWED_HORIZONS_SESSIONS,
            allowed_templates=ALLOWED_V3_TEMPLATES,
            maximum_genuine_hypothesis_count=20,
            maximum_hypothesis_count=37,
            maximum_configurations_per_hypothesis=3,
            maximum_total_trials=150,
            train_validation_holdout_policy=TRAIN_VALIDATION_HOLDOUT_POLICY_TEXT,
            multiple_testing_family_policy=MULTIPLE_TESTING_FAMILY_POLICY_TEXT,
            robustness_requirements=ROBUSTNESS_REQUIREMENTS,
            lookback_years=command.lookback_years,
            registered_at=command.registered_at,
        )
        existing = self._repository.agenda_by_id(agenda.agenda_id)
        if existing is not None:
            return existing
        return self._repository.save_agenda(agenda)


# ==========================================================================
# Resumable agenda-run cursor: reuses ``AgendaRun``/``AgendaRunStatus``
# (``domain.cross_market_research``) verbatim -- fully generic, no
# price-only coupling.
# ==========================================================================


class RegisterCorporateAgendaRun:
    def __init__(self, *, repository: CorporateMarketResearchRepository) -> None:
        self._repository = repository

    def execute(self, *, agenda: CorporateMarketResearchAgenda, created_at: datetime) -> AgendaRun:
        existing = self._repository.agenda_run_by_agenda(agenda.agenda_id)
        if existing is not None:
            return existing
        hypotheses = build_v3_agenda_hypotheses(agenda, created_at=created_at)
        for hypothesis in hypotheses:
            saved = self._repository.hypothesis_by_id(hypothesis.hypothesis_id)
            if saved is None:
                self._repository.save_hypothesis(hypothesis)
        # AgendaRun.draft is typed to cross_market_research.ResearchAgenda, but its body
        # reads only `.agenda_id`/`.maximum_hypothesis_count` -- both present here with
        # compatible types (verified against its source), so this reuse is genuinely safe,
        # not merely duck-typed by accident.
        run = AgendaRun.draft(
            agenda=cast(ResearchAgenda, agenda),
            hypothesis_ids=tuple(item.hypothesis_id for item in hypotheses),
            created_at=created_at,
        )
        return self._repository.save_agenda_run(run)


@dataclass(frozen=True, slots=True)
class AdvanceCorporateAgendaRunResult:
    run: AgendaRun
    outcome: HypothesisRunOutcome | None
    quality_report: AgendaQualityReport | None


class AdvanceCorporateAgendaRun:
    """Steps the resumable cursor by exactly one hypothesis per call (or,
    on the final hypothesis, also runs ``FinalizeCorporateAgenda``) --
    mirrors ``cross_market_research.AdvanceAgendaRun``'s own bounded,
    resumable, idempotent step-at-a-time contract."""

    def __init__(
        self,
        *,
        repository: CorporateMarketResearchRepository,
        methodology: ResearchMethodologyCollaborator,
        panel_builder: CorporatePanelBuilderCollaborator,
    ) -> None:
        self._repository = repository
        self._methodology = methodology
        self._panel_builder = panel_builder

    def execute(
        self, *, agenda: CorporateMarketResearchAgenda, created_at: datetime
    ) -> AdvanceCorporateAgendaRunResult:
        run = self._repository.agenda_run_by_agenda(agenda.agenda_id)
        if run is None:
            raise CorporateMarketResearchNotFound("CORPORATE_AGENDA_RUN_NOT_REGISTERED")
        if run.status is AgendaRunStatus.COMPLETE:
            report = self._repository.quality_report_by_agenda(agenda.agenda_id)
            return AdvanceCorporateAgendaRunResult(run=run, outcome=None, quality_report=report)

        panel = self._panel_builder.execute(
            corporate_dataset_id=agenda.corporate_dataset_id,
            equity_snapshot_id=agenda.equity_snapshot_id,
            replay_run_id=agenda.replay_run_id,
            cross_market_dataset_id=agenda.cross_market_dataset_id,
        )
        period_policy = split_train_validation_holdout(
            panel.monthly_dates, replay_run_id=agenda.replay_run_id
        )
        if period_policy is None:
            raise CorporateMarketResearchApplicationError(
                "TRAIN_VALIDATION_HOLDOUT_SPLIT_UNAVAILABLE"
            )
        runner = RunSingleCorporateHypothesis(
            methodology=self._methodology, panel=panel, period_policy=period_policy
        )

        remaining = [
            hid for hid in run.hypothesis_ids if hid not in set(run.completed_hypothesis_ids)
        ]
        if not remaining:
            # Defensive only: the last genuinely remaining hypothesis's own
            # ``advance(..., is_final=True)`` call below already flips
            # ``status`` to COMPLETE in the same save, so the top-of-
            # function COMPLETE short-circuit always catches any later
            # call under normal sequential use -- this branch should be
            # unreachable in practice. It intentionally never calls
            # ``run.advance()`` again (that would re-append the already-
            # completed final hypothesis id and overcount ``cursor_
            # ordinal``); it just returns the run's current, already-
            # accurate state plus a freshly (re)computed report.
            report = self._finalize(agenda, panel=panel, created_at=created_at)
            return AdvanceCorporateAgendaRunResult(run=run, outcome=None, quality_report=report)

        next_id = remaining[0]
        hypothesis = self._repository.hypothesis_by_id(next_id)
        if hypothesis is None:
            raise CorporateMarketResearchNotFound("CORPORATE_HYPOTHESIS_NOT_FOUND")
        outcome = runner.execute(hypothesis, created_at=created_at)
        trials_added = sum(
            1
            for trial in (
                outcome.exploratory_trial,
                outcome.validation_trial,
                outcome.holdout_trial,
            )
            if trial is not None
        )
        is_final = len(remaining) == 1
        updated = run.advance(
            hypothesis_id=next_id, trials_added=trials_added, is_final=is_final, at=created_at
        )
        saved_run = self._repository.save_agenda_run(updated)
        final_report: AgendaQualityReport | None = None
        if is_final:
            final_report = self._finalize(agenda, panel=panel, created_at=created_at)
        return AdvanceCorporateAgendaRunResult(
            run=saved_run, outcome=outcome, quality_report=final_report
        )

    def _finalize(
        self, agenda: CorporateMarketResearchAgenda, *, panel: CorporatePanel, created_at: datetime
    ) -> AgendaQualityReport:
        period_policy = split_train_validation_holdout(
            panel.monthly_dates, replay_run_id=agenda.replay_run_id
        )
        assert period_policy is not None
        runner = RunSingleCorporateHypothesis(
            methodology=self._methodology, panel=panel, period_policy=period_policy
        )
        hypotheses = self._repository.hypotheses_for_agenda(agenda.agenda_id)
        outcomes = [runner.execute(item, created_at=created_at) for item in hypotheses]
        finalizer = FinalizeCorporateAgenda(repository=self._repository)
        return finalizer.execute(
            agenda=agenda,
            outcomes=outcomes,
            panel=panel,
            methodology=self._methodology,
            created_at=created_at,
        )


# ==========================================================================
# Section 8: incremental-value evidence for conditional/interaction
# hypotheses -- market-context-only baseline, corporate-feature-only, and
# combined, retained side by side (never averaged into one score).
# ==========================================================================


def compute_incremental_contribution(
    hypothesis: GeneratedCorporateHypothesis,
    *,
    panel: CorporatePanel,
    period_policy: ResearchPeriodPolicy,
) -> IncrementalContributionResult | None:
    if hypothesis.secondary_context_dimension is None:
        return None
    dimension = hypothesis.secondary_context_dimension
    label = hypothesis.secondary_condition_label
    validation_period = next(
        item for item in period_policy.periods if item.role is ResearchPeriodRole.VALIDATION
    )
    dates = _dates_in_range(
        panel.monthly_dates, start=validation_period.start_date, end=validation_period.end_date
    )
    train_period = next(
        item for item in period_policy.periods if item.role is ResearchPeriodRole.TRAIN
    )
    train_dates = _dates_in_range(
        panel.monthly_dates, start=train_period.start_date, end=train_period.end_date
    )
    threshold = _train_median_threshold(panel, dates=train_dates, feature_id=hypothesis.feature_id)
    if threshold is None:
        return None

    regime_match: list[Decimal] = []
    regime_other: list[Decimal] = []
    high_group: list[Decimal] = []
    low_group: list[Decimal] = []
    for security in panel.usable_security_ids:
        for date in dates:
            outcome_value = _outcome_value(
                panel,
                security=security,
                date=date,
                outcome_id=hypothesis.outcome_id,
                horizon_sessions=hypothesis.horizon_sessions,
            )
            if outcome_value is None:
                continue
            if panel.context_label(dimension, date) == label:
                regime_match.append(outcome_value)
            else:
                regime_other.append(outcome_value)
            feature_value = _feature_value(
                panel, security=security, date=date, feature_id=hypothesis.feature_id
            )
            if feature_value is None:
                continue
            (high_group if feature_value >= threshold else low_group).append(outcome_value)

    market_only_effect = None
    if regime_match and regime_other:
        market_mean_match, market_mean_other = _mean_of(regime_match), _mean_of(regime_other)
        if market_mean_match is not None and market_mean_other is not None:
            market_only_effect = bound_statistical_decimal(market_mean_match - market_mean_other)

    corporate_only_effect = None
    if high_group and low_group:
        high_mean, low_mean = _mean_of(high_group), _mean_of(low_group)
        if high_mean is not None and low_mean is not None:
            corporate_only_effect = bound_statistical_decimal(high_mean - low_mean)

    combined_sample = build_hypothesis_sample(
        hypothesis, panel=panel, period_policy=period_policy, role=ResearchPeriodRole.VALIDATION
    )
    combined_effect = None
    combined_count = 0
    if combined_sample is not None and combined_sample.treatment and combined_sample.benchmark:
        combined_count = len(combined_sample.treatment)
        treatment_mean = _mean_of(tuple(row.value for row in combined_sample.treatment))
        benchmark_mean = _mean_of(tuple(row.value for row in combined_sample.benchmark))
        if treatment_mean is not None and benchmark_mean is not None:
            combined_effect = bound_statistical_decimal(treatment_mean - benchmark_mean)

    return IncrementalContributionResult.create(
        hypothesis_id=hypothesis.hypothesis_id,
        feature_id=hypothesis.feature_id,
        context_dimension=dimension,
        market_only_effect=market_only_effect,
        corporate_only_effect=corporate_only_effect,
        combined_effect=combined_effect,
        market_only_sample=len(regime_match) + len(regime_other),
        corporate_only_sample=len(high_group) + len(low_group),
        combined_sample=combined_count,
    )


# ==========================================================================
# Section 22: bounded corporate contribution accounting seam.
# ==========================================================================


def compute_contribution_evidence(
    outcome: HypothesisRunOutcome,
    *,
    panel: CorporatePanel,
    incremental: IncrementalContributionResult | None,
) -> CorporateContributionEvidence:
    standalone = (
        outcome.validation_trial.diagnostics.effect_size.cohens_d
        if outcome.validation_trial is not None and outcome.validation_trial.diagnostics.effect_size
        else None
    )
    covered_rows = 0
    total_possible = 0
    missingness_burden = 0
    for security in panel.usable_security_ids:
        for observation in panel.features_by_security.get(security, ()):
            total_possible += 1
            if observation.feature(outcome.hypothesis.feature_id) is not None:
                covered_rows += 1
            elif outcome.hypothesis.feature_id.value in observation.missing_fields:
                missingness_burden += 1
    coverage = (
        bound_statistical_decimal(Decimal(covered_rows) / Decimal(total_possible))
        if total_possible > 0
        else None
    )
    redundant_with: tuple[tuple[CorporateFeatureId, Decimal], ...] = ()
    if outcome.hypothesis.feature_id is CorporateFeatureId.REVENUE_GROWTH_YOY:
        redundant_with = _redundancy_against(
            outcome.hypothesis.feature_id,
            CorporateFeatureId.NET_INCOME_GROWTH_YOY,
            panel=panel,
            dates=panel.monthly_dates,
        )
    notes = (
        "STANDALONE_AND_INCREMENTAL_EFFECTS_ARE_NOT_INDEPENDENT_CONFIRMATIONS_WHEN_"
        "FEATURES_CORRELATE",
    )
    return CorporateContributionEvidence.create(
        hypothesis_id=outcome.hypothesis.hypothesis_id,
        feature_id=outcome.hypothesis.feature_id,
        standalone_effect_size=standalone,
        incremental_result_id=(
            deterministic_id(
                "corporate_market_incremental_contribution",
                (outcome.hypothesis.hypothesis_id, str(incremental.combined_effect)),
            )
            if incremental is not None
            else None
        ),
        redundant_with=redundant_with,
        coverage_fraction=coverage,
        missingness_burden_count=missingness_burden,
        notes=notes,
    )


def _redundancy_against(
    feature_a: CorporateFeatureId,
    feature_b: CorporateFeatureId,
    *,
    panel: CorporatePanel,
    dates: Sequence[str],
) -> tuple[tuple[CorporateFeatureId, Decimal], ...]:
    pairs: list[tuple[Decimal, Decimal]] = []
    for security in panel.usable_security_ids:
        for date in dates:
            value_a = _feature_value(panel, security=security, date=date, feature_id=feature_a)
            value_b = _feature_value(panel, security=security, date=date, feature_id=feature_b)
            if value_a is not None and value_b is not None:
                pairs.append((value_a, value_b))
    correlation = pearson_correlation(pairs)
    return ((feature_b, correlation),) if correlation is not None else ()


# ==========================================================================
# Section 10: template-matched calibration. Reuses ``research_calibration_
# v2.ResearchDesign``/``ConditioningShape``/``MatchedCalibrationResult``
# and ``research_calibration.EvidenceProfile``/``derive_evidence_grade``
# unmodified -- computed on demand from already-persisted trial evidence
# (genuine and negative-control), never as its own durable table (every
# input is already retained; recomputing this comparison is cheap).
# ==========================================================================

_TEMPLATE_TO_DESIGN: dict[str, ResearchDesign] = {
    "CROSS_SECTIONAL_V1": ResearchDesign.CROSS_SECTIONAL,
    "CORPORATE_CHANGE_V1": ResearchDesign.CHANGE_BASED,
    "FILING_EVENT_V1": ResearchDesign.FILING_EVENT,
    "CONDITIONAL_RELATIONSHIP_V1": ResearchDesign.CONDITIONAL,
    "INTERACTION_V1": ResearchDesign.INTERACTION,
}
_TEMPLATE_TO_CONDITIONING_SHAPE: dict[str, ConditioningShape] = {
    "CROSS_SECTIONAL_V1": ConditioningShape.NUMERIC_SPLIT,
    "CORPORATE_CHANGE_V1": ConditioningShape.NUMERIC_SPLIT,
    "FILING_EVENT_V1": ConditioningShape.UNCONDITIONAL,
    "CONDITIONAL_RELATIONSHIP_V1": ConditioningShape.LABEL_SPLIT,
    "INTERACTION_V1": ConditioningShape.NUMERIC_INTERACTION,
}


def corporate_matched_stratum_key(hypothesis: GeneratedCorporateHypothesis) -> str:
    design = _TEMPLATE_TO_DESIGN[hypothesis.template_id]
    shape = _TEMPLATE_TO_CONDITIONING_SHAPE[hypothesis.template_id]
    return (
        f"{design.value}|{hypothesis.outcome_id.value}|H{hypothesis.horizon_sessions}|{shape.value}"
    )


def _cohens_d(trial: ResearchTrial | None) -> Decimal | None:
    if trial is None or trial.diagnostics.effect_size is None:
        return None
    return trial.diagnostics.effect_size.cohens_d


def classify_hypothesis_calibration(
    outcomes: Sequence[HypothesisRunOutcome],
    families: Mapping[CorporateHypothesisFamily, ResearchTrialFamily],
    *,
    agenda_id: str,
    created_at: datetime,
) -> tuple[DesignClassification, ...]:
    controls_by_stratum: dict[str, list[Decimal]] = {}
    for item in outcomes:
        if not item.hypothesis.is_negative_control:
            continue
        stat = _cohens_d(item.validation_trial)
        if stat is not None:
            stratum_key = corporate_matched_stratum_key(item.hypothesis)
            controls_by_stratum.setdefault(stratum_key, []).append(stat)
    all_controls = [
        stat
        for item in outcomes
        if item.hypothesis.is_negative_control
        and (stat := _cohens_d(item.validation_trial)) is not None
    ]

    classifications: list[DesignClassification] = []
    for item in outcomes:
        if item.hypothesis.is_negative_control:
            continue
        stratum = corporate_matched_stratum_key(item.hypothesis)
        statistic = _cohens_d(item.validation_trial)
        matched = MatchedCalibrationResult.evaluate(
            statistic=statistic,
            matched_null_statistics=tuple(controls_by_stratum.get(stratum, ())),
            pooled_null_statistics=tuple(all_controls),
            matched_stratum=stratum,
        )
        trial_family = families.get(item.hypothesis.family)
        validation_trial_id = item.validation_trial.trial_id if item.validation_trial else None
        corrected = _corrected_significance(trial_family, trial_id=validation_trial_id)
        raw_p = trial_p_value(item.validation_trial) if item.validation_trial else None
        raw_significant = raw_p is not None and raw_p <= Decimal("0.05")
        # Cohen's own conventional "small effect" threshold (0.20), the identical default
        # every apex.domain.research_calibration.EffectSizeFloorPolicy family floor falls
        # back to -- used directly since CorporateOutcomeId is not an OutcomeFamily member.
        effect_meets_floor = statistic is not None and abs(statistic) >= Decimal("0.20")
        conclusion_state = _outcome_conclusion_state(item)
        profile = EvidenceProfile.create(
            subject_id=item.hypothesis.hypothesis_id,
            subject_kind="HYPOTHESIS",
            benchmark_relative_effect=(
                item.validation_trial.benchmark.relative_result
                if item.validation_trial is not None and item.validation_trial.benchmark
                else None
            ),
            cohens_d=statistic,
            validation_beats_benchmark=_beats_benchmark(item.validation_trial),
            holdout_result=(
                "BEATS_BENCHMARK"
                if _beats_benchmark(item.holdout_trial)
                else "DOES_NOT_BEAT_OR_UNAVAILABLE"
            ),
            walk_forward_consistency=None,
            robustness_passed=(item.robustness_run.summary.passed if item.robustness_run else None),
            corrected_significant=corrected,
            effective_sample=(
                item.validation_trial.diagnostics.overlap.effective_sample_count
                if item.validation_trial is not None
                else Decimal(0)
            ),
            regime_concentration=(
                item.stability.state.value if item.stability else "INSUFFICIENT_EVIDENCE"
            ),
            null_percentile=matched.matched_percentile,
            practical_significance=classify_practical_significance(
                statistically_significant=raw_significant, meets_effect_floor=effect_meets_floor
            ),
            created_at=created_at,
        )
        grade = derive_evidence_grade(
            conclusion_state=conclusion_state,
            profile=profile,
            effect_meets_floor=effect_meets_floor,
        )
        classifications.append(
            DesignClassification.create(
                calibration_id=f"corporate_market_calibration:{agenda_id}",
                subject_id=item.hypothesis.hypothesis_id,
                subject_kind="HYPOTHESIS",
                research_design=_TEMPLATE_TO_DESIGN[item.hypothesis.template_id],
                conditioning_shape=_TEMPLATE_TO_CONDITIONING_SHAPE[item.hypothesis.template_id],
                matched=matched,
                raw_significant=raw_significant,
                corrected_significant=corrected,
                cohens_d=statistic,
                evidence_grade_v2=grade,
                created_at=created_at,
            )
        )
    return tuple(classifications)


def _outcome_conclusion_state(outcome: HypothesisRunOutcome) -> ResearchConclusionState:
    requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.VALIDATION)
    effective = (
        outcome.validation_trial.diagnostics.overlap.effective_sample_count
        if outcome.validation_trial is not None
        else Decimal(0)
    )
    sample_quality = (
        SampleQualityState.SUFFICIENT
        if outcome.validation_trial is not None
        and outcome.validation_trial.diagnostics.overlap.inference_available
        and effective >= requirement.minimum_effective_sample
        else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
    )
    return derive_conclusion_state(
        sample_quality=sample_quality,
        robustness_summary=outcome.robustness_run.summary if outcome.robustness_run else None,
        validation_beats_benchmark=_beats_benchmark(outcome.validation_trial),
        holdout_beats_benchmark=_beats_benchmark(outcome.holdout_trial),
        multiple_testing_corrected_significant=None,
    )


# ==========================================================================
# Section 23/24: forward options seam. Structural readiness is proven by
# genuinely reading whatever forward-evidence checkpoint the options-
# research-operations repository retains; when zero genuine forward
# sessions exist (the honest state as of this milestone), the seam
# reports ``OPTIONS_SEAM_FACTUALLY_UNAVAILABLE`` rather than fabricating
# descriptive values. Never reads or writes a historical frame.
# ==========================================================================


def describe_options_forward_seam(
    *, reader: OptionsForwardCollaborator | None, plan_id: str | None
) -> OptionsForwardSeamSnapshot:
    if reader is None or plan_id is None:
        return OptionsForwardSeamSnapshot.unavailable(
            reason="NO_OPTIONS_FORWARD_PLAN_OR_REPOSITORY_SUPPLIED"
        )
    checkpoint = reader.latest_checkpoint(plan_id)
    if checkpoint is None:
        return OptionsForwardSeamSnapshot.unavailable(
            reason="ZERO_GENUINE_FORWARD_OPTIONS_CHECKPOINTS_RETAINED"
        )
    captured_symbols = tuple(getattr(checkpoint, "underlying_symbols_captured", ()) or ())
    if not captured_symbols:
        return OptionsForwardSeamSnapshot.unavailable(
            reason="LATEST_CHECKPOINT_CAPTURED_ZERO_UNDERLYINGS_MISSED_CAPTURE_WINDOW_OR_SIMILAR"
        )
    created_at = getattr(checkpoint, "created_at", None)
    # Structurally ready and factually available: the seam ingests exactly the already-
    # retained descriptive fields (domain.options_structures.VolatilityContext's atm_iv_
    # nearest_expiration/skew_state/term_structure_shape/expected_move_percent_*, and IV/RV
    # via that context's own atm_iv against genuine realized volatility) for each captured
    # underlying -- per-underlying enrichment is intentionally left to the caller, which
    # already holds the concrete snapshot/structure repository this narrow Protocol does not
    # widen into; this function's own job is only the availability gate above.
    return OptionsForwardSeamSnapshot(
        availability=OPTIONS_SEAM_STRUCTURALLY_READY,
        as_of=created_at,
        underlying_symbols=captured_symbols,
        atm_iv_by_underlying=(),
        skew_state_by_underlying=(),
        term_structure_shape_by_underlying=(),
        expected_move_percent_by_underlying=(),
        iv_to_realized_volatility_by_underlying=(),
        limitations=(
            OPTIONS_SEAM_CURRENT_DESCRIPTIVE_ONLY,
            "PER_UNDERLYING_ENRICHMENT_REQUIRES_THE_CALLERS_OWN_OPTIONS_STRUCTURES_REPOSITORY",
        ),
    )


# ==========================================================================
# In-memory repository (tests, and any bounded offline CLI usage) --
# mirrors ``cross_market_research.InMemoryCrossMarketResearchRepository``.
# ==========================================================================


class InMemoryCorporateMarketResearchRepository:
    def __init__(self) -> None:
        self._agendas: dict[str, CorporateMarketResearchAgenda] = {}
        self._hypotheses: dict[str, GeneratedCorporateHypothesis] = {}
        self._agenda_runs: dict[str, AgendaRun] = {}
        self._executions: dict[str, HypothesisExecutionRecord] = {}
        self._knowledge_entries: dict[str, ResearchKnowledgeEntry] = {}
        self._contradictions: dict[str, ResearchContradiction] = {}
        self._follow_ups: dict[str, FollowUpProposal] = {}
        self._quality_reports: dict[str, AgendaQualityReport] = {}

    def agenda_by_id(self, agenda_id: str) -> CorporateMarketResearchAgenda | None:
        return self._agendas.get(agenda_id)

    def save_agenda(self, agenda: CorporateMarketResearchAgenda) -> CorporateMarketResearchAgenda:
        self._agendas[agenda.agenda_id] = agenda
        return agenda

    def list_agendas(self) -> tuple[CorporateMarketResearchAgenda, ...]:
        return tuple(sorted(self._agendas.values(), key=lambda item: item.registered_at))

    def hypothesis_by_id(self, hypothesis_id: str) -> GeneratedCorporateHypothesis | None:
        return self._hypotheses.get(hypothesis_id)

    def save_hypothesis(
        self, hypothesis: GeneratedCorporateHypothesis
    ) -> GeneratedCorporateHypothesis:
        self._hypotheses[hypothesis.hypothesis_id] = hypothesis
        return hypothesis

    def hypotheses_for_agenda(self, agenda_id: str) -> tuple[GeneratedCorporateHypothesis, ...]:
        return tuple(
            sorted(
                (item for item in self._hypotheses.values() if item.agenda_id == agenda_id),
                key=lambda item: item.ordinal,
            )
        )

    def agenda_run_by_agenda(self, agenda_id: str) -> AgendaRun | None:
        for run in self._agenda_runs.values():
            if run.agenda_id == agenda_id:
                return run
        return None

    def agenda_run_by_id(self, agenda_run_id: str) -> AgendaRun | None:
        return self._agenda_runs.get(agenda_run_id)

    def save_agenda_run(self, run: AgendaRun) -> AgendaRun:
        self._agenda_runs[run.agenda_run_id] = run
        return run

    def execution_by_hypothesis(self, hypothesis_id: str) -> HypothesisExecutionRecord | None:
        return self._executions.get(hypothesis_id)

    def save_execution(self, record: HypothesisExecutionRecord) -> HypothesisExecutionRecord:
        self._executions.setdefault(record.hypothesis_id, record)
        return self._executions[record.hypothesis_id]

    def executions_for_agenda(self, agenda_id: str) -> tuple[HypothesisExecutionRecord, ...]:
        return tuple(
            sorted(
                (item for item in self._executions.values() if item.agenda_id == agenda_id),
                key=lambda item: item.created_at,
            )
        )

    def knowledge_entries_for_hypothesis(
        self, hypothesis_id: str
    ) -> tuple[ResearchKnowledgeEntry, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._knowledge_entries.values()
                    if item.hypothesis_id == hypothesis_id
                ),
                key=lambda item: item.revision,
            )
        )

    def save_knowledge_entry(self, entry: ResearchKnowledgeEntry) -> ResearchKnowledgeEntry:
        self._knowledge_entries.setdefault(entry.entry_id, entry)
        return self._knowledge_entries[entry.entry_id]

    def list_knowledge_entries(self) -> tuple[ResearchKnowledgeEntry, ...]:
        return tuple(sorted(self._knowledge_entries.values(), key=lambda item: item.created_at))

    def save_contradiction(self, item: ResearchContradiction) -> ResearchContradiction:
        self._contradictions.setdefault(item.contradiction_id, item)
        return self._contradictions[item.contradiction_id]

    def list_contradictions(self) -> tuple[ResearchContradiction, ...]:
        return tuple(sorted(self._contradictions.values(), key=lambda item: item.created_at))

    def save_follow_up(self, item: FollowUpProposal) -> FollowUpProposal:
        self._follow_ups.setdefault(item.follow_up_id, item)
        return self._follow_ups[item.follow_up_id]

    def list_follow_ups(self) -> tuple[FollowUpProposal, ...]:
        return tuple(sorted(self._follow_ups.values(), key=lambda item: item.created_at))

    def quality_report_by_agenda(self, agenda_id: str) -> AgendaQualityReport | None:
        for report in self._quality_reports.values():
            if report.agenda_id == agenda_id:
                return report
        return None

    def save_quality_report(self, report: AgendaQualityReport) -> AgendaQualityReport:
        self._quality_reports.setdefault(report.report_id, report)
        return self._quality_reports[report.report_id]
