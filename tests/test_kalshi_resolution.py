"""Kalshi Settlement & Resolution Evidence Maturation v1: pure domain tests
for ``domain.kalshi_resolution``. No I/O, no Postgres -- see
``tests/test_kalshi_resolution_application.py`` for the in-memory
repository/point-in-time tests and
``tests/persistence/test_postgres_kalshi_resolution.py`` for real-Postgres
coverage."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.kalshi_resolution import (
    KalshiMarketResolutionFact,
    KalshiResolutionError,
    KalshiResolutionState,
    build_kalshi_resolution_fact,
    classify_kalshi_resolution_state,
    kalshi_resolution_fact_id,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.values import ValueValidationError

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _build(**overrides: object) -> KalshiMarketResolutionFact:
    values: dict[str, object] = {
        "market_ticker": "KXHIGHLAX-26MAY20-T76",
        "event_ticker": "KXHIGHLAX-26MAY20",
        "provider_status": "finalized",
        "raw_result": "yes",
        "settlement_value_dollars": "1.0000",
        "settlement_ts": NOW,
        "close_time": NOW,
        "source_endpoint_identity": "kalshi-public-market-followup-v1",
        "evidence_class": EvidenceClass.GENUINE_PUBLIC,
        "captured_at": NOW,
        "run_id": "test-run-1",
    }
    values.update(overrides)
    return build_kalshi_resolution_fact(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# classify_kalshi_resolution_state: total, fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_status",
    ["initialized", "unopened", "active", "open", "inactive", "paused"],
)
def test_pretrading_and_open_statuses_classify_not_yet_closed(raw_status: str) -> None:
    assert classify_kalshi_resolution_state(raw_status) is KalshiResolutionState.NOT_YET_CLOSED


def test_closed_status_classifies_closed_awaiting_result() -> None:
    assert (
        classify_kalshi_resolution_state("closed") is KalshiResolutionState.CLOSED_AWAITING_RESULT
    )


def test_determined_status_classifies_result_determined() -> None:
    assert classify_kalshi_resolution_state("determined") is KalshiResolutionState.RESULT_DETERMINED


def test_disputed_status_classifies_disputed() -> None:
    assert classify_kalshi_resolution_state("disputed") is KalshiResolutionState.DISPUTED


def test_amended_status_classifies_amended() -> None:
    assert classify_kalshi_resolution_state("amended") is KalshiResolutionState.AMENDED


@pytest.mark.parametrize("raw_status", ["finalized", "settled"])
def test_finalized_and_settled_statuses_classify_finalized(raw_status: str) -> None:
    assert classify_kalshi_resolution_state(raw_status) is KalshiResolutionState.FINALIZED


def test_case_and_whitespace_insensitive() -> None:
    assert classify_kalshi_resolution_state("  Finalized  ") is KalshiResolutionState.FINALIZED


@pytest.mark.parametrize("raw_status", [None, "", "banana", "PENDING_REVIEW"])
def test_unrecognized_or_missing_status_fails_closed(raw_status: str | None) -> None:
    assert classify_kalshi_resolution_state(raw_status) is KalshiResolutionState.UNRECOGNIZED


# ---------------------------------------------------------------------------
# build_kalshi_resolution_fact: the authoritative-gating rule (Governing
# Constraint -- Apex must never treat an unresolved Kalshi market as if its
# outcome were known)
# ---------------------------------------------------------------------------


def test_finalized_read_produces_authoritative_result() -> None:
    fact = _build(provider_status="finalized", raw_result="yes")
    assert fact.resolution_state is KalshiResolutionState.FINALIZED
    assert fact.resolved_result == "YES"
    assert fact.settlement_value_dollars == Decimal("1.0000")
    assert fact.settlement_ts == NOW


@pytest.mark.parametrize("raw_status", ["closed", "determined", "disputed", "amended"])
def test_non_finalized_read_never_produces_authoritative_result_even_with_raw_result(
    raw_status: str,
) -> None:
    """The exact governing-constraint regression this milestone exists to
    close: a provider ``result`` field present before ``finalized`` (Kalshi
    genuinely populates ``result`` at ``determined``, before its dispute
    window closes) must never be trusted as Apex's knowledge of the
    market's true outcome."""

    fact = _build(provider_status=raw_status, raw_result="yes")
    assert fact.resolved_result is None
    assert fact.settlement_value_dollars is None
    assert fact.settlement_ts is None
    # Honest lineage: the raw value the provider actually sent is still
    # retained, just never surfaced as authoritative.
    assert fact.raw_result == "YES"


