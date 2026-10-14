from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

import apex.adapters.coinbase_universe_public as coinbase_universe_public
from apex.adapters.coinbase_universe_public import (
    PRODUCT_SORT_ORDER,
    CoinbasePublicUniverseSource,
    FixtureCoinbaseUniverseSource,
    _parse_product_page,
)
from apex.application.crypto_universe import (
    CaptureCryptoCalibrationHistory,
    CaptureCryptoUniverseObservations,
    CapturedProductInput,
    CapturedProductPage,
    CapturePublicCryptoProducts,
    CryptoUniverseConflict,
    InMemoryCryptoUniverseRepository,
    MaterializeCryptoUniverse,
    RunCryptoDiscovery,
    crypto_universe_workspace,
)
from apex.domain.crypto_universe import (
    CRYPTO_DISCOVERY_V1,
    CRYPTO_UNIVERSE_POLICY_V2,
    CatalogPaginationStatus,
    CryptoCurrentMarketEvidence,
    CryptoUniverseError,
    OpportunityStatus,
    PublicCryptoProduct,
    materialize_universe,
    product_route_exclusion_reasons,
    run_relative_strength,
)
from apex.domain.research_evidence import EvidenceClass
from apex.operator_api import app
from scripts.dev import parser

NOW = datetime(2026, 7, 29, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 7, 1, tzinfo=UTC)


class _SyntheticPagedSource:
    evidence_class = EvidenceClass.FIXTURE
    network_access = False

    def __init__(self, *, repeated_cursor: bool = False) -> None:
        self.calls = 0
        self.repeated_cursor = repeated_cursor

    def capture_products(
        self,
        *,
        requested_at: datetime,
        maximum_products: int,
        cursor: str | None,
    ) -> CapturedProductPage:
        self.calls += 1
        next_cursor = cursor if self.repeated_cursor and cursor else f"cursor-{self.calls}"
        product_id = f"ASSET{self.calls}-USD"
        raw = f'{{"page":{self.calls},"product":"{product_id}"}}'.encode()
        return CapturedProductPage(
            captured_at=requested_at,
            raw_payload=raw,
            products=(
                CapturedProductInput(
                    product_id=product_id,
                    base_asset=f"ASSET{self.calls}",
                    quote_asset="USD",
                    market_type="SPOT",
                    trading_status="ACTIVE",
                    product_status="ONLINE",
                    price_increment="0.01",
                    quantity_increment="0.0001",
                    minimum_quantity="0.0001",
                    minimum_notional="1",
                    public_volume_24h=str(1_000_000_000 - self.calls),
                ),
            ),
            source_locator=f"fixture://page/{self.calls}",
            runtime_milliseconds=1,
            request_cursor=cursor,
            next_cursor=next_cursor,
            has_next=True,
        )

    def capture_daily_observations(self, **_: object) -> object:
        raise AssertionError("history capture is not part of pagination tests")


class _CountingHistorySource(FixtureCoinbaseUniverseSource):
    def __init__(self, *, partial_product_id: str | None = None) -> None:
        self.calls = 0
        self.partial_product_id = partial_product_id

    def capture_daily_observations(self, **kwargs: object):
        self.calls += 1
        page = super().capture_daily_observations(**kwargs)  # type: ignore[arg-type]
        if kwargs["product_id"] != self.partial_product_id:
            return page
        retained = tuple(
            item for item in page.candles if item.bucket_start >= datetime(2025, 1, 1, tzinfo=UTC)
        )
        return replace(
            page,
            candles=retained,
            missing_buckets=page.missing_buckets + len(page.candles) - len(retained),
        )


