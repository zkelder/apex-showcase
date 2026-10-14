from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_DEV_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dev.py"
_SPEC = importlib.util.spec_from_file_location("apex_dev_runner", _DEV_PATH)
assert _SPEC is not None and _SPEC.loader is not None
dev = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dev)


def test_verify_persistence_parser_accepts_only_explicit_capture_free_mode() -> None:
    arguments = dev.parser().parse_args(("verify-persistence", "--pytest-arg=--capture=no"))

    assert arguments.pytest_arg == ["--capture=no"]


def test_db_identity_command_is_registered() -> None:
    """`db-identity` (worktree verification reliability) is a real, argument-free
    subcommand -- the read-only "inspect the resolved target before mutation"
    diagnostic documented in docs/WORKTREE_VERIFICATION.md."""

    arguments = dev.parser().parse_args(("db-identity",))

    assert arguments.command == "db-identity"


def test_candidate_generation_commands_are_explicit_and_bounded() -> None:
    created = dev.parser().parse_args(
        (
            "candidate-search-create",
            "--experiment-id",
            "experiment",
            "--name",
            "Bounded search",
            "--dataset-id",
            "dataset",
            "--feature-schema-id",
            "schema",
            "--specification",
            "strategy-lab-initial-bounded-v1",
            "--objective",
            "BRIER",
            "--maximum-trials",
            "30",
            "--seed",
            "17",
            "--reason",
            "Predeclared research",
            "--idempotency-key",
            "candidate-plan-v1",
        )
    )
    assert created.maximum_trials == 30
    assert created.objective == "BRIER"
    run = dev.parser().parse_args(
        (
            "candidate-search-run",
            "--search-plan-id",
            "plan",
            "--requested-trials",
            "30",
            "--idempotency-key",
            "candidate-run-v1",
        )
    )
    assert run.requested_trials == 30
    assert dev.parser().parse_args(("mlflow-status",)).command == "mlflow-status"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "candidate-search-create",
                "--experiment-id",
                "experiment",
                "--name",
                "Unsafe",
                "--dataset-id",
                "dataset",
                "--feature-schema-id",
                "schema",
                "--specification",
                "arbitrary-python",
                "--objective",
                "CUSTOM",
                "--maximum-trials",
                "30",
                "--seed",
                "17",
                "--reason",
                "unsafe",
                "--idempotency-key",
                "unsafe",
            )
        )


def test_bounded_candidate_demo_migrates_application_database_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apex.adapters import candidate_generation_demo

    events: list[object] = []
    monkeypatch.setattr(dev, "migrate", lambda: events.append("migrate"))
    monkeypatch.setattr(
        candidate_generation_demo,
        "bounded_candidate_generation_demo",
        lambda *, local_root: events.append(("run", local_root)) or {"ok": True},
    )
    monkeypatch.setattr(
        dev,
        "_strategy_lab_json",
        lambda value: events.append(("report", value)),
    )
    dev.bounded_candidate_generation_demo()
    assert events == [
        "migrate",
        ("run", dev.ROOT / ".local"),
        ("report", {"ok": True}),
    ]


def test_persistence_pytest_argument_validation_is_narrow() -> None:
    assert dev._validate_persistence_pytest_args(()) == ()
    assert dev._validate_persistence_pytest_args(("--capture=no",)) == ("--capture=no",)

    with pytest.raises(SystemExit, match="only --pytest-arg=--capture=no is permitted"):
        dev._validate_persistence_pytest_args(("-q",))


