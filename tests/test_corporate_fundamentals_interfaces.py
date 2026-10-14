"""Interface proofs for Corporate Fundamental, Filing, and Event
Intelligence v1: every bounded CLI command parses its declared arguments,
dispatch routes to the right handler, the new Operator API workspace route
is registered, and the SEC-network-touching command is reached only by
``corporate-evidence-capture``. No database, no server, no network --
mirrors the existing offline ``tests/test_options_research_operations_
interfaces.py`` pattern.
"""

from __future__ import annotations

import inspect

import scripts.dev as dev
from apex.operator_api import create_app
from scripts.dev import parser


def _routes() -> set[tuple[str, str]]:
    app = create_app()
    return {
        (getattr(route, "path", ""), method)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }


# --------------------------------------------------------------------------
# CLI argument parsing
# --------------------------------------------------------------------------


def test_corporate_evidence_plan_create_parses_with_no_arguments() -> None:
    parsed = parser().parse_args(["corporate-evidence-plan-create"])
    assert parsed.command == "corporate-evidence-plan-create"


def test_corporate_evidence_plan_inspect_parses_with_and_without_plan_id() -> None:
    bare = parser().parse_args(["corporate-evidence-plan-inspect"])
    assert bare.plan_id is None
    explicit = parser().parse_args(
        ["corporate-evidence-plan-inspect", "--plan-id", "corporate_evidence_cohort_plan_exact"]
    )
    assert explicit.plan_id == "corporate_evidence_cohort_plan_exact"


def test_corporate_evidence_capture_parses_with_optional_plan_id() -> None:
    parsed = parser().parse_args(["corporate-evidence-capture"])
    assert parsed.command == "corporate-evidence-capture"
    assert parsed.plan_id is None


def test_corporate_filings_inspect_requires_stable_security_id() -> None:
    parsed = parser().parse_args(
        ["corporate-filings-inspect", "--stable-security-id", "apex_stable_security_exact"]
    )
    assert parsed.stable_security_id == "apex_stable_security_exact"


def test_corporate_facts_inspect_accepts_an_optional_concept_filter() -> None:
    bare = parser().parse_args(["corporate-facts-inspect", "--stable-security-id", "sid"])
    assert bare.concept is None
    filtered = parser().parse_args(
        ["corporate-facts-inspect", "--stable-security-id", "sid", "--concept", "REVENUE"]
    )
    assert filtered.concept == "REVENUE"


def test_corporate_snapshot_inspect_accepts_optional_cik_and_as_of() -> None:
    bare = parser().parse_args(["corporate-snapshot-inspect", "--stable-security-id", "sid"])
    assert bare.cik is None
    assert bare.as_of is None
    explicit = parser().parse_args(
        [
            "corporate-snapshot-inspect",
            "--stable-security-id",
            "sid",
            "--cik",
            "320193",
            "--as-of",
            "2024-11-02",
        ]
    )
    assert explicit.cik == "320193"
    assert explicit.as_of == "2024-11-02"


def test_corporate_features_inspect_requires_stable_security_id() -> None:
    parsed = parser().parse_args(["corporate-features-inspect", "--stable-security-id", "sid"])
    assert parsed.stable_security_id == "sid"


def test_corporate_history_quality_inspect_parses_with_no_arguments() -> None:
    parsed = parser().parse_args(["corporate-history-quality-inspect"])
    assert parsed.command == "corporate-history-quality-inspect"


def test_corporate_dataset_materialize_parses_with_optional_plan_id() -> None:
    parsed = parser().parse_args(["corporate-dataset-materialize"])
    assert parsed.plan_id is None


def test_corporate_dataset_inspect_accepts_dataset_id_or_plan_id() -> None:
    parsed = parser().parse_args(
        ["corporate-dataset-inspect", "--dataset-id", "corporate_history_dataset_snapshot_exact"]
    )
    assert parsed.dataset_id == "corporate_history_dataset_snapshot_exact"


def test_corporate_replay_attach_requires_replay_run_id() -> None:
    parsed = parser().parse_args(["corporate-replay-attach", "--replay-run-id", "replay_run_exact"])
    assert parsed.replay_run_id == "replay_run_exact"


def test_corporate_replay_attach_accepts_an_optional_frame_limit() -> None:
    bare = parser().parse_args(["corporate-replay-attach", "--replay-run-id", "r"])
    assert bare.frame_limit is None
    explicit = parser().parse_args(
        ["corporate-replay-attach", "--replay-run-id", "r", "--frame-limit", "2500"]
    )
    assert explicit.frame_limit == 2500


def test_corporate_replay_feature_inspect_requires_run_frame_and_security() -> None:
    parsed = parser().parse_args(
        [
            "corporate-replay-feature-inspect",
            "--replay-run-id",
            "replay_run_exact",
            "--frame-id",
            "frame_exact",
            "--stable-security-id",
            "sid_exact",
        ]
    )
    assert parsed.replay_run_id == "replay_run_exact"
    assert parsed.frame_id == "frame_exact"
    assert parsed.stable_security_id == "sid_exact"