def test_unrecognized_status_never_produces_authoritative_result() -> None:
    fact = _build(provider_status="some_new_kalshi_state", raw_result="yes")
    assert fact.resolution_state is KalshiResolutionState.UNRECOGNIZED
    assert fact.resolved_result is None


def test_not_yet_closed_status_never_produces_authoritative_result() -> None:
    fact = _build(provider_status="active", raw_result=None, settlement_value_dollars=None)
    assert fact.resolution_state is KalshiResolutionState.NOT_YET_CLOSED
    assert fact.resolved_result is None
    assert fact.raw_result is None


def test_result_normalization_strips_and_uppercases() -> None:
    fact = _build(provider_status="finalized", raw_result="  no  ")
    assert fact.raw_result == "NO"
    assert fact.resolved_result == "NO"


def test_missing_result_on_finalized_market_is_none_not_a_crash() -> None:
    """A finalized market with no ``result`` field at all (malformed/
    incomplete provider payload) fails closed to ``None``, never a crash
    and never a fabricated value (Part 13)."""

    fact = _build(provider_status="finalized", raw_result=None, settlement_value_dollars=None)
    assert fact.resolution_state is KalshiResolutionState.FINALIZED
    assert fact.resolved_result is None
    assert fact.settlement_value_dollars is None


# ---------------------------------------------------------------------------
# Identity / idempotency / correction-lineage (Part 5/13)
# ---------------------------------------------------------------------------


def test_identical_content_produces_identical_id_regardless_of_run_id_or_captured_at() -> None:
    first = _build(run_id="run-a", captured_at=NOW)
    second = _build(run_id="run-b", captured_at=NOW.replace(hour=13))
    assert first.resolution_fact_id == second.resolution_fact_id
    assert first.canonical_hash == second.canonical_hash


def test_repeated_identical_provider_response_is_idempotent_at_the_id_level() -> None:
    """Simulates the recurring cycle observing the exact same terminal
    state on two different ticks: same id, safe to no-op on replay."""

    first = _build()
    second = _build()
    assert first.resolution_fact_id == second.resolution_fact_id


@pytest.mark.parametrize(
    ("field", "other_value"),
    [
        ("provider_status", "determined"),
        ("raw_result", "no"),
        ("settlement_value_dollars", "0.0000"),
        ("settlement_ts", NOW.replace(hour=13)),
        ("close_time", NOW.replace(hour=11)),
    ],
)
def test_a_genuine_content_change_produces_a_new_id_never_a_mutation(
    field: str, other_value: object
) -> None:
    """A market progressing (e.g. determined -> finalized) or Kalshi
    correcting a result must land as a brand new, additional row -- never
    silently overwrite the prior observation's own identity."""

    original = _build()
    changed = _build(**{field: other_value})
    assert original.resolution_fact_id != changed.resolution_fact_id


def test_correction_from_determined_to_finalized_is_a_new_id() -> None:
    determined = _build(provider_status="determined", raw_result="yes")
    finalized = _build(provider_status="finalized", raw_result="yes")
    assert determined.resolution_fact_id != finalized.resolution_fact_id
    # Both are independently valid, retained, immutable facts about the
    # *same* market -- neither is deleted or rewritten by the other.
    assert determined.market_ticker == finalized.market_ticker


