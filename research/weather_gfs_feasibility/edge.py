"""Bounded, read-only GFS-versus-historical-market weather research.

This module intentionally lives outside :mod:`apex`: it can retain public
evidence, but owns no campaign, order, risk, ledger, or persistence state.
"""

from __future__ import annotations

import argparse
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from research.weather_gfs_feasibility.archive import (
    NoaaGfsArchive,
    SnapshotResult,
    reconstruct_daily_maximum,
)
from research.weather_gfs_feasibility.core import (
    FeasibilityError,
    GridPoint,
    RequestBudget,
    binding_configuration,
    canonical_json_bytes,
    compact_json,
    gfs_object_key,
    latest_gfs_initialization,
    local_contract_hours,
    nearest_gfs_grid_point,
    parse_gfs_index,
    sha256_hex,
)
from research.weather_gfs_feasibility.kalshi import (
    HistoricalCandle,
    HistoricalMarket,
    KalshiPublicEvidence,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIGURATION_PATH = Path(__file__).with_name("binding.json")
LOCAL_OUTPUT_ROOT = ROOT / ".local" / "research" / "weather_gfs_feasibility" / "weather_edge"
DEFAULT_BOOTSTRAP_SEED = 20260721
MINIMUM_EVENT_DATES = 60
MINIMUM_TRAIN_DATES = 40
MINIMUM_TEST_DATES = 15
MINIMUM_TEST_ROWS = 100
_GREATER = re.compile(
    r"\bis\s+(?:strictly\s+)?greater\s+than\s+(?P<strike>-?\d+(?:\.\d+)?)\s*(?:°|degrees?)", re.I
)
_LESS = re.compile(
    r"\bis\s+(?:strictly\s+)?less\s+than\s+(?P<strike>-?\d+(?:\.\d+)?)\s*(?:°|degrees?)", re.I
)
_BETWEEN = re.compile(
    r"\bbetween\s+(?P<lower>-?\d+(?:\.\d+)?)\s*(?:-|and)\s*"
    r"(?P<upper>-?\d+(?:\.\d+)?)",
    re.I,
)


@dataclass(frozen=True)
class StructuredThreshold:
    kind: Literal["GREATER_THAN", "LESS_THAN", "BETWEEN"]
    value: Decimal
    upper: Decimal | None = None


@dataclass(frozen=True)
class EligibleMarket:
    market: HistoricalMarket
    threshold: StructuredThreshold


@dataclass(frozen=True)
class PriceEvidence:
    market: EligibleMarket
    lead_hours: int
    as_of: datetime
    candle: HistoricalCandle


@dataclass(frozen=True)
class ResearchRow:
    contract_date: date
    ticker: str
    event_ticker: str
    series_ticker: str
    lead_hours: int
    threshold_kind: str
    threshold: Decimal
    result_yes: int
    forecast_daily_maximum: Decimal
    signed_margin: Decimal
    market_probability: Decimal
    candle_end_time: datetime
    forecast_initialization: datetime
    forecast_source_hash: str
    market_source_hash: str
    rule_sha256: str
    binding_hash: str
    threshold_upper: Decimal | None = None


@dataclass(frozen=True)
class Split:
    training_dates: tuple[date, ...]
    testing_dates: tuple[date, ...]
    configuration_hash: str


@dataclass(frozen=True)
class LogisticModel:
    name: str
    coefficients: tuple[float, ...]
    margin_mean: float
    margin_scale: float
    converged: bool
    iterations: int


@dataclass(frozen=True)
class Metric:
    observations: int
    distinct_event_dates: int
    coverage: Decimal
    yes_frequency: Decimal
    mean_probability: Decimal
    brier: Decimal
    log_loss: Decimal
    direction_accuracy: Decimal


def parse_leads(raw: str) -> tuple[int, ...]:
    try:
        leads = tuple(int(item) for item in raw.split(",") if item)
    except ValueError as error:
        raise argparse.ArgumentTypeError("lead hours must be comma-separated integers") from error
    if leads != (48, 24):
        raise argparse.ArgumentTypeError("lead hours must be exactly 48,24 in that order")
    return leads


def parse_threshold(rule: str) -> StructuredThreshold:
    greater = _GREATER.search(rule)
    less = _LESS.search(rule)
    if greater is not None and less is None:
        return StructuredThreshold("GREATER_THAN", _strict_decimal(greater.group("strike")))
    if less is not None and greater is None:
        return StructuredThreshold("LESS_THAN", _strict_decimal(less.group("strike")))
    between = _BETWEEN.search(rule)
    if between is not None and greater is None and less is None:
        lower = _strict_decimal(between.group("lower"))
        upper = _strict_decimal(between.group("upper"))
        if lower >= upper:
            raise FeasibilityError("structured BETWEEN strike has invalid bounds")
        return StructuredThreshold("BETWEEN", lower, upper)
    raise FeasibilityError("market rule has no supported structured strike")


def eligible_markets(
    markets: Sequence[HistoricalMarket], *, series_ticker: str
) -> tuple[tuple[EligibleMarket, ...], Counter[str]]:
    retained: list[EligibleMarket] = []
    rejected: Counter[str] = Counter()
    seen: set[str] = set()
    rule_families: dict[str, str] = {}
    for market in markets:
        if market.ticker in seen:
            raise FeasibilityError("duplicate market identity is not permitted")
        seen.add(market.ticker)
        if market.series_ticker != series_ticker:
            rejected["SERIES_MISMATCH"] += 1
            continue
        if market.result is None:
            rejected["MISSING_RESULT"] += 1
            continue
        if market.result in {"void", "disputed"}:
            rejected["VOIDED_OR_DISPUTED_OUTCOME"] += 1
            continue
        if market.result not in {"yes", "no"}:
            rejected["UNSUPPORTED_FINAL_RESULT"] += 1
            continue
        if market.status in {"void", "disputed"}:
            rejected["VOIDED_OR_DISPUTED_OUTCOME"] += 1
            continue
        if market.status is not None and market.status not in {"finalized", "settled", "closed"}:
            rejected["NON_SETTLED_STATUS"] += 1
            continue
        try:
            threshold = parse_threshold(market.rule)
        except FeasibilityError as error:
            rejected[str(error)] += 1
            continue
        family = _rule_family(market.rule)
        prior_family = rule_families.setdefault(threshold.kind, family)
        if prior_family != family:
            rejected["INCOMPATIBLE_RULE_VERSION"] += 1
            continue
        retained.append(EligibleMarket(market=market, threshold=threshold))
    return tuple(retained), rejected


def _rule_family(rule: str) -> str:
    normalized = re.sub(
        r"highest temperature recorded in Los Angeles Airport, CA for "
        r"[A-Z][a-z]+ \d{1,2}, \d{4} as reported by the National Weather Service's "
        r"Climatological Report \(Daily\)",
        "highest temperature recorded in Los Angeles Airport, CA for <DATE> as reported by "
        "the National Weather Service's Climatological Report (Daily)",
        rule,
        flags=re.IGNORECASE,
    )
    normalized = _GREATER.sub("is greater than <STRIKE>", normalized)
    normalized = _LESS.sub("is less than <STRIKE>", normalized)
    return _BETWEEN.sub("between <LOWER>-<UPPER>", normalized)


def select_candle_at_or_before(
    candles: Sequence[HistoricalCandle], *, as_of: datetime
) -> HistoricalCandle | None:
    if as_of.tzinfo is None:
        raise FeasibilityError("candle as-of timestamp must be timezone-aware")
    eligible = [candle for candle in candles if candle.end_time <= as_of.astimezone(UTC)]
    return max(eligible, key=lambda item: item.end_time) if eligible else None


def chronological_split(dates: Sequence[date]) -> Split:
    ordered = tuple(sorted(set(dates)))
    if not ordered:
        raise FeasibilityError("chronological split requires event dates")
    training_count = int(len(ordered) * Decimal("0.70"))
    if training_count <= 0 or training_count >= len(ordered):
        raise FeasibilityError("chronological split does not leave both partitions")
    train, test = ordered[:training_count], ordered[training_count:]
    configuration_hash = sha256_hex(
        canonical_json_bytes(
            {
                "algorithm": "chronological-earliest-70-percent-v1",
                "train": [item.isoformat() for item in train],
                "test": [item.isoformat() for item in test],
            }
        )
    )
    return Split(train, test, configuration_hash)


def signed_margin(forecast_daily_maximum: Decimal, threshold: StructuredThreshold) -> Decimal:
    if threshold.kind == "GREATER_THAN":
        return forecast_daily_maximum - threshold.value
    if threshold.kind == "LESS_THAN":
        return threshold.value - forecast_daily_maximum
    if threshold.upper is None:
        raise FeasibilityError("BETWEEN threshold is missing upper bound")
    return min(forecast_daily_maximum - threshold.value, threshold.upper - forecast_daily_maximum)


def model_a_probability(row: ResearchRow) -> Decimal:
    return Decimal("1") if row.signed_margin > 0 else Decimal("0")


def _features(row: ResearchRow, *, model: str, mean: float, scale: float) -> tuple[float, ...]:
    normalized = (float(row.signed_margin) - mean) / scale
    if model == "B":
        return (1.0, normalized)
    is_24 = 1.0 if row.lead_hours == 24 else 0.0
    return (1.0, normalized, is_24, normalized * is_24)


def fit_logistic(rows: Sequence[ResearchRow], *, model: Literal["B", "C"]) -> LogisticModel:
    if not rows:
        raise FeasibilityError("logistic fitting requires training rows")
    raw_margins = [float(row.signed_margin) for row in rows]
    mean = sum(raw_margins) / len(raw_margins)
    variance = sum((value - mean) ** 2 for value in raw_margins) / len(raw_margins)
    scale = max(math.sqrt(variance), 1.0)
    coefficient_count = 2 if model == "B" else 4
    coefficients = [0.0] * coefficient_count
    regularization = 0.02
    learning_rate = 0.2
    converged = False
    iterations = 0
    for iteration in range(1, 5001):
        gradients = [0.0] * coefficient_count
        for row in rows:
            vector = _features(row, model=model, mean=mean, scale=scale)
            probability = _sigmoid(
                sum(
                    value * coefficient
                    for value, coefficient in zip(vector, coefficients, strict=True)
                )
            )
            error = probability - float(row.result_yes)
            for index, value in enumerate(vector):
                gradients[index] += error * value
        maximum_change = 0.0
        for index in range(coefficient_count):
            penalty = regularization * coefficients[index] if index else 0.0
            change = learning_rate * (gradients[index] / len(rows) + penalty)
            coefficients[index] -= change
            maximum_change = max(maximum_change, abs(change))
        iterations = iteration
        if maximum_change < 1e-7:
            converged = True
            break
    return LogisticModel(
        name=f"MODEL_{model}",
        coefficients=tuple(coefficients),
        margin_mean=mean,
        margin_scale=scale,
        converged=converged,
        iterations=iterations,
    )


def model_probability(model: LogisticModel, row: ResearchRow) -> Decimal:
    model_code = "B" if model.name == "MODEL_B" else "C"
    vector = _features(row, model=model_code, mean=model.margin_mean, scale=model.margin_scale)
    value = _sigmoid(
        sum(
            item * coefficient for item, coefficient in zip(vector, model.coefficients, strict=True)
        )
    )
    return Decimal(str(value))


def select_model(
    training_rows: Sequence[ResearchRow],
) -> tuple[LogisticModel, LogisticModel, LogisticModel, str]:
    model_b = fit_logistic(training_rows, model="B")
    counts = Counter(row.lead_hours for row in training_rows)
    model_c = fit_logistic(training_rows, model="C")
    if counts[48] >= 20 and counts[24] >= 20 and model_c.converged:
        return model_b, model_c, model_c, "MODEL_C_IF_EACH_LEAD_HAS_20_TRAINING_ROWS_AND_CONVERGES"
    return model_b, model_c, model_b, "MODEL_C_IF_EACH_LEAD_HAS_20_TRAINING_ROWS_AND_CONVERGES"


def evaluate(
    rows: Sequence[ResearchRow], probabilities: Sequence[Decimal], *, total_rows: int
) -> Metric:
    if len(rows) != len(probabilities) or not rows:
        raise FeasibilityError("metric evaluation requires aligned non-empty rows")
    labels = [Decimal(row.result_yes) for row in rows]
    clipped = [_clip_probability(value) for value in probabilities]
    brier = sum(
        ((value - label) ** 2 for value, label in zip(clipped, labels, strict=True)),
        Decimal("0"),
    ) / len(rows)
    log_loss = -sum(
        (
            label * _decimal_log(value)
            + (Decimal("1") - label) * _decimal_log(Decimal("1") - value)
            for value, label in zip(clipped, labels, strict=True)
        ),
        Decimal("0"),
    ) / len(rows)
    accuracy = sum(
        int((value >= Decimal("0.5")) == bool(label))
        for value, label in zip(clipped, labels, strict=True)
    )
    return Metric(
        observations=len(rows),
        distinct_event_dates=len({row.contract_date for row in rows}),
        coverage=Decimal(len(rows)) / Decimal(total_rows),
        yes_frequency=sum(labels, Decimal("0")) / len(rows),
        mean_probability=sum(clipped, Decimal("0")) / len(rows),
        brier=brier,
        log_loss=log_loss,
        direction_accuracy=Decimal(accuracy) / len(rows),
    )


def calibration(
    rows: Sequence[ResearchRow], probabilities: Sequence[Decimal]
) -> list[dict[str, object]]:
    if len(rows) != len(probabilities):
        raise FeasibilityError("calibration requires aligned rows and probabilities")
    bins = (
        (Decimal("0.0"), Decimal("0.2")),
        (Decimal("0.2"), Decimal("0.4")),
        (Decimal("0.4"), Decimal("0.6")),
        (Decimal("0.6"), Decimal("0.8")),
        (Decimal("0.8"), Decimal("1.0")),
    )
    table: list[dict[str, object]] = []
    for index, (lower, upper) in enumerate(bins):
        matched = [
            (row, probability)
            for row, probability in zip(rows, probabilities, strict=True)
            if lower <= probability <= upper and (index == len(bins) - 1 or probability < upper)
        ]
        if not matched:
            table.append({"lower": lower, "upper": upper, "count": 0, "distinct_event_dates": 0})
            continue
        table.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(matched),
                "distinct_event_dates": len({row.contract_date for row, _ in matched}),
                "mean_model_probability": sum(value for _, value in matched) / len(matched),
                "yes_frequency": Decimal(sum(row.result_yes for row, _ in matched)) / len(matched),
                "mean_market_probability": sum(row.market_probability for row, _ in matched)
                / len(matched),
            }
        )
    return table