def test_broad_equity_and_options_intelligence_commands_are_explicit_and_bounded() -> None:
    capture = dev.parser().parse_args(("broad-equity-reference-capture",))
    assert capture.command == "broad-equity-reference-capture"
    universe_create = dev.parser().parse_args(
        ("broad-equity-universe-create", "--idempotency-key", "universe-v1")
    )
    assert universe_create.idempotency_key == "universe-v1"
    data_capture = dev.parser().parse_args(
        (
            "broad-equity-data-capture",
            "--universe-id",
            "universe-v1",
            "--start",
            "2026-01-01",
            "--end",
            "2026-07-01",
            "--idempotency-key",
            "capture-v1",
        )
    )
    assert data_capture.idempotency_key == "capture-v1"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "broad-equity-data-capture",
                "--universe-id",
                "universe-v1",
                "--start",
                "2026-01-01",
                "--end",
                "2026-07-01",
            )
        )
    shadow_report = dev.parser().parse_args(("broad-equity-shadow-report", "--plan-id", "plan-1"))
    assert shadow_report.plan_id == "plan-1"

    capability = dev.parser().parse_args(("options-intelligence-capability",))
    assert capability.command == "options-intelligence-capability"
    reference_capture = dev.parser().parse_args(
        ("options-intelligence-reference-capture", "--universe-id", "universe-v1")
    )
    assert reference_capture.universe_id == "universe-v1"
    optionable = dev.parser().parse_args(
        ("options-intelligence-optionable-underlyings", "--universe-id", "universe-v1")
    )
    assert optionable.universe_id == "universe-v1"
    contracts = dev.parser().parse_args(
        ("options-intelligence-contracts", "--canonical-security-id", "security-acme")
    )
    assert contracts.canonical_security_id == "security-acme"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(("options-intelligence-contracts",))


def test_distinct_contract_count_ignores_in_call_duplicate_rows() -> None:
    """``options-intelligence-reference-capture`` must report how many
    *distinct* contracts a capture persisted, not the raw length of
    ``RecordOptionContracts.execute()``'s return value -- that call
    deliberately echoes its full input unchanged (including any duplicate
    official source row), so ``len()`` of it over-reports distinct persisted
    contracts by exactly the in-call duplicate count. Mirrors the exact
    duplicate-row fixture in
    ``test_save_contracts_chunks_large_batches_and_deduplicates_in_batch_repeats``.
    """
    from datetime import UTC, datetime

    from apex.adapters.options_reference import parse_nasdaq_options_directory

    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"  # a genuine duplicate row
        b"ACME|N|P|09/18/2026|95.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    contracts = parse_nasdaq_options_directory(
        payload=payload,
        received_at=datetime(2026, 7, 29, 15, tzinfo=UTC),
        underlying_ids={"ACME": "security-acme"},
    ).contracts
    assert len(contracts) == 3  # the parser retains every row, duplicate included

    # The buggy formula this regression guards against: counting the raw
    # RecordOptionContracts.execute() return value directly.
    assert len(contracts) != dev._distinct_contract_count(contracts)
    assert dev._distinct_contract_count(contracts) == 2


def test_reusable_completed_capture_only_matches_a_genuinely_completed_capture() -> None:
    """``broad-equity-reference-capture`` must be safe to invoke more than
    once per day: a COMPLETED capture already retained under today's exact
    deterministic capture_id is reused, never re-fetched (the official
    source can genuinely regenerate within a day, and a same-identity
    re-fetch that observed different bytes correctly fails closed rather
    than silently overwriting retained evidence -- reusing avoids ever
    reaching that path on an ordinary same-day repeat call). A RUNNING
    (partial) capture, or no capture at all, must never be treated as
    reusable."""
    from datetime import UTC, datetime

    from apex.adapters.security_reference import NASDAQ_LISTED_PATH, parse_nasdaq_symbol_directory
    from apex.application.research_evidence import (
        CaptureNasdaqDirectory,
        InMemoryResearchEvidenceRepository,
    )

    now = datetime(2026, 7, 29, 15, tzinfo=UTC)
    payload = (
        b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|"
        b"NextShares\n"
        b"ACME|Acme Corporation Common Stock|Q|N|N|100|N|N\n"
        b"BETA|Beta Corporation Common Stock|Q|N|N|100|N|N\n"
        b"File Creation Time: 0729202612:00|||||||\n"
    )
    parsed = parse_nasdaq_symbol_directory(
        payload=payload, source_path=NASDAQ_LISTED_PATH, received_at=now
    )
    repository = InMemoryResearchEvidenceRepository()

    assert dev._reusable_completed_capture(repository, "broad-equity-nasdaq-2026-07-29") is None

    partial = CaptureNasdaqDirectory(repository).start(
        capture_id="broad-equity-nasdaq-2026-07-29",
        evidence_class="GENUINE_PUBLIC",
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        raw_payload=payload,
        records=parsed.records,
        requested_at=now,
        ingested_at=now,
        chunk_size=1,
    )
    assert partial.status.value == "RUNNING"
    assert dev._reusable_completed_capture(repository, partial.capture_id) is None

    completed = CaptureNasdaqDirectory(repository).resume(
        capture_id=partial.capture_id,
        raw_payload=payload,
        records=parsed.records,
        resumed_at=now,
        chunk_size=10,
    )
    assert completed.status.value == "COMPLETED"
    reused = dev._reusable_completed_capture(repository, completed.capture_id)
    assert reused is not None
    assert reused.capture_id == completed.capture_id
    assert reused.status.value == "COMPLETED"