def _fixture_workflow() -> tuple[InMemoryCryptoUniverseRepository, object, object]:
    repository = InMemoryCryptoUniverseRepository()
    source = FixtureCoinbaseUniverseSource()
    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="crypto-universe-fixture-products-v1",
        requested_at=NOW,
    )
    CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=START,
        interval_end=END,
        requested_at=NOW,
    )
    snapshot = MaterializeCryptoUniverse(repository).execute(
        snapshot_id="crypto-universe-fixture-snapshot-v1",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    run, opportunities = RunCryptoDiscovery(repository).execute(
        run_id="crypto-universe-fixture-discovery-v1",
        snapshot_id=snapshot.snapshot_id,
        generated_at=NOW,
    )
    return repository, run, opportunities


def test_product_parser_and_decimal_contract() -> None:
    raw = (
        b'{"products":[{"base_currency_id":"BTC","base_increment":"0.00000001",'
        b'"base_min_size":"0.0001","price_increment":"0.01","product_id":"BTC-USD",'
        b'"product_type":"SPOT","quote_currency_id":"USD","quote_min_size":"1",'
        b'"status":"online","trading_disabled":false,'
        b'"approximate_quote_24h_volume":"123","alias":"BTC-USD",'
        b'"alias_to":["BTC-USDC"]}],"pagination":{"has_next":false,'
        b'"next_cursor":""}}'
    )
    products, next_cursor, has_next = _parse_product_page(raw, 10)
    product = products[0]
    assert product.product_id == "BTC-USD"
    assert product.price_increment == Decimal("0.01")
    assert product.public_volume_24h == Decimal("123")
    assert product.provider_alias_to == ("BTC-USDC",)
    assert next_cursor is None and not has_next
    with pytest.raises(CryptoUniverseError, match="CRYPTO_PRICE_INCREMENT_INVALID"):
        PublicCryptoProduct.create(
            product_id="BAD-USD",
            provider="COINBASE",
            base_asset="BAD",
            quote_asset="USD",
            market_type="SPOT",
            trading_status="ACTIVE",
            product_status="ONLINE",
            price_increment=0.01,
            quantity_increment="0.1",
            minimum_quantity="1",
            minimum_notional=None,
            public_volume_24h="1",
            captured_at=NOW,
            source_contract_version="v1",
            raw_evidence_hash="a" * 64,
            parser_version="v1",
            evidence_class=FixtureCoinbaseUniverseSource.evidence_class,
        )
    unusable_precision = PublicCryptoProduct.create(
        product_id="NOPRECISION-USD",
        provider="COINBASE",
        base_asset="NOPRECISION",
        quote_asset="USD",
        market_type="SPOT",
        trading_status="ACTIVE",
        product_status="ONLINE",
        price_increment="0",
        quantity_increment="0",
        minimum_quantity="1",
        minimum_notional=None,
        public_volume_24h="1",
        captured_at=NOW,
        source_contract_version="v1",
        raw_evidence_hash="b" * 64,
        parser_version="v1",
        evidence_class=FixtureCoinbaseUniverseSource.evidence_class,
    )
    assert "PRECISION_UNUSABLE" in product_route_exclusion_reasons(unusable_precision)


def test_fixture_capture_is_bounded_and_replay_is_idempotent() -> None:
    repository = InMemoryCryptoUniverseRepository()
    source = FixtureCoinbaseUniverseSource()
    service = CapturePublicCryptoProducts(repository, source)
    first = service.execute(capture_id="products-v1", requested_at=NOW)
    replay = service.execute(capture_id="products-v1", requested_at=NOW)
    assert first == replay
    assert first.request_count == 4
    assert len(first.product_ids) == 30
    assert first.pagination_status is CatalogPaginationStatus.COMPLETE
    assert first.catalog_complete
    assert len(first.pages) == 4
    assert tuple(page.page_ordinal for page in first.pages) == (1, 2, 3, 4)
    assert all(page.raw_evidence_hash for page in first.pages)
    assert first.evidence_class.value == "FIXTURE"

    observations = CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=first.capture_id,
        interval_start=START,
        interval_end=END,
        requested_at=NOW,
    )
    replayed = CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=first.capture_id,
        interval_start=START,
        interval_end=END,
        requested_at=NOW,
    )
    assert observations == replayed
    assert len(observations) == 20
    with pytest.raises(CryptoUniverseConflict, match="CRYPTO_OBSERVATION_IDENTITY_CONFLICT"):
        CaptureCryptoUniverseObservations(repository, source).execute(
            capture_id=first.capture_id,
            interval_start=START + timedelta(days=1),
            interval_end=END,
            requested_at=NOW,
        )