def block_bootstrap(
    rows: Sequence[ResearchRow],
    forecast: Sequence[Decimal],
    market: Sequence[Decimal],
    *,
    repetitions: int,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, object]:
    if repetitions <= 0 or len(rows) != len(forecast) or len(rows) != len(market):
        raise FeasibilityError("bootstrap inputs are invalid")
    by_date: dict[date, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_date[row.contract_date].append(index)
    dates = tuple(sorted(by_date))
    if not dates:
        raise FeasibilityError("bootstrap requires event dates")
    generator = random.Random(seed)
    brier_differences: list[Decimal] = []
    log_differences: list[Decimal] = []
    for _ in range(repetitions):
        indexes = [
            index
            for sampled in (generator.choice(dates) for _ in dates)
            for index in by_date[sampled]
        ]
        brier_differences.append(_brier_difference(rows, forecast, market, indexes))
        log_differences.append(_log_loss_difference(rows, forecast, market, indexes))
    return {
        "method": "event-date-block-bootstrap-percentile-v1",
        "seed": seed,
        "repetitions": repetitions,
        "negative_difference_means_forecast_model_scored_better": True,
        "brier_difference_ci_95": _percentile_interval(brier_differences),
        "log_loss_difference_ci_95": _percentile_interval(log_differences),
    }


def historical_diagnostic(
    rows: Sequence[ResearchRow], probabilities: Sequence[Decimal]
) -> list[dict[str, object]]:
    if len(rows) != len(probabilities):
        raise FeasibilityError("diagnostic requires aligned rows and probabilities")
    diagnostics: list[dict[str, object]] = []
    for row, probability in zip(rows, probabilities, strict=True):
        disagreement = probability - row.market_probability
        if abs(disagreement) < Decimal("0.05"):
            continue
        diagnostics.append(
            {
                "label": "NON_EXECUTABLE_HISTORICAL_DIAGNOSTIC",
                "ticker": row.ticker,
                "event_date": row.contract_date,
                "lead_hours": row.lead_hours,
                "direction": "YES" if disagreement > 0 else "NO",
                "eventual_binary_outcome": row.result_yes,
                "gross_probability_disagreement": disagreement,
                "hypothetical_quantity": Decimal("1"),
                "slippage_reserve": Decimal("0.02"),
                "fee_evidence": "UNSUPPORTED_HISTORICAL_FEE_EVIDENCE",
                "modeled_fee": None,
                "conservative_research_score": None,
            }
        )
    return diagnostics


def _strict_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise FeasibilityError("value is not an exact decimal") from error
    if not parsed.is_finite():
        raise FeasibilityError("value is not finite")
    return parsed


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 700.0)))
    exponential = math.exp(max(value, -700.0))
    return exponential / (1.0 + exponential)


