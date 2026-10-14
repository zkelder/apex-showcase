"""Pure strategy-family knowledge and disjoint crypto replication rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.crypto_research import (
    AUTHORITY_WARNING,
    BTC_USD_DAILY_TREND_VALIDATION_V1,
    BTC_USD_PRIOR_DAY_SMA_FEATURES_V1,
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    CONSERVATIVE_FEE_BPS,
    CONSERVATIVE_SLIPPAGE_BPS,
    CRYPTO_RESEARCH_COST_MODEL_V1,
    CRYPTO_VALIDATION_POLICY_V1,
    FAST_SMA_DAYS,
    HYPOTHETICAL_WARNING,
    PRIOR_DAY_WARNING,
    RETROSPECTIVE_WARNING,
    SLOW_SMA_DAYS,
    CostScenarioName,
    CryptoPerformanceMetrics,
    CryptoSignalDecision,
    PortfolioName,
)
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
)
from apex.domain.values import content_digest, parse_decimal

CRYPTO_STRATEGY_SPECIFICATION_V1 = "CRYPTO_STRATEGY_SPECIFICATION_V1"
CRYPTO_STRATEGY_SPECIFICATION_V2 = "CRYPTO_STRATEGY_SPECIFICATION_V2"
BTC_USD_SMA_20_100_STRATEGY_FAMILY = "BTC_USD_DAILY_SMA_TREND"
BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY = "BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM"
BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY = "BTC_USD_MONTHLY_200_OBSERVATION_PRICE_TREND"
BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY = "BTC_USD_MONTHLY_STATEFUL_90_45_CLOSE_BREAKOUT"
CRYPTO_DISJOINT_REPLICATION_PROTOCOL_V1 = "CRYPTO_DISJOINT_REPLICATION_PROTOCOL_V1"
CRYPTO_DISJOINT_REPLICATION_RESULT_V1 = "CRYPTO_DISJOINT_REPLICATION_RESULT_V1"
CRYPTO_EVIDENCE_DOSSIER_V1 = "CRYPTO_EVIDENCE_DOSSIER_V1"
CRYPTO_DOSSIER_CONCLUSION_POLICY_V1 = "CRYPTO_DOSSIER_CONCLUSION_POLICY_V1"
NO_APPROVED_CHAMPION = "NO_APPROVED_CHAMPION"

ORIGINAL_HYPOTHESIS_VERSION_ID = "research_hypothesis_version_c25f211be622f6520ee29319"
ORIGINAL_SNAPSHOT_ID = "btc-usd-genuine-snapshot-2024-07-01-2026-07-01-v1"
ORIGINAL_SNAPSHOT_MANIFEST_HASH = "4d8f3295f2d2879d320ffe338ca6a5cdb85682f53f52c5714adf27fdf044f8aa"
ORIGINAL_RUN_ID = "btc-usd-genuine-trend-validation-v1"
ORIGINAL_RESULT_HASH = "66507c0fca3948a0a81a3a092cfbf87eeddd8eb9fb1b908b7ddabeda345ad62c"
ORIGINAL_CANONICAL_RUN_HASH = "4d1909809d254ed830f36f0f5619b3858496d4eed92dd1035be03c67f6c02b96"
ORIGINAL_OUTCOME_ID = "btc-usd-genuine-outcome-v1"
ORIGINAL_OUTCOME_HASH = "0511e06618068de95f8bcee0bc8fac3774dbd274f6e0ca6164945dd710c79f73"

REPLICATION_DATA_START = datetime(2022, 7, 1, tzinfo=UTC)
REPLICATION_DATA_END = datetime(2024, 7, 1, tzinfo=UTC)
REPLICATION_WARMUP_START = REPLICATION_DATA_START
REPLICATION_WARMUP_END = datetime(2023, 7, 1, tzinfo=UTC)
REPLICATION_VALIDATION_START = REPLICATION_WARMUP_END
REPLICATION_VALIDATION_END = REPLICATION_DATA_END

REPLICATION_WARNINGS = (
    RETROSPECTIVE_WARNING,
    HYPOTHETICAL_WARNING,
    PRIOR_DAY_WARNING,
    AUTHORITY_WARNING,
    "DISJOINT RETROSPECTIVE REPLICATION — NOT A FORWARD TRIAL",
)

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


class CryptoDossierError(ValueError):
    pass


class CryptoStrategyLifecycle(StrEnum):
    REJECTED_RESEARCH = "REJECTED_RESEARCH"
    CHALLENGER_RESEARCH = "CHALLENGER_RESEARCH"


class CryptoDossierConclusionType(StrEnum):
    REJECTION_CONFIRMED = "REJECTION_CONFIRMED"
    MIXED_EVIDENCE = "MIXED_EVIDENCE"
    INCONCLUSIVE = "INCONCLUSIVE"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise CryptoDossierError(code)
    return normalized


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoDossierError(code)
    return value.astimezone(UTC)


def frozen_semantics_payload(
    *,
    fast_sma_days: int = FAST_SMA_DAYS,
    slow_sma_days: int = SLOW_SMA_DAYS,
    fee_bps_per_side: Decimal | str = CONSERVATIVE_FEE_BPS,
    slippage_bps_per_side: Decimal | str = CONSERVATIVE_SLIPPAGE_BPS,
    allow_short: bool = False,
    allow_leverage: bool = False,
    execution_timing_rule: str = "NEXT_AVAILABLE_DAILY_OPEN",
    information_boundary_rule: str = "PRIOR_COMPLETED_DAILY_BUCKET_ONLY",
    missing_data_policy: str = "FAIL_CLOSED_NO_FILL_OR_INTERPOLATION",
) -> tuple[tuple[str, object], ...]:
    fee = parse_decimal(fee_bps_per_side)
    slippage = parse_decimal(slippage_bps_per_side)
    if not 1 <= fast_sma_days < slow_sma_days <= 365:
        raise CryptoDossierError("CRYPTO_STRATEGY_MOVING_AVERAGES_INVALID")
    if fee > 1000 or slippage > 1000:
        raise CryptoDossierError("CRYPTO_STRATEGY_COST_BOUND_INVALID")
    return (
        ("strategy_family", BTC_USD_SMA_20_100_STRATEGY_FAMILY),
        ("product_id", COINBASE_PRODUCT),
        ("data_type", "DAILY_OHLCV"),
        ("granularity", COINBASE_GRANULARITY),
        ("feature_definition", BTC_USD_PRIOR_DAY_SMA_FEATURES_V1),
        ("fast_sma_days", fast_sma_days),
        ("slow_sma_days", slow_sma_days),
        ("entry_rule", "LONG_WHEN_PRIOR_FAST_SMA_STRICTLY_ABOVE_PRIOR_SLOW_SMA"),
        ("exit_or_cash_rule", "CASH_OTHERWISE"),
        ("information_boundary_rule", information_boundary_rule),
        ("execution_timing_rule", execution_timing_rule),
        ("position_sizing_rule", "FULL_HYPOTHETICAL_CASH_LONG_OR_CASH"),
        (
            "expected_holding_behavior",
            "HOLD_LONG_UNTIL_PRIOR_FAST_SMA_NOT_ABOVE_PRIOR_SLOW_SMA",
        ),
        ("allow_short", allow_short),
        ("allow_leverage", allow_leverage),
        ("fee_bps_per_side", fee),
        ("slippage_bps_per_side", slippage),
        ("missing_data_policy", missing_data_policy),
        ("baselines", ("CASH", "BUY_AND_HOLD")),
        (
            "success_thresholds",
            (
                "conservative_net_return>=0",
                "trend_maximum_drawdown<buy_and_hold_maximum_drawdown",
                "complete_required_evidence=true",
                "reproducibility_status=REPRODUCIBLE",
                "completed_round_trips>=1",
            ),
        ),
        ("executor_identity", BTC_USD_DAILY_TREND_VALIDATION_V1),
        ("cost_model", CRYPTO_RESEARCH_COST_MODEL_V1),
        ("validation_policy", CRYPTO_VALIDATION_POLICY_V1),
    )


def monthly_momentum_semantics_payload() -> tuple[tuple[str, object], ...]:
    """Return the predeclared materially different challenger semantics."""

    return (
        ("strategy_family", BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY),
        ("product_id", COINBASE_PRODUCT),
        ("data_type", "DAILY_OHLCV"),
        ("granularity", COINBASE_GRANULARITY),
        ("feature_definition", "BTC_USD_PRIOR_MONTH_END_ABSOLUTE_MOMENTUM_252_V1"),
        ("lookback_accepted_daily_observations", 252),
        (
            "entry_rule",
            "LONG_WHEN_PRIOR_COMPLETED_MONTH_END_CLOSE_DIVIDED_BY_CLOSE_252_"
            "ACCEPTED_OBSERVATIONS_EARLIER_MINUS_ONE_STRICTLY_ABOVE_ZERO",
        ),
        ("exit_or_cash_rule", "CASH_OTHERWISE"),
        ("decision_cadence", "CALENDAR_MONTH_BOUNDARY"),
        ("information_boundary_rule", "PRIOR_COMPLETED_CALENDAR_MONTH_DAILY_CLOSE_ONLY"),
        ("execution_timing_rule", "NEXT_AVAILABLE_DAILY_OPEN_AFTER_MONTH_END"),
        ("position_sizing_rule", "FULL_HYPOTHETICAL_CASH_LONG_OR_CASH"),
        ("expected_holding_behavior", "HOLD_STATE_UNTIL_NEXT_CALENDAR_MONTH_DECISION"),
        ("allow_short", False),
        ("allow_leverage", False),
        ("fee_bps_per_side", CONSERVATIVE_FEE_BPS),
        ("slippage_bps_per_side", CONSERVATIVE_SLIPPAGE_BPS),
        (
            "missing_data_policy",
            "FAIL_CLOSED_WITH_RETAINED_DECISION_REASON_NO_BRIDGING_OR_INTERPOLATION",
        ),
        ("baselines", ("CASH", "BUY_AND_HOLD", "RETAINED_REJECTED_SMA_20_100")),
        (
            "success_thresholds",
            (
                "conservative_net_return>=0",
                "maximum_drawdown<buy_and_hold_maximum_drawdown",
                "complete_required_evidence=true",
                "reproducibility_status=REPRODUCIBLE",
                "eligible_long_periods>=1",
                "disqualifying_warnings=0",
            ),
        ),
        ("executor_identity", "BTC_USD_MONTHLY_ABSOLUTE_MOMENTUM_252_V1"),
        ("cost_model", CRYPTO_RESEARCH_COST_MODEL_V1),
        ("validation_policy", "CRYPTO_MONTHLY_MULTI_WINDOW_POLICY_V1"),
    )


def monthly_200_price_trend_semantics_payload() -> tuple[tuple[str, object], ...]:
    """Return the predeclared campaign Candidate 1 semantics."""

    return (
        ("strategy_family", BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY),
        ("product_id", COINBASE_PRODUCT),
        ("market_type", "SPOT_RESEARCH"),
        ("data_type", "DAILY_OHLCV"),
        ("granularity", COINBASE_GRANULARITY),
        ("feature_definition", "BTC_USD_MONTH_END_CLOSE_VS_200_CLOSE_MEAN_V1"),
        ("lookback_accepted_daily_observations", 200),
        ("mean_includes_current_completed_month_end_close", True),
        (
            "entry_rule",
            "LONG_WHEN_PRIOR_COMPLETED_MONTH_END_CLOSE_STRICTLY_ABOVE_"
            "ARITHMETIC_MEAN_OF_THAT_CLOSE_AND_PRECEDING_199_ACCEPTED_CLOSES",
        ),
        ("exit_or_cash_rule", "CASH_OTHERWISE"),
        ("decision_cadence", "CALENDAR_MONTH_BOUNDARY"),
        ("information_boundary_rule", "PRIOR_COMPLETED_CALENDAR_MONTH_DAILY_CLOSE_ONLY"),
        ("execution_timing_rule", "NEXT_AVAILABLE_ELIGIBLE_DAILY_OPEN_AFTER_MONTH_END"),
        ("position_sizing_rule", "FULL_HYPOTHETICAL_CASH_LONG_OR_CASH"),
        ("expected_holding_behavior", "HOLD_STATE_UNTIL_NEXT_ELIGIBLE_MONTHLY_DECISION"),
        ("allow_short", False),
        ("allow_leverage", False),
        ("allow_derivatives", False),
        ("allow_partial_sizing", False),
        ("fee_bps_per_side", CONSERVATIVE_FEE_BPS),
        ("slippage_bps_per_side", CONSERVATIVE_SLIPPAGE_BPS),
        (
            "missing_data_policy",
            "FAIL_CLOSED_NO_BRIDGING_FORWARD_FILL_INTERPOLATION_OR_EXECUTION_SUBSTITUTION",
        ),
        (
            "baselines",
            (
                "CASH",
                "BUY_AND_HOLD",
                "RETAINED_REJECTED_SMA_20_100",
                "RETAINED_REJECTED_MONTHLY_252_ENDPOINT_MOMENTUM",
            ),
        ),
        (
            "success_thresholds",
            (
                "conservative_net_return>=0",
                "maximum_drawdown<buy_and_hold_maximum_drawdown",
                "complete_required_evidence=true",
                "reproducibility_status=REPRODUCIBLE",
                "eligible_long_periods>=1",
                "disqualifying_warnings=0",
            ),
        ),
        ("executor_identity", "BTC_USD_MONTHLY_200_PRICE_TREND_EVALUATOR_V1"),
        ("cost_model", CRYPTO_RESEARCH_COST_MODEL_V1),
        ("validation_policy", "CRYPTO_CAMPAIGN_CANDIDATE_1_TWO_WINDOW_POLICY_V1"),
        ("authority", "NONE"),
    )


def monthly_stateful_breakout_semantics_payload() -> tuple[tuple[str, object], ...]:
    """Return the predeclared campaign Candidate 2 semantics."""

    return (
        ("strategy_family", BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY),
        ("product_id", COINBASE_PRODUCT),
        ("market_type", "SPOT_RESEARCH"),
        ("data_type", "DAILY_OHLCV"),
        ("granularity", COINBASE_GRANULARITY),
        ("feature_definition", "BTC_USD_MONTHLY_STATEFUL_90_45_CLOSE_BREAKOUT_V1"),
        ("initial_state_each_window", "CASH"),
        ("entry_reference_observations", 90),
        ("entry_reference_excludes_decision_close", True),
        (
            "entry_rule",
            "WHILE_CASH_ENTER_LONG_WHEN_DECISION_CLOSE_STRICTLY_EXCEEDS_"
            "MAXIMUM_OF_PRECEDING_90_ACCEPTED_COMPLETED_CLOSES",
        ),
        ("exit_reference_observations", 45),
        ("exit_reference_excludes_decision_close", True),
        (
            "exit_rule",
            "WHILE_LONG_EXIT_TO_CASH_WHEN_DECISION_CLOSE_STRICTLY_BELOW_"
            "MINIMUM_OF_PRECEDING_45_ACCEPTED_COMPLETED_CLOSES",
        ),
        ("state_machine", "PERSISTENT_ASYMMETRIC_CASH_LONG_HYSTERESIS"),
        ("decision_cadence", "CALENDAR_MONTH_BOUNDARY"),
        ("information_boundary_rule", "PRIOR_COMPLETED_CALENDAR_MONTH_DAILY_CLOSE_ONLY"),
        ("execution_timing_rule", "NEXT_AVAILABLE_ELIGIBLE_DAILY_OPEN_AFTER_MONTH_END"),
        ("position_sizing_rule", "FULL_HYPOTHETICAL_CASH_LONG_OR_CASH"),
        ("allow_short", False),
        ("allow_leverage", False),
        ("allow_derivatives", False),
        ("allow_partial_sizing", False),
        ("allow_volatility_targeting", False),
        ("fee_bps_per_side", CONSERVATIVE_FEE_BPS),
        ("slippage_bps_per_side", CONSERVATIVE_SLIPPAGE_BPS),
        (
            "missing_entry_policy",
            "INELIGIBLE_REMAIN_CASH_NO_BRIDGING_FORWARD_FILL_OR_INTERPOLATION",
        ),
        (
            "missing_exit_policy",
            "INELIGIBLE_PROTECTIVE_EXIT_TO_CASH_AT_NEXT_ELIGIBLE_OPEN",
        ),
        (
            "baselines",
            (
                "CASH",
                "BUY_AND_HOLD",
                "RETAINED_REJECTED_SMA_20_100",
                "RETAINED_REJECTED_MONTHLY_252_ENDPOINT_MOMENTUM",
                "RETAINED_REJECTED_MONTHLY_200_PRICE_TREND",
            ),
        ),
        (
            "success_thresholds",
            (
                "conservative_net_return>=0",
                "maximum_drawdown<buy_and_hold_maximum_drawdown",
                "complete_required_evidence=true",
                "reproducibility_status=REPRODUCIBLE",
                "eligible_long_periods>=1",
                "disqualifying_warnings=0",
            ),
        ),
        ("executor_identity", "BTC_USD_MONTHLY_STATEFUL_90_45_BREAKOUT_EVALUATOR_V1"),
        ("cost_model", CRYPTO_RESEARCH_COST_MODEL_V1),
        ("validation_policy", "CRYPTO_CAMPAIGN_CANDIDATE_2_TWO_WINDOW_POLICY_V1"),
        ("authority", "NONE"),
    )


@dataclass(frozen=True, slots=True)
class CryptoStrategySpecification:
    specification_id: str
    strategy_family: str
    version: int
    parent_specification_id: str | None
    change_rationale: str
    description: str
    hypothesis_version_id: str
    original_run_id: str
    original_outcome_id: str
    semantics: tuple[tuple[str, object], ...]
    semantics_hash: str
    created_at: datetime
    lifecycle_state: CryptoStrategyLifecycle
    known_limitations: tuple[str, ...]
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    opportunity_authority: str = "NONE"
    order_authority: str = "NONE"
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        specification_id: str,
        version: int,
        created_at: datetime,
        semantics: tuple[tuple[str, object], ...],
        parent_specification_id: str | None = None,
        change_rationale: str = "",
        hypothesis_version_id: str = ORIGINAL_HYPOTHESIS_VERSION_ID,
        original_run_id: str = ORIGINAL_RUN_ID,
        original_outcome_id: str = ORIGINAL_OUTCOME_ID,
    ) -> CryptoStrategySpecification:
        if version < 1 or (version == 1 and parent_specification_id is not None):
            raise CryptoDossierError("CRYPTO_STRATEGY_VERSION_LINEAGE_INVALID")
        if version > 1 and (parent_specification_id is None or not change_rationale.strip()):
            raise CryptoDossierError("CRYPTO_STRATEGY_CHANGE_RATIONALE_REQUIRED")
        semantics_hash = content_digest(semantics)
        created = _aware(created_at, "CRYPTO_STRATEGY_CREATED_AT_INVALID")
        canonical = (
            CRYPTO_STRATEGY_SPECIFICATION_V1,
            _identity(specification_id, "CRYPTO_STRATEGY_ID_INVALID"),
            version,
            parent_specification_id,
            change_rationale.strip(),
            hypothesis_version_id,
            original_run_id,
            original_outcome_id,
            semantics_hash,
            created,
            CryptoStrategyLifecycle.REJECTED_RESEARCH,
        )
        return cls(
            specification_id=canonical[1],
            strategy_family=BTC_USD_SMA_20_100_STRATEGY_FAMILY,
            version=version,
            parent_specification_id=parent_specification_id,
            change_rationale=canonical[4],
            description=(
                "Rejected BTC-USD daily long-or-cash 20/100 completed-close SMA rule "
                "with prior-day signals and next-open hypothetical execution."
            ),
            hypothesis_version_id=hypothesis_version_id,
            original_run_id=original_run_id,
            original_outcome_id=original_outcome_id,
            semantics=semantics,
            semantics_hash=semantics_hash,
            created_at=created,
            lifecycle_state=CryptoStrategyLifecycle.REJECTED_RESEARCH,
            known_limitations=(
                "RETROSPECTIVE_HISTORY_NOT_VINTAGE_POINT_IN_TIME",
                "ORIGINAL_GENUINE_VALIDATION_REJECTED_FOR_NEGATIVE_CONSERVATIVE_RETURN",
                "NO_FORWARD_TRIAL",
            ),
            canonical_hash=content_digest(canonical),
        )

    @classmethod
    def frozen(cls, *, specification_id: str, created_at: datetime) -> CryptoStrategySpecification:
        return cls.create(
            specification_id=specification_id,
            version=1,
            created_at=created_at,
            semantics=frozen_semantics_payload(),
            change_rationale="FROZEN_FROM_REJECTED_GENUINE_VALIDATION",
        )

    @classmethod
    def monthly_momentum_challenger(
        cls,
        *,
        specification_id: str,
        hypothesis_version_id: str,
        parent: CryptoStrategySpecification,
        created_at: datetime,
    ) -> CryptoStrategySpecification:
        if parent.strategy_family != BTC_USD_SMA_20_100_STRATEGY_FAMILY:
            raise CryptoDossierError("CRYPTO_CHALLENGER_PARENT_FAMILY_INVALID")
        semantics = monthly_momentum_semantics_payload()
        rationale = (
            "Replace the daily 20/100 relative moving-average crossover with a "
            "monthly 252-observation absolute-momentum decision to test whether "
            "lower decision frequency and an absolute trend threshold reduce "
            "whipsaw and transaction-cost drag."
        )
        semantics_hash = content_digest(semantics)
        created = _aware(created_at, "CRYPTO_STRATEGY_CREATED_AT_INVALID")
        identity = _identity(specification_id, "CRYPTO_STRATEGY_ID_INVALID")
        canonical = (
            CRYPTO_STRATEGY_SPECIFICATION_V2,
            identity,
            2,
            parent.specification_id,
            rationale,
            hypothesis_version_id,
            parent.original_run_id,
            parent.original_outcome_id,
            semantics_hash,
            created,
            CryptoStrategyLifecycle.CHALLENGER_RESEARCH,
        )
        return cls(
            specification_id=identity,
            strategy_family=BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY,
            version=2,
            parent_specification_id=parent.specification_id,
            change_rationale=rationale,
            description=(
                "Research-only BTC-USD monthly long-or-cash absolute-momentum "
                "rule using the prior completed month and a 252-observation lookback."
            ),
            hypothesis_version_id=_identity(
                hypothesis_version_id, "CRYPTO_HYPOTHESIS_VERSION_ID_INVALID"
            ),
            original_run_id=parent.original_run_id,
            original_outcome_id=parent.original_outcome_id,
            semantics=semantics,
            semantics_hash=semantics_hash,
            created_at=created,
            lifecycle_state=CryptoStrategyLifecycle.CHALLENGER_RESEARCH,
            known_limitations=(
                "RETROSPECTIVE_HISTORY_NOT_VINTAGE_POINT_IN_TIME",
                "TWO_WINDOWS_ONLY",
                "NO_FORWARD_TRIAL",
                "NO_PAPER_OR_LIVE_AUTHORITY",
            ),
            canonical_hash=content_digest(canonical),
        )

    @classmethod
    def monthly_200_price_trend_candidate(
        cls,
        *,
        specification_id: str,
        hypothesis_version_id: str,
        parent: CryptoStrategySpecification,
        created_at: datetime,
    ) -> CryptoStrategySpecification:
        if parent.strategy_family != BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM_FAMILY:
            raise CryptoDossierError("CRYPTO_CAMPAIGN_CANDIDATE_PARENT_FAMILY_INVALID")
        semantics = monthly_200_price_trend_semantics_payload()
        rationale = (
            "Replace endpoint-to-endpoint 252-observation momentum with a monthly "
            "comparison of the completed month-end close to the arithmetic mean of "
            "that close and the preceding 199 accepted closes. This medium-horizon "
            "path-sensitive filter is designed to react to sustained declines sooner "
            "than endpoint momentum while retaining lower expected turnover than the "
            "rejected daily 20/100 crossover."
        )
        semantics_hash = content_digest(semantics)
        created = _aware(created_at, "CRYPTO_STRATEGY_CREATED_AT_INVALID")
        identity = _identity(specification_id, "CRYPTO_STRATEGY_ID_INVALID")
        canonical = (
            "CRYPTO_STRATEGY_SPECIFICATION_V3",
            identity,
            3,
            parent.specification_id,
            rationale,
            hypothesis_version_id,
            parent.original_run_id,
            parent.original_outcome_id,
            semantics_hash,
            created,
            CryptoStrategyLifecycle.CHALLENGER_RESEARCH,
        )
        return cls(
            specification_id=identity,
            strategy_family=BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY,
            version=3,
            parent_specification_id=parent.specification_id,
            change_rationale=rationale,
            description=(
                "Campaign Candidate 1: research-only BTC-USD monthly long-or-cash "
                "price-versus-200-observation-average trend rule."
            ),
            hypothesis_version_id=_identity(
                hypothesis_version_id, "CRYPTO_HYPOTHESIS_VERSION_ID_INVALID"
            ),
            original_run_id=parent.original_run_id,
            original_outcome_id=parent.original_outcome_id,
            semantics=semantics,
            semantics_hash=semantics_hash,
            created_at=created,
            lifecycle_state=CryptoStrategyLifecycle.CHALLENGER_RESEARCH,
            known_limitations=(
                "EXPOSED_DEVELOPMENT_EVIDENCE_NOT_INDEPENDENT_VALIDATION",
                "TWO_PREDECLARED_DEVELOPMENT_WINDOWS_ONLY",
                "SEALED_HOLDOUT_NOT_ACCESSED_OR_EVALUATED",
                "NO_FORWARD_TRIAL",
                "NO_PAPER_OR_LIVE_AUTHORITY",
            ),
            canonical_hash=content_digest(canonical),
        )

    @classmethod
    def monthly_stateful_breakout_candidate(
        cls,
        *,
        specification_id: str,
        hypothesis_version_id: str,
        parent: CryptoStrategySpecification,
        created_at: datetime,
    ) -> CryptoStrategySpecification:
        if parent.strategy_family != BTC_USD_MONTHLY_200_PRICE_TREND_FAMILY:
            raise CryptoDossierError("CRYPTO_CAMPAIGN_CANDIDATE_2_PARENT_FAMILY_INVALID")
        semantics = monthly_stateful_breakout_semantics_payload()
        rationale = (
            "Replace Candidate 1's stateless price-versus-average selection with a "
            "persistent state machine: while cash, enter only on a strict close breakout "
            "above the preceding 90 closes; while long, exit only on a strict close "
            "breakdown below the preceding 45 closes. The asymmetric extrema rules create "
            "hysteresis without moving averages, endpoint momentum, or parameter search."
        )
        semantics_hash = content_digest(semantics)
        created = _aware(created_at, "CRYPTO_STRATEGY_CREATED_AT_INVALID")
        identity = _identity(specification_id, "CRYPTO_STRATEGY_ID_INVALID")
        canonical = (
            "CRYPTO_STRATEGY_SPECIFICATION_V4",
            identity,
            4,
            parent.specification_id,
            rationale,
            hypothesis_version_id,
            parent.original_run_id,
            parent.original_outcome_id,
            semantics_hash,
            created,
            CryptoStrategyLifecycle.CHALLENGER_RESEARCH,
        )
        return cls(
            specification_id=identity,
            strategy_family=BTC_USD_MONTHLY_STATEFUL_BREAKOUT_FAMILY,
            version=4,
            parent_specification_id=parent.specification_id,
            change_rationale=rationale,
            description=(
                "Campaign Candidate 2: research-only BTC-USD monthly stateful "
                "90-close upside breakout and 45-close downside breakdown rule."
            ),
            hypothesis_version_id=_identity(
                hypothesis_version_id, "CRYPTO_HYPOTHESIS_VERSION_ID_INVALID"
            ),
            original_run_id=parent.original_run_id,
            original_outcome_id=parent.original_outcome_id,
            semantics=semantics,
            semantics_hash=semantics_hash,
            created_at=created,
            lifecycle_state=CryptoStrategyLifecycle.CHALLENGER_RESEARCH,
            known_limitations=(
                "EXPOSED_DEVELOPMENT_EVIDENCE_NOT_INDEPENDENT_VALIDATION",
                "TWO_PREDECLARED_DEVELOPMENT_WINDOWS_ONLY",
                "SEALED_HOLDOUT_NOT_ACCESSED_OR_EVALUATED",
                "NO_FORWARD_TRIAL",
                "NO_PAPER_OR_LIVE_AUTHORITY",
            ),
            canonical_hash=content_digest(canonical),
        )


@dataclass(frozen=True, slots=True)
class CryptoReplicationProtocol:
    protocol_id: str
    strategy_specification_id: str
    strategy_semantics_hash: str
    hypothesis_version_id: str
    original_run_id: str
    original_outcome_id: str
    data_start: datetime
    data_end: datetime
    warmup_start: datetime
    warmup_end: datetime
    validation_start: datetime
    validation_end: datetime
    disjoint_reason: str
    cost_model: str
    validation_policy: str
    baseline_definitions: tuple[str, ...]
    executor_identity: str
    conclusion_policy: str
    protocol_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def fixed(
        cls,
        *,
        protocol_id: str,
        strategy: CryptoStrategySpecification,
        created_at: datetime,
    ) -> CryptoReplicationProtocol:
        payload = (
            CRYPTO_DISJOINT_REPLICATION_PROTOCOL_V1,
            strategy.specification_id,
            strategy.semantics_hash,
            strategy.hypothesis_version_id,
            strategy.original_run_id,
            strategy.original_outcome_id,
            REPLICATION_DATA_START,
            REPLICATION_DATA_END,
            REPLICATION_WARMUP_START,
            REPLICATION_WARMUP_END,
            REPLICATION_VALIDATION_START,
            REPLICATION_VALIDATION_END,
            (
                "The interval ends exactly where the original dataset begins; "
                "no daily bucket overlaps."
            ),
            CRYPTO_RESEARCH_COST_MODEL_V1,
            CRYPTO_VALIDATION_POLICY_V1,
            ("CASH", "BUY_AND_HOLD"),
            BTC_USD_DAILY_TREND_VALIDATION_V1,
            CRYPTO_DOSSIER_CONCLUSION_POLICY_V1,
        )
        protocol_hash = content_digest(payload)
        created = _aware(created_at, "CRYPTO_REPLICATION_PROTOCOL_TIME_INVALID")
        protocol_identity = _identity(protocol_id, "CRYPTO_REPLICATION_PROTOCOL_ID_INVALID")
        return cls(
            protocol_id=protocol_identity,
            strategy_specification_id=strategy.specification_id,
            strategy_semantics_hash=strategy.semantics_hash,
            hypothesis_version_id=strategy.hypothesis_version_id,
            original_run_id=strategy.original_run_id,
            original_outcome_id=strategy.original_outcome_id,
            data_start=REPLICATION_DATA_START,
            data_end=REPLICATION_DATA_END,
            warmup_start=REPLICATION_WARMUP_START,
            warmup_end=REPLICATION_WARMUP_END,
            validation_start=REPLICATION_VALIDATION_START,
            validation_end=REPLICATION_VALIDATION_END,
            disjoint_reason=payload[12],
            cost_model=CRYPTO_RESEARCH_COST_MODEL_V1,
            validation_policy=CRYPTO_VALIDATION_POLICY_V1,
            baseline_definitions=("CASH", "BUY_AND_HOLD"),
            executor_identity=BTC_USD_DAILY_TREND_VALIDATION_V1,
            conclusion_policy=CRYPTO_DOSSIER_CONCLUSION_POLICY_V1,
            protocol_hash=protocol_hash,
            created_at=created,
            canonical_hash=content_digest((protocol_identity, protocol_hash, created)),
        )


@dataclass(frozen=True, slots=True)
class CryptoReplicationRun:
    run_id: str
    protocol_id: str
    strategy_specification_id: str
    snapshot_id: str
    strategy_semantics_hash: str
    configuration: tuple[tuple[str, object], ...]
    configuration_hash: str
    code_revision: str
    deterministic_seed: int
    executed_at: datetime
    state: ExperimentState
    complete: bool
    reproducibility_status: ReproducibilityStatus
    evidence_class: EvidenceClass
    signals: tuple[CryptoSignalDecision, ...]
    metrics: tuple[CryptoPerformanceMetrics, ...]
    warnings: tuple[str, ...]
    failure_reason: str | None
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoReplicationOutcome:
    outcome_id: str
    run_id: str
    decision: ExperimentOutcomeDecision
    reason: str
    decided_at: datetime
    actor: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoEvidenceDossier:
    dossier_id: str
    strategy_specification_id: str
    hypothesis_version_id: str
    original_snapshot_id: str
    original_run_id: str
    original_result_hash: str
    original_outcome_id: str
    replication_protocol_id: str
    replication_snapshot_id: str
    replication_run_id: str
    replication_result_hash: str
    replication_outcome_id: str
    shared_semantics_hash: str
    complete_windows: int
    passing_windows: int
    failing_windows: int
    median_conservative_net_return: Decimal
    worst_maximum_drawdown: Decimal
    consistency_status: str
    warnings: tuple[str, ...]
    created_at: datetime
    membership_hash: str
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoDossierConclusion:
    conclusion_id: str
    dossier_id: str
    conclusion: CryptoDossierConclusionType
    reason: str
    policy_version: str
    decided_at: datetime
    actor: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    authority: str = "NONE"


def conservative_metric(
    metrics: tuple[CryptoPerformanceMetrics, ...], portfolio: PortfolioName
) -> CryptoPerformanceMetrics:
    try:
        return next(
            value
            for value in metrics
            if value.portfolio is portfolio
            and value.scenario is CostScenarioName.CONSERVATIVE_RESEARCH
        )
    except StopIteration as error:
        raise CryptoDossierError("CRYPTO_CONSERVATIVE_METRIC_MISSING") from error


def run_passes_policy(run: CryptoReplicationRun) -> bool:
    if (
        not run.complete
        or run.state is not ExperimentState.COMPLETED
        or run.reproducibility_status is not ReproducibilityStatus.REPRODUCIBLE
    ):
        return False
    trend = conservative_metric(run.metrics, PortfolioName.TREND_RULE)
    baseline = conservative_metric(run.metrics, PortfolioName.BUY_AND_HOLD)
    return (
        trend.net_return >= 0
        and trend.maximum_drawdown < baseline.maximum_drawdown
        and trend.completed_round_trips >= 1
    )
