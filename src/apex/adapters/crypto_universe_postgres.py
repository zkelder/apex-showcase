"""PostgreSQL persistence for immutable multi-asset crypto research evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_campaign_generic_postgres import (
    PostgresGenericCryptoCampaignRepository,
)
from apex.adapters.crypto_research_postgres import _data, _payload
from apex.application.crypto_research import CryptoResearchConflict
from apex.domain.crypto_universe import (
    CatalogPaginationStatus,
    CryptoAssetGroupDecision,
    CryptoCalibrationAssetCoverage,
    CryptoCalibrationBucket,
    CryptoCalibrationDecisionOutcome,
    CryptoCalibrationHorizon,
    CryptoDailyObservation,
    CryptoDiscoveryResult,
    CryptoDiscoveryRun,
    CryptoHistoryPartition,
    CryptoProductCapture,
    CryptoProductObservation,
    CryptoProductPageEvidence,
    CryptoRelativeStrengthCalibration,
    CryptoResearchOpportunity,
    CryptoRouteSelectionDecision,
    CryptoUniverseDecision,
    CryptoUniverseSnapshot,
    OpportunityStatus,
    ProductEligibility,
    PublicCryptoProduct,
)
from apex.domain.evidence_recovery import RECOVERY_CAPTURE_ID_PREFIX
from apex.domain.research_evidence import EvidenceClass

metadata = sa.MetaData()

product_capture_table = sa.Table(
    "crypto_public_product_capture",
    metadata,
    sa.Column("capture_id", sa.String(160), primary_key=True),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
product_table = sa.Table(
    "crypto_public_product",
    metadata,
    sa.Column("capture_id", sa.String(160), primary_key=True),
    sa.Column("product_id", sa.String(160), primary_key=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
observation_table = sa.Table(
    "crypto_product_observation",
    metadata,
    sa.Column("observation_id", sa.String(160), primary_key=True),
    sa.Column("product_id", sa.String(160), nullable=False),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("checkpoint_ordinal", sa.Integer(), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
universe_table = sa.Table(
    "crypto_universe_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(160), primary_key=True),
    sa.Column("source_capture_id", sa.String(160), nullable=False),
    sa.Column("policy_id", sa.String(80), nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
discovery_table = sa.Table(
    "crypto_discovery_run",
    metadata,
    sa.Column("run_id", sa.String(160), primary_key=True),
    sa.Column("universe_snapshot_id", sa.String(160), nullable=False),
    sa.Column("method_id", sa.String(80), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
opportunity_table = sa.Table(
    "crypto_research_opportunity",
    metadata,
    sa.Column("opportunity_id", sa.String(160), primary_key=True),
    sa.Column("discovery_run_id", sa.String(160), nullable=False),
    sa.Column("product_id", sa.String(160), nullable=False),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_UNIVERSE_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _optional_int(value: object) -> int | None:
    return None if value is None else int(str(value))


def _product(row: RowMapping) -> PublicCryptoProduct:
    value = _data(row)
    return PublicCryptoProduct(
        product_id=str(value["product_id"]),
        provider=str(value["provider"]),
        base_asset=str(value["base_asset"]),
        quote_asset=str(value["quote_asset"]),
        market_type=str(value["market_type"]),
        trading_status=str(value["trading_status"]),
        product_status=str(value["product_status"]),
        price_increment=Decimal(str(value["price_increment"])),
        quantity_increment=Decimal(str(value["quantity_increment"])),
        minimum_quantity=(
            None if value["minimum_quantity"] is None else Decimal(str(value["minimum_quantity"]))
        ),
        minimum_notional=(
            None if value["minimum_notional"] is None else Decimal(str(value["minimum_notional"]))
        ),
        public_volume_24h=(
            None if value["public_volume_24h"] is None else Decimal(str(value["public_volume_24h"]))
        ),
        captured_at=_time(value["captured_at"]),
        source_contract_version=str(value["source_contract_version"]),
        raw_evidence_hash=str(value["raw_evidence_hash"]),
        parser_version=str(value["parser_version"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        canonical_hash=str(value["canonical_hash"]),
        canonical_asset_id=str(value.get("canonical_asset_id", value["base_asset"])),
        provider_alias=(
            None if value.get("provider_alias") in (None, "") else str(value["provider_alias"])
        ),
        provider_alias_to=_strings(value.get("provider_alias_to", [])),
        restriction_reasons=_strings(value.get("restriction_reasons", [])),
        authority="NONE",
    )


def _page(value: dict[str, Any]) -> CryptoProductPageEvidence:
    return CryptoProductPageEvidence(
        page_ordinal=int(value["page_ordinal"]),
        request_cursor=(
            None if value.get("request_cursor") is None else str(value["request_cursor"])
        ),
        next_cursor=None if value.get("next_cursor") is None else str(value["next_cursor"]),
        has_next=bool(value["has_next"]),
        product_ids=_strings(value["product_ids"]),
        product_count=int(value["product_count"]),
        raw_evidence_hash=str(value["raw_evidence_hash"]),
        raw_bytes=int(value["raw_bytes"]),
        completed_at=_time(value["completed_at"]),
        source_locator=str(value["source_locator"]),
        runtime_milliseconds=int(value["runtime_milliseconds"]),
        completion_status=str(value["completion_status"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _capture(row: RowMapping) -> CryptoProductCapture:
    value = _data(row)
    raw_pages = value.get("pages", [])
    if not isinstance(raw_pages, list):
        raise CryptoResearchConflict("CRYPTO_PRODUCT_PAGES_INVALID")
    return CryptoProductCapture(
        capture_id=str(value["capture_id"]),
        provider=str(value["provider"]),
        captured_at=_time(value["captured_at"]),
        source_contract_version=str(value["source_contract_version"]),
        parser_version=str(value["parser_version"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        raw_evidence_hash=str(value["raw_evidence_hash"]),
        product_ids=_strings(value["product_ids"]),
        request_count=int(value["request_count"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        pages=tuple(_page(item) for item in raw_pages),
        pagination_status=CatalogPaginationStatus(
            value.get("pagination_status", "LEGACY_DIAGNOSTIC")
        ),
        continuation_cursor=(
            None if value.get("continuation_cursor") is None else str(value["continuation_cursor"])
        ),
        total_raw_bytes=int(value.get("total_raw_bytes", 0)),
        runtime_milliseconds=int(value.get("runtime_milliseconds", 0)),
        authority="NONE",
    )


def _observation(row: RowMapping) -> CryptoProductObservation:
    value = _data(row)
    raw_candles = value["candles"]
    if not isinstance(raw_candles, list):
        raise CryptoResearchConflict("CRYPTO_OBSERVATION_CANDLES_INVALID")
    candles = tuple(
        CryptoDailyObservation(
            bucket_start=_time(item["bucket_start"]),
            close=Decimal(str(item["close"])),
            volume=Decimal(str(item["volume"])),
        )
        for item in raw_candles
    )
    product_id = str(value["product_id"])
    source_capture_id = value.get("source_capture_id")
    if source_capture_id is None:
        suffix = f"-{product_id.lower()}-daily-observation-v1"
        observation_identity = str(value["observation_id"])
        if not observation_identity.endswith(suffix):
            raise CryptoResearchConflict("CRYPTO_OBSERVATION_CAPTURE_LINEAGE_INVALID")
        source_capture_id = observation_identity.removesuffix(suffix)
    raw_partitions = value.get("coverage_partitions", [])
    if not isinstance(raw_partitions, list):
        raise CryptoResearchConflict("CRYPTO_HISTORY_PARTITIONS_INVALID")
    coverage_partitions = tuple(
        CryptoHistoryPartition(
            ordinal=int(item["ordinal"]),
            interval_start=_time(item["interval_start"]),
            interval_end=_time(item["interval_end"]),
            accepted_buckets=int(item["accepted_buckets"]),
            missing_buckets=int(item["missing_buckets"]),
            duplicate_records=int(item["duplicate_records"]),
            rejected_records=int(item["rejected_records"]),
            raw_evidence_hash=str(item["raw_evidence_hash"]),
            source_locator=str(item["source_locator"]),
            runtime_milliseconds=int(item["runtime_milliseconds"]),
        )
        for item in raw_partitions
    )
    return CryptoProductObservation(
        observation_id=str(value["observation_id"]),
        source_capture_id=str(source_capture_id),
        product_id=product_id,
        interval_start=_time(value["interval_start"]),
        interval_end=_time(value["interval_end"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        candles=candles,
        missing_buckets=int(value["missing_buckets"]),
        duplicate_records=int(value["duplicate_records"]),
        rejected_records=int(value["rejected_records"]),
        request_count=int(value["request_count"]),
        raw_evidence_hashes=_strings(value["raw_evidence_hashes"]),
        source_contract_version=str(value["source_contract_version"]),
        parser_version=str(value["parser_version"]),
        checkpoint_ordinal=int(value["checkpoint_ordinal"]),
        completed_at=_time(value["completed_at"]),
        canonical_hash=str(value["canonical_hash"]),
        canonical_asset_id=str(value.get("canonical_asset_id", product_id.rsplit("-", 1)[0])),
        authority="NONE",
        coverage_partitions=coverage_partitions,
        coverage_status=str(value.get("coverage_status", "LEGACY_WINDOW")),
        coverage_limitations=_strings(value.get("coverage_limitations", [])),
    )


def _decision(value: dict[str, Any]) -> CryptoUniverseDecision:
    return CryptoUniverseDecision(
        product_id=str(value["product_id"]),
        status=ProductEligibility(value["status"]),
        reasons=_strings(value["reasons"]),
        history_buckets=int(value["history_buckets"]),
        missing_buckets=int(value["missing_buckets"]),
        mean_daily_quote_volume=Decimal(str(value["mean_daily_quote_volume"])),
        rank=None if value["rank"] is None else int(value["rank"]),
        canonical_hash=str(value["canonical_hash"]),
        canonical_asset_id=str(
            value.get("canonical_asset_id", str(value["product_id"]).rsplit("-", 1)[0])
        ),
        representative_product_id=(
            None
            if value.get("representative_product_id") is None
            else str(value["representative_product_id"])
        ),
    )


def _route(value: dict[str, Any]) -> CryptoRouteSelectionDecision:
    return CryptoRouteSelectionDecision(
        product_id=str(value["product_id"]),
        selected=bool(value["selected"]),
        reasons=_strings(value["reasons"]),
        public_quote_volume_24h=Decimal(str(value["public_quote_volume_24h"])),
        history_quality=str(value["history_quality"]),
        supporting_observation_id=(
            None
            if value.get("supporting_observation_id") is None
            else str(value["supporting_observation_id"])
        ),
        provider_alias=(
            None if value.get("provider_alias") is None else str(value["provider_alias"])
        ),
        provider_alias_to=_strings(value.get("provider_alias_to", [])),
        canonical_hash=str(value["canonical_hash"]),
    )


def _group(value: dict[str, Any]) -> CryptoAssetGroupDecision:
    raw_routes = value["route_decisions"]
    if not isinstance(raw_routes, list):
        raise CryptoResearchConflict("CRYPTO_ASSET_GROUP_ROUTES_INVALID")
    return CryptoAssetGroupDecision(
        canonical_asset_id=str(value["canonical_asset_id"]),
        product_route_ids=_strings(value["product_route_ids"]),
        selected_product_id=(
            None if value.get("selected_product_id") is None else str(value["selected_product_id"])
        ),
        route_decisions=tuple(_route(item) for item in raw_routes),
        history_shortlist_rank=(
            None
            if value.get("history_shortlist_rank") is None
            else int(value["history_shortlist_rank"])
        ),
        history_shortlist_reasons=_strings(value["history_shortlist_reasons"]),
        provider_declared_aliases=_strings(value["provider_declared_aliases"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _universe(row: RowMapping) -> CryptoUniverseSnapshot:
    value = _data(row)
    raw_decisions = value["decisions"]
    if not isinstance(raw_decisions, list):
        raise CryptoResearchConflict("CRYPTO_UNIVERSE_DECISIONS_INVALID")
    raw_groups = value.get("asset_groups", [])
    if not isinstance(raw_groups, list):
        raise CryptoResearchConflict("CRYPTO_UNIVERSE_ASSET_GROUPS_INVALID")
    raw_selected_routes = value.get("selected_routes", [])
    if not isinstance(raw_selected_routes, list):
        raise CryptoResearchConflict("CRYPTO_UNIVERSE_SELECTED_ROUTES_INVALID")
    return CryptoUniverseSnapshot(
        snapshot_id=str(value["snapshot_id"]),
        source_capture_id=str(value["source_capture_id"]),
        policy_id=str(value["policy_id"]),
        policy_version=int(value["policy_version"]),
        considered_product_ids=_strings(value["considered_product_ids"]),
        decisions=tuple(_decision(item) for item in raw_decisions),
        eligible_product_ids=_strings(value["eligible_product_ids"]),
        excluded_product_ids=_strings(value["excluded_product_ids"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        evidence_limitations=_strings(value["evidence_limitations"]),
        manifest_hash=str(value["manifest_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        catalog_pagination_status=CatalogPaginationStatus(
            value.get("catalog_pagination_status", "LEGACY_DIAGNOSTIC")
        ),
        asset_groups=tuple(_group(item) for item in raw_groups),
        history_shortlist_asset_ids=_strings(value.get("history_shortlist_asset_ids", [])),
        eligible_asset_ids=_strings(value.get("eligible_asset_ids", [])),
        selected_routes=tuple(
            (str(item[0]), str(item[1]))
            for item in raw_selected_routes
            if isinstance(item, list) and len(item) == 2
        ),
        authority="NONE",
    )


def _result(value: dict[str, Any]) -> CryptoDiscoveryResult:
    return CryptoDiscoveryResult(
        product_id=str(value["product_id"]),
        rank=int(value["rank"]),
        trailing_return=Decimal(str(value["trailing_return"])),
        realized_volatility=Decimal(str(value["realized_volatility"])),
        risk_adjusted_return=Decimal(str(value["risk_adjusted_return"])),
        mean_daily_quote_volume=Decimal(str(value["mean_daily_quote_volume"])),
        estimated_round_trip_cost=Decimal(str(value["estimated_round_trip_cost"])),
        liquidity_penalty=Decimal(str(value["liquidity_penalty"])),
        gross_research_score=Decimal(str(value["gross_research_score"])),
        supporting_observation_id=str(value["supporting_observation_id"]),
        canonical_hash=str(value["canonical_hash"]),
        canonical_asset_id=str(
            value.get("canonical_asset_id", str(value["product_id"]).rsplit("-", 1)[0])
        ),
        alternate_product_route_ids=_strings(value.get("alternate_product_route_ids", [])),
        trailing_return_30d=_optional_decimal(value.get("trailing_return_30d")),
        trailing_return_90d=_optional_decimal(value.get("trailing_return_90d")),
        universe_median_90d=_optional_decimal(value.get("universe_median_90d")),
        relative_strength=_optional_decimal(value.get("relative_strength")),
        reversal_penalty=_optional_decimal(value.get("reversal_penalty")),
        volatility_penalty=_optional_decimal(value.get("volatility_penalty")),
        spread_penalty=_optional_decimal(value.get("spread_penalty")),
        depth_penalty=_optional_decimal(value.get("depth_penalty")),
        calibration_bucket=(
            None if value.get("calibration_bucket") is None else str(value["calibration_bucket"])
        ),
        expected_gross_return=_optional_decimal(value.get("expected_gross_return")),
        expected_net_return=_optional_decimal(value.get("expected_net_return")),
        uncertainty_lower=_optional_decimal(value.get("uncertainty_lower")),
        uncertainty_upper=_optional_decimal(value.get("uncertainty_upper")),
        calibration_sample_count=_optional_int(value.get("calibration_sample_count")),
        source_eligible=bool(value.get("source_eligible", False)),
        source_rejection_reasons=_strings(value.get("source_rejection_reasons", [])),
        current_spread_bps=_optional_decimal(value.get("current_spread_bps")),
        visible_ask_depth=_optional_decimal(value.get("visible_ask_depth")),
        required_capital=_optional_decimal(value.get("required_capital")),
        maximum_loss=_optional_decimal(value.get("maximum_loss")),
        maximum_sensible_allocation=_optional_decimal(value.get("maximum_sensible_allocation")),
        supporting_checkpoint_id=(
            None
            if value.get("supporting_checkpoint_id") is None
            else str(value["supporting_checkpoint_id"])
        ),
        trailing_return_7d=_optional_decimal(value.get("trailing_return_7d")),
        downside_probability=_optional_decimal(value.get("downside_probability")),
        downside_p05=_optional_decimal(value.get("downside_p05")),
        maximum_observed_loss=_optional_decimal(value.get("maximum_observed_loss")),
        calibration_horizons=tuple(
            _calibration_horizon(item) for item in value.get("calibration_horizons", [])
        ),
    )


def _calibration_horizon(value: dict[str, Any]) -> CryptoCalibrationHorizon:
    return CryptoCalibrationHorizon(
        outcome_days=int(value["outcome_days"]),
        sample_count=int(value["sample_count"]),
        expected_gross_return=_optional_decimal(value.get("expected_gross_return")),
        expected_net_return=_optional_decimal(value.get("expected_net_return")),
        uncertainty_lower=_optional_decimal(value.get("uncertainty_lower")),
        uncertainty_upper=_optional_decimal(value.get("uncertainty_upper")),
        downside_probability=_optional_decimal(value.get("downside_probability")),
        downside_p05=_optional_decimal(value.get("downside_p05")),
        maximum_observed_loss=_optional_decimal(value.get("maximum_observed_loss")),
        canonical_hash=str(value["canonical_hash"]),
    )


def _calibration_bucket(value: dict[str, Any]) -> CryptoCalibrationBucket:
    return CryptoCalibrationBucket(
        bucket=str(value["bucket"]),
        sample_count=int(value["sample_count"]),
        expected_gross_return=_optional_decimal(value.get("expected_gross_return")),
        uncertainty_lower=_optional_decimal(value.get("uncertainty_lower")),
        uncertainty_upper=_optional_decimal(value.get("uncertainty_upper")),
        canonical_hash=str(value["canonical_hash"]),
        modeled_round_trip_cost=Decimal(str(value.get("modeled_round_trip_cost", "0.014"))),
        expected_net_return=_optional_decimal(value.get("expected_net_return")),
        net_uncertainty_lower=_optional_decimal(value.get("net_uncertainty_lower")),
        net_uncertainty_upper=_optional_decimal(value.get("net_uncertainty_upper")),
        horizon_results=tuple(
            _calibration_horizon(item) for item in value.get("horizon_results", [])
        ),
    )


def _calibration_coverage(value: dict[str, Any]) -> CryptoCalibrationAssetCoverage:
    return CryptoCalibrationAssetCoverage(
        canonical_asset_id=str(value["canonical_asset_id"]),
        product_id=str(value["product_id"]),
        observation_id=str(value["observation_id"]),
        requested_start=_time(value["requested_start"]),
        requested_end=_time(value["requested_end"]),
        accepted_buckets=int(value["accepted_buckets"]),
        missing_buckets=int(value["missing_buckets"]),
        duplicate_records=int(value["duplicate_records"]),
        rejected_records=int(value["rejected_records"]),
        request_count=int(value["request_count"]),
        coverage_status=str(value["coverage_status"]),
        limitations=_strings(value["limitations"]),
        raw_evidence_hashes=_strings(value["raw_evidence_hashes"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _calibration_decision(value: dict[str, Any]) -> CryptoCalibrationDecisionOutcome:
    return CryptoCalibrationDecisionOutcome(
        product_id=str(value["product_id"]),
        decision_at=_time(value["decision_at"]),
        outcome_at=_time(value["outcome_at"]),
        rank=int(value["rank"]),
        bucket=str(value["bucket"]),
        trailing_return_30d=Decimal(str(value["trailing_return_30d"])),
        trailing_return_90d=Decimal(str(value["trailing_return_90d"])),
        universe_median_90d=Decimal(str(value["universe_median_90d"])),
        relative_strength=Decimal(str(value["relative_strength"])),
        reversal_penalty=Decimal(str(value["reversal_penalty"])),
        volatility_penalty=Decimal(str(value["volatility_penalty"])),
        gross_research_score=Decimal(str(value["gross_research_score"])),
        forward_gross_return=Decimal(str(value["forward_gross_return"])),
        included_in_bucket=bool(value["included_in_bucket"]),
        exclusion_reasons=_strings(value["exclusion_reasons"]),
        canonical_hash=str(value["canonical_hash"]),
        trailing_return_7d=_optional_decimal(value.get("trailing_return_7d")),
        forward_gross_return_14d=_optional_decimal(value.get("forward_gross_return_14d")),
        forward_gross_return_30d=_optional_decimal(value.get("forward_gross_return_30d")),
    )


def _calibration(value: object) -> CryptoRelativeStrengthCalibration | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CryptoResearchConflict("CRYPTO_RELATIVE_STRENGTH_CALIBRATION_INVALID")
    raw_buckets = value.get("buckets")
    if not isinstance(raw_buckets, list):
        raise CryptoResearchConflict("CRYPTO_RELATIVE_STRENGTH_CALIBRATION_BUCKETS_INVALID")
    raw_coverage = value.get("asset_coverage", [])
    if not isinstance(raw_coverage, list):
        raise CryptoResearchConflict("CRYPTO_RELATIVE_STRENGTH_COVERAGE_INVALID")
    raw_decisions = value.get("decision_outcomes", [])
    if not isinstance(raw_decisions, list):
        raise CryptoResearchConflict("CRYPTO_RELATIVE_STRENGTH_DECISIONS_INVALID")
    return CryptoRelativeStrengthCalibration(
        calibration_id=str(value["calibration_id"]),
        method_id=str(value["method_id"]),
        version=int(value["version"]),
        dataset_start=_time(value["dataset_start"]),
        dataset_end=_time(value["dataset_end"]),
        decision_spacing_days=int(value["decision_spacing_days"]),
        lookback_days=int(value["lookback_days"]),
        outcome_days=int(value["outcome_days"]),
        observation_ids=_strings(value["observation_ids"]),
        buckets=tuple(_calibration_bucket(item) for item in raw_buckets),
        formula_hash=str(value["formula_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        asset_coverage=tuple(_calibration_coverage(item) for item in raw_coverage),
        usable_common_dates=tuple(_time(item) for item in value.get("usable_common_dates", [])),
        decision_outcomes=tuple(_calibration_decision(item) for item in raw_decisions),
        outcome_horizons=tuple(int(item) for item in value.get("outcome_horizons", [])),
    )


def _run(row: RowMapping) -> CryptoDiscoveryRun:
    value = _data(row)
    raw_results = value["results"]
    if not isinstance(raw_results, list):
        raise CryptoResearchConflict("CRYPTO_DISCOVERY_RESULTS_INVALID")
    return CryptoDiscoveryRun(
        run_id=str(value["run_id"]),
        universe_snapshot_id=str(value["universe_snapshot_id"]),
        method_id=str(value["method_id"]),
        method_version=int(value["method_version"]),
        generated_at=_time(value["generated_at"]),
        results=tuple(_result(item) for item in raw_results),
        warnings=_strings(value["warnings"]),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        source_pulse_id=(
            None if value.get("source_pulse_id") is None else str(value["source_pulse_id"])
        ),
        calibration=_calibration(value.get("calibration")),
        authority="NONE",
    )


def _opportunity(row: RowMapping) -> CryptoResearchOpportunity:
    value = _data(row)
    return CryptoResearchOpportunity(
        opportunity_id=str(value["opportunity_id"]),
        discovery_run_id=str(value["discovery_run_id"]),
        universe_snapshot_id=str(value["universe_snapshot_id"]),
        product_id=str(value["product_id"]),
        generated_at=_time(value["generated_at"]),
        valid_until=_time(value["valid_until"]),
        rank=int(value["rank"]),
        supporting_observation_id=str(value["supporting_observation_id"]),
        gross_research_score=Decimal(str(value["gross_research_score"])),
        volatility_estimate=Decimal(str(value["volatility_estimate"])),
        fee_bps_per_side=Decimal(str(value["fee_bps_per_side"])),
        slippage_bps_per_side=Decimal(str(value["slippage_bps_per_side"])),
        liquidity_limitations=_strings(value["liquidity_limitations"]),
        hypothetical_holding_days=int(value["hypothetical_holding_days"]),
        maximum_research_allocation=Decimal(str(value["maximum_research_allocation"])),
        status=OpportunityStatus(value["status"]),
        warnings=_strings(value["warnings"]),
        rejection_reasons=_strings(value["rejection_reasons"]),
        canonical_hash=str(value["canonical_hash"]),
        canonical_asset_id=str(
            value.get("canonical_asset_id", str(value["product_id"]).rsplit("-", 1)[0])
        ),
        alternate_product_route_ids=_strings(value.get("alternate_product_route_ids", [])),
        strategy_version=int(value.get("strategy_version", 1)),
        expected_gross_return=_optional_decimal(value.get("expected_gross_return")),
        expected_net_return=_optional_decimal(value.get("expected_net_return")),
        uncertainty_lower=_optional_decimal(value.get("uncertainty_lower")),
        uncertainty_upper=_optional_decimal(value.get("uncertainty_upper")),
        calibration_sample_count=_optional_int(value.get("calibration_sample_count")),
        required_capital=_optional_decimal(value.get("required_capital")),
        maximum_loss=_optional_decimal(value.get("maximum_loss")),
        maximum_sensible_allocation=_optional_decimal(value.get("maximum_sensible_allocation")),
        current_spread_bps=_optional_decimal(value.get("current_spread_bps")),
        visible_ask_depth=_optional_decimal(value.get("visible_ask_depth")),
        score_components=tuple(
            (str(item[0]), Decimal(str(item[1])))
            for item in value.get("score_components", [])
            if isinstance(item, list) and len(item) == 2
        ),
        calibration_id=(
            None if value.get("calibration_id") is None else str(value["calibration_id"])
        ),
        supporting_checkpoint_id=(
            None
            if value.get("supporting_checkpoint_id") is None
            else str(value["supporting_checkpoint_id"])
        ),
        research_only=bool(value.get("research_only", True)),
        paper_eligible=bool(value.get("paper_eligible", False)),
        live_eligible=bool(value.get("live_eligible", False)),
        capital_authority=str(value.get("capital_authority", "NONE")),
        broker_authority=str(value.get("broker_authority", "NONE")),
        financial_mutation_authority=str(value.get("financial_mutation_authority", "NONE")),
        method_id=str(value.get("method_id", "CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1")),
        trailing_return_7d=_optional_decimal(value.get("trailing_return_7d")),
        trailing_return_90d=_optional_decimal(value.get("trailing_return_90d")),
        realized_volatility_30d=_optional_decimal(value.get("realized_volatility_30d")),
        downside_probability=_optional_decimal(value.get("downside_probability")),
        downside_p05=_optional_decimal(value.get("downside_p05")),
        maximum_observed_loss=_optional_decimal(value.get("maximum_observed_loss")),
        calibration_horizons=tuple(
            _calibration_horizon(item) for item in value.get("calibration_horizons", [])
        ),
    )


class PostgresCryptoUniverseRepository(PostgresGenericCryptoCampaignRepository):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def product_capture(self, capture_id: str) -> CryptoProductCapture | None:
        row = self._one(
            sa.select(product_capture_table).where(product_capture_table.c.capture_id == capture_id)
        )
        return _capture(row) if row is not None else None

    def save_product_capture(
        self, capture: CryptoProductCapture, products: tuple[PublicCryptoProduct, ...]
    ) -> CryptoProductCapture:
        prior = self.product_capture(capture.capture_id)
        if prior is not None:
            if prior.canonical_hash != capture.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_PRODUCT_CAPTURE_IDENTITY_CONFLICT")
            return prior
        self._insert(
            product_capture_table,
            {
                "capture_id": capture.capture_id,
                "evidence_class": capture.evidence_class.value,
                "payload": _payload(capture),
                "canonical_hash": capture.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_PRODUCT_CAPTURE_IDENTITY_CONFLICT",
        )
        for product in products:
            self._insert(
                product_table,
                {
                    "capture_id": capture.capture_id,
                    "product_id": product.product_id,
                    "payload": _payload(product),
                    "canonical_hash": product.canonical_hash,
                    "authority": "NONE",
                },
                "CRYPTO_PRODUCT_IDENTITY_CONFLICT",
            )
        return capture

    def product_captures(self) -> tuple[CryptoProductCapture, ...]:
        return tuple(
            _capture(row)
            for row in self._rows(
                sa.select(product_capture_table).order_by(product_capture_table.c.capture_id)
            )
        )

    def products(self, capture_id: str) -> tuple[PublicCryptoProduct, ...]:
        return tuple(
            _product(row)
            for row in self._rows(
                sa.select(product_table)
                .where(product_table.c.capture_id == capture_id)
                .order_by(product_table.c.product_id)
            )
        )

    def observation(self, observation_id: str) -> CryptoProductObservation | None:
        row = self._one(
            sa.select(observation_table).where(observation_table.c.observation_id == observation_id)
        )
        return _observation(row) if row is not None else None

    def save_observation(self, observation: CryptoProductObservation) -> CryptoProductObservation:
        prior = self.observation(observation.observation_id)
        if prior is not None:
            if prior.canonical_hash != observation.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_OBSERVATION_IDENTITY_CONFLICT")
            return prior
        self._insert(
            observation_table,
            {
                "observation_id": observation.observation_id,
                "product_id": observation.product_id,
                "evidence_class": observation.evidence_class.value,
                "checkpoint_ordinal": observation.checkpoint_ordinal,
                "payload": _payload(observation),
                "canonical_hash": observation.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_OBSERVATION_IDENTITY_CONFLICT",
        )
        return observation

    def observations(
        self, *, include_recovered: bool = False
    ) -> tuple[CryptoProductObservation, ...]:
        """Every retained observation. Evidence acquired by Historical Evidence
        Gap Recovery (capture id prefixed ``evidence-recovery-``) is hidden by
        default so no existing consumer (follow-ups, PAPER, pulse reuse) can
        treat late-recovered candles as contemporaneous; only the recovery
        coverage reader opts in."""

        values = tuple(
            _observation(row)
            for row in self._rows(
                sa.select(observation_table).order_by(
                    observation_table.c.checkpoint_ordinal,
                    observation_table.c.observation_id,
                )
            )
        )
        if include_recovered:
            return values
        return tuple(
            item
            for item in values
            if not item.source_capture_id.startswith(RECOVERY_CAPTURE_ID_PREFIX)
        )

    def universe_snapshot(self, snapshot_id: str) -> CryptoUniverseSnapshot | None:
        row = self._one(
            sa.select(universe_table).where(universe_table.c.snapshot_id == snapshot_id)
        )
        return _universe(row) if row is not None else None

    def save_universe_snapshot(self, snapshot: CryptoUniverseSnapshot) -> CryptoUniverseSnapshot:
        prior = self.universe_snapshot(snapshot.snapshot_id)
        if prior is not None:
            if prior.canonical_hash != snapshot.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_UNIVERSE_SNAPSHOT_IDENTITY_CONFLICT")
            return prior
        self._insert(
            universe_table,
            {
                "snapshot_id": snapshot.snapshot_id,
                "source_capture_id": snapshot.source_capture_id,
                "policy_id": snapshot.policy_id,
                "manifest_hash": snapshot.manifest_hash,
                "payload": _payload(snapshot),
                "canonical_hash": snapshot.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_UNIVERSE_SNAPSHOT_IDENTITY_CONFLICT",
        )
        return snapshot

    def universe_snapshots(self) -> tuple[CryptoUniverseSnapshot, ...]:
        # Same non-chronological-identity pitfall as
        # ``PostgresCryptoMarketPulseRepository.pulse_snapshots`` (see its
        # comment): ``snapshot_id`` is a content-derived string, and callers
        # such as ``crypto_universe_workspace`` and
        # ``PostgresOperatingStateSource.snapshot`` take ``snapshots[-1]`` to
        # mean "most recently created universe snapshot", so the retained rows
        # must be reordered by genuine ``created_at`` evidence after decoding.
        decoded = tuple(
            _universe(row)
            for row in self._rows(sa.select(universe_table).order_by(universe_table.c.snapshot_id))
        )
        return tuple(sorted(decoded, key=lambda item: (item.created_at, item.snapshot_id)))

    def discovery_run(self, run_id: str) -> CryptoDiscoveryRun | None:
        row = self._one(sa.select(discovery_table).where(discovery_table.c.run_id == run_id))
        return _run(row) if row is not None else None

    def save_discovery_run(self, run: CryptoDiscoveryRun) -> CryptoDiscoveryRun:
        prior = self.discovery_run(run.run_id)
        if prior is not None:
            if prior.canonical_hash != run.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_DISCOVERY_RUN_IDENTITY_CONFLICT")
            return prior
        self._insert(
            discovery_table,
            {
                "run_id": run.run_id,
                "universe_snapshot_id": run.universe_snapshot_id,
                "method_id": run.method_id,
                "result_hash": run.result_hash,
                "payload": _payload(run),
                "canonical_hash": run.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_DISCOVERY_RUN_IDENTITY_CONFLICT",
        )
        return run

    def discovery_runs(self) -> tuple[CryptoDiscoveryRun, ...]:
        return tuple(
            _run(row)
            for row in self._rows(sa.select(discovery_table).order_by(discovery_table.c.run_id))
        )

    def opportunity(self, opportunity_id: str) -> CryptoResearchOpportunity | None:
        row = self._one(
            sa.select(opportunity_table).where(opportunity_table.c.opportunity_id == opportunity_id)
        )
        return _opportunity(row) if row is not None else None

    def save_opportunities(
        self, opportunities: tuple[CryptoResearchOpportunity, ...]
    ) -> tuple[CryptoResearchOpportunity, ...]:
        retained = []
        for value in opportunities:
            prior = self.opportunity(value.opportunity_id)
            if prior is not None:
                if prior.canonical_hash != value.canonical_hash:
                    raise CryptoResearchConflict("CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT")
                retained.append(prior)
                continue
            self._insert(
                opportunity_table,
                {
                    "opportunity_id": value.opportunity_id,
                    "discovery_run_id": value.discovery_run_id,
                    "product_id": value.product_id,
                    "status": value.status.value,
                    "payload": _payload(value),
                    "canonical_hash": value.canonical_hash,
                    "authority": "NONE",
                },
                "CRYPTO_OPPORTUNITY_IDENTITY_CONFLICT",
            )
            retained.append(value)
        return tuple(retained)

    def opportunities(self) -> tuple[CryptoResearchOpportunity, ...]:
        return tuple(
            _opportunity(row)
            for row in self._rows(
                sa.select(opportunity_table).order_by(opportunity_table.c.opportunity_id)
            )
        )
