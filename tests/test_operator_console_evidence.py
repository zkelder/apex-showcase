"""Focused contracts for the newer-evidence Operator Console projections:
Decision Ledger, Follow-Up, Options Research, Portfolio/Mandate, Operations,
and their additive Overview/Opportunities linkage.

Mirrors ``tests/test_operator_console_v1.py`` exactly: every ``build_operator_*``
function is exercised as a pure function over hand-built dict/tuple fixtures,
never against a live database, provider, or HTTP client -- this repository
never uses FastAPI's TestClient (see the sibling file); route existence and
verb are instead proven by inspecting ``app.routes`` directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apex.application.operator_console_evidence import (
    OperatorConsoleFilterError,
    build_followup_progress,
    build_operator_decision_detail,
    build_operator_decisions,
    build_operator_followups,
    build_operator_operations,
    build_operator_options_research,
    build_operator_overview_extensions,
    build_operator_portfolio_mandate,
    enrich_opportunities_with_decisions,
)
from apex.operator_api import app

# ==========================================================================
# Route existence, verbs, and adapter-free purity
# ==========================================================================


def test_new_operator_console_routes_exist_and_are_get_only() -> None:
    expected = {
        "/api/v1/operator-console/decisions",
        "/api/v1/operator-console/decisions/{decision_id}",
        "/api/v1/operator-console/followups",
        "/api/v1/operator-console/options-research",
        "/api/v1/operator-console/options-research/checkpoints/{checkpoint_id}",
        "/api/v1/operator-console/portfolio-mandate",
        "/api/v1/operator-console/portfolio-mandate/evaluations/{evaluation_id}",
        "/api/v1/operator-console/operations",
    }
    methods = {
        route.path: getattr(route, "methods", set())
        for route in app.routes
        if route.path in expected
    }
    assert methods.keys() == expected
    assert all(value == {"GET"} for value in methods.values())


def test_evidence_module_imports_no_provider_adapter() -> None:
    """Structural proof this projection layer can never make a provider or
    broker call: it is pure shaping over already-fetched dict/tuple
    evidence, so it must never import anything from ``apex.adapters`` --
    only ``apex.application``/``apex.domain`` (for the one reused pure
    readiness function) and the standard library."""

    source = Path("src/apex/application/operator_console_evidence.py").read_text()
    tree = ast.parse(source)
    imported_modules = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert imported_modules, "expected at least one import"
    assert not any(module.startswith("apex.adapters") for module in imported_modules)


# ==========================================================================
# Decision Ledger
# ==========================================================================


def _decision(
    decision_id: str,
    *,
    venue: str = "CRYPTO",
    disposition: str = "NOT_ADVANCED",
    subject: str = "proj-1",
    evaluated_at: str = "2026-08-10T00:00:00Z",
) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "origin": "SHARED_OPPORTUNITY_ELIGIBILITY",
        "origin_decision_id": f"origin-{decision_id}",
        "subject_projection_id": subject,
        "subject_source_record_type": "CryptoOpportunity",
        "subject_source_record_id": f"source-{decision_id}",
        "venue": venue,
        "opportunity_set_id": "set-1",
        "mandate_id": None,
        "mandate_version": None,
        "portfolio_snapshot_id": None,
        "evaluated_at": evaluated_at,
        "disposition": disposition,
        "raw_reason_codes": ("ECONOMICS_NEGATIVE",),
        "explanation": "rejected on negative net economics",
        "authority": "NONE",
        "canonical_hash": f"hash-{decision_id}",
    }


def _classification(
    decision_id: str,
    *,
    version: str,
    families: tuple[str, ...],
    classified_at: str = "2026-08-10T01:00:00Z",
) -> dict[str, object]:
    return {
        "classification_id": f"{decision_id}-{version}",
        "decision_id": decision_id,
        "classifier_version": version,
        "reason_families": families,
        "classified_at": classified_at,
    }


def test_build_operator_decisions_reports_empty_state_with_no_retained_decisions() -> None:
    value = build_operator_decisions(
        (),
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"),
    )
    assert value["items"] == ()
    assert value["total"] == 0
    assert value["empty_state"] == "NO_RETAINED_DECISIONS"
    assert value["authority"] == "NONE"


def test_build_operator_decisions_keeps_classifier_versions_visually_distinct() -> None:
    """A decision classified under both V1 and V2 must show both
    independent interpretations side by side, plus one clearly labeled
    'current' pointer -- never merged into a single reinterpreted value and
    never a rewrite of the decision's own stable fields."""

    decision = _decision("dec-1")
    classifications = [
        _classification("dec-1", version="REASON_CLASSIFIER_V1", families=("ECONOMICS",)),
        _classification(
            "dec-1", version="REASON_CLASSIFIER_V2", families=("ECONOMICS", "LIQUIDITY")
        ),
    ]
    value = build_operator_decisions(
        [decision],
        classifications_by_decision={"dec-1": classifications},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"),
    )
    row = value["items"][0]
    assert {item["classifier_version"] for item in row["classifications"]} == {
        "REASON_CLASSIFIER_V1",
        "REASON_CLASSIFIER_V2",
    }
    assert row["current_classifier_version"] == "REASON_CLASSIFIER_V2"
    assert row["current_reason_families"] == ("ECONOMICS", "LIQUIDITY")
    assert row["has_current_classification"] is True
    # The stable decision event itself is untouched by classification.
    assert row["disposition"] == "NOT_ADVANCED"
    assert row["raw_reason_codes"] == ("ECONOMICS_NEGATIVE",)


def test_build_operator_decisions_reports_missing_current_classification_honestly() -> None:
    decision = _decision("dec-2")
    classifications = [_classification("dec-2", version="REASON_CLASSIFIER_V1", families=())]
    value = build_operator_decisions(
        [decision],
        classifications_by_decision={"dec-2": classifications},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V1", "REASON_CLASSIFIER_V2"),
    )
    row = value["items"][0]
    assert row["has_current_classification"] is False
    assert row["current_reason_families"] == ()