def _clip_probability(value: Decimal) -> Decimal:
    return min(max(value, Decimal("0.000000000001")), Decimal("0.999999999999"))


def _decimal_log(value: Decimal) -> Decimal:
    return Decimal(str(math.log(float(value))))


def _brier_difference(
    rows: Sequence[ResearchRow],
    forecast: Sequence[Decimal],
    market: Sequence[Decimal],
    indexes: Sequence[int],
) -> Decimal:
    return sum(
        (
            (_clip_probability(forecast[index]) - Decimal(rows[index].result_yes)) ** 2
            - (_clip_probability(market[index]) - Decimal(rows[index].result_yes)) ** 2
            for index in indexes
        ),
        Decimal("0"),
    ) / len(indexes)


def _log_loss_difference(
    rows: Sequence[ResearchRow],
    forecast: Sequence[Decimal],
    market: Sequence[Decimal],
    indexes: Sequence[int],
) -> Decimal:
    def loss(value: Decimal, label: Decimal) -> Decimal:
        clipped = _clip_probability(value)
        return -(
            label * _decimal_log(clipped)
            + (Decimal("1") - label) * _decimal_log(Decimal("1") - clipped)
        )

    return sum(
        (
            loss(forecast[index], Decimal(rows[index].result_yes))
            - loss(market[index], Decimal(rows[index].result_yes))
            for index in indexes
        ),
        Decimal("0"),
    ) / len(indexes)


