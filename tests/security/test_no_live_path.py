from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.fixtures import (
    REGISTERED_FIXTURE_IDS,
    ClassifiedRuleBoundSettlementFixture,
    FixtureFormatError,
    FixtureIntegrityError,
    FixtureNotAllowedError,
    FixtureReader,
    PartialFillCanceledExecutionScriptFixture,
    RuleBoundSettlementFixture,
    VoidContractRuleFixture,
)
from apex.domain.campaign import OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
from apex.domain.market import SettlementOutcome, void_settlement_term_content_hash
from apex.domain.values import ContentHash

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src"
FIXTURE_ROOT = ROOT / "fixtures"
_VALID_DEMO_ARGUMENTS = (
    "demo-core",
    "--fixture",
    "golden",
    "--capital",
    "1000.00",
    "--seed",
    "20260716",
)
_OFFLINE_RUNNER = """
import runpy
import socket
import sys

def denied(*args, **kwargs):
    raise RuntimeError("external networking is disabled by the security test")

socket.create_connection = denied
socket.getaddrinfo = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied
sys.argv = ["apex.cli", *sys.argv[1:]]
runpy.run_module("apex.cli", run_name="__main__", alter_sys=False)
"""


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    source = str(SOURCE_ROOT)
    environment["PYTHONPATH"] = source if not existing else os.pathsep.join((source, existing))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["APEX_OFFLINE"] = "1"
    return environment


def _run_offline_cli(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (sys.executable, "-c", _OFFLINE_RUNNER, *arguments),
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        check=False,
        timeout=30,
    )


def _rewrite_decision_and_manifest(
    copied_root: Path,
    fixture_id: str,
    decision: dict[str, object],
) -> None:
    _rewrite_fixture_file_and_manifest(
        copied_root,
        fixture_id,
        "decision_inputs.json",
        decision,
    )