def test_build_operator_decisions_filters_by_venue_disposition_and_query_without_reordering() -> (
    None
):
    decisions = [
        _decision(
            "dec-1", venue="CRYPTO", disposition="NOT_ADVANCED", evaluated_at="2026-08-10T00:00:00Z"
        ),
        _decision(
            "dec-2", venue="KALSHI", disposition="ADVANCED", evaluated_at="2026-08-11T00:00:00Z"
        ),
        _decision(
            "dec-3", venue="CRYPTO", disposition="CONSTRAINED", evaluated_at="2026-08-12T00:00:00Z"
        ),
    ]
    value = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
        venue="CRYPTO",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-1", "dec-3"]

    value = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
        disposition="ADVANCED",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-2"]

    value = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
        query="dec-3",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-3"]
    assert value["empty_state"] is None

    empty = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
        query="no-such-decision",
    )
    assert empty["items"] == ()
    assert empty["empty_state"] == "NO_MATCHING_DECISIONS"


def test_build_operator_decisions_accepts_securities_venue_filter() -> None:
    """SECURITIES Operator Console Backend Read-Model Closure v1 (acceptance
    A/B): venue=SECURITIES must return real bounded decisions rather than
    400, while CRYPTO/KALSHI filtering stays exactly as before."""

    decisions = [
        _decision("dec-1", venue="CRYPTO"),
        _decision("dec-2", venue="KALSHI"),
        _decision("dec-3", venue="SECURITIES"),
    ]
    value = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
        venue="SECURITIES",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-3"]
    assert value["items"][0]["venue"] == "SECURITIES"

    # CRYPTO/KALSHI remain exactly as before (acceptance B).
    crypto_value = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=(),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
        venue="CRYPTO",
    )
    assert [item["decision_id"] for item in crypto_value["items"]] == ["dec-1"]


def test_build_operator_decisions_rejects_unknown_filter_values() -> None:
    with pytest.raises(OperatorConsoleFilterError):
        build_operator_decisions(
            (),
            classifications_by_decision={},
            followup_decision_ids=(),
            current_classifier_version="REASON_CLASSIFIER_V2",
            known_classifier_versions=("REASON_CLASSIFIER_V2",),
            venue="COINBASE",
        )
    with pytest.raises(OperatorConsoleFilterError):
        build_operator_decisions(
            (),
            classifications_by_decision={},
            followup_decision_ids=(),
            current_classifier_version="REASON_CLASSIFIER_V2",
            known_classifier_versions=("REASON_CLASSIFIER_V2",),
            disposition="MAYBE",
        )


def test_build_operator_decisions_marks_has_followup_only_when_observed() -> None:
    decisions = [_decision("dec-1"), _decision("dec-2")]
    value = build_operator_decisions(
        decisions,
        classifications_by_decision={},
        followup_decision_ids=("dec-1",),
        current_classifier_version="REASON_CLASSIFIER_V2",
        known_classifier_versions=("REASON_CLASSIFIER_V2",),
    )
    by_id = {item["decision_id"]: item for item in value["items"]}
    assert by_id["dec-1"]["has_followup"] is True
    assert by_id["dec-2"]["has_followup"] is False


def test_build_operator_decision_detail_reports_not_found_honestly() -> None:
    value = build_operator_decision_detail(None, classifications=(), followups=())
    assert value["decision"] is None
    assert value["empty_state"] == "DECISION_NOT_FOUND"
    assert value["authority"] == "NONE"


def test_build_operator_decision_detail_bundles_classifications_and_followups() -> None:
    decision = _decision("dec-1")
    classifications = [
        _classification(
            "dec-1",
            version="REASON_CLASSIFIER_V2",
            families=("ECONOMICS",),
            classified_at="2026-08-11T00:00:00Z",
        ),
        _classification(
            "dec-1",
            version="REASON_CLASSIFIER_V1",
            families=("ECONOMICS",),
            classified_at="2026-08-10T00:00:00Z",
        ),
    ]
    followups = [
        {
            "decision_id": "dec-1",
            "policy_version": "FOLLOWUP_POLICY_V2",
            "horizon_kind": "SHORT_HORIZON",
            "baseline_kind": "ORIGIN_EVIDENCE",
        },
        {
            "decision_id": "dec-1",
            "policy_version": "FOLLOWUP_POLICY_V2",
            "horizon_kind": "SHORT_HORIZON",
            "baseline_kind": "DECISION_TIME",
        },
    ]
    value = build_operator_decision_detail(
        decision, classifications=classifications, followups=followups
    )
    assert value["decision"] == decision
    assert value["empty_state"] is None
    assert [item["classifier_version"] for item in value["classifications"]] == [
        "REASON_CLASSIFIER_V1",
        "REASON_CLASSIFIER_V2",
    ]
    assert {item["baseline_kind"] for item in value["followups"]} == {
        "ORIGIN_EVIDENCE",
        "DECISION_TIME",
    }


# ==========================================================================
# Follow-Up
# ==========================================================================


def _observation(
    decision_id: str,
    *,
    policy_version: str = "FOLLOWUP_POLICY_V2",
    horizon_kind: str = "SHORT_HORIZON",
    baseline_kind: str = "DECISION_TIME",
    venue: str = "CRYPTO",
) -> dict[str, object]:
    return {
        "observation_id": f"{decision_id}-{policy_version}-{horizon_kind}-{baseline_kind}",
        "decision_id": decision_id,
        "venue": venue,
        "disposition": "NOT_ADVANCED",
        "policy_version": policy_version,
        "horizon_kind": horizon_kind,
        "baseline_kind": baseline_kind,
        "horizon_days": 7,
        "baseline_time": "2026-08-01T00:00:00Z",
        "target_time": "2026-08-08T00:00:00Z",
        "observation_type": "CRYPTO_REFERENCE_PRICE_MOVEMENT",
        "subject_instrument_id": "BTC-USD",
        "subject_product_or_market_id": "BTC-USD",
        "provenance": "HISTORICAL_PUBLIC",
        "observed_at": "2026-08-08T01:00:00Z",
        "data_limitations": (),
        "reference_price_at_baseline": "60000",
        "reference_price_at_target": "61000",
        "subsequent_return": "0.0167",
    }