def _percentile_interval(values: Sequence[Decimal]) -> dict[str, Decimal]:
    ordered = sorted(values)
    return {
        "lower": ordered[round((len(ordered) - 1) * 0.025)],
        "upper": ordered[round((len(ordered) - 1) * 0.975)],
    }


def _evenly_spaced(items: Sequence[date], count: int) -> tuple[date, ...]:
    ordered = tuple(sorted(items))
    if count >= len(ordered):
        return ordered
    if count <= 0:
        return ()
    if count == 1:
        return (ordered[0],)
    indexes = [round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)]
    return tuple(ordered[index] for index in indexes)


def _snapshot_request_count(
    *, contract_date: date, close_time: datetime, lead_hours: int, timezone_name: str
) -> int:
    initialization = latest_gfs_initialization(
        close_time.astimezone(UTC) - timedelta(hours=lead_hours)
    )
    return 2 * len(
        local_contract_hours(
            initialization=initialization, contract_date=contract_date, timezone_name=timezone_name
        )
    )


def _estimated_bytes(requests: int) -> int:
    """Conservative evidence-based estimate: 600 KiB per index/range request."""

    return requests * 600 * 1024


def _binding_record(configuration: dict[str, object], terms_hash: str) -> dict[str, object]:
    return {
        "selected_series": configuration["selected_series"],
        "configuration_version": configuration["binding_version"],
        "configuration_hash": configuration["canonical_configuration_hash"],
        "contract_terms_hash": terms_hash,
        "station": configuration["station"],
        "latitude": configuration["latitude"],
        "longitude": configuration["longitude"],
        "timezone": configuration["timezone"],
        "temperature_unit": configuration["temperature_unit"],
        "contract_type": configuration["contract_type"],
    }


def _write_report(report: dict[str, object]) -> Path:
    target = LOCAL_OUTPUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target.mkdir(parents=True, exist_ok=False)
    path = target / "weather-edge-report.json"
    path.write_text(compact_json(report) + "\n", encoding="utf-8")
    return path


def _run_cache() -> Path:
    path = LOCAL_OUTPUT_ROOT / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _group_by_date(markets: Sequence[EligibleMarket]) -> dict[date, tuple[EligibleMarket, ...]]:
    grouped: dict[date, list[EligibleMarket]] = defaultdict(list)
    for market in markets:
        grouped[market.market.contract_date].append(market)
    return {
        day: tuple(sorted(values, key=lambda item: item.market.ticker))
        for day, values in grouped.items()
    }


def _market_prices(
    *,
    kalshi: KalshiPublicEvidence,
    markets: Sequence[EligibleMarket],
    leads: Sequence[int],
) -> tuple[dict[tuple[str, int], PriceEvidence], Counter[str]]:
    retained: dict[tuple[str, int], PriceEvidence] = {}
    rejected: Counter[str] = Counter()
    for index, eligible in enumerate(markets, start=1):
        close_time = _parse_timestamp(eligible.market.close_time)
        for lead in leads:
            as_of = close_time - timedelta(hours=lead)
            candles = kalshi.historical_candles(
                ticker=eligible.market.ticker,
                start_time=as_of - timedelta(days=14),
                end_time=as_of,
            )
            candle = select_candle_at_or_before(candles, as_of=as_of)
            if candle is None:
                rejected["MISSING_HISTORICAL_PRICE"] += 1
                continue
            if candle.end_time > as_of:
                raise FeasibilityError("later market candle was selected")
            retained[(eligible.market.ticker, lead)] = PriceEvidence(eligible, lead, as_of, candle)
        if index % 100 == 0:
            print(f"KALSHI_PRICE_PROGRESS={index}/{len(markets)}")
    return retained, rejected


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise FeasibilityError("market close time is missing timezone")
    return parsed.astimezone(UTC)


def _date_has_both_leads(
    markets: Sequence[EligibleMarket], prices: dict[tuple[str, int], PriceEvidence]
) -> bool:
    return any(
        (market.market.ticker, 48) in prices and (market.market.ticker, 24) in prices
        for market in markets
    )