def test_current_universe_history_capture_is_partitioned_gap_explicit_and_exactly_replayed() -> (
    None
):
    repository, _, _ = _fixture_workflow()
    snapshot = repository.universe_snapshots()[0]
    source = _CountingHistorySource(partial_product_id=snapshot.eligible_product_ids[-1])
    service = CaptureCryptoCalibrationHistory(repository, source)
    first = service.execute(
        history_id="current-crypto-calibration-history-v1",
        universe_snapshot_id=snapshot.snapshot_id,
        interval_start=datetime(2024, 8, 1, tzinfo=UTC),
        interval_end=datetime(2026, 8, 1, tzinfo=UTC),
        requested_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    calls = source.calls
    replay = service.execute(
        history_id="current-crypto-calibration-history-v1",
        universe_snapshot_id=snapshot.snapshot_id,
        interval_start=datetime(2024, 8, 1, tzinfo=UTC),
        interval_end=datetime(2026, 8, 1, tzinfo=UTC),
        requested_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )

    assert len(first.observations) == len(snapshot.eligible_product_ids) == 10
    assert first.request_count == calls == 30
    assert not first.idempotent_replay
    assert replay.idempotent_replay and replay.request_count == 0
    assert source.calls == calls
    assert replay.observations == first.observations
    assert all(
        item.request_count == len(item.coverage_partitions) == 3
        and tuple(partition.ordinal for partition in item.coverage_partitions) == (1, 2, 3)
        and item.coverage_partitions[0].interval_start == first.interval_start
        and item.coverage_partitions[-1].interval_end == first.interval_end
        and all(
            left.interval_end == right.interval_start
            for left, right in pairwise(item.coverage_partitions)
        )
        and item.authority == "NONE"
        for item in first.observations
    )
    partial = next(
        item for item in first.observations if item.product_id == source.partial_product_id
    )
    assert partial.coverage_status == "PARTIAL_LISTING_HISTORY"
    assert partial.missing_buckets > 0
    assert partial.coverage_limitations == ("LISTING_HISTORY_DOES_NOT_COVER_REQUESTED_INTERVAL",)
    assert all(
        item.coverage_status == "COMPLETE"
        for item in first.observations
        if item.product_id != source.partial_product_id
    )


def test_relative_strength_calibration_rejects_same_time_outcome_evidence() -> None:
    repository, _, _ = _fixture_workflow()
    snapshot = repository.universe_snapshots()[0]
    history = CaptureCryptoCalibrationHistory(repository, FixtureCoinbaseUniverseSource()).execute(
        history_id="anti-leakage-history-v1",
        universe_snapshot_id=snapshot.snapshot_id,
        interval_start=datetime(2024, 8, 1, tzinfo=UTC),
        interval_end=datetime(2026, 8, 1, tzinfo=UTC),
        requested_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    signal = tuple(
        item
        for item in repository.observations()
        if item.product_id in snapshot.eligible_product_ids and not item.coverage_partitions
    )
    current_market = tuple(
        CryptoCurrentMarketEvidence(
            product_id=product_id,
            checkpoint_id=f"{product_id}-checkpoint",
            observed_at=datetime(2026, 7, 31, tzinfo=UTC),
            product_status="ACTIVE/ONLINE",
            spread_bps=Decimal("1"),
            visible_ask_depth=Decimal("2000"),
            required_capital=Decimal("1"),
            maximum_loss=Decimal("1"),
            maximum_sensible_allocation=Decimal("2000"),
            evidence_complete=True,
            canonical_hash="a" * 64,
        )
        for product_id in snapshot.eligible_product_ids
    )

    with pytest.raises(CryptoUniverseError, match="CRYPTO_RELATIVE_STRENGTH_CALIBRATION_LEAKAGE"):
        run_relative_strength(
            run_id="anti-leakage-relative-strength-v1",
            source_pulse_id="anti-leakage-pulse-v1",
            snapshot=snapshot,
            observations=signal,
            current_market=current_market,
            generated_at=datetime(2026, 7, 31, tzinfo=UTC),
            calibration_observations=history.observations,
        )


def test_public_query_is_explicitly_spot_volume_sorted_and_cursor_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        b'{"products":[{"product_id":"BTC-USD","base_currency_id":"BTC",'
        b'"quote_currency_id":"USD","product_type":"SPOT","status":"online",'
        b'"trading_disabled":false,"price_increment":"0.01",'
        b'"base_increment":"0.00000001","base_min_size":"0.0001",'
        b'"quote_min_size":"1","approximate_quote_24h_volume":"100"}],'
        b'"pagination":{"has_next":false,"next_cursor":""}}'
    )
    paths: list[str] = []

    def fake_read(path: str) -> tuple[bytes, datetime, int]:
        paths.append(path)
        return raw, NOW, 1

    monkeypatch.setattr(coinbase_universe_public, "_read", fake_read)
    page = CoinbasePublicUniverseSource().capture_products(
        requested_at=NOW,
        maximum_products=100,
        cursor="encoded-cursor",
    )
    assert page.products[0].product_id == "BTC-USD"
    assert "product_type=SPOT" in paths[0]
    assert f"products_sort_order={PRODUCT_SORT_ORDER}" in paths[0]
    assert "cursor=encoded-cursor" in paths[0]


def test_repeated_cursor_rejects_and_bounded_truncation_is_not_production_eligible() -> None:
    repeated_repository = InMemoryCryptoUniverseRepository()
    with pytest.raises(CryptoUniverseConflict, match="CRYPTO_PRODUCT_CURSOR_INCONSISTENT"):
        CapturePublicCryptoProducts(
            repeated_repository,
            _SyntheticPagedSource(repeated_cursor=True),
        ).execute(capture_id="repeated", requested_at=NOW)

    repository = InMemoryCryptoUniverseRepository()
    source = _SyntheticPagedSource()
    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="truncated",
        requested_at=NOW,
    )
    assert source.calls == 10
    assert capture.pagination_status is CatalogPaginationStatus.TRUNCATED
    assert capture.continuation_cursor == "cursor-10"
    assert not capture.catalog_complete
    with pytest.raises(
        CryptoUniverseError,
        match="CRYPTO_CATALOG_INCOMPLETE_FOR_PRODUCTION_UNIVERSE",
    ):
        MaterializeCryptoUniverse(repository).execute(
            snapshot_id="must-not-exist",
            capture_id=capture.capture_id,
            created_at=NOW,
        )
    assert repository.universe_snapshot("must-not-exist") is None