def test_securities_stage1_commands_are_explicit_and_bounded() -> None:
    imported = dev.parser().parse_args(
        (
            "securities-import-data",
            "--managed-source",
            "incoming/evidence.jsonl",
            "--dataset-name",
            "Managed evidence",
            "--idempotency-key",
            "import-v1",
        )
    )
    assert imported.managed_source == "incoming/evidence.jsonl"
    researched = dev.parser().parse_args(
        (
            "securities-research",
            "--campaign-id",
            "campaign",
            "--strategy-id",
            "securities-cash-baseline-v1",
            "--data-batch-id",
            "batch",
            "--start",
            "2026-01-01",
            "--end",
            "2026-12-31",
            "--idempotency-key",
            "research-v1",
        )
    )
    assert researched.strategy_id == "securities-cash-baseline-v1"
    conformance = dev.parser().parse_args(
        (
            "securities-conformance",
            "--research-run-id",
            "run",
            "--external-engine",
            "LEAN",
        )
    )
    assert conformance.external_engine == "LEAN"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "securities-conformance",
                "--research-run-id",
                "run",
                "--external-engine",
                "OTHER",
            )
        )


def test_securities_stage2_commands_are_explicit_and_bounded() -> None:
    paper_run = dev.parser().parse_args(
        (
            "securities-paper-run",
            "--campaign-id",
            "campaign",
            "--trial-id",
            "trial",
            "--strategy-id",
            "strategy",
            "--data-batch-id",
            "batch",
            "--start",
            "2026-01-01",
            "--end",
            "2026-12-31",
            "--idempotency-key",
            "paper-v1",
        )
    )
    assert paper_run.trial_id == "trial"
    assert (
        dev.parser()
        .parse_args(
            (
                "securities-exit",
                "--campaign-id",
                "campaign",
                "--as-of",
                "2026-07-26T12:00:00Z",
                "--reason",
                "OPERATOR_EXIT",
                "--idempotency-key",
                "exit-v1",
            )
        )
        .reason
        == "OPERATOR_EXIT"
    )
    conformance = dev.parser().parse_args(
        (
            "securities-paper-conformance",
            "--paper-run-id",
            "run",
            "--external-engine",
            "LEAN",
        )
    )
    assert conformance.external_engine == "LEAN"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "securities-exit",
                "--campaign-id",
                "campaign",
                "--as-of",
                "2026-07-26T12:00:00Z",
                "--reason",
                "SHORT",
                "--idempotency-key",
                "exit-v1",
            )
        )