def _preflight_extraction_method(
    archive: NoaaGfsArchive, event: EligibleMarket
) -> dict[str, object]:
    close_time = _parse_timestamp(event.market.close_time)
    as_of = close_time - timedelta(hours=48)
    initialization = latest_gfs_initialization(as_of)
    forecast_hour = local_contract_hours(
        initialization=initialization,
        contract_date=event.market.contract_date,
        timezone_name="America/Los_Angeles",
    )[0]
    index, source = archive.index(gfs_object_key(initialization, forecast_hour.forecast_hour))
    records = parse_gfs_index(index)
    interval_maximum_records = [record.raw for record in records if record.field == "TMAX"]
    return {
        "method": "HOURLY_TMP_2M_LOCAL_DAY_V1",
        "reason": (
            "hourly TMP is retained because archive index does not provide proven exact "
            "local-day interval boundaries"
        ),
        "preflight_object": source,
        "interval_maximum_record_count": len(interval_maximum_records),
        "interval_maximum_records": interval_maximum_records,
    }


def run_real(
    *,
    series: str,
    start_date: date,
    end_date: date,
    leads: tuple[int, ...],
    max_event_dates: int,
    bootstrap_repetitions: int,
    max_noaa_requests: int,
    max_download_mb: int,
    timeout_seconds: int,
) -> dict[str, object]:
    if series != "KXHIGHLAX":
        raise FeasibilityError("this reviewed research binding supports KXHIGHLAX only")
    if not 60 <= max_event_dates <= 160:
        raise FeasibilityError("max event dates must be between 60 and 160")
    if (
        start_date > end_date
        or bootstrap_repetitions <= 0
        or max_noaa_requests <= 0
        or max_download_mb <= 0
    ):
        raise FeasibilityError("research bounds are invalid")
    configuration = binding_configuration(CONFIGURATION_PATH)
    cache_directory = _run_cache()
    kalshi = KalshiPublicEvidence(timeout_seconds=timeout_seconds, cache_directory=cache_directory)
    terms_hash = kalshi.verify_contract_terms(configuration)
    resolution = kalshi.resolve_daily_high_binding(
        requested_series=series, configuration=configuration
    )
    if resolution.selected_series != series:
        raise FeasibilityError(
            "reviewed KXHIGHLAX binding no longer matches public series metadata"
        )
    binding = _binding_record(configuration, terms_hash)
    print("REVIEWED_BINDING=" + compact_json(binding))
    all_markets = kalshi.historical_daily_high_markets(
        series_ticker=series, start_date=start_date, end_date=end_date, maximum_pages=16
    )
    accepted, market_rejections = eligible_markets(all_markets, series_ticker=series)
    by_date = _group_by_date(accepted)
    inventory_dates = tuple(sorted(by_date))
    print(
        "KALSHI_INVENTORY="
        + compact_json(
            {
                "markets_received": len(all_markets),
                "eligible_markets": len(accepted),
                "eligible_dates": len(inventory_dates),
                "rejected": dict(sorted(market_rejections.items())),
            }
        )
    )
    inventory_sample = _evenly_spaced(inventory_dates, min(len(inventory_dates), max_event_dates))
    sampled_markets = [market for day in inventory_sample for market in by_date[day]]
    prices, price_rejections = _market_prices(kalshi=kalshi, markets=sampled_markets, leads=leads)
    price_complete_dates = tuple(
        day for day in inventory_sample if _date_has_both_leads(by_date[day], prices)
    )
    if len(price_complete_dates) < MINIMUM_EVENT_DATES:
        return _insufficient_report(
            binding=binding,
            all_markets=all_markets,
            accepted=accepted,
            market_rejections=market_rejections,
            price_rejections=price_rejections,
            price_complete_dates=price_complete_dates,
            reason="insufficient dates with a historical price for both lead buckets",
        )
    dates = price_complete_dates
    per_date_requests = {
        day: sum(
            _snapshot_request_count(
                contract_date=day,
                close_time=_parse_timestamp(by_date[day][0].market.close_time),
                lead_hours=lead,
                timezone_name=str(configuration["timezone"]),
            )
            for lead in leads
        )
        for day in dates
    }
    chosen = _evenly_spaced(dates, min(max_event_dates, len(dates)))
    maximum_bytes = max_download_mb * 1024 * 1024
    while chosen and (
        sum(per_date_requests[day] for day in chosen) + 1 > max_noaa_requests
        or _estimated_bytes(sum(per_date_requests[day] for day in chosen) + 1) > maximum_bytes
    ):
        chosen = _evenly_spaced(dates, len(chosen) - 1)
    estimated_requests = sum(per_date_requests[day] for day in chosen) + (1 if chosen else 0)
    estimated_bytes = _estimated_bytes(estimated_requests)
    print(
        "GFS_PLAN="
        + compact_json(
            {
                "sampling_rule": "deterministic-evenly-spaced-chronological-event-dates-v1",
                "date_count": len(chosen),
                "estimated_requests": estimated_requests,
                "estimated_bytes": estimated_bytes,
                "max_requests": max_noaa_requests,
                "max_bytes": maximum_bytes,
            }
        )
    )
    split = chronological_split(chosen) if chosen else None
    budget = RequestBudget(maximum_requests=max_noaa_requests, maximum_bytes=maximum_bytes)
    archive = NoaaGfsArchive(
        budget=budget,
        timeout_seconds=timeout_seconds,
        cache_directory=cache_directory,
    )
    extraction: dict[str, object] | None = None
    if chosen:
        extraction = _preflight_extraction_method(archive, by_date[chosen[0]][0])
        print("EXTRACTION_METHOD=" + compact_json(extraction))
    if (
        len(chosen) < MINIMUM_EVENT_DATES
        or split is None
        or len(split.training_dates) < MINIMUM_TRAIN_DATES
        or len(split.testing_dates) < MINIMUM_TEST_DATES
        or estimated_bytes > maximum_bytes
    ):
        return _resource_report(
            binding=binding,
            all_markets=all_markets,
            accepted=accepted,
            market_rejections=market_rejections,
            price_rejections=price_rejections,
            chosen=chosen,
            estimated_requests=estimated_requests,
            estimated_bytes=estimated_bytes,
            split=split,
            extraction=extraction,
            requests=budget.requests,
            downloaded_bytes=budget.downloaded_bytes,
        )
    grid = nearest_gfs_grid_point(
        Decimal(str(configuration["latitude"])), Decimal(str(configuration["longitude"]))
    )
    if extraction is None:
        raise FeasibilityError("GFS extraction-method preflight was not retained")
    snapshots: dict[tuple[date, int], SnapshotResult] = {}
    snapshot_rejections: Counter[str] = Counter()
    for index, day in enumerate(chosen, start=1):
        close_time = _parse_timestamp(by_date[day][0].market.close_time)
        for lead in leads:
            snapshot = reconstruct_daily_maximum(
                archive=archive,
                contract_date=day,
                close_time=close_time,
                lead_hours=lead,
                timezone_name=str(configuration["timezone"]),
                grid_point=grid,
                output_unit=str(configuration["temperature_unit"]),
            )
            if snapshot.complete:
                snapshots[(day, lead)] = snapshot
            else:
                snapshot_rejections[snapshot.reason or "UNKNOWN_SNAPSHOT_FAILURE"] += 1
        print(
            "GFS_PROGRESS="
            + compact_json(
                {
                    "event_dates_completed": index,
                    "event_dates_planned": len(chosen),
                    "requests": budget.requests,
                    "bytes": budget.downloaded_bytes,
                }
            )
        )
    rows = _dataset_rows(
        dates=chosen,
        markets=by_date,
        prices=prices,
        snapshots=snapshots,
        configuration_hash=str(configuration["canonical_configuration_hash"]),
    )
    report = _evaluate_report(
        binding=binding,
        grid=grid,
        extraction=extraction,
        all_markets=all_markets,
        accepted=accepted,
        market_rejections=market_rejections,
        price_rejections=price_rejections,
        snapshot_rejections=snapshot_rejections,
        split=split,
        rows=rows,
        snapshots=snapshots,
        requests=budget.requests,
        downloaded_bytes=budget.downloaded_bytes,
        bootstrap_repetitions=bootstrap_repetitions,
    )
    report["local_report"] = str(_write_report(report))
    print("WEATHER_EDGE_REPORT=" + compact_json(_compact_report(report)))
    return report