def test_event_ticker_and_source_endpoint_do_not_affect_identity() -> None:
    """Identity is deliberately about *what* was observed, not *how* --
    see module docstring."""

    via_event = _build(event_ticker="EVT-1", source_endpoint_identity="event-endpoint")
    via_market = _build(event_ticker=None, source_endpoint_identity="market-endpoint")
    assert via_event.resolution_fact_id == via_market.resolution_fact_id
    # canonical_hash *does* still differ, since it retains full lineage --
    # useful defense-in-depth even though it never gates identity.
    assert via_event.canonical_hash != via_market.canonical_hash


def test_kalshi_resolution_fact_id_is_a_pure_function_of_its_declared_inputs() -> None:
    computed = kalshi_resolution_fact_id(
        market_ticker="T",
        provider_status="finalized",
        raw_result="YES",
        settlement_value_dollars=Decimal("1.0000"),
        settlement_ts=NOW,
        close_time=NOW,
    )
    fact = _build(market_ticker="T", provider_status="finalized", raw_result="yes")
    assert computed == fact.resolution_fact_id


# ---------------------------------------------------------------------------
# Validation / fail-closed construction
# ---------------------------------------------------------------------------


def test_empty_market_ticker_rejected() -> None:
    with pytest.raises(KalshiResolutionError):
        _build(market_ticker="")


def test_empty_provider_status_rejected() -> None:
    with pytest.raises(KalshiResolutionError):
        _build(provider_status="")


def test_naive_captured_at_rejected() -> None:
    with pytest.raises(KalshiResolutionError):
        _build(captured_at=datetime(2026, 9, 1, 12, 0))


def test_naive_settlement_ts_rejected() -> None:
    with pytest.raises(KalshiResolutionError):
        _build(settlement_ts=datetime(2026, 9, 1, 12, 0))


def test_naive_close_time_rejected() -> None:
    with pytest.raises(KalshiResolutionError):
        _build(close_time=datetime(2026, 9, 1, 12, 0))


def test_direct_construction_rejects_authoritative_fields_below_finalized() -> None:
    """Defense in depth: even a caller that bypasses the builder and
    hand-constructs the frozen dataclass directly cannot smuggle an
    authoritative field onto a non-FINALIZED fact."""

    with pytest.raises(ValueValidationError):
        KalshiMarketResolutionFact(
            resolution_fact_id="x",
            market_ticker="T",
            event_ticker=None,
            provider_status="determined",
            resolution_state=KalshiResolutionState.RESULT_DETERMINED,
            raw_result="YES",
            resolved_result="YES",  # <-- smuggled authoritative value
            settlement_value_dollars=None,
            settlement_ts=None,
            close_time=None,
            source_endpoint_identity="e",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            captured_at=NOW,
            run_id="r",
            canonical_hash="a" * 64,
        )


def test_direct_construction_rejects_resolved_result_diverging_from_raw_result() -> None:
    with pytest.raises(ValueValidationError):
        KalshiMarketResolutionFact(
            resolution_fact_id="x",
            market_ticker="T",
            event_ticker=None,
            provider_status="finalized",
            resolution_state=KalshiResolutionState.FINALIZED,
            raw_result="YES",
            resolved_result="NO",  # <-- diverges from raw_result
            settlement_value_dollars=None,
            settlement_ts=None,
            close_time=None,
            source_endpoint_identity="e",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            captured_at=NOW,
            run_id="r",
            canonical_hash="a" * 64,
        )


def test_direct_construction_rejects_non_none_authority() -> None:
    with pytest.raises(ValueValidationError):
        KalshiMarketResolutionFact(
            resolution_fact_id="x",
            market_ticker="T",
            event_ticker=None,
            provider_status="active",
            resolution_state=KalshiResolutionState.NOT_YET_CLOSED,
            raw_result=None,
            resolved_result=None,
            settlement_value_dollars=None,
            settlement_ts=None,
            close_time=None,
            source_endpoint_identity="e",
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            captured_at=NOW,
            run_id="r",
            canonical_hash="a" * 64,
            authority="LIVE",
        )
