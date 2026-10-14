"""Finite, local-only SHADOW evidence-plan rules and forward scoring.

This module owns no provider client and no financial lifecycle.  It makes the
operator-visible plan terms, frozen market identities, bounded worker lease,
and public-outcome scoring rules explicit so adapters can persist them without
giving a plan any order, reservation, position, or settlement authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any, Protocol, cast

from apex.application.capture_public_orderbook_session import CaptureMarketCandidate
from apex.application.discover_public_markets import (
    PublicMarketScanRequest,
    PublicMarketScanSummary,
)
from apex.domain.orderbook_microstructure import OrderbookMicrostructureStrategyConfig
from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    ONE,
    ROUNDING_IDENTITY,
    OrderbookSide,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.published_fee_schedule import (
    KALSHI_PUBLISHED_FEE_SCHEDULE,
    FeeScheduleApplicabilityState,
    PublishedFeeScheduleApplicability,
    reconcile_kalshi_published_fee_schedule,
)
from apex.domain.values import ValueValidationError, content_digest, deterministic_id, parse_decimal

SHADOW_OPERATIONS_VERSION = "shadow-operations-v1"
FORWARD_SCORING_VERSION = "forward-performance-scoring-v1"
LOG_LOSS_FLOOR = Decimal("0.000000000000000000000000000001")
SCORE_QUANTUM = Decimal("0.00000001")


class ShadowEvidencePlanState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"


class ShadowEvidenceIterationState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class ShadowEvidenceCohortState(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"


class PublicOutcomeState(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    INVALID = "INVALID"


class AtomicShadowLaunchState(StrEnum):
    """Durable terminal and in-progress states for one atomic launch identity."""

    RUNNING = "RUNNING"
    CREATED_PLAN = "CREATED_PLAN"
    NO_PLAN = "NO_PLAN"
    FAILED_BEFORE_PROVIDER_IO = "FAILED_BEFORE_PROVIDER_IO"
    FAILED_AFTER_PROVIDER_IO = "FAILED_AFTER_PROVIDER_IO"
    INTERRUPTED_AFTER_PROVIDER_IO = "INTERRUPTED_AFTER_PROVIDER_IO"


class AtomicShadowLaunchLifecycleError(RuntimeError):
    """A retained launch identity cannot safely begin provider collection."""

    def __init__(self, state: AtomicShadowLaunchState) -> None:
        self.state = state
        super().__init__(state.value)


class ShadowPlanConfigurationBindingError(RuntimeError):
    """A retained plan cannot reproduce its exact governed strategy configuration."""


@dataclass(frozen=True, slots=True)
class StrategyConfigurationBinding:
    """One immutable executable strategy configuration frozen by a plan.

    ``configuration_payload`` is a canonical, string-only representation of
    the exact configuration hash input.  Keeping it with the hash means a
    later worker can reproduce and validate the configuration rather than
    silently rebuilding a nearby default.
    """

    selection_ordinal: int
    strategy_id: str
    strategy_version: str
    strategy_family: str
    configuration_version: str
    configuration_payload: tuple[tuple[str, str], ...]
    configuration_hash: str
    evidence_state: str

    def __post_init__(self) -> None:
        if not 0 <= self.selection_ordinal <= 20:
            raise ValueValidationError("strategy configuration selection ordinal is invalid")
        for label, value in (
            ("strategy configuration strategy ID", self.strategy_id),
            ("strategy configuration strategy version", self.strategy_version),
            ("strategy configuration family", self.strategy_family),
            ("strategy configuration version", self.configuration_version),
            ("strategy configuration evidence state", self.evidence_state),
        ):
            _text(value, label=label, maximum=160)
        if len(self.configuration_hash) != 64:
            raise ValueValidationError("strategy configuration hash must be a SHA-256 digest")
        if not self.configuration_payload:
            raise ValueValidationError("strategy configuration payload is required")
        if tuple(sorted(self.configuration_payload)) != self.configuration_payload:
            raise ValueValidationError("strategy configuration payload must be canonical")
        if len({key for key, _value in self.configuration_payload}) != len(
            self.configuration_payload
        ):
            raise ValueValidationError("strategy configuration payload keys must be unique")
        for key, value in self.configuration_payload:
            _text(key, label="strategy configuration payload key", maximum=160)
            _text(value, label="strategy configuration payload value", maximum=500)

    @property
    def payload(self) -> dict[str, str]:
        return dict(self.configuration_payload)

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)


ATOMIC_EXCLUSION_MAXIMUM_PRIOR_PLANS = 20
ATOMIC_EXCLUSION_MAXIMUM_MARKETS = 1_000
ATOMIC_EXCLUSION_MAXIMUM_EVENTS = 1_000


def _text(value: str, *, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueValidationError(f"{label} must be non-empty and at most {maximum} characters")
    return value


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ShadowEvidencePlanConfiguration:
    """All finite plan controls, including intentionally hard local bounds."""

    iterations: int = 12
    interval_seconds: int = 300
    market_count: int = 10
    max_pages: int = 3
    max_enrichments: int = 30
    max_markets_per_event: int = 2
    max_markets_per_series: int = 4
    orderbook_depth: int = 5
    maximum_predictions_per_iteration: int = 100
    maximum_opportunities_per_iteration: int = 40
    timeout_seconds: int = 10
    strategy_ids: tuple[str, ...] = (
        "market-consensus-baseline-v1",
        "kalshi-orderbook-microstructure-v1",
        "kalshi-market-dynamics-v1",
    )
    strategy_configuration_hashes: tuple[tuple[str, str], ...] = ()
    strategy_configuration_bindings: tuple[StrategyConfigurationBinding, ...] = ()
    policy_identity: str = "shadow-evidence-plan-policy-v1"
    policy_hash: str = ""

    def __post_init__(self) -> None:
        if not 3 <= self.iterations <= 288:
            raise ValueValidationError("shadow plan iterations must be between three and 288")
        if not 30 <= self.interval_seconds <= 86400:
            raise ValueValidationError("shadow plan interval must be between 30 and 86400 seconds")
        if not 3 <= self.market_count <= 50:
            raise ValueValidationError("shadow plan market count must be between three and 50")
        if not 1 <= self.max_pages <= 5:
            raise ValueValidationError("shadow plan maximum pages must be between one and five")
        if not self.market_count <= self.max_enrichments <= 80:
            raise ValueValidationError(
                "shadow plan maximum enrichments must cover the cohort and be at most 80"
            )
        if not 1 <= self.max_markets_per_event <= 5:
            raise ValueValidationError("shadow plan per-event cap must be between one and five")
        if not 1 <= self.max_markets_per_series <= 10:
            raise ValueValidationError("shadow plan per-series cap must be between one and ten")
        if not 1 <= self.orderbook_depth <= 20:
            raise ValueValidationError("shadow plan orderbook depth must be between one and 20")
        if not 1 <= self.maximum_predictions_per_iteration <= 500:
            raise ValueValidationError(
                "shadow plan maximum predictions must be between one and 500"
            )
        if not 1 <= self.maximum_opportunities_per_iteration <= 500:
            raise ValueValidationError(
                "shadow plan maximum opportunities must be between one and 500"
            )
        if self.maximum_duration_seconds > 30 * 24 * 60 * 60:
            raise ValueValidationError("shadow plan maximum total duration is 30 days")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueValidationError("shadow plan timeout must be between one and 60 seconds")
        if len(self.strategy_ids) != 3 or tuple(sorted(self.strategy_ids)) != tuple(
            sorted(set(self.strategy_ids))
        ):
            raise ValueValidationError("shadow plan requires three distinct fixed strategy IDs")
        for strategy_id in self.strategy_ids:
            _text(strategy_id, label="strategy ID", maximum=160)
        if self.strategy_configuration_hashes and tuple(
            item[0] for item in self.strategy_configuration_hashes
        ) != tuple(sorted(item[0] for item in self.strategy_configuration_hashes)):
            raise ValueValidationError("strategy configuration hashes must use sorted strategy IDs")
        for strategy_id, configuration_hash in self.strategy_configuration_hashes:
            _text(strategy_id, label="strategy configuration strategy ID", maximum=160)
            if len(configuration_hash) != 64:
                raise ValueValidationError("strategy configuration hash must be a SHA-256 digest")
        if self.strategy_configuration_bindings:
            bindings = tuple(
                sorted(
                    self.strategy_configuration_bindings,
                    key=lambda binding: binding.selection_ordinal,
                )
            )
            if bindings != self.strategy_configuration_bindings:
                raise ValueValidationError(
                    "strategy configuration bindings must use selection order"
                )
            if len({binding.strategy_id for binding in bindings}) != len(bindings):
                raise ValueValidationError("strategy configuration bindings must be unique")
            if tuple(binding.strategy_id for binding in bindings) != self.strategy_ids:
                raise ValueValidationError(
                    "strategy configuration bindings must match plan strategies"
                )
            hashes = tuple(
                sorted((binding.strategy_id, binding.configuration_hash) for binding in bindings)
            )
            if self.strategy_configuration_hashes and hashes != self.strategy_configuration_hashes:
                raise ValueValidationError(
                    "strategy configuration hashes must match retained bindings"
                )
            object.__setattr__(self, "strategy_configuration_hashes", hashes)
        _text(self.policy_identity, label="shadow plan policy identity", maximum=160)
        if self.policy_hash and len(self.policy_hash) != 64:
            raise ValueValidationError("shadow plan policy hash must be a SHA-256 digest")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    @property
    def maximum_duration_seconds(self) -> int:
        """Finite duration including the initial iteration, never a schedule expression."""

        return self.iterations * self.interval_seconds


def _string_payload(values: dict[str, object]) -> tuple[tuple[str, str], ...]:
    """Canonical string-only payload used for a retained configuration replay."""

    output: list[tuple[str, str]] = []
    for key, value in values.items():
        rendered = format(value, "f") if isinstance(value, Decimal) else str(value)
        output.append((key, rendered))
    return tuple(sorted(output))


def _microstructure_payload(configuration: object) -> tuple[tuple[str, str], ...]:
    """Render the exact public hash input without relying on dataclass internals."""

    from apex.domain.orderbook_microstructure import OrderbookMicrostructureStrategyConfig

    config = configuration
    if not isinstance(config, OrderbookMicrostructureStrategyConfig):
        raise ValueValidationError("microstructure configuration type is invalid")
    values: dict[str, object] = {
        "strategy_id": config.strategy_id,
        "strategy_version": config.strategy_version,
        "maximum_markets_per_invocation": config.maximum_markets_per_invocation,
        "orderbook_depth": config.orderbook_depth,
        "minimum_visible_quantity": config.minimum_visible_quantity,
        "maximum_spread": config.maximum_spread,
        "maximum_observation_age_seconds": int(config.maximum_observation_age.total_seconds()),
        "minimum_observations_per_market": config.minimum_observations_per_market,
        "maximum_observation_interval_seconds": int(
            config.maximum_observation_interval.total_seconds()
        ),
        "imbalance_weight": config.imbalance_weight,
        "microprice_weight": config.microprice_weight,
        "momentum_weight": config.momentum_weight,
        "maximum_probability_adjustment": config.maximum_probability_adjustment,
        "conservative_shrinkage_factor": config.conservative_shrinkage_factor,
        "minimum_conservative_advantage": config.minimum_conservative_advantage,
        "expected_fee_model": config.expected_fee_model,
        "slippage_reserve": config.slippage_reserve,
        "execution_uncertainty_reserve": config.execution_uncertainty_reserve,
        "evidence_tier": config.evidence_tier,
        "configuration_version": config.configuration_version,
    }
    if config.uses_published_fee_schedule:
        assert config.published_fee_schedule is not None
        values.update(
            {
                f"published_fee_{key}": value
                for key, value in config.published_fee_schedule.canonical_payload.items()
            }
        )
    return _string_payload(values)


def _dynamics_payload(configuration: object) -> tuple[tuple[str, str], ...]:
    from apex.domain.market_dynamics import MarketDynamicsStrategyConfig

    config = configuration
    if not isinstance(config, MarketDynamicsStrategyConfig):
        raise ValueValidationError("market dynamics configuration type is invalid")
    return _string_payload(
        {
            "strategy_id": config.strategy_id,
            "strategy_version": config.strategy_version,
            "maximum_markets_per_invocation": config.maximum_markets_per_invocation,
            "minimum_observations": config.minimum_observations,
            "maximum_observations": config.maximum_observations,
            "minimum_history_duration_seconds": int(
                config.minimum_history_duration.total_seconds()
            ),
            "maximum_history_duration_seconds": int(
                config.maximum_history_duration.total_seconds()
            ),
            "maximum_observation_age_seconds": int(config.maximum_observation_age.total_seconds()),
            "maximum_adjacent_observation_gap_seconds": int(
                config.maximum_adjacent_observation_gap.total_seconds()
            ),
            "maximum_latest_spread": config.maximum_latest_spread,
            "minimum_nonzero_price_moves": config.minimum_nonzero_price_moves,
            "window_momentum_scale": config.window_momentum_scale,
            "recent_momentum_scale": config.recent_momentum_scale,
            "window_momentum_weight": config.window_momentum_weight,
            "recent_momentum_weight": config.recent_momentum_weight,
            "directional_persistence_weight": config.directional_persistence_weight,
            "maximum_probability_adjustment": config.maximum_probability_adjustment,
            "conservative_shrinkage_factor": config.conservative_shrinkage_factor,
            "minimum_conservative_advantage": config.minimum_conservative_advantage,
            "expected_fee_model": config.expected_fee_model,
            "slippage_reserve": config.slippage_reserve,
            "execution_uncertainty_reserve": config.execution_uncertainty_reserve,
            "evidence_tier": config.evidence_tier,
            "configuration_version": config.configuration_version,
        }
    )


def fixed_strategy_configuration_bindings(
    *,
    market_count: int,
    microstructure_configuration: object | None = None,
) -> tuple[StrategyConfigurationBinding, ...]:
    """Freeze all executable plan configurations, not merely their hashes."""

    from apex.application.market_dynamics_shadow import market_dynamics_strategy_descriptor
    from apex.application.orderbook_microstructure_shadow import (
        orderbook_microstructure_strategy_descriptor,
    )
    from apex.application.strategy_shadow import production_shadow_strategy_descriptors
    from apex.domain.market_dynamics import MarketDynamicsStrategyConfig
    from apex.domain.orderbook_microstructure import OrderbookMicrostructureStrategyConfig

    if not 3 <= market_count <= 50:
        raise ValueValidationError(
            "strategy configuration hashing requires between three and 50 markets"
        )
    catalog = {item.strategy_id: item for item in production_shadow_strategy_descriptors()}
    if microstructure_configuration is None:
        micro = OrderbookMicrostructureStrategyConfig(
            maximum_markets_per_invocation=market_count,
            minimum_observations_per_market=4,
        )
    elif isinstance(microstructure_configuration, OrderbookMicrostructureStrategyConfig):
        micro = microstructure_configuration
        if micro.maximum_markets_per_invocation != market_count:
            raise ValueValidationError("microstructure configuration market count must match plan")
    else:
        raise ValueValidationError("microstructure configuration type is invalid")
    dynamics = MarketDynamicsStrategyConfig(
        maximum_markets_per_invocation=market_count,
        minimum_observations=4,
        maximum_observations=4,
        maximum_observation_age=timedelta(seconds=120),
    )
    consensus = catalog["market-consensus-baseline-v1"]
    micro_descriptor = orderbook_microstructure_strategy_descriptor(micro)
    dynamics_descriptor = market_dynamics_strategy_descriptor(dynamics)
    return (
        StrategyConfigurationBinding(
            selection_ordinal=0,
            strategy_id=consensus.strategy_id,
            strategy_version=consensus.version,
            strategy_family=consensus.family.value,
            configuration_version="shadow-scoring-v1",
            configuration_payload=_string_payload(
                {
                    "strategy_id": consensus.strategy_id,
                    "version": consensus.version,
                    "authority": consensus.authority_state.value,
                    "configuration_version": "shadow-scoring-v1",
                }
            ),
            configuration_hash=consensus.configuration_hash.value,
            evidence_state=consensus.evidence_state.value,
        ),
        StrategyConfigurationBinding(
            selection_ordinal=1,
            strategy_id=micro_descriptor.strategy_id,
            strategy_version=micro_descriptor.version,
            strategy_family=micro_descriptor.family.value,
            configuration_version=micro.configuration_version,
            configuration_payload=_microstructure_payload(micro),
            configuration_hash=micro_descriptor.configuration_hash.value,
            evidence_state=micro_descriptor.evidence_state.value,
        ),
        StrategyConfigurationBinding(
            selection_ordinal=2,
            strategy_id=dynamics_descriptor.strategy_id,
            strategy_version=dynamics_descriptor.version,
            strategy_family=dynamics_descriptor.family.value,
            configuration_version=dynamics.configuration_version,
            configuration_payload=_dynamics_payload(dynamics),
            configuration_hash=dynamics_descriptor.configuration_hash.value,
            evidence_state=dynamics_descriptor.evidence_state.value,
        ),
    )


def fixed_strategy_configuration_hashes(*, market_count: int) -> tuple[tuple[str, str], ...]:
    """Compatibility projection of the full exact plan configuration binding."""

    return tuple(
        sorted(
            (binding.strategy_id, binding.configuration_hash)
            for binding in fixed_strategy_configuration_bindings(market_count=market_count)
        )
    )


def _binding_integer(payload: dict[str, str], key: str) -> int:
    try:
        return int(payload[key])
    except (KeyError, ValueError) as error:
        raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_LINEAGE_MISSING") from error


def _binding_decimal(payload: dict[str, str], key: str) -> Decimal:
    try:
        return parse_decimal(payload[key])
    except (KeyError, ValueError) as error:
        raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_LINEAGE_MISSING") from error


def _binding_text(payload: dict[str, str], key: str) -> str:
    try:
        return payload[key]
    except KeyError as error:
        raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_LINEAGE_MISSING") from error


def resolve_plan_strategy_configurations(
    configuration: ShadowEvidencePlanConfiguration,
) -> dict[str, object]:
    """Rebuild and verify a plan's exact executable configurations.

    This is deliberately called by the worker before it performs any provider
    operation.  A legacy plan with only hashes is readable and classifiable,
    but cannot be resumed because it cannot prove the executable parameters.
    """

    from apex.application.market_dynamics_shadow import market_dynamics_strategy_descriptor
    from apex.application.orderbook_microstructure_shadow import (
        orderbook_microstructure_strategy_descriptor,
    )
    from apex.application.strategy_shadow import production_shadow_strategy_descriptors
    from apex.domain.market_dynamics import MarketDynamicsStrategyConfig
    from apex.domain.orderbook_microstructure import (
        PUBLISHED_FEE_CONFIGURATION_VERSION,
        OrderbookMicrostructureStrategyConfig,
    )

    bindings = configuration.strategy_configuration_bindings
    if not bindings:
        raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_LINEAGE_MISSING")
    if tuple(binding.strategy_id for binding in bindings) != configuration.strategy_ids:
        raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_MISMATCH")
    catalog = {item.strategy_id: item for item in production_shadow_strategy_descriptors()}
    executable: dict[str, object] = {}
    for binding in bindings:
        payload = binding.payload
        try:
            if binding.strategy_id == "market-consensus-baseline-v1":
                descriptor = catalog[binding.strategy_id]
                expected_hash = content_digest(
                    {
                        "strategy_id": _binding_text(payload, "strategy_id"),
                        "version": _binding_text(payload, "version"),
                        "authority": _binding_text(payload, "authority"),
                        "configuration_version": _binding_text(payload, "configuration_version"),
                    }
                )
                rebuilt: object = descriptor
            elif binding.strategy_id == "kalshi-orderbook-microstructure-v1":
                configuration_version = _binding_text(payload, "configuration_version")
                published_fee_schedule = None
                if configuration_version == PUBLISHED_FEE_CONFIGURATION_VERSION:
                    from apex.domain.published_fee_schedule import KALSHI_PUBLISHED_FEE_SCHEDULE

                    published_fee_schedule = KALSHI_PUBLISHED_FEE_SCHEDULE
                rebuilt = OrderbookMicrostructureStrategyConfig(
                    strategy_id=_binding_text(payload, "strategy_id"),
                    strategy_version=_binding_text(payload, "strategy_version"),
                    maximum_markets_per_invocation=_binding_integer(
                        payload, "maximum_markets_per_invocation"
                    ),
                    orderbook_depth=_binding_integer(payload, "orderbook_depth"),
                    minimum_visible_quantity=_binding_decimal(payload, "minimum_visible_quantity"),
                    maximum_spread=_binding_decimal(payload, "maximum_spread"),
                    maximum_observation_age=timedelta(
                        seconds=_binding_integer(payload, "maximum_observation_age_seconds")
                    ),
                    minimum_observations_per_market=_binding_integer(
                        payload, "minimum_observations_per_market"
                    ),
                    maximum_observation_interval=timedelta(
                        seconds=_binding_integer(payload, "maximum_observation_interval_seconds")
                    ),
                    imbalance_weight=_binding_decimal(payload, "imbalance_weight"),
                    microprice_weight=_binding_decimal(payload, "microprice_weight"),
                    momentum_weight=_binding_decimal(payload, "momentum_weight"),
                    maximum_probability_adjustment=_binding_decimal(
                        payload, "maximum_probability_adjustment"
                    ),
                    conservative_shrinkage_factor=_binding_decimal(
                        payload, "conservative_shrinkage_factor"
                    ),
                    minimum_conservative_advantage=_binding_decimal(
                        payload, "minimum_conservative_advantage"
                    ),
                    expected_fee_model=_binding_text(payload, "expected_fee_model"),
                    slippage_reserve=_binding_decimal(payload, "slippage_reserve"),
                    execution_uncertainty_reserve=_binding_decimal(
                        payload, "execution_uncertainty_reserve"
                    ),
                    evidence_tier=_binding_text(payload, "evidence_tier"),
                    configuration_version=configuration_version,
                    published_fee_schedule=published_fee_schedule,
                )
                descriptor = orderbook_microstructure_strategy_descriptor(rebuilt)
                expected_hash = rebuilt.canonical_hash.value
                if _microstructure_payload(rebuilt) != binding.configuration_payload:
                    raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_MISMATCH")
            elif binding.strategy_id == "kalshi-market-dynamics-v1":
                rebuilt = MarketDynamicsStrategyConfig(
                    strategy_id=_binding_text(payload, "strategy_id"),
                    strategy_version=_binding_text(payload, "strategy_version"),
                    maximum_markets_per_invocation=_binding_integer(
                        payload, "maximum_markets_per_invocation"
                    ),
                    minimum_observations=_binding_integer(payload, "minimum_observations"),
                    maximum_observations=_binding_integer(payload, "maximum_observations"),
                    minimum_history_duration=timedelta(
                        seconds=_binding_integer(payload, "minimum_history_duration_seconds")
                    ),
                    maximum_history_duration=timedelta(
                        seconds=_binding_integer(payload, "maximum_history_duration_seconds")
                    ),
                    maximum_observation_age=timedelta(
                        seconds=_binding_integer(payload, "maximum_observation_age_seconds")
                    ),
                    maximum_adjacent_observation_gap=timedelta(
                        seconds=_binding_integer(
                            payload, "maximum_adjacent_observation_gap_seconds"
                        )
                    ),
                    maximum_latest_spread=_binding_decimal(payload, "maximum_latest_spread"),
                    minimum_nonzero_price_moves=_binding_integer(
                        payload, "minimum_nonzero_price_moves"
                    ),
                    window_momentum_scale=_binding_decimal(payload, "window_momentum_scale"),
                    recent_momentum_scale=_binding_decimal(payload, "recent_momentum_scale"),
                    window_momentum_weight=_binding_decimal(payload, "window_momentum_weight"),
                    recent_momentum_weight=_binding_decimal(payload, "recent_momentum_weight"),
                    directional_persistence_weight=_binding_decimal(
                        payload, "directional_persistence_weight"
                    ),
                    maximum_probability_adjustment=_binding_decimal(
                        payload, "maximum_probability_adjustment"
                    ),
                    conservative_shrinkage_factor=_binding_decimal(
                        payload, "conservative_shrinkage_factor"
                    ),
                    minimum_conservative_advantage=_binding_decimal(
                        payload, "minimum_conservative_advantage"
                    ),
                    expected_fee_model=_binding_text(payload, "expected_fee_model"),
                    slippage_reserve=_binding_decimal(payload, "slippage_reserve"),
                    execution_uncertainty_reserve=_binding_decimal(
                        payload, "execution_uncertainty_reserve"
                    ),
                    evidence_tier=_binding_text(payload, "evidence_tier"),
                    configuration_version=_binding_text(payload, "configuration_version"),
                )
                descriptor = market_dynamics_strategy_descriptor(rebuilt)
                expected_hash = rebuilt.canonical_hash.value
            else:
                raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_LINEAGE_MISSING")
        except ValueValidationError as error:
            raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_MISMATCH") from error
        if (
            descriptor.strategy_id != binding.strategy_id
            or descriptor.version != binding.strategy_version
            or descriptor.family.value != binding.strategy_family
            or descriptor.evidence_state.value != binding.evidence_state
            or descriptor.configuration_hash.value != binding.configuration_hash
            or expected_hash != binding.configuration_hash
        ):
            raise ShadowPlanConfigurationBindingError("PLAN_CONFIGURATION_MISMATCH")
        if getattr(rebuilt, "configuration_version", binding.configuration_version) != (
            binding.configuration_version
        ):
            raise ShadowPlanConfigurationBindingError("CONFIGURATION_VERSION_MISMATCH")
        executable[binding.strategy_id] = rebuilt
    return executable


@dataclass(frozen=True, slots=True)
class ValidatedShadowCohortCandidate:
    """An exact-enrichment result that may freeze a plan without another GET.

    This is an application-internal hand-off: the atomic launch service is the
    only constructor used by production composition.  It deliberately retains
    both the immutable cohort member projection and the source identities that
    produced it, so a plan's canonical request hash binds the retained public
    evidence rather than a caller-supplied ticker list.
    """

    list_market_snapshot_id: str
    market_snapshot_id: str
    event_snapshot_id: str
    event_raw_response_id: str
    series_raw_response_id: str
    exact_market_raw_response_id: str
    fee_snapshot_id: str | None
    provider: str
    market_ticker: str
    event_ticker: str
    series_ticker: str
    rule_content_hash: str
    settlement_source_identity: str
    lifecycle_state: str
    binary_structure_identity: str
    observed_at: datetime
    available_at: datetime
    close_at: datetime
    market_lineage_hash: str
    event_lineage_hash: str
    series_lineage_hash: str
    fee_lineage_hash: str | None
    enriched_at: datetime
    preflight_orderbook: PublicOrderbookSnapshot | None = None
    preflight_levels: tuple[PublicOrderbookLevel, ...] = ()
    preflight_fee: SeriesFeeSnapshot | None = None
    fee_applicability: PublishedFeeScheduleApplicability | None = None

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("list market snapshot ID", self.list_market_snapshot_id, 160),
            ("market snapshot ID", self.market_snapshot_id, 160),
            ("event snapshot ID", self.event_snapshot_id, 160),
            ("event raw response ID", self.event_raw_response_id, 160),
            ("series raw response ID", self.series_raw_response_id, 160),
            ("exact market raw response ID", self.exact_market_raw_response_id, 160),
            ("provider", self.provider, 30),
            ("market ticker", self.market_ticker, 300),
            ("event ticker", self.event_ticker, 300),
            ("series ticker", self.series_ticker, 300),
            ("settlement source identity", self.settlement_source_identity, 160),
            ("lifecycle state", self.lifecycle_state, 30),
            ("binary structure identity", self.binary_structure_identity, 160),
        ):
            _text(value, label=label, maximum=maximum)
        if self.fee_snapshot_id is not None:
            _text(self.fee_snapshot_id, label="fee snapshot ID", maximum=160)
        for label, value in (
            ("rule content hash", self.rule_content_hash),
            ("market lineage hash", self.market_lineage_hash),
            ("event lineage hash", self.event_lineage_hash),
            ("series lineage hash", self.series_lineage_hash),
        ):
            if len(value) != 64:
                raise ValueValidationError(f"{label} must be a SHA-256 digest")
        if self.fee_lineage_hash is not None and len(self.fee_lineage_hash) != 64:
            raise ValueValidationError("fee lineage hash must be a SHA-256 digest")
        if self.fee_applicability is not None and (
            self.preflight_fee is None
            or self.fee_applicability.series_ticker != self.series_ticker
            or self.fee_applicability.provider_fee_type != self.preflight_fee.fee_type
        ):
            raise ValueValidationError(
                "validated cohort fee applicability does not match fee evidence"
            )
        observed = _aware(self.observed_at, label="validated cohort observation")
        available = _aware(self.available_at, label="validated cohort availability")
        close = _aware(self.close_at, label="validated cohort close")
        enriched = _aware(self.enriched_at, label="validated cohort enrichment")
        if self.lifecycle_state != "open" or available < observed or close <= available:
            raise ValueValidationError("validated cohort candidate lifecycle or times are invalid")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "close_at", close)
        object.__setattr__(self, "enriched_at", enriched)

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    @property
    def frozen_member(self) -> FrozenShadowEvidenceCohortMember:
        return FrozenShadowEvidenceCohortMember(
            market_snapshot_id=self.market_snapshot_id,
            provider=self.provider,
            market_ticker=self.market_ticker,
            event_ticker=self.event_ticker,
            series_ticker=self.series_ticker,
            rule_content_hash=self.rule_content_hash,
            lifecycle_state=self.lifecycle_state,
            observed_at=self.observed_at,
            available_at=self.available_at,
            close_at=self.close_at,
            market_lineage_hash=self.market_lineage_hash,
            event_lineage_hash=self.event_lineage_hash,
            series_lineage_hash=self.series_lineage_hash,
            fee_lineage_hash=self.fee_lineage_hash,
        )


@dataclass(frozen=True, slots=True)
class CreateShadowEvidencePlanCommand:
    campaign_id: str
    name: str
    configuration: ShadowEvidencePlanConfiguration
    idempotency_key: str
    requested_at: datetime
    operator_note: str = "local finite read-only SHADOW evidence plan"
    requested_market_tickers: tuple[str, ...] = ()
    validated_cohort_candidates: tuple[ValidatedShadowCohortCandidate, ...] = ()
    atomic_operation_request_hash: str | None = None
    atomic_exclusion_configuration_hash: str | None = None
    atomic_exclusions: AtomicShadowPlanExclusions | None = None
    atomic_terminal_state: AtomicShadowLaunchState | None = None
    atomic_terminal_summary: AtomicShadowPlanLaunchSummary | None = None

    def __post_init__(self) -> None:
        _text(self.campaign_id, label="campaign ID", maximum=160)
        _text(self.name, label="shadow plan name", maximum=160)
        _text(self.idempotency_key, label="shadow plan idempotency key", maximum=160)
        _text(self.operator_note, label="shadow plan operator note", maximum=500)
        _aware(self.requested_at, label="shadow plan requested timestamp")
        if self.requested_market_tickers and self.validated_cohort_candidates:
            raise ValueValidationError(
                "shadow plan accepts either explicit tickers or validated cohort candidates"
            )
        for label, value in (
            ("atomic shadow plan operation hash", self.atomic_operation_request_hash),
            (
                "atomic shadow plan exclusion configuration hash",
                self.atomic_exclusion_configuration_hash,
            ),
        ):
            if value is not None and len(value) != 64:
                raise ValueValidationError(f"{label} must be a SHA-256 digest")
        atomic_values = (
            self.atomic_operation_request_hash,
            self.atomic_exclusion_configuration_hash,
            self.atomic_exclusions,
        )
        if any(value is not None for value in atomic_values) and not all(
            value is not None for value in atomic_values
        ):
            raise ValueValidationError(
                "atomic shadow plan requires operation, exclusion configuration, "
                "and exclusion evidence"
            )
        terminal_values = (self.atomic_terminal_state, self.atomic_terminal_summary)
        if any(value is not None for value in terminal_values) and not all(
            value is not None for value in terminal_values
        ):
            raise ValueValidationError(
                "atomic shadow plan terminal state and summary must be supplied together"
            )
        if self.atomic_terminal_state is not None:
            if self.atomic_terminal_state is not AtomicShadowLaunchState.CREATED_PLAN:
                raise ValueValidationError("atomic plan terminal state must be CREATED_PLAN")
            if not all(value is not None for value in atomic_values):
                raise ValueValidationError(
                    "atomic plan terminal evidence requires atomic provenance"
                )
        if self.requested_market_tickers:
            if not 3 <= len(self.requested_market_tickers) <= 50:
                raise ValueValidationError(
                    "explicit shadow plan market tickers must contain between three and 50 values"
                )
            tickers = tuple(
                sorted(
                    _text(ticker, label="explicit shadow plan market ticker", maximum=300)
                    for ticker in self.requested_market_tickers
                )
            )
            if any(any(character.isspace() for character in ticker) for ticker in tickers):
                raise ValueValidationError(
                    "explicit shadow plan market tickers cannot contain whitespace"
                )
            if len(set(tickers)) != len(tickers):
                raise ValueValidationError("explicit shadow plan market tickers must be unique")
            if len(tickers) != self.configuration.market_count:
                raise ValueValidationError(
                    "explicit shadow plan ticker count must equal the configured market count"
                )
            object.__setattr__(self, "requested_market_tickers", tickers)
        if self.validated_cohort_candidates:
            if not 3 <= len(self.validated_cohort_candidates) <= 50:
                raise ValueValidationError(
                    "validated shadow cohort must contain between three and 50 candidates"
                )
            if len(self.validated_cohort_candidates) != self.configuration.market_count:
                raise ValueValidationError(
                    "validated shadow cohort count must equal the configured market count"
                )
            snapshot_ids = tuple(
                candidate.market_snapshot_id for candidate in self.validated_cohort_candidates
            )
            market_tickers = tuple(
                candidate.market_ticker for candidate in self.validated_cohort_candidates
            )
            if len(set(snapshot_ids)) != len(snapshot_ids) or len(set(market_tickers)) != len(
                market_tickers
            ):
                raise ValueValidationError(
                    "validated shadow cohort requires unique exact market identities"
                )
            canonical = tuple(
                sorted(
                    self.validated_cohort_candidates,
                    key=lambda candidate: (
                        candidate.event_ticker,
                        candidate.series_ticker,
                        candidate.market_ticker,
                        candidate.market_snapshot_id,
                    ),
                )
            )
            object.__setattr__(self, "validated_cohort_candidates", canonical)
        elif self.atomic_operation_request_hash is not None:
            raise ValueValidationError(
                "atomic shadow plan operation hash requires validated cohort candidates"
            )

    @property
    def canonical_request_hash(self) -> str:
        return content_digest(
            {
                "campaign_id": self.campaign_id,
                "name": self.name,
                "configuration": self.configuration,
                "idempotency_key": self.idempotency_key,
                "operator_note": self.operator_note,
                "requested_market_tickers": self.requested_market_tickers,
                "validated_cohort_candidates": self.validated_cohort_candidates,
                "atomic_operation_request_hash": self.atomic_operation_request_hash,
                "atomic_exclusion_configuration_hash": self.atomic_exclusion_configuration_hash,
                "atomic_exclusions": self.atomic_exclusions,
            }
        )


@dataclass(frozen=True, slots=True)
class AtomicShadowPlanExclusions:
    """Canonical frozen-cohort exclusions resolved before any provider request."""

    supplied_prior_plan_ids: tuple[str, ...] = ()
    resolved_prior_plan_ids: tuple[str, ...] = ()
    prior_plan_market_tickers: tuple[str, ...] = ()
    prior_plan_event_tickers: tuple[str, ...] = ()
    active_plan_market_tickers: tuple[str, ...] = ()
    exclude_prior_plan_events: bool = False
    exclude_active_plan_markets: bool = True

    def __post_init__(self) -> None:
        def normalized(values: tuple[str, ...], *, label: str, maximum: int) -> tuple[str, ...]:
            if len(values) > maximum:
                raise ValueValidationError(f"{label} exceeds its bounded maximum")
            parsed = tuple(sorted(_text(value, label=label, maximum=300) for value in values))
            if any(any(character.isspace() for character in value) for value in parsed):
                raise ValueValidationError(f"{label} cannot contain whitespace")
            if len(parsed) != len(set(parsed)):
                raise ValueValidationError(f"{label} must be unique")
            return parsed

        supplied = normalized(
            self.supplied_prior_plan_ids,
            label="atomic exclusion prior plan ID",
            maximum=ATOMIC_EXCLUSION_MAXIMUM_PRIOR_PLANS,
        )
        resolved = normalized(
            self.resolved_prior_plan_ids,
            label="atomic exclusion resolved prior plan ID",
            maximum=ATOMIC_EXCLUSION_MAXIMUM_PRIOR_PLANS,
        )
        if supplied != resolved:
            raise ValueValidationError(
                "atomic exclusion resolution must exactly match supplied prior plans"
            )
        object.__setattr__(self, "supplied_prior_plan_ids", supplied)
        object.__setattr__(self, "resolved_prior_plan_ids", resolved)
        object.__setattr__(
            self,
            "prior_plan_market_tickers",
            normalized(
                self.prior_plan_market_tickers,
                label="atomic exclusion prior plan market ticker",
                maximum=ATOMIC_EXCLUSION_MAXIMUM_MARKETS,
            ),
        )
        object.__setattr__(
            self,
            "prior_plan_event_tickers",
            normalized(
                self.prior_plan_event_tickers,
                label="atomic exclusion prior plan event ticker",
                maximum=ATOMIC_EXCLUSION_MAXIMUM_EVENTS,
            ),
        )
        object.__setattr__(
            self,
            "active_plan_market_tickers",
            normalized(
                self.active_plan_market_tickers,
                label="atomic exclusion active plan market ticker",
                maximum=ATOMIC_EXCLUSION_MAXIMUM_MARKETS,
            ),
        )
        if not isinstance(self.exclude_prior_plan_events, bool) or not isinstance(
            self.exclude_active_plan_markets, bool
        ):
            raise ValueValidationError("atomic exclusion flags must be boolean")

    @property
    def excluded_market_tickers(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.prior_plan_market_tickers) | set(self.active_plan_market_tickers))
        )

    @property
    def excluded_event_tickers(self) -> tuple[str, ...]:
        return self.prior_plan_event_tickers if self.exclude_prior_plan_events else ()

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)


class ShadowOperationsStore(Protocol):
    """Narrow persistence contract; it intentionally has no financial methods."""

    def create_plan(
        self, command: CreateShadowEvidencePlanCommand
    ) -> tuple[dict[str, object], bool]: ...

    def plan_for_atomic_launch(
        self,
        *,
        campaign_id: str,
        idempotency_key: str,
        exclusion_configuration_hash: str,
    ) -> dict[str, object] | None: ...

    def reserve_atomic_launch_operation(
        self,
        *,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        operation_id: str,
        operation_request_hash: str,
        exclusions: AtomicShadowPlanExclusions,
    ) -> AtomicShadowLaunchOperation: ...

    def complete_atomic_no_plan_operation(
        self,
        *,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        operation_id: str,
        operation_request_hash: str,
        exclusions: AtomicShadowPlanExclusions,
        state: AtomicShadowLaunchState,
        summary: AtomicShadowPlanLaunchSummary,
    ) -> AtomicShadowLaunchOperation: ...

    def resolve_atomic_launch_exclusions(
        self,
        *,
        campaign_id: str,
        prior_plan_ids: tuple[str, ...],
        exclude_prior_plan_events: bool,
        exclude_active_plan_markets: bool,
    ) -> AtomicShadowPlanExclusions: ...

    def control_plan(
        self,
        *,
        plan_id: str,
        action: str,
        idempotency_key: str,
        reason: str,
        requested_at: datetime,
    ) -> tuple[dict[str, object], bool]: ...

    def reconcile_stale_plans(self, *, now: datetime) -> int: ...

    def evaluate_public_outcome_fact(
        self, *, outcome_fact_id: str, evaluated_at: datetime
    ) -> tuple[int, int]: ...

    def performance_report(
        self,
        *,
        campaign_id: str,
        strategy_id: str | None,
        plan_id: str | None,
        requested_at: datetime,
    ) -> dict[str, object]: ...


class CreateShadowEvidencePlan:
    def __init__(self, store: ShadowOperationsStore) -> None:
        self._store = store

    def run(self, command: CreateShadowEvidencePlanCommand) -> tuple[dict[str, object], bool]:
        return self._store.create_plan(command)


@dataclass(frozen=True, slots=True)
class DiscoverAndCreateShadowEvidencePlanCommand:
    """The finite public-data controls for one atomic validated-cohort launch."""

    campaign_id: str
    name: str
    window_start: datetime
    window_end: datetime
    maximum_pages: int
    maximum_inspected_markets: int
    maximum_enrichments: int
    target_markets: int
    minimum_markets: int
    minimum_events: int
    maximum_markets_per_event: int
    maximum_markets_per_series: int
    iterations: int
    interval_seconds: int
    orderbook_depth: int
    idempotency_key: str
    decision_at: datetime
    maximum_orderbook_preflights: int = 80
    microstructure_configuration_version: str = "orderbook-microstructure-shadow-v1"
    prior_plan_ids: tuple[str, ...] = ()
    exclude_prior_plan_markets: bool = True
    exclude_prior_plan_events: bool = False
    exclude_active_plan_markets: bool = True
    operator_note: str = "atomic validated local read-only SHADOW evidence plan"
    timeout_seconds: int = 10
    strategy_ids: tuple[str, ...] = (
        "market-consensus-baseline-v1",
        "kalshi-orderbook-microstructure-v1",
        "kalshi-market-dynamics-v1",
    )

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("campaign ID", self.campaign_id, 160),
            ("shadow plan name", self.name, 160),
            ("shadow plan idempotency key", self.idempotency_key, 160),
            ("shadow plan operator note", self.operator_note, 500),
        ):
            _text(value, label=label, maximum=maximum)
        start = _aware(self.window_start, label="shadow plan window start")
        end = _aware(self.window_end, label="shadow plan window end")
        decided = _aware(self.decision_at, label="shadow plan decision timestamp")
        if end <= start or end - start > timedelta(days=30):
            raise ValueValidationError("shadow plan window must be positive and at most 30 days")
        if not 1 <= self.maximum_pages <= 5:
            raise ValueValidationError(
                "atomic shadow plan maximum pages must be between one and five"
            )
        if not 20 <= self.maximum_inspected_markets <= 500:
            raise ValueValidationError(
                "atomic shadow plan inspected markets must be between 20 and 500"
            )
        if not 3 <= self.target_markets <= 50:
            raise ValueValidationError(
                "atomic shadow plan target markets must be between three and 50"
            )
        if not 3 <= self.minimum_markets <= self.target_markets:
            raise ValueValidationError(
                "atomic shadow plan minimum markets must be between three and target"
            )
        if not 3 <= self.minimum_events <= self.target_markets:
            raise ValueValidationError(
                "atomic shadow plan minimum events must be between three and target"
            )
        if not self.target_markets <= self.maximum_enrichments <= 80:
            raise ValueValidationError(
                "atomic shadow plan enrichments must cover target markets and be at most 80"
            )
        if not self.target_markets <= self.maximum_orderbook_preflights <= 80:
            raise ValueValidationError(
                "atomic shadow plan orderbook preflights must cover target markets "
                "and be at most 80"
            )
        if not 1 <= self.maximum_markets_per_event <= 5:
            raise ValueValidationError(
                "atomic shadow plan per-event cap must be between one and five"
            )
        if not 1 <= self.maximum_markets_per_series <= 10:
            raise ValueValidationError(
                "atomic shadow plan per-series cap must be between one and ten"
            )
        if not 3 <= self.iterations <= 288:
            raise ValueValidationError(
                "atomic shadow plan iterations must be between three and 288"
            )
        if not 30 <= self.interval_seconds <= 86400:
            raise ValueValidationError("atomic shadow plan interval must be between 30 and 86400")
        if self.iterations * self.interval_seconds > 30 * 24 * 60 * 60:
            raise ValueValidationError("atomic shadow plan total duration must be at most 30 days")
        if not 1 <= self.orderbook_depth <= 20:
            raise ValueValidationError(
                "atomic shadow plan orderbook depth must be between one and 20"
            )
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueValidationError("atomic shadow plan timeout must be between one and 60")
        expected = tuple(
            sorted(
                (
                    "market-consensus-baseline-v1",
                    "kalshi-orderbook-microstructure-v1",
                    "kalshi-market-dynamics-v1",
                )
            )
        )
        if tuple(sorted(self.strategy_ids)) != expected:
            raise ValueValidationError("atomic shadow plan requires the three fixed strategy IDs")
        self.microstructure_configuration(market_count=self.target_markets)
        prior_plan_ids = tuple(
            sorted(
                _text(value, label="atomic exclusion prior plan ID", maximum=160)
                for value in self.prior_plan_ids
            )
        )
        if len(prior_plan_ids) > ATOMIC_EXCLUSION_MAXIMUM_PRIOR_PLANS:
            raise ValueValidationError("atomic exclusion prior plan IDs exceed twenty")
        if any(any(character.isspace() for character in value) for value in prior_plan_ids):
            raise ValueValidationError("atomic exclusion prior plan IDs cannot contain whitespace")
        if len(prior_plan_ids) != len(set(prior_plan_ids)):
            raise ValueValidationError("atomic exclusion prior plan IDs must be unique")
        for exclusion_flag in (
            self.exclude_prior_plan_markets,
            self.exclude_prior_plan_events,
            self.exclude_active_plan_markets,
        ):
            if not isinstance(exclusion_flag, bool):
                raise ValueValidationError("atomic exclusion flags must be boolean")
        if prior_plan_ids and not self.exclude_prior_plan_markets:
            raise ValueValidationError(
                "prior plan market exclusion is mandatory when prior plans are supplied"
            )
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(self, "decision_at", decided)
        object.__setattr__(self, "prior_plan_ids", prior_plan_ids)

    def microstructure_configuration(
        self, *, market_count: int
    ) -> OrderbookMicrostructureStrategyConfig:
        """Return the explicitly requested, plan-bound Microstructure configuration."""

        from apex.domain.orderbook_microstructure import (
            CONFIGURATION_VERSION,
            PUBLISHED_FEE_CONFIGURATION_VERSION,
            OrderbookMicrostructureStrategyConfig,
            published_fee_microstructure_v2_configuration,
        )

        if self.microstructure_configuration_version == CONFIGURATION_VERSION:
            return OrderbookMicrostructureStrategyConfig(
                maximum_markets_per_invocation=market_count,
                orderbook_depth=self.orderbook_depth,
                minimum_observations_per_market=4,
            )
        if self.microstructure_configuration_version == PUBLISHED_FEE_CONFIGURATION_VERSION:
            return published_fee_microstructure_v2_configuration(
                maximum_markets_per_invocation=market_count,
                orderbook_depth=self.orderbook_depth,
                minimum_observations_per_market=4,
            )
        raise ValueValidationError("atomic shadow plan microstructure configuration is unsupported")

    @property
    def requires_fee_compatible_preflight(self) -> bool:
        from apex.domain.orderbook_microstructure import PUBLISHED_FEE_CONFIGURATION_VERSION

        return self.microstructure_configuration_version == PUBLISHED_FEE_CONFIGURATION_VERSION

    @property
    def requested_microstructure_configuration_hash(self) -> str:
        configuration = self.microstructure_configuration(market_count=self.target_markets)
        return configuration.canonical_hash.value

    @property
    def exclusion_configuration_hash(self) -> str:
        """Stable caller-supplied exclusion identity, excluding the wall clock."""

        return content_digest(
            {
                "campaign_id": self.campaign_id,
                "name": self.name,
                "window_start": self.window_start,
                "window_end": self.window_end,
                "maximum_pages": self.maximum_pages,
                "maximum_inspected_markets": self.maximum_inspected_markets,
                "maximum_enrichments": self.maximum_enrichments,
                "maximum_orderbook_preflights": self.maximum_orderbook_preflights,
                "target_markets": self.target_markets,
                "minimum_markets": self.minimum_markets,
                "minimum_events": self.minimum_events,
                "maximum_markets_per_event": self.maximum_markets_per_event,
                "maximum_markets_per_series": self.maximum_markets_per_series,
                "iterations": self.iterations,
                "interval_seconds": self.interval_seconds,
                "orderbook_depth": self.orderbook_depth,
                "microstructure_configuration_version": self.microstructure_configuration_version,
                "requested_microstructure_configuration_hash": (
                    self.requested_microstructure_configuration_hash
                ),
                "idempotency_key": self.idempotency_key,
                "operator_note": self.operator_note,
                "timeout_seconds": self.timeout_seconds,
                "strategy_ids": self.strategy_ids,
                "prior_plan_ids": self.prior_plan_ids,
                "exclude_prior_plan_markets": self.exclude_prior_plan_markets,
                "exclude_prior_plan_events": self.exclude_prior_plan_events,
                "exclude_active_plan_markets": self.exclude_active_plan_markets,
            }
        )

    def operation_request_hash(self, exclusions: AtomicShadowPlanExclusions) -> str:
        """Bind idempotency to the canonical durable exclusion sets before any GET."""

        return content_digest(
            {
                "exclusion_configuration_hash": self.exclusion_configuration_hash,
                "exclusions": exclusions,
            }
        )


@dataclass(frozen=True, slots=True)
class ExactShadowCohortEnrichment:
    """Exact event/series/market evidence returned by one enrichment attempt."""

    list_market_snapshot_id: str
    market_snapshot_id: str
    event_snapshot_id: str
    event_raw_response_id: str
    series_raw_response_id: str
    exact_market_raw_response_id: str
    fee_snapshot_id: str | None
    provider: str
    market_ticker: str
    event_ticker: str
    series_ticker: str
    rule_content_hash: str
    settlement_source_identity: str
    lifecycle_state: str
    binary_structure_identity: str
    observed_at: datetime
    available_at: datetime
    close_at: datetime
    market_lineage_hash: str
    event_lineage_hash: str
    series_lineage_hash: str
    fee_lineage_hash: str | None
    enriched_at: datetime
    preflight_orderbook: PublicOrderbookSnapshot | None = None
    preflight_levels: tuple[PublicOrderbookLevel, ...] = ()
    preflight_fee: SeriesFeeSnapshot | None = None
    fee_applicability: PublishedFeeScheduleApplicability | None = None

    def validated_candidate(self) -> ValidatedShadowCohortCandidate:
        return ValidatedShadowCohortCandidate(
            list_market_snapshot_id=self.list_market_snapshot_id,
            market_snapshot_id=self.market_snapshot_id,
            event_snapshot_id=self.event_snapshot_id,
            event_raw_response_id=self.event_raw_response_id,
            series_raw_response_id=self.series_raw_response_id,
            exact_market_raw_response_id=self.exact_market_raw_response_id,
            fee_snapshot_id=self.fee_snapshot_id,
            provider=self.provider,
            market_ticker=self.market_ticker,
            event_ticker=self.event_ticker,
            series_ticker=self.series_ticker,
            rule_content_hash=self.rule_content_hash,
            settlement_source_identity=self.settlement_source_identity,
            lifecycle_state=self.lifecycle_state,
            binary_structure_identity=self.binary_structure_identity,
            observed_at=self.observed_at,
            available_at=self.available_at,
            close_at=self.close_at,
            market_lineage_hash=self.market_lineage_hash,
            event_lineage_hash=self.event_lineage_hash,
            series_lineage_hash=self.series_lineage_hash,
            fee_lineage_hash=self.fee_lineage_hash,
            enriched_at=self.enriched_at,
            preflight_orderbook=self.preflight_orderbook,
            preflight_levels=self.preflight_levels,
            preflight_fee=self.preflight_fee,
            fee_applicability=self.fee_applicability,
        )


@dataclass(frozen=True, slots=True)
class ExactShadowCohortEnrichmentAttempt:
    """One terminal exact-enrichment attempt, including all public GETs used."""

    selected: ExactShadowCohortEnrichment | None
    reason_codes: tuple[str, ...]
    provider_get_count: int
    event_enrichment_succeeded: bool
    series_enrichment_succeeded: bool
    exact_market_refresh_succeeded: bool
    orderbook_preflight_attempted: bool = False

    def __post_init__(self) -> None:
        if self.provider_get_count < 1:
            raise ValueValidationError(
                "exact enrichment attempt must retain its provider GET count"
            )
        if self.selected is not None and not self.exact_market_refresh_succeeded:
            raise ValueValidationError("selected exact enrichment requires an exact market refresh")
        if self.selected is not None and self.reason_codes:
            raise ValueValidationError("selected exact enrichment cannot carry rejection codes")
        if self.selected is None and not self.reason_codes:
            raise ValueValidationError("rejected exact enrichment requires a reason code")


@dataclass(frozen=True, slots=True)
class AtomicShadowPlanLaunchSummary:
    """Terminal audit projection; it never contains provider payload bytes."""

    operation_id: str
    operation_request_hash: str
    discovery_time: datetime
    window_start: datetime
    window_end: datetime
    pages_requested: int
    pages_completed: int
    markets_received: int
    structurally_eligible: int
    ranked_candidates: int
    list_two_sided_quote_candidates: int
    list_incomplete_quote_candidates: int
    list_spread_buckets: tuple[tuple[str, int], ...]
    enrichment_attempts: int
    enrichment_successes: int
    enrichment_failures: tuple[tuple[str, int], ...]
    event_enrichment_successes: int
    series_enrichment_successes: int
    exact_market_refresh_successes: int
    orderbook_preflights_attempted: int
    fee_compatible_candidates: int
    fee_incompatibility_reasons: tuple[tuple[str, int], ...]
    fee_applicability_states: tuple[tuple[str, int], ...]
    two_sided_books: int
    one_sided_books: int
    spread_compatible_books: int
    visible_quantity_compatible_books: int
    schedule_time_compatible_books: int
    exact_spread_buckets: tuple[tuple[str, int], ...]
    list_exact_spread_disagreement_count: int
    fee_and_spread_compatible_candidates: int
    supplied_prior_plan_ids: tuple[str, ...]
    resolved_prior_plan_ids: tuple[str, ...]
    exclusion_configuration_hash: str
    exclusion_set_hash: str
    prior_plan_market_exclusions: int
    prior_plan_event_exclusions: int
    active_plan_market_exclusions: int
    candidates_excluded_before_enrichment_by_market: int
    candidates_excluded_before_enrichment_by_event: int
    candidates_excluded_after_enrichment_by_market: int
    candidates_excluded_after_enrichment_by_event: int
    duplicate_event_exclusions: int
    series_cap_exclusions: int
    selected_market_tickers: tuple[str, ...]
    selected_event_tickers: tuple[str, ...]
    selected_series_tickers: tuple[str, ...]
    selected_count: int
    selected_event_count: int
    selected_cohort_hash: str
    selected_markets_overlapping_prior_plans: int
    selected_events_overlapping_prior_plans: int
    selected_markets_overlapping_active_plans: int
    prior_event_overlap_count: int
    new_distinct_event_count: int
    combined_distinct_event_coverage: int
    selected_configuration_versions: tuple[tuple[str, str], ...]
    selected_configuration_hashes: tuple[tuple[str, str], ...]
    no_plan_reason: str | None
    plan_id: str | None
    provider_get_count: int
    provider_order_count: int
    financial_state_before: str
    financial_state_after: str
    idempotent_replay: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "operation_request_hash": self.operation_request_hash,
            "discovery_time": self.discovery_time.isoformat(),
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "pages_requested": self.pages_requested,
            "pages_completed": self.pages_completed,
            "markets_received": self.markets_received,
            "structurally_eligible": self.structurally_eligible,
            "ranked_candidates": self.ranked_candidates,
            "list_two_sided_quote_candidates": self.list_two_sided_quote_candidates,
            "list_incomplete_quote_candidates": self.list_incomplete_quote_candidates,
            "list_spread_buckets": dict(self.list_spread_buckets),
            "enrichment_attempts": self.enrichment_attempts,
            "enrichment_successes": self.enrichment_successes,
            "enrichment_failures": dict(self.enrichment_failures),
            "event_enrichment_successes": self.event_enrichment_successes,
            "series_enrichment_successes": self.series_enrichment_successes,
            "exact_market_refresh_successes": self.exact_market_refresh_successes,
            "orderbook_preflights_attempted": self.orderbook_preflights_attempted,
            "orderbooks_inspected": self.orderbook_preflights_attempted,
            "fee_compatible_candidates": self.fee_compatible_candidates,
            "fee_incompatibility_reasons": dict(self.fee_incompatibility_reasons),
            "fee_applicability_states": dict(self.fee_applicability_states),
            "two_sided_books": self.two_sided_books,
            "one_sided_books": self.one_sided_books,
            "spread_compatible_books": self.spread_compatible_books,
            "visible_quantity_compatible_books": self.visible_quantity_compatible_books,
            "schedule_time_compatible_books": self.schedule_time_compatible_books,
            "exact_spread_buckets": dict(self.exact_spread_buckets),
            "list_exact_spread_disagreement_count": self.list_exact_spread_disagreement_count,
            "fee_and_spread_compatible_candidates": self.fee_and_spread_compatible_candidates,
            "supplied_prior_plan_ids": self.supplied_prior_plan_ids,
            "resolved_prior_plan_ids": self.resolved_prior_plan_ids,
            "exclusion_configuration_hash": self.exclusion_configuration_hash,
            "exclusion_set_hash": self.exclusion_set_hash,
            "prior_plan_market_exclusions": self.prior_plan_market_exclusions,
            "prior_plan_event_exclusions": self.prior_plan_event_exclusions,
            "active_plan_market_exclusions": self.active_plan_market_exclusions,
            "candidates_excluded_before_enrichment_by_market": (
                self.candidates_excluded_before_enrichment_by_market
            ),
            "candidates_excluded_before_enrichment_by_event": (
                self.candidates_excluded_before_enrichment_by_event
            ),
            "candidates_excluded_after_enrichment_by_market": (
                self.candidates_excluded_after_enrichment_by_market
            ),
            "candidates_excluded_after_enrichment_by_event": (
                self.candidates_excluded_after_enrichment_by_event
            ),
            "duplicate_event_exclusions": self.duplicate_event_exclusions,
            "series_cap_exclusions": self.series_cap_exclusions,
            "selected_market_tickers": self.selected_market_tickers,
            "selected_event_tickers": self.selected_event_tickers,
            "selected_series_tickers": self.selected_series_tickers,
            "selected_count": self.selected_count,
            "selected_event_count": self.selected_event_count,
            "selected_cohort_hash": self.selected_cohort_hash,
            "selected_markets_overlapping_prior_plans": (
                self.selected_markets_overlapping_prior_plans
            ),
            "selected_events_overlapping_prior_plans": self.selected_events_overlapping_prior_plans,
            "selected_markets_overlapping_active_plans": (
                self.selected_markets_overlapping_active_plans
            ),
            "prior_event_overlap_count": self.prior_event_overlap_count,
            "new_distinct_event_count": self.new_distinct_event_count,
            "combined_distinct_event_coverage": self.combined_distinct_event_coverage,
            "selected_configuration_versions": dict(self.selected_configuration_versions),
            "selected_configuration_hashes": dict(self.selected_configuration_hashes),
            "no_plan_reason": self.no_plan_reason,
            "plan_id": self.plan_id,
            "provider_get_count": self.provider_get_count,
            "provider_order_count": self.provider_order_count,
            "financial_state_before": self.financial_state_before,
            "financial_state_after": self.financial_state_after,
            "idempotent_replay": self.idempotent_replay,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AtomicShadowPlanLaunchSummary:
        """Rehydrate a bounded terminal projection retained in immutable audit data."""

        payload = dict(value)
        payload.pop("orderbooks_inspected", None)
        for field in ("discovery_time", "window_start", "window_end"):
            parsed = datetime.fromisoformat(str(payload[field]))
            payload[field] = _aware(parsed, label=f"atomic launch {field}")
        for field in (
            "list_spread_buckets",
            "enrichment_failures",
            "fee_incompatibility_reasons",
            "fee_applicability_states",
            "exact_spread_buckets",
        ):
            raw = payload[field]
            if not isinstance(raw, Mapping):
                raise ValueValidationError("retained atomic launch summary mapping is invalid")
            payload[field] = tuple(sorted((str(key), int(item)) for key, item in raw.items()))
        for field in ("selected_configuration_versions", "selected_configuration_hashes"):
            raw = payload[field]
            if not isinstance(raw, Mapping):
                raise ValueValidationError("retained atomic launch summary mapping is invalid")
            payload[field] = tuple(sorted((str(key), str(item)) for key, item in raw.items()))
        for field in (
            "supplied_prior_plan_ids",
            "resolved_prior_plan_ids",
            "selected_market_tickers",
            "selected_event_tickers",
            "selected_series_tickers",
        ):
            raw = payload[field]
            if not isinstance(raw, (list, tuple)):
                raise ValueValidationError("retained atomic launch summary collection is invalid")
            payload[field] = tuple(str(item) for item in raw)
        return cls(**cast(dict[str, Any], payload))


@dataclass(frozen=True, slots=True)
class AtomicShadowLaunchOperation:
    """A durable operation reservation or terminal replay projection."""

    operation_id: str
    operation_request_hash: str
    exclusions: AtomicShadowPlanExclusions
    state: AtomicShadowLaunchState
    created_now: bool
    result_payload: dict[str, object] | None = None
    plan: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _text(self.operation_id, label="atomic launch operation ID", maximum=160)
        if len(self.operation_request_hash) != 64:
            raise ValueValidationError("atomic launch operation hash must be a SHA-256 digest")
        if self.state is AtomicShadowLaunchState.RUNNING:
            if self.result_payload is not None or self.plan is not None:
                raise ValueValidationError("running atomic launch cannot have a terminal result")
        elif self.result_payload is None:
            raise ValueValidationError("terminal atomic launch requires a retained result")
        if self.state is AtomicShadowLaunchState.CREATED_PLAN and self.plan is None:
            raise ValueValidationError("created atomic launch requires its plan")


@dataclass(frozen=True, slots=True)
class AtomicShadowPlanLaunchResult:
    summary: AtomicShadowPlanLaunchSummary
    plan: dict[str, object] | None
    state: AtomicShadowLaunchState = AtomicShadowLaunchState.NO_PLAN
    result_hash: str | None = None


@dataclass(frozen=True, slots=True)
class _AtomicCohortPreflight:
    """Selection-only v2 compatibility result for one retained exact market."""

    reason_codes: tuple[str, ...]
    fee_compatible: bool
    two_sided: bool
    spread_compatible: bool
    visible_quantity_compatible: bool
    schedule_time_compatible: bool
    fee_applicability: PublishedFeeScheduleApplicability | None
    exact_spread: Decimal | None


class DiscoverAndCreateShadowEvidencePlan:
    """Discover, validate, diversify, and atomically freeze one finite cohort.

    The exact-enrichment callback owns public GETs and immutable public-evidence
    persistence.  This service never asks it to fetch a selected market twice;
    the same validated value objects flow directly into ``create_plan``.
    """

    def __init__(
        self,
        *,
        store: ShadowOperationsStore,
        discover: Callable[[PublicMarketScanRequest], PublicMarketScanSummary],
        select_candidates: Callable[[str, int], tuple[CaptureMarketCandidate, ...]],
        enrich_candidate: Callable[
            [CaptureMarketCandidate, str], ExactShadowCohortEnrichmentAttempt
        ],
    ) -> None:
        self._store = store
        self._discover = discover
        self._select_candidates = select_candidates
        self._enrich_candidate = enrich_candidate

    @staticmethod
    def _rank(
        candidate: CaptureMarketCandidate,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
    ) -> tuple[int, Decimal, int, Decimal, Decimal, str, str]:
        """Rank list evidence as a hint; the exact preflight remains decisive."""

        def quantity(value: Decimal | None) -> Decimal:
            return value if value is not None else Decimal("-1")

        list_spread = candidate.list_immediate_cross_spread
        liquidity_or_interest = (
            candidate.liquidity if candidate.liquidity is not None else candidate.open_interest
        )
        closes_in_window = (
            candidate.close_time is not None
            and command.window_start <= candidate.close_time <= command.window_end
        )
        return (
            0 if candidate.has_complete_list_two_sided_quotes else 1,
            list_spread if list_spread is not None else Decimal("Infinity"),
            0 if closes_in_window else 1,
            -quantity(liquidity_or_interest),
            -quantity(candidate.volume_24h),
            candidate.provider_market_ticker,
            candidate.market_snapshot_id,
        )

    @classmethod
    def _ranked_candidates(
        cls,
        *,
        candidates: tuple[CaptureMarketCandidate, ...],
        command: DiscoverAndCreateShadowEvidencePlanCommand,
    ) -> tuple[CaptureMarketCandidate, ...]:
        """Apply the deterministic list-evidence ordering before exact work.

        Exact event and series diversity is enforced only after enrichment.
        Interleaving list event tickers here would spend the finite enrichment
        budget on a wider book ahead of a better retained quote.
        """

        return tuple(sorted(candidates, key=lambda item: cls._rank(item, command)))

    @staticmethod
    def _spread_bucket(spread: Decimal | None, maximum_spread: Decimal) -> str:
        if spread is None:
            return "MISSING"
        if spread <= maximum_spread:
            return "AT_OR_BELOW_MAXIMUM"
        return "ABOVE_MAXIMUM"

    @staticmethod
    def _select_diverse(
        *,
        candidates: tuple[ValidatedShadowCohortCandidate, ...],
        command: DiscoverAndCreateShadowEvidencePlanCommand,
    ) -> tuple[tuple[ValidatedShadowCohortCandidate, ...], int, int]:
        selected: list[ValidatedShadowCohortCandidate] = []
        event_counts: dict[str, int] = {}
        series_counts: dict[str, int] = {}
        duplicate_event_exclusions = 0
        series_cap_exclusions = 0

        def admit(candidate: ValidatedShadowCohortCandidate) -> bool:
            nonlocal duplicate_event_exclusions, series_cap_exclusions
            if event_counts.get(candidate.event_ticker, 0) >= command.maximum_markets_per_event:
                duplicate_event_exclusions += 1
                return False
            if series_counts.get(candidate.series_ticker, 0) >= command.maximum_markets_per_series:
                series_cap_exclusions += 1
                return False
            selected.append(candidate)
            event_counts[candidate.event_ticker] = event_counts.get(candidate.event_ticker, 0) + 1
            series_counts[candidate.series_ticker] = (
                series_counts.get(candidate.series_ticker, 0) + 1
            )
            return True

        # First admit one market per event where possible.  This makes the
        # minimum-event rule deliberate rather than an accidental side effect
        # of an activity ranking dominated by one event.
        seen_events: set[str] = set()
        for candidate in candidates:
            if len({value.event_ticker for value in selected}) >= command.minimum_events:
                break
            if candidate.event_ticker in seen_events:
                continue
            seen_events.add(candidate.event_ticker)
            admit(candidate)
        selected_ids = {value.market_snapshot_id for value in selected}
        for candidate in candidates:
            if len(selected) >= command.target_markets:
                break
            if candidate.market_snapshot_id in selected_ids:
                continue
            if admit(candidate):
                selected_ids.add(candidate.market_snapshot_id)
        return tuple(selected), duplicate_event_exclusions, series_cap_exclusions

    @staticmethod
    def _fee_compatible_preflight(
        *,
        exact: ExactShadowCohortEnrichment,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        configuration: OrderbookMicrostructureStrategyConfig,
    ) -> _AtomicCohortPreflight:
        """Validate one exact book and fee fact without producing strategy artifacts."""

        reasons: set[str] = set()
        schedule = configuration.published_fee_schedule
        fee = exact.preflight_fee
        orderbook = exact.preflight_orderbook
        levels = exact.preflight_levels
        schedule_time_compatible = False
        fee_applicability: PublishedFeeScheduleApplicability | None = None

        if (
            schedule is None
            or schedule.source_url != KALSHI_PUBLISHED_FEE_SCHEDULE.source_url
            or schedule.schedule_identity != KALSHI_PUBLISHED_FEE_SCHEDULE.schedule_identity
        ):
            reasons.add("FEE_SCHEDULE_SOURCE_MISSING")
        elif schedule.source_sha256 != KALSHI_PUBLISHED_FEE_SCHEDULE.source_sha256:
            reasons.add("FEE_SCHEDULE_HASH_MISMATCH")

        if fee is None or schedule is None:
            reasons.add("AUTHORITATIVE_MAPPING_MISSING")
        else:
            if (
                fee.provider is not PublicMarketProvider.KALSHI
                or fee.series_ticker != exact.series_ticker
            ):
                reasons.add("LINEAGE_MISMATCH")
            fee_applicability = reconcile_kalshi_published_fee_schedule(
                provider_fee_type=fee.fee_type,
                series_ticker=fee.series_ticker,
                fee_multiplier=fee.fee_multiplier,
                fee_effective_at=fee.effective_at,
                decision_at=command.decision_at,
                fee_model_identity=fee.fee_model_identity,
                taker_coefficient=fee.taker_coefficient,
                maker_coefficient=fee.maker_coefficient,
                retained_rounding_identity=fee.rounding_identity,
                schedule_identity=fee.schedule_identity,
                schedule_effective_date=fee.schedule_effective_date,
                expected_fee_model_identity=FEE_MODEL_IDENTITY,
                expected_retained_rounding_identity=ROUNDING_IDENTITY,
                schedule=schedule,
            )
            schedule_time_compatible = fee_applicability.is_exact_taker_compatible
            if not fee_applicability.is_exact_taker_compatible:
                state = fee_applicability.state
                if state is FeeScheduleApplicabilityState.MULTIPLIER_TIME_AMBIGUOUS:
                    reasons.add("MULTIPLIER_TIME_AMBIGUOUS")
                elif state is FeeScheduleApplicabilityState.AUTHORITATIVE_MAPPING_MISSING:
                    reasons.add("AUTHORITATIVE_MAPPING_MISSING")
                elif state is FeeScheduleApplicabilityState.SCHEDULE_NOT_APPLICABLE:
                    reasons.add("FEE_TYPE_UNSUPPORTED")
                else:
                    reasons.add("CONFLICTING_FEE_EVIDENCE")

        if (
            exact.provider != PublicMarketProvider.KALSHI.value
            or exact.lifecycle_state != "open"
            or exact.close_at <= command.decision_at
        ):
            reasons.add("MARKET_NO_LONGER_ACTIVE")
        expected_structure = content_digest(
            {"market_snapshot_id": exact.market_snapshot_id, "structure": "BINARY_YES_NO"}
        )
        if exact.binary_structure_identity != expected_structure:
            reasons.add("LINEAGE_MISMATCH")

        yes_bids: tuple[PublicOrderbookLevel, ...] = ()
        no_bids: tuple[PublicOrderbookLevel, ...] = ()
        if orderbook is None:
            reasons.add("ONE_SIDED_BOOK")
        else:
            if (
                orderbook.provider is not PublicMarketProvider.KALSHI
                or orderbook.market_snapshot_id != exact.market_snapshot_id
                or orderbook.provider_market_ticker != exact.market_ticker
                or orderbook.provider_event_ticker != exact.event_ticker
                or orderbook.series_ticker != exact.series_ticker
                or orderbook.maximum_depth != command.orderbook_depth
                or orderbook.data_quality_state != "VALID"
            ):
                reasons.add("LINEAGE_MISMATCH")
            yes_bids = tuple(
                sorted(
                    (level for level in levels if level.side is OrderbookSide.YES_BID),
                    key=lambda level: (-level.bid_price, level.ordinal, level.level_id),
                )[: command.orderbook_depth]
            )
            no_bids = tuple(
                sorted(
                    (level for level in levels if level.side is OrderbookSide.NO_BID),
                    key=lambda level: (-level.bid_price, level.ordinal, level.level_id),
                )[: command.orderbook_depth]
            )
            if any(
                level.orderbook_snapshot_id != orderbook.orderbook_snapshot_id for level in levels
            ):
                reasons.add("LINEAGE_MISMATCH")

        two_sided = bool(yes_bids and no_bids)
        if not two_sided:
            reasons.add("ONE_SIDED_BOOK")
        spread_compatible = False
        visible_quantity_compatible = False
        exact_spread: Decimal | None = None
        if two_sided:
            best_yes = yes_bids[0]
            best_no = no_bids[0]
            derived_yes_ask = ONE - best_no.bid_price
            if best_yes.bid_price > derived_yes_ask:
                reasons.add("LINEAGE_MISMATCH")
            else:
                exact_spread = derived_yes_ask - best_yes.bid_price
            if exact_spread is not None and exact_spread > configuration.maximum_spread:
                reasons.add("SPREAD_TOO_WIDE")
            elif exact_spread is not None:
                spread_compatible = True
            visible_quantity_compatible = (
                min(
                    best_yes.quantity,
                    best_no.quantity,
                    sum((level.quantity for level in yes_bids), Decimal(0)),
                    sum((level.quantity for level in no_bids), Decimal(0)),
                )
                >= configuration.minimum_visible_quantity
            )
            if not visible_quantity_compatible:
                reasons.add("INSUFFICIENT_VISIBLE_QUANTITY")

        fee_compatible = (
            fee_applicability is not None
            and fee_applicability.is_exact_taker_compatible
            and not any(
                reason
                in {
                    "FEE_SCHEDULE_SOURCE_MISSING",
                    "FEE_SCHEDULE_HASH_MISMATCH",
                    "FEE_TYPE_UNSUPPORTED",
                    "AUTHORITATIVE_MAPPING_MISSING",
                    "MULTIPLIER_TIME_AMBIGUOUS",
                    "CONFLICTING_FEE_EVIDENCE",
                }
                for reason in reasons
            )
        )
        return _AtomicCohortPreflight(
            reason_codes=tuple(sorted(reasons)),
            fee_compatible=fee_compatible,
            two_sided=two_sided,
            spread_compatible=spread_compatible,
            visible_quantity_compatible=visible_quantity_compatible,
            schedule_time_compatible=schedule_time_compatible,
            fee_applicability=fee_applicability,
            exact_spread=exact_spread,
        )

    def _summary(
        self,
        *,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        operation_id: str,
        operation_request_hash: str,
        exclusions: AtomicShadowPlanExclusions,
        summary: PublicMarketScanSummary | None,
        ranked_count: int,
        list_two_sided_quote_candidates: int,
        list_incomplete_quote_candidates: int,
        list_spread_buckets: dict[str, int],
        attempts: int,
        successful: int,
        failures: dict[str, int],
        event_successes: int,
        series_successes: int,
        exact_successes: int,
        orderbook_preflights_attempted: int,
        fee_compatible_candidates: int,
        fee_incompatibilities: dict[str, int],
        fee_applicability_states: dict[str, int],
        two_sided_books: int,
        one_sided_books: int,
        spread_compatible_books: int,
        visible_quantity_compatible_books: int,
        schedule_time_compatible_books: int,
        exact_spread_buckets: dict[str, int],
        list_exact_spread_disagreement_count: int,
        fee_and_spread_compatible_candidates: int,
        candidates_excluded_before_enrichment_by_market: int,
        candidates_excluded_before_enrichment_by_event: int,
        candidates_excluded_after_enrichment_by_market: int,
        candidates_excluded_after_enrichment_by_event: int,
        duplicate_event_exclusions: int,
        series_cap_exclusions: int,
        selected: tuple[ValidatedShadowCohortCandidate, ...],
        no_plan_reason: str | None,
        plan: dict[str, object] | None,
        configuration: ShadowEvidencePlanConfiguration | None,
        provider_get_count: int,
        replay: bool,
    ) -> AtomicShadowPlanLaunchSummary:
        return AtomicShadowPlanLaunchSummary(
            operation_id=operation_id,
            operation_request_hash=operation_request_hash,
            discovery_time=command.decision_at,
            window_start=command.window_start,
            window_end=command.window_end,
            pages_requested=command.maximum_pages,
            pages_completed=summary.page_count if summary is not None else 0,
            markets_received=summary.market_count if summary is not None else 0,
            structurally_eligible=summary.eligible_count if summary is not None else 0,
            ranked_candidates=ranked_count,
            list_two_sided_quote_candidates=list_two_sided_quote_candidates,
            list_incomplete_quote_candidates=list_incomplete_quote_candidates,
            list_spread_buckets=tuple(sorted(list_spread_buckets.items())),
            enrichment_attempts=attempts,
            enrichment_successes=successful,
            enrichment_failures=tuple(sorted(failures.items())),
            event_enrichment_successes=event_successes,
            series_enrichment_successes=series_successes,
            exact_market_refresh_successes=exact_successes,
            orderbook_preflights_attempted=orderbook_preflights_attempted,
            fee_compatible_candidates=fee_compatible_candidates,
            fee_incompatibility_reasons=tuple(sorted(fee_incompatibilities.items())),
            fee_applicability_states=tuple(sorted(fee_applicability_states.items())),
            two_sided_books=two_sided_books,
            one_sided_books=one_sided_books,
            spread_compatible_books=spread_compatible_books,
            visible_quantity_compatible_books=visible_quantity_compatible_books,
            schedule_time_compatible_books=schedule_time_compatible_books,
            exact_spread_buckets=tuple(sorted(exact_spread_buckets.items())),
            list_exact_spread_disagreement_count=list_exact_spread_disagreement_count,
            fee_and_spread_compatible_candidates=fee_and_spread_compatible_candidates,
            supplied_prior_plan_ids=exclusions.supplied_prior_plan_ids,
            resolved_prior_plan_ids=exclusions.resolved_prior_plan_ids,
            exclusion_configuration_hash=command.exclusion_configuration_hash,
            exclusion_set_hash=exclusions.canonical_hash,
            prior_plan_market_exclusions=len(exclusions.prior_plan_market_tickers),
            prior_plan_event_exclusions=len(exclusions.excluded_event_tickers),
            active_plan_market_exclusions=len(exclusions.active_plan_market_tickers),
            candidates_excluded_before_enrichment_by_market=(
                candidates_excluded_before_enrichment_by_market
            ),
            candidates_excluded_before_enrichment_by_event=(
                candidates_excluded_before_enrichment_by_event
            ),
            candidates_excluded_after_enrichment_by_market=(
                candidates_excluded_after_enrichment_by_market
            ),
            candidates_excluded_after_enrichment_by_event=(
                candidates_excluded_after_enrichment_by_event
            ),
            duplicate_event_exclusions=duplicate_event_exclusions,
            series_cap_exclusions=series_cap_exclusions,
            selected_market_tickers=tuple(value.market_ticker for value in selected),
            selected_event_tickers=tuple(value.event_ticker for value in selected),
            selected_series_tickers=tuple(value.series_ticker for value in selected),
            selected_count=len(selected),
            selected_event_count=len({value.event_ticker for value in selected}),
            selected_cohort_hash=content_digest({"selected": selected}),
            selected_markets_overlapping_prior_plans=len(
                {value.market_ticker for value in selected}
                & set(exclusions.prior_plan_market_tickers)
            ),
            selected_events_overlapping_prior_plans=len(
                {value.event_ticker for value in selected}
                & set(exclusions.prior_plan_event_tickers)
            ),
            selected_markets_overlapping_active_plans=len(
                {value.market_ticker for value in selected}
                & set(exclusions.active_plan_market_tickers)
            ),
            prior_event_overlap_count=len(
                {value.event_ticker for value in selected}
                & set(exclusions.prior_plan_event_tickers)
            ),
            new_distinct_event_count=len(
                {value.event_ticker for value in selected}
                - set(exclusions.prior_plan_event_tickers)
            ),
            combined_distinct_event_coverage=len(
                {value.event_ticker for value in selected}
                | set(exclusions.prior_plan_event_tickers)
            ),
            selected_configuration_versions=(
                tuple(
                    (binding.strategy_id, binding.configuration_version)
                    for binding in configuration.strategy_configuration_bindings
                )
                if configuration is not None
                else ()
            ),
            selected_configuration_hashes=(
                configuration.strategy_configuration_hashes if configuration is not None else ()
            ),
            no_plan_reason=no_plan_reason,
            plan_id=str(plan["plan_id"]) if plan is not None else None,
            provider_get_count=provider_get_count,
            provider_order_count=0,
            financial_state_before="NONE",
            financial_state_after="NONE",
            idempotent_replay=replay,
        )

    @staticmethod
    def _replay_result(operation: AtomicShadowLaunchOperation) -> AtomicShadowPlanLaunchResult:
        """Return an immutable terminal result without touching public evidence."""

        payload = operation.result_payload
        if payload is None:
            raise ValueValidationError("terminal atomic launch result is missing")
        summary_payload = payload.get("summary")
        result_hash = payload.get("result_hash")
        if not isinstance(summary_payload, Mapping) or not isinstance(result_hash, str):
            raise ValueValidationError("terminal atomic launch result is invalid")
        summary = AtomicShadowPlanLaunchSummary.from_dict(summary_payload)
        if (
            summary.operation_id != operation.operation_id
            or summary.operation_request_hash != operation.operation_request_hash
            or content_digest(summary.as_dict()) != result_hash
        ):
            raise ValueValidationError("terminal atomic launch result hash is invalid")
        return AtomicShadowPlanLaunchResult(
            summary=replace(summary, idempotent_replay=True),
            plan=operation.plan,
            state=operation.state,
            result_hash=result_hash,
        )

    def _failure_summary(
        self,
        *,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        operation_id: str,
        operation_request_hash: str,
        exclusions: AtomicShadowPlanExclusions,
        reason: str,
    ) -> AtomicShadowPlanLaunchSummary:
        """Retain a bounded failure projection without preserving exception text or payloads."""

        return self._summary(
            command=command,
            operation_id=operation_id,
            operation_request_hash=operation_request_hash,
            exclusions=exclusions,
            summary=None,
            ranked_count=0,
            list_two_sided_quote_candidates=0,
            list_incomplete_quote_candidates=0,
            list_spread_buckets={},
            attempts=0,
            successful=0,
            failures={reason: 1},
            event_successes=0,
            series_successes=0,
            exact_successes=0,
            orderbook_preflights_attempted=0,
            fee_compatible_candidates=0,
            fee_incompatibilities={},
            fee_applicability_states={},
            two_sided_books=0,
            one_sided_books=0,
            spread_compatible_books=0,
            visible_quantity_compatible_books=0,
            schedule_time_compatible_books=0,
            exact_spread_buckets={},
            list_exact_spread_disagreement_count=0,
            fee_and_spread_compatible_candidates=0,
            candidates_excluded_before_enrichment_by_market=0,
            candidates_excluded_before_enrichment_by_event=0,
            candidates_excluded_after_enrichment_by_market=0,
            candidates_excluded_after_enrichment_by_event=0,
            duplicate_event_exclusions=0,
            series_cap_exclusions=0,
            selected=(),
            no_plan_reason=reason,
            plan=None,
            configuration=None,
            provider_get_count=0,
            replay=False,
        )

    def run(
        self, command: DiscoverAndCreateShadowEvidencePlanCommand
    ) -> AtomicShadowPlanLaunchResult:
        operation_id = deterministic_id(
            "atomic_shadow_evidence_plan_launch",
            {"campaign_id": command.campaign_id, "idempotency_key": command.idempotency_key},
        )
        exclusions = self._store.resolve_atomic_launch_exclusions(
            campaign_id=command.campaign_id,
            prior_plan_ids=command.prior_plan_ids,
            exclude_prior_plan_events=command.exclude_prior_plan_events,
            exclude_active_plan_markets=command.exclude_active_plan_markets,
        )
        operation_request_hash = command.operation_request_hash(exclusions)
        operation = self._store.reserve_atomic_launch_operation(
            command=command,
            operation_id=operation_id,
            operation_request_hash=operation_request_hash,
            exclusions=exclusions,
        )
        if operation.state is not AtomicShadowLaunchState.RUNNING:
            return self._replay_result(operation)
        if not operation.created_now:
            raise AtomicShadowLaunchLifecycleError(
                AtomicShadowLaunchState.INTERRUPTED_AFTER_PROVIDER_IO
            )
        try:
            discovery = self._discover(
                PublicMarketScanRequest(
                    run_key=operation_id,
                    page_size=min(100, command.maximum_inspected_markets),
                    maximum_pages=command.maximum_pages,
                    timeout_seconds=command.timeout_seconds,
                )
            )
        except Exception:
            failure_summary = self._failure_summary(
                command=command,
                operation_id=operation_id,
                operation_request_hash=operation_request_hash,
                exclusions=exclusions,
                reason="PROVIDER_DISCOVERY_FAILED",
            )
            terminal = self._store.complete_atomic_no_plan_operation(
                command=command,
                operation_id=operation_id,
                operation_request_hash=operation_request_hash,
                exclusions=exclusions,
                state=AtomicShadowLaunchState.FAILED_AFTER_PROVIDER_IO,
                summary=failure_summary,
            )
            return AtomicShadowPlanLaunchResult(
                summary=failure_summary,
                plan=None,
                state=AtomicShadowLaunchState.FAILED_AFTER_PROVIDER_IO,
                result_hash=(
                    str(terminal.result_payload["result_hash"])
                    if terminal.result_payload is not None
                    else None
                ),
            )
        candidates = self._select_candidates(
            discovery.ingestion_run_id, command.maximum_inspected_markets
        )
        ranked = self._ranked_candidates(candidates=candidates, command=command)[
            : command.maximum_inspected_markets
        ]
        list_two_sided_quote_candidates = sum(
            candidate.has_complete_list_two_sided_quotes for candidate in ranked
        )
        list_incomplete_quote_candidates = len(ranked) - list_two_sided_quote_candidates
        list_spread_buckets: dict[str, int] = {}
        list_maximum_spread = command.microstructure_configuration(
            market_count=command.target_markets
        ).maximum_spread
        for candidate in ranked:
            bucket = self._spread_bucket(candidate.list_immediate_cross_spread, list_maximum_spread)
            list_spread_buckets[bucket] = list_spread_buckets.get(bucket, 0) + 1
        failures: dict[str, int] = {}
        fee_incompatibilities: dict[str, int] = {}
        fee_applicability_states: dict[str, int] = {}
        exact_spread_buckets: dict[str, int] = {}
        list_exact_spread_disagreement_count = 0
        fee_and_spread_compatible_candidates = 0
        validated: list[ValidatedShadowCohortCandidate] = []
        attempts = 0
        enrichment_successes = 0
        provider_get_count = discovery.page_count
        event_successes = 0
        series_successes = 0
        exact_successes = 0
        orderbook_preflights_attempted = 0
        fee_compatible_candidates = 0
        two_sided_books = 0
        one_sided_books = 0
        spread_compatible_books = 0
        visible_quantity_compatible_books = 0
        schedule_time_compatible_books = 0
        preflight_configuration = command.microstructure_configuration(
            market_count=command.target_markets
        )
        if not isinstance(preflight_configuration, OrderbookMicrostructureStrategyConfig):
            raise ValueValidationError("atomic shadow plan microstructure configuration is invalid")
        candidates_excluded_before_enrichment_by_market = 0
        candidates_excluded_before_enrichment_by_event = 0
        candidates_excluded_after_enrichment_by_market = 0
        candidates_excluded_after_enrichment_by_event = 0
        for candidate in ranked:
            if candidate.provider_market_ticker in exclusions.excluded_market_tickers:
                candidates_excluded_before_enrichment_by_market += 1
                continue
            if candidate.provider_event_ticker in exclusions.excluded_event_tickers:
                candidates_excluded_before_enrichment_by_event += 1
                continue
            if attempts >= command.maximum_enrichments:
                break
            if (
                command.requires_fee_compatible_preflight
                and orderbook_preflights_attempted >= command.maximum_orderbook_preflights
            ):
                break
            attempts += 1
            attempt = self._enrich_candidate(candidate, operation_id)
            provider_get_count += attempt.provider_get_count
            event_successes += int(attempt.event_enrichment_succeeded)
            series_successes += int(attempt.series_enrichment_succeeded)
            exact_successes += int(attempt.exact_market_refresh_succeeded)
            orderbook_preflights_attempted += int(attempt.orderbook_preflight_attempted)
            exact = attempt.selected
            if exact is None:
                for reason in attempt.reason_codes:
                    failures[reason] = failures.get(reason, 0) + 1
                continue
            enrichment_successes += 1
            if exact.market_ticker in exclusions.excluded_market_tickers:
                candidates_excluded_after_enrichment_by_market += 1
                continue
            if exact.event_ticker in exclusions.excluded_event_tickers:
                candidates_excluded_after_enrichment_by_event += 1
                continue
            if not command.window_start <= exact.close_at <= command.window_end:
                failures["CLOSE_WINDOW_MISMATCH"] = failures.get("CLOSE_WINDOW_MISMATCH", 0) + 1
                continue
            if command.requires_fee_compatible_preflight:
                preflight = self._fee_compatible_preflight(
                    exact=exact,
                    command=command,
                    configuration=preflight_configuration,
                )
                fee_compatible_candidates += int(preflight.fee_compatible)
                two_sided_books += int(preflight.two_sided)
                one_sided_books += int(not preflight.two_sided)
                spread_compatible_books += int(preflight.spread_compatible)
                visible_quantity_compatible_books += int(preflight.visible_quantity_compatible)
                schedule_time_compatible_books += int(preflight.schedule_time_compatible)
                exact_bucket = self._spread_bucket(
                    preflight.exact_spread, preflight_configuration.maximum_spread
                )
                exact_spread_buckets[exact_bucket] = exact_spread_buckets.get(exact_bucket, 0) + 1
                if (
                    candidate.list_immediate_cross_spread is not None
                    and preflight.exact_spread is not None
                    and candidate.list_immediate_cross_spread != preflight.exact_spread
                ):
                    list_exact_spread_disagreement_count += 1
                if preflight.fee_applicability is not None:
                    state = preflight.fee_applicability.state.value
                    fee_applicability_states[state] = fee_applicability_states.get(state, 0) + 1
                fee_and_spread_compatible_candidates += int(
                    preflight.fee_compatible and preflight.spread_compatible
                )
                for reason in preflight.reason_codes:
                    failures[reason] = failures.get(reason, 0) + 1
                    if reason.startswith("FEE_") or reason in {
                        "AUTHORITATIVE_MAPPING_MISSING",
                        "MULTIPLIER_TIME_AMBIGUOUS",
                        "CONFLICTING_FEE_EVIDENCE",
                    }:
                        fee_incompatibilities[reason] = fee_incompatibilities.get(reason, 0) + 1
                if preflight.reason_codes:
                    continue
                exact = replace(exact, fee_applicability=preflight.fee_applicability)
            validated.append(exact.validated_candidate())
        selected, duplicate_event_exclusions, series_cap_exclusions = self._select_diverse(
            candidates=tuple(validated), command=command
        )
        no_plan_reason: str | None = None
        if len(selected) < command.minimum_markets:
            no_plan_reason = "MINIMUM_MARKET_COUNT_NOT_MET"
        elif len({value.event_ticker for value in selected}) < command.minimum_events:
            no_plan_reason = "MINIMUM_EVENT_DIVERSITY_NOT_MET"
        if no_plan_reason is not None:
            terminal_summary = self._summary(
                command=command,
                operation_id=operation_id,
                operation_request_hash=operation_request_hash,
                exclusions=exclusions,
                summary=discovery,
                ranked_count=len(ranked),
                list_two_sided_quote_candidates=list_two_sided_quote_candidates,
                list_incomplete_quote_candidates=list_incomplete_quote_candidates,
                list_spread_buckets=list_spread_buckets,
                attempts=attempts,
                successful=enrichment_successes,
                failures=failures,
                event_successes=event_successes,
                series_successes=series_successes,
                exact_successes=exact_successes,
                orderbook_preflights_attempted=orderbook_preflights_attempted,
                fee_compatible_candidates=fee_compatible_candidates,
                fee_incompatibilities=fee_incompatibilities,
                fee_applicability_states=fee_applicability_states,
                two_sided_books=two_sided_books,
                one_sided_books=one_sided_books,
                spread_compatible_books=spread_compatible_books,
                visible_quantity_compatible_books=visible_quantity_compatible_books,
                schedule_time_compatible_books=schedule_time_compatible_books,
                exact_spread_buckets=exact_spread_buckets,
                list_exact_spread_disagreement_count=list_exact_spread_disagreement_count,
                fee_and_spread_compatible_candidates=fee_and_spread_compatible_candidates,
                candidates_excluded_before_enrichment_by_market=(
                    candidates_excluded_before_enrichment_by_market
                ),
                candidates_excluded_before_enrichment_by_event=(
                    candidates_excluded_before_enrichment_by_event
                ),
                candidates_excluded_after_enrichment_by_market=(
                    candidates_excluded_after_enrichment_by_market
                ),
                candidates_excluded_after_enrichment_by_event=(
                    candidates_excluded_after_enrichment_by_event
                ),
                duplicate_event_exclusions=duplicate_event_exclusions,
                series_cap_exclusions=series_cap_exclusions,
                selected=selected,
                no_plan_reason=no_plan_reason,
                plan=None,
                configuration=None,
                provider_get_count=provider_get_count,
                replay=False,
            )
            terminal_operation = self._store.complete_atomic_no_plan_operation(
                command=command,
                operation_id=operation_id,
                operation_request_hash=operation_request_hash,
                exclusions=exclusions,
                state=AtomicShadowLaunchState.NO_PLAN,
                summary=terminal_summary,
            )
            return AtomicShadowPlanLaunchResult(
                summary=terminal_summary,
                plan=None,
                state=AtomicShadowLaunchState.NO_PLAN,
                result_hash=(
                    str(terminal_operation.result_payload["result_hash"])
                    if terminal_operation.result_payload is not None
                    else None
                ),
            )
        configuration = ShadowEvidencePlanConfiguration(
            iterations=command.iterations,
            interval_seconds=command.interval_seconds,
            market_count=len(selected),
            max_pages=command.maximum_pages,
            max_enrichments=command.maximum_enrichments,
            max_markets_per_event=command.maximum_markets_per_event,
            max_markets_per_series=command.maximum_markets_per_series,
            orderbook_depth=command.orderbook_depth,
            timeout_seconds=command.timeout_seconds,
            strategy_ids=command.strategy_ids,
            strategy_configuration_bindings=fixed_strategy_configuration_bindings(
                market_count=len(selected),
                microstructure_configuration=command.microstructure_configuration(
                    market_count=len(selected)
                ),
            ),
        )
        expected_plan: dict[str, object] = {
            "plan_id": deterministic_id(
                "shadow_evidence_plan",
                {"campaign_id": command.campaign_id, "idempotency_key": command.idempotency_key},
            )
        }
        terminal_summary = self._summary(
            command=command,
            operation_id=operation_id,
            operation_request_hash=operation_request_hash,
            exclusions=exclusions,
            summary=discovery,
            ranked_count=len(ranked),
            list_two_sided_quote_candidates=list_two_sided_quote_candidates,
            list_incomplete_quote_candidates=list_incomplete_quote_candidates,
            list_spread_buckets=list_spread_buckets,
            attempts=attempts,
            successful=enrichment_successes,
            failures=failures,
            event_successes=event_successes,
            series_successes=series_successes,
            exact_successes=exact_successes,
            orderbook_preflights_attempted=orderbook_preflights_attempted,
            fee_compatible_candidates=fee_compatible_candidates,
            fee_incompatibilities=fee_incompatibilities,
            fee_applicability_states=fee_applicability_states,
            two_sided_books=two_sided_books,
            one_sided_books=one_sided_books,
            spread_compatible_books=spread_compatible_books,
            visible_quantity_compatible_books=visible_quantity_compatible_books,
            schedule_time_compatible_books=schedule_time_compatible_books,
            exact_spread_buckets=exact_spread_buckets,
            list_exact_spread_disagreement_count=list_exact_spread_disagreement_count,
            fee_and_spread_compatible_candidates=fee_and_spread_compatible_candidates,
            candidates_excluded_before_enrichment_by_market=(
                candidates_excluded_before_enrichment_by_market
            ),
            candidates_excluded_before_enrichment_by_event=(
                candidates_excluded_before_enrichment_by_event
            ),
            candidates_excluded_after_enrichment_by_market=(
                candidates_excluded_after_enrichment_by_market
            ),
            candidates_excluded_after_enrichment_by_event=(
                candidates_excluded_after_enrichment_by_event
            ),
            duplicate_event_exclusions=duplicate_event_exclusions,
            series_cap_exclusions=series_cap_exclusions,
            selected=selected,
            no_plan_reason=None,
            plan=expected_plan,
            configuration=configuration,
            provider_get_count=provider_get_count,
            replay=False,
        )
        plan, _replay = self._store.create_plan(
            CreateShadowEvidencePlanCommand(
                campaign_id=command.campaign_id,
                name=command.name,
                configuration=configuration,
                idempotency_key=command.idempotency_key,
                requested_at=command.decision_at,
                operator_note=command.operator_note,
                validated_cohort_candidates=selected,
                atomic_operation_request_hash=operation_request_hash,
                atomic_exclusion_configuration_hash=command.exclusion_configuration_hash,
                atomic_exclusions=exclusions,
                atomic_terminal_state=AtomicShadowLaunchState.CREATED_PLAN,
                atomic_terminal_summary=terminal_summary,
            )
        )
        return AtomicShadowPlanLaunchResult(
            summary=terminal_summary,
            plan=plan,
            state=AtomicShadowLaunchState.CREATED_PLAN,
            result_hash=content_digest(terminal_summary.as_dict()),
        )


class _ControlShadowEvidencePlan:
    action: str

    def __init__(self, store: ShadowOperationsStore) -> None:
        self._store = store

    def run(
        self, *, plan_id: str, idempotency_key: str, reason: str, requested_at: datetime
    ) -> tuple[dict[str, object], bool]:
        return self._store.control_plan(
            plan_id=plan_id,
            action=self.action,
            idempotency_key=idempotency_key,
            reason=reason,
            requested_at=requested_at,
        )


class PauseShadowEvidencePlan(_ControlShadowEvidencePlan):
    action = "pause"


class ResumeShadowEvidencePlan(_ControlShadowEvidencePlan):
    action = "resume"


class CancelShadowEvidencePlan(_ControlShadowEvidencePlan):
    action = "cancel"


class RunShadowEvidencePlanIteration:
    """Application-owned due-iteration seam; composition supplies the local runner."""

    def __init__(self, runner: Callable[[str], dict[str, object]]) -> None:
        self._runner = runner

    def run(self, *, plan_id: str) -> dict[str, object]:
        return self._runner(plan_id)


class ReconcileStaleShadowEvidencePlans:
    def __init__(self, store: ShadowOperationsStore) -> None:
        self._store = store

    def run(self, *, reconciled_at: datetime) -> int:
        return self._store.reconcile_stale_plans(now=reconciled_at)


class CollectPublicOutcomeFacts:
    """One explicit public collector callback; no scheduler is implied."""

    def __init__(self, collector: Callable[[], dict[str, object]]) -> None:
        self._collector = collector

    def run(self) -> dict[str, object]:
        return self._collector()


class EvaluateResolvedShadowPredictions:
    def __init__(self, store: ShadowOperationsStore) -> None:
        self._store = store

    def run(self, *, outcome_fact_id: str, evaluated_at: datetime) -> tuple[int, int]:
        return self._store.evaluate_public_outcome_fact(
            outcome_fact_id=outcome_fact_id, evaluated_at=evaluated_at
        )


class BuildStrategyPerformanceReport:
    def __init__(self, store: ShadowOperationsStore) -> None:
        self._store = store

    def run(
        self,
        *,
        campaign_id: str,
        strategy_id: str | None,
        plan_id: str | None,
        requested_at: datetime,
    ) -> dict[str, object]:
        return self._store.performance_report(
            campaign_id=campaign_id,
            strategy_id=strategy_id,
            plan_id=plan_id,
            requested_at=requested_at,
        )


@dataclass(frozen=True, slots=True)
class FrozenShadowEvidenceCohortMember:
    market_snapshot_id: str
    provider: str
    market_ticker: str
    event_ticker: str
    series_ticker: str
    rule_content_hash: str
    lifecycle_state: str
    observed_at: datetime
    available_at: datetime
    close_at: datetime
    market_lineage_hash: str
    event_lineage_hash: str
    series_lineage_hash: str
    fee_lineage_hash: str | None
    cohort_state: ShadowEvidenceCohortState = ShadowEvidenceCohortState.ACTIVE

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("market snapshot ID", self.market_snapshot_id, 160),
            ("provider", self.provider, 30),
            ("market ticker", self.market_ticker, 300),
            ("event ticker", self.event_ticker, 300),
            ("series ticker", self.series_ticker, 300),
            ("lifecycle state", self.lifecycle_state, 30),
        ):
            _text(value, label=label, maximum=maximum)
        for label, value in (
            ("rule content hash", self.rule_content_hash),
            ("market lineage hash", self.market_lineage_hash),
            ("event lineage hash", self.event_lineage_hash),
            ("series lineage hash", self.series_lineage_hash),
        ):
            if len(value) != 64:
                raise ValueValidationError(f"{label} must be a SHA-256 digest")
        if self.fee_lineage_hash is not None and len(self.fee_lineage_hash) != 64:
            raise ValueValidationError("fee lineage hash must be a SHA-256 digest")
        if _aware(self.available_at, label="cohort availability") < _aware(
            self.observed_at, label="cohort observation"
        ):
            raise ValueValidationError("cohort availability cannot precede observation")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)


@dataclass(frozen=True, slots=True)
class ShadowForwardScore:
    point_brier_score: Decimal
    point_log_loss: Decimal
    conservative_brier_score: Decimal
    conservative_log_loss: Decimal
    consensus_brier_score: Decimal
    consensus_log_loss: Decimal
    point_direction_correct: bool
    conservative_direction_correct: bool
    point_brier_difference: Decimal
    conservative_brier_difference: Decimal
    point_log_loss_difference: Decimal
    conservative_log_loss_difference: Decimal
    numerical_method: str = "Decimal.ln"
    numerical_precision: int = 50

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)


def _probability_map(values: tuple[tuple[str, Decimal], ...], *, label: str) -> dict[str, Decimal]:
    if len(values) < 2:
        raise ValueValidationError(f"{label} must contain at least two outcomes")
    parsed: dict[str, Decimal] = {}
    for outcome, probability in values:
        _text(outcome, label="outcome identity", maximum=120)
        if outcome in parsed:
            raise ValueValidationError(f"{label} contains a duplicate outcome")
        parsed[outcome] = parse_decimal(probability)
    if sum(parsed.values(), Decimal(0)) != Decimal(1):
        raise ValueValidationError(f"{label} must sum exactly to one")
    return parsed


def _brier(probabilities: dict[str, Decimal], outcome: str) -> Decimal:
    return (
        sum(
            (probability - (Decimal(1) if identity == outcome else Decimal(0))) ** 2
            for identity, probability in probabilities.items()
        )
        / Decimal(len(probabilities))
    ).quantize(SCORE_QUANTUM)


def _log_loss(probability: Decimal) -> Decimal:
    clipped = min(max(probability, LOG_LOSS_FLOOR), Decimal(1) - LOG_LOSS_FLOOR)
    with localcontext() as context:
        context.prec = 50
        return (-clipped.ln()).quantize(SCORE_QUANTUM)


def score_shadow_prediction_against_public_outcome(
    *,
    point_probabilities: tuple[tuple[str, Decimal], ...],
    conservative_probabilities: tuple[tuple[str, Decimal], ...],
    consensus_probabilities: tuple[tuple[str, Decimal], ...],
    resolved_outcome: str,
) -> ShadowForwardScore:
    """Score one forecast using a documented deterministic Decimal method."""

    outcome = _text(resolved_outcome, label="resolved public outcome", maximum=120)
    point = _probability_map(point_probabilities, label="point probabilities")
    conservative = _probability_map(conservative_probabilities, label="conservative probabilities")
    consensus = _probability_map(consensus_probabilities, label="consensus probabilities")
    if set(point) != set(conservative) or set(point) != set(consensus) or outcome not in point:
        raise ValueValidationError(
            "public outcome and all scoring distributions must match exactly"
        )
    point_brier = _brier(point, outcome)
    point_log = _log_loss(point[outcome])
    conservative_brier = _brier(conservative, outcome)
    conservative_log = _log_loss(conservative[outcome])
    consensus_brier = _brier(consensus, outcome)
    consensus_log = _log_loss(consensus[outcome])
    return ShadowForwardScore(
        point_brier_score=point_brier,
        point_log_loss=point_log,
        conservative_brier_score=conservative_brier,
        conservative_log_loss=conservative_log,
        consensus_brier_score=consensus_brier,
        consensus_log_loss=consensus_log,
        point_direction_correct=max(point.items(), key=lambda item: (item[1], item[0]))[0]
        == outcome,
        conservative_direction_correct=max(
            conservative.items(), key=lambda item: (item[1], item[0])
        )[0]
        == outcome,
        point_brier_difference=(consensus_brier - point_brier).quantize(SCORE_QUANTUM),
        conservative_brier_difference=(consensus_brier - conservative_brier).quantize(
            SCORE_QUANTUM
        ),
        point_log_loss_difference=(consensus_log - point_log).quantize(SCORE_QUANTUM),
        conservative_log_loss_difference=(consensus_log - conservative_log).quantize(SCORE_QUANTUM),
    )


def next_due_at(
    *, completed_at: datetime, configuration: ShadowEvidencePlanConfiguration
) -> datetime:
    return _aware(completed_at, label="iteration completion") + timedelta(
        seconds=configuration.interval_seconds
    )
