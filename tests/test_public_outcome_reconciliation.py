"""Network-free proofs for strict final public-outcome identity reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from apex.application.reconcile_public_outcome_identity import (
    PublicOutcomeReconciliationState,
    RetainedPublicOutcomeIdentity,
    reconcile_public_final_outcome,
)
from apex.application.scan_public_binary_complements import CapturedKalshiPublicResponse
from apex.domain.public_event import EventSettlementSource
from apex.domain.values import content_digest

MARKET_TICKER = "KXFIXTURE-TEST-YES"
EVENT_TICKER = "KXFIXTURE-TEST"
SERIES_TICKER = "KXFIXTURE"
RULE = "The fixture outcome resolves YES only when the exact condition holds."
SOURCES = ({"name": "Fixture source", "url": "https://fixture.invalid/source"},)


def _retained() -> RetainedPublicOutcomeIdentity:
    return RetainedPublicOutcomeIdentity(
        market_ticker=MARKET_TICKER,
        event_ticker=EVENT_TICKER,
        series_ticker=SERIES_TICKER,
        rule_content_hash=sha256(RULE.encode("utf-8")).hexdigest(),
        settlement_source_hash=content_digest(
            (EventSettlementSource(name="Fixture source", url="https://fixture.invalid/source"),)
        ),
    )


def _market(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ticker": MARKET_TICKER,
        "event_ticker": EVENT_TICKER,
        "series_ticker": SERIES_TICKER,
        "rules_primary": RULE,
        "status": "finalized",
        "result": "yes",
    }
    value.update(overrides)
    return value


def _event(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "event_ticker": EVENT_TICKER,
        "series_ticker": SERIES_TICKER,
        "settlement_sources": list(SOURCES),
        "markets": [
            {
                "ticker": MARKET_TICKER,
                "event_ticker": EVENT_TICKER,
                "rules_primary": RULE,
            }
        ],
    }
    value.update(overrides)
    return value


def test_actual_structural_case_is_pending_then_reconciles_from_exact_event() -> None:
    final_market = _market(series_ticker=None)

    pending = reconcile_public_final_outcome(
        retained=_retained(), final_market=final_market, exact_event=None
    )
    resolved = reconcile_public_final_outcome(
        retained=_retained(), final_market=final_market, exact_event=_event()
    )

    assert pending.state is PublicOutcomeReconciliationState.PENDING_EXACT_EVENT
    assert resolved.state is PublicOutcomeReconciliationState.RESOLVED
    assert resolved.resolved_outcome == "YES"
    assert "SERIES_IDENTITY_RECONCILED_FROM_EXACT_EVENT" in resolved.reason_codes


def test_missing_final_event_identity_reconciles_only_from_exact_event_membership() -> None:
    result = reconcile_public_final_outcome(
        retained=_retained(), final_market=_market(event_ticker=None), exact_event=_event()
    )

    assert result.state is PublicOutcomeReconciliationState.RESOLVED
    assert "EVENT_IDENTITY_RECONCILED_FROM_EXACT_EVENT" in result.reason_codes


def test_missing_final_series_identity_is_not_scored_before_exact_event_is_available() -> None:
    result = reconcile_public_final_outcome(
        retained=_retained(), final_market=_market(series_ticker=None), exact_event=None
    )

    assert result.state is PublicOutcomeReconciliationState.PENDING_EXACT_EVENT
    assert result.resolved_outcome is None


def test_exact_series_response_can_add_another_strict_identity_check() -> None:
    result = reconcile_public_final_outcome(
        retained=_retained(),
        final_market=_market(series_ticker=None),
        exact_event=_event(),
        exact_series={"ticker": SERIES_TICKER},
    )

    assert result.state is PublicOutcomeReconciliationState.RESOLVED
    assert "PUBLIC_EXACT_SERIES_GET" in result.reason_codes


@pytest.mark.parametrize(
    ("final_market", "event", "reason"),
    (
        (_market(rules_primary="A changed economic rule."), _event(), "RULE_IDENTITY_MISMATCH"),
        (_market(ticker="KXFIXTURE-OTHER"), _event(), "MARKET_IDENTITY_MISMATCH"),
        (_market(event_ticker="KXFIXTURE-OTHER"), _event(), "EVENT_IDENTITY_MISMATCH"),
        (_market(series_ticker="KXOTHER"), _event(), "SERIES_IDENTITY_MISMATCH"),
        (
            _market(),
            _event(
                settlement_sources=[{"name": "Changed", "url": "https://fixture.invalid/changed"}]
            ),
            "SETTLEMENT_SOURCE_MISMATCH",
        ),
    ),
)
def test_genuine_identity_or_rule_changes_remain_rejected(
    final_market: dict[str, object], event: dict[str, object], reason: str
) -> None:
    result = reconcile_public_final_outcome(
        retained=_retained(), final_market=final_market, exact_event=event
    )

    assert result.state is PublicOutcomeReconciliationState.INVALID
    assert reason in result.reason_codes
    assert result.resolved_outcome is None


@pytest.mark.parametrize(
    ("event", "reason"),
    (
        (_event(markets=[]), "EVENT_NESTED_MARKET_MISSING"),
        (
            _event(
                markets=[
                    {
                        "ticker": MARKET_TICKER,
                        "event_ticker": EVENT_TICKER,
                        "rules_primary": RULE,
                    },
                    {
                        "ticker": MARKET_TICKER,
                        "event_ticker": EVENT_TICKER,
                        "rules_primary": RULE,
                    },
                ]
            ),
            "EVENT_NESTED_MARKET_AMBIGUOUS",
        ),
    ),
)
def test_nested_market_membership_must_be_exact_and_unambiguous(
    event: dict[str, object], reason: str
) -> None:
    result = reconcile_public_final_outcome(
        retained=_retained(), final_market=_market(series_ticker=None), exact_event=event
    )

    assert result.state is PublicOutcomeReconciliationState.INVALID
    assert reason in result.reason_codes


def test_missing_rule_or_unsupported_result_remains_invalid() -> None:
    missing_rule = reconcile_public_final_outcome(
        retained=_retained(), final_market=_market(rules_primary=None), exact_event=_event()
    )
    unsupported_result = reconcile_public_final_outcome(
        retained=_retained(), final_market=_market(result="void"), exact_event=_event()
    )

    assert "RULE_IDENTITY_MISSING" in missing_rule.reason_codes
    assert "UNSUPPORTED_FINAL_RESULT" in unsupported_result.reason_codes


class _CollectorEngine:
    def dispose(self) -> None:
        return None


class _CollectorStore:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.evaluated_fact_ids: list[str] = []

    def plans(self, **_: object) -> tuple[dict[str, object], ...]:
        return ({"plan_id": "plan-1"},)

    def cohort(self, _: str) -> tuple[dict[str, object], ...]:
        return ({"market_ticker": MARKET_TICKER},)

    def retained_public_outcome_identity(self, **_: object) -> dict[str, str]:
        return {
            "market_ticker": MARKET_TICKER,
            "event_ticker": EVENT_TICKER,
            "series_ticker": SERIES_TICKER,
            "rule_content_hash": sha256(RULE.encode("utf-8")).hexdigest(),
            "settlement_source_hash": _retained().settlement_source_hash,
        }

    def record_public_outcome_reconciliation_event_response(self, **_: object) -> str:
        return "final-event-raw-1"

    def record_public_outcome_fact(self, **kwargs: object) -> tuple[dict[str, object], bool]:
        self.records.append(kwargs)
        return ({"outcome_fact_id": f"outcome-fact-{len(self.records)}"}, False)

    def evaluate_public_outcome_fact(self, **kwargs: object) -> tuple[int, int]:
        self.evaluated_fact_ids.append(str(kwargs["outcome_fact_id"]))
        return (2, 0)


class _CollectorClient:
    def __init__(self, *, final_market: dict[str, object]) -> None:
        self._final_market = final_market
        self.market_calls = 0
        self.event_calls = 0

    @staticmethod
    def _response(*, endpoint: str, document: dict[str, object]) -> CapturedKalshiPublicResponse:
        return CapturedKalshiPublicResponse(
            endpoint_identity=endpoint,
            received_at=datetime(2031, 1, 1, tzinfo=UTC),
            http_status=200,
            response_content_type="application/json",
            raw_payload=str(document).encode("utf-8"),
            document=document,
        )

    def fetch_market(self, **_: object) -> CapturedKalshiPublicResponse:
        self.market_calls += 1
        return self._response(endpoint="KALSHI_GET_MARKET", document={"market": self._final_market})

    def fetch_event(self, **_: object) -> CapturedKalshiPublicResponse:
        self.event_calls += 1
        return self._response(endpoint="KALSHI_GET_EVENTS", document={"event": _event()})


def _install_collector_fakes(
    monkeypatch: pytest.MonkeyPatch, client: _CollectorClient
) -> _CollectorStore:
    from apex import cli
    from apex.adapters import postgres

    store = _CollectorStore()
    monkeypatch.setattr(postgres.PostgresSettings, "from_env", classmethod(lambda _: object()))
    monkeypatch.setattr(postgres, "create_postgres_engine", lambda _: _CollectorEngine())
    monkeypatch.setattr(postgres, "postgres_shadow_operations_store", lambda _: store)
    monkeypatch.setattr(cli, "KalshiPublicOrderbookDataClient", lambda: client)
    return store


def test_collector_reconciles_omitted_series_before_evaluating_predictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.cli import collect_shadow_outcomes

    client = _CollectorClient(final_market=_market(series_ticker=None))
    store = _install_collector_fakes(monkeypatch, client)

    result = collect_shadow_outcomes(campaign_id="campaign-1", maximum_markets=1)

    assert result["exact_market_gets"] == 1
    assert result["exact_event_gets"] == 1
    assert result["compatible_outcomes"] == 1
    assert result["scored_predictions"] == 2
    assert client.market_calls == 1
    assert client.event_calls == 1
    assert store.evaluated_fact_ids == ["outcome-fact-1"]
    assert store.records[0]["reconciliation_raw_response_ids"] == ("final-event-raw-1",)


def test_collector_rejects_market_mismatch_without_scoring_or_event_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.cli import collect_shadow_outcomes

    client = _CollectorClient(final_market=_market(ticker="KXFIXTURE-OTHER"))
    store = _install_collector_fakes(monkeypatch, client)

    result = collect_shadow_outcomes(campaign_id="campaign-1", maximum_markets=1)

    assert result["genuine_incompatible_outcomes"] == 1
    assert result["exact_event_gets"] == 0
    assert result["scored_predictions"] == 0
    assert client.event_calls == 0
    assert store.evaluated_fact_ids == []