def _rewrite_fixture_file_and_manifest(
    copied_root: Path,
    fixture_id: str,
    filename: str,
    document: dict[str, object],
) -> None:
    document_bytes = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    document_path = copied_root / fixture_id / filename
    document_path.write_bytes(document_bytes)
    manifest_path = copied_root / fixture_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][filename] = hashlib.sha256(document_bytes).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _rewrite_rule_bound_settlement_and_manifest(
    copied_root: Path,
    settlement: dict[str, object],
    *,
    fixture_id: str = "partial-fill-canceled-settled-no",
) -> None:
    if isinstance(settlement.get("raw_payload"), str):
        settlement["raw_payload_hash"] = hashlib.sha256(
            settlement["raw_payload"].encode("utf-8")
        ).hexdigest()
    evidence = {key: value for key, value in settlement.items() if key != "content_hash"}
    canonical_bytes = json.dumps(
        evidence,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    settlement["content_hash"] = hashlib.sha256(canonical_bytes).hexdigest()
    _rewrite_fixture_file_and_manifest(
        copied_root,
        fixture_id,
        "settlement_outcome.json",
        settlement,
    )


@pytest.mark.parametrize(
    "fixture_id",
    (
        "",
        ".",
        "..",
        "../golden",
        "..\\golden",
        "../no-proposal",
        "..\\no-proposal",
        "../policy-rejected",
        "..\\policy-rejected",
        "../invariant-rejected",
        "..\\invariant-rejected",
        "../partial-fill-canceled",
        "..\\partial-fill-canceled",
        "../partial-fill-canceled-settled-no",
        "..\\partial-fill-canceled-settled-no",
        "../partial-fill-canceled-settled-void",
        "..\\partial-fill-canceled-settled-void",
        "../unfilled-expired",
        "..\\unfilled-expired",
        "golden/decision_inputs.json",
        "golden\\decision_inputs.json",
        "no-proposal/decision_inputs.json",
        "no-proposal\\decision_inputs.json",
        "policy-rejected/decision_inputs.json",
        "policy-rejected\\decision_inputs.json",
        "invariant-rejected/decision_inputs.json",
        "invariant-rejected\\decision_inputs.json",
        "partial-fill-canceled/decision_inputs.json",
        "partial-fill-canceled\\decision_inputs.json",
        "partial-fill-canceled/execution_script.json",
        "partial-fill-canceled\\execution_script.json",
        "partial-fill-canceled-settled-no/decision_inputs.json",
        "partial-fill-canceled-settled-no\\decision_inputs.json",
        "partial-fill-canceled-settled-no/execution_script.json",
        "partial-fill-canceled-settled-no\\execution_script.json",
        "partial-fill-canceled-settled-no/settlement_outcome.json",
        "partial-fill-canceled-settled-no\\settlement_outcome.json",
        "partial-fill-canceled-settled-void/decision_inputs.json",
        "partial-fill-canceled-settled-void\\decision_inputs.json",
        "partial-fill-canceled-settled-void/execution_script.json",
        "partial-fill-canceled-settled-void\\execution_script.json",
        "partial-fill-canceled-settled-void/settlement_outcome.json",
        "partial-fill-canceled-settled-void\\settlement_outcome.json",
        "unfilled-expired/decision_inputs.json",
        "unfilled-expired\\decision_inputs.json",
        "unfilled-expired/execution_script.json",
        "unfilled-expired\\execution_script.json",
        "https://example.invalid/invariant-rejected",
        "https://example.invalid/partial-fill-canceled",
        "https://example.invalid/partial-fill-canceled-settled-no",
        "https://example.invalid/partial-fill-canceled-settled-void",
        "https://example.invalid/unfilled-expired",
        "GOLDEN",
        str((FIXTURE_ROOT / "golden").resolve()),
        str((FIXTURE_ROOT / "invariant-rejected").resolve()),
        str((FIXTURE_ROOT / "partial-fill-canceled").resolve()),
        str((FIXTURE_ROOT / "partial-fill-canceled-settled-no").resolve()),
        str((FIXTURE_ROOT / "partial-fill-canceled-settled-void").resolve()),
        str((FIXTURE_ROOT / "unfilled-expired").resolve()),
    ),
)
def test_fixture_reader_rejects_unregistered_and_path_like_ids(
    fixture_id: str, tmp_path: Path
) -> None:
    reader = FixtureReader(root=tmp_path)

    with pytest.raises(FixtureNotAllowedError):
        reader.load_decision(fixture_id)


def test_registered_fixture_ids_include_only_the_eight_offline_outcomes() -> None:
    assert REGISTERED_FIXTURE_IDS == (
        "golden",
        "invariant-rejected",
        "no-proposal",
        "partial-fill-canceled",
        "partial-fill-canceled-settled-no",
        "partial-fill-canceled-settled-void",
        "policy-rejected",
        "unfilled-expired",
    )


@pytest.mark.parametrize(
    ("fixture_id", "filename", "loader_name"),
    (
        ("golden", "decision_inputs.json", "load_decision"),
        ("golden", "settlement_outcome.json", "load_settlement"),
        ("no-proposal", "decision_inputs.json", "load_decision"),
        ("policy-rejected", "decision_inputs.json", "load_decision"),
        ("invariant-rejected", "decision_inputs.json", "load_decision"),
        ("partial-fill-canceled", "decision_inputs.json", "load_decision"),
        ("partial-fill-canceled", "execution_script.json", "load_decision"),
        (
            "partial-fill-canceled-settled-no",
            "decision_inputs.json",
            "load_decision",
        ),
        (
            "partial-fill-canceled-settled-no",
            "execution_script.json",
            "load_decision",
        ),
        (
            "partial-fill-canceled-settled-no",
            "settlement_outcome.json",
            "load_settlement",
        ),
        (
            "partial-fill-canceled-settled-void",
            "decision_inputs.json",
            "load_decision",
        ),
        (
            "partial-fill-canceled-settled-void",
            "execution_script.json",
            "load_decision",
        ),
        (
            "partial-fill-canceled-settled-void",
            "settlement_outcome.json",
            "load_settlement",
        ),
        ("unfilled-expired", "decision_inputs.json", "load_decision"),
        ("unfilled-expired", "execution_script.json", "load_decision"),
    ),
)
def test_fixture_reader_rejects_hash_tampering(
    fixture_id: str,
    filename: str,
    loader_name: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    reader = FixtureReader(root=copied_root)
    loader = getattr(reader, loader_name)

    loader(fixture_id)
    payload = copied_root / fixture_id / filename
    payload.write_bytes(payload.read_bytes() + b" ")

    with pytest.raises(FixtureIntegrityError):
        loader(fixture_id)


@pytest.mark.parametrize(
    "fixture_id",
    (
        "no-proposal",
        "policy-rejected",
        "invariant-rejected",
        "partial-fill-canceled",
        "unfilled-expired",
    ),
)
def test_decision_only_pack_has_no_settlement_capability(
    fixture_id: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    reader = FixtureReader(root=copied_root)
    decision = reader.load_decision(fixture_id)
    assert decision.fixture_id == fixture_id

    stray_settlement = copied_root / fixture_id / "settlement_outcome.json"
    stray_settlement.write_bytes((copied_root / "golden" / "settlement_outcome.json").read_bytes())
    with pytest.raises(FixtureNotAllowedError):
        reader.load_settlement(fixture_id)


@pytest.mark.parametrize(
    "fixture_id",
    (
        "no-proposal",
        "policy-rejected",
        "invariant-rejected",
        "partial-fill-canceled",
        "unfilled-expired",
    ),
)
def test_decision_only_manifest_rejects_a_settlement_entry(
    fixture_id: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    manifest_path = copied_root / fixture_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["settlement_outcome.json"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(FixtureFormatError, match="registered fixture spec"):
        FixtureReader(root=copied_root).load_decision(fixture_id)


def test_unfilled_expired_fixture_binds_split_no_fill_contract() -> None:
    fixture = FixtureReader().load_decision("unfilled-expired")
    pack = FIXTURE_ROOT / "unfilled-expired"
    decision = json.loads((pack / "decision_inputs.json").read_text(encoding="utf-8"))
    execution = json.loads((pack / "execution_script.json").read_text(encoding="utf-8"))
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))

    assert frozenset(manifest["files"]) == frozenset(
        ("decision_inputs.json", "execution_script.json")
    )
    assert "execution" not in decision
    assert decision["market_observation_source_id"] == "source-book-1"
    assert decision["maximum_operational_market_data_age_seconds"] == str(
        OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
    )
    assert decision["strategy_decision_at"] == "2026-07-15T15:00:01Z"
    assert decision["authorization_at"] == "2026-07-15T15:00:02Z"
    assert decision["order_side"] == "YES"
    assert decision["order_limit_price"] == "0.39"
    assert decision["order_expires_at"] == "2026-07-15T15:04:00Z"
    assert decision["fee_per_contract"] == "0.01"
    assert decision["fee_model_version"] == "synthetic-flat-per-contract-v1"
    assert decision["reference_estimate"]["expires_at"] == "2026-07-15T15:05:00Z"
    assert fixture.market_observation_source_id == "source-book-1"
    assert fixture.resting_limit_price == "0.39"
    assert fixture.order_expires_at == datetime(2026, 7, 15, 15, 4, tzinfo=UTC)

    assert execution["fixture_id"] == "unfilled-expired"
    assert execution["kind"] == "NONMARKETABLE_EXPIRE_UNFILLED"
    assert execution["side"] == "YES"
    assert execution["executable_yes_ask"] == "0.40"
    assert Decimal(decision["order_limit_price"]) < Decimal(execution["executable_yes_ask"])
    assert execution["opened_at"] == "2026-07-15T15:00:03Z"
    assert execution["expires_at"] == decision["order_expires_at"]
    assert execution["opened_sequence"] == "1"
    assert execution["expired_sequence"] == "2"
    assert not frozenset(execution).intersection(
        {
            "fill",
            "filled_at",
            "fill_price",
            "fill_quantity",
            "fill_sequence",
            "fee",
        }
    )


def test_partial_fill_canceled_fixture_binds_strict_split_contract() -> None:
    fixture = FixtureReader().load_decision("partial-fill-canceled")
    pack = FIXTURE_ROOT / "partial-fill-canceled"
    decision = json.loads((pack / "decision_inputs.json").read_text(encoding="utf-8"))
    execution = json.loads((pack / "execution_script.json").read_text(encoding="utf-8"))
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))

    assert frozenset(manifest["files"]) == frozenset(
        ("decision_inputs.json", "execution_script.json")
    )
    assert not (pack / "settlement_outcome.json").exists()
    assert "execution" not in decision
    assert decision["market_observation_source_id"] == "source-book-1"
    assert decision["order_side"] == "YES"
    assert decision["order_limit_price"] == "0.39"
    assert decision["order_expires_at"] == "2026-07-15T15:04:00Z"
    assert decision["fee_per_contract"] == "0.01"
    assert decision["logical_time"] == execution["canceled_at"]
    assert fixture.market_observation_source_id == "source-book-1"
    assert fixture.resting_limit_price == "0.39"
    assert fixture.order_expires_at == datetime(2026, 7, 15, 15, 4, tzinfo=UTC)

    assert frozenset(execution) == frozenset(
        (
            "fixture_id",
            "schema_version",
            "script_id",
            "kind",
            "side",
            "executable_yes_ask",
            "executable_no_ask",
            "visible_yes_quantity",
            "ordered_quantity",
            "fill_quantity",
            "remaining_quantity",
            "opened_at",
            "filled_at",
            "canceled_at",
            "opened_sequence",
            "fill_sequence",
            "canceled_sequence",
            "fill_price",
            "fee_per_contract",
        )
    )
    assert execution["fixture_id"] == "partial-fill-canceled"
    assert execution["script_id"] == "execution-partial-fill-canceled-v1"
    assert execution["kind"] == "PARTIAL_FILL_THEN_CANCEL"
    assert execution["side"] == "YES"
    assert execution["executable_yes_ask"] == "0.40"
    assert execution["executable_no_ask"] == "0.62"
    assert execution["visible_yes_quantity"] == "1000"
    assert execution["ordered_quantity"] == "250"
    assert execution["fill_quantity"] == "100"
    assert execution["remaining_quantity"] == "150"
    assert execution["fill_price"] == decision["order_limit_price"]
    assert execution["fee_per_contract"] == decision["fee_per_contract"]
    assert execution["opened_at"] == "2026-07-15T15:00:03Z"
    assert execution["filled_at"] == "2026-07-15T15:00:04Z"
    assert execution["canceled_at"] == "2026-07-15T15:00:05Z"
    assert (
        execution["opened_sequence"],
        execution["fill_sequence"],
        execution["canceled_sequence"],
    ) == ("1", "2", "3")
    assert Decimal(execution["fill_quantity"]) > 0
    assert Decimal(execution["fill_quantity"]) < Decimal(execution["ordered_quantity"])
    assert Decimal(execution["remaining_quantity"]) == (
        Decimal(execution["ordered_quantity"]) - Decimal(execution["fill_quantity"])
    )
    assert Decimal(execution["ordered_quantity"]) * (
        Decimal(execution["fill_price"]) + Decimal(execution["fee_per_contract"])
    ) == Decimal("100.00")
    assert Decimal(execution["fill_quantity"]) * Decimal(execution["fill_price"]) == Decimal(
        "39.00"
    )
    assert Decimal(execution["fill_quantity"]) * Decimal(execution["fee_per_contract"]) == Decimal(
        "1.00"
    )


def test_partial_fill_canceled_settled_no_fixture_binds_exact_evidence() -> None:
    reader = FixtureReader()
    fixture_id = "partial-fill-canceled-settled-no"
    decision_fixture = reader.load_decision(fixture_id)
    settlement_fixture = reader.load_settlement(fixture_id)
    pack = FIXTURE_ROOT / fixture_id
    decision = json.loads((pack / "decision_inputs.json").read_text(encoding="utf-8"))
    execution = json.loads((pack / "execution_script.json").read_text(encoding="utf-8"))
    settlement = json.loads((pack / "settlement_outcome.json").read_text(encoding="utf-8"))
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))

    assert frozenset(manifest["files"]) == frozenset(
        (
            "decision_inputs.json",
            "execution_script.json",
            "settlement_outcome.json",
        )
    )
    assert "execution" not in decision
    assert decision["logical_time"] == "2026-07-15T18:00:00Z"
    assert decision["order_limit_price"] == "0.39"
    assert decision["order_expires_at"] == "2026-07-15T15:04:00Z"
    assert execution["kind"] == "PARTIAL_FILL_THEN_CANCEL"
    assert execution["ordered_quantity"] == "250"
    assert execution["fill_quantity"] == "100"
    assert execution["remaining_quantity"] == "150"
    assert execution["opened_at"] == "2026-07-15T15:00:03Z"
    assert execution["filled_at"] == "2026-07-15T15:00:04Z"
    assert execution["canceled_at"] == "2026-07-15T15:00:05Z"

    assert frozenset(settlement) == frozenset(
        (
            "fixture_id",
            "schema_version",
            "source_id",
            "instrument_id",
            "rule_version_id",
            "rule_content_hash",
            "resolution_source",
            "source_event_at",
            "available_at",
            "settlement_value",
            "raw_payload",
            "raw_payload_hash",
            "content_hash",
        )
    )
    assert settlement["schema_version"] == "2"
    assert settlement["source_id"] == "settlement-partial-fill-canceled-no"
    assert settlement["instrument_id"] == decision["instrument"]["instrument_id"]
    assert settlement["rule_version_id"] == decision["contract_rule"]["rule_version_id"]
    assert settlement["rule_content_hash"] == decision["contract_rule"]["content_hash"]
    assert settlement["resolution_source"] == decision["contract_rule"]["resolution_source"]
    assert settlement["source_event_at"] == decision["contract_rule"]["determination_at"]
    assert settlement["available_at"] == "2026-07-15T17:00:03Z"
    assert settlement["settlement_value"] == decision["instrument"]["no_settlement_value"] == "0.00"
    assert isinstance(
        decision_fixture.execution,
        PartialFillCanceledExecutionScriptFixture,
    )
    assert decision_fixture.order_expires_at is not None
    assert datetime.fromisoformat(settlement["available_at"].replace("Z", "+00:00")) > max(
        decision_fixture.strategy_decision_at,
        decision_fixture.authorization_at,
        decision_fixture.execution.opened_at,
        decision_fixture.execution.filled_at,
        decision_fixture.execution.canceled_at,
        decision_fixture.order_expires_at,
    )
    assert (
        hashlib.sha256(settlement["raw_payload"].encode("utf-8")).hexdigest()
        == settlement["raw_payload_hash"]
    )
    evidence = {key: value for key, value in settlement.items() if key != "content_hash"}
    assert (
        hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        == settlement["content_hash"]
    )
    assert isinstance(settlement_fixture, RuleBoundSettlementFixture)
    assert settlement_fixture.instrument_id == decision_fixture.instrument.instrument_id
    assert settlement_fixture.rule_version_id == decision_fixture.contract_rule.rule_version_id
    assert settlement_fixture.rule_content_hash == decision_fixture.contract_rule.content_hash
    assert settlement_fixture.resolution_source == decision_fixture.contract_rule.resolution_source
    assert settlement_fixture.settlement_value == "0.00"