def _dataset_rows(
    *,
    dates: Sequence[date],
    markets: dict[date, tuple[EligibleMarket, ...]],
    prices: dict[tuple[str, int], PriceEvidence],
    snapshots: dict[tuple[date, int], SnapshotResult],
    configuration_hash: str,
) -> tuple[ResearchRow, ...]:
    rows: list[ResearchRow] = []
    for day in dates:
        for market in markets[day]:
            for lead in (48, 24):
                price = prices.get((market.market.ticker, lead))
                snapshot = snapshots.get((day, lead))
                if price is None or snapshot is None or snapshot.daily_maximum is None:
                    continue
                if price.candle.end_time > price.as_of or snapshot.initialization > snapshot.as_of:
                    raise FeasibilityError("a future evidence record was selected")
                rows.append(
                    ResearchRow(
                        contract_date=day,
                        ticker=market.market.ticker,
                        event_ticker=market.market.event_ticker,
                        series_ticker=market.market.series_ticker,
                        lead_hours=lead,
                        threshold_kind=market.threshold.kind,
                        threshold=market.threshold.value,
                        result_yes=1 if market.market.result == "yes" else 0,
                        forecast_daily_maximum=snapshot.daily_maximum,
                        signed_margin=signed_margin(snapshot.daily_maximum, market.threshold),
                        market_probability=_strict_decimal(price.candle.probability),
                        candle_end_time=price.candle.end_time,
                        forecast_initialization=snapshot.initialization,
                        forecast_source_hash=sha256_hex(compact_json(snapshot)),
                        market_source_hash=price.candle.source_response_sha256,
                        rule_sha256=market.market.rule_sha256,
                        binding_hash=configuration_hash,
                        threshold_upper=market.threshold.upper,
                    )
                )
    return tuple(rows)


