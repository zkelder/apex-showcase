"""Immutable research-only ensemble built from a retained intelligence run."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from apex.application.securities_strategy_intelligence import SecuritiesStrategyIntelligenceRun
from apex.application.strategy_lab import StrategyLabArtifact, StrategyLabArtifactStore
from apex.domain.securities_ensemble import (
    ComponentCredibility,
    ComponentCredibilityPolicy,
    ComponentEligibility,
    EnsembleComponent,
    EnsembleComponentFamily,
    EnsembleForecast,
    MetaDecision,
    MetaPortfolioPlan,
    MetaPortfolioPolicy,
    MetaStrategyActionPolicy,
    RegimeAwareEnsemblePolicy,
)
from apex.domain.strategy_intelligence import MarketRegime
from apex.domain.values import canonical_json_bytes, content_digest, deterministic_id, parse_decimal


class EnsembleApplicationError(RuntimeError):
    """The immutable ensemble evidence cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class SecuritiesEnsembleRun:
    run_id: str
    intelligence_run_id: str
    components_artifact: StrategyLabArtifact
    credibility_artifact: StrategyLabArtifact
    ensemble_artifact: StrategyLabArtifact
    decisions_artifact: StrategyLabArtifact
    portfolio_artifact: StrategyLabArtifact
    evaluation_artifact: StrategyLabArtifact
    snapshot_artifact: StrategyLabArtifact
    blockers: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str
    idempotency_key: str
    created_at: datetime


class EnsembleRepository(Protocol):
    def run_by_idempotency(self, key: str) -> SecuritiesEnsembleRun | None: ...

    def save_run(self, run: SecuritiesEnsembleRun) -> SecuritiesEnsembleRun: ...

    def runs(self) -> tuple[SecuritiesEnsembleRun, ...]: ...


class InMemoryEnsembleRepository:
    def __init__(self) -> None:
        self._items: dict[str, SecuritiesEnsembleRun] = {}

    def run_by_idempotency(self, key: str) -> SecuritiesEnsembleRun | None:
        return next((item for item in self._items.values() if item.idempotency_key == key), None)

    def save_run(self, run: SecuritiesEnsembleRun) -> SecuritiesEnsembleRun:
        prior = self._items.get(run.run_id)
        if prior is not None and prior.canonical_result_hash != run.canonical_result_hash:
            raise EnsembleApplicationError("ENSEMBLE_IMMUTABLE")
        self._items[run.run_id] = run
        return run

    def runs(self) -> tuple[SecuritiesEnsembleRun, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: (item.created_at, item.run_id)))


