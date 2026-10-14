"""Deterministic, no-database tests for the broad-equity universe-snapshot
schema-compatibility decoder in ``apex.adapters.security_reference_postgres``.

These exercise the pure ``_decode_universe`` path directly against literal
JSON payload text -- exactly what is stored in and read back from the
``security_reference_universe_snapshot.payload`` column -- so the
current/legacy/incompatible classification and rehydration logic is proven
without a live PostgreSQL instance. ``tests/persistence/test_postgres_security_reference.py``
proves the same behavior end to end through the real repository and a real
retained row.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from apex.adapters.security_reference_postgres import (
    LEGACY_PRE_EXECUTION_SEPARATION_SCHEMA_MARKER,
    SCHEMA_COMPATIBILITY_CURRENT,
    SCHEMA_COMPATIBILITY_INCOMPATIBLE,
    SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1,
    _decode_universe,
)
from apex.domain.security_reference import (
    APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1,
    CurrentEquityUniversePolicy,
    EvidenceState,
    ExecutionCapabilityState,
    SecurityReference,
    SecurityType,
    create_current_equity_universe_snapshot,
    reconcile_security_references,
)
from apex.domain.values import canonical_json_bytes

NOW = datetime(2026, 7, 28, 9, tzinfo=UTC)

# Modeled exactly on the genuine retained row that exposed the defect: one
# universe snapshot written before research/execution separation existed,
# carrying bare ``eligible``/``reasons`` decisions and no execution-capability
# field under any name. Kept byte-for-byte identical in spirit to the fixture
# in tests/persistence/test_postgres_security_reference.py.
LEGACY_UNIVERSE_PAYLOAD = {
    "universe_id": "broad_equity_universe_legacy0000000000000",
    "policy_id": "CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1",
    "policy_hash": "0" * 64,
    "as_of_date": "2026-07-28",
    "created_at": "2026-07-28T09:00:00Z",
    "source_receipt_ids": [],
    "total_considered": 1,
    "security_type_counts": [["COMMON_STOCK", 1]],
    "eligible_count": 0,
    "exclusion_counts": [["NOT_CONFIRMED_ALPACA_TRADABLE", 1]],
    "decisions": [
        {
            "canonical_security_id": "apex_security_legacy00000000000000000000",
            "current_symbol": "LEGACY",
            "eligible": False,
            "reasons": ["NOT_CONFIRMED_ALPACA_TRADABLE"],
            "security_hash": "1" * 64,
        }
    ],
    "provider_disagreements": [],
    "warning": "CURRENT-LISTED UNIVERSE — NOT HISTORICAL POINT-IN-TIME MEMBERSHIP",
    "canonical_hash": "2" * 64,
}


def _current_snapshot():
    security = SecurityReference.create(
        current_symbol="ACME",
        provider_symbol="ACME",
        issuer_name="Acme Corporation",
        listing_exchange="XNAS",
        listing_category="Q",
        security_type=SecurityType.COMMON_STOCK,
        common_stock=EvidenceState.YES,
        etf=EvidenceState.NO,
        adr=EvidenceState.NO,
        reit=EvidenceState.NO,
        test_issue=EvidenceState.NO,
        listing_status="NORMAL",
        round_lot_size=100,
        tradable=EvidenceState.UNKNOWN,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=EvidenceState.YES,
        cik=None,
        source_provider="NASDAQ_TRADER",
        source_receipt_id="r" * 10,
        source_timestamp=NOW,
        source_payload_hash="a" * 64,
    )
    reconciled = reconcile_security_references((security,))
    return create_current_equity_universe_snapshot(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=("r" * 10,),
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
    )


def test_legacy_pre_separation_payload_rehydrates_with_conservative_execution_state() -> None:
    payload_text = json.dumps(LEGACY_UNIVERSE_PAYLOAD)

    evidence = _decode_universe("universe-legacy-pre-separation", payload_text)

    assert evidence.schema_compatibility == SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1
    assert evidence.compatibility_blocker is None
    assert evidence.payload_schema_marker == LEGACY_PRE_EXECUTION_SEPARATION_SCHEMA_MARKER
    assert evidence.idempotency_key == "universe-legacy-pre-separation"

    snapshot = evidence.snapshot
    assert snapshot is not None
    # Exact snapshot ID, hashes, timestamps, and source lineage survive.
    assert snapshot.universe_id == LEGACY_UNIVERSE_PAYLOAD["universe_id"]
    assert snapshot.policy_id == LEGACY_UNIVERSE_PAYLOAD["policy_id"]
    assert snapshot.policy_hash == LEGACY_UNIVERSE_PAYLOAD["policy_hash"]
    assert snapshot.as_of_date == LEGACY_UNIVERSE_PAYLOAD["as_of_date"]
    assert snapshot.created_at == datetime(2026, 7, 28, 9, tzinfo=UTC)
    assert snapshot.source_receipt_ids == ()
    assert snapshot.canonical_hash == LEGACY_UNIVERSE_PAYLOAD["canonical_hash"]
    # The original research inclusion/exclusion result and its reasons are
    # preserved exactly -- not recomputed against today's policy.
    assert snapshot.research_eligible_count == 0
    assert snapshot.research_exclusion_counts == (("NOT_CONFIRMED_ALPACA_TRADABLE", 1),)
    # No positive or negative Alpaca capability is invented for a payload
    # that never captured any such evidence.
    assert snapshot.execution_capability_counts == (
        (ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN.value, 1),
    )
    assert snapshot.execution_capability_known_count == 0
    assert snapshot.execution_capability_unknown_count == 1
    assert len(snapshot.decisions) == 1
    decision = snapshot.decisions[0]
    assert decision.canonical_security_id == "apex_security_legacy00000000000000000000"
    assert decision.current_symbol == "LEGACY"
    assert decision.security_hash == "1" * 64
    assert decision.research_eligible is False
    assert decision.research_reasons == ("NOT_CONFIRMED_ALPACA_TRADABLE",)
    assert (
        decision.execution_capability
        is ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN
    )
    assert decision.execution_capability not in (
        ExecutionCapabilityState.ALPACA_TRADABLE,
        ExecutionCapabilityState.NOT_CONFIRMED_ALPACA_TRADABLE,
        ExecutionCapabilityState.ALPACA_CAPABILITY_NOT_OBSERVED,
    )

    # Decoding is a pure, deterministic function of the stored payload text.
    replay = _decode_universe("universe-legacy-pre-separation", payload_text)
    assert replay == evidence


def test_current_schema_payload_round_trips_exactly() -> None:
    snapshot = _current_snapshot()
    payload_text = canonical_json_bytes(snapshot).decode("utf-8")

    evidence = _decode_universe("universe-current-2026-07-29", payload_text)

    assert evidence.schema_compatibility == SCHEMA_COMPATIBILITY_CURRENT
    assert evidence.compatibility_blocker is None
    assert evidence.payload_schema_marker == APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1
    assert evidence.snapshot == snapshot


def test_payload_matching_neither_schema_is_retained_as_explicit_incompatible_evidence() -> None:
    payload_text = json.dumps({"universe_id": "mystery-universe", "unexpected_shape": True})

    evidence = _decode_universe("universe-neither-schema", payload_text)

    assert evidence.schema_compatibility == SCHEMA_COMPATIBILITY_INCOMPATIBLE
    assert evidence.snapshot is None
    assert evidence.idempotency_key == "universe-neither-schema"
    # A best-effort universe_id is still surfaced when the field happens to
    # be present and a string, even though the overall shape is unreadable.
    assert evidence.universe_id == "mystery-universe"
    assert evidence.compatibility_blocker
    assert "PAYLOAD_SHAPE_NOT_RECOGNIZED" in evidence.compatibility_blocker


def test_malformed_json_payload_does_not_raise() -> None:
    evidence = _decode_universe("universe-bad-json", "not-json{{{")

    assert evidence.schema_compatibility == SCHEMA_COMPATIBILITY_INCOMPATIBLE
    assert evidence.snapshot is None
    assert evidence.universe_id is None
    assert evidence.compatibility_blocker
    assert "PAYLOAD_JSON_INVALID" in evidence.compatibility_blocker


def test_non_object_json_payload_does_not_raise() -> None:
    evidence = _decode_universe("universe-json-array", json.dumps([1, 2, 3]))

    assert evidence.schema_compatibility == SCHEMA_COMPATIBILITY_INCOMPATIBLE
    assert evidence.snapshot is None
    assert evidence.compatibility_blocker == "PAYLOAD_IS_NOT_A_JSON_OBJECT"


def test_legacy_shaped_payload_missing_a_required_field_is_incompatible_not_crashing() -> None:
    """Even a payload that *looks* legacy by its top-level keys can be
    corrupted in a way this compatibility decoder cannot safely complete
    (here, a decision missing ``security_hash``). It must be surfaced as
    explicit incompatible evidence, never raised past this function and
    never guessed at.
    """
    broken = json.loads(json.dumps(LEGACY_UNIVERSE_PAYLOAD))
    del broken["decisions"][0]["security_hash"]

    evidence = _decode_universe("universe-legacy-corrupted", json.dumps(broken))

    assert evidence.schema_compatibility == SCHEMA_COMPATIBILITY_INCOMPATIBLE
    assert evidence.snapshot is None
    assert evidence.universe_id == LEGACY_UNIVERSE_PAYLOAD["universe_id"]
    assert evidence.payload_schema_marker == LEGACY_PRE_EXECUTION_SEPARATION_SCHEMA_MARKER
    assert evidence.compatibility_blocker
    assert "security_hash" in evidence.compatibility_blocker


@pytest.mark.parametrize("idempotency_key", ["a", "b"])
def test_decoding_never_mutates_or_depends_on_call_order(idempotency_key: str) -> None:
    payload_text = json.dumps(LEGACY_UNIVERSE_PAYLOAD)
    before = json.dumps(LEGACY_UNIVERSE_PAYLOAD)
    _decode_universe(idempotency_key, payload_text)
    assert json.dumps(LEGACY_UNIVERSE_PAYLOAD) == before
