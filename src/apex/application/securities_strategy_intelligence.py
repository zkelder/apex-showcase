"""Build immutable, research-only strategy-intelligence evidence from genuine data."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Protocol

from apex.application.securities_strategy_lab_dataset import (
    SecuritiesDailyDirectionDataset,
    SecuritiesDatasetEvidencePort,
)
from apex.application.strategy_lab import StrategyLabArtifact, StrategyLabArtifactStore
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
)
from apex.domain.strategy_intelligence import (
    SECURITIES_STRATEGY_INTELLIGENCE_FEATURES_V1,
    ExpectedOutcome,
    ExpectedOutcomePolicy,
    MarketRegimePolicy,
    NormalizedForecast,
    OpportunityRankingPolicy,
    RankedOpportunity,
    RegimeSnapshot,
    ResearchActionPolicy,
    ResearchDecision,
    StrategyIntelligenceFeatures,
    clamp,
    mean,
    q,
    stddev,
)
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id, parse_decimal


class StrategyIntelligenceApplicationError(RuntimeError):
    """The immutable genuine-evidence intelligence run cannot be built."""


@dataclass(frozen=True, slots=True)
class StrategyIntelligenceEvaluation:
    partition: str
    observation_count: int
    action_coverage: Decimal
    abstention_rate: Decimal
    realized_gross_return: Decimal
    realized_net_return: Decimal
    downside_when_acting: Decimal
    maximum_drawdown: Decimal
    turnover: Decimal
    cost_burden: Decimal
    false_positive_rate: Decimal
    regime_metrics: tuple[tuple[str, Decimal], ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class SecuritiesStrategyIntelligenceRun:
    run_id: str
    dataset_id: str
    raw_batch_id: str
    derived_batch_id: str
    feature_policy_id: str
    regime_policy_hash: str
    outcome_policy_hash: str
    action_policy_hash: str
    ranking_policy_hash: str
    model_integrity_state: str
    simulator_conformance_state: str
    blocking_reasons: tuple[str, ...]
    features_artifact: StrategyLabArtifact
    regimes_artifact: StrategyLabArtifact
    forecasts_artifact: StrategyLabArtifact
    outcomes_artifact: StrategyLabArtifact
    decisions_artifact: StrategyLabArtifact
    ranking_artifact: StrategyLabArtifact
    evaluation_artifact: StrategyLabArtifact
    latest_snapshot_artifact: StrategyLabArtifact
    canonical_input_hash: str
    canonical_result_hash: str
    idempotency_key: str
    created_at: datetime


class StrategyIntelligenceRepository(Protocol):
    def run_by_idempotency(self, key: str) -> SecuritiesStrategyIntelligenceRun | None: ...

    def save_run(
        self, run: SecuritiesStrategyIntelligenceRun
    ) -> SecuritiesStrategyIntelligenceRun: ...

    def runs(self) -> tuple[SecuritiesStrategyIntelligenceRun, ...]: ...


class InMemoryStrategyIntelligenceRepository:
    def __init__(self) -> None:
        self._items: dict[str, SecuritiesStrategyIntelligenceRun] = {}

    def run_by_idempotency(self, key: str) -> SecuritiesStrategyIntelligenceRun | None:
        return next((item for item in self._items.values() if item.idempotency_key == key), None)

    def save_run(self, run: SecuritiesStrategyIntelligenceRun) -> SecuritiesStrategyIntelligenceRun:
        prior = self._items.get(run.run_id)
        if prior is not None and prior.canonical_result_hash != run.canonical_result_hash:
            raise StrategyIntelligenceApplicationError("STRATEGY_INTELLIGENCE_IMMUTABLE")
        self._items[run.run_id] = run
        return run

    def runs(self) -> tuple[SecuritiesStrategyIntelligenceRun, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: (item.created_at, item.run_id)))


class BuildGenuineSecuritiesStrategyIntelligence:
    """One deterministic analysis run, never an order or deployment boundary."""

    def __init__(
        self,
        *,
        evidence: SecuritiesDatasetEvidencePort,
        artifacts: StrategyLabArtifactStore,
        repository: StrategyIntelligenceRepository,
        regime_policy: MarketRegimePolicy | None = None,
        outcome_policy: ExpectedOutcomePolicy | None = None,
        action_policy: ResearchActionPolicy | None = None,
        ranking_policy: OpportunityRankingPolicy | None = None,
    ) -> None:
        self._evidence = evidence
        self._artifacts = artifacts
        self._repository = repository
        self._regime = regime_policy or MarketRegimePolicy()
        self._outcome = outcome_policy or ExpectedOutcomePolicy()
        self._action = action_policy or ResearchActionPolicy()
        self._ranking = ranking_policy or OpportunityRankingPolicy()

    def execute(
        self,
        *,
        dataset: SecuritiesDailyDirectionDataset,
        idempotency_key: str,
        model_probabilities: Mapping[tuple[str, str], Mapping[str, Decimal]] | None = None,
        model_integrity_state: str = "BLOCKED_INTEGRITY",
        simulator_conformance_state: str = "MATERIAL_MISMATCH",
        blocking_reasons: Sequence[str] = ("PBO_ABOVE_MAXIMUM", "MATERIAL_SIMULATOR_MISMATCH"),
    ) -> SecuritiesStrategyIntelligenceRun:
        if not idempotency_key:
            raise StrategyIntelligenceApplicationError("STRATEGY_INTELLIGENCE_IDEMPOTENCY_REQUIRED")
        input_payload = {
            "dataset": dataset.canonical_result_hash,
            "feature_policy": SECURITIES_STRATEGY_INTELLIGENCE_FEATURES_V1,
            "regime": self._regime.canonical_hash,
            "outcome": self._outcome.canonical_hash,
            "action": self._action.canonical_hash,
            "ranking": self._ranking.canonical_hash,
            "models": tuple(sorted((model_probabilities or {}).keys())),
            "blockers": tuple(sorted(blocking_reasons)),
            "key": idempotency_key,
        }
        input_hash = content_digest(input_payload)
        prior = self._repository.run_by_idempotency(idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise StrategyIntelligenceApplicationError(
                    "STRATEGY_INTELLIGENCE_IDEMPOTENCY_CONFLICT"
                )
            return prior
        _instruments, sessions, bars, _actions = self._evidence.get_dataset(dataset.raw_batch_id)
        features, regime_inputs = _build_features(sessions=sessions, bars=bars, dataset=dataset)
        regimes = tuple(
            self._regime.classify(
                session_id=session_id, measurements=measurements, source_lineage_hash=lineage
            )
            for session_id, measurements, lineage in regime_inputs
        )
        regime_by_session = {item.decision_session_id: item for item in regimes}
        returns = _returns(self._artifacts, dataset.returns_artifact)
        forecasts = _forecasts(
            features=features,
            regimes=regime_by_session,
            provided=model_probabilities or {},
            model_integrity_state=model_integrity_state,
            simulator_conformance_state=simulator_conformance_state,
        )
        outcomes = tuple(
            self._outcome.estimate(
                forecast=forecast,
                prior_returns=_prior_returns(
                    returns, forecast.instrument_id, forecast.decision_session_id
                ),
                source_lineage_hash=forecast.source_lineage_hash,
            )
            for forecast in forecasts
        )
        decisions = tuple(
            self._action.decide(
                outcome=outcome,
                regime=regime_by_session[outcome.decision_session_id],
                agreement=_agreement(
                    next(
                        item
                        for item in forecasts
                        if item.instrument_id == outcome.instrument_id
                        and item.decision_session_id == outcome.decision_session_id
                    )
                ),
                evidence_quality=_quality(
                    next(
                        item
                        for item in features
                        if item.instrument_id == outcome.instrument_id
                        and item.decision_session_id == outcome.decision_session_id
                    )
                ),
                blocked=True,
                current_exposure=Decimal(),
                lineage=outcome.source_lineage_hash,
            )
            for outcome in outcomes
        )
        rankings = _rank_by_session(outcomes, decisions, self._ranking)
        evaluations = _evaluate(
            dataset=dataset,
            outcomes=outcomes,
            decisions=decisions,
            regimes=regime_by_session,
            returns=returns,
        )
        base = content_digest({"input": input_hash, "dataset": dataset.dataset_id})
        artifact_base = f"strategy-intelligence/{base}"
        feature_artifact = _write_jsonl(
            self._artifacts, f"{artifact_base}/features.jsonl", (asdict(item) for item in features)
        )
        regime_artifact = _write_jsonl(
            self._artifacts, f"{artifact_base}/regimes.jsonl", (asdict(item) for item in regimes)
        )
        forecast_artifact = _write_jsonl(
            self._artifacts,
            f"{artifact_base}/forecasts.jsonl",
            (asdict(item) for item in forecasts),
        )
        outcome_artifact = _write_jsonl(
            self._artifacts, f"{artifact_base}/outcomes.jsonl", (asdict(item) for item in outcomes)
        )
        decision_artifact = _write_jsonl(
            self._artifacts,
            f"{artifact_base}/decisions.jsonl",
            (asdict(item) for item in decisions),
        )
        ranking_artifact = _write_jsonl(
            self._artifacts, f"{artifact_base}/rankings.jsonl", (asdict(item) for item in rankings)
        )
        evaluation_artifact = _write_json(
            self._artifacts,
            f"{artifact_base}/evaluation.json",
            tuple(asdict(item) for item in evaluations),
        )
        latest_session = max(item.decision_session_id for item in regimes)
        latest_snapshot = {
            "label": "HISTORICAL RESEARCH SNAPSHOT — NOT A CURRENT MARKET RECOMMENDATION",
            "session_id": latest_session,
            "regime": asdict(regime_by_session[latest_session]),
            "opportunities": tuple(
                asdict(item) for item in rankings if item.decision_session_id == latest_session
            ),
            "warnings": (
                "STRATEGY INTELLIGENCE IS RESEARCH ONLY",
                "ABSTENTION IS A VALID DECISION",
                "GENUINE HISTORICAL PERFORMANCE DOES NOT ESTABLISH A TRADING EDGE",
            ),
        }
        snapshot_artifact = _write_json(
            self._artifacts, f"{artifact_base}/latest-snapshot.json", latest_snapshot
        )
        result_payload = {
            "features": feature_artifact.sha256,
            "regimes": regime_artifact.sha256,
            "forecasts": forecast_artifact.sha256,
            "outcomes": outcome_artifact.sha256,
            "decisions": decision_artifact.sha256,
            "rankings": ranking_artifact.sha256,
            "evaluation": evaluation_artifact.sha256,
            "snapshot": snapshot_artifact.sha256,
        }
        run = SecuritiesStrategyIntelligenceRun(
            run_id=deterministic_id("securities_strategy_intelligence_run", {"input": input_hash}),
            dataset_id=dataset.dataset_id,
            raw_batch_id=dataset.raw_batch_id,
            derived_batch_id=dataset.derived_batch_id,
            feature_policy_id=SECURITIES_STRATEGY_INTELLIGENCE_FEATURES_V1,
            regime_policy_hash=self._regime.canonical_hash,
            outcome_policy_hash=self._outcome.canonical_hash,
            action_policy_hash=self._action.canonical_hash,
            ranking_policy_hash=self._ranking.canonical_hash,
            model_integrity_state=model_integrity_state,
            simulator_conformance_state=simulator_conformance_state,
            blocking_reasons=tuple(sorted(set(blocking_reasons))),
            features_artifact=feature_artifact,
            regimes_artifact=regime_artifact,
            forecasts_artifact=forecast_artifact,
            outcomes_artifact=outcome_artifact,
            decisions_artifact=decision_artifact,
            ranking_artifact=ranking_artifact,
            evaluation_artifact=evaluation_artifact,
            latest_snapshot_artifact=snapshot_artifact,
            canonical_input_hash=input_hash,
            canonical_result_hash=content_digest(result_payload),
            idempotency_key=idempotency_key,
            created_at=datetime.now(tz=UTC),
        )
        return self._repository.save_run(run)


def build_forward_strategy_intelligence_rows(
    *,
    dataset: SecuritiesDailyDirectionDataset,
    historical_sessions: Sequence[SecuritiesCalendarSession],
    historical_bars: Sequence[SecuritiesBarEvidence],
    captured_sessions: Sequence[SecuritiesCalendarSession],
    captured_bars: Sequence[SecuritiesBarEvidence],
    target_session_id: str,
) -> tuple[tuple[str, Decimal, Decimal, Decimal, str], ...]:
    """Evaluate one retained session with the frozen transparent policies.

    The caller supplies separate immutable historical and newly-received
    evidence.  We deduplicate by exact session/bar identity and select only
    the requested completed session, so later revisions cannot rewrite an
    earlier prediction.
    """
    sessions = {item.session_id: item for item in (*historical_sessions, *captured_sessions)}
    bars = {item.bar_id: item for item in (*historical_bars, *captured_bars)}
    features, regime_inputs = _build_features(
        sessions=tuple(sessions.values()), bars=tuple(bars.values()), dataset=dataset
    )
    regime = MarketRegimePolicy()
    regimes = {
        session_id: regime.classify(
            session_id=session_id, measurements=measurements, source_lineage_hash=lineage
        )
        for session_id, measurements, lineage in regime_inputs
    }
    selected = tuple(item for item in features if item.decision_session_id == target_session_id)
    if len(selected) != dataset.instrument_count or target_session_id not in regimes:
        raise StrategyIntelligenceApplicationError("FORWARD_FEATURE_WINDOW_INCOMPLETE")
    forecasts = _forecasts(
        features=selected,
        regimes=regimes,
        provided={},
        model_integrity_state="BLOCKED_INTEGRITY",
        simulator_conformance_state="MATERIAL_MISMATCH",
    )
    outcome = ExpectedOutcomePolicy()
    close_history: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for bar in bars.values():
        if bar.adjustment_state is SecuritiesAdjustmentState.RAW:
            close_history[bar.instrument_id].append((bar.period_end, bar.close))
    prior_returns = {
        instrument: tuple(
            _ret(values[index][1], values[index - 1][1]) for index in range(1, len(values))
        )
        for instrument, values in (
            (key, sorted(value, key=lambda item: item[0])) for key, value in close_history.items()
        )
    }
    rows: list[tuple[str, Decimal, Decimal, Decimal, str]] = []
    for forecast in forecasts:
        estimated = outcome.estimate(
            forecast=forecast,
            prior_returns=prior_returns.get(forecast.instrument_id, ()),
            source_lineage_hash=forecast.source_lineage_hash,
        )
        if (
            forecast.calibrated_probability is None
            or estimated.expected_net_return is None
            or estimated.uncertainty is None
        ):
            raise StrategyIntelligenceApplicationError("FORWARD_OUTCOME_UNAVAILABLE")
        rows.append(
            (
                forecast.instrument_id,
                forecast.calibrated_probability,
                estimated.expected_net_return,
                estimated.uncertainty,
                next(
                    item.feature_hash
                    for item in selected
                    if item.instrument_id == forecast.instrument_id
                ),
            )
        )
    return tuple(rows)


def _build_features(
    *,
    sessions: Sequence[SecuritiesCalendarSession],
    bars: Sequence[SecuritiesBarEvidence],
    dataset: SecuritiesDailyDirectionDataset,
) -> tuple[
    tuple[StrategyIntelligenceFeatures, ...], tuple[tuple[str, dict[str, Decimal], str], ...]
]:
    ordered = tuple(sorted(sessions, key=lambda item: (item.opens_at, item.session_id)))
    raw = {
        (item.instrument_id, item.period_end.date().isoformat()): item
        for item in bars
        if item.adjustment_state is SecuritiesAdjustmentState.RAW
    }
    instruments = tuple(
        sorted(
            {
                item.instrument_id
                for item in bars
                if item.adjustment_state is SecuritiesAdjustmentState.RAW
            }
        )
    )
    if len(instruments) != dataset.instrument_count:
        raise StrategyIntelligenceApplicationError("STRATEGY_INTELLIGENCE_UNIVERSE_MISMATCH")
    features: list[StrategyIntelligenceFeatures] = []
    regimes: list[tuple[str, dict[str, Decimal], str]] = []
    previous_correlation: Decimal | None = None
    for ordinal, session in enumerate(ordered):
        if ordinal < 60:
            continue
        window = ordered[ordinal - 60 : ordinal + 1]
        series = {
            instrument: tuple(raw.get((instrument, item.session_date)) for item in window)
            for instrument in instruments
        }
        if any(any(value is None for value in values) for values in series.values()):
            continue
        closes = {
            instrument: tuple(item.close for item in values if item is not None)
            for instrument, values in series.items()
        }
        return20 = {
            instrument: _ret(values[-1], values[-21]) for instrument, values in closes.items()
        }
        return60 = {
            instrument: _ret(values[-1], values[0]) for instrument, values in closes.items()
        }
        vol20 = {instrument: _vol(values[-21:]) for instrument, values in closes.items()}
        vol60 = {instrument: _vol(values) for instrument, values in closes.items()}
        ranks20 = _ranks(return20)
        ranks60 = _ranks(return60)
        vol_ranks = _ranks(vol20)
        average20 = mean(tuple(return20.values()))
        pairwise_correlations = [
            _corr(_daily(values[-21:]), _daily(other[-21:]))
            for index, values in enumerate(closes.values())
            for other in tuple(closes.values())[index + 1 :]
        ]
        avg_correlation = mean(tuple(pairwise_correlations)) if pairwise_correlations else Decimal()
        correlation_change = (
            Decimal() if previous_correlation is None else q(avg_correlation - previous_correlation)
        )
        previous_correlation = avg_correlation
        breadth20 = q(
            Decimal(sum(value > 0 for value in return20.values())) / Decimal(len(instruments))
        )
        breadth60 = q(
            Decimal(sum(value > 0 for value in return60.values())) / Decimal(len(instruments))
        )
        above_mean = q(
            Decimal(sum(values[-1] > mean(values[-21:]) for values in closes.values()))
            / Decimal(len(instruments))
        )
        measurements = {
            "broad_trend_60": average20
            if "SPY" not in instruments
            else return60.get("SPY", average20),
            "breadth_20": breadth20,
            "volatility_20": mean(tuple(vol20.values())),
            "drawdown_60": mean(tuple(_drawdown(values) for values in closes.values())),
            "average_correlation": avg_correlation,
            "correlation_change": correlation_change,
            "return_dispersion": stddev(tuple(return20.values())),
        }
        session_lineage = content_digest(
            {
                "session": session.content_hash,
                "bars": tuple(
                    item.content_hash
                    for values in series.values()
                    for item in values
                    if item is not None
                ),
                "policy": SECURITIES_STRATEGY_INTELLIGENCE_FEATURES_V1,
            }
        )
        regimes.append((session.session_id, measurements, session_lineage))
        equal_daily = tuple(
            mean(tuple(_daily(values[-21:])[index] for values in closes.values()))
            for index in range(20)
        )
        for instrument in instruments:
            values = closes[instrument]
            source = tuple(item for item in series[instrument] if item is not None)
            peer_residual = q(
                _ret(values[-1], values[-6])
                - mean(tuple(_ret(other[-1], other[-6]) for other in closes.values()))
            )
            spy_series = closes.get("SPY")
            item_daily = _daily(values[-21:])
            state = {
                "trailing_return_1": _ret(values[-1], values[-2]),
                "trailing_return_5": _ret(values[-1], values[-6]),
                "trailing_return_20": return20[instrument],
                "trailing_return_60": return60[instrument],
                "trend_agreement_20_60": Decimal(
                    1 if return20[instrument] * return60[instrument] > 0 else 0
                ),
                "distance_mean_20": q(values[-1] / mean(values[-21:]) - 1),
                "distance_mean_60": q(values[-1] / mean(values) - 1),
                "reversal_residual_5": peer_residual,
                "reversal_rank": q(1 - ranks20[instrument]),
                "volatility_20": vol20[instrument],
                "volatility_60": vol60[instrument],
                "volatility_ratio": q(
                    vol20[instrument] / max(vol60[instrument], Decimal("0.00000001"))
                ),
                "drawdown_20": _drawdown(values[-21:]),
                "drawdown_60": _drawdown(values),
                "current_drawdown": _drawdown(values),
                "relative_equal_weight_20": q(return20[instrument] - average20),
                "cross_sectional_return_rank_20": ranks20[instrument],
                "cross_sectional_return_rank_60": ranks60[instrument],
                "cross_sectional_volatility_rank": vol_ranks[instrument],
                "breadth_positive_20": breadth20,
                "breadth_positive_60": breadth60,
                "breadth_above_mean_20": above_mean,
                "return_dispersion": stddev(tuple(return20.values())),
                "volatility_dispersion": stddev(tuple(vol20.values())),
                "average_pairwise_correlation": avg_correlation,
                "correlation_change": correlation_change,
                "correlation_equal_weight": _corr(item_daily, equal_daily),
                "complete_universe_coverage": Decimal(1),
                "feature_completeness": Decimal(1),
                "stale_or_missing_evidence": Decimal(0),
                "quality_warning_count": Decimal(2),
                "current_identity_limited": Decimal(1),
                "fixed_universe_limited": Decimal(1),
            }
            if spy_series is not None:
                state["relative_spy_20"] = q(return20[instrument] - return20["SPY"])
                state["correlation_spy"] = _corr(item_daily, _daily(spy_series[-21:]))
            lineage = content_digest(
                {
                    "session": session_lineage,
                    "instrument": instrument,
                    "bars": tuple(item.content_hash for item in source),
                }
            )
            payload = {
                "instrument": instrument,
                "session": session.session_id,
                "values": tuple(sorted(state.items())),
                "lineage": lineage,
            }
            features.append(
                StrategyIntelligenceFeatures(
                    instrument,
                    session.session_id,
                    session.session_date,
                    max(item.available_at for item in source).astimezone(UTC).isoformat(),
                    tuple(sorted(state.items())),
                    tuple(item.bar_id for item in source),
                    lineage,
                    content_digest(payload),
                    2,
                    True,
                    True,
                )
            )
    if not features or not regimes:
        raise StrategyIntelligenceApplicationError("STRATEGY_INTELLIGENCE_NO_ELIGIBLE_FEATURES")
    return tuple(features), tuple(regimes)


def _forecasts(
    *,
    features: Sequence[StrategyIntelligenceFeatures],
    regimes: Mapping[str, RegimeSnapshot],
    provided: Mapping[tuple[str, str], Mapping[str, Decimal]],
    model_integrity_state: str,
    simulator_conformance_state: str,
) -> tuple[NormalizedForecast, ...]:
    result = []
    for feature in features:
        values = dict(feature.values)
        momentum = clamp(
            Decimal("0.5")
            + values["trailing_return_20"] * Decimal(3)
            + values["trailing_return_60"] * Decimal(2)
        )
        reversal = clamp(
            Decimal("0.5")
            - values["trailing_return_1"] * Decimal(3)
            - values["reversal_residual_5"] * Decimal(2)
        )
        volatility = clamp(
            Decimal("0.5")
            + values["trailing_return_20"] * Decimal(2)
            - values["volatility_20"] * Decimal(4)
        )
        relative = clamp(Decimal("0.5") + values["relative_equal_weight_20"] * Decimal(3))
        components: dict[str, Decimal] = {
            "canonical_momentum": momentum,
            "canonical_mean_reversion": reversal,
            "volatility_targeted": volatility,
            "market_relative": relative,
        }
        components.update(provided.get((feature.instrument_id, feature.decision_session_id), {}))
        baseline = mean(tuple(components.values()))
        calibrated = clamp(
            baseline
            * (Decimal("0.75") + regimes[feature.decision_session_id].confidence * Decimal("0.25"))
        )
        lineage = content_digest(
            {
                "feature": feature.feature_hash,
                "components": tuple(sorted(components.items())),
                "regime": regimes[feature.decision_session_id].canonical_hash,
            }
        )
        result.append(
            NormalizedForecast(
                feature.instrument_id,
                feature.decision_session_id,
                tuple(sorted(components.items())),
                calibrated,
                baseline,
                model_integrity_state,
                simulator_conformance_state,
                True,
                lineage,
                content_digest({"lineage": lineage, "calibrated": calibrated}),
            )
        )
    return tuple(result)


def _returns(
    store: StrategyLabArtifactStore, artifact: StrategyLabArtifact
) -> dict[str, list[tuple[str, Decimal]]]:
    try:
        values = [json.loads(line) for line in store.read_bytes(artifact).splitlines()]
        result: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
        for item in values:
            result[str(item["instrument_id"])].append(
                (
                    str(item["decision_session_id"]),
                    parse_decimal(item["realized_total_return"], allow_negative=True),
                )
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise StrategyIntelligenceApplicationError(
            "STRATEGY_INTELLIGENCE_RETURN_LINEAGE_INVALID"
        ) from error
    return {key: sorted(value) for key, value in result.items()}


def _prior_returns(
    values: Mapping[str, Sequence[tuple[str, Decimal]]], instrument: str, session: str
) -> tuple[Decimal, ...]:
    return tuple(
        value for prior_session, value in values.get(instrument, ()) if prior_session < session
    )


def _agreement(forecast: NormalizedForecast) -> Decimal:
    return clamp(
        Decimal(1) - stddev(tuple(value for _name, value in forecast.components)) * Decimal(4)
    )


def _quality(feature: StrategyIntelligenceFeatures) -> Decimal:
    return Decimal("0.85") if feature.quality_warning_count <= 2 else Decimal("0.75")


def _rank_by_session(
    outcomes: Sequence[ExpectedOutcome],
    decisions: Sequence[ResearchDecision],
    policy: OpportunityRankingPolicy,
) -> tuple[RankedOpportunity, ...]:
    by_session: dict[str, list[ExpectedOutcome]] = defaultdict(list)
    for outcome in outcomes:
        by_session[outcome.decision_session_id].append(outcome)
    decisions_by_session: dict[str, list[ResearchDecision]] = defaultdict(list)
    for decision in decisions:
        decisions_by_session[decision.decision_session_id].append(decision)
    return tuple(
        item
        for session in sorted(by_session)
        for item in policy.rank(
            outcomes=by_session[session], decisions=decisions_by_session[session]
        )
    )


def _evaluate(
    *,
    dataset: SecuritiesDailyDirectionDataset,
    outcomes: Sequence[ExpectedOutcome],
    decisions: Sequence[ResearchDecision],
    regimes: Mapping[str, RegimeSnapshot],
    returns: Mapping[str, Sequence[tuple[str, Decimal]]],
) -> tuple[StrategyIntelligenceEvaluation, ...]:
    holdout = set(dataset.holdout_group_ids)
    realized = {
        (instrument, session): value
        for instrument, items in returns.items()
        for session, value in items
    }
    result = []
    for partition, sessions in (
        ("WALK_FORWARD", {item.decision_session_id for item in outcomes} - holdout),
        ("UNTOUCHED_HOLDOUT", holdout),
    ):
        selected = [item for item in decisions if item.decision_session_id in sessions]
        acted = [item for item in selected if item.action.value in {"OPEN_LONG", "HOLD"}]
        values = [
            realized.get((item.instrument_id, item.decision_session_id), Decimal())
            for item in acted
        ]
        net = [q(value - Decimal("0.001")) for value in values]
        equity = Decimal(1)
        peak = Decimal(1)
        drawdown = Decimal()
        for value in net:
            equity = q(equity * (Decimal(1) + value))
            peak = max(peak, equity)
            drawdown = min(drawdown, q(equity / peak - Decimal(1)))
        false_positive = (
            Decimal(sum(value <= 0 for value in values)) / Decimal(len(values))
            if values
            else Decimal()
        )
        regime_metrics: dict[str, list[Decimal]] = {
            regime.regime.value: [] for regime in regimes.values()
        }
        for item, value in zip(acted, values, strict=True):
            regime_metrics[regimes[item.decision_session_id].regime.value].append(value)
        payload = {
            "partition": partition,
            "selected": tuple(item.canonical_hash for item in selected),
            "values": values,
        }
        result.append(
            StrategyIntelligenceEvaluation(
                partition,
                len(selected),
                q(Decimal(len(acted)) / Decimal(len(selected))) if selected else Decimal(),
                q(Decimal(len(selected) - len(acted)) / Decimal(len(selected)))
                if selected
                else Decimal(1),
                mean(values) if values else Decimal(),
                mean(net) if net else Decimal(),
                min(values) if values else Decimal(),
                drawdown,
                q(
                    Decimal(len(acted)) / Decimal(max(1, len(selected))),
                ),
                q(Decimal(len(acted)) * Decimal("0.001")),
                q(false_positive),
                tuple(
                    sorted(
                        (name, mean(items) if items else Decimal())
                        for name, items in regime_metrics.items()
                    )
                ),
                content_digest(payload),
            )
        )
    return tuple(result)


def _ret(current: Decimal, prior: Decimal) -> Decimal:
    if prior <= 0:
        raise StrategyIntelligenceApplicationError("STRATEGY_INTELLIGENCE_NONPOSITIVE_RAW_CLOSE")
    return q(current / prior - Decimal(1))


def _daily(values: Sequence[Decimal]) -> tuple[Decimal, ...]:
    return tuple(_ret(current, prior) for prior, current in pairwise(values))


def _vol(values: Sequence[Decimal]) -> Decimal:
    return stddev(_daily(values))


def _drawdown(values: Sequence[Decimal]) -> Decimal:
    return q(values[-1] / max(values) - Decimal(1))


def _ranks(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    return {
        name: q(Decimal(index) / Decimal(max(1, len(ordered) - 1)))
        for index, (name, _value) in enumerate(ordered)
    }


def _corr(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 2:
        return Decimal()
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum(
        ((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)), Decimal()
    )
    denominator = stddev(left) * stddev(right) * Decimal(len(left))
    return q(numerator / denominator) if denominator else Decimal()


def _write_jsonl(
    store: StrategyLabArtifactStore, location: str, values: Sequence[object] | object
) -> StrategyLabArtifact:
    payload = b"".join(canonical_json_bytes(value) + b"\n" for value in values)  # type: ignore[union-attr]
    return store.write_bytes(location, payload)


def _write_json(
    store: StrategyLabArtifactStore, location: str, value: object
) -> StrategyLabArtifact:
    return store.write_bytes(location, canonical_json_bytes(value))
