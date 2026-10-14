"""Interface proofs for Corporate Calibration-Qualified Regime Follow-Up
v1's bounded CLI surface: every ``corporate-followup-*`` command parses,
dispatch routes to the new command handler after the existing calibration-
depth guard (so ``corporate-followup-requalify`` -- a Corporate Research
Calibration Depth v1 command that happens to share this module's prefix --
still routes there, never here) and before the ``corporate-market-``
prefix, and the new prefix never collides with the outer ``corporate-``
catch-all. No database; parses and inspects source only.
"""

from __future__ import annotations

import inspect

import scripts.dev as dev


def test_cli_parses_every_corporate_followup_command() -> None:
    parser = dev.parser()
    for command, extra in (
        ("corporate-followup-spec-create", ["--created-at", "2026-08-10T00:00:00Z"]),
        ("corporate-followup-spec-inspect", []),
        ("corporate-followup-run-start", ["--created-at", "2026-08-10T00:00:00Z"]),
        ("corporate-followup-run-resume", ["--created-at", "2026-08-10T00:00:00Z"]),
        ("corporate-followup-run-inspect", []),
        ("corporate-followup-stability-inspect", []),
        ("corporate-followup-calibration-inspect", []),
        ("corporate-followup-conclusion-inspect", []),
    ):
        parsed = parser.parse_args([command, *extra])
        assert parsed.command == command


def test_cli_parses_optional_follow_up_and_specification_id_overrides() -> None:
    parser = dev.parser()
    parsed = parser.parse_args(
        [
            "corporate-followup-spec-create",
            "--follow-up-id",
            "cross_market_follow_up_622f5a0d3af61dcb7b69bd9b",
            "--created-at",
            "2026-08-10T00:00:00Z",
        ]
    )
    assert parsed.follow_up_id == "cross_market_follow_up_622f5a0d3af61dcb7b69bd9b"
    parsed = parser.parse_args(
        [
            "corporate-followup-run-inspect",
            "--specification-id",
            "corporate_regime_followup_specification_x",
        ]
    )
    assert parsed.specification_id == "corporate_regime_followup_specification_x"


def test_cli_dispatch_routes_calibration_depth_before_the_followup_prefix() -> None:
    source = inspect.getsource(dev.corporate_fundamentals_command)
    calibration_index = source.index('startswith("corporate-calibration-")')
    followup_index = source.index('startswith("corporate-followup-"):')
    market_index = source.index('startswith("corporate-market-"):')
    assert calibration_index < followup_index < market_index


def test_cli_dispatch_does_not_collide_with_the_outer_corporate_prefix_guard() -> None:
    source = inspect.getsource(dev.main)
    dispatch_index = source.index('if arguments.command.startswith("corporate-"):')
    preceding = source[:dispatch_index]
    assert "corporate_regime_followup_command" not in preceding


def test_corporate_followup_requalify_still_routes_to_calibration_depth_not_regime_followup() -> (
    None
):
    # corporate-followup-requalify shares this module's "corporate-followup-"
    # prefix but is a Corporate Research Calibration Depth v1 command (it
    # requalifies all 13 retained v3 follow-up proposals); it must never be
    # captured by this milestone's narrower single-follow-up command family.
    source = inspect.getsource(dev.corporate_fundamentals_command)
    calibration_branch_start = source.index('startswith("corporate-calibration-")')
    followup_branch_start = source.index('startswith("corporate-followup-"):')
    requalify_mention = source.index('"corporate-followup-requalify"')
    assert calibration_branch_start < requalify_mention < followup_branch_start


def test_corporate_regime_followup_command_only_uses_the_narrow_follow_up_repository() -> None:
    source = inspect.getsource(dev.corporate_regime_followup_command)
    for forbidden in ("crypto", "kalshi", "options_", "PaperTrial", "Coinbase"):
        assert forbidden not in source
    assert "PostgresCorporateRegimeFollowUpRepository" in source
    assert 'authority": "NONE"' in source


# ==========================================================================
# Operator API: the existing workspace route is extended, not duplicated.
# ==========================================================================


def test_corporate_fundamentals_workspace_route_is_still_the_only_registration_point() -> None:
    from apex.operator_api import create_app

    app = create_app()
    routes = {
        (getattr(route, "path", ""), method)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/corporate-fundamentals/workspace", "GET") in routes
    # No new dashboard route was added for this milestone (section 23).
    assert not any("regime-followup" in path for path, _method in routes)


def test_operator_api_extends_the_existing_workspace_with_the_regime_followup_summary() -> None:
    import apex.operator_api as operator_api

    source = inspect.getsource(operator_api)
    assert "def _corporate_regime_followup_summary(" in source
    # The new summary is threaded into the same envelope dict the
    # pre-existing calibration-depth summary already extends -- confirm
    # both keys appear together, close enough to be the same dict literal,
    # rather than the new key living in some unrelated payload.
    calibration_index = source.index('"corporate_calibration_depth_summary"')
    followup_index = source.index('"corporate_regime_followup_summary"', calibration_index)
    between = source[calibration_index:followup_index]
    assert between.count("def ") == 0  # no function boundary between them.
