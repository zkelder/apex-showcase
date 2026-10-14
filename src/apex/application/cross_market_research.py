"""Adapter-free orchestration for controlled cross-market hypothesis
research (Cross-Market Feature, Regime, and Controlled Hypothesis
Research v1).

Composes, entirely unmodified:

* ``application.historical_market_data`` (``HistoricalMarketDataRepository``)
  and ``application.real_securities_data`` (``SecuritiesEvidencePort``) for
  the retained equity/ETF cohort's genuine daily bars -- the identical
  ports ``application.genuine_multi_asset_history`` already uses.
* ``application.genuine_multi_asset_history`` (``GenuineMultiAssetHistoryRepository``)
  for the retained ``CrossMarketDataset`` binding.
* A narrow crypto-candle port satisfied directly by
  ``application.crypto_research.CryptoResearchRepository`` for genuine BTC
  daily candles.
* ``application.research_methodology`` (governance policy, grouped-trial
  recording, trial-family multiple-testing correction, conclusion, and
  promotion services) for the *entire* governed trial lifecycle -- this
  module never re-implements holdout governance, overlap accounting,
  benchmark comparison, or the conclusion/promotion taxonomy.

Everything genuinely new lives in ``domain.cross_market_research``: the
feature/regime library, the five hypothesis templates, negative-control
construction, the knowledge base, contradictions, and follow-ups. This
module only builds the feature panel once, generates the bounded v1
agenda's hypotheses, and drives each one through the reused methodology
services above.

No service here has Strategy, Opportunity, PAPER, LIVE, or provider-write
authority; every retained record stays ``authority = "NONE"``.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from datetime import date as _date
from decimal import Decimal, localcontext
from typing import Protocol

from apex.application.research_methodology import (
    BuildConclusionCommand,
    BuildResearchConclusion,
    BuildResearchTrialFamily,
    BuildTrialFamilyCommand,
    DeclareGovernancePolicyCommand,
    DeclareResearchGovernancePolicy,
    EvaluatePromotion,
    EvaluatePromotionCommand,
    MethodologyConflict,
    MethodologyNotFound,
    RecordGroupedResearchTrialCommand,
    RecordHoldoutExposure,
    RecordHoldoutExposureCommand,
    ResearchMethodologyApplicationError,
    ResearchMethodologyRepository,
)
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.cross_market_research import (
    REGIME_CONDITION_FEATURE_IDS,
    AgendaQualityReport,
    AgendaRun,
    AgendaRunStatus,
    ConditionRule,
    CrossMarketObservation,
    CrossMarketOutcomeSet,
    CrossMarketRegimeDimension,
    CrossMarketRegimeSnapshot,
    CrossMarketResearchError,
    DatedValue,
    DiscoveryStatus,
    FeatureFamily,
    FeatureId,
    FollowUpKind,
    FollowUpProposal,
    GeneratedHypothesis,
    HypothesisExecutionRecord,
    HypothesisFamily,
    HypothesisGenerationMethod,
    HypothesisTemplateId,
    KnowledgeStatus,
    MarketId,
    OutcomeFamily,
    OutcomeId,
    ProposalSource,
    ResearchAgenda,
    ResearchContradiction,
    ResearchKnowledgeEntry,
    StabilityReport,
    StabilityState,
    SubperiodResult,
    basket_return,
    build_cross_market_regime_snapshot,
    cross_sectional_dispersion,
    derive_stability_state,
    deterministic_noise_series,
    deterministic_shuffle,
    fraction_positive,
    lag_displace,
    rolling_correlation_series,
    rolling_drawdown_series,
    rolling_returns,
    rolling_volatility_series,
    to_dated_closes,
)
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalDatasetSnapshot,
    observation_view_from_crypto_daily_candle,
    observation_view_from_securities_bar,
)
from apex.domain.historical_replay import (
    ResearchMethodId,
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
    SummaryKind,
    summarize_values,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1, CohortRole
from apex.domain.research_methodology import (
    IID_SE_METHOD_ID,
    BenchmarkKind,
    ConfigurationOrigin,
    EffectSize,
    FoldWindow,
    GovernanceIntent,
    MultipleTestingCorrectionMethod,
    OverlapDiagnostics,
    OverlapHandlingMode,
    PromotionPolicy,
    ResearchConclusionState,
    ResearchConfiguration,
    ResearchPhase,
    ResearchTrial,
    RobustnessCheckResult,
    RobustnessCheckRun,
    RobustnessCheckType,
    StandardErrorEstimate,
    StatisticalDiagnosticsV2,
    TrainingWindowMode,
    TrialSelectionMethod,
    TrialTestResult,
    bound_statistical_decimal,
    compare_to_benchmark,
    generate_fold_windows,
    newey_west_standard_error,
    robustness_cost_stress,
    seeded_bootstrap_confidence_interval,
    two_sided_z_test_p_value,
)
from apex.domain.securities import SecuritiesBarEvidence
from apex.domain.values import deterministic_id

EQUITY_SESSIONS_PER_YEAR = 252
BTC_SESSIONS_PER_YEAR = 365
CROSS_MARKET_METHOD_ID = ResearchMethodId.CROSS_MARKET_CONTROLLED_HYPOTHESIS_V1
NEGATIVE_CONTROL_SHUFFLE_SEED = 20260807
NEGATIVE_CONTROL_NOISE_SEED = 20260808
NEGATIVE_CONTROL_LAG_SESSIONS = 40
STABILITY_MINIMUM_QUARTER_SAMPLE = 20
MINIMUM_TRIAL_SAMPLE = 8
DEFAULT_BOOTSTRAP_SEED = 726348
ASSISTED_PROPOSALS_ENABLED = False


class CrossMarketResearchApplicationError(RuntimeError):
    pass


class CrossMarketResearchNotFound(CrossMarketResearchApplicationError):
    pass


class CrossMarketResearchConflict(CrossMarketResearchApplicationError):
    pass


def cross_market_research_warnings() -> tuple[str, ...]:
    from apex.domain.cross_market_research import cross_market_research_warnings as _warnings

    return _warnings()


# ==========================================================================
# Repository protocols
# ==========================================================================


class CryptoCandleCollaborator(Protocol):
    """The exact single method this module reads from
    ``application.crypto_research.CryptoResearchRepository`` /
    ``InMemoryCryptoResearchRepository`` -- reused unmodified."""

    def candles(self, capture_id: str) -> tuple[object, ...]: ...


class HistoricalMarketDataCollaborator(Protocol):
    def get_plan(self, plan_id: str) -> HistoricalCapturePlan | None: ...
    def snapshot_by_id(self, snapshot_id: str) -> HistoricalDatasetSnapshot | None: ...


class SecuritiesEvidenceCollaborator(Protocol):
    def get_dataset(
        self, batch_id: str
    ) -> tuple[object, object, tuple[SecuritiesBarEvidence, ...], object]: ...


class GenuineMultiAssetCollaborator(Protocol):
    def cross_market_dataset_by_id(self, dataset_id: str) -> CrossMarketDataset | None: ...


class CrossMarketResearchRepository(Protocol):
    def agenda_by_id(self, agenda_id: str) -> ResearchAgenda | None: ...
    def save_agenda(self, agenda: ResearchAgenda) -> ResearchAgenda: ...
    def list_agendas(self) -> tuple[ResearchAgenda, ...]: ...

    def hypothesis_by_id(self, hypothesis_id: str) -> GeneratedHypothesis | None: ...
    def save_hypothesis(self, hypothesis: GeneratedHypothesis) -> GeneratedHypothesis: ...
    def hypotheses_for_agenda(self, agenda_id: str) -> tuple[GeneratedHypothesis, ...]: ...

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


# ==========================================================================
# Section: feature/outcome panel construction (built once, shared by every
# hypothesis -- section 24: "do not rebuild identical feature frames per
# hypothesis")
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CrossMarketPanel:
    equity_snapshot_id: str
    cross_market_dataset_id: str
    session_dates: tuple[str, ...]
    observations: tuple[CrossMarketObservation, ...]
    outcomes_by_horizon: dict[int, tuple[CrossMarketOutcomeSet, ...]]
    equity_members: tuple[str, ...]
    warnings: tuple[str, ...]

    def feature_by_date(self, feature_id: FeatureId) -> dict[str, Decimal]:
        return {
            obs.session_date: value
            for obs in self.observations
            if (value := obs.feature(feature_id)) is not None
        }

    def outcome_by_date(
        self, outcome_id: OutcomeId, *, horizon_sessions: int
    ) -> dict[str, Decimal]:
        outcomes = self.outcomes_by_horizon.get(horizon_sessions, ())
        return {
            item.session_date: value
            for item in outcomes
            if (value := item.outcome(outcome_id)) is not None
        }

    def regime_by_date(self) -> dict[str, CrossMarketRegimeSnapshot]:
        return {obs.session_date: obs.regime for obs in self.observations}


def _end_of_day(session_date: str) -> datetime:
    return datetime.combine(_date.fromisoformat(session_date), time(23, 59, 59), tzinfo=UTC)


def _cohort_role_index() -> tuple[dict[str, tuple[CohortRole, ...]], frozenset[str]]:
    roles_by_symbol = {
        member.symbol: member.roles for member in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members
    }
    etf_symbols = frozenset(
        member.symbol for member in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members if member.is_etf
    )
    return roles_by_symbol, etf_symbols


class BuildCrossMarketPanel:
    """Reads the retained equity cohort's genuine daily bars and BTC's
    genuine daily candles exactly once, and builds one point-in-time-safe
    ``CrossMarketObservation``/``CrossMarketOutcomeSet`` per shared
    session date. Never re-derives a bar, never captures anything new."""

    def __init__(
        self,
        *,
        historical_repository: HistoricalMarketDataCollaborator,
        evidence: SecuritiesEvidenceCollaborator,
        genuine_repository: GenuineMultiAssetCollaborator,
        crypto_repository: CryptoCandleCollaborator,
    ) -> None:
        self._historical_repository = historical_repository
        self._evidence = evidence
        self._genuine_repository = genuine_repository
        self._crypto_repository = crypto_repository

    def execute(
        self, *, equity_snapshot_id: str, cross_market_dataset_id: str, horizons: tuple[int, ...]
    ) -> CrossMarketPanel:
        snapshot = self._historical_repository.snapshot_by_id(equity_snapshot_id)
        if snapshot is None:
            raise CrossMarketResearchNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        plan = self._historical_repository.get_plan(snapshot.plan_id)
        if plan is None:
            raise CrossMarketResearchNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        dataset = self._genuine_repository.cross_market_dataset_by_id(cross_market_dataset_id)
        if dataset is None:
            raise CrossMarketResearchNotFound("CROSS_MARKET_DATASET_NOT_FOUND")

        bars_by_instrument: dict[str, list[SecuritiesBarEvidence]] = {}
        for batch_id in snapshot.source_batch_ids:
            _instruments, _sessions, bars, _actions = self._evidence.get_dataset(batch_id)
            for bar in bars:
                bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)

        roles_by_symbol, etf_symbols = _cohort_role_index()
        member_series: dict[str, tuple[DatedValue, ...]] = {}
        for member in plan.universe:
            raw_bars = sorted(
                bars_by_instrument.get(member.instrument_id, ()), key=lambda item: item.period_end
            )
            views = tuple(observation_view_from_securities_bar(bar) for bar in raw_bars)
            member_series[member.symbol_at_capture] = to_dated_closes(views)

        candles: list[object] = []
        for capture_id in dataset.crypto_capture_ids:
            candles.extend(self._crypto_repository.candles(capture_id))
        btc_views = sorted(
            (
                observation_view_from_crypto_daily_candle(
                    product_id=candle.product_id,  # type: ignore[attr-defined]
                    bucket_start=candle.bucket_start,  # type: ignore[attr-defined]
                    open=candle.open,  # type: ignore[attr-defined]
                    high=candle.high,  # type: ignore[attr-defined]
                    low=candle.low,  # type: ignore[attr-defined]
                    close=candle.close,  # type: ignore[attr-defined]
                    volume=candle.volume,  # type: ignore[attr-defined]
                    provider=candle.provider,  # type: ignore[attr-defined]
                )
                for candle in candles
            ),
            key=lambda item: item.event_at,
        )
        btc_series = to_dated_closes(btc_views)

        if "SPY" not in member_series or not member_series["SPY"]:
            raise CrossMarketResearchApplicationError("BROAD_EQUITY_BENCHMARK_SPY_NOT_RESOLVED")
        if "TLT" not in member_series or not member_series["TLT"]:
            raise CrossMarketResearchApplicationError("RATES_PROXY_TLT_NOT_RESOLVED")
        if "DBC" not in member_series or not member_series["DBC"]:
            raise CrossMarketResearchApplicationError("COMMODITY_PROXY_DBC_NOT_RESOLVED")
        if not btc_series:
            raise CrossMarketResearchApplicationError("BTC_HISTORY_NOT_RESOLVED")

        overlap_start = dataset.common_overlap_start or snapshot.earliest_observation_date
        overlap_end = dataset.common_overlap_end or snapshot.latest_observation_date
        if overlap_start is None or overlap_end is None:
            raise CrossMarketResearchApplicationError("CROSS_MARKET_OVERLAP_WINDOW_UNAVAILABLE")
        session_dates = tuple(
            item.session_date
            for item in member_series["SPY"]
            if overlap_start <= item.session_date <= overlap_end
        )
        if len(session_dates) < 300:
            raise CrossMarketResearchApplicationError("CROSS_MARKET_PANEL_SESSION_COUNT_TOO_SMALL")

        gap_series_count = 0
        gap_observation_count = 0

        def _aligned(series: dict[str, Decimal]) -> tuple[Decimal, ...]:
            nonlocal gap_series_count, gap_observation_count
            values: list[Decimal] = []
            last: Decimal | None = None
            missing = 0
            for date in session_dates:
                value = series.get(date)
                if value is None:
                    missing += 1
                    value = last
                if value is None:
                    raise CrossMarketResearchApplicationError(
                        "PANEL_SERIES_MISSING_LEADING_OBSERVATIONS"
                    )
                values.append(value)
                last = value
            if missing:
                gap_series_count += 1
                gap_observation_count += missing
            return tuple(values)

        spy_values = _aligned({item.session_date: item.value for item in member_series["SPY"]})
        tlt_values = _aligned({item.session_date: item.value for item in member_series["TLT"]})
        dbc_values = _aligned({item.session_date: item.value for item in member_series["DBC"]})
        btc_by_date = {item.session_date: item.value for item in btc_series}
        btc_values = _aligned(btc_by_date)

        equity_symbols = tuple(sorted(s for s in member_series if s not in etf_symbols))
        sector_symbols = tuple(
            sorted(s for s in member_series if CohortRole.SECTOR_ETF in roles_by_symbol.get(s, ()))
        )

        equity_returns_20: dict[str, tuple[Decimal | None, ...]] = {
            s: rolling_returns(
                _aligned({d.session_date: d.value for d in member_series[s]}), window_sessions=20
            )
            for s in equity_symbols
        }
        equity_returns_1: dict[str, tuple[Decimal | None, ...]] = {
            s: rolling_returns(
                _aligned({d.session_date: d.value for d in member_series[s]}), window_sessions=1
            )
            for s in equity_symbols
        }
        equity_correlation_60: dict[str, tuple[Decimal | None, ...]] = {
            s: rolling_correlation_series(
                _aligned({d.session_date: d.value for d in member_series[s]}),
                spy_values,
                window_sessions=60,
            )
            for s in equity_symbols
        }
        large_returns = {
            s: equity_returns_20[s]
            for s in equity_symbols
            if CohortRole.LARGE_CAP_EQUITY in roles_by_symbol.get(s, ())
        }
        small_returns = {
            s: equity_returns_20[s]
            for s in equity_symbols
            if CohortRole.SMALL_CAP_EQUITY in roles_by_symbol.get(s, ())
        }
        growth_returns = {
            s: equity_returns_20[s]
            for s in equity_symbols
            if CohortRole.GROWTH_TILT in roles_by_symbol.get(s, ())
        }
        value_returns = {
            s: equity_returns_20[s]
            for s in equity_symbols
            if CohortRole.VALUE_TILT in roles_by_symbol.get(s, ())
        }
        cyclical_returns = {
            s: equity_returns_20[s]
            for s in equity_symbols
            if CohortRole.CYCLICAL_TILT in roles_by_symbol.get(s, ())
        }
        defensive_returns = {
            s: equity_returns_20[s]
            for s in equity_symbols
            if CohortRole.DEFENSIVE_TILT in roles_by_symbol.get(s, ())
        }
        sector_returns_20: dict[str, tuple[Decimal | None, ...]] = {
            s: rolling_returns(
                _aligned({d.session_date: d.value for d in member_series[s]}), window_sessions=20
            )
            for s in sector_symbols
        }

        spy_return_1 = rolling_returns(spy_values, window_sessions=1)
        equity_return_5 = rolling_returns(spy_values, window_sessions=5)
        equity_return_20 = rolling_returns(spy_values, window_sessions=20)
        equity_return_60 = rolling_returns(spy_values, window_sessions=60)
        equity_return_90 = rolling_returns(spy_values, window_sessions=90)
        equity_return_252 = rolling_returns(spy_values, window_sessions=252)
        equity_volatility_20 = rolling_volatility_series(
            spy_values, window_sessions=20, sessions_per_year=EQUITY_SESSIONS_PER_YEAR
        )
        equity_drawdown = rolling_drawdown_series(spy_values)
        rates_return_20 = rolling_returns(tlt_values, window_sessions=20)
        rates_equity_correlation_60 = rolling_correlation_series(
            tlt_values, spy_values, window_sessions=60
        )
        commodity_return_20 = rolling_returns(dbc_values, window_sessions=20)
        commodity_equity_correlation_60 = rolling_correlation_series(
            dbc_values, spy_values, window_sessions=60
        )
        btc_return_5 = rolling_returns(btc_values, window_sessions=5)
        btc_return_20 = rolling_returns(btc_values, window_sessions=20)
        btc_return_60 = rolling_returns(btc_values, window_sessions=60)
        btc_return_90 = rolling_returns(btc_values, window_sessions=90)
        btc_volatility_20 = rolling_volatility_series(
            btc_values, window_sessions=20, sessions_per_year=BTC_SESSIONS_PER_YEAR
        )
        btc_drawdown = rolling_drawdown_series(btc_values)
        btc_equity_correlation_60 = rolling_correlation_series(
            btc_values, spy_values, window_sessions=60
        )

        breadth_base: list[Decimal | None] = []
        dispersion_base: list[Decimal | None] = []
        avg_correlation_base: list[Decimal | None] = []
        large_minus_small: list[Decimal | None] = []
        growth_minus_value: list[Decimal | None] = []
        cyclical_minus_defensive: list[Decimal | None] = []
        sector_breadth: list[Decimal | None] = []
        sector_dispersion: list[Decimal | None] = []
        sign_series: list[Decimal] = []

        for index in range(len(session_dates)):
            one_session = [
                equity_returns_1[s][index]
                for s in equity_symbols
                if equity_returns_1[s][index] is not None
            ]
            breadth_base.append(fraction_positive([v for v in one_session if v is not None]))
            dispersion_values = [
                equity_returns_20[s][index]
                for s in equity_symbols
                if equity_returns_20[s][index] is not None
            ]
            dispersion_base.append(
                cross_sectional_dispersion([v for v in dispersion_values if v is not None])
            )
            correlations = [
                equity_correlation_60[s][index]
                for s in equity_symbols
                if equity_correlation_60[s][index] is not None
            ]
            avg_correlation_base.append(
                None
                if not correlations
                else bound_statistical_decimal(
                    sum((v for v in correlations if v is not None), Decimal(0))
                    / Decimal(len([v for v in correlations if v is not None]))
                )
            )
            large = basket_return(
                [(s, member_series[s]) for s in large_returns],
                as_of_date=session_dates[index],
                window_sessions=20,
            )
            small = basket_return(
                [(s, member_series[s]) for s in small_returns],
                as_of_date=session_dates[index],
                window_sessions=20,
            )
            large_minus_small.append(None if large is None or small is None else large - small)
            growth = basket_return(
                [(s, member_series[s]) for s in growth_returns],
                as_of_date=session_dates[index],
                window_sessions=20,
            )
            value = basket_return(
                [(s, member_series[s]) for s in value_returns],
                as_of_date=session_dates[index],
                window_sessions=20,
            )
            growth_minus_value.append(None if growth is None or value is None else growth - value)
            cyclical = basket_return(
                [(s, member_series[s]) for s in cyclical_returns],
                as_of_date=session_dates[index],
                window_sessions=20,
            )
            defensive = basket_return(
                [(s, member_series[s]) for s in defensive_returns],
                as_of_date=session_dates[index],
                window_sessions=20,
            )
            cyclical_minus_defensive.append(
                None if cyclical is None or defensive is None else cyclical - defensive
            )
            sector_values = [
                sector_returns_20[s][index]
                for s in sector_symbols
                if sector_returns_20[s][index] is not None
            ]
            sector_breadth.append(fraction_positive([v for v in sector_values if v is not None]))
            sector_dispersion.append(
                cross_sectional_dispersion([v for v in sector_values if v is not None])
            )
            spy_daily = spy_return_1[index]
            sign_series.append(
                Decimal(1) if spy_daily is not None and spy_daily > 0 else Decimal(-1)
            )

        base_features: dict[FeatureId, tuple[Decimal | None, ...]] = {
            FeatureId.EQUITY_RETURN_5: equity_return_5,
            FeatureId.EQUITY_RETURN_20: equity_return_20,
            FeatureId.EQUITY_RETURN_60: equity_return_60,
            FeatureId.EQUITY_RETURN_90: equity_return_90,
            FeatureId.EQUITY_RETURN_252: equity_return_252,
            FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING: tuple(breadth_base),
            FeatureId.EQUITY_DISPERSION_20: tuple(dispersion_base),
            FeatureId.EQUITY_VOLATILITY_20: equity_volatility_20,
            FeatureId.EQUITY_DRAWDOWN: equity_drawdown,
            FeatureId.EQUITY_AVERAGE_CORRELATION_60: tuple(avg_correlation_base),
            FeatureId.EQUITY_LARGE_MINUS_SMALL_20: tuple(large_minus_small),
            FeatureId.EQUITY_GROWTH_MINUS_VALUE_20: tuple(growth_minus_value),
            FeatureId.EQUITY_CYCLICAL_MINUS_DEFENSIVE_20: tuple(cyclical_minus_defensive),
            FeatureId.EQUITY_SECTOR_BREADTH_20: tuple(sector_breadth),
            FeatureId.EQUITY_SECTOR_DISPERSION_20: tuple(sector_dispersion),
            FeatureId.RATES_PROXY_RETURN_20: rates_return_20,
            FeatureId.RATES_EQUITY_CORRELATION_60: rates_equity_correlation_60,
            FeatureId.COMMODITY_PROXY_RETURN_20: commodity_return_20,
            FeatureId.COMMODITY_EQUITY_CORRELATION_60: commodity_equity_correlation_60,
            FeatureId.BTC_RETURN_5: btc_return_5,
            FeatureId.BTC_RETURN_20: btc_return_20,
            FeatureId.BTC_RETURN_60: btc_return_60,
            FeatureId.BTC_RETURN_90: btc_return_90,
            FeatureId.BTC_VOLATILITY_20: btc_volatility_20,
            FeatureId.BTC_DRAWDOWN: btc_drawdown,
            FeatureId.BTC_EQUITY_CORRELATION_60: btc_equity_correlation_60,
            FeatureId.EQUITY_DAILY_RETURN_SIGN: tuple(sign_series),
            FeatureId.ALWAYS_PRESENT_BASELINE: tuple(Decimal(1) for _ in session_dates),
        }

        breadth_dated = tuple(
            DatedValue(session_dates[i], v) for i, v in enumerate(breadth_base) if v is not None
        )
        shuffled = {
            item.session_date: item.value
            for item in deterministic_shuffle(breadth_dated, seed=NEGATIVE_CONTROL_SHUFFLE_SEED)
        }
        lag_displaced = {
            item.session_date: item.value
            for item in lag_displace(breadth_dated, sessions=NEGATIVE_CONTROL_LAG_SESSIONS)
        }
        noise = {
            item.session_date: item.value
            for item in deterministic_noise_series(session_dates, seed=NEGATIVE_CONTROL_NOISE_SEED)
        }
        base_features[FeatureId.SHUFFLED_EQUITY_BREADTH] = tuple(
            shuffled.get(date) for date in session_dates
        )
        base_features[FeatureId.LAG_DISPLACED_EQUITY_BREADTH] = tuple(
            lag_displaced.get(date) for date in session_dates
        )
        base_features[FeatureId.RANDOM_NOISE_BASELINE] = tuple(
            noise.get(date) for date in session_dates
        )

        observations: list[CrossMarketObservation] = []
        for index, session_date in enumerate(session_dates):
            regime = build_cross_market_regime_snapshot(
                equity_return_20=equity_return_20[index],
                equity_fraction_advancing=breadth_base[index],
                equity_volatility_20=equity_volatility_20[index],
                equity_dispersion_20=dispersion_base[index],
                equity_average_correlation_60=avg_correlation_base[index],
                btc_return_20=btc_return_20[index],
                btc_volatility_20=btc_volatility_20[index],
                rates_proxy_return_20=rates_return_20[index],
                rates_equity_correlation_60=rates_equity_correlation_60[index],
                commodity_proxy_return_20=commodity_return_20[index],
            )
            features = tuple(
                (feature_id, series[index]) for feature_id, series in base_features.items()
            )
            observations.append(
                CrossMarketObservation(
                    session_date=session_date,
                    as_of=_end_of_day(session_date),
                    features=features,
                    regime=regime,
                    data_quality_flags=(),
                )
            )

        outcomes_by_horizon: dict[int, tuple[CrossMarketOutcomeSet, ...]] = {}
        for horizon in horizons:
            outcomes_by_horizon[horizon] = _build_outcomes(
                session_dates=session_dates,
                horizon_sessions=horizon,
                spy_values=spy_values,
                tlt_values=tlt_values,
                btc_values=btc_values,
                equity_returns_1={s: equity_returns_1[s] for s in equity_symbols},
            )

        panel_warnings: tuple[str, ...] = ()
        if gap_series_count:
            # ``gap_series_count`` counts *computation passes* through
            # ``_aligned`` that found at least one gap -- a single security
            # missing one bar is forward-filled once per window it feeds
            # (breadth, dispersion, correlation, ...), so this is an upper
            # bound on affected series, not a distinct-security count.
            panel_warnings = (
                f"FORWARD_FILLED_{gap_observation_count}_GAP_OBSERVATIONS_ACROSS_"
                f"{gap_series_count}_SERIES_COMPUTATION_PASSES",
            )
        return CrossMarketPanel(
            equity_snapshot_id=equity_snapshot_id,
            cross_market_dataset_id=cross_market_dataset_id,
            session_dates=session_dates,
            observations=tuple(observations),
            outcomes_by_horizon=outcomes_by_horizon,
            equity_members=equity_symbols,
            warnings=panel_warnings,
        )


def _build_outcomes(
    *,
    session_dates: tuple[str, ...],
    horizon_sessions: int,
    spy_values: tuple[Decimal, ...],
    tlt_values: tuple[Decimal, ...],
    btc_values: tuple[Decimal, ...],
    equity_returns_1: dict[str, tuple[Decimal | None, ...]],
) -> tuple[CrossMarketOutcomeSet, ...]:
    total = len(session_dates)
    results: list[CrossMarketOutcomeSet] = []
    for index in range(total):
        target = index + horizon_sessions
        if target >= total:
            results.append(
                CrossMarketOutcomeSet(
                    session_date=session_dates[index],
                    horizon_sessions=horizon_sessions,
                    outcomes=(),
                    complete=False,
                )
            )
            continue
        equity_forward_return = bound_statistical_decimal(
            (spy_values[target] - spy_values[index]) / spy_values[index]
        )
        rates_forward_return = bound_statistical_decimal(
            (tlt_values[target] - tlt_values[index]) / tlt_values[index]
        )
        btc_forward_return = bound_statistical_decimal(
            (btc_values[target] - btc_values[index]) / btc_values[index]
        )
        spy_window = spy_values[index : target + 1]
        equity_forward_volatility = _window_annualized_volatility(
            spy_window, sessions_per_year=EQUITY_SESSIONS_PER_YEAR
        )
        btc_window = btc_values[index : target + 1]
        btc_forward_volatility = _window_annualized_volatility(
            btc_window, sessions_per_year=BTC_SESSIONS_PER_YEAR
        )
        cross_section = []
        for series in equity_returns_1.values():
            base = series[index]
            forward = series[target]
            if base is not None and forward is not None:
                cross_section.append(forward)
        dispersion = cross_sectional_dispersion(cross_section)
        outcomes = (
            (OutcomeId.EQUITY_FORWARD_RETURN, equity_forward_return),
            (OutcomeId.EQUITY_FORWARD_VOLATILITY, equity_forward_volatility),
            (OutcomeId.EQUITY_FORWARD_DISPERSION, dispersion),
            (OutcomeId.RATES_PROXY_FORWARD_RETURN, rates_forward_return),
            (OutcomeId.BTC_FORWARD_RETURN, btc_forward_return),
            (OutcomeId.BTC_FORWARD_VOLATILITY, btc_forward_volatility),
        )
        results.append(
            CrossMarketOutcomeSet(
                session_date=session_dates[index],
                horizon_sessions=horizon_sessions,
                outcomes=tuple((k, v) for k, v in outcomes if v is not None),
                complete=True,
            )
        )
    return tuple(results)


def _window_annualized_volatility(
    values: Sequence[Decimal], *, sessions_per_year: int
) -> Decimal | None:
    if len(values) < 3:
        return None
    floats = [float(value) for value in values]
    returns = [floats[i] / floats[i - 1] - 1 for i in range(1, len(floats)) if floats[i - 1] != 0]
    if len(returns) < 2:
        return None
    stdev = statistics.pstdev(returns)
    return Decimal(str(stdev * (sessions_per_year**0.5))).quantize(Decimal("0.00000001"))


# ==========================================================================
# Section: deterministic hypothesis generation (section 5) -- the entire
# genuine v1 agenda catalog (section 6/7), 25 hypotheses across five
# families including six negative controls, is declared here as data, not
# discovered from results. Nothing here is generated after the panel has
# been inspected.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class _Spec:
    template: HypothesisTemplateId
    feature: FeatureId
    rule: ConditionRule
    label: str | None
    secondary_feature: FeatureId | None
    secondary_rule: ConditionRule | None
    secondary_label: str | None
    scope: tuple[MarketId, ...]
    outcome: OutcomeId
    horizon: int
    benchmark: str
    family: HypothesisFamily
    rationale: str
    is_negative_control: bool = False
    negative_control_kind: str | None = None


_ATM = ConditionRule.ABOVE_TRAIN_MEDIAN
_LEQ = ConditionRule.LABEL_EQUALS
_UNCOND = "UNCONDITIONAL_SUBSEQUENT_RETURN"
_PRIOR = "PRIOR_REGIME_DISTRIBUTION"
_RANDOM = "RANDOM_DATE_BASELINE"

V1_AGENDA_HYPOTHESIS_SPECS: tuple[_Spec, ...] = (
    # -- EQUITY_INTERNAL (5) --------------------------------------------
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY,),
        OutcomeId.EQUITY_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_INTERNAL,
        "Broad-market breadth (advancing share) may carry information about subsequent "
        "broad-market return via participation/momentum.",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.EQUITY_DISPERSION_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY,),
        OutcomeId.EQUITY_FORWARD_VOLATILITY,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_INTERNAL,
        "Elevated cross-sectional dispersion may precede elevated realized broad-market "
        "volatility.",
    ),
    _Spec(
        HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING,
        _ATM,
        None,
        FeatureId.EQUITY_VOLATILITY_REGIME_LABEL,
        _LEQ,
        "HIGH_VOLATILITY",
        (MarketId.US_EQUITY,),
        OutcomeId.EQUITY_FORWARD_RETURN,
        20,
        _PRIOR,
        HypothesisFamily.EQUITY_INTERNAL,
        "Breadth's relationship with subsequent broad-market return may differ between "
        "high-volatility and other volatility regimes.",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.EQUITY_SECTOR_BREADTH_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY,),
        OutcomeId.EQUITY_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_INTERNAL,
        "Broad sector-level participation (sector breadth) may precede broad-market continuation.",
    ),
    _Spec(
        HypothesisTemplateId.PERSISTENCE_V1,
        FeatureId.EQUITY_TREND_REGIME_LABEL,
        _LEQ,
        "STRONG_UPTREND",
        None,
        None,
        None,
        (MarketId.US_EQUITY,),
        OutcomeId.PERSISTENCE_INDICATOR,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_INTERNAL,
        "A detected strong-uptrend equity trend regime may persist or mean-revert over "
        "the subsequent horizon.",
    ),
    # -- EQUITY_BTC (7) ---------------------------------------------------
    _Spec(
        HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
        FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        5,
        _UNCOND,
        HypothesisFamily.EQUITY_BTC,
        "Equity breadth may carry short-horizon information about subsequent BTC return "
        "via shared risk appetite.",
    ),
    _Spec(
        HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
        FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_BTC,
        "Equity breadth may carry medium-horizon information about subsequent BTC return.",
    ),
    _Spec(
        HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
        FeatureId.EQUITY_VOLATILITY_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_VOLATILITY,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_BTC,
        "Elevated equity volatility may precede elevated BTC volatility via shared "
        "risk-off conditions.",
    ),
    _Spec(
        HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        FeatureId.EQUITY_BREADTH_FRACTION_ADVANCING,
        _ATM,
        None,
        FeatureId.EQUITY_RISK_TONE_REGIME_LABEL,
        _LEQ,
        "RISK_OFF",
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _PRIOR,
        HypothesisFamily.EQUITY_BTC,
        "Breadth's relationship with subsequent BTC return may differ between risk-off "
        "and other equity risk-tone regimes.",
    ),
    _Spec(
        HypothesisTemplateId.INTERACTION_V1,
        FeatureId.BTC_RETURN_20,
        _ATM,
        None,
        FeatureId.EQUITY_VOLATILITY_20,
        _ATM,
        None,
        (MarketId.BTC, MarketId.US_EQUITY),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _PRIOR,
        HypothesisFamily.EQUITY_BTC,
        "BTC's own momentum may behave differently when equity volatility is elevated versus not.",
    ),
    _Spec(
        HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        FeatureId.BTC_RETURN_20,
        _ATM,
        None,
        FeatureId.BTC_EQUITY_CORRELATION_60,
        _ATM,
        None,
        (MarketId.BTC, MarketId.US_EQUITY),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _PRIOR,
        HypothesisFamily.EQUITY_BTC,
        "BTC momentum's relationship with subsequent BTC return may differ across "
        "BTC/equity correlation regimes.",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.BTC_VOLATILITY_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.BTC, MarketId.US_EQUITY),
        OutcomeId.EQUITY_FORWARD_VOLATILITY,
        20,
        _UNCOND,
        HypothesisFamily.EQUITY_BTC,
        "Elevated BTC volatility may precede elevated equity volatility.",
    ),
    # -- FIXED_INCOME_CONTEXT (4) ------------------------------------------
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.RATES_PROXY_RETURN_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        OutcomeId.EQUITY_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.FIXED_INCOME_CONTEXT,
        "A rising rates-proxy (TLT) trend may precede a different subsequent equity return.",
    ),
    _Spec(
        HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        FeatureId.RATES_PROXY_RETURN_20,
        _ATM,
        None,
        FeatureId.BOND_RISK_RELATIONSHIP_REGIME_LABEL,
        _LEQ,
        "RATE_TAILWIND",
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        OutcomeId.EQUITY_FORWARD_RETURN,
        20,
        _PRIOR,
        HypothesisFamily.FIXED_INCOME_CONTEXT,
        "The rates-proxy trend's relationship with subsequent equity return may differ "
        "by bond/equity correlation regime.",
    ),
    _Spec(
        HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
        FeatureId.RATES_PROXY_RETURN_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.RATES_PROXY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.FIXED_INCOME_CONTEXT,
        "The rates-proxy trend may carry information about subsequent BTC return.",
    ),
    _Spec(
        HypothesisTemplateId.PERSISTENCE_V1,
        FeatureId.BOND_RISK_RELATIONSHIP_REGIME_LABEL,
        _LEQ,
        "RATE_TAILWIND",
        None,
        None,
        None,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        OutcomeId.PERSISTENCE_INDICATOR,
        20,
        _UNCOND,
        HypothesisFamily.FIXED_INCOME_CONTEXT,
        "A detected rate-tailwind regime may persist or mean-revert over the subsequent horizon.",
    ),
    # -- COMMODITY_CONTEXT (3) ---------------------------------------------
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.COMMODITY_PROXY_RETURN_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.COMMODITY_PROXY, MarketId.US_EQUITY),
        OutcomeId.EQUITY_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.COMMODITY_CONTEXT,
        "The commodity-proxy (DBC) trend may precede a different subsequent equity return.",
    ),
    _Spec(
        HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
        FeatureId.COMMODITY_PROXY_RETURN_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.COMMODITY_PROXY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.COMMODITY_CONTEXT,
        "The commodity-proxy trend may carry information about subsequent BTC return.",
    ),
    _Spec(
        HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        FeatureId.COMMODITY_EQUITY_CORRELATION_60,
        _ATM,
        None,
        FeatureId.EQUITY_VOLATILITY_REGIME_LABEL,
        _LEQ,
        "HIGH_VOLATILITY",
        (MarketId.COMMODITY_PROXY, MarketId.US_EQUITY),
        OutcomeId.EQUITY_FORWARD_VOLATILITY,
        20,
        _PRIOR,
        HypothesisFamily.COMMODITY_CONTEXT,
        "Commodity/equity correlation's relationship with subsequent equity volatility "
        "may differ by volatility regime.",
    ),
    # -- NEGATIVE_CONTROL (6) -----------------------------------------------
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.SHUFFLED_EQUITY_BREADTH,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.NEGATIVE_CONTROL,
        "Negative control: equity breadth with its date alignment destroyed by a "
        "fixed-seed shuffle should show no relationship with subsequent BTC return.",
        True,
        "SHUFFLED_FEATURE",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.LAG_DISPLACED_EQUITY_BREADTH,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.NEGATIVE_CONTROL,
        "Negative control: equity breadth deliberately misaligned 40 sessions should "
        "show no relationship with subsequent BTC return.",
        True,
        "LAG_DISPLACED_FEATURE",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.EQUITY_SECTOR_DISPERSION_20,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_VOLATILITY,
        5,
        _UNCOND,
        HypothesisFamily.NEGATIVE_CONTROL,
        "Negative control: sector-level equity dispersion has no plausible fast "
        "transmission mechanism to next-week BTC volatility.",
        True,
        "ECONOMICALLY_UNRELATED_PAIR",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.RANDOM_NOISE_BASELINE,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.US_EQUITY, MarketId.BTC),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _RANDOM,
        HypothesisFamily.NEGATIVE_CONTROL,
        "Negative control: pure seeded noise, unrelated to any genuine evidence, should "
        "show no relationship with subsequent BTC return.",
        True,
        "RANDOM_DATE_BASELINE",
    ),
    _Spec(
        HypothesisTemplateId.PERSISTENCE_V1,
        FeatureId.EQUITY_DAILY_RETURN_SIGN,
        _LEQ,
        "POSITIVE",
        None,
        None,
        None,
        (MarketId.US_EQUITY,),
        OutcomeId.PERSISTENCE_INDICATOR,
        5,
        _UNCOND,
        HypothesisFamily.NEGATIVE_CONTROL,
        "Negative control: single-day equity return sign is close to a coin flip and "
        "should not show strong persistence.",
        True,
        "PERSISTENCE_BASELINE",
    ),
    _Spec(
        HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        FeatureId.ALWAYS_PRESENT_BASELINE,
        _ATM,
        None,
        None,
        None,
        None,
        (MarketId.BTC,),
        OutcomeId.BTC_FORWARD_RETURN,
        20,
        _UNCOND,
        HypothesisFamily.NEGATIVE_CONTROL,
        "Negative control: an always-present condition (the full unconditional sample) "
        "tests whether the methodology fabricates an edge with no real conditioning "
        "information.",
        True,
        "UNCONDITIONAL_OUTCOME",
    ),
)


V1_AGENDA_VERSION = "V1"
V1_AGENDA_OBJECTIVE = (
    "Systematically investigate a bounded set of economically plausible cross-market "
    "research questions (equity-internal, equity/BTC, fixed-income-context, "
    "commodity-context, and six negative controls) using genuine retained history. "
    "This is descriptive/inferential research evidence only -- it creates no Strategy, "
    "Shared Opportunity, PAPER order, or execution authority."
)
V1_AGENDA_MAXIMUM_TOTAL_TRIALS = 400
V1_AGENDA_TRAIN_VALIDATION_HOLDOUT_POLICY = (
    "One shared TRAIN < VALIDATION < HOLDOUT(locked) period policy across every "
    "hypothesis in the agenda; the holdout is declared and locked before any "
    "holdout-phase trial is recorded."
)
V1_AGENDA_MULTIPLE_TESTING_FAMILY_POLICY = (
    "PARTITIONED_BY_HYPOTHESIS_FAMILY_V1: Benjamini-Hochberg FDR correction applied "
    "independently within each of the five HypothesisFamily partitions."
)
V1_AGENDA_ROBUSTNESS_REQUIREMENTS = (
    "SUBPERIOD_SPLIT",
    "REGIME_SPLIT",
    "LEAVE_ONE_PERIOD_OUT",
    "MISSING_DATA_SENSITIVITY",
    "COST_STRESS",
)


def build_v1_agenda(
    *,
    dataset_snapshot_id: str,
    replay_run_id: str,
    cross_market_dataset_id: str,
    cohort_policy_id: str,
    registered_at: datetime,
) -> ResearchAgenda:
    """The one genuine bounded v1 agenda's fixed policy (sections 1 and
    6): every field except the runtime dataset/replay lineage is a fixed
    constant, declared once here so the CLI, the API, and the genuine
    acceptance run all register byte-identical agendas from the same
    lineage inputs -- the budget is never widened after the fact."""

    return ResearchAgenda.create(
        version=V1_AGENDA_VERSION,
        objective=V1_AGENDA_OBJECTIVE,
        allowed_markets=tuple(MarketId),
        allowed_feature_families=tuple(FeatureFamily),
        allowed_outcome_families=tuple(OutcomeFamily),
        allowed_horizons_sessions=(5, 20, 60),
        allowed_templates=tuple(HypothesisTemplateId),
        hypothesis_generation_method=HypothesisGenerationMethod.DETERMINISTIC_TEMPLATE_EXPANSION_V1,
        maximum_hypothesis_count=len(V1_AGENDA_HYPOTHESIS_SPECS),
        maximum_configurations_per_hypothesis=1,
        maximum_total_trials=V1_AGENDA_MAXIMUM_TOTAL_TRIALS,
        train_validation_holdout_policy=V1_AGENDA_TRAIN_VALIDATION_HOLDOUT_POLICY,
        multiple_testing_family_policy=V1_AGENDA_MULTIPLE_TESTING_FAMILY_POLICY,
        robustness_requirements=V1_AGENDA_ROBUSTNESS_REQUIREMENTS,
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        cohort_policy_id=cohort_policy_id,
        registered_at=registered_at,
    )


def generate_v1_agenda_hypotheses(
    *, agenda: ResearchAgenda, created_at: datetime
) -> tuple[GeneratedHypothesis, ...]:
    """Deterministic template expansion (section 5): every hypothesis this
    function can ever produce is fixed by ``V1_AGENDA_HYPOTHESIS_SPECS``
    above, declared before any dataset result is inspected. Calling this
    twice with the same agenda always produces byte-identical hypotheses
    (same ``hypothesis_id``s), since every field -- including ``ordinal``
    -- is a pure function of the spec table and the agenda."""

    if len(V1_AGENDA_HYPOTHESIS_SPECS) > agenda.maximum_hypothesis_count:
        raise CrossMarketResearchApplicationError("V1_AGENDA_HYPOTHESIS_COUNT_EXCEEDS_BUDGET")
    hypotheses = []
    for ordinal, spec in enumerate(V1_AGENDA_HYPOTHESIS_SPECS, start=1):
        hypotheses.append(
            GeneratedHypothesis.create(
                agenda=agenda,
                template_id=spec.template,
                template_version="V1",
                feature_id=spec.feature,
                condition_rule=spec.rule,
                condition_label=spec.label,
                secondary_feature_id=spec.secondary_feature,
                secondary_condition_rule=spec.secondary_rule,
                secondary_condition_label=spec.secondary_label,
                market_scope=spec.scope,
                outcome_id=spec.outcome,
                horizon_sessions=spec.horizon,
                benchmark_kind=spec.benchmark,
                family=spec.family,
                rationale=spec.rationale,
                proposal_source=ProposalSource.DETERMINISTIC_RULE,
                is_negative_control=spec.is_negative_control,
                negative_control_kind=spec.negative_control_kind,
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


# --------------------------------------------------------------------------
# Section 5 (assisted proposal seam): a constrained interface an external
# model's proposed research question could later be validated against.
# Disabled by default; model output cannot create authority, must map to
# an approved template and the existing feature vocabulary, and is never
# itself evidence. Nothing in this milestone calls an external model.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExternalHypothesisProposal:
    """The entire input contract an external-model integration would have
    to satisfy -- plain, bounded, already-vocabulary-constrained fields,
    never free text mapped to code."""

    template_id: str
    feature_id: str
    condition_rule: str
    condition_label: str | None
    secondary_feature_id: str | None
    secondary_condition_rule: str | None
    secondary_condition_label: str | None
    market_scope: tuple[str, ...]
    outcome_id: str
    horizon_sessions: int
    benchmark_kind: str
    family: str
    rationale: str
    model_identity: str


class AssistedProposalDisabledError(CrossMarketResearchApplicationError):
    pass


class AssistedProposalInvalidError(CrossMarketResearchApplicationError):
    pass


def submit_assisted_proposal(
    proposal: ExternalHypothesisProposal,
    *,
    agenda: ResearchAgenda,
    ordinal: int,
    created_at: datetime,
    enabled: bool = ASSISTED_PROPOSALS_ENABLED,
) -> GeneratedHypothesis:
    """The one function through which an external model's proposal could
    ever become a ``GeneratedHypothesis`` -- fails closed by default
    (``enabled`` defaults to the module-level ``ASSISTED_PROPOSALS_ENABLED
    = False``), and even when enabled, every field is re-validated against
    the identical approved template/feature/outcome/market vocabulary
    ``GeneratedHypothesis.create`` already enforces for deterministic
    proposals -- an external model can select from the existing bounded
    vocabulary, never invent new terms, and the proposal itself is never
    retained as evidence of anything beyond its own existence."""

    if not enabled:
        raise AssistedProposalDisabledError("ASSISTED_MODEL_PROPOSALS_ARE_DISABLED_THIS_MILESTONE")
    try:
        return GeneratedHypothesis.create(
            agenda=agenda,
            template_id=proposal.template_id,
            template_version="ASSISTED_V1",
            feature_id=proposal.feature_id,
            condition_rule=proposal.condition_rule,
            condition_label=proposal.condition_label,
            secondary_feature_id=proposal.secondary_feature_id,
            secondary_condition_rule=proposal.secondary_condition_rule,
            secondary_condition_label=proposal.secondary_condition_label,
            market_scope=proposal.market_scope,
            outcome_id=proposal.outcome_id,
            horizon_sessions=proposal.horizon_sessions,
            benchmark_kind=proposal.benchmark_kind,
            family=proposal.family,
            rationale=(
                f"[ASSISTED PROPOSAL from {proposal.model_identity}, not itself evidence] "
                f"{proposal.rationale}"
            ),
            proposal_source=ProposalSource.DETERMINISTIC_RULE,
            is_negative_control=False,
            negative_control_kind=None,
            discovery_status=DiscoveryStatus.DISCOVERED_THIS_AGENDA,
            ordinal=ordinal,
            created_at=created_at,
        )
    except CrossMarketResearchError as error:
        raise AssistedProposalInvalidError(str(error)) from error


# ==========================================================================
# Section: condition resolution -- turns a hypothesis's declared
# feature/rule/label into the concrete set of dates where its condition
# holds. The *only* place a numeric threshold is ever derived, and it is
# always derived from the train period alone (never validation/holdout),
# then applied identically and without re-derivation to every later
# period -- this is what keeps a "fixed a priori" configuration honest.
# ==========================================================================


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def resolve_condition_dates(
    *,
    feature_id: FeatureId,
    condition_rule: ConditionRule,
    condition_label: str | None,
    feature_by_date: dict[str, Decimal],
    regime_by_date: dict[str, CrossMarketRegimeSnapshot],
    train_dates: frozenset[str],
) -> frozenset[str]:
    if condition_rule is ConditionRule.ABOVE_TRAIN_MEDIAN:
        train_values = [feature_by_date[d] for d in train_dates if d in feature_by_date]
        threshold = _median(train_values)
        if threshold is None:
            return frozenset()
        return frozenset(d for d, value in feature_by_date.items() if value >= threshold)
    dimension = REGIME_CONDITION_FEATURE_IDS.get(feature_id)
    if dimension is not None:
        return frozenset(
            date
            for date, regime in regime_by_date.items()
            if regime.label_for(dimension) == condition_label
        )
    if feature_id is FeatureId.EQUITY_DAILY_RETURN_SIGN:
        target = Decimal(1) if condition_label == "POSITIVE" else Decimal(-1)
        return frozenset(d for d, value in feature_by_date.items() if value == target)
    raise CrossMarketResearchApplicationError("LABEL_EQUALS_CONDITION_HAS_NO_RESOLUTION_SOURCE")


def _persistence_outcome_by_date(
    *,
    feature_id: FeatureId,
    condition_rule: ConditionRule,
    condition_label: str | None,
    panel: CrossMarketPanel,
    horizon_sessions: int,
) -> dict[str, Decimal]:
    """The PERSISTENCE template's outcome (section 4/13): 1 if the source
    condition still holds ``horizon_sessions`` sessions later, else 0.
    Reuses ``resolve_condition_dates`` for both the T and T+H membership
    check -- there is exactly one definition of "the condition holds"."""

    dates = panel.session_dates
    feature_by_date = panel.feature_by_date(feature_id)
    regime_by_date = panel.regime_by_date()
    condition_dates = resolve_condition_dates(
        feature_id=feature_id,
        condition_rule=condition_rule,
        condition_label=condition_label,
        feature_by_date=feature_by_date,
        regime_by_date=regime_by_date,
        train_dates=frozenset(dates),
    )
    result: dict[str, Decimal] = {}
    for index in range(len(dates) - horizon_sessions):
        source_date = dates[index]
        if source_date not in condition_dates:
            continue
        target_date = dates[index + horizon_sessions]
        result[source_date] = Decimal(1) if target_date in condition_dates else Decimal(0)
    return result


def _direction_consistent(base_mean: Decimal, candidate_mean: Decimal | None) -> bool | None:
    if candidate_mean is None or base_mean == 0:
        return None
    return (candidate_mean > 0) == (base_mean > 0)


def derive_knowledge_status(
    conclusion_state: ResearchConclusionState, *, promotion_eligible: bool, robustness_passed: bool
) -> KnowledgeStatus:
    """Section 17/25: the *only* place a hypothesis's promotion boundary
    is decided. Deliberately does not special-case negative controls --
    if a negative control were ever to reach ``SUPPORTED_FOR_FURTHER_
    STUDY`` here, that is itself an important, honestly-surfaced finding
    about this methodology, not something to silently correct."""

    if conclusion_state in (
        ResearchConclusionState.REJECTED,
        ResearchConclusionState.HOLDOUT_FAILED,
        ResearchConclusionState.ROBUSTNESS_FAILED,
    ):
        return KnowledgeStatus.REJECTED
    if conclusion_state is ResearchConclusionState.INSUFFICIENT_EVIDENCE:
        return KnowledgeStatus.ACTIVE_RESEARCH
    if conclusion_state in (
        ResearchConclusionState.INCONCLUSIVE,
        ResearchConclusionState.WEAK_SUPPORT,
    ):
        return KnowledgeStatus.WEAK
    if conclusion_state is ResearchConclusionState.SUPPORTED:
        if promotion_eligible and robustness_passed:
            return KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY
        return KnowledgeStatus.WEAK
    return KnowledgeStatus.ACTIVE_RESEARCH


LIMITATIONS = (
    "POINT_IN_TIME_UNIVERSE_INCOMPLETE",
    "SURVIVORSHIP_LIMITED",
    "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",
)


# ==========================================================================
# Section: the shared agenda timeline (train/validation/holdout dates,
# stability quarters, walk-forward fold windows, and the ONE governance
# period policy every hypothesis's ``ResearchGovernancePolicy`` wraps) --
# computed once per agenda run, never per hypothesis.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class AgendaTimeline:
    train_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    holdout_dates: tuple[str, ...]
    holdout_start: str
    holdout_end: str
    all_dates: tuple[str, ...]
    quarters: tuple[tuple[str, str, str, bool], ...]
    fold_windows: tuple[FoldWindow, ...]
    period_policy: ResearchPeriodPolicy


def build_agenda_timeline(
    *,
    panel: CrossMarketPanel,
    agenda: ResearchAgenda,
    train_end: str,
    validation_end: str,
    created_at: datetime,
    minimum_sample_count: int = MINIMUM_TRIAL_SAMPLE,
) -> AgendaTimeline:
    dates = panel.session_dates
    train_dates = tuple(d for d in dates if d <= train_end)
    validation_dates = tuple(d for d in dates if train_end < d <= validation_end)
    holdout_dates = tuple(d for d in dates if d > validation_end)
    if not train_dates or not validation_dates or not holdout_dates:
        raise CrossMarketResearchApplicationError(
            "AGENDA_TIMELINE_PERIOD_BOUNDARIES_PRODUCE_AN_EMPTY_PERIOD"
        )
    period_policy = ResearchPeriodPolicy.create(
        replay_run_id=agenda.replay_run_id,
        periods=(
            ResearchPeriodDeclaration(
                ResearchPeriodRole.TRAIN, train_dates[0], train_dates[-1], False
            ),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, validation_dates[0], validation_dates[-1], False
            ),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.HOLDOUT, holdout_dates[0], holdout_dates[-1], True
            ),
        ),
        minimum_sample_count=minimum_sample_count,
        created_at=created_at,
    )
    total = len(dates)
    edges = (0, total // 4, total // 2, (3 * total) // 4, total)
    labels = ("EARLIEST_QUARTER", "SECOND_QUARTER", "THIRD_QUARTER", "MOST_RECENT_QUARTER")
    vol_by_date = panel.feature_by_date(FeatureId.EQUITY_VOLATILITY_20)

    def _average_volatility(start: str, end: str) -> Decimal | None:
        values = [vol_by_date[d] for d in dates if start <= d <= end and d in vol_by_date]
        return (
            None
            if not values
            else bound_statistical_decimal(sum(values, Decimal(0)) / Decimal(len(values)))
        )

    quarter_candidates = [(labels[i], dates[edges[i]], dates[edges[i + 1] - 1]) for i in range(4)]
    quarter_volatility = [
        (label, start, end, _average_volatility(start, end))
        for label, start, end in quarter_candidates
    ]
    evaluable: list[tuple[str, Decimal]] = [
        (label, vol) for label, _start, _end, vol in quarter_volatility if vol is not None
    ]
    highest_label = max(evaluable, key=lambda item: item[1])[0] if evaluable else None
    quarters = tuple(
        (label, start, end, label == highest_label)
        for label, start, end, _vol in quarter_volatility
    )
    fold_windows = generate_fold_windows(
        session_dates=dates,
        training_window_sessions=504,
        validation_window_sessions=0,
        test_window_sessions=252,
        step_sessions=252,
        window_mode=TrainingWindowMode.EXPANDING,
    )
    return AgendaTimeline(
        train_dates=train_dates,
        validation_dates=validation_dates,
        holdout_dates=holdout_dates,
        holdout_start=holdout_dates[0],
        holdout_end=holdout_dates[-1],
        all_dates=dates,
        quarters=quarters,
        fold_windows=fold_windows,
        period_policy=period_policy,
    )


@dataclass(frozen=True, slots=True)
class HypothesisRunResult:
    execution: HypothesisExecutionRecord
    knowledge_entry: ResearchKnowledgeEntry


SampleUniverseFn = Callable[[frozenset[str]], tuple[frozenset[str], frozenset[str]]]


def _grouped_by_label(
    pairs: Sequence[tuple[str, Decimal]],
) -> dict[str, list[Decimal]]:
    grouped: dict[str, list[Decimal]] = {}
    for label, value in pairs:
        grouped.setdefault(label, []).append(value)
    return grouped


def _effect_size_for_panel_sample(
    sample_values: Sequence[Decimal], benchmark_values: Sequence[Decimal]
) -> EffectSize:
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


def _panel_sample_diagnostics(
    *,
    sample_values: Sequence[Decimal],
    universe_values: Sequence[Decimal],
    outcome_horizon_sessions: int,
    grouped_pairs: tuple[tuple[str, Decimal], ...],
    bootstrap_seed: int | None,
    hac_lag: int | None,
) -> StatisticalDiagnosticsV2:
    """A drop-in replacement for ``domain.research_methodology.
    build_diagnostics_v2`` for a trial built from a scattered, condition-
    matched *panel* sample rather than a contiguous sequential return
    series -- reuses ``summarize_values``, ``newey_west_standard_error``,
    and ``seeded_bootstrap_confidence_interval`` completely unmodified,
    fixing exactly two structural gaps ``build_diagnostics_v2`` has for
    this shape of sample:

    1. It always calls ``summarize_values`` with the historical default
       ``is_return_series=True``, which multiplicatively compounds the
       sample as if it were a sequential return series -- meaningless for
       panel observations sampled at overlapping starting points, and it
       can raise ``decimal.InvalidOperation`` outright on a large,
       realistic sample (verified empirically while building this
       milestone's genuine panel).
    2. It derives overlap/effective-sample accounting entirely from a
       ``HistoricalResearchEvaluation`` sequence. The *grouped* trial path
       (``RecordGroupedResearchTrialCommand``, whose own docstring names
       exactly this "conditional sample" shape) always calls it with an
       empty sequence, so ``effective_sample_count`` is unconditionally
       zero regardless of the real sample size -- every trial would
       register as ``INSUFFICIENT_EFFECTIVE_SAMPLE`` regardless of what
       the data actually shows. This function instead applies the
       identical conservative stride formula ``compute_overlap_
       diagnostics`` already uses for its own non-trivial branch (``raw /
       outcome_horizon_sessions``), computed directly from the real
       sample size -- the same honest, conservative "assume every window
       overlaps" treatment, just not gated behind evaluation objects this
       milestone's grouped panel trials do not have.
    """

    base = summarize_values(
        tuple(sample_values),
        kind=SummaryKind.EXPLORATORY,
        group_label="trial",
        is_return_series=False,
    )
    raw = len(sample_values)
    if raw == 0:
        overlap = OverlapDiagnostics(
            overlapping_pair_count=0,
            raw_sample_count=0,
            effective_sample_count=Decimal(0),
            handling_mode=OverlapHandlingMode.RETAINED_DESCRIPTIVE,
            inference_available=True,
            note="NO_SAMPLES",
        )
    elif outcome_horizon_sessions <= 1:
        overlap = OverlapDiagnostics(
            overlapping_pair_count=0,
            raw_sample_count=raw,
            effective_sample_count=Decimal(raw),
            handling_mode=OverlapHandlingMode.RETAINED_DESCRIPTIVE,
            inference_available=True,
            note="SINGLE_SESSION_HORIZON_NO_FORWARD_WINDOW_OVERLAP",
        )
    else:
        effective = max(Decimal(1), Decimal(raw) / Decimal(outcome_horizon_sessions))
        overlap = OverlapDiagnostics(
            overlapping_pair_count=max(0, raw - 1),
            raw_sample_count=raw,
            effective_sample_count=bound_statistical_decimal(effective),
            handling_mode=OverlapHandlingMode.EXCLUDED_FROM_INFERENCE,
            inference_available=False,
            note=(
                "OVERLAPPING_FORWARD_OUTCOME_WINDOWS_ASSUMED_FOR_PANEL_SAMPLE_"
                "EFFECTIVE_SAMPLE_USES_HORIZON_STRIDE_ADJUSTMENT"
            ),
        )
    notes: list[str] = []
    standard_error = None
    if base.stdev is not None and overlap.effective_sample_count >= 2:
        if hac_lag is not None:
            standard_error = newey_west_standard_error(tuple(sample_values), lag=hac_lag)
            if standard_error is None:
                notes.append("HAC_STANDARD_ERROR_UNAVAILABLE_SAMPLE_TOO_THIN_FOR_REQUESTED_LAG")
        else:
            with localcontext() as context:
                context.prec = 28
                se_value = base.stdev / overlap.effective_sample_count.sqrt()
            standard_error = StandardErrorEstimate(
                value=bound_statistical_decimal(se_value), method_id=IID_SE_METHOD_ID
            )
    effect_size = (
        _effect_size_for_panel_sample(sample_values, universe_values) if universe_values else None
    )
    bootstrap = (
        seeded_bootstrap_confidence_interval(tuple(sample_values), seed=bootstrap_seed)
        if bootstrap_seed is not None
        else None
    )
    grouped = (
        tuple(
            (
                label,
                summarize_values(
                    tuple(values),
                    kind=SummaryKind.EXPLORATORY,
                    group_label=label,
                    is_return_series=False,
                ),
            )
            for label, values in sorted(_grouped_by_label(grouped_pairs).items())
        )
        if grouped_pairs
        else ()
    )
    if not overlap.inference_available:
        notes.append(overlap.note)
    return StatisticalDiagnosticsV2(
        base=base,
        overlap=overlap,
        standard_error=standard_error,
        effect_size=effect_size,
        bootstrap_confidence_interval=bootstrap,
        grouped=grouped,
        inference_available=overlap.inference_available,
        methodology_notes=tuple(notes),
    )


def record_panel_sample_trial(
    *, repository: ResearchMethodologyRepository, command: RecordGroupedResearchTrialCommand
) -> ResearchTrial:
    """Structurally identical to ``application.research_methodology.
    RecordGroupedResearchTrial.execute`` -- same validation, same
    benchmark construction via the unmodified ``compare_to_benchmark``,
    same idempotent save-or-conflict behavior -- the only difference is
    calling ``_panel_sample_diagnostics`` in place of ``build_diagnostics
    _v2`` for the two structural reasons documented there."""

    if not command.sample_evaluation_ids:
        raise ResearchMethodologyApplicationError("TRIAL_HAS_NO_RESOLVED_OUTCOMES_YET")
    governance_policy = None
    if command.governance_policy_id is not None:
        governance_policy = repository.governance_policy_by_id(command.governance_policy_id)
        if governance_policy is None:
            raise MethodologyNotFound("GOVERNANCE_POLICY_NOT_FOUND")
    benchmark = None
    kind = BenchmarkKind(command.benchmark_kind) if command.benchmark_kind is not None else None
    if kind is not None and command.universe_values:
        sample_mean = bound_statistical_decimal(
            sum(command.sample_values, Decimal(0)) / Decimal(len(command.sample_values))
        )
        benchmark = compare_to_benchmark(
            sample_mean=sample_mean, benchmark_kind=kind, benchmark_values=command.universe_values
        )
    diagnostics = _panel_sample_diagnostics(
        sample_values=command.sample_values,
        universe_values=command.universe_values,
        outcome_horizon_sessions=command.outcome_horizon_sessions,
        grouped_pairs=command.grouped_pairs,
        bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
        hac_lag=None,
    )
    trial = ResearchTrial.create(
        hypothesis_version_id=command.hypothesis_version_id,
        dataset_snapshot_id=command.dataset_snapshot_id,
        replay_run_id=command.replay_run_id,
        phase=command.phase,
        market_scope=command.market_scope,
        configuration=command.configuration,
        dated_evaluation_ids=command.sample_evaluation_ids,
        diagnostics=diagnostics,
        benchmark=benchmark,
        created_at=command.created_at,
        governance_policy=governance_policy,
    )
    existing = repository.trial_by_id(trial.trial_id)
    if existing is not None:
        if existing.canonical_hash != trial.canonical_hash:
            raise MethodologyConflict("TRIAL_IDENTITY_CONFLICT")
        return existing
    return repository.save_trial(trial)


class RunSingleHypothesis:
    """Drives exactly one hypothesis through the full governed methodology
    stack (section 9): governance policy, exploratory/validation/locked-
    holdout grouped trials, walk-forward fold trials, robustness checks,
    a retained conclusion, and a promotion evaluation -- entirely by
    composing the existing, unmodified ``application.research_methodology``
    services. Multiple-testing correction is deliberately *not* done here
    (see ``FinalizeAgenda``): it needs every hypothesis in a family's
    validation trial to exist first."""

    def __init__(
        self, *, methodology_repository: ResearchMethodologyRepository, timeline: AgendaTimeline
    ) -> None:
        self._methodology_repository = methodology_repository
        self._timeline = timeline

    def execute(
        self,
        *,
        agenda: ResearchAgenda,
        hypothesis: GeneratedHypothesis,
        panel: CrossMarketPanel,
        created_at: datetime,
    ) -> HypothesisRunResult:
        governance_policy = DeclareResearchGovernancePolicy(
            repository=self._methodology_repository
        ).execute(
            DeclareGovernancePolicyCommand(
                hypothesis_version_id=hypothesis.hypothesis_id,
                dataset_snapshot_id=agenda.dataset_snapshot_id,
                replay_run_id=agenda.replay_run_id,
                intent=GovernanceIntent.PROMOTION_TRACK,
                period_policy=self._timeline.period_policy,
                revision=1,
                supersedes_governance_policy_id=None,
                declared_at=created_at,
            )
        )

        feature_by_date = panel.feature_by_date(hypothesis.feature_id)
        regime_by_date = panel.regime_by_date()
        train_frozenset = frozenset(self._timeline.train_dates)

        if hypothesis.template_id is HypothesisTemplateId.PERSISTENCE_V1:
            outcome_by_date = _persistence_outcome_by_date(
                feature_id=hypothesis.feature_id,
                condition_rule=hypothesis.condition_rule,
                condition_label=hypothesis.condition_label,
                panel=panel,
                horizon_sessions=hypothesis.horizon_sessions,
            )
        else:
            outcome_by_date = panel.outcome_by_date(
                hypothesis.outcome_id, horizon_sessions=hypothesis.horizon_sessions
            )

        primary_dates = resolve_condition_dates(
            feature_id=hypothesis.feature_id,
            condition_rule=hypothesis.condition_rule,
            condition_label=hypothesis.condition_label,
            feature_by_date=feature_by_date,
            regime_by_date=regime_by_date,
            train_dates=train_frozenset,
        )
        secondary_dates: frozenset[str] | None = None
        if (
            hypothesis.secondary_feature_id is not None
            and hypothesis.secondary_condition_rule is not None
        ):
            secondary_feature_by_date = panel.feature_by_date(hypothesis.secondary_feature_id)
            secondary_dates = resolve_condition_dates(
                feature_id=hypothesis.secondary_feature_id,
                condition_rule=hypothesis.secondary_condition_rule,
                condition_label=hypothesis.secondary_condition_label,
                feature_by_date=secondary_feature_by_date,
                regime_by_date=regime_by_date,
                train_dates=train_frozenset,
            )

        def sample_universe(period_dates: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
            if secondary_dates is None:
                return period_dates & primary_dates, period_dates
            fixed = period_dates & primary_dates
            return fixed & secondary_dates, fixed - secondary_dates

        blockers: list[str] = []

        exploratory_trial = self._trial(
            hypothesis,
            agenda,
            governance_policy.governance_policy_id,
            ResearchPhase.EXPLORATORY,
            sample_universe(train_frozenset),
            outcome_by_date,
            created_at,
        )
        validation_trial = self._trial(
            hypothesis,
            agenda,
            governance_policy.governance_policy_id,
            ResearchPhase.VALIDATION,
            sample_universe(frozenset(self._timeline.validation_dates)),
            outcome_by_date,
            created_at,
        )
        if validation_trial is None:
            blockers.append("VALIDATION_SAMPLE_INSUFFICIENT")

        holdout_trial = None
        if validation_trial is not None:
            RecordHoldoutExposure(repository=self._methodology_repository).execute(
                RecordHoldoutExposureCommand(
                    governance_policy_id=governance_policy.governance_policy_id,
                    exposed_at=created_at,
                    actor="cross_market_research_agenda_v1",
                    purpose=("LOCKED_HOLDOUT_TRIAL_EVALUATION_FOR_CROSS_MARKET_RESEARCH_AGENDA_V1"),
                    consumed_trial_id=None,
                )
            )
            holdout_trial = self._trial(
                hypothesis,
                agenda,
                governance_policy.governance_policy_id,
                ResearchPhase.LOCKED_HOLDOUT,
                sample_universe(frozenset(self._timeline.holdout_dates)),
                outcome_by_date,
                created_at,
            )
            if holdout_trial is None:
                blockers.append("HOLDOUT_SAMPLE_INSUFFICIENT")

        walk_forward_ids: list[str] = []
        for window in self._timeline.fold_windows:
            test_dates = frozenset(
                d for d in panel.session_dates if window.test_start <= d <= window.test_end
            )
            fold_trial = self._trial(
                hypothesis,
                agenda,
                governance_policy.governance_policy_id,
                ResearchPhase.WALK_FORWARD,
                sample_universe(test_dates),
                outcome_by_date,
                created_at,
            )
            if fold_trial is not None:
                walk_forward_ids.append(fold_trial.trial_id)

        stability = self._stability(sample_universe, outcome_by_date)

        base_trial = holdout_trial or validation_trial
        robustness_run = None
        if base_trial is not None and base_trial.diagnostics.base.mean is not None:
            robustness_run = self._robustness(
                sample_universe, outcome_by_date, base_trial, regime_by_date, created_at
            )
            self._methodology_repository.save_robustness_run(robustness_run)
        else:
            blockers.append("ROBUSTNESS_NOT_RUN_NO_REFERENCE_TRIAL")

        conclusion = BuildResearchConclusion(repository=self._methodology_repository).execute(
            BuildConclusionCommand(
                hypothesis_version_id=hypothesis.hypothesis_id,
                dataset_snapshot_id=agenda.dataset_snapshot_id,
                replay_run_id=agenda.replay_run_id,
                training_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
                validation_trial_id=validation_trial.trial_id if validation_trial else None,
                holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
                walk_forward_run_id=None,
                robustness_run_id=robustness_run.robustness_run_id if robustness_run else None,
                trial_family_id=None,
                point_in_time_limitations=LIMITATIONS,
                rationale=hypothesis.rationale,
                created_at=created_at,
            )
        )
        promotion = EvaluatePromotion(repository=self._methodology_repository).execute(
            EvaluatePromotionCommand(
                conclusion_id=conclusion.conclusion_id,
                policy=PromotionPolicy(),
                lookahead_violation=False,
                created_at=created_at,
            )
        )
        robustness_passed = robustness_run.summary.passed if robustness_run is not None else False
        status = derive_knowledge_status(
            conclusion.conclusion_state,
            promotion_eligible=promotion.eligible,
            robustness_passed=robustness_passed,
        )
        execution = HypothesisExecutionRecord.create(
            hypothesis_id=hypothesis.hypothesis_id,
            agenda_id=agenda.agenda_id,
            governance_policy_id=governance_policy.governance_policy_id,
            exploratory_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
            validation_trial_id=validation_trial.trial_id if validation_trial else None,
            holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
            walk_forward_trial_ids=tuple(walk_forward_ids),
            robustness_run_id=robustness_run.robustness_run_id if robustness_run else None,
            trial_family_id=None,
            conclusion_id=conclusion.conclusion_id,
            promotion_evaluation_id=promotion.promotion_evaluation_id,
            stability=stability,
            evidence_shape="TIME_SERIES_PANEL_WITH_SHARED_MARKET_DATES_NOT_INDEPENDENT_SAMPLES",
            blockers=tuple(blockers),
            created_at=created_at,
        )
        knowledge_entry = ResearchKnowledgeEntry.create(
            hypothesis_id=hypothesis.hypothesis_id,
            conclusion_id=conclusion.conclusion_id,
            status=status,
            evidence_strength=f"INITIAL_{conclusion.conclusion_state.value}",
            phase_reached=conclusion.phase.value,
            markets=hypothesis.market_scope,
            regime_dependence=(stability.state.value if stability else "INSUFFICIENT_EVIDENCE"),
            benchmark_kind=hypothesis.benchmark_kind,
            holdout_result=(
                "NOT_EVALUATED"
                if holdout_trial is None or holdout_trial.benchmark is None
                else (
                    "BEATS_BENCHMARK"
                    if holdout_trial.benchmark.beats_benchmark
                    else "DOES_NOT_BEAT_BENCHMARK"
                )
            ),
            robustness_state=(
                "PASSED"
                if robustness_passed
                else ("NOT_RUN" if robustness_run is None else "FAILED")
            ),
            cross_sectional_or_time_series=(
                "TIME_SERIES_PANEL_WITH_SHARED_MARKET_DATES_NOT_INDEPENDENT_SAMPLES"
            ),
            limitations=LIMITATIONS,
            revision=1,
            supersedes_entry_id=None,
            created_at=created_at,
        )
        return HypothesisRunResult(execution=execution, knowledge_entry=knowledge_entry)

    def _trial(
        self,
        hypothesis: GeneratedHypothesis,
        agenda: ResearchAgenda,
        governance_policy_id: str,
        phase: ResearchPhase,
        sample_universe_pair: tuple[frozenset[str], frozenset[str]],
        outcome_by_date: dict[str, Decimal],
        created_at: datetime,
    ) -> ResearchTrial | None:
        sample_dates, universe_dates = sample_universe_pair
        sample_sorted = sorted(d for d in sample_dates if d in outcome_by_date)
        universe_sorted = sorted(d for d in universe_dates if d in outcome_by_date)
        if len(sample_sorted) < MINIMUM_TRIAL_SAMPLE or len(universe_sorted) < MINIMUM_TRIAL_SAMPLE:
            return None
        sample_values = tuple(outcome_by_date[d] for d in sample_sorted)
        universe_values = tuple(outcome_by_date[d] for d in universe_sorted)
        sample_ids = tuple(
            (
                d,
                deterministic_id(
                    "cross_market_observation", (hypothesis.hypothesis_id, phase.value, d)
                ),
            )
            for d in sample_sorted
        )
        grouped_pairs = tuple((phase.value, value) for value in sample_values)
        configuration = ResearchConfiguration.create(
            method_id=CROSS_MARKET_METHOD_ID,
            method_version=hypothesis.template_version,
            parameter_values=(
                ("hypothesis_id", hypothesis.hypothesis_id),
                ("template_id", hypothesis.template_id.value),
                ("feature_id", hypothesis.feature_id.value),
                ("outcome_id", hypothesis.outcome_id.value),
                ("horizon_sessions", str(hypothesis.horizon_sessions)),
            ),
            origin=ConfigurationOrigin.FIXED_A_PRIORI,
            candidates_examined_count=1,
        )
        command = RecordGroupedResearchTrialCommand(
            hypothesis_version_id=hypothesis.hypothesis_id,
            dataset_snapshot_id=agenda.dataset_snapshot_id,
            replay_run_id=agenda.replay_run_id,
            phase=phase,
            market_scope=tuple(item.value for item in hypothesis.market_scope),
            configuration=configuration,
            sample_evaluation_ids=sample_ids,
            sample_values=sample_values,
            universe_values=universe_values,
            grouped_pairs=grouped_pairs,
            benchmark_kind=hypothesis.benchmark_kind,
            created_at=created_at,
            governance_policy_id=governance_policy_id,
            outcome_horizon_sessions=hypothesis.horizon_sessions,
        )
        try:
            return record_panel_sample_trial(
                repository=self._methodology_repository, command=command
            )
        except MethodologyConflict:
            raise
        except ResearchMethodologyApplicationError:
            return None

    def _stability(
        self,
        sample_universe: SampleUniverseFn,
        outcome_by_date: dict[str, Decimal],
    ) -> StabilityReport:
        subperiods = []
        for label, start, end, is_stress in self._timeline.quarters:
            period_dates = frozenset(d for d in self._timeline.all_dates if start <= d <= end)
            sample, _universe = sample_universe(period_dates)
            values = [outcome_by_date[d] for d in sample if d in outcome_by_date]
            mean = (
                None
                if len(values) < STABILITY_MINIMUM_QUARTER_SAMPLE
                else bound_statistical_decimal(sum(values, Decimal(0)) / Decimal(len(values)))
            )
            subperiods.append(SubperiodResult(label, start, end, len(values), mean, is_stress))
        recent_period = frozenset(self._timeline.holdout_dates)
        recent_sample, _universe = sample_universe(recent_period)
        recent_values = [outcome_by_date[d] for d in recent_sample if d in outcome_by_date]
        recent_mean = (
            None
            if len(recent_values) < STABILITY_MINIMUM_QUARTER_SAMPLE
            else bound_statistical_decimal(
                sum(recent_values, Decimal(0)) / Decimal(len(recent_values))
            )
        )
        recent = SubperiodResult(
            "RECENT_HOLDOUT",
            self._timeline.holdout_start,
            self._timeline.holdout_end,
            len(recent_values),
            recent_mean,
            False,
        )
        state = derive_stability_state(subperiods)
        return StabilityReport(
            subperiods=tuple(subperiods),
            recent_holdout=recent,
            state=state,
            evidence_note=f"{state.value}_ACROSS_{len(subperiods)}_EVIDENCE_SUBPERIODS",
        )

    def _robustness(
        self,
        sample_universe: SampleUniverseFn,
        outcome_by_date: dict[str, Decimal],
        base_trial: ResearchTrial,
        regime_by_date: dict[str, CrossMarketRegimeSnapshot],
        created_at: datetime,
    ) -> RobustnessCheckRun:
        base_mean = base_trial.diagnostics.base.mean
        if base_mean is None:
            raise CrossMarketResearchApplicationError("ROBUSTNESS_BASE_TRIAL_HAS_NO_MEAN")
        results: list[RobustnessCheckResult] = []

        def _mean_of(dates: frozenset[str]) -> tuple[int, Decimal | None]:
            values = [outcome_by_date[d] for d in dates if d in outcome_by_date]
            if len(values) < 5:
                return len(values), None
            return len(values), bound_statistical_decimal(
                sum(values, Decimal(0)) / Decimal(len(values))
            )

        for label, start, end, _stress in self._timeline.quarters:
            period_dates = frozenset(d for d in self._timeline.all_dates if start <= d <= end)
            sample, _universe = sample_universe(period_dates)
            count, mean = _mean_of(sample)
            results.append(
                RobustnessCheckResult(
                    RobustnessCheckType.SUBPERIOD_SPLIT,
                    label,
                    count,
                    mean,
                    _direction_consistent(base_mean, mean),
                    "OK" if mean is not None else "INSUFFICIENT_DATA",
                )
            )

        combined = frozenset(self._timeline.validation_dates) | frozenset(
            self._timeline.holdout_dates
        )
        sample_all, _universe = sample_universe(combined)
        for label in ("RISK_ON", "RISK_OFF", "NEUTRAL"):
            subset = frozenset(
                d
                for d in sample_all
                if d in regime_by_date
                and regime_by_date[d].label_for(CrossMarketRegimeDimension.EQUITY_RISK_TONE)
                == label
            )
            count, mean = _mean_of(subset)
            results.append(
                RobustnessCheckResult(
                    RobustnessCheckType.REGIME_SPLIT,
                    label,
                    count,
                    mean,
                    _direction_consistent(base_mean, mean),
                    "OK" if mean is not None else "INSUFFICIENT_DATA",
                )
            )

        all_frozenset = frozenset(self._timeline.all_dates)
        for label, start, end, _stress in self._timeline.quarters:
            excluded = frozenset(d for d in self._timeline.all_dates if start <= d <= end)
            sample, _universe = sample_universe(all_frozenset - excluded)
            count, mean = _mean_of(sample)
            results.append(
                RobustnessCheckResult(
                    RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
                    f"exclude_{label}",
                    count,
                    mean,
                    _direction_consistent(base_mean, mean),
                    "OK" if mean is not None else "INSUFFICIENT_DATA",
                )
            )

        strided = frozenset(d for index, d in enumerate(sorted(sample_all)) if index % 2 == 0)
        count, mean = _mean_of(strided)
        results.append(
            RobustnessCheckResult(
                RobustnessCheckType.MISSING_DATA_SENSITIVITY,
                "stride_2_subsample",
                count,
                mean,
                _direction_consistent(base_mean, mean),
                "OK" if mean is not None else "INSUFFICIENT_DATA",
            )
        )
        results.append(robustness_cost_stress())

        return RobustnessCheckRun.create(
            base_trial_id=base_trial.trial_id,
            results=tuple(results),
            created_at=created_at,
        )


def _latest_knowledge_entries(
    entries: Sequence[ResearchKnowledgeEntry],
) -> dict[str, ResearchKnowledgeEntry]:
    latest: dict[str, ResearchKnowledgeEntry] = {}
    for entry in entries:
        current = latest.get(entry.hypothesis_id)
        if current is None or entry.revision > current.revision:
            latest[entry.hypothesis_id] = entry
    return latest


# ==========================================================================
# Section: agenda-level multiple-testing correction, contradiction
# detection, follow-up proposals, and the research-quality report
# (sections 16, 18, 19, 21). Runs once, after every hypothesis in the
# agenda has completed section-9 execution -- never before, since it
# needs every family member's validation trial to exist first.
# ==========================================================================


class FinalizeAgenda:
    """Section 16: builds one ``ResearchTrialFamily`` per
    ``HypothesisFamily`` from every member's validation-phase trial, then
    rebuilds each hypothesis's conclusion *and* records a new, superseding
    knowledge-base revision reflecting the corrected view -- the original
    (uncorrected) revision 1 entry from ``RunSingleHypothesis`` is never
    edited or deleted, so the knowledge base retains both the raw and the
    corrected picture side by side (section 16: "make visible how quickly
    interesting findings disappear as methodology gets stricter")."""

    def __init__(
        self,
        *,
        methodology_repository: ResearchMethodologyRepository,
        repository: CrossMarketResearchRepository,
    ) -> None:
        self._methodology_repository = methodology_repository
        self._repository = repository

    def execute(
        self, *, agenda: ResearchAgenda, panel_warnings: tuple[str, ...], created_at: datetime
    ) -> tuple[ResearchKnowledgeEntry, ...]:
        hypotheses = self._repository.hypotheses_for_agenda(agenda.agenda_id)
        hypotheses_by_id = {item.hypothesis_id: item for item in hypotheses}
        executions = {
            item.hypothesis_id: item
            for item in self._repository.executions_for_agenda(agenda.agenda_id)
        }
        by_family: dict[HypothesisFamily, list[GeneratedHypothesis]] = {}
        for hypothesis in hypotheses:
            by_family.setdefault(hypothesis.family, []).append(hypothesis)

        revised_entries: list[ResearchKnowledgeEntry] = []
        for family, members in sorted(by_family.items(), key=lambda item: item[0].value):
            raw_results: list[TrialTestResult] = []
            for hypothesis in members:
                execution = executions.get(hypothesis.hypothesis_id)
                if execution is None or execution.validation_trial_id is None:
                    continue
                trial = self._methodology_repository.trial_by_id(execution.validation_trial_id)
                if (
                    trial is None
                    or trial.benchmark is None
                    or trial.diagnostics.base.mean is None
                    or trial.diagnostics.base.stdev is None
                ):
                    continue
                p_value = two_sided_z_test_p_value(
                    sample_mean=trial.diagnostics.base.mean,
                    benchmark_mean=trial.benchmark.benchmark_mean,
                    sample_stdev=trial.diagnostics.base.stdev,
                    effective_sample_count=trial.diagnostics.overlap.effective_sample_count,
                )
                raw_results.append(
                    TrialTestResult(trial.trial_id, hypothesis.hypothesis_id, p_value)
                )
            if not raw_results:
                continue
            family_record = BuildResearchTrialFamily(
                repository=self._methodology_repository
            ).execute(
                BuildTrialFamilyCommand(
                    hypothesis_id=family.value,
                    trial_ids=tuple(item.trial_id for item in raw_results),
                    candidate_count=len(raw_results),
                    asset_count=max(1, len({scope for h in members for scope in h.market_scope})),
                    outcome_definition_count=max(1, len({h.outcome_id for h in members})),
                    regime_slice_count=max(1, len({h.template_id for h in members})),
                    selection_method=TrialSelectionMethod.PREDECLARED_SINGLE,
                    correction_method=MultipleTestingCorrectionMethod.BENJAMINI_HOCHBERG_FDR,
                    created_at=created_at,
                )
            )
            for hypothesis in members:
                execution = executions.get(hypothesis.hypothesis_id)
                if execution is None:
                    continue
                new_conclusion = BuildResearchConclusion(
                    repository=self._methodology_repository
                ).execute(
                    BuildConclusionCommand(
                        hypothesis_version_id=hypothesis.hypothesis_id,
                        dataset_snapshot_id=agenda.dataset_snapshot_id,
                        replay_run_id=agenda.replay_run_id,
                        training_trial_id=execution.exploratory_trial_id,
                        validation_trial_id=execution.validation_trial_id,
                        holdout_trial_id=execution.holdout_trial_id,
                        walk_forward_run_id=None,
                        robustness_run_id=execution.robustness_run_id,
                        trial_family_id=family_record.trial_family_id,
                        point_in_time_limitations=LIMITATIONS,
                        rationale=f"{hypothesis.rationale} [multiple-testing corrected]",
                        created_at=created_at,
                    )
                )
                promotion = EvaluatePromotion(repository=self._methodology_repository).execute(
                    EvaluatePromotionCommand(
                        conclusion_id=new_conclusion.conclusion_id,
                        policy=PromotionPolicy(),
                        lookahead_violation=False,
                        created_at=created_at,
                    )
                )
                robustness_run = (
                    self._methodology_repository.robustness_run_by_id(execution.robustness_run_id)
                    if execution.robustness_run_id
                    else None
                )
                robustness_passed = robustness_run.summary.passed if robustness_run else False
                status = derive_knowledge_status(
                    new_conclusion.conclusion_state,
                    promotion_eligible=promotion.eligible,
                    robustness_passed=robustness_passed,
                )
                prior_entries = self._repository.knowledge_entries_for_hypothesis(
                    hypothesis.hypothesis_id
                )
                latest = max(prior_entries, key=lambda item: item.revision, default=None)
                new_entry = ResearchKnowledgeEntry.create(
                    hypothesis_id=hypothesis.hypothesis_id,
                    conclusion_id=new_conclusion.conclusion_id,
                    status=status,
                    evidence_strength=(
                        f"MULTIPLE_TESTING_CORRECTED_{new_conclusion.conclusion_state.value}_"
                        f"FAMILY_{family.value}_OF_{len(raw_results)}"
                    ),
                    phase_reached=new_conclusion.phase.value,
                    markets=hypothesis.market_scope,
                    regime_dependence=(
                        execution.stability.state.value
                        if execution.stability
                        else "INSUFFICIENT_EVIDENCE"
                    ),
                    benchmark_kind=hypothesis.benchmark_kind,
                    holdout_result=(
                        "NOT_EVALUATED"
                        if new_conclusion.beats_benchmark is None
                        else (
                            "BEATS_BENCHMARK"
                            if new_conclusion.beats_benchmark
                            else "DOES_NOT_BEAT_BENCHMARK"
                        )
                    ),
                    robustness_state=(
                        "PASSED"
                        if robustness_passed
                        else ("NOT_RUN" if robustness_run is None else "FAILED")
                    ),
                    cross_sectional_or_time_series=(
                        "TIME_SERIES_PANEL_WITH_SHARED_MARKET_DATES_NOT_INDEPENDENT_SAMPLES"
                    ),
                    limitations=LIMITATIONS,
                    revision=(latest.revision + 1 if latest is not None else 1),
                    supersedes_entry_id=(latest.entry_id if latest is not None else None),
                    created_at=created_at,
                )
                saved = self._repository.save_knowledge_entry(new_entry)
                revised_entries.append(saved)

        all_entries = self._repository.list_knowledge_entries()
        relevant_entries = tuple(
            item for item in all_entries if item.hypothesis_id in hypotheses_by_id
        )
        for contradiction in detect_contradictions(
            hypotheses_by_id=hypotheses_by_id,
            entries=relevant_entries,
            executions_by_hypothesis=executions,
            methodology_repository=self._methodology_repository,
            created_at=created_at,
        ):
            self._repository.save_contradiction(contradiction)
        for follow_up in propose_follow_ups(
            hypotheses_by_id=hypotheses_by_id,
            entries=relevant_entries,
            executions_by_hypothesis=executions,
            created_at=created_at,
        ):
            self._repository.save_follow_up(follow_up)

        BuildAgendaQualityReport(repository=self._repository).execute(
            agenda=agenda, panel_warnings=panel_warnings, created_at=created_at
        )
        return tuple(revised_entries)


def detect_contradictions(
    *,
    hypotheses_by_id: dict[str, GeneratedHypothesis],
    entries: Sequence[ResearchKnowledgeEntry],
    executions_by_hypothesis: dict[str, HypothesisExecutionRecord],
    methodology_repository: ResearchMethodologyRepository,
    created_at: datetime,
) -> tuple[ResearchContradiction, ...]:
    """Section 18: two genuine retained conclusions about the same
    outcome/horizon/overlapping-market pair, both non-rejected, whose
    reference-trial sample means carry opposite signs. Never silently
    averaged -- every pair found here is retained explicitly."""

    latest = _latest_knowledge_entries(entries)
    supported_like = {KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY, KnowledgeStatus.WEAK}
    candidates: list[tuple[str, ResearchKnowledgeEntry, GeneratedHypothesis, Decimal]] = []
    for hypothesis_id, entry in latest.items():
        if entry.status not in supported_like:
            continue
        hypothesis = hypotheses_by_id.get(hypothesis_id)
        execution = executions_by_hypothesis.get(hypothesis_id)
        if hypothesis is None or execution is None or hypothesis.is_negative_control:
            continue
        trial_id = execution.holdout_trial_id or execution.validation_trial_id
        if trial_id is None:
            continue
        trial = methodology_repository.trial_by_id(trial_id)
        if trial is None or trial.diagnostics.base.mean is None or trial.diagnostics.base.mean == 0:
            continue
        candidates.append((hypothesis_id, entry, hypothesis, trial.diagnostics.base.mean))

    results: list[ResearchContradiction] = []
    for first_index in range(len(candidates)):
        hid_a, entry_a, hyp_a, mean_a = candidates[first_index]
        for second_index in range(first_index + 1, len(candidates)):
            hid_b, entry_b, hyp_b, mean_b = candidates[second_index]
            if (
                hyp_a.outcome_id != hyp_b.outcome_id
                or hyp_a.horizon_sessions != hyp_b.horizon_sessions
            ):
                continue
            if not (set(hyp_a.market_scope) & set(hyp_b.market_scope)):
                continue
            if (mean_a > 0) == (mean_b > 0):
                continue
            results.append(
                ResearchContradiction.create(
                    first_entry_id=entry_a.entry_id,
                    second_entry_id=entry_b.entry_id,
                    shared_evidence=(hyp_a.outcome_id.value, str(hyp_a.horizon_sessions)),
                    first_holds_when=f"{hyp_a.template_id.value}:{hyp_a.feature_id.value}",
                    second_holds_when=f"{hyp_b.template_id.value}:{hyp_b.feature_id.value}",
                    description=(
                        f"{hid_a} implies a "
                        f"{'positive' if mean_a > 0 else 'negative'} effect on "
                        f"{hyp_a.outcome_id.value} over {hyp_a.horizon_sessions} sessions; "
                        f"{hid_b} implies the opposite sign over the same outcome, horizon, "
                        "and an overlapping market scope."
                    ),
                    created_at=created_at,
                )
            )
    return tuple(results)


def propose_follow_ups(
    *,
    hypotheses_by_id: dict[str, GeneratedHypothesis],
    entries: Sequence[ResearchKnowledgeEntry],
    executions_by_hypothesis: dict[str, HypothesisExecutionRecord],
    created_at: datetime,
) -> tuple[FollowUpProposal, ...]:
    """Section 19: bounded, rule-based follow-up proposals linked to a
    parent conclusion. Never auto-executed and never itself a new agenda
    -- each is a retained recommendation only."""

    latest = _latest_knowledge_entries(entries)
    proposals: list[FollowUpProposal] = []
    for hypothesis_id, entry in latest.items():
        hypothesis = hypotheses_by_id.get(hypothesis_id)
        execution = executions_by_hypothesis.get(hypothesis_id)
        if hypothesis is None or execution is None or hypothesis.is_negative_control:
            continue
        if (
            execution.stability is not None
            and execution.stability.state is StabilityState.REGIME_DEPENDENT
        ):
            proposals.append(
                FollowUpProposal.create(
                    parent_conclusion_id=entry.conclusion_id,
                    parent_hypothesis_id=hypothesis_id,
                    kind=FollowUpKind.SPLIT_BY_REGIME,
                    proposed_template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
                    description=(
                        f"{hypothesis_id} showed regime-dependent stability; propose "
                        "explicitly splitting by the regime dimension driving the split."
                    ),
                    created_at=created_at,
                )
            )
        if entry.robustness_state == "FAILED":
            proposals.append(
                FollowUpProposal.create(
                    parent_conclusion_id=entry.conclusion_id,
                    parent_hypothesis_id=hypothesis_id,
                    kind=FollowUpKind.INVESTIGATE_ROBUSTNESS_FAILURE,
                    proposed_template_id=hypothesis.template_id,
                    description=(
                        f"{hypothesis_id} failed one or more robustness checks; propose "
                        "investigating which specific perturbation drove the failure."
                    ),
                    created_at=created_at,
                )
            )
        if entry.status is KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY:
            proposals.append(
                FollowUpProposal.create(
                    parent_conclusion_id=entry.conclusion_id,
                    parent_hypothesis_id=hypothesis_id,
                    kind=FollowUpKind.ALTERNATE_HORIZON,
                    proposed_template_id=hypothesis.template_id,
                    description=(
                        f"{hypothesis_id} reached supported-for-further-study at horizon "
                        f"{hypothesis.horizon_sessions}; propose testing an adjacent horizon "
                        "under a new declared budget before broadening scope further."
                    ),
                    created_at=created_at,
                )
            )
    return tuple(proposals)


class BuildAgendaQualityReport:
    """Section 21: the one retained answer to "how much should we trust
    what this agenda found?" -- built from already-retained evidence
    only, never a fresh calculation of anything financial."""

    def __init__(self, *, repository: CrossMarketResearchRepository) -> None:
        self._repository = repository

    def execute(
        self, *, agenda: ResearchAgenda, panel_warnings: tuple[str, ...], created_at: datetime
    ) -> AgendaQualityReport:
        hypotheses = self._repository.hypotheses_for_agenda(agenda.agenda_id)
        hypothesis_ids = {item.hypothesis_id for item in hypotheses}
        entries = tuple(
            item
            for item in self._repository.list_knowledge_entries()
            if item.hypothesis_id in hypothesis_ids
        )
        latest = _latest_knowledge_entries(entries)
        distribution: dict[str, int] = {}
        for entry in latest.values():
            distribution[entry.status.value] = distribution.get(entry.status.value, 0) + 1
        executions = self._repository.executions_for_agenda(agenda.agenda_id)
        trials_run = sum(
            1
            for execution in executions
            for trial_id in (
                execution.exploratory_trial_id,
                execution.validation_trial_id,
                execution.holdout_trial_id,
            )
            if trial_id is not None
        ) + sum(len(execution.walk_forward_trial_ids) for execution in executions)
        negative_control_count = sum(1 for item in hypotheses if item.is_negative_control)
        negative_control_supported = sum(
            1
            for item in hypotheses
            if item.is_negative_control
            and latest.get(item.hypothesis_id) is not None
            and latest[item.hypothesis_id].status
            in (KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY, KnowledgeStatus.WEAK)
        )
        report = AgendaQualityReport.create(
            agenda_id=agenda.agenda_id,
            dataset_limitations=LIMITATIONS,
            provider_coverage_limitations=(panel_warnings or ("NONE_OBSERVED",)),
            panel_dependence_note=(
                "Every trial's sample rows share a common panel of market dates; "
                "session-date rows are pooled time-series/panel observations, not "
                "independent samples. Overlap/effective-sample accounting and the "
                "chosen horizon's stride bound how many effectively independent "
                "observations a trial's inference relies on -- see each trial's own "
                "retained overlap diagnostics."
            ),
            multiple_testing_burden_note=(
                f"{len(hypotheses)} hypotheses were partitioned into "
                f"{len({item.family for item in hypotheses})} multiple-testing families "
                "(clearly partitioned by HypothesisFamily); Benjamini-Hochberg FDR "
                "correction was applied within each family using every member's "
                "validation-phase trial."
            ),
            negative_control_behavior_note=(
                f"{negative_control_count} negative controls were included; "
                f"{negative_control_supported} of {negative_control_count} reached a "
                "WEAK or SUPPORTED_FOR_FURTHER_STUDY knowledge status. A nonzero count "
                "here is a genuine methodology-sensitivity signal, not evidence of any "
                "real relationship, and is reported rather than concealed."
            ),
            holdout_integrity_note=(
                "One shared, locked holdout window was declared before any holdout-phase "
                "trial was recorded; every hypothesis's holdout exposure event is retained "
                "and precedes its own holdout trial for that hypothesis."
            ),
            conclusion_distribution=tuple(sorted(distribution.items())),
            hypotheses_proposed=len(hypotheses),
            trials_run=trials_run,
            created_at=created_at,
        )
        existing = self._repository.quality_report_by_agenda(agenda.agenda_id)
        if existing is not None:
            return existing
        return self._repository.save_quality_report(report)


# ==========================================================================
# Section: the bounded mutable agenda-run cursor (start/resume/inspect).
# Registering an agenda's hypotheses and starting/resuming its run are
# kept as separate steps so "research-agenda create/inspect" and
# "hypothesis-proposals generate/list" can be exercised independently of
# actually running anything.
# ==========================================================================


class RegisterAgendaRun:
    def __init__(self, *, repository: CrossMarketResearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        agenda: ResearchAgenda,
        hypotheses: Sequence[GeneratedHypothesis],
        created_at: datetime,
    ) -> AgendaRun:
        draft = AgendaRun.draft(
            agenda=agenda,
            hypothesis_ids=tuple(item.hypothesis_id for item in hypotheses),
            created_at=created_at,
        )
        existing = self._repository.agenda_run_by_agenda(agenda.agenda_id)
        if existing is not None:
            if existing.canonical_input_hash != draft.canonical_input_hash:
                raise CrossMarketResearchConflict("AGENDA_RUN_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_agenda_run(draft)


class AdvanceAgendaRun:
    """The one "start"/"resume" engine (they are the identical operation:
    process up to ``maximum_hypotheses`` pending hypotheses starting from
    the run's own cursor, which is exactly ``0`` the first time). Once the
    cursor reaches the end, automatically runs ``FinalizeAgenda`` in the
    same call -- resuming a completed run is then a safe no-op."""

    def __init__(
        self,
        *,
        repository: CrossMarketResearchRepository,
        methodology_repository: ResearchMethodologyRepository,
        timeline: AgendaTimeline,
        panel_warnings: tuple[str, ...] = (),
    ) -> None:
        self._repository = repository
        self._methodology_repository = methodology_repository
        self._hypothesis_runner = RunSingleHypothesis(
            methodology_repository=methodology_repository, timeline=timeline
        )
        self._finalizer = FinalizeAgenda(
            methodology_repository=methodology_repository, repository=repository
        )
        self._panel_warnings = panel_warnings

    def execute(
        self,
        *,
        agenda: ResearchAgenda,
        panel: CrossMarketPanel,
        maximum_hypotheses: int,
        created_at: datetime,
    ) -> AgendaRun:
        if maximum_hypotheses < 1:
            raise CrossMarketResearchApplicationError("AGENDA_RUN_MAXIMUM_HYPOTHESES_INVALID")
        run = self._repository.agenda_run_by_agenda(agenda.agenda_id)
        if run is None:
            raise CrossMarketResearchNotFound("AGENDA_RUN_NOT_REGISTERED")
        if run.status is AgendaRunStatus.COMPLETE:
            return run
        if run.status is AgendaRunStatus.FAILED:
            raise CrossMarketResearchApplicationError("AGENDA_RUN_FAILED_CANNOT_ADVANCE")
        starting_cursor = run.cursor_ordinal
        pending = run.hypothesis_ids[starting_cursor:]
        try:
            for offset, hypothesis_id in enumerate(pending[:maximum_hypotheses]):
                hypothesis = self._repository.hypothesis_by_id(hypothesis_id)
                if hypothesis is None:
                    raise CrossMarketResearchNotFound(f"HYPOTHESIS_NOT_FOUND:{hypothesis_id}")
                result = self._hypothesis_runner.execute(
                    agenda=agenda, hypothesis=hypothesis, panel=panel, created_at=created_at
                )
                self._repository.save_execution(result.execution)
                self._repository.save_knowledge_entry(result.knowledge_entry)
                trials_added = sum(
                    1
                    for trial_id in (
                        result.execution.exploratory_trial_id,
                        result.execution.validation_trial_id,
                        result.execution.holdout_trial_id,
                    )
                    if trial_id is not None
                ) + len(result.execution.walk_forward_trial_ids)
                is_final = (starting_cursor + offset + 1) == len(run.hypothesis_ids)
                run = run.advance(
                    hypothesis_id=hypothesis_id,
                    trials_added=trials_added,
                    is_final=is_final,
                    at=created_at,
                )
                run = self._repository.save_agenda_run(run)
        except CrossMarketResearchApplicationError as error:
            run = self._repository.save_agenda_run(run.fail(reason=str(error), at=created_at))
            raise
        if run.status is AgendaRunStatus.COMPLETE:
            self._finalizer.execute(
                agenda=agenda, panel_warnings=self._panel_warnings, created_at=created_at
            )
        return run


# ==========================================================================
# In-memory repository (fast application-layer tests; no persistence
# concern lives here)
# ==========================================================================


class InMemoryCrossMarketResearchRepository:
    def __init__(self) -> None:
        self._agendas: dict[str, ResearchAgenda] = {}
        self._hypotheses: dict[str, GeneratedHypothesis] = {}
        self._agenda_runs: dict[str, AgendaRun] = {}
        self._executions: dict[str, HypothesisExecutionRecord] = {}
        self._knowledge_entries: dict[str, ResearchKnowledgeEntry] = {}
        self._contradictions: dict[str, ResearchContradiction] = {}
        self._follow_ups: dict[str, FollowUpProposal] = {}
        self._quality_reports: dict[str, AgendaQualityReport] = {}

    def agenda_by_id(self, agenda_id: str) -> ResearchAgenda | None:
        return self._agendas.get(agenda_id)

    def save_agenda(self, agenda: ResearchAgenda) -> ResearchAgenda:
        self._agendas.setdefault(agenda.agenda_id, agenda)
        return self._agendas[agenda.agenda_id]

    def list_agendas(self) -> tuple[ResearchAgenda, ...]:
        return tuple(sorted(self._agendas.values(), key=lambda item: item.registered_at))

    def hypothesis_by_id(self, hypothesis_id: str) -> GeneratedHypothesis | None:
        return self._hypotheses.get(hypothesis_id)

    def save_hypothesis(self, hypothesis: GeneratedHypothesis) -> GeneratedHypothesis:
        self._hypotheses.setdefault(hypothesis.hypothesis_id, hypothesis)
        return self._hypotheses[hypothesis.hypothesis_id]

    def hypotheses_for_agenda(self, agenda_id: str) -> tuple[GeneratedHypothesis, ...]:
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
        self._executions[record.hypothesis_id] = record
        return record

    def executions_for_agenda(self, agenda_id: str) -> tuple[HypothesisExecutionRecord, ...]:
        return tuple(item for item in self._executions.values() if item.agenda_id == agenda_id)

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
        self._knowledge_entries[entry.entry_id] = entry
        return entry

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
        return self._quality_reports.get(agenda_id)

    def save_quality_report(self, report: AgendaQualityReport) -> AgendaQualityReport:
        self._quality_reports[report.agenda_id] = report
        return report