def _evaluate_report(
    *,
    binding: dict[str, object],
    grid: GridPoint,
    extraction: dict[str, object],
    all_markets: Sequence[HistoricalMarket],
    accepted: Sequence[EligibleMarket],
    market_rejections: Counter[str],
    price_rejections: Counter[str],
    snapshot_rejections: Counter[str],
    split: Split,
    rows: Sequence[ResearchRow],
    snapshots: dict[tuple[date, int], SnapshotResult],
    requests: int,
    downloaded_bytes: int,
    bootstrap_repetitions: int,
) -> dict[str, object]:
    training = [row for row in rows if row.contract_date in set(split.training_dates)]
    testing = [row for row in rows if row.contract_date in set(split.testing_dates)]
    test_leads = Counter(row.lead_hours for row in testing)
    if (
        len({row.contract_date for row in rows}) < MINIMUM_EVENT_DATES
        or len({row.contract_date for row in training}) < MINIMUM_TRAIN_DATES
        or len({row.contract_date for row in testing}) < MINIMUM_TEST_DATES
        or len(testing) < MINIMUM_TEST_ROWS
        or not test_leads[48]
        or not test_leads[24]
    ):
        return {
            "classification": "INSUFFICIENT_DATA",
            "reason": (
                "complete GFS and historical-price evidence did not meet the predeclared data gate"
            ),
            "binding": binding,
            "grid": grid,
            "extraction": extraction,
            "rows": len(rows),
            "snapshot_coverage": len(snapshots),
            "requests": requests,
            "downloaded_bytes": downloaded_bytes,
        }
    model_b, model_c, selected, selected_rule = select_model(training)
    selected_probabilities = [model_probability(selected, row) for row in testing]
    market_probabilities = [row.market_probability for row in testing]
    raw_probabilities = [model_a_probability(row) for row in testing]
    model_metric = evaluate(testing, selected_probabilities, total_rows=len(testing))
    market_metric = evaluate(testing, market_probabilities, total_rows=len(testing))
    raw_metric = evaluate(testing, raw_probabilities, total_rows=len(testing))
    lead_reports: dict[str, object] = {}
    for lead in (48, 24):
        lead_rows = [row for row in testing if row.lead_hours == lead]
        indexes = [index for index, row in enumerate(testing) if row.lead_hours == lead]
        lead_reports[str(lead)] = {
            "forecast": evaluate(
                lead_rows,
                [selected_probabilities[index] for index in indexes],
                total_rows=len(lead_rows),
            ),
            "market": evaluate(
                lead_rows,
                [market_probabilities[index] for index in indexes],
                total_rows=len(lead_rows),
            ),
            "raw_direction": evaluate(
                lead_rows,
                [raw_probabilities[index] for index in indexes],
                total_rows=len(lead_rows),
            ),
        }
    bootstrap = block_bootstrap(
        testing,
        selected_probabilities,
        market_probabilities,
        repetitions=bootstrap_repetitions,
    )
    diagnostics = historical_diagnostic(testing, selected_probabilities)
    classification = _classification(
        forecast=model_metric,
        market=market_metric,
        lead_reports=lead_reports,
        bootstrap=bootstrap,
        diagnostics=diagnostics,
    )
    return {
        "classification": classification,
        "binding": binding,
        "grid": grid,
        "extraction": extraction,
        "twelve_hour_boundary": (
            "deferred: observed station maximum through as-of plus forecast maximum for the "
            "remaining local day would be required; no NCEI observations were fetched"
        ),
        "inventory": {
            "historical_markets": len(all_markets),
            "eligible_markets": len(accepted),
            "market_rejections": dict(sorted(market_rejections.items())),
            "price_rejections": dict(sorted(price_rejections.items())),
        },
        "snapshot_coverage": {
            "complete": len(snapshots),
            "rejections": dict(sorted(snapshot_rejections.items())),
        },
        "resources": {"requests": requests, "downloaded_bytes": downloaded_bytes},
        "split": split,
        "dataset": {
            "training_rows": len(training),
            "testing_rows": len(testing),
            "testing_by_lead": dict(test_leads),
        },
        "models": {
            "B": model_b,
            "C": model_c,
            "selected": selected.name,
            "selection_rule": selected_rule,
        },
        "test": {
            "forecast": model_metric,
            "market": market_metric,
            "raw_direction": raw_metric,
            "by_lead": lead_reports,
            "brier_difference": model_metric.brier - market_metric.brier,
            "log_loss_difference": model_metric.log_loss - market_metric.log_loss,
            "calibration": calibration(testing, selected_probabilities),
            "bootstrap": bootstrap,
        },
        "non_executable_historical_diagnostic": {
            "qualifying_disagreements": len(diagnostics),
            "yes_direction": sum(item["direction"] == "YES" for item in diagnostics),
            "no_direction": sum(item["direction"] == "NO" for item in diagnostics),
            "rows": diagnostics,
        },
    }


def _classification(
    *,
    forecast: Metric,
    market: Metric,
    lead_reports: dict[str, object],
    bootstrap: dict[str, object],
    diagnostics: Sequence[dict[str, object]],
) -> str:
    brier_interval = bootstrap["brier_difference_ci_95"]
    if not isinstance(brier_interval, dict):
        raise FeasibilityError("bootstrap report is malformed")
    upper = brier_interval.get("upper")
    coherent = True
    for lead in ("48", "24"):
        report = lead_reports[lead]
        if not isinstance(report, dict):
            raise FeasibilityError("lead report is malformed")
        lead_forecast, lead_market = report["forecast"], report["market"]
        if not isinstance(lead_forecast, Metric) or not isinstance(lead_market, Metric):
            raise FeasibilityError("lead metric report is malformed")
        coherent = coherent and lead_forecast.brier < lead_market.brier
    if (
        forecast.brier < market.brier
        and isinstance(upper, Decimal)
        and upper < Decimal("-0.005")
        and forecast.log_loss <= market.log_loss + Decimal("0.01")
        and coherent
        and len(diagnostics) >= 10
    ):
        return "WEATHER_HYPOTHESIS_PROMISING"
    return "WEATHER_HYPOTHESIS_NOT_SUPPORTED"


def _insufficient_report(
    *,
    binding: dict[str, object],
    all_markets: Sequence[HistoricalMarket],
    accepted: Sequence[EligibleMarket],
    market_rejections: Counter[str],
    price_rejections: Counter[str],
    price_complete_dates: Sequence[date],
    reason: str,
) -> dict[str, object]:
    report: dict[str, object] = {
        "classification": "INSUFFICIENT_DATA",
        "reason": reason,
        "binding": binding,
        "inventory": {
            "historical_markets": len(all_markets),
            "eligible_markets": len(accepted),
            "dates_with_both_price_leads": len(price_complete_dates),
            "market_rejections": dict(sorted(market_rejections.items())),
            "price_rejections": dict(sorted(price_rejections.items())),
        },
    }
    report["local_report"] = str(_write_report(report))
    print("WEATHER_EDGE_REPORT=" + compact_json(report))
    return report