def test_policy_retains_inclusion_and_exclusion_reasons() -> None:
    repository, _, _ = _fixture_workflow()
    snapshot = repository.universe_snapshots()[0]
    decisions = {item.product_id: item for item in snapshot.decisions}
    assert snapshot.policy_id == CRYPTO_UNIVERSE_POLICY_V2
    assert 1 <= len(snapshot.eligible_product_ids) <= 10
    assert decisions["BTC-EUR"].reasons == (
        "ALTERNATE_ROUTE_NOT_SELECTED",
        "QUOTE_CURRENCY_UNSUPPORTED",
    )
    assert "MARKET_TYPE_NOT_SPOT" in decisions["ETH-PERP"].reasons
    assert "LEVERAGED_OR_INVERSE_TOKEN_EXCLUDED" in decisions["BTC3L-USD"].reasons
    assert "STABLE_VALUE_BASE_ASSET_EXCLUDED" in decisions["USDT-USDC"].reasons
    assert "WRAPPED_DUPLICATE_BASE_ASSET_EXCLUDED" in decisions["CBBTC-USD"].reasons
    assert "BTC" in snapshot.history_shortlist_asset_ids
    assert "ETH" in snapshot.history_shortlist_asset_ids
    assert len(snapshot.history_shortlist_asset_ids) == 20
    assert len(set(snapshot.eligible_asset_ids)) == len(snapshot.eligible_asset_ids)
    assert len(snapshot.eligible_asset_ids) <= 10


def test_base_asset_grouping_deduplicates_routes_and_retains_alternates() -> None:
    repository, run, opportunities = _fixture_workflow()
    snapshot = repository.universe_snapshots()[0]
    groups = {item.canonical_asset_id: item for item in snapshot.asset_groups}
    btc = groups["BTC"]
    assert btc.selected_product_id == "BTC-USD"
    assert btc.product_route_ids == ("BTC-USD", "BTC-USDC", "BTC-EUR")
    selected = next(item for item in btc.route_decisions if item.selected)
    alternate = next(item for item in btc.route_decisions if item.product_id == "BTC-USDC")
    assert "SELECTED_BY_TRADABILITY_PRECISION_VOLUME_AND_TIE_BREAK" in selected.reasons
    assert "ALTERNATE_ROUTE_FOR_SAME_CANONICAL_ASSET" in alternate.reasons
    assert "BTC-USDC" in btc.provider_declared_aliases
    assert len({item.canonical_asset_id for item in run.results}) == len(run.results)
    assert len({item.canonical_asset_id for item in opportunities}) == len(opportunities)


def test_history_shortlist_evaluates_major_liquid_assets_once_per_base() -> None:
    repository, _, _ = _fixture_workflow()
    observations = repository.observations()
    observed_assets = tuple(item.canonical_asset_id for item in observations)
    assert observed_assets[:2] == ("BTC", "ETH")
    assert len(observed_assets) == 20
    assert len(set(observed_assets)) == 20
    assert sum(item.product_id.startswith("BTC-") for item in observations) == 1
    assert sum(item.product_id.startswith("ETH-") for item in observations) == 1


def test_mixed_observation_lineage_is_rejected() -> None:
    repository, _, _ = _fixture_workflow()
    capture = repository.product_captures()[0]
    observations = repository.observations()
    mixed = (replace(observations[0], source_capture_id="other-capture"), *observations[1:])
    with pytest.raises(
        CryptoUniverseError,
        match="CRYPTO_UNIVERSE_MIXED_OBSERVATION_LINEAGE",
    ):
        materialize_universe(
            snapshot_id="mixed-lineage",
            capture=capture,
            products=repository.products(capture.capture_id),
            observations=mixed,
            created_at=NOW,
        )


def test_universe_snapshot_identity_conflict_fails_closed() -> None:
    repository, _, _ = _fixture_workflow()
    snapshot = repository.universe_snapshots()[0]
    with pytest.raises(RuntimeError, match="CRYPTO_UNIVERSE_SNAPSHOT_IDENTITY_CONFLICT"):
        repository.save_universe_snapshot(
            replace(snapshot, manifest_hash="f" * 64, canonical_hash="e" * 64)
        )


def test_fixture_and_genuine_observations_never_mix() -> None:
    class GenuineFixtureSource(FixtureCoinbaseUniverseSource):
        evidence_class = EvidenceClass.HISTORICAL_PUBLIC

    repository = InMemoryCryptoUniverseRepository()
    for capture_id, source in (
        ("fixture-products", FixtureCoinbaseUniverseSource()),
        ("genuine-products", GenuineFixtureSource()),
    ):
        CapturePublicCryptoProducts(repository, source).execute(
            capture_id=capture_id,
            requested_at=NOW,
        )
        CaptureCryptoUniverseObservations(repository, source).execute(
            capture_id=capture_id,
            interval_start=START,
            interval_end=END,
            requested_at=NOW,
        )
    snapshot = MaterializeCryptoUniverse(repository).execute(
        snapshot_id="genuine-universe",
        capture_id="genuine-products",
        created_at=NOW,
    )
    assert snapshot.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    assert all("EVIDENCE_CLASS_MISMATCH" not in item.reasons for item in snapshot.decisions)


def test_discovery_is_deterministic_decimal_safe_and_tie_broken() -> None:
    repository, first, _ = _fixture_workflow()
    replay, _ = RunCryptoDiscovery(repository).execute(
        run_id=first.run_id,
        snapshot_id=first.universe_snapshot_id,
        generated_at=NOW + timedelta(hours=1),
    )
    assert replay == first
    assert first.method_id == CRYPTO_DISCOVERY_V1
    assert tuple(item.rank for item in first.results) == tuple(range(1, len(first.results) + 1))
    assert all(isinstance(item.gross_research_score, Decimal) for item in first.results)
    assert tuple((item.gross_research_score, item.product_id) for item in first.results) == tuple(
        sorted(
            ((item.gross_research_score, item.product_id) for item in first.results),
            key=lambda item: (-item[0], item[1]),
        )
    )
    assert len({item.canonical_asset_id for item in first.results}) == len(first.results)


def test_opportunities_expire_and_have_zero_authority() -> None:
    _, _, opportunities = _fixture_workflow()
    assert opportunities
    assert all(item.valid_until > item.generated_at for item in opportunities)
    assert all(item.capital_authority == "NONE" for item in opportunities)
    assert all(not item.paper_eligible and not item.live_eligible for item in opportunities)
    assert all(item.broker_authority == "NONE" for item in opportunities)
    assert any(item.status is OpportunityStatus.REJECTED for item in opportunities)
    assert all(
        item.rejection_reasons
        for item in opportunities
        if item.status is OpportunityStatus.REJECTED
    )
    assert all(
        item.status is OpportunityStatus.REJECTED
        for item in opportunities
        if item.gross_research_score <= 0
    )


def test_workspace_has_useful_empty_and_populated_states() -> None:
    empty = crypto_universe_workspace(InMemoryCryptoUniverseRepository())
    assert empty["empty_state"] == "NO MULTI-ASSET CRYPTO UNIVERSE EVIDENCE RETAINED"
    repository, _, _ = _fixture_workflow()
    populated = crypto_universe_workspace(repository)
    assert populated["empty_state"] is None
    assert populated["catalog_complete"] is True
    assert populated["product_route_count"] == 30
    assert populated["canonical_base_asset_count"] == 25
    assert len(populated["history_shortlist"]) == 20
    assert populated["orders"] == 0
    assert populated["financial_mutation_authority"] == "NONE"


def test_exact_id_commands_and_read_only_api_routes_exist() -> None:
    parsed = parser().parse_args(
        [
            "crypto-observations-capture",
            "--capture-id",
            "products-v1",
            "--mode",
            "fixture",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-07-01T00:00:00Z",
        ]
    )
    assert parsed.capture_id == "products-v1"
    methods = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/crypto-research/product-captures/{capture_id}", "GET") in methods
    assert ("/api/v1/crypto-research/universe-policy", "GET") in methods
    assert ("/api/v1/crypto-research/universes/{snapshot_id}", "GET") in methods
    assert ("/api/v1/crypto-research/observations/{observation_id}", "GET") in methods
    assert ("/api/v1/crypto-research/discoveries/{run_id}", "GET") in methods
    assert (
        "/api/v1/crypto-research/opportunities/{opportunity_id}",
        "GET",
    ) in methods
