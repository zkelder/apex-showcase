"""Interface proofs for Research Calibration v2: every bounded CLI command
parses its declared arguments, and every bounded API route is registered
on the operator app -- mirrors the existing offline
``test_exact_cli_read_only_api_and_empty_workspace_surfaces`` pattern
(``tests/test_crypto_paper.py``): no database, no server, no network.
"""

from __future__ import annotations

from apex.cli import parser
from apex.operator_api import create_app


def _routes() -> set[tuple[str, str]]:
    app = create_app()
    return {
        (getattr(route, "path", ""), method)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }


# --------------------------------------------------------------------------
# CLI: calibration-v2 create/inspect; calibration-v2 run/resume/inspect;
# calibration-strata inspect; calibration-stability inspect;
# follow-up-requalify; evidence-grade-v2 inspect
# --------------------------------------------------------------------------


def test_calibration_v2_create_parses() -> None:
    parsed = parser().parse_args(
        [
            "calibration-v2-create",
            "--source-calibration-id",
            "research_calibration_plan_exact",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.source_calibration_id == "research_calibration_plan_exact"
    assert parsed.command == "calibration-v2-create"


def test_calibration_v2_inspect_parses() -> None:
    parsed = parser().parse_args(
        ["calibration-v2-inspect", "--calibration-id", "research_calibration_plan_v2_exact"]
    )
    assert parsed.calibration_id == "research_calibration_plan_v2_exact"


def test_calibration_v2_run_start_parses() -> None:
    parsed = parser().parse_args(
        [
            "calibration-v2-run-start",
            "--calibration-id",
            "cal-v2",
            "--source-calibration-id",
            "cal-v1",
            "--train-end",
            "2022-12-31",
            "--validation-end",
            "2024-06-30",
            "--maximum-specs",
            "20",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "calibration-v2-run-start"
    assert parsed.maximum_specs == 20


def test_calibration_v2_run_resume_parses() -> None:
    parsed = parser().parse_args(
        [
            "calibration-v2-run-resume",
            "--calibration-id",
            "cal-v2",
            "--source-calibration-id",
            "cal-v1",
            "--train-end",
            "2022-12-31",
            "--validation-end",
            "2024-06-30",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "calibration-v2-run-resume"
    assert parsed.maximum_specs == 20  # declared default


def test_calibration_v2_run_inspect_parses() -> None:
    parsed = parser().parse_args(["calibration-v2-run-inspect", "--calibration-id", "cal-v2"])
    assert parsed.calibration_id == "cal-v2"


def test_calibration_v2_recalibrate_agenda_parses() -> None:
    parsed = parser().parse_args(
        [
            "calibration-v2-recalibrate-agenda",
            "--calibration-id",
            "cal-v2",
            "--source-calibration-id",
            "cal-v1",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "calibration-v2-recalibrate-agenda"


def test_calibration_strata_inspect_parses_with_optional_research_design_filter() -> None:
    parsed = parser().parse_args(
        [
            "calibration-strata-inspect",
            "--calibration-id",
            "cal-v2",
            "--research-design",
            "CONDITIONAL",
        ]
    )
    assert parsed.research_design == "CONDITIONAL"
    without_filter = parser().parse_args(
        ["calibration-strata-inspect", "--calibration-id", "cal-v2"]
    )
    assert without_filter.research_design is None


def test_calibration_stability_inspect_parses() -> None:
    parsed = parser().parse_args(["calibration-stability-inspect", "--calibration-id", "cal-v2"])
    assert parsed.calibration_id == "cal-v2"


def test_follow_up_requalify_parses() -> None:
    parsed = parser().parse_args(
        [
            "follow-up-requalify",
            "--calibration-id",
            "cal-v2",
            "--source-calibration-id",
            "cal-v1",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "follow-up-requalify"


def test_evidence_grade_v2_inspect_parses() -> None:
    parsed = parser().parse_args(
        ["evidence-grade-v2-inspect", "--calibration-id", "cal-v2", "--subject-id", "hyp-1"]
    )
    assert parsed.subject_id == "hyp-1"


def test_v2_commands_are_distinct_from_v1_command_family() -> None:
    """Guards against the exact dispatch-prefix collision this milestone
    had to avoid: no v2 command name may start with a prefix v1's
    dispatcher already claims."""

    v1_prefixes = ("research-calibration-", "research-evidence-grade-", "follow-up-priority-")
    v2_commands = (
        "calibration-v2-create",
        "calibration-v2-inspect",
        "calibration-v2-run-start",
        "calibration-v2-run-resume",
        "calibration-v2-run-inspect",
        "calibration-v2-recalibrate-agenda",
        "calibration-strata-inspect",
        "calibration-stability-inspect",
        "follow-up-requalify",
        "evidence-grade-v2-inspect",
    )
    for command in v2_commands:
        assert command != "research-agenda-recalibrate"
        assert not command.startswith(v1_prefixes)


# --------------------------------------------------------------------------
# API: bounded research-calibration-v2 routes registered on the operator app
# --------------------------------------------------------------------------


def test_calibration_v2_api_routes_are_registered() -> None:
    routes = _routes()
    base = "/api/v1/securities/research-calibration-v2/plans"
    expected = (
        (f"{base}", "GET"),
        (f"{base}", "POST"),
        (f"{base}/{{calibration_id}}", "GET"),
        (f"{base}/{{calibration_id}}/run/start", "POST"),
        (f"{base}/{{calibration_id}}/run/resume", "POST"),
        (f"{base}/{{calibration_id}}/run", "GET"),
        (f"{base}/{{calibration_id}}/strata", "GET"),
        (f"{base}/{{calibration_id}}/stability", "GET"),
        (f"{base}/{{calibration_id}}/subjects/{{subject_id}}/evidence-grade", "GET"),
        (f"{base}/{{calibration_id}}/recalibrate-agenda", "POST"),
        (f"{base}/{{calibration_id}}/follow-ups/requalify", "POST"),
        (f"{base}/{{calibration_id}}/follow-ups/qualifications", "GET"),
    )
    for route in expected:
        assert route in routes, f"missing route {route}"


def test_calibration_v2_api_routes_share_the_existing_securities_namespace() -> None:
    """Section 22: "extend existing calibration/research workspaces. Do
    not create another dashboard" -- every v2 route lives under the same
    ``/api/v1/securities/...`` surface v1's calibration routes already
    use, never a new top-level namespace."""

    routes = _routes()
    v2_routes = {path for path, _method in routes if "research-calibration-v2" in path}
    assert v2_routes
    assert all(path.startswith("/api/v1/securities/") for path in v2_routes)


def test_calibration_v2_routes_are_additive_to_v1_routes() -> None:
    routes = _routes()
    v1_base = "/api/v1/securities/research-calibration/plans"
    assert (v1_base, "GET") in routes
    assert (v1_base, "POST") in routes