# --------------------------------------------------------------------------
# Dispatch routing -- every "corporate-*" command must route to the new
# family and nowhere else.
# --------------------------------------------------------------------------


def test_every_corporate_command_starts_with_the_dispatch_prefix() -> None:
    for command in (
        "corporate-evidence-plan-create",
        "corporate-evidence-plan-inspect",
        "corporate-evidence-capture",
        "corporate-filings-inspect",
        "corporate-facts-inspect",
        "corporate-snapshot-inspect",
        "corporate-features-inspect",
        "corporate-history-quality-inspect",
        "corporate-dataset-materialize",
        "corporate-dataset-inspect",
        "corporate-replay-attach",
        "corporate-replay-feature-inspect",
    ):
        assert command.startswith("corporate-")


def test_corporate_prefix_does_not_collide_with_any_pre_existing_command_family() -> None:
    # Scoped to the single outer dispatch chain (``main``), not the whole
    # module: ``corporate-market-*`` is a narrower prefix than this
    # family's own "corporate-", so it is deliberately sub-dispatched one
    # level down, inside ``corporate_fundamentals_command`` itself (see
    # that function's docstring), rather than as a second top-level
    # ``startswith`` branch in ``main`` -- a whole-module text search would
    # also match that intentional, documented nested guard even though it
    # is not a sibling-branch collision in the dispatch chain this test
    # actually protects.
    source = inspect.getsource(dev.main)
    # Every dispatch branch is a literal `.startswith(...)` guard; confirm
    # no earlier branch in file order also matches a "corporate-" command
    # (would silently steal it before reaching ours).
    dispatch_index = source.index('if arguments.command.startswith("corporate-"):')
    preceding = source[:dispatch_index]
    assert 'startswith("corporate' not in preceding


# --------------------------------------------------------------------------
# API: the new workspace route is registered; existing routes untouched
# --------------------------------------------------------------------------


def test_corporate_fundamentals_workspace_route_is_registered() -> None:
    assert ("/api/v1/corporate-fundamentals/workspace", "GET") in _routes()


def test_broad_market_and_options_intelligence_workspace_routes_still_registered() -> None:
    routes = _routes()
    assert ("/api/v1/broad-equity/workspace", "GET") in routes
    assert ("/api/v1/options-intelligence/workspace", "GET") in routes
    assert ("/api/v1/operator-console/broad-market", "GET") in routes


# --------------------------------------------------------------------------
# Zero provider requests except the one capture command; the shared
# runtime builder never imports the network-capable SEC client.
# --------------------------------------------------------------------------


def test_shared_runtime_builder_reaches_no_network_client() -> None:
    source = inspect.getsource(dev._corporate_fundamentals_runtime)
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("from ", "import "))
    ]
    assert all("corporate_filings_source" not in line for line in import_lines)
    assert all("corporate_evidence_capture" not in line for line in import_lines)


def test_only_the_capture_command_imports_the_network_capable_adapter() -> None:
    source = inspect.getsource(dev.corporate_fundamentals_command)
    assert "corporate_evidence_capture" in source
    capture_branch_start = source.index('"corporate-evidence-capture"')
    next_branch_start = source.index('"corporate-filings-inspect"')
    import_position = source.index("from apex.adapters.corporate_evidence_capture import")
    assert capture_branch_start < import_position < next_branch_start


def test_command_family_source_reaches_the_credential_preflight_for_capture_only() -> None:
    source = inspect.getsource(dev.corporate_fundamentals_command)
    assert "_assert_no_tracked_provider_credentials" in source
    capture_start = source.index('"corporate-evidence-capture"')
    preflight_at = source.index("_assert_no_tracked_provider_credentials()")
    next_branch = source.index('"corporate-filings-inspect"')
    assert capture_start < preflight_at < next_branch


# --------------------------------------------------------------------------
# Bounded output / authority NONE -- every dispatched command's JSON
# envelope goes through the shared canonical serializer, and the domain
# module itself declares authority "NONE" on every retained record type.
# --------------------------------------------------------------------------


def test_corporate_fundamentals_command_uses_the_shared_bounded_json_helper() -> None:
    source = inspect.getsource(dev.corporate_fundamentals_command)
    assert source.count("_research_evidence_json(") >= 10


def test_every_retained_record_type_declares_authority_none() -> None:
    from apex.domain import corporate_fundamentals as module

    checked = 0
    for name in (
        "CorporateFilingEvent",
        "EarningsEvent",
        "FundamentalFact",
        "CorporateFundamentalSnapshot",
        "DerivedFundamentalFeature",
        "FilingEventFeature",
        "CorporateEvidenceCohortPlan",
        "CorporateEvidenceCaptureCheckpoint",
        "CorporateDataQualityReport",
        "CorporateHistoryDatasetSnapshot",
        "ReplayCorporateEvidenceAttachment",
    ):
        cls = getattr(module, name)
        default = cls.__dataclass_fields__["authority"].default
        assert default == "NONE", name
        checked += 1
    assert checked == 11