class BuildSecuritiesEnsemble:
    """Combine retained research components; it never creates financial state."""

    def __init__(
        self, *, artifacts: StrategyLabArtifactStore, repository: EnsembleRepository
    ) -> None:
        self._artifacts = artifacts
        self._repository = repository
        self._credibility = ComponentCredibilityPolicy()
        self._ensemble = RegimeAwareEnsemblePolicy()
        self._action = MetaStrategyActionPolicy()
        self._portfolio = MetaPortfolioPolicy()

    def execute(
        self, *, intelligence: SecuritiesStrategyIntelligenceRun, idempotency_key: str
    ) -> SecuritiesEnsembleRun:
        if not idempotency_key:
            raise EnsembleApplicationError("ENSEMBLE_IDEMPOTENCY_REQUIRED")
        input_hash = content_digest(
            {
                "intelligence": intelligence.canonical_result_hash,
                "credibility": self._credibility.canonical_hash,
                "ensemble": self._ensemble.canonical_hash,
                "action": self._action.canonical_hash,
                "portfolio": self._portfolio.canonical_hash,
                "key": idempotency_key,
            }
        )
        prior = self._repository.run_by_idempotency(idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != input_hash:
                raise EnsembleApplicationError("ENSEMBLE_IDEMPOTENCY_CONFLICT")
            return prior
        forecasts = _read_lines(self._artifacts, intelligence.forecasts_artifact)
        outcomes = _read_lines(self._artifacts, intelligence.outcomes_artifact)
        regimes = {
            str(item["decision_session_id"]): item
            for item in _read_lines(self._artifacts, intelligence.regimes_artifact)
        }
        components = _components(forecasts, outcomes, intelligence.blocking_reasons)
        credibility = tuple(self._credibility.score(item) for item in components)
        by_key: dict[tuple[str, str], list[tuple[EnsembleComponent, ComponentCredibility]]] = (
            defaultdict(list)
        )
        for component, score in zip(components, credibility, strict=True):
            by_key[(component.instrument_id, component.session_id)].append((component, score))
        ensembles: list[EnsembleForecast] = []
        decisions: list[MetaDecision] = []
        plans: list[MetaPortfolioPlan] = []
        for key, items in sorted(by_key.items()):
            regime = regimes[key[1]]
            forecast = self._ensemble.combine(
                tuple(item for item, _score in items),
                tuple(score for _item, score in items),
                MarketRegime(cast(str, regime["regime"])),
                parse_decimal(cast(str | int | Decimal, regime["confidence"])),
            )
            ensembles.append(forecast)
            transparent_support = any(
                item.eligibility is ComponentEligibility.ELIGIBLE_RESEARCH_INPUT
                and item.expected_net_return is not None
                and item.expected_net_return > 0
                for item, _score in items
            )
            decision = self._action.decide(
                forecast,
                prior_exposure=Decimal(),
                transparent_support=transparent_support,
                quality=Decimal("0.85"),
            )
            decisions.append(decision)
            plans.append(self._portfolio.plan((decision,), (forecast,)))
        base = f"ensemble-meta/{input_hash}"
        components_artifact = _write_lines(self._artifacts, f"{base}/components.jsonl", components)
        credibility_artifact = _write_lines(
            self._artifacts, f"{base}/credibility.jsonl", credibility
        )
        ensemble_artifact = _write_lines(self._artifacts, f"{base}/ensemble.jsonl", ensembles)
        decisions_artifact = _write_lines(self._artifacts, f"{base}/decisions.jsonl", decisions)
        portfolio_artifact = _write_lines(self._artifacts, f"{base}/portfolio.jsonl", plans)
        evaluation_artifact = self._artifacts.write_bytes(
            f"{base}/evaluation.json",
            canonical_json_bytes(
                {
                    "walk_forward": "RETAINED_SEPARATELY_FROM_HOLDOUT",
                    "untouched_holdout": "RETAINED_SEPARATELY_FROM_WALK_FORWARD",
                    "component_comparisons": tuple(
                        sorted({item.family.value for item in components})
                    ),
                    "integrity_recommendation": "BLOCKED_INTEGRITY",
                    "blockers": intelligence.blocking_reasons,
                }
            ),
        )
        latest_session = max(item.session_id for item in ensembles)
        snapshot_artifact = self._artifacts.write_bytes(
            f"{base}/snapshot.json",
            canonical_json_bytes(
                {
                    "label": "HISTORICAL META-STRATEGY RESEARCH — NOT A CURRENT RECOMMENDATION",
                    "session_id": latest_session,
                    "ensembles": tuple(
                        asdict(item) for item in ensembles if item.session_id == latest_session
                    ),
                    "decisions": tuple(
                        asdict(item) for item in decisions if item.session_id == latest_session
                    ),
                    "portfolios": tuple(
                        asdict(item) for item in plans if item.session_id == latest_session
                    ),
                    "blockers": intelligence.blocking_reasons,
                }
            ),
        )
        result_hash = content_digest(
            tuple(
                item.sha256
                for item in (
                    components_artifact,
                    credibility_artifact,
                    ensemble_artifact,
                    decisions_artifact,
                    portfolio_artifact,
                    evaluation_artifact,
                    snapshot_artifact,
                )
            )
        )
        return self._repository.save_run(
            SecuritiesEnsembleRun(
                run_id=deterministic_id("securities_ensemble_run", {"input": input_hash}),
                intelligence_run_id=intelligence.run_id,
                components_artifact=components_artifact,
                credibility_artifact=credibility_artifact,
                ensemble_artifact=ensemble_artifact,
                decisions_artifact=decisions_artifact,
                portfolio_artifact=portfolio_artifact,
                evaluation_artifact=evaluation_artifact,
                snapshot_artifact=snapshot_artifact,
                blockers=intelligence.blocking_reasons,
                canonical_input_hash=input_hash,
                canonical_result_hash=result_hash,
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
            )
        )


def _read_lines(
    store: StrategyLabArtifactStore, artifact: StrategyLabArtifact
) -> list[dict[str, Any]]:
    return [json.loads(line) for line in store.read_bytes(artifact).splitlines()]


def _write_lines(
    store: StrategyLabArtifactStore, location: str, values: Sequence[object]
) -> StrategyLabArtifact:
    return store.write_bytes(
        location, b"".join(canonical_json_bytes(asdict(cast(Any, item))) + b"\n" for item in values)
    )


def _components(
    forecasts: Sequence[dict[str, Any]],
    outcomes: Sequence[dict[str, Any]],
    blockers: tuple[str, ...],
) -> tuple[EnsembleComponent, ...]:
    outcome_by_key = {
        (str(item["instrument_id"]), str(item["decision_session_id"])): item for item in outcomes
    }
    family_by_name = {
        "canonical_momentum": EnsembleComponentFamily.TIME_SERIES_MOMENTUM,
        "canonical_mean_reversion": EnsembleComponentFamily.SHORT_HORIZON_MEAN_REVERSION,
        "volatility_targeted": EnsembleComponentFamily.VOLATILITY_TARGETED_LONG_ONLY,
        "market_relative": EnsembleComponentFamily.MARKET_RELATIVE_STRENGTH,
    }
    result: list[EnsembleComponent] = []
    for forecast in forecasts:
        key = (str(forecast["instrument_id"]), str(forecast["decision_session_id"]))
        outcome = outcome_by_key[key]
        values = {str(name): parse_decimal(value) for name, value in forecast["components"]}
        expected_gross = _decimal(outcome.get("expected_gross_return"))
        expected_net = _decimal(outcome.get("expected_net_return"))
        downside = _decimal(outcome.get("expected_downside"))
        uncertainty = _decimal(outcome.get("uncertainty"))
        for name, family in family_by_name.items():
            value = values[name]
            result.append(
                _component(
                    key,
                    family,
                    value,
                    expected_gross,
                    expected_net,
                    downside,
                    uncertainty,
                    ComponentEligibility.ELIGIBLE_RESEARCH_INPUT,
                    (),
                    forecast,
                    outcome,
                )
            )
        result.append(
            _component(
                key,
                EnsembleComponentFamily.STRATEGY_INTELLIGENCE_EXPECTED_OUTCOME,
                _decimal(forecast.get("calibrated_probability")),
                expected_gross,
                expected_net,
                downside,
                uncertainty,
                ComponentEligibility.ELIGIBLE_RESEARCH_INPUT,
                (),
                forecast,
                outcome,
            )
        )
        result.append(
            _component(
                key,
                EnsembleComponentFamily.LOGISTIC_REGRESSION_RESEARCH,
                None,
                expected_gross,
                expected_net,
                downside,
                uncertainty,
                ComponentEligibility.RESEARCH_ONLY_BLOCKED,
                blockers,
                forecast,
                outcome,
            )
        )
        result.append(
            _component(
                key,
                EnsembleComponentFamily.HISTOGRAM_GRADIENT_BOOSTING_RESEARCH,
                None,
                expected_gross,
                expected_net,
                downside,
                uncertainty,
                ComponentEligibility.RESEARCH_ONLY_BLOCKED,
                blockers,
                forecast,
                outcome,
            )
        )
    return tuple(result)


def _component(
    key: tuple[str, str],
    family: EnsembleComponentFamily,
    value: Decimal | None,
    gross: Decimal | None,
    net: Decimal | None,
    downside: Decimal | None,
    uncertainty: Decimal | None,
    eligibility: ComponentEligibility,
    reasons: tuple[str, ...],
    forecast: dict[str, Any],
    outcome: dict[str, Any],
) -> EnsembleComponent:
    identity = content_digest(
        (
            key,
            family,
            value,
            gross,
            net,
            eligibility,
            forecast["canonical_hash"],
            outcome["canonical_hash"],
        )
    )
    return EnsembleComponent(
        instrument_id=key[0],
        session_id=key[1],
        family=family,
        forecast=value,
        probability_positive=value,
        expected_gross_return=gross,
        expected_net_return=net,
        downside=downside,
        uncertainty=uncertainty,
        regime_suitability=Decimal(1),
        calibration_state="RETAINED_BASELINE",
        evidence_quality=Decimal("0.85"),
        simulator_state="MATERIAL_MISMATCH",
        integrity_state="BLOCKED_INTEGRITY",
        eligibility=eligibility,
        reason_codes=reasons,
        source_hashes=(str(forecast["canonical_hash"]), str(outcome["canonical_hash"])),
        canonical_hash=identity,
    )


def _decimal(value: object) -> Decimal | None:
    return (
        None
        if value is None
        else parse_decimal(cast(str | int | Decimal, value), allow_negative=True)
    )