@pytest.mark.parametrize(
    ("field", "replacement", "expected_exception", "message"),
    (
        (
            "fixture_id",
            "golden",
            FixtureIntegrityError,
            "fixture_id does not match",
        ),
        (
            "source_id",
            "settlement-unregistered-no",
            FixtureIntegrityError,
            "source does not match",
        ),
        (
            "instrument_id",
            "SYNTH-OTHER-20260715",
            FixtureIntegrityError,
            "instrument does not match",
        ),
        (
            "rule_version_id",
            "rule-similar-but-different-v1",
            FixtureIntegrityError,
            "rule version does not match",
        ),
        (
            "rule_content_hash",
            "0" * 64,
            FixtureIntegrityError,
            "rule content hash does not match",
        ),
        (
            "resolution_source",
            "Different frozen resolution source",
            FixtureIntegrityError,
            "resolution source does not match",
        ),
        (
            "source_event_at",
            "2026-07-15T17:00:01Z",
            FixtureIntegrityError,
            "does not match rule determination",
        ),
        (
            "available_at",
            "2026-07-15T15:00:05Z",
            FixtureFormatError,
            "availability must be after its source event",
        ),
        (
            "available_at",
            "2026-07-15T17:00:02Z",
            FixtureIntegrityError,
            "availability lag does not match",
        ),
        (
            "available_at",
            "2026-07-15T18:00:01Z",
            FixtureFormatError,
            "cannot follow the frozen logical time",
        ),
        (
            "settlement_value",
            "1.01",
            FixtureFormatError,
            "bounded by the payout unit",
        ),
        (
            "settlement_value",
            "0.40",
            FixtureIntegrityError,
            "must equal the instrument NO settlement value",
        ),
    ),
)
def test_rule_bound_settlement_cross_file_tampering_fails_closed_after_rehash(
    field: str,
    replacement: object,
    expected_exception: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    path = copied_root / "partial-fill-canceled-settled-no" / "settlement_outcome.json"
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement[field] = replacement
    _rewrite_rule_bound_settlement_and_manifest(copied_root, settlement)

    with pytest.raises(expected_exception, match=message):
        FixtureReader(root=copied_root).load_settlement("partial-fill-canceled-settled-no")


def test_rule_bound_settlement_rejects_invalid_raw_payload_hash_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-no"
    filename = "settlement_outcome.json"
    path = copied_root / fixture_id / filename
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement["raw_payload_hash"] = "0" * 64
    evidence = {key: value for key, value in settlement.items() if key != "content_hash"}
    settlement["content_hash"] = hashlib.sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _rewrite_fixture_file_and_manifest(
        copied_root,
        fixture_id,
        filename,
        settlement,
    )

    with pytest.raises(FixtureIntegrityError, match="raw_payload content hash"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


def test_rule_bound_settlement_rejects_invalid_canonical_content_hash_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-no"
    filename = "settlement_outcome.json"
    path = copied_root / fixture_id / filename
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement["content_hash"] = "0" * 64
    _rewrite_fixture_file_and_manifest(
        copied_root,
        fixture_id,
        filename,
        settlement,
    )

    with pytest.raises(FixtureIntegrityError, match="canonical content hash"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


@pytest.mark.parametrize(
    ("filename", "extra_field", "value", "loader_name"),
    (
        (
            "execution_script.json",
            "second_fill_quantity",
            "1",
            "load_decision",
        ),
        (
            "settlement_outcome.json",
            "closing_order_id",
            "forbidden-closing-order",
            "load_settlement",
        ),
        (
            "settlement_outcome.json",
            "second_execution_event",
            "forbidden",
            "load_settlement",
        ),
    ),
)
def test_settled_partial_fill_schema_rejects_hidden_execution_or_order_fields(
    filename: str,
    extra_field: str,
    value: object,
    loader_name: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-no"
    path = copied_root / fixture_id / filename
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document[extra_field] = value
    _rewrite_fixture_file_and_manifest(
        copied_root,
        fixture_id,
        filename,
        document,
    )

    with pytest.raises(FixtureFormatError, match="fields mismatch"):
        getattr(FixtureReader(root=copied_root), loader_name)(fixture_id)


@pytest.mark.parametrize(
    ("field", "numeric_value"),
    (
        ("settlement_value", 0),
        ("available_at", 1700000000),
    ),
)
def test_rule_bound_settlement_rejects_json_numeric_tokens_after_rehash(
    field: str,
    numeric_value: int,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-no"
    filename = "settlement_outcome.json"
    path = copied_root / fixture_id / filename
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement[field] = numeric_value
    _rewrite_fixture_file_and_manifest(
        copied_root,
        fixture_id,
        filename,
        settlement,
    )

    with pytest.raises(FixtureFormatError, match="JSON numeric tokens"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


def test_partial_fill_canceled_settled_void_fixture_binds_exact_evidence() -> None:
    reader = FixtureReader()
    fixture_id = "partial-fill-canceled-settled-void"
    decision_fixture = reader.load_decision(fixture_id)
    settlement_fixture = reader.load_settlement(fixture_id)
    pack = FIXTURE_ROOT / fixture_id
    decision = json.loads((pack / "decision_inputs.json").read_text(encoding="utf-8"))
    execution = json.loads((pack / "execution_script.json").read_text(encoding="utf-8"))
    settlement = json.loads((pack / "settlement_outcome.json").read_text(encoding="utf-8"))
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))

    assert frozenset(manifest["files"]) == frozenset(
        (
            "decision_inputs.json",
            "execution_script.json",
            "settlement_outcome.json",
        )
    )
    assert "execution" not in decision
    assert decision["logical_time"] == "2026-07-15T18:00:00Z"
    assert decision["order_limit_price"] == "0.39"
    assert execution["kind"] == "PARTIAL_FILL_THEN_CANCEL"
    assert execution["ordered_quantity"] == "250"
    assert execution["fill_quantity"] == "100"
    assert execution["remaining_quantity"] == "150"
    assert (
        execution["opened_sequence"],
        execution["fill_sequence"],
        execution["canceled_sequence"],
    ) == ("1", "2", "3")

    assert frozenset(settlement) == frozenset(
        (
            "fixture_id",
            "schema_version",
            "source_id",
            "instrument_id",
            "rule_version_id",
            "rule_content_hash",
            "resolution_source",
            "outcome_classification",
            "source_event_at",
            "available_at",
            "settlement_value",
            "raw_payload",
            "raw_payload_hash",
            "content_hash",
        )
    )
    assert settlement["schema_version"] == "2"
    assert settlement["source_id"] == "settlement-partial-fill-canceled-void"
    assert settlement["instrument_id"] == decision["instrument"]["instrument_id"]
    assert settlement["rule_version_id"] == decision["contract_rule"]["rule_version_id"]
    assert settlement["rule_content_hash"] == decision["contract_rule"]["content_hash"]
    assert settlement["resolution_source"] == decision["contract_rule"]["resolution_source"]
    assert settlement["source_event_at"] == decision["contract_rule"]["determination_at"]
    assert settlement["outcome_classification"] == "VOID"
    assert settlement["settlement_value"] == decision["instrument"]["void_settlement_value"]
    void_term = decision["contract_rule"]["void_settlement_term"]
    assert void_term == {
        "outcome_classification": "VOID",
        "settlement_value": "0.40",
        "content_hash": "2d00611ca057d33025a8875b307955f95fa0501b25b768bc072ad300dfbb81d4",
    }
    assert settlement["outcome_classification"] == void_term["outcome_classification"]
    assert settlement["settlement_value"] == void_term["settlement_value"]
    assert settlement["settlement_value"] == "0.40"
    assert Decimal("0") < Decimal(settlement["settlement_value"]) <= Decimal("1")
    assert settlement["raw_payload"] == (
        "resolution:VOID;settlement_value=0.40;"
        "instrument=SYNTH-WEATHER-20260715;rule=rule-golden-v1;"
        "rule_hash=742ac2b67c6ebd52f63cc960e890a03cb98b86ba344c68a248a2c2d54d07747d;"
        "source=Frozen synthetic golden settlement record"
    )

    assert isinstance(
        decision_fixture.execution,
        PartialFillCanceledExecutionScriptFixture,
    )
    assert isinstance(decision_fixture.contract_rule, VoidContractRuleFixture)
    assert decision_fixture.order_expires_at is not None
    assert datetime.fromisoformat(settlement["available_at"].replace("Z", "+00:00")) > max(
        decision_fixture.strategy_decision_at,
        decision_fixture.authorization_at,
        decision_fixture.execution.opened_at,
        decision_fixture.execution.filled_at,
        decision_fixture.execution.canceled_at,
        decision_fixture.order_expires_at,
    )
    assert (
        hashlib.sha256(settlement["raw_payload"].encode("utf-8")).hexdigest()
        == settlement["raw_payload_hash"]
    )
    evidence = {key: value for key, value in settlement.items() if key != "content_hash"}
    assert (
        hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        == settlement["content_hash"]
    )
    assert isinstance(settlement_fixture, ClassifiedRuleBoundSettlementFixture)
    assert settlement_fixture.instrument_id == decision_fixture.instrument.instrument_id
    assert settlement_fixture.rule_version_id == decision_fixture.contract_rule.rule_version_id
    assert settlement_fixture.rule_content_hash == decision_fixture.contract_rule.content_hash
    assert settlement_fixture.resolution_source == decision_fixture.contract_rule.resolution_source
    assert settlement_fixture.outcome_classification == "VOID"
    assert settlement_fixture.settlement_value == "0.40"


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("classification", "must use VOID classification"),
        ("value", "exact rule text does not authorize"),
        ("term_hash", "term hash does not match"),
        ("missing", "fields mismatch"),
        ("hidden", "fields mismatch"),
    ),
)
def test_void_rule_term_tampering_fails_closed_after_rehash(
    case: str,
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    decision_path = copied_root / fixture_id / "decision_inputs.json"
    decision: dict[str, object] = json.loads(decision_path.read_text(encoding="utf-8"))
    rule = decision["contract_rule"]
    term = rule["void_settlement_term"]
    if case == "classification":
        term["outcome_classification"] = "NO"
    elif case == "value":
        term["settlement_value"] = "0.39"
        term["content_hash"] = void_settlement_term_content_hash(
            ContentHash(rule["content_hash"]),
            SettlementOutcome.VOID,
            "0.39",
        ).value
    elif case == "term_hash":
        term["content_hash"] = "0" * 64
    elif case == "missing":
        del rule["void_settlement_term"]
    else:
        term["universal_refund"] = "0.40"
    _rewrite_decision_and_manifest(copied_root, fixture_id, decision)

    with pytest.raises((FixtureFormatError, FixtureIntegrityError), match=message):
        FixtureReader(root=copied_root).load_decision(fixture_id)


def test_fully_rehashed_void_rule_term_must_match_the_instrument(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    decision_path = copied_root / fixture_id / "decision_inputs.json"
    decision: dict[str, object] = json.loads(decision_path.read_text(encoding="utf-8"))
    rule = decision["contract_rule"]
    term = rule["void_settlement_term"]
    rule["exact_text"] = rule["exact_text"].replace(
        "0.40 if it is VOID",
        "0.39 if it is VOID",
    )
    rule_hash = hashlib.sha256(rule["exact_text"].encode("utf-8")).hexdigest()
    rule["content_hash"] = rule_hash
    term["settlement_value"] = "0.39"
    term["content_hash"] = void_settlement_term_content_hash(
        ContentHash(rule_hash),
        SettlementOutcome.VOID,
        "0.39",
    ).value
    _rewrite_decision_and_manifest(copied_root, fixture_id, decision)

    with pytest.raises(FixtureIntegrityError, match="does not match the instrument"):
        FixtureReader(root=copied_root).load_decision(fixture_id)


def test_fully_rehashed_void_settlement_must_match_the_exact_rule_term(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    decision_path = copied_root / fixture_id / "decision_inputs.json"
    decision: dict[str, object] = json.loads(decision_path.read_text(encoding="utf-8"))
    rule = decision["contract_rule"]
    term = rule["void_settlement_term"]
    instrument = decision["instrument"]
    prior_rule_hash = rule["content_hash"]
    rule["exact_text"] = rule["exact_text"].replace(
        "0.40 if it is VOID",
        "0.39 if it is VOID",
    )
    rule_hash = hashlib.sha256(rule["exact_text"].encode("utf-8")).hexdigest()
    rule["content_hash"] = rule_hash
    term["settlement_value"] = "0.39"
    term["content_hash"] = void_settlement_term_content_hash(
        ContentHash(rule_hash),
        SettlementOutcome.VOID,
        "0.39",
    ).value
    instrument["void_settlement_value"] = "0.39"
    _rewrite_decision_and_manifest(copied_root, fixture_id, decision)

    settlement_path = copied_root / fixture_id / "settlement_outcome.json"
    settlement: dict[str, object] = json.loads(settlement_path.read_text(encoding="utf-8"))
    settlement["rule_content_hash"] = rule_hash
    settlement["raw_payload"] = settlement["raw_payload"].replace(
        prior_rule_hash,
        rule_hash,
    )
    _rewrite_rule_bound_settlement_and_manifest(
        copied_root,
        settlement,
        fixture_id=fixture_id,
    )

    with pytest.raises(FixtureIntegrityError, match="exact VOID rule term"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


@pytest.mark.parametrize(
    ("field", "replacement", "expected_exception", "message"),
    (
        ("fixture_id", "golden", FixtureIntegrityError, "fixture_id does not match"),
        (
            "source_id",
            "settlement-unregistered-void",
            FixtureIntegrityError,
            "source does not match",
        ),
        (
            "instrument_id",
            "SYNTH-OTHER-20260715",
            FixtureIntegrityError,
            "instrument does not match",
        ),
        (
            "rule_version_id",
            "rule-similar-but-different-v1",
            FixtureIntegrityError,
            "rule version does not match",
        ),
        (
            "rule_content_hash",
            "0" * 64,
            FixtureIntegrityError,
            "rule content hash does not match",
        ),
        (
            "resolution_source",
            "Different frozen resolution source",
            FixtureIntegrityError,
            "resolution source does not match",
        ),
        (
            "outcome_classification",
            "NO",
            FixtureIntegrityError,
            "outcome classification must be VOID",
        ),
        (
            "source_event_at",
            "2026-07-15T17:00:01Z",
            FixtureIntegrityError,
            "does not match rule determination",
        ),
        (
            "available_at",
            "2026-07-15T15:00:05Z",
            FixtureFormatError,
            "availability must be after its source event",
        ),
        (
            "available_at",
            "2026-07-15T17:00:02Z",
            FixtureIntegrityError,
            "availability lag does not match",
        ),
        (
            "available_at",
            "2026-07-15T18:00:01Z",
            FixtureFormatError,
            "cannot follow the frozen logical time",
        ),
        (
            "settlement_value",
            "0.00",
            FixtureFormatError,
            "VOID settlement value must be nonzero",
        ),
        (
            "settlement_value",
            "0.39",
            FixtureIntegrityError,
            "do not match the exact VOID rule term",
        ),
        (
            "settlement_value",
            "1.01",
            FixtureFormatError,
            "bounded by the payout unit",
        ),
    ),
)
def test_classified_rule_bound_void_cross_file_tampering_fails_closed_after_rehash(
    field: str,
    replacement: object,
    expected_exception: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    path = copied_root / fixture_id / "settlement_outcome.json"
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement[field] = replacement
    _rewrite_rule_bound_settlement_and_manifest(
        copied_root,
        settlement,
        fixture_id=fixture_id,
    )

    with pytest.raises(expected_exception, match=message):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


@pytest.mark.parametrize(
    "raw_payload",
    (
        (
            "resolution:NO;settlement_value=0.40;instrument=SYNTH-WEATHER-20260715;"
            "rule=rule-golden-v1;"
            "rule_hash=742ac2b67c6ebd52f63cc960e890a03cb98b86ba344c68a248a2c2d54d07747d;"
            "source=Frozen synthetic golden settlement record"
        ),
        (
            "resolution:VOID;settlement_value=0.39;instrument=SYNTH-WEATHER-20260715;"
            "rule=rule-golden-v1;"
            "rule_hash=742ac2b67c6ebd52f63cc960e890a03cb98b86ba344c68a248a2c2d54d07747d;"
            "source=Frozen synthetic golden settlement record"
        ),
        (
            "resolution:VOID;settlement_value=0.40;instrument=SYNTH-OTHER-20260715;"
            "rule=rule-golden-v1;"
            "rule_hash=742ac2b67c6ebd52f63cc960e890a03cb98b86ba344c68a248a2c2d54d07747d;"
            "source=Frozen synthetic golden settlement record"
        ),
    ),
)
def test_classified_void_raw_payload_is_semantically_bound_after_rehash(
    raw_payload: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    path = copied_root / fixture_id / "settlement_outcome.json"
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement["raw_payload"] = raw_payload
    _rewrite_rule_bound_settlement_and_manifest(
        copied_root,
        settlement,
        fixture_id=fixture_id,
    )

    with pytest.raises(FixtureIntegrityError, match="raw payload does not match"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


def test_classified_void_rejects_invalid_raw_payload_hash_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    filename = "settlement_outcome.json"
    path = copied_root / fixture_id / filename
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement["raw_payload_hash"] = "0" * 64
    evidence = {key: value for key, value in settlement.items() if key != "content_hash"}
    settlement["content_hash"] = hashlib.sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _rewrite_fixture_file_and_manifest(copied_root, fixture_id, filename, settlement)

    with pytest.raises(FixtureIntegrityError, match="raw_payload content hash"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


def test_classified_void_rejects_invalid_canonical_content_hash_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    filename = "settlement_outcome.json"
    path = copied_root / fixture_id / filename
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement["content_hash"] = "0" * 64
    _rewrite_fixture_file_and_manifest(copied_root, fixture_id, filename, settlement)

    with pytest.raises(FixtureIntegrityError, match="canonical content hash"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


@pytest.mark.parametrize(
    ("filename", "extra_field", "value", "loader_name"),
    (
        ("execution_script.json", "second_fill_quantity", "1", "load_decision"),
        ("settlement_outcome.json", "closing_order_id", "forbidden", "load_settlement"),
        ("settlement_outcome.json", "refund_quantity", "250", "load_settlement"),
        ("settlement_outcome.json", "universal_void_value", "0.40", "load_settlement"),
    ),
)
def test_classified_void_schema_rejects_hidden_execution_or_refund_fields(
    filename: str,
    extra_field: str,
    value: object,
    loader_name: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    path = copied_root / fixture_id / filename
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document[extra_field] = value
    _rewrite_fixture_file_and_manifest(copied_root, fixture_id, filename, document)

    with pytest.raises(FixtureFormatError, match="fields mismatch"):
        getattr(FixtureReader(root=copied_root), loader_name)(fixture_id)


@pytest.mark.parametrize(
    ("field", "numeric_value"),
    (
        ("settlement_value", 0.4),
        ("outcome_classification", 0),
        ("available_at", 1700000000),
    ),
)
def test_classified_void_settlement_rejects_json_numeric_tokens_after_rehash(
    field: str,
    numeric_value: int | float,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    fixture_id = "partial-fill-canceled-settled-void"
    filename = "settlement_outcome.json"
    path = copied_root / fixture_id / filename
    settlement: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    settlement[field] = numeric_value
    _rewrite_fixture_file_and_manifest(copied_root, fixture_id, filename, settlement)

    with pytest.raises(FixtureFormatError, match="JSON numeric tokens"):
        FixtureReader(root=copied_root).load_settlement(fixture_id)


@pytest.mark.parametrize(
    "fixture_id",
    (
        "golden",
        "no-proposal",
        "policy-rejected",
        "invariant-rejected",
        "partial-fill-canceled",
        "partial-fill-canceled-settled-no",
        "partial-fill-canceled-settled-void",
        "unfilled-expired",
    ),
)
def test_reference_estimate_source_must_be_available_at_cutoff_after_rehash(
    fixture_id: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    decision_path = copied_root / fixture_id / "decision_inputs.json"
    decision: dict[str, object] = json.loads(decision_path.read_text(encoding="utf-8"))
    estimate = decision["reference_estimate"]
    estimate_source_id = estimate["source_id"]
    estimate_record = next(
        record for record in decision["source_records"] if record["source_id"] == estimate_source_id
    )
    estimate_record["available_at"] = "2026-07-15T15:00:01Z"
    _rewrite_decision_and_manifest(copied_root, fixture_id, decision)

    with pytest.raises(FixtureFormatError, match="not available at the decision cutoff"):
        FixtureReader(root=copied_root).load_decision(fixture_id)


def test_unfilled_execution_sequences_must_start_at_one_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    filename = "execution_script.json"
    path = copied_root / "unfilled-expired" / filename
    execution: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    execution["opened_sequence"] = "2"
    execution["expired_sequence"] = "3"
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "unfilled-expired",
        filename,
        execution,
    )

    with pytest.raises(FixtureFormatError, match="exactly 1 then 2"):
        FixtureReader(root=copied_root).load_decision("unfilled-expired")


@pytest.mark.parametrize(
    ("filename", "field", "replacement", "expected_exception", "message"),
    (
        (
            "execution_script.json",
            "fixture_id",
            "golden",
            FixtureIntegrityError,
            "execution script fixture_id",
        ),
        (
            "execution_script.json",
            "executable_yes_ask",
            "0.41",
            FixtureIntegrityError,
            "book does not match",
        ),
        (
            "execution_script.json",
            "executable_no_ask",
            "0.63",
            FixtureIntegrityError,
            "book does not match",
        ),
        (
            "execution_script.json",
            "visible_yes_quantity",
            "999",
            FixtureIntegrityError,
            "book does not match",
        ),
        (
            "execution_script.json",
            "kind",
            "FULL_MARKETABLE_FILL",
            FixtureFormatError,
            "kind is invalid",
        ),
        (
            "execution_script.json",
            "side",
            "NO",
            FixtureFormatError,
            "side must be YES",
        ),
        (
            "execution_script.json",
            "expired_sequence",
            "3",
            FixtureFormatError,
            "sequences must be contiguous",
        ),
        (
            "execution_script.json",
            "opened_at",
            "2026-07-15T15:00:02Z",
            FixtureFormatError,
            "times are invalid",
        ),
        (
            "execution_script.json",
            "expires_at",
            "2026-07-15T15:03:59Z",
            FixtureFormatError,
            "logical time must equal",
        ),
        (
            "decision_inputs.json",
            "order_expires_at",
            "2026-07-15T15:03:59Z",
            FixtureIntegrityError,
            "expiration does not match",
        ),
        (
            "decision_inputs.json",
            "market_observation_source_id",
            "source-estimate-2",
            FixtureIntegrityError,
            "market observation source",
        ),
        (
            "decision_inputs.json",
            "maximum_operational_market_data_age_seconds",
            "301",
            FixtureFormatError,
            "sealed invariant",
        ),
        (
            "decision_inputs.json",
            "authorization_at",
            "2026-07-15T15:00:01Z",
            FixtureFormatError,
            "times are invalid",
        ),
        (
            "decision_inputs.json",
            "order_limit_price",
            "0.40",
            FixtureFormatError,
            "resting YES limit",
        ),
        (
            "decision_inputs.json",
            "order_limit_price",
            "0.395",
            FixtureFormatError,
            "resting YES limit",
        ),
    ),
)
def test_unfilled_cross_file_tampering_fails_closed_after_rehash(
    filename: str,
    field: str,
    replacement: object,
    expected_exception: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    path = copied_root / "unfilled-expired" / filename
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document[field] = replacement
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "unfilled-expired",
        filename,
        document,
    )

    with pytest.raises(expected_exception, match=message):
        FixtureReader(root=copied_root).load_decision("unfilled-expired")


def test_unfilled_execution_script_rejects_fill_fields_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    filename = "execution_script.json"
    path = copied_root / "unfilled-expired" / filename
    execution: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    execution["fill_price"] = "0.39"
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "unfilled-expired",
        filename,
        execution,
    )

    with pytest.raises(FixtureFormatError, match="fields mismatch"):
        FixtureReader(root=copied_root).load_decision("unfilled-expired")


@pytest.mark.parametrize(
    ("filename", "field", "numeric_value"),
    (
        ("decision_inputs.json", "order_limit_price", 0.39),
        ("execution_script.json", "opened_sequence", 1),
        ("execution_script.json", "executable_yes_ask", 0.4),
    ),
)
def test_unfilled_fixture_rejects_json_numeric_tokens_after_rehash(
    filename: str,
    field: str,
    numeric_value: int | float,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    path = copied_root / "unfilled-expired" / filename
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document[field] = numeric_value
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "unfilled-expired",
        filename,
        document,
    )

    with pytest.raises(FixtureFormatError, match="JSON numeric tokens"):
        FixtureReader(root=copied_root).load_decision("unfilled-expired")


@pytest.mark.parametrize(
    ("filename", "field", "replacement", "expected_exception", "message"),
    (
        (
            "execution_script.json",
            "fixture_id",
            "golden",
            FixtureIntegrityError,
            "execution script fixture_id",
        ),
        (
            "execution_script.json",
            "executable_yes_ask",
            "0.41",
            FixtureIntegrityError,
            "execution book does not match",
        ),
        (
            "execution_script.json",
            "executable_no_ask",
            "0.63",
            FixtureIntegrityError,
            "execution book does not match",
        ),
        (
            "execution_script.json",
            "visible_yes_quantity",
            "999",
            FixtureIntegrityError,
            "execution book does not match",
        ),
        (
            "execution_script.json",
            "kind",
            "FULL_MARKETABLE_FILL",
            FixtureFormatError,
            "kind is invalid",
        ),
        (
            "execution_script.json",
            "side",
            "NO",
            FixtureFormatError,
            "side must be YES",
        ),
        (
            "execution_script.json",
            "fee_per_contract",
            "0.02",
            FixtureIntegrityError,
            "execution fee does not match",
        ),
        (
            "execution_script.json",
            "fill_price",
            "0.38",
            FixtureIntegrityError,
            "fill price must equal",
        ),
        (
            "execution_script.json",
            "ordered_quantity",
            "251",
            FixtureFormatError,
            "partial-fill quantities",
        ),
        (
            "execution_script.json",
            "fill_quantity",
            "101",
            FixtureFormatError,
            "partial-fill quantities",
        ),
        (
            "execution_script.json",
            "remaining_quantity",
            "149",
            FixtureFormatError,
            "partial-fill quantities",
        ),
        (
            "execution_script.json",
            "fill_quantity",
            "250",
            FixtureFormatError,
            "partial-fill quantities",
        ),
        (
            "execution_script.json",
            "opened_sequence",
            "2",
            FixtureFormatError,
            "exactly 1, 2, 3",
        ),
        (
            "execution_script.json",
            "fill_sequence",
            "3",
            FixtureFormatError,
            "exactly 1, 2, 3",
        ),
        (
            "execution_script.json",
            "canceled_sequence",
            "4",
            FixtureFormatError,
            "exactly 1, 2, 3",
        ),
        (
            "execution_script.json",
            "opened_at",
            "2026-07-15T15:00:02Z",
            FixtureFormatError,
            "times are invalid",
        ),
        (
            "execution_script.json",
            "filled_at",
            "2026-07-15T15:00:03Z",
            FixtureFormatError,
            "strictly open, fill, then cancel",
        ),
        (
            "execution_script.json",
            "canceled_at",
            "2026-07-15T15:00:04Z",
            FixtureFormatError,
            "strictly open, fill, then cancel",
        ),
        (
            "execution_script.json",
            "canceled_at",
            "2026-07-15T15:00:06Z",
            FixtureFormatError,
            "logical time must equal",
        ),
        (
            "decision_inputs.json",
            "authorization_at",
            "2026-07-15T15:00:01Z",
            FixtureFormatError,
            "times are invalid",
        ),
        (
            "decision_inputs.json",
            "logical_time",
            "2026-07-15T15:00:06Z",
            FixtureFormatError,
            "logical time must equal",
        ),
        (
            "decision_inputs.json",
            "order_expires_at",
            "2026-07-15T15:00:05Z",
            FixtureFormatError,
            "times are invalid",
        ),
        (
            "decision_inputs.json",
            "order_expires_at",
            "2026-07-15T15:06:00Z",
            FixtureFormatError,
            "exceeds proposal availability",
        ),
        (
            "decision_inputs.json",
            "order_limit_price",
            "0.38",
            FixtureIntegrityError,
            "fill price must equal",
        ),
        (
            "decision_inputs.json",
            "order_limit_price",
            "0.40",
            FixtureFormatError,
            "partial-fill YES limit",
        ),
        (
            "decision_inputs.json",
            "order_limit_price",
            "0.395",
            FixtureFormatError,
            "partial-fill YES limit",
        ),
        (
            "decision_inputs.json",
            "fee_per_contract",
            "0.02",
            FixtureIntegrityError,
            "execution fee does not match",
        ),
        (
            "decision_inputs.json",
            "order_side",
            "NO",
            FixtureFormatError,
            "order side must be YES",
        ),
    ),
)
def test_partial_fill_cross_file_tampering_fails_closed_after_rehash(
    filename: str,
    field: str,
    replacement: object,
    expected_exception: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    path = copied_root / "partial-fill-canceled" / filename
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document[field] = replacement
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "partial-fill-canceled",
        filename,
        document,
    )

    with pytest.raises(expected_exception, match=message):
        FixtureReader(root=copied_root).load_decision("partial-fill-canceled")


@pytest.mark.parametrize(
    ("section", "field", "replacement", "expected_exception", "message"),
    (
        (
            "instrument",
            "yes_ask",
            "0.41",
            FixtureIntegrityError,
            "execution book does not match",
        ),
        (
            "instrument",
            "no_ask",
            "0.63",
            FixtureIntegrityError,
            "execution book does not match",
        ),
        (
            "instrument",
            "visible_yes_quantity",
            "999",
            FixtureIntegrityError,
            "execution book does not match",
        ),
        (
            "instrument",
            "price_tick",
            "0.02",
            FixtureFormatError,
            "partial-fill YES limit",
        ),
        (
            "instrument",
            "quantity_increment",
            "3",
            FixtureFormatError,
            "partial-fill quantities",
        ),
        (
            "reference_estimate",
            "expires_at",
            "2026-07-15T15:03:59Z",
            FixtureFormatError,
            "exceeds proposal availability",
        ),
    ),
)
def test_partial_fill_nested_decision_tampering_fails_closed_after_rehash(
    section: str,
    field: str,
    replacement: object,
    expected_exception: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    path = copied_root / "partial-fill-canceled" / "decision_inputs.json"
    decision: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    nested = decision[section]
    assert isinstance(nested, dict)
    nested[field] = replacement
    _rewrite_decision_and_manifest(copied_root, "partial-fill-canceled", decision)

    with pytest.raises(expected_exception, match=message):
        FixtureReader(root=copied_root).load_decision("partial-fill-canceled")


@pytest.mark.parametrize(
    ("extra_field", "value"),
    (
        ("second_fill_quantity", "1"),
        ("second_fill_price", "0.39"),
        ("second_fill_at", "2026-07-15T15:00:04Z"),
        ("second_fill_sequence", "3"),
        (
            "fills",
            (
                {
                    "quantity": "100",
                    "price": "0.39",
                    "sequence": "2",
                },
            ),
        ),
    ),
)
def test_partial_fill_execution_schema_rejects_hidden_second_fill_after_rehash(
    extra_field: str,
    value: object,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    filename = "execution_script.json"
    path = copied_root / "partial-fill-canceled" / filename
    execution: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    execution[extra_field] = value
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "partial-fill-canceled",
        filename,
        execution,
    )

    with pytest.raises(FixtureFormatError, match="fields mismatch"):
        FixtureReader(root=copied_root).load_decision("partial-fill-canceled")


def test_partial_fill_execution_schema_rejects_a_missing_required_field_after_rehash(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    filename = "execution_script.json"
    path = copied_root / "partial-fill-canceled" / filename
    execution: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    del execution["remaining_quantity"]
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "partial-fill-canceled",
        filename,
        execution,
    )

    with pytest.raises(FixtureFormatError, match="fields mismatch"):
        FixtureReader(root=copied_root).load_decision("partial-fill-canceled")


@pytest.mark.parametrize(
    ("filename", "field", "numeric_value"),
    (
        ("decision_inputs.json", "order_limit_price", 0.39),
        ("decision_inputs.json", "fee_per_contract", 0.01),
        ("decision_inputs.json", "maximum_operational_market_data_age_seconds", 300),
        ("execution_script.json", "ordered_quantity", 250),
        ("execution_script.json", "fill_quantity", 100),
        ("execution_script.json", "fill_price", 0.39),
        ("execution_script.json", "opened_sequence", 1),
    ),
)
def test_partial_fill_fixture_rejects_json_numeric_tokens_after_rehash(
    filename: str,
    field: str,
    numeric_value: int | float,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    path = copied_root / "partial-fill-canceled" / filename
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    document[field] = numeric_value
    _rewrite_fixture_file_and_manifest(
        copied_root,
        "partial-fill-canceled",
        filename,
        document,
    )

    with pytest.raises(FixtureFormatError, match="JSON numeric tokens"):
        FixtureReader(root=copied_root).load_decision("partial-fill-canceled")


def test_invariant_rejected_fixture_binds_strict_operational_context() -> None:
    fixture = FixtureReader().load_decision("invariant-rejected")
    decision = json.loads(
        (FIXTURE_ROOT / "invariant-rejected" / "decision_inputs.json").read_text(encoding="utf-8")
    )

    assert decision["maximum_operational_market_data_age_seconds"] == str(
        OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
    )
    assert decision["market_observation_source_id"] == "source-book-1"
    assert fixture.market_observation_source_id == "source-book-1"
    assert fixture.authorization_at == datetime(2026, 7, 15, 15, 3, 2, tzinfo=UTC)
    assert fixture.execution.opened_at == datetime(2026, 7, 15, 15, 0, 1, tzinfo=UTC)

    market_record = next(
        record
        for record in fixture.source_records
        if record.source_id == fixture.market_observation_source_id
    )
    estimate_record = next(
        record for record in fixture.source_records if record.source_id == "source-estimate-2"
    )
    observed_age = fixture.authorization_at - market_record.available_at
    assert market_record.available_at <= fixture.decision_cutoff
    assert estimate_record.available_at > market_record.available_at
    assert observed_age == timedelta(seconds=OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS + 1)
    assert fixture.authorization_at - timedelta(
        seconds=1
    ) - market_record.available_at == timedelta(seconds=OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS)


@pytest.mark.parametrize(
    ("field", "replacement", "expected_exception", "message"),
    (
        (
            "market_observation_source_id",
            "source-estimate-2",
            FixtureIntegrityError,
            "market observation source",
        ),
        (
            "maximum_operational_market_data_age_seconds",
            "301",
            FixtureFormatError,
            "sealed invariant",
        ),
        (
            "maximum_operational_market_data_age_seconds",
            300,
            FixtureFormatError,
            "JSON numeric tokens",
        ),
        (
            "authorization_at",
            "2026-07-15T15:00:00Z",
            FixtureFormatError,
            "cannot precede",
        ),
    ),
)
def test_invariant_operational_context_tampering_fails_closed_after_rehash(
    field: str,
    replacement: object,
    expected_exception: type[Exception],
    message: str,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    decision_path = copied_root / "invariant-rejected" / "decision_inputs.json"
    decision: dict[str, object] = json.loads(decision_path.read_text(encoding="utf-8"))
    decision[field] = replacement
    _rewrite_decision_and_manifest(copied_root, "invariant-rejected", decision)

    with pytest.raises(expected_exception, match=message):
        FixtureReader(root=copied_root).load_decision("invariant-rejected")


def test_policy_rejected_fixture_verifies_inline_policy_hash(tmp_path: Path) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    decision_path = copied_root / "policy-rejected" / "decision_inputs.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["policy"]["max_capital_lockup_seconds"] = "3601"
    decision_bytes = json.dumps(decision, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    decision_path.write_bytes(decision_bytes)
    manifest_path = copied_root / "policy-rejected" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["decision_inputs.json"] = hashlib.sha256(decision_bytes).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FixtureIntegrityError, match="policy content hash"):
        FixtureReader(root=copied_root).load_decision("policy-rejected")


def test_schema_v1_policy_age_cannot_configure_operational_freshness(
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    decision_path = copied_root / "golden" / "decision_inputs.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    policy = decision["policy"]
    policy["max_data_age_seconds"] = "301"
    policy_payload = "|".join(
        (
            policy["policy_version_id"],
            policy["profile"],
            policy["requested_risk_fraction"],
            policy["max_trade_loss_fraction"],
            policy["max_open_loss_fraction"],
            policy["min_synthetic_edge"],
            policy["max_spread"],
            policy["max_data_age_seconds"],
            policy["max_capital_lockup_seconds"],
        )
    )
    policy["content_hash"] = hashlib.sha256(policy_payload.encode("utf-8")).hexdigest()
    _rewrite_decision_and_manifest(copied_root, "golden", decision)

    with pytest.raises(FixtureFormatError, match="sealed operational invariant"):
        FixtureReader(root=copied_root).load_decision("golden")


@pytest.mark.parametrize(("field", "numeric_value"), (("yes_ask", 0.4), ("payout_unit", 1)))
def test_fixture_reader_rejects_authoritative_json_numbers(
    field: str,
    numeric_value: int | float,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, copied_root)
    decision_path = copied_root / "golden" / "decision_inputs.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["instrument"][field] = numeric_value
    decision_bytes = json.dumps(decision, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    decision_path.write_bytes(decision_bytes)
    manifest_path = copied_root / "golden" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["decision_inputs.json"] = hashlib.sha256(decision_bytes).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(FixtureFormatError):
        FixtureReader(root=copied_root).load_decision("golden")


@pytest.mark.parametrize(
    "arguments",
    (
        (*_VALID_DEMO_ARGUMENTS, "--mode", "live"),
        (*_VALID_DEMO_ARGUMENTS, "--execution-mode", "DEMO"),
        ("live",),
        ("submit-order",),
        ("trade",),
    ),
)
def test_cli_rejects_live_or_external_execution_modes(arguments: tuple[str, ...]) -> None:
    result = _run_offline_cli(*arguments)

    assert result.returncode != 0
    assert result.stdout == b""


@pytest.mark.parametrize(
    "fixture_id",
    (
        "../golden",
        "..\\golden",
        "../no-proposal",
        "..\\no-proposal",
        "../policy-rejected",
        "..\\policy-rejected",
        "../invariant-rejected",
        "..\\invariant-rejected",
        "../partial-fill-canceled",
        "..\\partial-fill-canceled",
        "../partial-fill-canceled-settled-no",
        "..\\partial-fill-canceled-settled-no",
        "../partial-fill-canceled-settled-void",
        "..\\partial-fill-canceled-settled-void",
        "../unfilled-expired",
        "..\\unfilled-expired",
        "golden/decision_inputs.json",
        "no-proposal/decision_inputs.json",
        "policy-rejected/decision_inputs.json",
        "invariant-rejected/decision_inputs.json",
        "partial-fill-canceled/decision_inputs.json",
        "partial-fill-canceled/execution_script.json",
        "partial-fill-canceled-settled-no/decision_inputs.json",
        "partial-fill-canceled-settled-no/execution_script.json",
        "partial-fill-canceled-settled-no/settlement_outcome.json",
        "partial-fill-canceled-settled-void/decision_inputs.json",
        "partial-fill-canceled-settled-void/execution_script.json",
        "partial-fill-canceled-settled-void/settlement_outcome.json",
        "unfilled-expired/decision_inputs.json",
        "unfilled-expired/execution_script.json",
        "https://example.invalid/invariant-rejected",
        "https://example.invalid/partial-fill-canceled",
        "https://example.invalid/partial-fill-canceled-settled-no",
        "https://example.invalid/partial-fill-canceled-settled-void",
        "https://example.invalid/unfilled-expired",
        str((FIXTURE_ROOT / "golden").resolve()),
        str((FIXTURE_ROOT / "no-proposal").resolve()),
        str((FIXTURE_ROOT / "policy-rejected").resolve()),
        str((FIXTURE_ROOT / "invariant-rejected").resolve()),
        str((FIXTURE_ROOT / "partial-fill-canceled").resolve()),
        str((FIXTURE_ROOT / "partial-fill-canceled-settled-no").resolve()),
        str((FIXTURE_ROOT / "partial-fill-canceled-settled-void").resolve()),
        str((FIXTURE_ROOT / "unfilled-expired").resolve()),
    ),
)
def test_cli_rejects_fixture_paths(fixture_id: str) -> None:
    arguments = list(_VALID_DEMO_ARGUMENTS)
    arguments[arguments.index("golden")] = fixture_id

    result = _run_offline_cli(*arguments)

    assert result.returncode != 0
    assert result.stdout == b""


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _flatten_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _flatten_strings(child)]
    return []


@pytest.mark.parametrize(
    "fixture_id",
    (
        "golden",
        "no-proposal",
        "policy-rejected",
        "invariant-rejected",
        "partial-fill-canceled",
        "partial-fill-canceled-settled-no",
        "partial-fill-canceled-settled-void",
        "unfilled-expired",
    ),
)
def test_demo_is_deterministic_with_external_networking_disabled(fixture_id: str) -> None:
    arguments = list(_VALID_DEMO_ARGUMENTS)
    arguments[arguments.index("golden")] = fixture_id
    first = _run_offline_cli(*arguments)
    second = _run_offline_cli(*arguments)

    assert first.returncode == 0, first.stderr.decode("utf-8", errors="replace")
    assert second.returncode == 0, second.stderr.decode("utf-8", errors="replace")
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr
    assert first.stdout.endswith(b"\n")

    report_bytes = first.stdout[:-1]
    report = json.loads(report_bytes)
    expected_canonical = json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert report_bytes == expected_canonical

    report_strings = _flatten_strings(report)
    assert "SIMULATION_ONLY" in report_strings
    assert "SYNTHETIC" in report_strings
    assert "NONE" in report_strings

    expected_hash = hashlib.sha256(first.stdout).hexdigest().encode("ascii")
    assert expected_hash in first.stderr.lower()