def test_securities_baseline_commands_are_explicit_and_bounded() -> None:
    run = dev.parser().parse_args(
        (
            "securities-baseline-run",
            "--baseline-id",
            "baseline",
            "--campaign-id",
            "campaign",
            "--data-batch-id",
            "batch",
            "--start",
            "2026-01-01",
            "--end",
            "2026-12-31",
            "--simulator",
            "VECTORBT",
            "--idempotency-key",
            "baseline-run-v1",
        )
    )
    assert run.simulator == "VECTORBT"
    suite = dev.parser().parse_args(
        (
            "securities-baseline-suite",
            "--campaign-id",
            "campaign",
            "--data-batch-id",
            "batch",
            "--start",
            "2026-01-01",
            "--end",
            "2026-12-31",
            "--simulators",
            "APEX_RESEARCH,VECTORBT,LEAN",
            "--idempotency-key",
            "baseline-suite-v1",
        )
    )
    assert suite.simulators == "APEX_RESEARCH,VECTORBT,LEAN"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "securities-baseline-run",
                "--baseline-id",
                "baseline",
                "--campaign-id",
                "campaign",
                "--data-batch-id",
                "batch",
                "--start",
                "2026-01-01",
                "--end",
                "2026-12-31",
                "--simulator",
                "BROKER",
                "--idempotency-key",
                "baseline-run-v1",
            )
        )


def test_historical_data_commands_are_explicit_and_bounded() -> None:
    plan_create = dev.parser().parse_args(
        (
            "historical-data-plan-create",
            "--symbols",
            "AAPL,MSFT,SPY",
            "--etf-symbols",
            "SPY",
            "--start",
            "2020-01-01",
            "--end",
            "2020-12-31",
            "--chunk-size",
            "24",
            "--maximum-instruments",
            "50",
            "--maximum-requests",
            "50",
            "--maximum-bytes",
            "5000000",
            "--evidence-class",
            "HISTORICAL_PUBLIC",
            "--idempotency-key",
            "historical-plan-v1",
        )
    )
    assert plan_create.chunk_size == 24
    assert plan_create.evidence_class == "HISTORICAL_PUBLIC"
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "historical-data-plan-create",
                "--symbols",
                "AAPL",
                "--start",
                "2020-01-01",
                "--end",
                "2020-12-31",
                "--chunk-size",
                "24",
                "--maximum-instruments",
                "50",
                "--maximum-requests",
                "50",
                "--maximum-bytes",
                "5000000",
                "--evidence-class",
                "NOT_A_REAL_EVIDENCE_CLASS",
                "--idempotency-key",
                "historical-plan-v1",
            )
        )
    capture_run = dev.parser().parse_args(
        ("historical-data-capture-run", "--plan-id", "plan-1", "--ordinal", "2")
    )
    assert capture_run.ordinal == 2
    resume = dev.parser().parse_args(("historical-data-capture-run", "--plan-id", "plan-1"))
    assert resume.ordinal is None
    snapshot = dev.parser().parse_args(
        ("historical-data-snapshot-materialize", "--plan-id", "plan-1")
    )
    assert snapshot.plan_id == "plan-1"
    quality = dev.parser().parse_args(
        ("historical-data-quality-report", "--snapshot-id", "snapshot-1")
    )
    assert quality.snapshot_id == "snapshot-1"
    calculate = dev.parser().parse_args(
        (
            "historical-data-calculate",
            "--snapshot-id",
            "snapshot-1",
            "--stable-security-id",
            "stable-1",
            "--as-of",
            "2020-06-30",
            "--calculation",
            "rolling_volatility",
            "--window",
            "20",
        )
    )
    assert calculate.calculation == "rolling_volatility"
    assert calculate.window == 20
    with pytest.raises(SystemExit):
        dev.parser().parse_args(
            (
                "historical-data-calculate",
                "--snapshot-id",
                "snapshot-1",
                "--stable-security-id",
                "stable-1",
                "--as-of",
                "2020-06-30",
                "--calculation",
                "not_a_real_calculation",
            )
        )
    assert dev.parser().parse_args(("historical-data-workspace",)).command == (
        "historical-data-workspace"
    )