def _plan_entry(
    decision_id: str,
    *,
    state: str,
    venue: str = "CRYPTO",
    horizon_kind: str = "SHORT_HORIZON",
    baseline_kind: str = "DECISION_TIME",
    target_time: str | None = "2026-08-20T00:00:00Z",
    reasons: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "venue": venue,
        "horizon_kind": horizon_kind,
        "baseline_kind": baseline_kind,
        "state": state,
        "target_time": target_time,
        "reasons": reasons,
    }


def test_build_operator_followups_reports_empty_state_when_nothing_retained_or_pending() -> None:
    value = build_operator_followups(
        (),
        (),
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
    )
    assert value["items"] == ()
    assert value["empty_state"] == "NO_RETAINED_OR_PENDING_FOLLOWUP"
    assert value["authority"] == "NONE"


def test_build_operator_followups_merges_observed_and_pending_without_duplication() -> None:
    observed = _observation("dec-1", horizon_kind="SHORT_HORIZON", baseline_kind="DECISION_TIME")
    # The plan already marks this exact (decision, horizon, baseline) OBSERVED;
    # it must never be shown a second time under a pending status.
    already_observed_entry = _plan_entry(
        "dec-1", state="OBSERVED", horizon_kind="SHORT_HORIZON", baseline_kind="DECISION_TIME"
    )
    due_entry = _plan_entry(
        "dec-1", state="DUE", horizon_kind="STRATEGY_HORIZON", baseline_kind="DECISION_TIME"
    )
    value = build_operator_followups(
        [observed],
        [already_observed_entry, due_entry],
        decisions_by_id={"dec-1": _decision("dec-1")},
        coverage={"observation_count": 1},
        plan_summary={"policy_version": "FOLLOWUP_POLICY_V2"},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        limit=25,
        offset=0,
    )
    assert value["total"] == 2
    statuses = sorted(item["status"] for item in value["items"])
    assert statuses == ["DUE", "OBSERVED"]
    due_row = next(item for item in value["items"] if item["status"] == "DUE")
    assert due_row["subject_product_or_market_id"] == "proj-1"
    assert due_row["disposition"] == "NOT_ADVANCED"


def test_build_operator_followups_observed_row_defaults_to_unassessed_evidence_eligibility() -> (
    None
):
    """CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
    Remediation v1: a caller that does not pass ``evidence_assessments_by_
    observation_id`` (every existing caller, and every existing test) keeps
    working unchanged, and every observed row honestly reports
    ``UNASSESSED`` rather than fabricating a verdict.  A pending (not yet
    observed) row carries no ``evidence_eligibility`` sub-object at all --
    a materially different fact from "observed but not yet assessed"."""

    observed = _observation("dec-1")
    due_entry = _plan_entry("dec-2", state="DUE")
    value = build_operator_followups(
        [observed],
        [due_entry],
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
    )
    observed_row = next(item for item in value["items"] if item["row_kind"] == "OBSERVED")
    pending_row = next(item for item in value["items"] if item["row_kind"] == "PLANNED")
    assert observed_row["evidence_eligibility"] == {
        "status": "UNASSESSED",
        "reason_code": None,
        "evidence_defect_type": None,
        "assessment_policy_version": None,
        "assessed_at": None,
    }
    assert pending_row["evidence_eligibility"] is None


def test_build_operator_followups_observed_row_surfaces_a_supplied_ineligible_assessment() -> None:
    """The confirmed DOGE-USD incident, at the read-model layer: once the
    caller supplies a retained assessment (fetched from ``adapters.
    counterfactual_followup_postgres.PostgresCounterfactualFollowUpRepository
    .assessments()``), the observed row honestly surfaces it -- eligibility
    is visible without a GUI redesign, purely as one more field."""

    observed = _observation("dec-1")
    value = build_operator_followups(
        [observed],
        [],
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        evidence_assessments_by_observation_id={
            observed["observation_id"]: {
                "verdict": "INELIGIBLE_EVIDENCE",
                "reason_code": "BASELINE_EVIDENCE_CLASS_NOT_ALLOWLISTED",
                "evidence_defect_type": "BASELINE_EVIDENCE_INELIGIBLE",
                "assessment_policy_version": "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1",
                "assessed_at": "2026-08-16T00:00:00Z",
            }
        },
    )
    row = value["items"][0]
    assert row["evidence_eligibility"]["status"] == "INELIGIBLE_EVIDENCE"
    assert row["evidence_eligibility"]["evidence_defect_type"] == "BASELINE_EVIDENCE_INELIGIBLE"


def test_build_operator_followups_distinguishes_origin_evidence_and_decision_time_baselines() -> (
    None
):
    """For one decision with both V2 baseline kinds, both rows must remain
    independently comparable -- never collapsed into one row or one return."""

    origin_row = _observation("dec-1", baseline_kind="ORIGIN_EVIDENCE")
    origin_row["reference_price_at_baseline"] = "59000"
    origin_row["subsequent_return"] = "-0.01"
    decision_row = _observation("dec-1", baseline_kind="DECISION_TIME")
    decision_row["reference_price_at_baseline"] = "60500"
    decision_row["subsequent_return"] = "0.02"

    value = build_operator_followups(
        [origin_row, decision_row],
        (),
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
    )
    by_baseline = {item["baseline_kind"]: item for item in value["items"]}
    assert set(by_baseline) == {"ORIGIN_EVIDENCE", "DECISION_TIME"}
    assert by_baseline["ORIGIN_EVIDENCE"]["subsequent_return"] == "-0.01"
    assert by_baseline["DECISION_TIME"]["subsequent_return"] == "0.02"
    assert (
        by_baseline["ORIGIN_EVIDENCE"]["reference_price_at_baseline"]
        != by_baseline["DECISION_TIME"]["reference_price_at_baseline"]
    )


def test_build_operator_followups_v1_rows_carry_v1_policy_version_distinctly_from_v2() -> None:
    v1_row = _observation("dec-1", policy_version="FOLLOWUP_POLICY_V1")
    v2_row = _observation("dec-1", policy_version="FOLLOWUP_POLICY_V2")
    value = build_operator_followups(
        [v1_row, v2_row],
        (),
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
    )
    versions = {item["policy_version"] for item in value["items"]}
    assert versions == {"FOLLOWUP_POLICY_V1", "FOLLOWUP_POLICY_V2"}

    v1_only = build_operator_followups(
        [v1_row, v2_row],
        (),
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        policy_version="FOLLOWUP_POLICY_V1",
    )
    assert [item["policy_version"] for item in v1_only["items"]] == ["FOLLOWUP_POLICY_V1"]


def test_build_operator_followups_filters_by_status_and_baseline_kind() -> None:
    entries = [
        _plan_entry("dec-1", state="DUE", baseline_kind="DECISION_TIME"),
        _plan_entry("dec-2", state="NOT_YET_DUE", baseline_kind="ORIGIN_EVIDENCE"),
        _plan_entry("dec-3", state="UNSUPPORTED", baseline_kind="DECISION_TIME", target_time=None),
    ]
    value = build_operator_followups(
        (),
        entries,
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        status="NOT_YET_DUE",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-2"]

    value = build_operator_followups(
        (),
        entries,
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        baseline_kind="ORIGIN_EVIDENCE",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-2"]


def test_build_operator_followups_accepts_securities_venue_filter() -> None:
    """SECURITIES Operator Console Backend Read-Model Closure v1 (acceptance
    A/B): venue=SECURITIES must return real bounded follow-up rows rather
    than 400, for both an OBSERVED row and a still-pending plan entry, while
    CRYPTO filtering stays exactly as before."""

    observed = _observation("dec-1", venue="SECURITIES")
    pending = _plan_entry("dec-2", state="DUE", venue="SECURITIES")
    other_venue_pending = _plan_entry("dec-3", state="DUE", venue="CRYPTO")
    value = build_operator_followups(
        [observed],
        [pending, other_venue_pending],
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        venue="SECURITIES",
    )
    assert {item["decision_id"] for item in value["items"]} == {"dec-1", "dec-2"}
    assert all(item["venue"] == "SECURITIES" for item in value["items"])

    # CRYPTO remains exactly as before (acceptance B).
    crypto_value = build_operator_followups(
        [observed],
        [pending, other_venue_pending],
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        venue="CRYPTO",
    )
    assert [item["decision_id"] for item in crypto_value["items"]] == ["dec-3"]

    # Unknown/invalid venue values remain fail-closed (acceptance E) --
    # widening the allow-list to SECURITIES must not loosen it generally.
    with pytest.raises(OperatorConsoleFilterError, match="VENUE_FILTER_INVALID"):
        build_operator_followups(
            [observed],
            [pending, other_venue_pending],
            decisions_by_id={},
            coverage={},
            plan_summary={},
            plan_policy_versions=("FOLLOWUP_POLICY_V2",),
            venue="COINBASE",
        )


def test_build_operator_followups_rejects_unknown_baseline_kind_filter() -> None:
    with pytest.raises(OperatorConsoleFilterError):
        build_operator_followups(
            (),
            (),
            decisions_by_id={},
            coverage={},
            plan_summary={},
            plan_policy_versions=("FOLLOWUP_POLICY_V2",),
            baseline_kind="SOMETHING_ELSE",
        )


def test_build_operator_followups_accepts_permanently_unavailable_status_filter() -> None:
    """Counterfactual Follow-Up Starvation Fix v1 -- a proven-permanent
    CRYPTO baseline must be filterable on its own, never rejected as an
    unknown status and never conflated with genuinely retryable DUE
    backlog."""

    entries = [
        _plan_entry("dec-1", state="DUE"),
        _plan_entry(
            "dec-2",
            state="PERMANENTLY_UNAVAILABLE",
            reasons=("CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE",),
        ),
    ]
    value = build_operator_followups(
        (),
        entries,
        decisions_by_id={},
        coverage={},
        plan_summary={},
        plan_policy_versions=("FOLLOWUP_POLICY_V2",),
        status="PERMANENTLY_UNAVAILABLE",
    )
    assert [item["decision_id"] for item in value["items"]] == ["dec-2"]
    assert value["items"][0]["status"] == "PERMANENTLY_UNAVAILABLE"


def test_build_followup_progress_reports_honest_empty_state_with_nothing_retained() -> None:
    """No completed cycle and no cursor row yet -- every fact stays
    genuinely absent (``None``), never a fabricated zero, and the cursor's
    own empty state is reported explicitly."""

    progress = build_followup_progress(
        plan_state_counts=(),
        latest_cycle_task=None,
        scan_cursors=(),
        kalshi_request_budget=20,
    )
    assert progress["last_cycle_completed_at"] is None
    assert progress["last_cycle_created"] is None
    assert progress["last_cycle_permanently_unavailable_skipped"] is None
    assert progress["scan_cursor_decision_id"] is None
    assert progress["scan_cursor_empty_state"] == "NO_COMPLETED_SCAN_YET"
    assert progress["kalshi_request_budget_per_cycle"] == 20
    assert progress["kalshi_budget_exhausted_last_cycle"] is False
    assert progress["currently_due"] == 0
    assert progress["currently_permanently_unavailable"] == 0
    # Kalshi Settlement & Resolution Evidence Maturation v1: a fresh
    # database, or one predating migration 0107, must report honest empty
    # state here too -- never a fabricated zero dressed up as real evidence.
    assert progress["kalshi_resolution_facts_retained"] == 0
    assert progress["kalshi_resolution_distinct_markets_tracked"] == 0
    assert progress["kalshi_resolution_current_state_by_market"] == {}
    assert progress["kalshi_resolution_markets_unresolved"] == 0
    # Kalshi Baseline Quote Capture & Economic Maturation v1: identical
    # discipline -- a fresh database, or one predating migration 0108, must
    # report honest empty state here too.
    assert progress["kalshi_baseline_quote_rows_retained"] == 0
    assert progress["kalshi_baseline_quote_matched_trade_rows"] == 0
    assert progress["kalshi_baseline_quote_market_quote_rows"] == 0
    assert progress["kalshi_baseline_quote_latest_captured_at"] is None
    assert progress["kalshi_resolution_latest_captured_at"] is None


def test_build_followup_progress_reports_kalshi_resolution_census_honestly() -> None:
    """The operator questions Part 12 names directly: is acquisition
    healthy, how many tracked markets remain unresolved, how many
    authoritative facts are retained, when did it last succeed."""

    progress = build_followup_progress(
        plan_state_counts=(),
        latest_cycle_task=None,
        scan_cursors=(),
        kalshi_request_budget=20,
        kalshi_resolution_census={
            "total_fact_rows": 7,
            "distinct_market_tickers": 5,
            "current_state_by_market": {
                "FINALIZED": 3,
                "RESULT_DETERMINED": 1,
                "CLOSED_AWAITING_RESULT": 1,
            },
            "latest_captured_at": "2026-09-01T12:00:00Z",
        },
    )
    assert progress["kalshi_resolution_facts_retained"] == 7
    assert progress["kalshi_resolution_distinct_markets_tracked"] == 5
    assert progress["kalshi_resolution_current_state_by_market"] == {
        "FINALIZED": 3,
        "RESULT_DETERMINED": 1,
        "CLOSED_AWAITING_RESULT": 1,
    }
    # 5 distinct markets tracked, only 3 currently FINALIZED -- 2 genuinely
    # unresolved, never conflated with the 7 raw historical fact rows.
    assert progress["kalshi_resolution_markets_unresolved"] == 2
    assert progress["kalshi_resolution_latest_captured_at"] == "2026-09-01T12:00:00Z"


def test_build_followup_progress_reports_kalshi_baseline_quote_census_honestly() -> None:
    """Kalshi Baseline Quote Capture & Economic Maturation v1: the operator
    questions Part 11 names directly -- is baseline capture healthy, how
    many of each kind are retained, when did it last succeed."""

    progress = build_followup_progress(
        plan_state_counts=(),
        latest_cycle_task=None,
        scan_cursors=(),
        kalshi_request_budget=20,
        kalshi_baseline_quote_census={
            "total_quote_rows": 9,
            "by_source_record_type": {"BinaryComplementEvaluation": 9},
            "by_quote_kind": {"MATCHED_TRADE_EXECUTION": 2, "MARKET_QUOTE_OBSERVATION": 7},
            "matched_trade_execution_rows": 2,
            "market_quote_observation_rows": 7,
            "latest_captured_at": "2026-09-01T12:00:00Z",
        },
    )
    assert progress["kalshi_baseline_quote_rows_retained"] == 9
    assert progress["kalshi_baseline_quote_matched_trade_rows"] == 2
    assert progress["kalshi_baseline_quote_market_quote_rows"] == 7
    assert progress["kalshi_baseline_quote_latest_captured_at"] == "2026-09-01T12:00:00Z"


def test_build_followup_progress_parses_latest_cycle_warning_codes_and_ignores_unknown_ones() -> (
    None
):
    task = {
        "completed_at": "2026-08-16T00:49:32Z",
        "status": "COMPLETED",
        "warning_codes": (
            "FOLLOWUP_CONSIDERED_PAIRS:25",
            "FOLLOWUP_CREATED:11",
            "FOLLOWUP_REUSED_IDEMPOTENT:0",
            "FOLLOWUP_BLOCKED_UNAVAILABLE:2",
            "FOLLOWUP_PERMANENTLY_UNAVAILABLE:29",
            "FOLLOWUP_SKIPPED_NOT_YET_DUE:3",
            "FOLLOWUP_SKIPPED_UNSUPPORTED:0",
            "FOLLOWUP_SKIPPED_ALREADY_OBSERVED:5",
            "FOLLOWUP_PROVIDER_REQUESTS:9",
            "FOLLOWUP_SAMPLE_LIMIT:25",
            "FOLLOWUP_CANDIDATES_EXAMINED:64",
            "FOLLOWUP_CANDIDATE_SCAN_LIMIT:2000",
            "FOLLOWUP_POLICY_VERSION:FOLLOWUP_POLICY_V2",  # non-numeric, must be ignored
            "NO_PAPER_LIVE_OR_FINANCIAL_MUTATION",  # not a KEY:VALUE fact, must be ignored
        ),
    }
    progress = build_followup_progress(
        plan_state_counts=(("DUE", 4), ("PERMANENTLY_UNAVAILABLE", 40)),
        latest_cycle_task=task,
        # Counterfactual Forward Outcome Temporal Integrity v1: the current
        # per-venue selection runs one independently-cursored scan per
        # policy version, so this is a sequence.  Deliberately out of
        # ``updated_at`` order, and with the *older* cursor listed first, so
        # the flat ``scan_cursor_*`` keys are genuinely proven to report the
        # most recently advanced scan rather than whichever happened to be
        # passed first.
        scan_cursors=(
            {
                "policy_version": "FOLLOWUP_POLICY_V2",
                "decision_id": "dec-2",
                "horizon_kind": "STRATEGY_HORIZON",
                "baseline_kind": "DECISION_TIME",
                "updated_at": "2026-08-15T00:49:32Z",
            },
            {
                "policy_version": "FOLLOWUP_POLICY_V3",
                "decision_id": "dec-9",
                "horizon_kind": "SHORT_HORIZON",
                "baseline_kind": "DECISION_TIME",
                "updated_at": "2026-08-16T00:49:32Z",
            },
        ),
        kalshi_request_budget=20,
    )
    assert progress["last_cycle_completed_at"] == "2026-08-16T00:49:32Z"
    assert progress["last_cycle_genuine_attempts"] == 25
    assert progress["last_cycle_created"] == 11
    # A transient block this cycle and a zero-cost permanent skip must never
    # be reported as the same figure.
    assert progress["last_cycle_transiently_blocked"] == 2
    assert progress["last_cycle_permanently_unavailable_skipped"] == 29
    assert progress["last_cycle_candidates_examined"] == 64
    assert progress["last_cycle_candidate_scan_limit"] == 2000
    assert progress["kalshi_budget_exhausted_last_cycle"] is False
    assert progress["scan_cursor_decision_id"] == "dec-9"
    assert progress["scan_cursor_policy_version"] == "FOLLOWUP_POLICY_V3"
    assert progress["scan_cursor_empty_state"] is None
    # Every cursor stays reported in full -- the flat keys above are an
    # at-a-glance view, never the only one available.
    assert len(progress["scan_cursors"]) == 2
    assert progress["currently_due"] == 4
    assert progress["currently_permanently_unavailable"] == 40


def test_build_followup_progress_reports_kalshi_budget_exhaustion_honestly() -> None:
    task = {
        "completed_at": "2026-08-16T00:49:32Z",
        "status": "COMPLETED",
        "warning_codes": (
            "FOLLOWUP_PROVIDER_REQUESTS:20",
            "COUNTERFACTUAL_FOLLOWUP_KALSHI_REQUEST_BUDGET_EXHAUSTED_THIS_CYCLE",
        ),
    }
    progress = build_followup_progress(
        plan_state_counts=(), latest_cycle_task=task, scan_cursors=(), kalshi_request_budget=20
    )
    assert progress["last_cycle_provider_requests"] == 20
    assert progress["kalshi_budget_exhausted_last_cycle"] is True


# ==========================================================================
# Options Research
# ==========================================================================


def _checkpoint(
    session_id: str,
    *,
    checkpoint_id: str | None = None,
    created_at: str = "2026-08-13T20:10:00Z",
    plan_id: str = "options-forward-plan-v1",
    captured: int = 19,
    requested: int = 20,
) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint_id or f"checkpoint-{session_id}",
        "plan_id": plan_id,
        "session_id": session_id,
        "window_open_at": f"{session_id}T20:00:00Z",
        "window_close_at": f"{session_id}T20:15:00Z",
        "provider": "ALPACA",
        "feed": "INDICATIVE",
        "feed_entitlement_state": "OPRA_INDICATIVE_UNCONFIRMED",
        "underlying_symbols_captured": tuple(f"SYM{i}" for i in range(captured)),
        "underlyings_requested_count": requested,
        "underlyings_captured_count": captured,
        "stale_snapshot_count": 0,
        "quote_quality_counts": (("GOOD", captured),),
        "chain_coverage_quality_counts": (("CAP_CONSTRAINED", 18), ("INSUFFICIENT_COVERAGE", 1)),
        "cap_constrained_underlyings": tuple(f"SYM{i}" for i in range(18)),
        "structures_evaluated": 400,
        "structures_retained": 306,
        "fee_evidence_status": "INCOMPLETE",
        "opra_entitlement_state": "INDICATIVE_UNCONFIRMED",
        "open_interest_limitation": "UNAVAILABLE",
        "historical_pricing_limitation": "UNAVAILABLE",
        "iv_history_ready_underlying_count": 0,
        "forward_history_underlying_count": captured,
        "errors": (),
        "limitations": ("OPRA_INDICATIVE_UNCONFIRMED", "OPEN_INTEREST_UNAVAILABLE"),
        "created_at": created_at,
    }


def test_build_operator_options_research_reports_zero_of_target_with_no_checkpoints() -> None:
    value = build_operator_options_research((), target_session_count=20)
    assert value["campaign_sessions_captured"] == 0
    assert value["campaign_sessions_target"] == 20
    assert value["campaign_progress_label"] == "0/20"
    assert value["readiness_state"] == "INSUFFICIENT_HISTORY"
    assert value["empty_state"] == "NO_RETAINED_FORWARD_SESSION"
    assert value["latest_session"] is None
    assert value["authority"] == "NONE"


def test_build_operator_options_research_reports_one_of_target_and_reads_target_from_caller() -> (
    None
):
    """The '1/20' style progress label must come from the caller-supplied
    target (traced to ``IV_RANK_MINIMUM_HISTORY`` at the route), never a
    value baked into this module."""

    value = build_operator_options_research([_checkpoint("2026-08-13")], target_session_count=20)
    assert value["campaign_sessions_captured"] == 1
    assert value["campaign_progress_label"] == "1/20"
    assert value["readiness_state"] == "CURRENT_SNAPSHOT_AVAILABLE"
    assert value["empty_state"] is None

    other_target = build_operator_options_research(
        [_checkpoint("2026-08-13")], target_session_count=5
    )
    assert other_target["campaign_progress_label"] == "1/5"


def test_build_operator_options_research_collapses_same_session_replay_to_latest_checkpoint() -> (
    None
):
    first_poll = _checkpoint(
        "2026-08-13", checkpoint_id="checkpoint-a", created_at="2026-08-13T20:05:00Z", captured=10
    )
    second_poll = _checkpoint(
        "2026-08-13", checkpoint_id="checkpoint-b", created_at="2026-08-13T20:12:00Z", captured=19
    )
    value = build_operator_options_research([first_poll, second_poll], target_session_count=20)
    assert value["campaign_sessions_captured"] == 1  # one distinct session, not two checkpoints
    assert len(value["sessions"]) == 1
    session = value["sessions"][0]
    assert session["checkpoint_id"] == "checkpoint-b"
    assert session["underlyings_captured_count"] == 19
    assert session["checkpoint_count_this_session"] == 2


def test_build_operator_options_research_reaches_longitudinal_ready_at_target() -> None:
    checkpoints = [_checkpoint(f"2026-08-{day:02d}") for day in range(1, 21)]
    value = build_operator_options_research(checkpoints, target_session_count=20)
    assert value["campaign_sessions_captured"] == 20
    assert value["campaign_progress_label"] == "20/20"
    assert value["readiness_state"] == "LONGITUDINAL_RESEARCH_READY"


def test_build_operator_options_research_builds_honest_per_underlying_rows() -> None:
    snapshot = {
        "underlying_symbol": "XLRE",
        "underlying_canonical_security_id": "sec-xlre",
        "data_quality_status": "PARTIAL",
        "observation_ids": ("obs-1", "obs-2", "obs-3"),
        "contracts_considered": 40,
        "contracts_quoted": 12,
        "contracts_excluded": 28,
        "data_quality_notes": ("INSUFFICIENT_COVERAGE",),
        "historical_options_pricing_note": "UNAVAILABLE",
        "captured_at": "2026-08-13T20:10:00Z",
    }
    value = build_operator_options_research(
        [_checkpoint("2026-08-13")],
        target_session_count=20,
        latest_underlying_snapshots=[snapshot],
    )
    row = value["latest_underlyings"][0]
    assert row["symbol"] == "XLRE"
    assert row["quote_count"] == 3
    assert row["snapshot_quality"] == "PARTIAL"
    assert row["evidence_limitations"] == ("INSUFFICIENT_COVERAGE",)


def test_build_operator_options_research_keeps_genuine_misses_visible_and_distinct() -> None:
    """A calendar session whose window closed with no retained checkpoint
    (e.g. the real 2026-08-10/11/12 misses) must stay visible -- separate
    from, never merged into, the banked ``sessions`` list -- and must never
    be fabricated for a session this caller does not report as missed."""

    value = build_operator_options_research(
        [_checkpoint("2026-08-13")],
        target_session_count=20,
        missed_session_ids=["2026-08-12", "2026-08-11", "2026-08-10", "2026-08-10"],
    )
    assert value["missed_session_ids"] == ("2026-08-10", "2026-08-11", "2026-08-12")
    assert [session["session_id"] for session in value["sessions"]] == ["2026-08-13"]

    without_misses = build_operator_options_research(
        [_checkpoint("2026-08-13")], target_session_count=20
    )
    assert without_misses["missed_session_ids"] == ()


# ==========================================================================
# Portfolio / Mandate
# ==========================================================================


def test_build_operator_portfolio_mandate_reports_empty_states_honestly() -> None:
    value = build_operator_portfolio_mandate(
        portfolio_snapshot=None,
        account_balances=(),
        positions=(),
        mandate=None,
        mandate_lineage_versions=(),
        latest_evaluations=(),
    )
    assert value["portfolio_empty_state"] == "NO_PORTFOLIO_SNAPSHOT_RETAINED"
    assert value["mandate_empty_state"] == "NO_MANDATE_AUTHORED"
    assert value["authority"] == "NONE"
    assert value["latest_evaluation_count"] == 0


def test_build_operator_portfolio_mandate_orders_evaluations_newest_first_and_bounds_to_25() -> (
    None
):
    evaluations = [
        {"evaluation_id": f"eval-{i}", "evaluated_at": f"2026-08-{i:02d}T00:00:00Z"}
        for i in range(1, 31)
    ]
    value = build_operator_portfolio_mandate(
        portfolio_snapshot={"snapshot_id": "snap-1", "as_of": "2026-08-13T00:00:00Z"},
        account_balances=(),
        positions=(),
        mandate={"mandate_id": "mandate-1", "version": 1},
        mandate_lineage_versions=({"mandate_id": "mandate-1", "version": 1},),
        latest_evaluations=evaluations,
    )
    assert value["latest_evaluation_count"] == 30
    assert len(value["latest_evaluations"]) == 25
    assert value["latest_evaluations"][0]["evaluation_id"] == "eval-30"
    assert value["portfolio_empty_state"] is None
    assert value["mandate_empty_state"] is None


# ==========================================================================
# Operations
# ==========================================================================


def _task_result(
    task_type: str, *, status: str, completed_at: str, output_identities: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "task_result_id": f"{task_type}-{completed_at}",
        "cycle_id": "cycle-1",
        "task_type": task_type,
        "status": status,
        "completed_at": completed_at,
        "output_identities": output_identities,
        "changed": status == "COMPLETED",
        "warning_codes": (),
        "error_code": None,
    }


def test_build_operator_operations_reports_empty_state_with_no_retained_task_results() -> None:
    value = build_operator_operations(
        (), latest_due_tasks=(), latest_cycle_id=None, latest_cycle_as_of=None
    )
    assert value["items"] == ()
    assert value["empty_state"] == "NO_RETAINED_TASK_RESULTS"
    assert value["authority"] == "NONE"


def test_build_operator_operations_uses_existing_status_vocabulary_verbatim() -> None:
    rows = [
        _task_result(
            "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
            status="SKIPPED_NOT_DUE",
            completed_at="2026-08-13T00:00:00Z",
        ),
        _task_result(
            "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
            status="COMPLETED",
            completed_at="2026-08-13T20:12:00Z",
            output_identities=("checkpoint-1",),
        ),
        _task_result(
            "COUNTERFACTUAL_FOLLOWUP_REFRESH", status="REUSED", completed_at="2026-08-13T21:00:00Z"
        ),
        _task_result(
            "CRYPTO_UNIVERSE_REFRESH", status="FAILED", completed_at="2026-08-13T22:00:00Z"
        ),
    ]
    value = build_operator_operations(
        rows,
        latest_due_tasks=[
            {
                "task_type": "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
                "disposition": "NOT_DUE",
                "reasons": ("ALREADY_CAPTURED_THIS_SESSION",),
            }
        ],
        latest_cycle_id="cycle-1",
        latest_cycle_as_of="2026-08-13T22:05:00Z",
    )
    # Newest first.
    assert [item["task_type"] for item in value["items"]] == [
        "CRYPTO_UNIVERSE_REFRESH",
        "COUNTERFACTUAL_FOLLOWUP_REFRESH",
        "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
        "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
    ]
    statuses = {item["status"] for item in value["items"]}
    assert statuses == {"SKIPPED_NOT_DUE", "COMPLETED", "REUSED", "FAILED"}

    highlights = {item["task_type"]: item for item in value["highlights"]}
    options_highlight = highlights["OPTIONS_RESEARCH_EVIDENCE_CAPTURE"]
    assert options_highlight["current_disposition"] == "NOT_DUE"
    assert options_highlight["current_reasons"] == ("ALREADY_CAPTURED_THIS_SESSION",)
    assert options_highlight["latest_effective_status"] == "COMPLETED"
    assert options_highlight["latest_effective_output_identities"] == ("checkpoint-1",)

    followup_highlight = highlights["COUNTERFACTUAL_FOLLOWUP_REFRESH"]
    assert followup_highlight["latest_effective_status"] == "REUSED"


def test_build_operator_operations_filters_by_task_type_and_status() -> None:
    rows = [
        _task_result(
            "OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
            status="COMPLETED",
            completed_at="2026-08-13T20:12:00Z",
        ),
        _task_result(
            "CRYPTO_UNIVERSE_REFRESH", status="COMPLETED", completed_at="2026-08-13T20:00:00Z"
        ),
    ]
    value = build_operator_operations(
        rows,
        latest_due_tasks=(),
        latest_cycle_id=None,
        latest_cycle_as_of=None,
        task_type="OPTIONS_RESEARCH_EVIDENCE_CAPTURE",
    )
    assert len(value["items"]) == 1
    assert value["items"][0]["task_type"] == "OPTIONS_RESEARCH_EVIDENCE_CAPTURE"


def test_build_operator_operations_rejects_unknown_status_filter() -> None:
    with pytest.raises(OperatorConsoleFilterError):
        build_operator_operations(
            (), latest_due_tasks=(), latest_cycle_id=None, latest_cycle_as_of=None, status="MADE_UP"
        )


# ==========================================================================
# Overview extensions and Opportunities linkage
# ==========================================================================


def test_build_operator_overview_extensions_is_additive_and_bounded() -> None:
    value = build_operator_overview_extensions(
        decision_coverage={"ledger_eligibility_decision_count": 500},
        followup_coverage={"observation_count": 60},
        followup_plan_summary={
            "policy_version": "FOLLOWUP_POLICY_V2",
            "state_counts": (("DUE", 4),),
        },
        options_summary={
            "campaign_progress_label": "1/20",
            "campaign_sessions_captured": 1,
            "campaign_sessions_target": 20,
            "readiness_state": "CURRENT_SNAPSHOT_AVAILABLE",
            "latest_session": {
                "session_id": "2026-08-13",
                "underlyings_captured_count": 19,
                "structures_retained": 306,
                "created_at": "2026-08-13T20:12:00Z",
            },
        },
        portfolio_summary={
            "portfolio_snapshot": {
                "as_of": "2026-08-13T00:00:00Z",
                "completeness": "PARTIAL_UNKNOWN_SOURCES",
            },
            "mandate": {"version": 1, "effective_at": "2026-08-01T00:00:00Z"},
            "latest_evaluation_count": 3,
        },
    )
    assert value["decision_ledger_summary"]["ledger_eligibility_decision_count"] == 500
    assert value["followup_summary"]["current_policy_version"] == "FOLLOWUP_POLICY_V2"
    assert value["options_research_summary"]["campaign_progress_label"] == "1/20"
    assert value["options_research_summary"]["latest_session"]["session_id"] == "2026-08-13"
    assert value["portfolio_mandate_summary"]["mandate_version"] == 1
    assert value["portfolio_mandate_summary"]["portfolio_completeness"] == "PARTIAL_UNKNOWN_SOURCES"


def test_build_operator_overview_extensions_surfaces_a_compact_followup_progress_slice() -> None:
    """Home gets only a compact 'is it stuck' slice of the full Follow-Up
    page progress evidence -- present when supplied, honestly absent
    (``None``) when the caller omits it entirely (an older caller not yet
    passing it must keep working, never crash)."""

    with_progress = build_operator_overview_extensions(
        decision_coverage={},
        followup_coverage={},
        followup_plan_summary={"policy_version": "FOLLOWUP_POLICY_V2"},
        options_summary={"latest_session": None},
        portfolio_summary={"portfolio_snapshot": None, "mandate": None},
        followup_progress={
            "last_cycle_status": "COMPLETED",
            "last_cycle_completed_at": "2026-08-16T00:49:32Z",
            "last_cycle_created": 11,
            "last_cycle_permanently_unavailable_skipped": 29,
            "currently_permanently_unavailable": 40,
        },
    )
    assert with_progress["followup_summary"]["last_cycle_status"] == "COMPLETED"
    assert with_progress["followup_summary"]["last_cycle_created"] == 11
    assert with_progress["followup_summary"]["currently_permanently_unavailable"] == 40

    without_progress = build_operator_overview_extensions(
        decision_coverage={},
        followup_coverage={},
        followup_plan_summary={},
        options_summary={"latest_session": None},
        portfolio_summary={"portfolio_snapshot": None, "mandate": None},
    )
    assert without_progress["followup_summary"]["last_cycle_status"] is None
    assert without_progress["followup_summary"]["currently_permanently_unavailable"] is None


def test_build_operator_overview_extensions_handles_missing_options_session_honestly() -> None:
    value = build_operator_overview_extensions(
        decision_coverage={},
        followup_coverage={},
        followup_plan_summary={},
        options_summary={"campaign_progress_label": "0/20", "latest_session": None},
        portfolio_summary={
            "portfolio_snapshot": None,
            "mandate": None,
            "latest_evaluation_count": 0,
        },
    )
    assert value["options_research_summary"]["latest_session"] is None
    assert value["portfolio_mandate_summary"]["portfolio_as_of"] is None
    assert value["portfolio_mandate_summary"]["mandate_version"] is None


def test_enrich_opportunities_with_decisions_links_without_changing_row_order_or_other_fields() -> (
    None
):
    payload = {
        "items": (
            {"projection_id": "proj-1", "venue": "CRYPTO"},
            {"projection_id": "proj-2", "venue": "KALSHI"},
        ),
        "total": 2,
        "empty_state": None,
    }
    value = enrich_opportunities_with_decisions(
        payload,
        decisions_by_subject={"proj-1": {"decision_id": "dec-1"}},
    )
    assert [item["projection_id"] for item in value["items"]] == ["proj-1", "proj-2"]
    assert value["items"][0]["counterfactual_decision_id"] == "dec-1"
    assert (
        value["items"][0]["counterfactual_decision_href"]
        == "/api/v1/operator-console/decisions/dec-1"
    )
    assert value["items"][1]["counterfactual_decision_id"] is None
    assert value["items"][1]["counterfactual_decision_href"] is None
    # Every other field and the envelope's own top-level keys survive untouched.
    assert value["items"][0]["venue"] == "CRYPTO"
    assert value["total"] == 2


def test_enrich_opportunities_with_decisions_handles_empty_items() -> None:
    value = enrich_opportunities_with_decisions(
        {"items": (), "total": 0, "empty_state": "NO_RETAINED_OPPORTUNITIES"},
        decisions_by_subject={},
    )
    assert value["items"] == ()
    assert value["empty_state"] == "NO_RETAINED_OPPORTUNITIES"
