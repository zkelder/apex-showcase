"""Interface proofs for Defined-Risk Options Structure Economics and
Research Integration v1: every bounded CLI command parses its declared
arguments, the extended options-intelligence workspace route stays
registered, and this command family reaches no provider credential path
at all. No database, no server, no network -- mirrors the existing
offline ``tests/test_options_quote_intelligence_interfaces.py`` pattern.
"""

from __future__ import annotations

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
# CLI: options-structure-policy create/inspect; options-structures-build;
# options-structures-snapshot-inspect; options-structure-inspect;
# options-structure-scenarios-inspect; options-structure-comparison
# --------------------------------------------------------------------------


def test_options_structure_policy_create_parses_with_and_without_overrides() -> None:
    bare = parser().parse_args(["options-structure-policy-create"])
    assert bare.command == "options-structure-policy-create"
    assert bare.dte_minimum is None
    assert bare.dte_maximum is None
    assert bare.moneyness_band is None
    assert bare.maximum_structures_per_underlying is None
    assert bare.maximum_total_structures is None
    assert bare.allow_stale_quotes_for_research is False
    overridden = parser().parse_args(
        [
            "options-structure-policy-create",
            "--dte-minimum",
            "5",
            "--dte-maximum",
            "30",
            "--moneyness-band",
            "0.10",
            "--maximum-structures-per-underlying",
            "10",
            "--maximum-total-structures",
            "100",
            "--allow-stale-quotes-for-research",
        ]
    )
    assert overridden.dte_minimum == 5
    assert overridden.dte_maximum == 30
    assert overridden.moneyness_band == "0.10"
    assert overridden.maximum_structures_per_underlying == 10
    assert overridden.maximum_total_structures == 100
    assert overridden.allow_stale_quotes_for_research is True


def test_options_structure_policy_inspect_parses_with_and_without_a_policy_id() -> None:
    bare = parser().parse_args(["options-structure-policy-inspect"])
    assert bare.policy_id is None
    explicit = parser().parse_args(
        [
            "options-structure-policy-inspect",
            "--policy-id",
            "options_structure_research_policy_exact",
        ]
    )
    assert explicit.policy_id == "options_structure_research_policy_exact"


def test_options_structures_build_parses_single_underlying_and_cohort_modes() -> None:
    cohort = parser().parse_args(["options-structures-build"])
    assert cohort.command == "options-structures-build"
    assert cohort.underlying_canonical_security_id is None
    assert cohort.quantity is None
    single = parser().parse_args(
        [
            "options-structures-build",
            "--underlying-canonical-security-id",
            "apex_stable_security_exact",
            "--snapshot-id",
            "options_chain_snapshot_exact",
            "--quantity",
            "2",
        ]
    )
    assert single.underlying_canonical_security_id == "apex_stable_security_exact"
    assert single.snapshot_id == "options_chain_snapshot_exact"
    assert single.quantity == 2


def test_options_structures_snapshot_inspect_parses() -> None:
    by_id = parser().parse_args(
        [
            "options-structures-snapshot-inspect",
            "--research-snapshot-id",
            "options_structure_research_snapshot_exact",
        ]
    )
    assert by_id.research_snapshot_id == "options_structure_research_snapshot_exact"
    by_underlying = parser().parse_args(
        [
            "options-structures-snapshot-inspect",
            "--underlying-canonical-security-id",
            "apex_stable_security_exact",
        ]
    )
    assert by_underlying.underlying_canonical_security_id == "apex_stable_security_exact"


def test_options_structure_inspect_parses() -> None:
    parsed = parser().parse_args(
        ["options-structure-inspect", "--structure-id", "options_structure_exact"]
    )
    assert parsed.structure_id == "options_structure_exact"


def test_options_structure_inspect_requires_structure_id() -> None:
    try:
        parser().parse_args(["options-structure-inspect"])
    except SystemExit as error:
        assert error.code != 0
    else:  # pragma: no cover - defensive
        raise AssertionError("expected argparse to reject a missing --structure-id")


def test_options_structure_scenarios_inspect_parses() -> None:
    parsed = parser().parse_args(
        ["options-structure-scenarios-inspect", "--structure-id", "options_structure_exact"]
    )
    assert parsed.structure_id == "options_structure_exact"


def test_options_structure_comparison_parses_repeated_structure_ids() -> None:
    parsed = parser().parse_args(
        [
            "options-structure-comparison",
            "--structure-id",
            "options_structure_a",
            "--structure-id",
            "options_structure_b",
        ]
    )
    assert parsed.structure_id == ["options_structure_a", "options_structure_b"]


# --------------------------------------------------------------------------
# API: the extended options-intelligence workspace route stays registered
# --------------------------------------------------------------------------


def test_options_intelligence_workspace_route_is_still_registered() -> None:
    assert ("/api/v1/options-intelligence/workspace", "GET") in _routes()


# --------------------------------------------------------------------------
# Zero provider writes / zero credential path: this command family only
# ever reads already-retained options-quote-intelligence evidence and
# writes structure-economics rows -- it never reaches a provider adapter.
# --------------------------------------------------------------------------


def _import_lines(source: str) -> list[str]:
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def test_options_structures_command_module_never_imports_a_provider_adapter() -> None:
    import inspect

    import apex.application.options_structures as application_module
    import apex.domain.options_structures as domain_module

    for module in (domain_module, application_module):
        for line in _import_lines(inspect.getsource(module)):
            for forbidden in ("alpaca", "coinbase", "kalshi", "requests", "urllib"):
                assert forbidden not in line.lower(), (
                    f"{module.__name__} unexpectedly imports a provider module: {line!r}"
                )


def test_options_structures_runtime_reaches_no_provider_and_needs_no_preflight() -> None:
    import inspect

    import scripts.dev as dev

    source = inspect.getsource(dev._options_structures_runtime)
    assert "_assert_no_tracked_provider_credentials" not in source
    for line in _import_lines(source):
        assert "alpaca" not in line.lower()
