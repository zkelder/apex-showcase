from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.application.sustained_shadow import (
    InMemorySustainedShadowRepository,
    SustainedShadowService,
)
from apex.domain.sustained_shadow import ShadowCaptureState, SustainedShadowPlan


def _plan() -> SustainedShadowPlan:
    return SustainedShadowPlan.draft(
        universe_id="u",
        universe_hash="a" * 64,
        feature_schema_hash="b" * 64,
        intelligence_run_id="i",
        ensemble_run_id="e",
        blockers=("PBO_ABOVE_MAXIMUM", "MATERIAL_SIMULATOR_MISMATCH"),
        operator_action_id="operator",
        idempotency_key="plan",
    )


def test_capture_requires_confirmation_complete_universe_and_is_idempotent() -> None:
    repo = InMemorySustainedShadowRepository()
    service = SustainedShadowService(repo)
    draft = service.create(_plan())
    with pytest.raises(ValueError, match="EXACT_CONFIRMATION"):
        service.activate(draft, "wrong")
    plan = service.activate(draft, draft.plan_id)
    rows = tuple(
        (f"I{n}", Decimal("0.5"), Decimal("0.01"), Decimal("0.1"), "c" * 64) for n in range(16)
    )
    assert (
        service.capture(
            plan,
            session_id="s1",
            received_at=datetime(2026, 7, 28, tzinfo=UTC),
            capture_hash="d" * 64,
            rows=rows,
        ).state
        is ShadowCaptureState.CAPTURED
    )
    assert len(repo.predictions(plan.plan_id)) == 16
    assert (
        service.capture(
            repo.plans[plan.plan_id],
            session_id="s1",
            received_at=datetime(2026, 7, 28, tzinfo=UTC),
            capture_hash="d" * 64,
            rows=rows,
        ).state
        is ShadowCaptureState.NO_NEW_SESSION
    )
    assert (
        service.capture(
            repo.plans[plan.plan_id],
            session_id="s2",
            received_at=datetime(2026, 7, 28, tzinfo=UTC),
            capture_hash="d" * 64,
            rows=rows[:15],
        ).state
        is ShadowCaptureState.PARTIAL
    )
    paused = service.pause(repo.plans[plan.plan_id], "operator review")
    assert paused.state.value == "PAUSED"
    resumed = service.resume(paused, paused.plan_id)
    assert resumed.state.value == "ACTIVE"
    outcomes = service.resolve(
        resumed,
        cutoff=datetime(2026, 8, 5, tzinfo=UTC),
        realized_returns={"s1:PLUS_FIVE": Decimal("0.02")},
    )
    assert len(outcomes) == 16
    assert service.report(resumed)["resolved_outcome_count"] == 16
    assert service.revoke(resumed, resumed.plan_id).state.value == "REVOKED"


def test_draft_successor_retains_parent_lineage_without_predictions_or_watermark() -> None:
    parent = _plan()
    successor = SustainedShadowPlan.draft(
        universe_id=parent.universe_id,
        universe_hash=parent.universe_hash,
        feature_schema_hash=parent.feature_schema_hash,
        intelligence_run_id=parent.intelligence_run_id,
        ensemble_run_id=parent.ensemble_run_id,
        blockers=("PBO_ABOVE_MAXIMUM",),
        operator_action_id="v2",
        idempotency_key="v2",
        parent_plan_id=parent.plan_id,
    )

    assert successor.parent_plan_id == parent.plan_id
    assert successor.watermark is None
    assert successor.state.value == "DRAFT"
    assert successor.plan_id != parent.plan_id
