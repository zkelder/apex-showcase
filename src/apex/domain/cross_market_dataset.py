"""Cross-market historical dataset snapshot (milestone section 10).

Binds -- never merges into one forced calendar -- three genuinely distinct
evidence sources this milestone assembled:

* the genuine equity/ETF cohort's ``HistoricalDatasetSnapshot``
  (``domain.historical_market_data``, migration ``0077``);
* existing genuine BTC-USD historical evidence
  (``domain.crypto_research.CryptoHistoricalCapture``, reused unmodified --
  no new crypto capture is performed here);
* the genuine historical Total Market Context series, which is not a
  separate capture at all: ``adapters.historical_total_market_context_
  sources.HistoricalTotalMarketContextSource`` already derives it directly
  from the *same* equity dataset snapshot's SPY/TLT/USO bars (migration
  ``a85d712``'s own section 7 work) whenever those three symbols are in
  the cohort -- true here, so this binding records that dependency
  explicitly rather than re-deriving or duplicating it.

This is new, genuinely-immutable evidence with no existing table shaped to
hold it (a cohort policy id, several independent coverage windows, and an
explicit alignment policy reference), hence migration ``0080``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from apex.domain.values import content_digest, deterministic_id

CROSS_MARKET_DATASET_V1 = "CROSS_MARKET_DATASET_V1"
TMC_DERIVATION_NOTE = (
    "TOTAL_MARKET_CONTEXT_IS_DERIVED_FROM_THE_EQUITY_SNAPSHOTS_OWN_SPY_TLT_USO_BARS_"
    "NOT_A_SEPARATE_CAPTURE"
)
TMC_WARMUP_NOTE = (
    "TOTAL_MARKET_CONTEXT_REQUIRES_A_42_SESSION_SPY_TRAILING_WARMUP_BEFORE_ITS_FIRST_"
    "VOLATILITY_COMPONENT_IS_COMPUTABLE"
)


class CrossMarketDatasetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CrossMarketDataset:
    cross_market_dataset_id: str
    equity_dataset_snapshot_id: str
    equity_dataset_snapshot_hash: str
    equity_coverage_start: str
    equity_coverage_end: str
    cohort_policy_id: str
    cohort_policy_hash: str
    quality_gate_report_id: str
    quality_gate_report_hash: str
    crypto_provider: str
    crypto_product_id: str
    crypto_capture_ids: tuple[str, ...]
    crypto_coverage_start: str
    crypto_coverage_end: str
    tmc_component_symbols: tuple[str, ...]
    tmc_source_identity: str
    common_overlap_start: str | None
    common_overlap_end: str | None
    adjustment_policy_id: str
    missing_data_policy_id: str
    alignment_policy_id: str
    point_in_time_limitations: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        equity_dataset_snapshot_id: str,
        equity_dataset_snapshot_hash: str,
        equity_coverage_start: str,
        equity_coverage_end: str,
        cohort_policy_id: str,
        cohort_policy_hash: str,
        quality_gate_report_id: str,
        quality_gate_report_hash: str,
        crypto_provider: str,
        crypto_product_id: str,
        crypto_capture_ids: tuple[str, ...],
        crypto_coverage_start: str,
        crypto_coverage_end: str,
        tmc_component_symbols: tuple[str, ...],
        tmc_source_identity: str,
        adjustment_policy_id: str,
        missing_data_policy_id: str,
        alignment_policy_id: str,
        point_in_time_limitations: tuple[str, ...],
        created_at: datetime,
    ) -> CrossMarketDataset:
        if created_at.tzinfo is None:
            raise CrossMarketDatasetError("CROSS_MARKET_DATASET_CREATED_AT_MUST_BE_AWARE")
        if not crypto_capture_ids:
            raise CrossMarketDatasetError(
                "CROSS_MARKET_DATASET_REQUIRES_AT_LEAST_ONE_CRYPTO_CAPTURE"
            )
        for label, value in (
            ("equity_coverage_start", equity_coverage_start),
            ("equity_coverage_end", equity_coverage_end),
            ("crypto_coverage_start", crypto_coverage_start),
            ("crypto_coverage_end", crypto_coverage_end),
        ):
            date.fromisoformat(value)  # raises ValueError if malformed; label kept for debugging
            del label
        overlap_start = max(equity_coverage_start, crypto_coverage_start)
        overlap_end = min(equity_coverage_end, crypto_coverage_end)
        common_start = overlap_start if overlap_start <= overlap_end else None
        common_end = overlap_end if overlap_start <= overlap_end else None
        limitations = tuple(sorted(set(point_in_time_limitations)))
        capture_ids = tuple(sorted(set(crypto_capture_ids)))
        tmc_symbols = tuple(sorted(set(tmc_component_symbols)))
        payload = (
            CROSS_MARKET_DATASET_V1,
            equity_dataset_snapshot_id,
            equity_dataset_snapshot_hash,
            equity_coverage_start,
            equity_coverage_end,
            cohort_policy_id,
            cohort_policy_hash,
            quality_gate_report_id,
            quality_gate_report_hash,
            crypto_provider,
            crypto_product_id,
            capture_ids,
            crypto_coverage_start,
            crypto_coverage_end,
            tmc_symbols,
            tmc_source_identity,
            common_start,
            common_end,
            adjustment_policy_id,
            missing_data_policy_id,
            alignment_policy_id,
            limitations,
        )
        return cls(
            cross_market_dataset_id=deterministic_id("cross_market_dataset", payload),
            equity_dataset_snapshot_id=equity_dataset_snapshot_id,
            equity_dataset_snapshot_hash=equity_dataset_snapshot_hash,
            equity_coverage_start=equity_coverage_start,
            equity_coverage_end=equity_coverage_end,
            cohort_policy_id=cohort_policy_id,
            cohort_policy_hash=cohort_policy_hash,
            quality_gate_report_id=quality_gate_report_id,
            quality_gate_report_hash=quality_gate_report_hash,
            crypto_provider=crypto_provider,
            crypto_product_id=crypto_product_id,
            crypto_capture_ids=capture_ids,
            crypto_coverage_start=crypto_coverage_start,
            crypto_coverage_end=crypto_coverage_end,
            tmc_component_symbols=tmc_symbols,
            tmc_source_identity=tmc_source_identity,
            common_overlap_start=common_start,
            common_overlap_end=common_end,
            adjustment_policy_id=adjustment_policy_id,
            missing_data_policy_id=missing_data_policy_id,
            alignment_policy_id=alignment_policy_id,
            point_in_time_limitations=limitations,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )
