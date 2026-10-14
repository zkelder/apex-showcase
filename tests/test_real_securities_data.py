from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apex.adapters.alpaca_market_data import (
    ALPACA_ENDPOINT_POLICY_HASH,
    ALPACA_ENDPOINT_POLICY_ID,
    AlpacaMarketDataHttpAdapter,
    LocalAlpacaRawArtifactStore,
    ProviderHttpResponse,
    ProviderRawPage,
    SecuritiesProviderHttpError,
)
from apex.adapters.alpaca_securities_normalizer import AlpacaSecuritiesEvidenceNormalizer
from apex.adapters.securities_data import RetainedSecuritiesCalendar
from apex.adapters.securities_synthetic import synthetic_securities_dataset
from apex.application.real_securities_data import (
    CheckSecuritiesProviderCapabilities,
    initial_provider_configuration,
)
from apex.domain.real_securities_data import (
    ACTION_TIMING_WARNING,
    SecuritiesDataIntegrityState,
    SecuritiesDataSuitability,
    SecuritiesForwardValidationPlan,
    SecuritiesHistoricalMembershipClaim,
    SecuritiesProviderCapabilityState,
    SecuritiesSurvivorshipState,
    SecuritiesUniverseSnapshot,
    derived_eod_available,
    evaluate_securities_data_quality,
    forward_observed_available,
    retrospective_action_warning,
)

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)


class _Transport:
    def __init__(self, responses: list[ProviderHttpResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        self,
        *,
        host: str,
        target: str,
        headers: dict[str, str],
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> ProviderHttpResponse:
        del timeout_seconds, maximum_response_bytes
        self.calls.append((host, target, headers))
        return self._responses.pop(0)


def _response(status: int, body: object, **headers: str) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        status=status,
        headers=headers,
        body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        received_at=NOW,
    )


def _adapter(
    monkeypatch: pytest.MonkeyPatch, responses: list[ProviderHttpResponse]
) -> tuple[AlpacaMarketDataHttpAdapter, _Transport, list[float]]:
    monkeypatch.setenv("APCA_API_KEY_ID", "fixture-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fixture-secret")
    transport = _Transport(responses)
    delays: list[float] = []
    return (
        AlpacaMarketDataHttpAdapter(transport=transport, sleeper=delays.append),
        transport,
        delays,
    )


def test_provider_policy_paginates_retries_and_redacts_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, transport, delays = _adapter(
        monkeypatch,
        [
            _response(429, {"message": "slow"}, **{"Retry-After": "0"}),
            _response(
                200,
                {"bars": {"SPY": [{"t": "2026-07-24T00:00:00Z"}]}, "next_page_token": "a"},
                **{"X-Request-ID": "request-one", "Set-Cookie": "not-retained"},
            ),
            _response(
                200,
                {"bars": {"SPY": [{"t": "2026-07-25T00:00:00Z"}]}, "next_page_token": None},
                **{"X-Request-ID": "request-two"},
            ),
        ],
    )

    pages = adapter.fetch_pages(
        endpoint_id="historical_daily_bars",
        query={
            "symbols": "SPY",
            "timeframe": "1Day",
            "start": "2026-07-24",
            "end": "2026-07-25",
            "feed": "iex",
            "adjustment": "raw",
            "limit": "2",
        },
        maximum_pages=2,
        maximum_response_bytes=10_000,
        timeout_seconds=10,
    )

    assert len(pages) == 2
    assert len(transport.calls) == 3
    assert transport.calls[0][0] == "data.alpaca.markets"
    assert transport.calls[0][1].startswith("/v2/stocks/bars?")
    assert "page_token=a" in transport.calls[-1][1]
    assert delays == [0.0]
    monkeypatch.chdir(tmp_path)
    artifact = LocalAlpacaRawArtifactStore().retain(
        operation_id="provider_operation_fixture_01",
        pages=pages,
        adapter_version=adapter.adapter_version,
    )
    manifest = (tmp_path / artifact.relative_path / "manifest.json").read_text()
    assert "fixture-key-id" not in manifest
    assert "fixture-secret" not in manifest
    assert "set-cookie" not in manifest
    assert "request-one" in manifest


def test_provider_rejects_authentication_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, transport, delays = _adapter(monkeypatch, [_response(401, {"message": "no"})])

    with pytest.raises(SecuritiesProviderHttpError, match="ALPACA_AUTHENTICATION_REJECTED"):
        adapter.fetch_pages(
            endpoint_id="latest_snapshots",
            query={"symbols": "SPY", "feed": "iex"},
            maximum_pages=1,
            maximum_response_bytes=10_000,
            timeout_seconds=10,
        )

    assert len(transport.calls) == 1
    assert delays == []


def test_capability_keeps_asset_identity_401_endpoint_limited_when_bars_are_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _transport, _delays = _adapter(
        monkeypatch,
        [
            _response(200, {"bars": {"SPY": []}}),
            _response(200, {"snapshots": {"SPY": {}}}),
            _response(200, {"corporate_actions": []}),
            _response(401, {"message": "redacted"}),
        ],
    )
    configuration = initial_provider_configuration(
        environment="test",
        adapter_version=adapter.adapter_version,
        endpoint_policy_id=ALPACA_ENDPOINT_POLICY_ID,
        endpoint_policy_hash=ALPACA_ENDPOINT_POLICY_HASH,
        operator_action_id="fixture-operator",
        created_at=NOW,
    )

    class Repository:
        saved = None

        def save_capability(self, value: object) -> object:
            self.saved = value
            return value

    repository = Repository()
    result = CheckSecuritiesProviderCapabilities(
        repository=repository,  # type: ignore[arg-type]
        provider=adapter,
    ).execute(configuration=configuration, checked_at=NOW)

    assert result.state is SecuritiesProviderCapabilityState.PARTIAL
    assert result.authentication_state == "AUTHENTICATED"
    assert result.historical_bars_state == "READY"
    assert result.asset_master_state == "PERMISSION_LIMITED"
    assert result.endpoint_reason_codes["asset_master"] == (
        "ALPACA_AUTHENTICATION_REJECTED_HTTP_401",
    )


def test_provider_rejects_unreviewed_endpoint_and_page_token_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _transport, _delays = _adapter(
        monkeypatch,
        [
            _response(200, {"corporate_actions": [], "next_page_token": "same"}),
            _response(200, {"corporate_actions": [], "next_page_token": "same"}),
        ],
    )
    with pytest.raises(SecuritiesProviderHttpError, match="PROVIDER_ENDPOINT_REJECTED"):
        adapter.fetch_pages(
            endpoint_id="orders",
            query={},
            maximum_pages=1,
            maximum_response_bytes=10_000,
            timeout_seconds=10,
        )
    with pytest.raises(SecuritiesProviderHttpError, match="PROVIDER_PAGE_TOKEN_CYCLE"):
        adapter.fetch_pages(
            endpoint_id="corporate_actions",
            query={"symbols": "SPY", "start": "2026-07-24", "end": "2026-07-25"},
            maximum_pages=3,
            maximum_response_bytes=10_000,
            timeout_seconds=10,
        )


def test_current_asset_identity_uses_only_reviewed_paper_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, transport, _delays = _adapter(monkeypatch, [_response(200, [])])

    pages = adapter.fetch_pages(
        endpoint_id="asset_master",
        query={"status": "active", "asset_class": "us_equity", "exchange": "NYSE"},
        maximum_pages=1,
        maximum_response_bytes=10_000,
        timeout_seconds=10,
    )

    assert len(pages) == 1
    assert transport.calls[0][0] == "paper-api.alpaca.markets"
    assert transport.calls[0][1].startswith("/v2/assets?")
    assert transport.calls[0][0] != "api.alpaca.markets"
    for prohibited in ("accounts", "orders", "positions", "activities", "watchlists"):
        with pytest.raises(SecuritiesProviderHttpError, match="PROVIDER_ENDPOINT_REJECTED"):
            adapter.fetch_pages(
                endpoint_id=prohibited,
                query={},
                maximum_pages=1,
                maximum_response_bytes=10_000,
                timeout_seconds=10,
            )


def test_fixed_universe_and_availability_policies_remain_honest() -> None:
    configuration = initial_provider_configuration(
        environment="test",
        adapter_version="fixture-v1",
        endpoint_policy_id=ALPACA_ENDPOINT_POLICY_ID,
        endpoint_policy_hash=ALPACA_ENDPOINT_POLICY_HASH,
        operator_action_id="fixture-operator",
        created_at=NOW,
    )
    assert configuration.feed == "IEX"
    universe = SecuritiesUniverseSnapshot.fixed(
        display_name="Frozen ETF research scope",
        symbols=("SPY", "TLT"),
        reason="declared before data acquisition",
        frozen_at=NOW,
        operator_action_id="fixture-operator",
        idempotency_key="fixture-universe",
    )
    assert (
        universe.historical_membership_claim
        is SecuritiesHistoricalMembershipClaim.PREDECLARED_FIXED_UNIVERSE
    )
    assert universe.survivorship_state is SecuritiesSurvivorshipState.NOT_APPLICABLE_FIXED_SCOPE
    assert derived_eod_available(session_close=NOW).isoformat() == "2026-07-27T12:30:00+00:00"
    assert forward_observed_available(received_at=NOW) == NOW
    assert retrospective_action_warning(first_observed_at=NOW)[0] == ACTION_TIMING_WARNING
    draft = SecuritiesForwardValidationPlan.draft(
        configuration=configuration,
        universe=universe,
        feature_schema_id="fixture-schema",
        feature_schema_hash="b" * 64,
        prediction_horizon_sessions=5,
        operator_action_id="fixture-operator",
        idempotency_key="fixture-forward-plan",
        created_at=NOW,
    )
    assert draft.state.value == "DRAFT"
    assert draft.activated_at is None
    assert draft.capture_cutoff_policy["identity"] == "OBSERVED_PROVIDER_RECEIPT_V1"


def test_quality_blocks_missing_canonical_evidence_without_forward_fill() -> None:
    instruments, sessions, bars, actions = synthetic_securities_dataset()
    universe = SecuritiesUniverseSnapshot.fixed(
        display_name="Exact fixture universe",
        symbols=tuple(item.symbol for item in instruments),
        reason="fixed before fixture retrieval",
        frozen_at=NOW,
        operator_action_id="fixture-operator",
        idempotency_key="fixture-quality-universe",
    )
    evaluation = evaluate_securities_data_quality(
        market_data_batch_id="fixture-batch",
        market_data_batch_hash="a" * 64,
        universe=universe,
        instruments=instruments,
        sessions=sessions,
        bars=bars[:1],
        corporate_actions=actions,
        availability_policy_id="DERIVED_EOD_AVAILABLE_V1",
        availability_policy={"delay_minutes": 30},
        revision_count=0,
        created_at=NOW,
    )
    assert evaluation.integrity_state is SecuritiesDataIntegrityState.BLOCKED
    assert evaluation.missing_bar_session_count > 0
    assert evaluation.suitability_states == (SecuritiesDataSuitability.NOT_READY,)


def test_retained_alpaca_pages_normalize_only_with_retained_calendar() -> None:
    instruments, sessions, _bars, _actions = synthetic_securities_dataset()
    session = sessions[0]
    instrument = instruments[0]
    bar_page = _response(
        200,
        {
            "bars": {
                instrument.symbol: [
                    {
                        "t": f"{session.session_date}T00:00:00Z",
                        "o": "10.00",
                        "h": "11.00",
                        "l": "9.00",
                        "c": "10.50",
                        "v": "100",
                    }
                ]
            }
        },
    )
    bar = ProviderRawPage(
        endpoint_id="historical_daily_bars",
        query={},
        status=bar_page.status,
        headers=bar_page.headers,
        body=bar_page.body,
        received_at=NOW,
        page_ordinal=1,
        next_page_token_hash=None,
        provider_request_id=None,
        retry_delays_seconds=(),
    )
    action_page = ProviderRawPage(
        endpoint_id="corporate_actions",
        query={},
        status=200,
        headers={},
        body=json.dumps(
            {
                "corporate_actions": [
                    {
                        "symbol": instrument.symbol,
                        "ca_type": "cash_dividend",
                        "ex_date": session.session_date,
                        "cash": "0.12",
                    }
                ]
            }
        ).encode(),
        received_at=NOW,
        page_ordinal=1,
        next_page_token_hash=None,
        provider_request_id=None,
        retry_delays_seconds=(),
    )
    normalizer = AlpacaSecuritiesEvidenceNormalizer()
    calendar = RetainedSecuritiesCalendar(sessions)
    bars = normalizer.normalize_bars(
        pages=(bar,),
        instruments=(instrument,),
        calendar=calendar,
        calendar_identity=session.calendar_identity,
    )
    actions = normalizer.normalize_corporate_actions(
        pages=(action_page,),
        instruments=(instrument,),
    )
    assert bars.bars[0].available_at > session.closes_at
    assert bars.bars[0].source_identity.endswith(bar.raw_sha256)
    assert actions.corporate_actions[0].available_at == NOW
    assert ACTION_TIMING_WARNING in actions.warnings
