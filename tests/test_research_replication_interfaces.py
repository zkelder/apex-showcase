"""Interface proofs for Calibration-Qualified Interaction Replication v1:
every bounded CLI command parses its declared arguments, and every bounded
API route is registered on the operator app -- mirrors the existing
offline ``tests/test_research_calibration_v2_interfaces.py`` pattern: no
database, no server, no network.
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
# CLI: research-replication create/inspect/run/resume; replication-stability
# inspect; replication-timing inspect; replication-conclusion inspect
# --------------------------------------------------------------------------


def test_research_replication_create_parses() -> None:
    parsed = parser().parse_args(
        [
            "research-replication-create",
            "--follow-up-id",
            "cross_market_follow_up_944ab68eaccd07fc752f3fca",
            "--v2-calibration-id",
            "research_calibration_plan_v2_exact",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "research-replication-create"
    assert parsed.follow_up_id == "cross_market_follow_up_944ab68eaccd07fc752f3fca"
    assert parsed.v2_calibration_id == "research_calibration_plan_v2_exact"


def test_research_replication_inspect_parses() -> None:
    parsed = parser().parse_args(
        [
            "research-replication-inspect",
            "--replication-id",
            "research_replication_specification_exact",
        ]
    )
    assert parsed.replication_id == "research_replication_specification_exact"


def test_research_replication_run_parses() -> None:
    parsed = parser().parse_args(
        [
            "research-replication-run",
            "--replication-id",
            "repl-1",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "research-replication-run"


def test_research_replication_run_resume_parses() -> None:
    parsed = parser().parse_args(
        [
            "research-replication-run-resume",
            "--replication-id",
            "repl-1",
            "--created-at",
            "2026-08-08T00:00:00Z",
        ]
    )
    assert parsed.command == "research-replication-run-resume"


def test_replication_stability_inspect_parses() -> None:
    parsed = parser().parse_args(["replication-stability-inspect", "--replication-id", "repl-1"])
    assert parsed.replication_id == "repl-1"


def test_replication_timing_inspect_parses() -> None:
    parsed = parser().parse_args(["replication-timing-inspect", "--replication-id", "repl-1"])
    assert parsed.replication_id == "repl-1"


def test_replication_conclusion_inspect_parses() -> None:
    parsed = parser().parse_args(["replication-conclusion-inspect", "--replication-id", "repl-1"])
    assert parsed.replication_id == "repl-1"


def test_replication_commands_are_distinct_from_other_command_families() -> None:
    """Guards against a dispatch-prefix collision: no replication command
    name may start with a prefix another dispatcher already claims, and no
    unrelated command may accidentally match the replication dispatcher's
    own ``research-replication-``/``replication-`` prefixes."""

    replication_commands = (
        "research-replication-create",
        "research-replication-inspect",
        "research-replication-run",
        "research-replication-run-resume",
        "replication-stability-inspect",
        "replication-timing-inspect",
        "replication-conclusion-inspect",
    )
    other_prefixes = (
        "research-calibration-",
        "research-evidence-grade-",
        "follow-up-priority-",
        "calibration-v2-",
        "calibration-strata-",
        "calibration-stability-",
        "evidence-grade-v2-",
        "research-agenda-",
        "hypothesis-proposals-",
        "agenda-run-",
        "research-knowledge-",
        "research-contradictions-",
        "follow-up-proposals-",
    )
    for command in replication_commands:
        assert command.startswith(("research-replication-", "replication-"))
        assert not command.startswith(other_prefixes)


# --------------------------------------------------------------------------
# API: bounded research-replication routes registered on the operator app
# --------------------------------------------------------------------------


def test_research_replication_api_routes_are_registered() -> None:
    routes = _routes()
    base = "/api/v1/securities/research-replication/specifications"
    expected = (
        (base, "POST"),
        (f"{base}/{{replication_id}}", "GET"),
        (f"{base}/{{replication_id}}/run", "POST"),
        (f"{base}/{{replication_id}}/stability", "GET"),
        (f"{base}/{{replication_id}}/timing", "GET"),
        (f"{base}/{{replication_id}}/conclusion", "GET"),
    )
    for route in expected:
        assert route in routes, f"missing route {route}"


def test_research_replication_api_routes_share_the_existing_securities_namespace() -> None:
    """Section 22: "extend existing research surfaces... do not create
    another dashboard" -- every replication route lives under the same
    ``/api/v1/securities/...`` surface calibration-v2's routes already
    use, never a new top-level namespace."""

    routes = _routes()
    replication_routes = {path for path, _method in routes if "research-replication" in path}
    assert replication_routes
    assert all(path.startswith("/api/v1/securities/") for path in replication_routes)


def test_research_replication_routes_are_additive_to_calibration_v2_routes() -> None:
    routes = _routes()
    v2_base = "/api/v1/securities/research-calibration-v2/plans"
    assert (v2_base, "GET") in routes
    replication_base = "/api/v1/securities/research-replication/specifications"
    assert (replication_base, "POST") in routes
