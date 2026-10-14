"""Genuine zero-intent and isolated deterministic LIVE-preflight acceptance."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

from apex.adapters.cross_venue_allocation_acceptance import (
    run_cross_venue_fixture_acceptance,
)
from apex.application.live_preflight import (
    ApproveCoinbaseProviderPreview,
    CoinbasePreviewTransport,
    ConstructLiveIntent,
    EvaluateLiveIntentEligibility,
    InMemoryLivePreflightRepository,
    LivePreflightRepository,
    MaterializeLivePreflightAssessment,
    RetainKalshiPreviewUnavailable,
    RunApprovedCoinbaseProviderPreview,
    RunInternalPreflight,
    live_preflight_workspace,
)
from apex.domain.cross_venue_allocation import (
    CrossVenueAllocationDecision,
    CrossVenueAllocationLine,
    PaperTrialProposal,
)
from apex.domain.live_preflight import (
    InternalPreflight,
    InternalPreflightInputs,
    LiveIntentEligibilityInputs,
    LiveIntentProposal,
    LiveOrderType,
    LivePreflightAssessment,
)
from apex.domain.live_readiness import (
    ExecutionControlState,
    Provider,
    ReadinessRecommendation,
)
from apex.domain.shared_opportunity import EligibilityDecision, SharedOpportunityProjection
from apex.domain.values import content_digest

ACCEPTANCE_AT = datetime(2026, 8, 4, 12, tzinfo=UTC)
FIXTURE_HASH = content_digest(("LIVE_PREFLIGHT_FIXTURE", 1))


class DeterministicCoinbasePreviewFixture(CoinbasePreviewTransport):
    """A no-network preview transport that records the exact request."""

    def __init__(self, response: Mapping[str, object], status_code: int = 200) -> None:
        self.response = response
        self.status_code = status_code
        self.requests: list[tuple[str, str, str, Mapping[str, object], str, int]] = []

    def preview(
        self,
        *,
        method: str,
        host: str,
        path: str,
        body: Mapping[str, object],
        credential_reference_id: str,
        timeout_seconds: int,
    ) -> tuple[int, Mapping[str, object]]:
        self.requests.append((method, host, path, body, credential_reference_id, timeout_seconds))
        return self.status_code, self.response


def _sources(
    venue: str,
) -> tuple[
    SharedOpportunityProjection,
    EligibilityDecision,
    CrossVenueAllocationDecision,
    CrossVenueAllocationLine,
    PaperTrialProposal,
]:
    values = run_cross_venue_fixture_acceptance()
    projections = cast(tuple[SharedOpportunityProjection, ...], values["projections"])
    eligibility = cast(tuple[EligibilityDecision, ...], values["eligibility"])
    decision = cast(CrossVenueAllocationDecision, values["decision"])
    lines = cast(tuple[CrossVenueAllocationLine, ...], values["lines"])
    proposals = cast(tuple[PaperTrialProposal, ...], values["paper_trial_proposals"])
    index = next(index for index, item in enumerate(projections) if item.venue.value == venue)
    line = next(item for item in lines if item.projection_id == projections[index].projection_id)
    proposal = next(item for item in proposals if item.allocation_line_id == line.line_id)
    return projections[index], eligibility[index], decision, line, proposal


def _repository_with_intent(
    *, venue: str = "CRYPTO", suffix: str = "passing"
) -> tuple[InMemoryLivePreflightRepository, LiveIntentProposal]:
    projection, eligibility, decision, line, proposal = _sources(venue)
    repository = InMemoryLivePreflightRepository()
    provider = Provider.COINBASE if venue == "CRYPTO" else Provider.KALSHI
    intent = ConstructLiveIntent(repository).execute(
        intent_id=f"fixture-live-intent-{suffix}",
        projection=projection,
        shared_eligibility=eligibility,
        allocation=decision,
        allocation_line=line,
        paper_trial_proposal=proposal,
        provider=provider,
        account_scope_id=f"fixture-{venue.lower()}-live-account",
        side="BUY" if venue == "CRYPTO" else "BUY_YES",
        order_type=LiveOrderType.LIMIT,
        requested_quantity="0.10" if venue == "CRYPTO" else "2",
        requested_notional=None,
        limit_price="10.00" if venue == "CRYPTO" else "0.50",
        maximum_risk="2",
        strategy_or_method_version=f"{projection.strategy_or_method_id}:V1",
        capability_observed_at=ACCEPTANCE_AT,
        created_at=ACCEPTANCE_AT,
        expires_at=ACCEPTANCE_AT + timedelta(hours=1),
        actor="operator:fixture",
        confirmation=f"CONSTRUCT_LIVE_INTENT:fixture-live-intent-{suffix}",
    )
    return repository, intent


def _eligibility_inputs(**changes: object) -> LiveIntentEligibilityInputs:
    values: dict[str, object] = {
        "source_currently_eligible": True,
        "allocation_nonzero": True,
        "trial_proposal_lineage_valid": True,
        "capability_current": True,
        "account_constraints_available": True,
        "product_constraints_available": True,
        "fee_evidence_available": True,
        "minimum_evidence_available": True,
        "liquidity_sufficient": True,
        "strategy_health_state": "HEALTHY",
        "paper_sample_count": 30,
        "required_paper_sample_count": 30,
        "reconciliation_quality": "CLEAN",
        "operating_cycle_health": "HEALTHY",
        "backup_restore_verified": True,
        "execution_control": ExecutionControlState.LIVE_PILOT_ARMED,
        "safe_provider_permissions": True,
        "live_risk_passed": True,
        "explicit_operator_review": True,
        "evaluated_at": ACCEPTANCE_AT + timedelta(minutes=1),
    }
    values.update(changes)
    return LiveIntentEligibilityInputs(**values)  # type: ignore[arg-type]


def _preflight_inputs(*, preview: bool = True, **changes: object) -> InternalPreflightInputs:
    values: dict[str, object] = {
        "reference_price": "10",
        "quantity_increment": "0.01",
        "price_increment": "0.01",
        "minimum_quantity": "0.01",
        "minimum_notional": "1",
        "fee_rate": "0.001",
        "slippage_rate": "0.001",
        "available_balance": "100",
        "displayed_liquidity_quantity": "10",
        "current_total_live_capital": "0",
        "maximum_total_live_capital": "100",
        "current_strategy_exposure": "0",
        "maximum_strategy_exposure": "25",
        "current_instrument_exposure": "0",
        "maximum_instrument_exposure": "25",
        "current_venue_exposure": "0",
        "maximum_venue_exposure": "40",
        "current_daily_loss": "0",
        "maximum_daily_loss": "25",
        "current_drawdown_ratio": "0",
        "maximum_drawdown_ratio": "0.10",
        "open_order_count": 0,
        "maximum_open_orders": 1,
        "open_position_count": 0,
        "maximum_open_positions": 1,
        "cash_before": "100",
        "minimum_retained_cash_ratio": "0.90",
        "provider_preview_supported": preview,
        "evaluated_at": ACCEPTANCE_AT + timedelta(minutes=2),
    }
    values.update(changes)
    return InternalPreflightInputs(**values)  # type: ignore[arg-type]


def _run_internal(
    *,
    venue: str = "CRYPTO",
    suffix: str,
    eligibility_changes: Mapping[str, object] | None = None,
    preflight_changes: Mapping[str, object] | None = None,
    preview_supported: bool = True,
) -> tuple[InMemoryLivePreflightRepository, LiveIntentProposal, InternalPreflight]:
    repository, intent = _repository_with_intent(venue=venue, suffix=suffix)
    eligibility = EvaluateLiveIntentEligibility(repository).execute(
        decision_id=f"fixture-live-eligibility-{suffix}",
        intent_id=intent.intent_id,
        inputs=_eligibility_inputs(**dict(eligibility_changes or {})),
    )
    preflight = RunInternalPreflight(repository).execute(
        preflight_id=f"fixture-internal-preflight-{suffix}",
        intent_id=intent.intent_id,
        eligibility_decision_id=eligibility.decision_id,
        capability_snapshot_id=f"fixture-capability-{suffix}",
        capability_snapshot_hash=FIXTURE_HASH,
        readiness_assessment_id=f"fixture-readiness-{suffix}",
        readiness_assessment_hash=FIXTURE_HASH,
        readiness_recommendation=ReadinessRecommendation.LIVE_REVIEW_REQUIRED,
        risk_decision_id=f"fixture-risk-{suffix}",
        risk_decision_hash=FIXTURE_HASH,
        inputs=_preflight_inputs(
            preview=preview_supported,
            **dict(preflight_changes or {}),
        ),
    )
    return repository, intent, preflight


def run_fixture_live_preflight_acceptance() -> dict[str, LivePreflightAssessment]:
    """Exercise every required branch without network, financial state, or authority."""

    scenarios: dict[str, LivePreflightAssessment] = {}
    eligibility_branches: dict[str, dict[str, object]] = {
        "stale_opportunity": {"source_currently_eligible": False},
        "unavailable_liquidity": {"liquidity_sufficient": False},
        "failed_strategy_health": {"strategy_health_state": "BLOCKED"},
        "unresolved_reconciliation": {"reconciliation_quality": "UNRESOLVED"},
        "unhealthy_operating_cycle": {"operating_cycle_health": "DEGRADED"},
        "emergency_stop": {"execution_control": ExecutionControlState.EMERGENCY_STOP},
        "dangerous_provider_permissions": {"safe_provider_permissions": False},
    }
    preflight_branches: dict[str, dict[str, object]] = {
        "insufficient_balance": {"available_balance": "0.50"},
        "minimum_size_failure": {"minimum_quantity": "1"},
        "precision_failure": {"quantity_increment": "0.03"},
    }
    for name, changes in eligibility_branches.items():
        repository, _, preflight = _run_internal(
            suffix=name,
            eligibility_changes=changes,
        )
        scenarios[name] = MaterializeLivePreflightAssessment(repository).execute(
            assessment_id=f"fixture-preflight-assessment-{name}",
            preflight_id=preflight.preflight_id,
            preview_capture_id=None,
            assessed_at=ACCEPTANCE_AT + timedelta(minutes=3),
        )
    for name, changes in preflight_branches.items():
        repository, _, preflight = _run_internal(
            suffix=name,
            preflight_changes=changes,
        )
        scenarios[name] = MaterializeLivePreflightAssessment(repository).execute(
            assessment_id=f"fixture-preflight-assessment-{name}",
            preflight_id=preflight.preflight_id,
            preview_capture_id=None,
            assessed_at=ACCEPTANCE_AT + timedelta(minutes=3),
        )

    repository, intent, preflight = _run_internal(
        venue="KALSHI",
        suffix="preview-unavailable",
        preview_supported=False,
        preflight_changes={
            "reference_price": "0.50",
            "quantity_increment": "1",
            "price_increment": "0.01",
        },
    )
    unavailable = RetainKalshiPreviewUnavailable(repository).execute(
        capture_id="fixture-kalshi-preview-unavailable",
        intent_id=intent.intent_id,
        captured_at=ACCEPTANCE_AT + timedelta(minutes=3),
    )
    scenarios["preview_unavailable"] = MaterializeLivePreflightAssessment(repository).execute(
        assessment_id="fixture-preflight-assessment-preview-unavailable",
        preflight_id=preflight.preflight_id,
        preview_capture_id=unavailable.capture_id,
        assessed_at=ACCEPTANCE_AT + timedelta(minutes=3),
    )

    for name, product_id in (("preview_mismatch", "WRONG-USD"), ("fully_passing", None)):
        repository, intent, preflight = _run_internal(suffix=name)
        approval = ApproveCoinbaseProviderPreview(repository).execute(
            approval_id=f"fixture-preview-approval-{name}",
            credential_reference_id="fixture-coinbase-preview-view-reference",
            intent_id=intent.intent_id,
            maximum_request_count=1,
            timeout_seconds=5,
            approved_at=ACCEPTANCE_AT + timedelta(minutes=2),
            expires_at=ACCEPTANCE_AT + timedelta(minutes=10),
            actor="operator:fixture",
            reason="Isolated deterministic provider preview.",
            idempotency_key=f"fixture-preview-approval-{name}",
            confirmation=(
                f"APPROVE_COINBASE_PROVIDER_PREVIEW:fixture-preview-approval-{name}:"
                f"{intent.intent_id}"
            ),
        )
        response: dict[str, object] = {
            "commission_total": "0.001",
            "errs": [],
            "warning": [],
            "base_size": "0.10",
            "best_bid": "9.99",
            "best_ask": "10.00",
            "order_total": "1.001",
            "slippage": "0.001",
            "authorization": "Bearer fixture-must-redact",  # pragma: allowlist secret
        }
        if product_id is not None:
            response["product_id"] = product_id
        transport = DeterministicCoinbasePreviewFixture(response)
        preview = (
            RunApprovedCoinbaseProviderPreview(repository, transport)
            .execute(
                capture_id=f"fixture-preview-capture-{name}",
                approval_id=approval.approval_id,
                preflight_id=preflight.preflight_id,
                requested_at=ACCEPTANCE_AT + timedelta(minutes=3),
            )
            .capture
        )
        scenarios[name] = MaterializeLivePreflightAssessment(repository).execute(
            assessment_id=f"fixture-preflight-assessment-{name}",
            preflight_id=preflight.preflight_id,
            preview_capture_id=preview.capture_id,
            assessed_at=ACCEPTANCE_AT + timedelta(minutes=3),
        )
    scenarios["valid_coinbase_spot_intent"] = scenarios["fully_passing"]
    scenarios["valid_kalshi_defined_risk_intent"] = scenarios["preview_unavailable"]
    return scenarios


def run_genuine_zero_intent_acceptance(
    repository: LivePreflightRepository | None = None,
) -> dict[str, object]:
    """Current genuine acceptance: retain no invented intent and make no request."""

    retained = repository or InMemoryLivePreflightRepository()
    workspace = live_preflight_workspace(retained, as_of=ACCEPTANCE_AT)
    if retained.intents():
        raise RuntimeError("GENUINE_ZERO_INTENT_ACCEPTANCE_FOUND_UNEXPECTED_INTENT")
    return {
        "intent_count": 0,
        "eligible_intent_count": 0,
        "nonzero_allocation_count": 0,
        "private_capability_capture_count": 0,
        "provider_request_count": 0,
        "provider_write_count": 0,
        "execution_control": "LIVE_BLOCKED",
        "readiness": "LIVE_NOT_READY",
        "result": "ZERO_INTENT_BLOCKED",
        "operating_cycle_tasks": workspace["operating_cycle_tasks"],
        "authority": "NONE",
        "live_authority": "NONE",
    }