def _resource_report(
    *,
    binding: dict[str, object],
    all_markets: Sequence[HistoricalMarket],
    accepted: Sequence[EligibleMarket],
    market_rejections: Counter[str],
    price_rejections: Counter[str],
    chosen: Sequence[date],
    estimated_requests: int,
    estimated_bytes: int,
    split: Split | None,
    extraction: dict[str, object] | None,
    requests: int,
    downloaded_bytes: int,
) -> dict[str, object]:
    report: dict[str, object] = {
        "classification": "RESOURCE_BOUND_BLOCKER",
        "reason": (
            "the exact hourly TMP reconstruction cannot fit the minimum chronological dataset "
            "within the configured byte/request limits"
        ),
        "binding": binding,
        "inventory": {
            "historical_markets": len(all_markets),
            "eligible_markets": len(accepted),
            "market_rejections": dict(sorted(market_rejections.items())),
            "price_rejections": dict(sorted(price_rejections.items())),
        },
        "plan": {
            "selected_event_dates": len(chosen),
            "estimated_noaa_requests": estimated_requests,
            "estimated_noaa_bytes": estimated_bytes,
            "split": split,
            "sampling_rule": "deterministic-evenly-spaced-chronological-event-dates-v1",
        },
        "extraction": extraction,
        "resources": {"requests": requests, "downloaded_bytes": downloaded_bytes},
    }
    report["local_report"] = str(_write_report(report))
    print("WEATHER_EDGE_REPORT=" + compact_json(report))
    return report


def _compact_report(report: dict[str, object]) -> dict[str, object]:
    compact = {
        key: value
        for key, value in report.items()
        if key not in {"non_executable_historical_diagnostic"}
    }
    diagnostic = report.get("non_executable_historical_diagnostic")
    if isinstance(diagnostic, dict):
        compact["non_executable_historical_diagnostic"] = {
            key: value for key, value in diagnostic.items() if key != "rows"
        }
    return compact


def run_fixture() -> dict[str, object]:
    base = date(2025, 3, 1)
    rows: list[ResearchRow] = []
    for offset in range(60):
        for lead in (48, 24):
            margin = Decimal((offset % 9) - 4)
            outcome = int((margin > 0 and offset % 5 != 0) or (margin == 0 and offset % 2 == 0))
            rows.append(
                ResearchRow(
                    contract_date=base + timedelta(days=offset),
                    ticker=f"KXHIGHLAX-{offset}-{lead}",
                    event_ticker=f"KXHIGHLAX-{offset}",
                    series_ticker="KXHIGHLAX",
                    lead_hours=lead,
                    threshold_kind="GREATER_THAN",
                    threshold=Decimal("70"),
                    result_yes=outcome,
                    forecast_daily_maximum=Decimal("70") + margin,
                    signed_margin=margin,
                    market_probability=Decimal("0.50"),
                    candle_end_time=datetime(2025, 2, 28, tzinfo=UTC),
                    forecast_initialization=datetime(2025, 2, 28, tzinfo=UTC),
                    forecast_source_hash="forecast",
                    market_source_hash="market",
                    rule_sha256="rule",
                    binding_hash="binding",
                )
            )
    split = chronological_split([row.contract_date for row in rows])
    assert set(split.training_dates).isdisjoint(split.testing_dates)
    training = [row for row in rows if row.contract_date in split.training_dates]
    testing = [row for row in rows if row.contract_date in split.testing_dates]
    b, c, selected, rule = select_model(training)
    probabilities = [model_probability(selected, row) for row in testing]
    metric = evaluate(testing, probabilities, total_rows=len(testing))
    bootstrap = block_bootstrap(
        testing, probabilities, [row.market_probability for row in testing], repetitions=10
    )
    diagnostics = historical_diagnostic(testing, probabilities)
    assert b.converged and c.converged and selected.name == "MODEL_C"
    assert metric.observations == 36
    assert diagnostics
    assert all(row.candle_end_time <= datetime(2025, 3, 1, tzinfo=UTC) for row in rows)
    assert all(row.forecast_initialization <= datetime(2025, 3, 1, tzinfo=UTC) for row in rows)
    fixture_result: dict[str, object] = {
        "fixture": "GFS_WEATHER_EDGE_FIXTURE_PASSED",
        "split": split,
        "selected": selected.name,
        "selection_rule": rule,
        "metric": metric,
        "bootstrap": bootstrap,
        "diagnostics": len(diagnostics),
    }
    print(compact_json(fixture_result))
    return fixture_result


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(description="bounded NOAA GFS weather-edge research")
    subparsers = command_parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("fixture")
    real = subparsers.add_parser("real")
    real.add_argument("--series", required=True)
    real.add_argument("--start-date", type=date.fromisoformat, required=True)
    real.add_argument("--end-date", type=date.fromisoformat, required=True)
    real.add_argument("--lead-hours", type=parse_leads, required=True)
    real.add_argument("--max-event-dates", type=int, required=True)
    real.add_argument("--bootstrap-repetitions", type=int, required=True)
    real.add_argument("--max-noaa-requests", type=int, required=True)
    real.add_argument("--max-download-mb", type=int, required=True)
    real.add_argument("--timeout-seconds", type=int, default=10)
    return command_parser


def main() -> None:
    arguments = parser().parse_args()
    if arguments.command == "fixture":
        run_fixture()
        return
    run_real(
        series=arguments.series,
        start_date=arguments.start_date,
        end_date=arguments.end_date,
        leads=arguments.lead_hours,
        max_event_dates=arguments.max_event_dates,
        bootstrap_repetitions=arguments.bootstrap_repetitions,
        max_noaa_requests=arguments.max_noaa_requests,
        max_download_mb=arguments.max_download_mb,
        timeout_seconds=arguments.timeout_seconds,
    )


if __name__ == "__main__":
    main()
