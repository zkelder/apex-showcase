"""Interface proofs for Genuine Options Quote-Chain and Volatility
Intelligence v1: every bounded CLI command parses its declared arguments,
and the extended options-intelligence API route stays registered. No
database, no server, no network -- mirrors the existing offline
``tests/test_research_replication_interfaces.py`` pattern.
"""

from __future__ import annotations

import pytest

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
# CLI: options-quote-capability-inspect; options-chain-plan create/inspect;
# options-chain-capture; options-chain-snapshot-inspect;
# options-volatility-summary; options-chain-quality-inspect
# --------------------------------------------------------------------------


def test_options_quote_capability_inspect_parses() -> None:
    parsed = parser().parse_args(["options-quote-capability-inspect"])
    assert parsed.command == "options-quote-capability-inspect"


def test_options_chain_plan_create_parses() -> None:
    parsed = parser().parse_args(["options-chain-plan-create"])
    assert parsed.command == "options-chain-plan-create"


def test_options_chain_plan_inspect_parses_with_and_without_a_plan_id() -> None:
    bare = parser().parse_args(["options-chain-plan-inspect"])
    assert bare.command == "options-chain-plan-inspect"
    assert bare.plan_id is None
    explicit = parser().parse_args(
        ["options-chain-plan-inspect", "--plan-id", "options_chain_capture_plan_exact"]
    )
    assert explicit.plan_id == "options_chain_capture_plan_exact"


def test_options_chain_capture_parses_optional_plan_and_bound_arguments() -> None:
    bare = parser().parse_args(["options-chain-capture"])
    assert bare.command == "options-chain-capture"
    assert bare.plan_id is None
    assert bare.max_underlyings is None
    bounded = parser().parse_args(
        [
            "options-chain-capture",
            "--plan-id",
            "options_chain_capture_plan_exact",
            "--max-underlyings",
            "3",
        ]
    )
    assert bounded.plan_id == "options_chain_capture_plan_exact"
    assert bounded.max_underlyings == 3


def test_options_chain_snapshot_inspect_parses() -> None:
    parsed = parser().parse_args(
        ["options-chain-snapshot-inspect", "--snapshot-id", "options_chain_snapshot_exact"]
    )
    assert parsed.snapshot_id == "options_chain_snapshot_exact"


def test_options_volatility_summary_parses() -> None:
    parsed = parser().parse_args(
        [
            "options-volatility-summary",
            "--underlying-canonical-security-id",
            "apex_stable_security_exact",
        ]
    )
    assert parsed.underlying_canonical_security_id == "apex_stable_security_exact"


def test_options_chain_quality_inspect_parses() -> None:
    parsed = parser().parse_args(
        ["options-chain-quality-inspect", "--snapshot-id", "options_chain_snapshot_exact"]
    )
    assert parsed.snapshot_id == "options_chain_snapshot_exact"


def test_options_quote_validation_report_parses_with_and_without_a_plan_id() -> None:
    bare = parser().parse_args(["options-quote-validation-report"])
    assert bare.command == "options-quote-validation-report"
    assert bare.plan_id is None
    explicit = parser().parse_args(
        ["options-quote-validation-report", "--plan-id", "options_chain_capture_plan_exact"]
    )
    assert explicit.plan_id == "options_chain_capture_plan_exact"


# --------------------------------------------------------------------------
# API: the extended options-intelligence workspace route stays registered
# --------------------------------------------------------------------------


def test_options_intelligence_workspace_route_is_still_registered() -> None:
    assert ("/api/v1/options-intelligence/workspace", "GET") in _routes()


# --------------------------------------------------------------------------
# Credential-leak preflight: the options-quote command family reaches a
# real, credential-gated Alpaca adapter exactly as directly as the other
# provider-touching stack builders do, so it must run the identical
# ``_assert_no_tracked_provider_credentials`` check they already run --
# not the one path that silently skips it.
# --------------------------------------------------------------------------


def test_options_quote_intelligence_runtime_runs_the_credential_leak_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    original = dev._assert_no_tracked_provider_credentials

    def _spy() -> None:
        calls.append(True)
        original()

    monkeypatch.setattr(dev, "_assert_no_tracked_provider_credentials", _spy)
    engine, _identity_repo, _quote_repo = dev._options_quote_intelligence_runtime()
    engine.dispose()
    assert calls == [True]


def test_options_quote_intelligence_runtime_propagates_a_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail() -> None:
        raise SystemExit("provider credential preflight failed in tracked/staged files: fake.env")

    monkeypatch.setattr(dev, "_assert_no_tracked_provider_credentials", _fail)
    with pytest.raises(SystemExit, match="provider credential preflight failed"):
        dev._options_quote_intelligence_runtime()
