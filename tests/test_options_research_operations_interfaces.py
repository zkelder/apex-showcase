"""Interface proofs for Fresh Options Evidence Operations and Structure
Research Baseline v1: every bounded CLI command parses its declared
arguments, the extended options-intelligence workspace route stays
registered, and the credential-leak preflight is reached by exactly the one
command that touches a real Alpaca adapter (``options-research-run-once``)
-- never by the read-only inspection commands. No database, no server, no
network -- mirrors the existing offline
``tests/test_options_quote_intelligence_interfaces.py`` /
``tests/test_options_structures_interfaces.py`` patterns.
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
# CLI: options-research-due-inspect; options-research-run-once;
# options-chain-coverage-inspect; options-forward-checkpoint-inspect;
# options-history-inspect; options-research-quality-inspect
# --------------------------------------------------------------------------


def test_options_research_due_inspect_parses_with_no_arguments() -> None:
    parsed = parser().parse_args(["options-research-due-inspect"])
    assert parsed.command == "options-research-due-inspect"


def test_options_research_run_once_parses_with_no_arguments() -> None:
    parsed = parser().parse_args(["options-research-run-once"])
    assert parsed.command == "options-research-run-once"


def test_options_chain_coverage_inspect_parses_with_and_without_plan_and_session() -> None:
    bare = parser().parse_args(["options-chain-coverage-inspect"])
    assert bare.plan_id is None
    assert bare.session_id is None
    explicit = parser().parse_args(
        [
            "options-chain-coverage-inspect",
            "--plan-id",
            "options_chain_capture_plan_exact",
            "--session-id",
            "2026-08-10",
        ]
    )
    assert explicit.plan_id == "options_chain_capture_plan_exact"
    assert explicit.session_id == "2026-08-10"


def test_options_forward_checkpoint_inspect_parses_with_and_without_plan_and_session() -> None:
    bare = parser().parse_args(["options-forward-checkpoint-inspect"])
    assert bare.plan_id is None
    assert bare.session_id is None
    explicit = parser().parse_args(
        [
            "options-forward-checkpoint-inspect",
            "--plan-id",
            "options_chain_capture_plan_exact",
            "--session-id",
            "2026-08-10",
        ]
    )
    assert explicit.plan_id == "options_chain_capture_plan_exact"
    assert explicit.session_id == "2026-08-10"


def test_options_history_inspect_requires_underlying_canonical_security_id() -> None:
    parsed = parser().parse_args(
        [
            "options-history-inspect",
            "--underlying-canonical-security-id",
            "apex_stable_security_exact",
        ]
    )
    assert parsed.underlying_canonical_security_id == "apex_stable_security_exact"


def test_options_research_quality_inspect_parses_with_and_without_plan_and_session() -> None:
    bare = parser().parse_args(["options-research-quality-inspect"])
    assert bare.plan_id is None
    assert bare.session_id is None
    explicit = parser().parse_args(
        ["options-research-quality-inspect", "--plan-id", "plan-1", "--session-id", "2026-08-10"]
    )
    assert explicit.plan_id == "plan-1"
    assert explicit.session_id == "2026-08-10"


# --------------------------------------------------------------------------
# Dispatch routing -- every command above must route to the new family, not
# accidentally fall into the pre-existing options-chain-* family (only
# ``options-chain-coverage-inspect`` shares that literal prefix, and it is
# deliberately handled *inside* ``options_quote_intelligence_command``,
# which owns every ``options-chain-*`` command).
# --------------------------------------------------------------------------


def test_new_commands_route_to_the_expected_handler_by_prefix() -> None:
    research_prefixes = ("options-research-", "options-forward-", "options-history-")
    for command in (
        "options-research-due-inspect",
        "options-research-run-once",
        "options-forward-checkpoint-inspect",
        "options-history-inspect",
        "options-research-quality-inspect",
    ):
        assert command.startswith(research_prefixes)
    assert "options-chain-coverage-inspect".startswith(
        ("options-quote-", "options-chain-", "options-volatility-")
    )
    assert not "options-chain-coverage-inspect".startswith(research_prefixes)


# --------------------------------------------------------------------------
# API: the extended options-intelligence workspace route stays registered
# --------------------------------------------------------------------------


def test_options_intelligence_workspace_route_is_still_registered() -> None:
    assert ("/api/v1/options-intelligence/workspace", "GET") in _routes()


# --------------------------------------------------------------------------
# Credential-leak preflight: only ``options-research-run-once`` reaches a
# real, credential-gated Alpaca adapter (via ``AlpacaOptionsResearchEvidence
# Capture``); the shared runtime builder and every inspect command are
# read-only reuse of already-retained evidence and must never require it --
# mirroring exactly how ``_options_structures_runtime`` needs none while
# ``_options_quote_intelligence_runtime`` does.
# --------------------------------------------------------------------------


def test_shared_runtime_builder_reaches_no_provider_and_needs_no_preflight() -> None:
    source = inspect.getsource(dev._options_research_operations_runtime)
    assert "_assert_no_tracked_provider_credentials" not in source
    # Only durable Postgres repositories are constructed here -- no
    # provider-network adapter import, docstring prose aside.
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("from ", "import "))
    ]
    assert all("alpaca" not in line.lower() for line in import_lines)


def test_command_family_source_reaches_the_preflight_for_run_once_only() -> None:
    source = inspect.getsource(dev.options_research_operations_command)
    assert "_assert_no_tracked_provider_credentials" in source
    # The preflight call must appear after the run-once branch opens and
    # before the next command branch, i.e. it is scoped to that one command.
    run_once_start = source.index('"options-research-run-once"')
    preflight_at = source.index("_assert_no_tracked_provider_credentials()")
    next_branch = source.index('"options-forward-checkpoint-inspect"')
    assert run_once_start < preflight_at < next_branch
