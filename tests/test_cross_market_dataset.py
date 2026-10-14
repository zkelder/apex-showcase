from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apex.domain.cross_market_dataset import CrossMarketDataset, CrossMarketDatasetError

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def _dataset(**overrides: object) -> CrossMarketDataset:
    defaults: dict[str, object] = dict(
        equity_dataset_snapshot_id="snap-1",
        equity_dataset_snapshot_hash="hash-1",
        equity_coverage_start="2020-07-27",
        equity_coverage_end="2026-08-06",
        cohort_policy_id="policy-1",
        cohort_policy_hash="policy-hash-1",
        quality_gate_report_id="qg-1",
        quality_gate_report_hash="qg-hash-1",
        crypto_provider="COINBASE",
        crypto_product_id="BTC-USD",
        crypto_capture_ids=("btc-a", "btc-b", "btc-c"),
        crypto_coverage_start="2020-07-01",
        crypto_coverage_end="2026-06-30",
        tmc_component_symbols=("SPY", "TLT", "USO"),
        tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
        adjustment_policy_id="ALPACA_IEX_RAW_NO_ADJUSTMENT_V1",
        missing_data_policy_id="FAIL_CLOSED_NO_FORWARD_FILL_V1",
        alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE", "SURVIVORSHIP_LIMITED"),
        created_at=NOW,
    )
    defaults.update(overrides)
    return CrossMarketDataset.create(**defaults)  # type: ignore[arg-type]


def test_common_overlap_is_the_intersection_of_equity_and_crypto_windows() -> None:
    dataset = _dataset()
    assert dataset.common_overlap_start == "2020-07-27"
    assert dataset.common_overlap_end == "2026-06-30"


def test_no_overlap_reports_none_rather_than_a_fabricated_range() -> None:
    dataset = _dataset(crypto_coverage_start="2010-01-01", crypto_coverage_end="2015-01-01")
    assert dataset.common_overlap_start is None
    assert dataset.common_overlap_end is None


def test_identity_is_deterministic_given_identical_inputs() -> None:
    first = _dataset()
    second = _dataset()
    assert first.cross_market_dataset_id == second.cross_market_dataset_id
    assert first.canonical_hash == second.canonical_hash


def test_identity_changes_when_equity_snapshot_changes() -> None:
    first = _dataset()
    second = _dataset(equity_dataset_snapshot_id="snap-2")
    assert first.cross_market_dataset_id != second.cross_market_dataset_id


def test_requires_at_least_one_crypto_capture() -> None:
    with pytest.raises(CrossMarketDatasetError, match="REQUIRES_AT_LEAST_ONE_CRYPTO_CAPTURE"):
        _dataset(crypto_capture_ids=())


def test_requires_aware_created_at() -> None:
    with pytest.raises(CrossMarketDatasetError, match="MUST_BE_AWARE"):
        _dataset(created_at=datetime(2026, 8, 7))


def test_authority_is_always_none() -> None:
    assert _dataset().authority == "NONE"


def test_capture_ids_and_tmc_symbols_are_deduplicated_and_sorted() -> None:
    dataset = _dataset(
        crypto_capture_ids=("btc-c", "btc-a", "btc-a"),
        tmc_component_symbols=("USO", "SPY", "SPY"),
    )
    assert dataset.crypto_capture_ids == ("btc-a", "btc-c")
    assert dataset.tmc_component_symbols == ("SPY", "USO")
