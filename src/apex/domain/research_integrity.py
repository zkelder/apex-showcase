"""Pure research-selection and backtest-overfitting controls.

The calculations in this module are evidence diagnostics.  They never grant
strategy, deployment, capital, order, PAPER, or LIVE authority.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import combinations

from apex.domain.values import content_digest

RESEARCH_METRICS_POLICY_ID = "RESEARCH_METRICS_V1"
PSR_POLICY_ID = "PROBABILISTIC_SHARPE_RATIO_V1"
DSR_POLICY_ID = "DEFLATED_SHARPE_RATIO_V1"
EFFECTIVE_TRIAL_POLICY_ID = "EFFECTIVE_TRIAL_COUNT_CORRELATION_V1"
CSCV_POLICY_ID = "CSCV_PBO_V1"
INTEGRITY_POLICY_ID = "RESEARCH_SELECTION_INTEGRITY_POLICY_V1"

MIN_DAILY_OBSERVATIONS = 120
MIN_EVENT_OBSERVATIONS = 60
MIN_INDEPENDENT_GROUPS = 6
MIN_PSR = Decimal("0.95")
MIN_DSR = Decimal("0.80")
MAX_PBO = Decimal("0.25")
MAX_WINNER_CHANGES = 1
MAX_PERIOD_CONCENTRATION = Decimal("0.35")


class ResearchFrequency(StrEnum):
    DAILY = "DAILY"
    EVENT_RESOLUTION = "EVENT_RESOLUTION"


class ResearchIntegrityError(ValueError):
    """Research evidence is invalid, incomparable, or insufficient."""


@dataclass(frozen=True, slots=True)
class ReturnObservation:
    period_id: str
    value: Decimal
    group_id: str
    instrument_id: str

    def __post_init__(self) -> None:
        if (
            not self.period_id
            or not self.group_id
            or not self.instrument_id
            or not self.value.is_finite()
        ):
            raise ResearchIntegrityError("RETURN_OBSERVATION_INVALID")


@dataclass(frozen=True, slots=True)
class NormalizedMetrics:
    observation_count: int
    independent_group_count: int
    gross_return: Decimal
    net_return: Decimal
    mean_periodic_return: Decimal
    periodic_volatility: Decimal
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    maximum_drawdown: Decimal
    turnover: Decimal
    hit_rate: Decimal
    skewness: Decimal | None
    excess_kurtosis: Decimal | None
    autocorrelations: tuple[Decimal, ...]
    effective_sample_size: Decimal
    largest_period_concentration: Decimal
    largest_group_concentration: Decimal
    largest_instrument_concentration: Decimal
    warnings: tuple[str, ...]
    policy_id: str = RESEARCH_METRICS_POLICY_ID


@dataclass(frozen=True, slots=True)
class EffectiveTrialEstimate:
    raw_trial_count: int
    effective_trial_count: Decimal
    overlap_observation_count: int
    overlap_coverage: Decimal
    incomparable_trial_ids: tuple[str, ...]
    eigenvalue_summary: dict[str, str]
    warnings: tuple[str, ...]
    policy_id: str = EFFECTIVE_TRIAL_POLICY_ID


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    expected_maximum_sharpe: Decimal
    probabilistic_sharpe_ratio: Decimal
    deflated_sharpe_ratio: Decimal
    minimum_track_record_length: Decimal | None
    current_observations: int
    remaining_observation_shortfall: Decimal | None
    policy_id: str = DSR_POLICY_ID


@dataclass(frozen=True, slots=True)
class CscvPartitionResult:
    ordinal: int
    training_period_ids: tuple[str, ...]
    test_period_ids: tuple[str, ...]
    selected_trial_id: str
    selected_test_rank_percentile: Decimal
    below_median: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CscvResult:
    probability_of_backtest_overfitting: Decimal
    partitions: tuple[CscvPartitionResult, ...]
    partition_count: int
    evaluated_split_count: int
    policy_id: str = CSCV_POLICY_ID


@dataclass(frozen=True, slots=True)
class StabilityDiagnostics:
    original_winner: str
    best_fold_excluded: dict[str, object]
    best_period_excluded: dict[str, object]
    top_group_excluded: dict[str, object]
    parameter_neighborhood: dict[str, object]
    canonical_hash: str


def _sample_standard_deviation(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise ResearchIntegrityError("SAMPLE_TOO_SHORT")
    mean = sum(values, Decimal()) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), Decimal()) / Decimal(len(values) - 1)
    if variance < 0:
        raise ResearchIntegrityError("NEGATIVE_VARIANCE")
    with localcontext() as context:
        context.prec = 40
        return variance.sqrt()


def _moment(values: Sequence[Decimal], order: int) -> Decimal:
    mean = sum(values, Decimal()) / Decimal(len(values))
    return sum(((value - mean) ** order for value in values), Decimal()) / Decimal(len(values))


def _correlation(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal:
    if len(left) != len(right) or len(left) < 3:
        raise ResearchIntegrityError("CORRELATION_ALIGNMENT_INVALID")
    left_mean = sum(left, Decimal()) / Decimal(len(left))
    right_mean = sum(right, Decimal()) / Decimal(len(right))
    numerator = sum(
        (
            (left_value - left_mean) * (right_value - right_mean)
            for left_value, right_value in zip(left, right, strict=True)
        ),
        Decimal(),
    )
    left_sum = sum(((value - left_mean) ** 2 for value in left), Decimal())
    right_sum = sum(((value - right_mean) ** 2 for value in right), Decimal())
    if left_sum == 0 or right_sum == 0:
        return Decimal()
    with localcontext() as context:
        context.prec = 40
        return numerator / (left_sum * right_sum).sqrt()


def _maximum_drawdown(values: Sequence[Decimal]) -> Decimal:
    equity = Decimal(1)
    peak = equity
    drawdown = Decimal()
    for value in values:
        equity *= Decimal(1) + value
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)
    return drawdown


def _concentration(observations: Sequence[ReturnObservation], attribute: str) -> Decimal:
    absolute_total = sum((abs(item.value) for item in observations), Decimal())
    if absolute_total == 0:
        return Decimal()
    grouped: dict[str, Decimal] = {}
    for item in observations:
        key = str(getattr(item, attribute))
        grouped[key] = grouped.get(key, Decimal()) + abs(item.value)
    return max(grouped.values(), default=Decimal()) / absolute_total


def normalize_return_metrics(
    *,
    gross_observations: Sequence[ReturnObservation],
    net_observations: Sequence[ReturnObservation],
    frequency: ResearchFrequency,
    turnover: Decimal = Decimal(),
    maximum_autocorrelation_lag: int = 5,
) -> NormalizedMetrics:
    """Calculate the fixed V1 exact periodic metric vocabulary."""

    if len(gross_observations) != len(net_observations) or not net_observations:
        raise ResearchIntegrityError("RETURN_SERIES_ALIGNMENT_INVALID")
    if tuple(item.period_id for item in gross_observations) != tuple(
        item.period_id for item in net_observations
    ):
        raise ResearchIntegrityError("RETURN_PERIOD_ALIGNMENT_INVALID")
    if turnover < 0 or not turnover.is_finite():
        raise ResearchIntegrityError("TURNOVER_INVALID")

    values = tuple(item.value for item in net_observations)
    count = len(values)
    mean = sum(values, Decimal()) / Decimal(count)
    warnings: list[str] = []
    volatility = Decimal()
    sharpe: Decimal | None = None
    sortino: Decimal | None = None
    skewness: Decimal | None = None
    excess_kurtosis: Decimal | None = None
    autocorrelations: list[Decimal] = []
    effective_sample_size = Decimal(count)
    if count >= 2:
        volatility = _sample_standard_deviation(values)
        if volatility > 0:
            annualizer = Decimal(252).sqrt() if frequency is ResearchFrequency.DAILY else Decimal(1)
            sharpe = mean / volatility * annualizer
        downside = tuple(min(value, Decimal()) for value in values)
        if any(value < 0 for value in downside):
            downside_deviation = (
                sum((value * value for value in downside), Decimal()) / Decimal(count)
            ).sqrt()
            if downside_deviation > 0:
                sortino = mean / downside_deviation
    if count >= 4 and volatility > 0:
        second = _moment(values, 2)
        skewness = _moment(values, 3) / (second ** Decimal("1.5"))
        excess_kurtosis = _moment(values, 4) / (second**2) - Decimal(3)
        for lag in range(1, min(maximum_autocorrelation_lag, count - 2) + 1):
            autocorrelations.append(_correlation(values[:-lag], values[lag:]))
        inflation = Decimal(1) + Decimal(2) * sum(
            (max(value, Decimal()) for value in autocorrelations), Decimal()
        )
        effective_sample_size = max(Decimal(1), min(Decimal(count), Decimal(count) / inflation))
    else:
        warnings.append("MOMENTS_OR_AUTOCORRELATION_UNSUPPORTED")
    if sharpe is None:
        warnings.append("SHARPE_UNSUPPORTED")

    return NormalizedMetrics(
        observation_count=count,
        independent_group_count=len({item.group_id for item in net_observations}),
        gross_return=_compound_return(tuple(item.value for item in gross_observations)),
        net_return=_compound_return(values),
        mean_periodic_return=mean,
        periodic_volatility=volatility,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        maximum_drawdown=_maximum_drawdown(values),
        turnover=turnover,
        hit_rate=Decimal(sum(value > 0 for value in values)) / Decimal(count),
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
        autocorrelations=tuple(autocorrelations),
        effective_sample_size=effective_sample_size,
        largest_period_concentration=_concentration(net_observations, "period_id"),
        largest_group_concentration=_concentration(net_observations, "group_id"),
        largest_instrument_concentration=_concentration(net_observations, "instrument_id"),
        warnings=tuple(warnings),
    )


def _compound_return(values: Sequence[Decimal]) -> Decimal:
    equity = Decimal(1)
    for value in values:
        equity *= Decimal(1) + value
    return equity - Decimal(1)


def probabilistic_sharpe_ratio(
    *,
    observed_sharpe: Decimal,
    benchmark_sharpe: Decimal,
    effective_sample_size: Decimal,
    skewness: Decimal,
    excess_kurtosis: Decimal,
) -> Decimal:
    """Bailey/López de Prado PSR using effective observations and sample moments."""

    if effective_sample_size < 3:
        raise ResearchIntegrityError("PSR_OBSERVATIONS_INSUFFICIENT")
    kurtosis = excess_kurtosis + Decimal(3)
    denominator_term = (
        Decimal(1)
        - skewness * observed_sharpe
        + ((kurtosis - Decimal(1)) / Decimal(4)) * observed_sharpe**2
    )
    if denominator_term <= 0:
        raise ResearchIntegrityError("PSR_DENOMINATOR_INVALID")
    with localcontext() as context:
        context.prec = 40
        statistic = (
            (observed_sharpe - benchmark_sharpe)
            * (effective_sample_size - Decimal(1)).sqrt()
            / denominator_term.sqrt()
        )
    return _normal_cdf(statistic)


def _normal_cdf(value: Decimal) -> Decimal:
    probability = (1.0 + math.erf(float(value) / math.sqrt(2.0))) / 2.0
    return Decimal(str(max(0.0, min(1.0, probability))))


def _inverse_normal_cdf(probability: Decimal) -> Decimal:
    """Acklam's bounded inverse-normal approximation."""

    if probability <= 0 or probability >= 1:
        raise ResearchIntegrityError("NORMAL_PROBABILITY_INVALID")
    p = float(probability)
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996,
        3.754408661907416,
    )
    low = 0.02425
    high = 1 - low
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        result = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    elif p <= high:
        q = p - 0.5
        r = q * q
        numerator = ((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]
        denominator = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
        result = numerator * q / denominator
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        result = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    return Decimal(str(result))


def estimate_effective_trial_count(
    aligned_returns: Mapping[str, Sequence[Decimal]],
) -> EffectiveTrialEstimate:
    """Eigenvalue participation ratio via trace(C)^2 / trace(C^2)."""

    raw_count = len(aligned_returns)
    if raw_count < 1:
        raise ResearchIntegrityError("NO_TRIALS")
    lengths = {len(values) for values in aligned_returns.values()}
    comparable = {
        trial_id: tuple(values)
        for trial_id, values in aligned_returns.items()
        if len(values) >= 3 and len(lengths) == 1
    }
    incomparable = tuple(sorted(set(aligned_returns) - set(comparable)))
    if len(comparable) < 2:
        return EffectiveTrialEstimate(
            raw_trial_count=raw_count,
            effective_trial_count=Decimal(raw_count),
            overlap_observation_count=min(lengths, default=0),
            overlap_coverage=Decimal(len(comparable)) / Decimal(raw_count),
            incomparable_trial_ids=incomparable,
            eigenvalue_summary={"method": "RAW_CONSERVATIVE_FALLBACK"},
            warnings=("ALIGNED_RETURNS_UNAVAILABLE_RAW_COUNT_USED",),
        )
    vectors = list(comparable.values())
    matrix: list[list[Decimal]] = []
    for left in vectors:
        matrix.append([_correlation(left, right) for right in vectors])
    trace = sum((matrix[index][index] for index in range(len(matrix))), Decimal())
    trace_squared = sum((value * value for row in matrix for value in row), Decimal())
    effective = trace * trace / trace_squared if trace_squared else Decimal(len(matrix))
    effective = max(Decimal(1), min(Decimal(raw_count), effective))
    return EffectiveTrialEstimate(
        raw_trial_count=raw_count,
        effective_trial_count=effective,
        overlap_observation_count=len(vectors[0]),
        overlap_coverage=Decimal(len(comparable)) / Decimal(raw_count),
        incomparable_trial_ids=incomparable,
        eigenvalue_summary={
            "trace": str(trace),
            "trace_squared": str(trace_squared),
            "participation_ratio": str(effective),
        },
        warnings=("INCOMPARABLE_TRIALS_EXCLUDED",) if incomparable else (),
    )


def deflated_sharpe_ratio(
    *,
    best_sharpe: Decimal,
    observed_trial_sharpes: Sequence[Decimal],
    effective_trial_count: Decimal,
    effective_sample_size: Decimal,
    skewness: Decimal,
    excess_kurtosis: Decimal,
    confidence_target: Decimal = Decimal("0.95"),
) -> DeflatedSharpeResult:
    """Deflate the selected Sharpe against the expected maximum null Sharpe."""

    if len(observed_trial_sharpes) < 2 or effective_trial_count < 1:
        raise ResearchIntegrityError("DSR_TRIALS_INSUFFICIENT")
    trial_volatility = _sample_standard_deviation(observed_trial_sharpes)
    effective = max(Decimal(1), min(Decimal(len(observed_trial_sharpes)), effective_trial_count))
    euler_gamma = Decimal("0.5772156649015329")
    first_probability = Decimal(1) - Decimal(1) / effective
    second_probability = Decimal(1) - Decimal(1) / (effective * Decimal(str(math.e)))
    first_probability = max(Decimal("0.000001"), min(Decimal("0.999999"), first_probability))
    second_probability = max(Decimal("0.000001"), min(Decimal("0.999999"), second_probability))
    expected_maximum = trial_volatility * (
        (Decimal(1) - euler_gamma) * _inverse_normal_cdf(first_probability)
        + euler_gamma * _inverse_normal_cdf(second_probability)
    )
    ordinary_psr = probabilistic_sharpe_ratio(
        observed_sharpe=best_sharpe,
        benchmark_sharpe=Decimal(),
        effective_sample_size=effective_sample_size,
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
    )
    deflated = probabilistic_sharpe_ratio(
        observed_sharpe=best_sharpe,
        benchmark_sharpe=expected_maximum,
        effective_sample_size=effective_sample_size,
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
    )
    minimum: Decimal | None = None
    remaining: Decimal | None = None
    denominator = best_sharpe - expected_maximum
    if denominator > 0:
        kurtosis = excess_kurtosis + Decimal(3)
        moment_term = (
            Decimal(1)
            - skewness * best_sharpe
            + ((kurtosis - Decimal(1)) / Decimal(4)) * best_sharpe**2
        )
        if moment_term > 0:
            minimum = (
                Decimal(1)
                + (_inverse_normal_cdf(confidence_target) * moment_term.sqrt() / denominator) ** 2
            )
            remaining = max(Decimal(), minimum - effective_sample_size)
    return DeflatedSharpeResult(
        expected_maximum_sharpe=expected_maximum,
        probabilistic_sharpe_ratio=ordinary_psr,
        deflated_sharpe_ratio=deflated,
        minimum_track_record_length=minimum,
        current_observations=int(effective_sample_size),
        remaining_observation_shortfall=remaining,
    )


def cscv_probability_of_backtest_overfitting(
    *,
    trial_returns: Mapping[str, Sequence[ReturnObservation]],
    partition_count: int = 6,
) -> CscvResult:
    """Bounded combinatorially symmetric cross-validation over aligned evidence."""

    if partition_count < 6 or partition_count > 16 or partition_count % 2:
        raise ResearchIntegrityError("CSCV_PARTITION_COUNT_INVALID")
    if len(trial_returns) < 3 or len(trial_returns) > 500:
        raise ResearchIntegrityError("CSCV_TRIAL_COUNT_INVALID")
    lengths = {len(values) for values in trial_returns.values()}
    if len(lengths) != 1:
        raise ResearchIntegrityError("CSCV_SERIES_INCOMPARABLE")
    observation_count = next(iter(lengths))
    if observation_count < MIN_DAILY_OBSERVATIONS or observation_count < partition_count:
        raise ResearchIntegrityError("CSCV_OBSERVATIONS_INSUFFICIENT")
    period_orders = {tuple(item.period_id for item in values) for values in trial_returns.values()}
    if len(period_orders) != 1:
        raise ResearchIntegrityError("CSCV_PERIOD_ALIGNMENT_INVALID")

    block_size, remainder = divmod(observation_count, partition_count)
    blocks: list[tuple[int, ...]] = []
    start = 0
    for ordinal in range(partition_count):
        length = block_size + (1 if ordinal < remainder else 0)
        blocks.append(tuple(range(start, start + length)))
        start += length
    all_splits = list(combinations(range(partition_count), partition_count // 2))
    if len(all_splits) > 256:
        stride = len(all_splits) / 256
        all_splits = [all_splits[int(index * stride)] for index in range(256)]

    trial_ids = tuple(sorted(trial_returns))
    periods = next(iter(period_orders))
    results: list[CscvPartitionResult] = []
    for ordinal, training_blocks in enumerate(all_splits, start=1):
        training_indices = tuple(index for block in training_blocks for index in blocks[block])
        test_indices = tuple(
            index
            for block in range(partition_count)
            if block not in training_blocks
            for index in blocks[block]
        )
        training_scores = {
            trial_id: sum(
                (trial_returns[trial_id][index].value for index in training_indices),
                Decimal(),
            )
            / Decimal(len(training_indices))
            for trial_id in trial_ids
        }
        selected = max(trial_ids, key=lambda item: (training_scores[item], item))
        test_scores = {
            trial_id: sum(
                (trial_returns[trial_id][index].value for index in test_indices), Decimal()
            )
            / Decimal(len(test_indices))
            for trial_id in trial_ids
        }
        ordered = sorted(trial_ids, key=lambda item: (test_scores[item], item))
        rank = ordered.index(selected)
        percentile = Decimal(rank) / Decimal(max(1, len(ordered) - 1))
        training_period_ids = tuple(periods[index] for index in training_indices)
        test_period_ids = tuple(periods[index] for index in test_indices)
        payload = {
            "ordinal": ordinal,
            "training_periods": training_period_ids,
            "test_periods": test_period_ids,
            "selected": selected,
            "rank": str(percentile),
        }
        results.append(
            CscvPartitionResult(
                ordinal=ordinal,
                training_period_ids=training_period_ids,
                test_period_ids=test_period_ids,
                selected_trial_id=selected,
                selected_test_rank_percentile=percentile,
                below_median=percentile < Decimal("0.5"),
                canonical_hash=content_digest(payload),
            )
        )
    pbo = Decimal(sum(item.below_median for item in results)) / Decimal(len(results))
    return CscvResult(
        probability_of_backtest_overfitting=pbo,
        partitions=tuple(results),
        partition_count=partition_count,
        evaluated_split_count=len(results),
    )


def selection_stability(
    *,
    trial_returns: Mapping[str, Sequence[ReturnObservation]],
    fold_count: int = 6,
    parameter_neighborhoods: Mapping[str, Sequence[str]] | None = None,
) -> StabilityDiagnostics:
    """Re-rank after deterministic fold, best-period, and top-group exclusions."""

    if len(trial_returns) < 2:
        raise ResearchIntegrityError("STABILITY_TRIALS_INSUFFICIENT")
    trial_ids = tuple(sorted(trial_returns))
    lengths = {len(values) for values in trial_returns.values()}
    if len(lengths) != 1:
        raise ResearchIntegrityError("STABILITY_SERIES_INCOMPARABLE")
    count = next(iter(lengths))
    if count < fold_count:
        raise ResearchIntegrityError("STABILITY_OBSERVATIONS_INSUFFICIENT")

    def score(trial_id: str, retained: Sequence[int]) -> Decimal:
        return sum(
            (trial_returns[trial_id][index].value for index in retained), Decimal()
        ) / Decimal(len(retained))

    all_indices = tuple(range(count))
    original = max(trial_ids, key=lambda item: (score(item, all_indices), item))
    fold_results: list[dict[str, object]] = []
    for fold in range(fold_count):
        retained = tuple(index for index in all_indices if index % fold_count != fold)
        winner = max(trial_ids, key=lambda item: (score(item, retained), item))
        fold_results.append(
            {
                "fold": fold,
                "winner": winner,
                "winner_changed": winner != original,
                "selected_metric": str(score(original, retained)),
            }
        )
    original_values = trial_returns[original]
    best_period_index = max(all_indices, key=lambda index: (original_values[index].value, index))
    without_period = tuple(index for index in all_indices if index != best_period_index)
    period_winner = max(trial_ids, key=lambda item: (score(item, without_period), item))
    group_contributions: dict[str, Decimal] = {}
    for item in original_values:
        group_contributions[item.group_id] = (
            group_contributions.get(item.group_id, Decimal()) + item.value
        )
    top_group = max(group_contributions, key=lambda item: (group_contributions[item], item))
    without_group = tuple(
        index for index, item in enumerate(original_values) if item.group_id != top_group
    )
    group_winner = (
        max(trial_ids, key=lambda item: (score(item, without_group), item))
        if without_group
        else original
    )
    neighborhoods: dict[str, object] = {"supported": False}
    if parameter_neighborhoods and original in parameter_neighborhoods:
        neighbors = tuple(
            item for item in parameter_neighborhoods[original] if item in trial_returns
        )
        neighborhoods = {
            "supported": True,
            "selected_trial": original,
            "neighbor_trial_ids": neighbors,
            "neighbor_metrics": {item: str(score(item, all_indices)) for item in neighbors},
            "isolated_spike": bool(neighbors)
            and all(
                score(original, all_indices) > score(item, all_indices) * Decimal("1.5")
                for item in neighbors
            ),
            "definition_hash": content_digest({"selected": original, "neighbors": neighbors}),
        }
    fold_summary = {
        "results": fold_results,
        "winner_change_count": sum(bool(item["winner_changed"]) for item in fold_results),
        "dominance_by_one_fold": sum(bool(item["winner_changed"]) for item in fold_results)
        > MAX_WINNER_CHANGES,
    }
    period_summary = {
        "excluded_period": original_values[best_period_index].period_id,
        "winner": period_winner,
        "winner_changed": period_winner != original,
        "selected_metric": str(score(original, without_period)),
    }
    group_summary = {
        "excluded_group": top_group,
        "winner": group_winner,
        "winner_changed": group_winner != original,
        "selected_metric": str(score(original, without_group)) if without_group else None,
    }
    payload = {
        "original": original,
        "fold": fold_summary,
        "period": period_summary,
        "group": group_summary,
        "neighborhood": neighborhoods,
    }
    return StabilityDiagnostics(
        original_winner=original,
        best_fold_excluded=fold_summary,
        best_period_excluded=period_summary,
        top_group_excluded=group_summary,
        parameter_neighborhood=neighborhoods,
        canonical_hash=content_digest(payload),
    )


def research_integrity_reasons(
    *,
    predeclared: bool,
    budget_respected: bool,
    all_attempts_retained: bool,
    aligned_scope: bool,
    aligned_costs: bool,
    material_simulator_mismatch: bool,
    lineage_valid: bool,
    artifacts_reproducible: bool,
    metrics: NormalizedMetrics,
    psr: Decimal,
    dsr: Decimal,
    pbo: Decimal,
    stability: StabilityDiagnostics,
    severe_recent_degradation: bool,
) -> tuple[str, ...]:
    """Return fixed hard-gate failures for the V1 research-only policy."""

    reasons: list[str] = []
    winner_change_count = stability.best_fold_excluded.get("winner_change_count")
    fold_stable = isinstance(winner_change_count, int) and winner_change_count <= MAX_WINNER_CHANGES
    gates = (
        (predeclared, "EXPERIMENT_NOT_PREDECLARED"),
        (budget_respected, "TRIAL_BUDGET_EXCEEDED"),
        (all_attempts_retained, "ATTEMPT_LEDGER_INCOMPLETE"),
        (aligned_scope, "DATA_OR_UNIVERSE_INCOMPARABLE"),
        (aligned_costs, "COST_OR_EXECUTION_ASSUMPTIONS_INCOMPARABLE"),
        (not material_simulator_mismatch, "MATERIAL_SIMULATOR_MISMATCH"),
        (lineage_valid, "LEAKAGE_OR_LINEAGE_FAILURE"),
        (artifacts_reproducible, "ARTIFACT_OR_METRIC_REPRODUCTION_FAILED"),
        (
            metrics.observation_count >= MIN_DAILY_OBSERVATIONS,
            "OBSERVATIONS_INSUFFICIENT",
        ),
        (
            metrics.independent_group_count >= MIN_INDEPENDENT_GROUPS,
            "INDEPENDENT_GROUPS_INSUFFICIENT",
        ),
        (psr >= MIN_PSR, "PROBABILISTIC_SHARPE_BELOW_THRESHOLD"),
        (dsr >= MIN_DSR, "DEFLATED_SHARPE_BELOW_THRESHOLD"),
        (pbo <= MAX_PBO, "PBO_ABOVE_MAXIMUM"),
        (
            fold_stable,
            "WINNER_FOLD_UNSTABLE",
        ),
        (
            not bool(stability.best_period_excluded["winner_changed"]),
            "WINNER_BEST_PERIOD_DEPENDENT",
        ),
        (
            not bool(stability.top_group_excluded["winner_changed"]),
            "WINNER_TOP_GROUP_DEPENDENT",
        ),
        (
            metrics.largest_period_concentration <= MAX_PERIOD_CONCENTRATION,
            "PERIOD_CONCENTRATION_EXCESSIVE",
        ),
        (not severe_recent_degradation, "SEVERE_RECENT_DEGRADATION"),
    )
    for passed, code in gates:
        if not passed:
            reasons.append(code)
    return tuple(reasons)
